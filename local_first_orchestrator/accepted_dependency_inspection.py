"""Read-only accepted active-tranche dependency inspection.

This module consumes only a bounded detached JSON snapshot.  It reconstructs
accepted DAG semantics from explicitly named trusted evidence, but returns a
diagnostic report rather than an authority or lifecycle decision.
"""
from __future__ import annotations

from types import MappingProxyType

from .accepted_dependency_links import _event, _reconstruct, _snapshot, _validated_edge, _freeze


_REQUIRED_RAW_KEYS = frozenset({"task", "parents", "children", "runs", "events"})


def _edge_set(values, label, declared):
    if type(values) not in (tuple, list):
        raise ValueError(label + " must be a sequence")
    edges = tuple(_validated_edge(value, label) for value in values)
    if len(edges) != len(set(edges)) or any(edge not in declared for edge in edges):
        raise ValueError(label + " contains duplicate or undeclared edge")
    return edges


def inspect_accepted_active_tranche_dag(*, trusted_accepted_source, trusted_accepted_token,
                                        frozen_create_receipts, observed_cards,
                                        proven_applied_edges, pending_or_unknown_edges,
                                        paused):
    """Describe a complete active-tranche DAG observation without side effects.

    A returned report never represents execution, acceptance, approval, release,
    or a compare-and-set guarantee.  Malformed raw data is represented as a
    conflict report so callers can retain all observed uncertainty without writes.
    """
    if type(paused) is not bool:
        raise ValueError("paused must be a plain boolean")
    fresh = _reconstruct(trusted_accepted_source=trusted_accepted_source,
                         trusted_accepted_token=trusted_accepted_token,
                         frozen_create_receipts=frozen_create_receipts)
    descriptor = object.__getattribute__(fresh, "_descriptor")
    declared = tuple(descriptor["declared_edges"])
    try:
        proven = _edge_set(proven_applied_edges, "proven applied edges", declared)
        pending = _edge_set(pending_or_unknown_edges, "pending or unknown edges", declared)
        if set(proven) & set(pending):
            raise ValueError("edge cannot be both proven and pending or unknown")
        cards = _snapshot(observed_cards)
        frozen_raw = _snapshot(frozen_create_receipts)
        frozen_by_task = {value["task"]["id"]: value for value in frozen_raw.values()
                          if type(value) is dict and type(value.get("task")) is dict
                          and type(value["task"].get("id")) is str}
        task_ids = set(descriptor["ticket_to_task"].values())
        if set(frozen_by_task) != task_ids:
            raise ValueError("frozen raw receipts do not exactly cover active accepted tranche")
        if set(cards) != task_ids:
            raise ValueError("observed raw cards do not exactly cover active accepted tranche")
        opaque = []
        parents = {task_id: [] for task_id in task_ids}
        children = {task_id: [] for task_id in task_ids}
        for source_ticket, target_ticket in proven:
            source = descriptor["ticket_to_task"][source_ticket]
            target = descriptor["ticket_to_task"][target_ticket]
            children[source].append(target)
            parents[target].append(source)
        for task_id in task_ids:
            card = cards[task_id]
            if type(card) is not dict or not _REQUIRED_RAW_KEYS <= set(card):
                raise ValueError("observed raw card is incomplete")
            frozen_has_runs = frozen_by_task[task_id].get(
                "show_has_runs", "runs" in frozen_by_task[task_id])
            observed_has_runs = card.get("show_has_runs", "runs" in card)
            frozen_show_runs = frozen_by_task[task_id].get(
                "show_runs", frozen_by_task[task_id].get("runs") if frozen_has_runs else None)
            observed_show_runs = card.get("show_runs", card.get("runs") if observed_has_runs else None)
            if (type(frozen_has_runs) is not bool or type(observed_has_runs) is not bool
                    or frozen_has_runs != observed_has_runs
                    or frozen_show_runs != observed_show_runs
                    or (not frozen_has_runs and frozen_show_runs is not None)):
                raise ValueError("observed raw show changed whether or how it contained runs")
            transport_meta = {"show_has_runs", "show_runs"}
            extras = set(card) - _REQUIRED_RAW_KEYS - transport_meta
            frozen_extras = set(frozen_by_task[task_id]) - _REQUIRED_RAW_KEYS - transport_meta
            if extras != frozen_extras or any(card[field] != frozen_by_task[task_id][field] for field in extras):
                raise ValueError("observed opaque raw fields differ from frozen receipt")
            if extras:
                opaque.append((task_id, tuple(sorted(extras))))
            task = card["task"]
            if (type(task) is not dict or task.get("id") != task_id or task.get("status") != "blocked"
                    or any(type(card[name]) is not list for name in ("parents", "children", "runs", "events"))
                    or card["runs"]):
                raise ValueError("observed active card is malformed, active, or not held")
            if card["parents"] != sorted(parents[task_id]) or card["children"] != sorted(children[task_id]):
                raise ValueError("observed topology differs from proven accepted edge table")
            base_events = __import__("json").loads(descriptor["frozen_create_event_json"][task_id])
            if card["events"][:len(base_events)] != base_events:
                raise ValueError("observed events do not retain frozen creation receipt prefix")
            seen = set()
            for event in card["events"]:
                _event(event, "observed")
            for event in card["events"][len(base_events):]:
                payload = event["payload"]
                if event["kind"] != "linked" or event["run_id"] is not None or set(payload) != {"parent", "child"}:
                    raise ValueError("observed post-create event is not a native link")
                matching = next((edge for edge in declared if (
                    descriptor["ticket_to_task"][edge[0]], descriptor["ticket_to_task"][edge[1]]
                ) == (payload["parent"], payload["child"])), None)
                if matching is None or matching not in proven or matching in seen or task_id != payload["child"]:
                    raise ValueError("observed link event is undeclared, duplicated, or unproven")
                seen.add(matching)
        missing = tuple(edge for edge in declared if edge not in proven and edge not in pending)
        opaque_fields = tuple({"task_id": task_id, "fields": fields} for task_id, fields in opaque)
        conflicts = ()
        outcome = "paused" if paused else "reconciliation_required"
        return _freeze({"outcome": outcome, "declared_edges": declared,
                        "proven_applied_edges": proven, "pending_or_unknown_edges": pending,
                        "missing_declared_edges": missing, "opaque_fields": opaque_fields, "conflicts": conflicts,
                        "observation_is_not_atomic": True,
                        "no_effect_authority": True})
    except ValueError as error:
        return _freeze({"outcome": "conflict", "declared_edges": declared,
                        "proven_applied_edges": (), "pending_or_unknown_edges": (),
                        "missing_declared_edges": declared,
                        "conflicts": ({"kind": "raw_observation_conflict", "reason": str(error)},),
                        "observation_is_not_atomic": True,
                        "no_effect_authority": True})
