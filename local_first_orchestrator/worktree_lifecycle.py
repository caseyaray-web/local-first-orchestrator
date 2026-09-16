from __future__ import annotations

import subprocess
from pathlib import Path

from .git_security import safe_git_argv, safe_git_env


def _git(repository: Path, *args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        safe_git_argv(args),
        cwd=cwd or repository,
        env=safe_git_env(),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def cleanup_completed_worktree(repository: Path, worktree: Path, *, accepted_commit_sha: str) -> str:
    """Remove one clean accepted worktree, or prune an already-missing registration."""
    repository = repository.resolve(strict=True)
    worktree = Path(worktree)
    try:
        resolved = worktree.resolve(strict=True)
    except FileNotFoundError:
        _git(repository, "worktree", "prune")
        return "pruned_missing"
    if resolved == repository:
        raise RuntimeError("refusing to remove canonical repository worktree")
    repo_common = Path(_git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    worktree_common = Path(
        _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=resolved)
    ).resolve(strict=True)
    if worktree_common != repo_common:
        raise RuntimeError("worktree does not belong to canonical repository")
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all", cwd=resolved):
        raise RuntimeError("completed worktree is dirty; refusing cleanup")
    head = _git(repository, "rev-parse", "HEAD", cwd=resolved)
    if head != accepted_commit_sha:
        raise RuntimeError("completed worktree HEAD does not match accepted commit")
    _git(repository, "worktree", "remove", str(resolved))
    _git(repository, "worktree", "prune")
    return "removed"
