"""One queue and one thread for every action, so nothing that touches git, /config or Home Assistant overlaps."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

log = logging.getLogger(__name__)


class Worker:
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker")
        self._lock = threading.Lock()
        self._jobs: list[str] = []

    def submit[T](self, job: Callable[[], T], label: str) -> Future[T]:
        """Queues a job; the label is what the panel shows while it waits or runs."""
        with self._lock:
            self._jobs.append(label)
        return self._pool.submit(self._run, job, label)

    def _run[T](self, job: Callable[[], T], label: str) -> T:
        try:
            return job()
        except Exception:
            log.exception("%s failed", label)
            raise
        finally:
            with self._lock:
                self._jobs.remove(label)

    @property
    def idle(self) -> bool:
        with self._lock:
            return not self._jobs

    @property
    def running(self) -> list[str]:
        """The jobs waiting or running, oldest first."""
        with self._lock:
            return list(self._jobs)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
