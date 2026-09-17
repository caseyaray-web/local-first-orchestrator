from __future__ import annotations

from dataclasses import asdict, dataclass
import signal
import threading
import time
from typing import Any, Callable

from .ledger import Ledger
from .scheduler import ProcessNextResult, ProcessNextScheduler, scheduler_observability


@dataclass(frozen=True)
class DaemonHealth:
    worker_id: str
    running: bool
    stop_requested: bool
    iterations: int
    successful_ticks: int
    idle_ticks: int
    busy_ticks: int
    paused_ticks: int
    transient_errors: int
    consecutive_errors: int
    last_status: str | None
    last_stage: str | None
    last_ticket_id: str | None
    last_error: str | None
    last_tick_started_at: float | None
    last_tick_completed_at: float | None


class SchedulerDaemon:
    """Thin operational loop around the proven one-tick scheduler primitive."""

    def __init__(
        self,
        ledger: Ledger,
        scheduler_factory: Callable[[], ProcessNextScheduler],
        external_progress_runner: Callable[[], str | None] | None = None,
        *,
        worker_id: str,
        idle_sleep_seconds: float = 1.0,
        busy_sleep_seconds: float = 0.25,
        error_backoff_seconds: float = 1.0,
        max_error_backoff_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if idle_sleep_seconds < 0 or busy_sleep_seconds < 0 or error_backoff_seconds < 0 or max_error_backoff_seconds < 0:
            raise ValueError("daemon sleep/backoff values must be non-negative")
        if error_backoff_seconds > max_error_backoff_seconds:
            raise ValueError("initial error backoff cannot exceed maximum error backoff")
        self.ledger = ledger
        self.scheduler_factory = scheduler_factory
        self.external_progress_runner = external_progress_runner
        self.worker_id = worker_id
        self.idle_sleep_seconds = idle_sleep_seconds
        self.busy_sleep_seconds = busy_sleep_seconds
        self.error_backoff_seconds = error_backoff_seconds
        self.max_error_backoff_seconds = max_error_backoff_seconds
        self.sleep = sleep
        self.clock = clock
        self._stop = threading.Event()
        self._running = False
        self._iterations = 0
        self._successful_ticks = 0
        self._idle_ticks = 0
        self._busy_ticks = 0
        self._paused_ticks = 0
        self._transient_errors = 0
        self._consecutive_errors = 0
        self._last_result: ProcessNextResult | None = None
        self._last_error: str | None = None
        self._last_tick_started_at: float | None = None
        self._last_tick_completed_at: float | None = None

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def health(self) -> DaemonHealth:
        result = self._last_result
        return DaemonHealth(
            worker_id=self.worker_id,
            running=self._running,
            stop_requested=self.stop_requested,
            iterations=self._iterations,
            successful_ticks=self._successful_ticks,
            idle_ticks=self._idle_ticks,
            busy_ticks=self._busy_ticks,
            paused_ticks=self._paused_ticks,
            transient_errors=self._transient_errors,
            consecutive_errors=self._consecutive_errors,
            last_status=None if result is None else result.status,
            last_stage=None if result is None else result.stage,
            last_ticket_id=None if result is None else result.ticket_id,
            last_error=self._last_error,
            last_tick_started_at=self._last_tick_started_at,
            last_tick_completed_at=self._last_tick_completed_at,
        )

    def status(self) -> dict[str, Any]:
        return {
            "health": asdict(self.health()),
            "scheduler": scheduler_observability(self.ledger),
        }

    def _sleep_after_result(self, result: ProcessNextResult) -> None:
        if self.stop_requested:
            return
        if result.status in {"idle", "no_work"}:
            self._idle_ticks += 1
            self.sleep(self.idle_sleep_seconds)
        elif result.status == "busy":
            self._busy_ticks += 1
            self.sleep(self.busy_sleep_seconds)
        elif result.status == "paused":
            self._paused_ticks += 1
            self.sleep(self.idle_sleep_seconds)

    def run_iteration(self) -> ProcessNextResult:
        if self.stop_requested:
            return ProcessNextResult("stopped")
        self._iterations += 1
        self._last_tick_started_at = self.clock()
        try:
            result = self.scheduler_factory().process_next()
            if result.status in {"idle", "no_work"} and self.external_progress_runner is not None:
                external_ticket_id = self.external_progress_runner()
                if external_ticket_id is not None:
                    result = ProcessNextResult("external_progress", "hermes_dispatch", external_ticket_id)
        except Exception as exc:
            self._transient_errors += 1
            self._consecutive_errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._last_tick_completed_at = self.clock()
            delay = min(
                self.max_error_backoff_seconds,
                self.error_backoff_seconds * (2 ** max(0, self._consecutive_errors - 1)),
            )
            if not self.stop_requested:
                self.sleep(delay)
            raise
        self._last_result = result
        self._last_error = None
        self._successful_ticks += 1
        self._consecutive_errors = 0
        self._last_tick_completed_at = self.clock()
        self._sleep_after_result(result)
        return result

    def run(self, *, max_iterations: int | None = None, continue_on_error: bool = True) -> DaemonHealth:
        if max_iterations is not None and max_iterations < 0:
            raise ValueError("max_iterations must be non-negative")
        self._running = True
        try:
            while not self.stop_requested and (max_iterations is None or self._iterations < max_iterations):
                try:
                    self.run_iteration()
                except Exception:
                    if not continue_on_error:
                        raise
        finally:
            self._running = False
        return self.health()

    def install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda _signum, _frame: self.request_stop())
        return previous

    @staticmethod
    def restore_signal_handlers(previous: dict[int, Any]) -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
