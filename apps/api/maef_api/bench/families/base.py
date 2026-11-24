"""TaskFamily: a task template, a parameter space and a reference solution.

A seed plus a noise level picks one Variant. The task text and the hidden acceptance cases are both derived from
that exact variant, so memorising the public version of the task does not help.
"""
from typing import Protocol

from pydantic import BaseModel, Field

STRAPLINE = ("Intelligence is not what you do when you know what to do, "
             "but what you do when you don't know what to do.")

JUDGED_BY = ("Your work is judged by hidden acceptance tests that call your public API exactly as named above. "
             "Your own tests also count: they should be real, runnable and cover the rules.")


class Variant(BaseModel):
    family: str
    seed: int
    noise: int
    params: dict = Field(default_factory=dict)
    names: dict = Field(default_factory=dict)


class TaskFamily(Protocol):
    id: str

    def sample(self, seed: int, noise: int) -> Variant: ...

    def render_task(self, v: Variant) -> str:
        """Markdown: Context, Requirements (R1..), Deliverables (D1..), how it is judged."""
        ...

    def hidden_cases(self, v: Variant) -> list[dict]:
        """Deterministic from the variant. Each case: {"name", "rolls"/inputs..., "expected"}."""
        ...

    def grader_source(self, v: Variant) -> str:
        """A stdlib-only script run in the container: imports the deliverable from /work, reads /grader/cases.json,
        prints one JSON object {"passed", "failed", "errors": [{case, expected, got}]} and never crashes."""
        ...
