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

from .artifacts import (
    MAX_RUN_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACTS,
    ArtifactRef,
    ArtifactRejected,
)
from .definitions import PinnedDefinition
from .leases import DecisionTask, FencingError, Lease, SequenceGap, TaskPayload
from .models import Budget, ExecutionEvent, RunDetail, RunRecord

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
    # Queue + leases: the task is stored at admission so a worker can run it without the API.
    """
    ALTER TABLE runs ADD COLUMN task TEXT;
    CREATE TABLE leases (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        worker_id TEXT NOT NULL,
        fencing_token INTEGER NOT NULL,
        expires_at REAL NOT NULL,
        last_wseq INTEGER NOT NULL DEFAULT 0
    );
    """,
    # Artifact refs per run; the bytes live in an ArtifactStore.
    """
    CREATE TABLE run_artifacts (
        run_id TEXT NOT NULL REFERENCES runs(id),
        seq INTEGER NOT NULL,
        digest TEXT NOT NULL,
        size INTEGER NOT NULL,
        mime TEXT NOT NULL,
        name TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    );
    CREATE INDEX run_artifacts_digest ON run_artifacts(digest);
    """,
    # Cooperative cancellation: the worker sees the flag on its next heartbeat or report.
    """
    ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
    """,
    # Decision runs share the ledger: `kind` tells them from agent runs.
    """
    ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent';
    """,
    # Principals and audit (step 11). Only sha256 hashes of tokens are stored; audit_log is append-only.
    """
    CREATE TABLE api_tokens (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        scopes TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    );
    CREATE TABLE worker_credentials (
        worker_id TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    );
    CREATE INDEX worker_credentials_worker ON worker_credentials(worker_id);
    CREATE TABLE audit_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        principal TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        outcome TEXT NOT NULL,
        client TEXT NOT NULL
    );
    CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
    CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
    """,
    # Runs already pushed to the OTLP collector (step 14); export is once per terminal run.
    """
    CREATE TABLE otel_exports (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        exported_at TEXT NOT NULL
    );
    """,
]

TERMINAL = ("succeeded", "failed", "cancelled")
# Statuses a worker may move a run to through report(); terminal states only via finish().
WORKER_STATUSES = ("running",)
# Event types only the ledger itself may write.
RESERVED_PREFIXES = ("task.", "lease.", "artifact.", "cancel.")


class UnknownRun(Exception):
    pass


