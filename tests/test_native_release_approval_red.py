from __future__ import annotations

import base64
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_first_orchestrator.native_release_approval import (
    APPROVAL_DOMAIN,
    APPROVAL_VERSION,
    canonical_approval_bytes,
    fingerprint_public_key,
    parse_approval_document,
    verify_detached_signature,
)


class NativeReleaseApprovalTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes_raw()
        self.document = {
            "domain": APPROVAL_DOMAIN,
            "version": APPROVAL_VERSION,
            "operation": "revalidate-native-release",
            "request_id": "req-1",
            "nonce": "nonce-1",
            "operator_id": "operator",
            "reason": "legacy evidence",
            "authority": {"ticket_id": "T-1", "release_graph_hash": "g", "snapshot_hash": "s"},
        }

    def test_canonical_document_rejects_duplicate_or_noncanonical_json(self):
        encoded = b'{"domain":"native-release-revalidation","domain":"wrong","version":1,"operation":"revalidate-native-release","request_id":"req-1","nonce":"nonce-1","operator_id":"operator","reason":"legacy evidence","authority":{"ticket_id":"T-1","release_graph_hash":"g","snapshot_hash":"s"}}'
        with self.assertRaises(ValueError):
            parse_approval_document(encoded)
        with self.assertRaises(ValueError):
            parse_approval_document(json.dumps(self.document, indent=2).encode())

    def test_signature_verification_uses_pinned_key_and_canonical_bytes(self):
        encoded = canonical_approval_bytes(self.document)
        signature = self.private.sign(encoded)
        self.assertTrue(verify_detached_signature(encoded, signature, self.public, fingerprint_public_key(self.public)))
        with self.assertRaises(ValueError):
            verify_detached_signature(encoded + b" ", signature, self.public, fingerprint_public_key(self.public))
        with self.assertRaises(ValueError):
            verify_detached_signature(encoded, signature, Ed25519PrivateKey.generate().public_key().public_bytes_raw(), fingerprint_public_key(self.public))


if __name__ == "__main__":
    unittest.main()
