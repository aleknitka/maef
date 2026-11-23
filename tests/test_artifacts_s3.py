"""S3-specific behaviour of S3ArtifactStore. The generic store contract runs against S3 in test_artifacts.py."""
import base64
import hashlib

import boto3
import pytest
from botocore.config import Config
from botocore.stub import ANY, Stubber
from maef_api import main
from maef_api.artifacts import (
    ArtifactCorrupt,
    ArtifactNotFound,
    LocalArtifactStore,
    default_artifact_store,
    sha256_digest,
)
from maef_api.artifacts_s3 import S3ArtifactStore
from maef_api.channel import LocalChannel
from maef_api.credentials import Credentials
from maef_api.egress import EgressDenied
from maef_api.ledger import Ledger
from maef_api.runs import Runs
from test_artifacts import MarkdownAnswer
from test_worker import admit, run_once


def stubbed_client():
    client = boto3.client('s3', region_name='us-east-1', endpoint_url='http://127.0.0.1:1', aws_access_key_id='k',
                          aws_secret_access_key='s', config=Config(s3={'addressing_style': 'path'}))
    return client, Stubber(client)


def key_of(store, digest):
    hex_ = digest.removeprefix('sha256:')
    return f'{store.prefix}sha256/{hex_[:2]}/{hex_[2:]}'


# --- stubbed: request shape ---

def test_put_sends_checksum_sse_content_type_and_metadata():
    client, stub = stubbed_client()
    store = S3ArtifactStore('bkt', prefix='p', client=client)
    digest = hashlib.sha256(b'hello').digest()
    stub.add_client_error('head_object', service_error_code='404', http_status_code=404)
    stub.add_response('put_object', {}, expected_params={
        'Bucket': 'bkt', 'Key': f'p/sha256/{digest.hex()[:2]}/{digest.hex()[2:]}', 'Body': b'hello',
        'ContentType': 'text/markdown', 'Metadata': {'maef-name': 'a.md'}, 'ServerSideEncryption': 'AES256',
        'ChecksumAlgorithm': 'SHA256', 'ChecksumSHA256': base64.b64encode(digest).decode()})
    with stub:
        store.put(b'hello', name='../a.md', mime='text/markdown; charset=utf-8')
        stub.assert_no_pending_responses()


def test_sse_can_be_disabled():
    client, stub = stubbed_client()
    store = S3ArtifactStore('bkt', client=client, sse=False)
    stub.add_client_error('head_object', service_error_code='404', http_status_code=404)
    stub.add_response('put_object', {}, expected_params={
        'Bucket': 'bkt', 'Key': ANY, 'Body': b'x', 'ContentType': 'application/octet-stream',
        'Metadata': {'maef-name': 'x'}, 'ChecksumAlgorithm': 'SHA256', 'ChecksumSHA256': ANY})
    with stub:
        store.put(b'x', name='x', mime='')
        stub.assert_no_pending_responses()


def test_checksum_rejection_falls_back_and_is_remembered():
    client, stub = stubbed_client()
    store = S3ArtifactStore('bkt', client=client, sse=False)
    stub.add_client_error('head_object', service_error_code='404', http_status_code=404)
    stub.add_client_error('put_object', service_error_code='NotImplemented', http_status_code=501)
    stub.add_response('put_object', {}, expected_params={
        'Bucket': 'bkt', 'Key': ANY, 'Body': b'one', 'ContentType': 'application/octet-stream',
        'Metadata': {'maef-name': 'one'}})
    stub.add_client_error('head_object', service_error_code='404', http_status_code=404)
    stub.add_response('put_object', {}, expected_params={
        'Bucket': 'bkt', 'Key': ANY, 'Body': b'two', 'ContentType': 'application/octet-stream',
        'Metadata': {'maef-name': 'two'}})
    with stub:
        store.put(b'one', name='one', mime='')
        store.put(b'two', name='two', mime='')
        stub.assert_no_pending_responses()


def test_existing_object_of_other_size_is_corrupt_and_not_overwritten():
    client, stub = stubbed_client()
    store = S3ArtifactStore('bkt', client=client)
    stub.add_response('head_object', {'ContentLength': 99})  # no put_object queued: a PUT would fail
    with stub, pytest.raises(ArtifactCorrupt):
        store.put(b'hello', name='a', mime='text/plain')


def test_server_errors_surface_as_stable_code_without_detail():
    client, stub = stubbed_client()
    store = S3ArtifactStore('bkt', client=client)
    stub.add_client_error('get_object', service_error_code='AccessDenied', http_status_code=403,
                          service_message='secret-detail')
    with stub, pytest.raises(Exception) as err:
        store.read(sha256_digest(b'x'))
    assert err.value.code == 'store_unavailable' and 'secret-detail' not in str(err.value)


# --- egress and construction ---

@pytest.mark.parametrize('endpoint', ['http://169.254.169.254', 'http://10.1.2.3:9000', 'http://192.168.0.5'])
def test_private_and_metadata_endpoints_are_refused(endpoint):
    with pytest.raises(EgressDenied):
        S3ArtifactStore('bkt', endpoint_url=endpoint)


def test_metadata_endpoint_is_refused_even_with_allow_private():
    with pytest.raises(EgressDenied):
        S3ArtifactStore('bkt', endpoint_url='http://169.254.169.254', allow_private=True)


