from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.execution_handoff import HANDOFF_MARKER
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.states import CanonicalState


class RoutingAuthorityBoundaryTests(unittest.TestCase):
    def test_final_unblock_read_rejects_routing_drift(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / ".worktrees" / "child"
            workspace.mkdir(parents=True)
            first = {"id": "child", "status": "blocked", "body": HANDOFF_MARKER, "assignee": "impl", "workspace_kind": "worktree", "workspace_path": str(workspace)}
            changed = {**first, "assignee": "changed"}
            shown = [first, changed]
            commands: list[tuple[str, ...]] = []

            def runner(argv, **kwargs):
                commands.append(tuple(argv))
                if "show" in argv:
                    task = shown.pop(0)
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"task": task, "parents": [], "children": []}), "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            adapter = HermesBoardAdapter(
                board="isolated", executable="/bin/true", runner=runner, allow_writes=True,
                implementation_profile="impl", canonical_repository=root,
            )
            routing = adapter.verify_native_release_task(adapter.get_task("child"), expected_workspace_path=str(workspace))
            with self.assertRaisesRegex(RuntimeError, "native release authority mismatch"):
                adapter.set_state("child", CanonicalState.READY_LOCAL, idempotency_key="release", expected_routing=routing)
            self.assertFalse(any("unblock" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
