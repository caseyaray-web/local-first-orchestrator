from __future__ import annotations

import argparse
import json
from pathlib import Path

from .controller import LocalFirstController, RuntimeConfig
from .generated_activation import GeneratedActivationError, activate_generated_ticket
from .generated_projection import GeneratedProjectionWorker
from .hermes_board import HermesBoardAdapter
from .ledger import Ledger
from .local_qwen import LOCAL_QWEN_MODEL, LOCAL_QWEN_PROVIDER
from .operator_config import ModelRegistration, OperatorConfig, default_execution_roots, save_operator_config


def _ledger(path: str) -> Ledger:
    ledger=Ledger(Path(path).expanduser()); ledger.migrate(); return ledger


def _controller(ledger: Ledger, args: argparse.Namespace, *, allow_board_writes: bool=False) -> LocalFirstController:
    root=Path(args.repository).resolve()
    allowlist=tuple(Path(item).resolve() for item in args.allow_repository)
    if not allowlist: allowlist=(root,)
    worktree_root, artifact_root = default_execution_roots(root)
    if args.worktree_root: worktree_root = Path(args.worktree_root).expanduser()
    if args.artifact_root: artifact_root = Path(args.artifact_root).expanduser()
    return LocalFirstController(ledger,HermesBoardAdapter(allow_writes=allow_board_writes),RuntimeConfig(root,worktree_root,artifact_root,repository_allowlist=allowlist,implementation_timeout_seconds=args.implementation_timeout_seconds))


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Add the standalone CLI's arguments to *parser*.

    Hermes invokes this through ``ctx.register_cli_command`` so the native
    ``hermes local-first-orchestrator`` command and the installed standalone
    ``local-first-orchestrator`` command share one parser contract.
    """
    parser.add_argument("--database",required=True,help="separate ledger database; never Hermes kanban.db")
    parser.add_argument("--repository",default=".",help="canonical repository root (required for import/run-once)")
    parser.add_argument("--allow-repository",action="append",default=[],help="exact canonical repository root allowed for imports/execution; repeatable")
    parser.add_argument("--worktree-root", help="external worktree root; defaults to stable ~/.hermes namespace")
    parser.add_argument("--artifact-root", help="external artifact root; defaults to stable ~/.hermes namespace")
    parser.add_argument("--implementation-timeout-seconds", type=int, default=300, help="implementation timeout (1..21600 seconds)")
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("migrate")
    status=commands.add_parser("status"); status.add_argument("--active",action="store_true")
    imported=commands.add_parser("import"); imported.add_argument("--task-id",required=True)
    run=commands.add_parser("run-once"); run.add_argument("--task-id",required=True); run.add_argument("--dry-run",action="store_true",default=True); run.add_argument("--execute",action="store_true"); run.add_argument("--allow-board-writes",action="store_true")
    inspect=commands.add_parser("inspect"); inspect.add_argument("--task-id",required=True)
    generated=commands.add_parser("project-generated"); generated.add_argument("--allow-board-writes",action="store_true"); generated.add_argument("--hermes-executable"); generated.add_argument("--board")
    activate=commands.add_parser("activate-generated"); activate.add_argument("ticket_id")
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
            print(json.dumps(data,sort_keys=True))
        elif args.command=="import":
            ctl=_controller(ledger,args); print(json.dumps({"ticket_id":ctl.import_card(ctl.board.get_task(args.task_id))}))
        elif args.command=="run-once":
            ctl=_controller(ledger,args,allow_board_writes=args.allow_board_writes)
            if not args.execute: print(json.dumps(ctl.dry_run(args.task_id),sort_keys=True))
            elif not args.allow_board_writes: raise PermissionError("--execute requires --allow-board-writes; no write-enabled execution without both")
            else: print(json.dumps({"executed":ctl.execute(args.task_id,repository=Path(args.repository),allow_board_writes=True)}))
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
                implementation_timeout_seconds=args.implementation_timeout_seconds,
            )
            path=save_operator_config(config, Path(args.config_path) if args.config_path else None)
            print(json.dumps({"registered": str(path)}, sort_keys=True))
        elif args.command=="activate-generated":
            root=Path(args.repository).resolve(); allowlist=tuple(Path(item).resolve() for item in args.allow_repository) or (root,)
            worktree_root, artifact_root = default_execution_roots(root)
            config=RuntimeConfig(root,worktree_root,artifact_root,repository_allowlist=allowlist,implementation_timeout_seconds=args.implementation_timeout_seconds)
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
