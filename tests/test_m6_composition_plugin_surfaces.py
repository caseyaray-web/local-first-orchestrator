from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import zipfile

import pytest

from local_first_orchestrator.config import PluginConfig
from local_first_orchestrator.composition import (_combined_checks, _git_observer, _repository_telemetry,
                                                  build_runtime, close_runtime, initialize_store)
from local_first_orchestrator.contracts import BoardSnapshot, ManagedMember
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.plugin_tools import TOOL_NAMES, register_tools
from local_first_orchestrator.cli import register_cli


class _Context:
    def __init__(self) -> None:
        self.tools = {}
        self.cli = []
        self.hooks = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


    def register_cli_command(self, *args, **kwargs):
        self.cli.append((args, kwargs))

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def test_public_finalization_cli_requires_only_the_operation_key():
    parser = argparse.ArgumentParser()
    register_cli(parser)
    args = parser.parse_args(["--config", "/trusted/config.json", "--board", "fixture", "--anchor-task-id", "anchor",
                              "finalize-local-review", "--operation-key", "handoff-1"])
    assert args.command == "finalize-local-review"
    assert args.operation_key == "handoff-1"


class _Board:
    is_fake = True

    def __init__(self) -> None:
        self.create_lock_assertion = None
        self.claim_create_attempt = None

    def list_tasks(self):
        return ()

    def read_task(self, task_id):
        raise AssertionError(f"unexpected board read: {task_id}")

    def hold(self, *args):
        raise AssertionError("unexpected board write")

    def release(self, *args):
        raise AssertionError("unexpected board write")

    def stop_run(self, *args):
        raise AssertionError("unexpected board write")


