"""M1 strict validation must not trust the ticket's own command list as authorization."""
from __future__ import annotations

import dataclasses
import subprocess
import sys
import tracemalloc

import pytest

from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.validation import DeterministicValidator, ValidationError


def git(repo, *args):
    return subprocess.run(("git", *args), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def candidate(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Fixture")
    (tmp_path / "app.py").write_text("def value(): return 1\n")
    (tmp_path / ".gitignore").write_text("*.local\n")
    git(tmp_path, "add", "app.py", ".gitignore")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "app.py").write_text("def value(): return 2\n")
    return tmp_path, base


def contract(command):
    return MicroTicket(
        "fixture", "Implement fixture value change", ("C1",), "app.py::value",
        ("app.py",), ("no unrelated changes",), PatchBudget(1, 10),
        VerificationProfile((command,), timeout_seconds=3, output_limit=100),
        "low", True, 2, (),
    )


def test_git_inspection_caps_output_before_buffering(candidate):
    repo, _ = candidate
    (repo / "bulk.txt").write_text("seed\n")
    git(repo, "add", "app.py", "bulk.txt")
    git(repo, "commit", "-qm", "bulk base")
    (repo / "bulk.txt").write_text("x" * 16_777_216 + "\n")
    validator = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence"))
    tracemalloc.start()
    try:
        with pytest.raises(ValidationError, match="inspection.*limit"):
            validator._git(repo, "diff", "--binary", "HEAD", "--")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4_000_000, f"git inspection buffered {peak} bytes in Python"


def test_untracked_file_snapshot_streams_without_loading_full_contents(candidate):
    repo, base = candidate
    with (repo / "large.bin").open("wb") as handle:
        handle.truncate(24 * 1024 * 1024)
    command = (sys.executable, "-c", "print('verified')")
    tracemalloc.start()
    try:
        result = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence")).validate_strict(
            repo, contract(command), base_sha=base, trusted_commands=(command,),
            trusted_max_timeout_seconds=3, trusted_max_output_limit=100,
            trusted_max_snapshot_file_bytes=32 * 1024 * 1024,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert not result.passed
    assert any("outside allowlist" in error for error in result.errors)
    assert peak < 4_000_000, f"candidate snapshot buffered {peak} bytes in Python"


def test_snapshot_rejects_oversized_file_before_check_and_preserves_it(candidate):
    repo, base = candidate
    with (repo / "large.bin").open("wb") as handle:
        handle.truncate(4096)
    marker = repo.parent / (repo.name + "-check-ran")
    command = (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()")
    validator = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence"))
    with pytest.raises(ValidationError, match="snapshot.*limit"):
        validator.validate_strict(repo, contract(command), base_sha=base, trusted_commands=(command,),
                                  trusted_max_timeout_seconds=3, trusted_max_output_limit=100,
                                  trusted_max_snapshot_file_bytes=2048)
    assert (repo / "large.bin").stat().st_size == 4096
    assert not marker.exists()


def test_snapshot_rejects_aggregate_limit_before_check(candidate):
    repo, base = candidate
    (repo / "one.bin").write_bytes(b"x" * 1500)
    (repo / "two.bin").write_bytes(b"y" * 1500)
    marker = repo.parent / (repo.name + "-check-ran")
    command = (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()")
    with pytest.raises(ValidationError, match="snapshot.*limit"):
        DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence")).validate_strict(
            repo, contract(command), base_sha=base, trusted_commands=(command,),
            trusted_max_timeout_seconds=3, trusted_max_output_limit=100,
            trusted_max_snapshot_total_bytes=2048)
    assert not marker.exists()
    assert (repo / "one.bin").read_bytes() == b"x" * 1500


def test_ignored_file_blocks_verification_before_it_can_depend_on_missing_frozen_input(candidate):
    repo, base = candidate
    ignored = repo / "settings.local"
    ignored.write_text("allow\n")
    marker = repo.parent / (repo.name + "-check-ran")
    command = (sys.executable, "-c", f"from pathlib import Path; assert Path('settings.local').read_text() == 'allow\\n'; Path({str(marker)!r}).touch()")
    validator = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence"))
    with pytest.raises(ValidationError, match="ignored.*frozen"):
        validator.validate_strict(repo, contract(command), base_sha=base, trusted_commands=(command,),
                                  trusted_max_timeout_seconds=3, trusted_max_output_limit=100)
    assert ignored.read_text() == "allow\n"  # Preserve useful work; never reset it.
    assert not marker.exists()


def test_artifacts_inside_candidate_are_refused_before_any_write(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "print('verified')")
    evidence = repo / "evidence"
    with pytest.raises(ValidationError, match="artifact.*outside"):
        DeterministicValidator(artifact_root=evidence).validate_strict(
            repo, contract(command), base_sha=base, trusted_commands=(command,),
            trusted_max_timeout_seconds=3, trusted_max_output_limit=100)
    assert not evidence.exists()


def test_large_command_output_does_not_accumulate_in_memory(tmp_path):
    command = (sys.executable, "-c", "import sys; sys.stdout.write('x' * 16777216); sys.stderr.write('y' * 16777216)")
    validator = DeterministicValidator(artifact_root=tmp_path / "evidence")
    tracemalloc.start()
    try:
        result = validator.run_verification_command(
            command, allowed_commands=(command,), cwd=tmp_path, timeout_seconds=5, output_limit=64,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.returncode == 0
    assert result.truncated
    assert result.stdout_summary == "x" * 64
    assert result.stderr_summary == "y" * 64
    assert peak < 4_000_000, f"output capture used {peak} bytes of Python heap"


def test_strict_rejects_ticket_timeout_above_independent_limit(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "print('should not execute')")
    ticket = dataclasses.replace(contract(command), verification=VerificationProfile((command,), timeout_seconds=9, output_limit=100))
    with pytest.raises(ValidationError, match="trusted.*timeout"):
        DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence")).validate_strict(
            repo, ticket, base_sha=base, trusted_commands=(command,),
            trusted_max_timeout_seconds=3, trusted_max_output_limit=100,
        )


def test_strict_rejects_ticket_output_above_independent_limit(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "print('should not execute')")
    ticket = dataclasses.replace(contract(command), verification=VerificationProfile((command,), timeout_seconds=3, output_limit=200))
    with pytest.raises(ValidationError, match="trusted.*output"):
        DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence")).validate_strict(
            repo, ticket, base_sha=base, trusted_commands=(command,),
            trusted_max_timeout_seconds=3, trusted_max_output_limit=100,
        )


def test_model_proposed_command_is_not_its_own_allowlist(candidate):
    repo, base = candidate
    marker = repo / "should-not-exist"
    proposed = (sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')")
    validator = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence"))
    with pytest.raises(ValidationError, match="trusted"):
        validator.validate_strict(repo, contract(proposed), base_sha=base,
                                  trusted_commands=((sys.executable, "-c", "print('authorized')"),),
                                  trusted_max_timeout_seconds=3, trusted_max_output_limit=100)
    assert not marker.exists()
    assert git(repo, "rev-parse", "HEAD") == base


def test_successful_verification_leaves_committed_candidate_clean(candidate):
    repo, base = candidate
    git(repo, "add", "app.py")
    git(repo, "commit", "-qm", "candidate")
    head = git(repo, "rev-parse", "HEAD")
    command = (sys.executable, "-c", "print('verified')")
    result = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence")).validate_strict(
        repo, contract(command), base_sha=base, expected_head_sha=head,
        trusted_commands=(command,), trusted_max_timeout_seconds=3, trusted_max_output_limit=100,
    )
    assert result.passed
    assert result.full_evidence_path.is_file()
    assert git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_trusted_command_verifies_without_changing_candidate(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "print('verified')")
    result = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence")).validate_strict(
        repo, contract(command), base_sha=base, trusted_commands=(command,), trusted_max_timeout_seconds=3, trusted_max_output_limit=100
    )
    assert result.passed
    assert result.commands[0].stdout_summary == "verified\n"
    assert git(repo, "rev-parse", "HEAD") == base


