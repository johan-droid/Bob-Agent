"""Background-service concurrency runner (single-process task manager)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ServiceSpec:
    """Registration for one background service."""

    name: str
    tick: Callable[[threading.Event], None]
    interval_seconds: float = 10.0
    max_in_flight: int = 1
    max_restarts: int = -1
    backoff_base: float = 1.0
    backoff_cap: float = 60.0
    run_immediately: bool = True


@dataclass
class ServiceStatus:
    running: bool = False
    restarts: int = 0
    last_error: str | None = None
    last_heartbeat: float | None = None
    last_duration_s: float | None = None
    stopped: bool = False


class BackgroundServiceRunner:
    """Owns the lifecycle of every background service in this process."""

    def __init__(self) -> None:
        self._specs: dict[str, ServiceSpec] = {}
        self._status: dict[str, ServiceStatus] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started = False

    def register(self, spec: ServiceSpec) -> None:
        with self._lock:
            if spec.name in self._specs:
                raise ValueError(f"background service '{spec.name}' already registered")
            self._specs[spec.name] = spec
            self._status[spec.name] = ServiceStatus()
            self._semaphores[spec.name] = threading.BoundedSemaphore(
                max(1, int(spec.max_in_flight))
            )

    def start_all(self) -> list[str]:
        """Start one daemon thread per registered service. Idempotent."""
        with self._lock:
            if self._started:
                return []
            self._stop.clear()
            self._started = True
            names = list(self._specs)
        started: list[str] = []
        for name in names:
            thread = threading.Thread(
                target=self._supervise, args=(name,), name=f"bob-svc-{name}", daemon=True
            )
            with self._lock:
                self._threads[name] = thread
            thread.start()
            started.append(name)
        return started

    def stop_all(self, timeout: float = 10.0) -> None:
        """Signal shutdown and join every service thread."""
        self._stop.set()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            alive = [t.name for t in self._threads.values() if t.is_alive()]
            for st in self._status.values():
                st.running = False
                st.stopped = True
            self._started = False
        if alive:
            logger.warning("background services still alive after stop: %s", alive)

    def status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                name: {
                    "running": st.running,
                    "restarts": st.restarts,
                    "last_error": st.last_error,
                    "last_heartbeat": st.last_heartbeat,
                    "last_duration_s": st.last_duration_s,
                    "stopped": st.stopped,
                }
                for name, st in self._status.items()
            }

    def _supervise(self, name: str) -> None:
        spec = self._specs[name]
        st = self._status[name]
        sem = self._semaphores[name]
        backoff = spec.backoff_base
        if spec.run_immediately:
            self._run_once(name, spec, st, sem)
            backoff = self._cooldown(st, spec, backoff)
        while not self._stop.is_set():
            if self._stop.wait(timeout=max(0.1, spec.interval_seconds)):
                break
            self._run_once(name, spec, st, sem)
            backoff = self._cooldown(st, spec, backoff)

    def _run_once(
        self, name: str, spec: ServiceSpec, st: ServiceStatus, sem: threading.BoundedSemaphore
    ) -> None:
        if not sem.acquire(blocking=False):
            logger.debug("service '%s' tick skipped: at cap", name)
            return
        st.running = True
        started = time.monotonic()
        try:
            spec.tick(self._stop)
            st.last_heartbeat = time.time()
            st.last_duration_s = time.monotonic() - started
            st.last_error = None
        except Exception as exc:
            st.last_error = f"{type(exc).__name__}: {exc}"[:500]
            st.restarts += 1
            logger.exception("background service '%s' tick crashed", name)
            if 0 <= spec.max_restarts < st.restarts:
                logger.error("service '%s' exceeded max restarts; parking", name)
                self._stop.wait(timeout=spec.backoff_cap)
        finally:
            try:
                sem.release()
            except ValueError:
                pass

    @staticmethod
    def _cooldown(st: ServiceStatus, spec: ServiceSpec, current: float) -> float:
        if st.last_error is not None:
            time.sleep(min(current, spec.backoff_cap))
            return min(current * 2.0, spec.backoff_cap)
        return spec.backoff_base


_runner: BackgroundServiceRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> BackgroundServiceRunner:
    """Process-wide singleton accessor."""
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = BackgroundServiceRunner()
        return _runner


__all__ = [
    "BackgroundServiceRunner",
    "ServiceSpec",
    "ServiceStatus",
    "get_runner",
]
