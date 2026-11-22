"""The run interface used by HTTP (and later the CLI): preview, submit, inspect, recover.

Runs admits and queues; it never executes. A worker claims the queued run (see worker.py)."""
import time
from collections.abc import Callable

from .definitions import Definitions, ResolveError
from .ledger import AdmissionError, Ledger
from .models import (
    SUPPORTED_DIALECTS,
    Budget,
    RunDetail,
    RunPreview,
    RunRecord,
    RunRequest,
)
from .tools import builtin_specs, spec_bytes


class RunsError(Exception):
    """Base for errors a caller can act on; the transport decides how to present them."""


class ExecutionDisabled(RunsError):
    pass


class AgentNotFound(RunsError):
    pass


class DefinitionConflict(RunsError):
    """The definition cannot be pinned (missing file, outside the agent directory, no profile)."""


class UnsupportedDialect(RunsError):
    pass


class AdmissionRejected(RunsError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def effective_budget(ceiling: Budget, requested: Budget | None) -> Budget:
    """A request may only tighten the agent's budget, never raise it."""
    if requested is None:
        return ceiling
    return Budget(
        max_tokens=min(ceiling.max_tokens, requested.max_tokens),
        max_duration_seconds=min(ceiling.max_duration_seconds, requested.max_duration_seconds),
        max_turns=min(ceiling.max_turns, requested.max_turns),
    )


class Runs:
    def __init__(
        self,
        definitions: Definitions,
        ledger: Ledger,
        *,
        execution_enabled: Callable[[], bool],
        allow_dirty: Callable[[], bool],
        supported_dialects: tuple[str, ...] = SUPPORTED_DIALECTS,
        clock: Callable[[], float] = time.time,
    ):
        self.definitions = definitions
        self.ledger = ledger
        self._supported_dialects = supported_dialects
        self._clock = clock
        self._execution_enabled = execution_enabled
        self._allow_dirty = allow_dirty

    def _agent(self, agent_id: str):
        agent = self.definitions.agents.get_agent(agent_id)
        if agent is None:
            raise AgentNotFound(agent_id)
        return agent

    def preview(self, req: RunRequest) -> RunPreview:
        agent = self._agent(req.agent_id)
        return RunPreview(
            agent_id=agent.id,
            instruction=req.instruction,
            effective_budget=effective_budget(agent.budget, req.budget),
            model_profile=agent.runtime.model_profile,
            sandbox_profile=agent.runtime.sandbox_profile,
            skills=agent.skills,
            mcp_servers=agent.mcp_servers,
        )

    def submit(self, req: RunRequest) -> RunRecord:
        """Admit a run and queue it for a worker; returns the queued run."""
        if not self._execution_enabled():
            raise ExecutionDisabled("Execution disabled. Set MAEF_EXECUTION_ENABLED=1 or use /api/v1/runs/preview.")
        agent = self._agent(req.agent_id)
        try:
            pinned = self.definitions.resolve(agent.id)
        except ResolveError as exc:
            raise DefinitionConflict(str(exc)) from exc
        if pinned.profile.api_dialect not in self._supported_dialects:
            raise UnsupportedDialect(f"No adapter for dialect {pinned.profile.api_dialect}")
        try:
            specs = builtin_specs(pinned.agent.tools)
        except ValueError as exc:
            raise DefinitionConflict(str(exc)) from exc
        budget = effective_budget(agent.budget, req.budget)
        try:
            run_id = self.ledger.admit(
                pinned, budget,
                prompt_bytes=len(pinned.instructions.encode()) + len(req.instruction.encode()) + spec_bytes(specs),
                instruction=req.instruction,
                allow_dirty=self._allow_dirty(),
            )
        except AdmissionError as exc:
            raise AdmissionRejected(exc.code, str(exc)) from exc
        record = self.ledger.get(run_id)
        return RunRecord.model_validate(record.model_dump(exclude={"events"}))

    def get(self, run_id: str) -> RunDetail | None:
        return self.ledger.get(run_id)

    def list(self) -> list[RunRecord]:
        return self.ledger.list()

    def recover(self) -> int:
        """Fail runs whose lease lapsed (fail closed). Queued runs stay queued. Called at startup
        and periodically."""
        return self.ledger.expire_leases(self._clock())
