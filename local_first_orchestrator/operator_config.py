"""Persisted, operator-owned registration for the dashboard read model.

This is deliberately separate from request data: the dashboard backend loads one
local registration and never accepts a ledger or runtime configuration path over
HTTP.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


_CONFIG_ENV = "LOCAL_FIRST_OPERATOR_CONFIG"


def default_config_path() -> Path:
    value = os.environ.get(_CONFIG_ENV)
    return Path(value).expanduser() if value else Path.home() / ".hermes" / "local-first-orchestrator" / "operator-config.json"


@dataclass(frozen=True)
class ModelRegistration:
    profile: str
    provider: str
    model: str

    @classmethod
    def parse(cls, raw: object, field: str) -> "ModelRegistration":
        if not isinstance(raw, dict) or set(raw) != {"profile", "provider", "model"}:
            raise ValueError(f"{field} must contain exactly profile, provider, and model")
        values = tuple(raw[key] for key in ("profile", "provider", "model"))
        if not all(isinstance(value, str) and value.strip() and len(value) <= 240 for value in values):
            raise ValueError(f"{field} values must be non-empty bounded strings")
        return cls(*(value.strip() for value in values))


@dataclass(frozen=True)
class OperatorConfig:
    ledger_path: Path
    canonical_repository: Path
    repository_allowlist: tuple[Path, ...]
    implementation: ModelRegistration
    review: ModelRegistration

    def validated(self, *, require_ledger: bool) -> "OperatorConfig":
        ledger = self.ledger_path.expanduser().resolve(strict=require_ledger)
        if ledger.name == "kanban.db":
            raise ValueError("ledger must be distinct from Hermes kanban.db")
        repository = self.canonical_repository.expanduser().resolve(strict=True)
        allowlist = tuple(path.expanduser().resolve(strict=True) for path in self.repository_allowlist)
        if repository not in allowlist:
            raise ValueError("canonical repository must be an exact allowlisted root")
        if not (repository / ".git").exists() or any(not (path / ".git").exists() for path in allowlist):
            raise ValueError("canonical repository and allowlist entries must be Git checkouts")
        return OperatorConfig(ledger, repository, allowlist, self.implementation, self.review)

    def as_json(self) -> dict[str, object]:
        return {
            "ledger_path": str(self.ledger_path),
            "canonical_repository": str(self.canonical_repository),
            "repository_allowlist": [str(path) for path in self.repository_allowlist],
            "implementation": asdict(self.implementation),
            "review": asdict(self.review),
        }


def load_operator_config(path: Path | None = None) -> OperatorConfig:
    config_path = path or default_config_path()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("operator dashboard is not registered") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("operator registration is not valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"ledger_path", "canonical_repository", "repository_allowlist", "implementation", "review"}:
        raise ValueError("operator registration has unexpected fields")
    paths = raw["repository_allowlist"]
    if not isinstance(paths, list) or not paths or not all(isinstance(item, str) for item in paths):
        raise ValueError("repository_allowlist must be a non-empty list of paths")
    if not isinstance(raw["ledger_path"], str) or not isinstance(raw["canonical_repository"], str):
        raise ValueError("ledger_path and canonical_repository must be paths")
    return OperatorConfig(Path(raw["ledger_path"]), Path(raw["canonical_repository"]), tuple(Path(item) for item in paths), ModelRegistration.parse(raw["implementation"], "implementation"), ModelRegistration.parse(raw["review"], "review")).validated(require_ledger=True)


def save_operator_config(config: OperatorConfig, path: Path | None = None) -> Path:
    checked = config.validated(require_ledger=True)
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(json.dumps(checked.as_json(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(config_path)
    return config_path
