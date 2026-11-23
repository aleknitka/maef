"""S3-compatible `ArtifactStore`: content-addressed objects `<prefix>sha256/<aa>/<rest>`.

boto3 is an optional extra (`pip install -e '.[s3]'`) and is imported lazily. Objects are written once:
an existing object of the same size is trusted as a dedupe hit (reads re-verify the sha256), a size
mismatch raises ArtifactCorrupt and is never overwritten. Credentials come from the boto3 default chain
or from `env:` references resolved through `Credentials`; secrets are never logged or put in errors.
"""
import base64
import hashlib
import ipaddress
from urllib.parse import urlparse

from .artifacts import (
    ArtifactCorrupt,
    ArtifactError,
    ArtifactNotFound,
    ArtifactRef,
    _digest_of,
    _verified,
    clean_mime,
    clean_name,
    sha256_digest,
)
from .credentials import Credentials
from .egress import EgressPolicy

DEFAULT_CREDENTIALS_REF = "aws:default-chain"
# Codes S3-compatible servers return when they do not understand additional checksums.
_CHECKSUM_REJECTED = {"InvalidRequest", "InvalidArgument", "NotImplemented", "XNotImplemented",
                      "UnsupportedOperation", "MissingContentLength", "InvalidDigest", "BadDigest",
                      "MalformedXML", "SignatureDoesNotMatch"}


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _status(exc) -> int | None:
    return getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")


def _code(exc) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


class S3ArtifactStore:
    def __init__(self, bucket: str, *, prefix: str = "artifacts/", endpoint_url: str | None = None,
                 region: str | None = None, client=None, credentials_ref: str = DEFAULT_CREDENTIALS_REF,
                 secret_ref: str | None = None, addressing_style: str = "auto", sse: bool = True,
                 allow_private: bool = False, credentials: Credentials | None = None,
                 egress: EgressPolicy | None = None):
        if not bucket:
            raise ValueError("bucket is required")
        if addressing_style not in ("auto", "path", "virtual"):
            raise ValueError("addressing_style must be auto, path or virtual")
        self.bucket = bucket
        self.prefix = (prefix.strip("/") + "/") if prefix.strip("/") else ""
        self.endpoint_url = endpoint_url
        self.sse = sse
        self._checksum = True  # cleared if the server rejects additional checksums
        if endpoint_url:
            self._check_egress(endpoint_url, allow_private, egress or EgressPolicy())
        self._client = client if client is not None else self._make_client(
            endpoint_url, region, credentials_ref, secret_ref, addressing_style, credentials or Credentials())

    @staticmethod
    def _check_egress(url: str, allow_private: bool, policy: EgressPolicy) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            from .egress import EgressDenied
            raise EgressDenied("egress_denied:bad_url")
        policy.check(url, allow_loopback=_is_loopback(parsed.hostname), allow_private=allow_private)

    @staticmethod
    def _make_client(endpoint_url, region, credentials_ref, secret_ref, addressing_style, credentials):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError("S3 artifact store needs boto3: pip install -e '.[s3]'") from exc
        options = {"signature_version": "s3v4", "s3": {"addressing_style": addressing_style},
                   "connect_timeout": 5, "read_timeout": 30, "retries": {"mode": "standard", "total_max_attempts": 3}}
        try:  # newer botocore adds CRC checksums by default, which many S3-compatible servers reject
            config = Config(request_checksum_calculation="when_required",
                            response_checksum_validation="when_required", **options)
        except TypeError:  # pragma: no cover - older botocore
            config = Config(**options)
        kwargs: dict = {"config": config}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if region:
            kwargs["region_name"] = region
        if credentials_ref != DEFAULT_CREDENTIALS_REF:
            key = credentials.resolve(credentials_ref)
            secret = credentials.resolve(secret_ref) if secret_ref else None
            if key is None or secret is None:
                raise ArtifactError("s3_credentials_incomplete")
            kwargs.update(aws_access_key_id=key.reveal(), aws_secret_access_key=secret.reveal())
        return boto3.client("s3", **kwargs)

    # --- keys -----------------------------------------------------------------------------------
    def _key(self, digest: str) -> str:
        hex_ = digest.removeprefix("sha256:")
        return f"{self.prefix}sha256/{hex_[:2]}/{hex_[2:]}"

    def _head(self, key: str) -> dict | None:
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _status(exc) == 404 or _code(exc) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise ArtifactError("store_unavailable") from None
        except BotoCoreError:
            raise ArtifactError("store_unavailable") from None

    # --- ArtifactStore --------------------------------------------------------------------------
    def put(self, data: bytes, *, name: str, mime: str) -> ArtifactRef:
        from botocore.exceptions import BotoCoreError, ClientError
        ref = ArtifactRef(digest=sha256_digest(data), size=len(data), mime=mime, name=name)
        key = self._key(ref.digest)
        existing = self._head(key)
        if existing is not None:
            if existing.get("ContentLength") != len(data):
                raise ArtifactCorrupt()  # never overwrite an object whose content differs
            return ref
        raw = hashlib.sha256(data).digest()
        for with_checksum in ((True, False) if self._checksum else (False,)):
            params = {"Bucket": self.bucket, "Key": key, "Body": data, "ContentType": clean_mime(mime),
                      "Metadata": {"maef-name": clean_name(name)}}
            if self.sse:
                params["ServerSideEncryption"] = "AES256"
            if with_checksum:
                params.update(ChecksumAlgorithm="SHA256", ChecksumSHA256=base64.b64encode(raw).decode())
            try:
                self._client.put_object(**params)
                return ref
            except ClientError as exc:
                if with_checksum and _code(exc) in _CHECKSUM_REJECTED:
                    self._checksum = False
                    continue
                raise ArtifactError("store_unavailable") from None
            except BotoCoreError:
                raise ArtifactError("store_unavailable") from None
        raise ArtifactError("store_unavailable")  # pragma: no cover

    def read(self, ref_or_digest: ArtifactRef | str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError
        digest = _digest_of(ref_or_digest)
        try:
            body = self._client.get_object(Bucket=self.bucket, Key=self._key(digest))["Body"]
            try:
                data = body.read()
            finally:
                body.close()
        except ClientError as exc:
            if _status(exc) == 404 or _code(exc) in ("404", "NoSuchKey", "NotFound"):
                raise ArtifactNotFound() from None
            raise ArtifactError("store_unavailable") from None
        except BotoCoreError:
            raise ArtifactError("store_unavailable") from None
        return _verified(digest, data)

    def exists(self, digest: str) -> bool:
        try:
            digest = _digest_of(digest)
        except ArtifactNotFound:
            return False
        return self._head(self._key(digest)) is not None
