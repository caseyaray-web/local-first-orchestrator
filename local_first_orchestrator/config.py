from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .budgets import BudgetPolicy


_REQUIRED_ROLES = frozenset({
    "implementation_profile", "local_review_profile", "planning_profile", "paid_review_profile",
})
_REQUIRED_BUDGETS = frozenset({
    "implementation_attempts", "review_corrections", "infrastructure_retries",
    "workflow_repairs", "paid_capacity",
})


def _canonical_existing_directory(value: object, *, field: str, private: bool = False) -> Path:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be an absolute path")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError(f"{field} must be an existing non-symlink directory")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ValueError(f"{field} must be canonical")
    if private and (path.stat().st_mode & 0o077):
        raise ValueError(f"{field} must not be group/world accessible")
    return path


@dataclass(frozen=True, slots=True)
class PluginConfig:
    """Strict, versioned configuration from a trusted local bootstrap source.

    It has no browser/request path; callers must load and validate it before
    composition.  Existing state is opened fail-closed, never migrated implicitly.
    """

    version: int
    state_root: Path
    hermes_executable: Path
    hermes_home: Path
    kanban_home: Path
    trusted_roots: Mapping[str, Path]
    roles: Mapping[str, str]
    budget_policy: BudgetPolicy
    poll_interval_seconds: int
    scope: Mapping[str, str]
    check_commands: tuple[tuple[str, tuple[str, ...]], ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PluginConfig":
        required_fields = {
            "version", "state_root", "hermes_executable", "hermes_home", "kanban_home",
            "trusted_roots", "roles", "budgets", "poll_interval_seconds", "scope", "check_commands",
        }
        if not isinstance(raw, Mapping) or set(raw) != required_fields:
            missing = required_fields - set(raw) if isinstance(raw, Mapping) else required_fields
            if "budgets" in missing:
                raise ValueError("budgets configuration is required")
            raise ValueError("plugin configuration has unknown or missing fields")
        if raw["version"] != 1:
            raise ValueError("unsupported plugin configuration version")
        state_root = _canonical_existing_directory(raw["state_root"], field="state_root", private=True)
        hermes_home = _canonical_existing_directory(raw["hermes_home"], field="hermes_home")
        kanban_home = _canonical_existing_directory(raw["kanban_home"], field="kanban_home")
        executable = raw["hermes_executable"]
        if type(executable) is not str:
            raise ValueError("hermes_executable must be an absolute executable file")
        executable_path = Path(executable)
        if (not executable_path.is_absolute() or executable_path.is_symlink() or not executable_path.is_file()
                or not os.access(executable_path, os.X_OK)):
            raise ValueError("hermes_executable must be an absolute executable file")
        executable_path = executable_path.resolve(strict=True)
        roots = raw["trusted_roots"]
        if not isinstance(roots, Mapping) or set(roots) != {"repository", "workspace"}:
            raise ValueError("trusted_roots must contain exactly repository and workspace")
        trusted_roots = {name: _canonical_existing_directory(value, field=f"trusted_roots.{name}")
                         for name, value in roots.items()}
        scope = raw["scope"]
        if not isinstance(scope, Mapping) or set(scope) != {"board_id", "anchor_task_id"} or not all(
                type(value) is str and value and len(value) <= 256 for value in scope.values()):
            raise ValueError("scope must contain exact bounded board_id and anchor_task_id")
        roles = raw["roles"]
        if not isinstance(roles, Mapping) or set(roles) != _REQUIRED_ROLES:
            raise ValueError("roles must contain exactly the configured plugin roles")
        normalized_roles = {}
        for key, value in roles.items():
            if type(value) is not str or not value or len(value) > 256:
                raise ValueError("configured role profiles must be bounded non-empty strings")
            normalized_roles[key] = value
        if len(set(normalized_roles.values())) != len(normalized_roles):
            raise ValueError("configured role profiles must be distinct")
        poll_interval_seconds = raw["poll_interval_seconds"]
        if type(poll_interval_seconds) is not int or not 1 <= poll_interval_seconds <= 3600:
            raise ValueError("poll_interval_seconds must be a bounded positive integer")
        budgets = raw["budgets"]
        if not isinstance(budgets, Mapping) or set(budgets) != _REQUIRED_BUDGETS:
            raise ValueError("budgets must contain every finite policy category")
        try:
            policy = BudgetPolicy(**dict(budgets))
        except (TypeError, ValueError) as error:
            raise ValueError("budgets must be finite non-negative integers") from error
        commands = raw["check_commands"]
        if not isinstance(commands, list) or not commands:
            raise ValueError("check_commands must be a non-empty configured command list")
        parsed_commands: list[tuple[str, tuple[str, ...]]] = []
        for item in commands:
            if not isinstance(item, Mapping) or set(item) != {"check_id", "argv"}:
                raise ValueError("each check command needs check_id and argv")
            check_id, argv = item["check_id"], item["argv"]
            if (type(check_id) is not str or not check_id or len(check_id) > 128
                    or not isinstance(argv, list) or not argv
                    or not all(type(arg) is str and arg and len(arg) <= 1024 for arg in argv)):
                raise ValueError("configured check command is malformed")
            command_path = Path(argv[0])
            if (not command_path.is_absolute() or command_path.is_symlink() or not command_path.is_file()
                    or not os.access(command_path, os.X_OK)):
                raise ValueError("configured check executable must be an absolute executable file")
            parsed_commands.append((check_id, tuple(argv)))
        if [item[0] for item in parsed_commands] != sorted(item[0] for item in parsed_commands) or len({item[0] for item in parsed_commands}) != len(parsed_commands):
            raise ValueError("configured check IDs must be unique and sorted")
        return cls(1, state_root, executable_path, hermes_home, kanban_home,
                   MappingProxyType(trusted_roots), MappingProxyType(normalized_roles), policy,
                   poll_interval_seconds, MappingProxyType(dict(scope)), tuple(parsed_commands))

    @classmethod
    def from_file(cls, path: Path) -> "PluginConfig":
        if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError("configuration path must be an absolute regular file")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("configuration must be valid JSON") from error
        return cls.from_mapping(raw)

    @property
    def evidence_store_path(self) -> Path:
        return self.state_root / "evidence.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_root / "coordinator.lock"


@dataclass(frozen=True)
class OrchestratorConfig:
    database: Path
    poll_interval_seconds: int = 15
    local_worker_concurrency: int = 1

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OrchestratorConfig":
        source = raw.get("orchestrator", raw)
        if not isinstance(source, Mapping):
            raise ValueError("orchestrator configuration must be a mapping")
        raw_database = str(source.get("database", "")).strip()
        if not raw_database:
            raise ValueError("orchestrator.database is required")
        database = Path(raw_database).expanduser()
        if database.name == "kanban.db":
            raise ValueError("orchestrator database must be distinct from Hermes kanban.db")
        interval = int(source.get("poll_interval_seconds", 15))
        workers = int(source.get("local_worker_concurrency", 1))
        if interval < 1 or workers != 1:
            raise ValueError("poll_interval_seconds must be positive and local_worker_concurrency must be 1")
        return cls(database=database, poll_interval_seconds=interval, local_worker_concurrency=workers)

    @classmethod
    def from_file(cls, path: Path) -> "OrchestratorConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("configuration JSON must be an object")
        return cls.from_mapping(raw)
