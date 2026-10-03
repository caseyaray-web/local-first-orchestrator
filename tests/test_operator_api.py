from __future__ import annotations

import importlib.util
import json
import subprocess
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from local_first_orchestrator.composition import build_runtime, close_runtime, initialize_store
from local_first_orchestrator.config import PluginConfig
from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, ManagedMember
from local_first_orchestrator.evidence_store import EvidenceStore

SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor"}
PREFIX = "/api/plugins/local-first-orchestrator"


def _snapshot(task_id: str, status: str, revision: int, runs=()) -> BoardSnapshot:
    return BoardSnapshot({"id": task_id, "status": status}, (), tuple(runs), (), (), (),
                         f"fixture-{revision}", f"{task_id}:{status}:{revision}:{len(runs)}")


class FixtureBoard:
    is_fake = True

    def __init__(self) -> None:
        self.cards = {
            "anchor": _snapshot("anchor", "blocked", 0),
            "work": _snapshot("work", "running", 0, ({"id": "run-1", "status": "running", "stop_supported": False},)),
        }
        self.writes: list[str] = []
        self.create_lock_assertion = None
        self.claim_create_attempt = None

    def read_task(self, task_id: str) -> BoardSnapshot:
        return self.cards[task_id]

    def hold(self, action: Action, task_id: str, _reason: str) -> ActionResult:
        self.writes.append("hold:" + task_id)
        old = self.cards[task_id]
        self.cards[task_id] = _snapshot(task_id, "blocked", len(self.writes), old.runs)
        return ActionResult(action.key, "verified", "fixture hold", self.cards[task_id].to_dict())

    def release(self, action: Action, task_id: str, _reason: str) -> ActionResult:
        self.writes.append("release:" + task_id)
        self.cards[task_id] = _snapshot(task_id, "ready", len(self.writes))
        return ActionResult(action.key, "verified", "fixture release", self.cards[task_id].to_dict())

    def stop_run(self, action: Action, task_id: str, run_id: str, _reason: str) -> ActionResult:
        self.writes.append("stop:" + task_id + ":" + run_id)
        return ActionResult(action.key, "unsupported", "fixture native stop unsupported", self.cards[task_id].to_dict())


