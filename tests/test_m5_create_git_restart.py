"""M5 crash/reopen evidence for held-card creation and real Git CAS integration."""
from __future__ import annotations

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import CandidateIdentity
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.git_adapter import GitWorktreeAdapter
from tests.test_m4_public_core_flow import (
    _candidate,
    _complete_same_card_local_review,
    _install_board,
    _regression_core,
    _review,
)


class InjectedCrash(BaseException):
    """Simulate process death; ordinary coordinator exception handling must not absorb it."""


def _reopen(ctl, database, board, tmp_path):
    store = EvidenceStore.open(database)
    # The public-core fixture deliberately supplies its temporary repository base
    # through this observer seam; restore the same trusted fixture binding on the
    # fresh coordinator rather than carrying an old SQLite method closure over.
    base = getattr(ctl, "_m5_git_base", None)
    if base is not None:
        accepted_reader = store.read_accepted_plan
        store.read_accepted_plan = lambda scope, plan_id: {**accepted_reader(scope, plan_id), "base_sha": base}
    return Coordinator(
        ctl.scope, board=board, store=store,
        lock=instance_lock(tmp_path / "reopened.lock"), budget_policy=ctl.budget_policy,
        configured_roles=ctl.configured_roles, planning_observer=ctl.planning_observer,
        planning_profile=ctl.planning_profile, planning_workspace=ctl.planning_workspace,
    )


def _create_context(tmp_path, monkeypatch):
    ctl, store, board, cards, _runs = _regression_core(tmp_path, monkeypatch)
    calls = []
    native_create = board.create_held

    def counted_create(*args, **kwargs):
        result = native_create(*args, **kwargs)
        # The fake's durable claim runs inside native_create, before its simulated
        # mutation, matching the production adapter's pre-create boundary.
        calls.append(args[0].key)
        return result

    board.create_held = counted_create
    return ctl, store, board, cards, calls


def _create_operation(store, scope):
    return next(op for op in store.read_scope(scope)["operations"]
                if op.effect == "create_held" and op.target.get("ticket_id") == "TK-A")


def _git_context(tmp_path, monkeypatch):
    ctl, store, board, cards, runs = _regression_core(tmp_path, monkeypatch)
    held = ctl.prepare_active_tranche("plan-1")
    assert held["outcome"] == "held"
    assert ctl.release_active_piece("plan-1", "TK-A")["outcome"] == "released"
    repo = tmp_path / "repo"
    repo.mkdir()
    from tests.test_m4_public_core_flow import _git
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "M5")
    _git(repo, "config", "user.email", "m5@example.invalid")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    adapter = GitWorktreeAdapter(repo, tmp_path / "attempts")
    adapter.resolve_execution_base("TR-A", base)
    accepted_reader = store.read_accepted_plan
    store.read_accepted_plan = lambda scope, plan_id: {**accepted_reader(scope, plan_id), "base_sha": base}
    candidate = _candidate(adapter, repo, "TK-A", base, "a.txt")
    task_id = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == "TK-A")
    review = _review("local-A-review", candidate, role="local", task_id=task_id,
                     run_id="local-A", session="local-A-session")
    _complete_same_card_local_review(ctl, store, cards, runs, monkeypatch, "TK-A", task_id, candidate, review)
    assert ctl.submit_local_review("plan-1", "TK-A", candidate, review)["outcome"] == "approved"
    ctl._m5_git_base = base
    return ctl, store, board, adapter, candidate, base