def test_check_adding_undeclared_file_invalidates_snapshot(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "from pathlib import Path; Path('surprise.txt').write_text('useful work')")
    validator = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence"))
    with pytest.raises(ValidationError, match="candidate.*changed"):
        validator.validate_strict(repo, contract(command), base_sha=base, trusted_commands=(command,), trusted_max_timeout_seconds=3, trusted_max_output_limit=100)
    assert (repo / "surprise.txt").read_text() == "useful work"


def test_check_moving_head_invalidates_candidate_without_reset(candidate):
    repo, base = candidate
    command = ("git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture",
               "commit", "-qam", "verification moved head")
    validator = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence"))
    with pytest.raises(ValidationError, match="candidate.*changed"):
        validator.validate_strict(repo, contract(command), base_sha=base, trusted_commands=(command,), trusted_max_timeout_seconds=3, trusted_max_output_limit=100)
    assert git(repo, "rev-parse", "HEAD") != base
    assert (repo / "app.py").read_text() == "def value(): return 2\n"


def test_mutating_check_fails_closed_and_preserves_work(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "from pathlib import Path; Path('app.py').write_text('def value(): return 3\\n')")
    validator = DeterministicValidator(artifact_root=repo.parent / (repo.name + "-evidence"))
    with pytest.raises(ValidationError, match="candidate.*changed"):
        validator.validate_strict(repo, contract(command), base_sha=base, trusted_commands=(command,), trusted_max_timeout_seconds=3, trusted_max_output_limit=100)
    assert (repo / "app.py").read_text() == "def value(): return 3\n"
