from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
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
    repository_identity: str | None = None
    base_sha: str | None = None


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
    repository_identity: str | None = None
    base_sha: str | None = None


class _BoardRevalidationCapability:
    """Adapter-owned proof that an IMMEDIATE transaction is still held."""

    __slots__ = ("snapshot", "_adapter", "_connection", "_token", "_task_id", "_active")

    def __init__(self, adapter: "HermesBoardAdapter", connection: sqlite3.Connection, task_id: str, snapshot: ExternalExecutionSnapshot, token: object) -> None:
        self.snapshot, self._adapter, self._connection, self._token = snapshot, adapter, connection, token
        self._task_id, self._active = task_id, True

    def _verify_for_ledger(self, task_id: str, external_task_id: str) -> None:
        if not self._active or self._token is not self._adapter._revalidation_token:
            raise PermissionError("trusted board revalidation capability is inactive or unproven")
        if task_id != self._task_id or external_task_id != self.snapshot.task.id:
            raise PermissionError("trusted board revalidation capability identity mismatch")
        current = self._adapter._snapshot_from_connection(self._connection, self._task_id)
        if current != self.snapshot:
            raise RuntimeError("native release revalidation board snapshot drift before ledger commit")

    def close(self) -> None:
        self._active = False


class HermesBoardAdapter:
    """Hermes CLI adapter. Writes require explicit opt-in; reads are always safe."""
    is_fake = False

    def __init__(self, *, board: str, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, executable: str, allow_writes: bool = False, timeout_seconds: int = 15, output_limit: int = 200_000, implementation_profile: str | None = None, canonical_repository: Path | None = None, board_db_path: Path | None = None) -> None:
        if timeout_seconds < 1 or output_limit < 1: raise ValueError("positive process limits required")
        if not board or not board.replace("-", "").replace("_", "").isalnum(): raise ValueError("explicit board slug required")
        path=Path(executable)
        if not path.is_absolute() or not path.is_file(): raise ValueError("Hermes executable must be an absolute existing path")
        if implementation_profile is not None and (not implementation_profile.strip() or any(c.isspace() for c in implementation_profile)):
            raise ValueError("implementation profile must be a non-empty token")
        self.runner, self.executable, self.board, self.allow_writes, self.timeout_seconds, self.output_limit = runner, str(path), board, allow_writes, timeout_seconds, output_limit
        self.implementation_profile = implementation_profile
        self.canonical_repository = None if canonical_repository is None else Path(canonical_repository).expanduser().resolve()
        self.board_db_path = None if board_db_path is None else Path(board_db_path).expanduser().resolve()
        self._revalidation_token: object | None = None

    def _resolved_board_db_path(self) -> Path:
        if self.board_db_path is not None:
            path = self.board_db_path
        else:
            try:
                from hermes_cli import kanban_db
                path = Path(kanban_db.kanban_db_path(self.board))
            except Exception as exc:
                raise RuntimeError("trusted board revalidation cannot resolve Hermes Kanban DB") from exc
        try:
            path = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("trusted board revalidation requires an existing local SQLite board DB") from exc
        if not path.is_file():
            raise RuntimeError("trusted board revalidation requires an existing local SQLite board DB")
        return path

    @staticmethod
    def _snapshot_from_connection(connection: sqlite3.Connection, task_id: str) -> ExternalExecutionSnapshot:
        try:
            from hermes_cli import kanban_db
            task = kanban_db.get_task(connection, task_id)
            if task is None:
                raise KeyError(task_id)
            raw_task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            parents = tuple(sorted(str(row["parent_id"]) for row in connection.execute("SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id", (task_id,))))
            children = tuple(sorted(str(row["child_id"]) for row in connection.execute("SELECT child_id FROM task_links WHERE parent_id=? ORDER BY child_id", (task_id,))))
            runs = kanban_db.list_runs(connection, task_id)
        except (sqlite3.DatabaseError, KeyError, AttributeError, TypeError) as exc:
            raise RuntimeError("Hermes Kanban execution snapshot read failed") from exc
        keys = set(raw_task.keys()) if raw_task is not None else set()
        optional = lambda name, fallback=None: raw_task[name] if raw_task is not None and name in keys else fallback
        external_runs = tuple(ExternalExecutionRun(int(run.id), str(run.status or ""), None if run.outcome is None else str(run.outcome), int(run.started_at) if run.started_at is not None else None, None if run.ended_at is None else int(run.ended_at), None if run.summary is None else str(run.summary), None if run.profile is None else str(run.profile), None if run.worker_pid is None else int(run.worker_pid), run.metadata) for run in runs)
        return ExternalExecutionSnapshot(
            task=ExternalTicket(str(task.id), str(task.title or ""), str(task.body or ""), str(task.status or ""), task.workspace_path, parents=parents, children=children, assignee=task.assignee, workspace_kind=task.workspace_kind, repository_identity=optional("repository_identity"), base_sha=optional("base_sha")),
            session_id=optional("session_id", task.session_id), branch_name=task.branch_name, started_at=task.started_at, completed_at=task.completed_at,
            runs=external_runs, repository_identity=optional("repository_identity"), base_sha=optional("base_sha"),
        )

    @contextmanager
    def revalidation(self, task_id: str):
        """Hold the exact Hermes board write lock across the caller's ledger commit."""
        path = self._resolved_board_db_path()
        try:
            from hermes_cli.sqlite_util import open_db
            connection = open_db(path, db_label=f"kanban:{self.board}", busy_timeout_ms=int(self.timeout_seconds * 1000), wal=False, check_same_thread=False)
        except Exception as exc:
            raise RuntimeError("trusted board revalidation cannot open the configured local SQLite board") from exc
        token = object()
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._snapshot_from_connection(connection, task_id)
            self._revalidation_token = token
            capability = _BoardRevalidationCapability(self, connection, task_id, snapshot, token)
            try:
                yield capability
            finally:
                capability.close()
                self._revalidation_token = None
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()

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
            None if row.get("repository_identity") is None else str(row["repository_identity"]),
            None if row.get("base_sha") is None else str(row["base_sha"]),
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
            None if row.get("repository_identity") is None else str(row["repository_identity"]),
            None if row.get("base_sha") is None else str(row["base_sha"]),
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
            repository_identity=None if row.get("repository_identity") is None else str(row["repository_identity"]),
            base_sha=None if row.get("base_sha") is None else str(row["base_sha"]),
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

    def park_native_dependency_child(self, ticket_id: str, *, idempotency_key: str) -> None:
        """Durably keep a linked handoff child non-dispatchable until release."""
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        task = self.get_task(ticket_id)
        if task.status == "blocked":
            return
        if task.status in {"done", "running"}:
            raise RuntimeError("native dependency graph cannot park an active or completed child")
        self._run("block", ticket_id, f"native dependency graph parked ({idempotency_key})", "--kind", "needs_input")

    def add_comment(self, ticket_id: str, comment: str) -> None:
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        self._run("comment", ticket_id, comment, "--author", "local-first-orchestrator")

    def deliver_comment(self, ticket_id: str, comment: str, *, idempotency_key: str) -> None:
        """Comment-worker boundary; the persisted marker supplies reconciliation identity."""
        self.add_comment(ticket_id, comment)
