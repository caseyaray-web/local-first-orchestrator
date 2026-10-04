"""Fast advisory guard for ordinary managed-implementer completion mistakes.

Registered only by the eventual composition root.  CLI and dashboard writes bypass
this hook, so coordinator evidence checks remain the acceptance boundary.
"""
from __future__ import annotations

import os
from typing import Callable, Protocol


class HookContext(Protocol):
    def register_hook(self, name: str, callback: Callable[..., object]) -> object: ...


def register_hooks(ctx: HookContext, *, member_role: Callable[[str], str | None]) -> None:
    """Register a read-only pre-tool hint; never perform board or Git mutations."""
    if not callable(member_role):
        raise ValueError("trusted scoped membership lookup is required")

    def pre_tool_call(tool_name: str, args: object = None, **kwargs: object) -> dict[str, str] | None:
        if tool_name != "kanban_complete":
            return None
        task_id = os.environ.get("HERMES_KANBAN_TASK")
        run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
        if not task_id or not run_id:
            return None
        if not isinstance(args, dict) or args.get("task_id") not in (None, task_id):
            return None
        try:
            role = member_role(task_id)
        except Exception:
            return {"action": "block", "message": "Managed membership unavailable; completion needs reconciliation before retry."}
        if role != "implementation":
            return None
        return {"action": "block", "message": "Call local_first_request_local_review, obtain its exact pending review_marker from local_first_status, then call kanban_request_review from this implementation run. The later operator finalizes that native transition; completion is not local approval."}

    ctx.register_hook("pre_tool_call", pre_tool_call)


__all__ = ["register_hooks"]