def _config(tmp_path: Path) -> PluginConfig:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    executable = tmp_path / "hermes"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    return PluginConfig.from_mapping({
        "version": 1,
        "state_root": str(state),
        "hermes_executable": str(executable),
        "hermes_home": str(home),
        "kanban_home": str(home),
        "trusted_roots": {"repository": str(workspace), "workspace": str(workspace)},
        "roles": {"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "planner", "paid_review_profile": "paid"},
        "budgets": {"implementation_attempts": 1, "review_corrections": 1, "infrastructure_retries": 1, "workflow_repairs": 1, "paid_capacity": 1},
        "poll_interval_seconds": 1,
        "scope": {"board_id": "fixture", "anchor_task_id": "anchor"},
        "check_commands": [{"check_id": "fixture", "argv": [str(executable)]}],
    })


def test_composition_is_explicit_and_does_not_open_store_until_requested(tmp_path: Path):
    config = _config(tmp_path)
    db = config.state_root / "evidence.sqlite3"
    assert not db.exists()
    initialize_store(config)
    runtime = build_runtime(config, board=_Board(), scope={"board_id": "fixture", "anchor_task_id": "anchor"})
    try:
        assert db.is_file()
        assert runtime.coordinator.scope == {"board_id": "fixture", "anchor_task_id": "anchor"}
    finally:
        close_runtime(runtime)


def test_tool_registration_is_complete_and_import_side_effect_free():
    ctx = _Context()
    register_tools(ctx, runtime_factory=lambda: (_ for _ in ()).throw(AssertionError("handler executed during registration")))
    assert set(ctx.tools) == set(TOOL_NAMES)
    assert all(item["toolset"] == "local_first_orchestrator" for item in ctx.tools.values())


def test_plugin_register_wires_native_cli_tools_and_fast_hooks_without_runtime_factory(monkeypatch):
    import importlib.util

    entrypoint = Path(__file__).parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("m6_plugin_entrypoint", entrypoint)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    ctx = _Context()
    monkeypatch.setattr(module, "runtime_factory", lambda: (_ for _ in ()).throw(AssertionError("registration must not compose runtime")))
    module.register(ctx)
    assert set(ctx.tools) == set(TOOL_NAMES)
    assert ctx.cli
    assert "pre_tool_call" in ctx.hooks


def test_plugin_config_rejects_missing_budget_and_untrusted_root(tmp_path: Path):
    with pytest.raises(ValueError, match="budgets"):
        PluginConfig.from_mapping({"version": 1})
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    outside.chmod(0o700)
    executable = outside / "hermes"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    with pytest.raises(ValueError, match="trusted_roots"):
        PluginConfig.from_mapping({
            "version": 1, "state_root": str(outside), "hermes_executable": str(executable),
            "hermes_home": str(outside), "kanban_home": str(outside),
            "trusted_roots": {"repository": str(outside)},
            "roles": {"implementation_profile": "i", "local_review_profile": "r", "planning_profile": "p", "paid_review_profile": "q"},
            "budgets": {"implementation_attempts": 1, "review_corrections": 1, "infrastructure_retries": 1, "workflow_repairs": 1, "paid_capacity": 1},
            "poll_interval_seconds": 1,
            "scope": {"board_id": "fixture", "anchor_task_id": "anchor"},
            "check_commands": [{"check_id": "fixture", "argv": [str(executable)]}],
        })


def test_packaging_declares_plugin_root_registration_and_dashboard_assets():
    import tomllib

    metadata = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = metadata["tool"]["setuptools"]["data-files"]
    assets = {asset for paths in data_files.values() for asset in paths}
    assert {"plugin.yaml", "__init__.py", "dashboard/manifest.json", "dashboard/plugin_api.py", "dashboard/dist/index.js"} <= assets
    manifest = (Path(__file__).parents[1] / "plugin.yaml").read_text(encoding="utf-8")
    tool_section = manifest.split("provides_tools:\n", 1)[1].split("provides_hooks:", 1)[0]
    declared_tools = [line.strip()[2:] for line in tool_section.splitlines() if line.strip().startswith("- ")]
    assert len(declared_tools) == len(set(declared_tools))
    assert set(declared_tools) == set(TOOL_NAMES)


def test_production_git_and_check_callbacks_use_temporary_trusted_git_only(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    repo = config.trusted_roots["repository"]
    (repo / "proof.txt").write_text("fixture\n", encoding="utf-8")
    for argv in (("git", "init"), ("git", "config", "user.email", "fixture@example.test"),
                 ("git", "config", "user.name", "Fixture"), ("git", "add", "proof.txt"),
                 ("git", "commit", "-m", "fixture")):
        subprocess.run(argv, cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-fixture")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "run-fixture")
    monkeypatch.setenv("HERMES_SESSION_ID", "session-fixture")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", config.scope["board_id"])
    observed = _git_observer(config, config.scope)
    assert observed["candidate"]["originating_run_id"] == "run-fixture"
    assert observed["checks"][0]["outcome"] == "passed"
    head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    assert _combined_checks(config, {"head_sha": head})["checks"][0]["exit_code"] == 0
    with pytest.raises(ValueError, match="scope"):
        _git_observer(config, {"board_id": "other", "anchor_task_id": "anchor"})


def test_registered_local_review_handoff_status_and_submission_use_frozen_evidence(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    repo = config.trusted_roots["repository"]
    (repo / "proof.txt").write_text("fixture\n", encoding="utf-8")
    for argv in (("git", "init"), ("git", "config", "user.email", "fixture@example.test"),
                 ("git", "config", "user.name", "Fixture"), ("git", "add", "proof.txt"),
                 ("git", "commit", "-m", "fixture")):
        subprocess.run(argv, cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    initialize_store(config)
    store = EvidenceStore.open(config.evidence_store_path, create_new=False)
    try:
        store.register_member(ManagedMember("fixture", "anchor", "implementation-task", "implementation", 0, (), "fixture-work"))
    finally:
        store.close()

    class Board(_Board):
        def __init__(self):
            super().__init__()
            self.implementation_run = {"id": "implementation-run", "profile": "implementer", "status": "running",
                                       "metadata": None}
            self.card = BoardSnapshot({"id": "implementation-task", "status": "running", "assignee": "implementer"},
                                      (), (self.implementation_run,), (), (), (), "fixture", "implementation-task")
            self.anchor = BoardSnapshot({"id": "anchor", "status": "blocked"}, (), (), (), (), (), "fixture", "anchor")

        def list_tasks(self):
            return (self.anchor, self.card)

        def read_task(self, task_id):
            assert task_id in {"anchor", "implementation-task"}
            return self.anchor if task_id == "anchor" else self.card

        def read_scoped_run(self, scope, task_id, run_id):
            assert scope == config.scope and task_id == "implementation-task"
            return next(run for run in self.card.runs if run["id"] == run_id)

        def complete_review(self, marker):
            self.implementation_run = {"id": "implementation-run", "profile": "implementer", "status": "completed",
                                       "outcome": "review_requested", "metadata": {"worker_session_id": "implementation-session",
                                                                                      "local_first_review": marker}}
            reviewer_run = {"id": "reviewer-run", "profile": "reviewer", "status": "completed",
                            "outcome": "completed", "metadata": {"worker_session_id": "reviewer-session"}}
            self.card = BoardSnapshot({"id": "implementation-task", "status": "done", "assignee": "reviewer"}, (),
                                      (self.implementation_run, reviewer_run), (), (
                                          {"kind": "review_requested", "run_id": "implementation-run",
                                           "payload": {"implementer": "implementer", "reviewer": "reviewer"}},
                                          {"kind": "claimed", "run_id": "reviewer-run",
                                           "payload": {"source_status": "review"}},
                                          {"kind": "completed", "run_id": "reviewer-run", "payload": {"summary": "approved"}},
                                      ), (), "fixture", "implementation-task")

    board, ctx = Board(), _Context()
    reviewer_context = {"active": False}
    def runtime_factory(_scope):
        runtime = build_runtime(config, board=board, scope=config.scope)
        if reviewer_context["active"]:
            runtime.coordinator.git_observer = lambda _scope: (_ for _ in ()).throw(
                AssertionError("reviewer must consume the frozen handoff"))
        return runtime
    register_tools(ctx, runtime_factory=runtime_factory)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "implementation-task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implementation-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implementation-session")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", config.scope["board_id"])
    arguments = {**config.scope, "operation_key": "local-handoff-fixture", "summary": "fixture candidate"}
    observed = json.loads(ctx.tools["local_first_request_local_review"]["handler"](arguments))
    assert observed["ok"] is True
    assert observed["result"]["outcome"] == "proposed"
    # The reviewer consumes this exact detached JSON record through the
    # registered read-only tool, not a coordinator helper or Python repr.
    status = json.loads(ctx.tools["local_first_status"]["handler"](dict(config.scope)))
    assert status["ok"] is True, status
    handoff = status["result"]["review_handoffs"]
    assert handoff == [{"state": "pending", "operation_key": "local-handoff-fixture", "phase": "pending",
                        "task_id": "implementation-task", "reviewer_profile": "reviewer",
                        "review_marker": handoff[0]["review_marker"],
                        "reason": "awaiting_native_request_review_finalization"}]
    # The isolated transport now exposes the exact native request-review marker
    # and a distinct completed reviewer run.  No native effect or provider runs.
    board.complete_review(handoff[0]["review_marker"])
    # Native transition happens outside this plugin. Public finalization is
    # read-only and may run after review claim/completion with no worker env.
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(name, raising=False)
    runtime = build_runtime(config, board=board, scope=config.scope)
    try:
        finalized = runtime.coordinator.finalize_local_review("local-handoff-fixture")
    finally:
        close_runtime(runtime)
    assert finalized["outcome"] == "finalized"
    status = json.loads(ctx.tools["local_first_status"]["handler"](dict(config.scope)))
    finalized_handoff = next(item for item in status["result"]["review_handoffs"] if item["state"] == "finalized")
    frozen = finalized_handoff["frozen_handoff"]
    review = {
        "review_id": "reviewer-approved", "candidate_identity": frozen["candidate"], "reviewer_role": "local",
        "native_review": {"task_id": "implementation-task", "run_id": "reviewer-run",
                          "session_id": "reviewer-session", "profile": "reviewer"},
        "checks": frozen["checks"], "checks_identity": frozen["checks_identity"], "verdict": "approved",
        "criterion_evidence": [{"criterion_id": item, "outcome": "pass", "evidence": "reviewed frozen handoff"}
                               for item in frozen["criterion_ids"]], "findings": [],
    }
    reviewer_context["active"] = True
    # A sibling worker cannot submit a payload recorded by the actual reviewer:
    # registered-tool provenance must bind to the invoker's exact live context
    # before the coordinator is allowed to persist anything.
    monkeypatch.setenv("HERMES_KANBAN_TASK", "implementation-task")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", config.scope["board_id"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "other-reviewer-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "other-reviewer-session")
    mismatched = json.loads(ctx.tools["local_first_submit_review"]["handler"]({
        **config.scope, "task_id": "implementation-task", "candidate": frozen["candidate"], "review": review,
    }))
    assert mismatched["ok"] is False
    assert "active trusted worker identity" in mismatched["error"]
    after_mismatch = json.loads(ctx.tools["local_first_status"]["handler"](dict(config.scope)))
    assert after_mismatch["ok"] is True
    assert after_mismatch["result"]["reviews"] == []

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "reviewer-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "reviewer-session")
    submitted = json.loads(ctx.tools["local_first_submit_review"]["handler"]({
        **config.scope, "task_id": "implementation-task", "candidate": frozen["candidate"], "review": review,
    }))
    assert submitted == {"ok": True, "result": {"outcome": "accepted", "candidate": frozen["candidate"]["content_identity"],
                                                      "review_id": "reviewer-approved"}}
    recorded = json.loads(ctx.tools["local_first_status"]["handler"](dict(config.scope)))
    assert recorded["ok"] is True
    assert len(recorded["result"]["reviews"]) == 1


def test_operator_status_uses_read_only_repository_telemetry_not_worker_candidate_authority(tmp_path: Path, monkeypatch, capsys):
    config = _config(tmp_path)
    repo = config.trusted_roots["repository"]
    (repo / "proof.txt").write_text("fixture\n", encoding="utf-8")
    for argv in (("git", "init"), ("git", "config", "user.email", "fixture@example.test"),
                 ("git", "config", "user.name", "Fixture"), ("git", "add", "proof.txt"),
                 ("git", "commit", "-m", "fixture")):
        subprocess.run(argv, cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    initialize_store(config)
    check_marker = tmp_path / "configured-check-ran"
    config.hermes_executable.write_text("#!/bin/sh\nprintf checked > " + str(check_marker) + "\n", encoding="utf-8")

    class Board(_Board):
        def __init__(self):
            super().__init__()
            self.anchor = BoardSnapshot({"id": "anchor", "status": "blocked"}, (), (), (), (), (), "fixture", "anchor")

        def list_tasks(self):
            return (self.anchor,)

        def read_task(self, task_id):
            assert task_id == "anchor"
            return self.anchor

    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(name, raising=False)
    runtime = build_runtime(config, board=Board(), scope=config.scope)
    try:
        before = runtime.store.read_scope(config.scope)
        status = runtime.coordinator.status()
        after = runtime.store.read_scope(config.scope)
        assert status["repository_telemetry"]["state"] == "available"
        assert status["repository_telemetry"]["clean"] is True
        assert "candidate" not in status["repository_telemetry"]
        assert "checks" not in status["repository_telemetry"]
        assert not check_marker.exists()
        assert before == after
    finally:
        close_runtime(runtime)

    # The public CLI must remain readable before dispatch, while a native-shaped
    # worker is active, and after its environment has gone away.  Every call
    # constructs fresh composition, but status leaves the durable evidence and
    # budgets exactly as it found them.
    import local_first_orchestrator.cli as cli
    config_path = tmp_path / "trusted-config.json"
    config_path.write_text(json.dumps({
        "version": 1, "state_root": str(config.state_root), "hermes_executable": str(config.hermes_executable),
        "hermes_home": str(config.hermes_home), "kanban_home": str(config.kanban_home),
        "trusted_roots": {key: str(value) for key, value in config.trusted_roots.items()}, "roles": dict(config.roles),
        "budgets": {"implementation_attempts": 1, "review_corrections": 1, "infrastructure_retries": 1,
                    "workflow_repairs": 1, "paid_capacity": 1}, "poll_interval_seconds": config.poll_interval_seconds,
        "scope": dict(config.scope), "check_commands": [{"check_id": key, "argv": list(argv)} for key, argv in config.check_commands],
    }), encoding="utf-8")
    monkeypatch.setattr(cli, "build_runtime", lambda parsed, *, scope: build_runtime(parsed, board=Board(), scope=scope))
    store = EvidenceStore.open(config.evidence_store_path, create_new=False)
    try:
        cli_before = store.read_scope(config.scope)
    finally:
        store.close()
    command = ("--config", str(config_path), "--board", config.scope["board_id"],
               "--anchor-task-id", config.scope["anchor_task_id"], "status")
    for worker_env in (False, True, False):
        if worker_env:
            monkeypatch.setenv("HERMES_KANBAN_TASK", "task-fixture")
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "run-fixture")
            monkeypatch.setenv("HERMES_SESSION_ID", "session-fixture")
            monkeypatch.setenv("HERMES_KANBAN_BOARD", config.scope["board_id"])
        else:
            for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID", "HERMES_KANBAN_BOARD"):
                monkeypatch.delenv(name, raising=False)
        assert cli.main(command) == 0
        assert json.loads(capsys.readouterr().out)["status"]["repository_telemetry"]["state"] == "available"
        assert not check_marker.exists()
    store = EvidenceStore.open(config.evidence_store_path, create_new=False)
    try:
        assert store.read_scope(config.scope) == cli_before
    finally:
        store.close()

    shutil.rmtree(repo)
    assert _repository_telemetry(config, config.scope) == {
        "state": "unavailable", "error": {"code": "repository_observation_unavailable"}}


def _hermes_test_executable() -> Path:
    """Allow a pinned native CLI, otherwise use the installed CLI on PATH."""
    configured = os.environ.get("HERMES_TEST_CLI")
    executable = shutil.which(configured if configured is not None else "hermes")
    if executable is None:
        raise RuntimeError(
            "Hermes CLI is unavailable or not executable; set HERMES_TEST_CLI "
            "to an installed Hermes executable, or install hermes on PATH"
        )
    return Path(executable).absolute()


@pytest.mark.parametrize("mode", ["configured", "path", "invalid_override", "missing"])
def test_artifact_hermes_executable_discovery(tmp_path: Path, monkeypatch, mode):
    executable = tmp_path / "hermes"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.delenv("HERMES_TEST_CLI", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path) if mode in {"path", "invalid_override"} else "")
    if mode == "configured":
        monkeypatch.setenv("HERMES_TEST_CLI", str(executable))
    elif mode == "invalid_override":
        monkeypatch.setenv("HERMES_TEST_CLI", str(tmp_path / "not-installed"))
    if mode in {"missing", "invalid_override"}:
        with pytest.raises(RuntimeError, match="set HERMES_TEST_CLI"):
            _hermes_test_executable()
    else:
        assert _hermes_test_executable() == executable


def test_built_distribution_imports_every_module_and_registers_from_temporary_hermes_home(tmp_path: Path):
    """Exercise the wheel, not this checkout, through Hermes' native Plugin Doctor."""
    hermes = _hermes_test_executable()
    root = Path(__file__).parents[1]
    dist = tmp_path / "dist"
    dist.mkdir()
    # Build the sdist from this source tree, then the wheel from that sdist.  The
    # second step cannot inherit stale files from the intentionally preserved
    # checkout ``build/`` directory.
    subprocess.run(("uv", "build", "--offline", "--sdist", "--out-dir", str(dist)), cwd=root, check=True)
    sdist = next(dist.glob("*.tar.gz"))
    subprocess.run(("uv", "build", "--offline", "--wheel", "--out-dir", str(dist), str(sdist)), cwd=tmp_path, check=True)
    wheel = next(dist.glob("*.whl"))
    venv = tmp_path / "venv"
    subprocess.run(("uv", "venv", "--offline", "--python", sys.executable, str(venv)), check=True)
    python = venv / "bin" / "python"
    subprocess.run(("uv", "pip", "install", "--offline", "--python", str(python), str(wheel)), check=True)

    retired = {"comment_delivery", "gateway_notification", "corrections", "hermes_profiles",
               "context_packet", "readiness", "repository_snapshot", "symbols"}
    with zipfile.ZipFile(wheel) as archive:
        module_names = sorted({name.rsplit("/", 1)[-1][:-3] for name in archive.namelist()
                               if name.startswith("local_first_orchestrator/") and name.endswith(".py")})
        assert not retired.intersection(module_names)
        marker = ".data/data/local-first-orchestrator/"
        plugin_root = tmp_path / "hermes-home" / "plugins" / "local-first-orchestrator"
        for name in archive.namelist():
            if marker in name and not name.endswith("/"):
                destination = plugin_root / name.split(marker, 1)[1]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
    assert {"plugin.yaml", "__init__.py", "dashboard/manifest.json", "dashboard/plugin_api.py", "dashboard/dist/index.js"} <= {
        path.relative_to(plugin_root).as_posix() for path in plugin_root.rglob("*") if path.is_file()
    }

    imported = subprocess.run((str(python), "-I", "-c", "import importlib; " + "; ".join(
        f"importlib.import_module('local_first_orchestrator.{name}')" for name in module_names)),
        cwd=tmp_path, text=True, capture_output=True)
    assert imported.returncode == 0, imported.stdout + imported.stderr

    # Read the registration from the staged directory-plugin wrapper, not from
    # this checkout or an already-installed profile plugin.
    registration = subprocess.run((str(python), "-I", "-c", """
import importlib.util, json, sys
from pathlib import Path
root = Path(sys.argv[1]); spec = importlib.util.spec_from_file_location('staged_local_first', root / '__init__.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
class Context:
    def __init__(self): self.tools = {}
    def register_tool(self, **kwargs): self.tools[kwargs['name']] = kwargs
    def register_hook(self, *args, **kwargs): pass
    def register_cli_command(self, **kwargs): pass
context = Context(); module.register(context)
print(json.dumps(context.tools['local_first_submit_plan']['schema']['parameters'], sort_keys=True))
""", str(plugin_root)), cwd=tmp_path, text=True, capture_output=True)
    assert registration.returncode == 0, registration.stdout + registration.stderr
    typed_parameters = json.loads(registration.stdout)
    assert "decisions" in typed_parameters["properties"]
    assert "proposal_json" not in typed_parameters["properties"]

    home = tmp_path / "hermes-home"
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - local-first-orchestrator\n", encoding="utf-8")
    site_packages = next((venv / "lib").glob("python*/site-packages"))
    env = {"PATH": os.environ["PATH"], "HERMES_HOME": str(home), "HERMES_M0_CLI": "",
           "PYTHONPATH": str(site_packages), "HERMES_PLUGINS_DEBUG": "1"}
    # The CLI may be a user-facing wrapper that unsets PYTHONPATH; the staged
    # artifact-local package keeps discovery independent of checkout imports.
    doctor = subprocess.run((str(hermes), "plugins", "doctor", str(plugin_root), "--ci"), cwd=tmp_path,
                            env=env, text=True, capture_output=True)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert f"registrations: {len(TOOL_NAMES)} tool(s), 1 hook(s)" in doctor.stdout
    # Doctor runs Hermes' actual discovery/parser/import/register sequence in
    # its own temporary home and reports the native registry readback above.
