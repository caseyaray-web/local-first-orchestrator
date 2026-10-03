"""Replacement operator CLI sharing the plugin composition root."""
from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Sequence

from .composition import build_git_adapter, build_runtime, close_runtime, initialize_store, operator_planning_request
from .config import PluginConfig
from .daemon import CoordinatorLoop

_EXIT = {"verified": 0, "recorded": 0, "deduplicated": 0, "no-op": 0,
         "held": 3, "released": 0, "linked": 0, "integrated": 0,
         "accepted": 0, "approved": 0, "proposed": 0,
         "changes_requested": 0, "partial": 4, "conflict": 5,
         "unsupported": 6, "invalid": 2, "unknown": 7}


def register_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, type=Path, help="trusted versioned plugin configuration JSON")
    parser.add_argument("--board", required=True)
    parser.add_argument("--anchor-task-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("initialize-store", help="explicitly create the configured empty evidence store")
    bootstrap = commands.add_parser("bootstrap-planning", help="persist an operator-owned initial planning request")
    bootstrap.add_argument("--request-file", required=True, type=Path)
    bootstrap.add_argument("--request-id")
    commands.add_parser("enroll")
    planner_prepare = commands.add_parser("prepare-planner"); planner_prepare.add_argument("--request-id")
    planner_release = commands.add_parser("release-planner"); planner_release.add_argument("--request-id")
    accept_plan = commands.add_parser("accept-plan"); accept_plan.add_argument("--plan-id", required=True); accept_plan.add_argument("--request-id")
    prepare_piece = commands.add_parser("prepare-piece"); prepare_piece.add_argument("--plan-id", required=True); prepare_piece.add_argument("--ticket-id", required=True); prepare_piece.add_argument("--request-id")
    link_piece = commands.add_parser("link-piece"); link_piece.add_argument("--plan-id", required=True); link_piece.add_argument("--child-ticket-id", required=True); link_piece.add_argument("--parent-ticket-id", required=True); link_piece.add_argument("--request-id")
    release_piece = commands.add_parser("release-piece"); release_piece.add_argument("--plan-id", required=True); release_piece.add_argument("--ticket-id", required=True)
    integrate_piece = commands.add_parser("integrate-piece"); integrate_piece.add_argument("--plan-id", required=True); integrate_piece.add_argument("--ticket-id", required=True); integrate_piece.add_argument("--review-id", required=True)
    prepare_paid = commands.add_parser("prepare-paid-review"); prepare_paid.add_argument("--plan-id", required=True)
    release_paid = commands.add_parser("release-paid-review"); release_paid.add_argument("--plan-id", required=True)
    prepare_correction = commands.add_parser("prepare-paid-correction"); prepare_correction.add_argument("--plan-id", required=True); prepare_correction.add_argument("--review-id", required=True)
    release_correction = commands.add_parser("release-paid-correction"); release_correction.add_argument("--plan-id", required=True); release_correction.add_argument("--review-id", required=True)
    accept_tranche = commands.add_parser("accept-tranche"); accept_tranche.add_argument("--plan-id", required=True); accept_tranche.add_argument("--review-id", required=True); accept_tranche.add_argument("--authorize-successor", action="store_true", required=True)
    pause = commands.add_parser("pause"); pause.add_argument("--stop", action="store_true")
    commands.add_parser("reconcile")
    resume = commands.add_parser("resume"); resume.add_argument("--authorized-clear", action="store_true")
    commands.add_parser("cancel")
    commands.add_parser("recover")
    run = commands.add_parser("run", help="run the singleton coordinator loop until interrupted")
    run.add_argument("--once", action="store_true", help="execute one bounded polling tick for fixture/operator smoke use")


def _json_value(value: Any, *, depth: int = 0) -> Any:
    """Render bounded domain records without leaking repr-only objects to CLI JSON."""
    if depth > 12:
        return "<depth-limited>"
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(child, depth=depth + 1) for key, child in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(child, depth=depth + 1) for child in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict(), depth=depth + 1)
    if hasattr(value, "__dataclass_fields__"):
        return _json_value({name: getattr(value, name) for name in value.__dataclass_fields__}, depth=depth + 1)
    raise ValueError("CLI result contains an unsupported non-JSON record")


def render_result(result: Any) -> int:
    payload = dict(result) if isinstance(result, Mapping) else {"outcome": "unknown", "result": result}
    print(json.dumps(_json_value(payload), sort_keys=True, separators=(",", ":")))
    return _EXIT.get(str(payload.get("outcome", "unknown")), 7)


