"""Token budget admission: reserve before a run starts, settle or release when it ends (ADR-0004).

Budgets are metered in tokens (input + output as reported by the provider), which is
provider-neutral and exact. Fails closed on a dirty or unknown git state.
"""
import uuid

from .models import Budget, ResolvedDefinition
from .store import Store, append_event, now


class AdmissionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def worst_case_tokens(budget: Budget, max_output_tokens: int, prompt_bytes: int) -> int:
    """Upper bound on tokens used. One byte is at least one token's worth of input, so bytes
    bound tokens. Turn n re-sends the prompt plus every earlier turn's worst-case output."""
    turns = budget.max_turns
    return turns * prompt_bytes + max_output_tokens * turns * (turns - 1) // 2 + turns * max_output_tokens


def admit(
    store: Store,
    definition: ResolvedDefinition,
    budget: Budget,
    *,
    instruction: str,
    system_prompt_bytes: int,
    max_output_tokens: int,
    account_id: str = "default",
    allow_dirty: bool = False,
) -> str:
    """Reserve tokens and create the run in one transaction. Returns the run id."""
    if definition.dirty and not allow_dirty:
        raise AdmissionError("dirty", "Agent directory has uncommitted changes or unknown git state.")

    prompt_bytes = system_prompt_bytes + len(instruction.encode("utf-8"))
    bound = worst_case_tokens(budget, max_output_tokens, prompt_bytes)
    reserve = min(budget.max_tokens, bound)
    run_id = uuid.uuid4().hex

    with store.transaction() as conn:
        account = conn.execute("SELECT * FROM budget_accounts WHERE id=?", (account_id,)).fetchone()
        if account is None:
            raise AdmissionError("no_account", f"Unknown budget account: {account_id}")
        free = account["limit_tokens"] - account["reserved_tokens"] - account["spent_tokens"]
        if reserve > free:
            raise AdmissionError("insufficient_budget", f"Needs {reserve} tokens, {free} available.")
        conn.execute("UPDATE budget_accounts SET reserved_tokens = reserved_tokens + ? WHERE id=?", (reserve, account_id))
        conn.execute(
            "INSERT INTO runs (id, status, account_id, agent_id, git_rev, dirty, definition_digest, profile_digest, "
            "effective_budget, reserved_tokens, created_at) VALUES (?, 'admitted', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, account_id, definition.agent_id, definition.git_rev, int(definition.dirty),
             definition.definition_digest, definition.profile_digest, budget.model_dump_json(), reserve, now()),
        )
        conn.execute("INSERT INTO reservations (run_id, account_id, reserved_tokens) VALUES (?, ?, ?)", (run_id, account_id, reserve))
        append_event(conn, run_id, "task.created", {"agent_id": definition.agent_id, "dirty_allowed": definition.dirty and allow_dirty})
        append_event(conn, run_id, "task.admitted", {"reserved_tokens": reserve, "worst_case_tokens": bound})
    return run_id


def settle(store: Store, run_id: str, *, tokens_used: int, status: str, terminal_reason: str) -> None:
    """Charge the actual usage, return the unused reservation and close the run."""
    if status not in ("succeeded", "failed", "cancelled"):
        raise ValueError(f"Invalid terminal status: {status}")
    with store.transaction() as conn:
        res = conn.execute("SELECT * FROM reservations WHERE run_id=?", (run_id,)).fetchone()
        if res is None or res["settled_tokens"] is not None:
            raise AdmissionError("not_open", f"Run {run_id} has no open reservation.")
        released = max(res["reserved_tokens"] - tokens_used, 0)
        conn.execute("UPDATE reservations SET settled_tokens=?, released_tokens=? WHERE run_id=?", (tokens_used, released, run_id))
        conn.execute(
            "UPDATE budget_accounts SET reserved_tokens = reserved_tokens - ?, spent_tokens = spent_tokens + ? WHERE id=?",
            (res["reserved_tokens"], tokens_used, res["account_id"]),
        )
        conn.execute(
            "UPDATE runs SET status=?, tokens_used=?, terminal_reason=?, finished_at=? WHERE id=?",
            (status, tokens_used, terminal_reason, now(), run_id),
        )
        payload = {"tokens_used": tokens_used, "released_tokens": released, "terminal_reason": terminal_reason}
        if tokens_used > res["reserved_tokens"]:
            payload["overrun_tokens"] = tokens_used - res["reserved_tokens"]
        append_event(conn, run_id, f"task.{status}", payload)


def release(store: Store, run_id: str, reason: str) -> None:
    """Cancel a run that never used anything."""
    settle(store, run_id, tokens_used=0, status="cancelled", terminal_reason=reason)
