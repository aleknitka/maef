import json

from ..credentials import Credentials
from ..egress import EgressPolicy
from ..models import ModelEndpointProfile, OpenAICompatibleEndpoint
from .base import Message, ModelResponse, ToolCall, ToolSpec, normalise_stop
from .http import ModelHTTPError, post_json

_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}


def _wire_message(m: Message) -> dict:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text}
    if m.role == "assistant" and m.tool_calls:
        return {"role": "assistant", "content": m.text or None, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in m.tool_calls]}
    return {"role": m.role, "content": m.text}


def _parse_call(index: int, raw: dict) -> ToolCall:
    """Local servers vary: id may be missing, arguments may be a JSON string or an object."""
    fn = raw.get("function") or {}
    args, error = fn.get("arguments"), None
    if args is None or args == "":
        args = {}
    elif isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args, error = {}, "invalid_arguments"
    if not isinstance(args, dict):
        args, error = {}, "invalid_arguments"
    return ToolCall(id=raw.get("id") or f"call_{index}", name=fn.get("name") or "", arguments=args, parse_error=error)


class OpenAICompatibleAdapter:
    def __init__(self, profile: ModelEndpointProfile, credentials: Credentials, sleep=None, policy: EgressPolicy | None = None):
        if not isinstance(profile.endpoint, OpenAICompatibleEndpoint):
            raise TypeError(f"Profile {profile.id} is not an openai-compatible profile")
        self.profile = profile
        self._credentials = credentials
        self._policy = policy or EgressPolicy()
        self._extra = {"sleep": sleep} if sleep else {}

    def converse(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        ep = self.profile.endpoint
        key = self._credentials.resolve(self.profile.credential_ref)
        body = {"model": ep.model_id, ep.token_param: max_output_tokens,
                "messages": [{"role": "system", "content": system}, *map(_wire_message, messages)]}
        if tools:  # some local servers reject an empty list
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.input_schema}} for t in tools]
        reply = post_json(
            f"{ep.base_url}/chat/completions",
            {"Authorization": f"Bearer {key.reveal()}"} if key else {},
            body,
            timeout=timeout_seconds, policy=self._policy, allow_loopback=ep.is_loopback,
            allow_private=ep.allow_private_network, max_retries=self.profile.max_retries, **self._extra,
        )
        usage = reply.get("usage") or {}
        if "prompt_tokens" not in usage or "completion_tokens" not in usage:
            # Budgets cannot be metered without usage: fail rather than record a guess.
            raise ModelHTTPError(None, "missing_usage")
        choice = reply["choices"][0]
        message = choice.get("message") or {}
        calls = tuple(_parse_call(i, c) for i, c in enumerate(message.get("tool_calls") or []))
        # Local servers often report finish_reason "stop" alongside tool calls.
        finish = choice.get("finish_reason")
        # Local servers may say "stop" alongside tool calls; but "length" means the call was cut off.
        stop = "tool_use" if calls and finish != "length" else normalise_stop(finish, _STOP)
        return ModelResponse(
            text=message.get("content") or "", tool_calls=calls, stop_reason=stop,
            input_tokens=usage["prompt_tokens"], output_tokens=usage["completion_tokens"],
        )
