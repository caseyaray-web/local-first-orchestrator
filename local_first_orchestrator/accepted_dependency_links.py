"""Pure accepted-DAG reconstruction and transition observation validation.

`trusted_accepted_source` and `trusted_accepted_token` name caller-selected
trusted inputs.  This module proves only their internal semantic consistency;
it neither establishes native authority nor performs a board effect.  Raw card
receipts and observations are untrusted wire data and are reconstructed against
those trusted inputs at every public consumption boundary.
"""
from __future__ import annotations

import hashlib
import json
import math
from types import MappingProxyType

from .planning_coordinator import (
    ActiveTrancheRoute,
    accepted_active_tranche_create_payload,
    first_active_tranche_materialization,
    reconstruct_evidence,
)

_KIND = "accepted_active_tranche_dependency_links_v1"
_MAX_EVENT_TEXT = 16_384
_MAX_EVENT_PAYLOAD_BYTES = 131_072


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


_SNAPSHOT_MAX_DEPTH = 32
_SNAPSHOT_MAX_NODES = 250_000
_SNAPSHOT_MAX_CONTAINER_ITEMS = 16_384
_SNAPSHOT_MAX_STRING_CHARS = 4_000_000
_SNAPSHOT_MAX_UTF8_BYTES = 4_000_000


def _bounded_utf8_byte_count(value, remaining):
    """Count UTF-8 bytes without materializing an encoded copy; stop at budget."""
    count = 0
    for char in value:
        codepoint = ord(char)
        if codepoint < 0x80:
            width = 1
        elif codepoint < 0x800:
            width = 2
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("accepted dependency input string is not valid UTF-8")
        elif codepoint < 0x10000:
            width = 3
        else:
            width = 4
        count += width
        if count > remaining:
            raise ValueError("accepted dependency input exceeds snapshot UTF-8 byte limit")
    return count


def _snapshot(value):
    """Validate bounded exact JSON wire data, then thaw it with the canonical serializer.

    This boundary intentionally has a dedicated 4 MB aggregate UTF-8 evidence
    budget. It is not an identifier or event-body cap: those retain their own
    narrower semantic limits below. The aggregate and structural ceilings are
    sized to admit the recorded 978,027-byte native capture while bounding every
    untrusted source, token, receipt, and observed-card snapshot before semantic
    reconstruction, equality, hashing, or copy allocation.
    """
    if type(value) not in (dict, list):
        raise ValueError("accepted dependency input is not plain JSON")
    nodes, utf8_bytes, active_containers = [0], [0], set()

    def charge_nodes(amount):
        nodes[0] += amount
        if nodes[0] > _SNAPSHOT_MAX_NODES:
            raise ValueError("accepted dependency input exceeds snapshot node limit")

    def visit(item, depth, counted=False):
        if depth > _SNAPSHOT_MAX_DEPTH:
            raise ValueError("accepted dependency input exceeds nesting limit")
        if not counted:
            charge_nodes(1)
        if type(item) is dict:
            identity = id(item)
            if identity in active_containers:
                raise ValueError("accepted dependency input contains a container cycle")
            size = dict.__len__(item)
            if size > _SNAPSHOT_MAX_CONTAINER_ITEMS:
                raise ValueError("accepted dependency mapping exceeds snapshot item limit")
            charge_nodes(size * 2)
            active_containers.add(identity)
            try:
                for key, child in dict.items(item):
                    if type(key) is not str:
                        raise ValueError("accepted dependency mapping keys must be strings")
                    visit(key, depth + 1, True)
                    visit(child, depth + 1, True)
            finally:
                active_containers.remove(identity)
            return
        if type(item) is list:
            identity = id(item)
            if identity in active_containers:
                raise ValueError("accepted dependency input contains a container cycle")
            size = list.__len__(item)
            if size > _SNAPSHOT_MAX_CONTAINER_ITEMS:
                raise ValueError("accepted dependency array exceeds snapshot item limit")
            charge_nodes(size)
            active_containers.add(identity)
            try:
                for child in list.__iter__(item):
                    visit(child, depth + 1, True)
            finally:
                active_containers.remove(identity)
            return
        if type(item) is str:
            if len(item) > _SNAPSHOT_MAX_STRING_CHARS:
                raise ValueError("accepted dependency string exceeds snapshot character limit")
            utf8_bytes[0] += _bounded_utf8_byte_count(item, _SNAPSHOT_MAX_UTF8_BYTES - utf8_bytes[0])
            return
        if type(item) in (int, bool) or item is None:
            return
        if type(item) is float and math.isfinite(item):
            return
        raise ValueError("accepted dependency input is not plain JSON")

    visit(value, 0)
    # Validation permits only exact built-ins. Serialize and parse them rather
    # than treating a MappingProxy or another immutable-looking wrapper as safe.
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))


