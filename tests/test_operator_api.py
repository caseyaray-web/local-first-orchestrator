from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.operator_config import ModelRegistration, OperatorConfig, default_execution_roots, save_operator_config
from local_first_orchestrator.states import CanonicalState


class OperatorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.database = root / "ledger.db"
        self.repository = root / "repo"
        self.repository.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.repository, check=True)
        ledger = Ledger(self.database)
        ledger.migrate()
        ledger.close()
        config_path = root / "operator-config.json"
        save_operator_config(
            OperatorConfig(
                self.database,
                self.repository,
                (self.repository,),
                ModelRegistration("impl-profile", "impl-provider", "impl-model"),
                ModelRegistration("review-profile", "review-provider", "review-model"),
                root / "worktrees", root / "artifacts", 1800, 900,
            ),
            config_path,
        )
        self.old_config = os.environ.get("LOCAL_FIRST_OPERATOR_CONFIG")
        os.environ["LOCAL_FIRST_OPERATOR_CONFIG"] = str(config_path)
        ledger = Ledger(self.database)
        ledger.migrate()
        ledger.connection.execute("INSERT INTO features(id, title, status, created_at, updated_at) VALUES ('F', 'feature', 'active', 1, 1)")
        ledger.connection.execute("INSERT INTO tranches(id, feature_id, ordinal, status, integration_commands_json) VALUES ('T', 'F', 0, 'active', '[]')")
        self.ready_ticket = ledger.create_ticket(title="ready", state=CanonicalState.READY_LOCAL)
        self.running_ticket = ledger.create_ticket(title="running", state=CanonicalState.IMPLEMENTING)
        ledger.connection.execute("UPDATE tickets SET feature_id='F', tranche_id='T' WHERE id=?", (self.running_ticket,))
        ledger.create_ticket(title="triage", state=CanonicalState.NEEDS_TRIAGE)
        ledger.create_ticket(title="done", state=CanonicalState.DONE)
        ledger.close()
        module_path = Path(__file__).parents[1] / "dashboard" / "plugin_api.py"
        spec = importlib.util.spec_from_file_location("local_first_operator_api_test", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router, prefix="/api/plugins/local-first-orchestrator")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self.old_config is None:
            os.environ.pop("LOCAL_FIRST_OPERATOR_CONFIG", None)
        else:
            os.environ["LOCAL_FIRST_OPERATOR_CONFIG"] = self.old_config
        self.tempdir.cleanup()

    def test_status_is_bounded_and_pause_resume_gate_new_admission(self) -> None:
        before = self.client.get("/api/plugins/local-first-orchestrator/status")
        self.assertEqual(before.status_code, 200)
        body = before.json()
        self.assertEqual(body["ready_local"], 1)
        self.assertEqual(body["running"], 1)
        self.assertEqual(body["needs_triage"], 1)
        self.assertEqual(body["done"], 1)
        self.assertEqual(body["active"], [{"ticket_id": self.running_ticket, "state": "implementing", "feature_id": "F", "tranche_id": "T"}])
        self.assertFalse(body["active_truncated"])
        self.assertEqual(body["failed_attempt_reconciliations"], [])
        self.assertEqual(body["configuration"], {
            "canonical_repository": str(self.repository.resolve()),
            "repository_allowlist": [str(self.repository.resolve())],
            "implementation": {"profile": "impl-profile", "provider": "impl-provider", "model": "impl-model"},
            "review": {"profile": "review-profile", "provider": "review-provider", "model": "review-model"},
            "worktree_root": str((Path(self.tempdir.name) / "worktrees").resolve()),
            "artifact_root": str((Path(self.tempdir.name) / "artifacts").resolve()),
            "implementation_timeout_seconds": 1800,
            "review_timeout_seconds": 900,
        })

        paused = self.client.post("/api/plugins/local-first-orchestrator/pause", json={"reason": "maintenance"})
        self.assertEqual(paused.status_code, 200)
        self.assertTrue(paused.json()["paused"])
        ledger = Ledger(self.database)
        ledger.migrate()
        self.assertFalse(ledger.claim_specific(self.ready_ticket, "new-worker", 60))
        self.assertEqual(ledger.get_ticket(self.running_ticket)["state"], CanonicalState.IMPLEMENTING.value)
        ledger.close()

        resumed = self.client.post("/api/plugins/local-first-orchestrator/resume", json={"reason": "ready"})
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["paused"])

    def test_request_cannot_select_ledger_repository_or_provider(self) -> None:
        baseline = self.client.get("/api/plugins/local-first-orchestrator/status").json()
        forged = self.client.get(
            "/api/plugins/local-first-orchestrator/status?database=/etc/passwd&repository=/tmp&provider=attacker&model=attacker"
        )
        self.assertEqual(forged.status_code, 200)
        self.assertEqual(forged.json(), baseline)
        self.assertEqual(self.client.post("/api/plugins/local-first-orchestrator/pause?database=/etc/passwd", json={}).status_code, 200)
        self.assertTrue(self.client.get("/api/plugins/local-first-orchestrator/status").json()["paused"])


if __name__ == "__main__":
    unittest.main()
