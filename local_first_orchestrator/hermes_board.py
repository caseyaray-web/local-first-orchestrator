from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

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

    def __init__(self, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, executable: str = "hermes", allow_writes: bool = False) -> None:
        self.runner, self.executable, self.allow_writes = runner, executable, allow_writes

    def _run(self, *args: str) -> Any:
        completed = self.runner((self.executable, "kanban", *args), text=True, capture_output=True, timeout=60, check=False)
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Hermes Kanban CLI failed")
        return json.loads(completed.stdout) if "--json" in args else completed.stdout

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
