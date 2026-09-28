"""M1 Git evidence stays revision-bound and never discards useful work."""
from __future__ import annotations

import subprocess

import pytest

from local_first_orchestrator.git_adapter import (
    DirtyCheckoutError, GitAdapterError, GitWorktreeAdapter, IntegrationHeadConflictError,
)


def git(repo, *args):
    return subprocess.run(("git", *args), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / "app.py").write_text("original\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    adapter = GitWorktreeAdapter(repo, tmp_path / "attempts")
    return repo, base, adapter


def test_freeze_rejects_dirty_candidate_without_erasing_it(repository):
    repo, base, adapter = repository
    attempt = adapter.create_attempt("T-1", 1, base)
    marker = attempt.path / "useful-untracked.txt"
    marker.write_text("preserve\n")
    with pytest.raises(DirtyCheckoutError):
        adapter.freeze_candidate(attempt.path, base_sha=base, expected_head_sha=base)
    assert marker.read_text() == "preserve\n"
    assert attempt.path.is_dir()
    assert git(repo, "rev-parse", "HEAD") == base


def test_freeze_rejects_primary_checkout_subdirectory(repository):
    repo, base, adapter = repository
    nested = repo / "src"
    nested.mkdir()
    with pytest.raises(GitAdapterError, match="isolated"):
        adapter.freeze_candidate(nested, base_sha=base, expected_head_sha=base)
    assert git(repo, "rev-parse", "HEAD") == base


def test_freeze_rejects_stale_expected_head_without_erasing_work(repository):
    repo, base, adapter = repository
    attempt = adapter.create_attempt("T-2", 1, base)
    (attempt.path / "app.py").write_text("committed useful work\n")
    git(attempt.path, "add", "app.py")
    git(attempt.path, "commit", "-qm", "useful work")
    newer = git(attempt.path, "rev-parse", "HEAD")
    with pytest.raises(GitAdapterError, match="head"):
        adapter.freeze_candidate(attempt.path, base_sha=base, expected_head_sha=base)
    assert git(attempt.path, "rev-parse", "HEAD") == newer
    frozen = adapter.freeze_candidate(attempt.path, base_sha=base, expected_head_sha=newer)
    assert frozen.base_sha == base
    assert frozen.head_sha == newer
    assert frozen.tree_sha == git(attempt.path, "rev-parse", "HEAD^{tree}")
    assert git(repo, "rev-parse", "HEAD") == base


def test_teardown_rejects_dirty_worktree_instead_of_forcing_removal(repository):
    _, base, adapter = repository
    attempt = adapter.create_attempt("T-5", 1, base)
    marker = attempt.path / "keep.txt"
    marker.write_text("valuable\n")
    with pytest.raises(DirtyCheckoutError):
        adapter.teardown(attempt)
    assert marker.read_text() == "valuable\n"


def test_integration_cas_rejects_stale_and_unrelated_candidate(repository):
    repo, base, adapter = repository
    attempt = adapter.create_attempt("T-3", 1, base)
    (attempt.path / "app.py").write_text("next\n")
    git(attempt.path, "add", "app.py")
    git(attempt.path, "commit", "-qm", "next")
    next_sha = git(attempt.path, "rev-parse", "HEAD")
    assert adapter.resolve_execution_base("tranche", base) == base
    assert adapter.advance_integration_head("tranche", base, next_sha) == next_sha
    with pytest.raises(IntegrationHeadConflictError):
        adapter.advance_integration_head("tranche", base, next_sha)
    assert adapter.existing_execution_base("tranche", base) == next_sha
    # Another, unrelated commit in the same repository must not be accepted as the next piece.
    other = adapter.create_attempt("T-4", 1, base)
    (other.path / "app.py").write_text("unrelated\n")
    git(other.path, "add", "app.py")
    git(other.path, "commit", "-qm", "other")
    with pytest.raises(IntegrationHeadConflictError):
        adapter.advance_integration_head("tranche", next_sha, git(other.path, "rev-parse", "HEAD"))
    assert adapter.existing_execution_base("tranche", base) == next_sha
