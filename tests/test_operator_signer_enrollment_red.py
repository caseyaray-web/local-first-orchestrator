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
from local_first_orchestrator.operator_config import enroll_operator_signer, load_operator_config
from local_first_orchestrator.signer_enrollment import (
    ENROLLMENT_DOMAIN, ENROLLMENT_VERSION, parse_enrollment_document,
    _canonical_document, _verify,
)


class OperatorSignerEnrollmentRedTests(unittest.TestCase):
    def test_enrollment_document_is_canonical_duplicate_free_and_detached_signed(self) -> None:
        private = Ed25519PrivateKey.generate(); public = private.public_key().public_bytes_raw()
        public_b64 = base64.b64encode(public).decode(); fingerprint = hashlib.sha256(public).hexdigest()
        document = {
            "domain": ENROLLMENT_DOMAIN, "version": ENROLLMENT_VERSION, "operation": "enroll-operator-signer",
            "ledger_identity": "/tmp/fixture-ledger.db", "old_config_identity": {"dev": 1, "ino": 2, "uid": os.getuid(), "mode": 0o600, "path": "/tmp/operator.json"},
            "old_config_hash": "a" * 64, "new_public_key": public_b64, "new_fingerprint": fingerprint,
            "ticket_ids": ["ticket-1"], "binding_projection_release_identities": {"ticket-1": {"binding": {}, "projection": {}, "release": {}}},
            "operator_id": "operator", "reason": "legacy migration", "nonce": "nonce-1",
        }
        encoded = _canonical_document(document)
        signature = private.sign(encoded)
        self.assertEqual(parse_enrollment_document(encoded)[0], document)
        self.assertEqual(_verify(document, signature, public_b64, fingerprint)[0], encoded)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_enrollment_document(encoded.replace(b'"nonce":"nonce-1"', b'"nonce":"nonce-1","nonce":"nonce-2"'))

    def test_config_duplicate_null_and_symlink_inputs_stop_before_registration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp); target = root / "target.json"; link = root / "operator.json"
            target.write_text('{"ledger_path":"x","ledger_path":"y"}')
            link.symlink_to(target)
            with self.assertRaises((ValueError, OSError)):
                load_operator_config(link)
            target.unlink(); link.unlink()
            target.write_text('{"ledger_path":null}')
            with self.assertRaises(ValueError):
                load_operator_config(target)

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
