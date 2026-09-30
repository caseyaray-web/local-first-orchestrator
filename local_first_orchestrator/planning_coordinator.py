"""Pure plan evidence serialization and reconstruction; no lifecycle effects."""
from __future__ import annotations
import json
import hashlib
import math
import posixpath
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping
from .decomposition_planner import PlanningRequest, PlanProposal, parse_proposal, request_payload, serialize_proposal
from .decomposition_planner import topological_ticket_order


def _validate_route_fields(route):
    """Validate route inputs at construction and every public consumption boundary."""
    missing = object()
    profile = getattr(route, "implementation_profile", missing)
    workspace = getattr(route, "workspace", missing)
    if type(profile) is not str or not profile.strip() or profile.strip() != profile:
        raise ValueError("implementation profile must be explicit, non-empty, and whitespace-stable")
    if type(workspace) is not str or not workspace.startswith("/") or workspace == "/" or "\\" in workspace or workspace.strip() != workspace:
        raise ValueError("workspace must be an absolute canonical lexical path")
    if any(unicodedata.category(char) == "Cc" for char in profile + workspace):
        raise ValueError("route fields must not contain control characters")
    if posixpath.normpath(workspace) != workspace or any(part in ("", ".", "..") for part in workspace.split("/")[1:]):
        raise ValueError("workspace must be an absolute canonical lexical path")
    try:
        profile.encode("utf-8")
        workspace.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("route fields must be UTF-8 encodable") from error


@dataclass(frozen=True)
class ActiveTrancheRoute:
    """Explicit fixture-routing inputs for pure materialization proposals."""
    implementation_profile: str
    workspace: str

    def __post_init__(self):
        _validate_route_fields(self)


@dataclass(frozen=True)
class HeldCardTarget:
    """Immutable description only; this is not a native Action or effect."""
    ticket_id: str
    ticket_contract_hash: str
    tranche_id: str
    tranche_ordinal: int
    criterion_ids: tuple[str, ...]
    declared_dependencies: tuple[str, ...]
    implementation_profile: str
    workspace: str
    association: str
    operation_key: str
    native_parent: bool = False
    initial_status: str = "blocked"

    def native_dependencies(self) -> tuple[()]:
        return ()


@dataclass(frozen=True)
class ActiveTrancheMaterialization:
    """Pure proposal fixture; never accepted-plan authority and not wired to live authority/effects."""
    source: Mapping[str, Any]
    active_tranche_id: str
    active_tranche_ordinal: int
    targets: tuple[HeldCardTarget, ...]


def _deep_freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _canonical_digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


_SNAPSHOT_MAX_DEPTH = 32
_SNAPSHOT_MAX_NODES = 250_000
_SNAPSHOT_MAX_CONTAINER_ITEMS = 16_384
_SNAPSHOT_MAX_STRING_CHARS = 4_000_000
_SNAPSHOT_MAX_UTF8_BYTES = 4_000_000


def _bounded_utf8_byte_count(value, remaining):
    """Count UTF-8 bytes without materializing an encoded copy; stop at the budget."""
    count = 0
    for char in value:
        codepoint = ord(char)
        if codepoint < 0x80:
            width = 1
        elif codepoint < 0x800:
            width = 2
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("plan evidence string is not valid UTF-8")
        elif codepoint < 0x10000:
            width = 3
        else:
            width = 4
        count += width
        if count > remaining:
            raise ValueError("plan evidence exceeds snapshot UTF-8 byte limit")
    return count