@pytest.mark.parametrize("boundary", ["before_claim", "after_claim_before_effect"])
def test_m5_held_create_claim_boundaries_reopen_without_native_duplicate(tmp_path, monkeypatch, boundary):
    """Both claim boundaries use the public accepted held-create API after reopen."""
    ctl, store, board, cards, calls = _create_context(tmp_path, monkeypatch)
    database = store.path
    real_begin = store.begin_effect_attempt
    if boundary == "before_claim":
        monkeypatch.setattr(store, "begin_effect_attempt", lambda *_a, **_k: (_ for _ in ()).throw(InjectedCrash("crash before create claim")))
    else:
        def claim_then_crash(*args, **kwargs):
            real_begin(*args, **kwargs)
            raise InjectedCrash("crash after create claim")
        monkeypatch.setattr(store, "begin_effect_attempt", claim_then_crash)
    try:
        with pytest.raises(InjectedCrash, match="crash .*create claim"):
            ctl.prepare_active_piece("plan-1", "TK-A")
        assert calls == []
        operation = _create_operation(store, ctl.scope)
        assert operation.phase == ("pending" if boundary == "before_claim" else "unknown")
    finally:
        store.close()

    resumed = _reopen(ctl, database, board, tmp_path)
    try:
        result = resumed.prepare_active_piece("plan-1", "TK-A")
        if boundary == "before_claim":
            assert result["outcome"] == "held" and result["actions_attempted"] == 1
            assert calls == [operation.key]
        else:
            assert result["outcome"] == "partial" and result["actions_attempted"] == 0
            assert calls == []
        # A claim-before-effect crash has no native card; the pending branch sends
        # exactly one on reopen.  Neither branch creates a duplicate.
        assert len(cards) == (1 if boundary == "before_claim" else 0)
    finally:
        resumed.store.close()


def test_m5_held_create_after_native_effect_before_ack_reopens_by_exact_marker_without_duplicate(tmp_path, monkeypatch):
    ctl, store, board, cards, calls = _create_context(tmp_path, monkeypatch)
    database = store.path
    native_create = board.create_held

    def create_then_crash(*args, **kwargs):
        native_create(*args, **kwargs)
        raise InjectedCrash("crash after native create")

    board.create_held = create_then_crash
    try:
        with pytest.raises(InjectedCrash, match="crash after native create"):
            ctl.prepare_active_piece("plan-1", "TK-A")
        operation = _create_operation(store, ctl.scope)
        assert calls == [operation.key]
        assert operation.phase == "unknown"
    finally:
        store.close()

    resumed = _reopen(ctl, database, board, tmp_path)
    try:
        result = resumed.prepare_active_piece("plan-1", "TK-A")
        assert result["outcome"] == "held" and result["actions_attempted"] == 0
        assert calls == [operation.key]
        assert len(cards) == 1
        assert len([m for m in resumed.store.read_scope(resumed.scope)["members"] if m.role == "implementation"]) == 1
    finally:
        resumed.store.close()


def test_m5_held_create_after_ack_reopen_is_readonly(tmp_path, monkeypatch):
    ctl, store, board, cards, calls = _create_context(tmp_path, monkeypatch)
    database = store.path
    try:
        first = ctl.prepare_active_piece("plan-1", "TK-A")
        assert first["outcome"] == "held" and first["actions_attempted"] == 1
        operation = _create_operation(store, ctl.scope)
        assert operation.phase == "applied" and calls == [operation.key]
    finally:
        store.close()

    resumed = _reopen(ctl, database, board, tmp_path)
    try:
        replay = resumed.prepare_active_piece("plan-1", "TK-A")
        assert replay["outcome"] == "held" and replay["actions_attempted"] == 0
        assert calls == [operation.key] and len(cards) == 1
    finally:
        resumed.store.close()


