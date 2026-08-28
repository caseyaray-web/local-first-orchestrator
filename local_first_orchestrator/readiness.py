from __future__ import annotations

from .ticket import MicroTicket


class ReadinessError(ValueError):
    pass


_VAGUE = ("improve", "clean up", "fix things", "as needed", "etc", "all files", "anything")


def validate_ticket(ticket: MicroTicket) -> MicroTicket:
    objective = ticket.objective.strip()
    if len(objective) < 12 or any(word in objective.lower() for word in _VAGUE):
        raise ReadinessError("objective is missing, vague, or unsafe")
    if not ticket.criterion_ids or not ticket.primary_symbol or "::" not in ticket.primary_symbol:
        raise ReadinessError("criterion IDs and a bounded primary symbol are required")
    if not ticket.allowed_files or len(ticket.allowed_files) > ticket.patch_budget.max_files:
        raise ReadinessError("allowed files exceed bounded patch budget")
    if any(not path or path.startswith("/") or path.endswith("/") or ".." in path for path in ticket.allowed_files):
        raise ReadinessError("allowed files must be explicit repository-relative files")
    if not ticket.forbidden_changes or not ticket.verification.commands:
        raise ReadinessError("forbidden changes and allowlisted verification commands are required")
    if ticket.risk not in {"low", "medium", "high"} or not 1 <= ticket.max_attempts <= 2:
        raise ReadinessError("risk and maximum two attempts are required")
    budget = ticket.patch_budget
    if budget.max_files > 2 or budget.max_changed_lines > 180:
        if not budget.exception_reason or len(budget.exception_reason.strip()) < 12:
            raise ReadinessError("broader patch budget requires a bounded exception reason")
    return ticket
