from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from .source_languages import normalized_repository_path

MAX_REFERENCES_PER_TICKET = 2048
MAX_VERIFICATION_COMMANDS = 32
MAX_ARGV_MEMBERS = 32
MAX_ARG_LENGTH = 256
MAX_ARGV_BYTES = 4096


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _unique(values: tuple[str, ...]) -> bool:
    return len(values) == len(set(values))


@dataclass(frozen=True)
class PatchBudget:
    max_files: int
    max_changed_lines: int
    max_attempts: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (self.max_files, self.max_changed_lines, self.max_attempts)):
            raise ValueError("patch budgets must be positive finite integers")


@dataclass(frozen=True)
class VerificationProfile:
    commands: tuple[tuple[str, ...], ...]
    timeout_seconds: int
    output_limit: int
    working_directory: str = "."

    def __post_init__(self) -> None:
        valid_commands = (
            isinstance(self.commands, tuple)
            and bool(self.commands)
            and len(self.commands) <= MAX_VERIFICATION_COMMANDS
            and all(
                isinstance(command, tuple)
                and bool(command)
                and len(command) <= MAX_ARGV_MEMBERS
                and all(isinstance(argument, str) and argument.strip() and len(argument) <= MAX_ARG_LENGTH for argument in command)
                for command in self.commands
            )
        )
        if not valid_commands:
            raise ValueError("verification requires immutable non-empty command argv")
        if len(set(self.commands)) != len(self.commands):
            raise ValueError("verification commands must be unique")
        if sum(len(argument.encode("utf-8")) for command in self.commands for argument in command) > MAX_ARGV_BYTES:
            raise ValueError("verification argv exceeds byte limit")
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0 or type(self.output_limit) is not int or self.output_limit <= 0:
            raise ValueError("verification limits must be positive finite integers")
        if self.working_directory != "." and normalized_repository_path(self.working_directory) is None:
            raise ValueError("verification working directory must be repository-relative")


@dataclass(frozen=True)
class TicketContract:
    ticket_id: str
    objective: str
    criterion_ids: tuple[str, ...]
    non_goals: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    verification: VerificationProfile
    patch_budget: PatchBudget
    context_budget_tokens: int
    dependencies: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.verification) is not VerificationProfile or type(self.patch_budget) is not PatchBudget:
            raise ValueError("ticket verification and patch budget must be exact immutable contract types")
        if type(self.context_budget_tokens) is not int or self.context_budget_tokens <= 0:
            raise ValueError("context budget must be a positive finite integer")
        if self.schema_version != 1 or not isinstance(self.ticket_id, str) or not self.ticket_id.strip() or not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("invalid ticket identity, objective, or schema version")
        if not isinstance(self.criterion_ids, tuple) or not self.criterion_ids or any(not isinstance(value, str) or not value.strip() for value in self.criterion_ids) or not _unique(self.criterion_ids):
            raise ValueError("ticket criteria must be immutable, non-empty and unique")
        if not isinstance(self.allowed_paths, tuple) or not self.allowed_paths or not _unique(self.allowed_paths) or any(normalized_repository_path(path) is None for path in self.allowed_paths):
            raise ValueError("ticket paths must be unique normalized repository files")
        if len(self.allowed_paths) > self.patch_budget.max_files:
            raise ValueError("ticket paths exceed declared file budget")
        if (
            not isinstance(self.non_goals, tuple)
            or any(not isinstance(value, str) or not value.strip() for value in self.non_goals)
            or not isinstance(self.dependencies, tuple)
            or len(self.dependencies) > MAX_REFERENCES_PER_TICKET
            or any(not isinstance(value, str) or not value.strip() for value in self.dependencies)
            or not _unique(self.dependencies)
            or self.ticket_id in self.dependencies
        ):
            raise ValueError("ticket non-goals/dependencies must be immutable non-empty strings and dependencies unique")

    @property
    def contract_hash(self) -> str:
        return _hash(contract_payload(self))


def declared_ticket_paths(contract: TicketContract) -> tuple[str, ...]:
    if type(contract) is not TicketContract:
        raise ValueError("ticket contract is required")
    return contract.allowed_paths


def contract_payload(contract: TicketContract) -> dict[str, object]:
    if type(contract) is not TicketContract:
        raise ValueError("ticket contract is required")
    return {
        "schema_version": contract.schema_version,
        "ticket_id": contract.ticket_id,
        "objective": contract.objective,
        "criterion_ids": list(contract.criterion_ids),
        "non_goals": list(contract.non_goals),
        "allowed_paths": list(contract.allowed_paths),
        "dependencies": list(contract.dependencies),
        "patch_budget": {
            "max_files": contract.patch_budget.max_files,
            "max_changed_lines": contract.patch_budget.max_changed_lines,
            "max_attempts": contract.patch_budget.max_attempts,
        },
        "context_budget_tokens": contract.context_budget_tokens,
        "verification": {
            "commands": [list(command) for command in contract.verification.commands],
            "working_directory": contract.verification.working_directory,
            "timeout_seconds": contract.verification.timeout_seconds,
            "output_limit": contract.verification.output_limit,
        },
    }


def parse_contract(payload: object) -> TicketContract:
    if not isinstance(payload, Mapping):
        raise ValueError("ticket contract payload must be an object")
    expected_keys = {"schema_version", "ticket_id", "objective", "criterion_ids", "non_goals", "allowed_paths", "dependencies", "patch_budget", "context_budget_tokens", "verification"}
    if set(payload) != expected_keys:
        raise ValueError("ticket contract payload has unexpected fields")
    budget = payload["patch_budget"]
    verification = payload["verification"]
    if not isinstance(budget, Mapping) or not isinstance(verification, Mapping):
        raise ValueError("ticket contract nested values must be objects")
    if (set(budget) != {"max_files", "max_changed_lines", "max_attempts"}
            or set(verification) != {"commands", "working_directory", "timeout_seconds", "output_limit"}):
        raise ValueError("ticket contract nested values have missing or unexpected fields")
    try:
        return TicketContract(
            ticket_id=payload["ticket_id"], objective=payload["objective"],
            criterion_ids=tuple(payload["criterion_ids"]), non_goals=tuple(payload["non_goals"]),
            allowed_paths=tuple(payload["allowed_paths"]), dependencies=tuple(payload["dependencies"]),
            schema_version=payload["schema_version"], context_budget_tokens=payload["context_budget_tokens"],
            patch_budget=PatchBudget(budget["max_files"], budget["max_changed_lines"], budget["max_attempts"]),
            verification=VerificationProfile(tuple(tuple(command) for command in verification["commands"]), verification["timeout_seconds"], verification["output_limit"], verification["working_directory"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid ticket contract payload") from exc
