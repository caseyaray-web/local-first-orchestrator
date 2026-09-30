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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import Action, ActionResult, BoardSnapshot, validate_scope
from .planning_coordinator import (request_from_payload, _canonical_digest, _evidence_snapshot,
    reconstruct_evidence, first_active_tranche_materialization, accepted_active_tranche_create_payload,
    ActiveTrancheRoute, HeldCardTarget)
from .ticket import parse_contract, contract_payload

_AUTHOR = "local-first-orchestrator"
_MAX_FIELD = 16_384
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
                 accepted_piece_create_lookup: Callable[[Mapping[str, str], str], Any] | None = None) -> None:
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
        self.capabilities = BoardCapabilities.native_m0()

    def _accepted_piece_payload(self, action: Action):
        if action.effect != "create_held" or action.target.get("kind") != "accepted_active_tranche_piece_v1":
            return None
        if self.accepted_piece_create_lookup is None:
            raise ValueError("trusted accepted-piece resolver is required")
        resolved = self.accepted_piece_create_lookup(action.scope, action.key)
        if not all(hasattr(resolved, name) for name in ("title", "body", "target", "idempotency_key", "plan_evidence")):
            raise ValueError("trusted accepted-piece resolver returned malformed payload")
        if not isinstance(resolved.target, Mapping):
            raise ValueError("trusted accepted-piece target must be a mapping")
        canonical = dict(resolved.target)
        expected_kind = "accepted_active_tranche_piece_v1"
        if type(canonical.get("kind")) is not str or canonical.get("kind") != expected_kind:
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
        rebuilt = accepted_active_tranche_create_payload(token, evidence, selected[0])
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
        return self._result(action, "no-op", "exact marked held creation already present", snapshot)

    def create_held(self, action: Action, *, title: str, body: str, assignee: str, workspace: str, idempotency_key: str) -> ActionResult:
        accepted_piece: Any = None
        if action.target.get("kind") == "accepted_active_tranche_piece_v1":
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
        return self._result(action, "verified", "held creation verified by exact readback", after)

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
    def link(self, action: Action, parent_task_id: str, child_task_id: str) -> ActionResult:
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
    def verify_effect(self, action: Action) -> ActionResult:
        if action.effect == "create_held":
            accepted_piece: Any = None
            if action.target.get("kind") == "accepted_active_tranche_piece_v1":
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
        if action.effect not in {"hold", "release", "request_review"}:
            return self._result(action, "unsupported", "standalone readback is only supported for exact native effect markers", None)
        error = self._scope_and_target(action, action.effect, task_id)
        if error: return self._result(action, "conflict", error, None)
        try: before = self._snapshot(task_id)
        except (_BoardUnavailable, ValueError) as exc: return self._result(action, "unknown", f"read-only effect verification unavailable: {exc}", None)
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
