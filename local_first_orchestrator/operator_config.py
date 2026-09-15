"""Persisted, operator-owned registration for the dashboard read model.

This is deliberately separate from request data: the dashboard backend loads one
local registration and never accepts a ledger or runtime configuration path over
HTTP.  Older registrations remain readable, but cannot construct execution.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


_CONFIG_ENV = "LOCAL_FIRST_OPERATOR_CONFIG"
_LEGACY_FIELDS = frozenset({"ledger_path", "canonical_repository", "repository_allowlist", "implementation", "review"})
_RUNTIME_FIELDS = frozenset({"worktree_root", "artifact_root", "implementation_timeout_seconds", "review_timeout_seconds"})
_ROUTING_FIELDS = frozenset({"decomposition"})
_PAID_FIELDS = frozenset({"paid_checkpoint", "paid_escalation"})
_SIGNER_FIELDS = frozenset({"operator_signing_public_key", "operator_signing_key_fingerprint"})


def default_config_path() -> Path:
    value = os.environ.get(_CONFIG_ENV)
    return Path(value).expanduser() if value else Path.home() / ".hermes" / "local-first-orchestrator" / "operator-config.json"


def stable_repository_identity(repository: Path) -> str:
    """Stable, non-basename namespace for controller-owned external paths."""
    canonical = str(Path(repository).expanduser().resolve(strict=True))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def default_execution_roots(repository: Path) -> tuple[Path, Path]:
    identity = stable_repository_identity(repository)
    root = Path.home() / ".hermes" / "local-first-orchestrator"
    return root / "worktrees" / identity, root / "artifacts" / identity


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
    worktree_root: Path | None = None
    artifact_root: Path | None = None
    implementation_timeout_seconds: int | None = None
    review_timeout_seconds: int | None = None
    decomposition: tuple[tuple[str, ModelRegistration], ...] = ()
    paid_checkpoint: ModelRegistration | None = None
    paid_escalation: ModelRegistration | None = None
    operator_signing_public_key: str | None = None
    operator_signing_key_fingerprint: str | None = None

    @property
    def signer_public_key_bytes(self) -> bytes:
        if not self.operator_signing_public_key or not self.operator_signing_key_fingerprint:
            raise ValueError("external operator signer authority is not registered")
        try:
            key = base64.b64decode(self.operator_signing_public_key, validate=True)
        except Exception as exc:
            raise ValueError("registered operator signing public key is invalid") from exc
        from .native_release_approval import fingerprint_public_key
        if fingerprint_public_key(key) != self.operator_signing_key_fingerprint:
            raise ValueError("registered operator signer fingerprint mismatch")
        return key

    @property
    def operator_authority_hash(self) -> str:
        return hashlib.sha256((self.operator_signing_key_fingerprint or "").encode("ascii")).hexdigest()

    def decomposition_route(self, cost_class: str) -> ModelRegistration:
        routes = dict(self.decomposition)
        route = routes.get(cost_class)
        if route is None:
            raise ValueError(f"decomposition route is not configured for cost class: {cost_class}")
        return route

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
        runtime = (self.worktree_root, self.artifact_root, self.implementation_timeout_seconds, self.review_timeout_seconds)
        if any(value is not None for value in runtime) and any(value is None for value in runtime):
            raise ValueError("execution_runtime_not_configured")
        if self.worktree_root is not None and (not isinstance(self.worktree_root, Path) or not isinstance(self.artifact_root, Path) or not isinstance(self.implementation_timeout_seconds, int) or not isinstance(self.review_timeout_seconds, int)):
            raise ValueError("execution_runtime_not_configured")
        if (self.operator_signing_public_key is None) != (self.operator_signing_key_fingerprint is None):
            raise ValueError("operator signer registration requires public key and fingerprint together")
        if self.operator_signing_public_key is not None:
            self.signer_public_key_bytes
        return OperatorConfig(ledger, repository, allowlist, self.implementation, self.review, self.worktree_root, self.artifact_root, self.implementation_timeout_seconds, self.review_timeout_seconds, self.decomposition, self.paid_checkpoint, self.paid_escalation, self.operator_signing_public_key, self.operator_signing_key_fingerprint)

    @property
    def execution_configured(self) -> bool:
        return self.worktree_root is not None and self.artifact_root is not None and self.implementation_timeout_seconds is not None and self.review_timeout_seconds is not None

    def runtime_config(self):
        """Build the sole execution configuration or fail closed for legacy data."""
        if not self.execution_configured:
            raise ValueError("execution_runtime_not_configured")
        from .controller import RuntimeConfig
        assert self.worktree_root is not None and self.artifact_root is not None and self.implementation_timeout_seconds is not None and self.review_timeout_seconds is not None
        config = RuntimeConfig(self.canonical_repository, self.worktree_root, self.artifact_root, self.repository_allowlist, implementation_timeout_seconds=self.implementation_timeout_seconds, review_timeout_seconds=self.review_timeout_seconds, operator_signer_fingerprint=self.operator_signing_key_fingerprint, operator_authority_hash=self.operator_authority_hash, operator_signer_public_key=self.signer_public_key_bytes if self.operator_signing_public_key else None)
        config.validate_execution_roots()
        return config

    def as_json(self) -> dict[str, object]:
        if not self.execution_configured:
            raise ValueError("execution_runtime_not_configured")
        result = {
            "ledger_path": str(self.ledger_path),
            "canonical_repository": str(self.canonical_repository),
            "repository_allowlist": [str(path) for path in self.repository_allowlist],
            "implementation": asdict(self.implementation),
            "review": asdict(self.review),
            "worktree_root": str(self.worktree_root),
            "artifact_root": str(self.artifact_root),
            "implementation_timeout_seconds": self.implementation_timeout_seconds,
            "review_timeout_seconds": self.review_timeout_seconds,
        }
        if self.decomposition:
            result["decomposition"] = {cost: asdict(route) for cost, route in self.decomposition}
        if self.paid_checkpoint is not None:
            result["paid_checkpoint"] = asdict(self.paid_checkpoint)
        if self.paid_escalation is not None:
            result["paid_escalation"] = asdict(self.paid_escalation)
        if self.operator_signing_public_key is not None:
            result["operator_signing_public_key"] = self.operator_signing_public_key
            result["operator_signing_key_fingerprint"] = self.operator_signing_key_fingerprint
        return result


def load_operator_config(path: Path | None = None) -> OperatorConfig:
    config_path = path or default_config_path()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("operator dashboard is not registered") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("operator registration is not valid JSON") from exc
    if not isinstance(raw, dict) or not _LEGACY_FIELDS <= set(raw) or set(raw) - (_LEGACY_FIELDS | _RUNTIME_FIELDS | _ROUTING_FIELDS | _PAID_FIELDS | _SIGNER_FIELDS):
        raise ValueError("operator registration has unexpected fields")
    if bool(set(raw) & _RUNTIME_FIELDS) and not _RUNTIME_FIELDS <= set(raw):
        raise ValueError("execution_runtime_not_configured")
    paths = raw["repository_allowlist"]
    if not isinstance(paths, list) or not paths or not all(isinstance(item, str) for item in paths):
        raise ValueError("repository_allowlist must be a non-empty list of paths")
    if not isinstance(raw["ledger_path"], str) or not isinstance(raw["canonical_repository"], str):
        raise ValueError("ledger_path and canonical_repository must be paths")
    if _RUNTIME_FIELDS <= set(raw):
        if not isinstance(raw["worktree_root"], str) or not isinstance(raw["artifact_root"], str) or not isinstance(raw["implementation_timeout_seconds"], int) or not isinstance(raw["review_timeout_seconds"], int):
            raise ValueError("execution_runtime_not_configured")
        runtime = (Path(raw["worktree_root"]), Path(raw["artifact_root"]), raw["implementation_timeout_seconds"], raw["review_timeout_seconds"])
    else:
        runtime = (None, None, None, None)
    decomposition = ()
    if "decomposition" in raw:
        if not isinstance(raw["decomposition"], dict) or not raw["decomposition"]:
            raise ValueError("decomposition must be a non-empty cost-class route mapping")
        decomposition = tuple(sorted((str(cost), ModelRegistration.parse(value, f"decomposition.{cost}")) for cost, value in raw["decomposition"].items()))
    paid_checkpoint = ModelRegistration.parse(raw["paid_checkpoint"], "paid_checkpoint") if "paid_checkpoint" in raw else None
    paid_escalation = ModelRegistration.parse(raw["paid_escalation"], "paid_escalation") if "paid_escalation" in raw else None
    signer = (raw.get("operator_signing_public_key"), raw.get("operator_signing_key_fingerprint"))
    if any(value is not None for value in signer) and not all(isinstance(value, str) and value.strip() for value in signer):
        raise ValueError("operator signer registration requires public key and fingerprint together")
    return OperatorConfig(Path(raw["ledger_path"]), Path(raw["canonical_repository"]), tuple(Path(item) for item in paths), ModelRegistration.parse(raw["implementation"], "implementation"), ModelRegistration.parse(raw["review"], "review"), *runtime, decomposition, paid_checkpoint, paid_escalation, *signer).validated(require_ledger=True)


def save_operator_config(config: OperatorConfig, path: Path | None = None) -> Path:
    checked = config.validated(require_ledger=True)
    # Registration is an execution-authority write, so it uses the same resolved
    # containment and timeout gate as the production runtime constructor.
    checked.runtime_config()
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(json.dumps(checked.as_json(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(config_path)
    return config_path


def enroll_operator_signer(*args, **kwargs):
    """Supported paused legacy signer enrollment entry point."""
    from .signer_enrollment import enroll_operator_signer as _enroll
    return _enroll(*args, **kwargs)
