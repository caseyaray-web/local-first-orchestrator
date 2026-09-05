from __future__ import annotations

import argparse
import json
from pathlib import Path

from .controller import LocalFirstController, RuntimeConfig
from .generated_activation import GeneratedActivationError, activate_generated_ticket
from .generated_projection import GeneratedProjectionWorker
from .hermes_board import HermesBoardAdapter
from .ledger import Ledger
from .local_qwen import LOCAL_QWEN_MODEL, LOCAL_QWEN_PROVIDER, LocalQwenAdapter
from .operator_config import ModelRegistration, OperatorConfig, default_execution_roots, load_operator_config, save_operator_config


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
    model = LocalQwenAdapter(provider=config.implementation.provider, model=config.implementation.model, hermes_home=Path.home()/".hermes"/"profiles"/config.implementation.profile, implementation_timeout_seconds=runtime.implementation_timeout_seconds, review_timeout_seconds=runtime.review_timeout_seconds)
    model.review_provider, model.review_model = config.review.provider, config.review.model
    return LocalFirstController(ledger,_board_for_cli(args,allow_board_writes),runtime,local_model=model), config


def _board_for_cli(args: argparse.Namespace, allow_board_writes: bool):
    if getattr(args, "hermes_executable", None) and getattr(args, "board", None):
        return HermesBoardAdapter(executable=args.hermes_executable, board=args.board, allow_writes=allow_board_writes)
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
    status=commands.add_parser("status"); status.add_argument("--active",action="store_true")
    imported=commands.add_parser("import"); imported.add_argument("--task-id",required=True)
    run=commands.add_parser("run-once"); run.add_argument("--task-id",required=True); run.add_argument("--dry-run",action="store_true",default=True); run.add_argument("--execute",action="store_true"); run.add_argument("--allow-board-writes",action="store_true")
    inspect=commands.add_parser("inspect"); inspect.add_argument("--task-id",required=True)
    generated=commands.add_parser("project-generated"); generated.add_argument("--allow-board-writes",action="store_true")
    activate=commands.add_parser("activate-generated"); activate.add_argument("ticket_id")
    reconcile=commands.add_parser("reconcile-failed-attempt", help="explicitly retire a blocked failed attempt; performs no cleanup or execution")
    reconcile.add_argument("--task-id", required=True)
    reconcile.add_argument("--classification", required=True, choices=("runtime_infrastructure_failure", "model_timeout", "process_error", "validation_failure", "review_exhaustion"))
    reconcile.add_argument("--operator-id", default="local-first-cli")
    reconcile.add_argument("--forensic-artifact-path", action="append", default=[], help="existing artifact root retained with the retired attempt; repeatable")
    review_resume=commands.add_parser("resume-failed-review", help="authorize a review-only retry for an unchanged validated candidate")
    review_resume.add_argument("--task-id", required=True)
    review_resume.add_argument("--operator-id", default="local-first-cli")
    state_reconcile=commands.add_parser("reconcile-state-projections", help="ledger-only: supersede stale state intents and ensure the current state intent")
    state_reconcile.add_argument("--task-id", required=True)
    confirm_cleanup=commands.add_parser("confirm-retired-attempt-cleanup", help="verify separately-authorized cleanup; never removes files")
    confirm_cleanup.add_argument("--task-id", required=True)
    confirm_cleanup.add_argument("--operator-id", default="local-first-cli")
    status.add_argument("--show-cleanup-prerequisites", action="store_true", help="read-only: tickets with retired attempts whose forensic residue must be removed before the next attempt")
    register=commands.add_parser("register-dashboard", help="persist the dashboard's one safe ledger/runtime registration")
    register.add_argument("--config-path", help="operator registration path (default: ~/.hermes/local-first-orchestrator/operator-config.json)")
    register.add_argument("--implementation-profile", default="worker-code-local")
    register.add_argument("--implementation-provider", default=LOCAL_QWEN_PROVIDER)
    register.add_argument("--implementation-model", default=LOCAL_QWEN_MODEL)
    register.add_argument("--review-profile", default="worker-code-local")
    register.add_argument("--review-provider", default=LOCAL_QWEN_PROVIDER)
    register.add_argument("--review-model", default=LOCAL_QWEN_MODEL)


def run_command(args: argparse.Namespace) -> int:
    """Run a parsed standalone or native Hermes CLI command."""
    ledger=_ledger(args.database)
    try:
        if args.command=="migrate": pass
        elif args.command=="status":
            data=ledger.status()
            if args.active: data["active"]=[dict(r) for r in ledger.connection.execute("SELECT id, external_id, state, lease_owner, lease_expires_at FROM tickets WHERE state IN ('implementing','verifying','local_review','repairing') ORDER BY updated_at")]
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
        elif args.command=="inspect": print(json.dumps(ledger.get_ticket(args.task_id),sort_keys=True))
        elif args.command=="register-dashboard":
            root=Path(args.repository).resolve(strict=True)
            allowlist=tuple(Path(item).resolve(strict=True) for item in args.allow_repository) or (root,)
            worktree_root, artifact_root = default_execution_roots(root)
            if args.worktree_root: worktree_root = Path(args.worktree_root).expanduser()
            if args.artifact_root: artifact_root = Path(args.artifact_root).expanduser()
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
            )
            path=save_operator_config(config, Path(args.config_path) if args.config_path else None)
            print(json.dumps({"registered": str(path)}, sort_keys=True))
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
            board=HermesBoardAdapter(executable=args.hermes_executable,board=args.board,allow_writes=True)
            result=GeneratedProjectionWorker(ledger,board,worker_id="local-first-cli").deliver_one()
            print(json.dumps(result.__dict__,sort_keys=True))
    finally: ledger.close()
    return 0


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description="Local-first Kanban controller; dry-run is default")
    register_cli(parser)
    return run_command(parser.parse_args(argv))

if __name__=="__main__": raise SystemExit(main())
