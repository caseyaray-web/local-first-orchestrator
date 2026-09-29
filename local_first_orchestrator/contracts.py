"""Transport-independent records for the plugin-only orchestration boundary.

These records retain observations and plugin evidence only.  They deliberately do
not model native task lanes or expose a generic task-state mutation operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping


_JSON_VALUE = str | int | float | bool | None
_EFFECT_PHASES = frozenset({"pending", "applied", "unknown"})
_ACTION_OUTCOMES = frozenset(
    {"verified", "no-op", "retryable", "ambiguous", "conflict", "unknown", "unsupported", "partial"}
)


class ContractError(ValueError):
    """Base class for boundary outcomes that require explicit handling."""


class ConflictError(ContractError):
    pass


class UnknownError(ContractError):
    pass


class UnsupportedError(ContractError):
    pass


class PartialError(ContractError):
    pass


_ERROR_BY_OUTCOME = {
    "conflict": ConflictError,
    "unknown": UnknownError,
    "unsupported": UnsupportedError,
    "partial": PartialError,
}


def _freeze(value: Any, *, _active: set[int] | None = None, _depth: int = 0) -> Any:
    """Make finite acyclic JSON-compatible evidence immutable."""
    if _depth > 64:
        raise ValueError("evidence nesting exceeds limit")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("evidence values must be finite")
        return value
    if isinstance(value, (Mapping, tuple, list)):
        active = set() if _active is None else _active
        marker = id(value)
        if marker in active:
            raise ValueError("cyclic evidence value")
        active.add(marker)
        try:
            if isinstance(value, Mapping):
                if not all(isinstance(key, str) for key in value):
                    raise ValueError("evidence mapping keys must be strings")
                return MappingProxyType({key: _freeze(item, _active=active, _depth=_depth + 1) for key, item in value.items()})
            return tuple(_freeze(item, _active=active, _depth=_depth + 1) for item in value)
        finally:
            active.remove(marker)
    raise ValueError(f"evidence value is not JSON-compatible: {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    frozen = _freeze(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _records(value: tuple[Mapping[str, Any], ...], name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    return tuple(_mapping(record, name) for record in value)


def _strings(value: tuple[str, ...], name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be a tuple of non-empty strings")
    return value


def _required(payload: Mapping[str, Any], fields: tuple[str, ...], name: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{name} payload must be a mapping")
    keys = set(payload)
    expected = set(fields)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise ValueError(f"{name} payload fields mismatch: missing={missing}, extra={extra}")
    return dict(payload)


def validate_scope(scope: Mapping[str, Any]) -> dict[str, str]:
    """Validate and detach a scope, which is always board plus anchor identity."""
    data = _required(scope, ("board_id", "anchor_task_id"), "scope")
    if not all(isinstance(data[field], str) and data[field] for field in data):
        raise ValueError("scope board_id and anchor_task_id must be non-empty strings")
    return {"board_id": data["board_id"], "anchor_task_id": data["anchor_task_id"]}


@dataclass(frozen=True, slots=True)
class BoardSnapshot:
    native_task: Mapping[str, Any]
    parents: tuple[Mapping[str, Any], ...]
    runs: tuple[Mapping[str, Any], ...]
    comments: tuple[Mapping[str, Any], ...]
    events: tuple[Mapping[str, Any], ...]
    attachments: tuple[Mapping[str, Any], ...]
    observed_at: str
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "native_task", _mapping(self.native_task, "native_task"))
        for name in ("parents", "runs", "comments", "events", "attachments"):
            object.__setattr__(self, name, _records(getattr(self, name), name))
        if not isinstance(self.observed_at, str) or not self.observed_at:
            raise ValueError("observed_at must be a non-empty string")
        if not isinstance(self.digest, str) or not self.digest:
            raise ValueError("digest must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return _thaw({field: getattr(self, field) for field in self.__dataclass_fields__})

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BoardSnapshot":
        data = _required(payload, tuple(cls.__dataclass_fields__), cls.__name__)
        for field in ("parents", "runs", "comments", "events", "attachments"):
            if not isinstance(data[field], list):
                raise ValueError(f"{field} must be a transport list")
            data[field] = tuple(data[field])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ManagedMember:
    board_id: str
    anchor_task_id: str
    task_id: str
    role: str
    generation: int
    finding_ids: tuple[str, ...]
    work_association: str

    def __post_init__(self) -> None:
        validate_scope({"board_id": self.board_id, "anchor_task_id": self.anchor_task_id})
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("role must be a non-empty string")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        object.__setattr__(self, "finding_ids", _strings(self.finding_ids, "finding_ids"))
        if not isinstance(self.work_association, str) or not self.work_association:
            raise ValueError("work_association must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {**validate_scope({"board_id": self.board_id, "anchor_task_id": self.anchor_task_id}), "task_id": self.task_id, "role": self.role, "generation": self.generation, "finding_ids": list(self.finding_ids), "work_association": self.work_association}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ManagedMember":
        data = _required(payload, tuple(cls.__dataclass_fields__), cls.__name__)
        if not isinstance(data["finding_ids"], list):
            raise ValueError("finding_ids must be a transport list")
        data["finding_ids"] = tuple(data["finding_ids"])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    repository_identity: str
    worktree: str
    base_sha: str
    head_sha: str
    content_identity: str
    diff_identity: str
    originating_run_id: str
    contract_hash: str

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            value = getattr(self, field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field} must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidateIdentity":
        return cls(**_required(payload, tuple(cls.__dataclass_fields__), cls.__name__))


@dataclass(frozen=True, slots=True)
class OperationIntent:
    key: str
    scope: Mapping[str, str]
    target: Mapping[str, Any]
    effect: str
    expected_observed_identity: str
    before_evidence: Mapping[str, Any]
    outcome: str | None
    readback: Mapping[str, Any] | None
    retry: Mapping[str, Any]
    phase: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("key must be a non-empty string")
        object.__setattr__(self, "scope", MappingProxyType(validate_scope(self.scope)))
        if not isinstance(self.effect, str) or not self.effect or self.effect == "set_state":
            raise ValueError("effect must be a non-empty operation, not set_state")
        if not isinstance(self.expected_observed_identity, str) or not self.expected_observed_identity:
            raise ValueError("expected_observed_identity must be a non-empty string")
        if self.outcome is not None and (not isinstance(self.outcome, str) or not self.outcome):
            raise ValueError("outcome must be a non-empty string or None")
        if self.phase not in _EFFECT_PHASES:
            raise ValueError("phase must be pending, applied, or unknown")
        object.__setattr__(self, "target", _mapping(self.target, "target"))
        object.__setattr__(self, "before_evidence", _mapping(self.before_evidence, "before_evidence"))
        object.__setattr__(self, "retry", _mapping(self.retry, "retry"))
        if self.readback is not None:
            object.__setattr__(self, "readback", _mapping(self.readback, "readback"))

    def to_dict(self) -> dict[str, Any]:
        return _thaw({field: getattr(self, field) for field in self.__dataclass_fields__})

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperationIntent":
        return cls(**_required(payload, tuple(cls.__dataclass_fields__), cls.__name__))


@dataclass(frozen=True, slots=True)
class PauseIntent:
    scope: Mapping[str, str]
    origin: str
    generation: int
    stop_requested: bool
    cancellation_requested: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", MappingProxyType(validate_scope(self.scope)))
        if self.origin not in {"operator", "automatic"}:
            raise ValueError("origin must be operator or automatic")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if not isinstance(self.stop_requested, bool) or not isinstance(self.cancellation_requested, bool):
            raise ValueError("pause flags must be booleans")

    def to_dict(self) -> dict[str, Any]:
        return _thaw({field: getattr(self, field) for field in self.__dataclass_fields__})

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PauseIntent":
        return cls(**_required(payload, tuple(cls.__dataclass_fields__), cls.__name__))


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    scope: Mapping[str, str]
    problem: str
    attempted_actions: tuple[str, ...]
    preserved_work: tuple[str, ...]
    active_workers: tuple[str, ...]
    required_operator_action: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", MappingProxyType(validate_scope(self.scope)))
        if not isinstance(self.problem, str) or not self.problem:
            raise ValueError("problem must be a non-empty string")
        for field in ("attempted_actions", "preserved_work", "active_workers"):
            object.__setattr__(self, field, _strings(getattr(self, field), field))
        if self.required_operator_action is not None and (not isinstance(self.required_operator_action, str) or not self.required_operator_action):
            raise ValueError("required_operator_action must be a non-empty string or None")

    def to_dict(self) -> dict[str, Any]:
        return _thaw({field: getattr(self, field) for field in self.__dataclass_fields__})

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RecoveryReport":
        data = _required(payload, tuple(cls.__dataclass_fields__), cls.__name__)
        for field in ("attempted_actions", "preserved_work", "active_workers"):
            if not isinstance(data[field], list):
                raise ValueError(f"{field} must be a transport list")
            data[field] = tuple(data[field])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class Action:
    key: str
    scope: Mapping[str, str]
    target: Mapping[str, Any]
    effect: str
    expected_observed_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("key must be a non-empty string")
        object.__setattr__(self, "scope", MappingProxyType(validate_scope(self.scope)))
        object.__setattr__(self, "target", _mapping(self.target, "target"))
        if not isinstance(self.effect, str) or not self.effect or self.effect == "set_state":
            raise ValueError("effect must be a non-empty operation, not set_state")
        if not isinstance(self.expected_observed_identity, str) or not self.expected_observed_identity:
            raise ValueError("expected_observed_identity must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return _thaw({field: getattr(self, field) for field in self.__dataclass_fields__})

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Action":
        return cls(**_required(payload, tuple(cls.__dataclass_fields__), cls.__name__))


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_key: str
    outcome: str
    details: str
    readback: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        if not isinstance(self.action_key, str) or not self.action_key:
            raise ValueError("action_key must be a non-empty string")
        if self.outcome not in _ACTION_OUTCOMES:
            raise ValueError("outcome is not a supported action result")
        if not isinstance(self.details, str):
            raise ValueError("details must be a string")
        if self.readback is not None:
            object.__setattr__(self, "readback", _mapping(self.readback, "readback"))

    @property
    def error(self) -> ContractError | None:
        error_type = _ERROR_BY_OUTCOME.get(self.outcome)
        return error_type(self.details) if error_type else None

    def to_dict(self) -> dict[str, Any]:
        return _thaw({field: getattr(self, field) for field in self.__dataclass_fields__})

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionResult":
        return cls(**_required(payload, tuple(cls.__dataclass_fields__), cls.__name__))


__all__ = [
    "Action",
    "ActionResult",
    "BoardSnapshot",
    "CandidateIdentity",
    "ConflictError",
    "ContractError",
    "ManagedMember",
    "OperationIntent",
    "PartialError",
    "PauseIntent",
    "RecoveryReport",
    "UnknownError",
    "UnsupportedError",
    "validate_scope",
]
