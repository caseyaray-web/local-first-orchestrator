"""Native Hermes registration for the local-first orchestration CLI.

The controller remains a standalone package.  This thin adapter only exposes
the identical parser and handler at ``hermes local-first-orchestrator``.
"""

from local_first_orchestrator.cli import register_cli, run_command


def register(ctx) -> None:
    """Register the operator CLI without adding model tools or hooks."""
    ctx.register_cli_command(
        name="local-first-orchestrator",
        help="Operate the Local First Hermes-native orchestration runtime",
        setup_fn=register_cli,
        handler_fn=run_command,
        description=(
            "Operator CLI for durable Local First orchestration, recovery, planning, "
            "validation, integration, and metrics. Dry-run is the default; board "
            "writes require explicit flags."
        ),
    )