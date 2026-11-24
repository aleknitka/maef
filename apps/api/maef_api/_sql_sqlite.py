"""SQLite dialect for the Ledger: plain sqlite3, WAL, BEGIN IMMEDIATE, PRAGMA user_version migrations.

Usage is stored as integer tokens, never REAL. run_events and audit_log are append-only via triggers.
"""
import json
import sqlite3
from contextlib import contextmanager
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



class SqliteDialect:
    name = "sqlite"
    database_error = sqlite3.DatabaseError
    order_col = "rowid"      # insertion order of a table
    row_lock = ""            # BEGIN IMMEDIATE already serialises writers
    skip_locked = ""
    jsonp = "?"              # placeholder for a JSON text parameter

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.url = str(path)
        if self.url != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

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

    @staticmethod
    def begin(conn) -> None:
        conn.execute("BEGIN IMMEDIATE")  # takes the write lock up front, so check-then-insert is atomic

    @staticmethod
    def commit(conn) -> None:
        conn.execute("COMMIT")

    @staticmethod
    def rollback(conn) -> None:
        conn.execute("ROLLBACK")

    def migrate(self, conn) -> None:
        self.begin(conn)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for n, script in enumerate(MIGRATIONS[version:], start=version + 1):
                for statement in _split(script):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={n}")
        except BaseException:
            self.rollback(conn)
            raise
        self.commit(conn)

    @staticmethod
    def dump_json(value: dict) -> str:
        return json.dumps(value, sort_keys=True)

    @staticmethod
    def load_json(value):
        return json.loads(value)

    @staticmethod
    def json_int(column: str, key: str) -> str:
        return f"json_extract({column}, '$.{key}')"

    @staticmethod
    def json_float(column: str, key: str) -> str:
        return f"json_extract({column}, '$.{key}')"

    @staticmethod
    def json_bool(column: str, key: str) -> str:
        return f"COALESCE(json_extract({column}, '$.{key}'), 0)"

    @staticmethod
    def seconds_between(start: str, end: str) -> str:
        return f"(julianday({end}) - julianday({start})) * 86400.0"


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
