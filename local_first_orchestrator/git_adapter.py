from __future__ import annotations

import hashlib
import os
import re
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .git_security import safe_git_argv, safe_git_env


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


@dataclass(frozen=True)
class FrozenCandidate:
    """Immutable Git evidence; the retained worktree remains user-owned."""
    path: Path
    base_sha: str
    head_sha: str
    tree_sha: str


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
            return subprocess.run(safe_git_argv(args), cwd=cwd or self.primary_checkout, env=safe_git_env(), text=True, capture_output=True, timeout=30, check=check)
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
        expected = self._git("rev-parse", "--verify", f"{expected_base}^{{commit}}").stdout.strip()
        accepted = self._git("rev-parse", "--verify", f"{accepted_commit}^{{commit}}").stdout.strip()
        if self._git("merge-base", "--is-ancestor", expected, accepted, check=False).returncode != 0:
            raise IntegrationHeadConflictError("accepted commit does not descend from expected integration head")
        if tranche_id is None:
            return accepted
        result = self._git("update-ref", self.integration_head_ref(tranche_id), accepted, expected, check=False)
        if result.returncode != 0:
            raise IntegrationHeadConflictError("integration head changed concurrently")
        return accepted

    def create_attempt(self, ticket_id: str, attempt_number: int, base_sha: str) -> AttemptWorktree:
        # The canonical checkout is read-only for attempts; its user changes need not block
        # creating a separate worktree from an immutable commit.
        self._require_safe_worktree_root()
        if (not isinstance(ticket_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", ticket_id)
                or type(attempt_number) is not int or attempt_number <= 0):
            raise ValueError("invalid ticket id or attempt number for worktree")
        branch = f"local-first/{ticket_id}/attempt-{attempt_number}"
        if self._git("check-ref-format", "--branch", branch, check=False).returncode:
            raise ValueError("invalid ticket id for branch")
        parent = (self.worktree_root / ticket_id).resolve()
        if parent.parent != self.worktree_root:
            raise GitAdapterError("ticket worktree path escapes configured root")
        resolved = self._git("rev-parse", "--verify", f"{base_sha}^{{commit}}").stdout.strip()
        path = parent / f"attempt-{attempt_number}"
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

    def freeze_candidate(self, workspace: Path, *, base_sha: str, expected_head_sha: str) -> FrozenCandidate:
        """Read a clean, revision-pinned candidate without moving refs or deleting work."""
        workspace = Path(workspace).resolve(strict=True)
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha) or not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
            raise GitAdapterError("candidate base/head must be full lowercase commit hashes")
        common = self._git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=workspace).stdout.strip()
        primary_common = self._git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        top_level = Path(self._git("rev-parse", "--show-toplevel", cwd=workspace).stdout.strip()).resolve()
        if Path(common).resolve() != Path(primary_common).resolve() or top_level != workspace or top_level == self.primary_checkout:
            raise GitAdapterError("candidate workspace is not an isolated worktree in this repository")
        actual = self._git("rev-parse", "HEAD", cwd=workspace).stdout.strip()
        if actual != expected_head_sha:
            raise GitAdapterError("candidate head changed since observation")
        if self._git("merge-base", "HEAD", base_sha, cwd=workspace).stdout.strip() != base_sha:
            raise GitAdapterError("candidate base is not an ancestor of head")
        if self._git("status", "--porcelain=v1", "--untracked-files=all", cwd=workspace).stdout.strip():
            raise DirtyCheckoutError("candidate worktree is dirty; preserve and reconcile before freezing")
        if self._git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", cwd=workspace).stdout:
            raise DirtyCheckoutError("candidate worktree contains ignored files outside frozen Git evidence; preserve and reconcile before freezing")
        tree = self._git("rev-parse", "HEAD^{tree}", cwd=workspace).stdout.strip()
        if self._git("rev-parse", "HEAD", cwd=workspace).stdout.strip() != actual:
            raise GitAdapterError("candidate head changed while freezing")
        return FrozenCandidate(workspace, base_sha, actual, tree)

    def diff_hash(self, attempt: Path) -> str:
        """Hash a bounded Git diff without retaining its full output in memory."""
        argv = safe_git_argv(("diff", "--binary", "--no-ext-diff", "HEAD"))
        process = subprocess.Popen(argv, cwd=attempt, env=safe_git_env(), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        checksum = hashlib.sha256()
        stderr = bytearray()
        total = 0
        deadline = time.monotonic() + 30
        try:
            with selectors.DefaultSelector() as selector:
                assert process.stdout is not None and process.stderr is not None
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map() or process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GitAdapterError("diff inspection timed out")
                    for key, _ in selector.select(timeout=min(.1, remaining)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == "stdout":
                            total += len(chunk)
                            if total > 16_000_000:
                                raise GitAdapterError("diff exceeds bounded inspection limit")
                            checksum.update(chunk)
                        else:
                            if len(stderr) + len(chunk) > 8192:
                                raise GitAdapterError("diff stderr exceeds inspection limit")
                            stderr.extend(chunk)
            process.wait(timeout=.5)
        finally:
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                try: process.wait(timeout=.5)
                except subprocess.TimeoutExpired: pass
            if process.stdout is not None: process.stdout.close()
            if process.stderr is not None: process.stderr.close()
        if process.returncode:
            raise GitAdapterError("diff inspection failed: " + stderr.decode("utf-8", errors="replace")[:2000])
        return checksum.hexdigest()

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
        path = attempt.path.resolve(strict=True)
        if self.worktree_root not in path.parents or path == self.primary_checkout:
            raise GitAdapterError("refusing teardown outside configured worktree root")
        if self._git("status", "--porcelain=v1", "--untracked-files=all", cwd=path).stdout.strip():
            raise DirtyCheckoutError("attempt has useful uncommitted work; refusing teardown")
        # Git's own non-forced guard remains authoritative if work changes after readback.
        self._git("worktree", "remove", str(path))
