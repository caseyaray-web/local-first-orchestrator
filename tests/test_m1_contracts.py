import dataclasses
import hashlib
import json
import subprocess
import sys

import pytest

from local_first_orchestrator.m1_contracts import (
    PatchBudget, TicketContract, VerificationProfile, PlanContract, TrancheContract,
    validate_plan, normalize_review,
)


def ticket():
    return TicketContract(
        ticket_id="TK-1", objective="Implement guard", criterion_ids=("AC-1",),
        non_goals=("No API changes",), allowed_paths=("app.py", "test_app.py"),
        verification=VerificationProfile(commands=(("python", "-m", "pytest"),), timeout_seconds=60, output_limit=20000),
        patch_budget=PatchBudget(max_files=2, max_changed_lines=100, max_attempts=2),
    )


def plan(t=None):
    t = t or ticket()
    tr = TrancheContract("TR-1", 0, (t,), ("AC-1",))
    return PlanContract("PL-1", 1, (tr,), {"AC-1": (t.ticket_id,)})


def test_pure_contract_import_does_not_import_legacy_runtime():
    script = '''import sys
import local_first_orchestrator.m1_contracts
for name in ("ledger", "states", "local_qwen", "controller", "hermes_board"):
    assert "local_first_orchestrator." + name not in sys.modules, name
'''
    completed = subprocess.run((sys.executable, "-c", script), capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def test_ticket_is_immutable_versioned_and_canonically_identified():
    t = ticket()
    assert t.schema_version == 1
    assert t.contract_hash == hashlib.sha256(json.dumps(t.payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.objective = "changed"


def test_new_contract_requires_explicit_limits_instead_of_legacy_defaults():
    with pytest.raises(TypeError):
        PatchBudget()
    with pytest.raises(TypeError):
        VerificationProfile(commands=(("python", "-m", "pytest"),))


def test_contract_rejects_unsafe_paths_unbounded_verification_and_mutable_inputs():
    with pytest.raises(ValueError):
        dataclasses.replace(ticket(), allowed_paths=("../outside.py",))
    with pytest.raises(ValueError):
        dataclasses.replace(ticket(), allowed_paths=("/absolute.py",))
    with pytest.raises(ValueError):
        dataclasses.replace(ticket(), allowed_paths=("app.py", "test_app.py", "extra.py"))
    with pytest.raises(ValueError):
        VerificationProfile(commands=(("python", "-m", "pytest"),), timeout_seconds=60, output_limit=20000, working_directory="../outside")
    with pytest.raises(ValueError):
        VerificationProfile(commands=(("python", "-m", "pytest"),), timeout_seconds=0, output_limit=20000)
    with pytest.raises(ValueError):
        dataclasses.replace(ticket(), criterion_ids=["AC-1"])


def test_plan_requires_unique_ids_exact_coverage_and_acyclic_dependencies():
    t = ticket()
    duplicate = TrancheContract("TR-1", 1, (t,), ("AC-1",))
    assert "duplicate_tranche_id" in validate_plan(PlanContract("PL", 1, (plan().tranches[0], duplicate), {"AC-1": ("TK-1",)}), expected_criteria={"AC-1"})
    second = dataclasses.replace(t, ticket_id="TK-2", dependencies=("TK-1",))
    first = dataclasses.replace(t, dependencies=("TK-2",))
    tr = TrancheContract("TR-1", 0, (first, second), ("AC-1",))
    cyclic_plan = PlanContract("PL-1", 1, (tr,), {"AC-1": ("TK-1", "TK-2")})
    assert "dependency_cycle" in validate_plan(cyclic_plan, expected_criteria={"AC-1"})
    assert "criterion_coverage_mismatch" in validate_plan(dataclasses.replace(plan(), criterion_coverage={}), expected_criteria={"AC-1"})


def test_plan_hash_covers_full_ticket_payload_and_trusted_expected_criteria():
    original = plan()
    changed = plan(dataclasses.replace(ticket(), objective="Different objective"))
    assert original.contract_hash != changed.contract_hash
    assert "missing_required_criterion" in validate_plan(original, expected_criteria={"AC-1", "AC-2"})
    duplicate_ordinal = dataclasses.replace(original, tranches=(original.tranches[0], dataclasses.replace(original.tranches[0], tranche_id="TR-2")))
    assert "duplicate_tranche_ordinal" in validate_plan(duplicate_ordinal, expected_criteria={"AC-1"})


def test_plan_requires_trusted_criteria_and_frozen_coverage():
    p = plan()
    with pytest.raises(TypeError):
        validate_plan(p)
    assert "missing_required_criterion" in validate_plan(p, expected_criteria={"AC-1", "AC-2"})
    expanded = dataclasses.replace(p.tranches[0], criterion_ids=("AC-1", "AC-2"))
    forged = dataclasses.replace(p, tranches=(expanded,), criterion_coverage={"AC-1": ("TK-1",), "AC-2": ("TK-1",)})
    assert "unknown_criterion" in validate_plan(forged, expected_criteria={"AC-1"})
    raw = {"AC-1": ("TK-1",)}
    frozen = dataclasses.replace(p, criterion_coverage=raw)
    original_hash = frozen.contract_hash
    raw["AC-1"] = ("wrong",)
    assert frozen.contract_hash == original_hash
    with pytest.raises(TypeError):
        frozen.criterion_coverage["AC-1"] = ("wrong",)


def test_plan_rejects_ticket_criteria_not_bound_to_coverage():
    original = ticket()
    other = dataclasses.replace(original, ticket_id="TK-2")
    tranche = TrancheContract("TR-1", 0, (original, other), ("AC-1",))
    p = PlanContract("PL-1", 1, (tranche,), {"AC-1": ("TK-1",)})
    assert "ticket_criterion_uncovered" in validate_plan(p, expected_criteria={"AC-1"})


def test_plan_rejects_dependency_on_future_tranche():
    first = dataclasses.replace(ticket(), dependencies=("TK-2",))
    second = dataclasses.replace(ticket(), ticket_id="TK-2")
    early = TrancheContract("TR-1", 0, (first,), ("AC-1",))
    later = TrancheContract("TR-2", 1, (second,), ("AC-1",))
    p = PlanContract("PL-1", 1, (early, later), {"AC-1": ("TK-1", "TK-2")})
    assert "dependency_future_tranche" in validate_plan(p, expected_criteria={"AC-1"})


def test_escalation_requires_an_actionable_reason():
    t = ticket()
    evidence = {"ticket_id": t.ticket_id, "contract_hash": t.contract_hash,
                "candidate_sha": "a" * 40, "verdict": "escalate"}
    with pytest.raises(ValueError, match="escalat"):
        normalize_review(evidence, t, "a" * 40)
    result = normalize_review({**evidence, "reason": "Infrastructure unable to verify"}, t, "a" * 40)
    assert result.verdict == "escalate" and result.escalation_reason == "Infrastructure unable to verify"


def test_review_is_pinned_and_fails_closed_on_criteria_and_blockers():
    t = ticket()
    candidate_sha = "a" * 40
    with pytest.raises(ValueError):
        normalize_review({"verdict": "pass", "criterion_results": []}, t, candidate_sha)
    with pytest.raises(ValueError):
        normalize_review({"ticket_id": t.ticket_id, "contract_hash": t.contract_hash, "candidate_sha": candidate_sha, "verdict": "pass", "criterion_results": [
            {"criterion_id": "AC-1", "status": "pass", "evidence": "ok"},
            {"criterion_id": "AC-1", "status": "pass", "evidence": "duplicate"},
        ]}, t, candidate_sha)
    with pytest.raises(ValueError):
        normalize_review({"ticket_id": t.ticket_id, "contract_hash": t.contract_hash, "candidate_sha": candidate_sha, "verdict": "repair", "findings": []}, t, candidate_sha)
    with pytest.raises(ValueError):
        normalize_review({"ticket_id": t.ticket_id, "contract_hash": t.contract_hash,
                          "candidate_sha": "A" * 40, "verdict": "pass",
                          "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "ok"}]}, t, candidate_sha)
    with pytest.raises(ValueError):
        normalize_review({"ticket_id": t.ticket_id, "contract_hash": t.contract_hash,
                          "candidate_sha": candidate_sha, "verdict": "repair",
                          "criterion_results": [{"criterion_id": "AC-1", "status": "fail", "evidence": "failed"}],
                          "findings": [{"criterion_id": "AC-1", "file": "app.py", "severity": "suggestion",
                                        "symbol": "guard", "evidence": "issue", "minimal_repair": "fix", "verification": "test"}]}, t, candidate_sha)
    result = normalize_review({
        "ticket_id": t.ticket_id, "contract_hash": t.contract_hash, "candidate_sha": candidate_sha, "verdict": "pass",
        "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "test passed"}],
    }, t, candidate_sha)
    assert result.ticket_id == t.ticket_id and result.contract_hash == t.contract_hash and result.candidate_sha == candidate_sha
    with pytest.raises(TypeError):
        result.criterion_results[0]["status"] = "fail"