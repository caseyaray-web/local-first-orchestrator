from __future__ import annotations

from typing import Any

from .symbols import contract_target_scope
from .ticket import MicroTicket


OBSOLETE_FILE_SCOPE_SYMBOL_VALIDATION = "obsolete_file_scope_symbol_validation"


def _obsolete_file_scope_symbol_error(path: str) -> str:
    return f"symbol scope exceeded in test file: {path}"


def classify_obsolete_validation_failure(ticket: MicroTicket, historical: object) -> str | None:
    """Classify only deliberately recognized obsolete validation evidence.

    This predicate intentionally consumes the durable compact failure evidence,
    not an operator-provided classification and not a historical artifact as
    authority.  A future obsolete controller defect must add a separate,
    narrow predicate rather than broadening this one.
    """
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
