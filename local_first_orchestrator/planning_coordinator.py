"""Governed, proposal-only decomposition planning.

The ``hermes chat`` executable is only a transport used by an explicitly
trusted planner constructed with ``cost_class='local'``.  Its executable name
does not determine cost policy; paid planners must be paid-model adapters.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from .decomposition import (
    DecompositionPlan,
    FeatureContract,
    PlanValidationResult,
    PlanValidator,
    activate_validated_plan,
)
from .decomposition_planner import PlannerError, LocalDecompositionPlanner, packet, parse
from .paid_model import PaidInvocationError, PaidModelAdapter
from .repository_snapshot import RepositoryPlanValidator, RepositorySnapshot, snapshot
from .usage_governor import PaidPurpose
from .controller import RuntimeConfig
from .ledger import Ledger


class Planner(Protocol):
    cost_class: str


@dataclass(frozen=True)
class PlanningOutcome:
    status: str
    feature_id: str
    request_key: str | None = None
    snapshot_hash: str | None = None
    plan_id: str | None = None
    activated_ticket_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


class PlanningCoordinator:
    """Coordinates proposal-only planning; it never projects or executes tickets."""

    def __init__(
        self,
        ledger: Ledger,
        config: RuntimeConfig,
        planner: Planner,
        *,
        artifact_root: Path | None = None,
        plan_validator: PlanValidator | None = None,
        repository_validator: RepositoryPlanValidator | None = None,
        planner_identity: str | None = None,
    ) -> None:
        self.ledger = ledger
        self.config = config
        self.planner = planner
        self.artifact_root = Path(artifact_root or config.artifact_root)
        self.plan_validator = plan_validator or PlanValidator()
        self.repository_validator = repository_validator or RepositoryPlanValidator()
        self.planner_identity = planner_identity or type(planner).__name__

    def _cost_class(self) -> str:
        value = getattr(self.planner, "cost_class", "unknown")
        return value if value in {"local", "paid", "unknown"} else "unknown"

    def _request_key(self, feature: FeatureContract, snap: RepositorySnapshot) -> str:
        material = {
            "architecture_purpose": PaidPurpose.ARCHITECTURE.value,
            "feature_id": feature.id,
            "contract_hash": feature.contract_hash,
            "repo_base_sha": snap.base_sha,
            "repo_snapshot_hash": snap.snapshot_hash,
            "repository_identity": snap.repository_id,
            "planner_identity": self.planner_identity,
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _artifact_dir(self, feature: FeatureContract, request_key: str) -> Path:
        return self.artifact_root / feature.id / request_key

    @staticmethod
    def _plan_json(plan: DecompositionPlan) -> str:
        return json.dumps(asdict(plan), sort_keys=True, separators=(",", ":"))

    def _record(self, request_key: str, feature: FeatureContract, snap: RepositorySnapshot, *, status: str, artifact: Path | None = None, structural: tuple[str, ...] = (), repository: tuple[str, ...] = (), plan_id: str | None = None, ticket_ids: tuple[str, ...] = ()) -> None:
        now = self.ledger._now()
        with self.ledger._transaction() as conn:
            conn.execute(
                """INSERT INTO planning_runs(request_key,feature_id,contract_hash,repo_base_sha,repo_snapshot_hash,planner_identity,cost_class,status,response_artifact,structural_reasons_json,repository_reasons_json,plan_id,ticket_ids_json,created_at,updated_at,repository_identity,repo_snapshot_manifest_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,response_artifact=excluded.response_artifact,structural_reasons_json=excluded.structural_reasons_json,repository_reasons_json=excluded.repository_reasons_json,plan_id=excluded.plan_id,ticket_ids_json=excluded.ticket_ids_json,updated_at=excluded.updated_at""",
                (request_key, feature.id, feature.contract_hash, snap.base_sha, snap.snapshot_hash, self.planner_identity, self._cost_class(), status, str(artifact) if artifact else None, json.dumps(structural), json.dumps(repository), plan_id, json.dumps(ticket_ids), now, now, snap.repository_id, snap.manifest_json),
            )

    def _existing_activated(self, feature: FeatureContract, snap: RepositorySnapshot) -> PlanningOutcome | None:
        rows = self.ledger.connection.execute("SELECT * FROM decomposition_plans WHERE feature_id=? AND status='active' ORDER BY activated_at", (feature.id,)).fetchall()
        for row in rows:
            try:
                stored = json.loads(row["plan_json"])
                plan = parse(json.dumps(stored["plan"], sort_keys=True, separators=(",", ":")))
            except (PlannerError, KeyError, TypeError, ValueError):
                continue
            if (plan.feature_contract_hash == feature.contract_hash and plan.repository_identity == snap.repository_id and plan.repo_base_sha == snap.base_sha and plan.repo_snapshot_hash == snap.snapshot_hash and plan.repo_snapshot_manifest_json == snap.manifest_json and row["repository_identity"] == snap.repository_id and row["repo_base_sha"] == snap.base_sha and row["repo_snapshot_hash"] == snap.snapshot_hash and row["repo_snapshot_manifest_json"] == snap.manifest_json):
                ids = tuple(r["id"] for r in self.ledger.connection.execute("SELECT t.id FROM tickets t JOIN tranches tr ON tr.id=t.tranche_id WHERE t.feature_id=? AND tr.ordinal=0 ORDER BY t.id", (feature.id,)))
                return PlanningOutcome("already_activated", feature.id, snapshot_hash=snap.snapshot_hash, plan_id=str(row["id"]), activated_ticket_ids=ids)
        return None

    @staticmethod
    def _proposal_text(value: dict[str, Any]) -> str:
        candidate = value.get("plan", value)
        return json.dumps(candidate, sort_keys=True, separators=(",", ":"))

    def materialize_next_tranche(self, feature_id: str) -> PlanningOutcome:
        """Complete the active tranche and materialize exactly the next coarse tranche."""
        from .tranche_completion import TrancheNotComplete, completion_evidence
        row = self.ledger.connection.execute("SELECT * FROM feature_contracts WHERE feature_id=?", (feature_id,)).fetchone()
        stored_plan = self.ledger.connection.execute("SELECT * FROM decomposition_plans WHERE feature_id=? ORDER BY created_at LIMIT 1", (feature_id,)).fetchone()
        if row is None or stored_plan is None: return PlanningOutcome("planner_failed", feature_id, reasons=("feature plan missing",))
        raw = json.loads(row["contract_json"])
        from .decomposition import Criterion
        feature = FeatureContract(raw["id"], raw["title"], raw["objective"], tuple(Criterion(x["id"], x["statement"], x.get("verification_hint", "")) for x in raw["acceptance_criteria"]), tuple(raw["non_goals"]), tuple(raw["invariants"]), tuple(raw["constraints"]), raw["source_revision"])
        stored = json.loads(stored_plan["plan_json"]); coarse_plan = parse(json.dumps(stored["plan"], sort_keys=True, separators=(",", ":")))
        active_row = self.ledger.connection.execute("SELECT * FROM tranches WHERE feature_id=? AND status='active' ORDER BY ordinal", (feature_id,)).fetchone()
        if active_row is None: return PlanningOutcome("planner_failed", feature_id, reasons=("active tranche missing",))
        if int(active_row["ordinal"]) > 0: return PlanningOutcome("already_materialized", feature_id, plan_id=str(stored_plan["id"]))
        active = next(t for t in coarse_plan.tranches if t.id == active_row["id"])
        repository = self.config.canonical_repository(self.config.repository)
        from .corrections import CorrectionService
        authority = CorrectionService(self.ledger, repository).completion_authority(active.id)
        if not authority["authorized"]:
            return PlanningOutcome("waiting_not_complete", feature_id, reasons=(f"predecessor completion authority is {authority['status']}",))
        try:
            completion = authority["completion"]
            if authority["kind"] == "h1":
                completion = completion_evidence(self.ledger, repository, active.id)
            if completion is None:
                raise TrancheNotComplete("durable predecessor completion is missing")
        except (TrancheNotComplete, OSError, subprocess.CalledProcessError) as exc: return PlanningOutcome("waiting_not_complete", feature_id, reasons=(str(exc),))
        next_coarse = next((t for t in coarse_plan.tranches if t.ordinal == active.ordinal + 1), None)
        if next_coarse is None:
            self.ledger.record_tranche_completion(completion)
            return PlanningOutcome("feature_complete_candidate", feature_id, reasons=("no later coarse tranche",))
        final = authority["final_integration_sha"]
        snap = snapshot(self.config.canonical_repository(self.config.repository), final, feature)
        artifact_dir = self._artifact_dir(feature, self._request_key(feature, snap) + "-" + next_coarse.id); artifact_dir.mkdir(parents=True, exist_ok=True)
        try:
            proposer = getattr(self.planner, "propose_next", None) or getattr(self.planner, "propose")
            proposal = proposer(feature, snap, coarse_tranche=next_coarse, completion_evidence=completion, artifact_dir=artifact_dir) if getattr(self.planner, "propose_next", None) else proposer(feature, snap, artifact_dir=artifact_dir)
        except TypeError:
            proposal = self.planner.propose(feature, snap, artifact_dir=artifact_dir)  # type: ignore[attr-defined]
        proposal = replace(proposal, feature_id=feature.id, feature_contract_hash=feature.contract_hash, repository_identity=snap.repository_id, repo_base_sha=snap.base_sha, repo_snapshot_hash=snap.snapshot_hash, repo_snapshot_manifest_json=snap.manifest_json, tranches=(replace(proposal.tranches[0], id=next_coarse.id, ordinal=0),))
        validation = self.plan_validator.validate_tranche(feature, proposal.tranches[0], next_coarse)
        repository_validation = self.repository_validator.validate(proposal, snap)
        if not validation.passed: return PlanningOutcome("structural_rejected", feature_id, reasons=validation.reasons)
        if not repository_validation.passed: return PlanningOutcome("repository_rejected", feature_id, reasons=repository_validation.reasons)
        created = self.ledger.materialize_next_tranche(feature=feature, tranche=proposal.tranches[0], plan=proposal, completion=completion)
        return PlanningOutcome("activated", feature_id, snapshot_hash=snap.snapshot_hash, plan_id=str(stored_plan["id"]), activated_ticket_ids=created)

    def _load_plan(self, row: object) -> DecompositionPlan:
        stored = json.loads(row["plan_json"])
        return parse(json.dumps(stored["plan"], sort_keys=True, separators=(",", ":")))

    def _plan_fingerprint(self, feature: FeatureContract, proposal: DecompositionPlan) -> tuple[str, str, str]:
        raw = json.dumps({"feature": feature.__dict__, "plan": asdict(proposal)}, default=lambda x: x.__dict__ if hasattr(x, "__dict__") else list(x), sort_keys=True, separators=(",", ":"))
        return "plan-" + hashlib.sha256(raw.encode()).hexdigest()[:16], hashlib.sha256(raw.encode()).hexdigest(), raw

    def _existing_pending(self, feature: FeatureContract, snap: RepositorySnapshot) -> PlanningOutcome | None:
        rows = self.ledger.connection.execute("SELECT * FROM decomposition_plans WHERE feature_id=? ORDER BY created_at", (feature.id,)).fetchall()
        for row in rows:
            if row["status"] != "validated_pending_activation":
                continue
            if (row["repository_identity"], row["repo_base_sha"], row["repo_snapshot_hash"], row["repo_snapshot_manifest_json"]) != (snap.repository_id, snap.base_sha, snap.snapshot_hash, snap.manifest_json):
                raise ValueError("conflicting durable decomposition plan")
            plan = self._load_plan(row)
            if plan.feature_contract_hash != feature.contract_hash:
                raise ValueError("conflicting durable decomposition plan")
            run = self.ledger.connection.execute("SELECT request_key FROM planning_runs WHERE plan_id=? ORDER BY updated_at DESC LIMIT 1", (row["id"],)).fetchone()
            return PlanningOutcome("validated_pending_activation", feature.id, request_key=run["request_key"] if run else None, snapshot_hash=snap.snapshot_hash, plan_id=str(row["id"]))
        return None

    def generate_plan_only(self, feature: FeatureContract, *, repository: Path | None = None, feature_terms: tuple[str, ...] = ()) -> PlanningOutcome:
        """Generate, validate, and persist one plan without activation or tickets."""
        if self._cost_class() == "unknown":
            return PlanningOutcome("unknown_cost_class", feature.id, reasons=("planner cost class is not trusted",))
        repo = self.config.canonical_repository(self.config.repository)
        if repository is not None and self.config.canonical_repository(repository) != repo:
            raise ValueError("repository is not the controller-approved canonical repository")
        base = feature.source_revision.strip() or subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
        snap = snapshot(repo, base, feature, feature_terms)
        active = self._existing_activated(feature, snap)
        if active:
            return active
        pending = self._existing_pending(feature, snap)
        if pending:
            return pending
        request_key = self._request_key(feature, snap)
        artifact_dir = self._artifact_dir(feature, request_key)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        response_path = artifact_dir / "planner-response.json"
        prior = self.ledger.connection.execute("SELECT * FROM planning_runs WHERE request_key=?", (request_key,)).fetchone()
        try:
            if prior is not None and prior["status"] in {"structural_rejected", "repository_rejected", "validated_pending_activation", "completed"} and prior["response_artifact"] and Path(prior["response_artifact"]).exists():
                proposal = parse(Path(prior["response_artifact"]).read_text(encoding="utf-8"))
            elif self._cost_class() == "local":
                proposal = self.planner.propose(feature, snap, artifact_dir=artifact_dir)  # type: ignore[attr-defined]
                if not response_path.exists():
                    response_path.write_text(self._plan_json(proposal), encoding="utf-8")
            else:
                request_payload = {"feature_id": feature.id, "contract_hash": feature.contract_hash, "repo_base_sha": snap.base_sha, "repo_snapshot_hash": snap.snapshot_hash, "repository_identity": snap.repository_id, "repo_snapshot_manifest_json": snap.manifest_json, "planner_identity": self.planner_identity, "purpose": PaidPurpose.ARCHITECTURE.value}
                result = self.planner.invoke(feature.id, PaidPurpose.ARCHITECTURE, request_key, {"request": request_payload, "prompt": packet(feature, snap)})  # type: ignore[attr-defined]
                response_path.write_text(self._proposal_text(result), encoding="utf-8")
                proposal = parse(response_path.read_text(encoding="utf-8"))
        except PaidInvocationError as exc:
            message = str(exc)
            return PlanningOutcome("budget_exhausted" if "budget exhausted" in message else "planner_ambiguous", feature.id, request_key, snap.snapshot_hash, reasons=(message,))
        except PlannerError as exc:
            return PlanningOutcome("planner_timeout" if "timeout" in str(exc) else "planner_failed", feature.id, request_key, snap.snapshot_hash, reasons=(str(exc),))
        except (OSError, TypeError, ValueError, KeyError, TimeoutError) as exc:
            return PlanningOutcome("planner_failed", feature.id, request_key, snap.snapshot_hash, reasons=(str(exc),))
        proposal = replace(proposal, repository_identity=snap.repository_id, repo_base_sha=snap.base_sha, repo_snapshot_hash=snap.snapshot_hash, repo_snapshot_manifest_json=snap.manifest_json)
        structural = self.plan_validator.validate(feature, proposal)
        if not structural.passed:
            self._record(request_key, feature, snap, status="structural_rejected", artifact=response_path, structural=structural.reasons)
            return PlanningOutcome("structural_rejected", feature.id, request_key, snap.snapshot_hash, reasons=structural.reasons)
        repository_validation = self.repository_validator.validate(proposal, snap)
        if not repository_validation.passed:
            self._record(request_key, feature, snap, status="repository_rejected", artifact=response_path, repository=repository_validation.reasons)
            return PlanningOutcome("repository_rejected", feature.id, request_key, snap.snapshot_hash, reasons=repository_validation.reasons)
        plan_id, fingerprint, raw = self._plan_fingerprint(feature, proposal)
        persisted = self.ledger.persist_validated_decomposition_plan(plan_id=plan_id, feature_id=feature.id, fingerprint=fingerprint, plan_json=json.dumps({"feature": feature.__dict__, "plan": asdict(proposal)}, default=lambda x: x.__dict__ if hasattr(x, "__dict__") else list(x), sort_keys=True, separators=(",", ":")), repository_identity=snap.repository_id, repo_base_sha=snap.base_sha, repo_snapshot_hash=snap.snapshot_hash, repo_snapshot_manifest_json=snap.manifest_json)
        self._record(request_key, feature, snap, status="validated_pending_activation", artifact=response_path, plan_id=persisted)
        return PlanningOutcome("validated_pending_activation", feature.id, request_key, snap.snapshot_hash, persisted)

    def activate_persisted_plan(self, feature: FeatureContract, *, request_key: str, plan_id: str) -> PlanningOutcome:
        row = self.ledger.connection.execute("SELECT * FROM decomposition_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None: raise ValueError("persisted decomposition plan missing")
        proposal = self._load_plan(row)
        structural = self.plan_validator.validate(feature, proposal)
        snap = snapshot(self.config.canonical_repository(self.config.repository), proposal.repo_base_sha, feature)
        repository_validation = self.repository_validator.validate(proposal, snap)
        activated_id, ticket_ids = activate_validated_plan(self.ledger, feature, proposal, structural, repository_validation)
        self.ledger.finalize_planning_run_activation(request_key, plan_id=activated_id, ticket_ids=ticket_ids)
        return PlanningOutcome("activated", feature.id, request_key, snap.snapshot_hash, activated_id, ticket_ids)

    def plan(self, feature: FeatureContract, *, repository: Path | None = None, feature_terms: tuple[str, ...] = ()) -> PlanningOutcome:
        """Legacy combined planning path: generate/persist, then activate."""
        outcome = self.generate_plan_only(feature, repository=repository, feature_terms=feature_terms)
        if outcome.status == "already_activated":
            return outcome
        if outcome.status != "validated_pending_activation":
            return outcome
        return self.activate_persisted_plan(feature, request_key=str(outcome.request_key), plan_id=str(outcome.plan_id))
