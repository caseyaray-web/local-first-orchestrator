from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.hermes_board import ExternalTicket
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


F1_FILE = "scripts/test-meal-planner-c0910-ui.mjs"


def contract() -> dict[str, object]:
    return {
        "objective": "Update the UI fixture.", "criterion_ids": ["AC-1"],
        "primary_symbol": f"{F1_FILE}::run", "allowed_files": [F1_FILE],
        "forbidden_changes": ["no unrelated changes"], "patch_budget": {"max_files": 1, "max_changed_lines": 30},
        "verification": {"commands": [["true"]]}, "risk": "low",
        "review_required": True, "max_attempts": 2,
    }


class Board:
    is_fake = False
    def set_state(self, ticket_id: str, state: object, *, idempotency_key: str) -> None: pass


class F1Model:
    provider = "fixture-provider"
    model = "fixture-model"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
        self.calls.append(purpose)
        assert workdir is not None
        if purpose == "implementation":
            (workdir / F1_FILE).write_text("function run() { return true; }\nfunction incidental() { return true; }\n", encoding="utf-8")
        artifact = artifact_dir / f"{purpose}.json"
        artifact.write_text("{}", encoding="utf-8")
        return type("Result", (), {"payload": {}, "artifact_path": artifact})()


class HistoricalAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(); self.root = Path(self.temp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        self.git("init", "-q", "-b", "main"); self.git("config", "user.email", "t@example.invalid"); self.git("config", "user.name", "Test")
        (self.repo / "scripts").mkdir(); (self.repo / F1_FILE).write_text("function run() { return false; }\n", encoding="utf-8")
        self.git("add", "."); self.git("commit", "-qm", "base"); self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate()
        self.config = RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,))
        card = ExternalTicket("C09.10-T0-F1-fixture", "F1 fixture", "<!-- local-first-orchestrator -->\n```local-first-contract\n" + json.dumps(contract()) + "\n```", "scheduled", str(self.repo))
        self.model = F1Model()
        self.controller = LocalFirstController(self.ledger, Board(), self.config, local_model=self.model)
        self.ticket = self.controller.import_card(card)
        self.ledger.pause("operator", reason="authorization fixture")

    def tearDown(self) -> None:
        self.ledger.close(); self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True)

    def create_obsolete_failure(self) -> None:
        import local_first_orchestrator.validation as validation_module
        with mock.patch.object(validation_module, "contract_target_scope", lambda ticket: "symbol"), mock.patch.object(validation_module, "enforce_symbol_scope", lambda *args: ((f"symbol scope exceeded in test file: {F1_FILE}",), False)):
            self.assertIsNone(self.controller.execute_implementation(self.ticket, repository=self.repo))
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"], CanonicalState.NEEDS_TRIAGE.value)
        self.assertEqual(self.model.calls, ["implementation"])

    def authorize(self, *, operator: str = "operator", reason: str = "recognized F1 obsolete rule") -> dict[str, object]:
        return self.controller.authorize_historical_revalidation(self.ticket, 1, repository=self.repo, operator_id=operator, reason=reason)

    def test_exact_f1_attempt_can_receive_bound_authorization(self) -> None:
        self.create_obsolete_failure()
        authorization = self.authorize()
        self.assertEqual(authorization["ticket_id"], self.ticket)
        self.assertEqual(authorization["attempt_number"], 1)
        self.assertEqual(authorization["base_sha"], self.base)
        self.assertEqual(authorization["target_file"], F1_FILE)
        self.assertEqual(authorization["failure_classification"], "obsolete_file_scope_symbol_validation")
        self.assertTrue(authorization["implementation_invocation_id"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM historical_revalidation_authorizations").fetchone()[0], 1)

    def test_identical_replay_is_idempotent_and_conflicts_fail_closed(self) -> None:
        self.create_obsolete_failure(); first = self.authorize(); replay = self.authorize()
        self.assertEqual(replay["authorization_id"], first["authorization_id"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM historical_revalidation_authorizations").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.authorize(reason="different reason")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.ledger.create_historical_revalidation_authorization(
                ticket_id=self.ticket, attempt_number=1, base_sha="f" * 40,
                repository_identity=str(self.repo.resolve()), target_file=F1_FILE,
                failure_classification=first["failure_classification"], failure_evidence_identity=first["failure_evidence_identity"],
                implementation_invocation_id=first["implementation_invocation_id"], operator_id="operator", reason="recognized F1 obsolete rule")

    def test_authorization_requires_latest_attempt_and_rejects_other_identity(self) -> None:
        self.create_obsolete_failure(); first = self.authorize()
        self.ledger.ensure_attempt(self.ticket, 2)
        with self.assertRaisesRegex(ValueError, "latest"):
            self.controller.authorize_historical_revalidation(self.ticket, 1, repository=self.repo)
        with self.assertRaisesRegex(ValueError, "completed implementation"):
            self.controller.authorize_historical_revalidation(self.ticket, 2, repository=self.repo)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.ledger.create_historical_revalidation_authorization(
                ticket_id=self.ticket, attempt_number=1, base_sha=first["base_sha"],
                repository_identity=str(self.repo.resolve()), target_file=F1_FILE,
                failure_classification=first["failure_classification"], failure_evidence_identity=first["failure_evidence_identity"],
                implementation_invocation_id="different-invocation", operator_id="operator", reason="recognized F1 obsolete rule")

    def test_authorization_alone_cannot_bypass_integrity_gate_or_invoke_model(self) -> None:
        self.create_obsolete_failure(); self.authorize()
        with self.assertRaisesRegex(RuntimeError, "implementation-integrity attestation"):
            self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
        self.assertEqual(self.model.calls, ["implementation"])
        self.assertIsNone(self.ledger.review_candidate(self.ticket))
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"], CanonicalState.NEEDS_TRIAGE.value)

    def test_other_ticket_cannot_reuse_authorization(self) -> None:
        self.create_obsolete_failure(); authorization = self.authorize()
        other = self.ledger.create_ticket(title="other", state=CanonicalState.NEEDS_TRIAGE, contract=contract())
        self.ledger.bind_runtime(other, str(self.repo.resolve()), self.base)
        self.assertIsNone(self.ledger.historical_revalidation_authorization(other, 1))
        with self.assertRaisesRegex(ValueError, "completed implementation"):
            self.controller.revalidate_historical_implementation(other, 1, repository=self.repo)
        self.assertNotEqual(other, authorization["ticket_id"])


if __name__ == "__main__": unittest.main()