def _evidence_snapshot(value):
    """Copy only plain JSON evidence containers; cap depth, nodes, items and UTF-8 bytes.

    The evidence boundary accepts exact built-in dict/list containers and exact JSON
    scalar types only. Each container's direct children are charged before iteration
    or snapshot allocation. The 4 MB byte ceiling matches the planner parser's largest
    permitted proposal payload.
    """
    budget = [0, 0]

    def visit(item, depth, counted=False):
        if depth > _SNAPSHOT_MAX_DEPTH:
            raise ValueError("plan evidence exceeds snapshot nesting limit")
        if not counted:
            budget[0] += 1
            if budget[0] > _SNAPSHOT_MAX_NODES:
                raise ValueError("plan evidence exceeds snapshot node limit")
        if type(item) is dict:
            size = len(item)
            if size > _SNAPSHOT_MAX_CONTAINER_ITEMS:
                raise ValueError("plan evidence mapping exceeds snapshot item limit")
            budget[0] += size * 2
            if budget[0] > _SNAPSHOT_MAX_NODES:
                raise ValueError("plan evidence exceeds snapshot node limit")
            result = {}
            for key, child in dict.items(item):
                if type(key) is not str:
                    raise ValueError("plan evidence mapping keys must be strings")
                visit(key, depth + 1, True)
                if key in result:
                    raise ValueError("plan evidence mapping keys must be unique strings")
                result[key] = visit(child, depth + 1, True)
            return result
        if type(item) is list:
            size = len(item)
            if size > _SNAPSHOT_MAX_CONTAINER_ITEMS:
                raise ValueError("plan evidence array exceeds snapshot item limit")
            budget[0] += size
            if budget[0] > _SNAPSHOT_MAX_NODES:
                raise ValueError("plan evidence exceeds snapshot node limit")
            return [visit(child, depth + 1, True) for child in list.__iter__(item)]
        if type(item) is str:
            if len(item) > _SNAPSHOT_MAX_STRING_CHARS:
                raise ValueError("plan evidence string exceeds snapshot character limit")
            byte_count = _bounded_utf8_byte_count(item, _SNAPSHOT_MAX_UTF8_BYTES - budget[1])
            budget[1] += byte_count
            return item
        if type(item) in (int, bool) or item is None:
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("plan evidence number must be finite")
            return item
        raise ValueError("plan evidence contains a non-JSON value")

    return visit(value, 0)


def first_active_tranche_materialization(evidence: Mapping[str, Any], route: ActiveTrancheRoute) -> ActiveTrancheMaterialization:
    """Reconstruct proposal evidence and describe only tranche zero; never authority or effects.

    Planner provenance belongs in immutable source evidence, not piece association identity.
    Changing only that provenance therefore changes source but not the proposed card identity.
    No filesystem existence, environment, board, store, budget, or live authority is consulted.
    """
    if type(route) is not ActiveTrancheRoute:
        raise ValueError("explicit ActiveTrancheRoute required")
    _validate_route_fields(route)
    route_profile, route_workspace = route.implementation_profile, route.workspace
    snapshot = _evidence_snapshot(evidence)
    request, proposal = reconstruct_evidence(snapshot)
    if route_profile == snapshot["planner"]["profile"]:
        raise ValueError("implementation profile must differ from recorded planner profile")
    plan = proposal.plan
    tranche = plan.tranches[0]
    order = topological_ticket_order(plan)
    ticket_by_id = {ticket.ticket_id: ticket for ticket in tranche.tickets}
    source = {
        "schema_version": 1,
        "board_id": request.board_id,
        "anchor_task_id": request.anchor_id,
        "plan_id": plan.plan_id,
        "request_identity": request.identity,
        "proposal_hash": proposal.proposal_hash,
        "plan_contract_hash": plan.contract_hash,
        "planner": dict(snapshot["planner"]),
    }
    targets = []
    for ticket_id in order:
        ticket = ticket_by_id.get(ticket_id)
        if ticket is None:
            continue
        if ticket.ticket_id == request.anchor_id or request.anchor_id in ticket.dependencies:
            raise ValueError("anchor cannot be an active piece or declared dependency")
        assoc_payload = {
            "kind": "active_tranche_piece_v1", "board_id": request.board_id,
            "anchor_task_id": request.anchor_id, "plan_id": plan.plan_id,
            "request_identity": request.identity, "proposal_hash": proposal.proposal_hash,
            "plan_contract_hash": plan.contract_hash, "tranche_id": tranche.tranche_id,
            "tranche_ordinal": 0, "ticket_id": ticket.ticket_id,
            "ticket_contract_hash": ticket.contract_hash,
        }
        association = "active-tranche-piece:" + _canonical_digest(assoc_payload)
        native_workspace = "dir:" + route_workspace
        operation_payload = {
            "kind": "active_tranche_held_create_v1", "association": association,
            "implementation_profile": route_profile,
            "workspace": native_workspace,
            "declared_dependencies": list(ticket.dependencies),
        }
        targets.append(HeldCardTarget(
            ticket_id=ticket.ticket_id, ticket_contract_hash=ticket.contract_hash,
            tranche_id=tranche.tranche_id, tranche_ordinal=0,
            criterion_ids=tuple(ticket.criterion_ids), declared_dependencies=tuple(ticket.dependencies),
            implementation_profile=route_profile, workspace=native_workspace,
            association=association, operation_key="active-tranche-held:" + _canonical_digest(operation_payload),
        ))
    return ActiveTrancheMaterialization(_deep_freeze(source), tranche.tranche_id, 0, tuple(targets))

