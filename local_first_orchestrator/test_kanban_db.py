from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


_TEST_FILENAME = re.compile(r"(?:^|[-_.])test(?:[-_.]|$)", re.IGNORECASE)


def _reject_if_symlinked(path: Path, *, root: Path) -> None:
    current = root
    relative_parent = path.parent.relative_to(root)
    for part in relative_parent.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("test database path cannot contain symlinks")


def init_test_kanban_db(db_path: Path, *, temp_root: Path, board: str) -> Path:
    """Initialize a fresh test board through Hermes in a fenced subprocess.

    This helper deliberately does not alter Hermes' delegated-child predicate. It
    gives the subprocess only the test path and a minimal environment, so no
    default or production board resolution can be reached.
    """
    root_input = Path(temp_root)
    path_input = Path(db_path)
    if not root_input.is_absolute() or not path_input.is_absolute():
        raise ValueError("temporary root and test database path must be absolute")
    if root_input.is_symlink() or path_input.is_symlink():
        raise ValueError("test database path cannot contain symlinks")
    if not root_input.is_dir():
        raise ValueError("supplied temporary root must be a directory")
    root = root_input.resolve(strict=True)
    path = path_input.resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("test database path must be inside the supplied temporary root") from exc
    _reject_if_symlinked(path_input, root=root_input)
    _reject_if_symlinked(path, root=root)
    if path.exists() or path.is_symlink():
        raise ValueError("test database path must not already exist")
    if not _TEST_FILENAME.search(path.name):
        raise ValueError("test database filename must identify a test database")

    code = (
        "import os, sys; "
        "assert not os.getenv('HERMES_DELEGATED_CHILD_CONTEXT'); "
        "assert not any(k.startswith('HERMES_KANBAN_') for k in os.environ); "
        "from pathlib import Path; "
        "from hermes_cli.kanban_db_connect import init_db; "
        "init_db(Path(sys.argv[1]), board=sys.argv[2])"
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1"}
    result = subprocess.run(
        (sys.executable, "-c", code, str(path), board),
        cwd=str(root),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"Hermes test DB initialization failed: {detail}")
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Hermes test DB initialization did not create the requested file")
    return path
