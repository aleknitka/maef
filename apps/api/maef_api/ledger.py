"""SQLite run ledger: the only module that contains SQL.

Plain sqlite3; migrations are versioned with PRAGMA user_version. Usage is stored as integer
tokens, never REAL. run_events is append-only: triggers reject UPDATE and DELETE, and no
code path issues either.

Token budget admission (ADR-0004): reserve before a run starts, settle or release when it
ends. Budgets are metered in tokens (input + output as reported by the provider), which is
provider-neutral and exact. Fails closed on a dirty or unknown git state.
"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .definitions import PinnedDefinition
from .models import Budget, RunDetail, RunRecord

MIGRATIONS = [
    """
    CREATE TABLE budget_accounts (
        id TEXT PRIMARY KEY,
        limit_micro INTEGER NOT NULL CHECK (limit_micro >= 0),
        reserved_micro INTEGER NOT NULL DEFAULT 0 CHECK (reserved_micro >= 0),
        spent_micro INTEGER NOT NULL DEFAULT 0 CHECK (spent_micro >= 0)
    );
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES budget_accounts(id),
        agent_id TEXT NOT NULL,
        git_rev TEXT,
        dirty INTEGER NOT NULL,
        definition_digest TEXT NOT NULL,
        profile_digest TEXT NOT NULL,
        effective_budget TEXT NOT NULL,
        reserved_micro INTEGER NOT NULL,
        cost_micro INTEGER,
        terminal_reason TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE TABLE run_events (
        run_id TEXT NOT NULL REFERENCES runs(id),
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        ts TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    );
    CREATE TRIGGER run_events_no_update BEFORE UPDATE ON run_events
        BEGIN SELECT RAISE(ABORT, 'run_events is append-only'); END;
    CREATE TRIGGER run_events_no_delete BEFORE DELETE ON run_events
        BEGIN SELECT RAISE(ABORT, 'run_events is append-only'); END;
    CREATE TABLE reservations (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        account_id TEXT NOT NULL REFERENCES budget_accounts(id),
        reserved_micro INTEGER NOT NULL,
        settled_micro INTEGER,
        released_micro INTEGER
    );
    """,
    # Budgets are metered in tokens, not USD.
    """
    ALTER TABLE budget_accounts RENAME COLUMN limit_micro TO limit_tokens;
    ALTER TABLE budget_accounts RENAME COLUMN reserved_micro TO reserved_tokens;
    ALTER TABLE budget_accounts RENAME COLUMN spent_micro TO spent_tokens;
    ALTER TABLE runs RENAME COLUMN reserved_micro TO reserved_tokens;
    ALTER TABLE runs RENAME COLUMN cost_micro TO tokens_used;
    ALTER TABLE reservations RENAME COLUMN reserved_micro TO reserved_tokens;
    ALTER TABLE reservations RENAME COLUMN settled_micro TO settled_tokens;
    ALTER TABLE reservations RENAME COLUMN released_micro TO released_tokens;
    """,
]



class AdmissionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def worst_case_tokens(budget: Budget, max_output_tokens: int, prompt_bytes: int) -> int:
    """Upper bound on tokens used. One byte is at least one token's worth of input, so bytes
    bound tokens. Turn n re-sends the prompt plus every earlier turn's worst-case output."""
    turns = budget.max_turns
    return turns * prompt_bytes + max_output_tokens * turns * (turns - 1) // 2 + turns * max_output_tokens


def _record(row: sqlite3.Row) -> dict:
    return {**dict(row), "dirty": bool(row["dirty"]), "effective_budget": json.loads(row["effective_budget"])}


def _append(conn: sqlite3.Connection, run_id: str, type_: str, payload: dict | None = None) -> None:
    """Append inside the caller's transaction; seq is gapless per run."""
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id=?", (run_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO run_events (run_id, seq, type, ts, payload) VALUES (?, ?, ?, ?, ?)",
        (run_id, seq, type_, now(), json.dumps(payload or {}, sort_keys=True)),
    )


