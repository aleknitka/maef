from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelResponse:
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int


class ModelAdapter(Protocol):
    def converse(self, *, system: str, user: str, max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        """One model call. Raises on transport or service errors."""
        ...
