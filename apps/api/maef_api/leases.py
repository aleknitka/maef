"""Lease vocabulary shared by the ledger, the worker channel and the worker.

A lease is the right to run one admitted run until `expires_at`. The fencing token makes a
stale holder harmless: once a lease is superseded, expired or finished, every call carrying
its token is refused.
"""
from pydantic import BaseModel

from .definitions import PinnedDefinition
from .models import Budget


class FencingError(Exception):
    """The token is not the run's current lease, or the run is already terminal."""


class SequenceGap(Exception):
    """A worker event arrived with a wseq beyond last_wseq + 1: something was lost."""


class ChannelError(Exception):
    """The control plane could not be reached or answered unexpectedly. Never carries a body."""


class TaskPayload(BaseModel):
    """Everything a worker needs, stored at admission. Never carries credentials."""
    instruction: str
    budget: Budget
    reserved_tokens: int
    pinned: PinnedDefinition


class Lease(BaseModel):
    run_id: str
    fencing_token: int
    expires_at: float  # unix seconds
    task: TaskPayload
