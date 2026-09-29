"""Advisory guidance only; acceptance never depends on this hook."""
import os
from local_first_orchestrator.plugin_hooks import register_hooks


class Context:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def test_managed_implementer_completion_is_guided_to_native_review(monkeypatch):
    ctx = Context()
    register_hooks(ctx, member_role=lambda task: "implementation" if task == "piece" else None)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    result = ctx.hooks["pre_tool_call"]("kanban_complete", {"task_id": "piece"}, session_id="session")
    assert result["action"] == "block"
    assert "kanban_request_review" in result["message"]


def test_unmanaged_and_review_worker_completions_are_not_blocked(monkeypatch):
    ctx = Context()
    register_hooks(ctx, member_role=lambda task: {"piece": "implementation", "review": "local_review"}.get(task))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    for task in ("other", "review"):
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
        assert ctx.hooks["pre_tool_call"]("kanban_complete", {"task_id": task}, session_id="s") is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    assert ctx.hooks["pre_tool_call"]("kanban_request_review", {"task_id": "piece"}, session_id="s") is None


def test_ambiguous_membership_does_not_grant_approval(monkeypatch):
    ctx = Context()
    register_hooks(ctx, member_role=lambda task: (_ for _ in ()).throw(RuntimeError("store unavailable")))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    result = ctx.hooks["pre_tool_call"]("kanban_complete", {"task_id": "piece"}, session_id="s")
    assert result["action"] == "block"
    assert "unavailable" in result["message"].lower()
