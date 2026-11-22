"""SQLite run ledger. Plain sqlite3; migrations are versioned with PRAGMA user_version.

Money is stored as integer micro-USD, never REAL. run_events is append-only: triggers
reject UPDATE and DELETE, and no code path issues either.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

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
]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
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
    def transaction(self):
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

    def ensure_account(self, account_id: str, limit_micro: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO budget_accounts (id, limit_micro) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET limit_micro=excluded.limit_micro",
                (account_id, limit_micro),
            )

    def account(self, account_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM budget_accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC")]

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            run = dict(row)
            run["events"] = [
                {**dict(e), "payload": json.loads(e["payload"])}
                for e in conn.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY seq", (run_id,))
            ]
        return run


def append_event(conn: sqlite3.Connection, run_id: str, type_: str, payload: dict | None = None) -> int:
    """Append inside the caller's transaction; seq is gapless per run."""
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id=?", (run_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO run_events (run_id, seq, type, ts, payload) VALUES (?, ?, ?, ?, ?)",
        (run_id, seq, type_, now(), json.dumps(payload or {}, sort_keys=True)),
    )
    return seq


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
