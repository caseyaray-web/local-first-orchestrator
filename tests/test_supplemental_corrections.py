from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from local_first_orchestrator.controller import RuntimeConfig
from local_first_orchestrator.generated_activation import activate_generated_ticket
from local_first_orchestrator.generated_projection import GeneratedProjectionWorker
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


class SupplementalCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "src").mkdir(); (self.repo / "scripts").mkdir()
        (self.repo / "src/client.py").write_text("def request(): return None\n")
        (self.repo / "scripts/test-contract.mjs").write_text("console.log('base')\n")
        self.git("add", "."); self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate()
        # Per-ticket accepted-commit map so multi-predecessor specs reference
        # each predecessor's real integrated commit, not the planning root.
        self.accepted_map: dict[str, str] = {}
        self.accepted = ""
        self.seed_accepted("A")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()

    def seed_accepted(self, ticket_id):
        now = self.ledger._now()
        with self.ledger._transaction() as c:
            c.execute("INSERT OR IGNORE INTO features(id,title,objective,status,created_at,updated_at) VALUES ('F','feature','objective','planned',?,?)", (now, now))
            c.execute("INSERT OR IGNORE INTO tranches(id,feature_id,ordinal,status,base_sha,integration_commands_json) VALUES ('T','F',0,'active',?,'[]')", (self.base,))
            c.execute("INSERT INTO tickets(id,feature_id,tranche_id,title,objective,criterion_ids_json,primary_symbol,allowed_files_json,new_test_files_json,forbidden_changes_json,patch_budget_json,verification_json,risk,review_required,max_attempts,dependencies_json,state,created_at,updated_at) VALUES (?,?, 'T',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                ticket_id, 'F', ticket_id, 'accepted predecessor', '["AC"]', 'src/client.py::request', '["scripts/test-contract.mjs"]', '[]', '["src/**"]', '{"max_files":1,"max_changed_lines":20}', '{"commands":[["true"]],"working_directory":"."}', 'low', 1, 1, '[]', 'done', now, now))
        # Accepted evidence must be an actual integrated child commit: the
        # production completion proof intentionally rejects root commits.
        self.git("commit", "--allow-empty", "-m", f"accepted {ticket_id}")
        accepted_commit = self.git("rev-parse", "HEAD")
        self.accepted_map[ticket_id] = accepted_commit
        self.ledger.record_accepted_evidence(ticket_id, accepted_commit, "accepted", "validated")
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", accepted_commit)

    def spec(self, finding="auth-header-gap", predecessors=("A",)):
        from local_first_orchestrator.corrections import AcceptedPredecessor, CorrectionTicketSpec, SupplementalCorrectionPlan
        return SupplementalCorrectionPlan(
            feature_id="F", tranche_id="T", source_kind="operator_review", source_reference="review-1",
            finding_fingerprint=finding, finding_summary="wire headers are not asserted",
            predecessors=tuple(AcceptedPredecessor(ticket_id, self.accepted_map[ticket_id]) for ticket_id in predecessors),
            tickets=self._tickets(),
            repository_identity=str(self.repo), base_sha=self.base, snapshot_hash="snapshot")

    def _tickets(self):
        from local_first_orchestrator.corrections import CorrectionTicketSpec
        return (CorrectionTicketSpec(
                objective="Capture and assert request authorization headers.", criterion_ids=("AC",),
                primary_symbol="src/client.py::request", allowed_existing_files=("scripts/test-contract.mjs",),
                new_test_files=(), forbidden_changes=("src/**",),
                patch_budget=PatchBudget(max_files=1, max_changed_lines=20),
                verification=VerificationProfile((("true",),)), risk="low", review_required=True,
                max_attempts=1, dependencies=(), relevant_symbols=("request",),
                acceptance_criteria=("authorization is captured",), non_goals=("no production changes",),
                red_evidence="new header assertion fails before capture"),)

    def multi_ticket_spec(self, finding="multi-finding"):
        from local_first_orchestrator.corrections import AcceptedPredecessor, CorrectionTicketSpec, SupplementalCorrectionPlan
        first = self._tickets()[0]
        second = CorrectionTicketSpec(
            objective="Assert preview and undo wire requests carry the header too.", criterion_ids=("AC",),
            primary_symbol="src/client.py::request", allowed_existing_files=("scripts/test-contract.mjs",),
            new_test_files=(), forbidden_changes=("src/**",),
            patch_budget=PatchBudget(max_files=1, max_changed_lines=20),
            verification=VerificationProfile((("true",),)), risk="low", review_required=True,
            max_attempts=1, dependencies=("candidate-1",), relevant_symbols=("request",),
            acceptance_criteria=("preview/undo assert authorization too",), non_goals=("no production changes",),
            red_evidence="second assertion fails before first ticket lands")
        return SupplementalCorrectionPlan(
            feature_id="F", tranche_id="T", source_kind="tranche_review", source_reference="review-2",
            finding_fingerprint=finding, finding_summary="multi-ticket review finding",
            predecessors=(AcceptedPredecessor("A", self.accepted_map["A"]),),
            tickets=(first, second),
            repository_identity=str(self.repo), base_sha=self.base, snapshot_hash="snapshot")

    def service(self):
        from local_first_orchestrator.corrections import CorrectionService
        return CorrectionService(self.ledger, self.repo)

    def test_single_accepted_predecessor_materializes_once_without_mutating_original(self):
        before_ticket = self.ledger.get_ticket("A")
        before_evidence = self.ledger.accepted_commit("A")
        plan = self.service().create_plan(self.spec())
        ticket = self.service().materialize(plan.correction_plan_id)
        replay = self.service().materialize(plan.correction_plan_id)
        self.assertEqual(ticket.ticket_id, replay.ticket_id)
        self.assertEqual(ticket.ticket_id, "T-F1")
        self.assertEqual(self.ledger.get_ticket("A"), before_ticket)
        self.assertEqual(self.ledger.accepted_commit("A"), before_evidence)
        self.assertEqual(self.ledger.get_ticket(ticket.ticket_id)["state"], "draft")
        predecessor = self.ledger.connection.execute("SELECT ticket_id,accepted_commit_sha FROM correction_ticket_predecessors WHERE correction_ticket_id=?", (ticket.ticket_id,)).fetchone()
        self.assertEqual(tuple(predecessor), ("A", self.accepted_map["A"]))
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM board_projection_outbox WHERE ticket_id=? AND operation='create_microticket'", (ticket.ticket_id,)).fetchone()[0], 1)

    def test_rejects_missing_accepted_evidence_and_nonintegrated_predecessor(self):
        self.ledger.connection.execute("DELETE FROM accepted_evidence WHERE ticket_id='A'")
        with self.assertRaises(ValueError): self.service().create_plan(self.spec())
        self.ledger.record_accepted_evidence("A", self.base, "accepted", "validated")
        self.git("checkout", "-b", "side", self.base)
        self.git("commit", "--allow-empty", "-m", "accepted but not integrated")
        side = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")
        self.ledger.record_accepted_evidence("A", side, "accepted", "validated")
        with self.assertRaises(ValueError): self.service().create_plan(self.spec("not-integrated"))

    def test_materialization_rebases_admission_on_current_integration_head(self):
        plan = self.service().create_plan(self.spec())
        self.git("commit", "--allow-empty", "-m", "later integrated sibling")
        head2 = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", head2)
        ticket = self.service().materialize(plan.correction_plan_id)
        admission = self.service().admission_base(ticket.ticket_id)
        self.assertEqual(admission, head2)

    def test_distinct_findings_get_distinct_ids_and_multiple_predecessors_are_required(self):
        self.seed_accepted("B")
        first = self.service().create_plan(self.spec("one", ("A", "B")))
        second = self.service().create_plan(self.spec("two", ("A",)))
        self.assertNotEqual(first.correction_plan_id, second.correction_plan_id)
        self.assertEqual(self.service().materialize(first.correction_plan_id).ticket_id, "T-F1")
        self.assertEqual(self.service().materialize(second.correction_plan_id).ticket_id, "T-F2")
        self.ledger.connection.execute("DELETE FROM accepted_evidence WHERE ticket_id='B'")
        with self.assertRaises(ValueError): self.service().admission_base("T-F1")

    def test_concurrent_plans_get_distinct_ordinal_ids(self):
        import threading
        errors: list[BaseException] = []

        def create(index: int) -> None:
            try:
                self.service().create_plan(self.spec(f"concurrent-{index}"))
            except BaseException as exc:  # pragma: no cover - recorded, not raised
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        rows = self.ledger.connection.execute("SELECT ordinal FROM supplemental_correction_plans ORDER BY ordinal").fetchall()
        self.assertEqual([row[0] for row in rows], [1, 2, 3, 4])
        plan_ids = {r[0] for r in self.ledger.connection.execute(
            "SELECT correction_plan_id FROM supplemental_correction_plans").fetchall()}
        self.assertEqual(len(plan_ids), 4)

    def test_projection_and_activation_use_current_lineage_without_unpausing(self):
        from types import SimpleNamespace
        from local_first_orchestrator.generated_projection import GeneratedProjectionWorker
        plan = self.service().create_plan(self.spec())
        self.ledger.pause("operator", reason="preserve pause")
        correction = self.service().materialize(plan.correction_plan_id)
        class Board:
            timeout_seconds = 1
            allow_writes = True
            def __init__(self): self.body = ""; self.created = []
            def create_microticket(self, title, body, *, idempotency_key):
                self.created.append((title, idempotency_key)); self.body = body; return "external-correction"
            def get_task(self, task_id): return SimpleNamespace(id=task_id, body=self.body)
        board = Board()
        delivered = GeneratedProjectionWorker(self.ledger, board, worker_id="test").deliver_one()
        self.assertEqual(delivered.ticket_id, correction.ticket_id)
        self.assertEqual(self.service().activate(correction.ticket_id), self.accepted_map["A"])
        self.assertEqual(self.ledger.get_ticket(correction.ticket_id)["state"], "ready_local")
        self.assertEqual(self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()[0], 1)

    def test_full_correction_generated_e2e_advances_head_twice_and_keeps_original_intact(self):
        """Requirement #25: accepted original child -> correction plan -> materialize ->
        generated board projection -> activation -> injected implementation -> validation
        -> review pass -> accepted correction commit -> integration head advances again,
        with the original accepted history byte-for-byte unchanged."""
        from types import SimpleNamespace

        from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
        from local_first_orchestrator.generated_projection import GeneratedProjectionWorker
        from tests.test_runtime_milestone2 import FakeBoard, FakeModel

        plan = self.service().create_plan(self.spec())
        correction = self.service().materialize(plan.correction_plan_id)
        original_ticket_before = self.ledger.get_ticket("A")
        original_evidence_before = self.ledger.accepted_commit("A")
        a_commit = original_evidence_before

        class Board:
            timeout_seconds = 1
            allow_writes = True

            def __init__(self):
                self.body = ""
                self.created = []

            def create_microticket(self, title, body, *, idempotency_key):
                self.created.append((title, idempotency_key))
                self.body = body
                return "external-correction-e2e"

            def get_task(self, task_id):
                return SimpleNamespace(id=task_id, body=self.body)

        board = Board()
        delivered = GeneratedProjectionWorker(self.ledger, board, worker_id="test").deliver_one()
        self.assertEqual(delivered.ticket_id, correction.ticket_id)
        head_at_admission = self.service().activate(correction.ticket_id)

        # Requirement #15: while the correction is open, the tranche's review
        # lifecycle shows unresolved higher-level work without rewriting the
        # historical "all original children accepted" facts.
        lifecycle_open = self.service().lifecycle_status("T")
        self.assertEqual(lifecycle_open["review_status"], "open_corrections", lifecycle_open)
        self.assertEqual(lifecycle_open["unresolved_corrections"], 1)
        self.assertEqual(lifecycle_open["plans"][0]["status"], "materialized_open")
        self.assertEqual(head_at_admission, a_commit)  # H1: the accepted original child

        def implement_correction(path):
            f = path / "scripts" / "test-contract.mjs"
            base_text = f.read_text() if f.exists() else ""
            (path / "scripts" / "test-contract.mjs").write_text(
                "// preview/apply/undo each assert Authorization === AUTHED\nAUTHED='Authorization: AUTHED';\n" + base_text)

        config = RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", repository_allowlist=(self.repo,))

        class CriterionPassModel(FakeModel):
            def __init__(self, actions, criteria):
                super().__init__(actions)
                self._criteria = tuple(criteria)

            def invoke(self, purpose, packet, *, artifact_dir, workdir=None):
                self.calls.append((purpose, packet, Path(workdir) if workdir is not None else None))
                if purpose == "implementation":
                    self.actions.pop(0)(Path(workdir)); payload = {}
                else:
                    payload = {"verdict": "pass",
                               "criterion_results": [{"criterion_id": c, "status": "pass", "evidence": "verified"} for c in self._criteria],
                               "findings": [], "suggestions": []}
                return type("Result", (), {"payload": payload})()

        criteria = tuple(json.loads(self.ledger.get_ticket(correction.ticket_id)["criterion_ids_json"]))
        controller = LocalFirstController(self.ledger, FakeBoard(), config, local_model=CriterionPassModel([implement_correction], criteria))
        self.assertTrue(controller.execute(correction.ticket_id, repository=self.repo, allow_board_writes=True))

        correction_commit = self.ledger.accepted_commit(correction.ticket_id)
        self.assertIsNotNone(correction_commit)
        # Integration head advanced again to the accepted correction commit (H2).
        new_head = self.git("rev-parse", "--verify", "refs/local-first/tranches/T/integration-head")
        self.assertEqual(new_head, correction_commit)
        # Correction child serializes on top of the original accepted commit.
        self.assertEqual(self.git("rev-parse", f"{correction_commit}^"), a_commit)
        # Original C09-style history is byte/logically unchanged.
        self.assertEqual(self.ledger.get_ticket("A"), original_ticket_before)
        self.assertEqual(self.ledger.accepted_commit("A"), original_evidence_before)
        self.assertEqual(new_head, correction_commit)

        # Requirement #15 re-check: after the correction integrates, the review
        # lifecycle reports no unresolved work and completion evidence still
        # covers every accepted child (original + correction) in order.
        from local_first_orchestrator.tranche_completion import completion_evidence
        assert new_head is not None and correction_commit is not None
        evidence = completion_evidence(self.ledger, self.repo, "T")
        self.assertEqual(evidence["accepted_ticket_ids"], ["A", correction.ticket_id])
        self.assertEqual(evidence["final_integration_sha"], new_head)
        lifecycle_after = self.service().lifecycle_status("T")
        self.assertEqual(lifecycle_after["review_status"], "ready_for_recheck", lifecycle_after)
        self.assertEqual(lifecycle_after["unresolved_corrections"], 0)
        self.assertEqual(lifecycle_after["plans"][0]["status"], "accepted")

    def test_multi_ticket_correction_preserves_dag_and_real_dependency_ids(self):
        plan = self.service().create_plan(self.multi_ticket_spec())
        materialized = self.service().materialize(plan.correction_plan_id)
        first, second = "T-F1-1", "T-F1-2"
        self.assertEqual(materialized.ticket_id, first)
        deps = json.loads(self.ledger.get_ticket(second)["dependencies_json"])
        self.assertEqual(deps, [first], "sibling dependency must remap to the real correction ticket ID")
        # Sibling DAG ordering: second stays gated until first is accepted/integrated.
        with self.ledger._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO runtime_bindings(ticket_id,repository_path,starting_sha,ownership_verified,created_at) VALUES (?,?,?,1,?)", (second, str(self.repo), self.base, self.ledger._now()))
        ready = self.ledger.evaluate_ticket_readiness(second)
        self.assertEqual(ready.status, "waiting_on_dependencies", ready.unresolved_dependency_ids)
        with self.ledger._transaction() as conn:
            conn.execute("UPDATE tickets SET state='done' WHERE id=?", (first,))
        self.ledger.record_accepted_evidence(first, self.base, "accepted", "validated")
        ready = self.ledger.evaluate_ticket_readiness(second)
        self.assertEqual(ready.status, "ready", ready.unresolved_dependency_ids)

    def _runtime_config(self):
        return RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", repository_allowlist=(self.repo,))

    def delivered_correction(self):
        plan = self.service().create_plan(self.spec())
        correction = self.service().materialize(plan.correction_plan_id)

        class Board:
            timeout_seconds = 1
            allow_writes = True
            def __init__(self): self.body = ""
            def create_microticket(self, title, body, *, idempotency_key): self.body = body; return "external-correction"
            def get_task(self, task_id): return SimpleNamespace(id=task_id, body=self.body)

        delivered = GeneratedProjectionWorker(self.ledger, Board(), worker_id="test").deliver_one()
        self.assertEqual(delivered.ticket_id, correction.ticket_id)
        return correction.ticket_id

    def test_public_generated_activation_uses_correction_current_head_and_replays(self):
        ticket_id = self.delivered_correction()
        self.git("commit", "--allow-empty", "-m", "later integrated sibling")
        head2 = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", head2)
        first = activate_generated_ticket(ticket_id, self._runtime_config(), self.ledger)
        replay = activate_generated_ticket(ticket_id, self._runtime_config(), self.ledger)
        self.assertEqual((first.status, first.starting_sha), ("activated_ready", head2))
        self.assertEqual((replay.status, replay.starting_sha), ("already_activated", head2))
        self.assertEqual(self.ledger.runtime_binding(ticket_id)["starting_sha"], head2)

    def test_correction_activation_refuses_superseded_projection_until_replacement_is_delivered(self):
        ticket_id = self.delivered_correction()
        event_id = self.ledger.connection.execute(
            "SELECT event_id FROM board_projection_outbox WHERE ticket_id=? AND operation='create_microticket'",
            (ticket_id,),
        ).fetchone()[0]
        self.ledger.pause("operator", reason="pre-native recovery")
        self.ledger.recover_generated_projection(
            ticket_id=ticket_id, superseded_event_id=event_id,
            superseded_external_task_id="external-correction", observed_status="blocked",
            observed_snapshot_hash="a" * 64, operator_id="casey", reason="replace stale correction projection",
        )
        with self.assertRaisesRegex(ValueError, "incomplete"):
            activate_generated_ticket(ticket_id, self._runtime_config(), self.ledger)
        self.assertEqual(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)
        ).fetchone()[0], 0)

    def test_public_generated_activation_rejects_missing_predecessor_ancestry(self):
        ticket_id = self.delivered_correction()
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", self.base)
        with self.assertRaises(ValueError):
            activate_generated_ticket(ticket_id, self._runtime_config(), self.ledger)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()[0], 0)

    def test_public_generated_activation_rejects_accepted_evidence_mismatch(self):
        ticket_id = self.delivered_correction()
        self.ledger.record_accepted_evidence("A", self.base, "accepted", "tampered")
        with self.assertRaises(ValueError):
            activate_generated_ticket(ticket_id, self._runtime_config(), self.ledger)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()[0], 0)

    def test_public_generated_activation_rejects_wrong_existing_correction_binding(self):
        ticket_id = self.delivered_correction()
        self.ledger.bind_runtime(ticket_id, str(self.repo), self.base)
        with self.assertRaises(ValueError):
            activate_generated_ticket(ticket_id, self._runtime_config(), self.ledger)
        self.assertEqual(self.ledger.runtime_binding(ticket_id)["starting_sha"], self.base)

    def test_public_generated_activation_rejects_missing_or_ambiguous_correction_projection(self):
        plan = self.service().create_plan(self.spec())
        ticket_id = self.service().materialize(plan.correction_plan_id).ticket_id
        with self.assertRaisesRegex(ValueError, "incomplete"):
            activate_generated_ticket(ticket_id, self._runtime_config(), self.ledger)
        with self.ledger._transaction() as conn:
            conn.execute("INSERT INTO events(entity_type,entity_id,event_type,actor_type,actor_id,payload_json,created_at) VALUES ('ticket',?,'generated_microticket_created','controller','test','{}',?)", (ticket_id, self.ledger._now()))
        with self.assertRaises(ValueError):
            activate_generated_ticket(ticket_id, self._runtime_config(), self.ledger)

    def test_ordinary_plan_dependencies_remain_local_only(self):
        from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, FeatureContract, PlanValidator, Tranche
        feature = FeatureContract("F2", "feature", "ordinary feature objective", (Criterion("AC", "criterion"),), (), (), (), "base")
        external = MicroTicket("C", "ordinary ticket objective", ("AC",), "src/client.py::request", ("scripts/test-contract.mjs",), ("src/**",), PatchBudget(1, 20), VerificationProfile((("true",),)), "low", True, 1, ("A",))
        plan = DecompositionPlan(1, "F2", feature.contract_hash, self.base, "s", (), {"C": ("AC",)}, (Tranche("T2", 0, "x", (), ("AC",), (external,)),), repository_identity=str(self.repo), repo_snapshot_manifest_json="{}")
        self.assertIn("invalid_dependency", PlanValidator().validate(feature, plan).reasons)

    def test_correction_projection_reconciles_after_restart_and_original_plan_lifecycle_advance(self):
        """Correction identity comes from its own durable plan, not an active plan join."""
        plan = self.service().create_plan(self.spec())
        correction = self.service().materialize(plan.correction_plan_id)
        event_id = self.ledger.connection.execute(
            "SELECT event_id FROM board_projection_outbox WHERE ticket_id=? AND operation='create_microticket'",
            (correction.ticket_id,),
        ).fetchone()[0]
        row = self.ledger.connection.execute(
            "SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (correction.ticket_id, event_id),
        ).fetchone()
        payload = json.loads(row[0])
        prefix, encoded = payload["body"].split("```local-first-contract\\n", 1)
        contract, suffix = encoded.split("\\n```", 1)
        contract = json.loads(contract)
        for field in ("repository_identity", "repo_base_sha", "repo_snapshot_hash"):
            contract.pop(field)
        payload["body"] = prefix + "```local-first-contract\\n" + json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\\n```" + suffix
        self.ledger.connection.execute("UPDATE board_projection_outbox SET payload_json=? WHERE ticket_id=? AND event_id=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), correction.ticket_id, event_id))

        # There is deliberately no active decomposition plan.  A later lifecycle
        # state must not make correction provenance disappear.
        self.ledger.close()
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        repaired = self.ledger.reconcile_generated_projection(correction.ticket_id, event_id)
        contract = json.loads(repaired["body"].split("```local-first-contract\\n", 1)[1].split("\\n```", 1)[0])
        self.assertEqual(contract["repository_identity"], str(self.repo))
        self.assertEqual(contract["repo_base_sha"], self.base)

    def test_correction_projection_missing_or_conflicting_provenance_fails_closed(self):
        plan = self.service().create_plan(self.spec())
        correction = self.service().materialize(plan.correction_plan_id)
        event_id = self.ledger.connection.execute("SELECT event_id FROM board_projection_outbox WHERE ticket_id=?", (correction.ticket_id,)).fetchone()[0]
        self.ledger.connection.execute("DELETE FROM supplemental_correction_tickets WHERE correction_plan_id=?", (plan.correction_plan_id,))
        with self.assertRaises(ValueError):
            self.ledger.reconcile_generated_projection(correction.ticket_id, event_id)

        # Rebuild this case independently so the conflict cannot be hidden by
        # the missing correction row.
        self.tearDown(); self.setUp()
        plan = self.service().create_plan(self.spec("conflict"))
        correction = self.service().materialize(plan.correction_plan_id)
        event_id = self.ledger.connection.execute("SELECT event_id FROM board_projection_outbox WHERE ticket_id=?", (correction.ticket_id,)).fetchone()[0]
        now = self.ledger._now()
        self.ledger.connection.execute(
            "INSERT INTO decomposition_plans(id,feature_id,fingerprint,plan_json,status,created_at,repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("normal-conflict", "F", "normal-conflict", "{}", "active", now, "other-repository", self.base, "other-snapshot", "{}"),
        )
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.ledger.reconcile_generated_projection(correction.ticket_id, event_id)

    def test_correction_replay_keeps_acknowledged_external_identity_without_duplicate_card(self):
        from types import SimpleNamespace
        from local_first_orchestrator.generated_projection import GeneratedProjectionWorker
        plan = self.service().create_plan(self.spec("replay"))
        correction = self.service().materialize(plan.correction_plan_id)

        class Board:
            timeout_seconds = 1
            allow_writes = True
            def __init__(self): self.calls = 0; self.body = ""
            def create_microticket(self, title, body, *, idempotency_key):
                self.calls += 1; self.body = body; return "external-correction-replay"
            def get_task(self, task_id): return SimpleNamespace(id=task_id, body=self.body)

        board = Board()
        first = GeneratedProjectionWorker(self.ledger, board, worker_id="first").deliver_one()
        self.assertEqual(first.external_task_id, "external-correction-replay")
        self.ledger.close(); self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate()
        second = GeneratedProjectionWorker(self.ledger, board, worker_id="restart").deliver_one()
        self.assertEqual(second.status, "no_work")
        self.assertEqual(board.calls, 1)
        self.assertEqual(self.ledger.resolve_external_task_id(correction.ticket_id), "external-correction-replay")


if __name__ == "__main__":
    unittest.main()
