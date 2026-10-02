"""Bounded structured proposal tools for native planner and review workers."""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping, Protocol
from .composition import close_runtime


TOOL_NAMES = (
    "local_first_submit_plan",
    "local_first_submit_review",
    "local_first_request_corrections",
    "local_first_report_issue",
    "local_first_status",
)
_TOOLSET = "local_first_orchestrator"


class ToolContext(Protocol):
    def register_tool(self, *, name: str, toolset: str, schema: Mapping[str, Any], handler: Callable[..., str]) -> object: ...


def _schema(name: str, description: str, properties: Mapping[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description, "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"board_id": {"type": "string"}, "anchor_task_id": {"type": "string"}, **properties},
            "required": ["board_id", "anchor_task_id", *required]}}


SCHEMAS = {
    "local_first_submit_plan": _schema("local_first_submit_plan", "Submit a structured paid planning proposal from the currently running native planner task.", {"proposal_json": {"type": "string"}, "request_id": {"type": "string"}}, ["proposal_json"]),
    "local_first_submit_review": _schema("local_first_submit_review", "Submit structured local review evidence from the current native reviewer run.", {"task_id": {"type": "string"}, "candidate": {"type": "object"}, "review": {"type": "object"}}, ["task_id", "candidate", "review"]),
    "local_first_request_corrections": _schema("local_first_request_corrections", "Reserve bounded native correction handoff from the current reviewer run; it does not create work in this tool call.", {"task_id": {"type": "string"}, "candidate": {"type": "object"}, "review": {"type": "object"}, "operation_key": {"type": "string"}, "reason": {"type": "string"}}, ["task_id", "candidate", "review", "operation_key"]),
    "local_first_report_issue": _schema("local_first_report_issue", "Record a scoped recovery issue for later bounded reconciliation; this tool does not perform repair.", {"issue": {"type": "object"}}, ["issue"]),
    "local_first_status": _schema("local_first_status", "Read scoped Local First coordinator status without changing board state.", {}, []),
}


def _json_result(call: Callable[[], Mapping[str, Any]]) -> str:
    try:
        value = dict(call())
        return json.dumps({"ok": True, "result": value}, sort_keys=True, default=str)
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        return json.dumps({"ok": False, "outcome": "invalid_or_held", "error": str(error)}, sort_keys=True)
    except Exception:
        return json.dumps({"ok": False, "outcome": "unknown", "error": "tool execution failed; reconcile through an operator command"}, sort_keys=True)


def _runtime_for_args(runtime_factory: Callable[..., Any], args: Mapping[str, Any], *, require_worker: bool) -> Any:
    if not isinstance(args, Mapping):
        raise ValueError("object arguments are required")
    requested_scope = {"board_id": args.get("board_id"), "anchor_task_id": args.get("anchor_task_id")}
    runtime = runtime_factory(requested_scope)
    scope = getattr(runtime, "scope", None)
    if not isinstance(scope, Mapping):
        raise ValueError("trusted runtime is required")
    if args.get("board_id") != scope.get("board_id") or args.get("anchor_task_id") != scope.get("anchor_task_id"):
        raise ValueError("tool scope must match the composed trusted runtime")
    if require_worker:
        task, run = os.environ.get("HERMES_KANBAN_TASK"), os.environ.get("HERMES_KANBAN_RUN_ID")
        session, board = os.environ.get("HERMES_SESSION_ID"), os.environ.get("HERMES_KANBAN_BOARD")
        if not all(type(value) is str and value for value in (task, run, session, board)):
            raise ValueError("tool requires trusted native worker task, run, session, and board environment")
        if board != scope.get("board_id"):
            raise ValueError("worker native board environment differs from composed scope")
    return runtime


def register_tools(ctx: ToolContext, *, runtime_factory: Callable[[Mapping[str, object]], Any]) -> None:
    """Register schemas only; runtime creation happens inside a tool invocation."""
    if not callable(runtime_factory):
        raise ValueError("trusted runtime factory is required")

    def submit_plan(args: Mapping[str, Any], **_: Any) -> str:
        def call() -> Mapping[str, Any]:
            runtime = _runtime_for_args(runtime_factory, args, require_worker=True)
            try:
                return runtime.coordinator.submit_plan(args["proposal_json"], request_id=args.get("request_id"))
            finally:
                close_runtime(runtime)
        return _json_result(call)

    def submit_review(args: Mapping[str, Any], **_: Any) -> str:
        from .contracts import CandidateIdentity
        def call() -> Mapping[str, Any]:
            runtime = _runtime_for_args(runtime_factory, args, require_worker=True)
            try:
                return runtime.coordinator.submit_review(args["task_id"], CandidateIdentity.from_dict(args["candidate"]), args["review"], expected_profile=runtime.config.roles["local_review_profile"])
            finally:
                close_runtime(runtime)
        return _json_result(call)

    def request_corrections(args: Mapping[str, Any], **_: Any) -> str:
        from .contracts import CandidateIdentity
        def call() -> Mapping[str, Any]:
            runtime = _runtime_for_args(runtime_factory, args, require_worker=True)
            try:
                return runtime.coordinator.request_corrections(args["task_id"], CandidateIdentity.from_dict(args["candidate"]), args["review"], implementation_profile=runtime.config.roles["implementation_profile"], operation_key=args["operation_key"], reason=args.get("reason", "review findings require correction"))
            finally:
                close_runtime(runtime)
        return _json_result(call)

    def report_issue(args: Mapping[str, Any], **_: Any) -> str:
        from .recovery import RecoveryIssue
        def call() -> Mapping[str, Any]:
            runtime = _runtime_for_args(runtime_factory, args, require_worker=True)
            try:
                issue = args["issue"]
                if not isinstance(issue, Mapping) or set(issue) != {"kind", "scope", "task_id", "run_id", "finding_id", "generation", "candidate", "details", "observed_identity"}:
                    raise ValueError("issue must be a complete RecoveryIssue object")
                return runtime.coordinator.report_issue(RecoveryIssue(**dict(issue)))
            finally:
                close_runtime(runtime)
        return _json_result(call)

    def status(args: Mapping[str, Any], **_: Any) -> str:
        def call() -> Mapping[str, Any]:
            runtime = _runtime_for_args(runtime_factory, args, require_worker=False)
            try:
                return runtime.coordinator.status()
            finally:
                close_runtime(runtime)
        return _json_result(call)

    for name, handler in (("local_first_submit_plan", submit_plan), ("local_first_submit_review", submit_review),
                          ("local_first_request_corrections", request_corrections), ("local_first_report_issue", report_issue),
                          ("local_first_status", status)):
        ctx.register_tool(name=name, toolset=_TOOLSET, schema=SCHEMAS[name], handler=handler)


__all__ = ["TOOL_NAMES", "SCHEMAS", "register_tools"]
