"""Budget guard: decides before each model call whether the run may continue.

Pure and in-process. Prompt bytes bound prompt tokens (a token is at least one byte), so the
worst case for a call is tokens_used + prompt_bytes + max_output_tokens, which must fit in the
reservation made at admission.
"""
import time

from .models import Budget


class BudgetGuard:
    def __init__(self, budget: Budget, reserved_tokens: int, max_output_tokens: int, clock=time.monotonic):
        self._budget = budget
        self._reserved = reserved_tokens
        self._max_output = max_output_tokens
        self._clock = clock
        self._start = clock()
        self._tokens = 0
        self._turns = 0

    @property
    def tokens_used(self) -> int:
        return self._tokens

    @property
    def turns(self) -> int:
        return self._turns

    def remaining_seconds(self) -> float:
        return float(self._budget.max_duration_seconds) - (self._clock() - self._start)

    def check(self, prompt_bytes: int) -> str | None:
        """A stop reason, or None when another model call fits the budget."""
        if self._turns >= self._budget.max_turns:
            return "turns_exhausted"
        if self.remaining_seconds() <= 0:
            return "duration_exhausted"
        if self._tokens + prompt_bytes + self._max_output > self._reserved:
            return "tokens_exhausted"
        return None

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self._tokens += input_tokens + output_tokens
        self._turns += 1
