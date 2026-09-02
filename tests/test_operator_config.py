from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.operator_config import ModelRegistration, OperatorConfig, load_operator_config, save_operator_config


class OperatorConfigSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(); self.root = Path(self.temp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.repo, check=True)
        self.ledger = self.root / "ledger.db"; self.ledger.touch()
        self.base = {"ledger_path": str(self.ledger), "canonical_repository": str(self.repo), "repository_allowlist": [str(self.repo)], "implementation": {"profile":"impl","provider":"provider","model":"model"}, "review": {"profile":"review","provider":"provider","model":"model"}}
    def tearDown(self) -> None: self.temp.cleanup()
    def write(self, payload: dict[str, object]) -> Path:
        path = self.root / "operator.json"; path.write_text(json.dumps(payload), encoding="utf-8"); return path
    def test_legacy_known_schema_is_readable_but_cannot_build_execution_runtime(self) -> None:
        config = load_operator_config(self.write(self.base)); self.assertFalse(config.execution_configured)
        with self.assertRaisesRegex(ValueError, "execution_runtime_not_configured"): config.runtime_config()
    def test_new_schema_is_accepted_and_runtime_validated(self) -> None:
        raw = {**self.base, "worktree_root": str(self.root / "worktrees"), "artifact_root": str(self.root / "artifacts"), "implementation_timeout_seconds": 1800}
        config = load_operator_config(self.write(raw)); runtime = config.runtime_config()
        self.assertEqual(runtime.validate_execution_roots(), (self.repo.resolve(), (self.root / "worktrees").resolve(), (self.root / "artifacts").resolve()))
    def test_unknown_field_is_rejected_without_weakening_closed_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected fields"): load_operator_config(self.write({**self.base, "unrelated": True}))
    def test_save_requires_explicit_runtime_configuration(self) -> None:
        config = OperatorConfig(self.ledger, self.repo, (self.repo,), ModelRegistration("impl","p","m"), ModelRegistration("review","p","m"))
        with self.assertRaisesRegex(ValueError, "execution_runtime_not_configured"): save_operator_config(config, self.root / "out.json")

if __name__ == "__main__": unittest.main()
