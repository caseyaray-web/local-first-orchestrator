from __future__ import annotations

import hashlib
import json
import subprocess
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

    def canonical_repository(self, candidate: Path) -> Path:
        path = Path(candidate).resolve(strict=True)
        roots = tuple(Path(root).resolve(strict=True) for root in (self.repository_allowlist or (self.repository,)))
        if path not in roots: raise ValueError("repository is not an exact configured allowlist root")
        if not (path / ".git").exists(): raise ValueError("repository must be a Git checkout, not a broad directory")
        return path


def ticket_from_ledger(row: dict[str, Any]) -> MicroTicket:
    verification = json.loads(row["verification_json"])
    return MicroTicket(row["id"], row["objective"], tuple(json.loads(row["criterion_ids_json"])), row["primary_symbol"], tuple(json.loads(row["allowed_files_json"])), tuple(json.loads(row["forbidden_changes_json"])), PatchBudget(**json.loads(row["patch_budget_json"])), VerificationProfile(tuple(tuple(c) for c in verification["commands"]), verification.get("working_directory", "."), int(verification.get("timeout_seconds", 60)), int(verification.get("output_limit", 20000))), row["risk"], bool(row["review_required"]), int(row["max_attempts"]), tuple(json.loads(row["dependencies_json"])))


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
        return validate_ticket(MicroTicket(card.id,raw["objective"],tuple(raw["criterion_ids"]),raw["primary_symbol"],tuple(raw["allowed_files"]),tuple(raw["forbidden_changes"]),PatchBudget(**raw["patch_budget"]),VerificationProfile(tuple(tuple(x) for x in raw["verification"]["commands"]),raw["verification"].get("working_directory","."),int(raw["verification"].get("timeout_seconds",60)),int(raw["verification"].get("output_limit",20000))),raw["risk"],bool(raw["review_required"]),int(raw["max_attempts"]),tuple(raw.get("dependencies",()))))

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

    def execute(self, ticket_id: str, *, repository: Path, allow_board_writes: bool, owner: str="local-first-controller") -> bool:
        if not allow_board_writes: return False
        binding=self.ledger.runtime_binding(ticket_id); raw_repository=Path(repository).resolve(strict=True)
        if str(raw_repository) != binding["repository_path"]: raise ValueError("repository mismatch with imported binding")
        repo=self.config.canonical_repository(raw_repository); ticket=ticket_from_ledger(self.ledger.get_ticket(ticket_id))
        if ticket.risk != "low": raise PermissionError("only low-risk tickets may execute locally")
        if self.ledger.accepted_commit(ticket_id): self.ledger.project_ticket(ticket_id,self.board); return True
        state=CanonicalState(self.ledger.get_ticket(ticket_id)["state"])
        if state == CanonicalState.READY_LOCAL and not self.ledger.claim_specific(ticket_id,owner,self.config.lease_seconds): return False
        if state in {CanonicalState.NEEDS_TRIAGE,CanonicalState.BLOCKED,CanonicalState.DONE}: return False
        planning_base=str(binding["starting_sha"]); worktrees=GitWorktreeAdapter(repo,self.config.worktree_root)
        base=worktrees.resolve_execution_base(self.ledger.get_ticket(ticket_id)["tranche_id"] or None, planning_base)
        self.ledger.record_runtime_stage(ticket_id, "execution_base", base)
        attempt_number=max([int(r["attempt_number"]) for r in self.ledger.connection.execute("SELECT attempt_number FROM attempts WHERE ticket_id=?",(ticket_id,))] or [1])
        persisted = self.ledger.connection.execute("SELECT attempt_number FROM model_stage_artifacts WHERE ticket_id=? AND stage='implementation' ORDER BY attempt_number DESC LIMIT 1", (ticket_id,)).fetchone()
        if persisted is not None:
            attempt_number = int(persisted["attempt_number"])
        repair_evidence=""
        try:
            while attempt_number <= ticket.max_attempts:
                self.ledger.ensure_attempt(ticket_id,attempt_number); attempt=self._attempt(worktrees,ticket_id,attempt_number,base)
                artifacts_root=self.config.artifact_root/ticket_id/str(attempt_number); artifacts_root.mkdir(parents=True,exist_ok=True)
                impl=self.ledger.model_stage(ticket_id,attempt_number,"implementation")
                if not impl:
                    packet=ContextPacketBuilder().build_from_repository(ticket,attempt.path,repository_rules="Edit only allowed files. Return JSON only.",failure_evidence=repair_evidence)
                    artifacts=ContextPacketBuilder().write_artifacts(packet,artifact_root=artifacts_root); request_hash=hashlib.sha256(packet.text.encode()).hexdigest()
                    result=self.local_model.invoke("implementation",packet.text,artifact_dir=artifacts_root,workdir=attempt.path)
                    response_path=getattr(result,"artifact_path",artifacts_root/"implementation-result.json")
                    self.ledger.record_model_stage(ticket_id,attempt_number,"implementation",purpose="implementation",adapter=type(self.local_model).__name__,request_hash=request_hash,response_artifact=str(response_path),worktree_path=str(attempt.path),base_sha=base,diff_hash=worktrees.diff_hash(attempt.path))
                    self.ledger.record_runtime_stage(ticket_id,f"implementation-{attempt_number}",str(response_path)); self.ledger.record_runtime_stage(ticket_id,"implementation_completed",str(response_path)); self._crash("implementation_completed")
                self.ledger.transition(ticket_id,CanonicalState.VERIFYING) if CanonicalState(self.ledger.get_ticket(ticket_id)["state"]) == CanonicalState.IMPLEMENTING else None
                validation=DeterministicValidator(artifact_root=artifacts_root).validate(attempt.path,ticket,base_sha=base)
                self.ledger.record_runtime_stage(ticket_id,f"validation-{attempt_number}",validation.compact_evidence); self.ledger.record_runtime_stage(ticket_id,"validation_completed",validation.compact_evidence); self._crash("validation_completed")
                if not validation.passed:
                    if attempt_number >= ticket.max_attempts: self.ledger.transition(ticket_id,CanonicalState.NEEDS_TRIAGE,payload={"validation":validation.compact_evidence}); break
                    repair_evidence=validation.compact_evidence; self.ledger.transition(ticket_id,CanonicalState.REPAIRING,payload={"validation":repair_evidence}); self.ledger.transition(ticket_id,CanonicalState.IMPLEMENTING); attempt_number+=1; continue
                diff=subprocess.run(("git","diff",base),cwd=attempt.path,text=True,capture_output=True,check=True).stdout
                self.ledger.transition(ticket_id,CanonicalState.LOCAL_REVIEW) if CanonicalState(self.ledger.get_ticket(ticket_id)["state"]) == CanonicalState.VERIFYING else None
                review_packet=ReviewPacketBuilder().build(ticket,diff=diff,selected_files={p:(attempt.path/p).read_text() for p in ticket.allowed_files if (attempt.path/p).exists()},validation_evidence=validation.compact_evidence)
                review_stage=self.ledger.model_stage(ticket_id,attempt_number,"review")
                if review_stage and Path(review_stage["response_artifact"]).exists(): review=normalize_review(json.loads(Path(review_stage["response_artifact"]).read_text()).get("payload",{}),ticket)
                else:
                    result=LocalReviewAdapter(self.local_model).review(ticket,review_packet,artifact_dir=artifacts_root,workdir=attempt.path); response_path=getattr(result,"raw",None)
                    path=artifacts_root/"review-result.json"; path.write_text(json.dumps({"payload":result.raw},sort_keys=True),encoding="utf-8")
                    self.ledger.record_model_stage(ticket_id,attempt_number,"review",purpose="review",adapter=type(self.local_model).__name__,request_hash=hashlib.sha256(review_packet.encode()).hexdigest(),response_artifact=str(path),worktree_path=str(attempt.path),base_sha=base,diff_hash=worktrees.diff_hash(attempt.path)); self.ledger.record_runtime_stage(ticket_id,"review_completed",str(path)); self._crash("review_completed"); review=result
                outcome=SameTicketRepairCoordinator(self.ledger).apply(ticket_id,attempt_number,review)
                if outcome=="repair":
                    repair_evidence="; ".join(f"{f.criterion_id}: {f.evidence}; repair: {f.minimal_repair}" for f in review.findings)
                    self.ledger.transition(ticket_id,CanonicalState.IMPLEMENTING)
                    next_attempt=attempt_number+1
                    # A review repair is a new model attempt on the same isolated diff,
                    # not a fresh worktree that loses the implementation under review.
                    self.ledger.ensure_attempt(ticket_id,next_attempt)
                    self.ledger.connection.execute(
                        "UPDATE attempts SET base_sha=?, branch=?, worktree_path=?, pre_diff_hash=? WHERE ticket_id=? AND attempt_number=?",
                        (base,attempt.branch,str(attempt.path),worktrees.diff_hash(attempt.path),ticket_id,next_attempt),
                    )
                    attempt_number=next_attempt
                    continue
                if outcome=="triage": break
                if self.ledger.accepted_commit(ticket_id):
                    if CanonicalState(self.ledger.get_ticket(ticket_id)["state"]) == CanonicalState.ACCEPTED:
                        self.ledger.transition(ticket_id,CanonicalState.DONE)
                    break
                self.ledger.record_runtime_stage(ticket_id,"accepted_commit_created", "pending")
                commit=worktrees.accept(attempt,f"local-first: {self.ledger.get_ticket(ticket_id)['title']}")
                worktrees.advance_integration_head(self.ledger.get_ticket(ticket_id)["tranche_id"] or None, base, commit)
                self.ledger.record_accepted_evidence(ticket_id,commit,"accepted ticket diff",validation.compact_evidence); self.ledger.record_runtime_stage(ticket_id,"accepted_commit_created",commit); self._crash("accepted_commit_created"); self.ledger.transition(ticket_id,CanonicalState.DONE); break
            self.ledger.project_ticket(ticket_id,self.board); return True
        except Exception as exc:
            current=CanonicalState(self.ledger.get_ticket(ticket_id)["state"])
            if not isinstance(exc, InjectedCrash):
                if current == CanonicalState.LOCAL_REVIEW:
                    self.ledger.transition(ticket_id,CanonicalState.NEEDS_TRIAGE,payload={"runtime_error":"malformed local review; reconciliation required"})
                elif current in {CanonicalState.IMPLEMENTING,CanonicalState.VERIFYING,CanonicalState.REPAIRING}:
                    self.ledger.transition(ticket_id,CanonicalState.BLOCKED,payload={"runtime_error":"execution failed; reconciliation required"})
            raise
