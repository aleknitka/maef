"""Local thread-pool runner. Interim exception to ADR-0002: a tool-less model call runs no
untrusted code, so it may run in the API process. Replace with a worker once tools exist.
Execution never happens inside the HTTP request handler."""
from concurrent.futures import ThreadPoolExecutor

from .admission import settle
from .harness import PreparedRun, execute
from .store import Store


class Runner:
    def __init__(self, store: Store, max_workers: int = 2):
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="maef-run")

    def submit(self, run: PreparedRun, adapter):
        return self._pool.submit(execute, self.store, run, adapter)

    def sweep_orphans(self) -> int:
        """Runs left open by a previous process are failed. Their spend is unknown, so the
        whole reservation is charged (fail closed)."""
        orphans = [r for r in self.store.list_runs() if r["status"] in ("admitted", "running")]
        for r in orphans:
            settle(self.store, r["id"], tokens_used=r["reserved_tokens"], status="failed", terminal_reason="worker_lost")
        return len(orphans)
