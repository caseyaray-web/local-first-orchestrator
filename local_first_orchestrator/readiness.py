from __future__ import annotations

from .source_languages import is_supported_source, is_test_path, normalized_repository_path
from .ticket import MicroTicket, PATCH_BUDGET_POLICY


class ReadinessError(ValueError):
    pass


_VAGUE = ("improve", "clean up", "fix things", "as needed", "etc", "all files", "anything")


def validate_ticket(ticket: MicroTicket, *, budget_policy=PATCH_BUDGET_POLICY) -> MicroTicket:
    objective = ticket.objective.strip()
    if len(objective) < 12 or any(word in objective.lower() for word in _VAGUE):
        raise ReadinessError("objective is missing, vague, or unsafe")
    if not ticket.criterion_ids or not ticket.primary_symbol or "::" not in ticket.primary_symbol:
        raise ReadinessError("criterion IDs and a bounded primary symbol are required")
    paths = (*ticket.allowed_files, *ticket.create_files, *ticket.new_test_files)
    if not paths:
        raise ReadinessError("declared files exceed bounded patch budget")
    if len(set(paths)) != len(paths):
        raise ReadinessError("declared file categories must not overlap")
    if len(paths) > ticket.patch_budget.max_files:
        raise ReadinessError("declared files exceed bounded patch budget")
    if any(normalized_repository_path(path) is None for path in paths):
        raise ReadinessError("declared files must be explicit normalized repository-relative files")
    if len(set(paths)) != len(paths):
        raise ReadinessError("declared file categories must not overlap")
    if any(not is_supported_source(path) or is_test_path(path) for path in ticket.create_files):
        raise ReadinessError("create files must be supported non-test artifacts")
    if any(not is_supported_source(path) or not is_test_path(path) for path in ticket.new_test_files):
        raise ReadinessError("new files must be supported test artifacts")
    if not ticket.forbidden_changes or not ticket.verification.commands:
        raise ReadinessError("forbidden changes and allowlisted verification commands are required")
    if ticket.risk not in {"low", "medium", "high"} or not 1 <= ticket.max_attempts <= 2:
        raise ReadinessError("risk and maximum two attempts are required")
    budget = ticket.patch_budget
    if budget.max_files > budget_policy.normal_max_files or budget.max_changed_lines > budget_policy.normal_max_changed_lines:
        if not budget.exception_reason or len(budget.exception_reason.strip()) < budget_policy.minimum_exception_reason_length:
            raise ReadinessError("broader patch budget requires a bounded exception reason")
    return ticket
