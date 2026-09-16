from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from unittest import mock
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
        self.api_module = module
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
            "decomposition": {},
            "paid_checkpoint": None,
            "paid_escalation": None,
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

    def test_configuration_update_requires_pause_and_resolves_hermes_profiles(self) -> None:
        from local_first_orchestrator.operator_config import ModelRegistration, load_operator_config
        registrations = {
            "impl": ModelRegistration("impl", "custom:lm-studio", "qwen3.8-27b@iq3_s"),
            "review": ModelRegistration("review", "openai-codex", "gpt-5.6-luna"),
            "decomp": ModelRegistration("decomp", "openai-codex", "gpt-5.6-sol"),
        }
        payload = {
            "implementation_profile": "impl",
            "review_profile": "review",
            "decomposition_standard_profile": "decomp",
            "implementation_timeout_seconds": 1200,
            "review_timeout_seconds": 600,
        }
        with mock.patch.object(self.api_module, "resolve_registration", side_effect=lambda name: registrations[name]):
            blocked = self.client.put("/api/plugins/local-first-orchestrator/configuration", json=payload)
            self.assertEqual(blocked.status_code, 409)
            self.client.post("/api/plugins/local-first-orchestrator/pause", json={"reason": "configure roles"})
            saved = self.client.put("/api/plugins/local-first-orchestrator/configuration", json=payload)
        self.assertEqual(saved.status_code, 200)
        body = saved.json()
        self.assertEqual(body["implementation"], {"profile": "impl", "provider": "custom:lm-studio", "model": "qwen3.8-27b@iq3_s"})
        self.assertEqual(body["decomposition"]["standard"]["profile"], "decomp")
        config = load_operator_config(Path(os.environ["LOCAL_FIRST_OPERATOR_CONFIG"]))
        self.assertEqual(config.implementation_timeout_seconds, 1200)
        ledger = Ledger(self.database)
        event = ledger.connection.execute("SELECT event_type FROM events WHERE event_type='operator_configuration_updated' ORDER BY id DESC LIMIT 1").fetchone()
        ledger.close()
        self.assertIsNotNone(event)

    def test_configuration_update_can_change_canonical_repository_and_allowlist_while_paused(self) -> None:
        from local_first_orchestrator.operator_config import ModelRegistration, load_operator_config
        second = Path(self.tempdir.name) / "repo-two"
        second.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=second, check=True)
        registrations = {
            "impl": ModelRegistration("impl", "custom:lm-studio", "qwen3.8-27b@iq3_s"),
            "review": ModelRegistration("review", "openai-codex", "gpt-5.6-luna"),
        }
        payload = {
            "canonical_repository": str(second),
            "repository_allowlist": [str(self.repository), str(second)],
            "implementation_profile": "impl",
            "review_profile": "review",
            "implementation_timeout_seconds": 1800,
            "review_timeout_seconds": 900,
        }
        self.client.post("/api/plugins/local-first-orchestrator/pause", json={"reason": "change repo"})
        with mock.patch.object(self.api_module, "resolve_registration", side_effect=lambda name: registrations[name]):
            saved = self.client.put("/api/plugins/local-first-orchestrator/configuration", json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["canonical_repository"], str(second.resolve()))
        self.assertEqual(saved.json()["repository_allowlist"], [str(self.repository.resolve()), str(second.resolve())])
        loaded = load_operator_config(Path(os.environ["LOCAL_FIRST_OPERATOR_CONFIG"]))
        self.assertEqual(loaded.canonical_repository, second.resolve())
        self.assertEqual(loaded.repository_allowlist, (self.repository.resolve(), second.resolve()))

    def test_configuration_update_rejects_canonical_repository_outside_allowlist(self) -> None:
        from local_first_orchestrator.operator_config import ModelRegistration
        second = Path(self.tempdir.name) / "repo-two"
        second.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=second, check=True)
        registrations = {
            "impl": ModelRegistration("impl", "custom:lm-studio", "qwen3.8-27b@iq3_s"),
            "review": ModelRegistration("review", "openai-codex", "gpt-5.6-luna"),
        }
        payload = {
            "canonical_repository": str(second),
            "repository_allowlist": [str(self.repository)],
            "implementation_profile": "impl",
            "review_profile": "review",
            "implementation_timeout_seconds": 1800,
            "review_timeout_seconds": 900,
        }
        self.client.post("/api/plugins/local-first-orchestrator/pause", json={"reason": "change repo"})
        with mock.patch.object(self.api_module, "resolve_registration", side_effect=lambda name: registrations[name]):
            rejected = self.client.put("/api/plugins/local-first-orchestrator/configuration", json=payload)
        self.assertEqual(rejected.status_code, 409)
        self.assertIn("exact allowlisted root", rejected.json()["detail"])

    def test_profiles_endpoint_returns_resolved_profile_metadata(self) -> None:
        class Profile:
            def as_json(self):
                return {"profile": "worker", "provider": "provider", "model": "model", "gateway": "stopped", "alias": "worker"}
        with mock.patch.object(self.api_module, "discover_profiles", return_value=(Profile(),)):
            response = self.client.get("/api/plugins/local-first-orchestrator/profiles")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["profiles"][0]["profile"], "worker")

    def test_implementation_route_uses_named_operator_seam_while_paused(self) -> None:
        ledger = Ledger(self.database); ledger.pause("operator", reason="maintenance"); ledger.close()
        calls = []
        class FakeController:
            def __init__(self, *args, **kwargs): pass
            def execute_implementation(self, ticket_id, *, repository, owner):
                calls.append((ticket_id, owner)); return {"ticket_id": ticket_id, "state": "local_review"}
        with mock.patch.object(self.api_module, "LocalFirstController", FakeController), mock.patch.object(self.api_module, "LocalQwenAdapter", lambda **kwargs: object()):
            response = self.client.post("/api/plugins/local-first-orchestrator/implementation", json={"ticket_id": self.ready_ticket})
        self.assertEqual(response.status_code, 200); self.assertEqual(calls, [(self.ready_ticket, "dashboard-operator")])
        ledger = Ledger(self.database); self.assertEqual(ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()[0], 1); ledger.close()


if __name__ == "__main__":
    unittest.main()
