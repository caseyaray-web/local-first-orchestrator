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
    state: str
    consecutive_undispatchable: int
    last_reason_category: str | None
    last_reason: str | None
    last_error: str | None
    last_tick_started_at: float | None
    last_tick_completed_at: float | None


@dataclass(frozen=True)
class ExternalProgressResult:
    """Bounded result of the authorized Hermes progress probe."""

    status: str
    stage: str | None = None
    ticket_id: str | None = None
    reason_category: str | None = None
    reason: str | None = None


class SchedulerDaemon:
    """Thin operational loop around the proven one-tick scheduler primitive."""

    def __init__(
        self,
        ledger: Ledger,
        scheduler_factory: Callable[[], ProcessNextScheduler],
        external_progress_runner: Callable[[], ExternalProgressResult | str | dict[str, Any] | None] | None = None,
        *,
        worker_id: str,
        idle_sleep_seconds: float = 1.0,
        busy_sleep_seconds: float = 0.25,
        error_backoff_seconds: float = 1.0,
        max_error_backoff_seconds: float = 30.0,
        undispatchable_backoff_seconds: float = 1.0,
        max_undispatchable_backoff_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if idle_sleep_seconds < 0 or busy_sleep_seconds < 0 or error_backoff_seconds < 0 or max_error_backoff_seconds < 0 or undispatchable_backoff_seconds < 0 or max_undispatchable_backoff_seconds < 0:
            raise ValueError("daemon sleep/backoff values must be non-negative")
        if error_backoff_seconds > max_error_backoff_seconds:
            raise ValueError("initial error backoff cannot exceed maximum error backoff")
        if undispatchable_backoff_seconds > max_undispatchable_backoff_seconds:
            raise ValueError("initial undispatchable backoff cannot exceed maximum undispatchable backoff")
        self.ledger = ledger
        self.scheduler_factory = scheduler_factory
        self.external_progress_runner = external_progress_runner
        self.worker_id = worker_id
        self.idle_sleep_seconds = idle_sleep_seconds
        self.busy_sleep_seconds = busy_sleep_seconds
        self.error_backoff_seconds = error_backoff_seconds
        self.max_error_backoff_seconds = max_error_backoff_seconds
        self.undispatchable_backoff_seconds = undispatchable_backoff_seconds
        self.max_undispatchable_backoff_seconds = max_undispatchable_backoff_seconds
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
        self._state = "starting"
        self._consecutive_undispatchable = 0
        self._last_reason_category: str | None = None
        self._last_reason: str | None = None
        self._last_tick_started_at: float | None = None
        self._last_tick_completed_at: float | None = None
        # Do not erase a prior worker record during restart.  The previous
        # result/error remains operator-visible until the first new tick.
        persisted = self.ledger.connection.execute("SELECT * FROM daemon_status WHERE worker_id=?", (self.worker_id,)).fetchone()
        if persisted is None:
            self._persist_status()
        else:
            validated = Ledger._operator_daemon_row(persisted)
            if validated.get("status") == "authorized_undispatchable":
                self._consecutive_undispatchable = int(validated["consecutive_undispatchable"])

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
            state=self._state,
            consecutive_undispatchable=self._consecutive_undispatchable,
            last_reason_category=self._last_reason_category,
            last_reason=self._last_reason,
            last_error=self._last_error,
            last_tick_started_at=self._last_tick_started_at,
            last_tick_completed_at=self._last_tick_completed_at,
        )

    def status(self) -> dict[str, Any]:
        return {
            "health": asdict(self.health()),
            "scheduler": scheduler_observability(self.ledger),
            "daemon_status": self.ledger.daemon_status_rows(limit=1),
        }

    @staticmethod
    def _external_result(value: ExternalProgressResult | str | dict[str, Any] | None) -> ExternalProgressResult:
        if value is None:
            return ExternalProgressResult("no_authorized_work")
        if isinstance(value, str):
            return ExternalProgressResult("dispatched", stage="hermes_dispatch", ticket_id=value)
        if isinstance(value, ExternalProgressResult):
            outcome = value
        elif isinstance(value, dict):
            outcome = ExternalProgressResult(
                status=str(value.get("status") or ""),
                stage=None if value.get("stage") is None else str(value["stage"]),
                ticket_id=None if value.get("ticket_id") is None else str(value["ticket_id"]),
                reason_category=None if value.get("reason_category") is None else str(value["reason_category"]),
                reason=None if value.get("reason") is None else str(value["reason"]),
            )
        else:
            raise RuntimeError("external progress runner returned an unsupported result")
        if outcome.status not in {"no_authorized_work", "dispatched", "authorized_undispatchable"}:
            raise RuntimeError("external progress runner returned an unsupported status")
        if outcome.status == "dispatched" and not outcome.ticket_id:
            raise RuntimeError("external progress runner reported dispatch without a task")
        return outcome

    def _persist_status(self) -> None:
        result = self._last_result
        self.ledger.upsert_daemon_status(
            worker_id=self.worker_id,
            status=self._state,
            last_status=None if result is None else result.status,
            last_stage=None if result is None else result.stage,
            last_ticket_id=None if result is None else result.ticket_id,
            reason_category=self._last_reason_category,
            reason=self._last_reason,
            consecutive_undispatchable=self._consecutive_undispatchable,
            iterations=self._iterations,
            successful_ticks=self._successful_ticks,
            idle_ticks=self._idle_ticks,
            busy_ticks=self._busy_ticks,
            paused_ticks=self._paused_ticks,
            transient_errors=self._transient_errors,
            consecutive_errors=self._consecutive_errors,
            last_error=self._last_error,
            last_tick_started_at=self._last_tick_started_at,
            last_tick_completed_at=self._last_tick_completed_at,
        )

    def _sleep_after_result(self, result: ProcessNextResult) -> None:
        if self.stop_requested:
            return
        if result.status == "authorized_undispatchable":
            delay = min(self.max_undispatchable_backoff_seconds, self.undispatchable_backoff_seconds * (2 ** max(0, self._consecutive_undispatchable - 1)))
            self.sleep(delay)
        elif result.status in {"idle", "no_work"}:
            self.sleep(self.idle_sleep_seconds)
        elif result.status == "busy":
            self.sleep(self.busy_sleep_seconds)
        elif result.status == "paused":
            self.sleep(self.idle_sleep_seconds)

    def run_iteration(self) -> ProcessNextResult:
        if self.stop_requested:
            return ProcessNextResult("stopped")
        self._iterations += 1
        self._last_tick_started_at = self.clock()
        try:
            result = self.scheduler_factory().process_next()
            if result.status in {"idle", "no_work"} and self.external_progress_runner is not None:
                external = self._external_result(self.external_progress_runner())
                if external.status == "dispatched":
                    result = ProcessNextResult("external_progress", external.stage or "hermes_dispatch", external.ticket_id)
                    self._consecutive_undispatchable = 0
                    self._last_reason_category = None
                    self._last_reason = None
                elif external.status == "authorized_undispatchable":
                    self._consecutive_undispatchable += 1
                    category = external.reason_category or "no_dispatchable_plan"
                    self._last_reason_category = category if category.replace("_", "").replace("-", "").replace(":", "").isalnum() and len(category) <= 80 else "no_dispatchable_plan"
                    # Keep operator state categorical. Do not copy Hermes/model
                    # output or card text into the durable status record.
                    self._last_reason = "authorized work has no dispatchable plan"
                    result = ProcessNextResult("authorized_undispatchable", external.stage or "hermes_dispatch", external.ticket_id)
                else:
                    self._consecutive_undispatchable = 0
                    self._last_reason_category = None
                    self._last_reason = None
            else:
                self._consecutive_undispatchable = 0
                self._last_reason_category = None
                self._last_reason = None
        except Exception as exc:
            self._transient_errors += 1
            self._consecutive_errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._state = "error"
            self._last_tick_completed_at = self.clock()
            self._persist_status()
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
        self._state = result.status
        self._last_tick_completed_at = self.clock()
        if not self.stop_requested:
            if result.status in {"idle", "no_work"}:
                self._idle_ticks += 1
            elif result.status == "busy":
                self._busy_ticks += 1
            elif result.status == "paused":
                self._paused_ticks += 1
        self._persist_status()
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
