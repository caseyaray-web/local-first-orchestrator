from __future__ import annotations

import argparse
import json
from pathlib import Path

from .controller import LocalFirstController, RuntimeConfig
from .generated_projection import GeneratedProjectionWorker
from .hermes_board import HermesBoardAdapter
from .ledger import Ledger


def _ledger(path: str) -> Ledger:
    ledger=Ledger(Path(path).expanduser()); ledger.migrate(); return ledger


def _controller(ledger: Ledger, args: argparse.Namespace, *, allow_board_writes: bool=False) -> LocalFirstController:
    root=Path(args.repository).resolve()
    allowlist=tuple(Path(item).resolve() for item in args.allow_repository)
    if not allowlist: allowlist=(root,)
    return LocalFirstController(ledger,HermesBoardAdapter(allow_writes=allow_board_writes),RuntimeConfig(root,root/".hermes/local-first-worktrees",root/".hermes/local-first-artifacts",repository_allowlist=allowlist))


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description="Local-first Kanban controller; dry-run is default")
    parser.add_argument("--database",required=True,help="separate ledger database; never Hermes kanban.db")
    parser.add_argument("--repository",default=".",help="canonical repository root (required for import/run-once)")
    parser.add_argument("--allow-repository",action="append",default=[],help="exact canonical repository root allowed for imports/execution; repeatable")
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("migrate")
    status=commands.add_parser("status"); status.add_argument("--active",action="store_true")
    imported=commands.add_parser("import"); imported.add_argument("--task-id",required=True)
    run=commands.add_parser("run-once"); run.add_argument("--task-id",required=True); run.add_argument("--dry-run",action="store_true",default=True); run.add_argument("--execute",action="store_true"); run.add_argument("--allow-board-writes",action="store_true")
    inspect=commands.add_parser("inspect"); inspect.add_argument("--task-id",required=True)
    generated=commands.add_parser("project-generated"); generated.add_argument("--allow-board-writes",action="store_true"); generated.add_argument("--hermes-executable"); generated.add_argument("--board")
    args=parser.parse_args(argv); ledger=_ledger(args.database)
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
        elif args.command=="project-generated":
            if not args.allow_board_writes: raise PermissionError("project-generated requires --allow-board-writes")
            if not args.hermes_executable or not args.board: raise ValueError("project-generated requires --hermes-executable and --board")
            board=HermesBoardAdapter(executable=args.hermes_executable,board=args.board,allow_writes=True)
            result=GeneratedProjectionWorker(ledger,board,worker_id="local-first-cli").deliver_one()
            print(json.dumps(result.__dict__,sort_keys=True))
    finally: ledger.close()
    return 0

if __name__=="__main__": raise SystemExit(main())
