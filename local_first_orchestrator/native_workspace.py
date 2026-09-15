from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NativePathIdentity:
    dev: int
    ino: int


@dataclass
class PinnedNativeWorkspace:
    """Keep the repository objects stable while Git performs a native mutation."""

    repository: Path
    worktree_parent: Path
    repository_fd: int
    git_fd: int
    worktree_parent_fd: int
    repository_identity: NativePathIdentity
    git_identity: NativePathIdentity
    worktree_parent_identity: NativePathIdentity
    target: Path
    target_fd: int | None = None
    target_identity: NativePathIdentity | None = None

    @classmethod
    def open(cls, repository: Path, target: Path) -> "PinnedNativeWorkspace":
        repository = Path(repository)
        worktree_parent = repository / ".worktrees"
        _check_existing_components(repository)
        repository_fd = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _require_fd_path(repository, repository_fd, "repository")
            git_path = repository / ".git"
            _check_existing_components(git_path)
            git_fd = os.open(git_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _require_fd_path(git_path, git_fd, ".git")
                try:
                    os.mkdir(f"/proc/self/fd/{repository_fd}/.worktrees")
                except FileExistsError:
                    pass
                _check_existing_components(worktree_parent)
                parent_fd = os.open(worktree_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except BaseException:
                os.close(git_fd)
                raise
        except BaseException:
            os.close(repository_fd)
            raise
        try:
            _require_fd_path(worktree_parent, parent_fd, ".worktrees")
            instance = cls(repository, worktree_parent, repository_fd, git_fd, parent_fd,
                           _fd_identity(repository_fd), _fd_identity(git_fd), _fd_identity(parent_fd), target)
            instance.revalidate()
            if target.exists():
                instance.pin_existing_target(already_created=True)
            return instance
        except BaseException:
            os.close(parent_fd)
            os.close(git_fd)
            os.close(repository_fd)
            raise

    def close(self) -> None:
        for fd in (self.target_fd, self.worktree_parent_fd, self.git_fd, self.repository_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.target_fd = None

    def __enter__(self) -> "PinnedNativeWorkspace":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def revalidate(self, *, target_must_exist: bool | None = None) -> None:
        _require_fd_path(self.repository, self.repository_fd, "repository", self.repository_identity)
        _require_fd_path(self.repository / ".git", self.git_fd, ".git", self.git_identity)
        _require_fd_path(self.worktree_parent, self.worktree_parent_fd, ".worktrees", self.worktree_parent_identity)
        if target_must_exist is False:
            try:
                os.lstat(self.target)
            except FileNotFoundError:
                return
            raise RuntimeError("native workspace STOP: target appeared before mutation")
        if self.target_fd is None:
            if target_must_exist is True:
                raise RuntimeError("native workspace STOP: target identity is not pinned")
            return
        _require_fd_path(self.target, self.target_fd, "target", self.target_identity)

    def pin_existing_target(self, *, already_created: bool = False) -> None:
        if not already_created:
            self.revalidate(target_must_exist=False)
        try:
            fd = os.open(self.target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise RuntimeError("native workspace STOP: target is not a directory") from exc
        try:
            self.target_fd = fd
            self.target_identity = _fd_identity(fd)
            self.revalidate(target_must_exist=True)
        except BaseException:
            os.close(fd)
            self.target_fd = None
            self.target_identity = None
            raise

    def git(self, *args: str, check: bool = True, target: bool = False, git_fd_override: int | None = None, creates_target: bool = False, cwd_fd_override: int | None = None) -> subprocess.CompletedProcess[str]:
        self.revalidate(target_must_exist=True if target else (False if self.target_fd is None and not creates_target else None))
        git_fd = self.git_fd if git_fd_override is None else git_fd_override
        cwd_fd = cwd_fd_override if cwd_fd_override is not None else (self.target_fd if target else self.repository_fd)
        worktree_fd = self.target_fd if target else self.repository_fd
        if cwd_fd is None or worktree_fd is None:
            raise RuntimeError("native workspace STOP: target is not pinned")
        command = ("git", f"--git-dir=/proc/self/fd/{git_fd}", f"--work-tree=/proc/self/fd/{worktree_fd}", *args)
        try:
            result = subprocess.run(command, cwd=f"/proc/self/fd/{cwd_fd}", pass_fds=tuple({git_fd, cwd_fd, worktree_fd}),
                                    text=True, capture_output=True, timeout=30, check=check)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(exc.stderr.strip() or exc.stdout.strip() or "Git command failed") from exc
        self.revalidate(target_must_exist=True if target else (False if self.target_fd is None and not creates_target else None))
        return result

    def target_git_fd(self) -> int:
        if self.target_fd is None:
            raise RuntimeError("native workspace STOP: target is not pinned")
        raw = Path(f"/proc/self/fd/{self.target_fd}/.git").read_text(encoding="utf-8").strip()
        if not raw.startswith("gitdir: "):
            raise RuntimeError("native workspace STOP: target Git metadata is invalid")
        gitdir = Path(raw[8:]).resolve(strict=True)
        expected_parent = (self.repository / ".git" / "worktrees").resolve(strict=True)
        try:
            gitdir.relative_to(expected_parent)
        except ValueError as exc:
            raise RuntimeError("native workspace STOP: target Git metadata escapes pinned repository") from exc
        _check_existing_components(gitdir)
        fd = os.open(gitdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _require_fd_path(gitdir, fd, "target Git metadata")
        return fd


def _fd_identity(fd: int) -> NativePathIdentity:
    info = os.fstat(fd)
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeError("native workspace STOP: symlink identity")
    return NativePathIdentity(info.st_dev, info.st_ino)


def _require_fd_path(path: Path, fd: int, label: str, identity: NativePathIdentity | None = None) -> None:
    _check_existing_components(path)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"native workspace STOP: {label} disappeared") from exc
    expected = identity or _fd_identity(fd)
    if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != (expected.dev, expected.ino):
        raise RuntimeError(f"native workspace STOP: {label} identity changed")


def canonical_native_workspace_path(repository: Path, external_task_id: str) -> Path:
    """Return the one lexical native worktree path authorized for a task."""
    if not isinstance(external_task_id, str) or not external_task_id or external_task_id in {".", ".."}:
        raise ValueError("external task id must be one safe path component")
    if "/" in external_task_id or "\\" in external_task_id or "\x00" in external_task_id:
        raise ValueError("external task id must be one safe path component")
    if not external_task_id[0].isascii() or not external_task_id[0].isalnum() or any(not (char.isascii() and (char.isalnum() or char in "._-")) for char in external_task_id):
        raise ValueError("external task id must be one safe path component")
    repository_text = os.path.normpath(os.path.abspath(os.fspath(repository)))
    return Path(os.path.normpath(os.path.join(repository_text, ".worktrees", external_task_id)))


def _check_existing_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("native release authority mismatch: symlink in workspace path")


def validate_native_workspace_path(raw_path: str | os.PathLike[str], *, repository: Path, external_task_id: str, require_existing: bool = True) -> tuple[Path, NativePathIdentity | None]:
    expected = canonical_native_workspace_path(repository, external_task_id)
    if not isinstance(raw_path, (str, os.PathLike)):
        raise RuntimeError("native release authority mismatch: workspace path is not a path")
    raw = os.fspath(raw_path)
    if not os.path.isabs(raw) or raw != str(expected) or os.path.normpath(raw) != raw:
        raise RuntimeError("native release authority mismatch: workspace path spelling (worktree drift)")
    _check_existing_components(expected)
    if not require_existing:
        try:
            info = os.lstat(expected)
        except FileNotFoundError:
            return expected, None
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("native release authority mismatch: workspace is a symlink")
    try:
        resolved = expected.resolve(strict=True)
        info = os.stat(expected, follow_symlinks=False)
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError("native release authority mismatch: workspace is unavailable") from exc
    if resolved != expected or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("native release authority mismatch: workspace identity")
    resolved_info = os.stat(resolved, follow_symlinks=False)
    identity = NativePathIdentity(info.st_dev, info.st_ino)
    if (resolved_info.st_dev, resolved_info.st_ino) != (identity.dev, identity.ino):
        raise RuntimeError("native release authority mismatch: workspace identity")
    return expected, identity


def require_native_path_identity(path: Path, identity: NativePathIdentity) -> None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("native release authority mismatch: workspace identity changed") from exc
    if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != (identity.dev, identity.ino):
        raise RuntimeError("native release authority mismatch: workspace identity changed")
