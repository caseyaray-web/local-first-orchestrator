from __future__ import annotations

import json
import time
from copy import deepcopy
from collections.abc import Mapping

from tests.test_m4_active_piece_preparation import (
    _accepted_plan,
    _batch_proposal,
    native_fixture,
)
from tests.test_m4_plan_evidence import proposal


def _json_diff(before, after, path="$", out=None):
    """Return changed JSON paths with exact before/after values; retain unknown keys."""
    if out is None:
        out = []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(before.keys() | after.keys(), key=str):
            child = f"{path}.{key}"
            if key not in before:
                out.append({"path": child, "before": "<absent>", "after": after[key]})
            elif key not in after:
                out.append({"path": child, "before": before[key], "after": "<absent>"})
            else:
                _json_diff(before[key], after[key], child, out)
    elif isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        for index in range(max(len(before), len(after))):
            child = f"{path}[{index}]"
            if index >= len(before):
                out.append({"path": child, "before": "<absent>", "after": after[index]})
            elif index >= len(after):
                out.append({"path": child, "before": before[index], "after": "<absent>"})
            else:
                _json_diff(before[index], after[index], child, out)
    elif before != after:
        out.append({"path": path, "before": before, "after": after})
    return out


def test_json_diff_detects_unknown_nested_key_changes():
    before = {"native": {"unmodeled": {"sentinel": "before"}}}
    after = {"native": {"unmodeled": {"sentinel": "after"}}}
    expected = deepcopy(before)
    assert _json_diff(before, after) == [{
        "path": "$.native.unmodeled.sentinel", "before": "before", "after": "after"
    }]
    assert after != expected
    assert after != {"native": {"unmodeled": {"sentinel": "after"}, "unexpected": "extra"}}


