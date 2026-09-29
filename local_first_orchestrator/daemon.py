from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import math
import os
from pathlib import Path
import stat
import threading
import time
from numbers import Real
from typing import Any, Callable


class InstanceLockError(RuntimeError):
    """The configured coordinator instance cannot safely mutate state."""


@dataclass(frozen=True)
class _LockIdentity:
    path: str
    device: int
    inode: int
    token: object


_process_locks: dict[str, _LockIdentity] = {}
_process_locks_guard = threading.RLock()


def _finite_non_negative_real(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite non-negative real")
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite non-negative real") from None
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} must be a finite non-negative real")
    return 0.0 if normalized == 0.0 else normalized


def bounded_backoff(base: float, failures: int, maximum: float) -> float:
    """Return a finite capped exponential delay; the first failure uses base."""
    base = _finite_non_negative_real(base, name="backoff base")
    maximum = _finite_non_negative_real(maximum, name="backoff maximum")
    if type(failures) is not int or failures < 1:
        raise ValueError("failures must be a positive built-in int")
    if base > maximum:
        raise ValueError("backoff base cannot exceed maximum")
    if base == 0.0:
        return 0.0
    try:
        delay = math.ldexp(base, failures - 1)
    except OverflowError:
        return maximum
    return min(maximum, delay) if math.isfinite(delay) else maximum


