"""PostgreSQL dialect for the Ledger (psycopg 3, imported lazily so SQLite-only installs do not need it).

Same logical schema as the SQLite adapter at its latest version; Postgres starts fresh at the latest
schema, so MIGRATIONS holds one script per version rather than the SQLite history. Differences: BIGINT
token columns, JSONB payloads, an `ord` identity column for insertion order (SQLite's rowid), a
`schema_version` table, and a trigger function that rejects UPDATE/DELETE/TRUNCATE on append-only tables.
Concurrency comes from row locks (SELECT ... FOR UPDATE, FOR UPDATE SKIP LOCKED) in READ COMMITTED.
"""
import json
from contextlib import contextmanager

MIGRATIONS = [
    """
    CREATE TABLE budget_accounts (
        id TEXT PRIMARY KEY,
        limit_tokens BIGINT NOT NULL CHECK (limit_tokens >= 0),
        reserved_tokens BIGINT NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0),
        spent_tokens BIGINT NOT NULL DEFAULT 0 CHECK (spent_tokens >= 0)
    );
    CREATE TABLE runs (
        ord BIGINT GENERATED ALWAYS AS IDENTITY,
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES budget_accounts(id),
        agent_id TEXT NOT NULL,
        git_rev TEXT,
        dirty INTEGER NOT NULL,
        definition_digest TEXT NOT NULL,
        profile_digest TEXT NOT NULL,
        effective_budget JSONB NOT NULL,
        reserved_tokens BIGINT NOT NULL,
        tokens_used BIGINT,
        terminal_reason TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT,
        task TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        kind TEXT NOT NULL DEFAULT 'agent'
    );
    CREATE INDEX runs_queue ON runs(status, created_at, ord);
    CREATE TABLE run_events (
        run_id TEXT NOT NULL REFERENCES runs(id),
        seq BIGINT NOT NULL,
        type TEXT NOT NULL,
        ts TEXT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (run_id, seq)
    );
    CREATE TABLE reservations (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        account_id TEXT NOT NULL REFERENCES budget_accounts(id),
        reserved_tokens BIGINT NOT NULL,
        settled_tokens BIGINT,
        released_tokens BIGINT
    );
    CREATE TABLE leases (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        worker_id TEXT NOT NULL,
        fencing_token BIGINT NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL,
        last_wseq BIGINT NOT NULL DEFAULT 0
    );
    CREATE TABLE run_artifacts (
        ord BIGINT GENERATED ALWAYS AS IDENTITY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        seq BIGINT NOT NULL,
        digest TEXT NOT NULL,
        size BIGINT NOT NULL,
        mime TEXT NOT NULL,
        name TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    );
    CREATE INDEX run_artifacts_digest ON run_artifacts(digest);
    CREATE TABLE api_tokens (
        ord BIGINT GENERATED ALWAYS AS IDENTITY,
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        scopes TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    );
    CREATE TABLE worker_credentials (
        ord BIGINT GENERATED ALWAYS AS IDENTITY,
        worker_id TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    );
    CREATE INDEX worker_credentials_worker ON worker_credentials(worker_id);
    CREATE TABLE audit_log (
        seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        ts TEXT NOT NULL,
        principal TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        outcome TEXT NOT NULL,
        client TEXT NOT NULL
    );
    CREATE TABLE otel_exports (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        exported_at TEXT NOT NULL
    );
    CREATE FUNCTION maef_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
    END;
    $$;
    CREATE TRIGGER run_events_append_only BEFORE UPDATE OR DELETE ON run_events
        FOR EACH ROW EXECUTE FUNCTION maef_append_only();
    CREATE TRIGGER run_events_no_truncate BEFORE TRUNCATE ON run_events
        FOR EACH STATEMENT EXECUTE FUNCTION maef_append_only();
    CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION maef_append_only();
    CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION maef_append_only();
    """,
]

_MIGRATION_LOCK = 727_001  # pg_advisory_xact_lock key: serialises concurrent first starts


class _Conn:
    """A psycopg connection that speaks the ledger's `?` placeholders and returns dict rows."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=None):
        if params is None:
            return self._raw.execute(sql)  # no parameters: multi-statement scripts are allowed
        return self._raw.execute(sql.replace("?", "%s"), params)


class PostgresDialect:
    name = "postgres"
    order_col = "ord"
    row_lock = " FOR UPDATE"
    skip_locked = " FOR UPDATE SKIP LOCKED"
    jsonp = "CAST(? AS JSONB)"

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:  # pragma: no cover - depends on the install
            raise RuntimeError("A postgresql:// MAEF_DB needs the optional driver: pip install 'maef-control-plane[postgres]'") from e
        self.url = url
        self._psycopg, self._dict_row = psycopg, dict_row
        self.database_error = psycopg.DatabaseError
        self.path = None

    @contextmanager
    def connect(self):
        """A fresh connection per use. Autocommit: transactions are explicit BEGIN/COMMIT, like SQLite."""
        raw = self._psycopg.connect(self.url, autocommit=True, row_factory=self._dict_row)
        try:
            yield _Conn(raw)
        finally:
            raw.close()

    @staticmethod
    def begin(conn) -> None:
        conn.execute("BEGIN")

    @staticmethod
    def commit(conn) -> None:
        conn.execute("COMMIT")

    @staticmethod
    def rollback(conn) -> None:
        conn.execute("ROLLBACK")

    def migrate(self, conn) -> None:
        self.begin(conn)
        try:
            conn.execute(f"SELECT pg_advisory_xact_lock({_MIGRATION_LOCK})")
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            version = row["v"] or 0
            for n, script in enumerate(MIGRATIONS[version:], start=version + 1):
                conn.execute(script)
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (n,))
        except BaseException:
            self.rollback(conn)
            raise
        self.commit(conn)

    @staticmethod
    def dump_json(value: dict) -> str:
        return json.dumps(value, sort_keys=True)

    @staticmethod
    def load_json(value):
        return json.loads(value) if isinstance(value, str) else value  # psycopg already decodes JSONB

    @staticmethod
    def json_int(column: str, key: str) -> str:
        return f"CAST({column}->>'{key}' AS BIGINT)"

    @staticmethod
    def json_bool(column: str, key: str) -> str:
        return f"COALESCE(CAST(CAST({column}->>'{key}' AS BOOLEAN) AS INTEGER), 0)"

    @staticmethod
    def seconds_between(start: str, end: str) -> str:
        return f"EXTRACT(EPOCH FROM (CAST({end} AS TIMESTAMPTZ) - CAST({start} AS TIMESTAMPTZ)))"