def test_native_first_edge_link_characterization(tmp_path, native_fixture, monkeypatch):
    board, anchor, workspace, adapter, membership, cli, controller, store, scope, accepted, request_id = _accepted_plan(
        tmp_path, native_fixture, monkeypatch, proposal_factory=_batch_proposal
    )
    plan_id = accepted["plan_id"]
    try:
        prepared = controller.prepare_active_tranche(plan_id, request_id=request_id)
        assert prepared["outcome"] == "held", prepared
        assert prepared["completed"] == prepared["total"] == 2
        assert tuple(piece["ticket_id"] for piece in prepared["pieces"]) == ("TK-A", "TK-B")
        assert all(piece["outcome"] == "held" for piece in prepared["pieces"])

        state_before = store.read_scope(scope)
        targets = {}
        for piece in prepared["pieces"]:
            operation = next(op for op in state_before["operations"] if op.key == piece["operation_key"])
            targets[operation.target["ticket_id"]] = (piece["task_id"], operation.target["association"])
        source_id = targets["TK-A"][0]
        child_id = targets["TK-B"][0]
        assert source_id != child_id
        assert all(member.task_id in (source_id, child_id) for member in state_before["members"] if member.role == "implementation")

        def raw_show(task_id):
            return json.loads(cli("show", task_id, "--json").stdout)

        def observed(task_id):
            snapshot = adapter.read_task(task_id)
            return snapshot.to_dict()

        raw_before = {"source": raw_show(source_id), "child": raw_show(child_id)}
        runs_before = {"source": json.loads(cli("runs", source_id, "--json").stdout),
                       "child": json.loads(cli("runs", child_id, "--json").stdout)}
        adapter_before = {"source": observed(source_id), "child": observed(child_id)}
        assert child_id not in raw_before["source"]["children"]
        assert source_id not in raw_before["child"]["parents"]
        raw_source_expected = deepcopy(raw_before["source"])
        raw_source_expected["children"] = raw_before["source"]["children"] + [child_id]
        raw_child_expected = deepcopy(raw_before["child"])
        raw_child_expected["parents"] = raw_before["child"]["parents"] + [source_id]
        adapter_source_before = deepcopy(adapter_before["source"])
        adapter_child_before = deepcopy(adapter_before["child"])
        child_events_before = deepcopy(raw_before["child"]["events"])
        memberships_before = tuple(state_before["members"])
        operations_before = tuple(state_before["operations"])
        budgets_before = tuple(state_before["budget_events"])
        receipts_before = {
            op.key: (op.phase, op.outcome, op.readback)
            for op in operations_before if op.target.get("ticket_id") in ("TK-A", "TK-B")
        }

        command_started = time.time()
        link_result = cli("link", source_id, child_id)
        command_ended = time.time()
        assert link_result.returncode == 0

        raw_after = {"source": raw_show(source_id), "child": raw_show(child_id)}
        runs_after = {"source": json.loads(cli("runs", source_id, "--json").stdout),
                      "child": json.loads(cli("runs", child_id, "--json").stdout)}
        adapter_after = {"source": observed(source_id), "child": observed(child_id)}
        state_after = store.read_scope(scope)
        assert raw_after["source"] == raw_source_expected
        raw_event = raw_after["child"]["events"][-1]
        assert len(raw_after["child"]["events"]) == len(child_events_before) + 1
        assert raw_after["child"]["events"][:-1] == child_events_before
        assert set(raw_event) == {"created_at", "kind", "payload", "run_id"}
        assert isinstance(raw_event["created_at"], int) and not isinstance(raw_event["created_at"], bool)
        assert raw_event["created_at"] > 0
        assert int(command_started) <= raw_event["created_at"] <= int(command_ended)
        assert raw_event["kind"] == "linked"
        assert raw_event["run_id"] is None
        assert raw_event["payload"] == {"child": child_id, "parent": source_id}
        raw_child_expected["events"] = child_events_before + [deepcopy(raw_event)]
        assert raw_after["child"] == raw_child_expected
        assert adapter_after["source"]["digest"] == adapter_before["source"]["digest"]
        assert adapter_after["source"]["events"] == adapter_before["source"]["events"]
        adapter_source_expected = deepcopy(adapter_source_before)
        adapter_source_expected.pop("observed_at")
        adapter_source_after = deepcopy(adapter_after["source"])
        adapter_source_after.pop("observed_at")
        assert adapter_source_after == adapter_source_expected
        assert adapter_after["child"]["events"] == raw_after["child"]["events"]
        adapter_child_expected = deepcopy(adapter_child_before)
        adapter_child_expected["parents"] = adapter_child_before["parents"] + [{"id": source_id}]
        adapter_child_expected["events"] = adapter_child_before["events"] + [deepcopy(raw_event)]
        adapter_child_expected["digest"] = adapter_after["child"]["digest"]
        adapter_child_expected.pop("observed_at")
        adapter_child_after = deepcopy(adapter_after["child"])
        adapter_child_after.pop("observed_at")
        assert adapter_after["child"]["digest"] != adapter_before["child"]["digest"]
        assert adapter_child_after == adapter_child_expected
        diffs = {
            "raw_show": {key: _json_diff(raw_before[key], raw_after[key]) for key in ("source", "child")},
            "runs": {key: _json_diff(runs_before[key], runs_after[key]) for key in ("source", "child")},
            "adapter": {key: _json_diff(adapter_before[key], adapter_after[key]) for key in ("source", "child")},
        }
        # observed_at is diagnostic metadata only; it is never normalized in the recorded diff.
        print(json.dumps({"source_task_id": source_id, "child_task_id": child_id,
                          "changed_paths": diffs,
                          "adapter_observed_at": {key: {"before": adapter_before[key].get("observed_at"),
                                                         "after": adapter_after[key].get("observed_at")}
                                                   for key in ("source", "child")}},
                         sort_keys=True, separators=(",", ":")))

        assert [item["path"] for item in diffs["raw_show"]["source"]] == ["$.children[0]"]
        assert [item["path"] for item in diffs["raw_show"]["child"]] == ["$.events[2]", "$.parents[0]"]
        assert diffs["runs"] == {"source": [], "child": []}
        assert [item["path"] for item in diffs["adapter"]["source"]] == ["$.observed_at"]
        assert [item["path"] for item in diffs["adapter"]["child"]] == [
            "$.digest", "$.events[2]", "$.observed_at", "$.parents[0]"
        ]

        assert adapter_before["source"]["native_task"]["status"] == "blocked"
        assert adapter_before["child"]["native_task"]["status"] == "blocked"
        assert adapter_after["source"]["native_task"]["status"] == "blocked"
        assert adapter_after["child"]["native_task"]["status"] == "blocked"
        assert runs_before == runs_after == {"source": [], "child": []}
        assert not adapter_before["source"]["parents"] and not adapter_after["source"]["parents"]
        assert not adapter_before["child"]["parents"]
        assert len(adapter_after["child"]["parents"]) == len(adapter_before["child"]["parents"]) + 1
        assert source_id in {
            str(parent.get("id")) if isinstance(parent, Mapping) else str(parent)
            for parent in adapter_after["child"]["parents"]
        }
        assert tuple(state_after["members"]) == memberships_before
        assert tuple(state_after["budget_events"]) == budgets_before
        assert {
            op.key: (op.phase, op.outcome, op.readback)
            for op in state_after["operations"] if op.target.get("ticket_id") in ("TK-A", "TK-B")
        } == receipts_before
        assert {op.key: op for op in state_after["operations"]} == {op.key: op for op in operations_before}
    finally:
        store.close()
