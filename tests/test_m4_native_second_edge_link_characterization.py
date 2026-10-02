"""Disposable public-CLI characterization for native three-card DAG link behavior.

No coordinator link operation is exercised here.  The fixture holds three accepted
pieces, then invokes only ``kanban link PARENT CHILD`` and proves every local
EvidenceStore row remains immutable across the native graph mutations.
"""
from __future__ import annotations

import copy
import dataclasses
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from collections.abc import Mapping

import pytest

from local_first_orchestrator.decomposition import TranchePlan
from local_first_orchestrator.decomposition_planner import PlanProposal, TrancheSemantics
from tests.test_m4_active_piece_preparation import _accepted_plan, native_fixture
from tests.test_m4_native_first_edge_link_characterization import _json_diff
from tests.test_m4_plan_evidence import proposal


def _three_card_proposal(req):
    """Keep the canonical root objective/non-goals while making A <- B <- C held."""
    original = proposal(req)
    first, second = original.plan.tranches
    ticket_a = first.tickets[0]
    ticket_b = second.tickets[0]
    ticket_c = dataclasses.replace(ticket_b, ticket_id="TK-C", dependencies=("TK-B", "TK-A"))
    plan = dataclasses.replace(
        original.plan,
        tranches=(TranchePlan("TR-A", 0, (ticket_a, ticket_b, ticket_c), ("AC-1", "AC-2")),),
        criterion_coverage={"AC-1": ("TK-A",), "AC-2": ("TK-B", "TK-C")},
    )
    return PlanProposal(
        req.identity,
        plan,
        (TrancheSemantics("TR-A", "First tranche", ("No unrelated changes",)),),
    )


def _plain(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(child) for child in value]
    return value


