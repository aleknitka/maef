from ..models import ModelEndpointProfile, OpenAICompatibleEndpoint
from .base import ModelResponse
from .http import ModelHTTPError, api_key, post_json

_STOP = {"stop": "end_turn", "length": "max_tokens"}


class OpenAICompatibleAdapter:
    def __init__(self, profile: ModelEndpointProfile, sleep=None):
        if not isinstance(profile.endpoint, OpenAICompatibleEndpoint):
            raise TypeError(f"Profile {profile.id} is not an openai-compatible profile")
        self.profile = profile
        self._extra = {"sleep": sleep} if sleep else {}

    def converse(self, *, system: str, user: str, max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        ep = self.profile.endpoint
        key = api_key(self.profile.credential_ref)
        reply = post_json(
            f"{ep.base_url}/chat/completions",
            {"Authorization": f"Bearer {key}"} if key else {},
            {"model": ep.model_id, ep.token_param: max_output_tokens,
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=timeout_seconds, max_retries=self.profile.max_retries, **self._extra,
        )
        usage = reply.get("usage") or {}
        if "prompt_tokens" not in usage or "completion_tokens" not in usage:
            # Cost cannot be computed without usage: fail rather than record a guess.
            raise ModelHTTPError(None, "missing_usage")
        choice = reply["choices"][0]
        finish = choice.get("finish_reason")
        return ModelResponse(
            text=choice["message"].get("content") or "",
            stop_reason=_STOP.get(finish, f"other:{finish}"),
            input_tokens=usage["prompt_tokens"],
            output_tokens=usage["completion_tokens"],
        )