class AlreadyTerminal(Exception):
    """The run has already finished; there is nothing left to cancel."""


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
    record = {**dict(row), "dirty": bool(row["dirty"]), "cancel_requested": bool(row["cancel_requested"]),
              "effective_budget": json.loads(row["effective_budget"])}
    record.pop("task", None)  # the stored task carries the pinned definition; never listed
    return record


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

    def status(self, run_id: str) -> str | None:
        """The run's status, or None for an unknown run. Cheap: one indexed read."""
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        return row["status"] if row else None

    def is_terminal(self, run_id: str) -> bool:
        return self.status(run_id) in TERMINAL

    def events_after(self, run_id: str, after_seq: int, limit: int = 500) -> "list[ExecutionEvent]":  # `list` is shadowed by Ledger.list
        """Events with seq > after_seq, oldest first (seq is gapless, so this is a resumable cursor)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT run_id, seq, type, ts, payload FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after_seq, limit)).fetchall()
        return [ExecutionEvent(**{**dict(r), "payload": json.loads(r["payload"])}) for r in rows]

    def request_cancel(self, run_id: str) -> str:
        """Ask a run to stop. Returns "cancelled" (it was queued and is now settled with 0 tokens) or
        "requested" (a worker holds it; it learns on its next heartbeat or report). Idempotent while
        the run is live. Raises UnknownRun, or AlreadyTerminal once the run has finished."""
        with self._transaction() as conn:
            row = conn.execute("SELECT status, cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise UnknownRun(run_id)
            if row["status"] in TERMINAL:
                raise AlreadyTerminal(run_id)
            if row["status"] == "admitted":
                conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
                _append(conn, run_id, "cancel.requested", {})
                _settle(conn, run_id, 0, "cancelled", "cancelled_by_user")
                return "cancelled"
            if not row["cancel_requested"]:
                conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
                _append(conn, run_id, "cancel.requested", {})
            return "requested"

    def admit(
        self,
        pinned: PinnedDefinition,
        budget: Budget,
        *,
        prompt_bytes: int,
        instruction: str,
        account_id: str = "default",
        allow_dirty: bool = False,
    ) -> str:
        """Reserve tokens and create the run in one transaction. Returns the run id."""
        if pinned.dirty and not allow_dirty:
            raise AdmissionError("dirty", "Agent directory has uncommitted changes or unknown git state.")

        bound = worst_case_tokens(budget, pinned.profile.max_output_tokens, prompt_bytes)
        reserve = min(budget.max_tokens, bound)
        run_id = uuid.uuid4().hex
        task = TaskPayload(instruction=instruction, budget=budget, reserved_tokens=reserve, pinned=pinned).model_dump_json()

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
                "effective_budget, reserved_tokens, created_at, task) VALUES (?, 'admitted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, account_id, pinned.agent.id, pinned.git_rev, int(pinned.dirty),
                 pinned.definition_digest, pinned.profile_digest, budget.model_dump_json(), reserve, now(), task),
            )
            conn.execute("INSERT INTO reservations (run_id, account_id, reserved_tokens) VALUES (?, ?, ?)", (run_id, account_id, reserve))
            _append(conn, run_id, "task.created", {"agent_id": pinned.agent.id, "dirty_allowed": pinned.dirty and allow_dirty})
            _append(conn, run_id, "task.admitted", {"reserved_tokens": reserve, "worst_case_tokens": bound})
        return run_id

    def admit_decision(self, decision: DecisionTask, *, reserve: int, max_duration_seconds: int,
                       account_id: str = "default") -> str:
        """Reserve `reserve` tokens and queue a decision run (kind "decision") in one transaction."""
        budget = Budget(max_tokens=reserve, max_duration_seconds=max_duration_seconds, max_turns=1)
        run_id = uuid.uuid4().hex
        task = TaskPayload(kind="decision", budget=budget, reserved_tokens=reserve, decision=decision).model_dump_json()
        profile_id = decision.profile.id
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
                "effective_budget, reserved_tokens, created_at, task, kind) VALUES (?, 'admitted', ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, 'decision')",
                (run_id, account_id, f"decision:{profile_id}", decision.profile_digest, decision.profile_digest,
                 budget.model_dump_json(), reserve, now(), task))
            conn.execute("INSERT INTO reservations (run_id, account_id, reserved_tokens) VALUES (?, ?, ?)", (run_id, account_id, reserve))
            _append(conn, run_id, "task.created", {"kind": "decision", "profile_id": profile_id})
            _append(conn, run_id, "task.admitted", {"reserved_tokens": reserve, "request_bytes": decision.request.request_bytes()})
        return run_id

    def append(self, run_id: str, type: str, payload: dict | None = None, *, status: str | None = None) -> None:
        """Append an event, optionally moving the run to `status` in the same transaction."""
        with self._transaction() as conn:
            if status:
                conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
            _append(conn, run_id, type, payload)

    def settle(self, run_id: str, *, tokens_used: int, status: str, terminal_reason: str) -> None:
        """Charge the actual usage, return the unused reservation and close the run."""
        with self._transaction() as conn:
            _settle(conn, run_id, tokens_used, status, terminal_reason)

    def release(self, run_id: str, reason: str) -> None:
        """Cancel a run that never used anything."""
        self.settle(run_id, tokens_used=0, status="cancelled", terminal_reason=reason)

    # --- artifacts (bytes live in an ArtifactStore; the ledger holds refs) ---

    def check_artifact_room(self, run_id: str, token: int | None, size: int, *,
                            digest: str | None = None, name: str | None = None,
                            worker_id: str | None = None) -> None:
        """Fencing and per-run limits, before any bytes are stored. An upload already recorded
        under the same (digest, name) only needs the fencing check."""
        with self.connect() as conn:
            if digest is not None and conn.execute(
                    "SELECT 1 FROM run_artifacts WHERE run_id=? AND digest=? AND name=?",
                    (run_id, digest, name)).fetchone():
                if token is not None:
                    _check_lease(conn, run_id, token, worker_id)
                return
            _artifact_guard(conn, run_id, token, size, worker_id)

    def add_artifact(self, run_id: str, token: int | None, ref: ArtifactRef, *, worker_id: str | None = None) -> None:
        """Record a ref and emit `artifact.stored`. Idempotent for the same (digest, name), so a
        retried upload does not double count. `token=None` skips fencing (in-process use)."""
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM run_artifacts WHERE run_id=? AND digest=? AND name=?",
                            (run_id, ref.digest, ref.name)).fetchone():
                if token is not None:
                    _check_lease(conn, run_id, token, worker_id)
                return
            _artifact_guard(conn, run_id, token, ref.size, worker_id)
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_artifacts WHERE run_id=?", (run_id,)).fetchone()[0]
            conn.execute("INSERT INTO run_artifacts (run_id, seq, digest, size, mime, name) VALUES (?, ?, ?, ?, ?, ?)",
                         (run_id, seq, ref.digest, ref.size, ref.mime, ref.name))
            _append(conn, run_id, "artifact.stored", ref.model_dump(include={"name", "digest", "size", "mime"}))

    def artifacts(self, run_id: str) -> "list[ArtifactRef]":  # `list` is shadowed by Ledger.list
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT digest, size, mime, name FROM run_artifacts WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [ArtifactRef(**dict(r)) for r in rows]

    def artifact_by_digest(self, digest: str) -> ArtifactRef | None:
        """The earliest recorded ref for this digest, or None: only recorded digests may be served."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT digest, size, mime, name FROM run_artifacts WHERE digest=? ORDER BY rowid LIMIT 1",
                (digest,)).fetchone()
        return ArtifactRef(**dict(row)) if row else None

    # --- principals and audit (only token hashes are stored; see auth.py) ---

    def register_api_token(self, name: str, scopes: "tuple[str, ...]", token_hash: str) -> str:
        """Store the hash of a new API token and return its id. Idempotent for an existing hash."""
        with self._transaction() as conn:
            row = conn.execute("SELECT id FROM api_tokens WHERE token_hash=?", (token_hash,)).fetchone()
            if row:
                return row["id"]
            token_id = uuid.uuid4().hex[:12]
            conn.execute("INSERT INTO api_tokens (id, name, token_hash, scopes, created_at) VALUES (?, ?, ?, ?, ?)",
                         (token_id, name, token_hash, ",".join(scopes), now()))
        return token_id

    def api_token_by_hash(self, token_hash: str) -> dict | None:
        """The live (not revoked) token with this hash as {id, name, scopes, token_hash}, or None."""
        with self.connect() as conn:
            row = conn.execute("SELECT id, name, scopes, token_hash FROM api_tokens WHERE token_hash=? AND revoked_at IS NULL",
                               (token_hash,)).fetchone()
        return None if row is None else {**dict(row), "scopes": tuple(s for s in row["scopes"].split(",") if s)}

    def list_api_tokens(self) -> "list[dict]":  # `list` is shadowed by Ledger.list
        with self.connect() as conn:
            rows = conn.execute("SELECT id, name, scopes, created_at, revoked_at FROM api_tokens ORDER BY created_at, rowid").fetchall()
        return [{**dict(r), "scopes": [s for s in r["scopes"].split(",") if s]} for r in rows]

    def revoke_api_token(self, token_id: str) -> bool:
        """True when a live token was revoked."""
        with self._transaction() as conn:
            return conn.execute("UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                (now(), token_id)).rowcount > 0

    def register_worker_credential(self, worker_id: str, token_hash: str) -> None:
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO worker_credentials (worker_id, token_hash, created_at) VALUES (?, ?, ?)",
                         (worker_id, token_hash, now()))

    def worker_by_hash(self, token_hash: str) -> dict | None:
        """The live credential with this hash as {worker_id, token_hash}, or None."""
        with self.connect() as conn:
            row = conn.execute("SELECT worker_id, token_hash FROM worker_credentials WHERE token_hash=? AND revoked_at IS NULL",
                               (token_hash,)).fetchone()
        return None if row is None else dict(row)

    def list_worker_credentials(self) -> "list[dict]":
        with self.connect() as conn:
            rows = conn.execute("SELECT worker_id, created_at, revoked_at FROM worker_credentials ORDER BY created_at, rowid").fetchall()
        return [dict(r) for r in rows]

    def revoke_worker(self, worker_id: str) -> int:
        """Revoke every live credential of `worker_id`; returns how many."""
        with self._transaction() as conn:
            return conn.execute("UPDATE worker_credentials SET revoked_at=? WHERE worker_id=? AND revoked_at IS NULL",
                                (now(), worker_id)).rowcount

    def audit(self, principal: str, action: str, target: str, outcome: str, client: str) -> None:
        """Append one audit row. Callers pass identifiers only: never bodies, tokens or secrets."""
        with self._transaction() as conn:
            conn.execute("INSERT INTO audit_log (ts, principal, action, target, outcome, client) VALUES (?, ?, ?, ?, ?, ?)",
                         (now(), principal, action[:200], target[:500], outcome[:200], client[:100]))

    def audit_after(self, after: int = 0, limit: int = 100) -> "list[dict]":
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
        return [dict(r) for r in rows]

    # --- observability (step 14) ---

    def unexported_runs(self, limit: int = 20) -> "list[str]":  # `list` is shadowed by Ledger.list
        """Terminal runs not yet exported over OTLP, oldest first."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM runs WHERE status IN ('succeeded', 'failed', 'cancelled') "
                "AND id NOT IN (SELECT run_id FROM otel_exports) ORDER BY finished_at, rowid LIMIT ?", (limit,)).fetchall()
        return [r["id"] for r in rows]

    def mark_exported(self, run_id: str) -> None:
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO otel_exports (run_id, exported_at) VALUES (?, ?)", (run_id, now()))

    def metrics(self) -> dict:
        """Aggregates for GET /api/v1/metrics, computed in SQL (events are never loaded into Python)."""
        with self.connect() as conn:
            one = lambda sql: conn.execute(sql).fetchone()
            usage = one("SELECT COALESCE(SUM(json_extract(payload, '$.input_tokens')), 0) AS i, "
                        "COALESCE(SUM(json_extract(payload, '$.output_tokens')), 0) AS o "
                        "FROM run_events WHERE type='usage.recorded'")
            duration = one("SELECT COUNT(*) AS n, COALESCE(SUM((julianday(finished_at) - julianday(created_at)) * 86400.0), 0) AS s "
                           "FROM runs WHERE finished_at IS NOT NULL")
            return {
                "runs": {(r["status"], r["kind"]): r["n"] for r in conn.execute(
                    "SELECT status, kind, COUNT(*) AS n FROM runs GROUP BY status, kind")},
                "tokens": {"input": usage["i"], "output": usage["o"]},
                "tokens_reserved_open": one("SELECT COALESCE(SUM(reserved_tokens), 0) FROM reservations WHERE settled_tokens IS NULL")[0],
                "duration_count": duration["n"], "duration_sum": max(duration["s"], 0.0),
                "exhaustions": {r["terminal_reason"].removesuffix("_exhausted"): r["n"] for r in conn.execute(
                    "SELECT terminal_reason, COUNT(*) AS n FROM runs WHERE terminal_reason IN "
                    "('tokens_exhausted', 'turns_exhausted', 'duration_exhausted') GROUP BY terminal_reason")},
                "worker_lost": one("SELECT COUNT(*) FROM runs WHERE terminal_reason='worker_lost'")[0],
                "tool_calls": {bool(r["e"]): r["n"] for r in conn.execute(
                    "SELECT COALESCE(json_extract(payload, '$.is_error'), 0) AS e, COUNT(*) AS n "
                    "FROM run_events WHERE type='tool.result' GROUP BY e")},
                "artifact_bytes": one("SELECT COALESCE(SUM(size), 0) FROM run_artifacts")[0],
            }

    # --- queue and leases (worker side; see docs/ARCHITECTURE.md remote-worker protocol) ---

    def claim(self, worker_id: str, lease_seconds: float, now: float) -> Lease | None:
        """Lease the oldest queued run to `worker_id`, or None when the queue is empty."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id, task FROM runs WHERE status='admitted' AND task IS NOT NULL "
                "ORDER BY created_at, rowid LIMIT 1").fetchone()
            if row is None:
                return None
            previous = conn.execute("SELECT fencing_token FROM leases WHERE run_id=?", (row["id"],)).fetchone()
            token = (previous["fencing_token"] if previous else 0) + 1
            expires = now + lease_seconds
            conn.execute(
                "INSERT OR REPLACE INTO leases (run_id, worker_id, fencing_token, expires_at, last_wseq) VALUES (?, ?, ?, ?, 0)",
                (row["id"], worker_id, token, expires))
            conn.execute("UPDATE runs SET status='leased' WHERE id=?", (row["id"],))
            _append(conn, row["id"], "lease.granted", {"worker_id": worker_id, "fencing_token": token})
        return Lease(run_id=row["id"], fencing_token=token, expires_at=expires, task=TaskPayload.model_validate_json(row["task"]))

    def heartbeat(self, run_id: str, token: int, lease_seconds: float, now: float, *,
                  worker_id: str | None = None) -> bool:
        """Extend the lease and return whether cancellation was requested. An already-expired lease
        cannot be revived: the sweep settles it."""
        with self._transaction() as conn:
            lease = _check_lease(conn, run_id, token, worker_id)
            if lease["expires_at"] < now:
                raise FencingError("lease expired")
            conn.execute("UPDATE leases SET expires_at=? WHERE run_id=?", (now + lease_seconds, run_id))
            return bool(lease["cancel_requested"])

    def report(self, run_id: str, token: int, wseq: int, type: str, payload: dict | None = None,
               status: str | None = None, *, worker_id: str | None = None) -> bool:
        """Append a worker event exactly once (a duplicate wseq is ignored) and return whether
        cancellation was requested."""
        if type.startswith(RESERVED_PREFIXES):
            raise ValueError(f"Event type is reserved: {type}")
        if status is not None and status not in WORKER_STATUSES:
            raise ValueError(f"Workers may not set status {status}")
        with self._transaction() as conn:
            lease = _check_lease(conn, run_id, token, worker_id)
            last = lease["last_wseq"]
            if wseq <= last:
                return bool(lease["cancel_requested"])
            if wseq != last + 1:
                raise SequenceGap(f"expected wseq {last + 1}, got {wseq}")
            if status:
                conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
            _append(conn, run_id, type, payload)
            conn.execute("UPDATE leases SET last_wseq=? WHERE run_id=?", (wseq, run_id))
            return bool(lease["cancel_requested"])

    def finish(self, run_id: str, token: int, *, tokens_used: int, status: str, terminal_reason: str,
               worker_id: str | None = None) -> None:
        """Settle the run on behalf of the lease holder. A second finish is a FencingError.

        Charges max(reported tokens, sum of usage.recorded events) so a worker cannot under-report."""
        with self._transaction() as conn:
            _check_lease(conn, run_id, token, worker_id)
            # Never charge less than the per-turn usage the worker already reported.
            recorded = sum(
                json.loads(row["payload"]).get("total_tokens", 0)
                for row in conn.execute(
                    "SELECT payload FROM run_events WHERE run_id=? AND type='usage.recorded'", (run_id,))
            )
            _settle(conn, run_id, max(tokens_used, recorded), status, terminal_reason)

    def expire_leases(self, now: float) -> int:
        """Fail runs whose lease lapsed (or that hold none from before leases existed). Their spend
        is unknown, so the whole reservation is charged (fail closed). Queued runs are untouched.
        Legacy queued runs without a stored task could never run and are failed too."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT r.id, r.reserved_tokens, l.worker_id, l.fencing_token FROM runs r "
                "LEFT JOIN leases l ON l.run_id = r.id "
                "WHERE (r.status IN ('leased', 'running') AND (l.run_id IS NULL OR l.expires_at < ?)) "
                "OR (r.status = 'admitted' AND r.task IS NULL)", (now,)).fetchall()
            for r in rows:
                if r["worker_id"] is not None:
                    _append(conn, r["id"], "lease.expired", {"worker_id": r["worker_id"], "fencing_token": r["fencing_token"]})
                _settle(conn, r["id"], r["reserved_tokens"], "failed", "worker_lost")
        return len(rows)


def _check_lease(conn: sqlite3.Connection, run_id: str, token: int, worker_id: str | None = None) -> sqlite3.Row:
    """`worker_id` (the authenticated worker) must be the lease holder; None skips the check (in-process use)."""
    row = conn.execute(
        "SELECT l.*, r.status, r.cancel_requested FROM leases l JOIN runs r ON r.id = l.run_id WHERE l.run_id=?", (run_id,)).fetchone()
    if row is None or row["fencing_token"] != token or row["status"] in TERMINAL:
        raise FencingError(f"Stale or unknown lease for run {run_id}")
    if worker_id is not None and row["worker_id"] != worker_id:
        raise FencingError(f"Lease for run {run_id} is held by another worker")
    return row


def _artifact_guard(conn: sqlite3.Connection, run_id: str, token: int | None, size: int,
                    worker_id: str | None = None) -> None:
    if token is not None:
        _check_lease(conn, run_id, token, worker_id)
    elif conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
        raise ArtifactRejected("unknown_run")
    count, total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM run_artifacts WHERE run_id=?", (run_id,)).fetchone()
    if count + 1 > MAX_RUN_ARTIFACTS or total + size > MAX_RUN_ARTIFACT_BYTES:
        raise ArtifactRejected("run_limit")


def _settle(conn: sqlite3.Connection, run_id: str, tokens_used: int, status: str, terminal_reason: str) -> None:
    if status not in TERMINAL:
        raise ValueError(f"Invalid terminal status: {status}")
    if tokens_used < 0:
        raise ValueError("tokens_used must not be negative")
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
