from ..models import ModelEndpointProfile
from .base import ModelResponse


class BedrockAdapter:
    """Amazon Bedrock Converse. Credentials come from the default AWS chain (`aws:default-chain`)."""

    def __init__(self, profile: ModelEndpointProfile, client=None):
        if profile.api_dialect != "bedrock-converse":
            raise ValueError(f"Profile {profile.id} is not a bedrock-converse profile")
        if profile.credential_ref != "aws:default-chain":
            raise ValueError(f"Unsupported credential_ref: {profile.credential_ref}")
        self.profile = profile
        self._client = client

    def _get_client(self, timeout_seconds: float):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # optional extra
                raise RuntimeError("Install the 'bedrock' extra: pip install -e '.[bedrock]'") from exc
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.profile.endpoint.region,
                config=Config(
                    read_timeout=timeout_seconds,
                    connect_timeout=min(timeout_seconds, 10),
                    retries={"max_attempts": self.profile.max_retries + 1, "mode": "standard"},
                ),
            )
        return self._client

    def converse(self, *, system: str, user: str, max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        reply = self._get_client(timeout_seconds).converse(
            modelId=self.profile.endpoint.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_output_tokens},
        )
        blocks = reply["output"]["message"]["content"]
        return ModelResponse(
            text="".join(b["text"] for b in blocks if "text" in b),
            stop_reason=reply["stopReason"],
            input_tokens=reply["usage"]["inputTokens"],
            output_tokens=reply["usage"]["outputTokens"],
        )
