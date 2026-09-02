from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitAdapterError(RuntimeError):
    pass


class DirtyCheckoutError(GitAdapterError):
    pass


class IntegrationHeadConflictError(GitAdapterError):
    """The controller lost the CAS race to advance a tranche head."""


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

    def _require_safe_worktree_root(self) -> None:
        try:
            self.worktree_root.relative_to(self.primary_checkout)
        except ValueError:
            return
        raise GitAdapterError("unsafe_worktree_root: must resolve outside canonical_repository")

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(("git", *args), cwd=cwd or self.primary_checkout, text=True, capture_output=True, timeout=30, check=check)
        except subprocess.CalledProcessError as exc:
            raise GitAdapterError(exc.stderr.strip() or exc.stdout.strip() or "git command failed") from exc

    def _require_clean_primary(self) -> None:
        status = self._git("status", "--porcelain=v1").stdout
        if status.strip():
            raise DirtyCheckoutError("primary checkout is dirty; refusing worktree creation")

    def integration_head_ref(self, tranche_id: str) -> str:
        """Return the controller-owned, repository-local anchor for a tranche."""
        ref = f"refs/local-first/tranches/{tranche_id}/integration-head"
        if not tranche_id or self._git("check-ref-format", ref, check=False).returncode:
            raise ValueError("invalid tranche id for integration head")
        return ref

    def resolve_execution_base(self, tranche_id: str | None, planning_base: str) -> str:
        """Anchor a tranche once and return its current serialized execution base.

        Tickets retain their immutable planning binding.  Only this controller ref
        moves, so a dependent ticket can begin from the accepted ancestry without
        rewriting the binding used to validate the original plan.
        """
        planning_base = self._git("rev-parse", "--verify", f"{planning_base}^{{commit}}").stdout.strip()
        if tranche_id is None:
            return planning_base
        ref = self.integration_head_ref(tranche_id)
        current = self._git("show-ref", "--verify", "--hash", ref, check=False)
        if current.returncode == 0:
            return self._git("rev-parse", "--verify", f"{current.stdout.strip()}^{{commit}}").stdout.strip()
        created = self._git("update-ref", ref, planning_base, "0" * 40, check=False)
        if created.returncode == 0:
            return planning_base
        # A concurrent controller may have created the anchor between our read
        # and CAS.  It is safe to use the resulting anchor, never the stale plan.
        current = self._git("show-ref", "--verify", "--hash", ref, check=False)
        if current.returncode == 0:
            return self._git("rev-parse", "--verify", f"{current.stdout.strip()}^{{commit}}").stdout.strip()
        raise GitAdapterError("unable to create tranche integration head")

    def existing_execution_base(self, tranche_id: str | None, planning_base: str) -> str:
        """Read an already-established integration anchor without creating or moving it."""
        planning_base = self._git("rev-parse", "--verify", f"{planning_base}^{{commit}}").stdout.strip()
        if tranche_id is None:
            return planning_base
        current = self._git("show-ref", "--verify", "--hash", self.integration_head_ref(tranche_id), check=False)
        if current.returncode != 0:
            raise GitAdapterError("integration_head_missing_reconciliation_required")
        return self._git("rev-parse", "--verify", f"{current.stdout.strip()}^{{commit}}").stdout.strip()

    def branch_exists(self, branch: str) -> bool:
        return self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0

    def advance_integration_head(self, tranche_id: str | None, expected_base: str, accepted_commit: str) -> str:
        """CAS-advance a tranche anchor after an accepted isolated commit."""
        if tranche_id is None:
            return accepted_commit
        expected = self._git("rev-parse", "--verify", f"{expected_base}^{{commit}}").stdout.strip()
        accepted = self._git("rev-parse", "--verify", f"{accepted_commit}^{{commit}}").stdout.strip()
        result = self._git("update-ref", self.integration_head_ref(tranche_id), accepted, expected, check=False)
        if result.returncode != 0:
            raise IntegrationHeadConflictError("integration head changed concurrently")
        return accepted

    def create_attempt(self, ticket_id: str, attempt_number: int, base_sha: str) -> AttemptWorktree:
        # The canonical checkout is read-only for attempts; its user changes need not block
        # creating a separate worktree from an immutable commit.
        self._require_safe_worktree_root()
        resolved = self._git("rev-parse", "--verify", f"{base_sha}^{{commit}}").stdout.strip()
        branch = f"local-first/{ticket_id}/attempt-{attempt_number}"
        path = self.worktree_root / ticket_id / f"attempt-{attempt_number}"
        if path.exists():
            raise GitAdapterError("attempt worktree already exists; reconcile it instead")
        if self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
            raise GitAdapterError("attempt branch already exists; refusing to reuse it")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-b", branch, str(path), resolved)
        resolved_path = path.resolve()
        try:
            resolved_path.relative_to(self.primary_checkout)
        except ValueError:
            return AttemptWorktree(ticket_id, attempt_number, resolved, branch, resolved_path, self.diff_hash(resolved_path))
        raise GitAdapterError("unsafe_worktree_root: concrete attempt resolves inside canonical_repository")

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