def request_from_payload(value: Mapping[str, Any]) -> PlanningRequest:
    if not isinstance(value, Mapping) or set(value) != set(PlanningRequest.__dataclass_fields__): raise ValueError("planning request fields mismatch")
    data = dict(value)
    for key in ("expected_criteria", "authorized_paths"):
        items = data[key]
        if type(items) is not list or any(type(item) is not str for item in items) or len(items) != len(set(items)):
            raise ValueError(f"{key} must contain unique strings")
        data[key] = frozenset(items)
    commands = data["verification_commands"]
    if type(commands) is not list or not commands or any(type(command) is not list or not command or any(type(arg) is not str for arg in command) for command in commands):
        raise ValueError("verification_commands must be nested arrays of strings")
    data["verification_commands"] = tuple(tuple(command) for command in commands)
    if type(data["non_goals"]) is not list or any(type(item) is not str for item in data["non_goals"]):
        raise ValueError("non_goals must be a string array")
    data["non_goals"] = tuple(data["non_goals"])
    statements = data["criterion_statements"]
    if type(statements) is not list or any(type(pair) is not list or len(pair) != 2 or any(type(item) is not str for item in pair) for pair in statements):
        raise ValueError("criterion_statements must be pairs of strings")
    data["criterion_statements"] = tuple(tuple(pair) for pair in statements)
    try:
        request = PlanningRequest(**data)
        if request_payload(request) != dict(value):
            raise ValueError("planning request is not canonical")
        return request
    except (TypeError, UnicodeError) as error:
        raise ValueError("planning request is malformed") from error

def evidence_payload(request: PlanningRequest, proposal: PlanProposal, *, planner_task_id: str, planner_run_id: str, planner_session_id: str, planner_profile: str) -> dict[str, Any]:
    if type(request) is not PlanningRequest or type(proposal) is not PlanProposal or proposal.request_identity != request.identity: raise ValueError("validated matching request and proposal required")
    provenance = (planner_task_id, planner_run_id, planner_session_id, planner_profile)
    if any(type(x) is not str or not x for x in provenance): raise ValueError("complete native planner provenance required")
    serialized = serialize_proposal(proposal)
    if parse_proposal(serialized, request) != proposal: raise ValueError("proposal failed canonical roundtrip")
    return {"schema_version": 1, "request": request_payload(request), "request_identity": request.identity, "proposal": json.loads(serialized), "proposal_hash": proposal.proposal_hash, "planner": {"task_id": planner_task_id, "run_id": planner_run_id, "session_id": planner_session_id, "profile": planner_profile}}

def reconstruct_evidence(payload: Mapping[str, Any]) -> tuple[PlanningRequest, PlanProposal]:
    if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "request", "request_identity", "proposal", "proposal_hash", "planner"} or type(payload["schema_version"]) is not int or payload["schema_version"] != 1: raise ValueError("plan evidence shape/version invalid")
    request = request_from_payload(payload["request"])
    if payload["request_identity"] != request.identity: raise ValueError("stored request identity mismatch")
    raw = json.dumps(payload["proposal"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    proposal = parse_proposal(raw, request)
    if payload["proposal_hash"] != proposal.proposal_hash: raise ValueError("stored proposal hash mismatch")
    p = payload["planner"]
    if not isinstance(p, Mapping) or set(p) != {"task_id", "run_id", "session_id", "profile"} or any(type(v) is not str or not v for v in p.values()): raise ValueError("stored planner provenance invalid")
    return request, proposal