def test_private_endpoint_allowed_when_opted_in_and_loopback_by_default():
    S3ArtifactStore('bkt', endpoint_url='http://10.1.2.3:9000', allow_private=True,
                    region='us-east-1', credentials_ref='env:K', secret_ref='env:S',
                    credentials=Credentials({'K': 'a', 'S': 'b'}))
    S3ArtifactStore('bkt', endpoint_url='http://127.0.0.1:9', region='us-east-1')


def test_missing_credential_reference_names_only_the_variable():
    from maef_api.credentials import CredentialError
    with pytest.raises(CredentialError) as err:
        S3ArtifactStore('bkt', endpoint_url='http://127.0.0.1:9', credentials_ref='env:NOPE_K', secret_ref='env:NOPE_S',
                        credentials=Credentials({}))
    assert 'NOPE_K' in str(err.value)


def test_default_store_selects_s3_from_url(monkeypatch):
    monkeypatch.setenv('MAEF_ARTIFACTS_URL', 's3://my-bucket/some/prefix')
    monkeypatch.setenv('MAEF_S3_ENDPOINT_URL', 'http://127.0.0.1:9')
    monkeypatch.setenv('MAEF_S3_REGION', 'eu-north-1')
    monkeypatch.setenv('MAEF_S3_ADDRESSING_STYLE', 'path')
    monkeypatch.setenv('MAEF_S3_SSE', '0')
    store = default_artifact_store()
    assert isinstance(store, S3ArtifactStore)
    assert (store.bucket, store.prefix, store.sse) == ('my-bucket', 'some/prefix/', False)


def test_default_store_is_local_without_url(monkeypatch, tmp_path):
    monkeypatch.delenv('MAEF_ARTIFACTS_URL', raising=False)
    monkeypatch.setenv('MAEF_ARTIFACTS_DIR', str(tmp_path))
    assert isinstance(default_artifact_store(), LocalArtifactStore)


def test_default_store_rejects_non_s3_url(monkeypatch):
    monkeypatch.setenv('MAEF_ARTIFACTS_URL', 'http://bucket/x')
    with pytest.raises(ValueError):
        default_artifact_store()


# --- live S3-compatible server ---

@pytest.mark.s3
def test_layout_content_type_and_metadata_on_server(s3_store):
    ref = s3_store.put(b'# hi', name='dir/report.md', mime='text/markdown')
    head = s3_store._client.head_object(Bucket=s3_store.bucket, Key=key_of(s3_store, ref.digest))
    assert head['ContentLength'] == 4
    assert head['ContentType'] == 'text/markdown'
    assert head['Metadata'] == {'maef-name': 'report.md'}


@pytest.mark.s3
def test_second_put_does_not_upload_again(s3_store):
    uploads = []
    s3_store._client.meta.events.register('before-call.s3.PutObject', lambda **kw: uploads.append(1))
    s3_store.put(b'same', name='a', mime='text/plain')
    s3_store.put(b'same', name='b', mime='text/plain')
    assert len(uploads) == 1


@pytest.mark.s3
def test_tampered_object_is_corrupt_and_missing_is_not_found(s3_store):
    ref = s3_store.put(b'hello', name='a', mime='text/plain')
    s3_store._client.put_object(Bucket=s3_store.bucket, Key=key_of(s3_store, ref.digest), Body=b'other')
    with pytest.raises(ArtifactCorrupt):
        s3_store.read(ref)
    with pytest.raises(ArtifactNotFound):
        s3_store.read(sha256_digest(b'never stored'))
    assert s3_store.exists(sha256_digest(b'never stored')) is False


@pytest.mark.s3
def test_put_over_tampered_object_of_other_size_is_corrupt(s3_store):
    ref = s3_store.put(b'hello', name='a', mime='text/plain')
    s3_store._client.put_object(Bucket=s3_store.bucket, Key=key_of(s3_store, ref.digest), Body=b'tampered!')
    with pytest.raises(ArtifactCorrupt):
        s3_store.put(b'hello', name='a', mime='text/plain')


@pytest.mark.s3
def test_worker_output_lands_in_s3_and_downloads_over_http(s3_store, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    ledger = Ledger(tmp_path / 'e2e.db')
    ledger.ensure_account('default', 1_000_000)
    run_id = admit(ledger)
    channel = LocalChannel(ledger, artifacts=s3_store)
    assert run_once(channel, MarkdownAnswer()) is True
    output = next(e.payload for e in ledger.get(run_id).events if e.type == 'run.output')
    assert s3_store._client.get_object(Bucket=s3_store.bucket, Key=key_of(s3_store, output['digest']))['Body'].read() \
        == MarkdownAnswer.TEXT.encode()

    monkeypatch.setattr(main, 'ledger', ledger)
    monkeypatch.setattr(main, 'runs', Runs(main.runs.definitions, ledger, execution_enabled=lambda: True,
                                           allow_dirty=lambda: True))
    monkeypatch.setattr(main, 'artifacts', s3_store)
    got = TestClient(main.app).get(f"/api/v1/artifacts/{output['digest']}")
    assert got.status_code == 200 and got.content == MarkdownAnswer.TEXT.encode()
    assert got.headers['content-type'].startswith('text/markdown')
