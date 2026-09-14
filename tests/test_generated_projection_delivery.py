from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from local_first_orchestrator.decomposition import PlanValidator, create_and_activate_validated_plan
from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.generated_activation import resolve_generated_activation_context
from local_first_orchestrator.generated_projection import (
    GeneratedProjectionDeliveryPolicy,
    GeneratedProjectionWorker,
)
from local_first_orchestrator.hermes_board import ExternalExecutionRun, ExternalExecutionSnapshot, ExternalTicket, HermesBoardAdapter
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
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
    if not (len(command) == 11 and command[2] == "--body" and command[4:6] == ["--workspace", "worktree"] and command[6] == "--idempotency-key" and command[8:] == ["--initial-status", "blocked", "--json"]):
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
elif command and command[0] in {"schedule", "unblock", "block", "complete"}:
    if len(command) < 2 or command[1] not in data["tasks"]:
        sys.exit(3)
    print("ok")
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
        create_and_activate_validated_plan(self.ledger, feature, plan, validated, Plans().repository_validation(plan))
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
        plan = self.ledger.connection.execute("SELECT repository_identity, repo_base_sha, repo_snapshot_hash FROM decomposition_plans WHERE feature_id='F' AND status='active'").fetchone()

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
        body = json.loads(self.state.read_text())["tasks"]["1"]["body"]
        contract = json.loads(body.split("```local-first-contract\\n", 1)[1].split("\\n```", 1)[0])
        self.assertEqual(contract["repository_identity"], plan["repository_identity"])
        self.assertEqual(contract["repo_base_sha"], plan["repo_base_sha"])
        self.assertEqual(contract["repo_snapshot_hash"], plan["repo_snapshot_hash"])

    def test_operator_can_reopen_terminal_precreate_failure_after_payload_is_valid_again(self) -> None:
        ticket_id, event_id = self.activate_one()
        original = self.ledger.connection.execute(
            "SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket_id,event_id),
        ).fetchone()["payload_json"]
        self.ledger.connection.execute(
            "UPDATE board_projection_outbox SET payload_json='{}' WHERE ticket_id=? AND event_id=?",
            (ticket_id,event_id),
        )

        failed = self.worker().deliver_one()

        self.assertEqual(failed.status, "terminal_failed")
        self.assertEqual(self.calls(), [])
        terminal = self.ledger.connection.execute(
            "SELECT terminal_error,external_task_id,acknowledged_at FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket_id,event_id),
        ).fetchone()
        self.assertIsNotNone(terminal["terminal_error"])
        self.assertIsNone(terminal["external_task_id"])
        self.assertIsNone(terminal["acknowledged_at"])

        self.ledger.connection.execute(
            "UPDATE board_projection_outbox SET payload_json=? WHERE ticket_id=? AND event_id=?",
            (original,ticket_id,event_id),
        )
        reopened = self.ledger.reopen_terminal_generated_projection(ticket_id,event_id)
        self.assertIsNone(reopened["terminal_error"])
        self.assertIsNone(reopened["last_error"])

        delivered = self.worker(now=101).deliver_one()
        self.assertEqual(delivered.status, "delivered")
        self.assertEqual(delivered.external_task_id, "1")

    def test_missing_or_mismatched_projected_provenance_fails_before_hermes_create(self) -> None:
        ticket_id, event_id = self.activate_one()
        original = self.ledger.connection.execute("SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()["payload_json"]
        cases = (("repository_identity", None), ("repo_base_sha", None), ("repo_snapshot_hash", None), ("repository_identity", "wrong-repository"), ("repo_base_sha", "wrong-base"), ("repo_snapshot_hash", "wrong-snapshot"))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.log.unlink(missing_ok=True)
                payload = json.loads(original)
                prefix, encoded = payload["body"].split("```local-first-contract\\n", 1)
                raw, suffix = encoded.split("\\n```", 1)
                contract = json.loads(raw)
                if value is None:
                    contract.pop(field, None)
                else:
                    contract[field] = value
                payload["body"] = prefix + "```local-first-contract\\n" + json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\\n```" + suffix
                self.ledger.connection.execute("UPDATE board_projection_outbox SET payload_json=? WHERE ticket_id=? AND event_id=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), ticket_id, event_id))
                self.assertEqual(self.worker().deliver_one().status, "terminal_failed")
                self.assertEqual(self.calls(), [])
                self.ledger.connection.execute("UPDATE board_projection_outbox SET payload_json=?, terminal_error=NULL, last_error=NULL, acknowledged_at=NULL, external_task_id=NULL, lease_owner=NULL, lease_expires_at=NULL WHERE ticket_id=? AND event_id=?", (original, ticket_id, event_id))

    def test_reconciles_legacy_unacknowledged_projection_from_persisted_provenance_without_board_io(self) -> None:
        ticket_id, event_id = self.activate_one()
        row = self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        legacy = json.loads(row["payload_json"])
        prefix, encoded = legacy["body"].split("```local-first-contract\\n", 1)
        raw, suffix = encoded.split("\\n```", 1)
        contract = json.loads(raw)
        for field in ("repository_identity", "repo_base_sha", "repo_snapshot_hash"):
            contract.pop(field, None)
        legacy["body"] = prefix + "```local-first-contract\\n" + json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\\n```" + suffix
        self.ledger.connection.execute("UPDATE board_projection_outbox SET payload_json=? WHERE ticket_id=? AND event_id=?", (json.dumps(legacy, sort_keys=True, separators=(",", ":")), ticket_id, event_id))
        before = self.ledger.connection.execute("SELECT event_id,operation,idempotency_key,acknowledged_at,external_task_id,attempt_count,lease_owner,lease_expires_at FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        plan = self.ledger.connection.execute("SELECT repository_identity,repo_base_sha,repo_snapshot_hash FROM decomposition_plans WHERE feature_id='F' AND status='active'").fetchone()

        reconciled = self.ledger.reconcile_generated_projection(ticket_id, event_id)

        after = self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        body = json.loads(after["payload_json"])["body"]
        repaired = json.loads(body.split("```local-first-contract\\n", 1)[1].split("\\n```", 1)[0])
        self.assertEqual(reconciled["orchestrator_ticket_id"], ticket_id)
        self.assertEqual((after["event_id"], after["operation"], after["idempotency_key"], after["acknowledged_at"], after["external_task_id"], after["attempt_count"], after["lease_owner"], after["lease_expires_at"]), tuple(before))
        self.assertEqual(repaired["repository_identity"], plan["repository_identity"])
        self.assertEqual(repaired["repo_base_sha"], plan["repo_base_sha"])
        self.assertEqual(repaired["repo_snapshot_hash"], plan["repo_snapshot_hash"])
        self.assertEqual(self.calls(), [])

    def test_reconciliation_refuses_unsafe_or_unqualifiable_rows_without_rewriting_body(self) -> None:
        ticket_id, event_id = self.activate_one()
        original = self.ledger.connection.execute("SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()["payload_json"]
        cases = (
            ("acknowledged", "UPDATE board_projection_outbox SET acknowledged_at=1 WHERE ticket_id=? AND event_id=?"),
            ("external_task_id", "UPDATE board_projection_outbox SET external_task_id='existing' WHERE ticket_id=? AND event_id=?"),
            ("operation", "UPDATE board_projection_outbox SET operation='other' WHERE ticket_id=? AND event_id=?"),
            ("active_ticket", "UPDATE tickets SET state='implementing' WHERE id=?"),
            ("missing_plan_provenance", "UPDATE decomposition_plans SET repo_base_sha=NULL WHERE feature_id='F' AND status='active'"),
        )
        for name, statement in cases:
            with self.subTest(name=name):
                if statement.count('?') == 2:
                    self.ledger.connection.execute(statement, (ticket_id, event_id))
                elif statement.count('?') == 1:
                    self.ledger.connection.execute(statement, (ticket_id,))
                else:
                    self.ledger.connection.execute(statement)
                with self.assertRaises(ValueError):
                    self.ledger.reconcile_generated_projection(ticket_id, event_id)
                self.assertEqual(self.ledger.connection.execute("SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()["payload_json"], original)
                self.assertEqual(self.calls(), [])
                self.ledger.connection.execute("UPDATE board_projection_outbox SET acknowledged_at=NULL, external_task_id=NULL, operation='create_microticket' WHERE ticket_id=? AND event_id=?", (ticket_id, event_id))
                self.ledger.connection.execute("UPDATE tickets SET state='draft' WHERE id=?", (ticket_id,))
                self.ledger.connection.execute("UPDATE decomposition_plans SET repo_base_sha='a' WHERE feature_id='F' AND status='active'")

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

    def test_generated_external_identity_missing_or_conflicting_fails_closed(self) -> None:
        ticket_id, _ = self.activate_one()
        with self.assertRaisesRegex(ValueError, "external_projection_identity_missing"):
            self.ledger.resolve_external_task_id(ticket_id)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        with self.ledger._transaction() as conn:
            event_id = self.ledger._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="generated_microticket_created", actor_id="test")
            conn.execute("INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation,external_task_id,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,?)", (ticket_id,event_id,"draft","{}",f"board-create:v1:{ticket_id}:conflict",1,"create_microticket","conflicting-task",1))
        with self.assertRaisesRegex(ValueError, "external_projection_identity_conflict"):
            self.ledger.resolve_external_task_id(ticket_id)

    def test_paused_operator_recovery_supersedes_acknowledged_projection_without_changing_ticket_authority(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")

        recovered = self.ledger.recover_generated_projection(
            ticket_id=ticket_id,
            superseded_event_id=event_id,
            superseded_external_task_id="1",
            observed_status="done",
            observed_snapshot_hash="a" * 64,
            operator_id="casey",
            reason="Hermes completed before Local First reconciliation",
        )

        old = self.ledger.connection.execute(
            "SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket_id, event_id),
        ).fetchone()
        replacement = self.ledger.connection.execute(
            "SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket_id, recovered["replacement_event_id"]),
        ).fetchone()
        audit = self.ledger.connection.execute(
            "SELECT * FROM generated_projection_recoveries WHERE recovery_id=?",
            (recovered["recovery_id"],),
        ).fetchone()
        self.assertEqual(old["external_task_id"], "1")
        self.assertIsNotNone(old["acknowledged_at"])
        self.assertIsNotNone(old["superseded_at"])
        self.assertEqual(old["superseded_by_event_id"], recovered["replacement_event_id"])
        self.assertEqual(replacement["operation"], "create_microticket")
        self.assertIsNone(replacement["external_task_id"])
        self.assertIsNone(replacement["acknowledged_at"])
        self.assertEqual(replacement["idempotency_key"], recovered["replacement_idempotency_key"])
        self.assertTrue(replacement["idempotency_key"].startswith(f"board-create:v2:{ticket_id}:"))
        self.assertEqual(audit["superseded_external_task_id"], "1")
        self.assertEqual(audit["observed_status"], "done")
        self.assertEqual(self.ledger.get_ticket(ticket_id)["state"], "draft")
        self.assertEqual(self.ledger.attempt_count(ticket_id), 0)

    def test_recovered_projection_delivers_as_the_only_current_external_identity(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")
        recovered = self.ledger.recover_generated_projection(
            ticket_id=ticket_id,
            superseded_event_id=event_id,
            superseded_external_task_id="1",
            observed_status="done",
            observed_snapshot_hash="b" * 64,
            operator_id="casey",
            reason="replace bypassed projection",
        )

        delivered = self.worker(now=200).deliver_one()

        self.assertEqual(delivered.status, "delivered")
        self.assertEqual(delivered.external_task_id, "2")
        self.assertEqual(self.ledger.resolve_external_task_id(ticket_id), "2")
        current = self.ledger.connection.execute(
            "SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket_id, recovered["replacement_event_id"]),
        ).fetchone()
        self.assertEqual(current["external_task_id"], "2")
        self.assertIsNotNone(current["acknowledged_at"])
        body = json.loads(self.state.read_text())["tasks"]["2"]["body"]
        contract = json.loads(body.split("```local-first-contract\\n", 1)[1].split("\\n```", 1)[0])
        self.assertEqual(contract["projection_generation"], "recovery-v2")
        self.assertEqual(contract["projection_key"], recovered["replacement_idempotency_key"])
        self.assertIn("local-first-awaiting-reconciliation", body)

    def test_recovered_projection_is_the_only_generated_activation_identity(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")
        self.ledger.recover_generated_projection(
            ticket_id=ticket_id, superseded_event_id=event_id,
            superseded_external_task_id="1", observed_status="done",
            observed_snapshot_hash="e" * 64, operator_id="casey", reason="replace",
        )
        self.assertEqual(self.worker(now=200).deliver_one().external_task_id, "2")
        self.assertEqual(
            [(row["ticket_id"], row["external_task_id"]) for row in self.ledger.generated_activation_candidates()],
            [(ticket_id, "2")],
        )
        root = Path(self.temp.name)
        runtime = RuntimeConfig(root, root / "worktrees", root / "artifacts", (root,))
        completed = type("Completed", (), {"stdout": "a\n"})()
        with patch.object(RuntimeConfig, "canonical_repository", return_value=Path("fixture-repo")), \
             patch("local_first_orchestrator.generated_activation.subprocess.run", return_value=completed):
            context = resolve_generated_activation_context(ticket_id, runtime, self.ledger)
        self.assertEqual(context.external_task_id, "2")

    def test_recovery_replay_is_idempotent_and_conflicting_operator_evidence_fails_closed(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")
        request = dict(
            ticket_id=ticket_id,
            superseded_event_id=event_id,
            superseded_external_task_id="1",
            observed_status="done",
            observed_snapshot_hash="c" * 64,
            operator_id="casey",
            reason="replace bypassed projection",
        )
        first = self.ledger.recover_generated_projection(**request)
        second = self.ledger.recover_generated_projection(**request)
        self.assertEqual(second, first)
        self.assertEqual(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM generated_projection_recoveries WHERE ticket_id=?", (ticket_id,)
        ).fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "recovery replay conflicts"):
            self.ledger.recover_generated_projection(**{**request, "reason": "different reason"})
        self.assertEqual(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM generated_projection_recoveries WHERE ticket_id=?", (ticket_id,)
        ).fetchone()[0], 1)

    def test_controller_reads_exact_external_snapshot_before_recording_recovery(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")

        class ReadOnlyExecutionBoard:
            is_fake = False
            def __init__(self) -> None:
                self.calls: list[str] = []
            def execution_snapshot(self, external_task_id: str) -> ExternalExecutionSnapshot:
                self.calls.append(external_task_id)
                return ExternalExecutionSnapshot(
                    task=ExternalTicket(external_task_id, ticket_id, "legacy", "done", None),
                    session_id="legacy-session", branch_name="legacy-branch",
                    started_at=10, completed_at=20, runs=(),
                )

        board = ReadOnlyExecutionBoard()
        root = Path(self.temp.name)
        controller = LocalFirstController(
            self.ledger, board, RuntimeConfig(root, root / "worktrees", root / "artifacts", (root,))
        )
        recovered = controller.recover_generated_projection(
            ticket_id, event_id, operator_id="casey", reason="replace bypassed projection"
        )
        replay = controller.recover_generated_projection(
            ticket_id, event_id, operator_id="casey", reason="replace bypassed projection"
        )
        self.assertEqual(replay, recovered)
        audit = self.ledger.connection.execute(
            "SELECT * FROM generated_projection_recoveries WHERE recovery_id=?", (recovered["recovery_id"],)
        ).fetchone()
        self.assertEqual(board.calls, ["1", "1"])
        self.assertEqual(audit["observed_status"], "done")
        self.assertEqual(len(audit["observed_snapshot_hash"]), 64)

    def test_recovery_refuses_existing_model_stage_authority_without_mutation(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")
        self.ledger.connection.execute(
            "INSERT INTO model_stage_artifacts(ticket_id,attempt_number,stage,purpose,adapter,request_hash,response_artifact,worktree_path,base_sha,diff_hash,completed_at,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, 1, "implementation", "test", "test", "request", "artifact", "/tmp/worktree", "base", "diff", 1, "completed"),
        )

        with self.assertRaisesRegex(ValueError, "existing lifecycle authority"):
            self.ledger.recover_generated_projection(
                ticket_id=ticket_id, superseded_event_id=event_id,
                superseded_external_task_id="1", observed_status="done",
                observed_snapshot_hash="d" * 64, operator_id="casey", reason="replace",
            )

        old = self.ledger.connection.execute(
            "SELECT superseded_at FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket_id, event_id),
        ).fetchone()
        self.assertIsNone(old["superseded_at"])
        self.assertEqual(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM generated_projection_recoveries WHERE ticket_id=?", (ticket_id,)
        ).fetchone()[0], 0)

    def test_controller_refuses_blocked_snapshot_with_any_execution_evidence(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")

        class ExecutedBoard:
            is_fake = False
            def execution_snapshot(self, external_task_id: str) -> ExternalExecutionSnapshot:
                return ExternalExecutionSnapshot(
                    task=ExternalTicket(external_task_id, ticket_id, "legacy", "blocked", None),
                    session_id="executed-session", branch_name="executed-branch",
                    started_at=None, completed_at=20,
                    runs=(ExternalExecutionRun(7, "completed", "completed", None, 20, "finished", None, None),),
                )

        root = Path(self.temp.name)
        controller = LocalFirstController(
            self.ledger, ExecutedBoard(), RuntimeConfig(root, root / "worktrees", root / "artifacts", (root,))
        )
        with self.assertRaisesRegex(RuntimeError, "inert blocked"):
            controller.recover_generated_projection(
                ticket_id, event_id, operator_id="casey", reason="replace bypassed projection"
            )
        self.assertEqual(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM generated_projection_recoveries WHERE ticket_id=?", (ticket_id,)
        ).fetchone()[0], 0)

    def test_controller_accepts_only_the_exact_inert_local_first_safety_gate_shape(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")

        class SafetyGateBoard:
            is_fake = False
            def execution_snapshot(self, external_task_id: str) -> ExternalExecutionSnapshot:
                return ExternalExecutionSnapshot(
                    task=ExternalTicket(external_task_id, ticket_id, "legacy", "blocked", None),
                    session_id=None, branch_name=None, started_at=None, completed_at=None,
                    runs=(ExternalExecutionRun(
                        7, "blocked", "blocked", 100, 100,
                        "Local First execution gate: authoritative dependencies/runtime authorization not satisfied",
                        None, None, None,
                    ),),
                )

        root = Path(self.temp.name)
        controller = LocalFirstController(
            self.ledger, SafetyGateBoard(), RuntimeConfig(root, root / "worktrees", root / "artifacts", (root,))
        )
        recovered = controller.recover_generated_projection(
            ticket_id, event_id, operator_id="casey", reason="replace pre-native safety-gated projection"
        )
        self.assertEqual(recovered["observed_status"], "blocked")

    def test_controller_refuses_blocked_snapshot_without_the_exact_safety_gate_run(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")

        class MissingGateBoard:
            is_fake = False
            def execution_snapshot(self, external_task_id: str) -> ExternalExecutionSnapshot:
                return ExternalExecutionSnapshot(
                    task=ExternalTicket(external_task_id, ticket_id, "legacy", "blocked", None),
                    session_id=None, branch_name=None, started_at=None, completed_at=None, runs=(),
                )

        root = Path(self.temp.name)
        controller = LocalFirstController(
            self.ledger, MissingGateBoard(), RuntimeConfig(root, root / "worktrees", root / "artifacts", (root,))
        )
        with self.assertRaisesRegex(RuntimeError, "inert blocked"):
            controller.recover_generated_projection(
                ticket_id, event_id, operator_id="casey", reason="replace pre-native safety-gated projection"
            )
        self.assertEqual(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM generated_projection_recoveries WHERE ticket_id=?", (ticket_id,)
        ).fetchone()[0], 0)

    def test_recovery_refuses_multiple_current_create_identities_without_mutation(self) -> None:
        ticket_id, event_id = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        self.ledger.pause("operator", reason="pre-native recovery")
        with self.ledger._transaction() as conn:
            conflicting_event = self.ledger._append_event(
                conn, entity_type="ticket", entity_id=ticket_id,
                event_type="generated_microticket_created", actor_id="test",
            )
            conn.execute(
                "INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation,external_task_id,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket_id, conflicting_event, "draft", "{}", "conflicting-current", 1, "create_microticket", "other", 1),
            )
        with self.assertRaisesRegex(ValueError, "current create identity"):
            self.ledger.recover_generated_projection(
                ticket_id=ticket_id, superseded_event_id=event_id,
                superseded_external_task_id="1", observed_status="done",
                observed_snapshot_hash="f" * 64, operator_id="casey", reason="replace",
            )
        self.assertEqual(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM generated_projection_recoveries WHERE ticket_id=?", (ticket_id,)
        ).fetchone()[0], 0)

    def test_state_projection_uses_acknowledged_external_task_id_not_internal_ticket_id(self) -> None:
        ticket_id, _ = self.activate_one()
        self.assertEqual(self.worker().deliver_one().external_task_id, "1")
        adapter = HermesBoardAdapter(executable=str(self.executable), board="board", allow_writes=True, timeout_seconds=2)
        evidence = self.ledger.enqueue_evidence_comment(ticket_id, 99, "evidence")
        self.assertEqual(evidence["external_task_id"], "1")
        self.ledger.connection.execute("UPDATE evidence_comment_outbox SET external_task_id=? WHERE operation_id=?", (ticket_id, evidence["operation_id"]))
        self.assertTrue(self.ledger.claim_comment(evidence["operation_id"], "replay-worker"))
        self.assertEqual(self.ledger.resolve_claimed_comment_target(evidence["operation_id"], "replay-worker"), "1")
        self.ledger.transition(ticket_id, CanonicalState.READY_LOCAL)
        ready_event = self.ledger.connection.execute("SELECT id FROM events WHERE entity_id=? AND to_state='ready_local' ORDER BY id DESC LIMIT 1", (ticket_id,)).fetchone()["id"]
        self.ledger.connection.execute("UPDATE board_projection_outbox SET external_task_id=NULL WHERE ticket_id=? AND event_id=?", (ticket_id, ready_event))
        self.assertTrue(self.ledger.project_ticket(ticket_id, adapter))
        self.ledger.connection.execute("UPDATE board_projection_outbox SET external_task_id=NULL WHERE ticket_id=? AND event_id=?", (ticket_id, ready_event))
        self.assertFalse(self.ledger.project_ticket(ticket_id, adapter))
        self.assertIsNone(self.ledger.connection.execute("SELECT external_task_id FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, ready_event)).fetchone()["external_task_id"])
        self.ledger.transition(ticket_id, CanonicalState.BLOCKED)
        self.assertTrue(self.ledger.project_ticket(ticket_id, adapter))
        calls = self.calls()
        self.assertEqual([call[3] for call in calls], ["create", "show", "show", "unblock", "show"])
        external_targets = [calls[index][4] for index in (2, 3, 4)]
        self.assertEqual(external_targets, ["1", "1", "1"])
        self.assertNotIn(ticket_id, external_targets)
        rows = self.ledger.connection.execute("SELECT external_task_id FROM board_projection_outbox WHERE ticket_id=? AND operation='set_state' ORDER BY event_id", (ticket_id,)).fetchall()
        self.assertEqual([row["external_task_id"] for row in rows], [None, "1"])
        self.assertIsNone(self.ledger.get_ticket(ticket_id)["external_id"])

    def test_policy_rejects_a_lease_shorter_than_the_bounded_create_show_horizon(self) -> None:
        adapter = HermesBoardAdapter(executable=str(self.executable), board="board", allow_writes=True, timeout_seconds=2)
        with self.assertRaisesRegex(ValueError, "horizon"):
            GeneratedProjectionWorker(self.ledger, adapter, GeneratedProjectionDeliveryPolicy(lease_seconds=13), worker_id="worker-a")


if __name__ == "__main__":
    unittest.main()
