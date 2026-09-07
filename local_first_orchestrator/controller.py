from __future__ import annotations

import hashlib
import json
import subprocess
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .admission import FeatureAdmissionResult, FeatureAdmissionSpec
from .context_packet import ContextPacketBuilder
from .evidence_hash import canonical_sha256
from .git_adapter import AttemptWorktree, GitWorktreeAdapter
from .historical_revalidation import attestation_hash_from_row, authorization_hash_from_row, classify_obsolete_validation_failure, derive_obsolete_validation_failure, historical_validation_result_hash
from .ledger import Ledger, _completion_evidence_hash, _recheck_evidence_hash
from .local_qwen import LocalQwenAdapter
from .readiness import validate_ticket
from .review import LocalReviewAdapter, ReviewPacketBuilder, SameTicketRepairCoordinator, normalize_review
from .repository_snapshot import snapshot as repository_snapshot
from .corrections import CorrectionService
from .states import CanonicalState
from .ticket import MicroTicket, PatchBudget, VerificationProfile
from .validation import DeterministicValidator


@dataclass(frozen=True)
class RuntimeConfig:
    repository: Path
    worktree_root: Path
    artifact_root: Path
    repository_allowlist: tuple[Path, ...] = ()
    lease_seconds: int = 1800
    implementation_timeout_seconds: int = 300
    review_timeout_seconds: int = 300

    def canonical_repository(self, candidate: Path) -> Path:
        path = Path(candidate).resolve(strict=True)
        roots = tuple(Path(root).resolve(strict=True) for root in (self.repository_allowlist or (self.repository,)))
        if path not in roots: raise ValueError("repository is not an exact configured allowlist root")
        if not (path / ".git").exists(): raise ValueError("repository must be a Git checkout, not a broad directory")
        return path

    @staticmethod
    def _inside(candidate: Path, parent: Path) -> bool:
        try:
            candidate.relative_to(parent)
            return True
        except ValueError:
            return False

    def validate_execution_roots(self) -> tuple[Path, Path, Path]:
        """Resolve and reject controller-owned output beneath the canonical repo.

        ``Path.resolve`` canonicalizes ``..`` and follows extant symlink aliases;
        containment is therefore structural rather than a fragile string prefix.
        This check intentionally runs before any output directory is created.
        """
        repository = self.canonical_repository(self.repository)
        worktree_root = Path(self.worktree_root).expanduser().resolve()
        artifact_root = Path(self.artifact_root).expanduser().resolve()
        if self._inside(worktree_root, repository):
            raise ValueError("unsafe_worktree_root: must resolve outside canonical_repository")
        if self._inside(artifact_root, repository):
            raise ValueError("unsafe_artifact_root: must resolve outside canonical_repository")
        if not isinstance(self.implementation_timeout_seconds, int) or not 1 <= self.implementation_timeout_seconds <= 21_600:
            raise ValueError("invalid_implementation_timeout_seconds")
        if not isinstance(self.review_timeout_seconds, int) or not 1 <= self.review_timeout_seconds <= 21_600:
            raise ValueError("invalid_review_timeout_seconds")
        return repository, worktree_root, artifact_root


def ticket_from_ledger(row: dict[str, Any]) -> MicroTicket:
    verification = json.loads(row["verification_json"])
    return MicroTicket(row["id"], row["objective"], tuple(json.loads(row["criterion_ids_json"])), row["primary_symbol"], tuple(json.loads(row["allowed_files_json"])), tuple(json.loads(row["forbidden_changes_json"])), PatchBudget(**json.loads(row["patch_budget_json"])), VerificationProfile(tuple(tuple(c) for c in verification["commands"]), verification.get("working_directory", "."), int(verification.get("timeout_seconds", 60)), int(verification.get("output_limit", 20000))), row["risk"], bool(row["review_required"]), int(row["max_attempts"]), tuple(json.loads(row["dependencies_json"])), tuple(json.loads(row.get("new_test_files_json") or "[]")))


class InjectedCrash(RuntimeError):
    """Test-only crash marker; completed stages remain resumable."""


