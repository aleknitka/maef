from ..models import ModelEndpointProfile
from .base import Message, ModelResponse, ToolCall, ToolSpec, grouped, normalise_stop

_STOP = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens"}


def _wire_messages(messages: list[Message]) -> list[dict]:
    out = []
    for role, batch in grouped(messages):
        if role == "tool":
            out.append({"role": "user", "content": [
                {"toolResult": {"toolUseId": m.tool_call_id, "content": [{"text": m.text}],
                                "status": "error" if m.is_error else "success"}} for m in batch]})
            continue
        for m in batch:
            blocks = [{"text": m.text}] if m.text else []
            blocks += [{"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.arguments}} for c in m.tool_calls]
            out.append({"role": m.role, "content": blocks})
    return out


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

    def converse(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        kwargs = {}
        if tools:
            kwargs["toolConfig"] = {"tools": [{"toolSpec": {
                "name": t.name, "description": t.description, "inputSchema": {"json": t.input_schema}}} for t in tools]}
        reply = self._get_client(timeout_seconds).converse(
            modelId=self.profile.endpoint.model_id,
            system=[{"text": system}],
            messages=_wire_messages(messages),
            inferenceConfig={"maxTokens": max_output_tokens},
            **kwargs,
        )
        blocks = reply["output"]["message"]["content"]
        return ModelResponse(
            text="".join(b["text"] for b in blocks if "text" in b),
            tool_calls=tuple(ToolCall(b["toolUse"]["toolUseId"], b["toolUse"]["name"], b["toolUse"]["input"])
                             for b in blocks if "toolUse" in b),
            stop_reason=normalise_stop(reply["stopReason"], _STOP),
            input_tokens=reply["usage"]["inputTokens"],
            output_tokens=reply["usage"]["outputTokens"],
        )
