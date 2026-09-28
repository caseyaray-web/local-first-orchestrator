"""M0 native CLI characterization. No model calls, dispatch, or production board access."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def native(tmp_path):
    binary = os.environ.get("HERMES_M0_CLI")
    if not binary or not Path(binary).is_file():
        pytest.skip("Set HERMES_M0_CLI to the installed Hermes executable")
    home = tmp_path / "hermes"
    home.mkdir()
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_KANBAN_BOARD="m0-fixture")
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT",
                "HERMES_PROFILE", "HERMES_KANBAN_TASK"):
        env.pop(key, None)
    def run(*args, ok=True):
        p = subprocess.run([binary, "kanban", *args], env=env,
                           capture_output=True, text=True, timeout=30)
        if ok and p.returncode:
            pytest.fail(f"{args!r}: {p.returncode}: {p.stdout}\n{p.stderr}")
        return p
    assert str(home) in run("boards", "create", "m0-fixture").stdout
    def scoped(*args, ok=True):
        return run("--board", "m0-fixture", *args, ok=ok)
    return scoped, home


def create(native, name, *, held=False):
    run, _ = native
    args = ["create", name, "--json"]
    if held:
        args += ["--initial-status", "blocked"]
    return json.loads(run(*args).stdout)["id"]


def read(native, task_id):
    run, _ = native
    return json.loads(run("show", task_id, "--json").stdout)


def test_held_creation_does_not_dispatch_or_touch_real_home(native):
    run, home = native
    task = create(native, "held fixture", held=True)
    assert read(native, task)["task"]["status"] == "blocked"
    assert (home / "kanban" / "boards" / "m0-fixture" / "kanban.db").exists()
    assert json.loads(run("runs", task, "--json").stdout) == []


def test_done_and_archive_release_native_dependents(native):
    run, _ = native
    done_parent = create(native, "done parent", held=True)
    done_child = create(native, "done child", held=True)
    run("link", done_parent, done_child)
    run("unblock", done_child)
    assert read(native, done_child)["task"]["status"] == "todo"
    run("complete", done_parent, "--result", "fixture completion only")
    assert read(native, done_child)["task"]["status"] == "ready"

    archive_parent = create(native, "archive parent", held=True)
    archive_child = create(native, "archive child", held=True)
    run("link", archive_parent, archive_child)
    run("unblock", archive_child)
    assert read(native, archive_child)["task"]["status"] == "todo"
    run("archive", archive_parent)
    assert read(native, archive_child)["task"]["status"] == "ready"


def test_non_review_run_cannot_request_changes(native):
    run, _ = native
    task = create(native, "review ownership", held=True)
    outcome = run("request-changes", task, "changes", ok=False)
    assert outcome.returncode != 0
    assert read(native, task)["task"]["status"] == "blocked"


def test_manual_claim_cannot_start_review_and_review_remains_waiting(native):
    run, _ = native
    task = create(native, "review candidate", held=True)
    run("unblock", task)
    run("request-review", task, "--summary", "fixture candidate")
    assert read(native, task)["task"]["status"] == "review"
    claim = run("claim", task, ok=False)
    assert claim.returncode != 0
    assert "status=review" in claim.stderr
    assert read(native, task)["task"]["status"] == "review"


def test_idempotency_key_reuses_existing_unarchived_card_sequentially(native):
    run, _ = native
    first = json.loads(run("create", "one", "--initial-status", "blocked",
                           "--idempotency-key", "m0-key", "--json").stdout)
    second = json.loads(run("create", "two", "--initial-status", "blocked",
                            "--idempotency-key", "m0-key", "--json").stdout)
    assert second["id"] == first["id"]
    assert first["title"] == "one"


def test_hold_and_release_are_explicit_native_transitions(native):
    run, _ = native
    task = create(native, "ready candidate")
    assert read(native, task)["task"]["status"] == "ready"
    run("block", task, "m0 native hold")
    assert read(native, task)["task"]["status"] == "blocked"
    run("unblock", task)
    assert read(native, task)["task"]["status"] == "ready"


def test_completed_hook_sees_already_released_child(native):
    _, home = native
    binary = os.environ["HERMES_M0_CLI"]
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_KANBAN_BOARD="m0-fixture")
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT"):
        env.pop(key, None)
    script = '''
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
with kbc.connect(board="m0-fixture") as conn:
    parent = kb.create_task(conn, title="parent", assignee="fixture")
    child = kb.create_task(conn, title="dependent", assignee="fixture", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    original = kb._fire_task_hook
    observed = []
    def hook(*args, **kwargs):
        observed.append(kb.get_task(conn, child).status)
    kb._fire_task_hook = hook
    try:
        assert kb.complete_task(conn, parent, result="fixture only")
    finally:
        kb._fire_task_hook = original
    assert observed == ["ready"], observed
'''
    p = subprocess.run([str(Path(binary).with_name("python")), "-c", script],
                       capture_output=True, text=True, timeout=30, env=env)
    assert p.returncode == 0, p.stdout + p.stderr


def test_dispatcher_review_claim_creates_fresh_review_run(native):
    run, home = native
    task = json.loads(run("create", "review candidate", "--assignee", "implementer",
                          "--json").stdout)["id"]
    run("request-review", task, "--summary", "candidate", "--reviewer", "reviewer")
    binary = os.environ["HERMES_M0_CLI"]
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_KANBAN_BOARD="m0-fixture")
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT"):
        env.pop(key, None)
    script = f'''from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
with kbc.connect(board="m0-fixture") as conn:
    claimed = kb.claim_review_task(conn, "{task}", claimer="fixture-review")
    assert claimed and claimed.status == "running", claimed
'''
    p = subprocess.run([str(Path(binary).with_name("python")), "-c", script],
                       capture_output=True, text=True, timeout=30, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    observed = read(native, task)
    assert observed["task"]["status"] == "running"
    runs = json.loads(run("runs", task, "--json").stdout)
    assert runs and runs[-1]["profile"] == "reviewer"
    assert any(e["kind"] == "claimed" and "review" in str(e.get("payload"))
               for e in observed["events"])


def test_held_card_is_not_in_dispatch_preview(native):
    run, _ = native
    task = create(native, "held for dispatch preview", held=True)
    preview = run("dispatch", "--dry-run", "--json")
    assert task not in preview.stdout
    assert read(native, task)["task"]["status"] == "blocked"
