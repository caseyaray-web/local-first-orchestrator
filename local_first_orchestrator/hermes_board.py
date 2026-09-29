"""Fail-closed, CLI-only Hermes Kanban adapter.

The adapter never treats a read as a CAS.  It binds one board and one enrolled
anchor, applies only bounded documented CLI commands, and returns partial,
conflict, unknown, or unsupported where native evidence cannot prove an effect.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import Action, ActionResult, BoardSnapshot, validate_scope

_AUTHOR = "local-first-orchestrator"
_MAX_FIELD = 16_384
_MAX_MARKER_SEARCH_ROWS = 1_000


@dataclass(frozen=True, slots=True)
class BoardCapabilities:
    read_tasks: bool; read_runs: bool; create_held: bool; comment: bool; request_review: bool
    request_changes: bool; return_waiting_review: bool; hold: bool; release: bool; link: bool
    complete_anchor: bool; exact_run_stop: bool; atomic_read_bound_mutation: bool

    @classmethod
    def native_m0(cls) -> "BoardCapabilities":
        return cls(True, True, True, True, True, False, True, True, True, True, False, False, False)


class _BoardUnavailable(RuntimeError): pass


class HermesBoardAdapter:
    is_fake = False

    def __init__(self, *, board: str, anchor_task_id: str, executable: str,
                 runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                 hermes_home: Path, kanban_home: Path, timeout_seconds: int = 15,
                 output_limit: int = 200_000,
                 managed_member_lookup: Callable[[Mapping[str, str], str], bool] | None = None,
                 completion_evidence_verifier: Callable[[Mapping[str, str], str, str], bool] | None = None,
                 create_lock_assertion: Callable[[Mapping[str, str], str], Any] | None = None,
                 claim_create_attempt: Callable[[Mapping[str, str], str], Any] | None = None) -> None:
        if not all(isinstance(x, str) and x and x.replace("-", "").replace("_", "").isalnum() for x in (board, anchor_task_id)):
            raise ValueError("explicit board and anchor IDs required")
        executable_path = Path(executable)
        if not executable_path.is_absolute() or not executable_path.is_file(): raise ValueError("Hermes executable must be an absolute existing path")
        if not isinstance(hermes_home, Path) or not isinstance(kanban_home, Path): raise ValueError("explicit isolated home paths required")
        if timeout_seconds < 1 or output_limit < 1: raise ValueError("positive process limits required")
        self.board, self.anchor_task_id, self.executable = board, anchor_task_id, str(executable_path)
        self.runner, self.hermes_home, self.kanban_home = runner, hermes_home, kanban_home
        self.timeout_seconds, self.output_limit = timeout_seconds, output_limit
        self.managed_member_lookup = managed_member_lookup
        self.completion_evidence_verifier = completion_evidence_verifier
        # The coordinator owns the singleton/OS lock.  This callback only
        # asserts it remains held for the full reconciliation and create.
        self.create_lock_assertion = create_lock_assertion
        # The evidence store consumes this entitlement before any native send.
        self.claim_create_attempt = claim_create_attempt
        self.capabilities = BoardCapabilities.native_m0()

    def _env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if os.environ.get(k)}
        env.update({"HERMES_HOME": str(self.hermes_home), "HERMES_KANBAN_HOME": str(self.kanban_home), "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
        return env

    def _invoke(self, *args: str, json_output: bool = False) -> Any:
        if not all(isinstance(x, str) and len(x) <= _MAX_FIELD for x in args): raise ValueError("bounded string argv required")
        argv = (self.executable, "kanban", "--board", self.board, *args)
        try: completed = self.runner(argv, text=True, capture_output=True, timeout=self.timeout_seconds, check=False, shell=False, env=self._env())
        except (OSError, subprocess.TimeoutExpired) as exc: raise _BoardUnavailable("Hermes Kanban CLI unavailable") from exc
        stdout, stderr = completed.stdout or "", completed.stderr or ""
        if len(stdout) > self.output_limit or len(stderr) > self.output_limit: raise _BoardUnavailable("Hermes Kanban output exceeded bound")
        if completed.returncode: raise _BoardUnavailable((stderr or stdout or "Hermes Kanban CLI failed")[:2000])
        if not json_output: return stdout
        try: return json.loads(stdout)
        except json.JSONDecodeError as exc: raise _BoardUnavailable("Hermes Kanban returned malformed JSON") from exc

    @staticmethod
    def _digest(payload: Mapping[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()

    def _snapshot(self, task_id: str) -> BoardSnapshot:
        if not isinstance(task_id, str) or not task_id: raise ValueError("task_id must be non-empty")
        payload = self._invoke("show", task_id, "--json", json_output=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("task"), dict) or payload["task"].get("id") != task_id: raise _BoardUnavailable("exact task read identity mismatch")
        runs = self._invoke("runs", task_id, "--json", json_output=True)
        if not isinstance(runs, list) or not all(isinstance(x, dict) for x in runs): raise _BoardUnavailable("exact run read malformed")
        def records(name: str) -> tuple[Mapping[str, Any], ...]:
            value = payload.get(name, [])
            if not isinstance(value, list) or not all(isinstance(x, dict) for x in value): raise _BoardUnavailable(f"exact task {name} malformed")
            return tuple(value)
        parents = payload.get("parents", [])
        if not isinstance(parents, list) or not all(isinstance(x, str) and x for x in parents): raise _BoardUnavailable("exact task parent graph malformed")
        data = {"native_task": payload["task"], "parents": [{"id": x} for x in parents], "runs": runs, "comments": list(records("comments")), "events": list(records("events")), "attachments": list(records("attachments"))}
        return BoardSnapshot(native_task=data["native_task"], parents=tuple(data["parents"]), runs=tuple(runs), comments=records("comments"), events=records("events"), attachments=records("attachments"), observed_at=datetime.now(timezone.utc).isoformat(), digest=self._digest(data))

    def list_tasks(self) -> tuple[BoardSnapshot, ...]:
        rows = self._invoke("list", "--json", json_output=True)
        if not isinstance(rows, list) or not all(isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"] for x in rows): raise _BoardUnavailable("task list malformed")
        return tuple(self._snapshot(x["id"]) for x in rows)
    def read_task(self, task_id: str) -> BoardSnapshot: return self._snapshot(task_id)
    def read_run(self, task_id: str, run_id: str) -> Mapping[str, Any]:
        for run in self._snapshot(task_id).runs:
            if str(run.get("id")) == str(run_id): return run
        raise KeyError(f"Hermes run not found: {task_id}/{run_id}")

    def read_scoped_run(self, scope: Mapping[str, str], task_id: str, run_id: str) -> Mapping[str, Any]:
        """Bind an exact native run read to this configured board and anchor.

        The native runs JSON omits board/anchor provenance. Only this adapter
        may add those fields after reading the exact managed task and run.
        """
        valid = validate_scope(scope)
        if valid != {"board_id": self.board, "anchor_task_id": self.anchor_task_id}:
            raise ValueError("native run scope does not match configured board and anchor")
        if not isinstance(task_id, str) or not task_id or not isinstance(run_id, str) or not run_id:
            raise ValueError("exact task and run IDs are required")
        if task_id != self.anchor_task_id and not self._trusted_member(valid, task_id):
            raise ValueError("native run task is not a trusted managed member")
        run = self.read_run(task_id, run_id)
        if run.get("id") != run_id:
            raise ValueError("native run identity is not exact")
        for field, expected in (("task_id", task_id), ("board_id", self.board), ("anchor_task_id", self.anchor_task_id)):
            if field in run and run[field] != expected:
                raise ValueError(f"native run {field} contradicts the exact read")
        return {**run, "task_id": task_id, "board_id": self.board, "anchor_task_id": self.anchor_task_id}

    @staticmethod
    def _evidence(snapshot: BoardSnapshot) -> dict[str, Any]: return snapshot.to_dict()
    def _result(self, action: Action, outcome: str, details: str, snapshot: BoardSnapshot | None) -> ActionResult:
        return ActionResult(action.key, outcome, details, None if snapshot is None else self._evidence(snapshot))
    @staticmethod
    def _running(snapshot: BoardSnapshot) -> bool:
        return snapshot.native_task.get("status") == "running" or any(x.get("status") == "running" for x in snapshot.runs)

    def _scope_and_target(self, action: Action, effect: str, task_id: str | None = None) -> str | None:
        if not isinstance(action, Action) or action.effect != effect: raise ValueError("action effect does not match adapter operation")
        if action.scope.get("board_id") != self.board or action.scope.get("anchor_task_id") != self.anchor_task_id: return "action scope is not this adapter's explicit board and anchor"
        if task_id is not None:
            if action.target.get("task_id") != task_id: return "action target does not match exact task"
            if task_id != self.anchor_task_id and not self._trusted_member(action.scope, task_id): return "target is neither the anchor nor a trusted managed member"
        return None

    def _trusted_member(self, scope: Mapping[str, str], task_id: str) -> bool:
        if self.managed_member_lookup is None: return False
        try: return self.managed_member_lookup(scope, task_id) is True
        except Exception: return False

    def _preflight(self, action: Action, effect: str, task_id: str) -> tuple[BoardSnapshot | None, ActionResult | None]:
        error = self._scope_and_target(action, effect, task_id)
        if error: return None, self._result(action, "conflict", error, None)
        try: before = self._snapshot(task_id)
        except (_BoardUnavailable, ValueError) as exc: return None, self._result(action, "unknown", f"pre-read unavailable: {exc}", None)
        if action.expected_observed_identity != before.digest: return before, self._result(action, "conflict", "action observation identity is stale", before)
        return before, None

    def _mutate(self, action: Action, *, task_id: str, argv: tuple[str, ...], verifier: Callable[[BoardSnapshot, BoardSnapshot], str | None], description: str) -> ActionResult:
        before, result = self._preflight(action, action.effect, task_id)
        if result is not None: return result
        assert before is not None
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton lock assertion is required immediately before native mutation", before)
        try: self._invoke(*argv)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"native {description} outcome unknown: {exc}", before)
        try: after = self._snapshot(task_id)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"post-{description} readback unavailable: {exc}", None)
        outcome = verifier(before, after)
        if outcome is None: return self._result(action, "verified", f"native {description} verified by exact readback", after)
        return self._result(action, outcome, f"native {description} could not be safely verified: {outcome}", after)

    def _assert_create_lock(self, action: Action) -> bool:
        if self.create_lock_assertion is None:
            return False
        try:
            self.create_lock_assertion(action.scope, self.anchor_task_id)
            return True
        except Exception:
            return False

    def _claim_create_attempt(self, action: Action) -> bool:
        if self.claim_create_attempt is None:
            return False
        try:
            self.claim_create_attempt(action.scope, action.key)
            return True
        except Exception:
            return False

    def _marked_create_matches(self, marker: str) -> tuple[int, tuple[BoardSnapshot, ...]]:
        rows = self._invoke("list", "--archived", "--json", json_output=True)
        if not isinstance(rows, list) or len(rows) > _MAX_MARKER_SEARCH_ROWS:
            raise _BoardUnavailable("bounded create marker search malformed or exceeded row limit")
        ids: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise _BoardUnavailable("bounded create marker search row malformed")
            ids.append(row["id"])
        matches = tuple(snapshot for snapshot in (self._snapshot(task_id) for task_id in ids)
                        if isinstance(snapshot.native_task.get("body"), str) and marker in snapshot.native_task["body"])
        if len(matches) > 1:
            return len(matches), tuple()
        return len(matches), matches

    @staticmethod
    def _create_marker(action: Action) -> str:
        """Bound reconciliation identity, scoped to one board-anchor lineage."""
        canonical = json.dumps(
            {"action_key": action.key, "anchor_task_id": action.scope["anchor_task_id"], "board_id": action.scope["board_id"]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        return "<!-- local-first-create:v1:sha256:" + hashlib.sha256(canonical.encode()).hexdigest() + " -->"

    @staticmethod
    def _native_marker(action: Action) -> str:
        """Stable native hold/release identity, bound to one board lineage."""
        canonical = json.dumps(
            {"action_key": action.key, "anchor_task_id": action.scope["anchor_task_id"],
             "board_id": action.scope["board_id"], "effect": action.effect},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        return "<!-- local-first-native:v1:sha256:" + hashlib.sha256(canonical.encode()).hexdigest() + " -->"

    @staticmethod
    def _contains_marker(value: Any, marker: str) -> bool:
        if isinstance(value, str): return marker in value
        if isinstance(value, Mapping): return any(HermesBoardAdapter._contains_marker(item, marker) for item in value.values())
        if isinstance(value, (list, tuple)): return any(HermesBoardAdapter._contains_marker(item, marker) for item in value)
        return False

    def _verify_native_hold_release(self, action: Action, snapshot: BoardSnapshot) -> str | None:
        """Read-only reconciliation of one exact native hold/release effect.

        Status alone is never evidence. Hold events retain the reason marker,
        so hold requires the exact comment and event. Unblock events do not
        retain a reason, so release requires the exact UNBLOCK comment instead
        of inventing an event-to-marker linkage.
        """
        marker = self._native_marker(action)
        is_hold = action.effect == "hold"
        expected_comment = f"BLOCKED: {marker}" if is_hold else f"UNBLOCK: {marker}"
        marked_comments = [item for item in snapshot.comments if self._contains_marker(item, marker)]
        exact_comments = [item for item in marked_comments if item.get("body") == expected_comment]
        if len(marked_comments) > 1 or len(exact_comments) > 1:
            return "conflict"
        if marked_comments and not exact_comments:
            return "conflict"
        marked_events = [item for item in snapshot.events if self._contains_marker(item, marker)]
        if not is_hold:
            # Native unblock events have no reason payload, so on a later
            # read they cannot be tied to this operation. The exact UNBLOCK
            # comment is the supported scoped evidence; do not invent linkage.
            if marked_events: return "conflict"
            if not exact_comments: return "unsupported"
        else:
            exact_events = [
                item for item in snapshot.events
                if item.get("kind") == "blocked"
                and isinstance(item.get("payload"), Mapping)
                and item["payload"].get("reason") == marker
                and item["payload"].get("kind") == "needs_input"
                and item["payload"].get("source_status") in {"ready", "running"}
                and isinstance(item["payload"].get("recurrences"), int)
                and item["payload"]["recurrences"] > 0
            ]
            if len(marked_events) > 1 or len(exact_events) > 1:
                return "conflict"
            if marked_events and not any(item in exact_events for item in marked_events):
                return "conflict"
            if not exact_comments and not exact_events:
                return "unsupported"
            if not exact_comments or not exact_events:
                return "unknown"
        if is_hold:
            if self._running(snapshot): return "partial"
            return None if snapshot.native_task.get("status") == "blocked" else "conflict"
        if snapshot.native_task.get("status") in {"ready", "todo"}: return None
        if self._running(snapshot) or snapshot.native_task.get("status") not in {"blocked"}: return "partial"
        return "unknown"

    def _verify_existing_create(self, action: Action, snapshot: BoardSnapshot, *, title: str, body: str, assignee: str, workspace: str, idempotency_key: str) -> ActionResult:
        task = snapshot.native_task
        if self._running(snapshot):
            return self._result(action, "partial", "marked create has active work and is not safely held", snapshot)
        expected = {"title": title, "body": body, "assignee": assignee, "workspace": workspace, "idempotency_key": idempotency_key}
        missing = [field for field in expected if field not in task]
        if missing:
            return self._result(action, "partial", f"marked create readback cannot expose identity fields: {', '.join(missing)}", snapshot)
        if any(task.get(field) != value for field, value in expected.items()):
            return self._result(action, "conflict", "marked create identity fields differ from action", snapshot)
        if task.get("status") != "blocked":
            return self._result(action, "conflict", "marked create is not held", snapshot)
        if not any(parent.get("id") == self.anchor_task_id for parent in snapshot.parents):
            return self._result(action, "conflict", "marked create is not associated with anchor", snapshot)
        return self._result(action, "no-op", "exact marked held creation already present", snapshot)

    def create_held(self, action: Action, *, title: str, body: str, assignee: str, workspace: str, idempotency_key: str) -> ActionResult:
        error = self._scope_and_target(action, "create_held")
        if error or action.target.get("anchor_task_id") != self.anchor_task_id: return self._result(action, "conflict", error or "create target must name exact anchor", None)
        if not all(isinstance(x, str) and x and len(x) <= _MAX_FIELD for x in (title, body, assignee, workspace, idempotency_key)): raise ValueError("bounded non-empty creation values required")
        marker = self._create_marker(action)
        created_body = body if marker in body else f"{body}\n\n{marker}"
        if len(created_body) > _MAX_FIELD: raise ValueError("creation body plus stable marker exceeds bound")
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton create lock assertion is required and must be held", None)
        try: before = self._snapshot(self.anchor_task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"pre-create anchor read unavailable: {exc}", None)
        if action.expected_observed_identity != before.digest: return self._result(action, "conflict", "action observation identity is stale", before)
        try:
            marker_count, matches = self._marked_create_matches(marker)
        except _BoardUnavailable as exc:
            return self._result(action, "unknown", f"pre-create marker reconciliation unavailable: {exc}", before)
        if marker_count > 1:
            return self._result(action, "conflict", "multiple exact stable create markers found", None)
        if marker_count == 1:
            assert len(matches) == 1
            return self._verify_existing_create(action, matches[0], title=title, body=created_body, assignee=assignee, workspace=workspace, idempotency_key=idempotency_key)
        if not self._claim_create_attempt(action):
            outcome = "unsupported" if self.claim_create_attempt is None else "unknown"
            return self._result(action, outcome, "durable create attempt claim is required and must succeed before native create", before)
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton create lock was not held immediately before native create", before)
        try: payload = self._invoke("create", title, "--body", created_body, "--assignee", assignee, "--workspace", workspace, "--parent", self.anchor_task_id, "--idempotency-key", idempotency_key, "--initial-status", "blocked", "--json", json_output=True)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"native create outcome unknown: {exc}", before)
        task_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not task_id: return self._result(action, "unknown", "native create returned no exact task identity", None)
        try: after = self._snapshot(task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"post-create readback unavailable: {exc}", None)
        task = after.native_task
        if self._running(after): return self._result(action, "partial", "created card has active work and is not safely held", after)
        if task.get("status") != "blocked": return self._result(action, "conflict", "create did not leave exact task held", after)
        expected_fields = {"id": task_id, "title": title, "body": created_body, "assignee": assignee, "workspace": workspace, "idempotency_key": idempotency_key}
        missing = [field for field in expected_fields if field not in task]
        if missing: return self._result(action, "partial", f"native create readback cannot expose identity fields: {', '.join(missing)}", after)
        if any(task.get(k) != v for k, v in expected_fields.items()): return self._result(action, "conflict", "created task identity fields differ from action", after)
        if not any(x.get("id") == self.anchor_task_id for x in after.parents): return self._result(action, "conflict", "created task is not associated with anchor", after)
        return self._result(action, "verified", "held creation verified by exact readback", after)

    def comment(self, action: Action, task_id: str, text: str) -> ActionResult:
        if not isinstance(text, str) or not text or len(text) > _MAX_FIELD: raise ValueError("bounded non-empty comment required")
        marker = f"<!-- local-first-action:{action.key} -->"
        if marker not in text: return self._result(action, "conflict", "comment lacks stable action marker", None)
        before, result = self._preflight(action, "comment", task_id)
        if result is not None: return result
        assert before is not None
        matching = [x for x in before.comments if x.get("body") == text and x.get("author") == _AUTHOR]
        if matching: return self._result(action, "no-op", "exact marked comment already present", before)
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton lock assertion is required immediately before native mutation", before)
        try: self._invoke("comment", task_id, text, "--author", _AUTHOR)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"native comment outcome unknown: {exc}", before)
        try: after = self._snapshot(task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"post-comment readback unavailable: {exc}", None)
        if any(x.get("body") == text and x.get("author") == _AUTHOR for x in after.comments): return self._result(action, "verified", "native comment verified by exact marker and author", after)
        return self._result(action, "conflict", "marked comment absent after native write", after)

    def request_review(self, action: Action, task_id: str, summary: str, *, reviewer: str | None = None, metadata: Mapping[str, Any] | None = None) -> ActionResult:
        if not isinstance(summary, str) or not summary or len(summary) > _MAX_FIELD or not isinstance(reviewer, str) or not reviewer or len(reviewer) > _MAX_FIELD: raise ValueError("bounded summary and reviewer required")
        before, result = self._preflight(action, "request_review", task_id)
        if result is not None: return result
        assert before is not None
        if before.native_task.get("status") not in {"ready", "todo"}: return self._result(action, "conflict", "review request requires exact waiting task", before)
        if reviewer == before.native_task.get("assignee"): return self._result(action, "conflict", "reviewer must differ from implementation assignee", before)
        encoded = None
        if metadata is not None:
            try: encoded = json.dumps(dict(metadata), sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError) as exc: raise ValueError("review metadata must be bounded JSON") from exc
            if len(encoded) > _MAX_FIELD: raise ValueError("review metadata exceeds bound")
        argv = ("request-review", task_id, "--summary", summary, "--reviewer", reviewer, *( () if encoded is None else ("--metadata", encoded)))
        return self._mutate(action, task_id=task_id, argv=argv, verifier=lambda _b, a: None if a.native_task.get("status") == "review" and a.native_task.get("reviewer") == reviewer else "partial", description="request-review")

    def request_changes(self, action: Action, task_id: str, reason: str, run_id: str) -> ActionResult:
        before, result = self._preflight(action, "request_changes", task_id)
        return result if result is not None else self._result(action, "unsupported", f"native request-changes cannot target exact review run {run_id}", before)
    def return_waiting_review(self, action: Action, task_id: str, reason: str) -> ActionResult:
        return self._mutate(action, task_id=task_id, argv=("reopen-review", task_id, "--reason", reason), verifier=lambda b,a: None if b.native_task.get("status") == "review" and a.native_task.get("status") in {"ready","todo"} else "conflict", description="reopen-review")
    def hold(self, action: Action, task_id: str, reason: str) -> ActionResult:
        before, result = self._preflight(action, "hold", task_id)
        if result is not None: return result
        assert before is not None
        if self._running(before): return self._result(action, "partial", "running work cannot be safely held", before)
        if before.native_task.get("status") == "blocked": return self._result(action, "no-op", "exact task already held", before)
        marker = self._native_marker(action)
        return self._mutate(action, task_id=task_id, argv=("block", task_id, marker, "--kind", "needs_input"), verifier=lambda _b, a: self._verify_native_hold_release(action, a), description="hold")
    def release(self, action: Action, task_id: str, reason: str) -> ActionResult:
        marker = self._native_marker(action)
        return self._mutate(action, task_id=task_id, argv=("unblock", task_id, "--reason", marker), verifier=lambda _b, a: self._verify_native_hold_release(action, a), description="release")
    def stop_run(self, action: Action, task_id: str, run_id: str, reason: str) -> ActionResult:
        before, result = self._preflight(action, "stop_run", task_id)
        if result is not None: return result
        assert before is not None
        return self._result(action, "conflict" if not any(str(x.get("id")) == str(run_id) for x in before.runs) else "unsupported", "exact run stop is not supported by native CLI", before)
    def link(self, action: Action, parent_task_id: str, child_task_id: str) -> ActionResult:
        if parent_task_id != self.anchor_task_id or parent_task_id == child_task_id or action.target.get("parent_task_id") != parent_task_id or action.target.get("child_task_id") != child_task_id: return self._result(action, "conflict", "links must originate at exact anchor with distinct endpoints", None)
        error = self._scope_and_target(action, "link")
        if error: return self._result(action, "conflict", error, None)
        if not self._trusted_member(action.scope, child_task_id): return self._result(action, "conflict", "link child is not a trusted managed member", None)
        try:
            parent, child = self._snapshot(parent_task_id), self._snapshot(child_task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"pre-read unavailable: {exc}", None)
        if action.expected_observed_identity != child.digest: return self._result(action, "conflict", "action observation identity is stale", child)
        if self._running(child): return self._result(action, "partial", "running dependent cannot be safely relinked", child)
        if any(item.get("id") == parent_task_id for item in child.parents): return self._result(action, "no-op", "exact dependency already present", child)
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton lock assertion is required immediately before native mutation", child)
        try: self._invoke("link", parent_task_id, child_task_id)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"native link outcome unknown: {exc}", child)
        try: after = self._snapshot(child_task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"post-link readback unavailable: {exc}", None)
        if self._running(after): return self._result(action, "partial", "linked dependent has active work after native link", after)
        if any(item.get("id") == parent_task_id for item in after.parents): return self._result(action, "verified", "dependency verified by exact child readback", after)
        return self._result(action, "conflict", "dependency missing after native link", after)
    def complete_anchor(self, action: Action, task_id: str, approval_evidence: str) -> ActionResult:
        before, result = self._preflight(action, "complete_anchor", task_id)
        if result is not None: return result
        assert before is not None
        if task_id != self.anchor_task_id: return self._result(action, "conflict", "only exact anchor may complete", before)
        if self.completion_evidence_verifier is None: return self._result(action, "unsupported", "trusted acceptance-evidence verifier is required", before)
        try: accepted = self.completion_evidence_verifier(action.scope, task_id, approval_evidence) is True
        except Exception: accepted = False
        if not accepted: return self._result(action, "conflict", "trusted verifier rejected acceptance evidence", before)
        return self._mutate(action, task_id=task_id, argv=("complete", task_id, "--result", approval_evidence), verifier=lambda _b,a: None if a.native_task.get("status") == "done" else "conflict", description="anchor completion")
    def verify_effect(self, action: Action) -> ActionResult:
        task_id = action.target.get("task_id")
        if not isinstance(task_id, str) or not task_id: raise ValueError("verify_effect requires exact task target")
        if action.effect not in {"hold", "release"}:
            return self._result(action, "unsupported", "standalone readback is only supported for exact native hold/release markers", None)
        error = self._scope_and_target(action, action.effect, task_id)
        if error: return self._result(action, "conflict", error, None)
        try: before = self._snapshot(task_id)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"read-only effect verification unavailable: {exc}", None)
        outcome = self._verify_native_hold_release(action, before)
        if outcome is None:
            evidence = "exact scoped native marker, event, and target status" if action.effect == "hold" else "exact scoped UNBLOCK marker and target status"
            return self._result(action, "verified", f"{evidence} verified read-only", before)
        if outcome == "unsupported": return self._result(action, outcome, "native hold/release marker and event are absent; lane alone cannot prove an effect", before)
        if outcome == "partial": return self._result(action, outcome, "native marker is present but target has active or advanced beyond-ready work", before)
        if outcome == "unknown": return self._result(action, outcome, "native marker history is incomplete and cannot prove the effect", before)
        return self._result(action, "conflict", "native marker history or target state contradicts the action", before)

__all__ = ["BoardCapabilities", "HermesBoardAdapter"]
