from .base import TaskFamily, Variant
from .bowling import Bowling

FAMILIES: dict[str, TaskFamily] = {"bowling": Bowling()}

__all__ = ["FAMILIES", "TaskFamily", "Variant"]
