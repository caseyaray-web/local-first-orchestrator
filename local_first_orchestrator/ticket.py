from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatchBudgetPolicy:
    normal_max_files: int = 2
    normal_max_changed_lines: int = 180
    minimum_exception_reason_length: int = 12

    def as_json(self) -> dict[str, object]:
        return {
            "normal_max_files": self.normal_max_files,
            "normal_max_changed_lines": self.normal_max_changed_lines,
            "declared_paths": "allowed_files + create_files + new_test_files",
            "declared_paths_must_be_unique": True,
            "broader_budget_requires_exception_reason": True,
            "minimum_exception_reason_length": self.minimum_exception_reason_length,
        }


PATCH_BUDGET_POLICY = PatchBudgetPolicy()


def declared_ticket_paths(ticket: "MicroTicket") -> tuple[str, ...]:
    return (*ticket.allowed_files, *ticket.create_files, *ticket.new_test_files)


@dataclass(frozen=True)
class PatchBudget:
    max_files: int = PATCH_BUDGET_POLICY.normal_max_files
    max_changed_lines: int = PATCH_BUDGET_POLICY.normal_max_changed_lines
    exception_reason: str | None = None


@dataclass(frozen=True)
class VerificationProfile:
    commands: tuple[tuple[str, ...], ...]
    working_directory: str = "."
    timeout_seconds: int = 60
    output_limit: int = 20_000


@dataclass(frozen=True)
class MicroTicket:
    ticket_id: str
    objective: str
    criterion_ids: tuple[str, ...]
    primary_symbol: str
    allowed_files: tuple[str, ...]
    forbidden_changes: tuple[str, ...]
    patch_budget: PatchBudget
    verification: VerificationProfile
    risk: str
    review_required: bool
    max_attempts: int
    dependencies: tuple[str, ...]
    new_test_files: tuple[str, ...] = ()
    create_files: tuple[str, ...] = ()

    def contract(self) -> dict[str, object]:
        contract: dict[str, object] = {
            "ticket_id": self.ticket_id, "objective": self.objective,
            "criterion_ids": list(self.criterion_ids), "primary_symbol": self.primary_symbol,
            "allowed_files": list(self.allowed_files), "forbidden_changes": list(self.forbidden_changes),
            "patch_budget": self.patch_budget.__dict__, "verification": {
                "commands": [list(c) for c in self.verification.commands],
                "working_directory": self.verification.working_directory,
                "timeout_seconds": self.verification.timeout_seconds,
                "output_limit": self.verification.output_limit,
            }, "risk": self.risk, "review_required": self.review_required,
            "max_attempts": self.max_attempts, "dependencies": list(self.dependencies),
        }
        if self.create_files:
            contract["create_files"] = list(self.create_files)
        if self.new_test_files:
            contract["new_test_files"] = list(self.new_test_files)
        return contract
