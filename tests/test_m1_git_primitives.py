"""M1 Git evidence stays revision-bound and never discards useful work."""
from __future__ import annotations

import hashlib
import subprocess
import tracemalloc

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


def test_create_attempt_rejects_escaping_ticket_ids_before_mkdir(repository):
    repo, base, adapter = repository
    for ticket_id in ("../escaped", "nested/escape", str(repo.parent / "absolute")):
        with pytest.raises((ValueError, GitAdapterError), match="ticket"):
            adapter.create_attempt(ticket_id, 1, base)
    assert not (repo.parent / "escaped").exists()
    assert not (repo.parent / "absolute").exists()
    assert not (repo.parent / "attempts" / "nested").exists()


def test_diff_hash_streams_large_work_without_buffering(repository):
    _, base, adapter = repository
    attempt = adapter.create_attempt("T-large", 1, base)
    (attempt.path / "app.py").write_text("b" * 6_000_000 + "\n")
    expected_diff = subprocess.run(("git", "diff", "--binary", "--no-ext-diff", "HEAD"),
                                   cwd=attempt.path, capture_output=True, check=True).stdout
    expected = hashlib.sha256(expected_diff).hexdigest()
    del expected_diff
    tracemalloc.start()
    try:
        actual = adapter.diff_hash(attempt.path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    assert peak < 4_000_000, f"adapter buffered {peak} bytes of Git diff"


def test_diff_hash_rejects_oversized_diff_without_deleting_work(repository):
    _, base, adapter = repository
    attempt = adapter.create_attempt("T-oversized", 1, base)
    (attempt.path / "app.py").write_text("b" * 20_000_000 + "\n")
    with pytest.raises(GitAdapterError, match="diff.*limit"):
        adapter.diff_hash(attempt.path)
    assert (attempt.path / "app.py").stat().st_size == 20_000_001


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


def test_freeze_rejects_ignored_candidate_input_without_erasing_it(repository):
    repo, base, adapter = repository
    (repo / ".gitignore").write_text("*.local\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore policy")
    base = git(repo, "rev-parse", "HEAD")
    attempt = adapter.create_attempt("T-ignored", 1, base)
    ignored = attempt.path / "settings.local"
    ignored.write_text("allow\n")
    with pytest.raises(DirtyCheckoutError, match="ignored"):
        adapter.freeze_candidate(attempt.path, base_sha=base, expected_head_sha=base)
    assert ignored.read_text() == "allow\n"
    ignored.unlink()
    assert adapter.freeze_candidate(attempt.path, base_sha=base, expected_head_sha=base).head_sha == base


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
