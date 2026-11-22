from ..credentials import Credentials
from ..egress import EgressPolicy
from ..models import ModelEndpointProfile, OpenAICompatibleEndpoint
from .base import ModelResponse
from .http import ModelHTTPError, post_json

_STOP = {"stop": "end_turn", "length": "max_tokens"}


class OpenAICompatibleAdapter:
    def __init__(self, profile: ModelEndpointProfile, credentials: Credentials, sleep=None, policy: EgressPolicy | None = None):
        if not isinstance(profile.endpoint, OpenAICompatibleEndpoint):
            raise TypeError(f"Profile {profile.id} is not an openai-compatible profile")
        self.profile = profile
        self._credentials = credentials
        self._policy = policy or EgressPolicy()
        self._extra = {"sleep": sleep} if sleep else {}

    def converse(self, *, system: str, user: str, max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        ep = self.profile.endpoint
        key = self._credentials.resolve(self.profile.credential_ref)
        reply = post_json(
            f"{ep.base_url}/chat/completions",
            {"Authorization": f"Bearer {key.reveal()}"} if key else {},
            {"model": ep.model_id, ep.token_param: max_output_tokens,
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=timeout_seconds, policy=self._policy, allow_loopback=ep.is_loopback,
            allow_private=ep.allow_private_network, max_retries=self.profile.max_retries, **self._extra,
        )
        usage = reply.get("usage") or {}
        if "prompt_tokens" not in usage or "completion_tokens" not in usage:
            # Budgets cannot be metered without usage: fail rather than record a guess.
            raise ModelHTTPError(None, "missing_usage")
        choice = reply["choices"][0]
        finish = choice.get("finish_reason")
        return ModelResponse(
            text=choice["message"].get("content") or "",
            stop_reason=_STOP.get(finish, f"other:{finish}"),
            input_tokens=usage["prompt_tokens"],
            output_tokens=usage["completion_tokens"],
        )