class InstanceLock:
    """A non-reentrant flock tied to one stable, user-owned lock-file inode."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("instance lock path must be absolute")
        self.path = candidate
        self._path_key = os.fspath(candidate)
        self._fd: int | None = None
        self._identity: _LockIdentity | None = None

    @staticmethod
    def _validate_user_owned_regular(st: os.stat_result, *, subject: str) -> None:
        if not stat.S_ISREG(st.st_mode):
            raise InstanceLockError(f"{subject} must be a regular file")
        if st.st_uid != os.geteuid():
            raise InstanceLockError(f"{subject} must be owned by the current user")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise InstanceLockError(f"{subject} must have mode 0600")

    def _validate_parent(self) -> None:
        try:
            parent = os.lstat(self.path.parent)
        except OSError as exc:
            raise InstanceLockError("instance lock parent is unavailable") from exc
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
            raise InstanceLockError("instance lock parent must be a directory, not a symlink")
        if parent.st_uid != os.geteuid():
            raise InstanceLockError("instance lock parent must be owned by the current user")

    def acquire(self) -> "InstanceLock":
        if self._fd is not None:
            raise InstanceLockError("instance lock is already acquired")
        self._validate_parent()
        with _process_locks_guard:
            if self._path_key in _process_locks:
                raise InstanceLockError("instance lock is already held in this process")
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EACCES, errno.ENOENT}:
                raise InstanceLockError("instance lock path is unsafe or unavailable") from exc
            raise
        try:
            fd_stat = os.fstat(fd)
            self._validate_user_owned_regular(fd_stat, subject="instance lock")
            path_stat = os.lstat(self.path)
            self._validate_user_owned_regular(path_stat, subject="instance lock")
            if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                raise InstanceLockError("instance lock path changed during acquisition")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstanceLockError("instance lock is held by another process") from exc
            # Detect replacement while waiting for flock too.
            path_stat = os.lstat(self.path)
            if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                raise InstanceLockError("instance lock path changed during acquisition")
            identity = _LockIdentity(self._path_key, fd_stat.st_dev, fd_stat.st_ino, object())
            with _process_locks_guard:
                if self._path_key in _process_locks:
                    raise InstanceLockError("instance lock is already held in this process")
                _process_locks[self._path_key] = identity
            self._fd = fd
            self._identity = identity
            return self
        except BaseException:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            raise

    def assert_held(self) -> None:
        fd, identity = self._fd, self._identity
        if fd is None or identity is None:
            raise InstanceLockError("instance lock is not held")
        with _process_locks_guard:
            if _process_locks.get(self._path_key) is not identity:
                raise InstanceLockError("instance lock ownership is no longer valid")
        try:
            fd_stat = os.fstat(fd)
            path_stat = os.lstat(self.path)
        except OSError as exc:
            raise InstanceLockError("instance lock path is unavailable") from exc
        self._validate_user_owned_regular(fd_stat, subject="instance lock")
        self._validate_user_owned_regular(path_stat, subject="instance lock")
        if (fd_stat.st_dev, fd_stat.st_ino) != (identity.device, identity.inode):
            raise InstanceLockError("instance lock descriptor changed")
        if (path_stat.st_dev, path_stat.st_ino) != (identity.device, identity.inode):
            raise InstanceLockError("instance lock path changed while held")

    def close(self) -> None:
        fd, identity = self._fd, self._identity
        self._fd = None
        self._identity = None
        if identity is not None:
            with _process_locks_guard:
                if _process_locks.get(self._path_key) is identity:
                    del _process_locks[self._path_key]
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def instance_lock(path: str | os.PathLike[str]) -> InstanceLock:
    """Create the explicit instance lock; callers hold it only around mutations."""
    return InstanceLock(path)


@dataclass(frozen=True)
class CoordinatorHealth:
    running: bool
    stop_requested: bool
    ticks: int
    successful_ticks: int
    errors: int
    consecutive_errors: int
    last_error: str | None
    last_tick_started_at: float | None
    last_tick_completed_at: float | None


class CoordinatorLoop:
    """Wakeable bounded coordinator driver with no board, model, or provider calls."""

    def __init__(
        self,
        coordinator_tick: Callable[[Callable[[], None]], Any],
        *,
        lock_path: str | os.PathLike[str],
        interval_seconds: float = 1.0,
        error_backoff_seconds: float = 1.0,
        max_error_backoff_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        wake_event: threading.Event | None = None,
    ) -> None:
        if not callable(coordinator_tick):
            raise ValueError("coordinator_tick must be callable")
        self.interval_seconds = _finite_non_negative_real(interval_seconds, name="interval_seconds")
        if self.interval_seconds == 0:
            raise ValueError("interval_seconds must be greater than zero to prevent a busy loop")
        self.error_backoff_seconds = _finite_non_negative_real(error_backoff_seconds, name="error_backoff_seconds")
        self.max_error_backoff_seconds = _finite_non_negative_real(max_error_backoff_seconds, name="max_error_backoff_seconds")
        if self.error_backoff_seconds > self.max_error_backoff_seconds:
            raise ValueError("error_backoff_seconds cannot exceed max_error_backoff_seconds")
        self._coordinator_tick = coordinator_tick
        self._lock_path = lock_path
        self._clock = clock
        self._wake = wake_event if wake_event is not None else threading.Event()
        self._stop = threading.Event()
        self._run_guard = threading.Lock()
        self._running = False
        self._ticks = 0
        self._successful_ticks = 0
        self._errors = 0
        self._consecutive_errors = 0
        self._last_error: str | None = None
        self._last_tick_started_at: float | None = None
        self._last_tick_completed_at: float | None = None

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def health(self) -> CoordinatorHealth:
        return CoordinatorHealth(
            running=self._running,
            stop_requested=self.stop_requested,
            ticks=self._ticks,
            successful_ticks=self._successful_ticks,
            errors=self._errors,
            consecutive_errors=self._consecutive_errors,
            last_error=self._last_error,
            last_tick_started_at=self._last_tick_started_at,
            last_tick_completed_at=self._last_tick_completed_at,
        )

    def tick(self) -> Any | None:
        if self.stop_requested:
            return None
        self._ticks += 1
        self._last_tick_started_at = self._clock()
        try:
            with instance_lock(self._lock_path) as lock:
                lock.assert_held()
                result = self._coordinator_tick(lock.assert_held)
            self._successful_ticks += 1
            self._consecutive_errors = 0
            self._last_error = None
            return result
        except BaseException as exc:
            self._errors += 1
            self._consecutive_errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._last_tick_completed_at = self._clock()

    def run(self, *, max_ticks: int | None = None) -> CoordinatorHealth:
        if max_ticks is not None and (type(max_ticks) is not int or max_ticks < 0):
            raise ValueError("max_ticks must be None or a non-negative built-in int")
        if not self._run_guard.acquire(blocking=False):
            raise RuntimeError("coordinator loop is already running")
        self._running = True
        invocation_ticks = 0
        try:
            while not self.stop_requested and (max_ticks is None or invocation_ticks < max_ticks):
                invocation_ticks += 1
                failed = False
                try:
                    self.tick()
                except Exception:
                    failed = True
                if self.stop_requested or (max_ticks is not None and invocation_ticks >= max_ticks):
                    break
                delay = bounded_backoff(
                    self.error_backoff_seconds, self._consecutive_errors, self.max_error_backoff_seconds
                ) if failed else self.interval_seconds
                if self._wake.wait(delay):
                    self._wake.clear()
        finally:
            self._running = False
            self._run_guard.release()
        return self.health()
