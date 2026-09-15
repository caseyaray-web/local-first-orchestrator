from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from local_first_orchestrator.test_kanban_db import init_test_kanban_db


def test_delegated_parent_can_initialize_only_owned_test_db_without_leaking_identity() -> None:
    with TemporaryDirectory() as temp_name:
        root = Path(temp_name)
        db_path = root / "delegated-test.db"
        outside = root.parent / "outside-test.db"
        old = {key: os.environ.get(key) for key in (
            "HERMES_DELEGATED_CHILD_CONTEXT",
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_GOAL_MODE",
            "HERMES_KANBAN_GOAL_MAX_TURNS",
        )}
        try:
            os.environ.update({
                "HERMES_DELEGATED_CHILD_CONTEXT": "1",
                "HERMES_KANBAN_TASK": "parent-task",
                "HERMES_KANBAN_RUN_ID": "42",
                "HERMES_KANBAN_CLAIM_LOCK": "parent-lock",
            })
            assert init_test_kanban_db(db_path, temp_root=root, board="delegated-test") == db_path
            assert db_path.is_file()
            with pytest.raises(ValueError, match="inside the supplied temporary root"):
                init_test_kanban_db(outside, temp_root=root, board="delegated-test")
            probe = subprocess.run(
                (sys.executable, "-c", "import os; forbidden=('HERMES_DELEGATED_CHILD_CONTEXT','HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_KANBAN_CLAIM_LOCK'); assert not any(os.getenv(k) for k in forbidden)"),
                env={"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1"},
                check=False,
            )
            assert probe.returncode == 0
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
