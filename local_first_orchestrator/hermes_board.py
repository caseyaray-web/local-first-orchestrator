from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

from .comment_delivery import MarkerLookup
from .execution_handoff import HANDOFF_MARKER
from .states import CanonicalState


@dataclass(frozen=True)
class ExternalTicket:
    id: str
    title: str
    body: str
    status: str
    workspace_path: str | None
    parents: tuple[str, ...] = ()
    children: tuple[str, ...] = ()
    assignee: str | None = None
    workspace_kind: str | None = None


@dataclass(frozen=True)
class ExternalExecutionRun:
    id: int
    status: str
    outcome: str | None
    started_at: int | None
    ended_at: int | None
    summary: str | None
    profile: str | None
    worker_pid: int | None
    metadata: Any = None


@dataclass(frozen=True)
class ExternalExecutionSnapshot:
    task: ExternalTicket
    session_id: str | None
    branch_name: str | None
    started_at: int | None
    completed_at: int | None
    runs: tuple[ExternalExecutionRun, ...]


class HermesBoardAdapter:
    """Hermes CLI adapter. Writes require explicit opt-in; reads are always safe."""
    is_fake = False

    def __init__(self, *, board: str, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, executable: str, allow_writes: bool = False, timeout_seconds: int = 15, output_limit: int = 200_000, implementation_profile: str | None = None, canonical_repository: Path | None = None) -> None:
        if timeout_seconds < 1 or output_limit < 1: raise ValueError("positive process limits required")
        if not board or not board.replace("-", "").replace("_", "").isalnum(): raise ValueError("explicit board slug required")
        path=Path(executable)
        if not path.is_absolute() or not path.is_file(): raise ValueError("Hermes executable must be an absolute existing path")
        if implementation_profile is not None and (not implementation_profile.strip() or any(c.isspace() for c in implementation_profile)):
            raise ValueError("implementation profile must be a non-empty token")
        self.runner, self.executable, self.board, self.allow_writes, self.timeout_seconds, self.output_limit = runner, str(path), board, allow_writes, timeout_seconds, output_limit
        self.implementation_profile = implementation_profile
        self.canonical_repository = None if canonical_repository is None else Path(canonical_repository).expanduser().resolve()

    def _run(self, *args: str) -> Any:
        try:
            completed = self.runner((self.executable, "kanban", "--board", self.board, *args), text=True, capture_output=True, timeout=self.timeout_seconds, check=False, env={**os.environ,"NO_COLOR":"1","GIT_TERMINAL_PROMPT":"0"})
        except (OSError, subprocess.TimeoutExpired) as exc: raise RuntimeError("Hermes Kanban read unavailable") from exc
        if len(completed.stdout or "") > self.output_limit or len(completed.stderr or "") > self.output_limit: raise RuntimeError("Hermes Kanban output exceeded bound")
        if completed.returncode: raise RuntimeError((completed.stderr or completed.stdout or "Hermes Kanban CLI failed")[:2000])
        if "--json" not in args: return completed.stdout[:self.output_limit]
        try: return json.loads(completed.stdout[:self.output_limit])
        except json.JSONDecodeError as exc: raise RuntimeError("Hermes Kanban malformed JSON") from exc

    def get_task(self, task_id: str) -> ExternalTicket:
        payload = self._run("show", task_id, "--json")
        row = payload.get("task") if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            raise KeyError(f"Hermes task not found: {task_id}")
        parents = payload.get("parents", []) if isinstance(payload, dict) else []
        children = payload.get("children", []) if isinstance(payload, dict) else []
        if not isinstance(parents, list) or not all(isinstance(value, str) and value for value in parents):
            raise RuntimeError("Hermes Kanban malformed parent graph")
        if not isinstance(children, list) or not all(isinstance(value, str) and value for value in children):
            raise RuntimeError("Hermes Kanban malformed child graph")
        return ExternalTicket(
            str(row["id"]),
            str(row.get("title") or ""),
            str(row.get("body") or ""),
            str(row.get("status") or ""),
            row.get("workspace_path"),
            tuple(sorted(set(parents))),
            tuple(sorted(set(children))),
            None if row.get("assignee") is None else str(row["assignee"]),
            None if row.get("workspace_kind") is None else str(row["workspace_kind"]),
        )

    def execution_snapshot(self, task_id: str) -> ExternalExecutionSnapshot:
        payload = self._run("show", task_id, "--json")
        row = payload.get("task") if isinstance(payload, dict) else None
        runs = payload.get("runs") if isinstance(payload, dict) else None
        if not isinstance(row, dict) or not isinstance(runs, list):
            raise RuntimeError("Hermes execution snapshot is malformed")
        parents = payload.get("parents", [])
        children = payload.get("children", [])
        if not isinstance(parents, list) or not all(isinstance(value, str) and value for value in parents):
            raise RuntimeError("Hermes execution snapshot parent graph is malformed")
        if not isinstance(children, list) or not all(isinstance(value, str) and value for value in children):
            raise RuntimeError("Hermes execution snapshot child graph is malformed")
        task = ExternalTicket(
            str(row["id"]),
            str(row.get("title") or ""),
            str(row.get("body") or ""),
            str(row.get("status") or ""),
            row.get("workspace_path"),
            tuple(sorted(set(parents))),
            tuple(sorted(set(children))),
            None if row.get("assignee") is None else str(row["assignee"]),
            None if row.get("workspace_kind") is None else str(row["workspace_kind"]),
        )
        parsed: list[ExternalExecutionRun] = []
        for item in runs:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                raise RuntimeError("Hermes execution run is malformed")
            parsed.append(ExternalExecutionRun(
                id=int(item["id"]),
                status=str(item.get("status") or ""),
                outcome=None if item.get("outcome") is None else str(item["outcome"]),
                started_at=None if item.get("started_at") is None else int(item["started_at"]),
                ended_at=None if item.get("ended_at") is None else int(item["ended_at"]),
                summary=None if item.get("summary") is None else str(item["summary"]),
                profile=None if item.get("profile") is None else str(item["profile"]),
                worker_pid=None if item.get("worker_pid") is None else int(item["worker_pid"]),
                metadata=item.get("metadata"),
            ))
        return ExternalExecutionSnapshot(
            task=task,
            session_id=None if row.get("session_id") is None else str(row["session_id"]),
            branch_name=None if row.get("branch_name") is None else str(row["branch_name"]),
            started_at=None if row.get("started_at") is None else int(row["started_at"]),
            completed_at=None if row.get("completed_at") is None else int(row["completed_at"]),
            runs=tuple(sorted(parsed, key=lambda run: run.id)),
        )

    def import_candidates(self) -> list[ExternalTicket]:
        rows = self._run("list", "--json", "--status", "scheduled")
        if not isinstance(rows, list):
            raise RuntimeError("Kanban list JSON must be an array")
        candidates = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            body = str(row.get("body") or "")
            if "<!-- local-first-orchestrator -->" not in body:
                continue
            candidates.append(ExternalTicket(str(row["id"]), str(row.get("title") or ""), body, str(row.get("status") or ""), row.get("workspace_path")))
        return candidates

    def find_comment_marker(self, external_task_id: str, marker: str) -> MarkerLookup:
        payload = self._run("show", external_task_id, "--json")
        comments = payload.get("comments", []) if isinstance(payload, dict) else []
        if not isinstance(comments, list):
            return MarkerLookup.UNAVAILABLE
        for row in comments:
            if isinstance(row, dict) and marker in str(row.get("body") or ""):
                return MarkerLookup.FOUND
        return MarkerLookup.NOT_FOUND

    def set_state(self, ticket_id: str, state: CanonicalState, *, idempotency_key: str, expected_routing: dict[str, str] | None = None) -> None:
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        task = self.get_task(ticket_id)
        if expected_routing is not None:
            self.verify_native_release_task(task, expected_workspace_path=expected_routing["workspace_path"])
            if {"profile": task.assignee, "workspace_kind": task.workspace_kind, "workspace_path": task.workspace_path} != {
                "profile": expected_routing.get("profile"), "workspace_kind": expected_routing.get("workspace_kind"), "workspace_path": expected_routing.get("workspace_path")
            }:
                raise RuntimeError("native release authority mismatch")
        current = task.status
        handoff = HANDOFF_MARKER in task.body
        if state == CanonicalState.DONE:
            if current == "done":
                return
            if current == "scheduled":
                self._run("unblock", ticket_id, "--reason", f"local-first completion {idempotency_key}")
            self._run("complete", ticket_id, "--result", f"local-first projection {idempotency_key}")
        elif handoff and state in {CanonicalState.READY_LOCAL, CanonicalState.REPAIRING}:
            if current == "ready":
                return
            if current in {"blocked", "scheduled", "todo"}:
                self._run("unblock", ticket_id, "--reason", f"local-first execution released {idempotency_key}")
                return
            if current == "running":
                return
            raise RuntimeError(f"Hermes execution handoff release has incompatible state: {current}")
        elif handoff:
            if current == "blocked":
                return
            if current == "done":
                raise RuntimeError("Hermes execution handoff became final before Local First completion")
            if current in {"running", "ready", "todo", "scheduled"}:
                raise RuntimeError(f"Hermes execution handoff is not parked for Local First trust stage: {current}")
            return
        elif state in {CanonicalState.BLOCKED, CanonicalState.NEEDS_TRIAGE, CanonicalState.NEEDS_CHECKPOINT, CanonicalState.NEEDS_HUMAN_TEST}:
            if current == "blocked":
                return
            self._run("block", ticket_id, f"local-first projection {state.value} ({idempotency_key})", "--kind", "needs_input")
        else:
            # Scheduled is deliberately non-dispatchable by Hermes; the local-first
            # controller owns execution and avoids racing the gateway dispatcher.
            if current == "scheduled":
                return
            self._run("schedule", ticket_id, f"local-first projection {state.value} ({idempotency_key})")

    def create_microticket(self, title: str, body: str, *, idempotency_key: str) -> str:
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        args = ["create", title, "--body", body]
        if self.implementation_profile is not None:
            if self.canonical_repository is None:
                raise RuntimeError("native release authority missing canonical repository")
            args += ["--assignee", self.implementation_profile, "--workspace", f"worktree:{self.canonical_repository}"]
        else:
            args += ["--workspace", "worktree"]
        args += ["--idempotency-key", idempotency_key, "--initial-status", "blocked", "--json"]
        payload = self._run(*args)
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not payload["id"]:
            raise RuntimeError("Hermes create JSON missing task id")
        return payload["id"]

    def verify_native_release_task(self, task: ExternalTicket, *, expected_workspace_path: str) -> dict[str, str]:
        """Verify the externally resolved handoff against operator-owned authority."""
        if self.implementation_profile is None or self.canonical_repository is None:
            raise RuntimeError("native release authority is not configured")
        expected = str(Path(expected_workspace_path).expanduser().resolve())
        actual = None if task.workspace_path is None else str(Path(task.workspace_path).expanduser().resolve())
        expected_root = (self.canonical_repository / ".worktrees").resolve()
        try:
            Path(expected).relative_to(expected_root)
        except ValueError as exc:
            raise RuntimeError("native release authority mismatch") from exc
        if task.assignee != self.implementation_profile or task.workspace_kind != "worktree" or actual != expected:
            raise RuntimeError("native release authority mismatch")
        assert actual is not None
        return {"profile": self.implementation_profile, "workspace_kind": "worktree", "workspace_path": actual}

    def link_dependency(self, parent_task_id: str, child_task_id: str) -> None:
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        if not parent_task_id or not child_task_id or parent_task_id == child_task_id:
            raise ValueError("valid distinct dependency task ids required")
        self._run("link", parent_task_id, child_task_id)

    def add_comment(self, ticket_id: str, comment: str) -> None:
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        self._run("comment", ticket_id, comment, "--author", "local-first-orchestrator")

    def deliver_comment(self, ticket_id: str, comment: str, *, idempotency_key: str) -> None:
        """Comment-worker boundary; the persisted marker supplies reconciliation identity."""
        self.add_comment(ticket_id, comment)
