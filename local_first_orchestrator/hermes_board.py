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
from .execution_handoff import HANDOFF_MARKER, HANDOFF_SENTINEL, attach_execution_handoff
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
    raw: dict[str, Any] | None = None


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
    current_run_id: int | None = None
    task_events: tuple[dict[str, Any], ...] = ()
    comments: tuple[Any, ...] = ()
    # Lossless CLI representations.  Typed fields are convenience views only;
    # authority and hashes must use these complete values.
    raw_task: dict[str, Any] | None = None
    raw_comments: tuple[dict[str, Any], ...] = ()
    raw_events: tuple[dict[str, Any], ...] = ()
    raw_runs: tuple[dict[str, Any], ...] = ()
    raw_snapshot: dict[str, Any] | None = None


from .revalidation_boundary import create_revalidation_capability, revoke_revalidation_capability
from .native_workspace import validate_native_workspace_path, canonical_native_workspace_path


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
        # Keep the configured spelling (including a symlink) so validation can
        # detect retargeting instead of silently following the new target.
        self.board_db_path = None if board_db_path is None else Path(board_db_path).expanduser()

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
            from hermes_cli.kanban_output import _SHOW_RUN_FIELDS, _obj_dict, _task_to_dict
            parents = tuple(kanban_db.parent_ids(connection, task_id))
            children = tuple(kanban_db.child_ids(connection, task_id))
            raw_task = _task_to_dict(task)
            runs = kanban_db.list_runs(connection, task_id)
            comments = kanban_db.list_comments(connection, task_id)
            events = kanban_db.list_events(connection, task_id)
            latest_summary = kanban_db.latest_summary(connection, task_id)
        except (sqlite3.DatabaseError, KeyError, AttributeError, TypeError) as exc:
            raise RuntimeError("Hermes Kanban execution snapshot read failed") from exc
        external_runs = tuple(ExternalExecutionRun(int(run.id), str(run.status or ""), None if run.outcome is None else str(run.outcome), int(run.started_at) if run.started_at is not None else None, None if run.ended_at is None else int(run.ended_at), None if run.summary is None else str(run.summary), None if run.profile is None else str(run.profile), None if run.worker_pid is None else int(run.worker_pid), run.metadata) for run in runs)
        active_runs = [run for run in external_runs if run.status == "running"]
        if str(task.status or "") == "running" and len(active_runs) != 1:
            raise RuntimeError("Hermes running task execution evidence drift: exactly one active run required")
        derived_run_id = active_runs[0].id if len(active_runs) == 1 else None
        raw_snapshot = {"task": raw_task, "latest_summary": latest_summary, "parents": list(parents), "children": list(children), "comments": [_obj_dict(c, ("author", "body", "created_at")) for c in comments], "events": [_obj_dict(e, ("kind", "payload", "created_at", "run_id")) for e in events], "runs": [_obj_dict(r, _SHOW_RUN_FIELDS) for r in runs]}
        return ExternalExecutionSnapshot(
            task=ExternalTicket(str(task.id), str(task.title or ""), str(task.body or ""), str(task.status or ""), task.workspace_path, parents=parents, children=children, assignee=task.assignee, workspace_kind=task.workspace_kind),
            session_id=raw_task.get("session_id"), branch_name=raw_task.get("branch_name"), started_at=raw_task.get("started_at"), completed_at=raw_task.get("completed_at"),
            runs=external_runs, repository_identity=None, base_sha=None,
            current_run_id=derived_run_id, raw_snapshot=raw_snapshot,
            raw_task=raw_task, raw_comments=tuple(raw_snapshot["comments"]), raw_events=tuple(raw_snapshot["events"]), raw_runs=tuple(raw_snapshot["runs"]),
        )

    @contextmanager
    def revalidation(self, local_first_ticket_id: str, external_task_id: str | None = None):
        """Hold the exact Hermes board write lock across the caller's ledger commit."""
        if external_task_id is None:
            # Legacy direct-adapter callers used one external identity for both
            # domains. Controller paths always pass both explicitly.
            external_task_id = local_first_ticket_id
        path = self._resolved_board_db_path()
        try:
            from hermes_cli.sqlite_util import open_db
            connection = open_db(path, db_label=f"kanban:{self.board}", busy_timeout_ms=int(self.timeout_seconds * 1000), wal=False, check_same_thread=False)
        except Exception as exc:
            raise RuntimeError("trusted board revalidation cannot open the configured local SQLite board") from exc
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._snapshot_from_connection(connection, external_task_id)
            capability = create_revalidation_capability(
                self, connection, path, local_first_ticket_id, external_task_id,
                snapshot, self.board, configured_path=self.board_db_path or path,
            )
            try:
                yield capability
            finally:
                revoke_revalidation_capability(capability)
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

    def dispatch_one_if_allowed(self, allowed_task_ids: set[str]) -> str | None:
        """Dispatch at most one task, refusing a board plan containing unrelated work."""
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        if not allowed_task_ids:
            return None
        preview = self._run("dispatch", "--dry-run", "--max", "100", "--json")
        if not isinstance(preview, dict) or not isinstance(preview.get("spawned"), list):
            raise RuntimeError("Hermes dispatch dry-run returned malformed JSON")
        planned_ids: list[str] = []
        for item in preview["spawned"]:
            if not isinstance(item, dict) or not isinstance(item.get("task_id"), str):
                raise RuntimeError("Hermes dispatch dry-run returned malformed spawn identity")
            planned_ids.append(str(item["task_id"]))
        if not planned_ids:
            return None
        unexpected = sorted(set(planned_ids) - set(allowed_task_ids))
        if unexpected:
            raise RuntimeError("Hermes dispatch plan contains non-Local-First task(s): " + ",".join(unexpected))
        result = self._run("dispatch", "--max", "1", "--json")
        if not isinstance(result, dict) or not isinstance(result.get("spawned"), list):
            raise RuntimeError("Hermes dispatch returned malformed JSON")
        spawned = result["spawned"]
        if not spawned:
            return None
        if len(spawned) != 1 or not isinstance(spawned[0], dict) or not isinstance(spawned[0].get("task_id"), str):
            raise RuntimeError("Hermes dispatch violated single-task bound")
        task_id = str(spawned[0]["task_id"])
        if task_id not in allowed_task_ids:
            raise RuntimeError("Hermes dispatch spawned a task outside Local First authority")
        return task_id

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
            dict(row),
        )

    def execution_snapshot(self, task_id: str) -> ExternalExecutionSnapshot:
        payload = self._run("show", task_id, "--json")
        row = payload.get("task") if isinstance(payload, dict) else None
        runs = payload.get("runs") if isinstance(payload, dict) else None
        if not isinstance(row, dict) or not isinstance(runs, list):
            raise RuntimeError("Hermes execution snapshot is malformed")
        parents = payload.get("parents")
        children = payload.get("children")
        comments = payload.get("comments", [])
        events = payload.get("events", [])
        if not isinstance(parents, list) or not all(type(value) is str and value for value in parents):
            raise RuntimeError("Hermes execution snapshot parent graph is malformed")
        if not isinstance(children, list) or not all(type(value) is str and value for value in children):
            raise RuntimeError("Hermes execution snapshot child graph is malformed")
        if not isinstance(comments, list) or not all(isinstance(item, dict) for item in comments):
            raise RuntimeError("Hermes execution snapshot comments are malformed")
        if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
            raise RuntimeError("Hermes execution snapshot events are malformed")
        for key in ("started_at", "completed_at"):
            if row.get(key) is not None and (type(row[key]) is not int or row[key] < 0):
                raise RuntimeError(f"Hermes task {key} has the wrong type")
        for key in ("session_id", "branch_name"):
            if row.get(key) is not None and type(row[key]) is not str:
                raise RuntimeError(f"Hermes task {key} has the wrong type")
        task = ExternalTicket(
            str(row["id"]),
            str(row.get("title") or ""),
            str(row.get("body") or ""),
            str(row.get("status") or ""),
            row.get("workspace_path"),
            tuple(parents),
            tuple(children),
            None if row.get("assignee") is None else str(row["assignee"]),
            None if row.get("workspace_kind") is None else str(row["workspace_kind"]),
            None if row.get("repository_identity") is None else str(row["repository_identity"]),
            None if row.get("base_sha") is None else str(row["base_sha"]),
            dict(row),
        )
        parsed: list[ExternalExecutionRun] = []
        for item in runs:
            if not isinstance(item, dict) or type(item.get("id")) is not int:
                raise RuntimeError("Hermes execution run is malformed")
            if item["id"] < 0:
                raise RuntimeError("Hermes execution run id is invalid")
            for key in ("status", "profile", "summary", "outcome"):
                if item.get(key) is not None and type(item[key]) is not str:
                    raise RuntimeError(f"Hermes execution run {key} has the wrong type")
            for key in ("started_at", "ended_at"):
                if item.get(key) is not None and (type(item[key]) is not int or item[key] < 0):
                    raise RuntimeError(f"Hermes execution run {key} has the wrong type")
            if item.get("worker_pid") is not None and (type(item["worker_pid"]) is not int or item["worker_pid"] <= 0):
                raise RuntimeError("Hermes execution run worker_pid has the wrong type")
            if item.get("metadata") is not None and not isinstance(item["metadata"], dict):
                raise RuntimeError("Hermes execution run metadata has the wrong type")
            if item.get("started_at") is None:
                raise RuntimeError("Hermes execution run started_at is missing")
            if item.get("ended_at") is not None and item["ended_at"] < item["started_at"]:
                raise RuntimeError("Hermes execution run timestamps are not monotonic")
            parsed.append(ExternalExecutionRun(
                id=int(item["id"]),
                status=item.get("status"),
                outcome=item.get("outcome"),
                started_at=item.get("started_at"),
                ended_at=item.get("ended_at"),
                summary=item.get("summary"),
                profile=item.get("profile"),
                worker_pid=item.get("worker_pid"),
                metadata=item.get("metadata"),
            ))
        active_runs = [run for run in parsed if run.status == "running"]
        if task.status == "running" and len(active_runs) != 1:
            raise RuntimeError("Hermes running task execution evidence drift: exactly one active run required")
        derived_run_id = active_runs[0].id if len(active_runs) == 1 else None
        return ExternalExecutionSnapshot(
            task=task,
            session_id=None if row.get("session_id") is None else str(row["session_id"]),
            branch_name=None if row.get("branch_name") is None else str(row["branch_name"]),
            started_at=None if row.get("started_at") is None else int(row["started_at"]),
            completed_at=None if row.get("completed_at") is None else int(row["completed_at"]),
            # Hermes already supplies start/id order. Never sort or deduplicate
            # history here: order drift is evidence, not presentation noise.
            runs=tuple(parsed),
            repository_identity=None if row.get("repository_identity") is None else str(row["repository_identity"]),
            base_sha=None if row.get("base_sha") is None else str(row["base_sha"]),
            current_run_id=derived_run_id,
            task_events=tuple(events),
            comments=tuple(comments),
            raw_task=dict(row), raw_comments=tuple(comments), raw_events=tuple(events),
            raw_runs=tuple(dict(item) for item in runs), raw_snapshot=dict(payload),
        )

    def import_candidates(self) -> list[ExternalTicket]:
        rows = self._run("list", "--json", "--status", "scheduled")
        if not isinstance(rows, list):
            raise RuntimeError("Kanban list JSON must be an array")
        candidates = []
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("Kanban list contains a non-object task")
            body = str(row.get("body") or "")
            if "<!-- local-first-orchestrator -->" not in body:
                continue
            candidates.append(ExternalTicket(
                str(row["id"]), str(row.get("title") or ""), body, str(row.get("status") or ""), row.get("workspace_path"),
                assignee=None if row.get("assignee") is None else str(row["assignee"]),
                workspace_kind=None if row.get("workspace_kind") is None else str(row["workspace_kind"]),
                repository_identity=None if row.get("repository_identity") is None else str(row["repository_identity"]),
                base_sha=None if row.get("base_sha") is None else str(row["base_sha"]),
                raw=dict(row),
            ))
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
            verified = self._verify_native_release_route(task, expected_workspace_path=expected_routing["workspace_path"], allow_unbound_workspace=True)
            if verified != {"profile": expected_routing.get("profile"), "workspace_kind": expected_routing.get("workspace_kind"), "workspace_path": expected_routing.get("workspace_path")}:
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

    def activation_marker_present(self, ticket_id: str, marker: str) -> bool:
        try:
            from hermes_cli.sqlite_util import open_db
            path = self._resolved_board_db_path()
            with open_db(path, db_label=f"kanban:{self.board}", busy_timeout_ms=int(self.timeout_seconds * 1000), wal=False, check_same_thread=False) as connection:
                rows = connection.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (ticket_id,)).fetchall()
                return sum(1 for row in rows if str(row["body"]) == f"UNBLOCK: {marker}") == 1
        except Exception as exc:
            raise RuntimeError("native release activation marker read unavailable") from exc

    def activate_native_release(self, ticket_id: str, *, activation_marker: str, expected_routing: dict[str, str]) -> ExternalTicket:
        """Perform exactly one supported scheduled-to-ready Hermes mutation."""
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        if not activation_marker or not isinstance(expected_routing, dict):
            raise ValueError("native release activation requires marker and routing")
        task = self.get_task(ticket_id)
        self.verify_native_release_task(task, expected_workspace_path=expected_routing["workspace_path"])
        if task.status == "ready":
            raise RuntimeError("native release activation requires the exact scheduled side effect marker")
        if task.status != "scheduled":
            raise RuntimeError(f"native release activation target has incompatible state: {task.status}")
        if {"profile": task.assignee, "workspace_kind": task.workspace_kind, "workspace_path": task.workspace_path} != {"profile": expected_routing.get("profile"), "workspace_kind": expected_routing.get("workspace_kind"), "workspace_path": expected_routing.get("workspace_path")}:
            raise RuntimeError("native release activation routing drift")
        self._run("unblock", ticket_id, "--reason", activation_marker)
        updated = self.get_task(ticket_id)
        self.verify_native_release_task(updated, expected_workspace_path=expected_routing["workspace_path"])
        if updated.status != "ready":
            raise RuntimeError("native release activation did not produce ready state")
        return updated

    def reclaim_for_repair(self, ticket_id: str, *, reason: str) -> ExternalTicket:
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        task = self.get_task(ticket_id)
        if task.status not in {"todo", "ready"}:
            self._run("reclaim", ticket_id, "--reason", reason)
            task = self.get_task(ticket_id)
        if task.status not in {"todo", "ready"}:
            raise RuntimeError(f"Hermes repair reclaim did not make task dispatchable: {task.status}")
        return task

    def repair_predecessor_task(self, task_id: str) -> ExternalTicket:
        task = self.get_task(task_id)
        if task.status == "done":
            return task
        if task.status in {"blocked", "triage"}:
            snapshot = self.execution_snapshot(task_id)
            if snapshot.runs:
                latest = max(snapshot.runs, key=lambda run: run.id)
                if latest.status == "blocked" and latest.outcome == "blocked" and latest.summary == HANDOFF_SENTINEL and latest.ended_at is not None:
                    return task
        raise RuntimeError(f"Hermes repair predecessor is not terminal Local First work: {task.status}")

    def create_repair_task(
        self,
        predecessor_task_id: str,
        *,
        title: str,
        body: str,
        workspace_path: str,
        downstream_child_ids: tuple[str, ...],
        idempotency_key: str,
        reason: str,
    ) -> ExternalTicket:
        """Insert a fresh repair task between a completed task and its children.

        Downstream children are parked before any dependency edge is removed, so
        an interrupted rewrite fails closed instead of releasing successor work.
        Replaying with the same idempotency key converges the same task/graph.
        """
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        if not predecessor_task_id or not workspace_path or not idempotency_key or not reason:
            raise ValueError("repair task creation requires predecessor, workspace, key, and reason")
        predecessor = self.repair_predecessor_task(predecessor_task_id)
        expected_children = tuple(sorted(set(downstream_child_ids)))
        if predecessor_task_id in expected_children:
            raise ValueError("repair task cannot depend on itself")
        args = [
            "create", title,
            "--body", attach_execution_handoff(body),
            "--parent", predecessor_task_id,
            "--workspace", f"dir:{workspace_path}",
            "--idempotency-key", idempotency_key,
            "--initial-status", "blocked",
        ]
        assignee = self.implementation_profile or predecessor.assignee
        if assignee:
            args += ["--assignee", assignee]
        args += ["--json"]
        payload = self._run(*args)
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not payload["id"]:
            raise RuntimeError("Hermes repair create JSON missing task id")
        repair_task_id = str(payload["id"])
        if repair_task_id == predecessor_task_id:
            raise RuntimeError("Hermes repair task identity conflicts with predecessor")
        repair = self.get_task(repair_task_id)
        if predecessor_task_id not in set(repair.parents):
            raise RuntimeError("Hermes repair task is missing predecessor dependency")
        extra_children = set(repair.children) - set(expected_children)
        if extra_children:
            raise RuntimeError("Hermes repair task has unexpected downstream dependencies")

        for child_id in expected_children:
            child = self.get_task(child_id)
            if child.status in {"running", "done"}:
                raise RuntimeError(f"Hermes downstream child cannot be safely re-parented: {child_id}:{child.status}")
            if child.status != "blocked":
                self._run("block", child_id, f"Local First repair dependency insertion ({idempotency_key})", "--kind", "needs_input")
                child = self.get_task(child_id)
            if child.status != "blocked":
                raise RuntimeError(f"Hermes downstream child was not durably parked: {child_id}:{child.status}")
            parents = set(child.parents)
            if repair_task_id not in parents:
                self._run("link", repair_task_id, child_id)
                child = self.get_task(child_id)
                parents = set(child.parents)
            if repair_task_id not in parents:
                raise RuntimeError(f"Hermes repair dependency link did not converge: {child_id}")
            if predecessor_task_id in parents:
                self._run("unlink", predecessor_task_id, child_id)
                child = self.get_task(child_id)
                parents = set(child.parents)
            if repair_task_id not in parents or predecessor_task_id in parents:
                raise RuntimeError(f"Hermes repair dependency rewrite did not converge: {child_id}")

        repair = self.get_task(repair_task_id)
        if tuple(sorted(set(repair.children))) != expected_children:
            raise RuntimeError("Hermes repair task downstream graph did not converge")
        if repair.status == "blocked":
            self._run("unblock", repair_task_id, "--reason", reason)
            repair = self.get_task(repair_task_id)
        if repair.status not in {"todo", "ready", "scheduled", "running", "blocked", "done"}:
            raise RuntimeError(f"Hermes repair task entered an unsupported status: {repair.status}")
        return repair

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

    def _verify_native_release_route(self, task: ExternalTicket, *, expected_workspace_path: str, allow_unbound_workspace: bool) -> dict[str, str]:
        if self.implementation_profile is None or self.canonical_repository is None:
            raise RuntimeError("native release authority is not configured")
        expected = canonical_native_workspace_path(self.canonical_repository, task.id)
        if expected_workspace_path != str(expected):
            raise RuntimeError("native release authority mismatch")
        validate_native_workspace_path(
            expected_workspace_path,
            repository=self.canonical_repository,
            external_task_id=task.id,
        )
        if task.assignee != self.implementation_profile or task.workspace_kind != "worktree":
            raise RuntimeError("native release authority mismatch")
        if task.workspace_path is None:
            if not allow_unbound_workspace:
                raise RuntimeError("native release authority mismatch")
        else:
            validate_native_workspace_path(
                task.workspace_path,
                repository=self.canonical_repository,
                external_task_id=task.id,
            )
            if task.workspace_path != str(expected):
                raise RuntimeError("native release authority mismatch")
        return {"profile": self.implementation_profile, "workspace_kind": "worktree", "workspace_path": str(expected)}

    def bind_native_release_task(self, ticket_id: str, *, expected_workspace_path: str) -> dict[str, str]:
        """Bind pre-dispatch profile authority while Hermes still has no concrete workspace path."""
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        if self.implementation_profile is None or self.canonical_repository is None:
            raise RuntimeError("native release authority is not configured")
        task = self.get_task(ticket_id)
        expected = canonical_native_workspace_path(self.canonical_repository, task.id)
        if expected_workspace_path != str(expected):
            raise RuntimeError("native release authority mismatch")
        validate_native_workspace_path(
            expected_workspace_path,
            repository=self.canonical_repository,
            external_task_id=task.id,
        )
        if task.workspace_kind != "worktree":
            raise RuntimeError("native release authority mismatch")
        if task.workspace_path is not None:
            validate_native_workspace_path(task.workspace_path, repository=self.canonical_repository, external_task_id=task.id)
            if task.workspace_path != str(expected):
                raise RuntimeError("native release authority mismatch")
        if task.assignee is None:
            self._run("assign", ticket_id, self.implementation_profile)
            task = self.get_task(ticket_id)
        elif task.assignee != self.implementation_profile:
            raise RuntimeError("native release authority mismatch")
        return self._verify_native_release_route(task, expected_workspace_path=expected_workspace_path, allow_unbound_workspace=True)

    def verify_native_release_task(self, task: ExternalTicket, *, expected_workspace_path: str) -> dict[str, str]:
        """Strictly verify a Hermes task after a concrete workspace has been bound."""
        return self._verify_native_release_route(task, expected_workspace_path=expected_workspace_path, allow_unbound_workspace=False)

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
