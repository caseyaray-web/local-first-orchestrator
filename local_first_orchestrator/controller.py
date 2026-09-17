from __future__ import annotations

import hashlib
import base64
import re
import json
import os
import subprocess
import shutil
import sqlite3
import time
import tempfile
import uuid
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .admission import FeatureAdmissionResult, FeatureAdmissionSpec
from .context_packet import ContextPacketBuilder
from .evidence_hash import canonical_sha256
from .git_adapter import AttemptWorktree, GitWorktreeAdapter
from .git_security import safe_git_argv, safe_git_env
from .historical_revalidation import attestation_hash_from_row, authorization_hash_from_row, classify_obsolete_validation_failure, derive_obsolete_validation_failure, historical_validation_result_hash
from .hermes_profiles import review_profile_identity
from .execution_handoff import HANDOFF_MARKER, HANDOFF_SENTINEL
from .ledger import Ledger, _completion_evidence_hash, _recheck_evidence_hash
from .local_qwen import LocalQwenAdapter, REVIEW_JSON_SCHEMA
from .paid_model import PaidModelAdapter
from .readiness import validate_ticket
from .review import LocalReviewAdapter, ReviewPacketBuilder, SameTicketRepairCoordinator, normalize_review
from .repository_snapshot import snapshot as repository_snapshot
from .corrections import CorrectionService
from .states import CanonicalState
from .ticket import MicroTicket, PatchBudget, VerificationProfile
from .triage import LocalTriagePlanner, TriageCoordinator, TriageError, normalize_triage
from .usage_governor import PaidPurpose
from .validation import DeterministicValidator
from .native_release_approval import APPROVAL_DOMAIN, ACTIVATION_DOMAIN, APPROVAL_VERSION, canonical_approval_bytes, canonical_activation_bytes, canonical_snapshot_json, snapshot_authority, parse_approval_document, validate_activation_continuation_snapshot, validate_activation_post_snapshot, verify_detached_signature, fingerprint_public_key
from .native_workspace import PinnedNativeWorkspace, canonical_native_workspace_path, require_native_path_identity, validate_native_workspace_path
from .worktree_lifecycle import cleanup_completed_worktree


def _isolated_candidate_diff(path: Path, base_sha: str, *, allowed_new_paths: tuple[str, ...] = ()) -> dict[str, object]:
    """Build a candidate diff including approved new files without mutating the real Git index."""
    path = Path(path).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="local-first-index-") as temp_dir:
        env = safe_git_env()
        env["GIT_INDEX_FILE"] = str(Path(temp_dir) / "index")

        def git(*args: str) -> str:
            completed = subprocess.run(
                safe_git_argv(args), cwd=path, env=env, text=True, capture_output=True,
                check=True, timeout=30,
            )
            return completed.stdout

        git("read-tree", base_sha)
        untracked = tuple(item for item in git("ls-files", "--others", "--exclude-standard", "-z").split("\0") if item)
        approved_new = tuple(sorted(set(untracked) & set(allowed_new_paths)))
        if approved_new:
            git("add", "-N", "--", *approved_new)
        diff = git("diff", "--binary", base_sha, "--")
        changed = tuple(item for item in git("diff", "--name-only", base_sha, "--").splitlines() if item)
        numstat = git("diff", "--numstat", base_sha, "--")
    return {
        "diff": diff,
        "diff_hash": hashlib.sha256(diff.encode()).hexdigest(),
        "changed_paths": changed,
        "numstat": numstat,
        "untracked_paths": untracked,
    }


