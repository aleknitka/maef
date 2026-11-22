from ..credentials import CredentialError, Credentials
from ..egress import EgressPolicy
from ..models import AnthropicEndpoint, ModelEndpointProfile
from .base import Message, ModelResponse, ToolCall, ToolSpec, grouped, normalise_stop
from .http import post_json

_STOP = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens"}


def _wire_messages(messages: list[Message]) -> list[dict]:
    out = []
    for role, batch in grouped(messages):
        if role == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.text, "is_error": m.is_error}
                for m in batch]})
            continue
        for m in batch:
            if m.role == "assistant" and m.tool_calls:
                blocks = ([{"type": "text", "text": m.text}] if m.text else []) + [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls]
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": m.role, "content": m.text})
    return out


class AnthropicAdapter:
    """Anthropic Messages API."""

    def __init__(self, profile: ModelEndpointProfile, credentials: Credentials, sleep=None, policy: EgressPolicy | None = None):
        if not isinstance(profile.endpoint, AnthropicEndpoint):
            raise TypeError(f"Profile {profile.id} is not an anthropic profile")
        self.profile = profile
        self._credentials = credentials
        self._policy = policy or EgressPolicy()
        self._extra = {"sleep": sleep} if sleep else {}

    def converse(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        ep = self.profile.endpoint
        key = self._credentials.resolve(self.profile.credential_ref)
        if key is None:
            raise CredentialError("missing_credential:anthropic")
        body = {"model": ep.model_id, "max_tokens": max_output_tokens, "system": system,
                "messages": _wire_messages(messages)}
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
        reply = post_json(
            f"{ep.base_url}/v1/messages",
            {"x-api-key": key.reveal(), "anthropic-version": "2023-06-01"},
            body,
            timeout=timeout_seconds, policy=self._policy, allow_loopback=ep.is_loopback,
            allow_private=ep.allow_private_network, max_retries=self.profile.max_retries, **self._extra,
        )
        calls = tuple(
            ToolCall(b["id"], b["name"], b["input"]) if isinstance(b.get("input"), dict)
            else ToolCall(b["id"], b["name"], {}, parse_error="invalid_arguments")
            for b in reply["content"] if b.get("type") == "tool_use")
        return ModelResponse(
            tool_calls=calls,
            text="".join(b.get("text", "") for b in reply["content"] if b.get("type") == "text"),
            stop_reason=normalise_stop(reply["stop_reason"], _STOP),
            input_tokens=reply["usage"]["input_tokens"],
            output_tokens=reply["usage"]["output_tokens"],
        )