class Ledger:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self._migrate(conn)

    @contextmanager
    def connect(self):
        """A fresh connection per use (sqlite3 connections must not cross threads)."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self):
        """BEGIN IMMEDIATE takes the write lock up front, so check-then-insert is atomic."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for n, script in enumerate(MIGRATIONS[version:], start=version + 1):
                for statement in _split(script):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={n}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def ensure_account(self, account_id: str, limit_tokens: int) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO budget_accounts (id, limit_tokens) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET limit_tokens=excluded.limit_tokens",
                (account_id, limit_tokens),
            )

    def account(self, account_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM budget_accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[RunRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC").fetchall()
        return [RunRecord.model_validate(_record(r)) for r in rows]

    def get(self, run_id: str) -> RunDetail | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            events = [
                {**dict(e), "payload": json.loads(e["payload"])}
                for e in conn.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY seq", (run_id,))
            ]
        return RunDetail.model_validate({**_record(row), "events": events})

    def admit(
        self,
        pinned: PinnedDefinition,
        budget: Budget,
        *,
        prompt_bytes: int,
        account_id: str = "default",
        allow_dirty: bool = False,
    ) -> str:
        """Reserve tokens and create the run in one transaction. Returns the run id."""
        if pinned.dirty and not allow_dirty:
            raise AdmissionError("dirty", "Agent directory has uncommitted changes or unknown git state.")

        bound = worst_case_tokens(budget, pinned.profile.max_output_tokens, prompt_bytes)
        reserve = min(budget.max_tokens, bound)
        run_id = uuid.uuid4().hex

        with self._transaction() as conn:
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
                (run_id, account_id, pinned.agent.id, pinned.git_rev, int(pinned.dirty),
                 pinned.definition_digest, pinned.profile_digest, budget.model_dump_json(), reserve, now()),
            )
            conn.execute("INSERT INTO reservations (run_id, account_id, reserved_tokens) VALUES (?, ?, ?)", (run_id, account_id, reserve))
            _append(conn, run_id, "task.created", {"agent_id": pinned.agent.id, "dirty_allowed": pinned.dirty and allow_dirty})
            _append(conn, run_id, "task.admitted", {"reserved_tokens": reserve, "worst_case_tokens": bound})
        return run_id

    def append(self, run_id: str, type: str, payload: dict | None = None, *, status: str | None = None) -> None:
        """Append an event, optionally moving the run to `status` in the same transaction."""
        with self._transaction() as conn:
            if status:
                conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
            _append(conn, run_id, type, payload)

    def settle(self, run_id: str, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        """Charge the actual usage, return the unused reservation and close the run."""
        if status not in ("succeeded", "failed", "cancelled"):
            raise ValueError(f"Invalid terminal status: {status}")
        with self._transaction() as conn:
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
            _append(conn, run_id, f"task.{status}", payload)

    def release(self, run_id: str, reason: str) -> None:
        """Cancel a run that never used anything."""
        self.settle(run_id, tokens_used=0, status="cancelled", terminal_reason=reason)

    def fail_orphans(self) -> int:
        """Runs left open by a previous process are failed. Their spend is unknown, so the
        whole reservation is charged (fail closed)."""
        orphans = [r for r in self.list() if r.status in ("admitted", "running")]
        for r in orphans:
            self.settle(r.id, tokens_used=r.reserved_tokens, status="failed", terminal_reason="worker_lost")
        return len(orphans)


def _split(script: str) -> list[str]:
    """Split a migration into statements, keeping CREATE TRIGGER ... BEGIN ... END; whole."""
    statements, current = [], []
    for line in script.splitlines():
        current.append(line)
        text = " ".join(current).strip()
        if text.endswith(";") and (" TRIGGER " not in text or text.upper().endswith("END;")):
            statements.append(text)
            current = []
    return statements
