"""M0 native CLI characterization. No model calls, dispatch, or production board access."""
import json
import os
from pathlib import Path
import re
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
    listed = json.loads(run("list", "--json").stdout)
    assert task in str(listed)


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
    plugin = home / "plugins" / "m0_hook"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        "name: m0_hook\nversion: 0.1.0\ndescription: isolated completion observer\n")
    (plugin / "__init__.py").write_text('''def register(ctx):
    def on_complete(**kw):
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
        with kbc.connect(board="m0-fixture") as conn:
            status = kb.get_task(conn, os.environ["M0_CHILD"]).status
        Path(os.environ["M0_MARKER"]).write_text(status)
    ctx.register_hook("kanban_task_completed", on_complete)
''')
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - m0_hook\n")
    marker = home / "hook-observation.txt"
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_KANBAN_BOARD="m0-fixture", M0_MARKER=str(marker))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT"):
        env.pop(key, None)
    script = '''
import os
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
with kbc.connect(board="m0-fixture") as conn:
    parent = kb.create_task(conn, title="parent", assignee="default")
    child = kb.create_task(conn, title="dependent", assignee="default", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    os.environ["M0_CHILD"] = child
    assert kb.complete_task(conn, parent, result="fixture only")
'''
    p = subprocess.run([str(Path(binary).with_name("python")), "-c", script],
                       capture_output=True, text=True, timeout=30, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert marker.is_file(), "isolated kanban_task_completed hook did not write its observation"
    assert marker.read_text() == "ready"


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


def test_concurrent_create_idempotency_key_does_not_serialize(native):
    _, home = native
    binary = os.environ["HERMES_M0_CLI"]
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_KANBAN_BOARD="m0-fixture")
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT"):
        env.pop(key, None)
    script = '''
import threading
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
barrier = threading.Barrier(2, timeout=10)
original = kb._new_task_id
def synchronized_id():
    barrier.wait()
    return original()
kb._new_task_id = synchronized_id
outcomes = []
def create(n):
    with kbc.connect(board="m0-fixture") as conn:
        outcomes.append(kb.create_task(conn, title=f"concurrent-{n}",
                         idempotency_key="same", initial_status="blocked"))
threads = [threading.Thread(target=create, args=(n,)) for n in range(2)]
for thread in threads: thread.start()
for thread in threads: thread.join(timeout=15)
assert all(not thread.is_alive() for thread in threads)
assert len(outcomes) == 2 and len(set(outcomes)) == 2, outcomes
'''
    p = subprocess.run([str(Path(binary).with_name("python")), "-c", script],
                       capture_output=True, text=True, timeout=30, env=env)
    assert p.returncode == 0, p.stdout + p.stderr


def test_block_after_dispatch_claim_does_not_stop_live_stub_worker(native):
    _, home = native
    binary = os.environ["HERMES_M0_CLI"]
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_KANBAN_BOARD="m0-fixture")
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT"):
        env.pop(key, None)
    script = '''
import subprocess, sys
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
with kbc.connect(board="m0-fixture") as conn:
    task = kb.create_task(conn, title="race", assignee="default")
    workers = []
    def spawn(t, workspace):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        workers.append(process)
        return process.pid
    try:
        result = dispatch.dispatch_once(conn, spawn_fn=spawn, board="m0-fixture",
                                        max_spawn=1, reconcile_orphans=False)
        assert len(workers) == 1, result
        assert kb.get_task(conn, task).status == "running"
        assert kb.block_task(conn, task, reason="late hold", kind="needs_input")
        assert kb.get_task(conn, task).status == "blocked"
        assert workers[0].poll() is None, "block falsely appeared to stop stub worker"
    finally:
        for worker in workers:
            if worker.poll() is None: worker.terminate()
            worker.wait(timeout=10)
'''
    p = subprocess.run([str(Path(binary).with_name("python")), "-c", script],
                       capture_output=True, text=True, timeout=40, env=env)
    assert p.returncode == 0, p.stdout + p.stderr


def test_reclaim_stops_exact_stub_but_releases_card_to_ready(native):
    _, home = native
    binary = os.environ["HERMES_M0_CLI"]
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_KANBAN_BOARD="m0-fixture")
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT"):
        env.pop(key, None)
    script = '''
import subprocess, sys
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
with kbc.connect(board="m0-fixture") as conn:
    task = kb.create_task(conn, title="stop stub", assignee="default")
    workers = []
    def spawn(t, workspace):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        workers.append(process)
        return process.pid
    try:
        dispatch.dispatch_once(conn, spawn_fn=spawn, board="m0-fixture", max_spawn=1,
                               reconcile_orphans=False)
        assert len(workers) == 1
        assert kb.get_task(conn, task).status == "running"
        assert kb.reclaim_task(conn, task, reason="fixture stop")
        workers[0].wait(timeout=10)
        assert kb.get_task(conn, task).status == "ready"
    finally:
        for worker in workers:
            if worker.poll() is None: worker.terminate()
            worker.wait(timeout=10)
'''
    p = subprocess.run([str(Path(binary).with_name("python")), "-c", script],
                       capture_output=True, text=True, timeout=40, env=env)
    assert p.returncode == 0, p.stdout + p.stderr