def _config(tmp_path: Path) -> PluginConfig:
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    home = tmp_path / "home"; home.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"; workspace.mkdir(mode=0o700)
    subprocess.run(("git", "init", "-q"), cwd=workspace, check=True)
    subprocess.run(("git", "config", "user.email", "fixture@example.invalid"), cwd=workspace, check=True)
    subprocess.run(("git", "config", "user.name", "Fixture"), cwd=workspace, check=True)
    (workspace / "README").write_text("fixture\n", encoding="utf-8")
    subprocess.run(("git", "add", "README"), cwd=workspace, check=True)
    subprocess.run(("git", "commit", "-qm", "fixture"), cwd=workspace, check=True)
    executable = tmp_path / "hermes"; executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8"); executable.chmod(0o700)
    return PluginConfig.from_mapping({
        "version": 1, "state_root": str(state), "hermes_executable": str(executable),
        "hermes_home": str(home), "kanban_home": str(home), "scope": dict(SCOPE),
        "check_commands": [{"check_id": "fixture", "argv": [str(executable)]}],
        "trusted_roots": {"repository": str(workspace), "workspace": str(workspace)},
        "roles": {"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "planner", "paid_review_profile": "paid"},
        "budgets": {"implementation_attempts": 1, "review_corrections": 1, "infrastructure_retries": 1, "workflow_repairs": 1, "paid_capacity": 1},
        "poll_interval_seconds": 1,
    })


def _write_config(config: PluginConfig, path: Path) -> None:
    path.write_text(json.dumps({
        "version": config.version, "state_root": str(config.state_root),
        "hermes_executable": str(config.hermes_executable), "hermes_home": str(config.hermes_home),
        "kanban_home": str(config.kanban_home), "trusted_roots": {key: str(value) for key, value in config.trusted_roots.items()},
        "roles": dict(config.roles), "budgets": asdict(config.budget_policy),
        "poll_interval_seconds": config.poll_interval_seconds, "scope": dict(config.scope),
        "check_commands": [{"check_id": check_id, "argv": list(argv)} for check_id, argv in config.check_commands],
    }), encoding="utf-8")


@pytest.fixture
def mounted_api(tmp_path: Path, monkeypatch, request):
    module_path = Path(__file__).parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("m6_dashboard_api", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    config, board = _config(tmp_path), FixtureBoard()
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "fixture-run")
    initialize_store(config)
    store = EvidenceStore.open(config.evidence_store_path, create_new=False)
    if getattr(request, "param", True):
        store.register_member(ManagedMember("fixture-board", "anchor", "work", "implementation", 0, (), "fixture-work"))
    store.close()
    configuration_path = tmp_path / "trusted-dashboard-config.json"
    _write_config(config, configuration_path)
    module.configure_runtime_factory(lambda: build_runtime(PluginConfig.from_file(configuration_path), board=board, scope=SCOPE), configuration_path=configuration_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self): self._serve()
        def do_POST(self): self._serve()
        def log_message(self, _format, *_args): pass
        def _serve(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = None if not raw else json.loads(raw)
            except json.JSONDecodeError:
                code, body = 400, {"detail": "invalid JSON"}
            else:
                code, body = module.dispatch(self.command, self.path, payload)
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", board, configuration_path
    finally:
        server.shutdown(); thread.join(); server.server_close(); module.configure_runtime_factory(None)


def _request(url: str, method="GET", payload=None):
    request = Request(url, method=method, data=None if payload is None else json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def test_mounted_fixture_dashboard_status_stale_and_partial_stop(mounted_api):
    base, board, _ = mounted_api
    code, status = _request(base + PREFIX + "/status")
    assert code == 200
    assert status["scope"] == SCOPE
    assert status["active_workers"][0]["id"] == "run-1"
    assert "observation_digest" in status

    stale, stale_body = _request(base + PREFIX + "/actions/pause", "POST", {"expected_observation_digest": "sha256:" + "0" * 64})
    assert stale == 409 and stale_body["status"]["observation_digest"] == status["observation_digest"]
    assert board.writes == []

    rejected, _ = _request(base + PREFIX + "/actions/pause", "POST", {"expected_observation_digest": status["observation_digest"], "repository": "/tmp/attacker"})
    assert rejected == 409 and board.writes == []

    code, paused = _request(base + PREFIX + "/actions/pause", "POST", {"expected_observation_digest": status["observation_digest"]})
    assert code == 200 and paused["result"]["outcome"] == "partial"
    assert paused["status"]["uncontained_workers"][0]["id"] == "run-1"
    writes_after_pause = list(board.writes)

    stale_stop, _ = _request(base + PREFIX + "/actions/stop", "POST", {"expected_observation_digest": status["observation_digest"]})
    assert stale_stop == 409 and board.writes == writes_after_pause

    code, stopped = _request(base + PREFIX + "/actions/stop", "POST", {"expected_observation_digest": paused["status"]["observation_digest"]})
    assert code == 200 and stopped["result"]["outcome"] == "partial"
    assert stopped["status"]["uncontained_workers"][0]["id"] == "run-1"
    assert all(not item.startswith("stop:") for item in board.writes)


@pytest.mark.parametrize("mounted_api", [False], indirect=True)
def test_mounted_dashboard_profiles_bounded_configuration_and_enrollment(mounted_api):
    base, board, configuration_path = mounted_api
    code, status = _request(base + PREFIX + "/status")
    assert code == 200
    code, profiles = _request(base + PREFIX + "/profiles")
    assert code == 200 and profiles == {"scope": SCOPE, "profiles": {
        "implementation_profile": "implementer", "local_review_profile": "reviewer",
        "planning_profile": "planner", "paid_review_profile": "paid"}}
    code, enrolled = _request(base + PREFIX + "/enroll", "POST", {"expected_observation_digest": status["observation_digest"]})
    assert code == 200 and enrolled["result"]["outcome"] == "verified", enrolled
    assert board.writes == ["hold:anchor"]
    code, configuration = _request(base + PREFIX + "/configuration")
    assert code == 200
    tightened = dict(configuration["budgets"]); tightened["paid_capacity"] = 0
    code, saved = _request(base + PREFIX + "/configuration", "POST", {
        "expected_configuration_digest": configuration["configuration_digest"],
        "poll_interval_seconds": 2, "budgets": tightened,
    })
    assert code == 200 and saved["configuration"]["poll_interval_seconds"] == 2
    assert json.loads(configuration_path.read_text(encoding="utf-8"))["budgets"]["paid_capacity"] == 0
    assert board.writes == ["hold:anchor"]
    code, stale = _request(base + PREFIX + "/configuration", "POST", {
        "expected_configuration_digest": configuration["configuration_digest"],
        "poll_interval_seconds": 3, "budgets": tightened,
    })
    assert code == 409 and "stale" in stale["detail"]
    code, rejected = _request(base + PREFIX + "/configuration", "POST", {
        "expected_configuration_digest": saved["configuration"]["configuration_digest"],
        "poll_interval_seconds": 3, "budgets": {**tightened, "paid_capacity": 9},
        "hermes_executable": "/tmp/attacker", "scope": {"board_id": "other", "anchor_task_id": "other"},
    })
    assert code == 400 and board.writes == ["hold:anchor"]