def _freeze(value):
    if type(value) is dict:
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if type(value) is list:
        return tuple(_freeze(child) for child in value)
    return value


class _AcceptedDependencyAuthority:
    """Private immutable description, not a transferable authority capability."""
    __slots__ = ("_descriptor",)

    def __init__(self, marker, descriptor):
        if marker is not _CONSTRUCTION_MARKER:
            raise TypeError("accepted dependency authority is internally constructed")
        object.__setattr__(self, "_descriptor", _freeze(descriptor))

    def __setattr__(self, name, value):
        raise TypeError("accepted dependency authority is immutable")


_CONSTRUCTION_MARKER = object()


def _validate_receipt(raw, expected):
    if type(raw) is not dict or not {"task", "parents", "children", "runs", "events"} <= set(raw):
        raise ValueError("frozen create receipt is incomplete")
    task = raw["task"]
    if type(task) is not dict or type(task.get("id")) is not str or not task["id"]:
        raise ValueError("frozen create receipt task identity missing")
    if task.get("status") != "blocked" or raw["parents"] or raw["children"] or raw["runs"]:
        raise ValueError("frozen create receipt is not an unlinked held card")
    target = expected.target
    body = task.get("body")
    if type(body) is not str:
        raise ValueError("frozen create receipt body missing")
    try:
        actual_body = json.loads(body.split("\n\n<!-- local-first-create:", 1)[0])
        expected_body = json.loads(expected.body)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("frozen create receipt body is malformed") from error
    for actual, wanted in ((task.get("title"), expected.title), (actual_body, expected_body),
                           (task.get("assignee"), target["assignee"]),
                           ("dir:" + str(task.get("workspace_path")), target["workspace"])):
        if actual != wanted:
            raise ValueError("frozen create receipt does not match reconstructed accepted materialization")
    return task["id"]


