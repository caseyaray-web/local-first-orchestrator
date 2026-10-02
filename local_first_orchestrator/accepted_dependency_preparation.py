"""Pure preparation of one generalized accepted-DAG native-link intent.

This boundary only creates immutable pending-operation evidence.  It does not
claim an attempt, call a native mutator, or interpret a diagnostic report as
effect authority.
"""
from __future__ import annotations

import hashlib
import json

from .accepted_dependency_links import (
    _freeze, _reconstruct, _snapshot, _validated_edge,
    validate_observed_multi_edge_transition,
)


_PREPARATION_KIND = "accepted_active_tranche_native_link_preparation_v2"
_TARGET_KIND = "accepted_active_tranche_native_link_v2"


def _digest(value):
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _raw_capture(readback):
    if type(readback) is not dict:
        raise ValueError("frozen create receipt readback is malformed")
    capture = readback.get("raw_capture_v1")
    if type(capture) is not dict or set(capture) != {"kind", "show", "runs"}:
        raise ValueError("frozen create receipt lacks exact raw capture")
    if capture["kind"] != "hermes_kanban_raw_capture_v1" or type(capture["show"]) is not dict or type(capture["runs"]) is not list:
        raise ValueError("frozen create receipt raw capture is malformed")
    raw = dict(capture["show"])
    raw["runs"] = capture["runs"]
    raw["show_has_runs"] = "runs" in capture["show"]
    raw["show_runs"] = capture["show"].get("runs") if raw["show_has_runs"] else None
    return raw


def prepare_accepted_dependency_link_intent(*, trusted_accepted_source, trusted_accepted_token,
                                            frozen_create_receipts, observed_cards,
                                            scope, target_ticket_id, dependency_ticket_id,
                                            prior_applied_receipts=()):
    """Validate one held declared edge and return its immutable v2 reservation body.

    All inputs are data, not authority capabilities.  The coordinator establishes
    their trusted provenance and performs the durable reservation under its lock.
    """
    source = _snapshot(trusted_accepted_source)
    token = _snapshot(trusted_accepted_token)
    receipts = _snapshot(frozen_create_receipts)
    cards = _snapshot(observed_cards)
    prior_receipts = _snapshot(list(prior_applied_receipts)) if type(prior_applied_receipts) in (tuple, list) else None
    if prior_receipts is None:
        raise ValueError("prior applied link receipts must be a transport array")
    if type(scope) is not dict or set(scope) != {"board_id", "anchor_task_id"}:
        raise ValueError("preparation scope is malformed")
    descriptor = object.__getattribute__(_reconstruct(
        trusted_accepted_source=source, trusted_accepted_token=token,
        frozen_create_receipts=receipts,
    ), "_descriptor")
    edge = _validated_edge((dependency_ticket_id, target_ticket_id), "prepared edge")
    declared = tuple(descriptor["declared_edges"])
    if not declared:
        raise ValueError("accepted DAG has no dependency edge to prepare")
    if edge not in declared:
        raise ValueError("prepared edge is undeclared or self-referential")
    task_for = dict(descriptor["ticket_to_task"])
    if set(cards) != set(task_for.values()):
        raise ValueError("current raw cards do not exactly cover accepted active tranche")
    # The coordinator has already extracted exact raw show/runs captures from
    # immutable create receipts; do not re-interpret normalized readbacks here.
    frozen = receipts
    if set(frozen) != set(task_for):
        raise ValueError("frozen receipts do not exactly cover accepted active tranche")
    current_edges = []
    prior_cards = {task_for[ticket]: frozen[ticket] for ticket in task_for}
    # Each previous v2 acknowledgement must prove exactly one declared edge from
    # the immutable creation baseline.  This permits any valid topological order;
    # no lexical or ticket-name ordering is an authority rule.
    for receipt in prior_receipts:
        if type(receipt) is not dict or set(receipt) != {"kind", "prior_cards", "observed_cards", "prior_applied_edges", "edge", "time_window"}:
            raise ValueError("prior applied link receipt is malformed")
        if receipt["kind"] != "accepted_active_tranche_native_link_readback_v2":
            raise ValueError("prior applied link receipt has unsupported version")
        prior_edges = tuple(_validated_edge(item, "prior receipt edge") for item in receipt["prior_applied_edges"])
        transition_edge = _validated_edge(receipt["edge"], "prior receipt transition edge")
        window = receipt["time_window"]
        if (type(window) is not dict or set(window) != {"started", "ended"}
                or type(window["started"]) is not int or type(window["ended"]) is not int):
            raise ValueError("prior applied link receipt time window is malformed")
        if prior_edges != tuple(current_edges) or receipt["prior_cards"] != prior_cards:
            raise ValueError("prior applied link receipt chain is discontinuous")
        validate_observed_multi_edge_transition(
            trusted_accepted_source=source, trusted_accepted_token=token,
            frozen_create_receipts=frozen, authority=None, prior_cards=prior_cards,
            observed_cards=receipt["observed_cards"], prior_applied_edges=prior_edges,
            edge=transition_edge, command_window=(window["started"], window["ended"]),
        )
        current_edges.append(transition_edge)
        prior_cards = receipt["observed_cards"]
    for ticket, task_id in task_for.items():
        card = cards[task_id]
        # After one or more applied edges, the immediately preceding validated
        # receipt—not the zero-edge creation capture—is the exact raw baseline.
        # It carries the accepted link event and any native card fields changed
        # by that already-proven transition.
        baseline = prior_cards[task_id]
        if type(card) is not dict or set(card) != set(baseline):
            raise ValueError("current raw card shape differs from validated prior receipt")
        if type(card["parents"]) is not list or type(card["children"]) is not list:
            raise ValueError("current raw topology is malformed")
        if type(baseline["parents"]) is not list or type(baseline["children"]) is not list:
            raise ValueError("frozen raw topology is malformed")
        # Topology is the sole field family that this zero-applied-edge boundary
        # interprets separately.  Every other raw field (including unknown wire
        # fields and presence sidecars) must be exact canonical JSON equality.
        current_non_topology = {key: value for key, value in card.items()
                                if key not in {"parents", "children"}}
        frozen_non_topology = {key: value for key, value in baseline.items()
                               if key not in {"parents", "children"}}
        if current_non_topology != frozen_non_topology:
            raise ValueError("current raw card differs from frozen held receipt")
        # The full topology is checked below against the reconstructed chain.
    if cards != prior_cards:
        raise ValueError("current raw topology is not backed by generalized applied receipts")
    identity = {
        "kind": _TARGET_KIND, "scope": scope, "accepted_source": source,
        "accepted_token": token, "frozen_create_receipts": receipts,
        "edge": {"source_ticket_id": edge[0], "target_ticket_id": edge[1],
                 "source_task_id": task_for[edge[0]], "target_task_id": task_for[edge[1]]},
    }
    key = "native-link-v2:" + _digest(identity)
    before = {"kind": _PREPARATION_KIND, "identity": identity,
              "current_raw_cards": cards, "current_applied_edges": tuple(current_edges),
              "prior_applied_receipts": prior_receipts}
    return _freeze({"key": key, "target": {"kind": _TARGET_KIND, "operation_key": key,
                    **identity["edge"]}, "expected_observed_identity": _digest(identity),
                    "before_evidence": before, "retry": {"reconcile_only": True}})
