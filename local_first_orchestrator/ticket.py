from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatchBudget:
    max_files: int = 2
    max_changed_lines: int = 180
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

    def contract(self) -> dict[str, object]:
        return {
            "ticket_id": self.ticket_id, "objective": self.objective,
            "criterion_ids": list(self.criterion_ids), "primary_symbol": self.primary_symbol,
            "allowed_files": list(self.allowed_files), "forbidden_changes": list(self.forbidden_changes),
            "patch_budget": self.patch_budget.__dict__, "verification": {
                "commands": [list(c) for c in self.verification.commands],
                "working_directory": self.verification.working_directory,
            }, "risk": self.risk, "review_required": self.review_required,
            "max_attempts": self.max_attempts, "dependencies": list(self.dependencies),
        }
