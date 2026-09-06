from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .context_packet import ContextPacketBuilder
from .git_adapter import AttemptWorktree, GitWorktreeAdapter
from .ledger import Ledger
from .local_qwen import LocalQwenAdapter
from .readiness import validate_ticket
from .review import LocalReviewAdapter, ReviewPacketBuilder, SameTicketRepairCoordinator, normalize_review
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
                validation_record=(json.dumps({"attempt_number":attempt_number,"artifact_path":str(validation.full_evidence_path),"completed":True,"passed":validation.passed,"compact_evidence":validation.compact_evidence},sort_keys=True) if not validation.passed else validation.compact_evidence)
                self.ledger.record_runtime_stage(ticket_id,f"validation-{attempt_number}",validation_record); self.ledger.record_runtime_stage(ticket_id,"validation_completed",validation_record); self._crash("validation_completed")
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
