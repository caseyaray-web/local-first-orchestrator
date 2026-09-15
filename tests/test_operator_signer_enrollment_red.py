from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.operator_config import enroll_operator_signer


class OperatorSignerEnrollmentRedTests(unittest.TestCase):
    def test_enrollment_api_is_paused_and_never_accepts_private_key(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
            (repo / "README").write_text("fixture\n")
            subprocess.run(("git", "add", "README"), cwd=repo, check=True)
            subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"), cwd=repo, check=True)
            ledger_path = root / "ledger.db"; ledger = Ledger(ledger_path); ledger.migrate()
            config_path = root / "operator.json"
            config_path.write_bytes((json.dumps({
                "ledger_path": str(ledger_path), "canonical_repository": str(repo),
                "repository_allowlist": [str(repo)],
                "implementation": {"profile": "impl", "provider": "p", "model": "m"},
                "review": {"profile": "review", "provider": "p", "model": "m"},
                "worktree_root": str(root / "worktrees"), "artifact_root": str(root / "artifacts"),
                "implementation_timeout_seconds": 300, "review_timeout_seconds": 300,
            }, indent=2) + "\n").encode())
            key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
            fingerprint = hashlib.sha256(key).hexdigest()
            with self.assertRaisesRegex(PermissionError, "paused"):
                enroll_operator_signer(ledger, config_path=config_path, operator_id="op", reason="legacy", ticket_ids=(), public_key_b64=base64.b64encode(key).decode(), fingerprint=fingerprint)
            with self.assertRaises((ValueError, TypeError)):
                enroll_operator_signer(ledger, config_path=config_path, operator_id="op", reason="legacy", ticket_ids=(), public_key_b64="private-key", fingerprint=fingerprint)
            self.assertNotIn(key.hex(), config_path.read_text())
            ledger.close()


if __name__ == "__main__":
    unittest.main()
