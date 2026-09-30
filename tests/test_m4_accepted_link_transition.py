from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from local_first_orchestrator.hermes_board import validate_accepted_first_link_transition


def raw(task_id, *, parents=None, children=None, events=None, extra=None):
    value = {"task": {"id": task_id, "title": "unchanged", "status": "blocked"},
             "parents": [] if parents is None else parents,
             "children": [] if children is None else children,
             "events": [] if events is None else events,
             "comments": [{"body": "keep", "created_at": 4}]}
    if extra:
        value.update(extra)
    return value


def pair():
    source_before = raw("A")
    child_before = raw("B", events=[{"kind": "created", "created_at": 2, "payload": {"x": 1}}])
    source_after = deepcopy(source_before)
    source_after["children"].append("B")
    child_after = deepcopy(child_before)
    child_after["parents"].append("A")
    child_after["events"].append({"created_at": 10, "kind": "linked", "run_id": None,
                                   "payload": {"parent": "A", "child": "B"}})
    return source_before, child_before, source_after, child_after


def verify(items):
    return validate_accepted_first_link_transition(*items, source_id="A", child_id="B",
                                                   command_started_seconds=10, command_ended_seconds=10)


def test_accepts_exact_native_shape_and_returns_deep_immutable_receipt():
    items = pair()
    receipt = verify(items)
    assert receipt.source_id == "A" and receipt.child_id == "B"
    assert receipt.event["created_at"] == 10
    with pytest.raises((TypeError, AttributeError)):
        receipt.event["payload"]["parent"] = "forged"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        receipt.source_id = "other"


@pytest.mark.parametrize("mutation", [
    lambda s, c: s["comments"].append({"body": "changed"}),
    lambda s, c: c["events"].append({"created_at": 10, "kind": "linked", "run_id": None,
                                      "payload": {"parent": "A", "child": "B"}}),
    lambda s, c: c["events"][-1]["payload"].update(extra=True),
    lambda s, c: c["events"][-1].update(run_id="run"),
    lambda s, c: c["parents"].append("foreign"),
    lambda s, c: s["children"].append("foreign"),
    lambda s, c: s.update(unknown="mutation"),
])
def test_rejects_any_uncharacterized_raw_mutation(mutation):
    items = list(pair())
    mutation(items[2], items[3])
    with pytest.raises(ValueError):
        verify(items)


def test_rejects_bad_ids_bounds_event_time_and_nonfirst_edge():
    items = pair()
    for kwargs in ({"source_id": " A", "child_id": "B"},
                   {"source_id": "A", "child_id": "A"},
                   {"source_id": "A", "child_id": "B", "command_started_seconds": True},
                   {"source_id": "A", "child_id": "B", "command_ended_seconds": 9}):
        with pytest.raises(ValueError):
            validate_accepted_first_link_transition(*items, **kwargs)
    existing = pair()
    existing[0]["children"].append("old")
    with pytest.raises(ValueError):
        verify(existing)


@pytest.mark.parametrize("task_index", [0, 1], ids=["source", "child"])
@pytest.mark.parametrize("status", ["ready", "running", "done", "review", None, "<missing>"])
def test_rejects_nonblocked_held_precondition_even_when_unchanged(task_index, status):
    items = list(pair())
    before, after = ((items[0], items[2]) if task_index == 0 else (items[1], items[3]))
    if status == "<missing>":
        del before["task"]["status"]
        del after["task"]["status"]
    else:
        before["task"]["status"] = status
        after["task"]["status"] = status
    with pytest.raises(ValueError, match="held as blocked"):
        verify(items)


def test_rejects_bool_event_timestamp_and_prefix_rewrite():
    items = pair()
    items[3]["events"][-1]["created_at"] = True
    with pytest.raises(ValueError):
        verify(items)
    items = pair()
    items[3]["events"][0]["payload"]["x"] = 2
    with pytest.raises(ValueError):
        verify(items)
