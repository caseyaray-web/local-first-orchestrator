"""Provider-free guardrails for the future typed-planner rehearsal harness."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from local_first_orchestrator.decomposition_planner import packet
from tests.test_m7_public_lifecycle_wiring import _bootstrap_request
from scripts.m7_typed_planner_runtime import (
    planning_wait_outcome,
    schema_delivery_receipt,
)


def test_schema_delivery_receipt_requires_the_actual_request_bound_packet_schema():
    request = _bootstrap_request()
    delivered = packet(request)
    receipt = schema_delivery_receipt(delivered, request)
    assert receipt["request_identity"] == request.identity
    assert receipt["schema"] == "request_bound_decisions"
    assert receipt["legacy_proposal_json"] is False


def test_owned_terminal_blocked_run_without_a_persisted_proposal_stops_before_submission():
    result = planning_wait_outcome(
        {"id": "planner-task", "status": "blocked", "runs": [
            {"id": "planner-run", "profile": "planner", "status": "blocked"},
        ]},
        task_id="planner-task", run_id="planner-run", profile="planner", persisted_proposal=None,
        request_id="request-1", request_identity="identity-1",
    )
    assert result == {"action": "stop_cleanup_readback", "reason": "owned_terminal_run_without_matching_persisted_proposal"}


def test_wait_predicate_does_not_stop_for_foreign_or_persisted_terminal_evidence():
    foreign = planning_wait_outcome(
        {"id": "planner-task", "status": "done", "runs": [
            {"id": "other-run", "profile": "planner", "status": "done"},
        ]},
        task_id="planner-task", run_id="planner-run", profile="planner", persisted_proposal=None,
        request_id="request-1", request_identity="identity-1",
    )
    persisted = planning_wait_outcome(
        {"id": "planner-task", "status": "done", "runs": [
            {"id": "planner-run", "profile": "planner", "status": "done"},
        ]},
        task_id="planner-task", run_id="planner-run", profile="planner",
        persisted_proposal={"plan_id": "plan", "request_id": "request-1", "request_identity": "identity-1"},
        request_id="request-1", request_identity="identity-1",
    )
    assert foreign == {"action": "stop_cleanup_readback", "reason": "owned_run_missing_or_ambiguous"}
    assert persisted == {"action": "proposal_persisted"}


def test_wait_predicate_holds_mismatched_request_or_unknown_run_status():
    mismatch = planning_wait_outcome(
        {"id": "planner-task", "runs": [{"id": "planner-run", "profile": "planner", "status": "done"}]},
        task_id="planner-task", run_id="planner-run", profile="planner",
        persisted_proposal={"request_id": "other", "request_identity": "identity-1"},
        request_id="request-1", request_identity="identity-1",
    )
    unknown = planning_wait_outcome(
        {"id": "planner-task", "runs": [{"id": "planner-run", "profile": "planner", "status": None}]},
        task_id="planner-task", run_id="planner-run", profile="planner", persisted_proposal=None,
        request_id="request-1", request_identity="identity-1",
    )
    assert mismatch["action"] == "stop_cleanup_readback"
    assert unknown == {"action": "stop_cleanup_readback", "reason": "owned_run_status_unknown"}


def test_future_harness_prepares_only_a_pinned_clean_candidate_without_running_provider_or_worker(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.name", "fixture"), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.email", "fixture@invalid"), check=True)
    (source / "candidate.txt").write_text("candidate", encoding="utf-8")
    subprocess.run(("git", "-C", str(source), "add", "."), check=True)
    subprocess.run(("git", "-C", str(source), "commit", "-qm", "candidate"), check=True)
    commit = subprocess.check_output(("git", "-C", str(source), "rev-parse", "HEAD"), text=True).strip()
    wheel = tmp_path / "candidate.whl"; wheel.write_bytes(b"wheel-fixture")
    hermes = tmp_path / "hermes"; hermes.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8"); hermes.chmod(0o700)
    config = tmp_path / "config.json"; config.write_text("{}", encoding="utf-8")
    report = tmp_path / "route.json"; report.write_text(json.dumps({"structured_output": "unsupported"}), encoding="utf-8")
    run_root = tmp_path / "run-root"
    script = Path(__file__).parents[1] / "scripts" / "m7-typed-planner-rehearsal.sh"
    env = {**os.environ, "M7_TYPED_CANDIDATE_SOURCE": str(source), "M7_TYPED_SOURCE_COMMIT": commit,
           "M7_TYPED_WHEEL": str(wheel), "M7_TYPED_WHEEL_SHA256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
           "M7_TYPED_HERMES_CLI": str(hermes), "M7_TYPED_CONFIG": str(config),
           "M7_TYPED_PROVIDER_ROUTE_REPORT": str(report), "M7_TYPED_REHEARSAL_ROOT": str(run_root)}
    completed = subprocess.run(("bash", str(script), "--prepare"), env=env, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads((run_root / "preparation.json").read_text(encoding="utf-8"))
    assert receipt["candidate_source_commit"] == commit
    assert receipt["candidate_wheel_sha256"] == env["M7_TYPED_WHEEL_SHA256"]
    assert receipt["submission_budget"] == 1
    assert "before build/install/cutover/live provider run" in receipt["stops"]


def test_runner_fixture_exercises_production_schema_and_terminal_helpers_without_a_provider(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "m7-typed-planner-rehearsal.sh"
    fixture_root = tmp_path / "fixture-run"
    completed = subprocess.run(
        ("bash", str(script), "--fixture-test", str(fixture_root)),
        env={**os.environ, "HERMES_M0_CLI": "", "HERMES_TEST_CLI": ""},
        text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads((fixture_root / "fixture-receipt.json").read_text(encoding="utf-8"))
    assert receipt["helper_module"] == "scripts.m7_typed_planner_runtime"
    assert receipt["schema_delivery"]["schema"] == "request_bound_decisions"
    assert receipt["terminal_wait"]["action"] == "stop_cleanup_readback"
    assert receipt["provider_calls"] == 0


def test_authorized_runner_schema_self_test_imports_dynamic_schema_from_the_isolated_installed_wheel(tmp_path):
    source = tmp_path / "synthetic-candidate"; source.mkdir()
    package = source / "local_first_orchestrator"; package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "decomposition_planner.py").write_text(
        "import json\n"
        "def planner_decisions_schema():\n"
        "    return json.loads('{\\\"type\\\": \\\"object\\\", \\\"properties\\\": {\\\"dynamic\\\": {\\\"type\\\": \\\"string\\\"}}, \\\"required\\\": [\\\"dynamic\\\"]}')\n",
        encoding="utf-8",
    )
    (package / "plugin_tools.py").write_text(
        "from .decomposition_planner import planner_decisions_schema\n"
        "def _schema(name, description, properties, required):\n"
        "    return {'name': name, 'description': description, 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'board_id': {'type': 'string'}, 'anchor_task_id': {'type': 'string'}, **properties}, 'required': ['board_id', 'anchor_task_id', *required]}}\n"
        "SCHEMAS = {'local_first_submit_plan': _schema('local_first_submit_plan', 'dynamic typed decisions', {'decisions': planner_decisions_schema(), 'request_id': {'type': 'string'}}, ['decisions']), 'local_first_register_planning_request': _schema('local_first_register_planning_request', 'register', {'request_id': {'type': 'string'}}, []), 'local_first_status': _schema('local_first_status', 'status', {}, [])}\n",
        encoding="utf-8",
    )
    helper = source / "scripts"; helper.mkdir()
    (helper / "m7_typed_planner_runtime.py").write_text("# frozen fixture helper\n", encoding="utf-8")
    (source / "setup.py").write_text("from setuptools import setup\nsetup(name='synthetic-m7-candidate', version='0.0.0', packages=['local_first_orchestrator'])\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.name", "fixture"), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.email", "fixture@invalid"), check=True)
    wheel_dir = tmp_path / "wheel"; wheel_dir.mkdir()
    subprocess.run(("python3", "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), str(source)), check=True)
    subprocess.run(("git", "-C", str(source), "add", "."), check=True)
    subprocess.run(("git", "-C", str(source), "commit", "-qm", "synthetic candidate"), check=True)
    commit = subprocess.check_output(("git", "-C", str(source), "rev-parse", "HEAD"), text=True).strip()
    wheel, = wheel_dir.glob("*.whl")
    run_root = tmp_path / "schema-self-test"
    script = Path(__file__).parents[1] / "scripts" / "m7-typed-planner-authorized-runner.sh"
    completed = subprocess.run(
        ("bash", str(script), "--schema-extraction-self-test"),
        env={**os.environ, "M7_TYPED_CANDIDATE_SOURCE": str(source), "M7_TYPED_SOURCE_COMMIT": commit,
             "M7_TYPED_WHEEL": str(wheel), "M7_TYPED_WHEEL_SHA256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
             "M7_TYPED_REHEARSAL_ROOT": str(run_root)}, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads((run_root / "schema-extraction-self-test.json").read_text(encoding="utf-8"))
    assert receipt["source"] == "installed-wheel"
    assert receipt["decisions_schema"]["properties"]["dynamic"] == {"type": "string"}
    assert not receipt["module_path"].startswith(str(run_root / "frozen-source"))


def test_authorized_runner_complete_self_test_accepts_optional_submit_plan_request_id_from_a_built_wheel(tmp_path):
    repo = Path(__file__).parents[1]
    source = tmp_path / "candidate-source"
    subprocess.run(("git", "clone", "--no-local", str(repo), str(source)), check=True)
    commit = subprocess.check_output(("git", "-C", str(source), "rev-parse", "HEAD"), text=True).strip()
    wheel_dir = tmp_path / "wheel"; wheel_dir.mkdir()
    subprocess.run(
        ("python3", "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), str(source)),
        check=True,
    )
    wheel, = wheel_dir.glob("local_first_orchestrator-*.whl")
    subprocess.run(("git", "-C", str(source), "clean", "-fdx"), check=True)
    run_root = tmp_path / "complete-self-test"
    script = repo / "scripts" / "m7-typed-planner-authorized-runner.sh"
    completed = subprocess.run(
        ("bash", str(script), "--self-test"),
        env={**os.environ, "HERMES_M0_CLI": "", "M7_TYPED_CANDIDATE_SOURCE": str(source),
             "M7_TYPED_SOURCE_COMMIT": commit, "M7_TYPED_WHEEL": str(wheel),
             "M7_TYPED_WHEEL_SHA256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
             "M7_TYPED_REHEARSAL_ROOT": str(run_root)}, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "contract-proof: installed plugin schemas" in completed.stdout
    assert (run_root / "self-test-venv" / "bin" / "python").is_file()
    assert (run_root / "self-test-wheel-install.log").is_file()


def test_execute_mode_requires_parent_authorization_before_any_cli_invocation(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "m7-typed-planner-rehearsal.sh"
    marker = tmp_path / "cli-called"
    cli = tmp_path / "hermes"
    cli.write_text(f"#!/bin/sh\\ntouch {marker}\\n", encoding="utf-8")
    cli.chmod(0o700)
    completed = subprocess.run(("bash", str(script), "--execute-authorized"),
                               env={**os.environ, "M7_TYPED_HERMES_CLI": str(cli)},
                               text=True, capture_output=True)
    assert completed.returncode == 64
    assert "explicit parent authorization" in completed.stderr
    assert not marker.exists()
