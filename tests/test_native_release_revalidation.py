from __future__ import annotations

import json
import subprocess
import unittest
import hashlib
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.hermes_board import ExternalExecutionRun, ExternalExecutionSnapshot, ExternalTicket
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.native_release_approval import canonical_approval_bytes, fingerprint_public_key, parse_approval_document


class ReadOnlyBoard:
    is_fake = False

    def __init__(self, snapshot: ExternalExecutionSnapshot):
        self.snapshot = snapshot
        self.calls = 0

    def execution_snapshot(self, task_id: str) -> ExternalExecutionSnapshot:
        self.calls += 1
        return self.snapshot


class RaceBoard(ReadOnlyBoard):
    def __init__(self, snapshot: ExternalExecutionSnapshot, *, mutate_on: int | None = None, failure_on: int | None = None):
        super().__init__(snapshot)
        self.mutate_on = mutate_on
        self.failure_on = failure_on

    def execution_snapshot(self, task_id: str) -> ExternalExecutionSnapshot:
        self.calls += 1
        if self.failure_on == self.calls:
            raise RuntimeError("fixture snapshot read failure")
        if self.mutate_on == self.calls:
            self.snapshot = ExternalExecutionSnapshot(
                task=self.snapshot.task.__class__(self.snapshot.task.id, self.snapshot.task.title, self.snapshot.task.body, "running", self.snapshot.task.workspace_path, assignee=self.snapshot.task.assignee, workspace_kind=self.snapshot.task.workspace_kind),
                session_id="race-session", branch_name=self.snapshot.branch_name, started_at=2, completed_at=None,
                runs=self.snapshot.runs, repository_identity=self.snapshot.repository_identity, base_sha=self.snapshot.base_sha,
            )
        return self.snapshot


class NativeReleaseRevalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.repo, check=True)
        (self.repo / "README").write_text("fixture\n")
        subprocess.run(("git", "add", "README"), cwd=self.repo, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"), cwd=self.repo, check=True)
        self.base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, check=True, text=True, capture_output=True).stdout.strip()
        self.worktree = self.repo / ".worktrees" / "TK-1"
        subprocess.run(("git", "worktree", "add", "-q", "-b", "local-first/TK-1/maintenance", str(self.worktree), self.base), cwd=self.repo, check=True)
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.signing_key = Ed25519PrivateKey.generate()
        self.signer_public_key = self.signing_key.public_key().public_bytes_raw()
        self.signer_fingerprint = fingerprint_public_key(self.signer_public_key)
        self.ticket = self.ledger.create_ticket(title="TK-1", state=CanonicalState.DRAFT, external_id="card-1")
        self.ledger.connection.execute("UPDATE tickets SET dependencies_json='[]' WHERE id=?", (self.ticket,))
        authority_hash = hashlib.sha256(self.signer_fingerprint.encode("ascii")).hexdigest()
        self.ledger.bind_runtime(self.ticket, str(self.repo), self.base, operator_signer_fingerprint=self.signer_fingerprint, operator_authority_hash=authority_hash)
        with self.ledger._transaction() as conn:
            event = self.ledger._append_event(conn, entity_type="ticket", entity_id=self.ticket, event_type="generated_microticket_created", actor_id="test", to_state="draft", payload={"ticket_id": self.ticket})
            conn.execute("INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,acknowledged_at,external_task_id,operation) VALUES (?,?,?,?,?,1,1,'card-1','create_microticket')", (self.ticket, event, "draft", "{}", "legacy-key"))
            conn.execute("INSERT INTO native_dependency_graphs(ticket_id,child_external_id,local_dependency_ids_json,parent_external_ids_json,graph_hash,verified_at) VALUES (?,?,?,?,?,1)", (self.ticket, "card-1", "[]", "[]", "graph-hash"))
            conn.execute("INSERT INTO native_dependency_releases(ticket_id,graph_hash,child_external_id,parent_completion_hash,routing_authority_json,hermes_status,observed_at) VALUES (?,?,?,?,?,?,1)", (self.ticket, "graph-hash", "card-1", "parents", "{}", "ready"))
        self.ledger.pause("operator", reason="maintenance")
        self.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("card-1", "TK-1", "legacy", "scheduled", str(self.worktree), assignee="impl", workspace_kind="worktree"),
            session_id=None, branch_name="local-first/TK-1/maintenance", started_at=None, completed_at=None,
            runs=(), repository_identity=str(self.repo), base_sha=self.base,
        )
        self.board = ReadOnlyBoard(self.snapshot)
        self.controller = LocalFirstController(self.ledger, self.board, RuntimeConfig(self.repo, self.root / "unused-worktrees", self.root / "artifacts", (self.repo,), operator_signer_fingerprint=self.signer_fingerprint, operator_authority_hash=authority_hash, operator_signer_public_key=self.signer_public_key))

    def signed_revalidate(self, *, reason: str = "verify legacy evidence") -> dict[str, object]:
        if not hasattr(self, "approval_cache"):
            self.approval_cache = {}
        if reason in self.approval_cache:
            document, signature = self.approval_cache[reason]
            return self.controller.revalidate_native_release(self.ticket, operator_id="operator", reason=reason, implementation_profile="impl", approval_document=document, detached_signature=signature, signer_public_key=self.signer_public_key, signer_fingerprint=self.signer_fingerprint)
        prepared = self.controller.prepare_native_release_revalidation(self.ticket, operator_id="operator", reason=reason, implementation_profile="impl")
        document = parse_approval_document(prepared["canonical_document"])
        canonical = canonical_approval_bytes(document)
        signature = self.signing_key.sign(canonical)
        self.approval_cache[reason] = (document, signature)
        return self.controller.revalidate_native_release(self.ticket, operator_id="operator", reason=reason, implementation_profile="impl", approval_document=document, detached_signature=signature, signer_public_key=self.signer_public_key, signer_fingerprint=self.signer_fingerprint)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_direct_controller_omission_is_rejected_before_mutation(self) -> None:
        with self.assertRaisesRegex((PermissionError, ValueError), "(signer|approval|authority)"):
            self.controller.revalidate_native_release(self.ticket, operator_id="operator", reason="verify legacy evidence", implementation_profile="impl")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='native_dependency_release_revalidated'").fetchone()[0], 0)

    def test_success_appends_revalidation_and_controller_event_without_mutating_legacy_release(self) -> None:
        result = self.signed_revalidate()
        self.assertEqual(result["ticket_id"], self.ticket)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("SELECT routing_authority_json FROM native_dependency_releases WHERE ticket_id=?", (self.ticket,)).fetchone()[0], "{}")
        self.assertEqual(self.ledger.connection.execute("SELECT event_type FROM events WHERE entity_type='controller' ORDER BY id DESC LIMIT 1").fetchone()[0], "native_dependency_release_revalidated")

    def test_race_after_initial_record_read_stops_before_any_mutation(self) -> None:
        self.board = RaceBoard(self.snapshot, mutate_on=3)
        self.controller.board = self.board
        with self.assertRaisesRegex(RuntimeError, "snapshot|execution evidence|drift"):
            self.signed_revalidate()
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='native_dependency_release_revalidated'").fetchone()[0], 0)

    def test_snapshot_read_error_before_insert_rolls_back(self) -> None:
        self.board = RaceBoard(self.snapshot, failure_on=3)
        self.controller.board = self.board
        with self.assertRaisesRegex(RuntimeError, "snapshot read failure"):
            self.signed_revalidate()
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='native_dependency_release_revalidated'").fetchone()[0], 0)

    def test_post_commit_snapshot_drift_is_reported_fail_closed(self) -> None:
        self.board = RaceBoard(self.snapshot, mutate_on=5)
        self.controller.board = self.board
        with self.assertRaisesRegex(RuntimeError, "post-commit|current snapshot|drift"):
            self.signed_revalidate()
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='native_dependency_release_revalidated'").fetchone()[0], 1)

    def test_replay_is_idempotent_only_for_exact_request(self) -> None:
        first = self.signed_revalidate()
        second = self.signed_revalidate()
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "revalidation replay conflicts"):
            self.signed_revalidate(reason="changed")

    def test_rejects_execution_evidence_and_allows_spawn_failed_without_worker(self) -> None:
        self.board.snapshot = ExternalExecutionSnapshot(
            task=self.snapshot.task, session_id=None, branch_name=self.snapshot.branch_name,
            started_at=None, completed_at=None,
            runs=(ExternalExecutionRun(1, "completed", "completed", 1, 2, "done", None, None),),
            repository_identity=str(self.repo), base_sha=self.base,
        )
        with self.assertRaisesRegex(RuntimeError, "execution evidence"):
            self.signed_revalidate(reason="verify")
        self.board.snapshot = ExternalExecutionSnapshot(
            task=self.snapshot.task, session_id=None, branch_name=self.snapshot.branch_name,
            started_at=None, completed_at=None,
            runs=(ExternalExecutionRun(2, "spawn_failed", "spawn_failed", None, None, "could not spawn", None, None),),
            repository_identity=str(self.repo), base_sha=self.base,
        )
        self.signed_revalidate(reason="verify")

    def test_unpaused_and_worktree_drift_rollback_without_event(self) -> None:
        self.ledger.resume("operator", reason="test")
        with self.assertRaisesRegex(PermissionError, "paused"):
            self.signed_revalidate(reason="verify")
        self.ledger.pause("operator", reason="test")
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("card-1", "TK-1", "legacy", "scheduled", str(self.root / "wrong"), assignee="impl", workspace_kind="worktree"),
            session_id=None, branch_name=self.snapshot.branch_name, started_at=None, completed_at=None,
            runs=(), repository_identity=str(self.repo), base_sha=self.base,
        )
        with self.assertRaisesRegex(RuntimeError, "worktree drift"):
            self.signed_revalidate(reason="verify")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='native_dependency_release_revalidated'").fetchone()[0], 0)

    def test_fault_injection_rolls_back_record_and_event_and_detector_clears_only_after_valid_row(self) -> None:
        self.assertIsNotNone(self.ledger.native_dependency_release_migration_required())
        self.ledger.failure_injector = lambda point: (_ for _ in ()).throw(RuntimeError(point)) if point == "after_native_release_revalidation" else None
        with self.assertRaisesRegex(RuntimeError, "after_native_release_revalidation"):
            self.signed_revalidate(reason="verify")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='native_dependency_release_revalidated'").fetchone()[0], 0)
        self.ledger.failure_injector = None
        self.signed_revalidate(reason="verify")
        self.assertIsNone(self.ledger.native_dependency_release_migration_required(signer_public_key=self.signer_public_key, signer_fingerprint=self.signer_fingerprint, freshness_guard=lambda ticket_id: None))

    def test_revalidation_table_is_immutable(self) -> None:
        row = self.signed_revalidate(reason="verify")
        with self.assertRaisesRegex(Exception, "append-only"):
            self.ledger.connection.execute("UPDATE native_dependency_release_revalidations SET reason='tampered' WHERE revalidation_id=?", (row["revalidation_id"],))
        with self.assertRaisesRegex(Exception, "append-only"):
            self.ledger.connection.execute("DELETE FROM native_dependency_release_revalidations WHERE revalidation_id=?", (row["revalidation_id"],))
        with self.assertRaisesRegex(Exception, "immutable"):
            self.ledger.connection.execute("UPDATE events SET payload_json='{}' WHERE id=?", (row["revalidation_event_id"],))

    def test_direct_sql_valid_length_forgery_without_event_stays_reconciliation_required(self) -> None:
        self.ledger.connection.execute("""INSERT INTO native_dependency_release_revalidations
            (revalidation_id,ticket_id,release_graph_hash,release_child_external_id,release_parent_completion_hash,release_routing_authority_json,release_hermes_status,release_observed_at,projection_event_id,projection_key,external_task_id,implementation_profile,repository_identity,canonical_worktree_path,branch,base_sha,snapshot_hash,operator_id,reason,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("forged", self.ticket, "graph-hash", "card-1", "parents", "{}", "ready", 1, 999999, "legacy-key", "card-1", "impl", str(self.repo), str(self.root / "outside"), "forged", "a" * 40, "b" * 64, "operator", "forged", 1))
        result = self.ledger.native_dependency_release_migration_required()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["ticket_id"], self.ticket)

    def test_fake_event_does_not_authorize_forged_revalidation(self) -> None:
        with self.ledger._transaction() as conn:
            event_id = self.ledger._append_event(conn, entity_type="controller", entity_id="controller", event_type="native_dependency_release_revalidated", actor_id="forger", payload={"ticket_id": self.ticket, "revalidation_id": "forged"})
            self.ledger.connection.execute("""INSERT INTO native_dependency_release_revalidations
                (revalidation_id,ticket_id,release_graph_hash,release_child_external_id,release_parent_completion_hash,release_routing_authority_json,release_hermes_status,release_observed_at,projection_event_id,projection_key,external_task_id,implementation_profile,repository_identity,canonical_worktree_path,branch,base_sha,snapshot_hash,operator_id,reason,created_at,revalidation_event_id,event_key,evidence_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("forged", self.ticket, "graph-hash", "card-1", "parents", "{}", "ready", 1, 999999, "legacy-key", "card-1", "impl", str(self.repo), str(self.root / "outside"), "forged", "a" * 40, "b" * 64, "operator", "forged", 1, event_id, "native-release-revalidated:forged", "c" * 64))
        result = self.ledger.native_dependency_release_migration_required()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["ticket_id"], self.ticket)


if __name__ == "__main__":
    unittest.main()
