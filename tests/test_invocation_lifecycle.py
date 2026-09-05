from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.hermes_board import ExternalTicket
from local_first_orchestrator.ledger import Ledger


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
    def set_state(self, ticket_id: str, state: object, *, idempotency_key: str) -> None: pass


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
        artifact.write_text("{}", encoding="utf-8")
        payload = {} if purpose == "implementation" else {"verdict":"pass","criterion_results":[{"criterion_id":"AC-1","status":"pass","evidence":"ok"}],"findings":[],"suggestions":[]}
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

    def test_implementation_only_freezes_candidate_without_review_and_replays(self) -> None:
        model = LifecycleModel(); ctl, ticket = self.controller(model)
        first = ctl.execute_implementation(ticket, repository=self.repo)
        assert first is not None
        self.assertFalse(first["replayed"])
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "local_review")
        self.assertEqual(model.calls, 1)
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