@pytest.mark.parametrize("boundary", ["before_claim", "after_claim_before_effect"])
def test_m5_git_cas_claim_boundaries_reopen_without_ref_advance(tmp_path, monkeypatch, boundary):
    ctl, store, board, adapter, candidate, base = _git_context(tmp_path, monkeypatch)
    database = store.path
    real_begin = store.begin_effect_attempt
    if boundary == "before_claim":
        monkeypatch.setattr(store, "begin_effect_attempt", lambda *_a, **_k: (_ for _ in ()).throw(InjectedCrash("crash before git claim")))
    else:
        def claim_then_crash(*args, **kwargs):
            real_begin(*args, **kwargs)
            raise InjectedCrash("crash after git claim")
        monkeypatch.setattr(store, "begin_effect_attempt", claim_then_crash)
    try:
        with pytest.raises(InjectedCrash, match="crash .*git claim"):
            ctl.integrate_active_piece("plan-1", "TK-A", candidate, review_id="local-A-review", git_adapter=adapter)
        operation = next(op for op in store.read_scope(ctl.scope)["operations"] if op.effect == "git_integrate")
        assert adapter.existing_execution_base("TR-A", base) == base
        assert operation.phase == ("pending" if boundary == "before_claim" else "unknown")
    finally:
        store.close()

    resumed = _reopen(ctl, database, board, tmp_path)
    try:
        result = resumed.integrate_active_piece("plan-1", "TK-A", candidate, review_id="local-A-review", git_adapter=adapter)
        if boundary == "before_claim":
            assert result["outcome"] == "integrated" and adapter.existing_execution_base("TR-A", base) == candidate.head_sha
        else:
            assert result["outcome"] == "partial" and adapter.existing_execution_base("TR-A", base) == base
    finally:
        resumed.store.close()


def test_m5_git_cas_after_effect_before_ack_reopens_by_exact_ref_without_second_advance(tmp_path, monkeypatch):
    ctl, store, board, adapter, candidate, base = _git_context(tmp_path, monkeypatch)
    database = store.path
    native_advance = adapter.advance_integration_head
    advances = []

    def advance_then_crash(*args, **kwargs):
        advances.append((args, kwargs))
        native_advance(*args, **kwargs)
        raise InjectedCrash("crash after git CAS")

    monkeypatch.setattr(adapter, "advance_integration_head", advance_then_crash)
    try:
        with pytest.raises(InjectedCrash, match="crash after git CAS"):
            ctl.integrate_active_piece("plan-1", "TK-A", candidate, review_id="local-A-review", git_adapter=adapter)
        assert adapter.existing_execution_base("TR-A", base) == candidate.head_sha
        operation = next(op for op in store.read_scope(ctl.scope)["operations"] if op.effect == "git_integrate")
        assert operation.phase == "unknown" and len(advances) == 1
    finally:
        store.close()

    resumed = _reopen(ctl, database, board, tmp_path)
    try:
        result = resumed.integrate_active_piece("plan-1", "TK-A", candidate, review_id="local-A-review", git_adapter=adapter)
        assert result["outcome"] == "integrated" and len(advances) == 1
        assert resumed.store.read_scope(resumed.scope)["operations"][-1].phase == "applied"
    finally:
        resumed.store.close()


def test_m5_git_cas_after_ack_reopen_is_idempotent(tmp_path, monkeypatch):
    ctl, store, board, adapter, candidate, base = _git_context(tmp_path, monkeypatch)
    database = store.path
    advances = []
    native_advance = adapter.advance_integration_head
    adapter.advance_integration_head = lambda *args, **kwargs: (advances.append((args, kwargs)) or native_advance(*args, **kwargs))
    real_ack = store.ack_effect
    monkeypatch.setattr(store, "ack_effect", lambda *args, **kwargs: (real_ack(*args, **kwargs), (_ for _ in ()).throw(InjectedCrash("crash after git ack")))[1])
    try:
        with pytest.raises(InjectedCrash, match="crash after git ack"):
            ctl.integrate_active_piece("plan-1", "TK-A", candidate, review_id="local-A-review", git_adapter=adapter)
        assert adapter.existing_execution_base("TR-A", base) == candidate.head_sha and len(advances) == 1
    finally:
        store.close()

    resumed = _reopen(ctl, database, board, tmp_path)
    try:
        replay = resumed.integrate_active_piece("plan-1", "TK-A", candidate, review_id="local-A-review", git_adapter=adapter)
        assert replay["outcome"] == "integrated" and len(advances) == 1
    finally:
        resumed.store.close()
