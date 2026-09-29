from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import threading
import time

import pytest

from local_first_orchestrator.daemon import (
    CoordinatorLoop,
    InstanceLockError,
    instance_lock,
)


def _hold_lock(path: str, ready, release) -> None:
    with instance_lock(path):
        ready.set()
        release.wait(5)


def test_instance_lock_rejects_second_process_then_reopens_after_release(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_lock, args=(str(path), ready, release))
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(InstanceLockError):
            instance_lock(path).acquire()
        release.set()
        process.join(5)
        assert process.exitcode == 0
        with instance_lock(path) as lock:
            lock.assert_held()
    finally:
        release.set()
        process.join(5)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_instance_lock_fails_closed_when_lock_path_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.lock"
    with instance_lock(path) as lock:
        replacement = tmp_path / "replacement.lock"
        replacement.write_text("replacement")
        replacement.chmod(0o600)
        os.replace(replacement, path)
        with pytest.raises(InstanceLockError):
            lock.assert_held()
    with instance_lock(path) as reopened:
        reopened.assert_held()


def test_instance_lock_rejects_symlink_and_insecure_mode(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x")
    path = tmp_path / "coordinator.lock"
    path.symlink_to(target)
    with pytest.raises(InstanceLockError):
        instance_lock(path).acquire()

    path.unlink()
    path.write_text("insecure")
    path.chmod(0o644)
    with pytest.raises(InstanceLockError):
        instance_lock(path).acquire()


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class RecordingWake:
    def __init__(self) -> None:
        self.waits: list[float] = []
        self._event = threading.Event()

    def wait(self, delay: float) -> bool:
        self.waits.append(delay)
        return self._event.wait(0.001)

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()


def test_tick_holds_guard_for_one_injected_bounded_action(tmp_path: Path) -> None:
    calls: list[str] = []

    def action(guard) -> str:
        guard()
        calls.append("action")
        return "done"

    loop = CoordinatorLoop(action, lock_path=tmp_path / "coordinator.lock")
    assert loop.tick() == "done"
    assert calls == ["action"]
    health = loop.health()
    assert health.ticks == 1
    assert health.successful_ticks == 1
    assert not health.running


def test_error_releases_lock_for_a_subsequent_tick(tmp_path: Path) -> None:
    def fails(_guard) -> None:
        raise RuntimeError("boom")

    path = tmp_path / "coordinator.lock"
    loop = CoordinatorLoop(fails, lock_path=path)
    with pytest.raises(RuntimeError, match="boom"):
        loop.tick()
    with instance_lock(path) as lock:
        lock.assert_held()
    assert loop.health().errors == 1


def test_run_wakes_and_request_stop_cancels_wait_without_busy_loop(tmp_path: Path) -> None:
    calls: list[int] = []
    wake = RecordingWake()
    loop: CoordinatorLoop

    def action(guard) -> None:
        guard()
        calls.append(1)
        if len(calls) == 1:
            loop.wake()
        else:
            loop.request_stop()

    loop = CoordinatorLoop(action, lock_path=tmp_path / "coordinator.lock", interval_seconds=3, wake_event=wake)
    health = loop.run()
    assert len(calls) == 2
    assert wake.waits == [3.0]
    assert health.stop_requested
    assert not health.running


def test_run_uses_finite_backoff_after_error_and_stops_at_bound(tmp_path: Path) -> None:
    wake = RecordingWake()
    calls = 0

    def action(_guard) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    loop = CoordinatorLoop(
        action,
        lock_path=tmp_path / "coordinator.lock",
        interval_seconds=5,
        error_backoff_seconds=0.25,
        max_error_backoff_seconds=1,
        wake_event=wake,
    )
    health = loop.run(max_ticks=2)
    assert calls == 2
    assert wake.waits == [0.25]
    assert health.errors == 2
