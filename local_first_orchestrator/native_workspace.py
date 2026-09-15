from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NativePathIdentity:
    dev: int
    ino: int


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
