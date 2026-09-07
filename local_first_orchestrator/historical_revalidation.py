from __future__ import annotations

from typing import Any, Mapping

from .evidence_hash import canonical_sha256
from .symbols import contract_target_scope
from .ticket import MicroTicket


OBSOLETE_FILE_SCOPE_SYMBOL_VALIDATION = "obsolete_file_scope_symbol_validation"
AUTHORIZATION_IDENTITY_FIELDS = (
    "ticket_id", "attempt_number", "base_sha", "repository_identity", "target_file",
    "failure_classification", "failure_evidence_identity", "implementation_invocation_id",
    "operator_id", "reason",
)


def authorization_identity(*, ticket_id: str, attempt_number: int, base_sha: str, repository_identity: str, target_file: str, failure_classification: str, failure_evidence_identity: str, implementation_invocation_id: str, operator_id: str, reason: str) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id, "attempt_number": attempt_number, "base_sha": base_sha,
        "repository_identity": repository_identity, "target_file": target_file,
        "failure_classification": failure_classification,
        "failure_evidence_identity": failure_evidence_identity,
        "implementation_invocation_id": implementation_invocation_id,
        "operator_id": operator_id, "reason": reason,
    }


def authorization_hash(identity: Mapping[str, Any]) -> str:
    if set(identity) != set(AUTHORIZATION_IDENTITY_FIELDS):
        raise ValueError("historical authorization identity fields are incomplete or ambiguous")
    return canonical_sha256({field: identity[field] for field in AUTHORIZATION_IDENTITY_FIELDS})


def authorization_hash_from_row(row: Mapping[str, Any]) -> str:
    return authorization_hash({field: row[field] for field in AUTHORIZATION_IDENTITY_FIELDS})


ATTESTATION_IDENTITY_FIELDS = (
    "ticket_id", "attempt_number", "base_sha", "repository_identity",
    "implementation_invocation_id", "implementation_artifact", "implementation_diff_hash",
    "worktree_path", "worktree_diff_hash", "authorization_hash", "operator_id",
)


def attestation_identity(*, ticket_id: str, attempt_number: int, base_sha: str, repository_identity: str, implementation_invocation_id: str, implementation_artifact: str, implementation_diff_hash: str, worktree_path: str, worktree_diff_hash: str, authorization_hash: str, operator_id: str) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id, "attempt_number": attempt_number, "base_sha": base_sha,
        "repository_identity": repository_identity, "implementation_invocation_id": implementation_invocation_id,
        "implementation_artifact": implementation_artifact, "implementation_diff_hash": implementation_diff_hash,
        "worktree_path": worktree_path, "worktree_diff_hash": worktree_diff_hash,
        "authorization_hash": authorization_hash, "operator_id": operator_id,
    }


def attestation_hash(identity: Mapping[str, Any]) -> str:
    if set(identity) != set(ATTESTATION_IDENTITY_FIELDS):
        raise ValueError("historical implementation attestation identity fields are incomplete or ambiguous")
    return canonical_sha256({field: identity[field] for field in ATTESTATION_IDENTITY_FIELDS})


def attestation_hash_from_row(row: Mapping[str, Any]) -> str:
    return attestation_hash({field: row[field] for field in ATTESTATION_IDENTITY_FIELDS})


HISTORICAL_VALIDATION_CLAIM_FIELDS = (
    "ticket_id", "attempt_number", "authorization_hash", "attestation_hash",
    "base_sha", "implementation_diff_hash", "validation_profile_hash",
)


def historical_validation_identity(**values: Any) -> dict[str, Any]:
    if set(values) != set(HISTORICAL_VALIDATION_CLAIM_FIELDS):
        raise ValueError("historical validation identity fields are incomplete or ambiguous")
    return {field: values[field] for field in HISTORICAL_VALIDATION_CLAIM_FIELDS}


def historical_validation_hash(identity: Mapping[str, Any]) -> str:
    return canonical_sha256(historical_validation_identity(**dict(identity)))


def historical_validation_result_hash(**values: Any) -> str:
    required = (*HISTORICAL_VALIDATION_CLAIM_FIELDS, "artifact_sha256", "passed", "compact_evidence")
    if set(values) != set(required):
        raise ValueError("historical validation result identity fields are incomplete or ambiguous")
    return canonical_sha256({field: values[field] for field in required})


def _obsolete_file_scope_symbol_error(path: str) -> str:
    return f"symbol scope exceeded in test file: {path}"


def classify_obsolete_validation_failure(ticket: MicroTicket, historical: object) -> str | None:
    """Classify only deliberately recognized obsolete validation evidence."""
    if type(historical) is not dict:
        return None
    if type(historical.get("attempt_number")) is not int:
        return None
    if type(historical.get("passed")) is not bool or historical["passed"] is not False:
        return None
    compact = historical.get("compact_evidence")
    if type(compact) is not str or not compact:
        return None
    if contract_target_scope(ticket) != "file":
        return None
    if len(ticket.allowed_files) != 1 or ticket.new_test_files:
        return None
    path = ticket.allowed_files[0]
    if compact != _obsolete_file_scope_symbol_error(path):
        return None
    return OBSOLETE_FILE_SCOPE_SYMBOL_VALIDATION


def derive_obsolete_validation_failure(ticket: MicroTicket, historical: object, *, attempt_number: int, stage: str) -> tuple[str, str, str] | None:
    """Derive the exact approved failure and source identity from old evidence."""
    if type(historical) is dict:
        if historical.get("attempt_number") != attempt_number:
            return None
        classification = classify_obsolete_validation_failure(ticket, historical)
        if classification is None:
            return None
        compact = historical["compact_evidence"]
        return classification, canonical_sha256({"attempt_number": attempt_number, "passed": False, "compact_evidence": compact}), "structured_validation_provenance"
    if type(historical) is not str or contract_target_scope(ticket) != "file" or len(ticket.allowed_files) != 1 or ticket.new_test_files:
        return None
    target = ticket.allowed_files[0]
    expected = _obsolete_file_scope_symbol_error(target)
    if historical != expected:
        return None
    return OBSOLETE_FILE_SCOPE_SYMBOL_VALIDATION, canonical_sha256({"historical_evidence_kind": "legacy_runtime_stage_detail", "historical_stage": stage, "raw_detail_sha256": canonical_sha256({"raw_detail": historical}), "attempt_number": attempt_number, "target_file": target, "failure_classification": OBSOLETE_FILE_SCOPE_SYMBOL_VALIDATION}), "legacy_runtime_stage_detail"


def recognized_obsolete_validation_failure(ticket: MicroTicket, historical: object) -> bool:
    return classify_obsolete_validation_failure(ticket, historical) == OBSOLETE_FILE_SCOPE_SYMBOL_VALIDATION
