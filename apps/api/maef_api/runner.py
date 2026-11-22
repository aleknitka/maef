"""Local thread-pool runner. Interim exception to ADR-0002: a tool-less model call runs no
untrusted code, so it may run in the API process. Replace with a worker once tools exist.
Execution never happens inside the HTTP request handler."""
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor


class Runner:
    def __init__(self, max_workers: int = 2):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="maef-run")

    def submit(self, fn: Callable[[], None]) -> Future:
        return self._pool.submit(fn)
