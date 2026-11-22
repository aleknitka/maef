"""Lease vocabulary shared by the ledger, the worker channel and the worker.

A lease is the right to run one admitted run until `expires_at`. The fencing token makes a
stale holder harmless: once a lease is superseded, expired or finished, every call carrying
its token is refused.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .adapters.decision import DecisionRequest
from .definitions import PinnedDefinition
from .models import Budget, ModelEndpointProfile


class FencingError(Exception):
    """The token is not the run's current lease, or the run is already terminal."""


class SequenceGap(Exception):
    """A worker event arrived with a wseq beyond last_wseq + 1: something was lost."""


class ChannelError(Exception):
    """The control plane could not be reached or answered unexpectedly. Never carries a body."""


class DecisionTask(BaseModel):
    """A decision call pinned at admission: the request and the exact profile bytes it runs under."""
    model_config = ConfigDict(frozen=True, ser_json_bytes="base64", val_json_bytes="base64")
    request: DecisionRequest
    profile: ModelEndpointProfile
    profile_bytes: bytes
    profile_digest: str


class TaskPayload(BaseModel):
    """Everything a worker needs, stored at admission. Never carries credentials.
    `kind` discriminates: agent runs carry `instruction` and `pinned`; decision runs carry `decision`."""
    kind: Literal["agent", "decision"] = "agent"
    instruction: str = ""
    budget: Budget
    reserved_tokens: int
    pinned: PinnedDefinition | None = None
    decision: DecisionTask | None = None

    @model_validator(mode="after")
    def _kind_fields(self):
        if self.kind == "agent" and self.pinned is None:
            raise ValueError("agent task needs pinned")
        if self.kind == "decision" and (self.decision is None or self.pinned is not None):
            raise ValueError("decision task needs decision and no pinned definition")
        return self


class Lease(BaseModel):
    run_id: str
    fencing_token: int
    expires_at: float  # unix seconds
    task: TaskPayload
