from __future__ import annotations

import json
import sqlite3
import subprocess
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.generated_projection import GeneratedProjectionWorker
from local_first_orchestrator.hermes_board import ExternalTicket
from local_first_orchestrator.ledger import Ledger, _stable_scheduler_failure_fingerprint
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.triage import LocalTriagePlanner


def contract(*, new_test_files: list[str] | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "objective": "Make the fixture value pass.", "criterion_ids": ["AC-1"],
        "primary_symbol": "app.py::value", "allowed_files": ["app.py"],
        "forbidden_changes": ["No API change."], "patch_budget": {"max_files": 2, "max_changed_lines": 80},
        "verification": {"commands": [["python", "-c", "from app import value; assert value() == 'ok'"]]},
        "risk": "low", "review_required": True, "max_attempts": 2,
    }
    if new_test_files:
        value["new_test_files"] = new_test_files
    return value


class Board:
    is_fake = False
    allow_writes = True
    writes_enabled = True
    timeout_seconds = 1
    def set_state(self, ticket_id: str, state: object, *, idempotency_key: str) -> None: pass
    def find_comment_marker(self, ticket_id: str, marker: str) -> str: return "not_found"
    def deliver_comment(self, ticket_id: str, comment: str, *, idempotency_key: str) -> None: pass


class ProjectionBoard(Board):
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self._body_by_id: dict[str, str] = {}

    def create_microticket(self, title: str, body: str, *, idempotency_key: str) -> str:
        task_id = f"external-{len(self.created) + 1}"
        self.created.append((title, body, idempotency_key))
        self._body_by_id[task_id] = body
        return task_id

    def get_task(self, task_id: str):
        return SimpleNamespace(id=task_id, body=self._body_by_id[task_id])


class LifecycleModel:
    provider = "fixture-provider"
    model = "fixture-model"
    def __init__(self, mode: str = "completed") -> None:
        self.mode, self.calls = mode, 0
    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
        self.calls += 1
        if purpose == "implementation":
            assert workdir is not None
            if self.mode == "timeout":
                raise subprocess.TimeoutExpired(("fixture",), 3)
            if self.mode == "error":
                raise RuntimeError("launch failed")
            (workdir / "app.py").write_text("def value():\n    return 'ok'\n", encoding="utf-8")
        artifact = artifact_dir / f"{purpose}-result.json"
        payload = {} if purpose == "implementation" else {"verdict":"pass","criterion_results":[{"criterion_id":"AC-1","status":"pass","evidence":"ok"}],"findings":[],"suggestions":[]}
        if purpose == "review":
            artifact.write_text(json.dumps({"provider":self.provider,"model":self.model,"payload":payload},sort_keys=True), encoding="utf-8")
        else:
            artifact.write_text("{}", encoding="utf-8")
        return type("Result", (), {"payload": payload, "artifact_path": artifact})()


class InvalidLifecycleModel(LifecycleModel):
    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
        self.calls += 1
        if purpose == "implementation":
            assert workdir is not None
            (workdir / "app.py").write_text("def value():\n    return 'still-bad'\n", encoding="utf-8")
        artifact = artifact_dir / f"{purpose}-result.json"
        artifact.write_text("{}", encoding="utf-8")
        return type("Result", (), {"payload": {}, "artifact_path": artifact})()


class SequencedLifecycleModel(LifecycleModel):
    def __init__(self, implementation_values: list[str], review_payload: dict[str, object] | None = None) -> None:
        super().__init__()
        self.implementation_values = implementation_values
        self.review_payload = review_payload
        self.implementation_calls = 0
        self.implementation_packets: list[str] = []

    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
        self.calls += 1
        if purpose == "implementation":
            assert workdir is not None
            self.implementation_packets.append(packet)
            index = min(self.implementation_calls, len(self.implementation_values) - 1)
            value = self.implementation_values[index]
            self.implementation_calls += 1
            (workdir / "app.py").write_text(f"def value():\n    return {value!r}\n", encoding="utf-8")
            payload: dict[str, object] = {}
        else:
            payload = self.review_payload or {"verdict":"pass","criterion_results":[{"criterion_id":"AC-1","status":"pass","evidence":"ok"}],"findings":[],"suggestions":[]}
        artifact = artifact_dir / f"{purpose}-result.json"
        if purpose == "review":
            artifact.write_text(json.dumps({"provider":self.provider,"model":self.model,"payload":payload},sort_keys=True), encoding="utf-8")
        else:
            artifact.write_text("{}", encoding="utf-8")
        return type("Result", (), {"payload": payload, "artifact_path": artifact})()


class InvocationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(); self.root = Path(self.temp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        self.git("init", "-q", "-b", "main"); self.git("config", "user.email", "t@example.invalid"); self.git("config", "user.name", "Test")
        (self.repo / "app.py").write_text("def value():\n    return 'bad'\n", encoding="utf-8"); self.git("add", "."); self.git("commit", "-qm", "base")
        self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate()
        self.config = RuntimeConfig(self.repo, self.root / "external-worktrees", self.root / "external-artifacts", (self.repo,), implementation_timeout_seconds=17)
    def tearDown(self) -> None:
        self.ledger.close(); self.temp.cleanup()
    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", *args), cwd=self.repo, check=True, capture_output=True, text=True)
    def controller(self, model: LifecycleModel, crash: str | None = None) -> tuple[LocalFirstController, str]:
        card = ExternalTicket("card", "fixture", "<!-- local-first-orchestrator -->\n```local-first-contract\n" + json.dumps(contract()) + "\n```", "scheduled", str(self.repo))
        ticket = LocalFirstController(self.ledger, Board(), self.config, local_model=model).import_card(card)
        def fault(stage: str) -> None:
            if stage == crash: raise RuntimeError("simulated controller death")
        return LocalFirstController(self.ledger, Board(), self.config, local_model=model, fault_injector=fault), ticket
    def invocation(self, ticket: str) -> dict[str, object]:
        row = self.ledger.connection.execute("SELECT * FROM model_invocations WHERE ticket_id=?", (ticket,)).fetchone(); assert row is not None; return dict(row)
    def run_until_stage(self, scheduler: ProcessNextScheduler, stage: str, *, limit: int = 10):
        for _ in range(limit):
            result = scheduler.process_next()
            if result.stage == stage:
                return result
        self.fail(f"scheduler did not reach stage {stage!r}")

    def prepare_scheduler_accepted(self, *, attach_tranche: bool = False) -> tuple[LocalFirstController, str, SequencedLifecycleModel]:
        model = SequencedLifecycleModel(["ok"])
        ctl, ticket = self.controller(model)
        if attach_tranche:
            base = self.git("rev-parse", "HEAD").stdout.strip()
            self.ledger.connection.execute("INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('fixture-feature','fixture','active',0,0)")
            self.ledger.connection.execute("INSERT INTO tranches(id,feature_id,ordinal,status,base_sha) VALUES ('fixture-tranche','fixture-feature',0,'active',?)", (base,))
            self.ledger.connection.execute("UPDATE tickets SET feature_id='fixture-feature',tranche_id='fixture-tranche' WHERE id=?", (ticket,))
            self.git("update-ref", "refs/local-first/tranches/fixture-tranche/integration-head", base)
        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler",
            lease_seconds=30,
            clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
            review_runner=lambda value: ctl.execute_fresh_review_only(value, repository=self.repo),
            review_execution_policy_hash=ctl.review_execution_policy_hash(),
            acceptance_runner=lambda value: ctl.inspect_acceptance_candidate_only(value, repository=self.repo),
        )
        self.assertEqual(scheduler.process_next().stage, "implementation")
        self.assertEqual(scheduler.process_next().stage, "validation")
        self.run_until_stage(scheduler, "review")
        self.run_until_stage(scheduler, "repair_routing")
        self.run_until_stage(scheduler, "acceptance")
        for _ in range(6):
            result = scheduler.process_next()
            if result.stage is None:
                break
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], CanonicalState.ACCEPTED.value)
        self.assertIsNotNone(self.ledger.accepted_candidate(ticket))
        return ctl, ticket, model

    def triage_parent(self) -> tuple[LocalFirstController, str]:
        ctl, ticket = self.controller(LifecycleModel())
        # Establish the already-durable repair-routing precondition directly;
        # this fixture is testing the next scheduler stage, not board projection.
        self.ledger.connection.execute("UPDATE tickets SET state=?,lease_owner=NULL,lease_expires_at=NULL WHERE id=?", (CanonicalState.NEEDS_TRIAGE.value, ticket))
        self.ledger.connection.execute("DELETE FROM board_projection_outbox WHERE ticket_id=?", (ticket,))
        self.ledger.record_runtime_stage(
            ticket,
            "repair-routing-1",
            json.dumps({"ticket_id": ticket, "attempt_number": 1, "action": "triage", "failure_evidence": "ticket is too broad"}, sort_keys=True, separators=(",", ":")),
            attempt_number=1,
        )
        return ctl, ticket

    def triage_planner(self, payload: dict[str, object], calls: list[tuple[str, ...]] | None = None) -> LocalTriagePlanner:
        def runner(argv, **kwargs):
            if calls is not None:
                calls.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        return LocalTriagePlanner(runner=runner, provider="triage-provider", model="triage-model", profile="triage-profile", timeout_seconds=23)

    def triage_child_payload(self) -> dict[str, object]:
        child = contract()
        child["ticket_id"] = "model-child-id"
        child["dependencies"] = []
        return {
            "classification": "oversized_ticket",
            "root_cause_evidence": "The ticket contains two separable concerns.",
            "recommended_action": "decompose",
            "children": [{"id": "suggested-child", "resolves_criteria": ["AC-1"], "ticket": child}],
        }

    def test_scheduler_triage_decomposes_once_and_queues_authoritative_child_projection(self) -> None:
        ctl, ticket = self.triage_parent()
        calls: list[tuple[str, ...]] = []
        planner = self.triage_planner(self.triage_child_payload(), calls)
        preview = preview_next(self.ledger, now=100)
        self.assertEqual((preview.next_stage, preview.ticket_id), ("triage", ticket))

        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="triage", lease_seconds=30, clock=lambda: 100,
            triage_runner=lambda value: ctl.execute_triage_only(value, planner=planner),
            triage_execution_policy_hash=planner.execution_policy_hash(),
        )
        result = scheduler.process_next()
        self.assertEqual((result.stage, result.status), ("triage", "completed"))
        self.assertEqual(len(calls), 1)
        self.assertIn("safe", calls[0])
        children = self.ledger.connection.execute("SELECT * FROM tickets WHERE parent_ticket_id=?", (ticket,)).fetchall()
        self.assertEqual(len(children), 1)
        child_id = str(children[0]["id"])
        self.assertNotEqual(child_id, "model-child-id")
        self.assertEqual(children[0]["state"], "ready_local")
        binding = self.ledger.runtime_binding(child_id)
        parent_binding = self.ledger.runtime_binding(ticket)
        self.assertEqual((binding["repository_path"], binding["starting_sha"], binding["canonical_sha"], binding["ownership_verified"]), (parent_binding["repository_path"], parent_binding["starting_sha"], parent_binding["canonical_sha"], parent_binding["ownership_verified"]))
        self.assertEqual(self.ledger.connection.execute("SELECT criterion_id FROM ticket_criteria WHERE ticket_id=?", (child_id,)).fetchone()[0], "AC-1")
        outbox = self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND operation='create_microticket'", (child_id,)).fetchone()
        self.assertIsNotNone(outbox)
        payload = json.loads(str(outbox["payload_json"]))
        contract_body = json.loads(payload["body"].split("```local-first-contract\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(contract_body["orchestrator_ticket_id"], child_id)
        self.assertEqual(contract_body["parent_ticket_id"], ticket)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "needs_triage")
        # Even a delayed projection retry gates implementation eligibility.
        self.ledger.connection.execute("UPDATE board_projection_outbox SET next_attempt_at=200 WHERE ticket_id=? AND operation='create_microticket'", (child_id,))
        self.assertNotEqual(preview_next(self.ledger, now=100).next_stage, "implementation")

    def test_scheduler_triage_child_projection_uses_existing_retry_safe_worker(self) -> None:
        ctl, ticket = self.triage_parent()
        planner = self.triage_planner(self.triage_child_payload())
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="triage", lease_seconds=30, clock=lambda: 100,
            triage_runner=lambda value: ctl.execute_triage_only(value, planner=planner),
            triage_execution_policy_hash=planner.execution_policy_hash(),
        )
        self.assertEqual(scheduler.process_next().stage, "triage")
        child_id = str(self.ledger.connection.execute("SELECT id FROM tickets WHERE parent_ticket_id=?", (ticket,)).fetchone()[0])
        board = ProjectionBoard()
        delivered = GeneratedProjectionWorker(self.ledger, board, worker_id="projection", clock=lambda: 101).deliver_one()
        self.assertEqual((delivered.status, delivered.ticket_id), ("delivered", child_id))
        self.assertEqual(len(board.created), 1)
        _, body, key = board.created[0]
        projected = json.loads(body.split("```local-first-contract\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(projected["kind"], "triage_microticket")
        self.assertEqual(projected["orchestrator_ticket_id"], child_id)
        self.assertEqual(projected["parent_ticket_id"], ticket)
        self.assertEqual(projected["projection_key"], key)
        outbox = self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND operation='create_microticket'", (child_id,)).fetchone()
        self.assertIsNotNone(outbox["acknowledged_at"])

    def test_scheduler_triage_block_and_checkpoint_do_not_create_children(self) -> None:
        for action, expected_state in (("block", "blocked"), ("checkpoint", "needs_checkpoint")):
            with self.subTest(action=action):
                self.tearDown(); self.setUp()
                ctl, ticket = self.triage_parent()
                planner = self.triage_planner({"classification":"architecture_gap","root_cause_evidence":"A higher-level decision is required.","recommended_action":action,"children":[]})
                scheduler = ProcessNextScheduler(
                    self.ledger, Board(), worker_id="triage", lease_seconds=30, clock=lambda: 100,
                    triage_runner=lambda value, ctl=ctl, planner=planner: ctl.execute_triage_only(value, planner=planner),
                    triage_execution_policy_hash=planner.execution_policy_hash(),
                )
                result = scheduler.process_next()
                self.assertEqual(result.stage, "triage")
                self.assertEqual(self.ledger.get_ticket(ticket)["state"], expected_state)
                self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM tickets WHERE parent_ticket_id=?", (ticket,)).fetchone()[0], 0)

    def test_scheduler_triage_recovers_completed_invocation_without_reinvocation_or_duplicate_children(self) -> None:
        ctl, ticket = self.triage_parent()
        calls: list[tuple[str, ...]] = []
        planner = self.triage_planner(self.triage_child_payload(), calls)
        crashing = LocalFirstController(self.ledger, Board(), self.config, local_model=LifecycleModel(), fault_injector=lambda stage: (_ for _ in ()).throw(RuntimeError("simulated controller death")) if stage == "triage_invocation_completed" else None)
        first = ProcessNextScheduler(
            self.ledger, Board(), worker_id="triage-a", lease_seconds=30, clock=lambda: 100,
            triage_runner=lambda value: crashing.execute_triage_only(value, planner=planner),
            triage_execution_policy_hash=planner.execution_policy_hash(),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            first.process_next()
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.ledger.model_stage(ticket, 1, "triage"))
        invocation = self.ledger.connection.execute("SELECT * FROM model_invocations WHERE ticket_id=? AND stage='triage'", (ticket,)).fetchone()
        self.assertEqual(invocation["status"], "completed")

        resumed_ctl = LocalFirstController(self.ledger, Board(), self.config, local_model=LifecycleModel())
        second = ProcessNextScheduler(
            self.ledger, Board(), worker_id="triage-b", lease_seconds=30, clock=lambda: 131,
            triage_runner=lambda value: resumed_ctl.execute_triage_only(value, planner=planner),
            triage_execution_policy_hash=planner.execution_policy_hash(),
        )
        result = second.process_next()
        self.assertEqual(result.stage, "triage")
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(self.ledger.model_stage(ticket, 1, "triage"))
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM tickets WHERE parent_ticket_id=?", (ticket,)).fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM board_projection_outbox WHERE operation='create_microticket'", ()).fetchone()[0], 1)

    def test_scheduler_triage_restart_after_child_materialization_does_not_duplicate_children_or_outbox(self) -> None:
        ctl, ticket = self.triage_parent()
        calls: list[tuple[str, ...]] = []
        planner = self.triage_planner(self.triage_child_payload(), calls)
        crashing = LocalFirstController(self.ledger, Board(), self.config, local_model=LifecycleModel(), fault_injector=lambda stage: (_ for _ in ()).throw(RuntimeError("simulated controller death")) if stage == "triage_action_applied" else None)
        first = ProcessNextScheduler(
            self.ledger, Board(), worker_id="triage-a", lease_seconds=30, clock=lambda: 100,
            triage_runner=lambda value: crashing.execute_triage_only(value, planner=planner),
            triage_execution_policy_hash=planner.execution_policy_hash(),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            first.process_next()
        self.assertEqual(len(calls), 1)
        child_before = str(self.ledger.connection.execute("SELECT id FROM tickets WHERE parent_ticket_id=?", (ticket,)).fetchone()[0])
        self.assertIsNone(self.ledger.runtime_stage(ticket, "triage-applied-1"))
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM board_projection_outbox WHERE operation='create_microticket'", ()).fetchone()[0], 1)

        resumed_ctl = LocalFirstController(self.ledger, Board(), self.config, local_model=LifecycleModel())
        projection_board = ProjectionBoard()
        second = ProcessNextScheduler(
            self.ledger, projection_board, worker_id="triage-b", lease_seconds=30, clock=lambda: 131,
            triage_runner=lambda value: resumed_ctl.execute_triage_only(value, planner=planner),
            triage_execution_policy_hash=planner.execution_policy_hash(),
        )
        projection = second.process_next()
        self.assertEqual(projection.stage, "generated_projection")
        self.assertEqual(len(projection_board.created), 1)
        result = second.process_next()
        self.assertEqual(result.stage, "triage")
        self.assertEqual(len(calls), 1)
        children = self.ledger.connection.execute("SELECT id FROM tickets WHERE parent_ticket_id=?", (ticket,)).fetchall()
        self.assertEqual([str(row[0]) for row in children], [child_before])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM board_projection_outbox WHERE operation='create_microticket'", ()).fetchone()[0], 1)

    def test_scheduler_triage_refuses_unknown_started_invocation_and_policy_drift(self) -> None:
        ctl, ticket = self.triage_parent()
        planner = self.triage_planner(self.triage_child_payload())
        crashing = LocalFirstController(self.ledger, Board(), self.config, local_model=LifecycleModel(), fault_injector=lambda stage: (_ for _ in ()).throw(RuntimeError("simulated controller death")) if stage == "triage_invocation_started" else None)
        first = ProcessNextScheduler(
            self.ledger, Board(), worker_id="triage-a", lease_seconds=30, clock=lambda: 100,
            triage_runner=lambda value: crashing.execute_triage_only(value, planner=planner),
            triage_execution_policy_hash=planner.execution_policy_hash(),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            first.process_next()
        invocation = self.ledger.connection.execute("SELECT * FROM model_invocations WHERE ticket_id=? AND stage='triage'", (ticket,)).fetchone()
        self.assertEqual(invocation["status"], "started")
        drifted = self.triage_planner(self.triage_child_payload())
        drifted.model = "different-model"
        with self.assertRaisesRegex(RuntimeError, "claim identity drift"):
            self.ledger.claim_next_scheduler_triage("other", lease_seconds=30, triage_execution_policy_hash=drifted.execution_policy_hash(), now=131)

        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="triage-b", lease_seconds=30, clock=lambda: 131,
            triage_runner=lambda value: ctl.execute_triage_only(value, planner=planner),
            triage_execution_policy_hash=planner.execution_policy_hash(),
        )
        with self.assertRaisesRegex(RuntimeError, "triage_reconciliation_required"):
            resumed.process_next()
    def test_completed_persists_start_before_call_and_links_output_artifact(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model); self.assertTrue(ctl.execute(ticket, repository=self.repo, allow_board_writes=True))
        row = self.invocation(ticket); self.assertEqual(row["status"], "completed"); self.assertEqual(row["timeout_seconds"], 17); self.assertTrue(Path(str(row["model_artifact"])).is_file())
        self.assertEqual(self.ledger.incomplete_model_invocations(ticket), [])
    def test_timeout_persists_terminal_timeout_without_success_artifact_or_review(self) -> None:
        model = LifecycleModel("timeout"); ctl, ticket = self.controller(model)
        with self.assertRaises(subprocess.TimeoutExpired): ctl.execute(ticket, repository=self.repo, allow_board_writes=True)
        row = self.invocation(ticket); self.assertEqual(row["status"], "timeout"); self.assertIsNone(row["model_artifact"]); self.assertIsNone(self.ledger.model_stage(ticket, 1, "implementation")); self.assertEqual(model.calls, 1)
        self.assertIsNone(self.ledger.accepted_commit(ticket)); self.assertEqual(self.ledger.get_ticket(ticket)["state"], "blocked")
    def test_process_error_persists_terminal_error(self) -> None:
        model = LifecycleModel("error"); ctl, ticket = self.controller(model)
        with self.assertRaisesRegex(RuntimeError, "launch failed"): ctl.execute(ticket, repository=self.repo, allow_board_writes=True)
        row = self.invocation(ticket); self.assertEqual(row["status"], "process_error"); self.assertIn("launch failed", str(row["error_json"]))
    def test_crash_after_start_is_incomplete_and_never_reinvoked(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model, "implementation_invocation_started")
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"): ctl.execute(ticket, repository=self.repo, allow_board_writes=True)
        self.ledger.close(); self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate()
        row = self.invocation(ticket); self.assertEqual(row["status"], "started"); self.assertEqual(len(self.ledger.incomplete_model_invocations(ticket)), 1)
        resumed = LocalFirstController(self.ledger, Board(), self.config, local_model=model)
        with self.assertRaisesRegex(RuntimeError, "execution_reconciliation_required"): resumed.execute(ticket, repository=self.repo, allow_board_writes=True)
        self.assertEqual(model.calls, 0)

    def test_scheduler_model_only_stage_stops_before_validation_or_review(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler",
            lease_seconds=30,
            clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        )

        result = scheduler.process_next()

        self.assertEqual((result.stage, result.status, result.ticket_id), ("implementation", "completed", ticket))
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "implementing")
        self.assertIsNotNone(self.ledger.model_stage(ticket, 1, "implementation"))
        self.assertIsNone(self.ledger.runtime_stage(ticket, "validation_completed"))
        self.assertEqual(self.ledger.review_invocations(ticket, 1), [])
        self.assertIsNone(self.ledger.review_candidate(ticket))
        claim = self.ledger.scheduler_claim(str(result.claim_id))
        self.assertEqual(claim["status"], "completed")
        self.assertIsNotNone(claim["side_effect_started_at"])
        self.assertIsNotNone(claim["side_effect_completed_at"])
        self.assertIsNotNone(claim["finalized_at"])
        row = self.ledger.get_ticket(ticket)
        self.assertIsNone(row["lease_owner"])
        self.assertIsNone(row["lease_expires_at"])

    def test_scheduler_validation_stage_persists_evidence_without_review_or_candidate_freeze(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        implementation = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-implementation",
            lease_seconds=30,
            clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        ).process_next()
        self.assertEqual(implementation.stage, "implementation")

        validation = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-validation",
            lease_seconds=30,
            clock=lambda: 101,
            validation_runner=lambda ticket_id: ctl.execute_deterministic_validation_only(ticket_id, repository=self.repo),
        ).process_next()

        self.assertEqual((validation.stage, validation.status, validation.ticket_id), ("validation", "completed", ticket))
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "local_review")
        self.assertIsNotNone(self.ledger.runtime_stage(ticket, "validation-1"))
        self.assertIsNotNone(self.ledger.runtime_stage(ticket, "validation_completed"))
        self.assertEqual(self.ledger.review_invocations(ticket, 1), [])
        self.assertIsNone(self.ledger.review_candidate(ticket))
        claim = self.ledger.scheduler_claim(str(validation.claim_id))
        self.assertEqual(claim["status"], "completed")
        self.assertIsNotNone(claim["side_effect_started_at"])
        self.assertIsNotNone(claim["side_effect_completed_at"])
        self.assertIsNotNone(claim["finalized_at"])
        row = self.ledger.get_ticket(ticket)
        self.assertIsNone(row["lease_owner"])
        self.assertIsNone(row["lease_expires_at"])

    def test_scheduler_review_persists_independent_packet_only_evidence_without_repair_or_acceptance(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-implementation", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        ).process_next()
        ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-validation", lease_seconds=30, clock=lambda: 101,
            validation_runner=lambda ticket_id: ctl.execute_deterministic_validation_only(ticket_id, repository=self.repo),
        ).process_next()

        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-review", lease_seconds=30, clock=lambda: 102,
            review_runner=lambda ticket_id: ctl.execute_fresh_review_only(ticket_id, repository=self.repo),
            review_execution_policy_hash=ctl.review_execution_policy_hash(),
        )
        results = [scheduler.process_next() for _ in range(3)]
        result = next(item for item in results if item.stage == "review")

        self.assertEqual((result.stage, result.status, result.ticket_id), ("review", "completed", ticket))
        self.assertEqual(model.calls, 2)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "local_review")
        self.assertIsNotNone(self.ledger.review_candidate(ticket, 1))
        self.assertIsNotNone(self.ledger.model_stage(ticket, 1, "review"))
        self.assertIsNone(self.ledger.connection.execute("SELECT * FROM review_results WHERE ticket_id=?", (ticket,)).fetchone())
        self.assertIsNone(self.ledger.accepted_commit(ticket))
        review_call = self.ledger.review_invocations(ticket, 1)[0]
        self.assertEqual(review_call["worktree_path"], "packet-only")
        review_artifact = json.loads(Path(str(review_call["model_artifact"])).read_text(encoding="utf-8"))
        self.assertEqual(set(review_artifact), {"provider", "model", "payload"})
        self.assertEqual(review_artifact["provider"], model.provider)
        self.assertEqual(review_artifact["model"], model.model)
        claim = self.ledger.scheduler_claim(str(result.claim_id))
        self.assertEqual(claim["status"], "completed")
        self.assertIsNotNone(claim["side_effect_completed_at"])
        self.assertIsNotNone(claim["finalized_at"])

    def test_scheduler_review_recovery_finalizes_completed_output_without_reinvocation(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(self.ledger, Board(), worker_id="implementation", lease_seconds=30, clock=lambda: 100, implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo)).process_next()
        ProcessNextScheduler(self.ledger, Board(), worker_id="validation", lease_seconds=30, clock=lambda: 101, validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo)).process_next()
        claim = self.ledger.claim_next_scheduler_review("crashed", lease_seconds=1, review_execution_policy_hash=ctl.review_execution_policy_hash(), now=102)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed", now=102)
        persisted = ctl.execute_fresh_review_only(ticket, repository=self.repo)
        self.ledger.complete_scheduler_review_effect(claim_id, "crashed", persisted, now=102)
        calls: list[str] = []
        resumed = ProcessNextScheduler(self.ledger, Board(), worker_id="recovery", lease_seconds=30, clock=lambda: 104, review_runner=lambda value: calls.append(value) or (_ for _ in ()).throw(AssertionError("reviewer must not be reinvoked")), review_execution_policy_hash=ctl.review_execution_policy_hash())

        results = [resumed.process_next() for _ in range(3)]

        final = next(item for item in results if item.stage == "review")
        self.assertEqual((final.status, final.ticket_id), ("completed", ticket))
        self.assertEqual(calls, [])
        self.assertIsNotNone(self.ledger.scheduler_claim(claim_id)["finalized_at"])

    def test_scheduler_review_refuses_unknown_started_review_without_reinvocation(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(self.ledger, Board(), worker_id="implementation", lease_seconds=30, clock=lambda: 100, implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo)).process_next()
        ProcessNextScheduler(self.ledger, Board(), worker_id="validation", lease_seconds=30, clock=lambda: 101, validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo)).process_next()
        crashing = LocalFirstController(self.ledger, Board(), self.config, local_model=model, fault_injector=lambda stage: (_ for _ in ()).throw(RuntimeError("simulated controller death")) if stage == "review_invocation_started" else None)
        first = ProcessNextScheduler(self.ledger, Board(), worker_id="crashed", lease_seconds=30, clock=lambda: 102, review_runner=lambda value: crashing.execute_fresh_review_only(value, repository=self.repo), review_execution_policy_hash=crashing.review_execution_policy_hash())
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            for _ in range(3): first.process_next()
        self.assertEqual(model.calls, 1)
        review_call = self.ledger.review_invocations(ticket, 1)[0]
        self.assertEqual(review_call["status"], "started")

        calls: list[str] = []
        resumed = ProcessNextScheduler(self.ledger, Board(), worker_id="recovery", lease_seconds=30, clock=lambda: 133, review_runner=lambda value: calls.append(value) or ctl.execute_fresh_review_only(value, repository=self.repo), review_execution_policy_hash=ctl.review_execution_policy_hash())
        with self.assertRaisesRegex(RuntimeError, "review_reconciliation_required"):
            for _ in range(3): resumed.process_next()
        self.assertEqual(calls, [])
        claim = self.ledger.connection.execute("SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage LIKE 'review:%'", (ticket,)).fetchone()
        self.assertIsNotNone(claim["side_effect_started_at"])
        self.assertIsNone(claim["side_effect_completed_at"])

    def test_scheduler_review_recovers_completed_invocation_before_model_stage_without_reinvocation(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(self.ledger, Board(), worker_id="implementation", lease_seconds=30, clock=lambda: 100, implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo)).process_next()
        ProcessNextScheduler(self.ledger, Board(), worker_id="validation", lease_seconds=30, clock=lambda: 101, validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo)).process_next()
        crashing = LocalFirstController(self.ledger, Board(), self.config, local_model=model, fault_injector=lambda stage: (_ for _ in ()).throw(RuntimeError("simulated controller death")) if stage == "review_invocation_completed" else None)
        first = ProcessNextScheduler(self.ledger, Board(), worker_id="crashed", lease_seconds=30, clock=lambda: 102, review_runner=lambda value: crashing.execute_fresh_review_only(value, repository=self.repo), review_execution_policy_hash=crashing.review_execution_policy_hash())
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            for _ in range(3): first.process_next()
        self.assertEqual(model.calls, 2)
        review_call = self.ledger.review_invocations(ticket, 1)[0]
        self.assertEqual(review_call["status"], "completed")
        artifact_before = Path(str(review_call["model_artifact"])).read_bytes()
        self.assertIsNone(self.ledger.model_stage(ticket, 1, "review"))

        resumed_ctl = LocalFirstController(self.ledger, Board(), self.config, local_model=model)
        resumed = ProcessNextScheduler(self.ledger, Board(), worker_id="recovery", lease_seconds=30, clock=lambda: 133, review_runner=lambda value: resumed_ctl.execute_fresh_review_only(value, repository=self.repo), review_execution_policy_hash=resumed_ctl.review_execution_policy_hash())
        results = [resumed.process_next() for _ in range(3)]
        final = next(item for item in results if item.stage == "review")
        self.assertEqual(final.status, "completed")
        self.assertEqual(model.calls, 2)
        self.assertIsNotNone(self.ledger.model_stage(ticket, 1, "review"))
        self.assertEqual(Path(str(review_call["model_artifact"])).read_bytes(), artifact_before)

    def test_scheduler_review_reclaim_fails_closed_on_execution_policy_drift(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(self.ledger, Board(), worker_id="implementation", lease_seconds=30, clock=lambda: 100, implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo)).process_next()
        ProcessNextScheduler(self.ledger, Board(), worker_id="validation", lease_seconds=30, clock=lambda: 101, validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo)).process_next()
        original_policy_hash = ctl.review_execution_policy_hash()
        claim = self.ledger.claim_next_scheduler_review("crashed", lease_seconds=1, review_execution_policy_hash=original_policy_hash, now=102)
        assert claim is not None
        original_identity = json.loads(str(claim["candidate_identity_json"]))
        model.model = "fixture-model-v2"
        drifted_ctl = LocalFirstController(self.ledger, Board(), self.config, local_model=model)
        self.assertNotEqual(original_policy_hash, drifted_ctl.review_execution_policy_hash())
        with self.assertRaisesRegex(RuntimeError, "review execution policy drift"):
            self.ledger.claim_next_scheduler_review("recovery", lease_seconds=30, review_execution_policy_hash=drifted_ctl.review_execution_policy_hash(), now=104)
        self.assertEqual(json.loads(str(self.ledger.scheduler_claim(str(claim["claim_id"]))["candidate_identity_json"])), original_identity)

    def test_scheduler_review_fails_closed_when_candidate_identity_drifts(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(self.ledger, Board(), worker_id="implementation", lease_seconds=30, clock=lambda: 100, implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo)).process_next()
        ProcessNextScheduler(self.ledger, Board(), worker_id="validation", lease_seconds=30, clock=lambda: 101, validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo)).process_next()
        claim = self.ledger.claim_next_scheduler_review("reviewer", lease_seconds=30, review_execution_policy_hash=ctl.review_execution_policy_hash(), now=102)
        assert claim is not None
        self.ledger.begin_scheduler_claim_effect(str(claim["claim_id"]), "reviewer", now=102)
        self.ledger.connection.execute("UPDATE review_candidates SET runtime_identity_json='{}' WHERE ticket_id=? AND attempt_number=1", (ticket,))

        with self.assertRaisesRegex(RuntimeError, "review_reconciliation_required"):
            ctl.execute_fresh_review_only(ticket, repository=self.repo)

        self.assertEqual(model.calls, 1)
        self.assertIsNone(self.ledger.model_stage(ticket, 1, "review"))

    def test_scheduler_validation_rejects_post_validation_worktree_drift(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-implementation", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        ).process_next()
        claim = self.ledger.claim_next_scheduler_validation("scheduler-validation", lease_seconds=30, now=101)
        assert claim is not None
        self.ledger.begin_scheduler_claim_effect(str(claim["claim_id"]), "scheduler-validation", now=101)
        from local_first_orchestrator.validation import DeterministicValidator
        original_validate = DeterministicValidator.validate

        def mutate_after_validation(validator, worktree, ticket_value, *, base_sha):
            result = original_validate(validator, worktree, ticket_value, base_sha=base_sha)
            (worktree / "app.py").write_text("def value():\n    return 'drifted'\n", encoding="utf-8")
            return result

        with mock.patch.object(DeterministicValidator, "validate", new=mutate_after_validation):
            with self.assertRaisesRegex(RuntimeError, "validation_reconciliation_required"):
                ctl.execute_deterministic_validation_only(ticket, repository=self.repo)

        self.assertIsNone(self.ledger.runtime_stage(ticket, "validation-1"))
        self.assertIsNone(self.ledger.runtime_stage(ticket, "validation_completed"))
        self.assertIsNone(self.ledger.scheduler_claim(str(claim["claim_id"]))["side_effect_completed_at"])

    def test_scheduler_validation_rejects_changed_implementation_artifact(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-implementation", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        ).process_next()
        claim = self.ledger.claim_next_scheduler_validation("scheduler-validation", lease_seconds=30, now=101)
        assert claim is not None
        self.ledger.begin_scheduler_claim_effect(str(claim["claim_id"]), "scheduler-validation", now=101)
        implementation = self.ledger.model_stage(ticket, 1, "implementation")
        assert implementation is not None
        Path(str(implementation["response_artifact"])).write_text('{"tampered":true}', encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "validation_reconciliation_required"):
            ctl.execute_deterministic_validation_only(ticket, repository=self.repo)

        self.assertIsNone(self.ledger.runtime_stage(ticket, "validation-1"))

    def test_scheduler_validation_claim_is_scoped_to_the_implementation_attempt(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-implementation", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        ).process_next()
        first = self.ledger.claim_next_scheduler_validation("scheduler-validation-a", lease_seconds=30, now=101)
        assert first is not None
        implementation = self.ledger.model_stage(ticket, 1, "implementation")
        assert implementation is not None
        self.ledger.ensure_attempt(ticket, 2)
        self.assertTrue(self.ledger.record_model_stage(
            ticket, 2, "implementation", purpose="implementation", adapter="fixture",
            request_hash="request-2", response_artifact=str(implementation["response_artifact"]),
            worktree_path=str(implementation["worktree_path"]), base_sha=str(implementation["base_sha"]),
            diff_hash=str(implementation["diff_hash"]),
        ))
        self.ledger.connection.execute("UPDATE tickets SET state='implementing' WHERE id=?", (ticket,))

        second = self.ledger.claim_next_scheduler_validation("scheduler-validation-b", lease_seconds=30, now=102)

        assert second is not None
        self.assertNotEqual(second["claim_id"], first["claim_id"])
        self.assertEqual(json.loads(second["candidate_identity_json"])["attempt_number"], 2)

    def test_scheduler_validation_rejects_tampered_completed_effect_on_finalization_recovery(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-implementation", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        ).process_next()
        claim = self.ledger.claim_next_scheduler_validation("crashed-worker", lease_seconds=1, now=101)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed-worker", now=101)
        result = ctl.execute_deterministic_validation_only(ticket, repository=self.repo)
        self.ledger.complete_scheduler_validation_effect(claim_id, "crashed-worker", result, now=101)
        Path(str(result["validation_artifact"])).write_text('{"tampered":true}', encoding="utf-8")

        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-recovery", lease_seconds=30, clock=lambda: 103,
            validation_runner=lambda ticket_id: (_ for _ in ()).throw(AssertionError("completed effect must not invoke runner")),
        )
        with self.assertRaisesRegex(RuntimeError, "validation_reconciliation_required"):
            for _ in range(4):
                resumed.process_next()

        self.assertEqual(self.ledger.scheduler_claim(claim_id)["status"], "claimed")

    def test_scheduler_validation_rejects_tampered_persisted_evidence_on_recovery(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-implementation", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        ).process_next()
        claim = self.ledger.claim_next_scheduler_validation("crashed-worker", lease_seconds=1, now=101)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed-worker", now=101)
        result = ctl.execute_deterministic_validation_only(ticket, repository=self.repo)
        Path(str(result["validation_artifact"])).write_text('{"tampered":true}', encoding="utf-8")

        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-recovery", lease_seconds=30, clock=lambda: 103,
            validation_runner=lambda ticket_id: ctl.execute_deterministic_validation_only(ticket_id, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "validation_reconciliation_required"):
            for _ in range(4):
                resumed.process_next()

        self.assertIsNone(self.ledger.scheduler_claim(claim_id)["side_effect_completed_at"])

    def test_scheduler_finalizes_completed_validation_claim_after_expiry_without_rerunning(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-implementation", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        ).process_next()
        claim = self.ledger.claim_next_scheduler_validation("crashed-worker", lease_seconds=1, now=101)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed-worker", now=101)
        result = ctl.execute_deterministic_validation_only(ticket, repository=self.repo)
        self.assertFalse(result["replayed"])
        calls: list[str] = []

        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler-recovery", lease_seconds=30, clock=lambda: 103,
            validation_runner=lambda ticket_id: calls.append(ticket_id) or ctl.execute_deterministic_validation_only(ticket_id, repository=self.repo),
        )
        stages = [resumed.process_next() for _ in range(4)]

        final = next(stage for stage in stages if stage.stage == "validation")
        self.assertEqual((final.status, final.ticket_id), ("completed", ticket))
        self.assertEqual(calls, [ticket])
        self.assertEqual(self.ledger.scheduler_claim(claim_id)["status"], "completed")
        self.assertIsNone(self.ledger.get_ticket(ticket)["lease_owner"])
        self.assertIsNone(self.ledger.get_ticket(ticket)["lease_expires_at"])

    def test_scheduler_replays_persisted_implementation_after_crash_without_reinvocation(self) -> None:
        model = LifecycleModel(); crashing, ticket = self.controller(model, "implementation_completed")
        first = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-a",
            lease_seconds=30,
            clock=lambda: 100,
            implementation_runner=lambda ticket_id: crashing.execute_implementation_model_only(ticket_id, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            first.process_next()
        self.assertEqual(model.calls, 1)
        claim = self.ledger.connection.execute("SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage='implementation'", (ticket,)).fetchone()
        self.assertIsNotNone(claim["side_effect_started_at"])
        self.assertIsNone(claim["side_effect_completed_at"])
        self.assertIsNotNone(self.ledger.model_stage(ticket, 1, "implementation"))

        resumed = LocalFirstController(self.ledger, Board(), self.config, local_model=model)
        second = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-b",
            lease_seconds=30,
            clock=lambda: 131,
            implementation_runner=lambda ticket_id: resumed.execute_implementation_model_only(ticket_id, repository=self.repo),
        )
        result = second.process_next()
        self.assertEqual((result.stage, result.status), ("implementation", "completed"))
        self.assertEqual(model.calls, 1)
        final = self.ledger.scheduler_claim(str(result.claim_id))
        self.assertEqual(final["attempt_count"], 2)
        self.assertIsNotNone(final["side_effect_completed_at"])
        self.assertIsNotNone(final["finalized_at"])

    def test_scheduler_recovers_completed_invocation_before_model_stage_record_without_reinvocation(self) -> None:
        model = LifecycleModel(); crashing, ticket = self.controller(model, "implementation_invocation_completed")
        first = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-a",
            lease_seconds=30,
            clock=lambda: 100,
            implementation_runner=lambda ticket_id: crashing.execute_implementation_model_only(ticket_id, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            first.process_next()
        self.assertEqual(model.calls, 1)
        invocation = self.invocation(ticket)
        self.assertEqual(invocation["status"], "completed")
        self.assertIsNone(self.ledger.model_stage(ticket, 1, "implementation"))

        resumed = LocalFirstController(self.ledger, Board(), self.config, local_model=model)
        second = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-b",
            lease_seconds=30,
            clock=lambda: 131,
            implementation_runner=lambda ticket_id: resumed.execute_implementation_model_only(ticket_id, repository=self.repo),
        )
        result = second.process_next()
        self.assertEqual((result.stage, result.status), ("implementation", "completed"))
        self.assertEqual(model.calls, 1)
        self.assertIsNotNone(self.ledger.model_stage(ticket, 1, "implementation"))

    def test_scheduler_does_not_retry_terminal_failed_model_invocation(self) -> None:
        model = LifecycleModel("timeout"); ctl, ticket = self.controller(model)
        first = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-a",
            lease_seconds=30,
            clock=lambda: 100,
            implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(ticket_id, repository=self.repo),
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            first.process_next()
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.invocation(ticket)["status"], "timeout")

        resumed = LocalFirstController(self.ledger, Board(), self.config, local_model=model)
        second = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-b",
            lease_seconds=30,
            clock=lambda: 131,
            implementation_runner=lambda ticket_id: resumed.execute_implementation_model_only(ticket_id, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "execution_reconciliation_required"):
            second.process_next()
        self.assertEqual(model.calls, 1)

    def test_scheduler_never_reinvokes_unknown_started_model_invocation(self) -> None:
        model = LifecycleModel(); crashing, ticket = self.controller(model, "implementation_invocation_started")
        first = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-a",
            lease_seconds=30,
            clock=lambda: 100,
            implementation_runner=lambda ticket_id: crashing.execute_implementation_model_only(ticket_id, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            first.process_next()
        self.assertEqual(model.calls, 0)
        self.assertEqual(len(self.ledger.incomplete_model_invocations(ticket)), 1)

        resumed = LocalFirstController(self.ledger, Board(), self.config, local_model=model)
        second = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler-b",
            lease_seconds=30,
            clock=lambda: 131,
            implementation_runner=lambda ticket_id: resumed.execute_implementation_model_only(ticket_id, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "execution_reconciliation_required"):
            second.process_next()
        self.assertEqual(model.calls, 0)
        claim = self.ledger.connection.execute("SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage='implementation'", (ticket,)).fetchone()
        self.assertEqual(claim["status"], "claimed")
        self.assertIsNotNone(claim["side_effect_started_at"])
        self.assertIsNone(claim["side_effect_completed_at"])

    def test_scheduler_validation_failure_fingerprint_ignores_volatile_locations_and_timestamps(self) -> None:
        first = _stable_scheduler_failure_fingerprint(
            "ticket",
            "validation",
            "FAILED /tmp/work-a/app.py line 41 at 2026-09-10T14:00:01Z: expected ok",
        )
        second = _stable_scheduler_failure_fingerprint(
            "ticket",
            "validation",
            "failed /var/tmp/work-b/app.py line 987 at 2026-09-11T09:22:33Z: expected ok",
        )
        self.assertEqual(first, second)

    def test_scheduler_validation_failure_routes_to_repair_and_next_attempt_uses_failure_evidence(self) -> None:
        model = SequencedLifecycleModel(["still-bad", "ok"]); ctl, ticket = self.controller(model)
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
        )
        self.assertEqual(scheduler.process_next().stage, "implementation")
        validation = scheduler.process_next()
        self.assertEqual(validation.stage, "validation")
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "verifying")
        preview = preview_next(self.ledger, now=100)
        self.assertEqual((preview.next_stage, preview.ticket_id), ("repair_routing", ticket))

        routed = scheduler.process_next()
        self.assertEqual((routed.stage, routed.status), ("repair_routing", "completed"))
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "repairing")
        decision = json.loads(str(self.ledger.runtime_stage(ticket, "repair-routing-1")["detail"]))
        self.assertEqual((decision["action"], decision["next_attempt_number"]), ("repair", 2))
        attempt1 = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()
        attempt2 = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=2", (ticket,)).fetchone()
        self.assertEqual((attempt2["worktree_path"], attempt2["branch"]), (attempt1["worktree_path"], attempt1["branch"]))

        repaired = scheduler.process_next()
        self.assertEqual(repaired.stage, "implementation")
        self.assertEqual(model.implementation_calls, 2)
        self.assertIn("## failure_evidence: compact", model.implementation_packets[1])
        self.assertIsNotNone(self.ledger.model_stage(ticket, 2, "implementation"))

    def test_scheduler_repeated_validation_failure_routes_to_triage_once(self) -> None:
        model = SequencedLifecycleModel(["still-bad", "still-bad"]); ctl, ticket = self.controller(model)
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
        )
        for expected in ("implementation", "validation", "repair_routing", "implementation", "validation", "repair_routing"):
            self.assertEqual(scheduler.process_next().stage, expected)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "needs_triage")
        decision = json.loads(str(self.ledger.runtime_stage(ticket, "repair-routing-2")["detail"]))
        self.assertEqual(decision["action"], "triage")
        self.assertTrue(decision["repeated_fingerprint"])
        triage_events = [event for event in self.ledger.events_for(ticket) if event["event_type"] == "state_transition" and event["to_state"] == "needs_triage"]
        self.assertEqual(len(triage_events), 1)
        self.assertIsNone(self.ledger.claim_next_scheduler_repair_routing("other", lease_seconds=30, now=100))

    def test_scheduler_validation_failure_respects_max_attempts_without_repeat(self) -> None:
        model = SequencedLifecycleModel(["still-bad"]); ctl, ticket = self.controller(model)
        self.ledger.connection.execute("UPDATE tickets SET max_attempts=1 WHERE id=?", (ticket,))
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
        )
        self.assertEqual(scheduler.process_next().stage, "implementation")
        self.assertEqual(scheduler.process_next().stage, "validation")
        self.assertEqual(scheduler.process_next().stage, "repair_routing")
        decision = json.loads(str(self.ledger.runtime_stage(ticket, "repair-routing-1")["detail"]))
        self.assertEqual(decision["action"], "triage")
        self.assertFalse(decision["repeated_fingerprint"])
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "needs_triage")

    def test_scheduler_review_repair_routes_same_ticket_and_records_review_evidence(self) -> None:
        review_payload = {
            "verdict": "repair",
            "criterion_results": [{"criterion_id":"AC-1","status":"fail","evidence":"value is wrong"}],
            "findings": [{"severity":"blocking","criterion_id":"AC-1","file":"app.py","symbol":"value","evidence":"wrong value","minimal_repair":"return ok","verification":"run configured test","fingerprint_input":"wrong return value"}],
            "suggestions": [],
        }
        model = SequencedLifecycleModel(["ok", "ok"], review_payload); ctl, ticket = self.controller(model)
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
            review_runner=lambda value: ctl.execute_fresh_review_only(value, repository=self.repo),
            review_execution_policy_hash=ctl.review_execution_policy_hash(),
        )
        self.assertEqual(scheduler.process_next().stage, "implementation")
        self.assertEqual(scheduler.process_next().stage, "validation")
        self.run_until_stage(scheduler, "review")
        routed = self.run_until_stage(scheduler, "repair_routing")
        self.assertEqual(routed.ticket_id, ticket)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "repairing")
        review_row = self.ledger.connection.execute("SELECT * FROM review_results WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()
        self.assertEqual(review_row["verdict"], "repair")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM review_findings WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()[0], 1)
        decision = json.loads(str(self.ledger.runtime_stage(ticket, "repair-routing-1")["detail"]))
        self.assertEqual(decision["action"], "repair")

    def test_scheduler_pass_review_is_recorded_without_accepting_in_routing_tick(self) -> None:
        model = SequencedLifecycleModel(["ok"]); ctl, ticket = self.controller(model)
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
            review_runner=lambda value: ctl.execute_fresh_review_only(value, repository=self.repo),
            review_execution_policy_hash=ctl.review_execution_policy_hash(),
        )
        self.assertEqual(scheduler.process_next().stage, "implementation")
        self.assertEqual(scheduler.process_next().stage, "validation")
        self.run_until_stage(scheduler, "review")
        self.run_until_stage(scheduler, "repair_routing")
        decision = json.loads(str(self.ledger.runtime_stage(ticket, "repair-routing-1")["detail"]))
        self.assertEqual(decision["action"], "pass")
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "local_review")
        self.assertEqual(self.ledger.connection.execute("SELECT verdict FROM review_results WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()[0], "pass")
        self.assertIsNone(self.ledger.accepted_commit(ticket))

    def test_scheduler_acceptance_freezes_exact_candidate_without_creating_commit(self) -> None:
        model = SequencedLifecycleModel(["ok"]); ctl, ticket = self.controller(model)
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
            review_runner=lambda value: ctl.execute_fresh_review_only(value, repository=self.repo),
            review_execution_policy_hash=ctl.review_execution_policy_hash(),
            acceptance_runner=lambda value: ctl.inspect_acceptance_candidate_only(value, repository=self.repo),
        )
        self.assertEqual(scheduler.process_next().stage, "implementation")
        self.assertEqual(scheduler.process_next().stage, "validation")
        self.run_until_stage(scheduler, "review")
        self.run_until_stage(scheduler, "repair_routing")
        self.assertEqual(preview_next(self.ledger, now=100).next_stage, "acceptance")
        accepted = self.run_until_stage(scheduler, "acceptance")
        self.assertEqual(accepted.status, "completed")
        frozen = self.ledger.accepted_candidate(ticket)
        self.assertIsNotNone(frozen)
        candidate = self.ledger.review_candidate(ticket, 1)
        self.assertEqual(frozen["candidate_fingerprint"], candidate["candidate_fingerprint"])
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "accepted")
        self.assertIsNone(self.ledger.accepted_commit(ticket))
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()
        self.assertIsNone(attempt["accepted_commit_sha"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("UPDATE accepted_candidates SET evidence_hash='tampered' WHERE ticket_id=?", (ticket,))

    def test_scheduler_acceptance_fails_closed_on_live_candidate_drift(self) -> None:
        model = SequencedLifecycleModel(["ok"]); ctl, ticket = self.controller(model)
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
            review_runner=lambda value: ctl.execute_fresh_review_only(value, repository=self.repo),
            review_execution_policy_hash=ctl.review_execution_policy_hash(),
            acceptance_runner=lambda value: ctl.inspect_acceptance_candidate_only(value, repository=self.repo),
        )
        self.assertEqual(scheduler.process_next().stage, "implementation")
        self.assertEqual(scheduler.process_next().stage, "validation")
        self.run_until_stage(scheduler, "review")
        self.run_until_stage(scheduler, "repair_routing")
        attempt = self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()
        Path(str(attempt["worktree_path"])) .joinpath("app.py").write_text("def value():\n    return 'drifted'\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "candidate worktree drift"):
            self.run_until_stage(scheduler, "acceptance")
        self.assertIsNone(self.ledger.accepted_candidate(ticket))
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "local_review")

    def test_scheduler_acceptance_finalizes_after_restart_without_reinspection(self) -> None:
        model = SequencedLifecycleModel(["ok"]); ctl, ticket = self.controller(model)
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
            review_runner=lambda value: ctl.execute_fresh_review_only(value, repository=self.repo),
            review_execution_policy_hash=ctl.review_execution_policy_hash(),
        )
        self.assertEqual(scheduler.process_next().stage, "implementation")
        self.assertEqual(scheduler.process_next().stage, "validation")
        self.run_until_stage(scheduler, "review")
        self.run_until_stage(scheduler, "repair_routing")
        claim = self.ledger.claim_next_scheduler_acceptance("crashed", lease_seconds=1, now=101)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed", now=101)
        inspected = ctl.inspect_acceptance_candidate_only(ticket, repository=self.repo)
        applied = self.ledger.apply_scheduler_acceptance_effect(claim_id, "crashed", current_diff_hash=str(inspected["current_diff_hash"]), now=101)
        self.assertIsNotNone(applied["side_effect_completed_at"])
        self.assertIsNone(applied["finalized_at"])
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "accepted")
        calls: list[str] = []
        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="recovery", lease_seconds=30, clock=lambda: 103,
            acceptance_runner=lambda value: calls.append(value) or (_ for _ in ()).throw(AssertionError("acceptance inspector must not rerun")),
        )
        result = self.run_until_stage(resumed, "acceptance")
        self.assertEqual(result.status, "completed")
        self.assertEqual(calls, [])
        self.assertEqual(self.ledger.scheduler_claim(claim_id)["status"], "completed")
        transitions = [event for event in self.ledger.events_for(ticket) if event["event_type"] == "state_transition" and event["to_state"] == "accepted"]
        self.assertEqual(len(transitions), 1)

    def test_scheduler_git_integration_commits_exact_candidate_without_completion(self) -> None:
        ctl, ticket, _ = self.prepare_scheduler_accepted()
        self.assertEqual(preview_next(self.ledger, now=100).next_stage, "git_integration")
        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="git",
            lease_seconds=30,
            clock=lambda: 100,
            git_integration_runner=lambda value: ctl.execute_git_integration_only(value, repository=self.repo),
        )
        result = scheduler.process_next()
        self.assertEqual((result.stage, result.status, result.ticket_id), ("git_integration", "completed", ticket))
        evidence = self.ledger.git_commit_evidence(ticket)
        intent = self.ledger.git_commit_intent(ticket)
        self.assertIsNotNone(evidence)
        self.assertEqual(intent["status"], "completed")
        self.assertEqual(intent["commit_sha"], evidence["commit_sha"])
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()
        worktree = Path(str(attempt["worktree_path"]))
        self.assertEqual(attempt["accepted_commit_sha"], evidence["commit_sha"])
        self.assertEqual(subprocess.run(("git", "rev-parse", "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(), evidence["commit_sha"])
        self.assertEqual(subprocess.run(("git", "rev-parse", "HEAD^"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(), evidence["base_sha"])
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), evidence["base_sha"])
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "accepted")
        self.assertIsNone(self.ledger.accepted_commit(ticket))
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM accepted_evidence WHERE ticket_id=?", (ticket,)).fetchone()[0], 0)
        self.assertFalse(any(event["to_state"] == "done" for event in self.ledger.events_for(ticket)))
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("UPDATE git_commit_evidence SET commit_sha='bad' WHERE ticket_id=?", (ticket,))

    def test_scheduler_git_integration_fails_closed_on_candidate_drift_and_untracked_content(self) -> None:
        ctl, ticket, _ = self.prepare_scheduler_accepted()
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()
        worktree = Path(str(attempt["worktree_path"]))
        (worktree / "app.py").write_text("def value():\n    return 'drifted'\n", encoding="utf-8")
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="git", lease_seconds=30, clock=lambda: 100,
            git_integration_runner=lambda value: ctl.execute_git_integration_only(value, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "accepted candidate diff drift"):
            scheduler.process_next()
        self.assertIsNone(self.ledger.git_commit_intent(ticket))
        self.assertIsNone(self.ledger.git_commit_evidence(ticket))
        self.assertEqual(subprocess.run(("git", "rev-parse", "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(), attempt["base_sha"])

        # Restore the accepted tracked diff, then add content that the candidate fingerprint never bound.
        (worktree / "app.py").write_text("def value():\n    return 'ok'\n", encoding="utf-8")
        (worktree / "extra.py").write_text("x = 1\n", encoding="utf-8")
        replay = ProcessNextScheduler(
            self.ledger, Board(), worker_id="git-replay", lease_seconds=30, clock=lambda: 131,
            git_integration_runner=lambda value: ctl.execute_git_integration_only(value, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "accepted fingerprint does not bind untracked content"):
            replay.process_next()
        self.assertIsNone(self.ledger.git_commit_intent(ticket))
        self.assertIsNone(self.ledger.git_commit_evidence(ticket))

    def test_scheduler_git_integration_recovers_commit_created_before_ledger_update_without_second_commit(self) -> None:
        ctl, ticket, _ = self.prepare_scheduler_accepted()
        crashing = LocalFirstController(
            self.ledger,
            Board(),
            self.config,
            local_model=LifecycleModel(),
            fault_injector=lambda stage: (_ for _ in ()).throw(RuntimeError("simulated controller death")) if stage == "git_commit_created" else None,
        )
        first = ProcessNextScheduler(
            self.ledger, Board(), worker_id="git-a", lease_seconds=30, clock=lambda: 100,
            git_integration_runner=lambda value: crashing.execute_git_integration_only(value, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            first.process_next()
        intent = self.ledger.git_commit_intent(ticket)
        self.assertEqual(intent["status"], "started")
        self.assertIsNone(self.ledger.git_commit_evidence(ticket))
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()
        worktree = Path(str(attempt["worktree_path"]))
        commit_sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip()
        self.assertNotEqual(commit_sha, attempt["base_sha"])
        self.assertEqual(subprocess.run(("git", "rev-list", "--count", f"{attempt['base_sha']}..HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(), "1")

        resumed_ctl = LocalFirstController(self.ledger, Board(), self.config, local_model=LifecycleModel())
        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="git-b", lease_seconds=30, clock=lambda: 131,
            git_integration_runner=lambda value: resumed_ctl.execute_git_integration_only(value, repository=self.repo),
        )
        result = resumed.process_next()
        self.assertEqual(result.stage, "git_integration")
        self.assertEqual(self.ledger.git_commit_evidence(ticket)["commit_sha"], commit_sha)
        self.assertEqual(self.ledger.git_commit_intent(ticket)["status"], "completed")
        self.assertEqual(subprocess.run(("git", "rev-list", "--count", f"{attempt['base_sha']}..HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(), "1")

    def test_scheduler_git_integration_recovers_after_tranche_head_advanced(self) -> None:
        ctl, ticket, _ = self.prepare_scheduler_accepted(attach_tranche=True)
        accepted = self.ledger.accepted_candidate(ticket)
        ref = "refs/local-first/tranches/fixture-tranche/integration-head"
        self.assertEqual(self.git("rev-parse", ref).stdout.strip(), accepted["base_sha"])
        crashing = LocalFirstController(
            self.ledger,
            Board(),
            self.config,
            local_model=LifecycleModel(),
            fault_injector=lambda stage: (_ for _ in ()).throw(RuntimeError("simulated controller death")) if stage == "git_integration_head_advanced" else None,
        )
        first = ProcessNextScheduler(
            self.ledger, Board(), worker_id="git-a", lease_seconds=30, clock=lambda: 100,
            git_integration_runner=lambda value: crashing.execute_git_integration_only(value, repository=self.repo),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated controller death"):
            first.process_next()
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=1", (ticket,)).fetchone()
        worktree = Path(str(attempt["worktree_path"]))
        commit_sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual(self.git("rev-parse", ref).stdout.strip(), commit_sha)
        self.assertIsNone(self.ledger.git_commit_evidence(ticket))

        resumed_ctl = LocalFirstController(self.ledger, Board(), self.config, local_model=LifecycleModel())
        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="git-b", lease_seconds=30, clock=lambda: 131,
            git_integration_runner=lambda value: resumed_ctl.execute_git_integration_only(value, repository=self.repo),
        )
        self.assertEqual(resumed.process_next().stage, "git_integration")
        evidence = self.ledger.git_commit_evidence(ticket)
        self.assertEqual((evidence["integration_head_before"], evidence["integration_head_after"]), (accepted["base_sha"], commit_sha))
        self.assertEqual(self.git("rev-parse", ref).stdout.strip(), commit_sha)
        self.assertEqual(subprocess.run(("git", "rev-list", "--count", f"{accepted['base_sha']}..HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(), "1")

    def test_scheduler_git_integration_finalizes_after_effect_without_rerunning_git(self) -> None:
        ctl, ticket, _ = self.prepare_scheduler_accepted()
        claim = self.ledger.claim_next_scheduler_git_integration("crashed", lease_seconds=1, now=101)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed", now=101)
        git_result = ctl.execute_git_integration_only(ticket, repository=self.repo)
        applied = self.ledger.apply_scheduler_git_integration_effect(claim_id, "crashed", git_result, now=101)
        self.assertIsNotNone(applied["side_effect_completed_at"])
        self.assertIsNone(applied["finalized_at"])
        calls: list[str] = []
        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="git-recovery", lease_seconds=30, clock=lambda: 103,
            git_integration_runner=lambda value: calls.append(value) or (_ for _ in ()).throw(AssertionError("Git integration must not rerun")),
        )
        result = resumed.process_next()
        self.assertEqual((result.stage, result.status), ("git_integration", "completed"))
        self.assertEqual(calls, [])
        self.assertEqual(self.ledger.scheduler_claim(claim_id)["status"], "completed")

    def test_scheduler_git_integration_refuses_commit_without_durable_intent(self) -> None:
        _, ticket, _ = self.prepare_scheduler_accepted()
        claim = self.ledger.claim_next_scheduler_git_integration("operator", lease_seconds=30, now=100)
        assert claim is not None
        identity = json.loads(str(claim["candidate_identity_json"]))
        worktree = Path(str(identity["worktree_path"]))
        subprocess.run(("git", "add", "-A"), cwd=worktree, check=True, capture_output=True, text=True)
        subprocess.run(("git", "commit", "-m", str(identity["commit_message"])), cwd=worktree, check=True, capture_output=True, text=True)
        ctl = LocalFirstController(self.ledger, Board(), self.config, local_model=LifecycleModel())
        with self.assertRaisesRegex(RuntimeError, "commit exists without durable launch intent"):
            ctl.execute_git_integration_only(ticket, repository=self.repo)
        self.assertIsNone(self.ledger.git_commit_intent(ticket))
        self.assertIsNone(self.ledger.git_commit_evidence(ticket))

    def test_scheduler_repair_routing_finalizes_after_restart_without_duplicate_transition(self) -> None:
        model = SequencedLifecycleModel(["still-bad"]); ctl, ticket = self.controller(model)
        scheduler = ProcessNextScheduler(
            self.ledger, Board(), worker_id="scheduler", lease_seconds=30, clock=lambda: 100,
            implementation_runner=lambda value: ctl.execute_implementation_model_only(value, repository=self.repo),
            validation_runner=lambda value: ctl.execute_deterministic_validation_only(value, repository=self.repo),
        )
        self.assertEqual(scheduler.process_next().stage, "implementation")
        self.assertEqual(scheduler.process_next().stage, "validation")
        claim = self.ledger.claim_next_scheduler_repair_routing("crashed", lease_seconds=1, now=101)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed", now=101)
        applied = self.ledger.apply_scheduler_repair_routing_effect(claim_id, "crashed", now=101)
        self.assertIsNotNone(applied["side_effect_completed_at"])
        self.assertIsNone(applied["finalized_at"])
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "repairing")

        resumed = ProcessNextScheduler(self.ledger, Board(), worker_id="recovery", lease_seconds=30, clock=lambda: 103)
        final = resumed.process_next()
        self.assertEqual((final.stage, final.status, final.ticket_id), ("repair_routing", "completed", ticket))
        self.assertEqual(self.ledger.scheduler_claim(claim_id)["status"], "completed")
        transitions = [event for event in self.ledger.events_for(ticket) if event["event_type"] == "state_transition" and event["to_state"] == "repairing"]
        self.assertEqual(len(transitions), 1)

    def test_implementation_only_freezes_candidate_without_review_and_replays(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        self.ledger.pause("operator", reason="maintenance")
        other = self.ledger.create_ticket(title="other", state=CanonicalState.READY_LOCAL)
        self.assertFalse(self.ledger.claim_specific(other, "background", 60))
        self.assertEqual(self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()[0], 1)
        first = ctl.execute_implementation(ticket, repository=self.repo)
        assert first is not None
        self.assertFalse(first["replayed"])
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "local_review")
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.ledger.get_ticket(other)["state"], "ready_local")
        self.assertEqual(self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()[0], 1)
        self.assertEqual(self.ledger.review_invocations(ticket, 1), [])
        self.assertIsNotNone(self.ledger.model_stage(ticket, 1, "implementation"))
        self.assertIsNotNone(self.ledger.review_candidate(ticket))
        replay = ctl.execute_implementation(ticket, repository=self.repo)
        self.assertEqual(replay["candidate_fingerprint"], first["candidate_fingerprint"])
        self.assertEqual(replay["implementation_artifact"], first["implementation_artifact"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(model.calls, 1)
        self.assertIsNone(self.ledger.accepted_commit(ticket))

    def test_implementation_only_validation_failure_stops_before_review(self) -> None:
        model = InvalidLifecycleModel(); ctl, ticket = self.controller(model)
        result = ctl.execute_implementation(ticket, repository=self.repo)
        self.assertIsNone(result)
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "needs_triage")
        self.assertEqual(self.ledger.review_invocations(ticket, 1), [])
        self.assertIsNone(self.ledger.accepted_commit(ticket))

    def test_isolation_uses_canonical_provenance_separately_from_execution_base(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        canonical = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-qb", "integration")
        (self.repo / "app.py").write_text("def value():\n    return 'integrated'\n", encoding="utf-8")
        self.git("add", "."); self.git("commit", "-qm", "integrated base")
        execution = self.git("rev-parse", "HEAD").stdout.strip(); self.git("checkout", "main")
        self.ledger.connection.execute("UPDATE runtime_bindings SET starting_sha=?,canonical_sha=? WHERE ticket_id=?", (execution, canonical, ticket))
        result = ctl.execute_implementation(ticket, repository=self.repo)
        assert result is not None
        self.assertEqual(self.ledger.connection.execute("SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage='execution_base'", (ticket,)).fetchone()[0], execution)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "local_review")

    def test_isolation_rejects_genuine_canonical_checkout_movement(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        self.git("commit", "--allow-empty", "-qm", "unexpected canonical movement")
        with self.assertRaisesRegex(RuntimeError, "canonical_head_moved_since_admission"):
            ctl.execute_implementation(ticket, repository=self.repo)
        self.assertEqual(model.calls, 0)

    def test_implementation_only_restart_recovers_same_candidate(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        first = ctl.execute_implementation(ticket, repository=self.repo)
        assert first is not None
        self.ledger.close(); self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate()
        resumed = LocalFirstController(self.ledger, Board(), self.config, local_model=model)
        replay = resumed.execute_implementation(ticket, repository=self.repo)
        self.assertEqual(replay["candidate_fingerprint"], first["candidate_fingerprint"])
        self.assertEqual(model.calls, 1)


if __name__ == "__main__":
    unittest.main()
