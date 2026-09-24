from __future__ import annotations

import base64
import hashlib
import json
import unittest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_first_orchestrator.operator_config import enroll_operator_signer, load_operator_config
from local_first_orchestrator.signer_enrollment import prepare_operator_signer_enrollment
from tests.test_operator_signer_enrollment_red import OperatorSignerEnrollmentRedTests


class ModernSignerEnrollmentTests(unittest.TestCase):
    def fixture(self, *, modern_routing: str | None = None):
        # Reuse the real migrated ledger, Git repository, projection, release,
        # and operator config, then promote only the modern routing facts.
        tmp, root, ledger, config, _, _, _, _, ticket = OperatorSignerEnrollmentRedTests()._enrollment_fixture(modern=True, modern_routing=modern_routing)
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes_raw()
        public_b64 = base64.b64encode(public).decode()
        fingerprint = hashlib.sha256(public).hexdigest()
        return tmp, root, ledger, config, ticket, private, public_b64, fingerprint

    def test_modern_routed_local_review_enrolls_without_other_effects(self):
        tmp, root, ledger, config, ticket, key, public, fingerprint = self.fixture()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(ledger.close)
        before = [tuple(row) for row in ledger.connection.execute("SELECT * FROM native_dependency_releases WHERE ticket_id=?", (ticket,))]
        projection_before = [tuple(row) for row in ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=?", (ticket,))]
        doc = prepare_operator_signer_enrollment(ledger, config_path=config, operator_id="operator", reason="modern review binding", ticket_ids=(ticket,), public_key_b64=public, fingerprint=fingerprint, nonce="modern-nonce")
        self.assertEqual(doc["document"]["binding_projection_release_identities"][ticket]["release"]["routing_authority_json"], json.dumps({"canonical_repository": str(root / "repo"), "profile": "i"}, sort_keys=True, separators=(",", ":")))
        result = enroll_operator_signer(ledger, config_path=config, document=doc["document"], detached_signature=key.sign(doc["document_bytes"]), public_key_b64=public, fingerprint=fingerprint)
        self.assertEqual(result["status"], "finalized")
        self.assertEqual(load_operator_config(config).operator_signing_key_fingerprint, fingerprint)
        self.assertEqual(ledger.connection.execute("SELECT operator_signer_fingerprint FROM runtime_bindings WHERE ticket_id=?", (ticket,)).fetchone()[0], fingerprint)
        self.assertEqual([tuple(row) for row in ledger.connection.execute("SELECT * FROM native_dependency_releases WHERE ticket_id=?", (ticket,))], before)
        self.assertEqual([tuple(row) for row in ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=?", (ticket,))], projection_before)
    def test_modern_enrollment_refuses_live_tick_before_prepare(self):
        tmp, root, ledger, config, ticket, key, public, fingerprint = self.fixture()
        self.addCleanup(tmp.cleanup); self.addCleanup(ledger.close)
        ledger.connection.execute("INSERT INTO scheduler_tick_lease(id,lease_owner,lease_token,lease_expires_at,updated_at) VALUES (1,'other','token',9999999999,1)")
        before = config.read_bytes()
        with self.assertRaises(PermissionError):
            prepare_operator_signer_enrollment(ledger, config_path=config, operator_id="operator", reason="modern review binding", ticket_ids=(ticket,), public_key_b64=public, fingerprint=fingerprint)
        self.assertEqual(config.read_bytes(), before)
        self.assertIsNone(ledger.connection.execute("SELECT operator_signer_fingerprint FROM runtime_bindings WHERE ticket_id=?", (ticket,)).fetchone()[0])

    def test_modern_enrollment_requires_exact_registered_route(self):
        for route in ('{"profile":"other","canonical_repository":"/fixture"}', '{"profile":"i"}', '{"profile":"i","profile":"other","canonical_repository":"/fixture"}', '{}'):
            with self.subTest(route=route):
                tmp, root, ledger, config, ticket, key, public, fingerprint = self.fixture(modern_routing=route)
                try:
                    with self.assertRaises(ValueError):
                        prepare_operator_signer_enrollment(ledger, config_path=config, operator_id="operator", reason="modern review binding", ticket_ids=(ticket,), public_key_b64=public, fingerprint=fingerprint)
                    self.assertIsNone(ledger.connection.execute("SELECT operator_signer_fingerprint FROM runtime_bindings WHERE ticket_id=?", (ticket,)).fetchone()[0])
                finally:
                    ledger.close(); tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
