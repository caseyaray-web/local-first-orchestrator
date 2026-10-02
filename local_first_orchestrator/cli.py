"""Replacement operator CLI sharing the plugin composition root."""
from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path
from typing import Any, Sequence

from .composition import build_runtime, close_runtime, initialize_store
from .config import PluginConfig
from .daemon import CoordinatorLoop

_EXIT = {"verified": 0, "recorded": 0, "deduplicated": 0, "no-op": 0, "held": 3,
         "partial": 4, "conflict": 5, "unsupported": 6, "invalid": 2, "unknown": 7}


def register_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, type=Path, help="trusted versioned plugin configuration JSON")
    parser.add_argument("--board", required=True)
    parser.add_argument("--anchor-task-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("initialize-store", help="explicitly create the configured empty evidence store")
    commands.add_parser("enroll")
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
    if isinstance(value, dict):
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
    payload = result if isinstance(result, dict) else {"outcome": "unknown", "result": result}
    print(json.dumps(_json_value(payload), sort_keys=True, separators=(",", ":")))
    return _EXIT.get(str(payload.get("outcome", "unknown")), 7)


def run_command(args: argparse.Namespace) -> int:
    config = PluginConfig.from_file(args.config)
    if args.command == "initialize-store":
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
        elif args.command == "enroll":
            result = coordinator.enroll()
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
