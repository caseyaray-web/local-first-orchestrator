"""Fail-closed, CLI-only Hermes Kanban adapter.

The adapter never treats a read as a CAS.  It binds one board and one enrolled
anchor, applies only bounded documented CLI commands, and returns partial,
conflict, unknown, or unsupported where native evidence cannot prove an effect.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from types import MappingProxyType
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import Action, ActionResult, BoardSnapshot, NATIVE_COMMENT_AUTHOR, validate_scope
from .planning_coordinator import (request_from_payload, _canonical_digest, _evidence_snapshot,
    reconstruct_evidence, first_active_tranche_materialization, accepted_active_tranche_create_payload,
    ActiveTrancheRoute, HeldCardTarget)


def _plain_json_snapshot(value, *, max_nodes=20_000, max_bytes=1_000_000, max_depth=64):
    """Detach exact built-in JSON values without invoking subclass hooks."""
    nodes = 0
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError("JSON transport exceeds traversal bounds")
        if type(item) is dict:
            for key, child in dict.items(item):
                if type(key) is not str:
                    raise ValueError("JSON object keys must be plain strings")
                stack.append((key, depth + 1)); stack.append((child, depth + 1))
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in list.__iter__(item))
        elif type(item) is str:
            if not _utf8_clean(item): raise ValueError("invalid UTF-8 JSON string")
        elif item is None or type(item) in (bool, int, float):
            if type(item) is float and not __import__("math").isfinite(item):
                raise ValueError("non-finite JSON number")
        else:
            raise ValueError("transport contains a non-plain JSON value")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("JSON transport exceeds byte bound")
    return json.loads(encoded)


def _freeze_link_value(value):
    if type(value) is dict:
        return MappingProxyType({key: _freeze_link_value(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_link_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class AcceptedFirstLinkTransition:
    """Bounded description of a characterized raw native first-link transition only."""
    source_id: str
    child_id: str
    event: Mapping
    source_before: Mapping
    child_before: Mapping
    source_after: Mapping
    child_after: Mapping


def validate_accepted_first_link_transition(before_source, before_child, after_source, after_child,
                                            *, source_id, child_id,
                                            command_started_seconds=None, command_ended_seconds=None):
    """Validate the exact observed parentless first-edge raw-show transition; no I/O or authority."""
    if (type(source_id) is not str or not source_id or len(source_id) > _MAX_FIELD or not _utf8_clean(source_id)
            or type(child_id) is not str or not child_id or len(child_id) > _MAX_FIELD
            or not _utf8_clean(child_id) or source_id == child_id):
        raise ValueError("distinct bounded nonempty literal task IDs required")
    if (type(command_started_seconds) is not int or command_started_seconds <= 0
            or type(command_ended_seconds) is not int or command_ended_seconds < command_started_seconds):
        raise ValueError("positive integer command time bounds required")
    bundle = _evidence_snapshot({"before_source": before_source, "before_child": before_child,
                                 "after_source": after_source, "after_child": after_child})
    bs, bc = bundle["before_source"], bundle["before_child"]
    a_s, a_c = bundle["after_source"], bundle["after_child"]
    if any(type(item) is not dict for item in (bs, bc, a_s, a_c)):
        raise ValueError("raw task show objects must be JSON objects")
    task_rows = [item.get("task") for item in (bs, bc, a_s, a_c)]
    if any(type(row) is not dict or type(row.get("id")) is not str for row in task_rows):
        raise ValueError("raw task identity fields are malformed")
    if (task_rows[0]["id"] != source_id or task_rows[1]["id"] != child_id
            or task_rows[2]["id"] != source_id or task_rows[3]["id"] != child_id):
        raise ValueError("raw task IDs do not match the literal requested IDs")
    if any(type(row.get("status")) is not str or row["status"] != "blocked"
           for row in (task_rows[0], task_rows[1])):
        raise ValueError("first-link precondition requires source and child held as blocked")
    if any(item.get("parents") != [] for item in (bs, bc, a_s)):
        raise ValueError("transition requires parentless source and child")
    if bs.get("children") != [] or bc.get("children") != []:
        raise ValueError("transition requires no pre-existing outbound children")
    if a_s.get("children") != [child_id]:
        raise ValueError("source must append exactly the child edge")
    expected_source = dict(bs)
    expected_source["children"] = [child_id]
    if a_s != expected_source:
        raise ValueError("source raw show changed outside the characterized children append")
    before_events = bc.get("events")
    after_events = a_c.get("events")
    if type(before_events) is not list or type(after_events) is not list or len(after_events) != len(before_events) + 1:
        raise ValueError("child must append exactly one event")
    if after_events[:-1] != before_events:
        raise ValueError("child event history was rewritten")
    event = after_events[-1]
    if type(event) is not dict or set(event) != {"created_at", "kind", "payload", "run_id"}:
        raise ValueError("event does not match characterized linked event shape")
    timestamp = event["created_at"]
    if type(timestamp) is not int or timestamp <= 0 or not command_started_seconds <= timestamp <= command_ended_seconds:
        raise ValueError("linked event timestamp is outside the exact command window")
    if event["kind"] != "linked" or event["run_id"] is not None or event["payload"] != {"parent": source_id, "child": child_id}:
        raise ValueError("linked event fields do not match the requested edge")
    expected_child = dict(bc)
    expected_child["parents"] = [source_id]
    expected_child["events"] = before_events + [event]
    if a_c != expected_child:
        raise ValueError("child raw show changed outside the characterized parent and event appends")
    frozen = [_freeze_link_value(item) for item in (event, bs, bc, a_s, a_c)]
    return AcceptedFirstLinkTransition(source_id, child_id, *frozen)
from .ticket import parse_contract, contract_payload

_AUTHOR = NATIVE_COMMENT_AUTHOR
_MAX_FIELD = 16_384
# A claim operation is a whole reconstructed evidence bundle, not a scalar ID,
# title, body, or argv field.  Keep its transport under the same 1 MiB aggregate
# JSON ceiling enforced by _plain_json_snapshot; individual scalar limits remain
# _MAX_FIELD at every identifier and native-command boundary.
_MAX_LINK_CLAIM_OPERATION_BYTES = 1_000_000
_MAX_MARKER_SEARCH_ROWS = 30
_MAX_MARKER_SEARCH_CLI_CALLS = 64
_MAX_NATIVE_MARKER_DEPTH = 64
_MAX_NATIVE_MARKER_NODES = 1_000


def _utf8_clean(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
        return True
    except UnicodeEncodeError:
        return False


@dataclass(frozen=True, slots=True)
class BoardCapabilities:
    read_tasks: bool; read_runs: bool; create_held: bool; comment: bool; request_review: bool
    request_changes: bool; return_waiting_review: bool; hold: bool; release: bool; link: bool
    complete_anchor: bool; exact_run_stop: bool; atomic_read_bound_mutation: bool

    @classmethod
    def native_m0(cls) -> "BoardCapabilities":
        return cls(True, True, True, True, True, False, True, True, True, True, False, False, False)


class _BoardUnavailable(RuntimeError): pass


class HermesBoardAdapter:
    is_fake = False

    @staticmethod
    def _valid_parentless_planner(action: Action, *, assignee: str, workspace: str, idempotency_key: str) -> bool:
        target = action.to_dict()["target"]
        marker = target.get("planner_marker")
        try:
            request = request_from_payload(target.get("request"))
        except (TypeError, ValueError, KeyError):
            return False
        required = {"task_id", "anchor_task_id", "request_identity", "request", "planner_marker",
                    "association", "reviewer_profile", "native_parent", "create_title", "create_body",
                    "create_workspace", "create_idempotency_key"}
        allowed = required | {"request_id"}
        request_id_valid = ("request_id" not in target or
                            (type(target["request_id"]) is str and bool(target["request_id"])
                             and len(target["request_id"]) <= 256
                             and _utf8_clean(target["request_id"])))
        route = workspace[4:] if isinstance(workspace, str) and workspace.startswith("dir:") else ""
        canonical_route = (bool(route) and os.path.isabs(route) and os.path.normpath(route) == route
                           and not any(part in {"", ".", ".."} for part in route.split("/")[1:]))
        return (required <= set(target) and set(target) <= allowed and request_id_valid
                and target.get("native_parent") is False
                and target.get("association") == request.identity
                and canonical_route and isinstance(marker, Mapping)
                and marker.get("workspace") == route
                and set(marker) == {"kind", "request_identity", "workspace", "profile", "operation_key"}
                and marker.get("kind") == "planner_card"
                and marker.get("request_identity") == target.get("request_identity") == request.identity
                and request.board_id == action.scope.get("board_id")
                and request.anchor_id == action.scope.get("anchor_task_id")
                and target.get("task_id") == target.get("anchor_task_id") == action.scope.get("anchor_task_id")
                and marker.get("operation_key") == action.key == idempotency_key == target.get("create_idempotency_key")
                and marker.get("workspace") == route
                and workspace == target.get("create_workspace")
                and marker.get("profile") == assignee == target.get("reviewer_profile")
                and target.get("create_title") == "Planning: " + request.identity[:48]
                and target.get("create_body") == json.dumps(
                    {"request_identity": request.identity, "marker": dict(marker)}, sort_keys=True)
                and isinstance(target.get("create_title"), str) and 0 < len(target["create_title"]) <= _MAX_FIELD
                and isinstance(target.get("create_body"), str) and 0 < len(target["create_body"]) <= _MAX_FIELD
                and _utf8_clean(target["create_title"]) and _utf8_clean(target["create_body"]))

    def __init__(self, *, board: str, anchor_task_id: str, executable: str,
                 runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                 hermes_home: Path, kanban_home: Path, timeout_seconds: int = 15,
                 output_limit: int = 200_000,
                 managed_member_lookup: Callable[[Mapping[str, str], str], bool] | None = None,
                 completion_evidence_verifier: Callable[[Mapping[str, str], str, str], bool] | None = None,
                 create_lock_assertion: Callable[[Mapping[str, str], str], Any] | None = None,
                 claim_create_attempt: Callable[[Mapping[str, str], str], Any] | None = None,
                 accepted_piece_create_lookup: Callable[[Mapping[str, str], str], Any] | None = None,
                 accepted_dependency_link_lookup: Callable[[Mapping[str, str], str], Any] | None = None,
                 dependency_link_attempt_claim: Callable[[Mapping[str, str], str], Any] | None = None) -> None:
        if not all(isinstance(x, str) and x and x.replace("-", "").replace("_", "").isalnum() for x in (board, anchor_task_id)):
            raise ValueError("explicit board and anchor IDs required")
        executable_path = Path(executable)
        if not executable_path.is_absolute() or not executable_path.is_file(): raise ValueError("Hermes executable must be an absolute existing path")
        if not isinstance(hermes_home, Path) or not isinstance(kanban_home, Path): raise ValueError("explicit isolated home paths required")
        if timeout_seconds < 1 or output_limit < 1: raise ValueError("positive process limits required")
        self.board, self.anchor_task_id, self.executable = board, anchor_task_id, str(executable_path)
        self.runner, self.hermes_home, self.kanban_home = runner, hermes_home, kanban_home
        self.timeout_seconds, self.output_limit = timeout_seconds, output_limit
        self.managed_member_lookup = managed_member_lookup
        self.completion_evidence_verifier = completion_evidence_verifier
        # The coordinator owns the singleton/OS lock.  This callback only
        # asserts it remains held for the full reconciliation and create.
        self.create_lock_assertion = create_lock_assertion
        # The evidence store consumes this entitlement before any native send.
        self.claim_create_attempt = claim_create_attempt
        # This resolver must re-read accepted-plan authority from its trusted store;
        # caller-supplied Action JSON is never itself acceptance authority.
        self.accepted_piece_create_lookup = accepted_piece_create_lookup
        # These callbacks are intentionally separate from legacy link authority:
        # the resolver returns a closed frozen first-edge proof wrapper, and the
        # claim callback durably consumes its one-shot attempt immediately pre-send.
        self.accepted_dependency_link_lookup = accepted_dependency_link_lookup
        self.dependency_link_attempt_claim = dependency_link_attempt_claim
        self.capabilities = BoardCapabilities.native_m0()

    def _accepted_piece_payload(self, action: Action):
        if action.effect != "create_held" or action.target.get("kind") not in {"accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}:
            return None
        if self.accepted_piece_create_lookup is None:
            raise ValueError("trusted accepted-piece resolver is required")
        resolved = self.accepted_piece_create_lookup(action.scope, action.key)
        if not all(hasattr(resolved, name) for name in ("title", "body", "target", "idempotency_key", "plan_evidence")):
            raise ValueError("trusted accepted-piece resolver returned malformed payload")
        if not isinstance(resolved.target, Mapping):
            raise ValueError("trusted accepted-piece target must be a mapping")
        canonical = dict(resolved.target)
        expected_kind = action.target.get("kind")
        if type(expected_kind) is not str or type(canonical.get("kind")) is not str or canonical.get("kind") != expected_kind:
            raise ValueError("trusted accepted-piece target kind mismatch")
        if type(resolved.title) is not str or type(resolved.body) is not str or type(resolved.idempotency_key) is not str:
            raise ValueError("trusted accepted-piece payload fields must be strings")
        target_fields = {"kind", "title", "body", "assignee", "workspace", "idempotency_key", "association",
                         "accepted_token_key", "ticket_id", "ticket_contract_hash", "tranche_id", "tranche_ordinal",
                         "source_hashes", "role", "native_parent", "eligibility"}
        if set(canonical) != target_fields:
            raise ValueError("trusted accepted-piece target schema mismatch")
        if canonical.get("title") != resolved.title or canonical.get("body") != resolved.body or canonical.get("idempotency_key") != resolved.idempotency_key:
            raise ValueError("trusted accepted-piece raw target fields mismatch")
        string_fields = target_fields - {"tranche_ordinal", "native_parent", "source_hashes"}
        for field in string_fields:
            value = canonical.get(field)
            if type(value) is not str or not value or len(value) > _MAX_FIELD or not _utf8_clean(value) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError(f"trusted accepted-piece {field} must be a bounded clean string")
        if (type(canonical.get("tranche_ordinal")) is not int or canonical["tranche_ordinal"] != 0
                or canonical.get("native_parent") is not False or canonical.get("role") != "implementation"
                or canonical.get("eligibility") != "future_adapter_review_required"):
            raise ValueError("trusted accepted-piece target field value mismatch")
        try:
            body_object = json.loads(resolved.body)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("trusted accepted-piece body is malformed") from exc
        if (not isinstance(body_object, dict) or type(body_object.get("schema_version")) is not int
                or body_object.get("schema_version") != 1 or type(body_object.get("kind")) is not str
                or body_object.get("kind") != expected_kind):
            raise ValueError("trusted accepted-piece body kind/schema mismatch")
        if json.dumps(body_object, sort_keys=True, separators=(",", ":"), ensure_ascii=False) != resolved.body:
            raise ValueError("trusted accepted-piece body is not canonical JSON")
        # Reconstruct the accepted description from the trusted resolver's
        # detached store evidence and the token actually carried in its body.
        # Body hashes alone cannot establish criterion or tranche semantics.
        evidence = _evidence_snapshot(resolved.plan_evidence)
        reconstruct_evidence(evidence)
        token = _evidence_snapshot(body_object.get("accepted_token"))
        route_value = token.get("route") if isinstance(token, Mapping) else None
        if not isinstance(route_value, Mapping) or set(route_value) != {"implementation_profile", "workspace"}:
            raise ValueError("accepted token route malformed")
        route = ActiveTrancheRoute(route_value["implementation_profile"], route_value["workspace"])
        material = first_active_tranche_materialization(evidence, route)
        selected = [item for item in material.targets if item.ticket_id == canonical.get("ticket_id")]
        if len(selected) != 1:
            raise ValueError("accepted-piece target is not a unique reconstructed ticket")
        rebuilt = accepted_active_tranche_create_payload(token, evidence, selected[0], body_kind=expected_kind)
        if (resolved.title != rebuilt.title or resolved.body != rebuilt.body
                or dict(canonical) != dict(rebuilt.target)
                or resolved.idempotency_key != rebuilt.idempotency_key):
            raise ValueError("trusted accepted-piece payload differs from reconstructed plan source")
        token = body_object.get("accepted_token")
        token_fields = {"schema_version", "plan_id", "request_identity", "proposal_hash", "plan_contract_hash", "planner",
                        "repository_identity", "base_sha", "snapshot_hash", "root_contract_hash", "active_tranche", "route", "acceptance_identity"}
        if not isinstance(token, Mapping) or set(token) != token_fields:
            raise ValueError("accepted token schema mismatch")
        planner_fields = {"task_id", "run_id", "session_id", "profile"}
        if not isinstance(token.get("planner"), Mapping) or set(token["planner"]) != planner_fields:
            raise ValueError("accepted token planner schema mismatch")
        if (type(token.get("schema_version")) is not int or token["schema_version"] != 1
                or any(type(token.get(k)) is not str or not token[k] for k in token_fields - {"schema_version", "planner", "active_tranche", "route"})
                or any(type(token["planner"].get(k)) is not str or not token["planner"][k] for k in planner_fields)
                or not isinstance(token.get("route"), Mapping) or set(token["route"]) != {"implementation_profile", "workspace"}
                or not isinstance(token.get("active_tranche"), Mapping) or set(token["active_tranche"]) != {"tranche_id", "ordinal"}
                or type(token["active_tranche"].get("ordinal")) is not int or token["active_tranche"]["ordinal"] != 0):
            raise ValueError("accepted token fields malformed")
        identity = token["acceptance_identity"]
        if len(identity) != 71 or not identity.startswith("sha256:") or any(c not in "0123456789abcdef" for c in identity[7:]) or identity != "sha256:" + _canonical_digest({k:v for k,v in token.items() if k != "acceptance_identity"}):
            raise ValueError("accepted token acceptance identity invalid")
        source = canonical.get("source_hashes")
        source_fields = {"request_identity", "proposal_hash", "plan_contract_hash", "acceptance_identity"}
        body_fields = {"schema_version", "kind", "accepted_token_key", "accepted_token", "ticket",
                       "ticket_contract_hash", "criterion_statements", "tranche_semantics", "repository",
                       "association", "route"}
        if expected_kind == "accepted_active_tranche_piece_v2":
            body_fields.add("root_semantics")
        raw_digest_fields = {"proposal_hash", "plan_contract_hash"}
        hashes_valid = (isinstance(source, Mapping) and set(source) == source_fields
                        and all(type(source.get(name)) is str and len(source[name]) == 64
                                and all(char in "0123456789abcdef" for char in source[name])
                                for name in raw_digest_fields)
                        and type(source.get("acceptance_identity")) is str
                        and len(source["acceptance_identity"]) == 71
                        and source["acceptance_identity"].startswith("sha256:")
                        and all(char in "0123456789abcdef" for char in source["acceptance_identity"][7:])
                        and type(source.get("request_identity")) is str and bool(source["request_identity"])
                        and type(canonical.get("ticket_contract_hash")) is str
                        and len(canonical["ticket_contract_hash"]) == 64
                        and all(char in "0123456789abcdef" for char in canonical["ticket_contract_hash"]))
        if set(body_object) != body_fields or not hashes_valid:
            raise ValueError("accepted piece body schema or source hashes invalid")
        nested = {"active_tranche": {"tranche_id", "ordinal"}, "route": {"implementation_profile", "workspace"},
                  "repository": {"repository_identity", "base_sha", "snapshot_hash", "root_contract_hash"},
                  "association": {"operation_key", "declared_dependencies", "native_parent", "native_dependencies", "initial_status"}}
        if any(not isinstance(body_object.get(k), Mapping) or set(body_object[k]) != keys for k, keys in nested.items() if k != "active_tranche" and k != "route"):
            raise ValueError("accepted piece nested body schema mismatch")
        if (not isinstance(body_object.get("route"), Mapping) or set(body_object["route"]) != nested["route"]
                or not isinstance(body_object.get("tranche_semantics"), Mapping) or set(body_object["tranche_semantics"]) != {"objective", "non_goals"}
                or not isinstance(body_object.get("criterion_statements"), list)
                or any(not isinstance(x, Mapping) or set(x) != {"criterion", "statement"} or any(type(x[k]) is not str or not x[k] for k in x) for x in body_object["criterion_statements"])):
            raise ValueError("accepted piece body claims malformed")
        try:
            parsed_ticket = parse_contract(body_object.get("ticket"))
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("accepted piece ticket contract malformed") from exc
        if contract_payload(parsed_ticket) != body_object["ticket"] or parsed_ticket.contract_hash != body_object.get("ticket_contract_hash") or parsed_ticket.ticket_id != canonical.get("ticket_id"):
            raise ValueError("accepted piece ticket identity/hash mismatch")
        ticket_ids = list(parsed_ticket.criterion_ids)
        if [x["criterion"] for x in body_object["criterion_statements"]] != ticket_ids:
            raise ValueError("accepted piece criterion statement order mismatch")
        assoc = body_object["association"]
        if (assoc.get("native_parent") is not False or assoc.get("native_dependencies") != []
                or assoc.get("initial_status") != "blocked" or assoc.get("operation_key") != canonical.get("idempotency_key")
                or assoc.get("declared_dependencies") != list(parsed_ticket.dependencies)):
            raise ValueError("accepted piece association mismatch")
        expected_assoc = {"kind": "active_tranche_piece_v1", "board_id": self.board, "anchor_task_id": self.anchor_task_id,
                          "plan_id": token["plan_id"], "request_identity": token["request_identity"],
                          "proposal_hash": token["proposal_hash"], "plan_contract_hash": token["plan_contract_hash"],
                          "tranche_id": token["active_tranche"]["tranche_id"], "tranche_ordinal": 0,
                          "ticket_id": parsed_ticket.ticket_id, "ticket_contract_hash": parsed_ticket.contract_hash}
        association = "active-tranche-piece:" + _canonical_digest(expected_assoc)
        operation = {"kind": "active_tranche_held_create_v1", "association": association,
                     "implementation_profile": canonical["assignee"], "workspace": canonical["workspace"],
                     "declared_dependencies": list(parsed_ticket.dependencies)}
        if (canonical.get("association") != association or canonical.get("idempotency_key") != "active-tranche-held:" + _canonical_digest(operation)
                or canonical.get("tranche_id") != token["active_tranche"]["tranche_id"]
                or canonical.get("accepted_token_key") != "accept-plan:" + identity
                or canonical.get("ticket_contract_hash") != parsed_ticket.contract_hash
                or canonical.get("source_hashes") != {"request_identity": token["request_identity"], "proposal_hash": token["proposal_hash"],
                    "plan_contract_hash": token["plan_contract_hash"], "acceptance_identity": identity}
                or body_object.get("accepted_token_key") != canonical.get("accepted_token_key")
                or token.get("acceptance_identity") != source.get("acceptance_identity")
                or token.get("request_identity") != source.get("request_identity")
                or token.get("proposal_hash") != source.get("proposal_hash")
                or token.get("plan_contract_hash") != source.get("plan_contract_hash")
                or not isinstance(body_object.get("ticket"), Mapping)
                or body_object["ticket"].get("ticket_id") != canonical.get("ticket_id")
                or body_object.get("ticket_contract_hash") != canonical.get("ticket_contract_hash")
                or body_object.get("route", {}).get("implementation_profile") != canonical.get("assignee")
                or body_object.get("route", {}).get("workspace") != canonical.get("workspace")
                or type(canonical.get("tranche_ordinal")) is not int or canonical.get("tranche_ordinal") != 0
                or not isinstance(token.get("active_tranche"), Mapping)
                or type(token["active_tranche"].get("ordinal")) is not int
                or token["active_tranche"].get("ordinal") != canonical.get("tranche_ordinal")
                or token["active_tranche"].get("tranche_id") != canonical.get("tranche_id")):
            raise ValueError("trusted accepted-piece body provenance/target mismatch")
        expected = canonical
        supplied = action.to_dict()["target"]
        required = {"task_id", "anchor_task_id", "native_parent", "native_deps"}
        if not required <= set(supplied) or supplied.get("task_id") != self.anchor_task_id or supplied.get("anchor_task_id") != self.anchor_task_id:
            raise ValueError("accepted-piece action scope/anchor target mismatch")
        extra = {"task_id", "anchor_task_id", "native_deps"}
        if set(supplied) != set(expected) | extra or {k: v for k, v in supplied.items() if k not in extra} != expected:
            raise ValueError("accepted-piece action differs from trusted canonical payload")
        if supplied.get("native_parent") is not False or supplied.get("native_deps") != []:
            raise ValueError("accepted-piece create must be parentless with no native dependencies")
        if resolved.idempotency_key != action.key or action.scope.get("board_id") != self.board or action.scope.get("anchor_task_id") != self.anchor_task_id:
            raise ValueError("accepted-piece resolver proof scope or operation key mismatch")
        return resolved

    def _env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if os.environ.get(k)}
        env.update({"HERMES_HOME": str(self.hermes_home), "HERMES_KANBAN_HOME": str(self.kanban_home), "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
        return env

    def _invoke(self, *args: str, json_output: bool = False, deadline: float | None = None) -> Any:
        if not all(isinstance(x, str) and len(x) <= _MAX_FIELD for x in args): raise ValueError("bounded string argv required")
        argv = (self.executable, "kanban", "--board", self.board, *args)
        timeout = self.timeout_seconds
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BoardUnavailable("bounded marker search exceeded cumulative deadline")
            timeout = min(timeout, remaining)
        try: completed = self.runner(argv, text=True, capture_output=True, timeout=timeout, check=False, shell=False, env=self._env())
        except (OSError, subprocess.TimeoutExpired) as exc: raise _BoardUnavailable("Hermes Kanban CLI unavailable") from exc
        stdout, stderr = completed.stdout or "", completed.stderr or ""
        if len(stdout) > self.output_limit or len(stderr) > self.output_limit: raise _BoardUnavailable("Hermes Kanban output exceeded bound")
        if completed.returncode: raise _BoardUnavailable((stderr or stdout or "Hermes Kanban CLI failed")[:2000])
        if not json_output: return stdout
        try: return json.loads(stdout)
        except json.JSONDecodeError as exc: raise _BoardUnavailable("Hermes Kanban returned malformed JSON") from exc

    @staticmethod
    def _digest(payload: Mapping[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()

    def _snapshot(self, task_id: str, *, deadline: float | None = None) -> BoardSnapshot:
        if not isinstance(task_id, str) or not task_id: raise ValueError("task_id must be non-empty")
        payload = self._invoke("show", task_id, "--json", json_output=True, deadline=deadline)
        if not isinstance(payload, dict) or not isinstance(payload.get("task"), dict) or payload["task"].get("id") != task_id: raise _BoardUnavailable("exact task read identity mismatch")
        runs = self._invoke("runs", task_id, "--json", json_output=True, deadline=deadline)
        if not isinstance(runs, list) or not all(isinstance(x, dict) for x in runs): raise _BoardUnavailable("exact run read malformed")
        def records(name: str) -> tuple[Mapping[str, Any], ...]:
            value = payload.get(name, [])
            if not isinstance(value, list) or not all(isinstance(x, dict) for x in value): raise _BoardUnavailable(f"exact task {name} malformed")
            return tuple(value)
        parents = payload.get("parents", [])
        if not isinstance(parents, list) or not all(isinstance(x, str) and x for x in parents): raise _BoardUnavailable("exact task parent graph malformed")
        data = {"native_task": payload["task"], "parents": [{"id": x} for x in parents], "runs": runs, "comments": list(records("comments")), "events": list(records("events")), "attachments": list(records("attachments"))}
        return BoardSnapshot(native_task=data["native_task"], parents=tuple(data["parents"]), runs=tuple(runs), comments=records("comments"), events=records("events"), attachments=records("attachments"), observed_at=datetime.now(timezone.utc).isoformat(), digest=self._digest(data))

    def list_tasks(self) -> tuple[BoardSnapshot, ...]:
        rows = self._invoke("list", "--json", json_output=True)
        if not isinstance(rows, list) or not all(isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"] for x in rows): raise _BoardUnavailable("task list malformed")
        return tuple(self._snapshot(x["id"]) for x in rows)
    def read_task(self, task_id: str) -> BoardSnapshot: return self._snapshot(task_id)

    def read_accepted_active_tranche_raw_cards(self, scope: Mapping[str, str], task_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        """Read exact active-tranche raw cards and runs without a native mutation.

        The caller supplies the closed managed set reconstructed from accepted
        evidence; this helper deliberately does not list or discover cards.
        """
        valid = validate_scope(scope)
        if valid != {"board_id": self.board, "anchor_task_id": self.anchor_task_id}:
            raise ValueError("raw active-tranche read scope does not match configured board and anchor")
        if type(task_ids) is not tuple or not task_ids or any(type(task_id) is not str or not task_id for task_id in task_ids):
            raise ValueError("raw active-tranche read requires a non-empty tuple of exact task IDs")
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("raw active-tranche read task IDs must be unique")
        if any(task_id != self.anchor_task_id and not self._trusted_member(valid, task_id) for task_id in task_ids):
            raise ValueError("raw active-tranche read includes an unmanaged task")
        result = {}
        for task_id in task_ids:
            raw, runs = self._raw_show_and_runs(task_id)
            # Keep exact raw task fields and opaque top-level show fields; only
            # runs is supplied by its separate supported endpoint.
            if "show_has_runs" in raw or "show_runs" in raw:
                raise _BoardUnavailable("raw show collides with reserved inspection transport metadata")
            raw_plain = _plain_json_snapshot(raw)
            result[task_id] = {**raw_plain, "runs": _plain_json_snapshot(runs),
                               "show_has_runs": "runs" in raw,
                               "show_runs": _plain_json_snapshot(raw["runs"]) if "runs" in raw else None}
        return result

    def read_run(self, task_id: str, run_id: str) -> Mapping[str, Any]:
        for run in self._snapshot(task_id).runs:
            if str(run.get("id")) == str(run_id): return run
        raise KeyError(f"Hermes run not found: {task_id}/{run_id}")

    def read_scoped_run(self, scope: Mapping[str, str], task_id: str, run_id: str) -> Mapping[str, Any]:
        """Bind an exact native run read to this configured board and anchor.

        The native runs JSON omits board/anchor provenance. Only this adapter
        may add those fields after reading the exact managed task and run.
        """
        valid = validate_scope(scope)
        if valid != {"board_id": self.board, "anchor_task_id": self.anchor_task_id}:
            raise ValueError("native run scope does not match configured board and anchor")
        if not isinstance(task_id, str) or not task_id or not isinstance(run_id, str) or not run_id:
            raise ValueError("exact task and run IDs are required")
        if task_id != self.anchor_task_id and not self._trusted_member(valid, task_id):
            raise ValueError("native run task is not a trusted managed member")
        run = self.read_run(task_id, run_id)
        if str(run.get("id")) != run_id:
            raise ValueError("native run identity is not exact")
        for field, expected in (("task_id", task_id), ("board_id", self.board), ("anchor_task_id", self.anchor_task_id)):
            if field in run and run[field] != expected:
                raise ValueError(f"native run {field} contradicts the exact read")
        return {**run, "task_id": task_id, "board_id": self.board, "anchor_task_id": self.anchor_task_id}

    def read_active_worker_identity(self, scope: Mapping[str, str], task_id: str, run_id: str,
                                    session_id: str, profile: str) -> Mapping[str, Any]:
        """Return only an exact native task/run/session receipt.

        A profile-level session row, timestamp ordering, or worker environment
        cannot establish that an active native run owns that session.  Hermes
        currently stamps ``worker_session_id`` on terminal lifecycle metadata;
        if an active-run API has no exact binding, this remains fail-closed.
        """
        if (type(session_id) is not str or not session_id or type(profile) is not str
                or not profile or not profile.replace("-", "").replace("_", "").isalnum()):
            raise ValueError("native_session_unbound")
        run = self.read_scoped_run(scope, task_id, run_id)
        if run.get("profile") != profile or run.get("status") not in {"running", "active", "claimed"}:
            raise ValueError("native_run_inactive_or_profile_mismatch")
        metadata = run.get("metadata")
        if isinstance(metadata, Mapping) and "worker_session_id" in metadata:
            if metadata.get("worker_session_id") != session_id:
                raise ValueError("native_run_session_mismatch")
            return {"version": 1, "task_id": task_id, "run_id": run_id,
                    "session_id": session_id, "profile": profile,
                    "source": "native_run_metadata"}
        raise ValueError("native_session_unbound")

    def read_provisional_worker_context(self, scope: Mapping[str, str], task_id: str, run_id: str,
                                        session_id: str, profile: str) -> Mapping[str, Any]:
        """Observe an exact live worker context without claiming session authority.

        Native active runs do not yet carry ``worker_session_id``. The coordinator
        separately binds the supplied session to its current worker environment;
        this adapter only proves the configured active task/run/profile tuple.
        Terminal metadata remains mandatory for authoritative finalization.
        """
        if (type(session_id) is not str or not session_id or type(profile) is not str
                or not profile or not profile.replace("-", " ").replace("_", " ").isalnum()):
            raise ValueError("native_worker_context_unbound")
        run = self.read_scoped_run(scope, task_id, run_id)
        card = self.read_task(task_id)
        if (not isinstance(card, BoardSnapshot) or card.native_task.get("id") != task_id
                or card.native_task.get("assignee") != profile
                or card.native_task.get("status") not in {"running", "active"}
                or run.get("profile") != profile
                or run.get("status") not in {"running", "active", "claimed"}
                or not any(str(item.get("id")) == run_id for item in card.runs)):
            raise ValueError("native_task_run_profile_mismatch")
        return {"version": 1, "task_id": task_id, "run_id": run_id,
                "session_id": session_id, "profile": profile,
                "source": "active_worker_context"}

    @staticmethod
    def _evidence(snapshot: BoardSnapshot) -> dict[str, Any]: return snapshot.to_dict()
    def _result(self, action: Action, outcome: str, details: str, snapshot: BoardSnapshot | None) -> ActionResult:
        return ActionResult(action.key, outcome, details, None if snapshot is None else self._evidence(snapshot))
    @staticmethod
    def _running(snapshot: BoardSnapshot) -> bool:
        return snapshot.native_task.get("status") == "running" or any(x.get("status") == "running" for x in snapshot.runs)

    def _scope_and_target(self, action: Action, effect: str, task_id: str | None = None) -> str | None:
        if not isinstance(action, Action) or action.effect != effect: raise ValueError("action effect does not match adapter operation")
        if action.scope.get("board_id") != self.board or action.scope.get("anchor_task_id") != self.anchor_task_id: return "action scope is not this adapter's explicit board and anchor"
        if task_id is not None:
            if action.target.get("task_id") != task_id: return "action target does not match exact task"
            if task_id != self.anchor_task_id and not self._trusted_member(action.scope, task_id): return "target is neither the anchor nor a trusted managed member"
        return None

    def _trusted_member(self, scope: Mapping[str, str], task_id: str) -> bool:
        if self.managed_member_lookup is None: return False
        try: return self.managed_member_lookup(scope, task_id) is True
        except Exception: return False

    def _preflight(self, action: Action, effect: str, task_id: str) -> tuple[BoardSnapshot | None, ActionResult | None]:
        error = self._scope_and_target(action, effect, task_id)
        if error: return None, self._result(action, "conflict", error, None)
        try: before = self._snapshot(task_id)
        except (_BoardUnavailable, ValueError) as exc: return None, self._result(action, "unknown", f"pre-read unavailable: {exc}", None)
        if action.expected_observed_identity != before.digest: return before, self._result(action, "conflict", "action observation identity is stale", before)
        return before, None

    def _mutate(self, action: Action, *, task_id: str, argv: tuple[str, ...], verifier: Callable[[BoardSnapshot, BoardSnapshot], str | None], description: str) -> ActionResult:
        before, result = self._preflight(action, action.effect, task_id)
        if result is not None: return result
        assert before is not None
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton lock assertion is required immediately before native mutation", before)
        try: self._invoke(*argv)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"native {description} outcome unknown: {exc}", before)
        try: after = self._snapshot(task_id)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"post-{description} readback unavailable: {exc}", None)
        outcome = verifier(before, after)
        if outcome is None: return self._result(action, "verified", f"native {description} verified by exact readback", after)
        return self._result(action, outcome, f"native {description} could not be safely verified: {outcome}", after)

    def _assert_create_lock(self, action: Action) -> bool:
        if self.create_lock_assertion is None:
            return False
        try:
            self.create_lock_assertion(action.scope, self.anchor_task_id)
            return True
        except Exception:
            return False

    def _claim_create_attempt(self, action: Action) -> bool:
        if self.claim_create_attempt is None:
            return False
        try:
            self.claim_create_attempt(action.scope, action.key)
            return True
        except Exception:
            return False

    def _marked_create_matches(self, marker: str, *, idempotency_key: str | None = None) -> tuple[int, tuple[BoardSnapshot, ...]]:
        deadline = time.monotonic() + self.timeout_seconds
        calls = 0

        def invoke(*args: str) -> Any:
            nonlocal calls
            if calls >= _MAX_MARKER_SEARCH_CLI_CALLS:
                raise _BoardUnavailable("bounded create marker search exceeded CLI call limit")
            calls += 1
            return self._invoke(*args, json_output=True, deadline=deadline)

        rows = invoke("list", "--archived", "--json")
        if not isinstance(rows, list) or len(rows) > _MAX_MARKER_SEARCH_ROWS:
            raise _BoardUnavailable("bounded create marker search malformed or exceeded row limit")
        matches: list[str] = []
        opaque_ids: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise _BoardUnavailable("bounded create marker search row malformed")
            body = row.get("body")
            if isinstance(body, str) and marker in body:
                matches.append(row["id"])
            elif not isinstance(body, str):
                # A list row without a supported marker-bearing field is not
                # evidence of absence or uniqueness.  Even a visible marker
                # must fan out over every bounded opaque row before it can be
                # accepted; an exact show verifies the stable marker.
                opaque_ids.append(row["id"])
            if len(matches) > 1:
                return len(matches), tuple()
        # The native list serializer may omit both body and idempotency key.
        # Read every bounded opaque row exactly; absence and uniqueness are
        # proved only after this complete fan-out, never inferred from a list.
        for task_id in opaque_ids:
            shown = invoke("show", task_id, "--json")
            task = shown.get("task") if isinstance(shown, Mapping) else None
            if not isinstance(task, Mapping) or task.get("id") != task_id:
                raise _BoardUnavailable("opaque marker row exact read malformed")
            body = task.get("body")
            # A native null body is an explicit empty body, not an opaque
            # value that could retain a marker. Any other non-string shape
            # remains malformed and blocks a resend.
            if body is None:
                continue
            if not isinstance(body, str):
                raise _BoardUnavailable("opaque marker row exact read lacks body")
            if marker in body:
                matches.append(task_id)
                if len(matches) > 1:
                    return len(matches), tuple()
        if not matches:
            return 0, tuple()
        # list plus this exact show/runs read is the entire bounded search.
        if calls + 2 > _MAX_MARKER_SEARCH_CLI_CALLS:
            raise _BoardUnavailable("bounded create marker search exceeded CLI call limit")
        snapshot = self._snapshot(matches[0], deadline=deadline)
        calls += 2
        if not isinstance(snapshot.native_task.get("body"), str) or marker not in snapshot.native_task["body"]:
            raise _BoardUnavailable("native marker shortlist contradicted exact task read")
        return 1, (snapshot,)

    @staticmethod
    def _create_marker(action: Action) -> str:
        """Bound reconciliation identity, scoped to one board-anchor lineage."""
        canonical = json.dumps(
            {"action_key": action.key, "anchor_task_id": action.scope["anchor_task_id"], "board_id": action.scope["board_id"]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        return "<!-- local-first-create:v1:sha256:" + hashlib.sha256(canonical.encode()).hexdigest() + " -->"

    @staticmethod
    def _native_marker(action: Action) -> str:
        """Stable native hold/release identity, bound to one board lineage."""
        canonical = json.dumps(
            {"action_key": action.key, "anchor_task_id": action.scope["anchor_task_id"],
             "board_id": action.scope["board_id"], "effect": action.effect},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        return "<!-- local-first-native:v1:sha256:" + hashlib.sha256(canonical.encode()).hexdigest() + " -->"

    @staticmethod
    def _contains_marker(value: Any, marker: str) -> bool:
        """Bounded non-recursive scan of untrusted native JSON-like values."""
        stack: list[tuple[Any, int]] = [(value, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > _MAX_NATIVE_MARKER_NODES or depth > _MAX_NATIVE_MARKER_DEPTH:
                raise _BoardUnavailable("native marker evidence exceeded traversal bounds")
            if isinstance(item, str):
                if marker in item:
                    return True
            elif isinstance(item, Mapping):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, (list, tuple)):
                stack.extend((child, depth + 1) for child in item)
        return False

    def _verify_native_hold_release(self, action: Action, snapshot: BoardSnapshot) -> str | None:
        """Read-only reconciliation of one exact native hold/release effect.

        Status alone is never evidence. Hold events retain the reason marker,
        so hold requires the exact comment and event. Unblock events do not
        retain a reason, so release requires the exact UNBLOCK comment instead
        of inventing an event-to-marker linkage.
        """
        try:
            marker = self._native_marker(action)
            is_hold = action.effect == "hold"
            expected_comment = f"BLOCKED: {marker}" if is_hold else f"UNBLOCK: {marker}"
            marked_comments = [item for item in snapshot.comments if self._contains_marker(item, marker)]
            exact_comments = [item for item in marked_comments if item.get("body") == expected_comment]
            if len(marked_comments) > 1 or len(exact_comments) > 1:
                return "conflict"
            if marked_comments and not exact_comments:
                return "conflict"
            marked_events = [item for item in snapshot.events if self._contains_marker(item, marker)]
        except _BoardUnavailable:
            return "unknown"
        if not is_hold:
            # Native unblock events have no reason payload, so on a later
            # read they cannot be tied to this operation. The exact UNBLOCK
            # comment is the supported scoped evidence; do not invent linkage.
            if marked_events: return "conflict"
            if not exact_comments: return "unsupported"
        else:
            exact_events = [
                item for item in snapshot.events
                if item.get("kind") == "blocked"
                and isinstance(item.get("payload"), Mapping)
                and item["payload"].get("reason") == marker
                and item["payload"].get("kind") == "needs_input"
                and item["payload"].get("source_status") in {"ready", "running"}
                and isinstance(item["payload"].get("recurrences"), int)
                and item["payload"]["recurrences"] > 0
            ]
            if len(marked_events) > 1 or len(exact_events) > 1:
                return "conflict"
            if marked_events and not any(item in exact_events for item in marked_events):
                return "conflict"
            if not exact_comments and not exact_events:
                return "unsupported"
            if not exact_comments or not exact_events:
                return "unknown"
        if is_hold:
            if self._running(snapshot): return "partial"
            return None if snapshot.native_task.get("status") == "blocked" else "conflict"
        if snapshot.native_task.get("status") in {"ready", "todo"}: return None
        if self._running(snapshot) or snapshot.native_task.get("status") not in {"blocked"}: return "partial"
        return "unknown"

    @staticmethod
    def _verify_review_marker(action: Action, snapshot: BoardSnapshot) -> str | None:
        """Prove a handoff from public run metadata and its bound native event."""
        marker = action.target.get("review_marker")
        reviewer = action.target.get("reviewer_profile")
        if not isinstance(marker, Mapping):
            return "unsupported"
        if (not isinstance(reviewer, str) or snapshot.native_task.get("status") not in {"review", "done"}
                or snapshot.native_task.get("assignee") != reviewer):
            return "unsupported"
        runs = [run for run in snapshot.runs if isinstance(run.get("metadata"), Mapping)
                and run["metadata"].get("local_first_review") == dict(marker)]
        if len(runs) != 1:
            return "conflict" if runs else "unsupported"
        run = runs[0]
        if run.get("outcome") != "review_requested" or not isinstance(run.get("profile"), str):
            return "conflict"
        events = [event for event in snapshot.events
                  if event.get("kind") == "review_requested" and str(event.get("run_id")) == str(run.get("id"))
                  and isinstance(event.get("payload"), Mapping)
                  and event["payload"].get("implementer") == run.get("profile")
                  and event["payload"].get("reviewer") == reviewer]
        return None if len(events) == 1 else "conflict" if events else "unsupported"

    @staticmethod
    def _workspace_routing(task: Mapping[str, Any], workspace: str) -> str | None:
        """Verify the exact native workspace representation without inventing defaults."""
        if "workspace" in task:
            return None if task.get("workspace") == workspace else "conflict"
        kind, separator, path = workspace.partition(":")
        if not kind or (kind in {"dir", "worktree"} and (not separator or not path)):
            return "unsupported"
        if "workspace_kind" not in task or task.get("workspace_kind") != kind:
            return "partial" if "workspace_kind" not in task else "conflict"
        if kind in {"dir", "worktree"}:
            if "workspace_path" not in task:
                return "partial"
            return None if task.get("workspace_path") == path else "conflict"
        # scratch is a complete route by kind; native has no required path.
        return None if kind == "scratch" else "unsupported"

    def _verify_existing_create(self, action: Action, snapshot: BoardSnapshot, *, title: str, body: str, assignee: str, workspace: str, idempotency_key: str, native_parent: bool) -> ActionResult:
        task = snapshot.native_task
        if self._running(snapshot):
            return self._result(action, "partial", "marked create has active work and is not safely held", snapshot)
        # ``idempotency_key`` is accepted by native create but deliberately
        # omitted by the installed read serializer.  The generated marker in
        # the exact body binds it to this action; require only observable
        # identity/routing fields and validate optional fields when exposed.
        expected = {"title": title, "body": body, "assignee": assignee}
        missing = [field for field in expected if field not in task]
        if missing:
            return self._result(action, "partial", f"marked create readback cannot expose identity fields: {', '.join(missing)}", snapshot)
        if any(task.get(field) != value for field, value in expected.items()):
            return self._result(action, "conflict", "marked create identity fields differ from action", snapshot)
        workspace_outcome = self._workspace_routing(task, workspace)
        if workspace_outcome is not None:
            return self._result(action, workspace_outcome, "marked create workspace routing differs from action" if workspace_outcome == "conflict" else "marked create readback cannot prove workspace routing", snapshot)
        if "idempotency_key" in task and task.get("idempotency_key") != idempotency_key:
            return self._result(action, "conflict", "marked create idempotency key differs from action", snapshot)
        if task.get("status") != "blocked":
            return self._result(action, "conflict", "marked create is not held", snapshot)
        if native_parent and not any(parent.get("id") == self.anchor_task_id for parent in snapshot.parents):
            return self._result(action, "conflict", "marked create is not associated with anchor", snapshot)
        if not native_parent and snapshot.parents:
            return self._result(action, "conflict", "store-associated create unexpectedly has native dependencies", snapshot)
        return self._capture_accepted_piece_raw_readback(
            action, self._result(action, "no-op", "exact marked held creation already present", snapshot))

    def _capture_accepted_piece_raw_readback(self, action: Action, result: ActionResult) -> ActionResult:
        """Retain the exact public show envelope in new accepted-piece receipts."""
        if (action.target.get("kind") not in {"accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}
                or result.outcome not in {"verified", "no-op"} or result.readback is None):
            return result
        visited = 0
        def detach(value, depth=0):
            nonlocal visited
            visited += 1
            if visited > 20_000 or depth > 64:
                raise ValueError("accepted-piece readback exceeds raw-capture traversal bounds")
            if type(value) is MappingProxyType:
                return {key: detach(child, depth + 1) for key, child in value.items()}
            if type(value) is dict:
                return {key: detach(child, depth + 1) for key, child in dict.items(value)}
            if type(value) in (tuple, list):
                return [detach(child, depth + 1) for child in value]
            return value

        snapshot = None
        try:
            snapshot = BoardSnapshot.from_dict(_plain_json_snapshot(detach(result.readback)))
            task_id = snapshot.native_task.get("id")
            if type(task_id) is not str or not task_id:
                raise ValueError("accepted-piece readback lacks exact native identity")
            show, runs = self._raw_show_and_runs(task_id)
            parents = show.get("parents")
            expected_task = _plain_json_snapshot(detach(snapshot.native_task))
            if (type(show.get("task")) is dict and "workspace_path" in show["task"]
                    and "workspace_path" not in expected_task
                    and isinstance(expected_task.get("workspace"), str)
                    and expected_task["workspace"].startswith("dir:")
                    and show["task"]["workspace_path"] == expected_task["workspace"][4:]):
                expected_task["workspace_path"] = expected_task["workspace"][4:]
            expected_runs = _plain_json_snapshot(detach(snapshot.runs))
            expected_comments = _plain_json_snapshot(detach(snapshot.comments))
            expected_events = _plain_json_snapshot(detach(snapshot.events))
            expected_attachments = _plain_json_snapshot(detach(snapshot.attachments))
            if (show.get("task") != expected_task
                    or type(parents) is not list
                    or parents != [item.get("id") for item in snapshot.parents]
                    or show.get("children") != []
                    or runs != expected_runs
                    or show.get("comments", []) != expected_comments
                    or show.get("events", []) != expected_events
                    or ("attachments" in show and show["attachments"] != expected_attachments)
                    or ("attachments" not in show and snapshot.attachments)):
                raise ValueError("exact raw accepted-piece show differs from verified held snapshot")
            capture = {"kind": "hermes_kanban_raw_capture_v1",
                       "show": _plain_json_snapshot(show), "runs": _plain_json_snapshot(runs)}
            readback = _plain_json_snapshot(detach(result.readback))
            readback["raw_capture_v1"] = capture
            return ActionResult(result.action_key, result.outcome, result.details, readback)
        except (ValueError, TypeError, KeyError, _BoardUnavailable) as exc:
            details = f"accepted-piece exact raw creation capture unavailable: {str(exc)[:384]}"
            return ActionResult(result.action_key, "conflict", details, result.readback)

    def create_held(self, action: Action, *, title: str, body: str, assignee: str, workspace: str, idempotency_key: str) -> ActionResult:
        accepted_piece: Any = None
        if action.target.get("kind") in {"accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}:
            try:
                accepted_piece = self._accepted_piece_payload(action)
            except Exception as exc:
                return self._result(action, "conflict", f"trusted accepted-piece resolution failed: {exc}", None)
            if (title, body, assignee, workspace, idempotency_key) != (accepted_piece.title, accepted_piece.body,
                    accepted_piece.target["assignee"], accepted_piece.target["workspace"], accepted_piece.idempotency_key):
                return self._result(action, "conflict", "accepted-piece create arguments differ from trusted payload", None)
        error = self._scope_and_target(action, "create_held")
        if error or action.target.get("anchor_task_id") != self.anchor_task_id: return self._result(action, "conflict", error or "create target must name exact anchor", None)
        if not all(isinstance(x, str) and x and len(x) <= _MAX_FIELD for x in (title, body, assignee, workspace, idempotency_key)): raise ValueError("bounded non-empty creation values required")
        if not all(_utf8_clean(x) for x in (title, body, assignee, workspace, idempotency_key)):
            raise ValueError("creation values must be valid UTF-8 strings")
        marker = self._create_marker(action)
        native_parent = action.target.get("native_parent", True)
        if not isinstance(native_parent, bool):
            return self._result(action, "conflict", "native_parent must be a boolean when supplied", None)
        if accepted_piece is not None:
            # Canonical accepted-piece payload was checked above; do not route it
            # through legacy planner/correction authority branches.
            pass
        elif not native_parent and ("planner_marker" in action.target or "request_identity" in action.target):
            if not self._valid_parentless_planner(action, assignee=assignee, workspace=workspace, idempotency_key=idempotency_key):
                return self._result(action, "conflict", "parentless planner create requires exact validated request association", None)
            if title != action.target.get("create_title") or body != action.target.get("create_body"):
                return self._result(action, "conflict", "planner create title/body differ from persisted action identity", None)
        elif not native_parent:
            # Paid integrated review and its finding-driven correction are
            # review workflow members, not native children of the tranche
            # anchor.  Their durable operation targets carry association; a
            # parent edge would make the anchor a prerequisite and contradict
            # the canonical separate-review topology.
            kind = action.target.get("kind")
            if kind == "paid_integrated_review_v1":
                required = {"kind", "anchor_task_id", "plan_id", "tranche_id", "head_sha",
                            "check_operation_key", "candidate", "profile", "native_parent"}
                candidate = action.target.get("candidate")
                if (set(action.target) != required or action.target.get("anchor_task_id") != self.anchor_task_id
                        or not all(isinstance(action.target.get(field), str) and action.target[field]
                                   for field in ("plan_id", "tranche_id", "head_sha", "check_operation_key", "profile"))
                        or not isinstance(candidate, Mapping)):
                    return self._result(action, "conflict", "parentless paid review create requires exact revision target", None)
            elif kind == "paid_correction_v1":
                required = {"kind", "anchor_task_id", "plan_id", "ticket_id", "tranche_id", "source_review_id",
                            "source_task_id", "task_id", "head_sha", "candidate", "finding_ids", "findings", "native_parent"}
                finding_ids, findings = action.target.get("finding_ids"), action.target.get("findings")
                if (set(action.target) != required or action.target.get("anchor_task_id") != self.anchor_task_id
                        or action.target.get("task_id") != action.target.get("source_task_id")
                        or not all(isinstance(action.target.get(field), str) and action.target[field]
                                   for field in ("plan_id", "ticket_id", "tranche_id", "source_review_id", "source_task_id", "head_sha"))
                        or not isinstance(action.target.get("candidate"), Mapping)
                        or not isinstance(finding_ids, (tuple, list)) or not finding_ids
                        or not isinstance(findings, (tuple, list)) or len(findings) != len(finding_ids)):
                    return self._result(action, "conflict", "parentless paid correction create requires exact finding target", None)
            else:
                source_task = action.target.get("source_task_id")
                candidate = action.target.get("candidate")
                association = action.target.get("association")
                replacement_for = action.target.get("replacement_for")
                content = candidate.get("content_identity") if isinstance(candidate, Mapping) else None
                if action.target.get("correction_of") == source_task:
                    generation = action.target.get("generation")
                    finding_ids = action.target.get("finding_ids")
                    findings = action.target.get("findings")
                    valid_findings = (
                        isinstance(finding_ids, (tuple, list)) and bool(finding_ids)
                        and all(isinstance(finding_id, str) and finding_id for finding_id in finding_ids)
                        and len(set(finding_ids)) == len(finding_ids)
                        and isinstance(findings, (tuple, list)) and len(findings) == len(finding_ids)
                        and all(isinstance(finding, Mapping)
                                and set(finding) == {"finding_id", "criterion_id", "severity", "summary"}
                                and all(isinstance(finding.get(field), str) and finding[field]
                                        for field in ("finding_id", "criterion_id", "severity", "summary"))
                                and finding["severity"] in {"blocker", "major", "minor"}
                                for finding in findings)
                        and tuple(sorted(finding["finding_id"] for finding in findings)) == tuple(finding_ids)
                    )
                    expected_association = f"separate-review-correction:{source_task}:{content}:{generation}"
                    correction_contract_valid = (
                        isinstance(action.target.get("review_id"), str) and bool(action.target["review_id"])
                        and isinstance(generation, int) and not isinstance(generation, bool) and generation > 0
                        and action.target.get("task_id") == source_task and valid_findings
                    )
                else:
                    expected_association = (f"premature-done:{source_task}:{content}" if replacement_for is None
                                            else f"premature-done-replacement:{replacement_for}:{content}")
                    correction_contract_valid = True
                if (not isinstance(source_task, str) or not source_task or not isinstance(content, str) or not content
                        or (action.target.get("correction_of") is not None and action.target.get("correction_of") != source_task)
                        or (replacement_for is not None and (not isinstance(replacement_for, str)
                            or not replacement_for or replacement_for == source_task))
                        or not correction_contract_valid or association != expected_association):
                    return self._result(action, "conflict", "parentless create requires exact scoped source candidate association", None)
        created_body = body if marker in body else f"{body}\n\n{marker}"
        if len(created_body) > _MAX_FIELD: raise ValueError("creation body plus stable marker exceeds bound")
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton create lock assertion is required and must be held", None)
        if accepted_piece is not None:
            try:
                accepted_piece = self._accepted_piece_payload(action)
            except Exception as exc:
                return self._result(action, "conflict", f"trusted accepted-piece revalidation failed under lock: {exc}", None)
            if (title, body, assignee, workspace, idempotency_key) != (accepted_piece.title, accepted_piece.body,
                    accepted_piece.target["assignee"], accepted_piece.target["workspace"], accepted_piece.idempotency_key):
                return self._result(action, "conflict", "trusted accepted-piece payload changed under lock", None)
        try: before = self._snapshot(self.anchor_task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"pre-create anchor read unavailable: {exc}", None)
        if action.expected_observed_identity != before.digest: return self._result(action, "conflict", "action observation identity is stale", before)
        try:
            marker_count, matches = self._marked_create_matches(marker, idempotency_key=idempotency_key)
        except _BoardUnavailable as exc:
            return self._result(action, "unknown", f"pre-create marker reconciliation unavailable: {exc}", before)
        if marker_count > 1:
            return self._result(action, "conflict", "multiple exact stable create markers found", None)
        if marker_count == 1:
            assert len(matches) == 1
            return self._verify_existing_create(action, matches[0], title=title, body=created_body, assignee=assignee, workspace=workspace, idempotency_key=idempotency_key, native_parent=native_parent)
        if not self._claim_create_attempt(action):
            outcome = "unsupported" if self.claim_create_attempt is None else "unknown"
            return self._result(action, outcome, "durable create attempt claim is required and must succeed before native create", before)
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton create lock was not held immediately before native create", before)
        if accepted_piece is not None:
            try:
                latest_piece: Any = self._accepted_piece_payload(action)
            except Exception as exc:
                return self._result(action, "conflict", f"trusted accepted-piece proof failed immediately before create: {exc}", before)
            if (title, body, assignee, workspace, idempotency_key) != (latest_piece.title, latest_piece.body,
                    latest_piece.target["assignee"], latest_piece.target["workspace"], latest_piece.idempotency_key):
                return self._result(action, "conflict", "trusted accepted-piece payload changed immediately before create", before)
        argv = ("create", title, "--body", created_body, "--assignee", assignee, "--workspace", workspace)
        if native_parent:
            argv += ("--parent", self.anchor_task_id)
        argv += ("--idempotency-key", idempotency_key, "--initial-status", "blocked", "--json")
        try: payload = self._invoke(*argv, json_output=True)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"native create outcome unknown: {exc}", before)
        task_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not task_id: return self._result(action, "unknown", "native create returned no exact task identity", None)
        try: after = self._snapshot(task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"post-create readback unavailable: {exc}", None)
        task = after.native_task
        if self._running(after): return self._result(action, "partial", "created card has active work and is not safely held", after)
        if task.get("status") != "blocked": return self._result(action, "conflict", "create did not leave exact task held", after)
        expected_fields = {"id": task_id, "title": title, "body": created_body, "assignee": assignee}
        missing = [field for field in expected_fields if field not in task]
        if missing: return self._result(action, "partial", f"native create readback cannot expose identity fields: {', '.join(missing)}", after)
        if any(task.get(k) != v for k, v in expected_fields.items()): return self._result(action, "conflict", "created task identity fields differ from action", after)
        workspace_outcome = self._workspace_routing(task, workspace)
        if workspace_outcome is not None:
            return self._result(action, workspace_outcome, "created task workspace routing differs from action" if workspace_outcome == "conflict" else "native create readback cannot prove workspace routing", after)
        if "idempotency_key" in task and task.get("idempotency_key") != idempotency_key:
            return self._result(action, "conflict", "created task idempotency key differs from action", after)
        if native_parent and not any(x.get("id") == self.anchor_task_id for x in after.parents): return self._result(action, "conflict", "created task is not associated with anchor", after)
        if not native_parent and after.parents: return self._result(action, "conflict", "store-associated create unexpectedly has native dependencies", after)
        return self._capture_accepted_piece_raw_readback(
            action, self._result(action, "verified", "held creation verified by exact readback", after))

    def comment(self, action: Action, task_id: str, text: str) -> ActionResult:
        if not isinstance(text, str) or not text or len(text) > _MAX_FIELD: raise ValueError("bounded non-empty comment required")
        marker = f"<!-- local-first-action:{action.key} -->"
        if marker not in text: return self._result(action, "conflict", "comment lacks stable action marker", None)
        before, result = self._preflight(action, "comment", task_id)
        if result is not None: return result
        assert before is not None
        matching = [x for x in before.comments if x.get("body") == text and x.get("author") == _AUTHOR]
        if matching: return self._result(action, "no-op", "exact marked comment already present", before)
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton lock assertion is required immediately before native mutation", before)
        try: self._invoke("comment", task_id, text, "--author", _AUTHOR)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"native comment outcome unknown: {exc}", before)
        try: after = self._snapshot(task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"post-comment readback unavailable: {exc}", None)
        if any(x.get("body") == text and x.get("author") == _AUTHOR for x in after.comments): return self._result(action, "verified", "native comment verified by exact marker and author", after)
        return self._result(action, "conflict", "marked comment absent after native write", after)

    def request_review(self, action: Action, task_id: str, summary: str, *, reviewer: str | None = None, metadata: Mapping[str, Any] | None = None) -> ActionResult:
        if not isinstance(summary, str) or not summary or len(summary) > _MAX_FIELD or not isinstance(reviewer, str) or not reviewer or len(reviewer) > _MAX_FIELD: raise ValueError("bounded summary and reviewer required")
        before, result = self._preflight(action, "request_review", task_id)
        if result is not None: return result
        assert before is not None
        if before.native_task.get("status") not in {"ready", "todo"}: return self._result(action, "conflict", "review request requires exact waiting task", before)
        if reviewer == before.native_task.get("assignee"): return self._result(action, "conflict", "reviewer must differ from implementation assignee", before)
        # A generic CLI invocation cannot safely mutate a run owned by the
        # implementation worker.  That worker must use its native handoff;
        # this adapter retains only read-only marker reconciliation below.
        return self._result(action, "unsupported", "direct request-review is unsupported; only the owning native worker may hand off review", before)

    def request_changes(self, action: Action, task_id: str, reason: str, run_id: str) -> ActionResult:
        before, result = self._preflight(action, "request_changes", task_id)
        return result if result is not None else self._result(action, "unsupported", f"native request-changes cannot target exact review run {run_id}", before)
    def return_waiting_review(self, action: Action, task_id: str, reason: str) -> ActionResult:
        return self._mutate(action, task_id=task_id, argv=("reopen-review", task_id, "--reason", reason), verifier=lambda b,a: None if b.native_task.get("status") == "review" and a.native_task.get("status") in {"ready","todo"} else "conflict", description="reopen-review")
    def hold(self, action: Action, task_id: str, reason: str) -> ActionResult:
        before, result = self._preflight(action, "hold", task_id)
        if result is not None: return result
        assert before is not None
        if self._running(before): return self._result(action, "partial", "running work cannot be safely held", before)
        if before.native_task.get("status") == "blocked": return self._result(action, "no-op", "exact task already held", before)
        marker = self._native_marker(action)
        return self._mutate(action, task_id=task_id, argv=("block", task_id, marker, "--kind", "needs_input"), verifier=lambda _b, a: self._verify_native_hold_release(action, a), description="hold")
    def release(self, action: Action, task_id: str, reason: str) -> ActionResult:
        marker = self._native_marker(action)
        return self._mutate(action, task_id=task_id, argv=("unblock", task_id, "--reason", marker), verifier=lambda _b, a: self._verify_native_hold_release(action, a), description="release")
    def stop_run(self, action: Action, task_id: str, run_id: str, reason: str) -> ActionResult:
        before, result = self._preflight(action, "stop_run", task_id)
        if result is not None: return result
        assert before is not None
        return self._result(action, "conflict" if not any(str(x.get("id")) == str(run_id) for x in before.runs) else "unsupported", "exact run stop is not supported by native CLI", before)
    def _accepted_link_proof(self, action: Action):
        """Resolve only the documented closed adapter wrapper, never raw Action authority.

        Wrapper keys: target, authority, source_id, child_id, before_source,
        before_child, expected_source, source_snapshot, child_snapshot, and
        command_started_seconds. The complete transport is detached and bounded
        as plain JSON before any schema validation or field access.
        """
        if self.accepted_dependency_link_lookup is None:
            raise ValueError("trusted accepted-dependency resolver required")
        value = _plain_json_snapshot(self.accepted_dependency_link_lookup(action.scope, action.key))
        keys = {"target", "authority", "source_id", "child_id", "before_source", "before_child", "expected_source",
                "source_snapshot", "child_snapshot", "command_started_seconds"}
        if type(value) is not dict or set(value) != keys:
            raise ValueError("accepted-link resolver wrapper schema mismatch")
        authority = value["authority"]
        authority_keys = {"kind", "acceptance_identity", "plan_id", "request_identity", "scope", "route",
            "root_task_id", "tranche_ordinal", "tranche_id", "source_ticket_id", "target_ticket_id",
            "source_association", "target_association", "source_task_id", "target_task_id",
            "frozen_create_receipts", "operation_key", "read_only_first_edge_only"}
        if type(authority) is not dict or set(authority) != authority_keys:
            raise ValueError("accepted-link authority schema mismatch")
        def clean_id(item):
            return (type(item) is str and bool(item) and len(item.encode("utf-8")) <= _MAX_FIELD
                    and item == item.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in item))
        id_fields = ("acceptance_identity", "plan_id", "request_identity", "root_task_id", "tranche_id",
            "source_ticket_id", "target_ticket_id", "source_association", "target_association",
            "source_task_id", "target_task_id", "operation_key")
        if any(not clean_id(authority.get(field)) for field in id_fields):
            raise ValueError("accepted-link authority identifiers malformed")
        if (authority["kind"] != "accepted_active_tranche_native_link_v1"
                or type(authority["tranche_ordinal"]) is not int or authority["tranche_ordinal"] != 0
                or authority["read_only_first_edge_only"] is not True
                or type(authority["scope"]) is not dict or authority["scope"] != dict(action.scope)
                or authority["scope"] != {"board_id": self.board, "anchor_task_id": self.anchor_task_id}
                or authority["root_task_id"] != self.anchor_task_id
                or type(authority["route"]) is not dict
                or set(authority["route"]) != {"implementation_profile", "workspace"}
                or any(not clean_id(v) for v in authority["route"].values())):
            raise ValueError("accepted-link authority scope, route, or root mismatch")
        identity = {k: v for k, v in authority.items() if k not in {"operation_key", "read_only_first_edge_only"}}
        expected_key = "native-link:" + _canonical_digest(identity)
        if authority["operation_key"] != expected_key or action.key != expected_key:
            raise ValueError("accepted-link operation key does not bind exact authority")
        if (type(value["source_id"]) is not str or type(value["child_id"]) is not str
                or not clean_id(value["source_id"]) or not clean_id(value["child_id"])
                or value["source_id"] == value["child_id"]
                or value["source_id"] != authority["source_task_id"]
                or value["child_id"] != authority["target_task_id"]
                or type(value["command_started_seconds"]) is not int or value["command_started_seconds"] <= 0):
            raise ValueError("accepted-link IDs or time bound malformed")
        value["before_source"] = _plain_json_snapshot(value["before_source"])
        value["before_child"] = _plain_json_snapshot(value["before_child"])
        value["expected_source"] = _plain_json_snapshot(value["expected_source"])
        target = value["target"]
        expected_keys = {"kind", "source_task_id", "child_task_id", "operation_key"}
        if (type(target) is not dict or set(target) != expected_keys
                or target.get("kind") != "accepted_active_tranche_native_link_v1"
                or target.get("source_task_id") != value["source_id"]
                or target.get("child_task_id") != value["child_id"] or target.get("operation_key") != action.key
                or target != action.to_dict()["target"]):
            raise ValueError("action target differs from trusted canonical link target")
        if (value["source_id"] == self.anchor_task_id
                or not self._trusted_member(action.scope, value["source_id"])
                or not self._trusted_member(action.scope, value["child_id"])):
            raise ValueError("accepted-link endpoints must be distinct managed generation-zero cards")
        receipts = authority["frozen_create_receipts"]
        if type(receipts) is not list or not receipts or len(receipts) > 256:
            raise ValueError("accepted-link frozen receipts malformed")
        receipt_keys = {"ticket_id", "task_id", "association", "operation_key", "digest", "readback"}
        seen_tasks = set(); selected = {}
        for receipt in receipts:
            if type(receipt) is not dict or set(receipt) != receipt_keys:
                raise ValueError("accepted-link receipt schema mismatch")
            if any(not clean_id(receipt.get(k)) for k in ("ticket_id", "task_id", "association", "operation_key")):
                raise ValueError("accepted-link receipt identity malformed")
            if receipt["task_id"] in seen_tasks: raise ValueError("duplicate frozen receipt task identity")
            seen_tasks.add(receipt["task_id"]); selected[receipt["ticket_id"]] = receipt
            if not (type(receipt["digest"]) is str and receipt["digest"].startswith("sha256:")
                    and len(receipt["digest"]) == 71 and all(c in "0123456789abcdef" for c in receipt["digest"][7:])):
                raise ValueError("accepted-link receipt digest malformed")
            raw_receipt = _plain_json_snapshot(receipt["readback"])
            if type(raw_receipt) is not dict:
                raise ValueError("accepted-link receipt snapshot malformed")
            raw_capture = raw_receipt.pop("raw_capture_v1", None)
            if type(raw_capture) is not dict:
                raise ValueError("accepted-link receipt lacks exact raw creation capture")
            try:
                raw_snapshot = BoardSnapshot.from_dict(raw_receipt)
            except (TypeError, ValueError) as error:
                raise ValueError("accepted-link receipt normalized snapshot malformed") from error
            if not self._raw_capture_matches_snapshot(raw_snapshot, raw_capture):
                raise ValueError("accepted-link raw creation capture differs from normalized receipt")
            for field, endpoint_id in (("source_snapshot", value["source_id"]),
                                       ("child_snapshot", value["child_id"])):
                if receipt["task_id"] != endpoint_id:
                    continue
                resolver_snapshot = _plain_json_snapshot(value[field])
                if type(resolver_snapshot) is not dict:
                    raise ValueError("accepted-link resolver snapshot is malformed")
                resolver_capture = resolver_snapshot.pop("raw_capture_v1", None)
                if resolver_capture is not None and resolver_capture != raw_capture:
                    raise ValueError("accepted-link resolver raw capture differs from frozen receipt")
                if resolver_snapshot != raw_receipt:
                    raise ValueError("accepted-link transport snapshot differs from immutable receipt")
        if (authority["source_ticket_id"] not in selected or authority["target_ticket_id"] not in selected
                or selected[authority["source_ticket_id"]]["task_id"] != value["source_id"]
                or selected[authority["target_ticket_id"]]["task_id"] != value["child_id"]):
            raise ValueError("accepted-link endpoints are not unique frozen tranche receipts")
        snapshots = []
        for field, receipt_key in (("source_snapshot", "source_ticket_id"), ("child_snapshot", "target_ticket_id")):
            raw_snapshot = _plain_json_snapshot(value[field])
            receipt = selected[authority[receipt_key]]
            receipt_snapshot = _plain_json_snapshot(receipt["readback"])
            if type(raw_snapshot) is not dict or type(receipt_snapshot) is not dict:
                raise ValueError("accepted-link normalized snapshot is malformed")
            resolver_capture = raw_snapshot.pop("raw_capture_v1", None)
            receipt_capture = receipt_snapshot.pop("raw_capture_v1", None)
            if resolver_capture is not None and resolver_capture != receipt_capture:
                raise ValueError("accepted-link resolver capture differs from frozen create receipt")
            if raw_snapshot != receipt_snapshot or receipt["task_id"] != value["source_id" if field == "source_snapshot" else "child_id"]:
                raise ValueError("accepted-link snapshot does not equal its frozen create receipt")
            snapshot = BoardSnapshot.from_dict(raw_snapshot)
            if snapshot.parents or snapshot.runs or snapshot.native_task.get("status") != "blocked":
                raise ValueError("accepted-link immutable create snapshot malformed or active")
            if snapshot.digest != receipt["digest"]: raise ValueError("receipt digest does not match frozen snapshot")
            snapshots.append(snapshot)
        if authority["source_association"] != selected[authority["source_ticket_id"]]["association"] or authority["target_association"] != selected[authority["target_ticket_id"]]["association"]:
            raise ValueError("accepted-link ticket associations differ from receipts")
        return value, snapshots

    def _raw_show_and_runs(self, task_id: str):
        raw = self._invoke("show", task_id, "--json", json_output=True)
        runs = self._invoke("runs", task_id, "--json", json_output=True)
        if (not isinstance(raw, dict) or not isinstance(raw.get("task"), dict)
                or raw["task"].get("id") != task_id or not isinstance(runs, list)
                or not all(isinstance(run, dict) for run in runs)):
            raise _BoardUnavailable("accepted-link exact raw show or runs malformed")
        return raw, runs

    def read_accepted_first_link_prebarrier(self, action: Action, source_id: str, child_id: str):
        """Read only the exact empty native first-edge barrier; never claim or link.

        A successful capture is evidence for a coordinator-owned pending journal
        reservation, not authorization to perform the later native link effect.
        """
        if not self._assert_create_lock(action):
            raise ValueError("trusted singleton lock assertion required for accepted-link prebarrier")
        target = action.target
        exact_target = {"kind": "accepted_active_tranche_native_link_v1",
                        "source_task_id": source_id, "child_task_id": child_id,
                        "operation_key": action.key}
        if (action.effect != "link" or action.scope != {"board_id": self.board, "anchor_task_id": self.anchor_task_id}
                or target != exact_target or type(source_id) is not str or type(child_id) is not str
                or not source_id or not child_id or source_id == child_id):
            raise ValueError("accepted-link prebarrier requires exact scoped first-edge action and IDs")
        source_raw, source_runs = self._raw_show_and_runs(source_id)
        child_raw, child_runs = self._raw_show_and_runs(child_id)
        if (source_runs or child_runs or source_raw["task"].get("status") != "blocked"
                or child_raw["task"].get("status") != "blocked"
                or source_raw.get("children") != [] or child_raw.get("parents") != []):
            raise ValueError("accepted-link prebarrier is not an empty blocked first edge")
        return {"source_raw": _plain_json_snapshot(source_raw),
                "child_raw": _plain_json_snapshot(child_raw)}

    def _link_accepted_first_edge(self, action: Action, parent_task_id: str, child_task_id: str) -> ActionResult:
        if action.effect != "link" or action.scope != {"board_id": self.board, "anchor_task_id": self.anchor_task_id}:
            return self._result(action, "conflict", "accepted link requires exact link effect and adapter scope", None)
        if self.dependency_link_attempt_claim is None:
            return self._result(action, "unsupported", "durable first-link attempt claim is required", None)
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton lock assertion required before proof resolution", None)
        try:
            proof, immutable = self._accepted_link_proof(action)
        except Exception as exc:
            return self._result(action, "conflict", f"trusted accepted-link resolution failed: {exc}", None)
        source_id, target_id = proof["source_id"], proof["child_id"]
        if (parent_task_id != source_id or child_task_id != target_id
                or action.target["source_task_id"] != source_id or action.target["child_task_id"] != target_id):
            return self._result(action, "conflict", "link arguments differ from canonical trusted endpoints", None)
        if self.dependency_link_attempt_claim is None:
            return self._result(action, "unsupported", "durable first-link attempt claim is required", None)
        try:
            before_source, source_runs = self._raw_show_and_runs(source_id)
            before_child, child_runs = self._raw_show_and_runs(target_id)
            if source_runs or child_runs:
                raise ValueError("accepted-link endpoint has native run history")
            bundle = _evidence_snapshot({"before_source": before_source, "before_child": before_child})
            if (bundle["before_source"] != proof["before_source"]
                    or bundle["before_child"] != proof["before_child"]):
                raise ValueError("current raw before objects differ from frozen accepted proof")
            for current, snapshot in zip((before_source, before_child), immutable):
                snap = snapshot.to_dict()
                snap.pop("observed_at", None)
                raw_as_snapshot = self._snapshot_from_raw(current, [])
                current_data = raw_as_snapshot.to_dict()
                current_data.pop("observed_at", None)
                if current_data != snap:
                    raise ValueError("current native card differs from immutable held create receipt")
            expected_source = dict(bundle["before_source"])
            expected_source["children"] = [target_id]
            if dict(proof["expected_source"]) != expected_source:
                raise ValueError("trusted expected source is not exact single-child append")
            if action.expected_observed_identity != immutable[1].digest:
                raise ValueError("link action identity differs from frozen child receipt")
        except _BoardUnavailable as exc:
            return self._result(action, "unknown", f"accepted-link preflight read unavailable: {exc}", None)
        except Exception as exc:
            return self._result(action, "conflict", f"accepted-link preflight rejected: {exc}", None)
        try:
            self.create_lock_assertion(action.scope, self.anchor_task_id)
        except Exception as exc:
            return self._result(action, "unsupported", f"trusted singleton lock assertion required: {exc}", None)
        try:
            receipt = _plain_json_snapshot(self.dependency_link_attempt_claim(action.scope, action.key))
            expected_keys = {"before_phase", "operation"}
            if type(receipt) is not dict or set(receipt) != expected_keys or receipt.get("before_phase") != "pending":
                raise ValueError("claim receipt must prove pending prior phase")
            operation = receipt.get("operation")
            if type(operation) is not dict or set(operation) != {"key", "scope", "target", "effect", "expected_observed_identity", "before_evidence", "outcome", "readback", "retry", "phase"}:
                raise ValueError("claim receipt operation transport malformed")
            # Strict bounded JSON round-trip rejects custom mappings/hooks and
            # enforces the dedicated whole-evidence aggregate cap.  This is not
            # the scalar field/argv cap: first-link evidence intentionally
            # repeats independently frozen raw cards and receipts.
            encoded = json.dumps(operation, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            if (len(encoded.encode("utf-8")) > _MAX_LINK_CLAIM_OPERATION_BYTES
                    or json.loads(encoded) != operation):
                raise ValueError("claim receipt operation transport is not bounded plain JSON")
            if (operation["key"] != action.key or operation["scope"] != dict(action.scope)
                    or operation["effect"] != "link" or operation["target"] != action.to_dict()["target"]
                    or operation["expected_observed_identity"] != action.expected_observed_identity
                    or operation["phase"] != "unknown"):
                raise ValueError("claim receipt does not bind this link action in unknown phase")
        except Exception as exc:
            return self._result(action, "unknown", f"durable link attempt claim receipt invalid: {exc}", None)
        try:
            self.create_lock_assertion(action.scope, self.anchor_task_id)
        except Exception as exc:
            return self._result(action, "unknown", f"link lock assertion failed after durable claim: {exc}", None)
        started = int(time.time())
        try:
            self._invoke("link", source_id, target_id)
        except (_BoardUnavailable, ValueError) as exc:
            return self._result(action, "unknown", f"native accepted link outcome unknown: {exc}", None)
        ended = int(time.time())
        return self._reconcile_accepted_link(action, proof, immutable, started, ended, pending=True)

    def _snapshot_from_raw(self, raw: Mapping[str, Any], runs: list[dict[str, Any]]) -> BoardSnapshot:
        task = raw["task"]
        data = {"native_task": task, "parents": [{"id": x} for x in raw.get("parents", [])],
                "runs": runs, "comments": raw.get("comments", []), "events": raw.get("events", []),
                "attachments": raw.get("attachments", [])}
        return BoardSnapshot(native_task=data["native_task"], parents=tuple(data["parents"]),
            runs=tuple(runs), comments=tuple(data["comments"]), events=tuple(data["events"]),
            attachments=tuple(data["attachments"]), observed_at=datetime.now(timezone.utc).isoformat(),
            digest=self._digest(data))

    def _reconcile_accepted_link(self, action, proof, immutable, started, ended, *, pending):
        try:
            source_raw, source_runs = self._raw_show_and_runs(proof["source_id"])
            child_raw, child_runs = self._raw_show_and_runs(proof["child_id"])
            if (source_raw == proof["before_source"] and child_raw == proof["before_child"]
                    and not source_runs and not child_runs):
                outcome = "conflict" if pending else "unknown"
                return self._result(action, outcome, "accepted link edge is absent; reconciliation is read-only and will not resend", None)
            transition = validate_accepted_first_link_transition(
                proof["before_source"], proof["before_child"], source_raw, child_raw,
                source_id=proof["source_id"], child_id=proof["child_id"],
                command_started_seconds=max(started, proof["command_started_seconds"]),
                command_ended_seconds=ended)
            if source_runs or child_runs:
                raise ValueError("native runs appeared during accepted link")
            after_source_snapshot = self._snapshot_from_raw(source_raw, source_runs)
            after_child_snapshot = self._snapshot_from_raw(child_raw, child_runs)
            readback = {"kind": "accepted_active_tranche_native_link_readback_v1",
                "source": source_raw, "child": child_raw, "source_snapshot": after_source_snapshot.to_dict(),
                "child_snapshot": after_child_snapshot.to_dict(), "time_window": {"started": started, "ended": ended},
                "transition": {"source_id": transition.source_id, "child_id": transition.child_id,
                    "event": transition.event}}
            return ActionResult(action.key, "verified", "accepted first-edge linked append verified by two-card exact readback", readback)
        except _BoardUnavailable as exc:
            return self._result(action, "unknown", f"accepted-link readback unavailable: {exc}", None)
        except Exception as exc:
            return self._result(action, "conflict", f"accepted-link transition contradicts frozen proof: {exc}", None)

    def link(self, action: Action, parent_task_id: str, child_task_id: str) -> ActionResult:
        if action.target.get("kind") == "accepted_active_tranche_native_link_v2":
            # The coordinator supplies the complete immutable DAG prebarrier and
            # validates the all-card post-state.  The adapter owns only the one
            # supported native command plus endpoint readback; it never invents
            # ordering authority or retries an ambiguous v2 effect.
            exact = {"kind": "accepted_active_tranche_native_link_v2", "operation_key": action.key,
                     "source_ticket_id": action.target.get("source_ticket_id"), "target_ticket_id": action.target.get("target_ticket_id"),
                     "source_task_id": parent_task_id, "target_task_id": child_task_id}
            if action.effect != "link" or dict(action.target) != exact or parent_task_id == child_task_id:
                return self._result(action, "conflict", "generalized accepted-link target differs from exact endpoints", None)
            if not self._assert_create_lock(action):
                return self._result(action, "unsupported", "trusted singleton lock is required before generalized link", None)
            try:
                source_raw, source_runs = self._raw_show_and_runs(parent_task_id)
                child_raw, child_runs = self._raw_show_and_runs(child_task_id)
                if source_runs or child_runs or source_raw["task"].get("status") != "blocked" or child_raw["task"].get("status") != "blocked":
                    return self._result(action, "conflict", "generalized link endpoints are not held with no runs", None)
                if self.create_lock_assertion is None:
                    return self._result(action, "unsupported", "trusted singleton lock assertion callback is unavailable", None)
                self.create_lock_assertion(action.scope, self.anchor_task_id)
                self._invoke("link", parent_task_id, child_task_id)
                _after_source, _ = self._raw_show_and_runs(parent_task_id)
                after_child, _ = self._raw_show_and_runs(child_task_id)
                snapshot_data = {"native_task": after_child["task"], "parents": [{"id": item} for item in after_child.get("parents", [])],
                    "runs": [], "comments": after_child.get("comments", []), "events": after_child.get("events", []),
                    "attachments": after_child.get("attachments", [])}
                snapshot = BoardSnapshot(native_task=snapshot_data["native_task"], parents=tuple(snapshot_data["parents"]),
                    runs=(), comments=tuple(snapshot_data["comments"]), events=tuple(snapshot_data["events"]),
                    attachments=tuple(snapshot_data["attachments"]), observed_at=datetime.now(timezone.utc).isoformat(),
                    digest=self._digest(snapshot_data))
                return self._result(action, "verified", "generalized link command completed; coordinator must validate all-card receipt", snapshot)
            except (_BoardUnavailable, ValueError) as exc:
                return self._result(action, "unknown", f"generalized native link outcome unknown: {exc}", None)
        if action.target.get("kind") == "accepted_active_tranche_native_link_v1":
            return self._link_accepted_first_edge(action, parent_task_id, child_task_id)
        if parent_task_id != self.anchor_task_id or parent_task_id == child_task_id or action.target.get("parent_task_id") != parent_task_id or action.target.get("child_task_id") != child_task_id: return self._result(action, "conflict", "links must originate at exact anchor with distinct endpoints", None)
        error = self._scope_and_target(action, "link")
        if error: return self._result(action, "conflict", error, None)
        if not self._trusted_member(action.scope, child_task_id): return self._result(action, "conflict", "link child is not a trusted managed member", None)
        try:
            parent, child = self._snapshot(parent_task_id), self._snapshot(child_task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"pre-read unavailable: {exc}", None)
        if action.expected_observed_identity != child.digest: return self._result(action, "conflict", "action observation identity is stale", child)
        if self._running(child): return self._result(action, "partial", "running dependent cannot be safely relinked", child)
        if any(item.get("id") == parent_task_id for item in child.parents): return self._result(action, "no-op", "exact dependency already present", child)
        if not self._assert_create_lock(action):
            return self._result(action, "unsupported", "trusted singleton lock assertion is required immediately before native mutation", child)
        try: self._invoke("link", parent_task_id, child_task_id)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"native link outcome unknown: {exc}", child)
        try: after = self._snapshot(child_task_id)
        except _BoardUnavailable as exc: return self._result(action, "unknown", f"post-link readback unavailable: {exc}", None)
        if self._running(after): return self._result(action, "partial", "linked dependent has active work after native link", after)
        if any(item.get("id") == parent_task_id for item in after.parents): return self._result(action, "verified", "dependency verified by exact child readback", after)
        return self._result(action, "conflict", "dependency missing after native link", after)
    def complete_anchor(self, action: Action, task_id: str, approval_evidence: str) -> ActionResult:
        before, result = self._preflight(action, "complete_anchor", task_id)
        if result is not None: return result
        assert before is not None
        if task_id != self.anchor_task_id: return self._result(action, "conflict", "only exact anchor may complete", before)
        if self.completion_evidence_verifier is None: return self._result(action, "unsupported", "trusted acceptance-evidence verifier is required", before)
        try: accepted = self.completion_evidence_verifier(action.scope, task_id, approval_evidence) is True
        except Exception: accepted = False
        if not accepted: return self._result(action, "conflict", "trusted verifier rejected acceptance evidence", before)
        return self._mutate(action, task_id=task_id, argv=("complete", task_id, "--result", approval_evidence), verifier=lambda _b,a: None if a.native_task.get("status") == "done" else "conflict", description="anchor completion")

    @staticmethod
    def _raw_capture_matches_snapshot(snapshot: BoardSnapshot, capture: Any) -> bool:
        """Check the retained raw public envelope against its normalized snapshot."""
        if (type(capture) is not dict or set(capture) != {"kind", "show", "runs"}
                or capture.get("kind") != "hermes_kanban_raw_capture_v1"
                or type(capture.get("show")) is not dict or type(capture.get("runs")) is not list):
            return False
        def thaw(value: Any) -> Any:
            if type(value) is MappingProxyType:
                return {key: thaw(child) for key, child in value.items()}
            if type(value) is dict:
                return {key: thaw(child) for key, child in dict.items(value)}
            if type(value) in (tuple, list):
                return [thaw(child) for child in value]
            return value

        show, runs = capture["show"], capture["runs"]
        if "show_has_runs" in show or "show_runs" in show:
            return False
        expected_task = thaw(snapshot.native_task)
        shown_task = show.get("task")
        if (type(shown_task) is dict and "workspace_path" in shown_task
                and "workspace_path" not in expected_task
                and type(expected_task.get("workspace")) is str
                and expected_task["workspace"].startswith("dir:")
                and shown_task["workspace_path"] == expected_task["workspace"][4:]):
            expected_task["workspace_path"] = expected_task["workspace"][4:]
        expected = {
            "parents": [item.get("id") for item in snapshot.parents],
            "runs": thaw(snapshot.runs),
            "comments": thaw(snapshot.comments),
            "events": thaw(snapshot.events),
            "attachments": thaw(snapshot.attachments),
        }
        return (shown_task == expected_task
                and show.get("parents") == expected["parents"]
                and show.get("children") == []
                and runs == expected["runs"]
                and show.get("comments", []) == expected["comments"]
                and show.get("events", []) == expected["events"]
                and (("attachments" not in show and not expected["attachments"])
                     or show.get("attachments") == expected["attachments"]))

    def validate_accepted_piece_frozen_receipt(self, action: Action, readback: Any, native_task_id: str):
        """Check a historical accepted-piece creation receipt without native I/O.

        This certifies only that a bounded original readback is consistent with
        the trusted accepted-plan description and its digest.  It is not native
        truth or proof that a later effect occurred: a coordinator must still
        validate a fresh post-link transition against the original raw barrier.
        """
        # Detach and bound untrusted transport before invoking the trusted plan
        # resolver or constructing snapshot objects.  Only ordinary dict/list
        # JSON transport is accepted; callers must thaw frozen store objects.
        data = _plain_json_snapshot(readback)
        if type(data) is not dict:
            raise ValueError("frozen accepted-piece receipt is malformed")
        has_raw_capture = "raw_capture_v1" in data
        raw_capture = data.pop("raw_capture_v1", None)
        if (type(native_task_id) is not str or not native_task_id or len(native_task_id) > _MAX_FIELD
                or not _utf8_clean(native_task_id) or native_task_id != native_task_id.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in native_task_id)):
            raise ValueError("exact bounded literal native task ID required")
        fields = {"native_task", "parents", "runs", "comments", "events", "attachments", "observed_at", "digest"}
        if type(data) is not dict or set(data) != fields:
            raise ValueError("frozen receipt snapshot schema mismatch")
        try:
            snapshot = BoardSnapshot.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise ValueError("frozen receipt snapshot is malformed") from exc
        if has_raw_capture and not self._raw_capture_matches_snapshot(snapshot, raw_capture):
            raise ValueError("frozen receipt raw creation capture differs from normalized snapshot")
        canonical_data = {name: data[name] for name in ("native_task", "parents", "runs", "comments", "events", "attachments")}
        if (type(data["digest"]) is not str or len(data["digest"]) != 71 or not data["digest"].startswith("sha256:")
                or any(c not in "0123456789abcdef" for c in data["digest"][7:])
                or data["digest"] != self._digest(canonical_data)):
            raise ValueError("frozen receipt digest does not bind exact snapshot")
        if action.effect != "create_held" or action.target.get("kind") not in {"accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}:
            raise ValueError("frozen receipt requires an accepted-piece create action")
        # This reconstitutes the closed accepted description from trusted plan
        # evidence and proves the supplied Action is exactly its adapter target.
        accepted = self._accepted_piece_payload(action)
        if accepted is None:
            raise ValueError("frozen receipt requires a reconstructed accepted piece")
        marker = self._create_marker(action)
        expected_body = accepted.body if marker in accepted.body else f"{accepted.body}\n\n{marker}"
        task = snapshot.native_task
        required_task = {"id", "title", "body", "assignee", "status"}
        if not required_task <= set(task):
            raise ValueError("frozen receipt native task lacks canonical identity fields")
        if (task.get("id") != native_task_id or task.get("title") != accepted.title
                or task.get("body") != expected_body or task.get("assignee") != accepted.target["assignee"]
                or self._workspace_routing(task, accepted.target["workspace"]) is not None
                or task.get("status") != "blocked"):
            raise ValueError("frozen receipt native task differs from canonical accepted piece")
        if "idempotency_key" in task and task.get("idempotency_key") != accepted.idempotency_key:
            raise ValueError("frozen receipt idempotency key differs from canonical accepted piece")
        if snapshot.parents or snapshot.runs:
            raise ValueError("frozen receipt must be parentless with no native runs")
        return MappingProxyType({
            "kind": "accepted_piece_frozen_receipt_validation_v1",
            "requires_fresh_native_transition_validation": True,
            "native_task_id": native_task_id,
            "snapshot": snapshot,
        })

    def verify_effect(self, action: Action) -> ActionResult:
        if action.effect == "link" and action.target.get("kind") == "accepted_active_tranche_native_link_v1":
            if not self._assert_create_lock(action):
                return self._result(action, "unsupported", "trusted singleton lock required for accepted-link reconciliation", None)
            try:
                proof, immutable = self._accepted_link_proof(action)
                return self._reconcile_accepted_link(action, proof, immutable,
                    proof["command_started_seconds"], int(time.time()), pending=False)
            except Exception as exc:
                return self._result(action, "conflict", f"trusted accepted-link reconciliation failed: {exc}", None)
        if action.effect == "create_held":
            accepted_piece: Any = None
            if action.target.get("kind") in {"accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}:
                if not self._assert_create_lock(action):
                    return self._result(action, "unsupported", "trusted singleton lock is required for accepted-piece reconciliation", None)
                try:
                    accepted_piece = self._accepted_piece_payload(action)
                except Exception as exc:
                    return self._result(action, "conflict", f"trusted accepted-piece reconciliation failed: {exc}", None)
            error = self._scope_and_target(action, "create_held")
            if error:
                return self._result(action, "conflict", error, None)
            if accepted_piece is not None:
                marker = self._create_marker(action)
                created_body = accepted_piece.body if marker in accepted_piece.body else f"{accepted_piece.body}\n\n{marker}"
                try:
                    count, matches = self._marked_create_matches(marker, idempotency_key=accepted_piece.idempotency_key)
                except _BoardUnavailable as exc:
                    return self._result(action, "unknown", f"accepted-piece marker reconciliation unavailable: {exc}", None)
                if count != 1:
                    return self._result(action, "conflict" if count > 1 else "unsupported", "exact accepted-piece marker is not uniquely present", None)
                return self._verify_existing_create(action, matches[0], title=accepted_piece.title, body=created_body,
                    assignee=accepted_piece.target["assignee"], workspace=accepted_piece.target["workspace"],
                    idempotency_key=accepted_piece.idempotency_key, native_parent=False)
            fields = ("create_title", "create_body", "reviewer_profile", "create_workspace", "create_idempotency_key")
            if not all(isinstance(action.target.get(field), str) and action.target[field] for field in fields):
                return self._result(action, "unsupported", "create recovery requires exact persisted create identity", None)
            try:
                count, matches = self._marked_create_matches(self._create_marker(action),
                                                             idempotency_key=action.target["create_idempotency_key"])
            except _BoardUnavailable as exc:
                return self._result(action, "unknown", f"create marker reconciliation unavailable: {exc}", None)
            if count != 1:
                return self._result(action, "conflict" if count > 1 else "unsupported", "exact marked create is not uniquely present", None)
            body = action.target["create_body"]
            marker = self._create_marker(action)
            created_body = body if marker in body else f"{body}\n\n{marker}"
            return self._verify_existing_create(
                action, matches[0], title=action.target["create_title"], body=created_body,
                assignee=action.target["reviewer_profile"], workspace=action.target["create_workspace"],
                idempotency_key=action.target["create_idempotency_key"], native_parent=action.target.get("native_parent", True),
            )
        task_id = action.target.get("task_id")
        if not isinstance(task_id, str) or not task_id: raise ValueError("verify_effect requires exact task target")
        if action.effect not in {"hold", "release", "request_review", "comment"}:
            return self._result(action, "unsupported", "standalone readback is only supported for exact native effect markers", None)
        error = self._scope_and_target(action, action.effect, task_id)
        if error: return self._result(action, "conflict", error, None)
        try: before = self._snapshot(task_id)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"read-only effect verification unavailable: {exc}", None)
        if action.effect == "comment":
            text, author = action.target.get("marker"), action.target.get("author")
            if (not isinstance(text, str) or not text or not isinstance(author, str) or author != _AUTHOR
                    or f"<!-- local-first-action:{action.key} -->" not in text):
                return self._result(action, "conflict", "comment recovery requires exact native author and stable action marker", before)
            exact = [item for item in before.comments if item.get("author") == author and item.get("body") == text]
            marked = [item for item in before.comments if self._contains_marker(item, f"<!-- local-first-action:{action.key} -->")]
            if len(exact) == 1 and len(marked) == 1:
                return self._result(action, "verified", "exact native comment author, marker, and task verified read-only", before)
            if marked:
                return self._result(action, "conflict", "native comment marker contradicts exact author or body", before)
            return self._result(action, "unsupported", "exact native comment marker is absent", before)
        outcome = self._verify_review_marker(action, before) if action.effect == "request_review" else self._verify_native_hold_release(action, before)
        if outcome is None:
            evidence = ("exact scoped native marker, event, and target status" if action.effect == "hold"
                        else "exact scoped UNBLOCK marker and target status" if action.effect == "release"
                        else "exact scoped request-review metadata marker, reviewer, and target status")
            return self._result(action, "verified", f"{evidence} verified read-only", before)
        if outcome == "unsupported": return self._result(action, outcome, "native hold/release marker and event are absent; lane alone cannot prove an effect", before)
        if outcome == "partial": return self._result(action, outcome, "native marker is present but target has active or advanced beyond-ready work", before)
        if outcome == "unknown": return self._result(action, outcome, "native marker history is incomplete and cannot prove the effect", before)
        return self._result(action, "conflict", "native marker history or target state contradicts the action", before)

__all__ = ["BoardCapabilities", "HermesBoardAdapter"]