class LocalFirstController:
    def __init__(self, ledger: Ledger, board: Any, config: RuntimeConfig, *, local_model: LocalQwenAdapter | None = None, fault_injector: Callable[[str], None] | None = None) -> None:
        if getattr(board, "is_fake", False): raise ValueError("production controller refuses FakeBoardAdapter")
        self.ledger, self.board, self.config = ledger, board, config
        self.local_model = local_model or LocalQwenAdapter()
        self.fault_injector = fault_injector

    def _parse_card(self, card: Any) -> MicroTicket:
        if card.status != "scheduled": raise ValueError("ineligible card: status must be scheduled")
        if "<!-- local-first-orchestrator -->" not in card.body: raise ValueError("ineligible card: missing local-first ownership marker")
        marker = "```local-first-contract\n"
        if marker not in card.body: raise ValueError("ineligible card: missing local-first contract block")
        try: raw = json.loads(card.body.split(marker, 1)[1].split("```", 1)[0])
        except json.JSONDecodeError as exc: raise ValueError("ineligible card: malformed Qwen-ready contract") from exc
        required=("objective","criterion_ids","primary_symbol","allowed_files","forbidden_changes","patch_budget","verification","risk","review_required","max_attempts")
        missing=[key for key in required if key not in raw]
        if missing: raise ValueError("missing Qwen-ready fields: " + ", ".join(missing))
        return validate_ticket(MicroTicket(card.id,raw["objective"],tuple(raw["criterion_ids"]),raw["primary_symbol"],tuple(raw["allowed_files"]),tuple(raw["forbidden_changes"]),PatchBudget(**raw["patch_budget"]),VerificationProfile(tuple(tuple(x) for x in raw["verification"]["commands"]),raw["verification"].get("working_directory","."),int(raw["verification"].get("timeout_seconds",60)),int(raw["verification"].get("output_limit",20000))),raw["risk"],bool(raw["review_required"]),int(raw["max_attempts"]),tuple(raw.get("dependencies",())),tuple(raw.get("new_test_files",()))))

    def import_card(self, card: Any) -> str:
        ticket=self._parse_card(card)
        try: repo=self.config.canonical_repository(Path(card.workspace_path or self.config.repository))
        except FileNotFoundError as exc: raise ValueError("repository path does not exist") from exc
        base=subprocess.run(("git","rev-parse","HEAD"),cwd=repo,text=True,capture_output=True,check=True).stdout.strip()
        existing=self.ledger.connection.execute("SELECT id FROM tickets WHERE external_id=?",(card.id,)).fetchone()
        ticket_id=str(existing["id"]) if existing else self.ledger.create_ticket(title=card.title,external_id=card.id,state=CanonicalState.READY_LOCAL,contract=ticket.contract())
        self.ledger.bind_runtime(ticket_id,str(repo),base)
        return ticket_id

    def import_scheduled_cards(self) -> list[str]: return [self.import_card(c) for c in self.board.import_candidates()]
    def dry_run(self, task_id: str) -> dict[str, object]:
        row=self.ledger.get_ticket(task_id); binding=self.ledger.runtime_binding(task_id)
        return {"ticket_id":task_id,"state":row["state"],"repository":binding["repository_path"],"starting_sha":binding["starting_sha"],"would_invoke_model":False,"would_write_board":False,"would_modify_repository":False}

    def admit_feature_contract(self, spec: FeatureAdmissionSpec, *, repository: Path) -> FeatureAdmissionResult:
        """Admit one human-approved feature contract without planning or projection."""
        paused = self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
        if paused is None or int(paused["paused"]) != 1:
            raise PermissionError("feature admission requires Local First paused")
        repo = self.config.canonical_repository(Path(repository))
        if spec.predecessor_tranche_id is None:
            raise ValueError("feature admission requires an authoritative predecessor tranche")
        authority = CorrectionService(self.ledger, repo).completion_authority(spec.predecessor_tranche_id)
        if not authority.get("authorized"):
            raise PermissionError("predecessor completion authority is not authorized")
        rechecks = self.ledger.tranche_completion_rechecks(spec.predecessor_tranche_id)
        kind = str(authority.get("kind") or "")
        if kind not in {"h1", "recheck"}:
            raise PermissionError("unknown predecessor completion authority kind")
        if kind == "recheck":
            generation = authority.get("completion", {}).get("generation")
            if not rechecks or generation is None or int(rechecks[-1]["generation"]) != int(generation) or _recheck_evidence_hash(rechecks[-1]) != str(rechecks[-1]["evidence_hash"]):
                raise PermissionError("predecessor completion recheck evidence is missing or conflicting")
            authority_row = rechecks[-1]
            base = str(authority_row["current_integration_sha"])
            evidence_hash = str(authority_row["evidence_hash"])
            generation_value = int(authority_row["generation"])
        else:
            h1 = authority.get("completion")
            if h1 is None or not h1.get("evidence_hash") or _completion_evidence_hash(h1) != str(h1["evidence_hash"]):
                raise PermissionError("predecessor H1 evidence is missing or conflicting")
            base = str(h1["final_integration_sha"])
            evidence_hash = str(h1["evidence_hash"])
            generation_value = int(h1.get("generation", 0) or 0)
        if str(authority.get("final_integration_sha")) != base:
            raise PermissionError("predecessor integration authority conflicts")
        resolved = subprocess.run(("git", "rev-parse", "--verify", f"{base}^{{commit}}"), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
        if resolved != base:
            raise ValueError("predecessor integration base is not a commit")
        for file in spec.files:
            result = subprocess.run(("git", "cat-file", "-t", f"{base}:{file.path}"), cwd=repo, text=True, capture_output=True)
            object_type = result.stdout.strip()
            if file.disposition == "modify" and (result.returncode != 0 or object_type != "blob"):
                raise ValueError(f"modify target is not a Git blob at authoritative base: {file.path}")
            if file.disposition == "create" and result.returncode == 0:
                raise ValueError(f"create target already exists at authoritative base: {file.path}")
        snap = repository_snapshot(repo, base, feature=spec.contract, feature_terms=tuple(f.path for f in spec.files), limit=32)
        predecessor = {"tranche_id": spec.predecessor_tranche_id, "kind": authority["kind"], "generation": generation_value, "final_integration_sha": base, "evidence_hash": evidence_hash}
        result = self.ledger.admit_feature_contract(spec, repository_identity=str(repo), repo_base_sha=snap.base_sha, repo_snapshot_hash=snap.snapshot_hash, repo_snapshot_manifest_json=snap.manifest_json, predecessor=predecessor)
        return FeatureAdmissionResult(**result)

    def _crash(self, stage: str) -> None:
        if self.fault_injector: self.fault_injector(stage)

    def _attempt(self, worktrees: GitWorktreeAdapter, ticket_id: str, number: int, base: str) -> AttemptWorktree:
        row=self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?",(ticket_id,number)).fetchone()
        if row and row["worktree_path"] and Path(row["worktree_path"]).exists():
            path=Path(row["worktree_path"]); branch=str(row["branch"] or "")
            stage = self.ledger.model_stage(ticket_id, number, "implementation")
            current_diff = worktrees.diff_hash(path)
            if stage and current_diff != stage["diff_hash"]:
                raise RuntimeError("persisted worktree diff does not match; reconciliation required")
            if not stage and current_diff != str(row["pre_diff_hash"] or current_diff):
                raise RuntimeError("persisted worktree diff does not match; reconciliation required")
            return AttemptWorktree(ticket_id,number,base,branch,path,str(row["pre_diff_hash"] or worktrees.diff_hash(path)))
        attempt=worktrees.create_attempt(ticket_id,number,base)
        self.ledger.connection.execute("UPDATE attempts SET base_sha=?, branch=?, worktree_path=?, pre_diff_hash=? WHERE ticket_id=? AND attempt_number=?",(base,attempt.branch,str(attempt.path),attempt.pre_diff_hash,ticket_id,number))
        return attempt

    def _assert_pre_inference_isolation(self, *, repository: Path, attempt: AttemptWorktree, base_sha: str, ticket: MicroTicket, canonical_sha: str) -> None:
        """Fail closed before artifacts or inference can observe an unsafe attempt."""
        attempt_path = attempt.path.resolve()
        try:
            attempt_path.relative_to(repository)
        except ValueError:
            pass
        else:
            raise RuntimeError("unsafe_worktree_root: attempt resolves inside canonical_repository")
        def git(path: Path, *args: str) -> str:
            return subprocess.run(("git", *args), cwd=path, text=True, capture_output=True, check=True).stdout.strip()
        if git(attempt_path, "rev-parse", "--show-toplevel") != str(attempt_path):
            raise RuntimeError("attempt_worktree_identity_mismatch")
        if git(attempt_path, "rev-parse", "HEAD") != base_sha:
            raise RuntimeError("attempt_base_mismatch")
        if git(repository, "rev-parse", "HEAD") != canonical_sha:
            raise RuntimeError("canonical_head_moved_since_admission")
        if git(attempt_path, "status", "--porcelain=v1"):
            raise RuntimeError("attempt_not_clean_before_inference")
        for relative in ticket.new_test_files:
            if (attempt_path / relative).exists():
                raise RuntimeError("new_test_file_present_before_inference")
        for relative in ticket.allowed_files:
            if not (attempt_path / relative).is_file():
                raise RuntimeError("allowed_file_missing_before_inference")

    def _canonical_provenance_sha(self, ticket_id: str) -> str:
        row = self.ledger.connection.execute("SELECT canonical_sha FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
        if row is None or not row[0]:
            row = self.ledger.connection.execute("""SELECT p.base_sha FROM supplemental_correction_tickets sct JOIN supplemental_correction_plans p ON p.correction_plan_id=sct.correction_plan_id WHERE sct.ticket_id=?""", (ticket_id,)).fetchone()
        if row is None or not row[0]:
            row = self.ledger.connection.execute("SELECT repo_base_sha FROM decomposition_plans WHERE feature_id=(SELECT feature_id FROM tickets WHERE id=?) AND status='active' ORDER BY created_at DESC LIMIT 1", (ticket_id,)).fetchone()
        if row is None or not row[0]:
            raise RuntimeError("canonical_repository_provenance_missing")
        return str(row[0])

    def effective_runtime_identity(self) -> dict[str, object]:
        repository, worktree_root, artifact_root = self.config.validate_execution_roots()
        return {"canonical_repository": str(repository), "worktree_root": str(worktree_root), "artifact_root": str(artifact_root), "implementation_timeout_seconds": self.config.implementation_timeout_seconds, "review_timeout_seconds": self.config.review_timeout_seconds, "provider": str(getattr(self.local_model, "provider", type(self.local_model).__name__)), "model": str(getattr(self.local_model, "model", type(self.local_model).__name__))}

    def reconcile_failed_attempt(self, ticket_id: str, *, operator_id: str, classification: str, forensic_artifact_paths: tuple[Path, ...] = ()) -> dict[str, object]:
        """Retire failed history without cleanup or a subsequent model invocation."""
        binding = self.ledger.runtime_binding(ticket_id)
        repository, worktree_root, _ = self.config.validate_execution_roots()
        if str(repository) != binding["repository_path"]:
            raise ValueError("repository mismatch with imported binding")
        base = GitWorktreeAdapter(repository, worktree_root).existing_execution_base(self.ledger.get_ticket(ticket_id)["tranche_id"] or None, str(binding["starting_sha"]))
        paths = tuple(str(Path(path).expanduser().resolve()) for path in forensic_artifact_paths)
        return self.ledger.reconcile_failed_attempt(ticket_id, operator_id=operator_id, classification=classification, retry_base_sha=base, runtime_identity=self.effective_runtime_identity(), forensic_artifact_paths=paths)

    def confirm_retired_attempt_cleanup(self, ticket_id: str, *, operator_id: str) -> dict[str, object]:
        """Verify separately-authorized physical cleanup; never performs it."""
        reconciliation = self.ledger.failed_attempt_reconciliation(ticket_id)
        if reconciliation is None: raise ValueError("ticket has no retired attempt")
        retired = int(reconciliation["retired_attempt_number"])
        attempt = self.ledger.connection.execute("SELECT worktree_path, branch FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, retired)).fetchone()
        if attempt is None: raise RuntimeError("retired attempt history missing")
        repository, worktree_root, _ = self.config.validate_execution_roots()
        binding = self.ledger.runtime_binding(ticket_id)
        if str(repository) != binding["repository_path"]: raise ValueError("repository mismatch with imported binding")
        worktree_path = Path(attempt["worktree_path"])
        artifact_paths = tuple(Path(value) for value in json.loads(reconciliation["forensic_artifact_paths_json"]))
        remaining = [path for path in (worktree_path, *artifact_paths) if path.exists()]
        adapter = GitWorktreeAdapter(repository, worktree_root)
        if remaining or (attempt["branch"] and adapter.branch_exists(str(attempt["branch"]))):
            raise RuntimeError("retired attempt cleanup is incomplete")
        checked_values = [str(worktree_path), *(str(path) for path in artifact_paths)]
        if attempt["branch"]: checked_values.append(f"git-ref:{str(attempt['branch'])}")
        return self.ledger.confirm_retired_attempt_cleanup(ticket_id, retired_attempt_number=retired, operator_id=operator_id, checked_paths=tuple(checked_values))

    def resume_failed_review(self, ticket_id: str, *, operator_id: str) -> dict[str, object]:
        """Authorize only a fresh review of an unchanged frozen candidate."""
        binding=self.ledger.runtime_binding(ticket_id); repository, worktree_root, _=self.config.validate_execution_roots()
        if str(repository) != binding["repository_path"] or self.ledger.incomplete_model_invocations(ticket_id): raise ValueError("runtime or invocation reconciliation prerequisite failed")
        candidate=self.ledger.review_candidate(ticket_id)
        if candidate is None: raise ValueError("validated review candidate evidence unavailable")
        path=Path(candidate.get("worktree_path") or self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=? AND attempt_number=?",(ticket_id,candidate["attempt_number"])).fetchone()["worktree_path"])
        if not path.is_dir(): raise ValueError("validated attempt worktree is missing")
        adapter=GitWorktreeAdapter(repository,worktree_root); fingerprint=adapter.diff_hash(path)
        return self.ledger.authorize_review_resume(ticket_id,operator_id=operator_id,candidate_fingerprint=fingerprint,runtime_identity=self.effective_runtime_identity())

    def _implementation_stage(self, ticket_id: str, *, repository: Path, owner: str, allow_validation_repair: bool, failure_evidence: str = "", explicit_operator: bool = False) -> dict[str, object] | None:
        """Run implementation through validation and candidate freezing only.

        This is the single implementation path used by both the deliberate
        implementation-only operator operation and autonomous ``execute``.
        The boolean is an internal composition policy: the operator operation
        stops on validation failure, while autonomous execution retains its
        historical bounded validation-repair loop.  Neither path enters review.
        """
        binding=self.ledger.runtime_binding(ticket_id); raw_repository=Path(repository).resolve(strict=True)
        if str(raw_repository) != binding["repository_path"]: raise ValueError("repository mismatch with imported binding")
        repo, worktree_root, artifact_root = self.config.validate_execution_roots()
        if repo != raw_repository: raise ValueError("repository mismatch with configured canonical repository")
        ticket=ticket_from_ledger(self.ledger.get_ticket(ticket_id))
        if ticket.risk != "low": raise PermissionError("only low-risk tickets may execute locally")
        if self.ledger.incomplete_model_invocations(ticket_id):
            raise RuntimeError("execution_reconciliation_required: incomplete model invocation")
        reconciliation = self.ledger.failed_attempt_reconciliation(ticket_id)
        if reconciliation is not None and bool(reconciliation["cleanup_required"]) and not self.ledger.cleanup_confirmed(ticket_id, int(reconciliation["retired_attempt_number"])):
            raise RuntimeError("retired attempt cleanup confirmation required before retry execution")
        state=CanonicalState(self.ledger.get_ticket(ticket_id)["state"])
        if state == CanonicalState.READY_LOCAL and not (self.ledger.claim_specific_operator if explicit_operator else self.ledger.claim_specific)(ticket_id,owner,self.config.lease_seconds): return None
        if state in {CanonicalState.NEEDS_TRIAGE,CanonicalState.BLOCKED,CanonicalState.DONE}: return None
        planning_base=str(binding["starting_sha"]); canonical_sha=self._canonical_provenance_sha(ticket_id); worktrees=GitWorktreeAdapter(repo,worktree_root)
        base=worktrees.resolve_execution_base(self.ledger.get_ticket(ticket_id)["tranche_id"] or None, planning_base)
        self.ledger.record_runtime_stage(ticket_id, "execution_base", base)
        reconciliation = self.ledger.failed_attempt_reconciliation(ticket_id)
        if reconciliation is not None:
            attempt_number = int(reconciliation["prospective_next_attempt_number"])
            if self.ledger.connection.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone():
                raise RuntimeError("reconciliation-authorized attempt already exists; reconciliation required")
            attempt_limit = attempt_number
        else:
            latest = self.ledger.connection.execute("SELECT attempt_number, worktree_path FROM attempts WHERE ticket_id=? ORDER BY attempt_number DESC LIMIT 1", (ticket_id,)).fetchone()
            # A crash or same-ticket repair may already own the highest historical
            # attempt's isolated worktree. Resume it; only a retired/reconciled
            # failure is allowed to consume a fresh historical number here.
            if latest is not None and latest["worktree_path"]:
                attempt_number = int(latest["attempt_number"])
            else:
                attempt_number = self.ledger.next_attempt_number(ticket_id)
            attempt_limit = ticket.max_attempts
        repair_evidence=failure_evidence
        try:
            while attempt_number <= attempt_limit:
                existing_attempt=self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=? AND attempt_number=?",(ticket_id,attempt_number)).fetchone()
                fresh_attempt = not bool(existing_attempt and existing_attempt["worktree_path"])
                self.ledger.ensure_attempt(ticket_id,attempt_number); attempt=self._attempt(worktrees,ticket_id,attempt_number,base)
                impl=self.ledger.model_stage(ticket_id,attempt_number,"implementation")
                if not impl:
                    if fresh_attempt:
                        self._assert_pre_inference_isolation(repository=repo,attempt=attempt,base_sha=base,ticket=ticket,canonical_sha=canonical_sha)
                    artifacts_root=artifact_root/ticket_id/str(attempt_number); artifacts_root.mkdir(parents=True,exist_ok=True)
                    packet=ContextPacketBuilder().build_from_repository(ticket,attempt.path,repository_rules="Edit only allowed files. Return JSON only.",failure_evidence=repair_evidence)
                    artifacts=ContextPacketBuilder().write_artifacts(packet,artifact_root=artifacts_root); request_hash=hashlib.sha256(packet.text.encode()).hexdigest()
                    if hasattr(self.local_model, "implementation_timeout_seconds"):
                        self.local_model.implementation_timeout_seconds = self.config.implementation_timeout_seconds
                    invocation_id = uuid.uuid4().hex
                    provider = str(getattr(self.local_model, "provider", type(self.local_model).__name__))
                    model = str(getattr(self.local_model, "model", type(self.local_model).__name__))
                    self.ledger.start_model_invocation(invocation_id=invocation_id,ticket_id=ticket_id,attempt_number=attempt_number,stage="implementation",provider=provider,model=model,packet_hash=request_hash,worktree_path=str(attempt.path),timeout_seconds=self.config.implementation_timeout_seconds)
                    # This is the persistence boundary: the launch record is committed
                    # before the subprocess-capable adapter is entered.
                    self._crash("implementation_invocation_started")
                    started = time.monotonic()
                    try:
                        result=self.local_model.invoke("implementation",packet.text,artifact_dir=artifacts_root,workdir=attempt.path)
                    except subprocess.TimeoutExpired as exc:
                        self.ledger.finish_model_invocation(invocation_id,status="timeout",duration_seconds=time.monotonic()-started,error={"type":"TimeoutExpired","timeout_seconds":self.config.implementation_timeout_seconds,"process":str(exc)[:1000]})
                        raise
                    except Exception as exc:
                        self.ledger.finish_model_invocation(invocation_id,status="process_error",duration_seconds=time.monotonic()-started,error={"type":type(exc).__name__,"message":str(exc)[:1000]})
                        raise
                    response_path=getattr(result,"artifact_path",artifacts_root/"implementation-result.json")
                    self.ledger.finish_model_invocation(invocation_id,status="completed",duration_seconds=time.monotonic()-started,model_artifact=str(response_path))
                    self.ledger.record_model_stage(ticket_id,attempt_number,"implementation",purpose="implementation",adapter=type(self.local_model).__name__,request_hash=request_hash,response_artifact=str(response_path),worktree_path=str(attempt.path),base_sha=base,diff_hash=worktrees.diff_hash(attempt.path))
                    self.ledger.record_runtime_stage(ticket_id,f"implementation-{attempt_number}",str(response_path)); self.ledger.record_runtime_stage(ticket_id,"implementation_completed",str(response_path)); self._crash("implementation_completed")
                else:
                    artifacts_root=artifact_root/ticket_id/str(attempt_number)
                self.ledger.transition(ticket_id,CanonicalState.VERIFYING) if CanonicalState(self.ledger.get_ticket(ticket_id)["state"]) == CanonicalState.IMPLEMENTING else None
                validation=DeterministicValidator(artifact_root=artifacts_root).validate(attempt.path,ticket,base_sha=base)
                validation_artifact_path=validation.full_evidence_path.resolve(); validation_artifact_sha256=hashlib.sha256(validation_artifact_path.read_bytes()).hexdigest()
                validation_record=(json.dumps({"attempt_number":attempt_number,"artifact_path":str(validation_artifact_path),"artifact_sha256":validation_artifact_sha256,"completed":True,"passed":validation.passed,"compact_evidence":validation.compact_evidence},sort_keys=True) if not validation.passed else validation.compact_evidence)
                validation_metadata={"attempt_number":attempt_number,"artifact_path":str(validation_artifact_path),"artifact_sha256":validation_artifact_sha256,"base_sha":base}
                self.ledger.record_runtime_stage(ticket_id,f"validation-{attempt_number}",validation_record,**validation_metadata); self.ledger.record_runtime_stage(ticket_id,"validation_completed",validation_record,**validation_metadata); self._crash("validation_completed")
                if not validation.passed:
                    if not allow_validation_repair or attempt_number >= ticket.max_attempts:
                        self.ledger.transition(ticket_id,CanonicalState.NEEDS_TRIAGE,payload={"validation":validation.compact_evidence}); return None
                    repair_evidence=validation.compact_evidence; self.ledger.transition(ticket_id,CanonicalState.REPAIRING,payload={"validation":repair_evidence}); self.ledger.transition(ticket_id,CanonicalState.IMPLEMENTING); attempt_number+=1; continue
                diff=subprocess.run(("git","diff",base),cwd=attempt.path,text=True,capture_output=True,check=True).stdout
                candidate_fingerprint=hashlib.sha256(diff.encode()).hexdigest()
                self.ledger.transition(ticket_id,CanonicalState.LOCAL_REVIEW) if CanonicalState(self.ledger.get_ticket(ticket_id)["state"]) == CanonicalState.VERIFYING else None
                implementation_invocation=self.ledger.invocation_for_stage(ticket_id,attempt_number,"implementation")
                self.ledger.freeze_review_candidate(ticket_id,attempt_number,candidate_fingerprint=candidate_fingerprint,validation_evidence=validation.compact_evidence,implementation_invocation_id=str(implementation_invocation["invocation_id"]) if implementation_invocation else None,runtime_identity=self.effective_runtime_identity())
                return {"ticket": ticket, "base": base, "worktrees": worktrees, "attempt": attempt, "artifacts_root": artifacts_root, "validation": validation, "diff": diff, "candidate_fingerprint": candidate_fingerprint, "attempt_number": attempt_number}
        except Exception as exc:
            current=CanonicalState(self.ledger.get_ticket(ticket_id)["state"])
            if not isinstance(exc, InjectedCrash):
                if current in {CanonicalState.IMPLEMENTING,CanonicalState.VERIFYING,CanonicalState.REPAIRING}:
                    self.ledger.transition(ticket_id,CanonicalState.BLOCKED,payload={"runtime_error":"execution failed; reconciliation required"})
            raise

    def execute_implementation(self, ticket_id: str, *, repository: Path, owner: str="local-first-operator") -> dict[str, object] | None:
        """Public resumable implementation-only operation.

        A frozen validated candidate is recovered and verified from the ledger
        and worktree on replay.  No model, review, repair, commit, or
        integration operation is reachable from this method.
        """
        candidate=self.ledger.review_candidate(ticket_id)
        if candidate is not None:
            stage=self.ledger.model_stage(ticket_id,int(candidate["attempt_number"]),"implementation")
            artifact=Path(str(stage["response_artifact"])) if stage else None
            if stage is None or artifact is None or not artifact.is_file(): raise RuntimeError("persisted implementation candidate is incomplete")
            binding=self.ledger.runtime_binding(ticket_id); repo,_,_=self.config.validate_execution_roots()
            if str(repo) != binding["repository_path"]: raise ValueError("repository mismatch with imported binding")
            attempt_row=self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?",(ticket_id,int(candidate["attempt_number"]))).fetchone()
            if attempt_row is None or not attempt_row["worktree_path"]: raise RuntimeError("persisted implementation attempt is missing")
            path=Path(str(attempt_row["worktree_path"]))
            if not path.is_dir(): raise RuntimeError("persisted implementation worktree is missing")
            diff=subprocess.run(("git","diff",str(stage["base_sha"])),cwd=path,text=True,capture_output=True,check=True).stdout
            fingerprint=hashlib.sha256(diff.encode()).hexdigest()
            if fingerprint != candidate["candidate_fingerprint"]: raise RuntimeError("persisted candidate fingerprint mismatch")
            return {"ticket_id":ticket_id,"attempt_number":int(candidate["attempt_number"]),"candidate_fingerprint":fingerprint,"implementation_artifact":str(artifact),"state":self.ledger.get_ticket(ticket_id)["state"],"replayed":True}
        result=self._implementation_stage(ticket_id,repository=repository,owner=owner,allow_validation_repair=False,explicit_operator=True)
        if result is None: return None
        return {"ticket_id":ticket_id,"attempt_number":int(result["attempt_number"]),"candidate_fingerprint":str(result["candidate_fingerprint"]),"implementation_artifact":str(self.ledger.model_stage(ticket_id,int(result["attempt_number"]),"implementation")["response_artifact"]),"state":self.ledger.get_ticket(ticket_id)["state"],"replayed":False}

    def authorize_historical_revalidation(self, ticket_id: str, attempt_number: int, *, repository: Path, operator_id: str="local-first-operator", reason: str="operator authorization for historical revalidation") -> dict[str, object]:
        """Authorize one exact historical attempt for a future integrity gate."""
        allow_existing_review = False
        if type(attempt_number) is not int or attempt_number < 1:
            raise ValueError("attempt number must be a positive integer")
        paused = self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
        if paused is None or not paused["paused"]:
            raise PermissionError("historical revalidation authorization requires Local First paused")
        ticket_row = self.ledger.get_ticket(ticket_id)
        if ticket_row["state"] != CanonicalState.NEEDS_TRIAGE.value:
            raise ValueError("historical revalidation authorization requires needs_triage")
        binding = self.ledger.runtime_binding(ticket_id)
        repo, _, _ = self.config.validate_execution_roots()
        if repo != Path(repository).resolve(strict=True) or str(repo) != binding["repository_path"]:
            raise ValueError("repository mismatch with imported binding")
        ticket = ticket_from_ledger(ticket_row)
        if self.ledger.review_candidate(ticket_id) is not None:
            raise ValueError("review candidate already exists")
        if self.ledger.accepted_commit(ticket_id):
            raise ValueError("accepted evidence already exists")
        if self.ledger.incomplete_model_invocations(ticket_id):
            raise ValueError("incomplete model invocation requires explicit resolution")
        if self.ledger.connection.execute("SELECT 1 FROM review_results WHERE ticket_id=? UNION SELECT 1 FROM model_invocations WHERE ticket_id=? AND stage='review' UNION SELECT 1 FROM model_stage_artifacts WHERE ticket_id=? AND stage='review'", (ticket_id, ticket_id, ticket_id)).fetchone() and not allow_existing_review:
            raise ValueError("review activity already exists")
        if self.ledger.connection.execute("SELECT 1 FROM events WHERE entity_type='ticket' AND entity_id=? AND to_state=?", (ticket_id, CanonicalState.REPAIRING.value)).fetchone():
            raise ValueError("repair activity already exists")
        latest = self.ledger.connection.execute("SELECT MAX(attempt_number) AS latest FROM attempts WHERE ticket_id=?", (ticket_id,)).fetchone()["latest"]
        if latest is None or int(latest) != attempt_number:
            raise ValueError("selected attempt is not the latest attempt")
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        impl = self.ledger.model_stage(ticket_id, attempt_number, "implementation")
        invocation = self.ledger.invocation_for_stage(ticket_id, attempt_number, "implementation")
        if attempt is None or impl is None or invocation is None or invocation["status"] != "completed":
            raise ValueError("historical attempt lacks completed implementation provenance")
        old_validation = self.ledger.runtime_stage(ticket_id, f"validation-{attempt_number}")
        if old_validation is None:
            raise ValueError("historical attempt lacks deterministic validation failure")
        raw_detail = str(old_validation["detail"])
        try:
            old_payload = json.loads(raw_detail)
            if type(old_payload) is not dict:
                raise ValueError("historical validation provenance is ambiguous")
        except json.JSONDecodeError:
            old_payload = raw_detail
        derived = derive_obsolete_validation_failure(ticket, old_payload, attempt_number=attempt_number, stage=f"validation-{attempt_number}")
        if derived is None or not (Path(str(attempt["worktree_path"])) / ticket.allowed_files[0]).is_file():
            raise ValueError("historical validation failure is not a recognized obsolete controller defect")
        classification, evidence_identity, _evidence_kind = derived
        if str(attempt["base_sha"]) != str(impl["base_sha"]):
            raise ValueError("implementation base provenance mismatch")
        target_file = ticket.allowed_files[0]
        return self.ledger.create_historical_revalidation_authorization(ticket_id=ticket_id, attempt_number=attempt_number, base_sha=str(impl["base_sha"]), repository_identity=str(repo), target_file=target_file, failure_classification=classification, failure_evidence_identity=evidence_identity, implementation_invocation_id=str(invocation["invocation_id"]), operator_id=operator_id, reason=reason)

    def attest_historical_revalidation_implementation(self, ticket_id: str, attempt_number: int, *, repository: Path, operator_id: str="local-first-operator") -> dict[str, object]:
        """Attest preserved implementation identity only; never validate semantics."""
        allow_existing_review = False
        if type(attempt_number) is not int or attempt_number < 1:
            raise ValueError("attempt number must be a positive integer")
        paused = self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
        if paused is None or not paused["paused"]:
            raise PermissionError("historical implementation attestation requires Local First paused")
        ticket_row = self.ledger.get_ticket(ticket_id)
        if ticket_row["state"] != CanonicalState.NEEDS_TRIAGE.value:
            raise ValueError("historical implementation attestation requires needs_triage")
        binding = self.ledger.runtime_binding(ticket_id)
        repo, worktree_root, _ = self.config.validate_execution_roots()
        if repo != Path(repository).resolve(strict=True) or str(repo) != binding["repository_path"]:
            raise ValueError("repository mismatch with imported binding")
        ticket = ticket_from_ledger(ticket_row)
        if self.ledger.review_candidate(ticket_id) is not None or self.ledger.accepted_commit(ticket_id):
            raise ValueError("candidate or accepted evidence already exists")
        if self.ledger.incomplete_model_invocations(ticket_id):
            raise ValueError("incomplete model invocation requires explicit resolution")
        if self.ledger.connection.execute("SELECT 1 FROM review_results WHERE ticket_id=? UNION SELECT 1 FROM model_invocations WHERE ticket_id=? AND stage='review' UNION SELECT 1 FROM model_stage_artifacts WHERE ticket_id=? AND stage='review'", (ticket_id, ticket_id, ticket_id)).fetchone() and not allow_existing_review:
            raise ValueError("review activity already exists")
        if self.ledger.connection.execute("SELECT 1 FROM events WHERE entity_type='ticket' AND entity_id=? AND to_state=?", (ticket_id, CanonicalState.REPAIRING.value)).fetchone():
            raise ValueError("repair activity already exists")
        if self.ledger.connection.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND attempt_number>?", (ticket_id, attempt_number)).fetchone():
            raise ValueError("a later attempt already exists")
        authorization = self.ledger.historical_revalidation_authorization(ticket_id, attempt_number)
        if authorization is None:
            raise PermissionError("historical revalidation authorization is required")
        stored_auth_hash = authorization.get("authorization_hash")
        if type(stored_auth_hash) is not str or len(stored_auth_hash) != 64 or any(character not in "0123456789abcdef" for character in stored_auth_hash) or stored_auth_hash != authorization_hash_from_row(authorization):
            raise PermissionError("historical revalidation authorization hash is invalid")
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        impl = self.ledger.model_stage(ticket_id, attempt_number, "implementation")
        invocation = self.ledger.invocation_for_stage(ticket_id, attempt_number, "implementation")
        implementation_invocations = self.ledger.connection.execute("SELECT COUNT(*) AS count FROM model_invocations WHERE ticket_id=? AND attempt_number=? AND stage='implementation'", (ticket_id, attempt_number)).fetchone()["count"]
        if attempt is None or impl is None or invocation is None or invocation["status"] != "completed" or int(implementation_invocations) != 1:
            raise ValueError("historical implementation provenance is incomplete or ambiguous")
        if str(invocation["invocation_id"]) != str(authorization["implementation_invocation_id"]):
            raise PermissionError("authorization implementation invocation mismatch")
        if str(invocation["model_artifact"] or "") != str(impl["response_artifact"]):
            raise ValueError("implementation artifact provenance mismatch")
        if str(attempt["base_sha"]) != str(impl["base_sha"]) or str(impl["base_sha"]) != str(authorization["base_sha"]):
            raise ValueError("implementation base provenance mismatch")
        path = Path(str(attempt["worktree_path"])).resolve()
        if not path.is_dir() or str(path) != str(Path(str(impl["worktree_path"])).resolve()):
            raise ValueError("preserved implementation worktree is missing or ambiguous")
        if str(attempt["branch"] or "") == "" or str(attempt["branch"]) != f"local-first/{ticket_id}/attempt-{attempt_number}":
            raise ValueError("implementation worktree branch provenance mismatch")
        def git(*args: str) -> str:
            return subprocess.run(("git", *args), cwd=path, text=True, capture_output=True, check=True).stdout.strip()
        if git("rev-parse", "--show-toplevel") != str(path) or git("rev-parse", "HEAD") != str(impl["base_sha"]):
            raise ValueError("implementation worktree repository/base mismatch")
        worktree_diff_hash = GitWorktreeAdapter(repo, worktree_root).diff_hash(path)
        if worktree_diff_hash != str(impl["diff_hash"]):
            raise RuntimeError("preserved implementation does not match durable historical diff")
        result = self.ledger.create_historical_revalidation_attestation(ticket_id=ticket_id, attempt_number=attempt_number, base_sha=str(impl["base_sha"]), repository_identity=str(repo), implementation_invocation_id=str(invocation["invocation_id"]), implementation_artifact=str(impl["response_artifact"]), implementation_diff_hash=str(impl["diff_hash"]), worktree_path=str(path), worktree_diff_hash=worktree_diff_hash, authorization_hash_value=str(stored_auth_hash), operator_id=operator_id)
        return result

    def revalidate_historical_implementation(self, ticket_id: str, attempt_number: int, *, repository: Path, operator_id: str="local-first-operator", freeze_candidate: bool = False, allow_existing_review: bool = False) -> dict[str, object]:
        """Revalidate one preserved, rejected implementation without inference."""
        if type(attempt_number) is not int or attempt_number < 1:
            raise ValueError("attempt number must be a positive integer")
        paused = self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
        if paused is None or not paused["paused"]:
            raise PermissionError("historical implementation revalidation requires Local First paused")
        ticket_row = self.ledger.get_ticket(ticket_id)
        binding = self.ledger.runtime_binding(ticket_id)
        repo, worktree_root, artifact_root = self.config.validate_execution_roots()
        if repo != Path(repository).resolve(strict=True) or str(repo) != binding["repository_path"]:
            raise ValueError("repository mismatch with imported binding")
        ticket = ticket_from_ledger(ticket_row)
        candidate = self.ledger.review_candidate(ticket_id)
        if candidate is not None and not freeze_candidate:
            raise ValueError("review candidate already exists")
        if ticket_row["state"] != CanonicalState.NEEDS_TRIAGE.value:
            raise ValueError("historical implementation revalidation requires needs_triage")
        if self.ledger.accepted_commit(ticket_id):
            raise ValueError("accepted evidence already exists")
        if self.ledger.incomplete_model_invocations(ticket_id):
            raise ValueError("incomplete model invocation requires explicit resolution")
        if self.ledger.connection.execute("SELECT 1 FROM review_results WHERE ticket_id=? UNION SELECT 1 FROM model_invocations WHERE ticket_id=? AND stage='review' UNION SELECT 1 FROM model_stage_artifacts WHERE ticket_id=? AND stage='review'", (ticket_id, ticket_id, ticket_id)).fetchone() and not allow_existing_review:
            raise ValueError("review activity already exists")
        if self.ledger.connection.execute("SELECT 1 FROM events WHERE entity_type='ticket' AND entity_id=? AND to_state=?", (ticket_id, CanonicalState.REPAIRING.value)).fetchone():
            raise ValueError("repair activity already exists")
        if self.ledger.connection.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND attempt_number>?", (ticket_id, attempt_number)).fetchone():
            raise ValueError("a later attempt already exists")
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        impl = self.ledger.model_stage(ticket_id, attempt_number, "implementation")
        invocation = self.ledger.invocation_for_stage(ticket_id, attempt_number, "implementation")
        if attempt is None or impl is None or invocation is None or invocation["status"] != "completed":
            raise ValueError("historical attempt lacks completed implementation provenance")
        old_validation = self.ledger.runtime_stage(ticket_id, f"validation-{attempt_number}")
        if old_validation is None:
            raise ValueError("historical attempt lacks deterministic validation failure")
        raw_detail = str(old_validation["detail"])
        try:
            old_payload = json.loads(raw_detail)
            if type(old_payload) is not dict:
                raise ValueError("historical validation provenance is ambiguous")
        except json.JSONDecodeError:
            old_payload = raw_detail
        derived = derive_obsolete_validation_failure(ticket, old_payload, attempt_number=attempt_number, stage=f"validation-{attempt_number}")
        if derived is None:
            raise ValueError("historical validation failure is not a recognized obsolete controller defect")
        obsolete_classification, evidence_identity, _evidence_kind = derived
        authorization = self.ledger.historical_revalidation_authorization(ticket_id, attempt_number)
        if authorization is None:
            raise PermissionError("historical revalidation authorization is required")
        stored_hash = authorization.get("authorization_hash")
        if type(stored_hash) is not str or len(stored_hash) != 64 or any(character not in "0123456789abcdef" for character in stored_hash):
            raise PermissionError("historical revalidation authorization hash is malformed")
        try:
            recomputed_hash = authorization_hash_from_row(authorization)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PermissionError("historical revalidation authorization hash cannot be recomputed") from exc
        if stored_hash != recomputed_hash:
            raise PermissionError("historical revalidation authorization hash mismatch")
        target_file = ticket.allowed_files[0]
        expected_identity = {"ticket_id": ticket_id, "attempt_number": attempt_number, "base_sha": str(impl["base_sha"]), "repository_identity": str(repo), "target_file": target_file, "failure_classification": obsolete_classification, "failure_evidence_identity": evidence_identity, "implementation_invocation_id": str(invocation["invocation_id"])}
        if any(str(authorization[key]) != str(value) for key, value in expected_identity.items()):
            raise PermissionError("historical revalidation authorization identity mismatch")
        attestation = self.ledger.historical_revalidation_attestation(ticket_id, attempt_number)
        if attestation is None:
            raise RuntimeError("implementation-integrity attestation required before historical revalidation")
        stored_attestation_hash = attestation.get("attestation_hash")
        if type(stored_attestation_hash) is not str or len(stored_attestation_hash) != 64 or any(character not in "0123456789abcdef" for character in stored_attestation_hash):
            raise PermissionError("historical implementation attestation hash is malformed")
        try:
            recomputed_attestation_hash = attestation_hash_from_row(attestation)
        except (KeyError, TypeError, ValueError) as exc:
            raise PermissionError("historical implementation attestation hash cannot be recomputed") from exc
        if stored_attestation_hash != recomputed_attestation_hash:
            raise PermissionError("historical implementation attestation hash mismatch")
        attestation_identity = {"ticket_id": ticket_id, "attempt_number": attempt_number, "base_sha": str(impl["base_sha"]), "repository_identity": str(repo), "implementation_invocation_id": str(invocation["invocation_id"]), "implementation_artifact": str(impl["response_artifact"]), "implementation_diff_hash": str(impl["diff_hash"]), "worktree_path": str(Path(str(attempt["worktree_path"])).resolve()), "authorization_hash": str(stored_hash),}
        if any(str(attestation[key]) != str(value) for key, value in attestation_identity.items()):
            raise PermissionError("historical implementation attestation identity mismatch")
        expected_path = Path(str(attempt["worktree_path"])).resolve()
        attested_path = Path(str(attestation["worktree_path"])).resolve()
        if expected_path != attested_path or not expected_path.is_dir():
            raise PermissionError("historical implementation worktree is unavailable or mismatched")
        if str(attempt["branch"] or "") != f"local-first/{ticket_id}/attempt-{attempt_number}":
            raise PermissionError("historical implementation attempt branch mismatch")
        try:
            live_top_level = subprocess.run(("git", "rev-parse", "--show-toplevel"), cwd=expected_path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            live_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=expected_path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            live_branch = subprocess.run(("git", "branch", "--show-current"), cwd=expected_path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            registered_worktrees = subprocess.run(("git", "worktree", "list", "--porcelain"), cwd=repo, text=True, capture_output=True, check=True, timeout=15).stdout
            live_diff_hash = GitWorktreeAdapter(repo, worktree_root).diff_hash(expected_path)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PermissionError("historical implementation live worktree inspection failed") from exc
        expected_branch = f"local-first/{ticket_id}/attempt-{attempt_number}"
        registered = False
        for block in registered_worktrees.split("\n\n"):
            lines = block.splitlines()
            if lines and lines[0] == f"worktree {expected_path}":
                registered = (f"HEAD {live_head}" in lines and f"branch refs/heads/{expected_branch}" in lines)
                break
        if live_top_level != str(expected_path) or live_head != str(impl["base_sha"]) or live_branch != expected_branch or not registered:
            raise PermissionError("historical implementation live worktree identity mismatch")
        if live_diff_hash != str(attestation["worktree_diff_hash"]):
            raise PermissionError("historical implementation live worktree diff mismatch")
        if live_diff_hash != str(impl["diff_hash"]):
            raise PermissionError("historical implementation durable diff mismatch")
        validation_profile_hash = canonical_sha256(ticket.contract())
        existing_result = self.ledger.historical_revalidation_validation_result(ticket_id, attempt_number)
        if existing_result is not None:
            existing_claim = self.ledger.historical_revalidation_validation_claim(ticket_id, attempt_number)
            if existing_claim is None or str(existing_result["claim_id"]) != str(existing_claim["claim_id"]) or any(str(existing_claim[field]) != str(value) for field, value in {"authorization_hash": str(stored_hash), "attestation_hash": stored_attestation_hash, "base_sha": str(impl["base_sha"]), "implementation_diff_hash": str(impl["diff_hash"]), "validation_profile_hash": canonical_sha256(ticket.contract())}.items()):
                raise PermissionError("historical validation result claim identity mismatch")
            if int(existing_result["passed"]) not in (0, 1) or historical_validation_result_hash(ticket_id=ticket_id, attempt_number=attempt_number, authorization_hash=str(stored_hash), attestation_hash=stored_attestation_hash, base_sha=str(impl["base_sha"]), implementation_diff_hash=str(impl["diff_hash"]), validation_profile_hash=validation_profile_hash, artifact_sha256=str(existing_result["artifact_sha256"]), passed=bool(existing_result["passed"]), compact_evidence=str(existing_result["compact_evidence"])) != str(existing_result["result_hash"]):
                raise PermissionError("historical validation result integrity mismatch")
            if str(existing_result["validation_profile_hash"]) != validation_profile_hash or str(existing_result["authorization_hash"]) != str(stored_hash) or str(existing_result["attestation_hash"]) != stored_attestation_hash:
                raise PermissionError("historical validation result identity mismatch")
            if not Path(str(existing_result["artifact_path"])).is_file() or hashlib.sha256(Path(str(existing_result["artifact_path"])).read_bytes()).hexdigest() != str(existing_result["artifact_sha256"]):
                raise PermissionError("historical validation result artifact integrity mismatch")
            if not bool(existing_result["passed"]):
                raise RuntimeError("historical current validation failed; explicit handling required")
            if freeze_candidate:
                provenance = {"ticket_id": ticket_id, "attempt_number": attempt_number, "base_sha": str(impl["base_sha"]), "implementation_diff_hash": str(impl["diff_hash"]), "authorization_hash": str(stored_hash), "attestation_hash": stored_attestation_hash, "validation_claim_id": str(existing_claim["claim_id"]), "validation_result_id": str(existing_result["result_id"]), "validation_result_hash": str(existing_result["result_hash"]), "validation_profile_hash": validation_profile_hash}
                return self.ledger.freeze_review_candidate(ticket_id, attempt_number, candidate_fingerprint=live_diff_hash, validation_evidence=str(existing_result["compact_evidence"]), implementation_invocation_id=str(invocation["invocation_id"]), runtime_identity=self.effective_runtime_identity(), historical_provenance=provenance)
            raise RuntimeError("historical candidate freeze gate required")
        if freeze_candidate:
            raise RuntimeError("historical validation result required before candidate freeze")
        claim = self.ledger.claim_historical_revalidation_validation(ticket_id=ticket_id, attempt_number=attempt_number, authorization_hash_value=str(stored_hash), attestation_hash_value=stored_attestation_hash, base_sha=str(impl["base_sha"]), implementation_diff_hash=str(impl["diff_hash"]), validation_profile_hash=validation_profile_hash)
        if claim.get("status") == "completed":
            raise RuntimeError("historical validation result reconciliation required")
        try:
            validation = DeterministicValidator(artifact_root=artifact_root / ticket_id / str(attempt_number)).validate(expected_path, ticket, base_sha=str(impl["base_sha"]))
            artifact_path = Path(validation.full_evidence_path).resolve()
            artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            post_top_level = subprocess.run(("git", "rev-parse", "--show-toplevel"), cwd=expected_path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            post_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=expected_path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            post_branch = subprocess.run(("git", "branch", "--show-current"), cwd=expected_path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            post_registered_worktrees = subprocess.run(("git", "worktree", "list", "--porcelain"), cwd=repo, text=True, capture_output=True, check=True, timeout=15).stdout
            post_diff_hash = GitWorktreeAdapter(repo, worktree_root).diff_hash(expected_path)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            raise RuntimeError("historical validation live integrity inspection failed") from exc
        post_registered = any(
            lines and lines[0] == f"worktree {expected_path}"
            and f"HEAD {post_head}" in lines
            and f"branch refs/heads/{expected_branch}" in lines
            for lines in (block.splitlines() for block in post_registered_worktrees.split("\n\n"))
        )
        if post_top_level != str(expected_path) or post_head != str(impl["base_sha"]) or post_branch != expected_branch or not post_registered or post_diff_hash != str(impl["diff_hash"]) or post_diff_hash != str(attestation["worktree_diff_hash"]):
            raise RuntimeError("historical implementation changed during validation")
        result = self.ledger.record_historical_revalidation_validation_result(claim_id=str(claim["claim_id"]), ticket_id=ticket_id, attempt_number=attempt_number, authorization_hash_value=str(stored_hash), attestation_hash_value=stored_attestation_hash, base_sha=str(impl["base_sha"]), implementation_diff_hash=str(impl["diff_hash"]), validation_profile_hash=validation_profile_hash, artifact_path=str(artifact_path), artifact_sha256=artifact_sha256, passed=validation.passed, compact_evidence=validation.compact_evidence)
        if not validation.passed:
            raise RuntimeError("historical current validation failed; explicit handling required")
        raise RuntimeError("historical candidate freeze gate required")

    def freeze_historical_candidate(self, ticket_id: str, attempt_number: int, *, repository: Path, operator_id: str="local-first-operator", allow_existing_review: bool = False) -> dict[str, object]:
        """Freeze an exact, already-completed passing historical validation result."""
        return self.revalidate_historical_implementation(ticket_id, attempt_number, repository=repository, operator_id=operator_id, freeze_candidate=True, allow_existing_review=allow_existing_review)

    def review_historical_candidate(self, ticket_id: str, attempt_number: int, *, repository: Path, owner: str="local-first-reviewer") -> dict[str, object]:
        """Hand an R2d candidate to the ordinary fresh review machinery only."""
        candidate = self.ledger.review_candidate(ticket_id, attempt_number)
        if candidate is None:
            raise RuntimeError("canonical historical candidate is required before fresh review")
        candidate = self.freeze_historical_candidate(ticket_id, attempt_number, repository=repository, allow_existing_review=True)
        status = self.ledger.review_reconciliation_status(ticket_id, attempt_number)
        if status["classification"] == "valid_review_stage_pending_application":
            return {"ticket_id": ticket_id, "attempt_number": attempt_number, "candidate_fingerprint": str(candidate["candidate_fingerprint"]), "classification": status["classification"], "replayed": True}
        if status["classification"] in {"review_in_flight", "ambiguous_review_history", "review_retry_exhausted"}:
            raise RuntimeError("historical candidate review requires explicit reconciliation")
        if status["classification"] != "no_review_attempt":
            raise RuntimeError("historical candidate has non-fresh review activity")
        ticket_row = self.ledger.get_ticket(ticket_id); ticket = ticket_from_ledger(ticket_row)
        binding = self.ledger.runtime_binding(ticket_id); repo, _, artifact_root = self.config.validate_execution_roots()
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        if attempt is None:
            raise RuntimeError("historical candidate attempt is missing")
        path = Path(str(attempt["worktree_path"])).resolve(); base = str(candidate["historical_provenance_json"] and json.loads(str(candidate["historical_provenance_json"])).get("base_sha", ""))
        if not base or str(repo) != binding["repository_path"]:
            raise PermissionError("historical candidate repository provenance is invalid")
        diff = subprocess.run(("git", "diff", base), cwd=path, text=True, capture_output=True, check=True).stdout
        if hashlib.sha256(diff.encode()).hexdigest() != str(candidate["candidate_fingerprint"]):
            raise PermissionError("historical candidate fingerprint mismatch")
        selected_files = {relative: (path / relative).read_text(encoding="utf-8") for relative in (*ticket.allowed_files, *ticket.new_test_files) if (path / relative).is_file()}
        packet = ReviewPacketBuilder().build(ticket, diff=diff, selected_files=selected_files, validation_evidence=str(candidate["validation_evidence"]))
        artifacts_root = artifact_root / ticket_id / str(attempt_number); artifacts_root.mkdir(parents=True, exist_ok=True)
        invocation_id = uuid.uuid4().hex
        provider = str(getattr(self.local_model, "review_provider", getattr(self.local_model, "provider", type(self.local_model).__name__)))
        model = str(getattr(self.local_model, "review_model", getattr(self.local_model, "model", type(self.local_model).__name__)))
        packet_hash = hashlib.sha256(packet.encode()).hexdigest()
        try:
            self.ledger.start_model_invocation(invocation_id=invocation_id, ticket_id=ticket_id, attempt_number=attempt_number, stage="review", provider=provider, model=model, packet_hash=packet_hash, worktree_path=str(path), timeout_seconds=self.config.review_timeout_seconds)
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("historical candidate review is already in flight or ambiguous") from exc
        started = time.monotonic()
        try:
            if hasattr(self.local_model, "review_timeout_seconds"): self.local_model.review_timeout_seconds = self.config.review_timeout_seconds
            review = LocalReviewAdapter(self.local_model).review(ticket, packet, artifact_dir=artifacts_root)
        except subprocess.TimeoutExpired as exc:
            self.ledger.finish_model_invocation(invocation_id, status="timeout", duration_seconds=time.monotonic() - started, error={"type": "TimeoutExpired", "timeout_seconds": self.config.review_timeout_seconds, "process": str(exc)[:1000]})
            self.ledger.record_review_infrastructure_failure(ticket_id, attempt_number, outcome="review_timeout")
            raise
        except ValueError as exc:
            artifact = str(getattr(exc, "artifact_path", "")) or None
            self.ledger.finish_model_invocation(invocation_id, status="malformed_output", duration_seconds=time.monotonic() - started, error={"type": type(exc).__name__, "message": str(exc)[:1000]}, model_artifact=artifact)
            self.ledger.record_review_infrastructure_failure(ticket_id, attempt_number, outcome="review_malformed_output")
            raise
        except Exception as exc:
            self.ledger.finish_model_invocation(invocation_id, status="process_error", duration_seconds=time.monotonic() - started, error={"type": type(exc).__name__, "message": str(exc)[:1000]})
            self.ledger.record_review_infrastructure_failure(ticket_id, attempt_number, outcome="review_process_error")
            raise
        review_path = artifacts_root / "review-result.json"
        review_path.write_text(json.dumps({"payload": review.raw}, sort_keys=True), encoding="utf-8")
        self.ledger.finish_model_invocation(invocation_id, status="completed", duration_seconds=time.monotonic() - started, model_artifact=str(review_path))
        self.ledger.record_model_stage(ticket_id, attempt_number, "review", purpose="review", adapter=type(self.local_model).__name__, request_hash=packet_hash, response_artifact=str(review_path), worktree_path=str(path), base_sha=base, diff_hash=str(candidate["candidate_fingerprint"]))
        status = self.ledger.review_reconciliation_status(ticket_id, attempt_number)
        return {"ticket_id": ticket_id, "attempt_number": attempt_number, "candidate_fingerprint": str(candidate["candidate_fingerprint"]), "classification": status["classification"], "replayed": False}

    def apply_persisted_review_only(self, ticket_id: str, attempt_number: int, *, repository: Path) -> dict[str, object]:
        """Apply only the persisted semantic review; never dispose its verdict."""
        if self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"] != 1:
            raise PermissionError("persisted review application requires Local First paused")
        candidate = self.ledger.review_candidate(ticket_id, attempt_number)
        if candidate is None or candidate["status"] not in {"review_pending", "review_applied"}:
            raise PermissionError("review_pending historical candidate is required")
        current_ticket = self.ledger.get_ticket(ticket_id)
        current_state = str(current_ticket["state"])
        current_status = self.ledger.review_reconciliation_status(ticket_id, attempt_number)
        if current_status["classification"] == "valid_review_applied":
            return self.ledger.apply_persisted_review(ticket_id, attempt_number)
        if candidate["status"] != "review_pending":
            raise PermissionError("review_pending historical candidate is required")
        if current_state == CanonicalState.LOCAL_REVIEW.value:
            stage = self.ledger.model_stage(ticket_id, attempt_number, "review")
            attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            binding = self.ledger.runtime_binding(ticket_id)
            repo, _, _ = self.config.validate_execution_roots()
            if stage is None or attempt is None or str(repo) != str(binding["repository_path"]):
                raise PermissionError("ordinary review application provenance is invalid")
            path = Path(str(attempt["worktree_path"])).resolve()
            diff = subprocess.run(("git", "diff", str(stage["base_sha"])), cwd=path, text=True, capture_output=True, check=True).stdout
            if hashlib.sha256(diff.encode()).hexdigest() != str(candidate["candidate_fingerprint"]) or str(stage["diff_hash"]) != str(candidate["candidate_fingerprint"]):
                raise PermissionError("ordinary review candidate fingerprint mismatch")
        elif current_state == CanonicalState.NEEDS_TRIAGE.value:
            # Reuse the reviewed historical candidate gate for live R2 provenance and diff integrity.
            self.freeze_historical_candidate(ticket_id, attempt_number, repository=repository, allow_existing_review=True)
        else:
            raise PermissionError("persisted review application requires local_review or governed historical needs_triage")
        status = self.ledger.review_reconciliation_status(ticket_id, attempt_number)
        if status["classification"] != "valid_review_stage_pending_application":
            raise PermissionError("persisted review application is not pending")
        return self.ledger.apply_persisted_review(ticket_id, attempt_number)

    def accept_reviewed_candidate_only(self, ticket_id: str, attempt_number: int, *, repository: Path) -> dict[str, object]:
        """Commit and durably accept an applied candidate without integration."""
        return self._accept_reviewed_candidate_only(ticket_id, attempt_number, repository=repository, require_paused=True)

    def integrate_accepted_candidate_only(self, ticket_id: str, attempt_number: int, *, repository: Path) -> dict[str, object]:
        """Fast-forward only the authoritative tranche ref for one accepted candidate."""
        if self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"] != 1:
            raise PermissionError("integration-only operation requires Local First paused")
        ticket_row = self.ledger.get_ticket(ticket_id)
        if ticket_row["state"] != CanonicalState.DONE.value:
            raise PermissionError("integration requires done ticket")
        repo, worktree_root, _ = self.config.validate_execution_roots()
        if repo != Path(repository).resolve(strict=True):
            raise ValueError("repository mismatch with imported binding")
        latest = self.ledger.connection.execute("SELECT MAX(attempt_number) AS latest FROM attempts WHERE ticket_id=?", (ticket_id,)).fetchone()["latest"]
        if latest is None or int(latest) != attempt_number or self.ledger.connection.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND attempt_number>?", (ticket_id, attempt_number)).fetchone():
            raise ValueError("integration attempt is not the latest attempt")
        evidence_rows = self.ledger.connection.execute("SELECT * FROM accepted_evidence WHERE ticket_id=?", (ticket_id,)).fetchall()
        if len(evidence_rows) != 1:
            raise PermissionError("exactly one accepted evidence row is required")
        evidence = evidence_rows[0]; attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); candidate = self.ledger.review_candidate(ticket_id, attempt_number); review = self.ledger.connection.execute("SELECT * FROM review_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); review_stage = self.ledger.model_stage(ticket_id, attempt_number, "review")
        if attempt is None or candidate is None or review is None or review_stage is None or attempt["accepted_commit_sha"] != evidence["accepted_commit_sha"] or review["verdict"] != "pass" or json.loads(review["payload_json"]).get("findings") or self.ledger.review_reconciliation_status(ticket_id, attempt_number)["classification"] != "valid_review_applied":
            raise PermissionError("accepted passing review authority is incomplete")
        accepted = str(evidence["accepted_commit_sha"]); base = str(attempt["base_sha"]); candidate_fp = str(candidate["candidate_fingerprint"]); summary = json.loads(str(evidence["diff_summary"]))
        if summary.get("candidate_fingerprint") != candidate_fp or summary.get("base_sha") != base or summary.get("review_result_id") != review["id"] or review_stage["diff_hash"] != candidate_fp:
            raise PermissionError("accepted evidence and review candidate identity mismatch")
        if not ticket_row["tranche_id"]:
            raise PermissionError("accepted ticket has no authoritative tranche")
        provenance = json.loads(str(candidate["historical_provenance_json"]))
        if provenance.get("authorization_hash"):
            auth = self.ledger.connection.execute("SELECT * FROM historical_revalidation_authorizations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); attestation = self.ledger.connection.execute("SELECT * FROM historical_revalidation_attestations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); result = self.ledger.connection.execute("SELECT * FROM historical_revalidation_validation_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); impl_invocation = self.ledger.invocation_for_stage(ticket_id, attempt_number, "implementation")
            if auth is None or attestation is None or result is None or impl_invocation is None or authorization_hash_from_row(auth) != auth["authorization_hash"] or attestation_hash_from_row(attestation) != attestation["attestation_hash"]:
                raise PermissionError("historical integration provenance is invalid")
            if auth["authorization_hash"] != provenance.get("authorization_hash") or attestation["attestation_hash"] != provenance.get("attestation_hash") or result["result_id"] != provenance.get("validation_result_id") or result["result_hash"] != provenance.get("validation_result_hash") or not bool(result["passed"]) or result["implementation_diff_hash"] != candidate_fp or auth["implementation_invocation_id"] != impl_invocation["invocation_id"]:
                raise PermissionError("historical integration provenance conflicts with candidate")
            if not Path(str(attestation["implementation_artifact"])).is_file() or hashlib.sha256(Path(str(result["artifact_path"])).read_bytes()).hexdigest() != str(result["artifact_sha256"]):
                raise PermissionError("historical integration artifact integrity failed")
        ticket = ticket_from_ledger(ticket_row); authorized = set(ticket.allowed_files) | set(ticket.new_test_files); adapter = GitWorktreeAdapter(repo, worktree_root)
        commit_parent = subprocess.run(("git", "rev-parse", f"{accepted}^"), cwd=repo, text=True, capture_output=True, check=True).stdout.strip(); names = subprocess.run(("git", "diff", "--name-only", base, accepted), cwd=repo, text=True, capture_output=True, check=True).stdout.splitlines(); identity_diff = subprocess.run(("git", "diff", base, accepted, "--", *ticket.allowed_files), cwd=repo, text=True, capture_output=True, check=True).stdout; current = adapter.existing_execution_base(str(ticket_row["tranche_id"]), base)
        if commit_parent != base or not names or any(name not in authorized for name in names) or hashlib.sha256(identity_diff.encode()).hexdigest() != candidate_fp:
            raise PermissionError("accepted commit no longer matches frozen candidate")
        if current == accepted:
            return {"status": "already_integrated", "integrated": True, "integration_head": accepted}
        if current != commit_parent:
            raise RuntimeError("integration head is not the accepted commit parent")
        integrated = adapter.advance_integration_head(str(ticket_row["tranche_id"]), current, accepted)
        return {"status": "integrated", "integrated": True, "integration_head": integrated}

    def _accept_reviewed_candidate_only(self, ticket_id: str, attempt_number: int, *, repository: Path, require_paused: bool) -> dict[str, object]:
        if require_paused and self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"] != 1:
            raise PermissionError("acceptance-only operation requires Local First paused")
        ticket_row = self.ledger.get_ticket(ticket_id)
        existing = self.ledger.accepted_commit(ticket_id)
        if existing is not None and ticket_row["state"] in {CanonicalState.ACCEPTED.value, CanonicalState.DONE.value}:
            return {"accepted_commit_sha": existing, "status": "already_accepted", "integrated": False}
        if ticket_row["state"] not in ({CanonicalState.LOCAL_REVIEW.value} if require_paused else {CanonicalState.LOCAL_REVIEW.value, CanonicalState.ACCEPTED.value}):
            raise PermissionError("acceptance requires local_review")
        binding = self.ledger.runtime_binding(ticket_id)
        repo, worktree_root, _ = self.config.validate_execution_roots()
        if repo != Path(repository).resolve(strict=True) or str(repo) != binding["repository_path"]:
            raise ValueError("repository mismatch with imported binding")
        latest = self.ledger.connection.execute("SELECT MAX(attempt_number) AS latest FROM attempts WHERE ticket_id=?", (ticket_id,)).fetchone()["latest"]
        if latest is None or int(latest) != attempt_number or self.ledger.connection.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND attempt_number>?", (ticket_id, attempt_number)).fetchone():
            raise ValueError("accepted attempt is not the latest attempt")
        candidate = self.ledger.review_candidate(ticket_id, attempt_number)
        status = self.ledger.review_reconciliation_status(ticket_id, attempt_number)
        review_row = self.ledger.connection.execute("SELECT * FROM review_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        review_allowed = review_row is not None and (review_row["verdict"] == "pass" or (not require_paused and review_row["verdict"] == "repair"))
        if candidate is None or candidate["status"] != "review_pending" or status["classification"] != "valid_review_applied" or not review_allowed or (review_row is not None and json.loads(review_row["payload_json"]).get("findings")):
            raise PermissionError("applied passing review authority is required")
        ticket = ticket_from_ledger(ticket_row); attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); impl = self.ledger.model_stage(ticket_id, attempt_number, "implementation"); review_stage = self.ledger.model_stage(ticket_id, attempt_number, "review"); invocation = self.ledger.invocation_for_stage(ticket_id, attempt_number, "implementation")
        if attempt is None or impl is None or review_stage is None or invocation is None or invocation["status"] != "completed":
            raise PermissionError("accepted candidate implementation provenance is incomplete")
        worktree = Path(str(attempt["worktree_path"])).resolve(); authorized_files = set(ticket.allowed_files) | set(ticket.new_test_files)
        if not authorized_files:
            raise PermissionError("acceptance requires authorized candidate files")
        live_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(); live_branch = subprocess.run(("git", "branch", "--show-current"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(); base = str(attempt["base_sha"])
        live_diff = subprocess.run(("git", "diff", str(base)), cwd=worktree, text=True, capture_output=True, check=True).stdout; candidate_fp = str(candidate["candidate_fingerprint"])
        status_lines = subprocess.run(("git", "status", "--porcelain=v1"), cwd=worktree, text=True, capture_output=True, check=True).stdout.splitlines(); status_paths = [line[3:].strip() for line in status_lines if "__pycache__" not in line]
        if live_branch != str(attempt["branch"]):
            raise PermissionError("accepted candidate branch mismatch")
        if any(path not in authorized_files for path in status_paths):
            raise PermissionError("live candidate contains unauthorized changes")
        existing = self.ledger.accepted_commit(ticket_id)
        if existing is not None:
            if existing != live_head or attempt["accepted_commit_sha"] not in (None, existing):
                raise RuntimeError("conflicting accepted commit evidence")
            return {"accepted_commit_sha": existing, "status": "already_accepted", "integrated": False}
        identity_diff = subprocess.run(("git", "diff", str(base), "--", *ticket.allowed_files), cwd=worktree, text=True, capture_output=True, check=True).stdout
        created_new_commit = False
        if live_head == base:
            if not status_paths or hashlib.sha256(identity_diff.encode()).hexdigest() != candidate_fp:
                raise PermissionError("live candidate differs from frozen candidate")
            for cache in sorted(worktree.rglob("__pycache__"), reverse=True):
                if cache.is_dir(): shutil.rmtree(cache)
            worktrees = GitWorktreeAdapter(repo, worktree_root); attempt_worktree = AttemptWorktree(ticket_id, attempt_number, base, str(attempt["branch"]), worktree, hashlib.sha256(live_diff.encode()).hexdigest())
            accepted_sha = worktrees.accept(attempt_worktree, f"local-first: {ticket_row['title']}")
            created_new_commit = True
            self._crash("accepted_commit_created")
        else:
            parent = subprocess.run(("git", "rev-parse", "HEAD^"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(); committed_diff = subprocess.run(("git", "diff", base, "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout; names = subprocess.run(("git", "diff", "--name-only", base, "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.splitlines(); clean = subprocess.run(("git", "status", "--porcelain=v1"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip() == ""
            if parent != base or hashlib.sha256(subprocess.run(("git", "diff", base, "HEAD", "--", *ticket.allowed_files), cwd=worktree, text=True, capture_output=True, check=True).stdout.encode()).hexdigest() != candidate_fp or not names or any(name not in authorized_files for name in names) or not clean:
                raise RuntimeError("post-commit acceptance state is ambiguous")
            accepted_sha = live_head
        final_parent = subprocess.run(("git", "rev-parse", "HEAD^"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(); final_diff = subprocess.run(("git", "diff", base, "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout; final_names = subprocess.run(("git", "diff", "--name-only", base, "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.splitlines(); final_clean = subprocess.run(("git", "status", "--porcelain=v1"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip() == ""
        if accepted_sha != subprocess.run(("git", "rev-parse", "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip() or final_parent != base or hashlib.sha256(subprocess.run(("git", "diff", base, "HEAD", "--", *ticket.allowed_files), cwd=worktree, text=True, capture_output=True, check=True).stdout.encode()).hexdigest() != candidate_fp or not final_names or any(name not in authorized_files for name in final_names) or not final_clean:
            raise RuntimeError("accepted commit does not match frozen candidate")
        provenance = json.loads(str(candidate["historical_provenance_json"]))
        if provenance.get("authorization_hash"):
            auth = self.ledger.connection.execute("SELECT * FROM historical_revalidation_authorizations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); attestation = self.ledger.connection.execute("SELECT * FROM historical_revalidation_attestations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); result = self.ledger.connection.execute("SELECT * FROM historical_revalidation_validation_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            if auth is None or attestation is None or result is None or authorization_hash_from_row(auth) != auth["authorization_hash"] or attestation_hash_from_row(attestation) != attestation["attestation_hash"]:
                raise PermissionError("historical acceptance provenance is invalid")
            if auth["authorization_hash"] != provenance.get("authorization_hash") or attestation["attestation_hash"] != provenance.get("attestation_hash") or result["result_id"] != provenance.get("validation_result_id") or result["result_hash"] != provenance.get("validation_result_hash") or not bool(result["passed"]):
                raise PermissionError("historical acceptance provenance conflicts with candidate")
            if result["implementation_diff_hash"] != candidate_fp or auth["implementation_invocation_id"] != invocation["invocation_id"]:
                raise PermissionError("historical acceptance implementation binding mismatch")
            if not Path(str(attestation["implementation_artifact"])).is_file():
                raise PermissionError("historical implementation artifact is missing")
            if hashlib.sha256(Path(str(result["artifact_path"])).read_bytes()).hexdigest() != str(result["artifact_sha256"]):
                raise PermissionError("historical validation artifact digest mismatch")
        diff_summary = json.dumps({"candidate_fingerprint": candidate_fp, "base_sha": base, "files": final_names, "implementation_invocation_id": invocation["invocation_id"], "authorization_hash": provenance.get("authorization_hash"), "attestation_hash": provenance.get("attestation_hash"), "validation_result_id": provenance.get("validation_result_id"), "validation_result_hash": provenance.get("validation_result_hash"), "review_result_id": review_row["id"]}, sort_keys=True)
        evidence = self.ledger.persist_accepted_candidate(ticket_id, attempt_number, accepted_sha, candidate_fingerprint=candidate_fp, diff_summary=diff_summary, validation_summary=str(candidate["validation_evidence"]), allow_nonpass=not require_paused)
        return {"accepted_commit_sha": accepted_sha, "status": "accepted", "integrated": False, "evidence": evidence}

    def execute(self, ticket_id: str, *, repository: Path, allow_board_writes: bool, owner: str="local-first-controller") -> bool:
        if not allow_board_writes: return False
        if self.ledger.accepted_commit(ticket_id): self.ledger.project_ticket(ticket_id,self.board); return True
        while True:
            before_state=CanonicalState(self.ledger.get_ticket(ticket_id)["state"])
            result=self._implementation_stage(ticket_id,repository=repository,owner=owner,allow_validation_repair=True)
            if result is None:
                self.ledger.project_ticket(ticket_id,self.board)
                after_state=CanonicalState(self.ledger.get_ticket(ticket_id)["state"])
                return after_state != before_state
            ticket=result["ticket"]; base=str(result["base"]); worktrees=result["worktrees"]; attempt=result["attempt"]; artifacts_root=result["artifacts_root"]; validation=result["validation"]; diff=str(result["diff"]); attempt_number=int(result["attempt_number"])
            try:
                review_packet=ReviewPacketBuilder().build(ticket,diff=diff,selected_files={p:(attempt.path/p).read_text() for p in (*ticket.allowed_files, *ticket.new_test_files) if (attempt.path/p).exists()},validation_evidence=validation.compact_evidence)
                review_stage=self.ledger.model_stage(ticket_id,attempt_number,"review")
                if review_stage and Path(review_stage["response_artifact"]).exists(): review=normalize_review(json.loads(Path(review_stage["response_artifact"]).read_text()).get("payload",{}),ticket)
                else:
                    if hasattr(self.local_model,"review_timeout_seconds"): self.local_model.review_timeout_seconds=self.config.review_timeout_seconds
                    invocation_id=uuid.uuid4().hex; provider=str(getattr(self.local_model,"review_provider",getattr(self.local_model,"provider",type(self.local_model).__name__))); model=str(getattr(self.local_model,"review_model",getattr(self.local_model,"model",type(self.local_model).__name__)))
                    self.ledger.start_model_invocation(invocation_id=invocation_id,ticket_id=ticket_id,attempt_number=attempt_number,stage="review",provider=provider,model=model,packet_hash=hashlib.sha256(review_packet.encode()).hexdigest(),worktree_path=str(attempt.path),timeout_seconds=self.config.review_timeout_seconds)
                    started=time.monotonic()
                    try: result=LocalReviewAdapter(self.local_model).review(ticket,review_packet,artifact_dir=artifacts_root,workdir=attempt.path)
                    except subprocess.TimeoutExpired as exc:
                        self.ledger.finish_model_invocation(invocation_id,status="timeout",duration_seconds=time.monotonic()-started,error={"type":"TimeoutExpired","timeout_seconds":self.config.review_timeout_seconds,"process":str(exc)[:1000]}); self.ledger.record_review_infrastructure_failure(ticket_id,attempt_number,outcome="review_timeout"); self.ledger.transition(ticket_id,CanonicalState.NEEDS_TRIAGE,payload={"review_infrastructure":"review_timeout; reconciliation required","attempt_number":attempt_number}); raise
                    except ValueError as exc:
                        malformed_artifact=str(getattr(exc,"artifact_path","")) or None; self.ledger.finish_model_invocation(invocation_id,status="malformed_output",duration_seconds=time.monotonic()-started,error={"type":type(exc).__name__,"message":str(exc)[:1000]},model_artifact=malformed_artifact); self.ledger.record_review_infrastructure_failure(ticket_id,attempt_number,outcome="review_malformed_output"); raise
                    except Exception as exc:
                        self.ledger.finish_model_invocation(invocation_id,status="process_error",duration_seconds=time.monotonic()-started,error={"type":type(exc).__name__,"message":str(exc)[:1000]}); self.ledger.record_review_infrastructure_failure(ticket_id,attempt_number,outcome="review_process_error"); self.ledger.transition(ticket_id,CanonicalState.NEEDS_TRIAGE,payload={"review_infrastructure":"review_process_error; reconciliation required","attempt_number":attempt_number}); raise
                    path=artifacts_root/"review-result.json"; path.write_text(json.dumps({"payload":result.raw},sort_keys=True),encoding="utf-8"); self.ledger.finish_model_invocation(invocation_id,status="completed",duration_seconds=time.monotonic()-started,model_artifact=str(path)); self.ledger.record_model_stage(ticket_id,attempt_number,"review",purpose="review",adapter=type(self.local_model).__name__,request_hash=hashlib.sha256(review_packet.encode()).hexdigest(),response_artifact=str(path),worktree_path=str(attempt.path),base_sha=base,diff_hash=worktrees.diff_hash(attempt.path)); self.ledger.record_runtime_stage(ticket_id,"review_completed",str(path)); self._crash("review_completed"); review=result
                outcome=SameTicketRepairCoordinator(self.ledger).apply(ticket_id,attempt_number,review)
                if outcome=="repair":
                    repair_evidence="; ".join(f"{f.criterion_id}: {f.evidence}; repair: {f.minimal_repair}" for f in review.findings)
                    self.ledger.transition(ticket_id,CanonicalState.IMPLEMENTING); next_attempt=attempt_number+1; self.ledger.ensure_attempt(ticket_id,next_attempt); self.ledger.connection.execute("UPDATE attempts SET base_sha=?, branch=?, worktree_path=?, pre_diff_hash=? WHERE ticket_id=? AND attempt_number=?",(base,attempt.branch,str(attempt.path),worktrees.diff_hash(attempt.path),ticket_id,next_attempt)); result_failure_evidence=repair_evidence
                    result=self._implementation_stage(ticket_id,repository=repository,owner=owner,allow_validation_repair=True,failure_evidence=result_failure_evidence)
                    if result is None: self.ledger.project_ticket(ticket_id,self.board); return True
                    ticket=result["ticket"]; base=str(result["base"]); worktrees=result["worktrees"]; attempt=result["attempt"]; artifacts_root=result["artifacts_root"]; validation=result["validation"]; diff=str(result["diff"]); attempt_number=int(result["attempt_number"]); continue
                if outcome=="triage": break
                if self.ledger.accepted_commit(ticket_id):
                    if CanonicalState(self.ledger.get_ticket(ticket_id)["state"]) == CanonicalState.ACCEPTED: self.ledger.transition(ticket_id,CanonicalState.DONE)
                    break
                self.ledger.record_runtime_stage(ticket_id,"accepted_commit_created","pending"); commit=worktrees.accept(attempt,f"local-first: {self.ledger.get_ticket(ticket_id)['title']}"); worktrees.advance_integration_head(self.ledger.get_ticket(ticket_id)["tranche_id"] or None,base,commit); self.ledger.record_accepted_evidence(ticket_id,commit,"accepted ticket diff",validation.compact_evidence); self.ledger.record_runtime_stage(ticket_id,"accepted_commit_created",commit); self._crash("accepted_commit_created"); self.ledger.transition(ticket_id,CanonicalState.DONE); break
            except Exception as exc:
                current=CanonicalState(self.ledger.get_ticket(ticket_id)["state"])
                if not isinstance(exc,InjectedCrash) and current == CanonicalState.LOCAL_REVIEW:
                    review_status=self.ledger.review_reconciliation_status(ticket_id)
                    if review_status["classification"] not in {"review_invocation_failed_no_verdict","review_in_flight","review_retry_exhausted"}: self.ledger.transition(ticket_id,CanonicalState.NEEDS_TRIAGE,payload={"runtime_error":"malformed local review; reconciliation required"})
                raise
        self.ledger.project_ticket(ticket_id,self.board); return True
