from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


class OperatorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "ledger.db"
        ledger = Ledger(self.database)
        ledger.migrate()
        self.ready_ticket = ledger.create_ticket(title="ready", state=CanonicalState.READY_LOCAL)
        self.running_ticket = ledger.create_ticket(title="running", state=CanonicalState.IMPLEMENTING)
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
        self.tempdir.cleanup()

    def _url(self, action: str) -> str:
        return f"/api/plugins/local-first-orchestrator/{action}?database={self.database}"

    def test_status_is_bounded_and_pause_resume_gate_new_admission(self) -> None:
        before = self.client.get(self._url("status"))
        self.assertEqual(before.status_code, 200)
        self.assertEqual(
            before.json(),
            {
                "paused": False,
                "ready_local": 1,
                "running": 1,
                "outbox_pending": 0,
            },
        )

        paused = self.client.post(self._url("pause"), json={"reason": "maintenance"})
        self.assertEqual(paused.status_code, 200)
        self.assertTrue(paused.json()["paused"])
        ledger = Ledger(self.database)
        ledger.migrate()
        self.assertFalse(ledger.claim_specific(self.ready_ticket, "new-worker", 60))
        self.assertEqual(ledger.get_ticket(self.running_ticket)["state"], CanonicalState.IMPLEMENTING.value)
        ledger.close()

        resumed = self.client.post(self._url("resume"), json={"reason": "ready"})
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["paused"])
        ledger = Ledger(self.database)
        ledger.migrate()
        self.assertTrue(ledger.claim_specific(self.ready_ticket, "new-worker", 60))
        ledger.close()

    def test_missing_or_unknown_database_is_rejected_without_creating_it(self) -> None:
        self.assertEqual(self.client.get("/api/plugins/local-first-orchestrator/status").status_code, 422)
        missing = Path(self.tempdir.name) / "missing.db"
        response = self.client.get(f"/api/plugins/local-first-orchestrator/status?database={missing}")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