def _reconstruct(*, trusted_accepted_source, trusted_accepted_token, frozen_create_receipts):
    source = _snapshot(trusted_accepted_source)
    token = _snapshot(trusted_accepted_token)
    receipts = _snapshot(frozen_create_receipts)
    if type(source) is not dict or type(token) is not dict or type(receipts) is not dict:
        raise ValueError("trusted source/token and receipt transport must be plain JSON mappings")
    request, proposal = reconstruct_evidence(source)
    route_data = token.get("route")
    if type(route_data) is not dict:
        raise ValueError("trusted accepted token route missing")
    route = ActiveTrancheRoute(route_data.get("implementation_profile"), route_data.get("workspace"))
    materialization = first_active_tranche_materialization(source, route)
    expected_targets = {target.ticket_id: target for target in materialization.targets}
    expected = {}
    for target in materialization.targets:
        expected[target.ticket_id] = accepted_active_tranche_create_payload(token, source, target)
    if not expected or len(receipts) != len(expected):
        raise ValueError("frozen receipt set does not exactly cover accepted tranche")
    by_ticket, frozen_event_json = {}, {}
    for raw in receipts.values():
        if type(raw) is not dict or type(raw.get("task")) is not dict or type(raw["task"].get("body")) is not str:
            raise ValueError("frozen create receipt body missing")
        try:
            body = json.loads(raw["task"]["body"].split("\n\n<!-- local-first-create:", 1)[0])
            ticket_id = body["ticket"]["ticket_id"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("frozen create receipt body malformed") from error
        if ticket_id in by_ticket or ticket_id not in expected:
            raise ValueError("frozen receipt ticket is duplicate or outside accepted tranche")
        body_kind = body.get("kind")
        if type(body_kind) is not str or body_kind not in {
            "accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"
        }:
            raise ValueError("frozen create receipt kind is unsupported")
        expected_payload = accepted_active_tranche_create_payload(
            token, source, expected_targets[ticket_id], body_kind=body_kind)
        task_id = _validate_receipt(raw, expected_payload)
        by_ticket[ticket_id] = {"task_id": task_id, "association": expected[ticket_id].target["association"],
                                "receipt_digest": "sha256:" + _digest(raw)}
        frozen_event_json[task_id] = json.dumps(raw["events"], sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if set(by_ticket) != set(expected) or len({entry["task_id"] for entry in by_ticket.values()}) != len(by_ticket):
        raise ValueError("frozen receipt native IDs do not exactly bind accepted tickets")
    ticket_ids = tuple(sorted(expected))
    pairs = []
    for target in materialization.targets:
        for dependency in target.declared_dependencies:
            if dependency not in expected or dependency == target.ticket_id:
                raise ValueError("accepted plan declares an unmanaged or self dependency")
            pairs.append((dependency, target.ticket_id))
    if len(pairs) != len(set(pairs)):
        raise ValueError("accepted plan declares duplicate dependencies")
    pairs = tuple(sorted(pairs))
    # `reconstruct_evidence` already rejects cycles; retain a local full-DAG check.
    children = {ticket: [] for ticket in ticket_ids}
    for source_id, target_id in pairs:
        children[source_id].append(target_id)
    visiting, seen = set(), set()
    def walk(ticket):
        if ticket in visiting:
            raise ValueError("accepted plan dependency graph contains a cycle")
        if ticket not in seen:
            visiting.add(ticket)
            for child in children[ticket]: walk(child)
            visiting.remove(ticket); seen.add(ticket)
    for ticket in ticket_ids: walk(ticket)
    source_hash = "sha256:" + _digest(source)
    receipt_digest_set = {ticket: by_ticket[ticket]["receipt_digest"] for ticket in ticket_ids}
    table = []
    for source_id, target_id in pairs:
        item = {"source_ticket_id": source_id, "target_ticket_id": target_id,
                "source_task_id": by_ticket[source_id]["task_id"], "target_task_id": by_ticket[target_id]["task_id"],
                "source_association": by_ticket[source_id]["association"], "target_association": by_ticket[target_id]["association"],
                "receipt_digest_set": receipt_digest_set, "accepted_source_hash": source_hash}
        item["edge_hash"] = "sha256:" + _digest(item)
        table.append(item)
    edge_table_hash = "sha256:" + _digest(table)
    descriptor = {"kind": _KIND, "accepted_source_hash": source_hash, "ticket_to_task":
                  {ticket: by_ticket[ticket]["task_id"] for ticket in ticket_ids}, "declared_edges": pairs,
                  "frozen_create_event_json": frozen_event_json,
                  "edge_table": table, "edge_table_hash": edge_table_hash,
                  "authority_hash": "sha256:" + _digest({"source": source_hash, "edge_table": edge_table_hash})}
    return _AcceptedDependencyAuthority(_CONSTRUCTION_MARKER, descriptor)


def reconstruct_accepted_dependency_link_authority(*, trusted_accepted_source, trusted_accepted_token, frozen_create_receipts):
    """Create an opaque pure description from explicitly named trusted sources."""
    return _reconstruct(trusted_accepted_source=trusted_accepted_source, trusted_accepted_token=trusted_accepted_token,
                        frozen_create_receipts=frozen_create_receipts)


def _raw_card(value, task_id):
    if type(value) is not dict or not {"task", "parents", "children", "runs", "events"} <= set(value):
        raise ValueError("observed raw card receipt is incomplete")
    if (type(task_id) is not str or type(value["task"]) is not dict
            or type(value["task"].get("id")) is not str or value["task"]["id"] != task_id):
        raise ValueError("observed raw card task identity mismatch")
    if (value["task"].get("status") != "blocked"
            or any(type(value[key]) is not list for key in ("parents", "children", "runs", "events"))
            # A historical native blocked run is inert evidence, not a live
            # claim.  Preserve it verbatim while still rejecting every live or
            # unknown run at the raw transition boundary.
            or any(type(run) is not dict or run.get("status") != "blocked" for run in value["runs"])):
        raise ValueError("observed raw card collections malformed")


def _event(value, label):
    """Validate the complete public event envelope without normalizing fields."""
    if type(value) is not dict or set(value) != {"created_at", "kind", "payload", "run_id"}:
        raise ValueError(label + " event schema is malformed")
    created_at, kind, payload, run_id = (value["created_at"], value["kind"], value["payload"], value["run_id"])
    if type(created_at) is not int or created_at <= 0:
        raise ValueError(label + " event timestamp is malformed")
    try:
        kind_bytes = kind.encode("utf-8") if type(kind) is str else b""
    except UnicodeEncodeError:
        kind_bytes = b""
    if (type(kind) is not str or not kind_bytes or len(kind_bytes) > _MAX_EVENT_TEXT
            or any(ord(char) < 32 or ord(char) == 127 for char in kind)):
        raise ValueError(label + " event kind is malformed")
    if type(payload) is not dict:
        raise ValueError(label + " event payload is malformed")
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ValueError(label + " event payload is malformed") from error
    if len(encoded) > _MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError(label + " event payload exceeds bounds")
    if run_id is not None:
        try:
            run_id_bytes = run_id.encode("utf-8") if type(run_id) is str else b""
        except UnicodeEncodeError:
            run_id_bytes = b""
        if (type(run_id) is not str or not run_id_bytes or len(run_id_bytes) > _MAX_EVENT_TEXT
                or any(ord(char) < 32 or ord(char) == 127 for char in run_id)):
            raise ValueError(label + " event run identity is malformed")


def _validated_edge(value, label):
    """Copy one plain edge before equality, tuple conversion, or hashing."""
    if type(value) not in (tuple, list) or len(value) != 2:
        raise ValueError(label + " malformed")
    source, target = value[0], value[1]
    for node in (source, target):
        if type(node) is not str:
            raise ValueError(label + " nodes must be plain strings")
        encoded = node.encode("utf-8")
        if not encoded or len(encoded) > 240 or any(ord(character) < 32 or ord(character) == 127 for character in node):
            raise ValueError(label + " nodes are outside bounded ticket identifier syntax")
    return (source, target)


def validate_observed_multi_edge_transition(*, trusted_accepted_source, trusted_accepted_token, frozen_create_receipts,
                                             authority, prior_cards, observed_cards, prior_applied_edges, edge, command_window):
    """Reconstruct trusted semantics at consumption; validate exactly one observed edge.

    `authority` is deliberately not trusted or read: a MappingProxy, constructor
    bypass, or a stale genuine object cannot broaden the freshly reconstructed DAG.
    """
    fresh = _reconstruct(trusted_accepted_source=trusted_accepted_source, trusted_accepted_token=trusted_accepted_token,
                         frozen_create_receipts=frozen_create_receipts)
    descriptor = object.__getattribute__(fresh, "_descriptor")
    prior, observed = _snapshot(prior_cards), _snapshot(observed_cards)
    if type(prior) is not dict or type(observed) is not dict:
        raise ValueError("observed cards must be mappings")
    task_for, declared = descriptor["ticket_to_task"], tuple(descriptor["declared_edges"])
    if set(prior) != set(task_for.values()) or set(observed) != set(task_for.values()):
        raise ValueError("observed cards must exactly cover reconstructed accepted tickets")
    current = _validated_edge(edge, "transition edge")
    if current not in declared:
        raise ValueError("transition edge is not declared by reconstructed accepted DAG")
    if type(prior_applied_edges) not in (tuple, list) or len(prior_applied_edges) > len(declared):
        raise ValueError("prior applied edges malformed")
    applied = tuple(_validated_edge(item, "prior applied edge") for item in prior_applied_edges)
    if current in applied or len(applied) != len(set(applied)) or any(item not in declared for item in applied):
        raise ValueError("transition is not one new reconstructed accepted edge")
    if type(command_window) not in (tuple, list) or len(command_window) != 2 or any(type(x) not in (int, float) or type(x) is bool or not math.isfinite(x) for x in command_window) or command_window[0] > command_window[1]:
        raise ValueError("command window malformed")
    for task_id in task_for.values():
        _raw_card(prior[task_id], task_id)
        _raw_card(observed[task_id], task_id)
        for event in prior[task_id]["events"]: _event(event, "prior")
        for event in observed[task_id]["events"]: _event(event, "observed")
    source_task, target_task = task_for[current[0]], task_for[current[1]]
    expected_payload = {"child": target_task, "parent": source_task}
    native_edges = {(task_for[source_id], task_for[target_id]): (source_id, target_id)
                    for source_id, target_id in declared}
    historical = set()
    for task_id, card in prior.items():
        base_json = descriptor["frozen_create_event_json"].get(task_id)
        if type(base_json) is not str:
            raise ValueError("historical event prefix lacks frozen create receipt")
        base_events = json.loads(base_json)
        if card["events"][:len(base_events)] != base_events:
            raise ValueError("historical event prefix differs from frozen create receipt")
        for event in card["events"][len(base_events):]:
            payload = event["payload"]
            if event["kind"] != "linked" or set(payload) != {"child", "parent"} or event["run_id"] is not None:
                raise ValueError("prior linked-event history is malformed")
            historical_edge = native_edges.get((payload["parent"], payload["child"]))
            if historical_edge is None or task_id != task_for[historical_edge[1]] or historical_edge in historical:
                raise ValueError("prior linked-event history is not an exact reconstructed edge projection")
            historical.add(historical_edge)
    if current in historical:
        raise ValueError("historical linked event already records transition edge")
    if historical != set(applied):
        raise ValueError("prior linked-event history does not match prior applied edges")
    def topology(edges):
        parents, children = ({task: [] for task in task_for.values()} for _ in range(2))
        for source_id, target_id in edges:
            children[task_for[source_id]].append(task_for[target_id]); parents[task_for[target_id]].append(task_for[source_id])
        return ({key: sorted(value) for key, value in parents.items()}, {key: sorted(value) for key, value in children.items()})
    before_p, before_c = topology(applied); after_p, after_c = topology(applied + (current,))
    for task_id in task_for.values():
        before, after = prior[task_id], observed[task_id]
        if before["parents"] != before_p[task_id] or before["children"] != before_c[task_id] or after["parents"] != after_p[task_id] or after["children"] != after_c[task_id]:
            raise ValueError("observed topology is not exact reconstructed edge projection")
    if observed[target_task]["events"][:-1] != prior[target_task]["events"] or len(observed[target_task]["events"]) != len(prior[target_task]["events"]) + 1:
        raise ValueError("linked transition must append one event")
    event = observed[target_task]["events"][-1]
    if event["kind"] != "linked" or event["run_id"] is not None or event["payload"] != expected_payload or not command_window[0] <= event["created_at"] <= command_window[1]:
        raise ValueError("linked event does not exactly bind reconstructed edge")
    for task_id in task_for.values():
        expected = _snapshot(prior[task_id])
        expected["parents"] = after_p[task_id]
        expected["children"] = after_c[task_id]
        if task_id == target_task:
            expected["events"] = expected["events"] + [event]
        if observed[task_id] != expected:
            raise ValueError("raw card changed outside exact linked transition")
    edge_entry = next(item for item in descriptor["edge_table"] if (item["source_ticket_id"], item["target_ticket_id"]) == current)
    return _freeze({"status": "applied", "edge": current, "duplicate": False, "authority_hash": descriptor["authority_hash"],
                    "edge_table_hash": descriptor["edge_table_hash"], "edge_hash": edge_entry["edge_hash"]})
