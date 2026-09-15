from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from hermes_cli.sqlite_util import open_db


def raw_snapshot_from_typed(snapshot, *, comments=(), events=(), latest_summary=None, parents=(), children=()):
    """Build the complete test-only equivalent of Hermes ``show --json``."""
    task = snapshot.task
    raw_task = {
        "id": task.id, "title": task.title, "body": task.body, "status": task.status,
        "workspace_path": task.workspace_path, "assignee": task.assignee,
        "workspace_kind": task.workspace_kind, "session_id": snapshot.session_id,
        "branch_name": snapshot.branch_name, "started_at": snapshot.started_at,
        "completed_at": snapshot.completed_at,
    }
    runs = [{"id": r.id, "status": r.status, "outcome": r.outcome, "started_at": r.started_at,
             "ended_at": r.ended_at, "summary": r.summary, "profile": r.profile,
             "worker_pid": r.worker_pid, "metadata": r.metadata} for r in snapshot.runs]
    return {"task": raw_task, "latest_summary": latest_summary, "parents": list(parents),
            "children": list(children), "comments": list(comments), "events": list(events),
            "runs": runs}


def initialize_board(path: Path, *, board: str = "test", task_id: str = "T", status: str = "blocked") -> Path:
    """Create one explicit production-shaped Hermes board DB for tests."""
    path = Path(path)
    initializer = (
        "from pathlib import Path; import sys; "
        "from hermes_cli.kanban_db_connect import init_db; "
        "path=Path(sys.argv[1]); assert path.parent == Path.cwd(); "
        "assert not path.exists() and not path.is_symlink(); "
        f"init_db(path, board={board!r})"
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1"}
    result = subprocess.run((sys.executable, "-c", initializer, str(path)), cwd=path.parent,
                            env=env, text=True, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr.strip() or result.stdout.strip())
    with open_db(path, db_label=f"kanban:{board}", busy_timeout_ms=5000, wal=False, check_same_thread=False) as conn:
        conn.execute(
            "INSERT INTO tasks (id,title,body,assignee,status,priority,created_by,created_at,workspace_kind,workspace_path,branch_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "title", "body", "impl", status, 1, "test", 1, "worktree", "/tmp/wt", "main"),
        )
        conn.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                     (task_id, "created", '{"status":"%s"}' % status, 1))
        conn.commit()
    return path
