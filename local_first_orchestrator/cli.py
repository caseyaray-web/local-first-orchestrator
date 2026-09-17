from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .controller import LocalFirstController, RuntimeConfig, compute_review_execution_policy_hash
from .admission import FeatureAdmissionSpec
from .decomposition_planner import LocalDecompositionPlanner, resolve_hermes_identity
from .daemon import SchedulerDaemon
from .planning_coordinator import PlanningCoordinator
from .corrections import AcceptedPredecessor, CorrectionService, CorrectionTicketSpec, SupplementalCorrectionPlan
from .generated_activation import GeneratedActivationError, activate_generated_ticket
from .generated_projection import GeneratedProjectionWorker
from .hermes_board import HermesBoardAdapter
from .ledger import Ledger
from .local_qwen import LOCAL_QWEN_MODEL, LOCAL_QWEN_PROVIDER, LocalQwenAdapter
from .operator_config import ModelRegistration, OperatorConfig, default_execution_roots, load_operator_config, save_operator_config
from .signer_enrollment import enroll_operator_signer
from .runtime_metrics import RuntimeMetricsStore
from .native_release_approval import parse_approval_document
from .paid_model import HermesPaidModelAdapter
from .scheduler import ProcessNextScheduler, preview_database, preview_ticket_database, scheduler_observability
from .states import CanonicalState
from .ticket import MicroTicket, PatchBudget, VerificationProfile
from .triage import LocalTriagePlanner
from .usage_governor import PaidPurpose, UsageGovernor


def _correction_service(ledger: Ledger, args: argparse.Namespace) -> CorrectionService:
    root=Path(args.repository).resolve(strict=True)
    allowlist=tuple(Path(item).resolve(strict=True) for item in args.allow_repository) or (root,)
    if root not in allowlist: raise PermissionError("repository is not on the explicit --allow-repository list")
    return CorrectionService(ledger, root)


_MISSING = object()


