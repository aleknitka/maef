"""Neutral conversation types and the model seam. Each adapter translates these to and from
one wire format; nothing above the adapters knows a provider's JSON."""
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Set when the model's arguments could not be parsed; the harness answers with an error
    # tool result instead of invoking the tool.
    parse_error: str | None = None


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    is_error: bool = False


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    # Normalised: end_turn | tool_use | max_tokens | other:<raw>
    stop_reason: str
    input_tokens: int
    output_tokens: int


def normalise_stop(raw: str | None, mapping: dict[str, str]) -> str:
    return mapping.get(raw, f"other:{raw}")


def grouped(messages: list[Message]) -> Iterator[tuple[str, list[Message]]]:
    """Group messages by role, merging runs of consecutive tool results (several wire formats
    require them in one user message)."""
    batch: list[Message] = []
    for m in messages:
        if batch and not (m.role == "tool" and batch[0].role == "tool"):
            yield batch[0].role, batch
            batch = []
        batch.append(m)
    if batch:
        yield batch[0].role, batch


class ModelAdapter(Protocol):
    def converse(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        """One model call. Raises on transport or service errors."""
        ...


def model_error_reason(exc: BaseException) -> str:
    """Terminal reason for a failed model call: the exception class plus the HTTP status when known
    (`model_error:ModelHTTPError:http_429`). Never the message: provider text may echo prompts or keys."""
    status = getattr(exc, "status", None)
    response = getattr(exc, "response", None)
    if status is None and isinstance(response, dict):  # botocore ClientError
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    name = f"model_error:{type(exc).__name__}"
    return f"{name}:http_{status}" if isinstance(status, int) and 100 <= status <= 599 else name
