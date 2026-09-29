from __future__ import annotations

import os
import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

from local_first_orchestrator.contracts import Action, OperationIntent
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.hermes_board import HermesBoardAdapter


SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor-1"}


@dataclass
class FakeKanban:
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    create_returncode_without_effect: int | None = None

    def add(self, task_id: str, *, title: str | None = None, body: str = "body", status: str = "blocked", assignee: str = "worker", workspace: str = "dir:/repo") -> None:
        self.tasks[task_id] = {"task": {"id": task_id, "title": title or task_id, "body": body, "status": status, "assignee": assignee, "workspace": workspace}, "parents": [], "comments": [], "events": [], "runs": [], "attachments": []}

    def __call__(self, argv, **kwargs):
        assert kwargs["shell"] is False
        args = tuple(argv[4:]); self.calls.append(tuple(argv))
        if args[0] == "show": return subprocess.CompletedProcess(argv, 0, json.dumps(self.tasks.get(args[1], {})), "")
        if args[0] == "runs": return subprocess.CompletedProcess(argv, 0, json.dumps(self.tasks[args[1]]["runs"]), "")
        if args[0] == "list": return subprocess.CompletedProcess(argv, 0, json.dumps([task["task"] for task in self.tasks.values()]), "")
        if args[0] == "create":
            if self.create_returncode_without_effect is not None:
                code, self.create_returncode_without_effect = self.create_returncode_without_effect, None
                return subprocess.CompletedProcess(argv, code, "", "no-effect failure")
            value = lambda name: args[args.index(name) + 1]
            task_id = f"new-{len(self.tasks) + 1}"
            self.add(task_id, title=args[1], body=value("--body"), assignee=value("--assignee"), workspace=value("--workspace"))
            self.tasks[task_id]["task"]["idempotency_key"] = value("--idempotency-key")
            self.tasks[task_id]["parents"] = [value("--parent")]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"id": task_id}), "")
        if args[0] == "block": self.tasks[args[1]]["task"]["status"] = "blocked"
        elif args[0] == "unblock": self.tasks[args[1]]["task"]["status"] = "ready"
        else: return subprocess.CompletedProcess(argv, 2, "", "unsupported")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _board(fake: FakeKanban, tmp_path, *, lock_assertion, claim_create_attempt) -> HermesBoardAdapter:
    executable = tmp_path / "hermes"
    executable.write_text("fixture")
    return HermesBoardAdapter(
        board="fixture-board", anchor_task_id="anchor-1", executable=str(executable), runner=fake,
        hermes_home=tmp_path / "hermes-home", kanban_home=tmp_path / "kanban-home",
        create_lock_assertion=lock_assertion, claim_create_attempt=claim_create_attempt,
    )


def _action(board: HermesBoardAdapter) -> Action:
    return Action("create-1", SCOPE, {"anchor_task_id": "anchor-1"}, "create_held", board.read_task("anchor-1").digest)


def _reserve(store: EvidenceStore, action: Action) -> None:
    store.reserve_operation(OperationIntent(
        key=action.key, scope=action.scope, target=action.target, effect=action.effect,
        expected_observed_identity=action.expected_observed_identity, before_evidence={"native": "before"},
        outcome=None, readback=None, retry={"stable_marker": action.key}, phase="pending",
    ))


def _create(board: HermesBoardAdapter, action: Action):
    return board.create_held(action, title="new", body="body <!-- local-first-action:create-1 -->",
                             assignee="worker", workspace="dir:/repo", idempotency_key="create-1")


def _create_calls(fake: FakeKanban) -> list[tuple[str, ...]]:
    return [call for call in fake.calls if call[4] == "create"]


def test_real_instance_lock_and_store_allow_exactly_one_first_create(tmp_path):
    fake = FakeKanban(); fake.add("anchor-1")
    database = tmp_path / "evidence.sqlite3"
    with EvidenceStore.open(database, create_new=True) as store:
        store.migrate()
        lock_path = tmp_path / "coordinator.lock"
        board = _board(fake, tmp_path, lock_assertion=lambda *_: lock.assert_held(), claim_create_attempt=store.begin_effect_attempt)
        action = _action(board); _reserve(store, action)
        assert _create(board, action).outcome == "unsupported"
        assert _create_calls(fake) == []
        with instance_lock(lock_path) as lock:
            assert _create(board, action).outcome == "verified"
        assert _create(board, action).outcome == "unsupported"
    assert len(_create_calls(fake)) == 1


def test_nonzero_no_effect_persists_unknown_and_restart_never_sends_second_create(tmp_path):
    fake = FakeKanban(); fake.add("anchor-1"); fake.create_returncode_without_effect = 1
    database = tmp_path / "evidence.sqlite3"
    lock_path = tmp_path / "coordinator.lock"
    with EvidenceStore.open(database, create_new=True) as store:
        store.migrate()
        with instance_lock(lock_path) as lock:
            board = _board(fake, tmp_path, lock_assertion=lambda *_: lock.assert_held(), claim_create_attempt=store.begin_effect_attempt)
            action = _action(board); _reserve(store, action)
            assert _create(board, action).outcome == "unknown"
    with EvidenceStore.open(database) as restarted:
        with instance_lock(lock_path) as lock:
            board = _board(fake, tmp_path, lock_assertion=lambda *_: lock.assert_held(), claim_create_attempt=restarted.begin_effect_attempt)
            assert _create(board, action).outcome == "unknown"
    assert len(_create_calls(fake)) == 1


def test_real_instance_lock_path_replacement_fails_closed_before_create(tmp_path):
    fake = FakeKanban(); fake.add("anchor-1")
    database = tmp_path / "evidence.sqlite3"
    lock_path = tmp_path / "coordinator.lock"
    with EvidenceStore.open(database, create_new=True) as store:
        store.migrate()
        with instance_lock(lock_path) as lock:
            checks = 0
            def assert_lock(*_args):
                nonlocal checks
                checks += 1
                if checks == 2:
                    replacement = tmp_path / "replacement.lock"
                    replacement.write_text("replacement")
                    replacement.chmod(0o600)
                    os.replace(replacement, lock_path)
                lock.assert_held()
            board = _board(fake, tmp_path, lock_assertion=assert_lock, claim_create_attempt=store.begin_effect_attempt)
            action = _action(board); _reserve(store, action)
            assert _create(board, action).outcome == "unsupported"
    assert _create_calls(fake) == []


def test_real_instance_lock_path_replacement_fails_closed_before_hold_mutation(tmp_path):
    fake = FakeKanban(); fake.add("anchor-1", status="ready")
    lock_path = tmp_path / "coordinator.lock"
    with instance_lock(lock_path) as lock:
        def assert_lock(*_args):
            replacement = tmp_path / "replacement.lock"
            replacement.write_text("replacement")
            replacement.chmod(0o600)
            os.replace(replacement, lock_path)
            lock.assert_held()
        board = _board(fake, tmp_path, lock_assertion=assert_lock, claim_create_attempt=lambda *_: None)
        hold = Action("hold-1", SCOPE, {"task_id": "anchor-1"}, "hold", board.read_task("anchor-1").digest)
        assert board.hold(hold, "anchor-1", "pause").outcome == "unsupported"
    assert not [call for call in fake.calls if call[4] == "block"]
