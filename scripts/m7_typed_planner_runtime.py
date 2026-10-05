"""Production helpers for the bounded, future typed-planner rehearsal.

This module is deliberately source-controlled outside ``tests``.  The shell
runner imports it for its provider-free fixture and its authorized polling
loop, so fixture proof cannot silently test a disconnected lookalike.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from local_first_orchestrator.decomposition_planner import PlanningRequest, packet


TERMINAL_RUN_STATES = frozenset({"blocked", "done", "completed", "failed", "cancelled", "canceled", "stopped"})


def schema_delivery_receipt(delivered_packet: str, request: PlanningRequest) -> dict[str, object]:
    """Validate the exact public request-bound packet given to the planner."""
    if type(delivered_packet) is not str or type(request) is not PlanningRequest:
        raise ValueError("exact packet text and trusted request are required")
    try:
        payload = json.loads(delivered_packet)
    except json.JSONDecodeError as exc:
        raise ValueError("planner packet is not JSON") from exc
    if type(payload) is not dict or payload.get("request_identity") != request.identity:
        raise ValueError("planner packet request identity mismatch")
    schema = payload.get("schema")
    if type(schema) is not dict or schema.get("type") != "object":
        raise ValueError("planner packet has no decision schema")
    properties, required = schema.get("properties"), schema.get("required")
    expected = {"plan_id", "schema_version", "tranches", "criterion_coverage"}
    if (type(properties) is not dict or set(properties) != expected
            or type(required) is not list or set(required) != expected
            or "proposal_json" in properties):
        raise ValueError("planner packet did not deliver the closed typed decisions schema")
    return {"request_identity": request.identity, "schema": "request_bound_decisions", "legacy_proposal_json": False}


def planning_wait_outcome(task: Mapping[str, Any], *, task_id: str, run_id: str,
                          profile: str, persisted_proposal: object,
                          request_id: str, request_identity: object) -> dict[str, str]:
    """Fail closed unless one persisted plan is bound to this request and owned run."""
    if type(task) is not dict or task.get("id") != task_id:
        return {"action": "stop_cleanup_readback", "reason": "exact_task_missing_or_mismatched"}
    runs = task.get("runs")
    if type(runs) is not list:
        return {"action": "stop_cleanup_readback", "reason": "exact_task_runs_unknown"}
    owned = [run for run in runs if type(run) is dict and run.get("id") == run_id and run.get("profile") == profile]
    if len(owned) != 1:
        return {"action": "stop_cleanup_readback", "reason": "owned_run_missing_or_ambiguous"}
    proposal_ok = (type(persisted_proposal) is dict
                   and persisted_proposal.get("request_id") == request_id
                   and persisted_proposal.get("request_identity") == request_identity)
    if proposal_ok:
        return {"action": "proposal_persisted"}
    state = owned[0].get("status")
    if state in TERMINAL_RUN_STATES:
        return {"action": "stop_cleanup_readback", "reason": "owned_terminal_run_without_matching_persisted_proposal"}
    if not isinstance(state, str):
        return {"action": "stop_cleanup_readback", "reason": "owned_run_status_unknown"}
    return {"action": "continue_wait"}


def provider_free_fixture(run_root: Path) -> None:
    """Exercise the same runtime helpers with native-shaped saved JSON only."""
    request = PlanningRequest(
        board_id="fixture-board", anchor_id="fixture-anchor", repository_identity="fixture-repository",
        base_sha="a" * 40, snapshot_hash="b" * 64, root_contract_hash="c" * 64,
        expected_criteria=frozenset({"AC-1"}), authorized_paths=frozenset({"src/a.py"}),
        max_tranches=1, max_tickets=1, max_context_tokens=1024, max_patch_files=1,
        max_patch_lines=10, max_attempts=1, verification_commands=(("python", "-m", "pytest"),),
        verification_timeout_seconds=60, verification_output_limit=1024, max_payload_bytes=4096,
        max_json_depth=8, objective="fixture", non_goals=(), criterion_statements=(("AC-1", "fixture"),),
    )
    schema = schema_delivery_receipt(packet(request), request)
    terminal = planning_wait_outcome(
        {"id": "planner-task", "status": "blocked", "runs": [
            {"id": "planner-run", "profile": "planner", "status": "blocked"},
        ]}, task_id="planner-task", run_id="planner-run", profile="planner", persisted_proposal=None,
 request_id="fixture-request", request_identity=request.identity,
    )
    run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    (run_root / "fixture-receipt.json").write_text(json.dumps({
        "version": 1, "synthetic": True, "helper_module": __name__, "provider_calls": 0,
        "schema_delivery": schema, "terminal_wait": terminal,
    }, sort_keys=True) + "\n", encoding="utf-8")
