import dataclasses
import hashlib
import json
import subprocess
import sys

import pytest

from local_first_orchestrator.decomposition import (
    Criterion,
    DecompositionPlan,
    PlanValidator,
    TranchePlan,
)
from local_first_orchestrator.review import (
    ReviewFinding,
    ReviewPacketBuilder,
    failure_fingerprint,
    normalize_review,
    validate_review_identity,
)
from local_first_orchestrator.ticket import (
    PatchBudget,
    TicketContract,
    VerificationProfile,
    contract_payload,
    declared_ticket_paths,
    parse_contract,
)


def ticket(*, ticket_id="TK-1", criterion_ids=("AC-1",), dependencies=()):
    return TicketContract(
        ticket_id=ticket_id,
        objective="Implement guard",
        criterion_ids=criterion_ids,
        non_goals=("No API changes",),
        allowed_paths=("app.py", "test_app.py"),
        verification=VerificationProfile(
            commands=(("python", "-m", "pytest"),),
            timeout_seconds=60,
            output_limit=20000,
        ),
        patch_budget=PatchBudget(max_files=2, max_changed_lines=100, max_attempts=2),
        context_budget_tokens=4096,
        dependencies=dependencies,
    )


def plan(*, tickets=None, coverage=None):
    tickets = tickets or (ticket(),)
    return DecompositionPlan(
        plan_id="PL-1",
        schema_version=1,
        tranches=(TranchePlan("TR-1", 0, tickets, ("AC-1",)),),
        criterion_coverage=coverage or {"AC-1": tuple(t.ticket_id for t in tickets)},
    )


def validator(*, expected=("AC-1",), max_tickets=100):
    return PlanValidator(
        expected_criteria=frozenset(expected), max_tranches=10, max_tickets=max_tickets
    )


def test_incomplete_plugin_entrypoint_refuses_registration():
    import importlib.util
    from pathlib import Path

    entrypoint = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("m1_plugin_entrypoint", entrypoint)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="not ready"):
        module.register(object())


def test_pure_destination_contract_imports_do_not_import_legacy_runtime():
    script = '''import sys
import local_first_orchestrator as package
import local_first_orchestrator.ticket
import local_first_orchestrator.decomposition
import local_first_orchestrator.review
assert package.TicketContract is local_first_orchestrator.ticket.TicketContract
assert not hasattr(package, "Ledger")
for name in ("ledger", "states", "local_qwen", "controller", "hermes_board"):
    assert "local_first_orchestrator." + name not in sys.modules, name
'''
    completed = subprocess.run(
        (sys.executable, "-c", script), capture_output=True, text=True, timeout=10
    )
    assert completed.returncode == 0, completed.stderr


def test_ticket_is_immutable_versioned_canonically_identified_and_round_trips():
    contract = ticket()
    payload = contract_payload(contract)
    assert contract.schema_version == 1
    assert contract.contract_hash == hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert parse_contract(payload) == contract
    assert declared_ticket_paths(contract) == ("app.py", "test_app.py")
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.objective = "changed"


def test_parse_contract_rejects_unknown_nested_budget_and_verification_fields():
    for nested in ("patch_budget", "verification"):
        payload = contract_payload(ticket())
        nested_payload = payload[nested]
        assert isinstance(nested_payload, dict)
        nested_payload["unexpected"] = "not part of canonical identity"
        with pytest.raises(ValueError, match="unexpected"):
            parse_contract(payload)


def test_ticket_contract_requires_bounded_immutable_values():
    with pytest.raises(TypeError):
        PatchBudget()
    with pytest.raises(TypeError):
        VerificationProfile(commands=(("python", "-m", "pytest"),))
    with pytest.raises(ValueError):
        dataclasses.replace(ticket(), allowed_paths=("../outside.py",))
    with pytest.raises(ValueError):
        dataclasses.replace(ticket(), criterion_ids=["AC-1"])
    with pytest.raises(ValueError):
        dataclasses.replace(ticket(), dependencies=tuple(f"TK-{i}" for i in range(2049)))
    with pytest.raises(ValueError):
        VerificationProfile(
            commands=(("python", "-m", "pytest"),), timeout_seconds=0, output_limit=20000
        )


def test_verification_profile_has_hard_argv_resource_caps():
    valid = VerificationProfile(
        commands=(("python", "-m", "pytest"), ("python", "-m", "compileall", ".")),
        timeout_seconds=60,
        output_limit=20000,
    )
    assert len(valid.commands) == 2
    for commands in ((("x",),) * 33, (("x",) * 33,), (("x" * 257,),), ((" ",),)):
        with pytest.raises(ValueError):
            VerificationProfile(commands=commands, timeout_seconds=60, output_limit=20000)


def test_plan_hash_binds_full_content_and_rejects_conflicting_duplicate_plan_id():
    original = plan()
    changed = dataclasses.replace(original, tranches=(dataclasses.replace(original.tranches[0], tickets=(dataclasses.replace(ticket(), objective="Different objective"),)),))
    assert original.contract_hash != changed.contract_hash
    known = {original.plan_id: original.contract_hash}
    assert validator().validate(original, known_plan_hashes=known) == ()
    assert "duplicate_plan_id" in validator().validate(changed, known_plan_hashes=known)
    assert original.contract_hash == plan().contract_hash