def _write_replayable_artifact(path: Path, encoded: str) -> str:
    """Atomically persist an immutable artifact; exact crash-orphans are safe to replay."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise RuntimeError("manual adoption artifact conflict")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class RuntimeConfig:
    repository: Path
    worktree_root: Path
    artifact_root: Path
    repository_allowlist: tuple[Path, ...] = ()
    lease_seconds: int = 1800
    implementation_timeout_seconds: int = 300
    review_timeout_seconds: int = 300
    operator_signer_fingerprint: str | None = None
    operator_authority_hash: str | None = None
    operator_signer_public_key: bytes | None = None
    operator_config_path: Path | None = None

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
    return MicroTicket(row["id"], row["objective"], tuple(json.loads(row["criterion_ids_json"])), row["primary_symbol"], tuple(json.loads(row["allowed_files_json"])), tuple(json.loads(row["forbidden_changes_json"])), PatchBudget(**json.loads(row["patch_budget_json"])), VerificationProfile(tuple(tuple(c) for c in verification["commands"]), verification.get("working_directory", "."), int(verification.get("timeout_seconds", 60)), int(verification.get("output_limit", 20000))), row["risk"], bool(row["review_required"]), int(row["max_attempts"]), tuple(json.loads(row["dependencies_json"])), tuple(json.loads(row.get("new_test_files_json") or "[]")), tuple(json.loads(row.get("create_files_json") or "[]")))


class InjectedCrash(RuntimeError):
    """Test-only crash marker; completed stages remain resumable."""


def compute_review_execution_policy_hash(local_model: Any, review_timeout_seconds: int) -> str:
    """Pure review execution-policy hash shared by execution and dry-run preview."""
    review_home = Path(getattr(local_model, "review_hermes_home", ""))
    if not review_home.name:
        provider = str(getattr(local_model, "review_provider", getattr(local_model, "provider", type(local_model).__name__)))
        model = str(getattr(local_model, "review_model", getattr(local_model, "model", type(local_model).__name__)))
        execution_identity = {"profile": None, "provider": provider, "model": model, "routing_files": {}, "fingerprint": hashlib.sha256(f"{provider}\0{model}".encode()).hexdigest()}
    else:
        executable = str(getattr(local_model, "executable", "hermes"))
        execution_identity = review_profile_identity(review_home.name, executable=executable, profile_root=review_home)
    policy = {
        "execution_identity": execution_identity,
        "timeout_seconds": int(review_timeout_seconds),
        "schema": REVIEW_JSON_SCHEMA,
        "review_mode": "packet-only",
    }
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class LocalFirstController:
    def __init__(self, ledger: Ledger, board: Any, config: RuntimeConfig, *, local_model: LocalQwenAdapter | None = None, fault_injector: Callable[[str], None] | None = None) -> None:
        if getattr(board, "is_fake", False): raise ValueError("production controller refuses FakeBoardAdapter")
        self.ledger, self.board, self.config = ledger, board, config
        self.local_model = local_model or LocalQwenAdapter()
        self.fault_injector = fault_injector

    def _inject_failure(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point)

    def _card_contract_payload(self, card: Any) -> dict[str, Any]:
        if card.status != "scheduled": raise ValueError("ineligible card: status must be scheduled")
        if "<!-- local-first-orchestrator -->" not in card.body: raise ValueError("ineligible card: missing local-first ownership marker")
        body = str(card.body)
        marker = "```local-first-contract\n"
        escaped_marker = "```local-first-contract\\n"
        if marker in body:
            encoded = body.split(marker, 1)[1].split("```", 1)[0]
        elif escaped_marker in body:
            encoded = body.split(escaped_marker, 1)[1].split("\\n```", 1)[0]
        else:
            raise ValueError("ineligible card: missing local-first contract block")
        try: raw = json.loads(encoded)
        except json.JSONDecodeError as exc: raise ValueError("ineligible card: malformed Qwen-ready contract") from exc
        if not isinstance(raw, dict): raise ValueError("ineligible card: malformed Qwen-ready contract")
        return raw

    def _parse_card(self, card: Any) -> MicroTicket:
        raw = self._card_contract_payload(card)
        required=("objective","criterion_ids","primary_symbol","allowed_files","forbidden_changes","patch_budget","verification","risk","review_required","max_attempts")
        missing=[key for key in required if key not in raw]
        if missing: raise ValueError("missing Qwen-ready fields: " + ", ".join(missing))
        return validate_ticket(MicroTicket(card.id,raw["objective"],tuple(raw["criterion_ids"]),raw["primary_symbol"],tuple(raw["allowed_files"]),tuple(raw["forbidden_changes"]),PatchBudget(**raw["patch_budget"]),VerificationProfile(tuple(tuple(x) for x in raw["verification"]["commands"]),raw["verification"].get("working_directory","."),int(raw["verification"].get("timeout_seconds",60)),int(raw["verification"].get("output_limit",20000))),raw["risk"],bool(raw["review_required"]),int(raw["max_attempts"]),tuple(raw.get("dependencies",())),tuple(raw.get("new_test_files",()))))

    def import_card(self, card: Any) -> str:
        raw_contract = self._card_contract_payload(card)
        ticket=self._parse_card(card)
        try: repo=self.config.canonical_repository(self.config.repository)
        except FileNotFoundError as exc: raise ValueError("repository path does not exist") from exc
        native_workspace = None
        workspace_path = getattr(card, "workspace_path", None)
        workspace_kind = getattr(card, "workspace_kind", None)
        if workspace_path and workspace_kind == "worktree":
            native_workspace, _ = validate_native_workspace_path(workspace_path, repository=repo, external_task_id=str(card.id))
        elif workspace_path:
            legacy_workspace = Path(workspace_path).expanduser().resolve(strict=True)
            if legacy_workspace != repo:
                raise ValueError("legacy card workspace_path must resolve to canonical repository")
        base = str(getattr(card, "base_sha", None) or raw_contract.get("repo_base_sha") or "").strip()
        if not base:
            base_cwd = native_workspace if native_workspace is not None else repo
            base=subprocess.run(("git","rev-parse","HEAD"),cwd=base_cwd,text=True,capture_output=True,check=True).stdout.strip()
        verify = subprocess.run(("git","cat-file","-e",f"{base}^{{commit}}"),cwd=repo,text=True,capture_output=True)
        if verify.returncode != 0: raise ValueError("card base_sha is not a commit in canonical repository")
        return self.ledger.admit_imported_ticket(
            title=card.title,
            external_id=str(card.id),
            contract=ticket.contract(),
            repository_path=str(repo),
            starting_sha=base,
            feature_id=None if raw_contract.get("feature_id") is None else str(raw_contract["feature_id"]),
            tranche_id=None if raw_contract.get("tranche_id") is None else str(raw_contract["tranche_id"]),
            operator_signer_fingerprint=self.config.operator_signer_fingerprint,
            operator_authority_hash=self.config.operator_authority_hash,
        )

    def import_scheduled_cards(self) -> list[str]: return [self.import_card(c) for c in self.board.import_candidates()]

    def recover_generated_projection(self, ticket_id: str, superseded_event_id: int, *, operator_id: str, reason: str) -> dict[str, Any]:
        """Record a read-verified replacement intent for one pre-native Hermes card."""
        if not hasattr(self.board, "execution_snapshot"):
            raise RuntimeError("generated projection recovery requires execution snapshot support")
        prior = self.ledger.connection.execute(
            "SELECT * FROM generated_projection_recoveries WHERE ticket_id=? AND superseded_event_id=?",
            (ticket_id, superseded_event_id),
        ).fetchone()
        rows = self.ledger.connection.execute(
            "SELECT external_task_id,superseded_at FROM board_projection_outbox WHERE ticket_id=? AND event_id=? "
            "AND operation='create_microticket' AND acknowledged_at IS NOT NULL",
            (ticket_id, superseded_event_id),
        ).fetchall()
        if len(rows) != 1 or not isinstance(rows[0]["external_task_id"], str) or not rows[0]["external_task_id"]:
            raise RuntimeError("generated projection recovery external identity missing or ambiguous")
        if prior is None and rows[0]["superseded_at"] is not None:
            raise RuntimeError("generated projection recovery target is already superseded")
        external_task_id = str(rows[0]["external_task_id"])
        if prior is None and self.ledger.resolve_external_task_id(ticket_id) != external_task_id:
            raise RuntimeError("generated projection recovery external identity conflicts")
        snapshot = self.board.execution_snapshot(external_task_id)
        if snapshot.task.id != external_task_id:
            raise RuntimeError("generated projection recovery snapshot identity conflicts")
        if snapshot.task.status not in {"done", "blocked"}:
            raise RuntimeError("generated projection recovery requires a done or inert blocked card")
        if snapshot.task.status == "blocked":
            task_is_inert = all(value is None for value in (
                snapshot.session_id, snapshot.branch_name, snapshot.started_at, snapshot.completed_at
            ))
            expected_gate = "Local First execution gate: authoritative dependencies/runtime authorization not satisfied"
            runs_are_inert_safety_gates = len(snapshot.runs) == 1 and all(
                run.status == "blocked"
                and run.outcome == "blocked"
                and run.profile is None
                and run.worker_pid is None
                and run.metadata is None
                and (run.started_at, run.ended_at) in {(None, None), (run.started_at, run.started_at)}
                and str(run.summary or "") == expected_gate
                for run in snapshot.runs
            )
            if not task_is_inert or not runs_are_inert_safety_gates:
                raise RuntimeError("generated projection recovery requires an inert blocked card without execution evidence")
        snapshot_hash = canonical_sha256(snapshot_authority(snapshot))
        return self.ledger.recover_generated_projection(
            ticket_id=ticket_id,
            superseded_event_id=superseded_event_id,
            superseded_external_task_id=external_task_id,
            observed_status=snapshot.task.status,
            observed_snapshot_hash=snapshot_hash,
            operator_id=operator_id,
            reason=reason,
        )

    def _validate_native_release_snapshot(self, snapshot: Any, *, external_task_id: str, implementation_profile: str,
                                          repository: Path, expected_base: str, expected_path: Path | None,
                                          expected_branch: str | None = None, expected_snapshot_hash: str | None = None) -> tuple[str, str]:
        if snapshot.task.id != external_task_id:
            raise RuntimeError("native release revalidation external identity drift")
        if expected_snapshot_hash is not None and canonical_sha256(snapshot_authority(snapshot)) != expected_snapshot_hash:
            raise RuntimeError("native release revalidation snapshot drift")
        if snapshot.task.assignee != implementation_profile or snapshot.task.workspace_kind != "worktree":
            raise RuntimeError("native release revalidation routing drift")
        if expected_path is None:
            raise RuntimeError("native release revalidation worktree drift")
        _, workspace_identity = validate_native_workspace_path(
            snapshot.task.workspace_path or "", repository=repository, external_task_id=external_task_id,
        )
        assert workspace_identity is not None
        try:
            repository_stat = repository.stat()
        except OSError as exc:
            raise RuntimeError("native release revalidation repository identity unavailable") from exc
        repository_identity = (repository_stat.st_dev, repository_stat.st_ino)

        def assert_stable() -> None:
            require_native_path_identity(expected_path, workspace_identity)
            try:
                current = repository.stat()
            except OSError as exc:
                raise RuntimeError("native release revalidation repository identity changed") from exc
            if (current.st_dev, current.st_ino) != repository_identity:
                raise RuntimeError("native release revalidation repository identity changed")

        def git(*args: str) -> str:
            assert_stable()
            try:
                result = subprocess.run(safe_git_argv(args), cwd=expected_path, env=safe_git_env(), text=True, capture_output=True, check=True, timeout=15)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("native release revalidation worktree verification failed") from exc
            assert_stable()
            return result.stdout.strip()

        top = git("rev-parse", "--show-toplevel")
        head = git("rev-parse", "HEAD")
        branch = git("branch", "--show-current")
        try:
            common_dir = Path(git("rev-parse", "--git-common-dir"))
            common_dir = (expected_path / common_dir).resolve(strict=True) if not common_dir.is_absolute() else common_dir.resolve(strict=True)
            git_dir = Path(git("rev-parse", "--git-dir"))
            git_dir = (expected_path / git_dir).resolve(strict=True) if not git_dir.is_absolute() else git_dir.resolve(strict=True)
            commondir_file = Path(git("rev-parse", "--git-path", "commondir"))
            commondir_file = (expected_path / commondir_file).resolve(strict=True) if not commondir_file.is_absolute() else commondir_file.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("native release revalidation worktree verification failed") from exc
        assert_stable()
        canonical_git = (repository / ".git").resolve()
        if (top != str(expected_path) or head != expected_base or branch != (snapshot.branch_name or "")
                or common_dir != canonical_git or not commondir_file.is_file()
                or git_dir == canonical_git or not str(git_dir).startswith(str(canonical_git / "worktrees") + "/")):
            raise RuntimeError("native release revalidation branch/base/repository drift")
        if expected_branch is not None and branch != expected_branch:
            raise RuntimeError("native release revalidation branch/base/repository drift")

        # Repository/base fields are optional in real Hermes schemas.  When a
        # schema has them, every non-null copy is an assertion, never a source
        # of authority.  Authority comes from the registered binding and Git.
        for observed_repository in (snapshot.repository_identity, snapshot.task.repository_identity):
            if observed_repository is not None and observed_repository != str(repository):
                raise RuntimeError("native release revalidation branch/base/repository drift")
        for observed_base in (snapshot.base_sha, snapshot.task.base_sha):
            if observed_base is not None and observed_base != expected_base:
                raise RuntimeError("native release revalidation branch/base/repository drift")

        if snapshot.task.status not in {"scheduled", "blocked"}:
            raise RuntimeError("native release revalidation card is dispatchable or final")
        if any(value is not None for value in (snapshot.session_id, snapshot.completed_at)):
            raise RuntimeError("native release revalidation execution evidence exists")
        forbidden = {"running", "completed", "success", "successful"}
        spawn_failed = []
        scheduled = []
        for run in snapshot.runs:
            if run.status in forbidden or (run.outcome or "").lower() in forbidden or run.worker_pid is not None:
                raise RuntimeError("native release revalidation execution evidence exists")
            terminal = run.started_at is not None and run.ended_at is not None and run.ended_at >= run.started_at
            if not terminal:
                raise RuntimeError("native release revalidation run is not terminal")
            if run.status == "blocked":
                if run.outcome != "blocked" or run.profile is not None or run.metadata is not None:
                    raise RuntimeError("native release revalidation blocked run is not inert")
            elif run.status == "spawn_failed":
                if run.outcome not in {None, "spawn_failed"} or run.profile != implementation_profile:
                    raise RuntimeError("native release revalidation spawn evidence is ambiguous")
                if not isinstance(run.metadata, dict) or set(run.metadata) != {"failures", "retry_status"} or type(run.metadata["failures"]) is not int or run.metadata["failures"] <= 0 or run.metadata["retry_status"] != "ready":
                    raise RuntimeError("native release revalidation spawn evidence is ambiguous")
                spawn_failed.append(run)
            elif run.status == "scheduled":
                if snapshot.task.status != "scheduled" or run.outcome != "scheduled" or run.profile != implementation_profile or run.metadata is not None:
                    raise RuntimeError("native release revalidation scheduled run is ambiguous")
                scheduled.append(run)
            else:
                raise RuntimeError("native release revalidation run evidence is ambiguous")
        if len(spawn_failed) > 1 or len(scheduled) > 1:
            raise RuntimeError("native release revalidation overlapping current runs")
        if (spawn_failed or scheduled) and snapshot.task.status != "scheduled":
            raise RuntimeError("native release revalidation current task is not scheduled")
        if snapshot.started_at is not None:
            if len(spawn_failed) != 1 or snapshot.started_at != spawn_failed[0].started_at:
                raise RuntimeError("native release revalidation top-level start evidence is ambiguous")
        elif spawn_failed:
            raise RuntimeError("native release revalidation top-level start evidence is missing")
        return branch, canonical_sha256(snapshot_authority(snapshot))

    def revalidate_native_release(self, ticket_id: str, *, operator_id: str | None = None, reason: str | None = None,
                                  implementation_profile: str, approval_document: dict[str, Any] | None = None,
                                  detached_signature: bytes | None = None, signer_public_key: bytes | None = None,
                                  signer_fingerprint: str | None = None, _prepare_only: bool = False) -> dict[str, Any]:
        """Authorize only inside the trusted Hermes board transaction boundary."""
        projection = self.ledger.connection.execute(
            "SELECT external_task_id FROM board_projection_outbox WHERE ticket_id=? AND operation='create_microticket' AND superseded_at IS NULL AND acknowledged_at IS NOT NULL AND external_task_id IS NOT NULL",
            (ticket_id,),
        ).fetchall()
        if len(projection) != 1:
            raise ValueError("native release revalidation requires exactly one current projection")
        external_task_id = str(projection[0]["external_task_id"])
        if not hasattr(self.board, "revalidation"):
            raise RuntimeError("native release revalidation requires a trusted local SQLite board adapter")
        with self.board.revalidation(ticket_id, external_task_id) as board_capability:
            return self._revalidate_native_release_locked(ticket_id, operator_id=operator_id, reason=reason,
                implementation_profile=implementation_profile, approval_document=approval_document,
                detached_signature=detached_signature, signer_public_key=signer_public_key,
                signer_fingerprint=signer_fingerprint, _prepare_only=_prepare_only,
                _trusted_board_capability=board_capability)

    def _revalidate_native_release_locked(self, ticket_id: str, *, operator_id: str | None, reason: str | None,
                                  implementation_profile: str, approval_document: dict[str, Any] | None,
                                  detached_signature: bytes | None, signer_public_key: bytes | None,
                                  signer_fingerprint: str | None, _prepare_only: bool,
                                  _trusted_board_capability: Any) -> dict[str, Any]:
        """Implementation called only while HermesBoardAdapter holds BEGIN IMMEDIATE."""
        if not hasattr(self.board, "execution_snapshot"):
            raise RuntimeError("native release revalidation requires execution snapshot support")
        if not isinstance(operator_id, str) or not operator_id.strip() or not isinstance(reason, str) or not reason.strip() or not implementation_profile.strip():
            raise ValueError("operator identity, reason, and implementation profile required")
        if not self.config.operator_signer_fingerprint or not self.config.operator_signer_public_key:
            raise PermissionError("native release revalidation requires registered external signer authority")
        registered_signer = self.config.operator_signer_public_key
        fingerprint_public_key(registered_signer)
        if not _prepare_only and (signer_public_key != registered_signer or signer_fingerprint != self.config.operator_signer_fingerprint):
            raise PermissionError("native release revalidation signer authority is not the registered authority")
        projection = self.ledger.connection.execute("""
            SELECT event_id,idempotency_key,external_task_id FROM board_projection_outbox
            WHERE ticket_id=? AND operation='create_microticket' AND superseded_at IS NULL
              AND acknowledged_at IS NOT NULL AND external_task_id IS NOT NULL
        """, (ticket_id,)).fetchall()
        if len(projection) != 1:
            raise ValueError("native release revalidation requires exactly one current projection")
        projection = projection[0]
        external_task_id = str(projection["external_task_id"])
        snapshot = _trusted_board_capability.snapshot
        repository = self.config.repository.resolve(strict=True)
        expected_base = str(self.ledger.runtime_binding(ticket_id)["starting_sha"])
        expected_path = canonical_native_workspace_path(repository, external_task_id)
        _, workspace_identity = validate_native_workspace_path(
            snapshot.task.workspace_path or "", repository=repository, external_task_id=external_task_id,
        )
        assert workspace_identity is not None
        try:
            repository_stat = repository.stat()
        except OSError as exc:
            raise RuntimeError("native release revalidation repository identity unavailable") from exc
        repository_identity = (repository_stat.st_dev, repository_stat.st_ino)

        def assert_stable() -> None:
            require_native_path_identity(expected_path, workspace_identity)
            try:
                current = repository.stat()
            except OSError as exc:
                raise RuntimeError("native release revalidation repository identity changed") from exc
            if (current.st_dev, current.st_ino) != repository_identity:
                raise RuntimeError("native release revalidation repository identity changed")

        assert_stable()
        branch, initial_snapshot_hash = self._validate_native_release_snapshot(snapshot, external_task_id=external_task_id, implementation_profile=implementation_profile, repository=repository, expected_base=expected_base, expected_path=expected_path)
        release = self.ledger.native_dependency_release(ticket_id)
        graph = self.ledger.native_dependency_graph(ticket_id)
        binding = self.ledger.runtime_binding(ticket_id)
        if binding.get("operator_signer_fingerprint") != self.config.operator_signer_fingerprint or binding.get("operator_authority_hash") != self.config.operator_authority_hash:
            raise PermissionError("registered runtime signer authority is not bound to this ticket")
        document = {
            "domain": APPROVAL_DOMAIN, "version": APPROVAL_VERSION,
            "operation": "revalidate-native-release", "request_id": secrets.token_urlsafe(24),
            "nonce": secrets.token_urlsafe(24), "operator_id": operator_id, "reason": reason,
            "authority": {"ledger_identity": str(self.ledger.database.resolve()), "ticket_id": ticket_id,
                "release": release, "graph": graph, "projection": {"event_id": int(projection["event_id"]), "key": str(projection["idempotency_key"]), "external_id": external_task_id},
                "implementation_profile": implementation_profile, "repository_identity": str(repository), "canonical_worktree_path": str(expected_path),
                "branch": branch, "base_sha": expected_base, "snapshot_hash": initial_snapshot_hash,
                "snapshot_schema_version": 2,
                "runtime_authority_hash": binding.get("operator_authority_hash")},
        }
        if approval_document is not None:
            supplied_document = parse_approval_document(canonical_approval_bytes(approval_document))
            if supplied_document["authority"] != document["authority"]:
                raise ValueError("approval document is stale or does not match current state")
            document = supplied_document
        canonical = canonical_approval_bytes(document)
        if _prepare_only:
            return {"document": document, "canonical_document": canonical.decode("utf-8"), "approval_hash": hashlib.sha256(canonical).hexdigest()}
        if approval_document is None or detached_signature is None or not signer_fingerprint:
            raise PermissionError("native release revalidation requires detached operator signature")
        supplied = canonical_approval_bytes(approval_document)
        if supplied != canonical:
            raise ValueError("approval document is stale or does not match current state")
        verify_detached_signature(supplied, detached_signature, registered_signer, signer_fingerprint)
        result = self.ledger.record_native_release_revalidation(
            ticket_id=ticket_id, projection_event_id=int(projection["event_id"]),
            projection_key=str(projection["idempotency_key"]), external_task_id=external_task_id,
            implementation_profile=implementation_profile, repository_identity=str(repository),
            canonical_worktree_path=str(expected_path), branch=branch, base_sha=expected_base,
            snapshot_hash=initial_snapshot_hash, operator_id=operator_id, reason=reason,
            approval_document_json=canonical.decode("utf-8"),
            approval_document_hash=hashlib.sha256(canonical).hexdigest(),
            detached_signature=detached_signature, signer_fingerprint=signer_fingerprint, signer_public_key=signer_public_key,
            _trusted_board_capability=_trusted_board_capability,
        )
        return result

    def prepare_native_release_activation(self, ticket_id: str, *, revalidation_id: str, operator_id: str, reason: str, request_key: str) -> dict[str, Any]:
        """Emit exact activation bytes for an external operator signature; never mutates state."""
        row = self.ledger.connection.execute("SELECT * FROM native_dependency_release_revalidations r WHERE r.revalidation_id=? AND r.ticket_id=? AND NOT EXISTS (SELECT 1 FROM native_dependency_release_revalidation_supersessions s WHERE s.old_revalidation_id=r.revalidation_id)", (revalidation_id, ticket_id)).fetchone()
        if row is None:
            raise ValueError("native release activation requires the exact signed revalidation")
        if self.ledger.native_dependency_release_migration_required(signer_public_key=self.config.operator_signer_public_key, signer_fingerprint=self.config.operator_signer_fingerprint, config_path=self.config.operator_config_path, require_activation=False) is not None:
            raise RuntimeError("native release activation requires valid current signed revalidation authority")
        snapshot = self.board.execution_snapshot(str(row["external_task_id"]))
        if snapshot.task.status != "scheduled":
            raise RuntimeError("native release activation preparation requires exact scheduled pre-state")
        board_path = self.board._resolved_board_db_path()
        identity = board_path.stat()
        snapshot_data = json.loads(canonical_snapshot_json(snapshot))
        authority = {"ticket_id": ticket_id, "revalidation_id": revalidation_id, "external_task_id": str(row["external_task_id"]), "implementation_profile": str(row["implementation_profile"]), "repository_identity": str(row["repository_identity"]), "canonical_worktree_path": str(row["canonical_worktree_path"]), "branch": str(row["branch"]), "base_sha": str(row["base_sha"]), "pre_snapshot_hash": str(row["snapshot_hash"]), "pre_snapshot": snapshot_data, "transition": {"from": "scheduled", "to": "ready"}, "board": {"path": str(board_path), "dev": int(identity.st_dev), "ino": int(identity.st_ino)}, "activation_marker": f"local-first-native-release-activation:{revalidation_id}:{request_key}"}
        document = {"domain": ACTIVATION_DOMAIN, "version": APPROVAL_VERSION, "operation": "activate-native-release", "request_id": request_key, "nonce": secrets.token_urlsafe(24), "operator_id": operator_id, "reason": reason, "authority": authority}
        canonical = canonical_activation_bytes(document)
        return {"document": document, "canonical_document": canonical.decode("utf-8"), "approval_hash": hashlib.sha256(canonical).hexdigest()}

    def activate_native_release_revalidation(self, ticket_id: str, *, revalidation_id: str, operator_id: str, reason: str, request_key: str, approval_document: dict[str, Any] | None = None, detached_signature: bytes | None = None) -> dict[str, Any]:
        """Activate one exact externally signed legacy revalidation; never dispatch or unpause."""
        if not hasattr(self.board, "activate_native_release") or not hasattr(self.board, "execution_snapshot"):
            raise RuntimeError("native release activation requires supported Hermes board mutation and snapshot")
        if not self.board.allow_writes:
            raise PermissionError("native release activation requires --allow-board-writes")
        if approval_document is None or detached_signature is None:
            raise PermissionError("native release activation requires detached operator approval")
        if self.config.operator_config_path is not None:
            from .operator_config import load_operator_config
            fresh = load_operator_config(self.config.operator_config_path)
            if fresh.signer_public_key_bytes != self.config.operator_signer_public_key or fresh.operator_signing_key_fingerprint != self.config.operator_signer_fingerprint:
                raise PermissionError("native release activation external signer configuration changed")
        approval_bytes = canonical_activation_bytes(approval_document)
        verify_detached_signature(approval_bytes, detached_signature, self.config.operator_signer_public_key or b"", self.config.operator_signer_fingerprint or "")
        if approval_document["operator_id"] != operator_id or approval_document["reason"] != reason or approval_document["request_id"] != request_key:
            raise ValueError("native release activation approval identity mismatch")
        row = self.ledger.connection.execute("SELECT * FROM native_dependency_release_revalidations r WHERE r.revalidation_id=? AND r.ticket_id=? AND NOT EXISTS (SELECT 1 FROM native_dependency_release_revalidation_supersessions s WHERE s.old_revalidation_id=r.revalidation_id)", (revalidation_id, ticket_id)).fetchone()
        if row is None:
            raise ValueError("native release activation requires the exact signed revalidation")
        existing_intent = self.ledger.connection.execute("SELECT * FROM native_release_activation_intents WHERE request_key=? OR revalidation_id=?", (request_key, revalidation_id)).fetchall()
        if len(existing_intent) > 1:
            raise RuntimeError("native release activation intent is duplicated")
        if self.ledger.native_dependency_release_migration_required(signer_public_key=self.config.operator_signer_public_key, signer_fingerprint=self.config.operator_signer_fingerprint, config_path=self.config.operator_config_path, require_activation=False) is not None:
            raise RuntimeError("native release activation requires valid current signed revalidation authority")
        # This is an optimistic cross-process boundary: the supported Hermes CLI
        # owns the mutation, so the adapter cannot hold its SQLite transaction
        # across the subprocess.  Pin the configured path and inode before each
        # lock/read/effect checkpoint and reject every replacement, including a
        # same-byte replacement that would otherwise look harmless.
        board_path = self.board._resolved_board_db_path()
        before_pre_lock = board_path.stat()
        self._inject_failure("before_pre_lock")
        snapshot = self.board.execution_snapshot(str(row["external_task_id"]))
        after_pre_lock = self.board._resolved_board_db_path().stat()
        if (int(before_pre_lock.st_dev), int(before_pre_lock.st_ino)) != (int(after_pre_lock.st_dev), int(after_pre_lock.st_ino)):
            raise RuntimeError("native release activation board inode changed during pre-lock")
        self._inject_failure("after_pre_lock")

        def marker_present(marker: str) -> bool:
            return bool(getattr(self.board, "activation_marker_present", lambda *_: False)(str(row["external_task_id"]), marker))

        def signed_authority(intent_row: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            """Validate the persisted signed envelope without using board state."""
            raw = str(intent_row["approval_document_json"] or "").encode("utf-8")
            if not raw or hashlib.sha256(raw).hexdigest() != str(intent_row["approval_document_hash"]):
                raise RuntimeError("native release activation replay approval is invalid")
            document = parse_approval_document(raw)
            verify_detached_signature(raw, base64.b64decode(str(intent_row["detached_signature"]), validate=True), self.config.operator_signer_public_key or b"", str(intent_row["signer_fingerprint"]))
            authority = document["authority"]
            immutable = {"ticket_id": ticket_id, "revalidation_id": revalidation_id, "external_task_id": str(row["external_task_id"]), "implementation_profile": str(row["implementation_profile"]), "repository_identity": str(row["repository_identity"]), "canonical_worktree_path": str(row["canonical_worktree_path"]), "branch": str(row["branch"]), "base_sha": str(row["base_sha"]), "pre_snapshot_hash": str(row["snapshot_hash"]), "activation_marker": str(intent_row["activation_marker"])}
            if any(authority.get(key) != value for key, value in immutable.items()):
                raise RuntimeError("native release activation replay approval conflicts")
            pre = authority.get("pre_snapshot")
            if not isinstance(pre, dict) or hashlib.sha256(canonical_snapshot_json(pre).encode("utf-8")).hexdigest() != str(row["snapshot_hash"]):
                raise RuntimeError("native release activation signed pre-state is invalid")
            return document, pre

        if existing_intent:
            intent_row = existing_intent[0]
            if intent_row["approval_document_json"] != approval_bytes.decode("utf-8") or intent_row["approval_document_hash"] != hashlib.sha256(approval_bytes).hexdigest() or intent_row["signer_fingerprint"] != self.config.operator_signer_fingerprint:
                raise RuntimeError("native release activation replay approval conflicts")
            _, signed_pre = signed_authority(intent_row)
            current_json = canonical_snapshot_json(snapshot)
            pre_match = snapshot.task.status == "scheduled" and current_json == canonical_snapshot_json(signed_pre)
            post_match = snapshot.task.status == "ready" and marker_present(str(intent_row["activation_marker"]))
            if post_match:
                try:
                    validate_activation_post_snapshot(signed_pre, snapshot_authority(snapshot), marker_present=True, marker=str(intent_row["activation_marker"]))
                except ValueError as exc:
                    raise RuntimeError("native release activation replay post-state is not exact") from exc
            elif not pre_match:
                raise RuntimeError("native release activation replay state is ambiguous or stale")
            if intent_row["status"] == "acknowledged":
                if not post_match:
                    raise RuntimeError("native release activation acknowledgement is not exact post-state")
                return dict(intent_row)
            if post_match:
                post_json = canonical_snapshot_json(snapshot)
                post_hash = validate_activation_post_snapshot(signed_pre, snapshot_authority(snapshot), marker_present=True, marker=str(intent_row["activation_marker"]))
                if intent_row["status"] == "pending":
                    self.ledger.mark_native_release_activation_effect(request_key, post_hash, post_json)
                self._inject_failure("after_marker")
                self._inject_failure("before_ack")
                result = self.ledger.acknowledge_native_release_activation(request_key, post_activation_snapshot_hash=post_hash, post_activation_snapshot_json=post_json)
                self._inject_failure("after_ack")
                return result
        actual_hash = canonical_sha256(snapshot_authority(snapshot))
        board_stat = after_pre_lock
        snapshot_data = json.loads(canonical_snapshot_json(snapshot))
        expected_authority = {"ticket_id": ticket_id, "revalidation_id": revalidation_id, "external_task_id": str(row["external_task_id"]), "implementation_profile": str(row["implementation_profile"]), "repository_identity": str(row["repository_identity"]), "canonical_worktree_path": str(row["canonical_worktree_path"]), "branch": str(row["branch"]), "base_sha": str(row["base_sha"]), "pre_snapshot_hash": str(row["snapshot_hash"]), "pre_snapshot": snapshot_data, "transition": {"from": "scheduled", "to": "ready"}, "board": {"path": str(board_path), "dev": int(board_stat.st_dev), "ino": int(board_stat.st_ino)}, "activation_marker": f"local-first-native-release-activation:{revalidation_id}:{request_key}"}
        if approval_document["authority"] != expected_authority:
            raise RuntimeError("native release activation approval is stale or cross-ticket")
        if actual_hash != str(row["snapshot_hash"]) or snapshot.task.status != "scheduled":
            raise RuntimeError("native release activation pre-activation snapshot drift")
        intent = self.ledger.prepare_native_release_activation_intent(
            ticket_id=ticket_id, revalidation_id=revalidation_id, external_task_id=str(row["external_task_id"]),
            pre_activation_snapshot_hash=actual_hash, implementation_profile=str(row["implementation_profile"]),
            repository_identity=str(row["repository_identity"]), canonical_worktree_path=str(row["canonical_worktree_path"]),
            branch=str(row["branch"]), base_sha=str(row["base_sha"]), operator_id=operator_id, reason=reason, request_key=request_key, pre_activation_snapshot_json=canonical_snapshot_json(snapshot_data),
            approval_document_json=approval_bytes.decode("utf-8"), approval_document_hash=hashlib.sha256(approval_bytes).hexdigest(), detached_signature=detached_signature, signer_fingerprint=self.config.operator_signer_fingerprint, board_path=str(board_path), board_dev=int(board_stat.st_dev), board_ino=int(board_stat.st_ino))
        marker = str(intent["activation_marker"])
        if intent["status"] == "acknowledged":
            return intent
        if intent["status"] == "pending":
            released_pre_lock = self.board._resolved_board_db_path().stat()
            if (int(released_pre_lock.st_dev), int(released_pre_lock.st_ino)) != (int(board_stat.st_dev), int(board_stat.st_ino)):
                raise RuntimeError("native release activation board inode changed between pre-lock and effect")
            self._inject_failure("before_effect")
            self.board.activate_native_release(str(row["external_task_id"]), activation_marker=marker, expected_routing={"profile": str(row["implementation_profile"]), "workspace_kind": "worktree", "workspace_path": str(row["canonical_worktree_path"])})
            self._inject_failure("after_unblock")
            after_effect = self.board._resolved_board_db_path().stat()
            if (int(after_effect.st_dev), int(after_effect.st_ino)) != (int(board_stat.st_dev), int(board_stat.st_ino)):
                raise RuntimeError("native release activation board inode changed after effect")
            post = self.board.execution_snapshot(str(row["external_task_id"]))
            self._inject_failure("after_marker")
            post_json = canonical_snapshot_json(post)
            try:
                post_hash = validate_activation_post_snapshot(snapshot_data, snapshot_authority(post), marker_present=marker_present(marker), marker=marker)
            except ValueError as exc:
                raise RuntimeError("native release activation side effect is not exact") from exc
            self.ledger.mark_native_release_activation_effect(request_key, post_hash, post_json)
        post = self.board.execution_snapshot(str(row["external_task_id"]))
        if post.task.status != "ready":
            raise RuntimeError("native release activation acknowledgement requires ready task")
        before_ack = self.board._resolved_board_db_path().stat()
        if (int(before_ack.st_dev), int(before_ack.st_ino)) != (int(board_stat.st_dev), int(board_stat.st_ino)):
            raise RuntimeError("native release activation board inode changed before acknowledgement")
        self._inject_failure("before_ack")
        post_json = canonical_snapshot_json(post)
        post_hash = validate_activation_post_snapshot(snapshot_data, snapshot_authority(post), marker_present=marker_present(marker), marker=marker)
        result = self.ledger.acknowledge_native_release_activation(request_key, post_activation_snapshot_hash=post_hash, post_activation_snapshot_json=post_json)
        self._inject_failure("after_ack")
        return result

    def prepare_native_release_revalidation(self, ticket_id: str, *, operator_id: str, reason: str, implementation_profile: str) -> dict[str, Any]:
        """Read-only first step; never writes ledger, board, repository, or invokes a model."""
        return self.revalidate_native_release(ticket_id, operator_id=operator_id, reason=reason, implementation_profile=implementation_profile, _prepare_only=True)

    def reconcile_hermes_execution(self, external_task_id: str, *, hermes_run_id: int | None = None, require_handoff: bool = False) -> dict[str, Any]:
        """Bind one completed dispatcher-owned Hermes run into Local First.

        This operation never launches an implementation model.  It turns a
        verified external workspace candidate into the same durable
        implementation-stage evidence consumed by deterministic validation.
        """
        if not hasattr(self.board, "execution_snapshot"):
            raise RuntimeError("Hermes execution reconciliation requires execution snapshot support")
        snapshot = self.board.execution_snapshot(external_task_id)
        if snapshot.task.id != external_task_id:
            raise RuntimeError("Hermes execution task identity conflict")
        rows = self.ledger.connection.execute(
            "SELECT DISTINCT t.* FROM tickets t LEFT JOIN board_projection_outbox b ON b.ticket_id=t.id AND b.operation='create_microticket' AND b.acknowledged_at IS NOT NULL AND b.superseded_at IS NULL "
            "WHERE t.external_id=? OR b.external_task_id=? ORDER BY t.created_at,t.id LIMIT 2",
            (external_task_id, external_task_id),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("Hermes execution task identity is ambiguous")
        ticket_row = rows[0] if rows else None
        if ticket_row is None:
            raise KeyError(f"Local First ticket not found for Hermes task {external_task_id}")
        ticket_id = str(ticket_row["id"])
        if self.ledger.resolve_external_task_id(ticket_id) != external_task_id:
            raise RuntimeError("Hermes execution external identity conflict")
        generated_projection = self.ledger.connection.execute(
            """SELECT b.event_id FROM board_projection_outbox b JOIN events e ON e.id=b.event_id
               WHERE b.ticket_id=? AND b.operation='create_microticket' AND b.acknowledged_at IS NOT NULL
                 AND b.superseded_at IS NULL AND b.external_task_id=? AND e.entity_type='ticket' AND e.entity_id=?
                 AND e.event_type IN ('generated_microticket_created','generated_microticket_projection_recovered')""",
            (ticket_id, external_task_id, ticket_id),
        ).fetchall()
        generated_owned = ticket_row["external_id"] is None and len(generated_projection) == 1
        completed_generated_handoff = (
            require_handoff
            and snapshot.task.status == "done"
            and generated_owned
            and HANDOFF_MARKER in str(snapshot.task.body or "")
        )
        if snapshot.task.status == "done" and not completed_generated_handoff:
            raise RuntimeError("hermes_completion_authority_bypassed_reconciliation_required")
        if require_handoff and snapshot.task.status != "blocked" and not completed_generated_handoff:
            raise RuntimeError("Hermes execution handoff is not blocked for reconciliation")
        activation = self.ledger.connection.execute(
            """SELECT i.*, e.acknowledged_at FROM native_release_activation_intents i
               JOIN native_release_activation_evidence e ON e.request_key=i.request_key
               WHERE i.external_task_id=? AND i.status='acknowledged' ORDER BY e.acknowledged_at DESC""",
            (external_task_id,),
        ).fetchall()
        activated = activation[-1] if activation else None
        if activated is not None:
            # The acknowledged post snapshot is immutable authority.  Hermes may
            # append exactly one dispatcher continuation after it; running is a
            # live external owner and must not be reconciled or mutated here.
            post_authority = json.loads(str(activated["post_activation_snapshot_json"] or "null"))
            current_kind = validate_activation_continuation_snapshot(
                post_authority,
                snapshot_authority(snapshot),
                acknowledged_at=int(activated["acknowledged_at"]),
                profile=str(activated["implementation_profile"]),
                workspace_path=str(activated["canonical_worktree_path"]),
                branch=str(activated["branch"]),
                repository_identity=str(activated["repository_identity"]),
                base_sha=str(activated["base_sha"]),
                handoff_summary=HANDOFF_SENTINEL,
            )
            active_run_ids = [run.id for run in snapshot.runs if run.status == "running"]
            if len(active_run_ids) > 1:
                raise RuntimeError("Hermes activation continuation has multiple active runs")
            active_run_id = active_run_ids[0] if active_run_ids else None
            if hermes_run_id is not None and active_run_id != hermes_run_id:
                raise RuntimeError("Hermes activation continuation run identity mismatch")
            if current_kind == "running":
                if require_handoff:
                    raise RuntimeError("Hermes activation continuation is still running")
                return {"ticket_id": ticket_id, "external_task_id": external_task_id, "status": "externally_running", "run_id": active_run_id}
            if snapshot.task.status != "blocked" or not require_handoff:
                require_handoff = True
        if require_handoff:
            if completed_generated_handoff:
                candidates = [
                    run for run in snapshot.runs
                    if run.status in {"done", "completed"}
                    and run.outcome in {"completed", "success", "succeeded"}
                    and not str(run.summary or "").startswith("local-first projection ")
                ]
            else:
                candidates = [
                    run for run in snapshot.runs
                    if run.status == "blocked"
                    and run.outcome == "blocked"
                    and str(run.summary or "") == HANDOFF_SENTINEL
                ]
        else:
            candidates = [
                run for run in snapshot.runs
                if run.status in {"done", "completed", "blocked"}
                and (
                    run.outcome in {"completed", "success", "succeeded"}
                    or (run.outcome == "blocked" and str(run.summary or "") == HANDOFF_SENTINEL)
                )
                and not str(run.summary or "").startswith("local-first projection ")
            ]
        if hermes_run_id is not None:
            candidates = [run for run in candidates if run.id == hermes_run_id]
        if not candidates:
            raise RuntimeError("no completed Hermes worker run available for reconciliation")
        unreconciled = [run for run in candidates if self.ledger.hermes_execution_reconciliation(external_task_id, run.id) is None]
        if require_handoff and not unreconciled:
            raise RuntimeError("no unreconciled Hermes worker run available for reconciliation")
        if hermes_run_id is None:
            if len(unreconciled) > 1:
                raise RuntimeError("multiple Hermes worker runs require explicit run id")
            run = unreconciled[0] if unreconciled else candidates[-1]
        else:
            run = candidates[0]
        existing = self.ledger.hermes_execution_reconciliation(external_task_id, run.id)
        if existing is not None:
            return {**existing, "status": "already_reconciled"}

        repository, worktree_root, artifact_root = self.config.validate_execution_roots()
        binding = self.ledger.runtime_binding(ticket_id)
        workspace = Path(snapshot.task.workspace_path or "").expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise RuntimeError("Hermes execution workspace is not a directory")

        def git(*args: str, cwd: Path = workspace, check: bool = True) -> subprocess.CompletedProcess[str]:
            try:
                return subprocess.run(("git", *args), cwd=cwd, text=True, capture_output=True, timeout=30, check=check)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(exc.stderr.strip() or exc.stdout.strip() or "Hermes execution Git provenance check failed") from exc

        workspace_root = Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=True)
        if workspace_root != workspace:
            raise RuntimeError("Hermes execution workspace must be the Git toplevel")
        workspace_common_raw = git("rev-parse", "--git-common-dir").stdout.strip()
        workspace_common = (workspace / workspace_common_raw).resolve(strict=True) if not Path(workspace_common_raw).is_absolute() else Path(workspace_common_raw).resolve(strict=True)
        repo_common_raw = git("rev-parse", "--git-common-dir", cwd=repository).stdout.strip()
        repo_common = (repository / repo_common_raw).resolve(strict=True) if not Path(repo_common_raw).is_absolute() else Path(repo_common_raw).resolve(strict=True)
        if workspace_common != repo_common:
            raise RuntimeError("Hermes execution workspace is not attached to the configured repository")

        adapter = GitWorktreeAdapter(repository, worktree_root)
        base_sha = adapter.resolve_execution_base(ticket_row["tranche_id"] or None, str(binding["starting_sha"]))
        head_sha = git("rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        if git("merge-base", "--is-ancestor", base_sha, head_sha, check=False).returncode != 0:
            raise RuntimeError("Hermes execution HEAD does not descend from the authoritative execution base")
        diff = git("diff", "--binary", "--no-ext-diff", base_sha, "--").stdout
        diff_hash = hashlib.sha256(diff.encode()).hexdigest()
        if not diff.strip():
            raise RuntimeError("Hermes execution produced no candidate diff")

        pending_repair_attempt = self.ledger.pending_repair_attempt_number(ticket_id)
        attempt_number = pending_repair_attempt if pending_repair_attempt is not None else self.ledger.next_attempt_number(ticket_id)
        artifact_dir = artifact_root / ticket_id / str(attempt_number)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "adapter": "hermes-dispatch",
            "external_task_id": external_task_id,
            "hermes_run": {
                "id": run.id, "status": run.status, "outcome": run.outcome,
                "started_at": run.started_at, "ended_at": run.ended_at,
                "summary": run.summary, "profile": run.profile,
                "worker_pid": run.worker_pid, "metadata": run.metadata,
            },
            "session_id": snapshot.session_id,
            "branch_name": snapshot.branch_name,
            "workspace_path": str(workspace),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "diff_hash": diff_hash,
        }
        snapshot_hash = canonical_sha256(payload)
        artifact_path = artifact_dir / f"hermes-execution-{run.id}.json"
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        if artifact_path.exists() and artifact_path.read_text() != encoded:
            raise RuntimeError("Hermes execution artifact conflict")
        artifact_path.write_text(encoded)
        row = self.ledger.record_hermes_execution_reconciliation(
            external_task_id=external_task_id,
            hermes_run_id=run.id,
            ticket_id=ticket_id,
            attempt_number=attempt_number,
            run_status=run.status,
            run_outcome=run.outcome,
            session_id=snapshot.session_id,
            branch_name=snapshot.branch_name,
            workspace_path=str(workspace),
            base_sha=base_sha,
            head_sha=head_sha,
            diff_hash=diff_hash,
            artifact_path=str(artifact_path),
            snapshot_hash=snapshot_hash,
        )
        return {**row, "status": "reconciled"}

    def prepare_hermes_dispatch_worktree(self, ticket_id: str, external_task_id: str) -> dict[str, str]:
        """Prepare Hermes' canonical task worktree inside pinned Git objects."""
        repository, worktree_root, _ = self.config.validate_execution_roots()
        ticket = self.ledger.get_ticket(ticket_id)
        binding = self.ledger.runtime_binding(ticket_id)
        if self.ledger.resolve_external_task_id(ticket_id) != external_task_id:
            raise RuntimeError("hermes_dispatch_worktree_reconciliation_required: external identity drift")
        target, _ = validate_native_workspace_path(
            canonical_native_workspace_path(repository, external_task_id),
            repository=repository,
            external_task_id=external_task_id,
            require_existing=False,
        )
        branch = f"wt/{external_task_id}"
        with PinnedNativeWorkspace.open(repository, target) as pin:
            planning_base = pin.git("rev-parse", "--verify", f"{binding['starting_sha']}^{{commit}}").stdout.strip()
            tranche_id = ticket.get("tranche_id") or None
            if tranche_id is None:
                base_sha = planning_base
            else:
                ref = f"refs/local-first/tranches/{tranche_id}/integration-head"
                if pin.git("check-ref-format", ref, check=False).returncode:
                    raise RuntimeError("invalid tranche id for integration head")
                current = pin.git("show-ref", "--verify", "--hash", ref, check=False)
                if current.returncode == 0:
                    base_sha = pin.git("rev-parse", "--verify", f"{current.stdout.strip()}^{{commit}}").stdout.strip()
                else:
                    created = pin.git("update-ref", ref, planning_base, "0" * 40, check=False)
                    if created.returncode == 0:
                        base_sha = planning_base
                    else:
                        current = pin.git("show-ref", "--verify", "--hash", ref, check=False)
                        if current.returncode != 0:
                            raise RuntimeError("unable to create tranche integration head")
                        base_sha = pin.git("rev-parse", "--verify", f"{current.stdout.strip()}^{{commit}}").stdout.strip()

            if target.exists():
                pin.pin_existing_target(already_created=True)
                target_git_fd = pin.target_git_fd()
                target_root = Path(pin.git("rev-parse", "--show-toplevel", target=True, git_fd_override=target_git_fd).stdout.strip()).resolve(strict=True)
                common_raw = pin.git("rev-parse", "--git-common-dir", target=True, git_fd_override=target_git_fd).stdout.strip()
                target_common = (target / common_raw).resolve(strict=True) if not Path(common_raw).is_absolute() else Path(common_raw).resolve(strict=True)
                repo_common_raw = pin.git("rev-parse", "--git-common-dir").stdout.strip()
                repo_common = (repository / repo_common_raw).resolve(strict=True) if not Path(repo_common_raw).is_absolute() else Path(repo_common_raw).resolve(strict=True)
                head = pin.git("rev-parse", "HEAD", target=True, git_fd_override=target_git_fd).stdout.strip()
                actual_branch = pin.git("branch", "--show-current", target=True, git_fd_override=target_git_fd).stdout.strip()
                status = pin.git("status", "--porcelain=v1", target=True, git_fd_override=target_git_fd).stdout.strip()
                if target_root != target or target_common != repo_common or head != base_sha or actual_branch != branch or status:
                    raise RuntimeError("hermes_dispatch_worktree_reconciliation_required: existing worktree drift")
                validate_native_workspace_path(target, repository=repository, external_task_id=external_task_id)
                pin.final_revalidate()
                return {"workspace_path": str(target), "branch_name": branch, "base_sha": base_sha}

            pin.revalidate(target_must_exist=False)
            pin.require_expected_metadata_absent(external_task_id)
            branch_check = pin.git("rev-parse", "--verify", f"refs/heads/{branch}", check=False)
            if branch_check.returncode == 0 and branch_check.stdout.strip() != base_sha:
                raise RuntimeError("hermes_dispatch_worktree_reconciliation_required: existing branch drift")
            if branch_check.returncode == 0:
                pin.git("worktree", "add", "-q", external_task_id, branch, creates_target=True, cwd_fd_override=pin.worktree_parent_fd)
            else:
                pin.git("worktree", "add", "-q", "-b", branch, external_task_id, base_sha, creates_target=True, cwd_fd_override=pin.worktree_parent_fd)
            pin.pin_existing_target(already_created=True)
            target_git_fd = pin.target_git_fd()
            target_root = Path(pin.git("rev-parse", "--show-toplevel", target=True, git_fd_override=target_git_fd).stdout.strip()).resolve(strict=True)
            common_raw = pin.git("rev-parse", "--git-common-dir", target=True, git_fd_override=target_git_fd).stdout.strip()
            target_common = (target / common_raw).resolve(strict=True) if not Path(common_raw).is_absolute() else Path(common_raw).resolve(strict=True)
            repo_common_raw = pin.git("rev-parse", "--git-common-dir").stdout.strip()
            repo_common = (repository / repo_common_raw).resolve(strict=True) if not Path(repo_common_raw).is_absolute() else Path(repo_common_raw).resolve(strict=True)
            head = pin.git("rev-parse", "HEAD", target=True, git_fd_override=target_git_fd).stdout.strip()
            actual_branch = pin.git("branch", "--show-current", target=True, git_fd_override=target_git_fd).stdout.strip()
            if target_root != target or target_common != repo_common or head != base_sha or actual_branch != branch:
                raise RuntimeError("hermes_dispatch_worktree_reconciliation_required: created worktree drift")
            validate_native_workspace_path(target, repository=repository, external_task_id=external_task_id)
            pin.final_revalidate()
            return {"workspace_path": str(target), "branch_name": branch, "base_sha": base_sha}

    def prepare_hermes_repair_worktree(self, ticket_id: str, external_task_id: str, attempt_number: int) -> dict[str, str]:
        """Verify a generated repair retry starts from the previous reconciled Hermes head."""
        repository, _, _ = self.config.validate_execution_roots()
        if self.ledger.resolve_external_task_id(ticket_id) != external_task_id:
            raise RuntimeError("hermes_repair_worktree_reconciliation_required: external identity drift")
        attempt = self.ledger.connection.execute(
            "SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?",
            (ticket_id, attempt_number),
        ).fetchone()
        prior = self.ledger.connection.execute(
            "SELECT * FROM hermes_execution_reconciliations WHERE ticket_id=? AND attempt_number=?",
            (ticket_id, attempt_number - 1),
        ).fetchone()
        if attempt is None or prior is None:
            raise RuntimeError("hermes_repair_worktree_reconciliation_required: repair provenance is incomplete")
        target, _ = validate_native_workspace_path(
            canonical_native_workspace_path(repository, external_task_id),
            repository=repository,
            external_task_id=external_task_id,
        )
        expected_branch = str(prior["branch_name"] or f"wt/{external_task_id}")
        expected_head = str(prior["head_sha"])
        if str(attempt["worktree_path"] or "") != str(target) or str(attempt["base_sha"]) != str(prior["base_sha"]):
            raise RuntimeError("hermes_repair_worktree_reconciliation_required: preallocated repair attempt drift")
        with PinnedNativeWorkspace.open(repository, target) as pin:
            pin.pin_existing_target(already_created=True)
            target_git_fd = pin.target_git_fd()
            target_root = Path(pin.git("rev-parse", "--show-toplevel", target=True, git_fd_override=target_git_fd).stdout.strip()).resolve(strict=True)
            common_raw = pin.git("rev-parse", "--git-common-dir", target=True, git_fd_override=target_git_fd).stdout.strip()
            target_common = (target / common_raw).resolve(strict=True) if not Path(common_raw).is_absolute() else Path(common_raw).resolve(strict=True)
            repo_common_raw = pin.git("rev-parse", "--git-common-dir").stdout.strip()
            repo_common = (repository / repo_common_raw).resolve(strict=True) if not Path(repo_common_raw).is_absolute() else Path(repo_common_raw).resolve(strict=True)
            head = pin.git("rev-parse", "HEAD", target=True, git_fd_override=target_git_fd).stdout.strip()
            branch = pin.git("branch", "--show-current", target=True, git_fd_override=target_git_fd).stdout.strip()
            status = pin.git("status", "--porcelain=v1", target=True, git_fd_override=target_git_fd).stdout.strip()
            if target_root != target or target_common != repo_common or head != expected_head or branch != expected_branch or status:
                raise RuntimeError("hermes_repair_worktree_reconciliation_required: existing repair worktree drift")
            pin.final_revalidate()
        return {
            "workspace_path": str(target),
            "branch_name": expected_branch,
            "base_sha": str(prior["base_sha"]),
            "starting_head_sha": expected_head,
            "attempt_number": str(attempt_number),
        }

    def dry_run(self, task_id: str) -> dict[str, object]:
        row=self.ledger.get_ticket(task_id); binding=self.ledger.runtime_binding(task_id)
        return {"ticket_id":task_id,"state":row["state"],"repository":binding["repository_path"],"starting_sha":binding["starting_sha"],"would_invoke_model":False,"would_write_board":False,"would_modify_repository":False}

    def adopt_existing_implementation(self, ticket_id: str, *, repository: Path, operator_id: str, reason: str) -> dict[str, object]:
        """Adopt a pre-existing native-worktree diff without claiming model or worker provenance."""
        if not operator_id.strip() or not reason.strip():
            raise ValueError("manual adoption requires operator identity and reason")
        paused = self.ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
        if paused is None or not paused["paused"]:
            raise PermissionError("manual implementation adoption requires Local First paused")
        ticket_row = self.ledger.get_ticket(ticket_id)
        if ticket_row["state"] not in {CanonicalState.READY_LOCAL.value, CanonicalState.IMPLEMENTING.value}:
            raise RuntimeError("manual adoption requires ready_local ticket or exact replay")
        binding = self.ledger.runtime_binding(ticket_id)
        repo, worktree_root, artifact_root = self.config.validate_execution_roots()
        if repo != Path(repository).resolve(strict=True) or str(repo) != str(binding["repository_path"]):
            raise ValueError("repository mismatch with imported binding")
        ticket = ticket_from_ledger(ticket_row)
        external_task_id = self.ledger.resolve_external_task_id(ticket_id)
        reconciliation = self.ledger.failed_attempt_reconciliation(ticket_id)
        attempt_number = int(reconciliation["prospective_next_attempt_number"]) if reconciliation is not None else 1
        if attempt_number > ticket.max_attempts:
            raise RuntimeError("manual adoption exceeds ticket max_attempts")
        if reconciliation is not None and bool(reconciliation["cleanup_required"]) and not self.ledger.cleanup_confirmed(ticket_id, int(reconciliation["retired_attempt_number"])):
            raise RuntimeError("manual adoption retry requires retired-attempt cleanup confirmation")
        path, _ = validate_native_workspace_path(
            canonical_native_workspace_path(repo, external_task_id), repository=repo, external_task_id=external_task_id
        )
        branch = f"wt/{external_task_id}"

        def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            try:
                return subprocess.run(safe_git_argv(args), cwd=path, env=safe_git_env(), text=True, capture_output=True, check=check, timeout=30)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(exc.stderr.strip() or exc.stdout.strip() or "manual adoption Git check failed") from exc

        if git("rev-parse", "--show-toplevel").stdout.strip() != str(path):
            raise RuntimeError("manual adoption workspace is not the Git toplevel")
        repo_common_raw = subprocess.run(safe_git_argv(("rev-parse", "--git-common-dir")), cwd=repo, env=safe_git_env(), text=True, capture_output=True, check=True, timeout=30).stdout.strip()
        path_common_raw = git("rev-parse", "--git-common-dir").stdout.strip()
        repo_common = (repo / repo_common_raw).resolve(strict=True) if not Path(repo_common_raw).is_absolute() else Path(repo_common_raw).resolve(strict=True)
        path_common = (path / path_common_raw).resolve(strict=True) if not Path(path_common_raw).is_absolute() else Path(path_common_raw).resolve(strict=True)
        if path_common != repo_common:
            raise RuntimeError("manual adoption worktree is not attached to canonical repository")
        if git("branch", "--show-current").stdout.strip() != branch:
            raise RuntimeError("manual adoption branch identity mismatch")
        base = str(reconciliation["retry_base_sha"]) if reconciliation is not None else GitWorktreeAdapter(repo, worktree_root).resolve_execution_base(ticket_row.get("tranche_id") or None, str(binding["starting_sha"]))
        if git("rev-parse", "HEAD").stdout.strip() != base:
            raise RuntimeError("manual adoption worktree HEAD does not match authoritative execution base")

        allowed_existing = set(ticket.allowed_files)
        allowed_created = set(ticket.create_files) | set(ticket.new_test_files)
        allowed = allowed_existing | allowed_created
        candidate = _isolated_candidate_diff(path, base, allowed_new_paths=tuple(sorted(allowed_created)))
        untracked = tuple(candidate["untracked_paths"])
        unexpected_untracked = sorted(set(untracked) - allowed_created)
        if unexpected_untracked:
            raise RuntimeError("manual adoption contains unapproved untracked paths: " + ", ".join(unexpected_untracked))
        changed = tuple(candidate["changed_paths"])
        if not changed:
            raise RuntimeError("manual adoption candidate has no diff")
        unexpected = sorted(set(changed) - allowed)
        if unexpected:
            raise RuntimeError("manual adoption exceeds allowed file scope: " + ", ".join(unexpected))
        if len(set(changed)) > ticket.patch_budget.max_files:
            raise RuntimeError("manual adoption exceeds max_files patch budget")
        changed_lines = 0
        for line in str(candidate["numstat"]).splitlines():
            added, deleted, _ = line.split("\t", 2)
            if not added.isdigit() or not deleted.isdigit():
                raise RuntimeError("manual adoption binary diff is not permitted")
            changed_lines += int(added) + int(deleted)
        if changed_lines > ticket.patch_budget.max_changed_lines:
            raise RuntimeError("manual adoption exceeds max_changed_lines patch budget")
        diff = str(candidate["diff"])
        diff_hash = str(candidate["diff_hash"])
        selected_sha256 = {
            relative: hashlib.sha256((path / relative).read_bytes()).hexdigest()
            for relative in sorted(set(changed)) if (path / relative).is_file()
        }
        payload = {
            "schema": "manual-implementation-adoption/v1",
            "ticket_id": ticket_id,
            "external_task_id": external_task_id,
            "attempt_number": attempt_number,
            "operator_id": operator_id,
            "reason": reason,
            "repository": str(repo),
            "worktree_path": str(path),
            "branch": branch,
            "base_sha": base,
            "diff_hash": diff_hash,
            "changed_paths": sorted(set(changed)),
            "changed_lines": changed_lines,
            "selected_sha256": selected_sha256,
        }
        artifact_path = artifact_root / ticket_id / str(attempt_number) / "manual-adoption.json"
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        artifact_sha256 = _write_replayable_artifact(artifact_path, encoded)
        recorded = self.ledger.record_manual_implementation_adoption(
            ticket_id=ticket_id, attempt_number=attempt_number, operator_id=operator_id, reason=reason,
            branch=branch, workspace_path=str(path), base_sha=base, diff_hash=diff_hash,
            artifact_path=str(artifact_path), artifact_sha256=artifact_sha256,
        )
        return {
            "ticket_id": ticket_id,
            "attempt_number": attempt_number,
            "status": str(recorded["status"]),
            "implementation_artifact": str(artifact_path),
            "diff_hash": diff_hash,
            "changed_paths": sorted(set(changed)),
            "changed_lines": changed_lines,
            "state": self.ledger.get_ticket(ticket_id)["state"],
        }

    def cleanup_completed_ticket_worktree(self, ticket_id: str, *, repository: Path) -> dict[str, object]:
        """Remove only an acknowledged, accepted, clean terminal ticket worktree."""
        existing = self.ledger.runtime_stage(ticket_id, "worktree_cleanup")
        if existing is not None:
            return {"ticket_id": ticket_id, "status": "already_cleaned"}
        ticket = self.ledger.get_ticket(ticket_id)
        if ticket["state"] != CanonicalState.DONE.value:
            raise PermissionError("worktree cleanup requires done ticket")
        accepted_commit = self.ledger.accepted_commit(ticket_id)
        if not accepted_commit:
            raise PermissionError("worktree cleanup requires accepted evidence")
        projection = self.ledger.connection.execute(
            "SELECT 1 FROM events e JOIN board_projection_outbox b ON b.ticket_id=e.entity_id AND b.event_id=e.id "
            "WHERE e.entity_type='ticket' AND e.entity_id=? AND e.event_type='state_transition' AND e.to_state='done' "
            "AND b.operation='set_state' AND b.acknowledged_at IS NOT NULL AND b.superseded_at IS NULL LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if projection is None:
            raise PermissionError("worktree cleanup requires acknowledged done projection")
        attempt = self.ledger.connection.execute(
            "SELECT * FROM attempts WHERE ticket_id=? AND accepted_commit_sha=? ORDER BY attempt_number DESC LIMIT 1",
            (ticket_id, accepted_commit),
        ).fetchone()
        if attempt is None or not attempt["worktree_path"]:
            raise RuntimeError("worktree cleanup requires accepted attempt workspace")
        repo, worktree_root, _ = self.config.validate_execution_roots()
        requested_repo = self.config.canonical_repository(Path(repository))
        if requested_repo != repo:
            raise ValueError("repository mismatch with runtime configuration")
        path = Path(str(attempt["worktree_path"]))
        lexical = Path(path.absolute())
        external_id = str(ticket.get("external_id") or "")
        native_path = canonical_native_workspace_path(repo, external_id) if external_id else None
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            resolved = None
        allowed_local = self.config._inside(resolved, worktree_root) if resolved is not None else self.config._inside(lexical, worktree_root)
        allowed_native = native_path is not None and lexical == native_path
        if allowed_native and resolved is not None:
            validate_native_workspace_path(path, repository=repo, external_task_id=external_id)
            allowed_native = resolved == native_path.resolve(strict=True)
        if not (allowed_local or allowed_native):
            raise RuntimeError("recorded worktree is outside authorized ticket workspace roots")
        status = cleanup_completed_worktree(repo, path, accepted_commit_sha=accepted_commit)
        detail = json.dumps({"status": status, "worktree_path": str(path), "accepted_commit_sha": accepted_commit}, sort_keys=True)
        self.ledger.record_runtime_stage(
            ticket_id,
            "worktree_cleanup",
            detail,
            attempt_number=int(attempt["attempt_number"]),
            base_sha=str(attempt["base_sha"] or ""),
        )
        return {"ticket_id": ticket_id, "status": status, "worktree_path": str(path)}

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
            if not rechecks or rechecks[-1]["status"] != "recheck_passed" or _recheck_evidence_hash(rechecks[-1]) != str(rechecks[-1]["evidence_hash"]):
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
        snap = repository_snapshot(repo, base, feature=spec.contract, feature_terms=tuple(f.path for f in spec.files), limit=32, authorized_modify_paths=tuple(f.path for f in spec.files if f.disposition == "modify"), authorized_create_paths=tuple(f.path for f in spec.files if f.disposition == "create"))
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
        for relative in (*ticket.create_files, *ticket.new_test_files):
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

    def execute_implementation_model_only(self, ticket_id: str, *, repository: Path) -> dict[str, object]:
        """Run or replay exactly the implementation-model stage.

        This operation assumes the scheduler already moved the ticket to
        ``implementing`` and owns the durable scheduler claim.  It never runs
        deterministic validation, review, repair, commit, or integration.
        """
        binding = self.ledger.runtime_binding(ticket_id)
        raw_repository = Path(repository).resolve(strict=True)
        if str(raw_repository) != binding["repository_path"]:
            raise ValueError("repository mismatch with imported binding")
        repo, worktree_root, artifact_root = self.config.validate_execution_roots()
        if repo != raw_repository:
            raise ValueError("repository mismatch with configured canonical repository")
        ticket = ticket_from_ledger(self.ledger.get_ticket(ticket_id))
        if ticket.risk != "low":
            raise PermissionError("only low-risk tickets may execute locally")
        state = CanonicalState(self.ledger.get_ticket(ticket_id)["state"])
        if state != CanonicalState.IMPLEMENTING:
            raise RuntimeError("implementation model stage requires implementing state")
        incomplete = self.ledger.incomplete_model_invocations(ticket_id)
        if incomplete:
            raise RuntimeError("execution_reconciliation_required: incomplete model invocation")
        reconciliation = self.ledger.failed_attempt_reconciliation(ticket_id)
        if reconciliation is not None and bool(reconciliation["cleanup_required"]) and not self.ledger.cleanup_confirmed(ticket_id, int(reconciliation["retired_attempt_number"])):
            raise RuntimeError("retired attempt cleanup confirmation required before retry execution")

        planning_base = str(binding["starting_sha"])
        canonical_sha = self._canonical_provenance_sha(ticket_id)
        worktrees = GitWorktreeAdapter(repo, worktree_root)
        base = worktrees.resolve_execution_base(self.ledger.get_ticket(ticket_id)["tranche_id"] or None, planning_base)
        self.ledger.record_runtime_stage(ticket_id, "execution_base", base)
        reconciliation = self.ledger.failed_attempt_reconciliation(ticket_id)
        if reconciliation is not None:
            attempt_number = int(reconciliation["prospective_next_attempt_number"])
            if self.ledger.connection.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone():
                raise RuntimeError("reconciliation-authorized attempt already exists; reconciliation required")
        else:
            latest = self.ledger.connection.execute("SELECT attempt_number, worktree_path FROM attempts WHERE ticket_id=? ORDER BY attempt_number DESC LIMIT 1", (ticket_id,)).fetchone()
            if latest is not None and latest["worktree_path"]:
                attempt_number = int(latest["attempt_number"])
            else:
                attempt_number = self.ledger.next_attempt_number(ticket_id)
        if attempt_number > ticket.max_attempts:
            raise RuntimeError("implementation attempt limit exhausted")

        existing_attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        fresh_attempt = not bool(existing_attempt and existing_attempt["worktree_path"])
        self.ledger.ensure_attempt(ticket_id, attempt_number)
        existing_stage = self.ledger.model_stage(ticket_id, attempt_number, "implementation")
        prior_invocation = self.ledger.invocation_for_stage(ticket_id, attempt_number, "implementation")
        if existing_stage is None and prior_invocation is not None:
            prior_status = str(prior_invocation["status"])
            if prior_status == "completed":
                artifact_value = prior_invocation.get("model_artifact")
                if not artifact_value:
                    raise RuntimeError("completed implementation invocation is missing its artifact; reconciliation required")
                artifact = Path(str(artifact_value))
                attempt_path = Path(str(prior_invocation["worktree_path"]))
                if not artifact.is_file() or not attempt_path.is_dir():
                    raise RuntimeError("completed implementation invocation artifacts are incomplete; reconciliation required")
                diff_hash = worktrees.diff_hash(attempt_path)
                recorded = self.ledger.record_model_stage(
                    ticket_id,
                    attempt_number,
                    "implementation",
                    purpose="implementation",
                    adapter=type(self.local_model).__name__,
                    request_hash=str(prior_invocation["packet_hash"]),
                    response_artifact=str(artifact),
                    worktree_path=str(attempt_path),
                    base_sha=base,
                    diff_hash=diff_hash,
                )
                if not recorded and self.ledger.model_stage(ticket_id, attempt_number, "implementation") is None:
                    raise RuntimeError("completed implementation invocation could not be recovered")
                self.ledger.record_runtime_stage(ticket_id, f"implementation-{attempt_number}", str(artifact))
                self.ledger.record_runtime_stage(ticket_id, "implementation_completed", str(artifact))
                return {
                    "ticket_id": ticket_id,
                    "attempt_number": attempt_number,
                    "implementation_artifact": str(artifact),
                    "diff_hash": diff_hash,
                    "replayed": True,
                }
            if prior_status in {"timeout", "process_error", "malformed_output"}:
                raise RuntimeError("execution_reconciliation_required: prior implementation invocation failed")
        attempt = self._attempt(worktrees, ticket_id, attempt_number, base)
        if existing_stage is not None:
            artifact = Path(str(existing_stage["response_artifact"]))
            if not artifact.is_file():
                raise RuntimeError("persisted implementation artifact is missing")
            if worktrees.diff_hash(attempt.path) != str(existing_stage["diff_hash"]):
                raise RuntimeError("persisted implementation diff does not match; reconciliation required")
            return {
                "ticket_id": ticket_id,
                "attempt_number": attempt_number,
                "implementation_artifact": str(artifact),
                "diff_hash": str(existing_stage["diff_hash"]),
                "replayed": True,
            }

        if fresh_attempt:
            self._assert_pre_inference_isolation(repository=repo, attempt=attempt, base_sha=base, ticket=ticket, canonical_sha=canonical_sha)
        artifacts_root = artifact_root / ticket_id / str(attempt_number)
        artifacts_root.mkdir(parents=True, exist_ok=True)
        failure_evidence = ""
        repair_stage = self.ledger.connection.execute(
            "SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage LIKE 'repair-routing-%' ORDER BY attempt_number DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if repair_stage is not None:
            try:
                repair_detail = json.loads(str(repair_stage["detail"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("repair routing evidence is malformed") from exc
            if repair_detail.get("action") == "repair" and int(repair_detail.get("next_attempt_number") or 0) == attempt_number:
                failure_evidence = str(repair_detail.get("failure_evidence") or "")
        packet = ContextPacketBuilder().build_from_repository(
            ticket,
            attempt.path,
            repository_rules="Edit only allowed files. Return JSON only.",
            failure_evidence=failure_evidence,
        )
        ContextPacketBuilder().write_artifacts(packet, artifact_root=artifacts_root)
        request_hash = hashlib.sha256(packet.text.encode()).hexdigest()
        if hasattr(self.local_model, "implementation_timeout_seconds"):
            self.local_model.implementation_timeout_seconds = self.config.implementation_timeout_seconds
        invocation_id = uuid.uuid4().hex
        provider = str(getattr(self.local_model, "provider", type(self.local_model).__name__))
        model = str(getattr(self.local_model, "model", type(self.local_model).__name__))
        self.ledger.start_model_invocation(
            invocation_id=invocation_id,
            ticket_id=ticket_id,
            attempt_number=attempt_number,
            stage="implementation",
            provider=provider,
            model=model,
            packet_hash=request_hash,
            worktree_path=str(attempt.path),
            timeout_seconds=self.config.implementation_timeout_seconds,
        )
        self._crash("implementation_invocation_started")
        started = time.monotonic()
        try:
            result = self.local_model.invoke("implementation", packet.text, artifact_dir=artifacts_root, workdir=attempt.path)
        except subprocess.TimeoutExpired as exc:
            self.ledger.finish_model_invocation(invocation_id, status="timeout", duration_seconds=time.monotonic() - started, error={"type": "TimeoutExpired", "timeout_seconds": self.config.implementation_timeout_seconds, "process": str(exc)[:1000]})
            raise
        except Exception as exc:
            self.ledger.finish_model_invocation(invocation_id, status="process_error", duration_seconds=time.monotonic() - started, error={"type": type(exc).__name__, "message": str(exc)[:1000]})
            raise
        response_path = getattr(result, "artifact_path", artifacts_root / "implementation-result.json")
        self.ledger.finish_model_invocation(invocation_id, status="completed", duration_seconds=time.monotonic() - started, model_artifact=str(response_path))
        self._crash("implementation_invocation_completed")
        diff_hash = worktrees.diff_hash(attempt.path)
        self.ledger.record_model_stage(ticket_id, attempt_number, "implementation", purpose="implementation", adapter=type(self.local_model).__name__, request_hash=request_hash, response_artifact=str(response_path), worktree_path=str(attempt.path), base_sha=base, diff_hash=diff_hash)
        self.ledger.record_runtime_stage(ticket_id, f"implementation-{attempt_number}", str(response_path))
        self.ledger.record_runtime_stage(ticket_id, "implementation_completed", str(response_path))
        self._crash("implementation_completed")
        return {
            "ticket_id": ticket_id,
            "attempt_number": attempt_number,
            "implementation_artifact": str(response_path),
            "diff_hash": diff_hash,
            "replayed": False,
        }

    def execute_deterministic_validation_only(self, ticket_id: str, *, repository: Path) -> dict[str, object]:
        """Validate one scheduler-claimed implementation candidate, without review or repair."""
        binding = self.ledger.runtime_binding(ticket_id)
        raw_repository = Path(repository).resolve(strict=True)
        repo, worktree_root, artifact_root = self.config.validate_execution_roots()
        if repo != raw_repository or str(repo) != binding["repository_path"]:
            raise ValueError("repository mismatch with imported binding")
        ticket_row = self.ledger.get_ticket(ticket_id)
        if ticket_row["state"] not in {CanonicalState.VERIFYING.value, CanonicalState.LOCAL_REVIEW.value, CanonicalState.NEEDS_TRIAGE.value}:
            raise RuntimeError("deterministic validation requires verifying state")
        claim_row = self.ledger.connection.execute(
            "SELECT c.* FROM scheduler_stage_claims c WHERE c.ticket_id=? AND c.status='claimed' "
            "AND c.stage=('validation:' || (SELECT MAX(m.attempt_number) FROM model_stage_artifacts m WHERE m.ticket_id=c.ticket_id AND m.stage='implementation')) LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if claim_row is None or not claim_row["candidate_identity_json"]:
            raise RuntimeError("validation_reconciliation_required: identity-bound scheduler claim is missing")
        identity = json.loads(str(claim_row["candidate_identity_json"]))
        attempt_number = int(identity["attempt_number"])
        implementation = self.ledger.model_stage(ticket_id, attempt_number, "implementation")
        invocation = self.ledger.invocation_for_stage(ticket_id, attempt_number, "implementation")
        attempt = self.ledger.connection.execute(
            "SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)
        ).fetchone()
        if implementation is None or attempt is None:
            raise RuntimeError("validation_reconciliation_required: implementation provenance is incomplete")
        hermes_execution = None
        manual_adoption = None
        implementation_adapter = str(implementation["adapter"])
        if implementation_adapter == "hermes-dispatch":
            hermes_execution = self.ledger.connection.execute(
                "SELECT * FROM hermes_execution_reconciliations WHERE ticket_id=? AND attempt_number=?",
                (ticket_id, attempt_number),
            ).fetchone()
            if hermes_execution is None or invocation is not None:
                raise RuntimeError("validation_reconciliation_required: Hermes execution provenance is incomplete")
        elif implementation_adapter == "manual-adoption":
            manual_adoption = self.ledger.runtime_stage(ticket_id, f"manual-adoption-{attempt_number}")
            if manual_adoption is None or invocation is not None:
                raise RuntimeError("validation_reconciliation_required: manual adoption provenance is incomplete")
        elif invocation is None or invocation["status"] != "completed":
            raise RuntimeError("validation_reconciliation_required: implementation provenance is incomplete")
        expected = {
            "ticket_id": ticket_id,
            "attempt_number": attempt_number,
            "implementation_artifact": str(implementation["response_artifact"]),
            "implementation_artifact_sha256": hashlib.sha256(Path(str(implementation["response_artifact"])).read_bytes()).hexdigest(),
            "worktree_path": str(implementation["worktree_path"]),
            "base_sha": str(implementation["base_sha"]),
            "implementation_diff_hash": str(implementation["diff_hash"]),
            "validation_policy_hash": self.ledger._validation_policy_hash(ticket_row),
        }
        if identity != expected:
            raise RuntimeError("validation_reconciliation_required: durable candidate identity drift")
        if manual_adoption is not None:
            artifact_sha = hashlib.sha256(Path(expected["implementation_artifact"]).read_bytes()).hexdigest()
            try:
                adoption_detail = json.loads(str(manual_adoption["detail"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("validation_reconciliation_required: manual adoption detail is malformed") from exc
            if (
                str(manual_adoption["artifact_path"] or "") != expected["implementation_artifact"]
                or str(manual_adoption["artifact_sha256"] or "") != artifact_sha
                or str(manual_adoption["base_sha"] or "") != expected["base_sha"]
                or str(implementation["request_hash"]) != artifact_sha
                or adoption_detail.get("ticket_id") != ticket_id
                or int(adoption_detail.get("attempt_number", 0)) != attempt_number
                or adoption_detail.get("workspace_path") != expected["worktree_path"]
                or adoption_detail.get("base_sha") != expected["base_sha"]
                or adoption_detail.get("diff_hash") != expected["implementation_diff_hash"]
            ):
                raise RuntimeError("validation_reconciliation_required: manual adoption identity drift")
        elif hermes_execution is None:
            if str(invocation["model_artifact"] or "") != expected["implementation_artifact"]:
                raise RuntimeError("validation_reconciliation_required: durable candidate identity drift")
        elif (
            str(hermes_execution["artifact_path"]) != expected["implementation_artifact"]
            or str(hermes_execution["base_sha"]) != expected["base_sha"]
            or str(hermes_execution["diff_hash"]) != expected["implementation_diff_hash"]
            or str(implementation["request_hash"]) != str(hermes_execution["snapshot_hash"])
        ):
            raise RuntimeError("validation_reconciliation_required: Hermes execution identity drift")
        path = Path(expected["worktree_path"]).resolve()
        artifact = Path(expected["implementation_artifact"])
        if not path.is_dir() or not artifact.is_file() or str(attempt["worktree_path"] or "") != expected["worktree_path"]:
            raise RuntimeError("validation_reconciliation_required: implementation evidence is incomplete")
        worktrees = GitWorktreeAdapter(repo, worktree_root)
        validation_ticket = ticket_from_ledger(ticket_row)
        manual_new_paths = tuple(sorted(set(validation_ticket.create_files) | set(validation_ticket.new_test_files)))
        try:
            live_root = subprocess.run(("git", "rev-parse", "--show-toplevel"), cwd=path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            live_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            if manual_adoption is not None:
                live_diff_hash = str(_isolated_candidate_diff(path, expected["base_sha"], allowed_new_paths=manual_new_paths)["diff_hash"])
            elif hermes_execution is None:
                live_diff_hash = worktrees.diff_hash(path)
            else:
                live_diff = subprocess.run(("git", "diff", "--binary", "--no-ext-diff", expected["base_sha"], "--"), cwd=path, text=True, capture_output=True, check=True, timeout=15).stdout
                live_diff_hash = hashlib.sha256(live_diff.encode()).hexdigest()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("validation_reconciliation_required: live worktree inspection failed") from exc
        expected_head = expected["base_sha"] if hermes_execution is None else str(hermes_execution["head_sha"])
        if live_root != str(path) or live_head != expected_head or live_diff_hash != expected["implementation_diff_hash"]:
            raise RuntimeError("validation_reconciliation_required: live candidate identity drift")
        existing = self.ledger.runtime_stage(ticket_id, f"validation-{attempt_number}")
        if existing is not None:
            try:
                record = json.loads(str(existing["detail"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("validation_reconciliation_required: persisted validation detail is malformed") from exc
            existing_artifact = Path(str(existing["artifact_path"] or ""))
            if (
                record.get("candidate_identity") != identity
                or not existing_artifact.is_file()
                or hashlib.sha256(existing_artifact.read_bytes()).hexdigest() != str(existing["artifact_sha256"] or "")
            ):
                raise RuntimeError("validation_reconciliation_required: persisted validation evidence is incomplete")
        else:
            validation = DeterministicValidator(artifact_root=artifact_root / ticket_id / str(attempt_number)).validate(
                path,
                ticket_from_ledger(ticket_row),
                base_sha=expected["base_sha"],
                expected_head_sha=(str(hermes_execution["head_sha"]) if hermes_execution is not None else None),
            )
            validation_path = Path(validation.full_evidence_path).resolve()
            # Commands are an external effect.  Rebuild the complete candidate
            # identity after they return before accepting their evidence.
            post_ticket = self.ledger.get_ticket(ticket_id)
            post_identity = {
                **expected,
                "validation_policy_hash": self.ledger._validation_policy_hash(post_ticket),
            }
            try:
                post_root = subprocess.run(("git", "rev-parse", "--show-toplevel"), cwd=path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
                post_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
                if manual_adoption is not None:
                    post_diff_hash = str(_isolated_candidate_diff(path, expected["base_sha"], allowed_new_paths=manual_new_paths)["diff_hash"])
                elif hermes_execution is None:
                    post_diff_hash = worktrees.diff_hash(path)
                else:
                    post_diff = subprocess.run(("git", "diff", "--binary", "--no-ext-diff", expected["base_sha"], "--"), cwd=path, text=True, capture_output=True, check=True, timeout=15).stdout
                    post_diff_hash = hashlib.sha256(post_diff.encode()).hexdigest()
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("validation_reconciliation_required: post-validation worktree inspection failed") from exc
            if (
                post_identity != identity
                or not artifact.is_file()
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != expected["implementation_artifact_sha256"]
                or post_root != str(path)
                or post_head != expected_head
                or post_diff_hash != expected["implementation_diff_hash"]
            ):
                raise RuntimeError("validation_reconciliation_required: live candidate changed during validation")
            validation_sha256 = hashlib.sha256(validation_path.read_bytes()).hexdigest()
            if manual_adoption is not None:
                review_diff = str(_isolated_candidate_diff(path, expected["base_sha"], allowed_new_paths=manual_new_paths)["diff"])
            else:
                review_diff = subprocess.run(("git", "diff", expected["base_sha"]), cwd=path, text=True, capture_output=True, check=True, timeout=15).stdout
            selected_files = {relative: (path / relative).read_text(encoding="utf-8") for relative in (*validation_ticket.allowed_files, *validation_ticket.create_files, *validation_ticket.new_test_files) if (path / relative).is_file()}
            if hashlib.sha256(review_diff.encode()).hexdigest() != expected["implementation_diff_hash"] or not selected_files:
                raise RuntimeError("validation_reconciliation_required: review packet inputs drift")
            record = {
                "candidate_identity": identity,
                "passed": validation.passed,
                "compact_evidence": validation.compact_evidence,
                "validation_artifact": str(validation_path),
                "validation_artifact_sha256": validation_sha256,
                "review_diff": review_diff,
                "review_selected_files": selected_files,
            }
            if not self.ledger.record_runtime_stage(ticket_id, f"validation-{attempt_number}", json.dumps(record, sort_keys=True), attempt_number=attempt_number, artifact_path=str(validation_path), artifact_sha256=validation_sha256, base_sha=expected["base_sha"]):
                raise RuntimeError("validation_reconciliation_required: validation artifact persistence conflicted")
            if manual_adoption is not None:
                self.ledger.advance_reconciled_runtime_marker(ticket_id, "validation_completed", attempt_number=attempt_number, detail=json.dumps(record, sort_keys=True), artifact_path=str(validation_path), artifact_sha256=validation_sha256, base_sha=expected["base_sha"])
            else:
                self.ledger.record_runtime_stage(ticket_id, "validation_completed", json.dumps(record, sort_keys=True), attempt_number=attempt_number, artifact_path=str(validation_path), artifact_sha256=validation_sha256, base_sha=expected["base_sha"])
        passed = bool(record.get("passed"))
        if passed and self.ledger.get_ticket(ticket_id)["state"] == CanonicalState.VERIFYING.value:
            self.ledger.transition(ticket_id, CanonicalState.LOCAL_REVIEW, payload={"validation": str(record["compact_evidence"]), "attempt_number": attempt_number})
        return {
            "ticket_id": ticket_id,
            "candidate_identity": identity,
            "validation_artifact": str(record["validation_artifact"]),
            "validation_artifact_sha256": str(record["validation_artifact_sha256"]),
            "passed": passed,
            "compact_evidence": str(record["compact_evidence"]),
            "replayed": existing is not None,
        }

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

    def review_execution_identity(self) -> dict[str, object]:
        """Resolve the live Hermes review profile and bind its non-secret routing state."""
        review_home = Path(getattr(self.local_model, "review_hermes_home", ""))
        if not review_home.name:
            provider = str(getattr(self.local_model, "review_provider", getattr(self.local_model, "provider", type(self.local_model).__name__)))
            model = str(getattr(self.local_model, "review_model", getattr(self.local_model, "model", type(self.local_model).__name__)))
            return {"profile": None, "provider": provider, "model": model, "routing_files": {}, "fingerprint": hashlib.sha256(f"{provider}\0{model}".encode()).hexdigest()}
        executable = str(getattr(self.local_model, "executable", "hermes"))
        return review_profile_identity(review_home.name, executable=executable, profile_root=review_home)

    def review_execution_policy_hash(self) -> str:
        """Hash the live review profile identity used for claim-time binding."""
        return compute_review_execution_policy_hash(self.local_model, self.config.review_timeout_seconds)

    def execute_fresh_review_only(self, ticket_id: str, *, repository: Path) -> dict[str, object]:
        """Persist one scheduler-claimed packet-only review, without disposition."""
        claim = self.ledger.connection.execute("SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage LIKE 'review:%' AND status='claimed' ORDER BY created_at DESC LIMIT 1", (ticket_id,)).fetchone()
        if claim is None or not claim["candidate_identity_json"]:
            raise RuntimeError("review_reconciliation_required: identity-bound scheduler claim is missing")
        identity = json.loads(str(claim["candidate_identity_json"]))
        attempt_number = int(identity["attempt_number"])
        candidate = self.ledger.review_candidate(ticket_id, attempt_number)
        stage = self.ledger.runtime_stage(ticket_id, f"validation-{attempt_number}")
        ticket_row = self.ledger.get_ticket(ticket_id)
        if candidate is None or stage is None or ticket_row["state"] != CanonicalState.LOCAL_REVIEW.value:
            raise RuntimeError("review_reconciliation_required: review candidate is incomplete")
        if candidate["runtime_identity_json"] != json.dumps(identity, sort_keys=True, separators=(",", ":")):
            raise RuntimeError("review_reconciliation_required: review candidate identity drift")
        try:
            validation = json.loads(str(stage["detail"]))
            diff = str(validation["review_diff"])
            selected_files = validation["review_selected_files"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("review_reconciliation_required: review packet inputs are malformed") from exc
        artifact = Path(str(stage["artifact_path"] or ""))
        if (not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != str(identity["validation_artifact_sha256"]) or hashlib.sha256(str(validation.get("compact_evidence", "")).encode()).hexdigest() != str(identity["validation_evidence_hash"]) or hashlib.sha256(diff.encode()).hexdigest() != str(identity["implementation_diff_hash"])):
            raise RuntimeError("review_reconciliation_required: validated review inputs drift")
        ticket = ticket_from_ledger(ticket_row)
        expected_review_policy_hash = self.ledger.review_policy_hash(ticket_row, self.review_execution_policy_hash())
        if expected_review_policy_hash != str(identity["review_policy_hash"]):
            raise RuntimeError("review_reconciliation_required: review policy drift")
        if not isinstance(selected_files, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in selected_files.items()):
            raise RuntimeError("review_reconciliation_required: selected review files are malformed")
        packet = ReviewPacketBuilder().build(ticket, diff=diff, selected_files=selected_files, validation_evidence=str(validation["compact_evidence"]))
        def result_payload(review_result: Any, response: Path, *, replayed: bool) -> dict[str, object]:
            return {
                "ticket_id": ticket_id,
                "attempt_number": attempt_number,
                "candidate_identity": identity,
                "review_artifact": str(response),
                "review_verdict": review_result.verdict,
                "criterion_results": list(review_result.criterion_results),
                "findings": [finding.__dict__ for finding in review_result.findings],
                "suggestions": list(review_result.suggestions),
                "review_raw": review_result.raw,
                "replayed": replayed,
            }
        existing = self.ledger.model_stage(ticket_id, attempt_number, "review")
        if existing is not None:
            response = Path(str(existing["response_artifact"]))
            if response.is_file() and str(existing["diff_hash"]) == str(identity["implementation_diff_hash"]):
                try:
                    envelope = json.loads(response.read_text(encoding="utf-8"))
                    existing_review = normalize_review(envelope["payload"], ticket)
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError("review_reconciliation_required: persisted review artifact is invalid") from exc
                return result_payload(existing_review, response, replayed=True)
            raise RuntimeError("review_reconciliation_required: persisted review stage conflicts")
        retry_authorization = None
        prior_invocations = self.ledger.review_invocations(ticket_id, attempt_number)
        if prior_invocations:
            prior = prior_invocations[-1]
            status = str(prior["status"])
            if status == "completed":
                artifact_value = prior.get("model_artifact")
                if not artifact_value:
                    raise RuntimeError("review_reconciliation_required: completed review invocation is missing its artifact")
                response = Path(str(artifact_value))
                if not response.is_file():
                    raise RuntimeError("review_reconciliation_required: completed review artifact is missing")
                try:
                    envelope = json.loads(response.read_text(encoding="utf-8"))
                    if not isinstance(envelope, dict) or set(envelope) != {"provider", "model", "payload"}:
                        raise ValueError("unexpected review artifact envelope")
                    live_review_identity = self.review_execution_identity()
                    provider = str(live_review_identity["provider"])
                    model = str(live_review_identity["model"])
                    if envelope["provider"] != provider or envelope["model"] != model or prior["provider"] != provider or prior["model"] != model:
                        raise ValueError("review execution identity drift")
                    recovered_review = normalize_review(envelope["payload"], ticket)
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("review_reconciliation_required: completed review artifact is invalid") from exc
                recorded = self.ledger.record_model_stage(
                    ticket_id,
                    attempt_number,
                    "review",
                    purpose="review",
                    adapter=type(self.local_model).__name__,
                    request_hash=str(prior["packet_hash"]),
                    response_artifact=str(response),
                    worktree_path="",
                    base_sha=str(validation["candidate_identity"]["base_sha"]),
                    diff_hash=str(identity["implementation_diff_hash"]),
                )
                if not recorded and self.ledger.model_stage(ticket_id, attempt_number, "review") is None:
                    raise RuntimeError("review_reconciliation_required: completed review invocation could not be recovered")
                return result_payload(recovered_review, response, replayed=True)
            retry_authorization = self.ledger.connection.execute(
                "SELECT * FROM review_retry_authorizations WHERE ticket_id=? AND attempt_number=? AND failed_invocation_id=? AND candidate_fingerprint=? AND consumed_invocation_id IS NULL ORDER BY authorized_at DESC LIMIT 1",
                (ticket_id, attempt_number, str(prior["invocation_id"]), str(identity["implementation_diff_hash"])),
            ).fetchone()
            if retry_authorization is None:
                raise RuntimeError("review_reconciliation_required: prior review invocation requires reconciliation")
        artifacts_root = self.config.validate_execution_roots()[2] / ticket_id / str(attempt_number); artifacts_root.mkdir(parents=True, exist_ok=True)
        invocation_id = uuid.uuid4().hex
        live_review_identity = self.review_execution_identity()
        provider = str(live_review_identity["provider"])
        model = str(live_review_identity["model"])
        if hasattr(self.local_model, "review_provider"):
            self.local_model.review_provider = provider
        if hasattr(self.local_model, "review_model"):
            self.local_model.review_model = model
        packet_hash = hashlib.sha256(packet.encode()).hexdigest()
        if retry_authorization is None:
            self.ledger.start_model_invocation(invocation_id=invocation_id, ticket_id=ticket_id, attempt_number=attempt_number, stage="review", provider=provider, model=model, packet_hash=packet_hash, worktree_path="packet-only", timeout_seconds=self.config.review_timeout_seconds)
        else:
            self.ledger.launch_authorized_review(str(retry_authorization["authorization_id"]), invocation_id=invocation_id, provider=provider, model=model, packet_hash=packet_hash, worktree_path="packet-only", timeout_seconds=self.config.review_timeout_seconds)
        self._crash("review_invocation_started")
        started = time.monotonic()
        try:
            review = LocalReviewAdapter(self.local_model).review(ticket, packet, artifact_dir=artifacts_root)
        except subprocess.TimeoutExpired as exc:
            self.ledger.finish_model_invocation(invocation_id, status="timeout", duration_seconds=time.monotonic()-started, error={"type":"TimeoutExpired","timeout_seconds":self.config.review_timeout_seconds,"process":str(exc)[:1000]}); self.ledger.record_review_infrastructure_failure(ticket_id,attempt_number,outcome="review_timeout"); raise
        except ValueError as exc:
            self.ledger.finish_model_invocation(invocation_id, status="malformed_output", duration_seconds=time.monotonic()-started, error={"type":type(exc).__name__,"message":str(exc)[:1000]}); self.ledger.record_review_infrastructure_failure(ticket_id,attempt_number,outcome="review_malformed_output"); raise
        except Exception as exc:
            self.ledger.finish_model_invocation(invocation_id, status="process_error", duration_seconds=time.monotonic()-started, error={"type":type(exc).__name__,"message":str(exc)[:1000]}); self.ledger.record_review_infrastructure_failure(ticket_id,attempt_number,outcome="review_process_error"); raise
        review_path = artifacts_root / "review-result.json"
        if not review_path.is_file():
            raise RuntimeError("review_reconciliation_required: review adapter did not persist its artifact")
        self.ledger.finish_model_invocation(invocation_id,status="completed",duration_seconds=time.monotonic()-started,model_artifact=str(review_path))
        self._crash("review_invocation_completed")
        self.ledger.record_model_stage(ticket_id,attempt_number,"review",purpose="review",adapter=type(self.local_model).__name__,request_hash=packet_hash,response_artifact=str(review_path),worktree_path="",base_sha=str(validation["candidate_identity"]["base_sha"]),diff_hash=str(identity["implementation_diff_hash"]))
        self._crash("review_completed")
        return result_payload(review, review_path, replayed=False)

    def execute_triage_only(self, ticket_id: str, *, planner: LocalTriagePlanner) -> dict[str, object]:
        """Run/recover one planning-only triage proposal and apply its bounded action."""
        claim_row = self.ledger.connection.execute(
            "SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage LIKE 'triage:%' AND status='claimed' ORDER BY created_at DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if claim_row is None or not claim_row["candidate_identity_json"]:
            raise RuntimeError("triage_reconciliation_required: identity-bound scheduler claim is missing")
        try:
            identity = json.loads(str(claim_row["candidate_identity_json"]))
            attempt_number = int(identity["attempt_number"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("triage_reconciliation_required: claim identity is malformed") from exc
        if identity.get("triage_execution_policy_hash") != planner.execution_policy_hash():
            raise RuntimeError("triage_reconciliation_required: triage execution policy drift")

        parent_row = self.ledger.get_ticket(ticket_id)
        parent = ticket_from_ledger(parent_row)
        routing = self.ledger.runtime_stage(ticket_id, f"repair-routing-{attempt_number}")
        if routing is None:
            raise RuntimeError("triage_reconciliation_required: repair-routing evidence is missing")
        try:
            routing_detail = json.loads(str(routing["detail"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError("triage_reconciliation_required: repair-routing evidence is malformed") from exc
        failure_evidence = str(routing_detail.get("failure_evidence") or "")
        if routing_detail.get("action") != "triage" or hashlib.sha256(failure_evidence.encode()).hexdigest() != identity.get("failure_evidence_hash"):
            raise RuntimeError("triage_reconciliation_required: repair-routing evidence drift")
        unresolved = set(self.ledger.unresolved_criteria(ticket_id))
        if sorted(unresolved) != identity.get("unresolved_criteria") or int(parent_row["depth"]) != int(identity["parent_depth"]):
            raise RuntimeError("triage_reconciliation_required: parent triage identity drift")

        applied_stage = self.ledger.runtime_stage(ticket_id, f"triage-applied-{attempt_number}")
        if applied_stage is not None:
            try:
                applied = json.loads(str(applied_stage["detail"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("triage_reconciliation_required: applied triage record is malformed") from exc
            if applied.get("candidate_identity") != identity:
                raise RuntimeError("triage_reconciliation_required: applied triage identity drift")
            return applied

        def proposal_from_artifact(path: Path):
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("triage_reconciliation_required: triage artifact is invalid") from exc
            if not isinstance(envelope, dict) or set(envelope) != {"provider", "model", "payload"}:
                raise RuntimeError("triage_reconciliation_required: triage artifact envelope is invalid")
            if envelope["provider"] != planner.provider or envelope["model"] != planner.model:
                raise RuntimeError("triage_reconciliation_required: triage artifact execution identity drift")
            try:
                proposal = normalize_triage(envelope["payload"], parent, parent_depth=int(parent_row["depth"]), unresolved_criteria=unresolved)
            except TriageError as exc:
                raise RuntimeError("triage_reconciliation_required: persisted triage proposal is invalid") from exc
            return proposal, envelope["payload"]

        stage = self.ledger.model_stage(ticket_id, attempt_number, "triage")
        proposal = None
        raw_payload: dict[str, object] | None = None
        artifact_path: Path | None = None
        if stage is not None:
            artifact_path = Path(str(stage["response_artifact"]))
            proposal, raw_payload = proposal_from_artifact(artifact_path)
        else:
            invocations = self.ledger.connection.execute(
                "SELECT * FROM model_invocations WHERE ticket_id=? AND attempt_number=? AND stage='triage' ORDER BY started_at,invocation_id",
                (ticket_id, attempt_number),
            ).fetchall()
            if invocations:
                prior = dict(invocations[-1])
                if prior["status"] == "completed":
                    artifact_value = prior.get("model_artifact")
                    if not artifact_value:
                        raise RuntimeError("triage_reconciliation_required: completed triage invocation is missing its artifact")
                    artifact_path = Path(str(artifact_value))
                    proposal, raw_payload = proposal_from_artifact(artifact_path)
                    recorded = self.ledger.record_model_stage(
                        ticket_id,
                        attempt_number,
                        "triage",
                        purpose="triage",
                        adapter=type(planner).__name__,
                        request_hash=str(prior["packet_hash"]),
                        response_artifact=str(artifact_path),
                        worktree_path="packet-only",
                        base_sha=str(self.ledger.runtime_binding(ticket_id)["starting_sha"]),
                        diff_hash=hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    )
                    if not recorded and self.ledger.model_stage(ticket_id, attempt_number, "triage") is None:
                        raise RuntimeError("triage_reconciliation_required: completed triage invocation could not be recovered")
                else:
                    raise RuntimeError("triage_reconciliation_required: prior triage invocation requires reconciliation")

        if proposal is None or raw_payload is None or artifact_path is None:
            artifact_root = self.config.validate_execution_roots()[2] / ticket_id / str(attempt_number)
            packet = planner.packet(parent, parent_depth=int(parent_row["depth"]), unresolved_criteria=unresolved, failure_evidence=failure_evidence)
            packet_hash = hashlib.sha256(packet.encode()).hexdigest()
            invocation_id = uuid.uuid4().hex
            self.ledger.start_model_invocation(
                invocation_id=invocation_id,
                ticket_id=ticket_id,
                attempt_number=attempt_number,
                stage="triage",
                provider=planner.provider,
                model=planner.model,
                packet_hash=packet_hash,
                worktree_path="packet-only",
                timeout_seconds=planner.timeout_seconds,
            )
            self._crash("triage_invocation_started")
            started = time.monotonic()
            try:
                planned = planner.propose(
                    parent,
                    parent_depth=int(parent_row["depth"]),
                    unresolved_criteria=unresolved,
                    failure_evidence=failure_evidence,
                    artifact_dir=artifact_root,
                    packet=packet,
                )
            except subprocess.TimeoutExpired as exc:
                self.ledger.finish_model_invocation(invocation_id, status="timeout", duration_seconds=time.monotonic() - started, error={"type":"TimeoutExpired","timeout_seconds":planner.timeout_seconds,"process":str(exc)[:1000]})
                raise
            except TriageError as exc:
                self.ledger.finish_model_invocation(invocation_id, status="malformed_output", duration_seconds=time.monotonic() - started, error={"type":type(exc).__name__,"message":str(exc)[:1000]})
                raise
            except Exception as exc:
                self.ledger.finish_model_invocation(invocation_id, status="process_error", duration_seconds=time.monotonic() - started, error={"type":type(exc).__name__,"message":str(exc)[:1000]})
                raise
            proposal, raw_payload, artifact_path = planned.proposal, planned.raw_payload, planned.artifact_path
            self.ledger.finish_model_invocation(invocation_id, status="completed", duration_seconds=time.monotonic() - started, model_artifact=str(artifact_path))
            self._crash("triage_invocation_completed")
            self.ledger.record_model_stage(
                ticket_id,
                attempt_number,
                "triage",
                purpose="triage",
                adapter=type(planner).__name__,
                request_hash=packet_hash,
                response_artifact=str(artifact_path),
                worktree_path="packet-only",
                base_sha=str(self.ledger.runtime_binding(ticket_id)["starting_sha"]),
                diff_hash=hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            )
            self._crash("triage_model_stage_completed")

        assert proposal is not None and raw_payload is not None and artifact_path is not None
        child_ids: list[str] = []
        target_state: str | None = None
        if proposal.recommended_action == "decompose":
            child_ids = TriageCoordinator(self.ledger).apply(ticket_id, proposal, project_children=True)
        elif proposal.recommended_action == "block":
            if self.ledger.get_ticket(ticket_id)["state"] == CanonicalState.NEEDS_TRIAGE.value:
                self.ledger.transition(ticket_id, CanonicalState.BLOCKED, actor_id="triage", payload={"classification":proposal.classification,"root_cause_evidence":proposal.root_cause_evidence})
            elif self.ledger.get_ticket(ticket_id)["state"] != CanonicalState.BLOCKED.value:
                raise RuntimeError("triage_reconciliation_required: block outcome state conflicts")
            target_state = CanonicalState.BLOCKED.value
        elif proposal.recommended_action == "checkpoint":
            if self.ledger.get_ticket(ticket_id)["state"] == CanonicalState.NEEDS_TRIAGE.value:
                self.ledger.transition(ticket_id, CanonicalState.NEEDS_CHECKPOINT, actor_id="triage", payload={"classification":proposal.classification,"root_cause_evidence":proposal.root_cause_evidence})
            elif self.ledger.get_ticket(ticket_id)["state"] != CanonicalState.NEEDS_CHECKPOINT.value:
                raise RuntimeError("triage_reconciliation_required: checkpoint outcome state conflicts")
            target_state = CanonicalState.NEEDS_CHECKPOINT.value

        self._crash("triage_action_applied")
        result: dict[str, object] = {
            "ticket_id": ticket_id,
            "attempt_number": attempt_number,
            "candidate_identity": identity,
            "classification": proposal.classification,
            "root_cause_evidence": proposal.root_cause_evidence,
            "recommended_action": proposal.recommended_action,
            "child_ids": child_ids,
            "target_state": target_state,
            "triage_artifact": str(artifact_path),
            "proposal_hash": hashlib.sha256(json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
        self.ledger.record_runtime_stage(ticket_id, f"triage-applied-{attempt_number}", json.dumps(result, sort_keys=True, separators=(",", ":")), attempt_number=attempt_number)
        return result

    def inspect_acceptance_candidate_only(self, ticket_id: str, *, repository: Path) -> dict[str, object]:
        """Read the exact accepted-candidate worktree identity without mutating Git."""
        claim = self.ledger.connection.execute(
            "SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage LIKE 'acceptance:%' AND status='claimed' ORDER BY created_at DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if claim is None or not claim["candidate_identity_json"]:
            raise RuntimeError("acceptance_reconciliation_required: scheduler claim missing")
        try:
            identity = json.loads(str(claim["candidate_identity_json"])); attempt_number = int(identity["attempt_number"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("acceptance_reconciliation_required: claim identity malformed") from exc
        binding = self.ledger.runtime_binding(ticket_id)
        repo = Path(repository).resolve(strict=True)
        if str(repo) != str(binding["repository_path"]):
            raise RuntimeError("acceptance_reconciliation_required: repository binding drift")
        configured_repo, worktree_root, _ = self.config.validate_execution_roots()
        if configured_repo != repo:
            raise RuntimeError("acceptance_reconciliation_required: configured repository drift")
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        if attempt is None or not attempt["worktree_path"] or not attempt["base_sha"]:
            raise RuntimeError("acceptance_reconciliation_required: attempt provenance missing")
        path = Path(str(attempt["worktree_path"]))
        if not path.is_dir():
            raise RuntimeError("acceptance_reconciliation_required: worktree missing")
        adapter = GitWorktreeAdapter(repo, worktree_root)
        try:
            live_root = subprocess.run(("git", "rev-parse", "--show-toplevel"), cwd=path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            live_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=path, text=True, capture_output=True, check=True, timeout=15).stdout.strip()
            diff_hash = adapter.diff_hash(path)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("acceptance_reconciliation_required: live worktree inspection failed") from exc
        if live_root != str(path.resolve()) or live_head != str(attempt["base_sha"]):
            raise RuntimeError("acceptance_reconciliation_required: worktree repository/base drift")
        return {"ticket_id": ticket_id, "attempt_number": attempt_number, "current_diff_hash": diff_hash}

    def execute_git_integration_only(self, ticket_id: str, *, repository: Path) -> dict[str, object]:
        """Create/recover exactly one accepted commit and advance only the controller integration ref."""
        claim = self.ledger.connection.execute(
            "SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage LIKE 'git_integration:%' AND status='claimed' ORDER BY created_at DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if claim is None or not claim["candidate_identity_json"]:
            raise RuntimeError("git_integration_reconciliation_required: scheduler claim missing")
        try:
            identity = json.loads(str(claim["candidate_identity_json"]))
            attempt_number = int(identity["attempt_number"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("git_integration_reconciliation_required: claim identity malformed") from exc
        ticket_row = self.ledger.get_ticket(ticket_id)
        accepted = self.ledger.accepted_candidate(ticket_id)
        if ticket_row["state"] != CanonicalState.ACCEPTED.value or accepted is None:
            raise RuntimeError("git_integration_reconciliation_required: accepted candidate authority missing")
        expected = {
            "ticket_id": ticket_id,
            "attempt_number": int(accepted["attempt_number"]),
            "accepted_evidence_hash": str(accepted["evidence_hash"]),
            "candidate_fingerprint": str(accepted["candidate_fingerprint"]),
            "base_sha": str(accepted["base_sha"]),
            "worktree_path": str(accepted["worktree_path"]),
        }
        if any(identity.get(key) != value for key, value in expected.items()):
            raise RuntimeError("git_integration_reconciliation_required: accepted candidate identity drift")
        attempt = self.ledger.connection.execute(
            "SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)
        ).fetchone()
        latest = self.ledger.connection.execute(
            "SELECT MAX(attempt_number) AS latest FROM attempts WHERE ticket_id=?", (ticket_id,)
        ).fetchone()["latest"]
        if attempt is None or latest is None or int(latest) != attempt_number or str(attempt["branch"] or "") != str(identity.get("branch") or ""):
            raise RuntimeError("git_integration_reconciliation_required: attempt identity drift")
        binding = self.ledger.runtime_binding(ticket_id)
        repo = Path(repository).resolve(strict=True)
        configured_repo, worktree_root, _ = self.config.validate_execution_roots()
        if configured_repo != repo or str(binding["repository_path"]) != str(repo):
            raise RuntimeError("git_integration_reconciliation_required: repository binding drift")
        worktree = Path(str(identity["worktree_path"])).resolve(strict=True)
        hermes_execution = self.ledger.connection.execute(
            "SELECT * FROM hermes_execution_reconciliations WHERE ticket_id=? AND attempt_number=?",
            (ticket_id, attempt_number),
        ).fetchone()
        try:
            worktree.relative_to(worktree_root.resolve())
        except ValueError as exc:
            if (
                hermes_execution is None
                or str(hermes_execution["workspace_path"]) != str(worktree)
                or str(hermes_execution["base_sha"]) != str(identity["base_sha"])
                or str(hermes_execution["diff_hash"]) != str(identity["candidate_fingerprint"])
            ):
                raise RuntimeError("git_integration_reconciliation_required: worktree outside configured root") from exc
        base = str(identity["base_sha"])
        candidate_fingerprint = str(identity["candidate_fingerprint"])
        commit_message = str(identity["commit_message"])
        tranche_id = identity.get("tranche_id")
        if tranche_id != (None if ticket_row["tranche_id"] is None else str(ticket_row["tranche_id"])):
            raise RuntimeError("git_integration_reconciliation_required: tranche identity drift")
        adapter = GitWorktreeAdapter(repo, worktree_root)
        ticket = ticket_from_ledger(ticket_row)
        authorized_files = set(ticket.allowed_files) | set(ticket.create_files) | set(ticket.new_test_files)
        if not authorized_files:
            raise RuntimeError("git_integration_reconciliation_required: accepted candidate has no authorized files")

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            try:
                return subprocess.run(("git", *args), cwd=worktree, text=True, capture_output=True, check=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("git_integration_reconciliation_required: git inspection failed") from exc

        live_root = git("rev-parse", "--show-toplevel").stdout.strip()
        live_branch = git("branch", "--show-current").stdout.strip()
        live_head = git("rev-parse", "HEAD").stdout.strip()
        if live_root != str(worktree) or live_branch != str(identity["branch"]):
            raise RuntimeError("git_integration_reconciliation_required: worktree root/branch drift")
        ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor", str(binding["starting_sha"]), base),
            cwd=repo, text=True, capture_output=True, check=False, timeout=30,
        )
        if ancestor.returncode != 0:
            raise RuntimeError("git_integration_reconciliation_required: accepted base is outside repository provenance")
        intent = self.ledger.git_commit_intent(ticket_id)
        if intent is None and live_head != base:
            raise RuntimeError("git_integration_reconciliation_required: commit exists without durable launch intent")

        def status_paths() -> list[str]:
            lines = git("status", "--porcelain=v1").stdout.splitlines()
            return [line[3:].strip() for line in lines if "__pycache__" not in line]

        if live_head == base:
            untracked = [
                line[3:].strip()
                for line in git("status", "--porcelain=v1").stdout.splitlines()
                if line.startswith("?? ") and "__pycache__" not in line
            ]
            if untracked:
                raise RuntimeError("git_integration_reconciliation_required: accepted fingerprint does not bind untracked content")
            paths = status_paths()
            if not paths or any(path not in authorized_files for path in paths):
                raise RuntimeError("git_integration_reconciliation_required: candidate status contains unauthorized or missing changes")
            if adapter.diff_hash(worktree) != candidate_fingerprint:
                raise RuntimeError("git_integration_reconciliation_required: accepted candidate diff drift")
            current = adapter.existing_execution_base(None if tranche_id is None else str(tranche_id), base)
            if current != base:
                raise RuntimeError("git_integration_reconciliation_required: integration head moved before commit")
            intent = self.ledger.start_git_commit_intent(ticket_id, identity)
            if intent["status"] == "completed":
                raise RuntimeError("git_integration_reconciliation_required: completed intent has uncommitted worktree")
            for cache in sorted(worktree.rglob("__pycache__"), reverse=True):
                if cache.is_dir():
                    shutil.rmtree(cache)
            attempt_worktree = AttemptWorktree(ticket_id, attempt_number, base, str(identity["branch"]), worktree, candidate_fingerprint)
            commit_sha = adapter.accept(attempt_worktree, commit_message)
            self._crash("git_commit_created")
        else:
            if intent is None:
                raise RuntimeError("git_integration_reconciliation_required: missing git commit intent")
            parent = git("rev-parse", "HEAD^").stdout.strip()
            committed_diff = git("diff", "--binary", "--no-ext-diff", base, "HEAD").stdout
            names = git("diff", "--name-only", base, "HEAD").stdout.splitlines()
            clean = git("status", "--porcelain=v1").stdout.strip() == ""
            actual_message = git("log", "-1", "--format=%B").stdout.strip()
            if parent != base or hashlib.sha256(committed_diff.encode()).hexdigest() != candidate_fingerprint or not names or any(name not in authorized_files for name in names) or not clean or actual_message != commit_message:
                raise RuntimeError("git_integration_reconciliation_required: post-commit state is ambiguous")
            commit_sha = live_head

        final_parent = git("rev-parse", f"{commit_sha}^").stdout.strip()
        final_diff = git("diff", "--binary", "--no-ext-diff", base, commit_sha).stdout
        final_names = git("diff", "--name-only", base, commit_sha).stdout.splitlines()
        final_message = git("log", "-1", "--format=%B", commit_sha).stdout.strip()
        if final_parent != base or hashlib.sha256(final_diff.encode()).hexdigest() != candidate_fingerprint or not final_names or any(name not in authorized_files for name in final_names) or final_message != commit_message or git("status", "--porcelain=v1").stdout.strip():
            raise RuntimeError("git_integration_reconciliation_required: committed candidate identity drift")

        if tranche_id is not None:
            current = adapter.existing_execution_base(str(tranche_id), base)
            if current == base:
                adapter.advance_integration_head(str(tranche_id), base, commit_sha)
                self._crash("git_integration_head_advanced")
            elif current != commit_sha:
                raise RuntimeError("git_integration_reconciliation_required: integration head conflicts with accepted commit")
            after = adapter.existing_execution_base(str(tranche_id), base)
            if after != commit_sha:
                raise RuntimeError("git_integration_reconciliation_required: integration head did not reach accepted commit")
        else:
            after = commit_sha
        return {
            "ticket_id": ticket_id,
            "attempt_number": attempt_number,
            "candidate_identity": identity,
            "commit_sha": commit_sha,
            "integration_head_before": base,
            "integration_head_after": after,
        }

    def execute_tranche_checkpoint_only(self, tranche_id: str, *, repository: Path) -> dict[str, object]:
        """Deterministically revalidate and checkpoint one completed tranche without activation or paid calls."""
        claim = self.ledger.connection.execute(
            "SELECT * FROM scheduler_stage_claims WHERE stage='tranche_checkpoint' AND status='claimed' AND json_extract(candidate_identity_json,'$.tranche_id')=? ORDER BY created_at DESC LIMIT 1",
            (tranche_id,),
        ).fetchone()
        if claim is None or not claim["candidate_identity_json"]:
            raise RuntimeError("tranche_checkpoint_reconciliation_required: scheduler claim missing")
        try:
            identity = json.loads(str(claim["candidate_identity_json"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError("tranche_checkpoint_reconciliation_required: claim identity malformed") from exc
        if identity.get("tranche_id") != tranche_id:
            raise RuntimeError("tranche_checkpoint_reconciliation_required: tranche identity drift")
        repo = Path(repository).resolve(strict=True)
        configured_repo, _, artifact_root = self.config.validate_execution_roots()
        if configured_repo != repo or str(identity["repository_identity"]) != str(repo):
            raise RuntimeError("tranche_checkpoint_reconciliation_required: repository identity drift")
        manifest_json = str(identity["planning_snapshot_manifest_json"])
        if hashlib.sha256(manifest_json.encode()).hexdigest() != str(identity["planning_snapshot_hash"]):
            raise RuntimeError("tranche_checkpoint_reconciliation_required: planning snapshot hash drift")
        try:
            manifest = json.loads(manifest_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("tranche_checkpoint_reconciliation_required: planning snapshot malformed") from exc
        planning_base = subprocess.run(("git", "rev-parse", "--verify", f"{identity['planning_base_sha']}^{{commit}}"), cwd=repo, text=True, capture_output=True, check=True, timeout=30).stdout.strip()
        if planning_base != identity["planning_base_sha"]:
            raise RuntimeError("tranche_checkpoint_reconciliation_required: planning base drift")
        for entry in manifest.get("evidence", []):
            path = str(entry.get("path") or "")
            content_hash = str(entry.get("content_hash") or "")
            if not path or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
                raise RuntimeError("tranche_checkpoint_reconciliation_required: planning evidence malformed")
            blob = subprocess.run(("git", "show", f"{planning_base}:{path}"), cwd=repo, capture_output=True, check=True, timeout=30).stdout
            if hashlib.sha256(blob).hexdigest() != content_hash:
                raise RuntimeError("tranche_checkpoint_reconciliation_required: planning snapshot content drift")

        from .tranche_completion import completion_evidence
        completion = completion_evidence(self.ledger, repo, tranche_id)
        if completion["accepted_ticket_ids"] != identity["ticket_ids"] or completion["accepted_commit_shas"] != identity["accepted_commit_shas"]:
            raise RuntimeError("tranche_checkpoint_reconciliation_required: serialized integration identity drift")
        final_ticket = str(identity["ticket_ids"][-1])
        final_commit = str(completion["final_integration_sha"])
        target = artifact_root / "tranches" / tranche_id
        target.mkdir(parents=True, exist_ok=True)
        artifact = target / "checkpoint.json"
        checkpoint_worktree = target / "integration-worktree"

        def cleanup_checkpoint_worktree() -> None:
            if checkpoint_worktree.exists():
                subprocess.run(("git", "worktree", "remove", "--force", str(checkpoint_worktree)), cwd=repo, text=True, capture_output=True, check=False, timeout=30)
            if checkpoint_worktree.exists():
                shutil.rmtree(checkpoint_worktree, ignore_errors=True)
            subprocess.run(("git", "worktree", "prune"), cwd=repo, text=True, capture_output=True, check=False, timeout=30)

        if artifact.exists():
            cleanup_checkpoint_worktree()
            try:
                recovered = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("tranche_checkpoint_reconciliation_required: checkpoint artifact malformed") from exc
            if (
                recovered.get("version") != 1
                or recovered.get("feature_id") != identity["feature_id"]
                or recovered.get("tranche_id") != tranche_id
                or recovered.get("candidate_identity") != identity
                or recovered.get("integration_commands") != identity["integration_commands"]
                or recovered.get("completion") != completion
                or recovered.get("decision") not in {"ready_for_checkpoint", "integration_failed"}
                or not isinstance(recovered.get("integration_results"), list)
            ):
                raise RuntimeError("tranche_checkpoint_reconciliation_required: checkpoint artifact conflicts")
            return {
                "tranche_id": tranche_id,
                "candidate_identity": identity,
                "completion": completion,
                "integration_results": recovered["integration_results"],
                "decision": recovered["decision"],
                "checkpoint_artifact": str(artifact),
                "checkpoint_artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }

        attempt = self.ledger.connection.execute(
            "SELECT * FROM attempts WHERE ticket_id=? AND accepted_commit_sha=? ORDER BY attempt_number DESC LIMIT 1",
            (final_ticket, final_commit),
        ).fetchone()
        if attempt is None or not attempt["worktree_path"]:
            raise RuntimeError("tranche_checkpoint_reconciliation_required: final integration attempt missing")
        attempt_worktree = Path(str(attempt["worktree_path"]))
        ephemeral_worktree = False
        if attempt_worktree.is_dir():
            worktree = attempt_worktree.resolve(strict=True)
            head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=worktree, text=True, capture_output=True, check=True, timeout=30).stdout.strip()
            status = subprocess.run(("git", "status", "--porcelain=v1"), cwd=worktree, text=True, capture_output=True, check=True, timeout=30).stdout.strip()
            if head != final_commit or status:
                raise RuntimeError("tranche_checkpoint_reconciliation_required: final worktree drift")
        else:
            cleanup_checkpoint_worktree()
            created = subprocess.run(("git", "worktree", "add", "--detach", "-q", str(checkpoint_worktree), final_commit), cwd=repo, text=True, capture_output=True, check=False, timeout=30)
            if created.returncode != 0:
                raise RuntimeError("tranche_checkpoint_reconciliation_required: unable to materialize final integration commit")
            worktree = checkpoint_worktree.resolve(strict=True)
            ephemeral_worktree = True

        try:
            integration_results: list[dict[str, object]] = []
            passed = True
            for index, command in enumerate(identity["integration_commands"]):
                try:
                    proc = subprocess.run(tuple(command), cwd=worktree, text=True, capture_output=True, timeout=300)
                    result = {
                        "index": index,
                        "command": command,
                        "returncode": int(proc.returncode),
                        "stdout": proc.stdout[-20000:],
                        "stderr": proc.stderr[-20000:],
                    }
                except (OSError, subprocess.TimeoutExpired) as exc:
                    result = {"index": index, "command": command, "returncode": -1, "stdout": "", "stderr": str(exc)[:20000]}
                integration_results.append(result)
                if int(result["returncode"]) != 0:
                    passed = False
                    break

            checkpoint_packet = {
                "version": 1,
                "feature_id": identity["feature_id"],
                "tranche_id": tranche_id,
                "candidate_identity": identity,
                "integration_commands": identity["integration_commands"],
                "planning_snapshot": {
                    "repository_identity": identity["repository_identity"],
                    "base_sha": identity["planning_base_sha"],
                    "snapshot_hash": identity["planning_snapshot_hash"],
                },
                "completion": completion,
                "integration_results": integration_results,
                "decision": "ready_for_checkpoint" if passed else "integration_failed",
            }
            content = json.dumps(checkpoint_packet, sort_keys=True, indent=2) + "\n"
            artifact.write_text(content, encoding="utf-8")
            return {
                "tranche_id": tranche_id,
                "candidate_identity": identity,
                "completion": completion,
                "integration_results": integration_results,
                "decision": checkpoint_packet["decision"],
                "checkpoint_artifact": str(artifact),
                "checkpoint_artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        finally:
            if ephemeral_worktree:
                cleanup_checkpoint_worktree()

    def execute_paid_stage_only(self, tranche_id: str, *, adapter: PaidModelAdapter, purpose: PaidPurpose) -> dict[str, object]:
        stage = "paid_checkpoint" if purpose == PaidPurpose.INTEGRATION_CHECKPOINT else "paid_escalation"
        claim = self.ledger.connection.execute(
            "SELECT * FROM scheduler_stage_claims WHERE stage=? AND status='claimed' AND json_extract(candidate_identity_json,'$.tranche_id')=? ORDER BY created_at DESC LIMIT 1",
            (stage, tranche_id),
        ).fetchone()
        if claim is None or not claim["candidate_identity_json"]:
            raise RuntimeError("paid_checkpoint_reconciliation_required: scheduler claim missing")
        identity = json.loads(str(claim["candidate_identity_json"]))
        if identity.get("purpose") != purpose.value:
            raise RuntimeError("paid_checkpoint_reconciliation_required: purpose identity drift")
        artifact = Path(str(identity["checkpoint_artifact"]))
        if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != identity["checkpoint_artifact_sha256"]:
            raise RuntimeError("paid_checkpoint_reconciliation_required: checkpoint artifact drift")
        try:
            checkpoint_packet = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("paid_checkpoint_reconciliation_required: checkpoint artifact malformed") from exc
        packet = {
            "version": 1,
            "role": "integration_checkpoint" if purpose == PaidPurpose.INTEGRATION_CHECKPOINT else "escalation",
            "feature_id": identity["feature_id"],
            "tranche_id": tranche_id,
            "checkpoint_artifact_sha256": identity["checkpoint_artifact_sha256"],
            "checkpoint_completion_hash": identity["checkpoint_completion_hash"],
            "final_integration_sha": identity["final_integration_sha"],
            "checkpoint": checkpoint_packet,
            "prior_checkpoint_decision": identity.get("prior_checkpoint_decision"),
            "output_contract": {
                "type": "object",
                "additionalProperties": False,
                "required": ["decision", "rationale"],
                "decision": ["approve", "escalate", "reject"],
                "rationale_max_chars": 4000,
            },
        }
        response = adapter.invoke(str(identity["feature_id"]), purpose, str(claim["claim_id"]), packet)
        if set(response) != {"decision", "rationale"}:
            raise RuntimeError("paid_checkpoint_reconciliation_required: response schema mismatch")
        decision = response.get("decision")
        rationale = response.get("rationale")
        if decision not in {"approve", "escalate", "reject"} or not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000:
            raise RuntimeError("paid_checkpoint_reconciliation_required: invalid paid response")
        return {
            "tranche_id": tranche_id,
            "candidate_identity": identity,
            "decision": decision,
            "rationale": rationale.strip(),
            "response": {"decision": decision, "rationale": rationale.strip()},
        }

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
        selected_files = {relative: (path / relative).read_text(encoding="utf-8") for relative in (*ticket.allowed_files, *ticket.create_files, *ticket.new_test_files) if (path / relative).is_file()}
        packet = ReviewPacketBuilder().build(ticket, diff=diff, selected_files=selected_files, validation_evidence=str(candidate["validation_evidence"]))
        artifacts_root = artifact_root / ticket_id / str(attempt_number); artifacts_root.mkdir(parents=True, exist_ok=True)
        invocation_id = uuid.uuid4().hex
        live_review_identity = self.review_execution_identity()
        provider = str(live_review_identity["provider"])
        model = str(live_review_identity["model"])
        if hasattr(self.local_model, "review_provider"):
            self.local_model.review_provider = provider
        if hasattr(self.local_model, "review_model"):
            self.local_model.review_model = model
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
            implementation = self.ledger.model_stage(ticket_id, attempt_number, "implementation")
            if implementation is not None and str(implementation["adapter"]) == "manual-adoption":
                ticket = ticket_from_ledger(self.ledger.get_ticket(ticket_id))
                allowed_new_paths = tuple(sorted(set(ticket.create_files) | set(ticket.new_test_files)))
                live_diff_hash = str(_isolated_candidate_diff(path, str(stage["base_sha"]), allowed_new_paths=allowed_new_paths)["diff_hash"])
            else:
                diff = subprocess.run(("git", "diff", str(stage["base_sha"])), cwd=path, text=True, capture_output=True, check=True).stdout
                live_diff_hash = hashlib.sha256(diff.encode()).hexdigest()
            if live_diff_hash != str(candidate["candidate_fingerprint"]) or str(stage["diff_hash"]) != str(candidate["candidate_fingerprint"]):
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
        ticket = ticket_from_ledger(ticket_row); authorized = set(ticket.allowed_files) | set(ticket.create_files) | set(ticket.new_test_files); adapter = GitWorktreeAdapter(repo, worktree_root)
        implementation_stage = self.ledger.model_stage(ticket_id, attempt_number, "implementation")
        manual_adoption = implementation_stage is not None and str(implementation_stage["adapter"]) == "manual-adoption"
        commit_parent = subprocess.run(("git", "rev-parse", f"{accepted}^"), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
        names = subprocess.run(("git", "diff", "--name-only", base, accepted), cwd=repo, text=True, capture_output=True, check=True).stdout.splitlines()
        identity_diff = (
            subprocess.run(("git", "diff", "--binary", "--no-ext-diff", base, accepted, "--"), cwd=repo, text=True, capture_output=True, check=True).stdout
            if manual_adoption
            else subprocess.run(("git", "diff", base, accepted, "--", *(*ticket.allowed_files, *ticket.create_files, *ticket.new_test_files)), cwd=repo, text=True, capture_output=True, check=True).stdout
        )
        current = adapter.existing_execution_base(str(ticket_row["tranche_id"]), base)
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
        if attempt is None or impl is None or review_stage is None or impl["status"] != "completed":
            raise PermissionError("accepted candidate implementation provenance is incomplete")
        hermes_execution = self.ledger.connection.execute("SELECT * FROM hermes_execution_reconciliations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        manual_adoption = self.ledger.runtime_stage(ticket_id, f"manual-adoption-{attempt_number}") if str(impl["adapter"]) == "manual-adoption" else None
        implementation_execution_id: str
        if str(impl["adapter"]) == "manual-adoption":
            if invocation is not None or hermes_execution is not None or manual_adoption is None:
                raise PermissionError("accepted manual-adoption provenance is incomplete")
            implementation_artifact = Path(str(impl["response_artifact"] or ""))
            if not implementation_artifact.is_file():
                raise PermissionError("accepted manual-adoption artifact is missing")
            implementation_artifact_sha = hashlib.sha256(implementation_artifact.read_bytes()).hexdigest()
            if implementation_artifact_sha != str(impl["request_hash"] or "") or str(manual_adoption["artifact_path"] or "") != str(implementation_artifact) or str(manual_adoption["artifact_sha256"] or "") != implementation_artifact_sha or str(manual_adoption["base_sha"] or "") != str(impl["base_sha"] or ""):
                raise PermissionError("accepted manual-adoption artifact provenance is invalid")
            try:
                adoption_detail = json.loads(str(manual_adoption["detail"] or ""))
            except json.JSONDecodeError as exc:
                raise PermissionError("accepted manual-adoption detail is malformed") from exc
            if adoption_detail.get("ticket_id") != ticket_id or int(adoption_detail.get("attempt_number", 0)) != attempt_number or adoption_detail.get("diff_hash") != str(impl["diff_hash"]) or adoption_detail.get("artifact_path") != str(implementation_artifact) or adoption_detail.get("artifact_sha256") != implementation_artifact_sha:
                raise PermissionError("accepted manual-adoption identity drift")
            implementation_execution_id = f"manual-adoption:{implementation_artifact_sha}"
        elif hermes_execution is not None:
            if invocation is not None or manual_adoption is not None:
                raise PermissionError("accepted Hermes execution provenance is ambiguous")
            if str(hermes_execution["run_status"]) not in {"done", "completed"} or str(hermes_execution["run_outcome"]) not in {"completed", "success", "succeeded"}:
                raise PermissionError("accepted Hermes execution is not a completed successful run")
            if str(hermes_execution["workspace_path"]) != str(impl["worktree_path"]) or str(hermes_execution["base_sha"]) != str(impl["base_sha"]) or str(hermes_execution["diff_hash"]) != str(impl["diff_hash"]) or str(hermes_execution["artifact_path"]) != str(impl["response_artifact"]):
                raise PermissionError("accepted Hermes execution provenance conflicts with implementation")
            hermes_artifact = Path(str(hermes_execution["artifact_path"] or ""))
            if not hermes_artifact.is_file():
                raise PermissionError("accepted Hermes execution artifact is missing")
            try:
                hermes_payload = json.loads(hermes_artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PermissionError("accepted Hermes execution artifact is malformed") from exc
            if canonical_sha256(hermes_payload) != str(hermes_execution["snapshot_hash"]):
                raise PermissionError("accepted Hermes execution artifact integrity failed")
            implementation_execution_id = f"hermes-run:{hermes_execution['external_task_id']}:{hermes_execution['hermes_run_id']}"
        else:
            if invocation is None or invocation["status"] != "completed":
                raise PermissionError("accepted candidate implementation invocation is incomplete")
            implementation_execution_id = str(invocation["invocation_id"])
        if str(candidate["implementation_invocation_id"] or "") != implementation_execution_id:
            raise PermissionError("accepted candidate implementation execution identity mismatch")
        worktree = Path(str(attempt["worktree_path"])).resolve(); authorized_files = set(ticket.allowed_files) | set(ticket.create_files) | set(ticket.new_test_files)
        if not authorized_files:
            raise PermissionError("acceptance requires authorized candidate files")
        live_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(); live_branch = subprocess.run(("git", "branch", "--show-current"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(); base = str(attempt["base_sha"])
        candidate_fp = str(candidate["candidate_fingerprint"])
        if manual_adoption is not None:
            manual_new_paths = tuple(sorted(set(ticket.create_files) | set(ticket.new_test_files)))
            canonical_live = _isolated_candidate_diff(worktree, base, allowed_new_paths=manual_new_paths)
            live_diff = str(canonical_live["diff"])
            live_candidate_hash = str(canonical_live["diff_hash"])
        else:
            live_diff = subprocess.run(("git", "diff", str(base)), cwd=worktree, text=True, capture_output=True, check=True).stdout
            identity_diff = subprocess.run(("git", "diff", str(base), "--", *(*ticket.allowed_files, *ticket.create_files, *ticket.new_test_files)), cwd=worktree, text=True, capture_output=True, check=True).stdout
            live_candidate_hash = hashlib.sha256(identity_diff.encode()).hexdigest()
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
        created_new_commit = False
        if live_head == base:
            if not status_paths or live_candidate_hash != candidate_fp:
                raise PermissionError("live candidate differs from frozen candidate")
            for cache in sorted(worktree.rglob("__pycache__"), reverse=True):
                if cache.is_dir(): shutil.rmtree(cache)
            worktrees = GitWorktreeAdapter(repo, worktree_root); attempt_worktree = AttemptWorktree(ticket_id, attempt_number, base, str(attempt["branch"]), worktree, live_candidate_hash)
            accepted_sha = worktrees.accept(attempt_worktree, f"local-first: {ticket_row['title']}")
            created_new_commit = True
            self._crash("accepted_commit_created")
        else:
            parent = subprocess.run(("git", "rev-parse", "HEAD^"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(); committed_diff = subprocess.run(("git", "diff", base, "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout; names = subprocess.run(("git", "diff", "--name-only", base, "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.splitlines(); clean = subprocess.run(("git", "status", "--porcelain=v1"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip() == ""
            committed_identity_diff = subprocess.run(("git", "diff", "--binary", "--no-ext-diff", base, "HEAD", "--"), cwd=worktree, text=True, capture_output=True, check=True).stdout if manual_adoption is not None else subprocess.run(("git", "diff", base, "HEAD", "--", *(*ticket.allowed_files, *ticket.create_files, *ticket.new_test_files)), cwd=worktree, text=True, capture_output=True, check=True).stdout
            if parent != base or hashlib.sha256(committed_identity_diff.encode()).hexdigest() != candidate_fp or not names or any(name not in authorized_files for name in names) or not clean:
                raise RuntimeError("post-commit acceptance state is ambiguous")
            accepted_sha = live_head
        final_parent = subprocess.run(("git", "rev-parse", "HEAD^"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip(); final_diff = subprocess.run(("git", "diff", base, "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout; final_names = subprocess.run(("git", "diff", "--name-only", base, "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.splitlines(); final_clean = subprocess.run(("git", "status", "--porcelain=v1"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip() == ""
        final_identity_diff = subprocess.run(("git", "diff", "--binary", "--no-ext-diff", base, "HEAD", "--"), cwd=worktree, text=True, capture_output=True, check=True).stdout if manual_adoption is not None else subprocess.run(("git", "diff", base, "HEAD", "--", *(*ticket.allowed_files, *ticket.create_files, *ticket.new_test_files)), cwd=worktree, text=True, capture_output=True, check=True).stdout
        if accepted_sha != subprocess.run(("git", "rev-parse", "HEAD"), cwd=worktree, text=True, capture_output=True, check=True).stdout.strip() or final_parent != base or hashlib.sha256(final_identity_diff.encode()).hexdigest() != candidate_fp or not final_names or any(name not in authorized_files for name in final_names) or not final_clean:
            raise RuntimeError("accepted commit does not match frozen candidate")
        provenance = json.loads(str(candidate["historical_provenance_json"]))
        if provenance.get("authorization_hash"):
            auth = self.ledger.connection.execute("SELECT * FROM historical_revalidation_authorizations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); attestation = self.ledger.connection.execute("SELECT * FROM historical_revalidation_attestations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone(); result = self.ledger.connection.execute("SELECT * FROM historical_revalidation_validation_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            if auth is None or attestation is None or result is None or authorization_hash_from_row(auth) != auth["authorization_hash"] or attestation_hash_from_row(attestation) != attestation["attestation_hash"]:
                raise PermissionError("historical acceptance provenance is invalid")
            if auth["authorization_hash"] != provenance.get("authorization_hash") or attestation["attestation_hash"] != provenance.get("attestation_hash") or result["result_id"] != provenance.get("validation_result_id") or result["result_hash"] != provenance.get("validation_result_hash") or not bool(result["passed"]):
                raise PermissionError("historical acceptance provenance conflicts with candidate")
            if invocation is None or result["implementation_diff_hash"] != candidate_fp or auth["implementation_invocation_id"] != invocation["invocation_id"]:
                raise PermissionError("historical acceptance implementation binding mismatch")
            if not Path(str(attestation["implementation_artifact"])).is_file():
                raise PermissionError("historical implementation artifact is missing")
            if hashlib.sha256(Path(str(result["artifact_path"])).read_bytes()).hexdigest() != str(result["artifact_sha256"]):
                raise PermissionError("historical validation artifact digest mismatch")
        diff_summary = json.dumps({"candidate_fingerprint": candidate_fp, "base_sha": base, "files": final_names, "implementation_execution_id": implementation_execution_id, "implementation_invocation_id": None if invocation is None else invocation["invocation_id"], "authorization_hash": provenance.get("authorization_hash"), "attestation_hash": provenance.get("attestation_hash"), "validation_result_id": provenance.get("validation_result_id"), "validation_result_hash": provenance.get("validation_result_hash"), "review_result_id": review_row["id"]}, sort_keys=True)
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
                review_packet=ReviewPacketBuilder().build(ticket,diff=diff,selected_files={p:(attempt.path/p).read_text() for p in (*ticket.allowed_files, *ticket.create_files, *ticket.new_test_files) if (attempt.path/p).exists()},validation_evidence=validation.compact_evidence)
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
