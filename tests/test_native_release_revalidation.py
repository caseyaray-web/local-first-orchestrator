from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hermes_cli.sqlite_util import open_db

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.hermes_board import HermesBoardAdapter, ExternalExecutionSnapshot
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.native_release_approval import (
    canonical_approval_bytes,
    fingerprint_public_key,
    parse_approval_document,
)


class _TestHermesBoardAdapter(HermesBoardAdapter):
    """Real adapter with narrowly-scoped, locked snapshot fault seams."""

    def __init__(self, *args, fault_on: int | None = None, drift_on: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.snapshot_reads = 0
        self.fault_on = fault_on
        self.drift_on = drift_on
        self.repository_identity: str | None = None
        self.base_sha: str | None = None

    def _snapshot_from_connection(self, connection: sqlite3.Connection, task_id: str) -> ExternalExecutionSnapshot:
        self.snapshot_reads += 1
        if self.fault_on == self.snapshot_reads:
            raise RuntimeError("fixture snapshot read failure")
        snapshot = HermesBoardAdapter._snapshot_from_connection(connection, task_id)
        snapshot = replace(
            snapshot,
            task=replace(snapshot.task, repository_identity=self.repository_identity, base_sha=self.base_sha),
            repository_identity=self.repository_identity,
            base_sha=self.base_sha,
        )
        if self.drift_on == self.snapshot_reads:
            snapshot = replace(snapshot, session_id="race-session", started_at=2)
        return snapshot


class NativeReleaseRevalidationTests(unittest.TestCase):
    _PARENT_IDENTITY_KEYS = (
        "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_GOAL_MODE",
        "HERMES_KANBAN_GOAL_MAX_TURNS",
    )

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.board_name = "native-release-test"
        self.board_db = self.root / "kanban-test.db"
        self._parent_identity = {key: os.environ.get(key) for key in self._PARENT_IDENTITY_KEYS}
        self.assertEqual(self.board_db.parent, self.root)
        self.assertEqual(self.board_db.name, "kanban-test.db")
        self.assertFalse(self.board_db.exists())
        self.assertFalse(self.board_db.is_symlink())
        initializer = (
            "from pathlib import Path; "
            "import os, sys; "
            "assert not os.getenv('HERMES_DELEGATED_CHILD_CONTEXT'); "
            "assert not any(key.startswith('HERMES_KANBAN_') for key in os.environ); "
            "path = Path(sys.argv[1]); "
            "assert path.name == 'kanban-test.db'; "
            "assert path.parent == Path.cwd(); "
            "assert not path.exists() and not path.is_symlink(); "
            "from hermes_cli.kanban_db_connect import init_db; "
            "init_db(path, board='native-release-test')"
        )
        result = subprocess.run(
            (sys.executable, "-c", initializer, str(self.board_db)),
            cwd=self.root,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1"},
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            self.fail(f"Hermes test DB initialization failed: {detail}")
        self.assertEqual(self.board_db.parent, self.root)
        self.assertEqual(self.board_db.name, "kanban-test.db")
        self.assertTrue(self.board_db.is_file())
        self.assertFalse(self.board_db.is_symlink())
        self._assert_parent_identity_unchanged()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.repo, check=True)
        (self.repo / "README").write_text("fixture\n")
        subprocess.run(("git", "add", "README"), cwd=self.repo, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"), cwd=self.repo, check=True)
        self.base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, check=True, text=True, capture_output=True).stdout.strip()
        self.worktree = self.repo / ".worktrees" / "TK-1"
        subprocess.run(("git", "worktree", "add", "-q", "-b", "local-first/TK-1/maintenance", str(self.worktree), self.base), cwd=self.repo, check=True)

        self.signing_key = Ed25519PrivateKey.generate()
        self.signer_public_key = self.signing_key.public_key().public_bytes_raw()
        self.signer_fingerprint = fingerprint_public_key(self.signer_public_key)
        self.adapter = _TestHermesBoardAdapter(
            board=self.board_name,
            executable=sys.executable,
            board_db_path=self.board_db,
            canonical_repository=self.repo,
        )
        self.adapter.repository_identity = str(self.repo)
        self.adapter.base_sha = self.base
        with self._open_board() as connection:
            self.external_id = "TK-1"
            connection.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, created_at, workspace_kind, workspace_path, branch_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.external_id, "TK-1", "legacy native release", "impl", "blocked", 0, "test", int(time.time()), "worktree", str(self.worktree), "local-first/TK-1/maintenance"),
            )
            connection.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (self.external_id, "created", '{"assignee":"impl","status":"blocked"}', int(time.time())),
            )
            connection.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (self.external_id, "blocked", '{"reason":"initial_status","status":"blocked","actor":"test"}', int(time.time())),
            )
            connection.commit()

        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ticket = self.ledger.create_ticket(title="TK-1", state=CanonicalState.DRAFT, external_id=self.external_id)
        self.ledger.connection.execute("UPDATE tickets SET dependencies_json='[]' WHERE id=?", (self.ticket,))
        authority_hash = hashlib.sha256(self.signer_fingerprint.encode("ascii")).hexdigest()
        self.ledger.bind_runtime(self.ticket, str(self.repo), self.base, operator_signer_fingerprint=self.signer_fingerprint, operator_authority_hash=authority_hash)
        with self.ledger._transaction() as conn:
            event = self.ledger._append_event(conn, entity_type="ticket", entity_id=self.ticket, event_type="generated_microticket_created", actor_id="test", to_state="draft", payload={"ticket_id": self.ticket})
            conn.execute("INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,acknowledged_at,external_task_id,operation) VALUES (?,?,?,?,?,1,1,?,'create_microticket')", (self.ticket, event, "draft", "{}", "legacy-key", self.external_id))
            conn.execute("INSERT INTO native_dependency_graphs(ticket_id,child_external_id,local_dependency_ids_json,parent_external_ids_json,graph_hash,verified_at) VALUES (?,?,?,?,?,1)", (self.ticket, self.external_id, "[]", "[]", "graph-hash"))
            conn.execute("INSERT INTO native_dependency_releases(ticket_id,graph_hash,child_external_id,parent_completion_hash,routing_authority_json,hermes_status,observed_at) VALUES (?,?,?,?,?,?,1)", (self.ticket, "graph-hash", self.external_id, "parents", "{}", "ready"))
        self.ledger.pause("operator", reason="maintenance")
        self.controller = LocalFirstController(self.ledger, self.adapter, RuntimeConfig(self.repo, self.root / "unused-worktrees", self.root / "artifacts", (self.repo,), operator_signer_fingerprint=self.signer_fingerprint, operator_authority_hash=authority_hash, operator_signer_public_key=self.signer_public_key))
        self.approval_cache: dict[str, tuple[dict, bytes]] = {}

    def _open_board(self) -> sqlite3.Connection:
        return open_db(self.board_db, db_label=f"kanban:{self.board_name}", busy_timeout_ms=5000, wal=False, check_same_thread=False)

    def _assert_parent_identity_unchanged(self) -> None:
        self.assertEqual(self._parent_identity, {key: os.environ.get(key) for key in self._PARENT_IDENTITY_KEYS})

    def signed_revalidate(self, *, reason: str = "verify legacy evidence") -> dict[str, object]:
        self._assert_parent_identity_unchanged()
        if reason in self.approval_cache:
            document, signature = self.approval_cache[reason]
        else:
            prepared = self.controller.prepare_native_release_revalidation(self.ticket, operator_id="operator", reason=reason, implementation_profile="impl")
            document = parse_approval_document(prepared["canonical_document"])
            signature = self.signing_key.sign(canonical_approval_bytes(document))
            self.approval_cache[reason] = (document, signature)
        return self.controller.revalidate_native_release(self.ticket, operator_id="operator", reason=reason, implementation_profile="impl", approval_document=document, detached_signature=signature, signer_public_key=self.signer_public_key, signer_fingerprint=self.signer_fingerprint)

    def seed_run(self, *, status: str, outcome: str, summary: str, started_at: int | None, ended_at: int | None, profile: str | None = None, worker_pid: int | None = None) -> None:
        self._assert_parent_identity_unchanged()
        with self._open_board() as connection:
            connection.execute("INSERT INTO task_runs(task_id,profile,step_key,status,claim_lock,claim_expires,worker_pid,max_runtime_seconds,last_heartbeat_at,started_at,ended_at,outcome,summary,metadata,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.external_id, profile, None, status, None, None, worker_pid, None, None, started_at or 1, ended_at, outcome, summary, None, None))
            connection.commit()

    def test_test_database_is_owned_by_fixture_root_and_fixed_filename(self) -> None:
        self._assert_parent_identity_unchanged()
        self.assertEqual(self.board_db.parent, self.root)
        self.assertEqual(self.board_db.name, "kanban-test.db")
        self.assertTrue(self.board_db.is_file())
        self.assertFalse(self.board_db.is_symlink())

    def tearDown(self) -> None:
        self._assert_parent_identity_unchanged()
        self.ledger.close()
        self.temp.cleanup()

    def test_direct_ledger_fake_verify_method_cannot_authorize_stale_board(self) -> None:
        prepared = self.controller.prepare_native_release_revalidation(self.ticket, operator_id="operator", reason="verify", implementation_profile="impl")
        document = parse_approval_document(prepared["canonical_document"])
        canonical = canonical_approval_bytes(document)
        signature = self.signing_key.sign(canonical)
        authority = document["authority"]
        projection = authority["projection"]
        class F:
            def _verify_for_ledger(self, *_args: object, **_kwargs: object) -> None:
                pass
        with self.assertRaisesRegex(PermissionError, "exact trusted board capability"):
            with self.adapter.revalidation(self.external_id) as capability:
                capability._connection.execute("UPDATE tasks SET status='running' WHERE id=?", (self.external_id,))
                self.ledger.record_native_release_revalidation(
                    ticket_id=self.ticket, projection_event_id=int(projection["event_id"]),
                    projection_key=str(projection["key"]), external_task_id=self.external_id,
                    implementation_profile=str(authority["implementation_profile"]),
                    repository_identity=str(authority["repository_identity"]),
                    canonical_worktree_path=str(authority["canonical_worktree_path"]),
                    branch=str(authority["branch"]), base_sha=str(authority["base_sha"]),
                    snapshot_hash=str(authority["snapshot_hash"]), operator_id="operator", reason="verify",
                    approval_document_json=canonical.decode(), approval_document_hash=hashlib.sha256(canonical).hexdigest(),
                    detached_signature=signature, signer_fingerprint=self.signer_fingerprint,
                    signer_public_key=self.signer_public_key, _trusted_board_capability=F(),
                )
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='native_dependency_release_revalidated'").fetchone()[0], 0)

        with self.assertRaisesRegex(RuntimeError, "trusted local SQLite board adapter"):
            LocalFirstController(self.ledger, object(), self.controller.config).revalidate_native_release(self.ticket, operator_id="operator", reason="verify", implementation_profile="impl")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)

    def test_signer_omission_on_trusted_board_preserves_signature_gate(self) -> None:
        with self.assertRaisesRegex(PermissionError, "signer authority"):
            self.controller.revalidate_native_release(self.ticket, operator_id="operator", reason="verify", implementation_profile="impl")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)

    def test_success_appends_revalidation_and_controller_event_without_mutating_legacy_release(self) -> None:
        result = self.signed_revalidate()
        self.assertEqual(result["ticket_id"], self.ticket)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("SELECT routing_authority_json FROM native_dependency_releases WHERE ticket_id=?", (self.ticket,)).fetchone()[0], "{}")
        self.assertEqual(self.ledger.connection.execute("SELECT event_type FROM events WHERE entity_type='controller' ORDER BY id DESC LIMIT 1").fetchone()[0], "native_dependency_release_revalidated")

    def test_trusted_snapshot_fault_before_insert_rolls_back(self) -> None:
        self.adapter.fault_on = 3
        with self.assertRaisesRegex(RuntimeError, "snapshot read failure"):
            self.signed_revalidate(reason="verify")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='native_dependency_release_revalidated'").fetchone()[0], 0)

    def test_locked_snapshot_drift_seam_stops_before_mutation(self) -> None:
        self.adapter.drift_on = 3
        with self.assertRaisesRegex(RuntimeError, "snapshot|drift|execution evidence"):
            self.signed_revalidate(reason="verify")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)

    def test_two_connection_write_waits_for_trusted_revalidation_commit(self) -> None:
        started = threading.Event()
        finished = threading.Event()
        def compete() -> None:
            with self._open_board() as connection:
                started.set()
                connection.execute("UPDATE tasks SET status='running' WHERE id=?", (self.external_id,))
                connection.commit()
                finished.set()
        worker = threading.Thread(target=compete)
        with self.adapter.revalidation(self.ticket, self.external_id) as proof:
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.1))
            proof._verify_for_ledger(self.ticket, self.external_id)
        worker.join(3)
        self.assertTrue(finished.is_set())

    def test_replay_is_idempotent_only_for_exact_request(self) -> None:
        first = self.signed_revalidate()
        second = self.signed_revalidate()
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "revalidation replay conflicts"):
            self.signed_revalidate(reason="changed")

    def test_rejects_execution_evidence_and_allows_spawn_failed_without_worker(self) -> None:
        self.seed_run(status="done", outcome="completed", summary="done", started_at=1, ended_at=2)
        with self.assertRaisesRegex(RuntimeError, "execution evidence"):
            self.signed_revalidate(reason="verify")
        with self._open_board() as connection:
            connection.execute("DELETE FROM task_runs WHERE task_id=?", (self.external_id,))
            connection.commit()
        self.seed_run(status="spawn_failed", outcome="spawn_failed", summary="could not spawn", started_at=None, ended_at=None)
        self.signed_revalidate(reason="verify")

    def test_unpaused_and_worktree_drift_rollback_without_event(self) -> None:
        self.ledger.resume("operator", reason="test")
        with self.assertRaisesRegex(PermissionError, "paused"):
            self.signed_revalidate(reason="verify")
        self.ledger.pause("operator", reason="test")
        with self._open_board() as connection:
            connection.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(self.root / "wrong"), self.external_id))
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "worktree drift"):
            self.signed_revalidate(reason="verify")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)

    def test_fault_injection_rolls_back_record_and_event_and_detector_clears_only_after_valid_row(self) -> None:
        self.assertIsNotNone(self.ledger.native_dependency_release_migration_required())
        self.ledger.failure_injector = lambda point: (_ for _ in ()).throw(RuntimeError(point)) if point == "after_native_release_revalidation" else None
        with self.assertRaisesRegex(RuntimeError, "after_native_release_revalidation"):
            self.signed_revalidate(reason="verify")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM native_dependency_release_revalidations").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='native_dependency_release_revalidated'").fetchone()[0], 0)
        self.ledger.failure_injector = None
        self.signed_revalidate(reason="verify")
        self.assertIsNone(self.ledger.native_dependency_release_migration_required(signer_public_key=self.signer_public_key, signer_fingerprint=self.signer_fingerprint))

    def test_revalidation_table_is_immutable(self) -> None:
        row = self.signed_revalidate(reason="verify")
        with self.assertRaisesRegex(Exception, "append-only"):
            self.ledger.connection.execute("UPDATE native_dependency_release_revalidations SET reason='tampered' WHERE revalidation_id=?", (row["revalidation_id"],))
        with self.assertRaisesRegex(Exception, "append-only"):
            self.ledger.connection.execute("DELETE FROM native_dependency_release_revalidations WHERE revalidation_id=?", (row["revalidation_id"],))
        with self.assertRaisesRegex(Exception, "immutable"):
            self.ledger.connection.execute("UPDATE events SET payload_json='{}' WHERE id=?", (row["revalidation_event_id"],))

    def test_direct_sql_valid_length_forgery_without_event_stays_reconciliation_required(self) -> None:
        self.ledger.connection.execute("INSERT INTO native_dependency_release_revalidations (revalidation_id,ticket_id,release_graph_hash,release_child_external_id,release_parent_completion_hash,release_routing_authority_json,release_hermes_status,release_observed_at,projection_event_id,projection_key,external_task_id,implementation_profile,repository_identity,canonical_worktree_path,branch,base_sha,snapshot_hash,operator_id,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("forged", self.ticket, "graph-hash", self.external_id, "parents", "{}", "ready", 1, 999999, "legacy-key", self.external_id, "impl", str(self.repo), str(self.root / "outside"), "forged", "a" * 40, "b" * 64, "operator", "forged", 1))
        result = self.ledger.native_dependency_release_migration_required()
        self.assertIsNotNone(result)
        self.assertEqual(result["ticket_id"], self.ticket)

    def test_fake_event_does_not_authorize_forged_revalidation(self) -> None:
        with self.ledger._transaction() as conn:
            event_id = self.ledger._append_event(conn, entity_type="controller", entity_id="controller", event_type="native_dependency_release_revalidated", actor_id="forger", payload={"ticket_id": self.ticket, "revalidation_id": "forged"})
            conn.execute("INSERT INTO native_dependency_release_revalidations (revalidation_id,ticket_id,release_graph_hash,release_child_external_id,release_parent_completion_hash,release_routing_authority_json,release_hermes_status,release_observed_at,projection_event_id,projection_key,external_task_id,implementation_profile,repository_identity,canonical_worktree_path,branch,base_sha,snapshot_hash,operator_id,reason,created_at,revalidation_event_id,event_key,evidence_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("forged", self.ticket, "graph-hash", self.external_id, "parents", "{}", "ready", 1, 999999, "legacy-key", self.external_id, "impl", str(self.repo), str(self.root / "outside"), "forged", "a" * 40, "b" * 64, "operator", "forged", 1, event_id, "native-release-revalidated:forged", "c" * 64))
        result = self.ledger.native_dependency_release_migration_required()
        self.assertIsNotNone(result)
        self.assertEqual(result["ticket_id"], self.ticket)


if __name__ == "__main__":
    unittest.main()
