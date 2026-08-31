from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.decomposition import PlanValidator, activate_validated_plan
from local_first_orchestrator.generated_projection import (
    GeneratedProjectionDeliveryPolicy,
    GeneratedProjectionWorker,
)
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.ledger import Ledger
from tests.test_decomposition import Plans


_FAKE_HERMES = r'''#!/usr/bin/env python3
import json
import os
import sys

log = os.environ["GENERATED_PROJECTION_LOG"]
state_path = os.environ["GENERATED_PROJECTION_STATE"]
mode = os.environ.get("GENERATED_PROJECTION_MODE", "")
args = sys.argv[1:]
with open(log, "a") as stream:
    stream.write(json.dumps(args) + "\n")


def load():
    try:
        with open(state_path) as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {"next_id": 1, "tasks": {}, "keys": {}}


def save(value):
    with open(state_path, "w") as stream:
        json.dump(value, stream)


if args[:3] != ["kanban", "--board", "board"]:
    sys.exit(9)
command = args[3:]
data = load()
if command and command[0] == "create":
    if mode == "create_failure":
        sys.exit(5)
    if not (len(command) == 11 and command[2] == "--body" and command[4:6] == ["--workspace", "scratch"] and command[6] == "--idempotency-key" and command[8:] == ["--initial-status", "blocked", "--json"]):
        sys.exit(9)
    key = command[7]
    task = data["tasks"].get(data["keys"].get(key, ""))
    if task is None:
        task_id = str(data["next_id"])
        data["next_id"] += 1
        task = {"id": task_id, "title": command[1], "body": command[3], "status": "blocked", "workspace_path": None, "idempotency_key": key}
        data["tasks"][task_id] = task
        data["keys"][key] = task_id
        save(data)
    print(json.dumps(task))
elif len(command) == 3 and command[0] == "show" and command[2] == "--json":
    if mode == "show_failure":
        sys.exit(5)
    task = data["tasks"].get(command[1])
    if task is None:
        sys.exit(3)
    shown = dict(task)
    if mode == "shown_id_mismatch":
        shown["id"] = "other"
    elif mode == "marker_mismatch":
        shown["body"] = "not ours"
    elif mode.startswith("contract_mismatch:"):
        field, value = mode.split(":", 1)[1].split("=", 1)
        marker = "```local-first-contract\\n"
        prefix, encoded = shown["body"].split(marker, 1)
        raw, suffix = encoded.split("\\n```", 1)
        contract = json.loads(raw)
        contract[field] = value
        shown["body"] = prefix + marker + json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\\n```" + suffix
    print(json.dumps({"task": shown}))
else:
    sys.exit(9)
'''


class GeneratedProjectionDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = root / "ledger.db"
        self.ledger = Ledger(self.database)
        self.ledger.migrate()
        self.log = root / "fake.log"
        self.state = root / "fake.json"
        self.executable = root / "hermes"
        self.executable.write_text(_FAKE_HERMES)
        self.executable.chmod(stat.S_IRWXU)
        self.old_environment = {key: os.environ.get(key) for key in ("GENERATED_PROJECTION_LOG", "GENERATED_PROJECTION_STATE", "GENERATED_PROJECTION_MODE")}
        os.environ["GENERATED_PROJECTION_LOG"] = str(self.log)
        os.environ["GENERATED_PROJECTION_STATE"] = str(self.state)
        os.environ.pop("GENERATED_PROJECTION_MODE", None)

    def tearDown(self) -> None:
        self.ledger.close()
        for key, value in self.old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def activate_one(self) -> tuple[str, int]:
        plans = Plans()
        feature = plans.feature()
        base = plans.plan()
        active = base.tranches[0]
        single_active = type(active)(active.id, active.ordinal, active.objective, active.capabilities, active.criterion_ids, active.microtickets[:1])
        plan = plans.plan(tranches=(single_active, base.tranches[1]))
        validated = PlanValidator().validate(feature, plan)
        activate_validated_plan(self.ledger, feature, plan, validated, Plans().repository_validation(plan))
        row = self.ledger.connection.execute("SELECT ticket_id, event_id FROM board_projection_outbox WHERE operation='create_microticket' ORDER BY ticket_id LIMIT 1").fetchone()
        return str(row["ticket_id"]), int(row["event_id"])

    def worker(self, *, now: int = 100, worker_id: str = "worker-a", fault_injector=None) -> GeneratedProjectionWorker:
        adapter = HermesBoardAdapter(executable=str(self.executable), board="board", allow_writes=True, timeout_seconds=2)
        return GeneratedProjectionWorker(
            self.ledger,
            adapter,
            GeneratedProjectionDeliveryPolicy(lease_seconds=15, retry_delay=5),
            worker_id=worker_id,
            clock=lambda: now,
            fault_injector=fault_injector,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_delivers_canonical_projection_after_create_show_and_contract_verification(self) -> None:
        ticket_id, event_id = self.activate_one()
        row = self.ledger.connection.execute("SELECT idempotency_key FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()

        result = self.worker().deliver_one()

        self.assertEqual(result.status, "delivered")
        persisted = self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        self.assertEqual(persisted["external_task_id"], "1")
        self.assertIsNotNone(persisted["acknowledged_at"])
        self.assertEqual(self.ledger.get_ticket(ticket_id)["state"], "draft")
        with self.assertRaises(KeyError):
            self.ledger.runtime_binding(ticket_id)
        self.assertEqual(self.calls()[0][10], row["idempotency_key"])
        self.assertEqual([call[3] for call in self.calls()], ["create", "show"])

    def test_retryable_create_and_show_failures_release_the_lease_with_a_schedule(self) -> None:
        ticket_id, event_id = self.activate_one()
        os.environ["GENERATED_PROJECTION_MODE"] = "create_failure"
        result = self.worker().deliver_one()
        self.assertEqual(result.status, "retry_scheduled")
        row = self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        self.assertEqual((row["attempt_count"], row["next_attempt_at"], row["lease_owner"]), (1, 105, None))
        self.assertEqual(self.worker(now=104).deliver_one().status, "no_work")
        os.environ["GENERATED_PROJECTION_MODE"] = "show_failure"
        self.assertEqual(self.worker(now=105).deliver_one().status, "retry_scheduled")
        self.assertEqual(self.ledger.connection.execute("SELECT attempt_count FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()[0], 2)

    def test_deterministic_payload_and_contract_conflicts_are_terminal_before_adoption(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.ledger.connection.execute("UPDATE board_projection_outbox SET payload_json='{}' WHERE ticket_id=? AND event_id=?", (ticket_id, event_id))
        self.assertEqual(self.worker().deliver_one().status, "terminal_failed")
        self.assertEqual(self.calls(), [])
        row = self.ledger.connection.execute("SELECT terminal_error FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        self.assertIn("payload", row["terminal_error"])

    def test_payload_ticket_identity_mismatch_fails_before_the_create_process(self) -> None:
        ticket_id, event_id = self.activate_one()
        row = self.ledger.connection.execute("SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        payload = json.loads(row["payload_json"])
        payload["orchestrator_ticket_id"] = "other"
        self.ledger.connection.execute("UPDATE board_projection_outbox SET payload_json=? WHERE ticket_id=? AND event_id=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), ticket_id, event_id))
        self.assertEqual(self.worker().deliver_one().status, "terminal_failed")
        self.assertEqual(self.calls(), [])

    def test_payload_feature_and_tranche_mismatches_fail_before_hermes_create(self) -> None:
        ticket_id, event_id = self.activate_one()
        original = self.ledger.connection.execute("SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()["payload_json"]
        for field in ("feature_id", "tranche_id"):
            with self.subTest(field=field):
                self.log.unlink(missing_ok=True)
                payload = json.loads(original)
                payload[field] = "tampered"
                self.ledger.connection.execute("UPDATE board_projection_outbox SET payload_json=? WHERE ticket_id=? AND event_id=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), ticket_id, event_id))
                self.assertEqual(self.worker().deliver_one().status, "terminal_failed")
                self.assertEqual(self.calls(), [])
                self.ledger.connection.execute("UPDATE board_projection_outbox SET payload_json=?, terminal_error=NULL, last_error=NULL WHERE ticket_id=? AND event_id=?", (original, ticket_id, event_id))

    def test_tampered_durable_projection_key_and_missing_ticket_relationship_fail_before_hermes_create(self) -> None:
        ticket_id, event_id = self.activate_one()
        original = self.ledger.connection.execute("SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()["payload_json"]
        payload = json.loads(original)
        payload["projection_key"] = "tampered-key"
        self.ledger.connection.execute("UPDATE board_projection_outbox SET idempotency_key=?, payload_json=? WHERE ticket_id=? AND event_id=?", ("tampered-key", json.dumps(payload, sort_keys=True, separators=(",", ":")), ticket_id, event_id))
        self.assertEqual(self.worker().deliver_one().status, "terminal_failed")
        self.assertEqual(self.calls(), [])
        self.ledger.connection.execute("UPDATE board_projection_outbox SET terminal_error=NULL, last_error=NULL, idempotency_key=?, payload_json=? WHERE ticket_id=? AND event_id=?", ("board-create:v1:" + ticket_id, original, ticket_id, event_id))
        self.ledger.connection.execute("UPDATE tickets SET feature_id=NULL WHERE id=?", (ticket_id,))
        self.assertEqual(self.worker().deliver_one().status, "terminal_failed")
        self.assertEqual(self.calls(), [])

    def test_shown_marker_and_each_required_provenance_identity_mismatch_are_terminal(self) -> None:
        for mode in ("shown_id_mismatch", "marker_mismatch", "contract_mismatch:orchestrator_ticket_id=other", "contract_mismatch:feature_id=other", "contract_mismatch:tranche_id=other", "contract_mismatch:projection_key=other"):
            with self.subTest(mode=mode):
                ticket_id, event_id = self.activate_one()
                os.environ["GENERATED_PROJECTION_MODE"] = mode
                self.assertEqual(self.worker().deliver_one().status, "terminal_failed")
                row = self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
                self.assertIsNone(row["external_task_id"])
                self.assertIsNone(row["acknowledged_at"])
                self.assertIsNotNone(row["terminal_error"])
                # Reuse the same durable intent only after clearing test-only terminal state.
                self.ledger.connection.execute("UPDATE board_projection_outbox SET terminal_error=NULL, last_error=NULL WHERE ticket_id=? AND event_id=?", (ticket_id, event_id))

    def test_crash_after_create_replays_the_same_idempotency_key_and_completes_one_row(self) -> None:
        ticket_id, event_id = self.activate_one()
        def crash(stage: str) -> None:
            if stage == "after_create":
                from local_first_orchestrator.generated_projection import GeneratedProjectionCrash
                raise GeneratedProjectionCrash()
        with self.assertRaises(RuntimeError):
            self.worker(fault_injector=crash).deliver_one()
        self.assertEqual([call[3] for call in self.calls()], ["create"])
        self.ledger.close()
        self.ledger = Ledger(self.database)
        self.ledger.migrate()
        self.assertEqual(self.worker(now=114, worker_id="worker-b").deliver_one().status, "no_work")
        result = self.worker(now=115, worker_id="worker-b").deliver_one()
        self.assertEqual((result.status, result.external_task_id), ("delivered", "1"))
        state = json.loads(self.state.read_text())
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()[0], 1)

    def test_stale_owner_is_rejected_after_reclaim_and_completed_projection_never_recreates(self) -> None:
        ticket_id, event_id = self.activate_one()
        claim_a = self.ledger.claim_next_generated_create_projection("worker-a", lease_seconds=10, now=100)
        claim_b = self.ledger.claim_next_generated_create_projection("worker-b", lease_seconds=10, now=110)
        self.assertEqual((claim_a["ticket_id"], claim_b["ticket_id"]), (ticket_id, ticket_id))
        with self.assertRaises(PermissionError):
            self.ledger.retry_generated_create_projection(ticket_id, event_id, "worker-a", error="stale", next_attempt_at=200, now=110)
        with self.assertRaises(PermissionError):
            self.ledger.complete_generated_create_projection(ticket_id, event_id, "worker-a", "wrong", now=110)
        self.ledger.complete_generated_create_projection(ticket_id, event_id, "worker-b", "verified", now=110)
        self.assertEqual(self.worker(now=111).deliver_one().status, "no_work")
        self.assertEqual(self.calls(), [])

    def test_policy_rejects_a_lease_shorter_than_the_bounded_create_show_horizon(self) -> None:
        adapter = HermesBoardAdapter(executable=str(self.executable), board="board", allow_writes=True, timeout_seconds=2)
        with self.assertRaisesRegex(ValueError, "horizon"):
            GeneratedProjectionWorker(self.ledger, adapter, GeneratedProjectionDeliveryPolicy(lease_seconds=13), worker_id="worker-a")


if __name__ == "__main__":
    unittest.main()
