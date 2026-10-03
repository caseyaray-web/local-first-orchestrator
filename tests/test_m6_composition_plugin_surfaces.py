from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import shutil
import sys
import zipfile

import pytest

from local_first_orchestrator.config import PluginConfig
from local_first_orchestrator.composition import _combined_checks, _git_observer, build_runtime, close_runtime, initialize_store
from local_first_orchestrator.plugin_tools import TOOL_NAMES, register_tools


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
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "run-fixture")
    observed = _git_observer(config, config.scope)
    assert observed["candidate"]["originating_run_id"] == "run-fixture"
    assert observed["checks"][0]["outcome"] == "passed"
    head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    assert _combined_checks(config, {"head_sha": head})["checks"][0]["exit_code"] == 0
    with pytest.raises(ValueError, match="scope"):
        _git_observer(config, {"board_id": "other", "anchor_task_id": "anchor"})


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
    assert "registrations: 7 tool(s), 1 hook(s)" in doctor.stdout
    # Doctor runs Hermes' actual discovery/parser/import/register sequence in
    # its own temporary home and reports the native registry readback above.