def run_command(args: argparse.Namespace) -> int:
    config = PluginConfig.from_file(args.config)
    if args.command == "initialize-store":
        if {"board_id": args.board, "anchor_task_id": args.anchor_task_id} != dict(config.scope):
            raise ValueError("initialize-store scope must match the configured board and anchor")
        return render_result({"outcome": "verified", "evidence_store": str(initialize_store(config))})
    runtime = build_runtime(config, scope={"board_id": args.board, "anchor_task_id": args.anchor_task_id})
    try:
        coordinator = runtime.coordinator
        if args.command == "status":
            result = {"outcome": "verified", "status": coordinator.status()}
        elif args.command == "pause":
            result = coordinator.pause(stop=bool(args.stop))
        elif args.command == "reconcile":
            result = coordinator.reconcile()
        elif args.command == "resume":
            result = coordinator.resume(authorized_clear=bool(args.authorized_clear))
        elif args.command == "cancel":
            result = coordinator.cancel()
        elif args.command == "recover":
            result = coordinator.recover()
        elif args.command == "bootstrap-planning":
            result = coordinator.bootstrap_planning_request(
                lambda: operator_planning_request(config, args.request_file), request_id=args.request_id)
        elif args.command == "enroll":
            result = coordinator.enroll()
        elif args.command == "prepare-planner":
            result = coordinator.prepare_planner(request_id=args.request_id)
        elif args.command == "release-planner":
            result = coordinator.release_planner(request_id=args.request_id)
        elif args.command == "accept-plan":
            result = dict(coordinator.accept_validated_plan(args.plan_id, request_id=args.request_id))
            # Older successful acceptance records predate an explicit outcome;
            # never overwrite a coordinator-held or invalid fail-closed result.
            result.setdefault("outcome", "accepted")
        elif args.command == "prepare-piece":
            result = coordinator.prepare_active_piece(args.plan_id, args.ticket_id, request_id=args.request_id)
        elif args.command == "link-piece":
            result = coordinator.execute_accepted_dependency_link(args.plan_id, args.child_ticket_id, args.parent_ticket_id,
                                                                   request_id=args.request_id)
        elif args.command == "release-piece":
            result = coordinator.release_active_piece(args.plan_id, args.ticket_id)
        elif args.command == "integrate-piece":
            result = coordinator.integrate_persisted_active_piece(args.plan_id, args.ticket_id, args.review_id,
                                                                    git_adapter=build_git_adapter(config))
        elif args.command == "prepare-paid-review":
            result = coordinator.prepare_paid_integrated_review(args.plan_id, git_adapter=build_git_adapter(config))
        elif args.command == "release-paid-review":
            result = coordinator.release_paid_integrated_review(args.plan_id, git_adapter=build_git_adapter(config))
        elif args.command == "prepare-paid-correction":
            result = coordinator.prepare_paid_correction(args.plan_id, args.review_id, git_adapter=build_git_adapter(config))
        elif args.command == "release-paid-correction":
            result = coordinator.release_paid_correction(args.plan_id, args.review_id, git_adapter=build_git_adapter(config))
        elif args.command == "accept-tranche":
            result = coordinator.accept_tranche(args.plan_id, args.review_id, git_adapter=build_git_adapter(config),
                                                 authorize_successor=args.authorize_successor)
        elif args.command == "run":
            loop = CoordinatorLoop(lambda _assert_held: coordinator.tick(), lock_path=config.lock_path,
                                   interval_seconds=config.poll_interval_seconds)
            old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            def stop(_signum: int, _frame: Any) -> None:
                loop.request_stop()
            try:
                for sig in old_handlers:
                    signal.signal(sig, stop)
                health = loop.run(max_ticks=1 if args.once else None)
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
            result = {"outcome": "verified", "health": health}
        else:
            result = {"outcome": "invalid", "reason": "unknown command"}
        return render_result(result)
    finally:
        close_runtime(runtime)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-first-orchestrator")
    register_cli(parser)
    try:
        return run_command(parser.parse_args(argv))
    except (ValueError, OSError, RuntimeError) as error:
        return render_result({"outcome": "invalid", "reason": str(error)})


__all__ = ["main", "register_cli", "run_command", "render_result"]


if __name__ == "__main__":
    raise SystemExit(main())
