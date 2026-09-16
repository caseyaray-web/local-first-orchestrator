from __future__ import annotations

import os
from collections.abc import Sequence


def safe_git_env() -> dict[str, str]:
    """Return a minimal environment for internal Git operations."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "LESS": "FRX",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def safe_git_argv(args: Sequence[str]) -> tuple[str, ...]:
    """Build Git argv with execution-capable repository features disabled."""
    if not args:
        raise ValueError("git arguments must not be empty")

    safe_args = tuple(args)
    command_index = 0
    while command_index < len(safe_args) and (
        safe_args[command_index].startswith("--git-dir=")
        or safe_args[command_index].startswith("--work-tree=")
    ):
        command_index += 1
    if command_index < len(safe_args) and safe_args[command_index] == "diff":
        safe_args = (
            *safe_args[:command_index],
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            *safe_args[command_index + 1 :],
        )

    return (
        "git",
        "--no-pager",
        "-c",
        "core.pager=cat",
        "-c",
        "diff.external=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        *safe_args,
    )
