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


def recognized_obsolete_validation_failure(ticket: MicroTicket, historical: object) -> bool:
    return classify_obsolete_validation_failure(ticket, historical) == OBSOLETE_FILE_SCOPE_SYMBOL_VALIDATION