def test_review_rejects_stale_candidate_during_normalization():
    contract = ticket()
    with pytest.raises(ValueError, match="candidate identity"):
        normalize_review(review_payload(contract, candidate_sha="b" * 40), contract, expected_candidate_sha="a" * 40)


def test_decomposition_plan_validator_reports_duplicate_ids_cycles_and_coverage():
    first = ticket(dependencies=("TK-2",))
    second = ticket(ticket_id="TK-2", dependencies=("TK-1",))
    cyclic = plan(tickets=(first, second), coverage={"AC-1": ("TK-1", "TK-2")})
    assert "dependency_cycle" in validator().validate(cyclic)

    duplicate = DecompositionPlan(
        "PL-1", 1,
        (TranchePlan("TR-1", 0, (ticket(),), ("AC-1",)), TranchePlan("TR-1", 1, (), ("AC-1",))),
        {"AC-1": ("TK-1",)},
    )
    assert "duplicate_tranche_id" in validator().validate(duplicate)
    assert "criterion_coverage_mismatch" in validator().validate(
        dataclasses.replace(plan(), criterion_coverage={})
    )


def test_decomposition_plan_validator_requires_declared_scope_and_finite_limits():
    criteria = (Criterion("AC-1", "Guard is implemented"),)
    assert validator().validate(plan()) == ()
    assert "missing_required_criterion" in validator(expected=("AC-1", "AC-2")).validate(plan())
    assert "unknown_criterion" in validator(expected=("AC-2",)).validate(plan())
    assert validator().validate_tranche(plan().tranches[0], criteria) == ()
    with pytest.raises(ValueError, match="trusted plan limits"):
        PlanValidator(expected_criteria=frozenset({"AC-1"}), max_tranches=0, max_tickets=100)


def test_decomposition_plan_rejects_future_dependencies_and_uncovered_ticket_criteria():
    early = TranchePlan("TR-1", 0, (ticket(dependencies=("TK-2",)),), ("AC-1",))
    later = TranchePlan("TR-2", 1, (ticket(ticket_id="TK-2"),), ("AC-1",))
    future = DecompositionPlan("PL-1", 1, (early, later), {"AC-1": ("TK-1", "TK-2")})
    assert "dependency_future_tranche" in validator().validate(future)

    other = ticket(ticket_id="TK-2")
    incomplete = plan(tickets=(ticket(), other), coverage={"AC-1": ("TK-1",)})
    assert "ticket_criterion_uncovered" in validator().validate(incomplete)


def review_payload(contract, *, verdict="pass", candidate_sha="a" * 40, results=None, findings=None):
    return {
        "ticket_id": contract.ticket_id,
        "contract_hash": contract.contract_hash,
        "candidate_sha": candidate_sha,
        "verdict": verdict,
        "criterion_results": results if results is not None else [
            {"criterion_id": "AC-1", "status": "pass", "evidence": "test passed"}
        ],
        "findings": findings if findings is not None else [],
    }


def test_review_normalization_is_pinned_fail_closed_and_fingerprinted():
    contract = ticket()
    with pytest.raises(ValueError):
        normalize_review({"verdict": "pass", "criterion_results": []}, contract, expected_candidate_sha="a" * 40)
    with pytest.raises(ValueError):
        normalize_review(review_payload(contract, verdict="repair"), contract, expected_candidate_sha="a" * 40)
    with pytest.raises(ValueError):
        normalize_review(
            review_payload(
                contract,
                verdict="pass",
                results=[
                    {"criterion_id": "AC-1", "status": "pass", "evidence": "ok"},
                    {"criterion_id": "AC-1", "status": "pass", "evidence": "duplicate"},
                ],
            ),
            contract,
            expected_candidate_sha="a" * 40,
        )
    result = normalize_review(review_payload(contract), contract, expected_candidate_sha="a" * 40)
    assert result.verdict == "pass"
    assert failure_fingerprint(result) is None
    with pytest.raises(TypeError):
        result.criterion_results[0]["status"] = "fail"


def test_repair_requires_valid_blocker_and_review_identity_matches_candidate():
    contract = ticket()
    finding = ReviewFinding(
        criterion_id="AC-1", file="app.py", symbol="guard", evidence="fails",
        minimal_repair="fix guard", verification="pytest",
    )
    result = normalize_review(
        review_payload(
            contract,
            verdict="repair",
            results=[{"criterion_id": "AC-1", "status": "fail", "evidence": "fails"}],
            findings=[dataclasses.asdict(finding)],
        ),
        contract,
        expected_candidate_sha="a" * 40,
    )
    assert failure_fingerprint(result)
    assert validate_review_identity(
        result, {"ticket_id": contract.ticket_id, "contract_hash": contract.contract_hash, "candidate_sha": "a" * 40}
    )
    assert not validate_review_identity(
        result, {"ticket_id": contract.ticket_id, "contract_hash": contract.contract_hash, "candidate_sha": "b" * 40}
    )


def test_review_packet_builder_pins_contract_candidate_and_checks():
    contract = ticket()
    packet = ReviewPacketBuilder.build(
        candidate={"sha": "a" * 40, "base_sha": "b" * 40},
        checks=({"command": ["python", "-m", "pytest"], "status": "pass"},),
        contract=contract,
    )
    assert packet["ticket_id"] == contract.ticket_id
    assert packet["contract_hash"] == contract.contract_hash
    assert packet["candidate_sha"] == "a" * 40
    assert packet["checks"][0]["status"] == "pass"
