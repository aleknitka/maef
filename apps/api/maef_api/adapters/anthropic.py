from ..models import AnthropicEndpoint, ModelEndpointProfile
from .base import ModelResponse
from .http import ModelHTTPError, api_key, post_json


class AnthropicAdapter:
    """Anthropic Messages API."""

    def __init__(self, profile: ModelEndpointProfile, sleep=None):
        if not isinstance(profile.endpoint, AnthropicEndpoint):
            raise TypeError(f"Profile {profile.id} is not an anthropic profile")
        self.profile = profile
        self._extra = {"sleep": sleep} if sleep else {}

    def converse(self, *, system: str, user: str, max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        ep = self.profile.endpoint
        key = api_key(self.profile.credential_ref)
        if key is None:
            raise ModelHTTPError(None, "missing_credential")
        reply = post_json(
            f"{ep.base_url}/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {"model": ep.model_id, "max_tokens": max_output_tokens, "system": system,
             "messages": [{"role": "user", "content": user}]},
            timeout=timeout_seconds, max_retries=self.profile.max_retries, **self._extra,
        )
        return ModelResponse(
            text="".join(b.get("text", "") for b in reply["content"] if b.get("type") == "text"),
            stop_reason=reply["stop_reason"],
            input_tokens=reply["usage"]["input_tokens"],
            output_tokens=reply["usage"]["output_tokens"],
        )
