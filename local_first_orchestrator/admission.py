from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .decomposition import Criterion, FeatureContract
from .evidence_hash import canonical_sha256
from .source_languages import normalized_repository_path


@dataclass(frozen=True)
class FileDisposition:
    path: str
    disposition: str

    def __post_init__(self) -> None:
        if self.disposition not in {"modify", "create"}:
            raise ValueError("file disposition must be modify or create")
        if normalized_repository_path(self.path) is None:
            raise ValueError("file path must be normalized and repository-relative")


@dataclass(frozen=True)
class FeatureAdmissionSpec:
    feature_id: str
    feature_title: str
    tranche_id: str
    tranche_title: str
    objective: str
    source_revision: str
    acceptance_criteria: tuple[Criterion, ...]
    non_goals: tuple[str, ...]
    invariants: tuple[str, ...]
    constraints: tuple[str, ...]
    files: tuple[FileDisposition, ...]
    predecessor_tranche_id: str | None = None

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.feature_id, self.feature_title, self.tranche_id, self.tranche_title, self.objective, self.source_revision)):
            raise ValueError("feature admission identity and objective are required")
        if not self.acceptance_criteria or len({c.id for c in self.acceptance_criteria}) != len(self.acceptance_criteria):
            raise ValueError("acceptance criteria must be non-empty and unique")
        if not self.files or len({f.path for f in self.files}) != len(self.files):
            raise ValueError("file dispositions must be non-empty and unique")
        if self.predecessor_tranche_id == self.tranche_id:
            raise ValueError("feature cannot depend on itself")

    @property
    def contract(self) -> FeatureContract:
        return FeatureContract(
            self.feature_id,
            self.feature_title,
            self.objective,
            self.acceptance_criteria,
            self.non_goals,
            self.invariants,
            self.constraints,
            self.source_revision,
        )

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "feature_title": self.feature_title,
            "tranche_id": self.tranche_id,
            "tranche_title": self.tranche_title,
            "objective": self.objective,
            "source_revision": self.source_revision,
            "acceptance_criteria": [c.__dict__ for c in self.acceptance_criteria],
            "non_goals": list(self.non_goals),
            "invariants": list(self.invariants),
            "constraints": list(self.constraints),
            "files": [f.__dict__ for f in self.files],
            "predecessor_tranche_id": self.predecessor_tranche_id,
        }

    @property
    def contract_hash(self) -> str:
        return canonical_sha256(self.canonical_payload)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "FeatureAdmissionSpec":
        allowed = {"feature_id", "feature_title", "tranche_id", "tranche_title", "objective", "source_revision", "acceptance_criteria", "non_goals", "invariants", "constraints", "files", "predecessor_tranche_id"}
        if set(raw) - allowed or not allowed - set(raw):
            raise ValueError("feature admission specification has unknown or missing fields")
        criteria = tuple(Criterion(str(x["id"]), str(x["statement"]), str(x.get("verification_hint", ""))) for x in raw["acceptance_criteria"])
        files = tuple(FileDisposition(str(x["path"]), str(x["disposition"])) for x in raw["files"])
        return cls(str(raw["feature_id"]), str(raw["feature_title"]), str(raw["tranche_id"]), str(raw["tranche_title"]), str(raw["objective"]), str(raw["source_revision"]), criteria, tuple(map(str, raw["non_goals"])), tuple(map(str, raw["invariants"])), tuple(map(str, raw["constraints"])), files, None if raw["predecessor_tranche_id"] is None else str(raw["predecessor_tranche_id"]))


@dataclass(frozen=True)
class FeatureAdmissionResult:
    feature_id: str
    tranche_id: str
    contract_hash: str
    repository_identity: str
    repo_base_sha: str
    repo_snapshot_hash: str
    predecessor_tranche_id: str | None
    predecessor_authority_kind: str | None
    predecessor_generation: int | None
