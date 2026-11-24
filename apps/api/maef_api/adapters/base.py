"""Neutral conversation types and the model seam. Each adapter translates these to and from
one wire format; nothing above the adapters knows a provider's JSON."""
import math
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
    # Normalised usage, identical for every provider:
    #   input_tokens       = ALL prompt tokens processed, INCLUDING cache reads and cache writes
    #                        (budgets charge input_tokens + output_tokens, so this is conservative).
    #   cache_read_tokens  = the part of input_tokens served from the prompt cache.
    #   cache_write_tokens = the part of input_tokens written to the prompt cache.
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # reasoning_tokens is the part of output_tokens spent on hidden reasoning (a subset, like cache tokens).
    reasoning_tokens: int = 0
    # Provider-reported cost in USD. Information only: budgets stay token-only.
    cost_usd: float | None = None
    # Who actually served the call (routers such as OpenRouter), and the call's id and model.
    provider: str | None = None
    response_id: str | None = None
    model: str | None = None
    reasoning: str = ""  # reasoning text if the provider returns it; only stored in the opt-in transcript


def token_count(value) -> int:
    """A non-negative int from provider JSON; anything else (None, bool, str, float, negative) is 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def cost_value(value) -> float | None:
    """A finite, non-negative number from provider JSON, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def short_str(value, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) else None


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
