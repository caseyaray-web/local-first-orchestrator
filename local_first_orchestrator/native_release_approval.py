"""Detached operator approval documents for native release revalidation.

This module has no signing capability. The plugin only verifies signatures made
outside the plugin by an operator-controlled key.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

APPROVAL_DOMAIN = "native-release-revalidation"
ACTIVATION_DOMAIN = "native-release-activation"
APPROVAL_VERSION = 1
_REQUIRED = {"domain", "version", "operation", "request_id", "nonce", "operator_id", "reason", "authority"}


def _canonical_bytes(document: dict[str, Any], *, domain: str, operation: str) -> bytes:
    if not isinstance(document, dict) or set(document) != _REQUIRED:
        raise ValueError("approval document has unexpected or missing fields")
    if document["domain"] != domain or document["version"] != APPROVAL_VERSION or document["operation"] != operation:
        raise ValueError("approval document domain or version mismatch")
    for key in ("request_id", "nonce", "operator_id", "reason"):
        if not isinstance(document[key], str) or not document[key].strip():
            raise ValueError(f"approval document {key} is required")
    if not isinstance(document["authority"], dict) or not document["authority"]:
        raise ValueError("approval document authority is required")
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def canonical_approval_bytes(document: dict[str, Any]) -> bytes:
    return _canonical_bytes(document, domain=APPROVAL_DOMAIN, operation="revalidate-native-release")


def canonical_activation_bytes(document: dict[str, Any]) -> bytes:
    return _canonical_bytes(document, domain=ACTIVATION_DOMAIN, operation="activate-native-release")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("approval document contains duplicate JSON keys")
        result[key] = value
    return result


def parse_approval_document(raw: bytes | str) -> dict[str, Any]:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(data, bytes):
        raise ValueError("approval document must be UTF-8 bytes")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("approval document is not valid canonical JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("approval document is not canonical JSON")
    if document.get("domain") == ACTIVATION_DOMAIN and document.get("operation") == "activate-native-release":
        canonical = canonical_activation_bytes(document)
    else:
        canonical = canonical_approval_bytes(document)
    if canonical != data:
        raise ValueError("approval document is not canonical JSON")
    return document


def fingerprint_public_key(public_key: bytes) -> str:
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("Ed25519 public key must be exactly 32 bytes")
    return hashlib.sha256(public_key).hexdigest()


def verify_detached_signature(document_bytes: bytes, signature: bytes, public_key: bytes, expected_fingerprint: str) -> bool:
    if fingerprint_public_key(public_key) != expected_fingerprint:
        raise ValueError("operator signer fingerprint does not match persisted registration")
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("detached Ed25519 signature must be 64 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, document_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("detached operator signature verification failed") from exc
    return True
