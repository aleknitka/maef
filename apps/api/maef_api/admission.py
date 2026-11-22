"""Budget admission: reserve before a run starts, settle or release when it ends (ADR-0004).

Fails closed: no price schedule, an unknown git state or a dirty tree all refuse the run.
All money is integer micro-USD. Prices are USD per million tokens, so
micro-USD = price * tokens, rounded up.
"""
import json
import uuid
from decimal import ROUND_CEILING, Decimal

from .models import Budget, ResolvedDefinition
from .store import Store, append_event, now

MICRO = Decimal(1_000_000)


class AdmissionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def to_micro(usd: Decimal) -> int:
    return int((usd * MICRO).to_integral_value(rounding=ROUND_CEILING))


def worst_case_cost_micro(definition: ResolvedDefinition, budget: Budget, max_output_tokens: int, prompt_bytes: int) -> int:
    """Upper bound on spend. One byte is at least one token's worth of input, so bytes bound tokens.

    Turn n re-sends the prompt plus every earlier turn's worst-case output.
    """
    price = definition.price_schedule
    turns = budget.max_turns
    input_tokens = turns * prompt_bytes + max_output_tokens * turns * (turns - 1) // 2
    output_tokens = turns * max_output_tokens
    return (
        to_micro(price.input_usd_per_million_tokens * input_tokens / MICRO)
        + to_micro(price.output_usd_per_million_tokens * output_tokens / MICRO)
    )


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
    """Reserve budget and create the run in one transaction. Returns the run id."""
    if definition.price_schedule is None:
        raise AdmissionError("unpriced", "Model profile has no price schedule; cost cannot be bounded.")
    if definition.dirty and not allow_dirty:
        raise AdmissionError("dirty", "Agent directory has uncommitted changes or unknown git state.")

    prompt_bytes = system_prompt_bytes + len(instruction.encode("utf-8"))
    bound = worst_case_cost_micro(definition, budget, max_output_tokens, prompt_bytes)
    reserve = min(to_micro(budget.max_cost_usd), bound)
    run_id = uuid.uuid4().hex

    with store.transaction() as conn:
        account = conn.execute("SELECT * FROM budget_accounts WHERE id=?", (account_id,)).fetchone()
        if account is None:
            raise AdmissionError("no_account", f"Unknown budget account: {account_id}")
        free = account["limit_micro"] - account["reserved_micro"] - account["spent_micro"]
        if reserve > free:
            raise AdmissionError("insufficient_budget", f"Needs {reserve} micro-USD, {free} available.")
        conn.execute("UPDATE budget_accounts SET reserved_micro = reserved_micro + ? WHERE id=?", (reserve, account_id))
        conn.execute(
            "INSERT INTO runs (id, status, account_id, agent_id, git_rev, dirty, definition_digest, profile_digest, "
            "effective_budget, reserved_micro, created_at) VALUES (?, 'admitted', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, account_id, definition.agent_id, definition.git_rev, int(definition.dirty),
             definition.definition_digest, definition.profile_digest, budget.model_dump_json(), reserve, now()),
        )
        conn.execute("INSERT INTO reservations (run_id, account_id, reserved_micro) VALUES (?, ?, ?)", (run_id, account_id, reserve))
        append_event(conn, run_id, "task.created", {"agent_id": definition.agent_id, "dirty_allowed": definition.dirty and allow_dirty})
        append_event(conn, run_id, "task.admitted", {"reserved_micro_usd": reserve, "worst_case_micro_usd": bound})
    return run_id


def settle(store: Store, run_id: str, *, cost_micro: int, status: str, terminal_reason: str) -> None:
    """Charge the actual cost, return the unused reservation and close the run."""
    if status not in ("succeeded", "failed", "cancelled"):
        raise ValueError(f"Invalid terminal status: {status}")
    with store.transaction() as conn:
        res = conn.execute("SELECT * FROM reservations WHERE run_id=?", (run_id,)).fetchone()
        if res is None or res["settled_micro"] is not None:
            raise AdmissionError("not_open", f"Run {run_id} has no open reservation.")
        released = max(res["reserved_micro"] - cost_micro, 0)
        conn.execute("UPDATE reservations SET settled_micro=?, released_micro=? WHERE run_id=?", (cost_micro, released, run_id))
        conn.execute(
            "UPDATE budget_accounts SET reserved_micro = reserved_micro - ?, spent_micro = spent_micro + ? WHERE id=?",
            (res["reserved_micro"], cost_micro, res["account_id"]),
        )
        conn.execute(
            "UPDATE runs SET status=?, cost_micro=?, terminal_reason=?, finished_at=? WHERE id=?",
            (status, cost_micro, terminal_reason, now(), run_id),
        )
        payload = {"cost_micro_usd": cost_micro, "released_micro_usd": released, "terminal_reason": terminal_reason}
        if cost_micro > res["reserved_micro"]:
            payload["overrun_micro_usd"] = cost_micro - res["reserved_micro"]
        append_event(conn, run_id, f"task.{status}", payload)


def release(store: Store, run_id: str, reason: str) -> None:
    """Cancel a run that never spent anything."""
    settle(store, run_id, cost_micro=0, status="cancelled", terminal_reason=reason)
