"""Reusable real-runtime driver for bounded M7 CLI and worker-tool lifecycle tests."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any, Mapping

from local_first_orchestrator import cli, plugin_tools
from local_first_orchestrator.composition import Runtime, build_runtime, close_runtime
from local_first_orchestrator.config import PluginConfig


class PublicLifecycleDriver:
    """Dispatch M7 public contracts into one real Coordinator and SQLite store.

    The board transport may be a disposable native-shaped test transport, but the
    runtime, coordinator, store, command parser, tool schemas, and Git adapter
    are all the production implementations.
    """

    def __init__(self, tmp_path: Path, monkeypatch: Any, *, board: Any,
                 scope: Mapping[str, str], repository: Path, config_path: Path,
                 config: PluginConfig | None = None) -> None:
        if config is None:
            state = tmp_path / "m7-state"; state.mkdir(mode=0o700)
            home = tmp_path / "m7-home"; home.mkdir(mode=0o700)
            executable = tmp_path / "m7-check"; executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8"); executable.chmod(0o700)
            config = PluginConfig.from_mapping({
                "version": 1, "state_root": str(state), "hermes_executable": str(executable),
                "hermes_home": str(home), "kanban_home": str(home),
                "trusted_roots": {"repository": str(repository), "workspace": str(repository)},
                "roles": {"implementation_profile": "implementer", "local_review_profile": "local",
                          "planning_profile": "planner", "paid_review_profile": "paid"},
                "budgets": {"implementation_attempts": 5, "review_corrections": 5,
                            "infrastructure_retries": 5, "workflow_repairs": 5, "paid_capacity": 5},
                "poll_interval_seconds": 1,
                "scope": dict(scope),
                "check_commands": [{"check_id": "smoke", "argv": [str(executable)]}],
            })
        self.config = config
        self.config_path = config_path
        self.board = board
        self._base_claim_create_attempt = getattr(board, "claim_create_attempt", None)
        self.scope = dict(scope)
        self.runtime_opens = 0
        self.runtime_closes = 0
        self._registered: dict[str, Mapping[str, Any]] = {}
        # The public parser loads the fixture's actual JSON bootstrap.  The sole
        # injected boundary is the disposable native-shaped board transport.
        monkeypatch.setattr(cli, "build_runtime", lambda _config, *, scope: self._runtime_for_scope(scope))
        def close(runtime: Runtime) -> None:
            self.runtime_closes += 1
            close_runtime(runtime)
        monkeypatch.setattr(cli, "close_runtime", close)
        monkeypatch.setattr(plugin_tools, "close_runtime", close)

        class Context:
            def register_tool(_self, **kwargs: Any) -> None:
                self._registered[kwargs["name"]] = kwargs
        plugin_tools.register_tools(Context(), runtime_factory=self._runtime_for_scope)

    def _runtime_for_scope(self, scope: Mapping[str, object]) -> Runtime:
        if dict(scope) != self.scope:
            raise ValueError("driver scope differs from configured scope")
        self.runtime_opens += 1
        runtime = build_runtime(PluginConfig.from_file(self.config_path), board=self.board, scope=scope)
        if hasattr(self, "git_observer"):
            runtime.coordinator.git_observer = self.git_observer
        if hasattr(self, "combined_check_runner"):
            runtime.coordinator.combined_check_runner = self.combined_check_runner
        return runtime

    def command(self, command: str, *selectors: str) -> dict[str, Any]:
        output = io.StringIO()
        argv = ("--config", str(self.config_path), "--board", self.scope["board_id"],
                "--anchor-task-id", self.scope["anchor_task_id"], command, *selectors)
        try:
            with contextlib.redirect_stdout(output):
                exit_code = cli.main(argv)
        finally:
            # Fresh composition installs its lock-bound adapter claim hook.  The
            # retained read coordinator uses its own hook for direct assertions.
            self.board.claim_create_attempt = self._base_claim_create_attempt
        result = json.loads(output.getvalue())
        # Held creation is an intentional successful lifecycle state and maps to
        # CLI exit 3; the structured coordinator result remains the authority.
        assert exit_code in {0, 3, 4, 7}, result
        return result

    def tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        payload = {"board_id": self.scope["board_id"], "anchor_task_id": self.scope["anchor_task_id"], **arguments}
        try:
            result = json.loads(self._registered[name]["handler"](payload))
        finally:
            self.board.claim_create_attempt = self._base_claim_create_attempt
        assert result["ok"], result
        return dict(result["result"])

    def enroll(self) -> dict[str, Any]:
        return self.command("enroll")

    def bootstrap_planning(self, request_file: Path, request_id: str) -> dict[str, Any]:
        return self.command("bootstrap-planning", "--request-file", str(request_file), "--request-id", request_id)

    def pause(self) -> dict[str, Any]:
        return self.command("pause")

    def resume(self) -> dict[str, Any]:
        return self.command("resume", "--authorized-clear")

    def cancel(self) -> dict[str, Any]:
        return self.command("cancel")

    def prepare_planner(self, request_id: str) -> dict[str, Any]:
        return self.command("prepare-planner", "--request-id", request_id)

    def release_planner(self, request_id: str) -> dict[str, Any]:
        return self.command("release-planner", "--request-id", request_id)

    def register_planning_request(self, request_id: str) -> dict[str, Any]:
        return self.tool("local_first_register_planning_request", {"request_id": request_id})

    def submit_plan(self, proposal_json: str, request_id: str) -> dict[str, Any]:
        return self.tool("local_first_submit_plan", {"proposal_json": proposal_json, "request_id": request_id})

    def accept_plan(self, plan_id: str, request_id: str) -> dict[str, Any]:
        result = self.command("accept-plan", "--plan-id", plan_id, "--request-id", request_id)
        self._accepted_request_id = request_id
        return result

    def prepare_piece(self, plan_id: str, ticket_id: str) -> dict[str, Any]:
        request_id = getattr(self, "_accepted_request_id", None)
        selectors = ("--plan-id", plan_id, "--ticket-id", ticket_id)
        if request_id is not None:
            selectors += ("--request-id", request_id)
        return self.command("prepare-piece", *selectors)

    def link_piece(self, plan_id: str, child_ticket_id: str, parent_ticket_id: str) -> dict[str, Any]:
        selectors = ("--plan-id", plan_id, "--child-ticket-id", child_ticket_id, "--parent-ticket-id", parent_ticket_id)
        request_id = getattr(self, "_accepted_request_id", None)
        if request_id is not None:
            selectors += ("--request-id", request_id)
        return self.command("link-piece", *selectors)

    def release_piece(self, plan_id: str, ticket_id: str) -> dict[str, Any]:
        return self.command("release-piece", "--plan-id", plan_id, "--ticket-id", ticket_id)

    def submit_local_review(self, task_id: str, candidate: Any, review: Mapping[str, Any]) -> dict[str, Any]:
        import os
        native = review["native_review"]
        os.environ.update({"HERMES_KANBAN_TASK": native["task_id"], "HERMES_KANBAN_RUN_ID": native["run_id"],
                           "HERMES_SESSION_ID": native["session_id"], "HERMES_KANBAN_BOARD": self.scope["board_id"]})
        self.git_observer = lambda _scope: {
            "candidate": candidate.to_dict(), "checks": review["checks"],
            "checks_identity": review["checks_identity"], "criterion_ids": ["AC-1"],
        }
        return self.tool("local_first_submit_review", {"task_id": task_id, "candidate": candidate.to_dict(), "review": review})

    def integrate_piece(self, plan_id: str, ticket_id: str, review_id: str) -> dict[str, Any]:
        return self.command("integrate-piece", "--plan-id", plan_id, "--ticket-id", ticket_id,
                            "--review-id", review_id)

    def prepare_paid_review(self, plan_id: str) -> dict[str, Any]:
        return self.command("prepare-paid-review", "--plan-id", plan_id)

    def release_paid_review(self, plan_id: str) -> dict[str, Any]:
        return self.command("release-paid-review", "--plan-id", plan_id)

    def submit_paid_review(self, plan_id: str, review: Mapping[str, Any]) -> dict[str, Any]:
        # The test transport supplies synthetic worker identities, while the tool
        # still derives and validates them exclusively from the worker environment.
        native = review["native_review"]
        import os
        os.environ.update({"HERMES_KANBAN_TASK": native["task_id"], "HERMES_KANBAN_RUN_ID": native["run_id"],
                           "HERMES_SESSION_ID": native["session_id"], "HERMES_KANBAN_BOARD": self.scope["board_id"]})
        return self.tool("local_first_submit_paid_review", {"plan_id": plan_id, "review": review})

    def prepare_paid_correction(self, plan_id: str, review_id: str) -> dict[str, Any]:
        return self.command("prepare-paid-correction", "--plan-id", plan_id, "--review-id", review_id)

    def release_paid_correction(self, plan_id: str, review_id: str) -> dict[str, Any]:
        return self.command("release-paid-correction", "--plan-id", plan_id, "--review-id", review_id)

    def accept_tranche(self, plan_id: str, review_id: str) -> dict[str, Any]:
        return self.command("accept-tranche", "--plan-id", plan_id, "--review-id", review_id,
                            "--authorize-successor")
