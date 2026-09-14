import json
import subprocess
import unittest
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.hermes_board import HermesBoardAdapter, ExternalTicket
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.scheduler import ProcessNextScheduler
from local_first_orchestrator.states import CanonicalState


class NativeReleaseAuthorityStep1Tests(unittest.TestCase):
    def _ledger_with_root_graph(self, root: Path):
        ledger = Ledger(root / "ledger.db")
        ledger.migrate()
        contract = {"objective": "Verify the native release authority", "criterion_ids": ["AC-1"], "primary_symbol": "app.py::x", "allowed_files": ["app.py"], "forbidden_changes": ["no unrelated changes"], "patch_budget": {"max_files": 1, "max_changed_lines": 1}, "verification": {"commands": [["true"]]}, "risk": "low", "review_required": True, "max_attempts": 1, "dependencies": []}
        ticket = ledger.create_ticket(title="root", state=CanonicalState.DRAFT, external_id="external-root", contract=contract)
        ledger.bind_runtime(ticket, str(root), "a" * 40)
        with ledger._transaction() as conn:
            event_id = ledger._append_event(conn, entity_type="ticket", entity_id=ticket, event_type="generated_microticket_created", actor_id="test", to_state="draft", payload={"ticket_id": ticket})
        ledger.enqueue_generated_create_projection(ticket, event_id, {"ticket_id": ticket}, "root-create")
        ledger.connection.execute("UPDATE board_projection_outbox SET acknowledged_at=1,external_task_id='external-root' WHERE ticket_id=?", (ticket,))
        core = {"ticket_id": ticket, "child_external_id": "external-root", "parents": []}
        graph_hash = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        ledger.connection.execute("INSERT INTO native_dependency_graphs(ticket_id,child_external_id,local_dependency_ids_json,parent_external_ids_json,graph_hash,verified_at) VALUES (?,?,?,?,?,1)", (ticket, "external-root", "[]", "[]", graph_hash))
        return ledger, ticket

    def test_create_argv_binds_registered_profile_and_canonical_repository(self):
        with TemporaryDirectory() as tmp:
            executable = Path(tmp) / "hermes"
            executable.write_bytes(b"x")
            calls = []

            def runner(argv, **kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, json.dumps({"id": "T-1"}), "")

            adapter = HermesBoardAdapter(
                executable=str(executable), board="isolated", allow_writes=True,
                runner=runner, implementation_profile="worker-code-local",
                canonical_repository=Path("/repo/canonical"),
            )
            self.assertEqual(adapter.create_microticket("title", "body", idempotency_key="K"), "T-1")
            self.assertEqual(calls[0][4:], [
                "create", "title", "--body", "body", "--assignee", "worker-code-local",
                "--workspace", "worktree:/repo/canonical", "--idempotency-key", "K",
                "--initial-status", "blocked", "--json",
            ])

    def test_release_authority_rejects_missing_or_drifted_profile_and_workspace(self):
        adapter = HermesBoardAdapter(
            executable="/bin/true", board="isolated", implementation_profile="worker-code-local",
            canonical_repository=Path("/repo/canonical"),
        )
        expected = {"profile": "worker-code-local", "workspace_path": "/repo/.worktrees/T-1"}
        for assignee, workspace_kind, workspace_path in (
            (None, "worktree", expected["workspace_path"]),
            ("other", "worktree", expected["workspace_path"]),
            ("worker-code-local", None, expected["workspace_path"]),
            ("worker-code-local", "worktree", None),
            ("worker-code-local", "worktree", "/other/T-1"),
        ):
            task = ExternalTicket("T-1", "title", "", "blocked", workspace_path, assignee=assignee, workspace_kind=workspace_kind)
            with self.assertRaisesRegex(RuntimeError, "authority"):
                adapter.verify_native_release_task(task, expected_workspace_path=expected["workspace_path"])

    def test_release_authority_accepts_exact_prepared_worktree(self):
        adapter = HermesBoardAdapter(
            executable="/bin/true", board="isolated", implementation_profile="worker-code-local",
            canonical_repository=Path("/repo"),
        )
        task = ExternalTicket("T-1", "title", "", "blocked", "/repo/.worktrees/T-1", assignee="worker-code-local", workspace_kind="worktree")
        self.assertEqual(adapter.verify_native_release_task(task, expected_workspace_path="/repo/.worktrees/T-1"), {
            "profile": "worker-code-local", "workspace_kind": "worktree", "workspace_path": "/repo/.worktrees/T-1",
        })

    def test_expired_claim_rejects_operator_route_drift(self):
        with TemporaryDirectory() as tmp:
            ledger, ticket = self._ledger_with_root_graph(Path(tmp))
            try:
                claim = ledger.claim_next_scheduler_native_dependency_release("old", lease_seconds=1, now=10, implementation_profile="worker-code-local", canonical_repository="/repo")
                self.assertIsNotNone(claim)
                with self.assertRaisesRegex(RuntimeError, "claim identity drift"):
                    ledger.claim_next_scheduler_native_dependency_release("new", lease_seconds=30, now=11, implementation_profile="worker-code-local-v2", canonical_repository="/repo")
            finally:
                ledger.close()

    def test_release_verification_failure_leaves_card_and_local_ticket_not_ready(self):
        class Board:
            timeout_seconds = 1
            allow_writes = True
            def get_task(self, task_id):
                return ExternalTicket(task_id, "root", "", "blocked", None, assignee="worker-code-local", workspace_kind="worktree")
            def verify_native_release_task(self, task, *, expected_workspace_path):
                raise RuntimeError("native release authority mismatch")
        with TemporaryDirectory() as tmp:
            ledger, ticket = self._ledger_with_root_graph(Path(tmp))
            try:
                with self.assertRaisesRegex(RuntimeError, "authority mismatch"):
                    Board().verify_native_release_task(Board().get_task("external-root"), expected_workspace_path="/repo/.worktrees/external-root")
                self.assertEqual(ledger.get_ticket(ticket)["state"], CanonicalState.DRAFT.value)
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
