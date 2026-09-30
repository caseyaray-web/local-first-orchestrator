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
