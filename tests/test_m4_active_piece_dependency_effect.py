from __future__ import annotations

import pytest

from tests.test_m4_active_piece_dependency_authority import (
    SCOPE, _batch_fixture, _database_rows, _seed_two,
)


def test_locked_helper_matches_public_proof_and_does_not_reacquire_lock(tmp_path, monkeypatch):
    coordinator, store, _board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        acquire_calls = []
        original_acquire = coordinator.lock.acquire
        coordinator.lock.acquire = lambda: (acquire_calls.append("acquire"), original_acquire())[1]
        with coordinator.lock:
            locked = coordinator._active_piece_dependency_authority_locked(
                "plan-1", "TK-B", "TK-A")
            assert coordinator.lock.assert_held() is None
        public = coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert dict(locked) == dict(public)
        assert acquire_calls == ["acquire", "acquire"]
        assert locked["frozen_create_receipts"] == public["frozen_create_receipts"]
    finally:
        store.close()


def test_locked_helper_rejects_unlocked_before_board_or_store_access(tmp_path, monkeypatch):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        reads = []
        writes = []
        original_read, original_write = board.read_task, store.read_scope
        board.read_task = lambda task_id: (reads.append(task_id), original_read(task_id))[1]
        store.read_scope = lambda *args, **kwargs: (writes.append("read"), original_write(*args, **kwargs))[1]
        with pytest.raises(Exception):
            coordinator._active_piece_dependency_authority_locked("plan-1", "TK-B", "TK-A")
        assert reads == []
        assert writes == []
    finally:
        store.close()


def test_locked_helper_preserves_full_barrier_and_invalid_edge_read_only(tmp_path, monkeypatch):
    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        before = _database_rows(store)
        reads = []
        original = board.read_task
        board.read_task = lambda task_id: (reads.append(task_id), original(task_id))[1]
        with coordinator.lock:
            with pytest.raises(ValueError):
                coordinator._active_piece_dependency_authority_locked("plan-1", "TK-A", "TK-B")
        assert reads == []
        assert _database_rows(store) == before

        # A third accepted tranche card is independently required by the barrier.
        from tests.test_m4_active_piece_dependency_authority import _three_card_fixture
        third_path = tmp_path / "third"
        third_path.mkdir(mode=0o700)
        third_path.chmod(0o700)
        coordinator2, store2, board2, cards2 = _three_card_fixture(third_path, monkeypatch)
        try:
            for ticket in ("TK-A", "TK-B", "TK-C"):
                assert coordinator2.prepare_active_piece("plan-1", ticket)["outcome"] == "held"
            original_c = cards2["native-TK-C"]
            cards2["native-TK-C"] = type(original_c)(native_task=original_c.native_task,
                parents=original_c.parents, runs=original_c.runs,
                comments=({"body": "drift"},), events=original_c.events,
                attachments=original_c.attachments, observed_at=original_c.observed_at,
                digest=original_c.digest)
            before_c = _database_rows(store2)
            with coordinator2.lock:
                with pytest.raises(ValueError):
                    coordinator2._active_piece_dependency_authority_locked("plan-1", "TK-B", "TK-A")
            assert _database_rows(store2) == before_c
        finally:
            store2.close()
    finally:
        store.close()


def test_persisted_context_is_non_authorizing_frozen_original_identity_without_native_calls(
    tmp_path, monkeypatch,
):
    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        before = _database_rows(store)
        calls = []
        original_read, original_verify = board.read_task, board.verify_effect
        board.read_task = lambda task_id: (calls.append(("read", task_id)), original_read(task_id))[1]
        board.verify_effect = lambda action: (calls.append(("verify", action.key)), original_verify(action))[1]
        with coordinator.lock:
            context = coordinator._active_piece_dependency_persisted_context_locked(
                "plan-1", "TK-B", "TK-A")
        assert context["kind"] == "accepted_first_link_persisted_context_v1"
        assert context["requires_native_receipt_validation"] is True
        assert calls == []
        assert _database_rows(store) == before

        authority = coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert dict(context["authority_identity"]) == {
            key: authority[key] for key in context["authority_identity"]
        }
        assert context["operation_key"] == authority["operation_key"]
        assert context["frozen_create_receipts"] == authority["frozen_create_receipts"]
        with pytest.raises(TypeError):
            context["authority_identity"]["plan_id"] = "changed"

        original = cards["native-TK-A"]
        cards["native-TK-A"] = type(original)(native_task=original.native_task,
            parents=original.parents, runs=original.runs, comments=({"body": "drift"},),
            events=original.events, attachments=original.attachments,
            observed_at=original.observed_at, digest=original.digest)
        calls.clear()
        with coordinator.lock:
            drift_context = coordinator._active_piece_dependency_persisted_context_locked(
                "plan-1", "TK-B", "TK-A")
        assert drift_context["frozen_create_receipts"] == context["frozen_create_receipts"]
        assert calls == []
        with pytest.raises(ValueError, match="fresh native card differs"):
            coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert _database_rows(store) == before
    finally:
        store.close()


@pytest.mark.parametrize("fault", ("edge", "request", "member", "pause"))
def test_persisted_context_rejects_invalid_edge_request_member_and_pause_before_native_calls(
    tmp_path, monkeypatch, fault,
):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        calls = []
        original_read, original_verify = board.read_task, board.verify_effect
        board.read_task = lambda task_id: (calls.append(("read", task_id)), original_read(task_id))[1]
        board.verify_effect = lambda action: (calls.append(("verify", action.key)), original_verify(action))[1]
        target, dependency = "TK-B", "TK-A"
        if fault == "edge":
            target, dependency = "TK-A", "TK-B"
        elif fault == "request":
            coordinator.planning_observer = lambda scope: {"request": {}}
        else:
            original_scope = store.read_scope

            def changed_scope(scope):
                state = original_scope(scope)
                if fault == "member":
                    assert any(member.task_id == "native-TK-B" for member in state["members"])
                    state["members"] = tuple(
                        member for member in state["members"] if member.task_id != "native-TK-B")
                else:
                    from local_first_orchestrator.contracts import PauseIntent
                    state["operator_intent"] = PauseIntent(SCOPE, "operator", 1, True, False)
                return state

            store.read_scope = changed_scope
        with coordinator.lock:
            with pytest.raises((ValueError, KeyError)):
                coordinator._active_piece_dependency_persisted_context_locked(
                    "plan-1", target, dependency)
        assert calls == []
    finally:
        store.close()
