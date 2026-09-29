"""M1 deterministic validation is bounded, contract-only, and candidate-bound."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
import tracemalloc

import pytest

from local_first_orchestrator.ticket import PatchBudget, TicketContract, VerificationProfile
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


def contract(*commands, paths=("app.py",), timeout=3, output_limit=100, cwd="."):
    return TicketContract(
        ticket_id="fixture", objective="Implement fixture value change", criterion_ids=("C1",),
        non_goals=("no unrelated changes",), allowed_paths=paths,
        verification=VerificationProfile(commands, timeout_seconds=timeout, output_limit=output_limit, working_directory=cwd),
        patch_budget=PatchBudget(len(paths), 10, 1), context_budget_tokens=100,
    )


def strict(validator, repo, ticket, base, trusted, **limits):
    return validator.validate(
        repo, ticket, base_sha=base, trusted_commands=trusted,
        trusted_max_timeout_seconds=limits.pop("timeout", 3),
        trusted_max_output_limit=limits.pop("output", 100), **limits,
    )


def test_contract_only_module_does_not_import_legacy_contracts():
    import local_first_orchestrator.validation as module

    assert "MicroTicket" not in module.__dict__
    assert "m1_contracts" not in module.__dict__
    assert "symbols" not in module.__dict__


def test_public_validation_requires_ticket_contract_and_trusted_policy(candidate):
    repo, base = candidate
    validator = DeterministicValidator(artifact_root=repo.parent / "evidence")
    command = (sys.executable, "-c", "print('must not run without policy')")
    with pytest.raises(TypeError):
        validator.validate(repo, contract(command), base_sha=base)
    with pytest.raises(ValidationError, match="ticket contract"):
        strict(validator, repo, object(), base, (command,))


def test_untrusted_command_is_rejected_before_execution(candidate):
    repo, base = candidate
    marker = repo.parent / "ran"
    command = (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()")
    with pytest.raises(ValidationError, match="trusted"):
        strict(DeterministicValidator(artifact_root=repo.parent / "evidence"), repo, contract(command), base,
               ((sys.executable, "-c", "print('allowed')"),))
    assert not marker.exists()


def test_contract_timeout_and_output_are_capped_by_trusted_limits(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "print('must not run')")
    validator = DeterministicValidator(artifact_root=repo.parent / "evidence")
    with pytest.raises(ValidationError, match="trusted.*timeout"):
        strict(validator, repo, contract(command, timeout=4), base, (command,))
    with pytest.raises(ValidationError, match="trusted.*output"):
        strict(validator, repo, contract(command, output_limit=101), base, (command,))


def test_scope_secret_and_patch_budget_fail_without_running_check(candidate):
    repo, base = candidate
    (repo / "extra.py").write_text("API_KEY = 'real-secret-value'\n")
    marker = repo.parent / "ran"
    command = (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()")
    result = strict(DeterministicValidator(artifact_root=repo.parent / "evidence"), repo, contract(command), base, (command,))
    assert not result.passed
    assert result.commands == ()
    assert any("outside allowlist" in error for error in result.errors)
    assert not marker.exists()


def test_declared_secret_fails_without_running_check(candidate):
    repo, base = candidate
    (repo / "app.py").write_text("API_KEY = 'real-secret-value'\n")
    command = (sys.executable, "-c", "raise SystemExit(9)")
    result = strict(DeterministicValidator(artifact_root=repo.parent / "evidence"), repo, contract(command), base, (command,))
    assert not result.passed
    assert any("secret material" in error for error in result.errors)
    assert result.commands == ()


def test_patch_line_budget_is_enforced_without_acceptance_dependencies(candidate):
    repo, base = candidate
    (repo / "app.py").write_text("\n".join("x" for _ in range(20)))
    command = (sys.executable, "-c", "raise SystemExit(9)")
    ticket = dataclasses.replace(contract(command), patch_budget=PatchBudget(1, 1, 1))
    result = strict(DeterministicValidator(artifact_root=repo.parent / "evidence"), repo, ticket, base, (command,))
    assert not result.passed
    assert "changed line budget exceeded" in result.errors
    assert result.commands == ()


def test_successful_verification_has_bounded_candidate_evidence(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "print('verified')")
    result = strict(DeterministicValidator(artifact_root=repo.parent / "evidence"), repo, contract(command), base, (command,))
    assert result.passed
    assert result.full_evidence_path.is_file()
    payload = json.loads(result.full_evidence_path.read_text())
    assert payload["candidate_identity"]
    assert result.commands[0].stdout_summary == "verified\n"


def test_verification_cannot_write_untracked_git_administrative_files(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "from pathlib import Path; Path('.git/identity-escape').write_text('changed')")
    evidence_root = repo.parent / f"{repo.name}-evidence"
    validator = DeterministicValidator(artifact_root=evidence_root)
    with pytest.raises(ValidationError, match="candidate.*changed"):
        strict(validator, repo, contract(command), base, (command,))
    assert not tuple(evidence_root.glob("validation-*.json"))


def test_linked_worktree_git_administration_is_candidate_bound(candidate):
    repo, base = candidate
    linked = repo.parent / "linked-candidate"
    git(repo, "worktree", "add", "-q", "-b", "fixture-candidate", str(linked), base)
    (linked / "app.py").write_text("def value(): return 3\n")
    command = (sys.executable, "-c", "import subprocess; from pathlib import Path; git_dir = subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], text=True).strip(); (Path(git_dir) / 'identity-escape').write_text('changed')")
    evidence_root = repo.parent / f"{repo.name}-linked-evidence"
    with pytest.raises(ValidationError, match="candidate.*changed"):
        strict(DeterministicValidator(artifact_root=evidence_root), linked, contract(command), base, (command,))
    assert not tuple(evidence_root.glob("validation-*.json"))


def test_candidate_mutation_during_check_fails_without_pass_artifact(candidate):
    repo, base = candidate
    evidence = repo.parent / f"{repo.name}-evidence"
    command = (sys.executable, "-c", "from pathlib import Path; Path('app.py').write_text('changed\\n')")
    with pytest.raises(ValidationError, match="candidate.*changed"):
        strict(DeterministicValidator(artifact_root=evidence), repo, contract(command), base, (command,))
    assert not tuple(evidence.glob("validation-*.json"))
    assert (repo / "app.py").read_text() == "changed\n"


def test_ignored_input_blocks_verification_before_execution(candidate):
    repo, base = candidate
    (repo / "settings.local").write_text("input\n")
    marker = repo.parent / "ran"
    command = (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()")
    with pytest.raises(ValidationError, match="ignored.*frozen"):
        strict(DeterministicValidator(artifact_root=repo.parent / "evidence"), repo, contract(command), base, (command,))
    assert not marker.exists()


def test_working_directory_cannot_escape_candidate(candidate):
    repo, base = candidate
    command = (sys.executable, "-c", "print('no')")
    with pytest.raises(ValueError, match="working directory"):
        contract(command, cwd="..")


def test_large_output_is_streamed_and_truncated(tmp_path):
    command = (sys.executable, "-c", "import sys; sys.stdout.write('x'*16777216); sys.stderr.write('y'*16777216)")
    validator = DeterministicValidator(artifact_root=tmp_path / "evidence")
    tracemalloc.start()
    try:
        record = validator.run_verification_command(command, allowed_commands=(command,), cwd=tmp_path, timeout_seconds=5, output_limit=64)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert record.returncode == 0 and record.truncated
    assert record.stdout_summary == "x" * 64 and record.stderr_summary == "y" * 64
    assert peak < 4_000_000


def test_timeout_terminates_process_group_and_returns_bounded_evidence(tmp_path):
    parent_pid, child_pid = tmp_path / "parent.pid", tmp_path / "child.pid"
    program = (
        "import pathlib,subprocess,sys,time; "
        f"pathlib.Path({str(parent_pid)!r}).write_text(str(__import__('os').getpid())); "
        f"p=subprocess.Popen([sys.executable,'-c',\"import os,pathlib,time; pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); time.sleep(30)\"]); "
        "time.sleep(30)"
    )
    command = (sys.executable, "-c", program)
    record = DeterministicValidator(artifact_root=tmp_path / "evidence").run_verification_command(
        command, allowed_commands=(command,), cwd=tmp_path, timeout_seconds=.1, output_limit=64)
    assert record.returncode == DeterministicValidator.TIMEOUT_RETURN_CODE
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (not child_pid.exists() or os.path.exists(f"/proc/{child_pid.read_text()}")):
        time.sleep(.02)
    assert parent_pid.exists() and child_pid.exists()
    assert not os.path.exists(f"/proc/{parent_pid.read_text()}")
    assert not os.path.exists(f"/proc/{child_pid.read_text()}")


def test_aggregate_deadline_prevents_later_command(candidate):
    repo, base = candidate
    marker = repo.parent / "second"
    first = (sys.executable, "-c", "import time; time.sleep(.4)")
    second = (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()")
    result = strict(DeterministicValidator(artifact_root=repo.parent / "evidence"), repo, contract(first, second), base,
                    (first, second), trusted_max_total_verification_seconds=.1)
    assert not result.passed and len(result.commands) == 1 and not marker.exists()
