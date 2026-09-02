from __future__ import annotations

import argparse
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.cli import _ad_hoc_controller, _registered_controller, register_cli
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.operator_config import ModelRegistration, OperatorConfig, save_operator_config


class RegisteredRuntimeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name); self.repo=self.root/"repo"; self.repo.mkdir()
        subprocess.run(("git","init","-q"),cwd=self.repo,check=True)
        self.database=self.root/"ledger.db"; self.ledger=Ledger(self.database); self.ledger.migrate()
        self.config_path=self.root/"operator.json"
        save_operator_config(OperatorConfig(self.database,self.repo,(self.repo,),ModelRegistration("registered-profile","registered-provider","registered-model"),ModelRegistration("review","review-provider","review-model"),self.root/"registered-worktrees",self.root/"registered-artifacts",1800),self.config_path)
    def tearDown(self) -> None: self.ledger.close(); self.temp.cleanup()
    def args(self, **changes: object) -> argparse.Namespace:
        value={"repository":".","allow_repository":[],"worktree_root":None,"artifact_root":None,"implementation_timeout_seconds":None,"operator_config_path":str(self.config_path),"ad_hoc_runtime":False}
        value.update(changes); return argparse.Namespace(**value)
    def test_registered_runtime_uses_authoritative_paths_identity_and_timeout(self) -> None:
        controller, config=_registered_controller(self.ledger,self.args())
        self.assertEqual(controller.config.worktree_root,(self.root/"registered-worktrees").resolve()); self.assertEqual(controller.config.artifact_root,(self.root/"registered-artifacts").resolve()); self.assertEqual(controller.config.implementation_timeout_seconds,1800)
        self.assertEqual(controller.local_model.implementation_timeout_seconds,1800); self.assertEqual(controller.local_model.provider,"registered-provider"); self.assertEqual(controller.local_model.model,"registered-model"); self.assertEqual(config.canonical_repository,self.repo.resolve())
    def test_registered_runtime_rejects_cli_override_instead_of_falling_back(self) -> None:
        with self.assertRaisesRegex(ValueError,"forbids runtime overrides"): _registered_controller(self.ledger,self.args(implementation_timeout_seconds=300))
    def test_ad_hoc_runtime_is_explicit_and_has_separate_default(self) -> None:
        controller=_ad_hoc_controller(self.ledger,self.args(repository=str(self.repo),operator_config_path=None,ad_hoc_runtime=True))
        self.assertEqual(controller.config.implementation_timeout_seconds,300); self.assertNotEqual(controller.config.worktree_root,(self.root/"registered-worktrees").resolve())

    def test_board_access_is_explicitly_available_to_import_and_registered_execution(self) -> None:
        parser = argparse.ArgumentParser(); register_cli(parser)
        for command, tail in (("import", ["--task-id", "card"]), ("run-once", ["--task-id", "ticket"])):
            args = parser.parse_args(["--database", str(self.database), "--board", "board", "--hermes-executable", "hermes", command, *tail])
            self.assertEqual(args.board, "board"); self.assertEqual(args.hermes_executable, "hermes")

if __name__ == "__main__": unittest.main()