def _unchecked_discovery_cli(tmp_path, board, *args):
    """Run one isolated public CLI discovery command without fixture success assertions."""
    executable = os.environ["HERMES_M0_CLI"]
    assert Path(executable).is_file(), "HERMES_M0_CLI must remain the pinned installed executable"
    env = os.environ.copy()
    env.update(
        HERMES_HOME=str(tmp_path / "home"),
        HERMES_KANBAN_HOME=str(tmp_path / "home"),
        HERMES_KANBAN_BOARD=board,
    )
    for key in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT",
        "HERMES_KANBAN_LOGS_ROOT", "HERMES_PROFILE", "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID",
    ):
        env.pop(key, None)
    return subprocess.run(
        [str(Path(executable).resolve()), "kanban", "--board", board, *args],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_discovery_cli_confines_cwd_and_preserves_child_guard(tmp_path, monkeypatch):
    executable = tmp_path / "fixture-hermes"
    executable.touch()
    monkeypatch.setenv("HERMES_M0_CLI", str(executable))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/unrelated/native.db")
    captured = {}

    def capture(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 1, "duplicate", "")

    monkeypatch.setattr(subprocess, "run", capture)
    result = _unchecked_discovery_cli(tmp_path, "fixture-board", "link", "A", "B")
    assert result.returncode == 1
    assert captured["cwd"] == tmp_path
    assert captured["env"]["HERMES_HOME"] == str(tmp_path / "home")
    assert captured["env"]["HERMES_KANBAN_HOME"] == str(tmp_path / "home")
    assert captured["env"]["HERMES_DELEGATED_CHILD_CONTEXT"] == "1"
    assert "HERMES_KANBAN_DB" not in captured["env"]
    assert captured["check"] is False
    assert captured["argv"] == [str(executable.resolve()), "kanban", "--board", "fixture-board", "link", "A", "B"]


def test_native_setup_refuses_child_before_any_subprocess(tmp_path, monkeypatch):
    from tests.test_m4_planner_creation import native_fixture as setup_fixture
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")

    def forbidden(*args, **kwargs):
        pytest.fail("child setup attempted a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(pytest.skip.Exception, match="authorized parent context"):
        setup_fixture.__wrapped__(tmp_path)


def test_discovery_evidence_serializes_complete_member_record():
    from local_first_orchestrator.contracts import ManagedMember
    member = ManagedMember("board", "anchor", "task", "implementation", 0, (), "association")
    encoded = json.dumps(_plain(member), sort_keys=True)
    assert json.loads(encoded) == {
        field.name: _plain(getattr(member, field.name))
        for field in dataclasses.fields(member)
    }


def _operation_records(state):
    """Preserve complete canonical receipts, including identity, effect, and retry evidence."""
    return tuple(_plain(operation.to_dict()) for operation in state["operations"])


_RECORDING_FIXTURE = Path(__file__).with_name("fixtures") / "m4_native_second_edge_recording.json"


def _scope_record(state):
    """Serialize every canonical ``read_scope`` member, not a selected projection."""
    assert set(state) == {
        "scope", "members", "candidates", "reviews", "operations", "effect_observations",
        "budget_events", "budget_reconciliations", "budget_net", "operator_intent",
    }
    return {
        key: _operation_records(state) if key == "operations" else _plain(value)
        for key, value in state.items()
    }


def _snapshot_capture(adapter, ordered_ids):
    started = time.time()
    snapshots = {label: adapter.read_task(task_id).to_dict() for label, task_id in ordered_ids.items()}
    ended = time.time()
    return snapshots, int(started), int(ended)


def _canonical_sha256(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _recording_artifact_sha256(recording):
    pinned = copy.deepcopy(recording)
    pinned["provenance"]["artifact_sha256"] = ""
    return _canonical_sha256(pinned)


def _assert_snapshot_matches_native(raw, runs, snapshot, started, ended):
    """The adapter snapshot is a digestible projection of this exact raw read."""
    assert set(snapshot) == {
        "native_task", "parents", "runs", "comments", "events", "attachments", "observed_at", "digest",
    }
    observed = datetime.fromisoformat(snapshot["observed_at"])
    assert observed.tzinfo is not None
    assert started <= observed.timestamp() <= ended + 1
    assert snapshot["native_task"] == raw["task"]
    assert snapshot["parents"] == [{"id": task_id} for task_id in raw["parents"]]
    assert snapshot["runs"] == runs == raw["runs"]
    assert snapshot["comments"] == raw["comments"]
    assert snapshot["events"] == raw["events"]
    # ``show`` in this native version omits attachments; the adapter contract normalizes that absence.
    assert snapshot["attachments"] == raw.get("attachments", [])
    canonical = {key: snapshot[key] for key in snapshot if key not in {"observed_at", "digest"}}
    from local_first_orchestrator.hermes_board import HermesBoardAdapter
    assert snapshot["digest"] == HermesBoardAdapter._digest(canonical)


def _assert_recorded_second_edge_observations(discovery):
    """Freeze only the complete raw deltas observed from the supported CLI calls."""
    assert set(discovery) == {"provenance", "initial_evidence", "observations"}
    provenance = discovery["provenance"]
    assert set(provenance) == {"source_sha256", "artifact_sha256"}
    assert all(isinstance(provenance[key], str) and len(provenance[key]) == 64
               and set(provenance[key]) <= set("0123456789abcdef") for key in provenance)
    assert provenance["source_sha256"] == _canonical_sha256({
        key: value for key, value in discovery.items() if key != "provenance"
    })
    assert provenance["artifact_sha256"] == _recording_artifact_sha256(discovery)
    initial = discovery["initial_evidence"]
    observations = discovery["observations"]
    assert len(observations) == 4
    assert set(initial) == {"scope_state", "accepted_token", "accepted_source_evidence", "raw_cards", "runs", "snapshots", "snapshot_started_seconds", "snapshot_ended_seconds"}
    assert set(initial["scope_state"]) == {"scope", "members", "candidates", "reviews", "operations", "effect_observations", "budget_events", "budget_reconciliations", "budget_net", "operator_intent"}
    raw_initial = initial["raw_cards"]
    ids = {label: raw_initial[label]["task"]["id"] for label in ("A", "B", "C")}
    assert len(set(ids.values())) == 3
    assert all(raw_initial[label]["task"]["status"] == "blocked" for label in ids)
    assert all(raw_initial[label]["parents"] == [] and raw_initial[label]["children"] == [] for label in ids)
    assert initial["runs"] == {"A": [], "B": [], "C": []}
    for label in ids:
        _assert_snapshot_matches_native(raw_initial[label], initial["runs"][label], initial["snapshots"][label], initial["snapshot_started_seconds"], initial["snapshot_ended_seconds"])

    expected = (
        ("A_to_B", "A", "B", {"A": [ids["B"]], "B": [], "C": []},
         {"A": [], "B": [ids["A"]], "C": []}),
        ("B_to_C", "B", "C", {"A": [ids["B"]], "B": [ids["C"]], "C": []},
         {"A": [], "B": [ids["A"]], "C": [ids["B"]]}),
        ("A_to_C", "A", "C", {"A": sorted((ids["B"], ids["C"])), "B": [ids["C"]], "C": []},
         {"A": [], "B": [ids["A"]], "C": sorted((ids["A"], ids["B"]))}),
        ("duplicate_A_to_C", "A", "C", {"A": sorted((ids["B"], ids["C"])), "B": [ids["C"]], "C": []},
         {"A": [], "B": [ids["A"]], "C": sorted((ids["A"], ids["B"]))}),
    )
    previous_raw = raw_initial
    for observation, (name, parent_label, child_label, children, parents) in zip(observations, expected, strict=True):
        parent, child = ids[parent_label], ids[child_label]
        assert observation["name"] == name
        assert observation["parent"] == parent
        assert observation["child"] == child
        assert observation["returncode"] == 0
        assert observation["stdout"] == f"Linked {parent} -> {child}\n"
        assert observation["stderr"] == ""
        assert observation["command_started_seconds"] <= observation["command_ended_seconds"]
        assert observation["raw_before"] == previous_raw
        assert observation["runs_before"] == observation["runs_after"] == initial["runs"]
        assert observation["before_state"] == {
            "scope_state": initial["scope_state"],
            "accepted_token": initial["accepted_token"],
            "accepted_source_evidence": initial["accepted_source_evidence"],
        }
        assert observation["post_state"] == {
            key: value for key, value in observation["before_state"].items()
        }

        raw_after = observation["raw_after"]
        expected_after = copy.deepcopy(previous_raw)
        expected_after[parent_label]["children"] = children[parent_label]
        expected_after[child_label]["parents"] = parents[child_label]
        event = raw_after[child_label]["events"][-1]
        assert set(event) == {"created_at", "kind", "payload", "run_id"}
        assert observation["command_started_seconds"] <= event["created_at"] <= observation["command_ended_seconds"]
        assert event == {
            "created_at": event["created_at"],
            "kind": "linked",
            "payload": {"child": child, "parent": parent},
            "run_id": None,
        }
        expected_after[child_label]["events"].append(event)
        assert raw_after == expected_after
        assert all(raw_after[label]["task"]["status"] == "blocked" for label in ids)
        assert {label: raw_after[label]["children"] for label in ids} == children
        assert {label: raw_after[label]["parents"] for label in ids} == parents
        assert observation["raw_changed_paths"] == {
            label: _json_diff(previous_raw[label], raw_after[label]) for label in ids
        }
        assert observation["runs_changed_paths"] == {label: [] for label in ids}

        snapshots_before = observation["snapshots_before"]
        snapshots_after = observation["snapshots_after"]
        for label in ids:
            before, after = snapshots_before[label], snapshots_after[label]
            _assert_snapshot_matches_native(observation["raw_before"][label], observation["runs_before"][label], before, observation["snapshots_before_started_seconds"], observation["snapshots_before_ended_seconds"])
            _assert_snapshot_matches_native(observation["raw_after"][label], observation["runs_after"][label], after, observation["snapshots_after_started_seconds"], observation["snapshots_after_ended_seconds"])
            assert before["observed_at"] != after["observed_at"]
            if label != child_label:
                assert after["digest"] == before["digest"]
                assert {key: value for key, value in after.items() if key != "observed_at"} == {
                    key: value for key, value in before.items() if key != "observed_at"
                }
            else:
                assert after["digest"] != before["digest"]
                assert after["parents"] == [{"id": task_id} for task_id in parents[label]]
                assert after["events"] == raw_after[label]["events"]
                assert {key: value for key, value in after.items() if key not in {"observed_at", "digest", "parents", "events"}} == {
                    key: value for key, value in before.items() if key not in {"observed_at", "digest", "parents", "events"}
                }
        assert observation["snapshot_changed_paths"] == {
            label: _json_diff(snapshots_before[label], snapshots_after[label]) for label in ids
        }
        previous_raw = raw_after


@pytest.fixture
def recorded_parent_second_edge_discovery():
    assert _RECORDING_FIXTURE.is_file(), (
        "missing durable genuine recording; rerun the parent-authorized native capture and copy its "
        "second-edge-discovery.json to tests/fixtures/m4_native_second_edge_recording.json"
    )
    recording = json.loads(_RECORDING_FIXTURE.read_text(encoding="utf-8"))
    # This is a stable self-excluding canonical artifact digest, so the pin can live in the JSON itself.
    assert recording["provenance"]["artifact_sha256"] == _recording_artifact_sha256(recording)
    return recording


def test_recorded_parent_second_edge_discovery_is_frozen(recorded_parent_second_edge_discovery):
    _assert_recorded_second_edge_observations(recorded_parent_second_edge_discovery)


@pytest.mark.parametrize("regression", ("unexpected_raw_delta", "event_rewrite", "extra_edge", "duplicate_edge", "omitted_scope_field", "native_task_corruption", "digest_corruption"))
def test_recorded_second_edge_validator_rejects_raw_regressions(recorded_parent_second_edge_discovery, regression):
    corrupted = copy.deepcopy(recorded_parent_second_edge_discovery)
    observations = corrupted["observations"]
    if regression == "unexpected_raw_delta":
        observations[0]["raw_after"]["A"]["task"]["status"] = "open"
    elif regression == "event_rewrite":
        observations[2]["raw_after"]["C"]["events"][2]["payload"]["parent"] = "rewritten"
    elif regression == "extra_edge":
        observations[1]["raw_after"]["A"]["children"].append(observations[1]["child"])
    elif regression == "omitted_scope_field":
        observations[0]["post_state"]["scope_state"].pop("reviews")
    elif regression == "native_task_corruption":
        observations[0]["snapshots_after"]["B"]["native_task"]["status"] = "open"
    elif regression == "digest_corruption":
        observations[0]["snapshots_after"]["B"]["digest"] = "sha256:" + "0" * 64
    else:
        observations[3]["raw_after"]["A"]["children"].append(observations[3]["child"])
    with pytest.raises(AssertionError):
        _assert_recorded_second_edge_observations(corrupted)


def test_native_second_edge_dag_characterization(tmp_path, native_fixture, monkeypatch):
    """Characterize A->B, B->C, A->C, and the duplicate A->C public CLI calls."""
    (board, anchor, workspace, adapter, membership, cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(
        tmp_path, native_fixture, monkeypatch, proposal_factory=_three_card_proposal
    )
    del anchor, workspace, membership
    try:
        prepared = controller.prepare_active_tranche(accepted["plan_id"], request_id=request_id)
        assert prepared["outcome"] == "held", prepared
        assert prepared["completed"] == prepared["total"] == 3
        assert tuple(piece["ticket_id"] for piece in prepared["pieces"]) == ("TK-A", "TK-B", "TK-C")
        assert all(piece["outcome"] == "held" for piece in prepared["pieces"])

        def raw_show(task_id):
            result = cli("show", task_id, "--json")
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        def raw_runs(task_id):
            result = cli("runs", task_id, "--json")
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        state_before = store.read_scope(scope)
        ids = {}
        for piece in prepared["pieces"]:
            operation = next(op for op in state_before["operations"] if op.key == piece["operation_key"])
            ids[operation.target["ticket_id"]] = piece["task_id"]
        assert set(ids) == {"TK-A", "TK-B", "TK-C"}
        assert len(set(ids.values())) == 3
        ordered_ids = {"A": ids["TK-A"], "B": ids["TK-B"], "C": ids["TK-C"]}

        scope_before = _scope_record(state_before)
        accepted_token_before = _plain(store.read_accepted_plan(scope, accepted["plan_id"]))
        accepted_source_before = _plain(store.read_plan(scope, accepted["plan_id"]))
        initial_raw = {label: raw_show(task_id) for label, task_id in ordered_ids.items()}
        initial_runs = {label: raw_runs(task_id) for label, task_id in ordered_ids.items()}
        initial_snapshots, initial_snapshot_started, initial_snapshot_ended = _snapshot_capture(adapter, ordered_ids)
        initial_evidence = {
            "scope_state": scope_before,
            "accepted_token": accepted_token_before,
            "accepted_source_evidence": accepted_source_before,
            "raw_cards": initial_raw,
            "runs": initial_runs,
            "snapshots": initial_snapshots,
            "snapshot_started_seconds": initial_snapshot_started,
            "snapshot_ended_seconds": initial_snapshot_ended,
        }
        evidence_path = tmp_path / "second-edge-discovery.json"
        discovery = {"provenance": {"source_sha256": "", "artifact_sha256": ""}, "initial_evidence": initial_evidence, "observations": []}
        evidence_path.write_text(json.dumps(discovery, sort_keys=True), encoding="utf-8")
        print(json.dumps({"evidence_path": str(evidence_path)}, sort_keys=True))
        assert all(raw["task"]["status"] == "blocked" for raw in initial_raw.values())
        assert all(raw["parents"] == [] and raw["children"] == [] for raw in initial_raw.values())
        assert initial_runs == {"A": [], "B": [], "C": []}

        # These are the only native mutations: supported public CLI link calls.
        for name, parent_label, child_label in (
            ("A_to_B", "A", "B"),
            ("B_to_C", "B", "C"),
            ("A_to_C", "A", "C"),
            ("duplicate_A_to_C", "A", "C"),
        ):
            before_state = {
                "scope_state": _scope_record(store.read_scope(scope)),
                "accepted_token": _plain(store.read_accepted_plan(scope, accepted["plan_id"])),
                "accepted_source_evidence": _plain(store.read_plan(scope, accepted["plan_id"])),
            }
            raw_before = {label: raw_show(task_id) for label, task_id in ordered_ids.items()}
            runs_before = {label: raw_runs(task_id) for label, task_id in ordered_ids.items()}
            snapshots_before, snapshots_before_started, snapshots_before_ended = _snapshot_capture(adapter, ordered_ids)
            started = time.time()
            result = _unchecked_discovery_cli(
                tmp_path, board, "link", ordered_ids[parent_label], ordered_ids[child_label]
            )
            ended = time.time()
            raw_after = {label: raw_show(task_id) for label, task_id in ordered_ids.items()}
            runs_after = {label: raw_runs(task_id) for label, task_id in ordered_ids.items()}
            snapshots_after, snapshots_after_started, snapshots_after_ended = _snapshot_capture(adapter, ordered_ids)
            state_after = store.read_scope(scope)
            post_state = {
                "scope_state": _scope_record(state_after),
                "accepted_token": _plain(store.read_accepted_plan(scope, accepted["plan_id"])),
                "accepted_source_evidence": _plain(store.read_plan(scope, accepted["plan_id"])),
            }
            observation = {
                "name": name,
                "parent": ordered_ids[parent_label],
                "child": ordered_ids[child_label],
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "command_started_seconds": int(started),
                "command_ended_seconds": int(ended),
                "raw_before": raw_before,
                "raw_after": raw_after,
                "runs_before": runs_before,
                "runs_after": runs_after,
                "snapshots_before": snapshots_before,
                "snapshots_after": snapshots_after,
                "snapshots_before_started_seconds": snapshots_before_started,
                "snapshots_before_ended_seconds": snapshots_before_ended,
                "snapshots_after_started_seconds": snapshots_after_started,
                "snapshots_after_ended_seconds": snapshots_after_ended,
                "before_state": before_state,
                "post_state": post_state,
                "raw_changed_paths": {label: _json_diff(raw_before[label], raw_after[label]) for label in ordered_ids},
                "runs_changed_paths": {label: _json_diff(runs_before[label], runs_after[label]) for label in ordered_ids},
                "snapshot_changed_paths": {label: _json_diff(snapshots_before[label], snapshots_after[label]) for label in ordered_ids},
            }
            discovery["observations"].append(observation)
            evidence_path.write_text(json.dumps(discovery, sort_keys=True), encoding="utf-8")
            print(json.dumps({key: observation[key] for key in (
                "name", "returncode", "stdout", "stderr", "raw_changed_paths", "runs_changed_paths"
            )}, sort_keys=True))
            # Any local-store mutation would be an unauthorized side effect of direct native CLI use.
            assert before_state == {
                "scope_state": scope_before,
                "accepted_token": accepted_token_before,
                "accepted_source_evidence": accepted_source_before,
            }
            assert post_state == before_state
            assert runs_before == runs_after == {"A": [], "B": [], "C": []}
            assert all(raw["task"]["status"] == "blocked" for raw in raw_after.values())
            for label in set(ordered_ids) - {parent_label, child_label}:
                assert raw_after[label] == raw_before[label]

        source = {key: value for key, value in discovery.items() if key != "provenance"}
        discovery["provenance"]["source_sha256"] = _canonical_sha256(source)
        discovery["provenance"]["artifact_sha256"] = _recording_artifact_sha256(discovery)
        evidence_path.write_text(json.dumps(discovery, sort_keys=True), encoding="utf-8")
        _assert_recorded_second_edge_observations(discovery)
    finally:
        store.close()