def test_isolated_profile_plugin_hook_loading(native):
    _, home = native
    binary = os.environ["HERMES_M0_CLI"]
    for profile in ("planner", "reviewer"):
        profile_home = home / "profiles" / profile
        plugin = profile_home / "plugins" / "m0_probe"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text(
            "name: m0_probe\nversion: 0.1.0\ndescription: isolated M0 hook probe\n")
        (plugin / "__init__.py").write_text(
            "def register(ctx):\n"
            "    ctx.register_hook('kanban_task_completed', lambda **kw: kw['task_id'])\n"
            "    ctx.register_hook('pre_tool_call', lambda **kw: {'action': 'block', 'message': 'BLOCKED: fixture'} if kw.get('tool_name') == 'kanban_complete' else None)\n")
        (profile_home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - m0_probe\n")
        env = os.environ.copy()
        env.update(HERMES_HOME=str(profile_home), HERMES_KANBAN_HOME=str(home))
        script = '''from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
manager.discover_and_load()
assert "m0_probe" in manager._plugins, manager._plugins
assert manager._hooks["kanban_task_completed"][0](task_id="fixture") == "fixture"
assert manager._hooks["pre_tool_call"][0](tool_name="kanban_complete")["action"] == "block"
from model_tools import handle_function_call
blocked = handle_function_call("kanban_complete", {"task_id": "t_fixture", "summary": "fixture only"}, session_id="m0")
assert "fixture" in blocked and "BLOCKED" in blocked.upper(), blocked
assert manager.unload("m0_probe")
assert "kanban_task_completed" not in manager._hooks
'''
        p = subprocess.run([str(Path(binary).with_name("python")), "-c", script],
                           capture_output=True, text=True, timeout=30, env=env)
        assert p.returncode == 0, f"{profile}: {p.stdout}{p.stderr}"


def test_wrong_review_run_is_refused_without_changing_card(native):
    run, home = native
    task = json.loads(run("create", "wrong run candidate", "--assignee", "implementer",
                          "--json").stdout)["id"]
    run("request-review", task, "--summary", "candidate", "--reviewer", "reviewer")
    binary = os.environ["HERMES_M0_CLI"]
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_KANBAN_BOARD="m0-fixture")
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT"):
        env.pop(key, None)
    script = f'''from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
with kbc.connect(board="m0-fixture") as conn:
    claimed = kb.claim_review_task(conn, "{task}", claimer="fixture-review")
    assert claimed and claimed.current_run_id is not None
    ok, reason = kb.request_changes(conn, "{task}", reason="fixture changes",
                                    expected_run_id=claimed.current_run_id + 1)
    assert not ok and reason == "run_id mismatch", (ok, reason)
    assert kb.get_task(conn, "{task}").status == "running"
'''
    p = subprocess.run([str(Path(binary).with_name("python")), "-c", script],
                       capture_output=True, text=True, timeout=30, env=env)
    assert p.returncode == 0, p.stdout + p.stderr


def test_dispatcher_spawns_fresh_review_stub_run(native):
    _, home = native
    binary = os.environ["HERMES_M0_CLI"]
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_KANBAN_BOARD="m0-fixture")
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT"):
        env.pop(key, None)
    script = '''
import subprocess, sys
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
with kbc.connect(board="m0-fixture") as conn:
    task = kb.create_task(conn, title="review dispatch", assignee="implementer")
    assert kb.request_review(conn, task, summary="candidate", reviewer="default")
    workers = []
    def spawn(t, workspace):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        workers.append(p)
        return p.pid
    try:
        dispatch.dispatch_once(conn, spawn_fn=spawn, board="m0-fixture",
                               max_spawn=1, reconcile_orphans=False)
        assert len(workers) == 1
        claimed = kb.get_task(conn, task)
        assert claimed.status == "running" and claimed.current_run_id is not None
        run = conn.execute("SELECT profile FROM task_runs WHERE id = ?", (claimed.current_run_id,)).fetchone()
        assert run["profile"] == "default"
        event = kb._latest_event(conn, task, "claimed", claimed.current_run_id)
        assert '"source_status": "review"' in event["payload"]
        assert kb.reclaim_task(conn, task, reason="fixture cleanup")
        workers[0].wait(timeout=10)
        assert kb.get_task(conn, task).status == "review"
    finally:
        for worker in workers:
            if worker.poll() is None: worker.terminate()
            worker.wait(timeout=10)
'''
    p = subprocess.run([str(Path(binary).with_name("python")), "-c", script],
                       capture_output=True, text=True, timeout=40, env=env)
    assert p.returncode == 0, p.stdout + p.stderr


def test_documented_cli_help_matches_target_host(native):
    run, _ = native
    snapshot = (Path(__file__).parents[1] / "docs" / "compatibility-cli-help.md").read_text()
    blocks = re.findall(r"^## ([a-z-]+)\n\n```text\n(.*?)\n```", snapshot,
                        re.MULTILINE | re.DOTALL)
    assert len(blocks) == 15
    for verb, expected in blocks:
        actual = run(verb, "--help").stdout
        marker = "usage: hermes kanban "
        assert marker in actual, verb
        assert actual[actual.index(marker):].strip() == expected.strip(), verb


def test_held_card_is_not_in_dispatch_preview(native):
    run, _ = native
    task = create(native, "held for dispatch preview", held=True)
    preview = run("dispatch", "--dry-run", "--json")
    assert task not in preview.stdout
    assert read(native, task)["task"]["status"] == "blocked"