def _json_object(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be an object")
    return value


def _json_field(mapping: dict[str, object], key: str, name: str, kind: type, *, default: object = _MISSING, nullable: bool = False) -> Any:
    value = mapping.get(key, _MISSING)
    if value is _MISSING:
        if default is not _MISSING:
            return default
        raise ValueError(f"{name}.{key} is required")
    if value is None and nullable:
        return None
    if value is None or type(value) is not kind:
        raise ValueError(f"{name}.{key} must be a {kind.__name__}")
    return value


def _json_string_list(value: object, name: str) -> tuple[str, ...]:
    if type(value) is not list or not all(type(item) is str and item for item in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return tuple(value)


def _correction_plan_from_file(path: str, repository_identity: str) -> SupplementalCorrectionPlan:
    """Parse a controller-owned correction plan file (fail-closed).

    The operator supplies structured provenance (source kind/reference,
    finding fingerprint/summary, predecessor ticket+accepted commit pairs,
    and MicroTicket-shaped contracts). IDs are derived by the controller;
    no board contents or model output are consulted here. Unknown keys or
    missing required fields reject the whole plan.
    """
    raw=_json_object(json.loads(Path(path).expanduser().read_text(encoding="utf-8")), "correction plan file")
    top_required={"feature_id","tranche_id","source_kind","source_reference","finding_fingerprint","finding_summary","predecessors","tickets","base_sha","snapshot_hash"}
    unknown=set(raw) - top_required
    if unknown: raise ValueError(f"unknown correction plan fields: {sorted(unknown)}")
    missing=top_required - set(raw)
    if missing: raise ValueError(f"missing correction plan fields: {sorted(missing)}")

    pred_raw=raw["predecessors"]
    if type(pred_raw) is not list or not pred_raw: raise ValueError("correction predecessors must be a non-empty list of objects")
    predecessors=[]
    for entry in pred_raw:
        entry=_json_object(entry, "predecessor")
        keys=set(entry) - {"ticket_id","accepted_commit"}
        if keys: raise ValueError(f"unknown predecessor fields: {sorted(keys)}")
        ticket=_json_field(entry, "ticket_id", "predecessor", str); commit=_json_field(entry, "accepted_commit", "predecessor", str)
        if not ticket or not commit: raise ValueError("predecessor requires non-empty ticket_id and accepted_commit")
        predecessors.append(AcceptedPredecessor(ticket,commit))

    spec_raws=raw["tickets"]
    if type(spec_raws) is not list or not spec_raws: raise ValueError("correction tickets must be a non-empty list")
    specs=[]
    for entry in spec_raws:
        entry=_json_object(entry, "ticket")
        keys=set(entry) - {"objective","criterion_ids","primary_symbol","allowed_existing_files","new_test_files","forbidden_changes","patch_budget","verification","risk","review_required","max_attempts","dependencies","relevant_symbols","acceptance_criteria","non_goals","red_evidence"}
        if keys: raise ValueError(f"unknown ticket fields: {sorted(keys)}")
        patch_raw=_json_object(_json_field(entry, "patch_budget", "ticket", dict), "patch_budget")
        verification_raw=_json_object(_json_field(entry, "verification", "ticket", dict), "verification")
        if set(patch_raw) - {"max_files", "max_changed_lines", "exception_reason"}:
            raise ValueError("patch_budget must be a closed object")
        if set(verification_raw) - {"commands", "working_directory", "timeout_seconds", "output_limit"}:
            raise ValueError("verification must be a closed object")
        raw_commands=_json_field(verification_raw, "commands", "verification", list, default=[])
        if type(raw_commands) is not list or not all(type(command) is list and command and all(type(arg) is str and arg for arg in command) for command in raw_commands):
            raise ValueError("verification.commands must be a list of non-empty string argv lists")
        commands=[tuple(command) for command in raw_commands]
        patch_max_files=_json_field(patch_raw, "max_files", "patch_budget", int, default=2)
        patch_max_lines=_json_field(patch_raw, "max_changed_lines", "patch_budget", int, default=180)
        if type(patch_max_files) is not int or type(patch_max_lines) is not int: raise ValueError("patch budget limits must be integers")
        exception_reason=_json_field(patch_raw, "exception_reason", "patch_budget", str, default=None, nullable=True)
        working_directory=_json_field(verification_raw, "working_directory", "verification", str, default=".")
        timeout_seconds=_json_field(verification_raw, "timeout_seconds", "verification", int, default=60)
        output_limit=_json_field(verification_raw, "output_limit", "verification", int, default=20_000)
        specs.append(CorrectionTicketSpec(
            objective=_json_field(entry, "objective", "ticket", str, default=""),
            criterion_ids=_json_string_list(_json_field(entry, "criterion_ids", "ticket", list, default=[]), "ticket.criterion_ids"),
            primary_symbol=_json_field(entry, "primary_symbol", "ticket", str, default=""),
            allowed_existing_files=_json_string_list(_json_field(entry, "allowed_existing_files", "ticket", list, default=[]), "ticket.allowed_existing_files"),
            new_test_files=_json_string_list(_json_field(entry, "new_test_files", "ticket", list, default=[]), "ticket.new_test_files"),
            forbidden_changes=_json_string_list(_json_field(entry, "forbidden_changes", "ticket", list, default=[]), "ticket.forbidden_changes"),
            patch_budget=PatchBudget(
                max_files=patch_max_files, max_changed_lines=patch_max_lines, exception_reason=exception_reason),
            verification=VerificationProfile(
                tuple(commands) if commands else (("true",),),
                working_directory=working_directory, timeout_seconds=timeout_seconds, output_limit=output_limit),
            risk=_json_field(entry, "risk", "ticket", str, default="low"),
            review_required=_json_field(entry, "review_required", "ticket", bool, default=True),
            max_attempts=_json_field(entry, "max_attempts", "ticket", int, default=1),
            dependencies=_json_string_list(_json_field(entry, "dependencies", "ticket", list, default=[]), "ticket.dependencies"),
            relevant_symbols=_json_string_list(_json_field(entry, "relevant_symbols", "ticket", list, default=[]), "ticket.relevant_symbols"),
            acceptance_criteria=_json_string_list(_json_field(entry, "acceptance_criteria", "ticket", list, default=[]), "ticket.acceptance_criteria"),
            non_goals=_json_string_list(_json_field(entry, "non_goals", "ticket", list, default=[]), "ticket.non_goals"),
            red_evidence=_json_field(entry, "red_evidence", "ticket", str, default="")))

    return SupplementalCorrectionPlan(
        feature_id=_json_field(raw, "feature_id", "correction plan", str), tranche_id=_json_field(raw, "tranche_id", "correction plan", str),
        source_kind=_json_field(raw, "source_kind", "correction plan", str), source_reference=_json_field(raw, "source_reference", "correction plan", str),
        finding_fingerprint=_json_field(raw, "finding_fingerprint", "correction plan", str), finding_summary=_json_field(raw, "finding_summary", "correction plan", str),
        predecessors=tuple(predecessors), tickets=tuple(specs),
        repository_identity=repository_identity, base_sha=_json_field(raw, "base_sha", "correction plan", str), snapshot_hash=_json_field(raw, "snapshot_hash", "correction plan", str))


def _ledger(path: str) -> Ledger:
    ledger=Ledger(Path(path).expanduser()); ledger.migrate(); return ledger


def _ad_hoc_controller(ledger: Ledger, args: argparse.Namespace, *, allow_board_writes: bool=False) -> LocalFirstController:
    """Development-only execution composition; registered runs must not use it."""
    root=Path(args.repository).resolve()
    allowlist=tuple(Path(item).resolve() for item in args.allow_repository)
    if not allowlist: allowlist=(root,)
    worktree_root, artifact_root = default_execution_roots(root)
    if args.worktree_root: worktree_root = Path(args.worktree_root).expanduser()
    if args.artifact_root: artifact_root = Path(args.artifact_root).expanduser()
    timeout = args.implementation_timeout_seconds or 300
    review_timeout = args.review_timeout_seconds or 900
    return LocalFirstController(ledger,_board_for_cli(args,allow_board_writes),RuntimeConfig(root,worktree_root,artifact_root,repository_allowlist=allowlist,implementation_timeout_seconds=timeout,review_timeout_seconds=review_timeout))


def _registered_controller(ledger: Ledger, args: argparse.Namespace, *, allow_board_writes: bool=False) -> tuple[LocalFirstController, OperatorConfig]:
    if args.worktree_root or args.artifact_root or args.implementation_timeout_seconds is not None or args.review_timeout_seconds is not None or args.allow_repository:
        raise ValueError("registered execution forbids runtime overrides; use the persisted operator registration")
    config = load_operator_config(Path(args.operator_config_path) if args.operator_config_path else None)
    if ledger.database.resolve() != config.ledger_path.resolve():
        raise ValueError("registered execution ledger does not match operator registration")
    requested = Path(args.repository).resolve() if args.repository != "." else config.canonical_repository
    if requested != config.canonical_repository:
        raise ValueError("registered execution repository does not match operator registration")
    runtime = config.runtime_config()
    local_review = config.local_review_registration
    model = LocalQwenAdapter(
        provider=config.implementation.provider,
        model=config.implementation.model,
        hermes_home=Path.home()/".hermes"/"profiles"/config.implementation.profile,
        review_hermes_home=Path.home()/".hermes"/"profiles"/local_review.profile,
        implementation_timeout_seconds=runtime.implementation_timeout_seconds,
        review_timeout_seconds=runtime.review_timeout_seconds,
    )
    model.review_provider, model.review_model = local_review.provider, local_review.model
    return LocalFirstController(ledger,_board_for_cli(args,allow_board_writes, implementation_profile=config.implementation.profile, canonical_repository=config.canonical_repository),runtime,local_model=model), config


def _board_for_cli(args: argparse.Namespace, allow_board_writes: bool, *, implementation_profile: str | None = None, canonical_repository: Path | None = None):
    if getattr(args, "hermes_executable", None) and getattr(args, "board", None):
        return HermesBoardAdapter(executable=args.hermes_executable, board=args.board, allow_writes=allow_board_writes, implementation_profile=implementation_profile, canonical_repository=canonical_repository)
    # Offline construction keeps read-only/reconciliation paths usable while any
    # external board write fails closed until --board/--hermes-executable exist.
    from .comment_delivery import MarkerLookup

    class _OfflineBoard:
        is_fake = False

        def import_candidates(self): raise PermissionError("offline CLI cannot read the external kanban")
        def get_task(self, task_id: str): raise PermissionError("offline CLI cannot read the external kanban")
        def find_comment_marker(self, *args_): return MarkerLookup.UNSUPPORTED
        def set_state(self, *args_, **kwargs): raise PermissionError("--board and --hermes-executable are required for board writes")
        def create_microticket(self, *args_, **kwargs): raise PermissionError("--board and --hermes-executable are required for board writes")
        def add_comment(self, *args_, **kwargs): raise PermissionError("--board and --hermes-executable are required for board writes")

    return _OfflineBoard()



def _registered_preview_policy_hashes(config: OperatorConfig, args: argparse.Namespace) -> tuple[str, str | None]:
    runtime = config.runtime_config()
    local_review = config.local_review_registration
    model = LocalQwenAdapter(
        provider=config.implementation.provider,
        model=config.implementation.model,
        hermes_home=Path.home()/".hermes"/"profiles"/config.implementation.profile,
        review_hermes_home=Path.home()/".hermes"/"profiles"/local_review.profile,
        implementation_timeout_seconds=runtime.implementation_timeout_seconds,
        review_timeout_seconds=runtime.review_timeout_seconds,
    )
    model.review_provider, model.review_model = local_review.provider, local_review.model
    review_hash = compute_review_execution_policy_hash(model, runtime.review_timeout_seconds)
    triage_route = dict(config.decomposition).get("local")
    if triage_route is None:
        return review_hash, None
    triage_hash = LocalTriagePlanner(
        executable=args.hermes_executable,
        provider=triage_route.provider,
        model=triage_route.model,
        profile=triage_route.profile,
        timeout_seconds=runtime.review_timeout_seconds,
    ).execution_policy_hash()
    return review_hash, triage_hash


def _registered_process_next_scheduler(ledger: Ledger, args: argparse.Namespace) -> ProcessNextScheduler:
    ctl, registered = _registered_controller(ledger,args,allow_board_writes=True)
    triage_route = dict(registered.decomposition).get("local")
    triage_planner = None if triage_route is None else LocalTriagePlanner(
        executable=args.hermes_executable,
        provider=triage_route.provider,
        model=triage_route.model,
        profile=triage_route.profile,
        timeout_seconds=ctl.config.review_timeout_seconds,
    )
    governor = UsageGovernor(ledger)
    paid_checkpoint = None if registered.paid_checkpoint is None else HermesPaidModelAdapter(
        ledger,
        governor,
        executable=args.hermes_executable,
        provider=registered.paid_checkpoint.provider,
        model=registered.paid_checkpoint.model,
        profile=registered.paid_checkpoint.profile,
        timeout_seconds=ctl.config.review_timeout_seconds,
    )
    paid_escalation = None if registered.paid_escalation is None else HermesPaidModelAdapter(
        ledger,
        governor,
        executable=args.hermes_executable,
        provider=registered.paid_escalation.provider,
        model=registered.paid_escalation.model,
        profile=registered.paid_escalation.profile,
        timeout_seconds=ctl.config.review_timeout_seconds,
    )
    successor_route = dict(registered.decomposition).get("standard")
    target_ticket_id = getattr(args, "ticket_id", None)

    def reconcile_hermes_owned_execution() -> dict[str, Any] | None:
        for candidate in ledger.hermes_execution_candidates():
            if target_ticket_id is not None and str(candidate["ticket_id"]) != target_ticket_id:
                continue
            external_task_id = str(candidate["external_task_id"])
            try:
                return ctl.reconcile_hermes_execution(external_task_id, require_handoff=True)
            except RuntimeError as exc:
                message = str(exc)
                if message in {
                    "Hermes execution handoff is not blocked for reconciliation",
                    "no completed Hermes worker run available for reconciliation",
                    "no unreconciled Hermes worker run available for reconciliation",
                }:
                    continue
                raise
        return None

    def activate_generated_work() -> dict[str, Any] | None:
        repair_candidates = [row for row in ledger.generated_repair_activation_candidates() if target_ticket_id is None or str(row["ticket_id"]) == target_ticket_id]
        if repair_candidates:
            candidate = repair_candidates[0]
            ticket_id = str(candidate["ticket_id"])
            external_task_id = str(candidate["external_task_id"])
            attempt_number = int(candidate["attempt_number"])
            prepared = ctl.prepare_hermes_repair_worktree(ticket_id, external_task_id, attempt_number)
            failure_evidence = str(candidate.get("failure_evidence") or "repair requested by Local First review")
            reason = f"Local First repair attempt {attempt_number}: {failure_evidence}"[:1500]
            task = ctl.board.reclaim_for_repair(external_task_id, reason=reason)
            binder = getattr(ctl.board, "bind_native_release_task", None)
            if binder is not None:
                binder(external_task_id, expected_workspace_path=prepared["workspace_path"])
                task = ctl.board.get_task(external_task_id)
            if task.status not in {"todo", "ready"}:
                raise RuntimeError("generated repair activation did not produce a dispatchable Hermes task")
            detail = json.dumps({
                "ticket_id": ticket_id,
                "external_task_id": external_task_id,
                "attempt_number": attempt_number,
                "previous_attempt_number": int(candidate["previous_attempt_number"]),
                "reason": reason,
                "prepared_execution": prepared,
                "hermes_status": task.status,
            }, sort_keys=True, separators=(",", ":"))
            stage = f"generated-repair-activation-{attempt_number}"
            if not ledger.record_runtime_stage(ticket_id, stage, detail, attempt_number=attempt_number, base_sha=prepared["base_sha"]):
                existing = ledger.connection.execute("SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage=?", (ticket_id, stage)).fetchone()
                if existing is None or str(existing["detail"]) != detail:
                    raise RuntimeError("generated repair activation reconciliation required")
            return {
                "ticket_id": ticket_id,
                "status": "repair_activated",
                "prepared_execution": prepared,
                "external_task_id": external_task_id,
                "attempt_number": attempt_number,
            }

        candidates = [row for row in ledger.generated_activation_candidates() if target_ticket_id is None or str(row["ticket_id"]) == target_ticket_id]
        if not candidates:
            return None
        ticket_id = str(candidates[0]["ticket_id"])
        result = activate_generated_ticket(ticket_id, ctl.config, ledger)
        prepared: dict[str, Any] | None = None
        if ledger.get_ticket(ticket_id)["state"] == CanonicalState.READY_LOCAL.value:
            external_task_id = ledger.resolve_external_task_id(ticket_id)
            prepared = ctl.prepare_hermes_dispatch_worktree(ticket_id, external_task_id)
        return {
            "ticket_id": ticket_id,
            "status": result.status,
            "repository_path": str(result.repository_path) if result.repository_path else None,
            "starting_sha": result.starting_sha,
            "readiness_status": result.readiness_status,
            "prepared_execution": prepared,
        }

    def materialize_successor(identity: dict[str, Any]) -> dict[str, Any]:
        if successor_route is None:
            raise RuntimeError("next tranche activation requires registered standard decomposition route")
        row = ledger.connection.execute("SELECT contract_json FROM feature_contracts WHERE feature_id=?", (identity["feature_id"],)).fetchone()
        if row is None:
            raise RuntimeError("next tranche activation feature contract is missing")
        stored = json.loads(str(row["contract_json"]))
        if isinstance(stored.get("spec"), dict):
            spec = FeatureAdmissionSpec.from_json(stored["spec"])
            allowed_paths = tuple((item.path, item.disposition) for item in spec.files)
        else:
            plan_row = ledger.connection.execute(
                "SELECT plan_json FROM decomposition_plans WHERE feature_id=? ORDER BY created_at,id LIMIT 1",
                (identity["feature_id"],),
            ).fetchone()
            if plan_row is None:
                raise RuntimeError("next tranche activation authorized repository paths are missing")
            try:
                envelope = json.loads(str(plan_row["plan_json"]))
                manifest = json.loads(str(envelope["plan"]["repo_snapshot_manifest_json"]))
                modify = tuple((str(path), "modify") for path in manifest["authorized_modify_paths"])
                create = tuple((str(path), "create") for path in manifest["authorized_create_paths"])
                allowed_paths = modify + create
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("next tranche activation authorized repository paths are malformed") from exc
            if not allowed_paths:
                raise RuntimeError("next tranche activation authorized repository paths are empty")
        planner = LocalDecompositionPlanner(
            executable=args.planner_executable,
            cost_class="standard",
            provider=successor_route.provider,
            model=successor_route.model,
            profile=successor_route.profile,
            allowed_paths=allowed_paths,
            role="decomposition",
            routing_source="operator-config.decomposition",
            sizing_provider=RuntimeMetricsStore(ledger).recommendation,
        )
        outcome = PlanningCoordinator(ledger, ctl.config, planner).materialize_next_tranche(str(identity["feature_id"]))
        if outcome.status not in {"activated", "already_materialized"}:
            raise RuntimeError(f"next tranche activation planning failed: {outcome.status}: {'; '.join(outcome.reasons)}")
        ticket_rows = ledger.connection.execute("SELECT id FROM tickets WHERE tranche_id=? ORDER BY id", (identity["successor_tranche_id"],)).fetchall()
        ticket_ids = [str(item["id"]) for item in ticket_rows]
        if not ticket_ids:
            raise RuntimeError("next tranche activation produced no successor tickets")
        snapshot_values: set[tuple[str, str, str]] = set()
        for ticket_id in ticket_ids:
            projections = ledger.connection.execute("SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND operation='create_microticket' AND superseded_at IS NULL", (ticket_id,)).fetchall()
            if len(projections) != 1:
                raise RuntimeError("next tranche activation missing generated card projection")
            projection = projections[0]
            payload = json.loads(str(projection["payload_json"]))
            body = str(payload.get("body") or "")
            marker = "```local-first-contract\\n"
            if marker not in body:
                raise RuntimeError("next tranche activation projection contract missing")
            contract_json = body.split(marker,1)[1].split("\\n```",1)[0]
            contract = json.loads(contract_json)
            snapshot_values.add((str(contract["repository_identity"]), str(contract["repo_base_sha"]), str(contract["repo_snapshot_hash"])))
        if len(snapshot_values) != 1:
            raise RuntimeError("next tranche activation successor snapshot identity diverged")
        repository_identity, repo_base_sha, repo_snapshot_hash = snapshot_values.pop()
        return {
            "candidate_identity": identity,
            "ticket_ids": ticket_ids,
            "repository_identity": repository_identity,
            "repo_base_sha": repo_base_sha,
            "repo_snapshot_hash": repo_snapshot_hash,
        }
    return ProcessNextScheduler(
        ledger,
        ctl.board,
        worker_id=args.worker_id,
        target_ticket_id=target_ticket_id,
        lease_seconds=ctl.config.lease_seconds,
        hermes_execution_runner=reconcile_hermes_owned_execution,
        generated_activation_runner=activate_generated_work,
        native_dependency_release_prepare_runner=lambda ticket_id, external_task_id: ctl.prepare_hermes_dispatch_worktree(ticket_id, external_task_id),
        native_dependency_release_profile=registered.implementation.profile,
        native_dependency_release_repository=str(registered.canonical_repository),
        native_dependency_release_signer_public_key=registered.signer_public_key_bytes if registered.operator_signing_public_key else None,
        native_dependency_release_signer_fingerprint=registered.operator_signing_key_fingerprint,
        native_dependency_release_signer_config_path=registered.config_path,
        implementation_runner=lambda ticket_id: ctl.execute_implementation_model_only(
            ticket_id, repository=registered.canonical_repository
        ),
        validation_runner=lambda ticket_id: ctl.execute_deterministic_validation_only(
            ticket_id, repository=registered.canonical_repository
        ),
        review_runner=lambda ticket_id: ctl.execute_fresh_review_only(
            ticket_id, repository=registered.canonical_repository
        ),
        review_execution_policy_hash=ctl.review_execution_policy_hash(),
        triage_runner=None if triage_planner is None else lambda ticket_id: ctl.execute_triage_only(ticket_id, planner=triage_planner),
        triage_execution_policy_hash=None if triage_planner is None else triage_planner.execution_policy_hash(),
        acceptance_runner=lambda ticket_id: ctl.inspect_acceptance_candidate_only(
            ticket_id, repository=registered.canonical_repository
        ),
        git_integration_runner=lambda ticket_id: ctl.execute_git_integration_only(
            ticket_id, repository=registered.canonical_repository
        ),
        worktree_cleanup_runner=lambda ticket_id: ctl.cleanup_completed_ticket_worktree(
            ticket_id, repository=registered.canonical_repository
        ),
        tranche_checkpoint_runner=lambda tranche_id: ctl.execute_tranche_checkpoint_only(
            tranche_id, repository=registered.canonical_repository
        ),
        paid_checkpoint_runner=None if paid_checkpoint is None else lambda tranche_id: ctl.execute_paid_stage_only(
            tranche_id, adapter=paid_checkpoint, purpose=PaidPurpose.INTEGRATION_CHECKPOINT
        ),
        paid_checkpoint_route=None if registered.paid_checkpoint is None else (
            registered.paid_checkpoint.provider, registered.paid_checkpoint.model, registered.paid_checkpoint.profile
        ),
        paid_escalation_runner=None if paid_escalation is None else lambda tranche_id: ctl.execute_paid_stage_only(
            tranche_id, adapter=paid_escalation, purpose=PaidPurpose.ESCALATION
        ),
        paid_escalation_route=None if registered.paid_escalation is None else (
            registered.paid_escalation.provider, registered.paid_escalation.model, registered.paid_escalation.profile
        ),
        next_tranche_materialize_runner=None if successor_route is None else materialize_successor,
    )

def _recovery_status(ledger: Ledger, *, limit: int = 25) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise ValueError("recovery status limit must be between 1 and 100")
    incomplete_invocations = [dict(row) for row in ledger.connection.execute(
        "SELECT ticket_id,attempt_number,stage,provider,model,started_at FROM model_invocations "
        "WHERE status='started' ORDER BY started_at,ticket_id LIMIT ?", (limit,)
    )]
    failed_reviews = [dict(row) for row in ledger.connection.execute(
        "SELECT ticket_id,attempt_number,status,last_outcome FROM review_candidates "
        "WHERE status='review_infrastructure_failed' ORDER BY updated_at,ticket_id LIMIT ?", (limit,)
    )]
    terminal_state_projections = [dict(row) for row in ledger.connection.execute(
        "SELECT ticket_id,event_id,operation,external_task_id,terminal_error FROM board_projection_outbox "
        "WHERE acknowledged_at IS NULL AND superseded_at IS NULL AND terminal_error IS NOT NULL "
        "ORDER BY queued_at,event_id LIMIT ?", (limit,)
    )]
    terminal_comments = [dict(row) for row in ledger.connection.execute(
        "SELECT ticket_id,event_id,operation_id,status,last_error FROM evidence_comment_outbox "
        "WHERE status='terminal' ORDER BY updated_at,operation_id LIMIT ?", (limit,)
    )]
    reconciliations = ledger.cleanup_prerequisites()[:limit]
    claimed = [dict(row) for row in ledger.connection.execute(
        "SELECT claim_id,ticket_id,stage,lease_owner,lease_expires_at,attempt_count,last_error FROM scheduler_stage_claims "
        "WHERE status='claimed' ORDER BY updated_at,claim_id LIMIT ?", (limit,)
    )]
    actions: list[dict[str, Any]] = []
    for row in incomplete_invocations:
        actions.append({
            "kind": "incomplete_model_invocation",
            "ticket_id": row["ticket_id"],
            "stage": row["stage"],
            "action": "inspect before any retry; incomplete model outcomes fail closed",
            "command": f"inspect --task-id {row['ticket_id']}",
        })
    for row in failed_reviews:
        actions.append({
            "kind": "review_infrastructure_failure",
            "ticket_id": row["ticket_id"],
            "action": "pause, inspect candidate provenance, then authorize one review-only resume if unchanged",
            "command": f"resume-failed-review --task-id {row['ticket_id']}",
        })
    for row in terminal_state_projections:
        actions.append({
            "kind": "terminal_state_projection",
            "ticket_id": row["ticket_id"],
            "action": "reconcile ledger state projection intent before delivery retry",
            "command": f"reconcile-state-projections --task-id {row['ticket_id']}",
        })
    for row in reconciliations:
        if row.get("cleanup_required") and not row.get("cleanup_confirmed"):
            actions.append({
                "kind": "retired_attempt_cleanup_required",
                "ticket_id": row["ticket_id"],
                "action": "remove separately-authorized forensic residue, then record cleanup confirmation while paused",
                "command": f"confirm-retired-attempt-cleanup --task-id {row['ticket_id']}",
            })
    return {
        "paused": ledger.status()["paused"],
        "incomplete_model_invocations": incomplete_invocations,
        "failed_reviews": failed_reviews,
        "terminal_state_projections": terminal_state_projections,
        "terminal_comments": terminal_comments,
        "cleanup_prerequisites": reconciliations,
        "claimed_scheduler_stages": claimed,
        "recommended_actions": actions[:limit],
    }


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Add the standalone CLI's arguments to *parser*.

    Hermes invokes this through ``ctx.register_cli_command`` so the native
    ``hermes local-first-orchestrator`` command and the installed standalone
    ``local-first-orchestrator`` command share one parser contract.
    """
    parser.add_argument("--database",required=True,help="separate ledger database; never Hermes kanban.db")
    parser.add_argument("--repository",default=".",help="canonical repository root (required for import/run-once)")
    parser.add_argument("--allow-repository",action="append",default=[],help="exact canonical repository root allowed for imports/execution; repeatable")
    parser.add_argument("--worktree-root", help="external worktree root; ad-hoc mode only")
    parser.add_argument("--artifact-root", help="external artifact root; ad-hoc mode only")
    parser.add_argument("--implementation-timeout-seconds", type=int, help="implementation timeout (1..21600); ad-hoc mode only")
    parser.add_argument("--review-timeout-seconds", type=int, help="review timeout (1..21600); ad-hoc mode only")
    parser.add_argument("--operator-config-path", help="registered operator config for production execution")
    parser.add_argument("--ad-hoc-runtime", action="store_true", help="explicit development-only runtime composition; never uses registered defaults")
    parser.add_argument("--hermes-executable", help="Hermes executable for explicit board access")
    parser.add_argument("--board", help="Hermes board name for explicit board access")
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("migrate")
    operator_status=commands.add_parser("operator-status", help="bounded lifecycle/status summary for operators")
    operator_status.add_argument("--limit", type=int, default=25)
    runtime_metrics=commands.add_parser("runtime-metrics", help="materialize and inspect bounded runtime metrics plus adaptive planner sizing")
    runtime_metrics.add_argument("--limit", type=int, default=500)
    recovery_status=commands.add_parser("recovery-status", aliases=("doctor",), help="read-only recovery blockers and exact next-action hints")
    recovery_status.add_argument("--limit", type=int, default=25)
    pause=commands.add_parser("pause", help="durably pause scheduler work before recovery/maintenance")
    pause.add_argument("--reason", required=True)
    pause.add_argument("--operator-id", default="local-first-cli")
    resume=commands.add_parser("resume", help="durably resume scheduler work after recovery/maintenance")
    resume.add_argument("--reason", required=True)
    resume.add_argument("--operator-id", default="local-first-cli")
    status=commands.add_parser("status"); status.add_argument("--active",action="store_true"); status.add_argument("--scheduler-detail",action="store_true",help="include one read-only scheduler lifecycle boundary snapshot")
    imported=commands.add_parser("import"); imported.add_argument("--task-id",required=True)
    run=commands.add_parser("run-once"); run.add_argument("--task-id",required=True); run.add_argument("--dry-run",action="store_true",default=True); run.add_argument("--execute",action="store_true"); run.add_argument("--allow-board-writes",action="store_true")
    process_next=commands.add_parser("process-next", help="run at most one durable Local First control stage; dry-run by default")
    process_next.add_argument("--execute", action="store_true")
    process_next.add_argument("--allow-board-writes", action="store_true")
    process_next.add_argument("--worker-id", default="local-first-process-next")
    process_next.add_argument("--planner-executable", default="hermes")
    process_next.add_argument("--ticket-id", help="scope this scheduler tick to exactly one ticket; tranche-wide stages are suppressed")
    daemon=commands.add_parser("daemon", help="repeatedly invoke the proven one-tick scheduler primitive")
    daemon.add_argument("--execute", action="store_true")
    daemon.add_argument("--allow-board-writes", action="store_true")
    daemon.add_argument("--worker-id", default="local-first-daemon")
    daemon.add_argument("--planner-executable", default="hermes")
    daemon.add_argument("--idle-sleep-seconds", type=float, default=1.0)
    daemon.add_argument("--busy-sleep-seconds", type=float, default=0.25)
    daemon.add_argument("--error-backoff-seconds", type=float, default=1.0)
    daemon.add_argument("--max-error-backoff-seconds", type=float, default=30.0)
    daemon.add_argument("--max-iterations", type=int)
    implementation=commands.add_parser("implementation-only", aliases=("implement-only",), help="run exactly implementation and deterministic validation; never review or accept")
    implementation.add_argument("--task-id",required=True)
    revalidate=commands.add_parser("revalidate-implementation", help="revalidate an existing implementation; never retry implementation or review")
    revalidate.add_argument("--task-id", required=True); revalidate.add_argument("--attempt-number", required=True, type=int); revalidate.add_argument("--operator-id", default="local-first-cli")
    authorize_revalidate=commands.add_parser("authorize-historical-revalidation", help="authorize one exact historical implementation for a future integrity gate; does not revalidate")
    authorize_revalidate.add_argument("--task-id",required=True); authorize_revalidate.add_argument("--attempt-number",required=True, type=int); authorize_revalidate.add_argument("--operator-id", default="local-first-cli"); authorize_revalidate.add_argument("--reason", default="operator authorization for historical revalidation")
    attest_revalidate=commands.add_parser("attest-historical-revalidation", help="attest one preserved historical implementation; does not validate or recover")
    attest_revalidate.add_argument("--task-id",required=True); attest_revalidate.add_argument("--attempt-number",required=True, type=int); attest_revalidate.add_argument("--operator-id", default="local-first-cli")
    apply_review=commands.add_parser("apply-persisted-review", help="apply one persisted review result without verdict disposition")
    apply_review.add_argument("--task-id", required=True); apply_review.add_argument("--attempt-number", required=True, type=int)
    accept_review=commands.add_parser("accept-reviewed-candidate", help="accept one reviewed candidate without advancing integration")
    accept_review.add_argument("--task-id", required=True); accept_review.add_argument("--attempt-number", required=True, type=int)
    integrate=commands.add_parser("integrate-accepted-candidate", help="fast-forward one accepted candidate into tranche integration")
    integrate.add_argument("--task-id", required=True); integrate.add_argument("--attempt-number", required=True, type=int)
    inspect=commands.add_parser("inspect"); inspect.add_argument("--task-id",required=True)
    generated=commands.add_parser("project-generated"); generated.add_argument("--allow-board-writes",action="store_true")
    activate=commands.add_parser("activate-generated"); activate.add_argument("ticket_id")
    reconcile=commands.add_parser("reconcile-failed-attempt", help="explicitly retire a blocked failed attempt; performs no cleanup or execution")
    reconcile.add_argument("--task-id", required=True)
    reconcile.add_argument("--classification", required=True, choices=("runtime_infrastructure_failure", "model_timeout", "process_error", "validation_failure", "review_exhaustion"))
    reconcile.add_argument("--operator-id", default="local-first-cli")
    reconcile.add_argument("--forensic-artifact-path", action="append", default=[], help="existing artifact root retained with the retired attempt; repeatable")
    correction_plan=commands.add_parser("correction-plan", aliases=("create-correction-plan",), help="validate and persist a supplemental post-acceptance correction plan from --plan-file JSON; never materializes, unpause, or executes")
    correction_plan.add_argument("--plan-file", required=True)
    correction_materialize=commands.add_parser("correction-materialize", aliases=("materialize-correction",), help="materialize one persisted correction plan into draft microtickets with the normal durable board-projection intent; never unpause or execute")
    correction_materialize.add_argument("--correction-plan-id", required=True)
    review_resume=commands.add_parser("resume-failed-review", help="authorize a review-only retry for an unchanged validated candidate")
    review_resume.add_argument("--task-id", required=True)
    review_resume.add_argument("--operator-id", default="local-first-cli")
    state_reconcile=commands.add_parser("reconcile-state-projections", help="ledger-only: supersede stale state intents and ensure the current state intent")
    state_reconcile.add_argument("--task-id", required=True)
    reopen_generated=commands.add_parser("reopen-terminal-generated-projection", help="ledger-only: reopen a terminal generated-card projection only when no external task was created and the current durable payload now verifies")
    reopen_generated.add_argument("--task-id", required=True)
    reopen_generated.add_argument("--event-id", required=True, type=int)
    recover_generated=commands.add_parser("recover-generated-projection", help="paused operator-only: read-verify and supersede one pre-native generated Hermes card; performs no board writes")
    recover_generated.add_argument("--task-id", required=True, help="Local First ticket id")
    recover_generated.add_argument("--event-id", required=True, type=int, help="acknowledged generated create event id")
    recover_generated.add_argument("--operator-id", required=True)
    recover_generated.add_argument("--reason", required=True)
    hermes_execution=commands.add_parser("reconcile-hermes-execution", help="bind one completed dispatcher-owned Hermes run into a Local First attempt; never launches implementation")
    hermes_execution.add_argument("--task-id", required=True, help="Hermes task id / Local First external task id")
    hermes_execution.add_argument("--run-id", type=int, help="explicit Hermes run id; required when multiple unreconciled completed worker runs exist")
    native_revalidate=commands.add_parser("revalidate-native-release", help="paused operator-only: append exact read-only evidence for one legacy native release")
    native_revalidate.add_argument("--task-id", required=True)
    native_revalidate.add_argument("--operator-id", required=True)
    native_revalidate.add_argument("--reason", required=True)
    native_revalidate.add_argument("--approval-file")
    native_revalidate.add_argument("--signature-file")
    native_activate=commands.add_parser("activate-native-release", help="paused operator-only: activate one exact signed legacy native release")
    native_activate.add_argument("--task-id", required=True)
    native_activate.add_argument("--revalidation-id", required=True)
    native_activate.add_argument("--operator-id", required=True)
    native_activate.add_argument("--reason", required=True)
    native_activate.add_argument("--request-key", required=True)
    native_activate.add_argument("--allow-board-writes", action="store_true")
    native_activate.add_argument("--approval-file", required=True)
    native_activate.add_argument("--signature-file", required=True)
    native_prepare=commands.add_parser("prepare-native-release-activation", help="read-only: emit canonical activation approval bytes")
    native_prepare.add_argument("--task-id", required=True)
    native_prepare.add_argument("--revalidation-id", required=True)
    native_prepare.add_argument("--operator-id", required=True)
    native_prepare.add_argument("--reason", required=True)
    native_prepare.add_argument("--request-key", required=True)
    native_prepare.add_argument("--output-file")
    enroll_signer=commands.add_parser("enroll-operator-signer", help="paused operator-only: enroll one public Ed25519 signer for selected legacy native releases")
    enroll_signer.add_argument("--operator-id", required=True)
    enroll_signer.add_argument("--reason", required=True)
    enroll_signer.add_argument("--public-key", required=True, help="base64 raw Ed25519 public key; private keys are never read")
    enroll_signer.add_argument("--fingerprint", required=True, help="lowercase SHA-256 public-key fingerprint")
    enroll_signer.add_argument("--ticket-id", action="append", dest="ticket_ids", required=True, help="explicit legacy ticket ID; repeatable")
    enroll_signer.add_argument("--document-file", help="canonical document emitted by prepare-operator-signer-enrollment")
    enroll_signer.add_argument("--signature-file", help="64-byte detached signature made by the new key")
    prepare_signer=commands.add_parser("prepare-operator-signer-enrollment", help="read-only: emit canonical external signer enrollment document")
    prepare_signer.add_argument("--operator-id", required=True)
    prepare_signer.add_argument("--reason", required=True)
    prepare_signer.add_argument("--public-key", required=True)
    prepare_signer.add_argument("--fingerprint", required=True)
    prepare_signer.add_argument("--ticket-id", action="append", dest="ticket_ids", required=True)
    prepare_signer.add_argument("--output-file")
    prepare_native=commands.add_parser("prepare-native-release-revalidation", help="read-only: emit canonical approval document; never signs or mutates")
    prepare_native.add_argument("--task-id", required=True)
    prepare_native.add_argument("--operator-id", required=True)
    prepare_native.add_argument("--reason", required=True)
    prepare_native.add_argument("--output-file")
    confirm_cleanup=commands.add_parser("confirm-retired-attempt-cleanup", help="verify separately-authorized cleanup; never removes files")
    confirm_cleanup.add_argument("--task-id", required=True)
    confirm_cleanup.add_argument("--operator-id", default="local-first-cli")
    status.add_argument("--show-cleanup-prerequisites", action="store_true", help="read-only: tickets with retired attempts whose forensic residue must be removed before the next attempt")
    register=commands.add_parser("register-dashboard", aliases=("init",), help="persist the operator's one safe ledger/runtime registration")
    register.add_argument("--config-path", help="operator registration path (default: ~/.hermes/local-first-orchestrator/operator-config.json)")
    register.add_argument("--implementation-profile", default="worker-code-local")
    register.add_argument("--implementation-provider", default=LOCAL_QWEN_PROVIDER)
    register.add_argument("--implementation-model", default=LOCAL_QWEN_MODEL)
    register.add_argument("--review-profile", default="worker-code-local")
    register.add_argument("--review-provider", default=LOCAL_QWEN_PROVIDER)
    register.add_argument("--review-model", default=LOCAL_QWEN_MODEL)
    register.add_argument("--local-review-profile")
    register.add_argument("--local-review-provider")
    register.add_argument("--local-review-model")
    register.add_argument("--operator-signing-public-key", help="base64 Ed25519 public key; private keys are never accepted")
    register.add_argument("--operator-signing-key-fingerprint", help="sha256 fingerprint of the registered public key")
    register.add_argument("--decomposition-local-profile")
    register.add_argument("--decomposition-local-provider")
    register.add_argument("--decomposition-local-model")
    register.add_argument("--decomposition-standard-profile")
    register.add_argument("--decomposition-standard-provider")
    register.add_argument("--decomposition-standard-model")
    register.add_argument("--paid-checkpoint-profile")
    register.add_argument("--paid-checkpoint-provider")
    register.add_argument("--paid-checkpoint-model")
    register.add_argument("--paid-escalation-profile")
    register.add_argument("--paid-escalation-provider")
    register.add_argument("--paid-escalation-model")
    paid_approve=commands.add_parser("approve-paid", help="grant exactly one additional paid call for one feature/purpose")
    paid_approve.add_argument("--feature-id", required=True)
    paid_approve.add_argument("--purpose", required=True, choices=(PaidPurpose.ARCHITECTURE.value, PaidPurpose.INTEGRATION_CHECKPOINT.value, PaidPurpose.ESCALATION.value))
    paid_approve.add_argument("--reason", required=True)
    paid_approve.add_argument("--idempotency-key", required=True)
    paid_approve.add_argument("--operator-id", default="local-first-cli")
    admission=commands.add_parser("admit-feature-contract", help="admit one paused, predecessor-authorized feature contract without planning")
    admission.add_argument("--spec-file", required=True)
    planning=commands.add_parser("plan-feature", help="generate and persist one validated decomposition plan without activation")
    planning.add_argument("--feature-id", required=True)
    planning.add_argument("--planner-executable", default="hermes")
    planning.add_argument("--planner-cost-class", choices=("local", "standard"), default="local", help="budget class within the configured decomposition role; never selects implementation routing")
    activate_plan=commands.add_parser("activate-feature-plan", help="activate or idempotently repair one persisted validated feature plan while paused")
    activate_plan.add_argument("--feature-id", required=True)
    activate_plan.add_argument("--plan-id", required=True)
    activate_plan.add_argument("--request-key", required=True)
    activate_plan.add_argument("--planner-executable", default="hermes")
    activate_plan.add_argument("--planner-cost-class", choices=("local", "standard"), default="standard")
    snapshot_revalidate=commands.add_parser("revalidate-feature-snapshot", help="append one deterministic feature repository snapshot revalidation")
    snapshot_revalidate.add_argument("--feature-id", required=True)
    snapshot_revalidate.add_argument("--planner-executable", default="hermes")
def run_command(args: argparse.Namespace) -> int:
    """Run a parsed standalone or native Hermes CLI command."""
    if args.command=="process-next" and not args.execute:
        if args.ad_hoc_runtime: raise ValueError("process-next requires registered operator runtime")
        registered = load_operator_config(Path(args.operator_config_path) if args.operator_config_path else None)
        board = _board_for_cli(args, False, implementation_profile=registered.implementation.profile, canonical_repository=registered.canonical_repository)
        if not getattr(args, "ticket_id", None):
            preview = preview_database(Path(args.database), operator_config=registered, board=board)
        else:
            review_hash, triage_hash = _registered_preview_policy_hashes(registered, args)
            preview = preview_ticket_database(
                Path(args.database),
                str(args.ticket_id),
                operator_config=registered,
                board=board,
                review_execution_policy_hash=review_hash,
                triage_execution_policy_hash=triage_hash,
            )
        print(json.dumps(asdict(preview),sort_keys=True))
        return 0
    ledger=_ledger(args.database)
    try:
        if args.command=="migrate": pass
        elif args.command=="operator-status":
            data=ledger.operator_status(active_limit=args.limit)
            data["scheduler_detail"]=scheduler_observability(ledger)
            print(json.dumps(data,sort_keys=True))
        elif args.command=="runtime-metrics":
            store=RuntimeMetricsStore(ledger)
            materialized=store.materialize_completed(limit=args.limit)
            print(json.dumps({"materialized":materialized,"summary":store.summary(limit=args.limit),"adaptive_sizing":store.recommendation().as_json()},sort_keys=True))
        elif args.command in {"recovery-status", "doctor"}:
            print(json.dumps(_recovery_status(ledger,limit=args.limit),sort_keys=True))
        elif args.command=="pause":
            ledger.pause(args.operator_id,reason=args.reason)
            print(json.dumps({"paused":True,"operator_id":args.operator_id,"reason":args.reason},sort_keys=True))
        elif args.command=="resume":
            ledger.resume(args.operator_id,reason=args.reason)
            print(json.dumps({"paused":False,"operator_id":args.operator_id,"reason":args.reason},sort_keys=True))
        elif args.command=="status":
            data=ledger.status()
            if args.active: data["active"]=[dict(r) for r in ledger.connection.execute("SELECT id, external_id, state, lease_owner, lease_expires_at FROM tickets WHERE state IN ('implementing','verifying','local_review','repairing') ORDER BY updated_at")]
            if getattr(args,"scheduler_detail",False): data["scheduler_detail"]=scheduler_observability(ledger)
            if getattr(args,"show_cleanup_prerequisites",False): data["cleanup_prerequisites"]=ledger.cleanup_prerequisites()
            print(json.dumps(data,sort_keys=True))
        elif args.command=="import":
            ctl=_ad_hoc_controller(ledger,args); print(json.dumps({"ticket_id":ctl.import_card(ctl.board.get_task(args.task_id))}))
        elif args.command=="run-once":
            if args.ad_hoc_runtime:
                ctl=_ad_hoc_controller(ledger,args,allow_board_writes=args.allow_board_writes); repository=Path(args.repository)
            else:
                ctl, registered=_registered_controller(ledger,args,allow_board_writes=args.allow_board_writes); repository=registered.canonical_repository
            if not args.execute: print(json.dumps(ctl.dry_run(args.task_id),sort_keys=True))
            elif not args.allow_board_writes: raise PermissionError("--execute requires --allow-board-writes; no write-enabled execution without both")
            else: print(json.dumps({"executed":ctl.execute(args.task_id,repository=repository,allow_board_writes=True)}))
        elif args.command=="process-next":
            if args.ad_hoc_runtime: raise ValueError("process-next requires registered operator runtime")
            if not args.allow_board_writes: raise PermissionError("process-next --execute requires --allow-board-writes")
            if not args.hermes_executable or not args.board: raise ValueError("process-next execution requires --hermes-executable and --board")
            result=_registered_process_next_scheduler(ledger,args).process_next()
            print(json.dumps(asdict(result),sort_keys=True))
        elif args.command=="daemon":
            if args.ad_hoc_runtime: raise ValueError("daemon requires registered operator runtime")
            if not args.execute: raise PermissionError("daemon requires --execute")
            if not args.allow_board_writes: raise PermissionError("daemon --execute requires --allow-board-writes")
            if not args.hermes_executable or not args.board: raise ValueError("daemon execution requires --hermes-executable and --board")
            daemon=SchedulerDaemon(
                ledger,
                lambda: _registered_process_next_scheduler(ledger,args),
                worker_id=args.worker_id,
                idle_sleep_seconds=args.idle_sleep_seconds,
                busy_sleep_seconds=args.busy_sleep_seconds,
                error_backoff_seconds=args.error_backoff_seconds,
                max_error_backoff_seconds=args.max_error_backoff_seconds,
            )
            previous=daemon.install_signal_handlers()
            try:
                health=daemon.run(max_iterations=args.max_iterations,continue_on_error=True)
            finally:
                daemon.restore_signal_handlers(previous)
            print(json.dumps({"health":asdict(health),"scheduler":scheduler_observability(ledger)},sort_keys=True))
        elif args.command in {"implementation-only", "implement-only"}:
            if args.ad_hoc_runtime: raise ValueError("implementation-only execution requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            result=ctl.execute_implementation(args.task_id,repository=registered.canonical_repository)
            print(json.dumps(result or {"ticket_id":args.task_id,"status":"not_run"},sort_keys=True))
        elif args.command=="revalidate-implementation":
            if args.ad_hoc_runtime: raise ValueError("implementation revalidation requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.revalidate_historical_implementation(args.task_id,args.attempt_number,repository=registered.canonical_repository,operator_id=args.operator_id),sort_keys=True))
        elif args.command=="authorize-historical-revalidation":
            if args.ad_hoc_runtime: raise ValueError("historical revalidation authorization requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.authorize_historical_revalidation(args.task_id,args.attempt_number,repository=registered.canonical_repository,operator_id=args.operator_id,reason=args.reason),sort_keys=True))
        elif args.command=="attest-historical-revalidation":
            if args.ad_hoc_runtime: raise ValueError("historical implementation attestation requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.attest_historical_revalidation_implementation(args.task_id,args.attempt_number,repository=registered.canonical_repository,operator_id=args.operator_id),sort_keys=True))
        elif args.command=="apply-persisted-review":
            if args.ad_hoc_runtime: raise ValueError("persisted review application requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.apply_persisted_review_only(args.task_id,args.attempt_number,repository=registered.canonical_repository),sort_keys=True))
        elif args.command=="accept-reviewed-candidate":
            if args.ad_hoc_runtime: raise ValueError("acceptance-only operation requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.accept_reviewed_candidate_only(args.task_id,args.attempt_number,repository=registered.canonical_repository),sort_keys=True,default=str))
        elif args.command=="integrate-accepted-candidate":
            if args.ad_hoc_runtime: raise ValueError("integration-only operation requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.integrate_accepted_candidate_only(args.task_id,args.attempt_number,repository=registered.canonical_repository),sort_keys=True,default=str))
        elif args.command=="inspect": print(json.dumps(ledger.get_ticket(args.task_id),sort_keys=True))
        elif args.command=="admit-feature-contract":
            if args.ad_hoc_runtime: raise ValueError("feature admission requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            spec=FeatureAdmissionSpec.from_json(json.loads(Path(args.spec_file).expanduser().read_text(encoding="utf-8")))
            print(json.dumps(ctl.admit_feature_contract(spec,repository=registered.canonical_repository).__dict__,sort_keys=True,default=str))
        elif args.command=="plan-feature":
            if args.ad_hoc_runtime: raise ValueError("plan-feature requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            row=ledger.connection.execute("SELECT contract_json FROM feature_contracts WHERE feature_id=?", (args.feature_id,)).fetchone()
            if row is None: raise ValueError("authoritative feature contract is missing")
            stored=json.loads(row["contract_json"])
            feature=FeatureAdmissionSpec.from_json(stored["spec"]).contract
            if feature.id != args.feature_id: raise ValueError("feature contract identity mismatch")
            route=registered.decomposition_route(args.planner_cost_class)
            allowed_paths=tuple((item["path"], item["disposition"]) for item in stored["spec"]["files"])
            planner=LocalDecompositionPlanner(executable=args.planner_executable,cost_class=args.planner_cost_class,provider=route.provider,model=route.model,profile=route.profile,allowed_paths=allowed_paths,role="decomposition",routing_source="operator-config.decomposition",sizing_provider=RuntimeMetricsStore(ledger).recommendation)
            coordinator=PlanningCoordinator(ledger, ctl.config, planner)
            print(json.dumps(coordinator.generate_plan_only(feature,repository=registered.canonical_repository).__dict__,sort_keys=True,default=str))
        elif args.command=="activate-feature-plan":
            if args.ad_hoc_runtime: raise ValueError("activate-feature-plan requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            row=ledger.connection.execute("SELECT contract_json FROM feature_contracts WHERE feature_id=?", (args.feature_id,)).fetchone()
            if row is None: raise ValueError("authoritative feature contract is missing")
            stored=json.loads(row["contract_json"])
            feature=FeatureAdmissionSpec.from_json(stored["spec"]).contract
            if feature.id != args.feature_id: raise ValueError("feature contract identity mismatch")
            route=registered.decomposition_route(args.planner_cost_class)
            allowed_paths=tuple((item["path"], item["disposition"]) for item in stored["spec"]["files"])
            planner=LocalDecompositionPlanner(executable=args.planner_executable,cost_class=args.planner_cost_class,provider=route.provider,model=route.model,profile=route.profile,allowed_paths=allowed_paths,role="decomposition",routing_source="operator-config.decomposition",sizing_provider=RuntimeMetricsStore(ledger).recommendation)
            coordinator=PlanningCoordinator(ledger, ctl.config, planner)
            print(json.dumps(coordinator.activate_persisted_plan(feature,request_key=args.request_key,plan_id=args.plan_id).__dict__,sort_keys=True,default=str))
        elif args.command=="revalidate-feature-snapshot":
            if args.ad_hoc_runtime: raise ValueError("feature snapshot revalidation requires registered operator runtime")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            row=ledger.connection.execute("SELECT contract_json FROM feature_contracts WHERE feature_id=?", (args.feature_id,)).fetchone()
            if row is None: raise ValueError("authoritative feature contract is missing")
            feature=FeatureAdmissionSpec.from_json(json.loads(row["contract_json"])["spec"]).contract
            route=registered.decomposition_route("standard")
            planner=LocalDecompositionPlanner(executable=args.planner_executable,cost_class="standard",provider=route.provider,model=route.model,profile=route.profile,allowed_paths=(),role="decomposition",routing_source="operator-config.decomposition",sizing_provider=RuntimeMetricsStore(ledger).recommendation)
            coordinator=PlanningCoordinator(ledger, ctl.config, planner)
            print(json.dumps(coordinator.revalidate_feature_repository_snapshot(args.feature_id),sort_keys=True,default=str))
        elif args.command in {"register-dashboard", "init"}:
            root=Path(args.repository).resolve(strict=True)
            allowlist=tuple(Path(item).resolve(strict=True) for item in args.allow_repository) or (root,)
            worktree_root, artifact_root = default_execution_roots(root)
            if args.worktree_root: worktree_root = Path(args.worktree_root).expanduser()
            if args.artifact_root: artifact_root = Path(args.artifact_root).expanduser()
            decomposition_routes = {}
            for cost in ("local", "standard"):
                profile = getattr(args, f"decomposition_{cost}_profile")
                provider = getattr(args, f"decomposition_{cost}_provider")
                model = getattr(args, f"decomposition_{cost}_model")
                supplied = (profile, provider, model)
                if any(value is not None for value in supplied):
                    if not all(isinstance(value, str) and value.strip() for value in supplied):
                        raise ValueError(f"decomposition {cost} route requires profile, provider, and model together")
                    decomposition_routes[cost] = ModelRegistration(profile, provider, model)
            paid_routes = {}
            for purpose in ("checkpoint", "escalation"):
                supplied = tuple(getattr(args, f"paid_{purpose}_{field}") for field in ("profile", "provider", "model"))
                if any(value is not None for value in supplied):
                    if not all(isinstance(value, str) and value.strip() for value in supplied):
                        raise ValueError(f"paid {purpose} route requires profile, provider, and model together")
                    paid_routes[purpose] = ModelRegistration(*supplied)
            local_review_values = (args.local_review_profile, args.local_review_provider, args.local_review_model)
            if any(value is not None for value in local_review_values):
                if not all(isinstance(value, str) and value.strip() for value in local_review_values):
                    raise ValueError("local review route requires profile, provider, and model together")
                local_review = ModelRegistration(*local_review_values)
            else:
                local_review = None
            config=OperatorConfig(
                ledger_path=Path(args.database),
                canonical_repository=root,
                repository_allowlist=allowlist,
                implementation=ModelRegistration(args.implementation_profile, args.implementation_provider, args.implementation_model),
                review=ModelRegistration(args.review_profile, args.review_provider, args.review_model),
                worktree_root=worktree_root,
                artifact_root=artifact_root,
                implementation_timeout_seconds=args.implementation_timeout_seconds or 300,
                review_timeout_seconds=args.review_timeout_seconds or 900,
                decomposition=tuple(sorted(decomposition_routes.items())),
                paid_checkpoint=paid_routes.get("checkpoint"),
                paid_escalation=paid_routes.get("escalation"),
                operator_signing_public_key=args.operator_signing_public_key,
                operator_signing_key_fingerprint=args.operator_signing_key_fingerprint,
                local_review=local_review,
            )
            path=save_operator_config(config, Path(args.config_path) if args.config_path else None)
            print(json.dumps({"registered": str(path)}, sort_keys=True))
        elif args.command=="approve-paid":
            governor=UsageGovernor(ledger)
            approval=governor.approve(args.feature_id,PaidPurpose(args.purpose),args.operator_id,args.reason,args.idempotency_key)
            print(json.dumps(asdict(approval),sort_keys=True))
        elif args.command=="reconcile-failed-attempt":
            if args.ad_hoc_runtime: raise ValueError("failed-attempt reconciliation requires registered operator runtime")
            ctl, _ = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.reconcile_failed_attempt(args.task_id,operator_id=args.operator_id,classification=args.classification,forensic_artifact_paths=tuple(Path(path) for path in args.forensic_artifact_path)),sort_keys=True))
        elif args.command=="resume-failed-review":
            if args.ad_hoc_runtime: raise ValueError("review reconciliation requires registered operator runtime")
            ctl, _ = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.resume_failed_review(args.task_id,operator_id=args.operator_id),sort_keys=True))
        elif args.command=="reconcile-state-projections":
            print(json.dumps(ledger.reconcile_state_projections(args.task_id), sort_keys=True))
        elif args.command=="reopen-terminal-generated-projection":
            print(json.dumps(ledger.reopen_terminal_generated_projection(args.task_id,args.event_id),sort_keys=True))
        elif args.command=="recover-generated-projection":
            if args.ad_hoc_runtime: raise ValueError("generated projection recovery requires registered operator runtime")
            if not args.hermes_executable or not args.board: raise ValueError("generated projection recovery requires --hermes-executable and --board")
            ctl, _ = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.recover_generated_projection(args.task_id,args.event_id,operator_id=args.operator_id,reason=args.reason),sort_keys=True))
        elif args.command=="reconcile-hermes-execution":
            if args.ad_hoc_runtime: raise ValueError("Hermes execution reconciliation requires registered operator runtime")
            if not args.hermes_executable or not args.board: raise ValueError("Hermes execution reconciliation requires --hermes-executable and --board")
            ctl, _ = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.reconcile_hermes_execution(args.task_id,hermes_run_id=args.run_id),sort_keys=True))
        elif args.command=="activate-native-release":
            if args.ad_hoc_runtime: raise ValueError("native release activation requires registered operator runtime")
            if not args.allow_board_writes: raise PermissionError("activate-native-release requires --allow-board-writes")
            if not args.hermes_executable or not args.board: raise ValueError("native release activation requires --hermes-executable and --board")
            ctl, _ = _registered_controller(ledger,args,allow_board_writes=True)
            document = parse_approval_document(Path(args.approval_file).read_bytes())
            print(json.dumps(ctl.activate_native_release_revalidation(args.task_id, revalidation_id=args.revalidation_id, operator_id=args.operator_id, reason=args.reason, request_key=args.request_key, approval_document=document, detached_signature=Path(args.signature_file).read_bytes()), sort_keys=True))
        elif args.command=="prepare-native-release-activation":
            if args.ad_hoc_runtime: raise ValueError("native release activation preparation requires registered operator runtime")
            if not args.hermes_executable or not args.board: raise ValueError("native release activation preparation requires --hermes-executable and --board")
            ctl, _ = _registered_controller(ledger,args,allow_board_writes=False)
            prepared = ctl.prepare_native_release_activation(args.task_id, revalidation_id=args.revalidation_id, operator_id=args.operator_id, reason=args.reason, request_key=args.request_key)
            raw = prepared["canonical_document"].encode("utf-8")
            if args.output_file:
                Path(args.output_file).write_bytes(raw)
                print(json.dumps({"approval_hash": prepared["approval_hash"], "output_file": args.output_file}, sort_keys=True))
            else:
                sys.stdout.write(prepared["canonical_document"])
        elif args.command=="revalidate-native-release":
            if args.ad_hoc_runtime: raise ValueError("native release revalidation requires registered operator runtime")
            if not args.hermes_executable or not args.board: raise ValueError("native release revalidation requires --hermes-executable and --board")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            if not args.approval_file or not args.signature_file:
                raise ValueError("revalidate-native-release requires --approval-file and --signature-file")
            document_bytes = Path(args.approval_file).read_bytes()
            document = parse_approval_document(document_bytes)
            if document["operator_id"] != args.operator_id or document["reason"] != args.reason:
                raise ValueError("CLI operator identity/reason must match signed approval document")
            result = ctl.revalidate_native_release(args.task_id, operator_id=args.operator_id, reason=args.reason, implementation_profile=registered.implementation.profile, approval_document=document, detached_signature=Path(args.signature_file).read_bytes(), signer_public_key=registered.signer_public_key_bytes, signer_fingerprint=registered.operator_signing_key_fingerprint)
            print(json.dumps(result, sort_keys=True))
        elif args.command=="enroll-operator-signer":
            if args.ad_hoc_runtime: raise ValueError("signer enrollment requires registered operator configuration")
            if args.worktree_root or args.artifact_root or args.implementation_timeout_seconds is not None or args.review_timeout_seconds is not None or args.allow_repository or args.repository != "." or args.hermes_executable or args.board:
                raise ValueError("signer enrollment forbids runtime, repository, board, and model overrides")
            config_path = Path(args.operator_config_path) if args.operator_config_path else None
            if config_path is None:
                from .operator_config import default_config_path
                config_path = default_config_path()
            if not args.document_file or not args.signature_file:
                raise ValueError("enroll-operator-signer requires --document-file and --signature-file")
            result = enroll_operator_signer(ledger, config_path=config_path, document=Path(args.document_file).read_bytes(), detached_signature=Path(args.signature_file).read_bytes(), public_key_b64=args.public_key, fingerprint=args.fingerprint)
            print(json.dumps(result, sort_keys=True))
        elif args.command=="prepare-operator-signer-enrollment":
            if args.ad_hoc_runtime: raise ValueError("signer enrollment preparation requires registered operator configuration")
            from .signer_enrollment import prepare_operator_signer_enrollment
            config_path = Path(args.operator_config_path) if args.operator_config_path else __import__("local_first_orchestrator.operator_config", fromlist=["default_config_path"]).default_config_path()
            prepared = prepare_operator_signer_enrollment(ledger, config_path=config_path, operator_id=args.operator_id, reason=args.reason, ticket_ids=tuple(args.ticket_ids), public_key_b64=args.public_key, fingerprint=args.fingerprint)
            if args.output_file:
                Path(args.output_file).write_bytes(prepared["document_bytes"])
                print(json.dumps({"document_hash": prepared["document_hash"], "output_file": args.output_file}, sort_keys=True))
            else:
                sys.stdout.buffer.write(prepared["document_bytes"])
        elif args.command=="prepare-native-release-revalidation":
            if args.ad_hoc_runtime: raise ValueError("native release preparation requires registered operator runtime")
            if not args.hermes_executable or not args.board: raise ValueError("native release preparation requires --hermes-executable and --board")
            ctl, registered = _registered_controller(ledger,args,allow_board_writes=False)
            prepared = ctl.prepare_native_release_revalidation(args.task_id, operator_id=args.operator_id, reason=args.reason, implementation_profile=registered.implementation.profile)
            encoded = prepared["canonical_document"].encode("utf-8")
            if args.output_file:
                Path(args.output_file).write_bytes(encoded)
                print(json.dumps({"approval_hash": prepared["approval_hash"], "output_file": args.output_file}, sort_keys=True))
            else:
                sys.stdout.write(prepared["canonical_document"])
        elif args.command=="confirm-retired-attempt-cleanup":
            if args.ad_hoc_runtime: raise ValueError("cleanup confirmation requires registered operator runtime")
            ctl, _ = _registered_controller(ledger,args,allow_board_writes=False)
            print(json.dumps(ctl.confirm_retired_attempt_cleanup(args.task_id,operator_id=args.operator_id),sort_keys=True))
        elif args.command=="activate-generated":
            root=Path(args.repository).resolve(); allowlist=tuple(Path(item).resolve() for item in args.allow_repository) or (root,)
            worktree_root, artifact_root = default_execution_roots(root)
            config=RuntimeConfig(root,worktree_root,artifact_root,repository_allowlist=allowlist,implementation_timeout_seconds=args.implementation_timeout_seconds or 300)
            result=activate_generated_ticket(args.ticket_id,config,ledger)
            print(json.dumps({**result.__dict__,"repository_path":str(result.repository_path) if result.repository_path else None},sort_keys=True))
        elif args.command=="project-generated":
            if not args.allow_board_writes: raise PermissionError("project-generated requires --allow-board-writes")
            if not args.hermes_executable or not args.board: raise ValueError("project-generated requires --hermes-executable and --board")
            controller, _ = _registered_controller(ledger, args, allow_board_writes=True)
            result=GeneratedProjectionWorker(ledger,controller.board,worker_id="local-first-cli").deliver_one()
            print(json.dumps(result.__dict__,sort_keys=True))
        elif args.command in {"correction-plan", "create-correction-plan"}:
            service=_correction_service(ledger,args)
            plan=_correction_plan_from_file(args.plan_file,str(service.repository))
            persisted=service.create_plan(plan)  # validates + persists; never materializes
            print(json.dumps(persisted.__dict__,sort_keys=True))
        elif args.command in {"correction-materialize", "materialize-correction"}:
            service=_correction_service(ledger,args)
            materialized=service.materialize(args.correction_plan_id)  # draft tickets only; no unpause/execute
            ticket_ids=[r[0] for r in ledger.connection.execute("SELECT sct.ticket_id FROM supplemental_correction_tickets sct WHERE sct.correction_plan_id=? ORDER BY sct.ordinal",(materialized.correction_plan_id,)).fetchall()]
            print(json.dumps({"correction_plan_id": materialized.correction_plan_id, "ticket_ids": ticket_ids}, sort_keys=True))
    finally: ledger.close()
    return 0


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description="Local-first Kanban controller; dry-run is default")
    register_cli(parser)
    return run_command(parser.parse_args(argv))

if __name__=="__main__": raise SystemExit(main())
