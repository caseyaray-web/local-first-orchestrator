"""Parent-only M5 restart evidence against the pinned disposable Hermes CLI.

These fixtures never start a provider, worker, dispatcher, or delegated child.
They are skipped before fixture setup in delegated-child contexts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from tests.test_m4_active_piece_preparation import _accepted_plan, _batch_proposal
from tests.test_m4_planner_creation import native_fixture as _base_native_fixture, setup
from tests.test_m4_planner_run_binding import _claim


class InjectedProcessDeath(BaseException):
    """Deliberately bypass normal ``Exception`` recovery, as process death does."""


@pytest.fixture
def parent_native_fixture(tmp_path):
    # Keep this first: no home, SQLite, CLI, or adapter exists in a child context.
    if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
        pytest.skip("native restart fixture mutations require the authorized parent context")
    executable = os.environ.get("HERMES_M0_CLI")
    if not executable or not Path(executable).is_file():
        pytest.skip("set HERMES_M0_CLI to the pinned installed Hermes executable")
    return _base_native_fixture.__wrapped__(tmp_path)


def _reopen(original, database, adapter, tmp_path):
    """Use fresh SQLite/coordinator objects and restore only public fixture context."""
    store = EvidenceStore.open(database)
    return Coordinator(
        original.scope,
        board=adapter,
        store=store,
        lock=instance_lock(tmp_path / "recovered.lock"),
        budget_policy=original.budget_policy,
        configured_roles=original.configured_roles,
        planning_observer=original.planning_observer,
        planning_profile=original.planning_profile,
        planning_workspace=original.planning_workspace,
    ), store


def _task_for_ticket(store, scope, held, ticket_id):
    piece = next(piece for piece in held["pieces"] if piece["ticket_id"] == ticket_id)
    operation = next(op for op in store.read_scope(scope)["operations"] if op.key == piece["operation_key"])
    return piece["task_id"], operation


def test_parent_native_v2_link_checkpoint_recovery_acks_without_resend(
    tmp_path, parent_native_fixture, monkeypatch
):
    """Persisted validated v2 receipt survives a process death and fresh coordinator."""
    (_board, _anchor, _workspace, adapter, membership, cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(
        tmp_path, parent_native_fixture, monkeypatch, proposal_factory=_batch_proposal,
    )
    database = store.path
    try:
        held = controller.prepare_active_tranche(accepted["plan_id"], request_id=request_id)
        parent_task, _ = _task_for_ticket(store, scope, held, "TK-A")
        child_task, _ = _task_for_ticket(store, scope, held, "TK-B")
        native_links = []
        invoke = adapter._invoke

        def counted_invoke(*args, **kwargs):
            if args and args[0] == "link":
                native_links.append(args[1:])
            return invoke(*args, **kwargs)

        original_ack = store.ack_effect

        def checkpoint_then_die(*args, **kwargs):
            # The coordinator writes the validated-link checkpoint before ack.
            raise InjectedProcessDeath("after validated receipt checkpoint before ack")

        monkeypatch.setattr(adapter, "_invoke", counted_invoke)
        monkeypatch.setattr(store, "ack_effect", checkpoint_then_die)
        with pytest.raises(InjectedProcessDeath, match="validated receipt checkpoint"):
            controller.execute_accepted_dependency_link(
                accepted["plan_id"], "TK-B", "TK-A", request_id=request_id
            )
        link = next(op for op in store.read_scope(scope)["operations"] if op.effect == "link")
        assert link.phase == "unknown"
        assert store.read_validated_link_receipt_checkpoint(scope, link.key) is not None
        assert native_links == [(parent_task, child_task)]
        # Prove the external CLI effect happened before closing SQLite.
        child_show = json.loads(cli("show", child_task, "--json").stdout)
        assert parent_task in child_show["parents"]
        monkeypatch.setattr(store, "ack_effect", original_ack)
    finally:
        store.close()

    recovered, reopened = _reopen(controller, database, adapter, tmp_path)
    # The adapter's native membership callback is intentionally a mutable test
    # fixture closure.  Rebind it to the reopened authoritative store before
    # recovery; do not weaken the adapter's managed-child guard.
    membership["store"] = reopened
    try:
        assert adapter._trusted_member(scope, parent_task)
        assert adapter._trusted_member(scope, child_task)
        result = recovered.execute_accepted_dependency_link(
            accepted["plan_id"], "TK-B", "TK-A", request_id=request_id
        )
        assert result == {"outcome": "linked", "operation_key": link.key, "actions_attempted": 0}
        assert native_links == [(parent_task, child_task)]
        applied = next(op for op in reopened.read_scope(scope)["operations"] if op.key == link.key)
        assert applied.phase == "applied" and applied.outcome == "verified"
        assert json.loads(cli("show", child_task, "--json").stdout)["parents"] == [parent_task]
        assert len([op for op in reopened.read_scope(scope)["operations"] if op.effect == "link"]) == 1
    finally:
        reopened.close()


def test_parent_native_pause_resume_restarts_once_with_operator_pause_preserved(
    tmp_path, parent_native_fixture, monkeypatch
):
    """Closest supported native continuation: public pause/resume, not provider recovery."""
    _board, _anchor, _workspace, adapter, membership, _cli = parent_native_fixture
    controller, store, scope, _req = setup(tmp_path, parent_native_fixture)
    database = store.path
    try:
        planner = controller.prepare_planner(request_id="m5-pause-resume")
        assert controller.release_planner(request_id="m5-pause-resume")["outcome"] == "released"
        task_id = planner["task_id"]
        sends = []
        raw_mutations = []
        invoke = adapter._invoke

        def counted_invoke(*args, **kwargs):
            # The supported adapter intentionally projects semantic hold/release
            # through the documented native block/unblock verbs.  Record both
            # layers so this fixture proves the real command, not an invented
            # transport name.
            if args and args[0] in {"block", "unblock"}:
                raw_mutations.append((args[0], args[1:]))
                sends.append(({"block": "hold", "unblock": "release"}[args[0]], args[1:]))
            return invoke(*args, **kwargs)

        monkeypatch.setattr(adapter, "_invoke", counted_invoke)
        paused = controller.pause()
        assert paused["outcome"] == "verified"
        assert adapter.read_task(task_id).native_task["status"] == "blocked"
        assert any(run.get("status") == "blocked" for run in adapter.read_task(task_id).runs)
        assert [name for name, _args in sends].count("hold") == 1
        assert [name for name, _args in raw_mutations].count("block") == 1
        assert store.read_scope(scope)["operator_intent"] is not None
    finally:
        store.close()

    recovered, reopened = _reopen(controller, database, adapter, tmp_path)
    # The native membership closure belongs to the fixture, so recovery must
    # consult the reopened authoritative EvidenceStore rather than the closed
    # original connection.
    membership["store"] = reopened
    try:
        # The explicit operator gate remains durable across a true close/open.
        blocked = recovered.tick()
        assert blocked["actions_attempted"] == 0
        assert reopened.read_scope(scope)["operator_intent"].active is True
        # Resume is deliberately three durable phases for the two managed cards:
        # one exact release per card, then a readback-only clear. No release key
        # may be replayed after its own native acknowledgement.
        # Members are released in task-ID order, not anchor/planner role order.
        first_task, second_task = sorted((_anchor, task_id))
        first_release = recovered.resume(authorized_clear=True)
        assert first_release == {"outcome": "partial", "reason": "resuming_release_in_progress", "actions_attempted": 1}
        assert adapter.read_task(first_task).native_task["status"] == "ready"
        assert adapter.read_task(second_task).native_task["status"] == "blocked"
        assert [name for name, _args in sends].count("release") == 1
        assert reopened.read_scope(scope)["operator_intent"] is not None
        second_release = recovered.resume(authorized_clear=True)
        assert second_release == {"outcome": "partial", "reason": "resuming_release_in_progress", "actions_attempted": 1}
        assert adapter.read_task(first_task).native_task["status"] == "ready"
        assert adapter.read_task(second_task).native_task["status"] == "ready"
        assert [name for name, _args in sends].count("release") == 2
        assert [name for name, _args in raw_mutations].count("unblock") == 2
        cleared = recovered.resume(authorized_clear=True)
        assert cleared == {"outcome": "verified", "reason": "release_verified", "actions_attempted": 0}
        assert [name for name, _args in sends].count("release") == 2
        assert [name for name, _args in raw_mutations].count("unblock") == 2
        retained_intent = reopened.read_scope(scope)["operator_intent"]
        assert retained_intent is not None
        assert retained_intent.active is False
    finally:
        reopened.close()
