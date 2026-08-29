from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

from .comment_delivery import MarkerLookup
from .states import CanonicalState


@dataclass(frozen=True)
class ExternalTicket:
    id: str
    title: str
    body: str
    status: str
    workspace_path: str | None


class HermesBoardAdapter:
    """Hermes CLI adapter. Writes require explicit opt-in; reads are always safe."""
    is_fake = False

    def __init__(self, *, board: str, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, executable: str, allow_writes: bool = False, timeout_seconds: int = 15, output_limit: int = 200_000) -> None:
        if timeout_seconds < 1 or output_limit < 1: raise ValueError("positive process limits required")
        if not board or not board.replace("-", "").replace("_", "").isalnum(): raise ValueError("explicit board slug required")
        path=Path(executable)
        if not path.is_absolute() or not path.is_file(): raise ValueError("Hermes executable must be an absolute existing path")
        self.runner, self.executable, self.board, self.allow_writes, self.timeout_seconds, self.output_limit = runner, str(path), board, allow_writes, timeout_seconds, output_limit

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
        return ExternalTicket(str(row["id"]), str(row.get("title") or ""), str(row.get("body") or ""), str(row.get("status") or ""), row.get("workspace_path"))

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
        """Current machine-readable Hermes reads do not expose comment bodies."""
        return MarkerLookup.UNSUPPORTED

    def set_state(self, ticket_id: str, state: CanonicalState, *, idempotency_key: str) -> None:
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        if state == CanonicalState.DONE:
            self._run("complete", ticket_id, "--result", f"local-first projection {idempotency_key}")
        elif state in {CanonicalState.BLOCKED, CanonicalState.NEEDS_TRIAGE, CanonicalState.NEEDS_CHECKPOINT, CanonicalState.NEEDS_HUMAN_TEST}:
            self._run("block", ticket_id, f"local-first projection {state.value} ({idempotency_key})", "--kind", "needs_input")
        else:
            # Scheduled is deliberately non-dispatchable by Hermes; the local-first
            # controller owns execution and avoids racing the gateway dispatcher.
            self._run("schedule", ticket_id, f"local-first projection {state.value} ({idempotency_key})")

    def add_comment(self, ticket_id: str, comment: str) -> None:
        if not self.allow_writes:
            raise PermissionError("real board writes require --allow-board-writes")
        self._run("comment", ticket_id, comment, "--author", "local-first-orchestrator")
