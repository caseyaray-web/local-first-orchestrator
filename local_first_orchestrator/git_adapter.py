from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitAdapterError(RuntimeError):
    pass


class DirtyCheckoutError(GitAdapterError):
    pass


@dataclass(frozen=True)
class AttemptWorktree:
    ticket_id: str
    attempt_number: int
    base_sha: str
    branch: str
    path: Path
    pre_diff_hash: str


class GitWorktreeAdapter:
    def __init__(self, primary_checkout: Path, worktree_root: Path) -> None:
        self.primary_checkout, self.worktree_root = Path(primary_checkout).resolve(), Path(worktree_root).resolve()

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(("git", *args), cwd=cwd or self.primary_checkout, text=True, capture_output=True, timeout=30, check=check)
        except subprocess.CalledProcessError as exc:
            raise GitAdapterError(exc.stderr.strip() or exc.stdout.strip() or "git command failed") from exc

    def _require_clean_primary(self) -> None:
        status = self._git("status", "--porcelain=v1").stdout
        if status.strip():
            raise DirtyCheckoutError("primary checkout is dirty; refusing worktree creation")

    def create_attempt(self, ticket_id: str, attempt_number: int, base_sha: str) -> AttemptWorktree:
        # The canonical checkout is read-only for attempts; its user changes need not block
        # creating a separate worktree from an immutable commit.
        resolved = self._git("rev-parse", "--verify", f"{base_sha}^{{commit}}").stdout.strip()
        branch = f"local-first/{ticket_id}/attempt-{attempt_number}"
        path = self.worktree_root / ticket_id / f"attempt-{attempt_number}"
        if path.exists():
            raise GitAdapterError("attempt worktree already exists; reconcile it instead")
        if self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
            raise GitAdapterError("attempt branch already exists; refusing to reuse it")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-b", branch, str(path), resolved)
        return AttemptWorktree(ticket_id, attempt_number, resolved, branch, path, self.diff_hash(path))

    def diff_hash(self, attempt: Path) -> str:
        diff = self._git("diff", "--binary", "--no-ext-diff", "HEAD", cwd=attempt).stdout
        return hashlib.sha256(diff.encode()).hexdigest()

    def metadata(self, attempt: AttemptWorktree) -> dict[str, str | int | None]:
        return {"base_sha": attempt.base_sha, "branch": attempt.branch, "worktree_path": str(attempt.path),
                "pre_diff_hash": attempt.pre_diff_hash, "post_diff_hash": self.diff_hash(attempt.path), "accepted_commit_sha": None}

    def accept(self, attempt: AttemptWorktree, message: str, *, default_branch: str = "main") -> str:
        current = self._git("branch", "--show-current", cwd=attempt.path).stdout.strip()
        if current == default_branch or attempt.branch == default_branch:
            raise PermissionError("automatic default-branch merge is forbidden")
        self._git("add", "-A", cwd=attempt.path)
        if not self._git("diff", "--cached", "--quiet", check=False, cwd=attempt.path).returncode:
            raise GitAdapterError("nothing to commit")
        self._git("commit", "-m", message, cwd=attempt.path)
        return self._git("rev-parse", "HEAD", cwd=attempt.path).stdout.strip()

    def teardown(self, attempt: AttemptWorktree) -> None:
        if not str(attempt.path).startswith(str(self.worktree_root) + "/"):
            raise GitAdapterError("refusing teardown outside configured worktree root")
        self._git("worktree", "remove", "--force", str(attempt.path))
