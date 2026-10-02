from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from local_first_orchestrator.hermes_board import HermesBoardAdapter
from tests.test_m4_accepted_link_adapter import _RawCli, _adapter

_RECORDING = Path(__file__).with_name("fixtures") / "m4_native_second_edge_recording.json"
_RECORDING_SHA256 = "d9a0587f6bf7a36195a8089b9f04088dd199ab1d1c2844ba017944381f0579e7"


def _trusted_inputs():
    raw = _RECORDING.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _RECORDING_SHA256
    initial = json.loads(raw)["initial_evidence"]
    return (copy.deepcopy(initial["accepted_source_evidence"]),
            copy.deepcopy(initial["accepted_token"]),
            copy.deepcopy(initial["raw_cards"]))


def test_pure_inspection_reports_full_declared_dag_without_completion_claim():
    from local_first_orchestrator.accepted_dependency_inspection import inspect_accepted_active_tranche_dag

    source, token, receipts = _trusted_inputs()
    recording = json.loads(_RECORDING.read_text())
    observed = {value["task"]["id"]: value for value in recording["observations"][1]["raw_after"].values()}
    report = inspect_accepted_active_tranche_dag(
        trusted_accepted_source=source,
        trusted_accepted_token=token,
        frozen_create_receipts=receipts,
        observed_cards=observed,
        proven_applied_edges=(("TK-A", "TK-B"), ("TK-B", "TK-C")),
        pending_or_unknown_edges=(),
        paused=False,
    )
    assert report["outcome"] == "reconciliation_required"
    assert report["proven_applied_edges"] == (("TK-A", "TK-B"), ("TK-B", "TK-C"))
    assert report["missing_declared_edges"] == (("TK-A", "TK-C"),)
    assert "complete" not in report and "approved" not in report and "released" not in report


@pytest.mark.parametrize("damage", ("unknown", "duplicate-event", "future-card", "run"))
def test_pure_inspection_rejects_raw_card_drift_and_reports_no_authority(damage):
    from local_first_orchestrator.accepted_dependency_inspection import inspect_accepted_active_tranche_dag

    source, token, receipts = _trusted_inputs()
    observed = copy.deepcopy(receipts)
    first = next(iter(observed.values()))
    if damage == "unknown":
        first["opaque"] = {"retained": True}
    elif damage == "duplicate-event":
        first["events"].append(copy.deepcopy(first["events"][0]))
    elif damage == "future-card":
        observed["future"] = copy.deepcopy(first)
        observed["future"]["task"]["id"] = "future-card"
    else:
        first["runs"] = [{"id": "run-1", "status": "running"}]
    report = inspect_accepted_active_tranche_dag(
        trusted_accepted_source=source, trusted_accepted_token=token,
        frozen_create_receipts=receipts, observed_cards=observed,
        proven_applied_edges=(), pending_or_unknown_edges=(), paused=False,
    )
    assert report["outcome"] == "conflict"
    assert "approval" not in report and "released" not in report and report["no_effect_authority"] is True


def test_production_adapter_complete_raw_read_preserves_show_runs_presence(tmp_path):
    cli = _RawCli()
    adapter, _action = _adapter(tmp_path, cli)
    absent = adapter.read_accepted_active_tranche_raw_cards(
        {"board_id": "fixture-board", "anchor_task_id": "anchor-1"}, ("native-A",))
    assert absent["native-A"]["show_has_runs"] is False
    assert absent["native-A"]["show_runs"] is None
    assert absent["native-A"]["runs"] == []
    cli.cards["native-A"]["runs"] = [{"id": "show-only"}]
    present = adapter.read_accepted_active_tranche_raw_cards(
        {"board_id": "fixture-board", "anchor_task_id": "anchor-1"}, ("native-A",))
    assert present["native-A"]["show_has_runs"] is True
    assert present["native-A"]["show_runs"] == [{"id": "show-only"}]
    assert present["native-A"]["runs"] == []
    cli.cards["native-A"]["runs"] = []
    present_empty = adapter.read_accepted_active_tranche_raw_cards(
        {"board_id": "fixture-board", "anchor_task_id": "anchor-1"}, ("native-A",))
    assert present_empty["native-A"]["show_has_runs"] is True
    assert present_empty["native-A"]["show_runs"] == []
    assert present_empty["native-A"]["runs"] == []


def test_production_adapter_complete_raw_read_is_exact_scope_and_zero_write(tmp_path):
    cli = _RawCli()
    adapter, _action = _adapter(tmp_path, cli)
    result = adapter.read_accepted_active_tranche_raw_cards(
        {"board_id": "fixture-board", "anchor_task_id": "anchor-1"},
        ("native-A", "native-B"),
    )
    assert set(result) == {"native-A", "native-B"}
    assert all(raw["runs"] == [] for raw in result.values())
    assert [call[0] for call in cli.calls] == ["show", "runs", "show", "runs"]
    assert not any(call[0] in {"link", "create", "unblock", "claim", "release", "comment"} for call in cli.calls)


def test_production_adapter_complete_raw_read_rejects_duplicate_or_unmanaged_scope(tmp_path):
    cli = _RawCli()
    adapter, _action = _adapter(tmp_path, cli)
    with pytest.raises(ValueError):
        adapter.read_accepted_active_tranche_raw_cards(
            {"board_id": "fixture-board", "anchor_task_id": "anchor-1"},
            ("native-A", "native-A"),
        )
    with pytest.raises(ValueError):
        adapter.read_accepted_active_tranche_raw_cards(
            {"board_id": "wrong", "anchor_task_id": "anchor-1"}, ("native-A",),
        )
    assert cli.calls == []
