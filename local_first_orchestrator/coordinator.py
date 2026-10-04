"""Scoped, bounded coordinator for durable operator containment and recovery."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, cast

from .budgets import GENERAL_ATTEMPT, PAID_CAPACITY, REVIEW_CORRECTIONS, BudgetPolicy, WORKFLOW_REPAIRS, admit_repair_operation, permit_action
from .contracts import Action, ActionResult, BoardSnapshot, ManagedMember, NATIVE_COMMENT_AUTHOR, OperationIntent, PauseIntent, validate_scope
from .daemon import InstanceLock
from .operator_controls import (
    ReconciliationDecision, native_digest_mismatches, plan_pause, plan_resume,
    reconcile_operator_edits, reject_late_result, verify_containment,
)
from .recovery import RecoveryIssue, deduplicate_issue, detect_issues, propose_repair, summarize_escalation
from .worker_context import (NativeWorkerContext, _is_captured_native_worker_context,
                             capture_native_worker_context)


class Coordinator:
    """Owns the durable fence around at most one native containment effect."""

    def __init__(self, scope: Mapping[str, Any], *, board: Any, store: Any,
                 lock: InstanceLock, git_observer: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None,
                 finalization_observer: Callable[[Mapping[str, str], Any], Mapping[str, Any]] | None = None,
                 repository_telemetry: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None,
                 budget_policy: BudgetPolicy | None = None,
                 configured_roles: Mapping[str, str] | None = None,
                 planning_observer: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None,
                 planning_profile: str | None = None,
                 planning_workspace: str | None = None,
                 combined_check_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
                 recovery_git_adapter: Any = None) -> None:
        self.scope = validate_scope(scope)
        if board is None or store is None or not isinstance(lock, InstanceLock):
            raise ValueError("explicit board, store, and InstanceLock are required")
        self._validate_board(board)
        self.board, self.store, self.lock = board, store, lock
        # Native adapters, including faithful transport fixtures, must invoke the
        # same coordinator-owned claim exactly once immediately before create.
        # This is a protocol boundary, not a production/fake behavior split.
        # Never trust a caller-supplied no-op callback as the mutation fence.
        # The adapter and coordinator must share this exact lock instance.
        board.create_lock_assertion = self._assert_native_lock
        board.claim_create_attempt = self._claim_native_create_attempt
        if not getattr(board, "is_fake", False):
            board.accepted_piece_create_lookup = self._accepted_piece_description
            board.accepted_dependency_link_lookup = self._accepted_dependency_link_description
            board.dependency_link_attempt_claim = self._claim_accepted_first_link_attempt
        if configured_roles is not None and not isinstance(configured_roles, Mapping):
            raise ValueError("configured roles must be a mapping")
        self.configured_roles = MappingProxyType(dict(configured_roles or {}))
        if repository_telemetry is not None and not callable(repository_telemetry):
            raise ValueError("repository telemetry must be a trusted callable")
        if finalization_observer is not None and not callable(finalization_observer):
            raise ValueError("finalization observer must be a trusted callable")
        self.git_observer, self.finalization_observer = git_observer, finalization_observer
        self.repository_telemetry, self.budget_policy = repository_telemetry, budget_policy
        # Trusted composition-root dependency. The callback observes current
        # repository, base, contract and approved objective/configuration itself.
        self.planning_observer = planning_observer
        self.planning_profile = planning_profile
        self.planning_workspace = planning_workspace
        # This is a composition-root dependency, not a model/caller supplied
        # verdict.  It executes the configured deterministic commands and
        # returns their immutable artifacts for one frozen integration head.
        if combined_check_runner is not None and not callable(combined_check_runner):
            raise ValueError("combined check runner must be a trusted callable")
        self.combined_check_runner = combined_check_runner
        # Recovery can continue a revision only through this composition-root
        # adapter; polling never accepts a model/report supplied Git authority.
        self.recovery_git_adapter = recovery_git_adapter
        # Set only by the registered public worker-tool wrapper.  It is scoped
        # to this freshly composed runtime and never populated from tool args.
        self._native_worker_context: NativeWorkerContext | None = None
        # Lifecycle bookkeeping only: public binding is determined by the
        # capture capability above, never by a caller-selected boolean.
        self._native_worker_context_public_entry = False

    def _bind_native_worker_context(self, context: NativeWorkerContext) -> None:
        """Install the one immutable public-entry worker snapshot exactly once."""
        if not _is_captured_native_worker_context(context):
            raise ValueError("native_worker_context_unbound")
        if context.board_id != self.scope["board_id"]:
            raise ValueError("native_worker_context_board_mismatch")
        if self._native_worker_context is not None:
            raise ValueError("native_worker_context_already_bound")
        self._native_worker_context = context
        self._native_worker_context_public_entry = True

    def _ensure_native_worker_context(self) -> NativeWorkerContext:
        """Capture once per direct legacy entry, never refreshing public-tool state."""
        if self._native_worker_context is None or not self._native_worker_context_public_entry:
            context = capture_native_worker_context()
            if context.board_id != self.scope["board_id"]:
                raise ValueError("native_worker_context_board_mismatch")
            self._native_worker_context = context
            self._native_worker_context_public_entry = False
        assert self._native_worker_context is not None
        return self._native_worker_context

    def _current_native_worker_context(self, *, task_id: str | None = None,
                                       run_id: str | None = None) -> NativeWorkerContext:
        context = self._native_worker_context
        if context is None:
            raise ValueError("native_worker_context_unbound")
        if ((task_id is not None and context.task_id != task_id)
                or (run_id is not None and context.run_id != run_id)):
            raise ValueError("native_worker_context_task_run_mismatch")
        return context

    def _accepted_piece_description(self, scope: Mapping[str, str], operation_key: str) -> Any:
        """Resolve a native create solely from strict accepted-store authority."""
        from .planning_coordinator import (ActiveTrancheRoute,
            accepted_active_tranche_create_payload, first_active_tranche_materialization)
        if validate_scope(scope) != self.scope or type(operation_key) is not str:
            raise ValueError("accepted-piece resolver scope or key mismatch")
        state = self.store.read_scope(self.scope)
        matches = [op for op in state["operations"] if op.key == operation_key and op.effect == "create_held"]
        if len(matches) != 1:
            raise ValueError("accepted-piece operation intent is not uniquely reserved")
        intent = matches[0]
        token_key = intent.target.get("accepted_token_key")
        plan_id = intent.target.get("plan_id")
        if type(plan_id) is not str or type(token_key) is not str:
            raise ValueError("accepted-piece intent lacks closed token identity")
        token = self.store.read_accepted_plan(self.scope, plan_id)
        if token_key != "accept-plan:" + token["acceptance_identity"]:
            raise ValueError("accepted-piece intent token key mismatch")
        evidence = self.store.read_plan(self.scope, plan_id)
        route = ActiveTrancheRoute(token["route"]["implementation_profile"], token["route"]["workspace"])
        material = first_active_tranche_materialization(evidence, route)
        tickets = [target for target in material.targets if target.ticket_id == intent.target.get("ticket_id")]
        if len(tickets) != 1 or tickets[0].operation_key != operation_key:
            raise ValueError("accepted-piece ticket/key is not a unique tranche-zero target")
        def thaw(value):
            if type(value) is type(token):
                return {key: thaw(child) for key, child in value.items()}
            if type(value) is dict:
                return {key: thaw(child) for key, child in value.items()}
            if type(value) in (list, tuple):
                return [thaw(child) for child in value]
            return value
        body_kind = intent.target.get("kind")
        if body_kind not in {"accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}:
            raise ValueError("reserved accepted-piece body kind is unsupported")
        built = accepted_active_tranche_create_payload(thaw(token), evidence, tickets[0], body_kind=body_kind)
        if intent.target.get("accepted_token_key") != built.target["accepted_token_key"]:
            raise ValueError("reserved token identity does not match rebuilt payload")
        return type("AcceptedPieceDescription", (), {
            "title": built.title, "body": built.body, "target": built.target,
            "idempotency_key": built.idempotency_key, "plan_evidence": evidence,
        })()

    def prepare_active_tranche(self, plan_id: str, *, request_id: str | None = None,
                               _already_locked: bool = False) -> Mapping[str, Any]:
        """Prepare accepted tranche-zero held pieces under one mutation lock."""
        with (nullcontext() if _already_locked else self.lock):
            self._assert_lock()
            return self._prepare_active_tranche_locked(plan_id, request_id=request_id)

    def _prepare_active_tranche_locked(self, plan_id: str, *, request_id: str | None = None) -> Mapping[str, Any]:
        """Prepare only accepted tranche zero, serially, without lifecycle activation."""
        from .planning_coordinator import ActiveTrancheRoute, first_active_tranche_materialization
        if type(plan_id) is not str or not plan_id.strip() or len(plan_id) > 256:
            raise ValueError("bounded explicit plan ID is required")
        if request_id is not None and (type(request_id) is not str or not request_id.strip() or len(request_id) > 256):
            raise ValueError("request_id must be a bounded explicit string")
        implementation, _ = self._local_review_roles()
        workspace = self.planning_workspace
        if (not isinstance(workspace, str) or not os.path.isabs(workspace)
                or os.path.realpath(workspace) != workspace or not os.path.isdir(workspace)
                or os.path.islink(workspace)):
            raise ValueError("trusted persistent planning workspace must be canonical and existing")

        def source():
            token = self.store.read_accepted_plan(self.scope, plan_id)
            registration = self.store.read_planning_request(self.scope, request_id=request_id)
            evidence = self.store.read_plan(self.scope, plan_id)
            route = ActiveTrancheRoute(implementation, workspace)
            material = first_active_tranche_materialization(evidence, route)
            from .planning_coordinator import request_from_payload, request_payload
            observed = self.planning_observer(dict(self.scope)) if self.planning_observer is not None else None
            if not isinstance(observed, Mapping) or set(observed) != {"request"}:
                raise ValueError("trusted planning observer returned malformed observation")
            current = request_from_payload(observed["request"])
            registered = request_from_payload(registration["request"])
            if (request_payload(current) != request_payload(registered)
                    or current.identity != token["request_identity"]):
                raise ValueError("trusted current and accepted plan requests differ")
            if token.get("route") != {"implementation_profile": implementation, "workspace": workspace}:
                raise ValueError("accepted route differs from configured implementation role/workspace")
            identity = (token["acceptance_identity"], token["request_identity"],
                        tuple((target.ticket_id, target.operation_key, target.association) for target in material.targets))
            return identity, material

        initial_identity, material = source()
        tickets = tuple(target.ticket_id for target in material.targets)
        pieces = []
        attempted = 0
        for ticket_id in tickets:
            try:
                current_identity, _ = source()
                if current_identity != initial_identity:
                    raise ValueError("accepted tranche source identity drift")
                result = self.prepare_active_piece(plan_id, ticket_id, request_id=request_id, _already_locked=True)
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
                pieces.append({"outcome": "partial", "plan_id": plan_id, "ticket_id": ticket_id,
                               "reason": str(error), "actions_attempted": 0})
                return {"outcome": "partial", "plan_id": plan_id, "request_id": request_id,
                        "active_tranche": {"tranche_id": material.active_tranche_id, "ordinal": 0},
                        "pieces": tuple(pieces), "completed": sum(item.get("outcome") == "held" for item in pieces), "total": len(tickets),
                        "actions_attempted": attempted}
            pieces.append(dict(result))
            count = result.get("actions_attempted", 0)
            if type(count) is int and count >= 0:
                attempted += count
            if result.get("outcome") != "held":
                return {"outcome": "partial", "plan_id": plan_id, "request_id": request_id,
                        "active_tranche": {"tranche_id": material.active_tranche_id, "ordinal": 0},
                        "pieces": tuple(pieces), "completed": sum(item.get("outcome") == "held" for item in pieces), "total": len(tickets),
                        "actions_attempted": attempted}
        try:
            final_identity, _ = source()
            if final_identity != initial_identity:
                raise ValueError("accepted tranche source identity drift at final barrier")
            # Read-only applied-operation replay validates exact persisted marker and
            # membership/card readback through the existing authority path.
            verified = []
            for ticket_id in tickets:
                result = self.prepare_active_piece(plan_id, ticket_id, request_id=request_id, _already_locked=True)
                if result.get("outcome") != "held" or result.get("actions_attempted") != 0:
                    raise ValueError("final tranche proof was not read-only and held")
                verified.append(result)
            if tuple((item.get("ticket_id"), item.get("operation_key"), item.get("task_id")) for item in verified) != tuple(
                    (item.get("ticket_id"), item.get("operation_key"), item.get("task_id")) for item in pieces):
                raise ValueError("final tranche proof differs from initial held receipts")
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
            return {"outcome": "partial", "plan_id": plan_id, "request_id": request_id,
                    "active_tranche": {"tranche_id": material.active_tranche_id, "ordinal": 0},
                    "pieces": tuple(pieces), "completed": len(pieces), "total": len(tickets),
                    "actions_attempted": attempted, "reason": str(error)}
        return {"outcome": "held", "plan_id": plan_id, "request_id": request_id,
                "active_tranche": {"tranche_id": material.active_tranche_id, "ordinal": 0},
                "pieces": tuple(pieces), "completed": len(pieces), "total": len(tickets),
                "actions_attempted": attempted}

    def prepare_active_piece(self, plan_id: str, ticket_id: str, *, request_id: str | None = None,
                             _already_locked: bool = False) -> Mapping[str, Any]:
        """Reserve/create at most one accepted tranche-zero held piece."""
        from .contracts import ManagedMember, Action
        from .planning_coordinator import ActiveTrancheRoute, first_active_tranche_materialization
        if any(type(value) is not str or not value.strip() or len(value) > 256 for value in (plan_id, ticket_id)):
            raise ValueError("bounded explicit plan and ticket IDs are required")
        if request_id is not None and (type(request_id) is not str or not request_id.strip() or len(request_id) > 256):
            raise ValueError("request_id must be a bounded explicit string")
        implementation, _ = self._local_review_roles()
        workspace = self.planning_workspace
        if (not isinstance(workspace, str) or not os.path.isabs(workspace) or os.path.realpath(workspace) != workspace
                or not os.path.isdir(workspace) or os.path.islink(workspace)):
            raise ValueError("trusted persistent planning workspace must be canonical and existing")
        with (nullcontext() if _already_locked else self.lock):
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            pause = state["operator_intent"]
            if pause is not None and pause.active:
                raise ValueError("active-piece creation is fenced by pause/cancellation")
            token = self.store.read_accepted_plan(self.scope, plan_id)
            # The store resolves an explicit alias in the immutable registration
            # key; aliases are deliberately not duplicated in the registration
            # payload. Successful strict lookup is the alias binding proof.
            registration = self.store.read_planning_request(self.scope, request_id=request_id)
            evidence = self.store.read_plan(self.scope, plan_id)
            if self.planning_observer is None:
                raise ValueError("trusted planning observer is required")
            observed = self.planning_observer(dict(self.scope))
            from .planning_coordinator import request_from_payload, request_payload
            current = request_from_payload(observed["request"])
            registered = request_from_payload(registration["request"])
            if request_payload(current) != request_payload(registered) or current.identity != token["request_identity"]:
                raise ValueError("trusted current and accepted plan requests differ")
            route = token["route"]
            if route != {"implementation_profile": implementation, "workspace": workspace}:
                raise ValueError("accepted route differs from configured implementation role/workspace")
            root = [m for m in state["members"] if m.role == "root"]
            if len(root) != 1 or root[0].task_id != self.scope["anchor_task_id"]:
                raise ValueError("exactly one scope root member required")
            material = first_active_tranche_materialization(evidence, ActiveTrancheRoute(implementation, workspace))
            selected = [item for item in material.targets if item.ticket_id == ticket_id]
            if len(selected) != 1:
                raise ValueError("ticket must uniquely belong to accepted tranche zero")
            target = selected[0]
            operation_key = target.operation_key
            member_assoc = target.association
            conflicts = [m for m in state["members"] if m.work_association == member_assoc]
            prior = next((op for op in state["operations"] if op.key == operation_key), None)
            if conflicts:
                # Existing associations are authoritative only when the immutable
                # applied create receipt independently binds the exact member.
                if (prior is None or prior.phase != "applied" or prior.effect != "create_held"
                        or prior.outcome not in {"verified", "no-op"}
                        or prior.target.get("association") != member_assoc):
                    raise ValueError("existing piece association has no applied creation authority")
                receipt = self._snapshot_from_readback(prior.readback)
                if (len(conflicts) != 1 or receipt is None
                        or conflicts[0] != ManagedMember(self.scope["board_id"], self.scope["anchor_task_id"],
                            str(receipt.native_task.get("id")), "implementation", 0, (), member_assoc)):
                    raise ValueError("existing piece association conflicts with immutable creation receipt")
                if (prior.target.get("task_id") != self.scope["anchor_task_id"]
                        or prior.target.get("ticket_id") != ticket_id
                        or prior.target.get("plan_id") != plan_id):
                    raise ValueError("existing piece association belongs to another accepted task")
            # Derive canonical action authority solely from the accepted token and
            # evidence, then compare the complete durable intent before any board read.
            def thaw_accepted(value):
                if isinstance(value, Mapping):
                    return {key: thaw_accepted(child) for key, child in value.items()}
                if isinstance(value, (tuple, list)):
                    return [thaw_accepted(child) for child in value]
                return value
            body_kind = ("accepted_active_tranche_piece_v2" if prior is None else prior.target.get("kind"))
            if body_kind not in {"accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}:
                raise ValueError("reserved accepted-piece body kind is unsupported")
            payload = __import__("local_first_orchestrator.planning_coordinator", fromlist=["accepted_active_tranche_create_payload"]).accepted_active_tranche_create_payload(
                thaw_accepted(token), evidence, target, body_kind=body_kind)
            full_target = {**dict(payload.target), "task_id": self.scope["anchor_task_id"],
                "anchor_task_id": self.scope["anchor_task_id"], "native_parent": False, "native_deps": [],
                "plan_id": plan_id, "ticket_id": ticket_id,
                "accepted_token_key": payload.target["accepted_token_key"]}
            if prior is not None and (thaw_accepted(prior.target) != full_target or prior.effect != "create_held"
                    or prior.key != operation_key or dict(prior.scope) != dict(self.scope)
                    or prior.target.get("task_id") != self.scope["anchor_task_id"]):
                raise ValueError("persisted active-piece intent differs from canonical accepted target")
            anchor = self.board.read_task(self.scope["anchor_task_id"])
            if not isinstance(anchor, BoardSnapshot) or anchor.native_task.get("id") != self.scope["anchor_task_id"]:
                raise ValueError("fresh exact native anchor required")
            expected = anchor.digest if prior is None else prior.expected_observed_identity
            built = payload
            if prior is None:
                intent = self._intent_for(Action(operation_key, self.scope, full_target, "create_held", expected))
                prior = self.store.reserve_operation(intent)
            action = self._action_from_intent(prior)
            native_target = {key: value for key, value in action.target.items() if key != "plan_id"}
            native_action = Action(action.key, action.scope, native_target, action.effect,
                                   action.expected_observed_identity)
            attempted = 0
            try:
                if prior.phase == "applied":
                    verifier = getattr(self.board, "verify_effect", None)
                    if not callable(verifier):
                        return {"outcome":"partial", "reason":"read-only applied receipt verifier unavailable", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":0}
                    result = verifier(native_action)
                elif prior.phase == "unknown":
                    verifier = getattr(self.board, "verify_effect", None)
                    if callable(verifier) and action.target.get("kind") in {"accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}:
                        # plan_id is durable coordinator resolver context, not part of
                        # the adapter's exact canonical accepted payload. ticket_id is canonical.
                        native_target = {key: value for key, value in action.target.items()
                                         if key != "plan_id"}
                        native_action = Action(action.key, action.scope, native_target, action.effect,
                                               action.expected_observed_identity)
                        result = verifier(native_action)
                    else:
                        result = verifier(action) if callable(verifier) else ActionResult(operation_key, "unknown", "read-only verifier unavailable", None)
                else:
                    attempted = 1
                    # plan_id is coordinator-local resolver context, not part of the
                    # native adapter's canonical create target contract.
                    native_target = {key: value for key, value in action.target.items() if key != "plan_id"}
                    native_action = Action(action.key, action.scope, native_target, action.effect,
                                           action.expected_observed_identity)
                    try:
                        result = self.board.create_held(native_action, title=built.title, body=built.body,
                            assignee=implementation, workspace=f"dir:{workspace}", idempotency_key=operation_key)
                    except Exception as error:
                        return {"outcome":"partial", "reason":f"create attempt outcome unknown: {error}", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
                try:
                    readback = self._portable_readback(result.readback)
                    self.store.record_effect_observation(self.scope, operation_key, outcome=result.outcome, details=result.details, readback=readback)
                except Exception as error:
                    return {"outcome":"partial", "reason":f"effect observation unavailable; outcome unknown: {error}", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
                proof = self._snapshot_from_readback(readback)
                if (prior.phase != "applied" and proof is not None
                        and action.target.get("kind") in {
                            "accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}
                        and not self._accepted_piece_raw_capture_matches(readback, proof)):
                    return {"outcome":"partial", "reason":"accepted-piece acknowledgement requires an exact immutable raw creation capture", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
                if result.outcome not in {"verified", "no-op"} or proof is None:
                    return {"outcome":"partial", "reason":result.details, "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
                if prior.phase != "applied":
                    # Keep the adapter's exact versioned raw-show capture in
                    # the immutable applied acknowledgement alongside the
                    # normalized snapshot used by existing readers.
                    self.store.ack_effect(self.scope, operation_key, readback=readback, outcome=result.outcome)
                else:
                    saved = self._snapshot_from_readback(prior.readback)
                    if (saved is None or proof.digest != saved.digest or proof.native_task.get("id") != saved.native_task.get("id")):
                        return {"outcome":"partial", "reason":"applied receipt differs from immutable acknowledgement readback", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":0}
                # An operator pause may race a native acknowledgement. Preserve the
                # receipt, but do not enroll work after the intent changes.
                latest_state = self.store.read_scope(self.scope)
                latest_pause = latest_state["operator_intent"]
                if latest_pause is not None and latest_pause.active:
                    return {"outcome":"partial", "reason":"active-piece creation acknowledged while pause/cancellation became active", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
                latest_root = [m for m in latest_state["members"] if m.role == "root"]
                if len(latest_root) != 1 or latest_root[0].task_id != self.scope["anchor_task_id"]:
                    return {"outcome":"partial", "reason":"scope root enrollment changed after native acknowledgement", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
                current_card = self.board.read_task(str(proof.native_task.get("id")))
                if current_card.digest != proof.digest or current_card.native_task.get("status") != "blocked" or current_card.parents or current_card.runs or current_card.native_task.get("assignee") != implementation or (prior.phase == "applied" and current_card.digest != prior.readback.get("digest")):
                    return {"outcome":"partial", "reason":"fresh held-card readback conflicts", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
                existing = [m for m in self.store.read_scope(self.scope)["members"] if m.work_association == member_assoc]
                member = ManagedMember(self.scope["board_id"], self.scope["anchor_task_id"], str(proof.native_task["id"]), "implementation", 0, (), member_assoc)
                if existing and existing != [member]:
                    return {"outcome":"partial", "reason":"existing member association conflicts", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
                try:
                    # Membership is authorized only by a fresh exact readback above.
                    # A store failure after native acknowledgement remains recoverable
                    # from the immutable receipt; never report held without membership.
                    self.store.register_member(member)
                except Exception as error:
                    return {"outcome":"partial", "reason":f"membership registration failed after native acknowledgement: {error}","plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
                return {"outcome":"held", "task_id":member.task_id,"plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
            except Exception as error:
                return {"outcome": "partial", "reason": str(error), "plan_id": plan_id, "ticket_id": ticket_id, "operation_key": operation_key, "actions_attempted": attempted}
    def inspect_accepted_active_tranche_dependencies(self, plan_id: str, *, request_id: str | None = None) -> Mapping[str, Any]:
        """Read the entire accepted active DAG without reserving or changing it.

        This is diagnostic prebarrier/reconciliation evidence only.  It does not
        certify completion and deliberately has no operation-store write path.
        """
        from .accepted_dependency_inspection import inspect_accepted_active_tranche_dag

        def plain(value):
            if isinstance(value, Mapping):
                return {key: plain(child) for key, child in value.items()}
            if isinstance(value, (tuple, list)):
                return [plain(child) for child in value]
            return value

        def uncertainty_report(reason):
            return MappingProxyType({"outcome": "conflict", "declared_edges": declared,
                "proven_applied_edges": (), "pending_or_unknown_edges": (),
                "missing_declared_edges": declared,
                "conflicts": ({"kind": "observation_uncertain", "reason": str(reason)[:384]},),
                "observation_uncertain": True, "observation_is_not_atomic": True,
                "no_effect_authority": True})

        with self.lock:
            self._assert_lock()
            before_state = self.store.read_scope(self.scope)
            paused = before_state["operator_intent"] is not None and before_state["operator_intent"].active
            if paused:
                # Describe only persisted accepted semantics while paused. No
                # observer/native read can prove an edge in this diagnostic.
                from .planning_coordinator import ActiveTrancheRoute, first_active_tranche_materialization
                paused_token = self.store.read_accepted_plan(self.scope, plan_id)
                paused_evidence = self.store.read_plan(self.scope, plan_id)
                paused_material = first_active_tranche_materialization(
                    paused_evidence, ActiveTrancheRoute(
                        paused_token["route"]["implementation_profile"], paused_token["route"]["workspace"]))
                paused_declared = tuple((dependency, target.ticket_id)
                    for target in paused_material.targets for dependency in target.declared_dependencies)
                return MappingProxyType({"outcome": "paused", "declared_edges": paused_declared,
                    "missing_declared_edges": paused_declared, "proven_applied_edges": (),
                    "pending_or_unknown_edges": (), "conflicts": (), "opaque_fields": (),
                    "no_effect_authority": True, "observation_is_not_atomic": True,
                    "reason": "operator pause/cancellation remains active; declared edges are not observed"})
            token = self.store.read_accepted_plan(self.scope, plan_id)
            evidence = self.store.read_plan(self.scope, plan_id)
            from .planning_coordinator import ActiveTrancheRoute, first_active_tranche_materialization
            implementation, _ = self._local_review_roles()
            # Every DAG shape shares the persisted authority fence; an empty
            # edge table never bypasses request, route, root, observer, or
            # accepted membership validation.
            context = self._active_piece_dependency_persisted_context_locked(
                plan_id, None, None, request_id=request_id)
            material = first_active_tranche_materialization(
                evidence, ActiveTrancheRoute(token["route"]["implementation_profile"], token["route"]["workspace"]))
            declared = tuple((dependency, target.ticket_id) for target in material.targets
                             for dependency in target.declared_dependencies)
            receipts = plain(context["frozen_create_receipts"])
            task_ids = tuple(item["task_id"] for item in receipts)
            operations_by_key = {operation.key: operation for operation in before_state["operations"]}
            frozen_raw = {}
            try:
                for receipt in receipts:
                    operation = operations_by_key.get(receipt["operation_key"])
                    stored = None if operation is None else plain(operation.readback)
                    capture = stored.get("raw_capture_v1") if type(stored) is dict else None
                    if (type(capture) is not dict or set(capture) != {"kind", "show", "runs"}
                            or capture.get("kind") != "hermes_kanban_raw_capture_v1"
                            or type(capture.get("show")) is not dict or type(capture.get("runs")) is not list):
                        raise ValueError("immutable accepted-piece creation receipt lacks exact raw show capture")
                    show, runs = capture["show"], capture["runs"]
                    snapshot = receipt["readback"]
                    if type(snapshot) is not dict or type(snapshot.get("native_task")) is not dict:
                        raise ValueError("normalized accepted-piece create receipt is malformed")
                    expected_task = dict(snapshot["native_task"])
                    shown_task = show.get("task")
                    if (type(shown_task) is dict and "workspace_path" in shown_task
                            and "workspace_path" not in expected_task
                            and type(expected_task.get("workspace")) is str
                            and expected_task["workspace"].startswith("dir:")
                            and shown_task["workspace_path"] == expected_task["workspace"][4:]):
                        expected_task["workspace_path"] = expected_task["workspace"][4:]
                    if (shown_task != expected_task
                            or show.get("children") != []
                            or show.get("parents") != [item["id"] for item in snapshot["parents"]]
                            or runs != snapshot["runs"]
                            or show.get("events") != snapshot["events"]
                            or show.get("comments") != snapshot["comments"]
                            or ("attachments" in show and show["attachments"] != snapshot["attachments"])
                            or ("attachments" not in show and snapshot["attachments"])):
                        raise ValueError("immutable raw creation envelope disagrees with normalized create receipt")
                    frozen_raw[receipt["ticket_id"]] = {
                        **show, "runs": runs, "show_has_runs": "runs" in show,
                        "show_runs": show.get("runs") if "runs" in show else None,
                    }
            except (KeyError, TypeError, ValueError) as error:
                return MappingProxyType({"outcome": "conflict", "declared_edges": declared,
                    "proven_applied_edges": (), "pending_or_unknown_edges": (),
                    "missing_declared_edges": declared,
                    "conflicts": ({"kind": "raw_creation_capture_conflict", "reason": str(error)[:384]},),
                    "observation_is_not_atomic": True, "no_effect_authority": True})
            try:
                reader = getattr(self.board, "read_accepted_active_tranche_raw_cards", None)
                if not callable(reader):
                    raise ValueError("production complete raw active-tranche reader unavailable")
                raw_cards = reader(self.scope, task_ids)
            except Exception as error:
                # Only the observation boundary is translated here. Invalid
                # accepted authority still fails at its earlier strict fence.
                return MappingProxyType({"outcome": "conflict", "declared_edges": declared,
                    "missing_declared_edges": declared, "proven_applied_edges": (),
                    "pending_or_unknown_edges": (), "opaque_fields": (),
                    "conflicts": (MappingProxyType({"kind": "raw_observation_conflict",
                                                   "reason": str(error)[:384]}),),
                    "observation_is_not_atomic": True, "no_effect_authority": True})
            try:
                self._active_piece_dependency_final_fence_locked(context)
                after_state = self.store.read_scope(self.scope)
                uncertain = (tuple(before_state["members"]) != tuple(after_state["members"])
                             or tuple(before_state["operations"]) != tuple(after_state["operations"])
                             or before_state["operator_intent"] != after_state["operator_intent"]
                             or self.store.read_accepted_plan(self.scope, plan_id) != token
                             or self.store.read_plan(self.scope, plan_id) != evidence)
            except ValueError as error:
                return uncertainty_report(error)
            if uncertain:
                return uncertainty_report("persisted accepted-tranche observations changed during native reads")
            by_task = {item["task_id"]: item["ticket_id"] for item in receipts}
            known, pending, invalid_applied = [], [], []
            for operation in after_state["operations"]:
                target = plain(operation.target)
                if type(target) is not dict:
                    if operation.effect == "link" and operation.phase == "applied":
                        invalid_applied.append({"kind": "applied_receipt_conflict",
                            "operation_key": operation.key, "reason": "applied link target is malformed"})
                    continue
                if operation.effect != "link":
                    continue
                if target.get("kind") != "accepted_active_tranche_native_link_v1":
                    if operation.phase == "applied":
                        invalid_applied.append({"kind": "applied_receipt_conflict",
                            "operation_key": operation.key,
                            "reason": "applied link target has missing or unsupported kind"})
                    continue
                source_task_id = target.get("source_task_id")
                child_task_id = target.get("child_task_id")
                if type(source_task_id) is not str or type(child_task_id) is not str:
                    if operation.phase == "applied":
                        invalid_applied.append({"kind": "applied_receipt_conflict",
                            "operation_key": operation.key,
                            "reason": "applied link has malformed source or child task identifier"})
                    continue
                source = by_task.get(source_task_id); child = by_task.get(child_task_id)
                edge = (source, child)
                if source is None or child is None or edge not in declared:
                    if operation.phase == "applied":
                        invalid_applied.append({"kind": "applied_receipt_conflict",
                            "operation_key": operation.key, "reason": "applied link does not name a declared active edge"})
                    continue
                if operation.phase == "applied":
                    def record_invalid_applied(error):
                        invalid_applied.append({"kind": "applied_receipt_conflict",
                            "operation_key": operation.key, "edge": edge,
                            "reason": str(error)[:384]})
                    if operation.outcome not in {"verified", "no-op"} or operation.readback is None:
                        record_invalid_applied("applied operation lacks verified outcome or immutable readback")
                        continue
                    try:
                        edge_context = self._active_piece_dependency_persisted_context_locked(
                            plan_id, child, source, request_id=request_id)
                        expected_target = {"kind": "accepted_active_tranche_native_link_v1",
                            "source_task_id": edge_context["authority_identity"]["source_task_id"],
                            "child_task_id": edge_context["authority_identity"]["target_task_id"],
                            "operation_key": edge_context["operation_key"]}
                        expected_authority = self._plain_link_value({**dict(edge_context["authority_identity"]),
                            "operation_key": edge_context["operation_key"], "scope": dict(self.scope),
                            "frozen_create_receipts": edge_context["frozen_create_receipts"],
                            "read_only_first_edge_only": True})
                        before = self._plain_link_value(operation.before_evidence)
                        if (operation.key != edge_context["operation_key"] or dict(operation.scope) != dict(self.scope)
                                or self._plain_link_value(operation.target) != expected_target
                                or type(before) is not dict or before.get("authority") != expected_authority):
                            raise ValueError("applied link receipt does not bind the reconstructed accepted edge")
                        checked = self._checked_accepted_link_readback(
                            operation, operation.readback,
                            source_task_id=expected_target["source_task_id"],
                            child_task_id=expected_target["child_task_id"], require_prebarrier=True,
                            expected_request_id=request_id)
                        source_show = self._exact_inspection_show(raw_cards.get(expected_target["source_task_id"]))
                        child_show = self._exact_inspection_show(raw_cards.get(expected_target["child_task_id"]))
                        if checked["source"] != source_show or checked["child"] != child_show:
                            raise ValueError("applied accepted-link receipt differs from fresh exact raw show envelope")
                    except (KeyError, TypeError, ValueError) as error:
                        record_invalid_applied(error)
                        continue
                    known.append(edge)
                elif operation.phase in {"pending", "unknown"}:
                    pending.append(edge)
            report = inspect_accepted_active_tranche_dag(
                trusted_accepted_source=plain(evidence), trusted_accepted_token=plain(token),
                frozen_create_receipts=frozen_raw, observed_cards=raw_cards,
                proven_applied_edges=tuple(known), pending_or_unknown_edges=tuple(pending),
                paused=False)
            result = dict(report)
            if invalid_applied:
                result["outcome"] = "conflict"
                result["conflicts"] = tuple(result.get("conflicts", ())) + tuple(invalid_applied)
            result["observation_uncertain"] = uncertain
            result["no_effect_authority"] = True
            return MappingProxyType(result)

    def prepare_accepted_dependency_link(self, plan_id: str, ticket_id: str,
                                         dependency_id: str, *,
                                         request_id: str | None = None,
                                         _already_locked: bool = False) -> Mapping[str, Any]:
        """Reserve one generalized v2 accepted-DAG link intent, without native effects.

        This is preparation only: no adapter mutation, attempt claim, acknowledgement,
        membership/budget update, release, or dispatch is reachable from this seam.
        """
        from .accepted_dependency_preparation import (
            prepare_accepted_dependency_link_intent,
        )
        from .accepted_dependency_links import _snapshot

        def plain(value):
            if isinstance(value, Mapping):
                return {key: plain(child) for key, child in value.items()}
            if isinstance(value, (tuple, list)):
                return [plain(child) for child in value]
            return value

        with (nullcontext() if _already_locked else self.lock):
            self._assert_lock()
            context = self._active_piece_dependency_persisted_context_locked(
                plan_id, ticket_id, dependency_id, request_id=request_id)
            fence = context["_persisted_fence"]
            receipts = plain(context["frozen_create_receipts"])
            frozen_raw = {}
            for receipt in receipts:
                readback = receipt["readback"]
                capture = readback.get("raw_capture_v1") if type(readback) is dict else None
                if (type(capture) is not dict or set(capture) != {"kind", "show", "runs"}
                        or capture.get("kind") != "hermes_kanban_raw_capture_v1"):
                    raise ValueError("frozen creation receipt lacks exact raw capture")
                frozen_raw[receipt["ticket_id"]] = {**plain(capture["show"]),
                    "runs": plain(capture["runs"]), "show_has_runs": "runs" in capture["show"],
                    "show_runs": plain(capture["show"].get("runs")) if "runs" in capture["show"] else None}
            frozen_by_task = {receipt["task_id"]: frozen_raw[receipt["ticket_id"]]
                              for receipt in receipts}
            if any(operation.effect == "link" and operation.phase == "unknown"
                   and operation.target.get("kind") == "accepted_active_tranche_native_link_v2"
                   and operation.target.get("source_task_id") == frozen_raw.get(dependency_id, {}).get("task", {}).get("id")
                   and operation.target.get("target_task_id") == frozen_raw.get(ticket_id, {}).get("task", {}).get("id")
                   for operation in fence["state_operations"]):
                raise ValueError("generalized link reconciliation is required before preparation retry")
            reader = getattr(self.board, "read_accepted_active_tranche_raw_cards", None)
            if not callable(reader):
                raise ValueError("production complete raw active-tranche reader unavailable")
            observed = _snapshot(reader(self.scope, tuple(item["task_id"] for item in receipts)))
            prior_v2 = [plain(operation.readback) for operation in fence["state_operations"]
                        if (operation.effect == "link" and operation.phase == "applied"
                            and operation.outcome in {"verified", "no-op"}
                            and isinstance(operation.readback, Mapping)
                            and operation.target.get("kind") == "accepted_active_tranche_native_link_v2")]
            prepared = prepare_accepted_dependency_link_intent(
                trusted_accepted_source=plain(fence["evidence"]),
                trusted_accepted_token=plain(fence["token"]),
                frozen_create_receipts=frozen_raw, observed_cards=observed,
                scope=plain(self.scope), target_ticket_id=ticket_id,
                dependency_ticket_id=dependency_id,
                prior_applied_receipts=prior_v2,
            )
            # Re-read all trusted persisted inputs after the external read and
            # before the sole permitted mutation (the pending reservation).
            self._active_piece_dependency_final_fence_locked(context)
            intent = OperationIntent(prepared["key"], self.scope, prepared["target"], "link",
                                     prepared["expected_observed_identity"], prepared["before_evidence"],
                                     None, None, prepared["retry"], "pending")
            reserved = self.store.reserve_operation(intent)
            if reserved.phase != "pending" or reserved.to_dict() != intent.to_dict():
                raise ValueError("existing generalized pending link intent conflicts with exact preparation")
            return MappingProxyType({"outcome": "pending", "operation_key": reserved.key,
                "effect": reserved.effect, "target": reserved.target, "actions_attempted": 0})

    def execute_accepted_dependency_link(self, plan_id: str, ticket_id: str, dependency_id: str, *, request_id: str | None = None) -> Mapping[str, Any]:
        """Execute/reconcile one prepared v2 DAG edge; unknown operations never resend."""
        from .accepted_dependency_links import validate_observed_multi_edge_transition

        def plain(value):
            if isinstance(value, Mapping): return {key: plain(child) for key, child in value.items()}
            if isinstance(value, (tuple, list)): return [plain(child) for child in value]
            return value

        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            existing = [item for item in state["operations"] if item.effect == "link"
                        and item.target.get("kind") == "accepted_active_tranche_native_link_v2"
                        and item.target.get("source_ticket_id") == dependency_id
                        and item.target.get("target_ticket_id") == ticket_id]
            if len(existing) > 1:
                raise ValueError("multiple generalized dependency operations match one declared edge")
            # A pending row is only a reservation, not authority to skip the
            # complete raw pre-barrier.  Applied and unknown rows are strictly
            # read-only recovery/replay paths and must not be reconstructed as a
            # new reservation.
            try:
                if existing and existing[0].phase == "pending":
                    from .accepted_dependency_preparation import prepare_accepted_dependency_link_intent
                    from .accepted_dependency_links import _snapshot
                    before = plain(existing[0].before_evidence)
                    identity = before["identity"]
                    reader = getattr(self.board, "read_accepted_active_tranche_raw_cards", None)
                    if not callable(reader):
                        raise ValueError("complete raw active-tranche reader unavailable")
                    task_ids = tuple(identity["frozen_create_receipts"][ticket]["task"]["id"]
                                     for ticket in sorted(identity["frozen_create_receipts"]))
                    rebuilt = prepare_accepted_dependency_link_intent(
                        trusted_accepted_source=identity["accepted_source"],
                        trusted_accepted_token=identity["accepted_token"],
                        frozen_create_receipts=identity["frozen_create_receipts"],
                        observed_cards=_snapshot(reader(self.scope, task_ids)),
                        scope=identity["scope"], target_ticket_id=ticket_id,
                        dependency_ticket_id=dependency_id,
                        prior_applied_receipts=before["prior_applied_receipts"],
                    )
                    if (rebuilt["key"] != existing[0].key
                            or plain(rebuilt["target"]) != plain(existing[0].target)
                            or plain(rebuilt["before_evidence"]) != plain(existing[0].before_evidence)):
                        raise ValueError("existing generalized pending link intent conflicts with exact preparation")
                    pending = {"operation_key": existing[0].key}
                elif existing:
                    pending = {"operation_key": existing[0].key}
                else:
                    pending = self.prepare_accepted_dependency_link(
                        plan_id, ticket_id, dependency_id, request_id=request_id,
                        _already_locked=True,
                    )
            except (KeyError, TypeError, ValueError) as error:
                return {"outcome": "conflict", "actions_attempted": 0,
                        "reason": f"accepted generalized-link pre-send barrier is unproven: {error}"}
            key = pending["operation_key"]
            operation = next(item for item in self.store.read_scope(self.scope)["operations"] if item.key == key)
            # Assigned in each recoverable branch below; initialization keeps
            # malformed future phases from inheriting a fresh clock value.
            command_started = command_ended = 0
            receipt_checkpoint = None
            sent_this_call = False
            if operation.phase == "pending":
                # The raw barrier above is external I/O.  Re-read both persisted
                # accepted authority and operator intent while still under this
                # mutation lock before recording a pre-send claim.  A late pause
                # or cancellation preserves the pending reservation unchanged.
                try:
                    accepted = self.store.read_accepted_plan(self.scope, plan_id)
                    if not isinstance(accepted, Mapping) or accepted.get("plan_id") != plan_id:
                        raise ValueError("accepted plan identity is unavailable")
                except (KeyError, TypeError, ValueError) as error:
                    return {"outcome": "conflict", "operation_key": key,
                            "actions_attempted": 0,
                            "reason": f"fresh accepted generalized-link authority is unproven: {error}"}
                active_intent = self.store.read_scope(self.scope)["operator_intent"]
                if active_intent is not None and active_intent.active:
                    return {"outcome": "held", "operation_key": key,
                            "actions_attempted": 0,
                            "reason": "operator pause or cancellation is active"}
                action = self._action_from_intent(operation)
                command_started = int(time.time())
                native_identity = {"action_key": key, "effect": "link",
                                   "source_task_id": action.target["source_task_id"],
                                   "target_task_id": action.target["target_task_id"]}
                # Persist the actual lower boundary and the exact command identity
                # before changing the claim or entering native I/O.  A crash in
                # either intervening interval remains recoverable without a resend.
                self.store.record_pre_send_attempt(self.scope, key,
                                                    native_operation_identity=native_identity,
                                                    command_started=command_started)
                self.store.begin_effect_attempt(self.scope, key)
                operation = next(item for item in self.store.read_scope(self.scope)["operations"] if item.key == key)
                attempted = 1
                sent_this_call = True
                try:
                    result = self.board.link(action, action.target["source_task_id"], action.target["target_task_id"])
                except Exception as error:
                    return {"outcome": "partial", "operation_key": key, "actions_attempted": attempted, "reason": f"native link outcome unknown: {error}"}
            elif operation.phase == "applied":
                attempted, result = 0, ActionResult(key, "verified", "read-only generalized-link reconciliation", None)
                stored_receipt = plain(operation.readback)
                stored_window = stored_receipt.get("time_window") if type(stored_receipt) is dict else None
                if (type(stored_window) is not dict or set(stored_window) != {"started", "ended"}
                        or type(stored_window["started"]) is not int or type(stored_window["ended"]) is not int):
                    return {"outcome": "conflict", "operation_key": key, "actions_attempted": 0,
                            "reason": "applied generalized-link receipt lacks exact command window"}
                command_started, command_ended = stored_window["started"], stored_window["ended"]
            elif operation.phase == "unknown":
                # An unknown claim never gets attribution merely because a later
                # native edge happens to look equivalent.  Only a receipt which
                # was durably checkpointed after the original adapter return and
                # full transition validation may be revalidated and acknowledged.
                receipt_checkpoint = self.store.read_validated_link_receipt_checkpoint(self.scope, key)
                if receipt_checkpoint is None:
                    return {"outcome": "partial", "operation_key": key, "actions_attempted": 0,
                            "reason": "unknown generalized link lacks a durable validated completion receipt; reconciliation will not resend or acknowledge"}
                attempt = self.store.read_pre_send_attempt(self.scope, key)
                expected_identity = {"action_key": key, "effect": "link",
                                     "source_task_id": operation.target.get("source_task_id"),
                                     "target_task_id": operation.target.get("target_task_id")}
                if attempt is None or attempt["native_operation_identity"] != expected_identity:
                    return {"outcome": "partial", "operation_key": key, "actions_attempted": 0,
                            "reason": "unknown generalized link lacks exact pre-send command-window evidence; reconciliation will not resend"}
                stored_receipt = plain(receipt_checkpoint["receipt"])
                stored_window = stored_receipt.get("time_window") if type(stored_receipt) is dict else None
                if (type(stored_window) is not dict or set(stored_window) != {"started", "ended"}
                        or type(stored_window["started"]) is not int or type(stored_window["ended"]) is not int):
                    return {"outcome": "partial", "operation_key": key, "actions_attempted": 0,
                            "reason": "unknown generalized link completion checkpoint lacks an exact command window"}
                command_started, command_ended = stored_window["started"], stored_window["ended"]
                if command_started != attempt["command_started"]:
                    return {"outcome": "partial", "operation_key": key, "actions_attempted": 0,
                            "reason": "unknown generalized link completion checkpoint conflicts with pre-send evidence"}
                attempted, result = 0, ActionResult(key, "verified", "read-only generalized-link reconciliation", None)
            else:
                raise ValueError("generalized link operation is not recoverable")
            if not isinstance(result, ActionResult) or result.action_key != key:
                return {"outcome": "partial", "operation_key": key, "actions_attempted": attempted, "reason": "link adapter result is malformed"}
            if operation.phase != "applied" and result.outcome not in {"verified", "no-op"}:
                return {"outcome": "partial", "operation_key": key, "actions_attempted": attempted, "reason": result.details}
            reader = getattr(self.board, "read_accepted_active_tranche_raw_cards", None)
            if not callable(reader):
                return {"outcome": "partial", "operation_key": key, "actions_attempted": attempted, "reason": "complete raw active-tranche reader unavailable"}
            before = plain(operation.before_evidence)
            identity = before["identity"]
            task_ids = tuple(identity["frozen_create_receipts"][ticket]["task"]["id"] for ticket in sorted(identity["frozen_create_receipts"]))
            observed = plain(reader(self.scope, task_ids))
            # A fresh replay read establishes only that the currently observed
            # link event is not from the future.  It must never replace either
            # immutable command boundary persisted by the original send.
            fresh_read_clock = int(time.time())
            if sent_this_call:
                command_ended = fresh_read_clock
            try:
                validate_observed_multi_edge_transition(
                    trusted_accepted_source=identity["accepted_source"], trusted_accepted_token=identity["accepted_token"],
                    frozen_create_receipts=identity["frozen_create_receipts"], authority=None,
                    prior_cards=before["current_raw_cards"], observed_cards=observed,
                    prior_applied_edges=before["current_applied_edges"],
                    edge=(identity["edge"]["source_ticket_id"], identity["edge"]["target_ticket_id"]),
                    command_window=(command_started, command_ended))
            except Exception as error:
                return {"outcome": "conflict" if operation.phase == "applied" else "partial", "operation_key": key,
                        "actions_attempted": attempted, "reason": f"exact generalized transition is unproven: {error}"}
            if operation.phase == "applied":
                target_task = cast(str, operation.target["target_task_id"])
                observed_cards = cast(Mapping[str, Mapping[str, Any]], observed)
                if observed_cards[target_task]["events"][-1]["created_at"] > fresh_read_clock:
                    return {"outcome": "conflict", "operation_key": key, "actions_attempted": 0,
                            "reason": "applied generalized-link event is newer than fresh observation clock"}
            receipt = {"kind": "accepted_active_tranche_native_link_readback_v2", "prior_cards": before["current_raw_cards"],
                       "observed_cards": observed, "prior_applied_edges": before["current_applied_edges"],
                       "edge": [identity["edge"]["source_ticket_id"], identity["edge"]["target_ticket_id"]],
                       "time_window": {"started": command_started, "ended": command_ended}}
            if operation.phase == "applied":
                if plain(operation.readback) != receipt:
                    return {"outcome": "conflict", "operation_key": key, "actions_attempted": 0, "reason": "applied v2 receipt differs from fresh raw evidence"}
                return {"outcome": "linked", "operation_key": key, "actions_attempted": 0}
            if operation.phase == "unknown" and not sent_this_call:
                assert receipt_checkpoint is not None
                if plain(receipt_checkpoint["receipt"]) != receipt:
                    return {"outcome": "partial", "operation_key": key, "actions_attempted": 0,
                            "reason": "fresh native readback differs from the durable validated completion receipt"}
            if sent_this_call:
                try:
                    self.store.record_validated_link_receipt_checkpoint(self.scope, key, receipt)
                except Exception as error:
                    return {"outcome": "partial", "operation_key": key, "actions_attempted": attempted,
                            "reason": f"validated generalized-link receipt checkpoint unavailable: {error}"}
            self.store.record_effect_observation(self.scope, key, outcome="verified", details="exact generalized DAG link readback", readback=receipt)
            self.store.ack_effect(self.scope, key, readback=receipt, outcome="verified")
            return {"outcome": "linked", "operation_key": key, "actions_attempted": attempted}

    def active_piece_dependency_authority(self, plan_id: str, ticket_id: str,
                                          dependency_id: str, *,
                                          request_id: str | None = None) -> Mapping[str, Any]:
        """Reconstruct a read-only authority for one declared first-edge link.

        This deliberately supports only a fully held, parentless tranche zero;
        no native link effect or post-link receipt is authorized here.
        """
        with self.lock:
            return self._active_piece_dependency_authority_locked(
                plan_id, ticket_id, dependency_id, request_id=request_id)

    def _active_piece_dependency_authority_locked(
            self, plan_id: str, ticket_id: str, dependency_id: str, *,
            request_id: str | None = None) -> Mapping[str, Any]:
        """Authorize only after a fresh native whole-tranche barrier succeeds."""
        context = self._active_piece_dependency_persisted_context_locked(
            plan_id, ticket_id, dependency_id, request_id=request_id)
        self._active_piece_dependency_native_barrier_locked(context)
        self._active_piece_dependency_final_fence_locked(context)
        from .planning_coordinator import _deep_freeze
        identity = context["authority_identity"]
        return _deep_freeze({**dict(identity), "operation_key": context["operation_key"],
            "scope": dict(self.scope), "frozen_create_receipts": context["frozen_create_receipts"],
            "read_only_first_edge_only": True})

    @staticmethod
    def _validate_first_link_event_lower_bound(
            before: Mapping[str, Any], *, current_trusted_clock_seconds: int) -> int:
        """Validate the immutable lower time bound for a pending first-link proof."""
        if type(before) is not dict:
            raise ValueError("first-link pending prebarrier evidence is malformed")
        if type(current_trusted_clock_seconds) is not int:
            raise ValueError("trusted first-link clock must be a plain integer")

        def observed_seconds(label: str) -> int:
            snapshot = before.get(label)
            observed_at = snapshot.get("observed_at") if isinstance(snapshot, Mapping) else None
            if type(observed_at) is not str:
                raise ValueError("immutable first-link snapshot observation time is malformed")
            try:
                parsed = datetime.fromisoformat(observed_at)
            except ValueError as exc:
                raise ValueError("immutable first-link snapshot observation time is malformed") from exc
            if (parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
                    or parsed.isoformat() != observed_at):
                raise ValueError("immutable first-link snapshot observation time is not canonical UTC")
            return math.floor(parsed.timestamp())

        def raw_seconds(label: str) -> list[int]:
            raw = before.get(label)
            if type(raw) is not dict:
                raise ValueError("first-link raw prebarrier is malformed")
            task, events = raw.get("task"), raw.get("events")
            if type(task) is not dict or type(events) is not list:
                raise ValueError("first-link raw prebarrier timestamp transport is malformed")
            timestamps: list[int] = []
            for record in (task, *events):
                if type(record) is not dict:
                    raise ValueError("first-link raw prebarrier record is malformed")
                if "created_at" in record:
                    value = record["created_at"]
                    if type(value) is not int:
                        raise ValueError("first-link native timestamp is not a plain integer")
                    timestamps.append(value)
            return timestamps

        lower = before.get("event_lower_bound_seconds")
        captured = before.get("captured_clock_seconds")
        if type(lower) is not int or type(captured) is not int:
            raise ValueError("first-link pending timestamp bounds must be plain integers")
        observations = (observed_seconds("source_snapshot"), observed_seconds("child_snapshot"))
        floor = max(*observations, *raw_seconds("source_raw"), *raw_seconds("child_raw"))
        if not max(observations) <= captured <= current_trusted_clock_seconds:
            raise ValueError("first-link captured clock is outside immutable observation bounds")
        if lower < max(floor, captured) or lower > current_trusted_clock_seconds:
            raise ValueError("first-link pending event lower bound is outside chronological range")
        return lower

    @staticmethod
    def _plain_link_value(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: Coordinator._plain_link_value(child) for key, child in value.items()}
        if isinstance(value, (tuple, list)):
            return [Coordinator._plain_link_value(child) for child in value]
        return value

    @staticmethod
    def _exact_inspection_show(card: Any) -> dict[str, Any]:
        """Restore the exact public show envelope from the reader transport."""
        if type(card) is not dict:
            raise ValueError("raw inspection card is malformed")
        has_metadata = "show_has_runs" in card or "show_runs" in card
        has_runs = card.get("show_has_runs", "runs" in card)
        if type(has_runs) is not bool:
            raise ValueError("raw inspection show-runs presence marker is malformed")
        if has_metadata and "show_runs" not in card:
            raise ValueError("raw inspection show-runs value is missing")
        show_runs = card.get("show_runs", card.get("runs") if has_runs else None)
        if not has_runs and show_runs is not None:
            raise ValueError("absent raw show.runs has a non-null transport value")
        show = {key: value for key, value in card.items()
                if key not in {"show_has_runs", "show_runs", "runs"}}
        if has_runs:
            show["runs"] = show_runs
        return show

    @staticmethod
    def _checked_link_snapshot(raw: Any, value: Any) -> dict[str, Any]:
        """Require the normalized snapshot to bind the exact raw public card fields."""
        if type(raw) is not dict or type(value) is not dict:
            raise ValueError("accepted-link snapshot transport is malformed")
        from .hermes_board import HermesBoardAdapter
        expected_fields = {"native_task", "parents", "runs", "comments", "events",
                           "attachments", "observed_at", "digest"}
        if set(value) != expected_fields or type(raw.get("task")) is not dict:
            raise ValueError("accepted-link normalized snapshot schema is malformed")
        try:
            BoardSnapshot.from_dict(value)
        except (TypeError, ValueError) as error:
            raise ValueError("accepted-link normalized snapshot schema is malformed") from error
        if (type(raw.get("parents")) is not list or type(raw.get("comments", [])) is not list
                or type(raw.get("events", [])) is not list or type(raw.get("attachments", [])) is not list
                or type(value.get("runs")) is not list or value["runs"] != []):
            raise ValueError("accepted-link normalized snapshot fields are malformed")
        normalized = {
            "native_task": raw["task"],
            "parents": [{"id": parent_id} for parent_id in raw["parents"]],
            "runs": value["runs"],
            "comments": raw.get("comments", []),
            "events": raw.get("events", []),
            "attachments": raw.get("attachments", []),
        }
        if any(type(parent_id) is not str for parent_id in raw["parents"]):
            raise ValueError("accepted-link raw parent identifiers are malformed")
        if (any(value.get(field) != normalized[field]
                for field in ("native_task", "parents", "runs", "comments", "events", "attachments"))
                or type(value.get("observed_at")) is not str
                or value.get("digest") != HermesBoardAdapter._digest(normalized)):
            raise ValueError("accepted-link normalized snapshot does not bind exact raw card evidence")
        return value

    @staticmethod
    def _checked_accepted_link_readback(
            operation: OperationIntent, value: Mapping[str, Any] | None, *,
            source_task_id: str, child_task_id: str, require_prebarrier: bool = False,
            expected_request_id: str | None = None) -> Mapping[str, Any]:
        """Canonicalize a two-card receipt and, when available, its raw barrier."""
        transport = OperationIntent(
            operation.key, operation.scope, operation.target, operation.effect,
            operation.expected_observed_identity, operation.before_evidence,
            operation.outcome, value, operation.retry, operation.phase,
        ).to_dict()["readback"]
        encoded = json.dumps(transport, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False)
        checked = json.loads(encoded)
        source = checked.get("source") if type(checked) is dict else None
        child = checked.get("child") if type(checked) is dict else None
        source_task = source.get("task") if type(source) is dict else None
        child_task = child.get("task") if type(child) is dict else None
        required_readback = {"kind", "source", "child", "source_snapshot", "child_snapshot",
                             "time_window", "transition"}
        if (type(checked) is not dict or set(checked) != required_readback
                or checked.get("kind") != "accepted_active_tranche_native_link_readback_v1"
                or type(source) is not dict or type(child) is not dict
                or type(source_task) is not dict or type(child_task) is not dict
                or source_task.get("id") != source_task_id
                or child_task.get("id") != child_task_id
                or source.get("children") != [child_task_id]
                or child.get("parents") != [source_task_id]):
            raise ValueError("accepted-link exact two-card readback is malformed")
        source_snapshot = Coordinator._checked_link_snapshot(source, checked["source_snapshot"])
        child_snapshot = Coordinator._checked_link_snapshot(child, checked["child_snapshot"])
        window = checked.get("time_window")
        if (type(window) is not dict or set(window) != {"started", "ended"}
                or type(window["started"]) is not int or window["started"] <= 0
                or type(window["ended"]) is not int or window["ended"] < window["started"]):
            raise ValueError("accepted-link receipt lacks exact time-window evidence")
        before = Coordinator._plain_link_value(operation.before_evidence)
        base_prebarrier = {"kind", "authority", "source_raw", "child_raw", "expected_source_raw",
                           "source_snapshot", "child_snapshot"}
        if (type(before) is not dict or not base_prebarrier <= set(before)
                or type(before.get("source_raw")) is not dict
                or type(before.get("child_raw")) is not dict
                or type(before.get("expected_source_raw")) is not dict
                or type(before.get("source_snapshot")) is not dict
                or type(before.get("child_snapshot")) is not dict):
            raise ValueError("accepted-link receipt lacks immutable raw prebarrier evidence")
        expected_source_raw = dict(before["source_raw"])
        expected_source_raw["children"] = [child_task_id]
        if before["expected_source_raw"] != expected_source_raw:
            raise ValueError("accepted-link immutable expected source transition is malformed")
        before_source_snapshot = Coordinator._checked_link_snapshot(
            before["source_raw"], before["source_snapshot"])
        before_child_snapshot = Coordinator._checked_link_snapshot(
            before["child_raw"], before["child_snapshot"])
        from .hermes_board import validate_accepted_first_link_transition
        transition = validate_accepted_first_link_transition(
            before["source_raw"], before["child_raw"], source, child,
            source_id=source_task_id, child_id=child_task_id,
            command_started_seconds=window["started"], command_ended_seconds=window["ended"],
        )
        expected_transition = {"source_id": transition.source_id, "child_id": transition.child_id,
                               "event": Coordinator._plain_link_value(transition.event)}
        if checked["transition"] != expected_transition:
            raise ValueError("accepted-link receipt transition summary differs from exact raw transition")
        if require_prebarrier:
            if before.get("kind") != "accepted_active_tranche_native_link_prebarrier_v2":
                raise ValueError("accepted-link applied receipt lacks supported v2 immutable prebarrier evidence")
            required_prebarrier = base_prebarrier | {
                "captured_clock_seconds", "event_lower_bound_seconds", "request_id"}
            if (set(before) != required_prebarrier
                    or type(before.get("authority")) is not dict
                    or before.get("request_id") != expected_request_id
                    or (before.get("request_id") is not None and type(before["request_id"]) is not str)
                    or type(before.get("captured_clock_seconds")) is not int
                    or before["captured_clock_seconds"] <= 0
                    or type(before.get("event_lower_bound_seconds")) is not int
                    or before["event_lower_bound_seconds"] <= 0
                    or before["event_lower_bound_seconds"] > before["captured_clock_seconds"]):
                raise ValueError("accepted-link v2 prebarrier evidence is malformed")
        elif before.get("kind") not in {
                "accepted_active_tranche_native_link_prebarrier_v1",
                "accepted_active_tranche_native_link_prebarrier_v2"}:
            raise ValueError("accepted-link pending receipt lacks a supported raw prebarrier")
        if before_source_snapshot is None or before_child_snapshot is None or source_snapshot is None or child_snapshot is None:
            raise ValueError("accepted-link snapshots are malformed")
        return checked

    def _accepted_dependency_link_description(self, scope: Mapping[str, str], operation_key: str) -> Mapping[str, Any]:
        """Expose one reserved first-link proof to the trusted native adapter.

        The returned wrapper is reconstructed from the durable accepted plan and
        immutable prebarrier.  It performs no board I/O; the adapter must still
        compare its own fresh raw reads before a mutation or reconciliation.
        """
        self._assert_lock()
        if validate_scope(scope) != self.scope or type(operation_key) is not str:
            raise ValueError("accepted first-link resolver scope or key mismatch")
        state = self.store.read_scope(self.scope)
        matches = [operation for operation in state["operations"] if operation.key == operation_key]
        if len(matches) != 1:
            raise ValueError("accepted first-link operation is not uniquely reserved")
        operation = matches[0]
        before = self._plain_link_value(operation.before_evidence)
        kind = before.get("kind") if type(before) is dict else None
        if (operation.effect != "link" or operation.phase not in {"pending", "unknown", "applied"}
                or type(before) is not dict
                or kind not in {"accepted_active_tranche_native_link_prebarrier_v1",
                                "accepted_active_tranche_native_link_prebarrier_v2"}):
            raise ValueError("accepted first-link operation is not a recoverable reserved effect")
        request_id = before.get("request_id") if kind == "accepted_active_tranche_native_link_prebarrier_v2" else None
        if request_id is not None and type(request_id) is not str:
            raise ValueError("accepted first-link immutable request ID is malformed")
        authority = before.get("authority")
        if type(authority) is not dict or type(authority.get("plan_id")) is not str:
            raise ValueError("accepted first-link immutable authority is malformed")
        source_ticket_id, target_ticket_id = authority.get("source_ticket_id"), authority.get("target_ticket_id")
        if type(source_ticket_id) is not str or type(target_ticket_id) is not str:
            raise ValueError("accepted first-link immutable ticket identities are malformed")
        context = self._active_piece_dependency_persisted_context_locked(
            authority["plan_id"], target_ticket_id, source_ticket_id, request_id=request_id)
        rebuilt = self._plain_link_value(context["authority_identity"])
        rebuilt["operation_key"] = context["operation_key"]
        rebuilt["read_only_first_edge_only"] = True
        child_receipts = [item for item in rebuilt["frozen_create_receipts"]
                          if item.get("task_id") == rebuilt["target_task_id"]]
        if len(child_receipts) != 1:
            raise ValueError("accepted first-link immutable child receipt is not unique")
        target = {"kind": "accepted_active_tranche_native_link_v1",
                  "source_task_id": rebuilt["source_task_id"],
                  "child_task_id": rebuilt["target_task_id"], "operation_key": operation_key}
        required = {"kind", "authority", "source_raw", "child_raw", "expected_source_raw",
                    "source_snapshot", "child_snapshot", "captured_clock_seconds",
                    "event_lower_bound_seconds"}
        if kind == "accepted_active_tranche_native_link_prebarrier_v2":
            required.add("request_id")
        if (set(before) != required or before["authority"] != rebuilt
                or self._plain_link_value(operation.target) != target
                or operation.expected_observed_identity != child_receipts[0]["digest"]
                or before["source_raw"].get("task", {}).get("id") != target["source_task_id"]
                or before["child_raw"].get("task", {}).get("id") != target["child_task_id"]):
            raise ValueError("accepted first-link immutable proof differs from current accepted evidence")
        self._validate_first_link_event_lower_bound(before, current_trusted_clock_seconds=int(time.time()))
        return {"target": target, "authority": rebuilt, "source_id": target["source_task_id"],
                "child_id": target["child_task_id"], "before_source": before["source_raw"],
                "before_child": before["child_raw"], "expected_source": before["expected_source_raw"],
                "source_snapshot": before["source_snapshot"], "child_snapshot": before["child_snapshot"],
                "command_started_seconds": before["event_lower_bound_seconds"]}

    def _claim_accepted_first_link_attempt(self, scope: Mapping[str, str], operation_key: str) -> Mapping[str, Any]:
        """Atomically fence the single native link attempt before adapter I/O."""
        self._assert_lock()
        # Reconstruct first: a stale/changed accepted source is not a license to
        # turn a pending intent into an ambiguous attempted effect.
        self._accepted_dependency_link_description(scope, operation_key)
        state = self.store.read_scope(self.scope)
        pause = state["operator_intent"]
        if pause is not None and pause.active:
            raise ValueError("accepted first-link attempt is fenced by pause/cancellation")
        operation = next((item for item in state["operations"] if item.key == operation_key), None)
        if operation is None or operation.phase != "pending":
            raise ValueError("accepted first-link attempt requires pending operation")
        self.store.begin_effect_attempt(self.scope, operation_key)
        claimed = next(item for item in self.store.read_scope(self.scope)["operations"] if item.key == operation_key)
        if claimed.phase != "unknown":
            raise ValueError("accepted first-link durable attempt claim was not recorded")
        return {"before_phase": "pending", "operation": claimed.to_dict()}

    def execute_accepted_first_link(self, plan_id: str, ticket_id: str, dependency_id: str, *,
                                    request_id: str | None = None) -> Mapping[str, Any]:
        """Execute or read-only reconcile exactly one accepted held first edge.

        No caller of this narrow M4 seam can release, claim, dispatch, create a
        successor, alter membership, or accept a tranche.  An ambiguous attempt
        is reconciled only through adapter readback and is never resent.
        """
        with self.lock:
            self._assert_lock()
            context = self._active_piece_dependency_persisted_context_locked(
                plan_id, ticket_id, dependency_id, request_id=request_id)
            key = context["operation_key"]
            state = self.store.read_scope(self.scope)
            existing = next((item for item in state["operations"] if item.key == key), None)
            if existing is None:
                operation = self._prepare_accepted_first_link_operation_locked(
                    plan_id, ticket_id, dependency_id, request_id=request_id)
            elif existing.phase == "pending":
                operation = self._prepare_accepted_first_link_operation_locked(
                    plan_id, ticket_id, dependency_id, request_id=request_id)
            elif existing.phase in {"unknown", "applied"}:
                operation = existing
            else:
                raise ValueError("accepted first-link operation is not recoverable")
            action = self._action_from_intent(operation)
            expected_target = {"kind": "accepted_active_tranche_native_link_v1",
                               "source_task_id": context["authority_identity"]["source_task_id"],
                               "child_task_id": context["authority_identity"]["target_task_id"],
                               "operation_key": key}
            if operation.phase == "applied" and self._plain_link_value(operation.target) != expected_target:
                return {"outcome": "conflict", "reason": "applied accepted-link receipt target differs from reconstructed authority",
                        "operation_key": key, "actions_attempted": 0}
            if action.effect != "link":
                raise ValueError("accepted first-link operation effect mismatch")
            attempted = 0
            if operation.phase == "pending":
                attempted = 1
                try:
                    result = self.board.link(action, action.target["source_task_id"], action.target["child_task_id"])
                except Exception as error:
                    return {"outcome": "partial", "reason": f"accepted link outcome unknown: {error}",
                            "operation_key": key, "actions_attempted": attempted}
            else:
                verifier = getattr(self.board, "verify_effect", None)
                if not callable(verifier):
                    return {"outcome": "partial", "reason": "accepted-link verifier unavailable",
                            "operation_key": key, "actions_attempted": 0}
                result = verifier(action)
            if not isinstance(result, ActionResult) or result.action_key != key:
                return {"outcome": "partial", "reason": "accepted-link adapter result is malformed",
                        "operation_key": key, "actions_attempted": attempted}
            adapter_failure = {"outcome": "partial",
                               "reason": f"accepted-link adapter {result.outcome}: {result.details}",
                               "operation_key": key, "actions_attempted": attempted}
            if operation.phase != "applied" and result.outcome not in {"verified", "no-op"}:
                # Rejected/ambiguous adapter results carry no success-shaped
                # two-card receipt. Preserve the adapter's diagnosis and leave
                # the durable attempt unknown for read-only reconciliation.
                return adapter_failure
            try:
                if operation.phase == "applied":
                    # An applied acknowledgement is immutable.  A production
                    # readback has fresh observation timestamps/time windows, so
                    # validate its current two-card state independently without
                    # refreshing the observation or acknowledging again.
                    stored = self._checked_accepted_link_readback(
                        operation, operation.readback,
                        source_task_id=action.target["source_task_id"],
                        child_task_id=action.target["child_task_id"], require_prebarrier=True,
                        expected_request_id=request_id,
                    )
                    if operation.outcome not in {"verified", "no-op"} or stored is None:
                        raise ValueError("applied accepted-link acknowledgement is not exact")
                    if result.outcome not in {"verified", "no-op"}:
                        return {"outcome": "conflict", "reason": f"applied accepted-link receipt is not proven: {result.outcome}: {result.details}",
                                "operation_key": key, "actions_attempted": 0}
                    fresh = self._checked_accepted_link_readback(
                        operation, result.readback,
                        source_task_id=action.target["source_task_id"],
                        child_task_id=action.target["child_task_id"], require_prebarrier=True,
                        expected_request_id=request_id,
                    )
                    # Observation timestamps may refresh, but the two raw cards
                    # are immutable acknowledgement evidence and must still be
                    # byte-for-byte equal to the live adapter readback.
                    if stored["source"] != fresh["source"] or stored["child"] != fresh["child"]:
                        raise ValueError("applied accepted-link receipt differs from fresh exact two-card readback")
                    return {"outcome": "linked", "operation_key": key, "actions_attempted": 0}
                readback = self._checked_accepted_link_readback(
                    operation, result.readback,
                    source_task_id=action.target["source_task_id"],
                    child_task_id=action.target["child_task_id"],
                ) if action.effect == "link" else self._portable_readback(result.readback)
                self.store.record_effect_observation(self.scope, key, outcome=result.outcome,
                                                     details=result.details, readback=readback)
            except Exception as error:
                outcome = "conflict" if operation.phase == "applied" else "partial"
                return {"outcome": outcome, "reason": f"accepted-link observation unavailable: {error}",
                        "operation_key": key, "actions_attempted": attempted}
            if result.outcome not in {"verified", "no-op"} or readback is None:
                return {"outcome": "partial", "reason": result.details, "operation_key": key,
                        "actions_attempted": attempted}
            try:
                self.store.ack_effect(self.scope, key, readback=readback, outcome=result.outcome)
            except Exception as error:
                return {"outcome": "partial", "reason": f"accepted-link acknowledgement unavailable: {error}",
                        "operation_key": key, "actions_attempted": attempted}
            return {"outcome": "linked", "operation_key": key, "actions_attempted": attempted}

    def _prepare_accepted_first_link_operation_locked(
            self, plan_id: str, ticket_id: str, dependency_id: str, *,
            request_id: str | None = None) -> OperationIntent:
        """Reserve a pending first-link proof only; this never claims or sends a link.

        The durable reservation records the exact raw empty-edge barrier for a
        deliberately deferred future effect method.  Reservation is not effect
        authorization and unknown/applied records are intentionally refused.
        """
        self._assert_lock()

        def plain(value):
            if isinstance(value, Mapping):
                return {key: plain(child) for key, child in value.items()}
            if isinstance(value, (tuple, list)):
                return [plain(child) for child in value]
            return value

        def signature(state):
            return (tuple(state["members"]), tuple(state["operations"]), state["operator_intent"])

        def creation_action(operation):
            action = self._action_from_intent(operation)
            target = dict(action.target)
            target.pop("plan_id", None)
            return Action(action.key, action.scope, target, action.effect,
                          action.expected_observed_identity)

        def validate_raw(authority, action, raw):
            expected_keys = {"source_raw", "child_raw"}
            if type(raw) is not dict or set(raw) != expected_keys:
                raise ValueError("accepted first-link raw prebarrier transport malformed")
            snapshotter = getattr(self.board, "_snapshot_from_raw", None)
            if not callable(snapshotter):
                raise ValueError("production raw prebarrier snapshot adapter unavailable")
            receipt_list = tuple(authority["frozen_create_receipts"])
            receipts = {item["task_id"]: item for item in receipt_list}
            source_id, child_id = authority["source_task_id"], authority["target_task_id"]
            if len(receipts) != len(receipt_list):
                raise ValueError("frozen creation receipt task identities are not unique")
            snapshots = {}
            for label, task_id in (("source_raw", source_id), ("child_raw", child_id)):
                snapshot = snapshotter(raw[label], [])
                frozen = self._snapshot_from_readback(receipts[task_id]["readback"])
                if not isinstance(snapshot, BoardSnapshot) or frozen is None:
                    raise ValueError("raw prebarrier snapshot is malformed")
                current, original = snapshot.to_dict(), frozen.to_dict()
                current.pop("observed_at", None); original.pop("observed_at", None)
                if (current != original or snapshot.digest != receipts[task_id]["digest"]
                        or frozen.digest != receipts[task_id]["digest"]):
                    raise ValueError("raw prebarrier differs from frozen creation receipt")
                snapshots[label] = plain(frozen.to_dict())
            source_raw_plain = plain(raw["source_raw"])
            if type(source_raw_plain) is not dict:
                raise ValueError("accepted first-link raw source transport malformed")
            expected_source: dict[str, Any] = dict(source_raw_plain)
            expected_source["children"] = [child_id]
            return {"kind": "accepted_active_tranche_native_link_prebarrier_v1",
                    "authority": plain(authority), "source_raw": plain(raw["source_raw"]),
                    "child_raw": plain(raw["child_raw"]), "expected_source_raw": expected_source,
                    "source_snapshot": snapshots["source_raw"],
                    "child_snapshot": snapshots["child_raw"]}

        authority = self._active_piece_dependency_authority_locked(
            plan_id, ticket_id, dependency_id, request_id=request_id)
        authority_plain = plain(authority)
        state_first = self.store.read_scope(self.scope)
        operations = {op.key: op for op in state_first["operations"]}
        validator = getattr(self.board, "validate_accepted_piece_frozen_receipt", None)
        if not callable(validator):
            raise ValueError("production frozen creation receipt validator unavailable")
        for receipt in authority["frozen_create_receipts"]:
            operation = operations.get(receipt["operation_key"])
            if operation is None:
                raise ValueError("frozen creation receipt lacks durable operation")
            frozen_readback = plain(receipt["readback"])
            if type(frozen_readback) is not dict:
                raise ValueError("frozen creation readback is malformed")
            frozen_snapshot = self._snapshot_from_readback(frozen_readback)
            if (frozen_snapshot is None
                    or not self._accepted_piece_raw_capture_matches(frozen_readback, frozen_snapshot)):
                raise ValueError("frozen creation receipt lacks an exact immutable raw creation baseline")
            validator(creation_action(operation), frozen_readback, receipt["task_id"])

        key = authority["operation_key"]
        source_id, child_id = authority["source_task_id"], authority["target_task_id"]
        target = {"kind": "accepted_active_tranche_native_link_v1", "source_task_id": source_id,
                  "child_task_id": child_id, "operation_key": key}
        child_receipt = next(item for item in authority["frozen_create_receipts"]
                             if item["task_id"] == child_id)
        action = Action(key, self.scope, target, "link", child_receipt["digest"])
        existing = next((op for op in state_first["operations"] if op.key == key), None)
        if existing is not None:
            if existing.phase != "pending":
                raise ValueError("existing first-link operation is not safely pending; recovery is deferred")
            before = plain(existing.before_evidence)
            if (existing.effect != "link" or dict(existing.scope) != dict(self.scope)
                    or plain(existing.target) != target
                    or existing.expected_observed_identity != action.expected_observed_identity
                    or plain(existing.retry) != {"reconcile_only": True}
                    or existing.outcome is not None or existing.readback is not None
                    or type(before) is not dict
                    or before.get("kind") not in {"accepted_active_tranche_native_link_prebarrier_v1",
                                                   "accepted_active_tranche_native_link_prebarrier_v2"}
                    or (before.get("kind") == "accepted_active_tranche_native_link_prebarrier_v2"
                        and before.get("request_id") != request_id)
                    or before.get("authority") != authority_plain
                    or set(before) != ({"kind", "authority", "source_raw", "child_raw", "expected_source_raw",
                                        "source_snapshot", "child_snapshot", "captured_clock_seconds",
                                        "event_lower_bound_seconds", "request_id"}
                                       if before.get("kind") == "accepted_active_tranche_native_link_prebarrier_v2"
                                       else {"kind", "authority", "source_raw", "child_raw", "expected_source_raw",
                                             "source_snapshot", "child_snapshot", "captured_clock_seconds",
                                             "event_lower_bound_seconds"})):
                raise ValueError("existing first-link operation cannot be safely validated")
            self._validate_first_link_event_lower_bound(
                before, current_trusted_clock_seconds=int(time.time()))
            rebuilt = validate_raw(authority, action, {"source_raw": before["source_raw"], "child_raw": before["child_raw"]})
            if any(before[field] != rebuilt[field] for field in
                   ("authority", "source_raw", "child_raw", "expected_source_raw",
                    "source_snapshot", "child_snapshot")):
                raise ValueError("existing first-link prebarrier evidence is not canonical")
            reader = getattr(self.board, "read_accepted_first_link_prebarrier", None)
            if not callable(reader):
                raise ValueError("production accepted first-link raw prebarrier unavailable")
            fresh_raw = reader(action, source_id, child_id)
            fresh_before = validate_raw(authority, action, fresh_raw)
            if (fresh_before["source_raw"] != before["source_raw"]
                    or fresh_before["child_raw"] != before["child_raw"]):
                raise ValueError("existing first-link raw prebarrier differs from fresh native evidence")
            authority_after = self._active_piece_dependency_authority_locked(
                plan_id, ticket_id, dependency_id, request_id=request_id)
            latest = self.store.read_scope(self.scope)
            if plain(authority_after) != authority_plain or signature(latest) != signature(state_first):
                raise ValueError("accepted first-link authority or store state changed during pending replay")
            # The immutable timestamp is intentionally not regenerated on replay.
            return existing

        reader = getattr(self.board, "read_accepted_first_link_prebarrier", None)
        if not callable(reader):
            raise ValueError("production accepted first-link raw prebarrier unavailable")
        raw = reader(action, source_id, child_id)
        before = validate_raw(authority, action, raw)
        captured_clock_seconds = int(time.time())
        before = {**before, "kind": "accepted_active_tranche_native_link_prebarrier_v2",
                  "request_id": request_id, "captured_clock_seconds": captured_clock_seconds,
                  "event_lower_bound_seconds": captured_clock_seconds}
        self._validate_first_link_event_lower_bound(
            before, current_trusted_clock_seconds=captured_clock_seconds)
        authority_after = self._active_piece_dependency_authority_locked(
            plan_id, ticket_id, dependency_id, request_id=request_id)
        latest = self.store.read_scope(self.scope)
        if plain(authority_after) != authority_plain or signature(latest) != signature(state_first):
            raise ValueError("accepted first-link authority or store state changed during raw prebarrier")
        intent = OperationIntent(key, self.scope, target, "link", action.expected_observed_identity,
                                 before, None, None, {"reconcile_only": True}, "pending")
        reserved = self.store.reserve_operation(intent)
        if reserved.phase != "pending" or reserved.to_dict() != intent.to_dict():
            raise ValueError("first-link reservation did not preserve exact pending prebarrier")
        return reserved

    def _active_piece_dependency_persisted_context_locked(
            self, plan_id: str, ticket_id: str | None, dependency_id: str | None, *,
            request_id: str | None = None) -> Mapping[str, Any]:
        """NON-AUTHORIZING reconstruction of persisted facts; lock required.

        This does not read native cards, verify a native effect, or certify a
        stored acknowledgement as historical native truth.  A later native
        barrier remains mandatory before the authority-shaped identity can be
        returned by the public helper.
        """
        from .contracts import ManagedMember
        from .planning_coordinator import (ActiveTrancheRoute, request_from_payload,
            request_payload, first_active_tranche_materialization, _canonical_digest,
            _deep_freeze)
        self._assert_lock()
        values = (plan_id,)
        if any(type(value) is not str or not value.strip() or len(value) > 256 for value in values):
            raise ValueError("bounded plan, target, and dependency IDs are required")
        if (ticket_id is None) != (dependency_id is None):
            raise ValueError("inspection context requires both edge identifiers or neither")
        if ticket_id is not None and (type(ticket_id) is not str or not ticket_id.strip() or len(ticket_id) > 256
                                      or type(dependency_id) is not str or not dependency_id.strip() or len(dependency_id) > 256):
            raise ValueError("bounded plan, target, and dependency IDs are required")
        if request_id is not None and (type(request_id) is not str or not request_id.strip() or len(request_id) > 256):
            raise ValueError("request_id must be a bounded explicit string")
        implementation, _ = self._local_review_roles()
        workspace = self.planning_workspace
        if (type(workspace) is not str or not os.path.isabs(workspace)
                or os.path.realpath(workspace) != workspace or not os.path.isdir(workspace)
                or os.path.islink(workspace)):
            raise ValueError("trusted persistent planning workspace must be canonical and existing")
        state = self.store.read_scope(self.scope)
        pause = state["operator_intent"]
        if pause is not None and pause.active:
            raise ValueError("dependency authority is fenced by pause/cancellation")
        token = self.store.read_accepted_plan(self.scope, plan_id)
        registration = self.store.read_planning_request(self.scope, request_id=request_id)
        evidence = self.store.read_plan(self.scope, plan_id)
        if self.planning_observer is None:
            raise ValueError("trusted planning observer is required")
        observed = self.planning_observer(dict(self.scope))
        if type(observed) is not dict or set(observed) != {"request"}:
            raise ValueError("trusted planning observer returned malformed observation")
        current_request = request_from_payload(observed["request"])
        registered_request = request_from_payload(registration["request"])
        if (request_payload(current_request) != request_payload(registered_request)
                or current_request.identity != token["request_identity"]):
            raise ValueError("current request differs from accepted source")
        route = token["route"]
        if route != {"implementation_profile": implementation, "workspace": workspace}:
            raise ValueError("accepted route differs from configured role/workspace")
        material = first_active_tranche_materialization(
            evidence, ActiveTrancheRoute(implementation, workspace))
        expected_associations = {item.association for item in material.targets}
        # The active materialization is the only accepted implementation-member
        # namespace at this stage. A generation-zero managed member with the
        # canonical active-piece association prefix but a different canonical
        # association belongs to a later/foreign tranche and must not be silently
        # ignored by a whole-tranche proof.
        unexpected_members = [member for member in state["members"]
                              if member.role == "implementation" and member.generation == 0
                              and type(member.work_association) is str
                              and member.work_association.startswith("active-tranche-piece:")
                              and member.work_association not in expected_associations]
        if unexpected_members:
            raise ValueError("future managed member association is outside accepted active materialization")
        targets = {target.ticket_id: target for target in material.targets}
        target = source = None
        if ticket_id is not None:
            target, source = targets.get(ticket_id), targets.get(dependency_id)
            if target is None or source is None or dependency_id == ticket_id:
                raise ValueError("dependency edge must name distinct tranche-zero tickets")
            if dependency_id not in target.declared_dependencies:
                raise ValueError("dependency is not declared by the target ticket")
            if target.tranche_ordinal != 0 or source.tranche_ordinal != 0:
                raise ValueError("only tranche-zero dependencies are supported")
        roots = [m for m in state["members"] if m.role == "root"]
        if len(roots) != 1 or roots[0].task_id != self.scope["anchor_task_id"]:
            raise ValueError("exactly one scope root member required")
        receipts = []
        for item in material.targets:
            matches = [op for op in state["operations"] if op.key == item.operation_key]
            members = [m for m in state["members"] if m.work_association == item.association]
            if (len(matches) != 1 or len(members) != 1):
                raise ValueError("every tranche-zero piece needs one exact create receipt and member")
            op, member = matches[0], members[0]
            description = self._accepted_piece_description(self.scope, item.operation_key)
            canonical_target = {**dict(description.target), "task_id": self.scope["anchor_task_id"],
                "anchor_task_id": self.scope["anchor_task_id"], "native_parent": False,
                "native_deps": [], "plan_id": plan_id, "ticket_id": item.ticket_id,
                "accepted_token_key": description.target["accepted_token_key"]}
            def plain(value):
                if isinstance(value, Mapping):
                    return {key: plain(child) for key, child in value.items()}
                if isinstance(value, (tuple, list)):
                    return [plain(child) for child in value]
                return value
            if (op.phase != "applied" or op.effect != "create_held"
                    or op.outcome not in {"verified", "no-op"}
                    or dict(op.scope) != dict(self.scope)
                    or plain(op.target) != plain(canonical_target)
                    or op.key != item.operation_key
                    or member != ManagedMember(self.scope["board_id"], self.scope["anchor_task_id"],
                        str(member.task_id), "implementation", 0, (), item.association)):
                raise ValueError("piece intent, receipt, or generation-zero membership is not canonical")
            proof = self._snapshot_from_readback(op.readback)
            frozen_readback = plain(op.readback)
            if proof is None or type(frozen_readback) is not dict:
                raise ValueError("immutable create receipt is malformed")
            route_check = getattr(self.board, "_workspace_routing", None)
            route_state = (route_check(proof.native_task, f"dir:{workspace}") if callable(route_check)
                           else (None if proof.native_task.get("workspace") == f"dir:{workspace}" else "unsupported"))
            if (proof.native_task.get("id") != member.task_id
                    or proof.native_task.get("status") != "blocked" or proof.parents or proof.runs
                    or proof.native_task.get("assignee") != implementation or route_state is not None):
                raise ValueError("immutable create receipt is not an exact held card")
            receipts.append({"ticket_id": item.ticket_id, "task_id": member.task_id,
                "association": item.association, "operation_key": op.key,
                "digest": proof.digest, "readback": frozen_readback})

        # A plan-scoped create token outside the accepted tranche is future work,
        # not inspection input.  Refuse it before any native read.
        expected_associations = {item.association for item in material.targets}
        if any(op.effect == "create_held" and self._plain_link_value(op.target).get("plan_id") == plan_id
               and self._plain_link_value(op.target).get("association") not in expected_associations
               for op in state["operations"]):
            raise ValueError("accepted plan has unexpected future create operation")
        if ticket_id is None:
            return _deep_freeze({"kind": "accepted_active_tranche_inspection_context_v1",
                "frozen_create_receipts": receipts,
                "_persisted_fence": {"state_members": tuple(state["members"]),
                    "state_operations": tuple(state["operations"]), "state_pause": pause,
                    "token": token, "evidence": evidence,
                    "current_request": request_payload(current_request), "request_id": request_id,
                    "plan_id": plan_id, "implementation": implementation, "workspace": workspace}})

        identity = {"kind": "accepted_active_tranche_native_link_v1",
            "acceptance_identity": token["acceptance_identity"], "plan_id": plan_id,
            "request_identity": token["request_identity"], "scope": dict(self.scope),
            "route": dict(token["route"]), "root_task_id": roots[0].task_id,
            "tranche_ordinal": 0, "tranche_id": material.active_tranche_id,
            "source_ticket_id": dependency_id, "target_ticket_id": ticket_id,
            "source_association": source.association, "target_association": target.association,
            "source_task_id": next(x["task_id"] for x in receipts if x["ticket_id"] == dependency_id),
            "target_task_id": next(x["task_id"] for x in receipts if x["ticket_id"] == ticket_id),
            "frozen_create_receipts": receipts}
        key = "native-link:" + _canonical_digest(identity)
        return _deep_freeze({"kind": "accepted_first_link_persisted_context_v1",
            "requires_native_receipt_validation": True, "authority_identity": identity,
            "operation_key": key, "frozen_create_receipts": receipts,
            "_persisted_fence": {"state_members": tuple(state["members"]),
                "state_operations": tuple(state["operations"]),
                "state_pause": pause, "token": token, "evidence": evidence,
                "current_request": request_payload(current_request), "request_id": request_id,
                "plan_id": plan_id, "implementation": implementation, "workspace": workspace}})

    def _active_piece_dependency_native_barrier_locked(self, context: Mapping[str, Any]) -> None:
        """Require fresh reads and exact native receipt verification for all pieces."""
        self._assert_lock()
        fence = context["_persisted_fence"]
        operations = {op.key: op for op in fence["state_operations"]}
        for receipt in context["frozen_create_receipts"]:
            proof = self._snapshot_from_readback(receipt["readback"])
            fresh = self.board.read_task(receipt["task_id"])
            if not isinstance(fresh, BoardSnapshot):
                raise ValueError("fresh native card differs from immutable create receipt")
            fresh_content, proof_content = fresh.to_dict(), proof.to_dict()
            fresh_content.pop("observed_at", None)
            proof_content.pop("observed_at", None)
            if fresh_content != proof_content:
                raise ValueError("fresh native card differs from immutable create receipt")
            verifier = getattr(self.board, "verify_effect", None)
            if not callable(verifier):
                raise ValueError("read-only exact creation verifier unavailable")
            op = operations[receipt["operation_key"]]
            action = self._action_from_intent(op)
            native_target = {key: value for key, value in action.target.items() if key != "plan_id"}
            verified = verifier(Action(action.key, action.scope, native_target, action.effect,
                                       action.expected_observed_identity))
            verified_snapshot = self._snapshot_from_readback(verified.readback) if isinstance(verified, ActionResult) else None
            verified_content = None if verified_snapshot is None else verified_snapshot.to_dict()
            proof_content = proof.to_dict()
            if verified_content is not None:
                verified_content.pop("observed_at", None)
            proof_content.pop("observed_at", None)
            if (not isinstance(verified, ActionResult) or verified.outcome not in {"verified", "no-op"}
                    or verified_content != proof_content or verified.action_key != op.key):
                # Keep the fence fail-closed, but retain bounded result metadata so
                # a production-only verifier rejection is distinguishable from a
                # receipt-content drift without exposing raw native card data.
                def bounded_detail(value: Any) -> str:
                    if type(value) is not str:
                        return f"<{type(value).__name__}>"
                    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
                    return escaped[:512] + ("…" if len(escaped) > 512 else "")

                def content_digest(value: Any) -> str:
                    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=True, allow_nan=False).encode("utf-8")
                    return hashlib.sha256(encoded).hexdigest()

                if verified_content is None:
                    content_state = "readback=malformed"
                elif verified_content == proof_content:
                    content_state = "readback=exact"
                else:
                    content_state = ("readback=drift"
                                     f" expected_sha256={content_digest(proof_content)}"
                                     f" actual_sha256={content_digest(verified_content)}")
                outcome = verified.outcome if isinstance(verified, ActionResult) else f"<{type(verified).__name__}>"
                details = bounded_detail(verified.details) if isinstance(verified, ActionResult) else "<no-action-result>"
                key = verified.action_key if isinstance(verified, ActionResult) else None
                raise ValueError("read-only verifier does not prove the immutable create receipt"
                                 f"; outcome={outcome}; details={details}; {content_state};"
                                 f" action_key_matches={key == op.key}")

    def _active_piece_dependency_final_fence_locked(self, context: Mapping[str, Any]) -> None:
        """Recheck persisted source and observer facts after the full native barrier."""
        self._assert_lock()
        from .planning_coordinator import _deep_freeze, request_from_payload, request_payload
        fence = context["_persisted_fence"]
        latest = self.store.read_scope(self.scope)
        latest_pause = latest["operator_intent"]
        if latest_pause is not None and latest_pause.active:
            raise ValueError("dependency authority fenced by changed pause/cancellation")
        if (tuple(latest["members"]) != fence["state_members"]
                or tuple(latest["operations"]) != fence["state_operations"]
                or latest["operator_intent"] != fence["state_pause"]):
            raise ValueError("authority store changed during read-only proof")
        latest_token = self.store.read_accepted_plan(self.scope, fence["plan_id"])
        latest_evidence = self.store.read_plan(self.scope, fence["plan_id"])
        latest_registration = self.store.read_planning_request(self.scope, request_id=fence["request_id"])
        latest_observed = self.planning_observer(dict(self.scope))
        if (_deep_freeze(latest_token) != fence["token"]
                or _deep_freeze(latest_evidence) != fence["evidence"]
                or _deep_freeze(request_payload(request_from_payload(latest_registration["request"]))) != fence["current_request"]
                or type(latest_observed) is not dict or set(latest_observed) != {"request"}
                or _deep_freeze(request_payload(request_from_payload(latest_observed["request"]))) != fence["current_request"]
                or fence["token"]["route"] != {"implementation_profile": fence["implementation"],
                    "workspace": fence["workspace"]}):
            raise ValueError("accepted source, request, or route changed during read-only proof")


    def bootstrap_planning_request(self, request_factory: Callable[[], Any], *,
                                   request_id: str | None = None) -> Mapping[str, Any]:
        """Record the operator's first request inside the scoped mutation fence.

        The factory performs composition-owned Git observation only after the
        active pause/cancellation check.  Store-level atomicity remains required
        because independent processes can have distinct coordinator locks.
        """
        if not callable(request_factory):
            raise ValueError("bootstrap request factory must be callable")
        if request_id is not None and (type(request_id) is not str or not request_id.strip() or len(request_id) > 256):
            raise ValueError("request_id must be a non-empty string of at most 256 characters")
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            pause = state["operator_intent"]
            if pause is not None and pause.active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active", "actions_attempted": 0}
            request = request_factory()
            target = self.store.bootstrap_planning_request(self.scope, request, request_id=request_id)
            return {"outcome": "recorded", **target}

    def prepare_planner(self, *, request_id: str | None = None,
                        planning_profile: str | None = None) -> Mapping[str, Any]:
        """Create one durable, blocked planner card. This never dispatches work.

        Paid-capacity eligibility is advisory only: held cards consume workflow
        repairs, not paid capacity. A later release/admission slice must reserve
        paid capacity atomically before any future start.
        """
        from .contracts import ManagedMember
        from .decomposition_planner import request_payload
        from .planning_coordinator import request_from_payload
        profile = self._planning_roles()
        if planning_profile is not None and planning_profile != profile:
            raise ValueError("requested planner profile does not match configured planner role")
        workspace = self.planning_workspace
        if (not isinstance(workspace, str) or not os.path.isabs(workspace)
                or os.path.realpath(workspace) != workspace or not os.path.isdir(workspace)
                or os.path.islink(workspace)):
            raise ValueError("trusted persistent planning workspace must be an existing canonical absolute directory")
        if self.planning_observer is None:
            raise ValueError("trusted planning observer is not configured")
        policy = self.budget_policy
        if not isinstance(policy, BudgetPolicy):
            raise ValueError("explicit finite planning budgets are required")
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            pause = state["operator_intent"]
            if pause is not None and pause.active:
                raise ValueError("planner creation is fenced by active pause/cancellation")
            observed = self.planning_observer(dict(self.scope))
            if not isinstance(observed, Mapping) or set(observed) != {"request"}:
                raise ValueError("trusted planning observer returned malformed observation")
            request = request_from_payload(observed["request"])
            if request.board_id != self.scope["board_id"] or request.anchor_id != self.scope["anchor_task_id"]:
                raise ValueError("trusted planning request has wrong scope")
            anchor = self._require_enrolled_held_anchor(state)
            if request_id is not None and (type(request_id) is not str or not request_id.strip() or len(request_id) > 256):
                raise ValueError("request_id must be a non-empty string of at most 256 characters")
            if request_id is not None:
                try:
                    request_id.encode("utf-8", errors="strict")
                except UnicodeEncodeError as error:
                    raise ValueError("request_id must be UTF-8 clean") from error
            identity = request.identity
            if request_id is not None:
                bound = [op for op in state["operations"] if op.effect == "create_held"
                         and op.target.get("request_id") == request_id]
                if any(op.target.get("request_identity") != identity for op in bound):
                    raise ValueError("explicit request_id is already bound to a different trusted planning request")
            association = identity
            existing = [m for m in state["members"] if m.role == "planner" and m.work_association == association]
            if existing:
                if len(existing) != 1:
                    raise ValueError("duplicate planner association; refusing ambiguous reconciliation")
                member = existing[0]
                matches = [op for op in state["operations"] if op.effect == "create_held"
                           and op.target.get("request_identity") == identity
                           and op.target.get("task_id") == self.scope["anchor_task_id"]]
                if len(matches) == 1 and matches[0].target.get("request_id") != request_id:
                    raise ValueError("planner association is bound to a different request_id")
                if len(matches) != 1 or matches[0].phase != "applied":
                    raise ValueError("planner member has no exact durable creation proof")
                proof = self._snapshot_from_readback(matches[0].readback)
                if proof is None or proof.native_task.get("id") != member.task_id:
                    raise ValueError("existing planner card has no exact durable held readback")
                current = self.board.read_task(str(proof.native_task["id"]))
                exact = (current.native_task.get("assignee") == profile
                         and current.native_task.get("status") == "blocked"
                         and not current.parents and not current.runs
                         and self.board._workspace_routing(current.native_task, f"dir:{workspace}") is None)
                if (current.digest != proof.digest or not exact
                        or current.native_task.get("id") != member.task_id):
                    raise ValueError("existing planner card conflicts with exact held readback; preserving existing card")
                return {"outcome": "held", "task_id": member.task_id}
            if not permit_action(policy, self.store, self.scope, PAID_CAPACITY, paid_authorized=True):
                raise ValueError("no remaining paid-capacity eligibility for new held planner card")
            if self.planning_profile is not None and self.planning_profile != profile:
                raise ValueError("configured planner profile changed")
            canonical = json.dumps({"scope": self.scope, "request_identity": identity, "request_id": request_id, "action": "prepare_planner"}, sort_keys=True, separators=(",", ":"))
            key = "planner-card:" + hashlib.sha256(canonical.encode()).hexdigest()
            marker = {"kind": "planner_card", "request_identity": identity, "workspace": workspace, "profile": profile, "operation_key": key}
            target = {"task_id": self.scope["anchor_task_id"],
                "anchor_task_id": self.scope["anchor_task_id"], "request_identity": identity,
                "request": request_payload(request), "planner_marker": marker, "association": association,
                "reviewer_profile": profile, "native_parent": False, "create_title": "Planning: " + identity[:48],
                "create_body": json.dumps({"request_identity": identity, "marker": marker}, sort_keys=True),
                "create_workspace": f"dir:{workspace}", "create_idempotency_key": key}
            if request_id is not None:
                target["request_id"] = request_id
            action = Action(key, self.scope, target, "create_held", anchor.digest)
            event = {"event_id": f"{WORKFLOW_REPAIRS}:{key}", "lineage_id": f"{self.scope['anchor_task_id']}:{GENERAL_ATTEMPT}",
                "root_task_id": self.scope["anchor_task_id"], "finding_id": GENERAL_ATTEMPT, "generation": 0,
                "source_task_id": self.scope["anchor_task_id"], "source_kind": "native_operation", "native_source_id": key, "count": 1}
            prior = next((op for op in state["operations"] if op.key == key), None)
            stored = (prior if prior is not None else
                      admit_repair_operation(policy, self.store, self.scope, self._intent_for(action), event))
            if stored.phase == "unknown":
                action = self._action_from_intent(stored)
                verifier = getattr(self.board, "verify_effect", None)
                result = verifier(action) if callable(verifier) else ActionResult(key, "unsupported", "marker verifier unavailable", None)
            else:
                if stored.phase != "applied":
                    result = self.board.create_held(action, title=action.target["create_title"], body=action.target["create_body"],
                        assignee=profile, workspace=f"dir:{workspace}", idempotency_key=key)
                else:
                    result = ActionResult(key, "verified", "existing durable held planner proof", stored.readback)
            readback = self._portable_readback(result.readback)
            self.store.record_effect_observation(self.scope, key, outcome=result.outcome, details=result.details, readback=readback)
            created = self._snapshot_from_readback(readback)
            if result.outcome not in {"verified", "no-op"} or created is None or not self._readback_proves(action, result):
                raise ValueError("planner held-card creation is unverified; reconcile by exact marker")
            if stored.phase != "applied":
                self.store.ack_effect(self.scope, key, readback=created.to_dict(), outcome=result.outcome)
            current = self.board.read_task(str(created.native_task["id"]))
            current_result = ActionResult(key, "verified", "fresh held planner readback", current.to_dict())
            if current.digest != created.digest or not self._readback_proves(action, current_result):
                raise ValueError("planner card changed before membership registration; preserving existing card")
            member = ManagedMember(self.scope["board_id"], self.scope["anchor_task_id"], str(created.native_task["id"]),
                                  "planner", 0, (), association)
            self.store.register_member(member)
            return {"outcome": "held", "task_id": member.task_id}

    def release_active_piece(self, plan_id: str, ticket_id: str, *, _already_locked: bool = False) -> Mapping[str, Any]:
        """Release one exact accepted held implementation card; never claim or dispatch it."""
        from .budgets import GENERAL_ATTEMPT, IMPLEMENTATION_ATTEMPTS
        if not isinstance(self.budget_policy, BudgetPolicy):
            raise ValueError("explicit finite implementation budget is required")
        if any(type(value) is not str or not value for value in (plan_id, ticket_id)):
            raise ValueError("explicit plan and ticket identities are required")
        implementation, _ = self._local_review_roles()
        with (nullcontext() if _already_locked else self.lock):
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            if state["operator_intent"] is not None and state["operator_intent"].active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active", "actions_attempted": 0}
            roots = [member for member in state["members"] if member.role == "root"]
            if len(roots) != 1 or roots[0].task_id != self.scope["anchor_task_id"]:
                return {"outcome": "held", "reason": "exact_enrolled_root_required", "actions_attempted": 0}
            creates = [op for op in state["operations"] if op.effect == "create_held" and op.phase == "applied"
                       and op.target.get("plan_id") == plan_id and op.target.get("ticket_id") == ticket_id]
            if len(creates) != 1:
                return {"outcome": "held", "reason": "exact_held_creation_receipt_required", "actions_attempted": 0}
            created = creates[0]
            held = self._snapshot_from_readback(created.readback)
            if held is None and created.target.get("kind") == "paid_correction_v1":
                held = self._snapshot_from_readback(created.readback.get("snapshot"))
            members = [member for member in state["members"] if member.role == "implementation" and member.task_id == (None if held is None else held.native_task.get("id"))]
            if held is None or len(members) != 1:
                return {"outcome": "held", "reason": "held_member_receipt_mismatch", "actions_attempted": 0}
            member = members[0]
            current = self.board.read_task(member.task_id)
            canonical = json.dumps({"scope": self.scope, "plan_id": plan_id, "ticket_id": ticket_id,
                                    "task_id": member.task_id, "generation": member.generation,
                                    "held_digest": held.digest}, sort_keys=True, separators=(",", ":"))
            key = "active-piece-release:" + hashlib.sha256(canonical.encode()).hexdigest()
            prior = next((op for op in state["operations"] if op.key == key), None)
            if prior is not None and prior.phase == "applied":
                proof = self._snapshot_from_readback(prior.readback)
                if proof is None or current.digest != proof.digest or current.native_task.get("status") not in {"ready", "todo"}:
                    return {"outcome": "held", "reason": "applied_release_readback_drift", "operation_key": key, "actions_attempted": 0}
                return {"outcome": "released", "task_id": member.task_id, "operation_key": key, "actions_attempted": 0}
            expected_parent_ids = set()
            try:
                correction = created.target.get("kind") == "paid_correction_v1"
                if correction:
                    target = None
                else:
                    target = next(item for item in self._accepted_tranche_targets(plan_id)
                                  if item.ticket_id == ticket_id)
                for dependency_id in () if target is None else target.declared_dependencies:
                    dependency_creates = [op for op in state["operations"]
                                          if op.effect == "create_held" and op.phase == "applied"
                                          and op.target.get("plan_id") == plan_id
                                          and op.target.get("ticket_id") == dependency_id]
                    if len(dependency_creates) != 1:
                        return {"outcome": "held", "reason": "declared_dependency_creation_receipt_required", "actions_attempted": 0}
                    dependency_snapshot = self._snapshot_from_readback(dependency_creates[0].readback)
                    if dependency_snapshot is None:
                        return {"outcome": "held", "reason": "declared_dependency_creation_receipt_required", "actions_attempted": 0}
                    expected_parent_ids.add(str(dependency_snapshot.native_task.get("id")))
            except KeyError:
                if current.parents:
                    return {"outcome": "held", "reason": "accepted_piece_dependency_authority_required", "actions_attempted": 0}
            except (StopIteration, ValueError):
                return {"outcome": "held", "reason": "accepted_piece_dependency_authority_required", "actions_attempted": 0}
            current_parent_ids = {str(parent.get("id")) for parent in current.parents if isinstance(parent, Mapping)}
            recovery_bridge = self._temporary_recovery_hold_bridge(
                state, plan_id, ticket_id, created, member, current, expected_parent_ids,
            )
            held_digest = current.digest if recovery_bridge is not None else held.digest
            if (prior is None and ((current.digest != held.digest and recovery_bridge is None)
                                  or current.native_task.get("status") != "blocked"
                                  or current.native_task.get("assignee") != implementation
                                  or current_parent_ids != expected_parent_ids or current.runs)):
                return {"outcome": "held", "reason": "current_raw_hold_receipt_or_capacity_precondition_failed", "actions_attempted": 0}
            action = self._action_from_intent(prior) if prior is not None else Action(key, self.scope, {
                "task_id": member.task_id, "ticket_id": ticket_id, "plan_id": plan_id,
                "member_generation": member.generation, "profile": implementation, "held_digest": held_digest}, "release", current.digest)
            event = {"event_id": f"{IMPLEMENTATION_ATTEMPTS}:{key}", "lineage_id": f"{self.scope['anchor_task_id']}:{GENERAL_ATTEMPT}",
                     "root_task_id": self.scope["anchor_task_id"], "finding_id": GENERAL_ATTEMPT, "generation": member.generation,
                     "source_task_id": member.task_id, "source_kind": "native_operation", "native_source_id": key, "count": 1}
            try:
                stored = self.store.reserve_active_piece_release(self.scope, self._intent_for(action), event, policy_limit=self.budget_policy.implementation_attempts)
            except Exception as error:
                return {"outcome": "held", "reason": str(error), "operation_key": key, "actions_attempted": 0}
            if stored.phase == "unknown":
                result = self.board.verify_effect(action) if callable(getattr(self.board, "verify_effect", None)) else ActionResult(key, "unknown", "release verifier unavailable", None)
                attempted = 0
            else:
                self.store.begin_effect_attempt(self.scope, key)
                try:
                    result = self.board.release(action, member.task_id, f"local-first active-piece release {self.board._native_marker(action)}")
                except Exception as error:
                    result = ActionResult(key, "unknown", f"native release exception: {error}", None)
                attempted = 1
            readback = self._portable_readback(result.readback)
            self.store.record_effect_observation(self.scope, key, outcome=result.outcome, details=result.details, readback=readback)
            proof = self._snapshot_from_readback(readback)
            if result.outcome not in {"verified", "no-op"} or proof is None or proof.native_task.get("status") not in {"ready", "todo"}:
                return {"outcome": "partial", "reason": result.details, "operation_key": key, "actions_attempted": attempted}
            self.store.ack_effect(self.scope, key, readback=readback, outcome=result.outcome)
            return {"outcome": "released", "task_id": member.task_id, "operation_key": key, "actions_attempted": attempted}

    def _accepted_tranche_targets(self, plan_id: str) -> tuple[Any, ...]:
        """Rebuild the accepted tranche instead of trusting a caller ticket id."""
        from .planning_coordinator import ActiveTrancheRoute, first_active_tranche_materialization
        token = self.store.read_accepted_plan(self.scope, plan_id)
        implementation, _ = self._local_review_roles()
        material = first_active_tranche_materialization(
            self.store.read_plan(self.scope, plan_id),
            ActiveTrancheRoute(implementation, token["route"]["workspace"]),
        )
        if material.active_tranche_id != token["active_tranche"]["tranche_id"]:
            raise ValueError("accepted tranche reconstruction differs from accepted token")
        return tuple(material.targets)

    def _integrated_piece_authority(self, state: Mapping[str, Any], plan_id: str, ticket_id: str,
                                    candidate: Any) -> None:
        """Bind normal pieces to accepted membership/release; corrections to paid lineage."""
        targets = self._accepted_tranche_targets(plan_id)
        target = next((item for item in targets if item.ticket_id == ticket_id), None)
        creates = [op for op in state["operations"] if op.effect == "create_held" and op.phase == "applied"
                   and op.target.get("plan_id") == plan_id and op.target.get("ticket_id") == ticket_id]
        releases = [op for op in state["operations"] if op.effect == "release" and op.phase == "applied"
                    and op.target.get("plan_id") == plan_id and op.target.get("ticket_id") == ticket_id]
        if target is not None:
            if len(creates) != 1 or len(releases) != 1 or creates[0].key != target.operation_key:
                raise ValueError("exact accepted piece creation and release receipts are required")
        else:
            if (len(creates) != 1 or len(releases) != 1
                    or creates[0].target.get("kind") != "paid_correction_v1"
                    or not creates[0].target.get("finding_ids")
                    or creates[0].target.get("head_sha") != candidate.base_sha):
                raise ValueError("extra integration is not an exact authorized paid correction")
        task_id = releases[0].target.get("task_id")
        if len([m for m in state["members"] if m.role == "implementation" and m.task_id == task_id]) != 1:
            raise ValueError("released candidate lacks exact managed implementation member")

    def integrate_active_piece(self, plan_id: str, ticket_id: str, candidate: Any, *, review_id: str, git_adapter: Any,
                               _already_locked: bool = False) -> Mapping[str, Any]:
        """CAS-integrate one locally approved frozen candidate into an existing tranche ref."""
        from .contracts import CandidateIdentity
        from .git_adapter import GitAdapterError, IntegrationHeadConflictError
        if not isinstance(candidate, CandidateIdentity) or not isinstance(review_id, str) or not review_id:
            raise ValueError("candidate and local review identity are required")
        with (nullcontext() if _already_locked else self.lock):
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            if state["operator_intent"] is not None and state["operator_intent"].active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active"}
            if review_id in self._topology_invalidated_review_ids(state):
                return {"outcome": "held", "reason": "human_dependency_adoption_invalidated_review_eligibility"}
            try:
                self._integrated_piece_authority(state, plan_id, ticket_id, candidate)
            except ValueError as error:
                return {"outcome": "held", "reason": str(error)}
            reviews = [review for review in state["reviews"] if review.get("review_id") == review_id]
            if len(reviews) != 1 or reviews[0].get("reviewer_role") != "local" or reviews[0].get("verdict") != "approved" or reviews[0].get("candidate_identity") != candidate.to_dict() or any(item.get("outcome") != "passed" for item in reviews[0].get("checks", [])) or any(item.get("outcome") != "pass" for item in reviews[0].get("criterion_evidence", [])):
                return {"outcome": "held", "reason": "exact_approved_local_review_and_checks_required"}
            token = self.store.read_accepted_plan(self.scope, plan_id)
            tranche = token["active_tranche"]
            completed = [op for op in state["operations"] if op.effect == "git_integrate" and op.phase == "applied"
                         and op.target.get("plan_id") == plan_id and op.target.get("ticket_id") == ticket_id
                         and op.target.get("candidate") == candidate.to_dict() and op.target.get("review_id") == review_id]
            if len(completed) > 1:
                return {"outcome": "held", "reason": "multiple_immutable_integration_results", "operation_key": None}
            if completed:
                prior = completed[0]
                actual = git_adapter.existing_execution_base(tranche["tranche_id"], token["base_sha"])
                if prior.readback.get("integration_head_after") != actual:
                    return {"outcome": "held", "reason": "immutable_integration_result_ref_drift", "operation_key": prior.key}
                return {"outcome": "integrated", "operation_key": prior.key, **dict(prior.readback)}
            # An unknown CAS may already have advanced the live tranche ref. Find
            # its immutable intent by the stable request identity before observing
            # the current ref; deriving a fresh key from that changed ref would
            # lose the original expected head and cannot safely reconcile it.
            matching = [op for op in state["operations"] if op.effect == "git_integrate"
                        and op.target.get("plan_id") == plan_id and op.target.get("ticket_id") == ticket_id
                        and op.target.get("candidate") == candidate.to_dict() and op.target.get("review_id") == review_id]
            if len(matching) > 1:
                return {"outcome": "held", "reason": "multiple_immutable_integration_results", "operation_key": None}
            prior = matching[0] if matching else None
            expected = (prior.target.get("expected_head") if prior is not None
                        else git_adapter.existing_execution_base(tranche["tranche_id"], token["base_sha"]))
            if not isinstance(expected, str) or not expected:
                return {"outcome": "held", "reason": "stored_integration_expected_head_is_malformed"}
            if candidate.base_sha != expected:
                return {"outcome": "held", "reason": "candidate_base_does_not_match_expected_tranche_head", "expected_head": expected}
            frozen = git_adapter.freeze_candidate(__import__("pathlib").Path(candidate.worktree), base_sha=expected, expected_head_sha=candidate.head_sha)
            if frozen.head_sha != candidate.head_sha:
                return {"outcome": "held", "reason": "candidate_freeze_identity_drift"}
            key = (prior.key if prior is not None else "active-piece-integration:" + hashlib.sha256(json.dumps({"plan_id":plan_id,"ticket_id":ticket_id,"candidate":candidate.to_dict(),"review_id":review_id,"expected":expected}, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
            if prior is not None and prior.phase == "applied":
                if prior.readback.get("integration_head_after") != git_adapter.existing_execution_base(tranche["tranche_id"], token["base_sha"]):
                    return {"outcome": "held", "reason": "immutable_integration_result_ref_drift", "operation_key": key}
                return {"outcome": "integrated", "operation_key": key, **dict(prior.readback)}
            action = self._action_from_intent(prior) if prior is not None else Action(key, self.scope, {"kind":"active_piece_git_integration_v1","plan_id":plan_id,"ticket_id":ticket_id,"candidate":candidate.to_dict(),"review_id":review_id,"tranche_id":tranche["tranche_id"],"expected_head":expected}, "git_integrate", expected)
            stored = prior if prior is not None else self.store.reserve_operation(self._intent_for(action))
            if stored.phase == "unknown":
                actual = git_adapter.existing_execution_base(tranche["tranche_id"], token["base_sha"])
                if actual != candidate.head_sha:
                    return {"outcome": "partial", "reason": "unknown_git_effect_requires_exact_head_readback", "operation_key": key}
                result = {"integration_head_before": expected, "integration_head_after": actual, "candidate_identity": candidate.to_dict(), "review_id": review_id}
            else:
                self.store.begin_effect_attempt(self.scope, key)
                try:
                    after = git_adapter.advance_integration_head(tranche["tranche_id"], expected, candidate.head_sha)
                except (GitAdapterError, IntegrationHeadConflictError) as error:
                    return {"outcome": "held", "reason": str(error), "operation_key": key}
                result = {"integration_head_before": expected, "integration_head_after": after, "candidate_identity": candidate.to_dict(), "review_id": review_id}
            self.store.ack_effect(self.scope, key, readback=result, outcome="verified")
            return {"outcome": "integrated", "operation_key": key, **result}

    def integrate_persisted_active_piece(self, plan_id: str, ticket_id: str, review_id: str, *, git_adapter: Any) -> Mapping[str, Any]:
        """Public identity-only integration façade.

        The selected local-review receipt is the authority for the candidate.  A
        CLI caller therefore cannot smuggle a worktree/head through JSON.
        """
        from .contracts import CandidateIdentity
        if not all(type(value) is str and value for value in (plan_id, ticket_id, review_id)):
            raise ValueError("explicit plan, ticket, and local review identities are required")
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            reviews = [item for item in state["reviews"] if item.get("review_id") == review_id]
            if len(reviews) != 1 or reviews[0].get("reviewer_role") != "local":
                return {"outcome": "held", "reason": "exact_persisted_local_review_required"}
            try:
                candidate = CandidateIdentity.from_dict(reviews[0]["candidate_identity"])
            except (KeyError, TypeError, ValueError):
                return {"outcome": "held", "reason": "persisted_local_review_candidate_is_malformed"}
            # The delegated method owns the same non-reentrant lock, so use its
            # implementation directly rather than re-entering a public lock.
            return self.integrate_active_piece(plan_id, ticket_id, candidate, review_id=review_id,
                                               git_adapter=git_adapter, _already_locked=True)

    def prepare_paid_integrated_review(self, plan_id: str, *, git_adapter: Any) -> Mapping[str, Any]:
        """Freeze the complete integrated head, run trusted checks, and hold one paid review card.

        The caller cannot provide checks or a verdict.  The configured runner is
        the sole command authority; its revision-bound artifacts are persisted in
        the review request before the held native card is created.
        """
        from .contracts import CandidateIdentity, ManagedMember
        if type(plan_id) is not str or not plan_id or self.combined_check_runner is None:
            raise ValueError("plan identity and configured trusted combined-check runner are required")
        paid = self._paid_review_role()
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            if state["operator_intent"] is not None and state["operator_intent"].active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active"}
            token = self.store.read_accepted_plan(self.scope, plan_id)
            tranche = dict(token["active_tranche"])
            head = git_adapter.existing_execution_base(tranche["tranche_id"], token["base_sha"])
            integrations = [op for op in state["operations"] if op.effect == "git_integrate" and op.phase == "applied"
                            and op.target.get("plan_id") == plan_id]
            try:
                targets = self._accepted_tranche_targets(plan_id)
            except ValueError as error:
                return {"outcome": "held", "reason": str(error)}
            required = {item.ticket_id for item in targets}
            if (not integrations or any(sum(op.target.get("ticket_id") == ticket for op in integrations) != 1
                                        for ticket in required)):
                return {"outcome": "held", "reason": "complete_immutable_piece_integration_evidence_required"}
            cursor, seen, remaining = token["base_sha"], set(), list(integrations)
            while remaining:
                matches = [op for op in remaining if op.readback.get("integration_head_before") == cursor]
                if len(matches) != 1:
                    return {"outcome": "held", "reason": "integration_head_not_bound_to_piece_evidence"}
                op = matches[0]; remaining.remove(op)
                ticket, after = op.target.get("ticket_id"), op.readback.get("integration_head_after")
                authorized_extra = False
                if ticket not in required:
                    create = next((item for item in state["operations"] if item.effect == "create_held"
                                   and item.phase == "applied" and item.target.get("ticket_id") == ticket), None)
                    authorized_extra = create is not None and create.target.get("kind") == "paid_correction_v1"
                if ticket in seen or not isinstance(after, str) or not after or (ticket not in required and not authorized_extra):
                    return {"outcome": "held", "reason": "integration_head_not_bound_to_piece_evidence"}
                seen.add(ticket); cursor = after
            if not required.issubset(seen) or cursor != head:
                return {"outcome": "held", "reason": "integration_head_not_bound_to_piece_evidence"}
            check_key = "combined-checks:" + hashlib.sha256(json.dumps({"plan_id": plan_id, "tranche": tranche, "head": head}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            existing = next((op for op in state["operations"] if op.key == check_key), None)
            if existing is None:
                result = self.combined_check_runner({"plan_id": plan_id, "tranche_id": tranche["tranche_id"], "base_sha": token["base_sha"], "head_sha": head})
                if not isinstance(result, Mapping) or set(result) != {"head_sha", "checks"} or result["head_sha"] != head or not isinstance(result["checks"], list) or not result["checks"]:
                    return {"outcome": "held", "reason": "trusted_combined_check_runner_returned_malformed_artifacts"}
                checks = []
                for item in result["checks"]:
                    if (not isinstance(item, Mapping) or set(item) != {"check_id", "command", "exit_code", "output_sha256"}
                            or not all(isinstance(item.get(k), str) and item[k] for k in ("check_id", "command", "output_sha256"))
                            or type(item.get("exit_code")) is not int):
                        return {"outcome": "held", "reason": "trusted_combined_check_artifact_is_malformed"}
                    checks.append(dict(item))
                if [item["check_id"] for item in checks] != sorted(item["check_id"] for item in checks) or len({item["check_id"] for item in checks}) != len(checks) or any(item["exit_code"] != 0 for item in checks):
                    return {"outcome": "held", "reason": "combined_checks_failed_or_noncanonical"}
                action = Action(check_key, self.scope, {"kind": "combined_checks_v1", "plan_id": plan_id, "tranche_id": tranche["tranche_id"], "base_sha": token["base_sha"], "head_sha": head}, "combined_checks", head)
                check_intent = OperationIntent(**{**self._intent_for(action).to_dict(), "outcome": "verified", "readback": {"head_sha": head, "checks": checks}, "phase": "applied"})
                existing = self.store.reserve_operation(check_intent)
            if existing.phase != "applied" or existing.readback.get("head_sha") != head:
                return {"outcome": "held", "reason": "combined_check_evidence_unavailable"}
            candidate = CandidateIdentity("integrated:" + tranche["tranche_id"], str(git_adapter.primary_checkout), token["base_sha"], head,
                "integrated:" + hashlib.sha256(head.encode()).hexdigest(), "combined:" + check_key, "combined-check:" + check_key, "plan:" + plan_id)
            self.store.record_candidate(self.scope, candidate)
            request_key = "paid-integrated-review:" + hashlib.sha256(json.dumps({"plan_id":plan_id,"head":head,"checks":check_key,"profile":paid}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            previous = next((op for op in self.store.read_scope(self.scope)["operations"] if op.key == request_key), None)
            if previous is not None and previous.phase == "applied":
                return {"outcome": "held", "review_task_id": previous.readback.get("task_id"), "review_request_key": request_key, "head_sha": head, "actions_attempted": 0}
            anchor = self.board.read_task(self.scope["anchor_task_id"])
            action = self._action_from_intent(previous) if previous else Action(request_key, self.scope, {"kind":"paid_integrated_review_v1", "anchor_task_id":self.scope["anchor_task_id"], "plan_id":plan_id, "tranche_id":tranche["tranche_id"], "head_sha":head, "check_operation_key":check_key, "candidate":candidate.to_dict(), "profile":paid, "native_parent":False}, "create_held", anchor.digest)
            stored = self.store.reserve_operation(self._intent_for(action)) if previous is None else previous
            if stored.phase == "unknown":
                result = self.board.verify_effect(action) if callable(getattr(self.board, "verify_effect", None)) else ActionResult(request_key, "unknown", "paid review create verifier unavailable", None)
                attempted = 0
            else:
                try:
                    result = self.board.create_held(action, title="Paid integrated review: " + tranche["tranche_id"], body=json.dumps({"plan_id":plan_id,"head_sha":head,"check_operation_key":check_key}, sort_keys=True), assignee=paid, workspace="dir:" + str(git_adapter.primary_checkout), idempotency_key=request_key)
                except Exception as error:
                    result = ActionResult(request_key, "unknown", f"paid review create exception: {error}", None)
                attempted = 1
            proof = self._snapshot_from_readback(self._portable_readback(result.readback))
            if result.outcome not in {"verified", "no-op"} or proof is None or proof.native_task.get("status") != "blocked":
                return {"outcome":"partial", "reason":result.details, "review_request_key":request_key, "actions_attempted":attempted}
            self.store.ack_effect(self.scope, request_key, readback={"task_id":proof.native_task["id"], "head_sha":head, "check_operation_key":check_key, "candidate":candidate.to_dict(), "snapshot":proof.to_dict()}, outcome=result.outcome)
            self.store.register_member(ManagedMember(self.scope["board_id"], self.scope["anchor_task_id"], str(proof.native_task["id"]), "paid_review", 0, (), request_key))
            return {"outcome":"held", "review_task_id":proof.native_task["id"], "review_request_key":request_key, "head_sha":head, "actions_attempted":attempted}

    def release_paid_integrated_review(self, plan_id: str, *, git_adapter: Any) -> Mapping[str, Any]:
        """Separately admit one exact held paid-review request with paid capacity."""
        if type(plan_id) is not str or not plan_id or not isinstance(self.budget_policy, BudgetPolicy):
            raise ValueError("explicit plan and finite paid-review capacity are required")
        paid = self._paid_review_role()
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            if state["operator_intent"] is not None and state["operator_intent"].active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active", "actions_attempted": 0}
            token = self.store.read_accepted_plan(self.scope, plan_id)
            head = git_adapter.existing_execution_base(token["active_tranche"]["tranche_id"], token["base_sha"])
            requests = [op for op in state["operations"] if op.effect == "create_held" and op.phase == "applied"
                        and op.target.get("kind") == "paid_integrated_review_v1" and op.target.get("plan_id") == plan_id
                        and op.readback.get("head_sha") == head]
            if len(requests) != 1:
                return {"outcome": "held", "reason": "exact_current_paid_review_request_required", "actions_attempted": 0}
            request = requests[0]
            held = self._snapshot_from_readback(request.readback.get("snapshot"))
            members = [member for member in state["members"] if member.role == "paid_review"
                       and held is not None and member.task_id == held.native_task.get("id")
                       and member.work_association == request.key]
            if held is None or len(members) != 1:
                return {"outcome": "held", "reason": "paid_review_held_member_receipt_mismatch", "actions_attempted": 0}
            member = members[0]
            current = self.board.read_task(member.task_id)
            canonical = json.dumps({"scope": self.scope, "plan_id": plan_id, "head_sha": head,
                                    "task_id": member.task_id, "generation": member.generation,
                                    "held_digest": held.digest}, sort_keys=True, separators=(",", ":"))
            key = "paid-integrated-review-release:" + hashlib.sha256(canonical.encode()).hexdigest()
            prior = next((op for op in state["operations"] if op.key == key), None)
            if prior is not None and prior.phase == "applied":
                proof = self._snapshot_from_readback(prior.readback)
                if proof is None or current.digest != proof.digest or current.native_task.get("status") not in {"ready", "todo"}:
                    return {"outcome": "held", "reason": "applied_paid_review_release_readback_drift", "operation_key": key, "actions_attempted": 0}
                return {"outcome": "released", "task_id": member.task_id, "operation_key": key, "actions_attempted": 0}
            if (current.digest != held.digest or current.native_task.get("status") != "blocked"
                    or current.native_task.get("assignee") != paid or current.parents or current.runs):
                return {"outcome": "held", "reason": "current_paid_review_hold_receipt_or_capacity_precondition_failed", "actions_attempted": 0}
            action = Action(key, self.scope, {"task_id": member.task_id, "request_id": request.key,
                                                "member_generation": member.generation, "profile": paid}, "release", current.digest)
            event = {"event_id": f"{PAID_CAPACITY}:{key}", "lineage_id": f"{self.scope['anchor_task_id']}:{GENERAL_ATTEMPT}",
                     "root_task_id": self.scope["anchor_task_id"], "finding_id": GENERAL_ATTEMPT,
                     "generation": member.generation, "source_task_id": member.task_id,
                     "source_kind": "native_operation", "native_source_id": key, "count": 1}
            try:
                stored = self.store.reserve_paid_release(self.scope, self._intent_for(action), event,
                                                         policy_limit=self.budget_policy.paid_capacity)
            except Exception as error:
                return {"outcome": "held", "reason": str(error), "operation_key": key, "actions_attempted": 0}
            if stored.phase == "unknown":
                result = self.board.verify_effect(action) if callable(getattr(self.board, "verify_effect", None)) else ActionResult(key, "unknown", "paid review release verifier unavailable", None)
                attempted = 0
            else:
                self.store.begin_effect_attempt(self.scope, key)
                try:
                    result = self.board.release(action, member.task_id, f"local-first paid review release {self.board._native_marker(action)}")
                except Exception as error:
                    result = ActionResult(key, "unknown", f"native paid review release exception: {error}", None)
                attempted = 1
            readback = self._portable_readback(result.readback)
            self.store.record_effect_observation(self.scope, key, outcome=result.outcome, details=result.details, readback=readback)
            proof = self._snapshot_from_readback(readback)
            if result.outcome not in {"verified", "no-op"} or proof is None or proof.native_task.get("status") not in {"ready", "todo"}:
                return {"outcome": "partial", "reason": result.details, "operation_key": key, "actions_attempted": attempted}
            self.store.ack_effect(self.scope, key, readback=readback, outcome=result.outcome)
            return {"outcome": "released", "task_id": member.task_id, "operation_key": key, "actions_attempted": attempted}

    def _accepted_successor_authority(self, parent_plan_id: str, successor_plan_id: str,
                                      acceptance_operation_key: str, *, git_adapter: Any) -> Mapping[str, Any]:
        """Reconstruct the explicit, revision-bound authority for one successor plan.

        A successor is deliberately a separately accepted plan.  This boundary does
        not manufacture a proposal, alter the original accepted token, or infer a
        later tranche from the predecessor's unmaterialized proposal.
        """
        if any(type(value) is not str or not value for value in
               (parent_plan_id, successor_plan_id, acceptance_operation_key)):
            raise ValueError("explicit parent, separately accepted successor, and acceptance receipt are required")
        state = self.store.read_scope(self.scope)
        if state["operator_intent"] is not None and state["operator_intent"].active:
            raise ValueError("successor admission is fenced by operator pause/cancellation")
        matches = [op for op in state["operations"] if op.key == acceptance_operation_key]
        if len(matches) != 1:
            raise ValueError("exact predecessor acceptance receipt is required")
        acceptance = matches[0]
        if (acceptance.effect != "tranche_accept" or acceptance.phase != "applied"
                or acceptance.outcome != "verified" or acceptance.target.get("kind") != "tranche_acceptance_v1"
                or acceptance.target.get("plan_id") != parent_plan_id):
            raise ValueError("predecessor acceptance receipt is not exact and applied")
        parent = self.store.read_accepted_plan(self.scope, parent_plan_id)
        successor = self.store.read_accepted_plan(self.scope, successor_plan_id)
        if successor_plan_id == parent_plan_id:
            raise ValueError("successor must have a distinct accepted plan identity")
        parent_tranche = parent["active_tranche"]
        head = git_adapter.existing_execution_base(parent_tranche["tranche_id"], parent["base_sha"])
        if (acceptance.target.get("tranche_id") != parent_tranche["tranche_id"]
                or acceptance.target.get("head_sha") != head
                or acceptance.readback != {"head_sha": head, "review_id": acceptance.target.get("review_id"),
                                            "successor_authorized": acceptance.readback.get("successor_authorized")}):
            raise ValueError("predecessor acceptance revision evidence drifted")
        # The successor planner has to accept a new immutable contract rooted at
        # the exact accepted integration revision.  Route/repository/root binding
        # prevents a caller from using an unrelated accepted plan as a release key.
        if (successor["base_sha"] != head
                or successor["repository_identity"] != parent["repository_identity"]
                or successor["root_contract_hash"] != parent["root_contract_hash"]
                or successor["route"] != parent["route"]):
            raise ValueError("successor accepted plan is not bound to current accepted predecessor consequences")
        return {"parent_plan_id": parent_plan_id, "successor_plan_id": successor_plan_id,
                "acceptance_operation_key": acceptance_operation_key, "head_sha": head,
                "parent_acceptance_identity": parent["acceptance_identity"],
                "successor_acceptance_identity": successor["acceptance_identity"],
                "successor_tranche": dict(successor["active_tranche"])}

    def materialize_accepted_successor(self, parent_plan_id: str, successor_plan_id: str,
                                        acceptance_operation_key: str, *, git_adapter: Any) -> Mapping[str, Any]:
        """Create only the separately accepted successor's held pieces.

        The actual held-create protocol remains `prepare_active_tranche`; this
        wrapper supplies the predecessor acceptance/revision fence before and after
        it.  It has no release, claim, dispatch, completion, or Git side effect.
        """
        with self.lock:
            self._assert_lock()
            authority = self._accepted_successor_authority(
                parent_plan_id, successor_plan_id, acceptance_operation_key, git_adapter=git_adapter)
            # The predecessor authority, each reserve/create/readback, and final
            # revision barrier share the same non-reentrant mutation lock.
            result = self.prepare_active_tranche(successor_plan_id, _already_locked=True)
            if result.get("outcome") != "held":
                return {"outcome": "partial", "authority": authority, "materialization": result,
                        "actions_attempted": result.get("actions_attempted", 0)}
            final = self._accepted_successor_authority(
                parent_plan_id, successor_plan_id, acceptance_operation_key, git_adapter=git_adapter)
            if final != authority:
                return {"outcome": "partial", "reason": "successor authority drift after held materialization",
                        "authority": authority, "materialization": result,
                        "actions_attempted": result.get("actions_attempted", 0)}
            return {"outcome": "held", "authority": authority, "materialization": result,
                    "actions_attempted": result.get("actions_attempted", 0)}

    def release_accepted_successor_piece(self, parent_plan_id: str, successor_plan_id: str,
                                         acceptance_operation_key: str, ticket_id: str, *, git_adapter: Any) -> Mapping[str, Any]:
        """Separately admit one already-materialized successor piece to ready state."""
        if type(ticket_id) is not str or not ticket_id:
            raise ValueError("explicit successor ticket identity is required")
        # Reconstruct parent authority under the same mutation lock immediately
        # before the reserve/unblock; do not drop the fence between wrappers.
        with self.lock:
            self._assert_lock()
            authority = self._accepted_successor_authority(
                parent_plan_id, successor_plan_id, acceptance_operation_key, git_adapter=git_adapter)
            state = self.store.read_scope(self.scope)
            creates = [op for op in state["operations"] if op.effect == "create_held" and op.phase == "applied"
                       and op.target.get("plan_id") == successor_plan_id and op.target.get("ticket_id") == ticket_id]
            if len(creates) != 1:
                return {"outcome": "held", "reason": "exact_successor_held_materialization_required",
                        "authority": authority, "actions_attempted": 0}
            released = self.release_active_piece(successor_plan_id, ticket_id, _already_locked=True)
            return {**released, "authority": authority}

    def accept_tranche(self, plan_id: str, review_id: str, *, git_adapter: Any, authorize_successor: bool = False) -> Mapping[str, Any]:
        """Record exact paid approval; successor needs a separate accepted-plan admission."""
        if type(plan_id) is not str or type(review_id) is not str or not plan_id or not review_id or type(authorize_successor) is not bool:
            raise ValueError("explicit plan, paid review, and successor authorization flag are required")
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            if state["operator_intent"] is not None and state["operator_intent"].active:
                return {"outcome":"held", "reason":"operator_pause_or_cancellation_active"}
            token = self.store.read_accepted_plan(self.scope, plan_id); tranche = token["active_tranche"]
            if review_id in self._topology_invalidated_review_ids(state):
                return {"outcome":"held", "reason":"human_dependency_adoption_invalidated_review_eligibility"}
            head = git_adapter.existing_execution_base(tranche["tranche_id"], token["base_sha"])
            review = next((item for item in state["reviews"] if item.get("review_id") == review_id), None)
            if review is None or review.get("reviewer_role") != "paid" or review.get("verdict") != "approved" or review.get("candidate_identity", {}).get("head_sha") != head:
                return {"outcome":"held", "reason":"exact_current_paid_approval_required"}
            paid = self._paid_review_role()
            if review.get("native_review", {}).get("profile") != paid or any(item.get("outcome") != "passed" for item in review.get("checks", [])) or any(item.get("outcome") != "pass" for item in review.get("criterion_evidence", [])):
                return {"outcome":"held", "reason":"paid_review_provenance_or_evidence_invalid"}
            check_ops = [op for op in state["operations"] if op.effect == "combined_checks" and op.phase == "applied" and op.target.get("plan_id") == plan_id and op.readback.get("head_sha") == head]
            try:
                required_tickets = {target.ticket_id for target in self._accepted_tranche_targets(plan_id)}
            except ValueError as error:
                return {"outcome":"held", "reason":str(error)}
            integrations = [op for op in state["operations"] if op.effect == "git_integrate" and op.phase == "applied" and op.target.get("plan_id") == plan_id]
            cursor, seen, remaining = token["base_sha"], set(), list(integrations)
            while remaining:
                matches = [op for op in remaining if op.readback.get("integration_head_before") == cursor]
                if len(matches) != 1:
                    return {"outcome":"held", "reason":"complete_immutable_piece_integration_evidence_required"}
                op = matches[0]; remaining.remove(op)
                ticket, after = op.target.get("ticket_id"), op.readback.get("integration_head_after")
                extra = next((create for create in state["operations"] if create.effect == "create_held" and create.phase == "applied" and create.target.get("ticket_id") == ticket and create.target.get("kind") == "paid_correction_v1"), None)
                if ticket in seen or not isinstance(after, str) or (ticket not in required_tickets and extra is None):
                    return {"outcome":"held", "reason":"complete_immutable_piece_integration_evidence_required"}
                seen.add(ticket); cursor = after
            if not required_tickets.issubset(seen) or cursor != head:
                return {"outcome":"held", "reason":"complete_immutable_piece_integration_evidence_required"}
            if len(check_ops) != 1 or any(item.get("exit_code") != 0 for item in check_ops[0].readback.get("checks", [])):
                return {"outcome":"held", "reason":"exact_current_combined_checks_required"}
            unresolved = [item for item in state["reviews"] if item.get("reviewer_role") == "paid" and item.get("verdict") == "changes_requested" and item.get("candidate_identity", {}).get("head_sha") == head]
            if unresolved:
                return {"outcome":"held", "reason":"unresolved_paid_findings_require_correction"}
            key = "tranche-acceptance:" + hashlib.sha256(json.dumps({"plan_id":plan_id,"head":head,"review_id":review_id,"checks":check_ops[0].key}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            action = Action(key, self.scope, {"kind":"tranche_acceptance_v1","plan_id":plan_id,"tranche_id":tranche["tranche_id"],"head_sha":head,"review_id":review_id,"check_operation_key":check_ops[0].key}, "tranche_accept", head)
            accepted = OperationIntent(**{**self._intent_for(action).to_dict(), "outcome":"verified", "readback":{"head_sha":head,"review_id":review_id,"successor_authorized":authorize_successor}, "phase":"applied"})
            self.store.reserve_operation(accepted)
            return {"outcome":"accepted", "acceptance_operation_key":key, "head_sha":head, "successor_decision":"held_separate_admission" if authorize_successor else "not_authorized"}

    def prepare_paid_correction(self, plan_id: str, review_id: str, *, git_adapter: Any) -> Mapping[str, Any]:
        """Materialize one canonical paid-finding correction held for separate admission.

        This does not release, claim, dispatch, integrate, or accept the
        correction.  Replays use the same immutable finding set/key; an unknown
        create is verification-only and never sends a second create.
        """
        from .budgets import GENERAL_ATTEMPT, REVIEW_CORRECTIONS
        from .contracts import ManagedMember
        if type(plan_id) is not str or type(review_id) is not str or not plan_id or not review_id:
            raise ValueError("explicit plan and paid review identities are required")
        if not isinstance(self.budget_policy, BudgetPolicy):
            raise ValueError("explicit finite correction budget is required")
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            if state["operator_intent"] is not None and state["operator_intent"].active:
                return {"outcome":"held", "reason":"operator_pause_or_cancellation_active"}
            review = next((item for item in state["reviews"] if item.get("review_id") == review_id), None)
            if review is None or review.get("reviewer_role") != "paid" or review.get("verdict") != "changes_requested":
                return {"outcome":"held", "reason":"exact_paid_changes_requested_evidence_required"}
            token = self.store.read_accepted_plan(self.scope, plan_id); tranche = token["active_tranche"]
            head = git_adapter.existing_execution_base(tranche["tranche_id"], token["base_sha"])
            if review.get("candidate_identity", {}).get("head_sha") != head or review.get("native_review", {}).get("profile") != self._paid_review_role():
                return {"outcome":"held", "reason":"paid_finding_revision_or_profile_drift"}
            findings = review.get("findings")
            if not isinstance(findings, list) or not findings:
                return {"outcome":"held", "reason":"paid_finding_set_required"}
            canonical_findings = tuple(sorted((dict(item) for item in findings), key=lambda item: item["finding_id"]))
            if len({item["finding_id"] for item in canonical_findings}) != len(canonical_findings):
                return {"outcome":"held", "reason":"paid_finding_set_is_ambiguous"}
            source_task = review["native_review"]["task_id"]
            members = [member for member in state["members"] if member.task_id == source_task and member.role == "paid_review"]
            if len(members) != 1:
                return {"outcome":"held", "reason":"paid_review_request_membership_missing"}
            member = members[0]
            key = "paid-correction:" + hashlib.sha256(json.dumps({"plan_id":plan_id,"head":head,"review_id":review_id,"findings":canonical_findings}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            prior = next((op for op in state["operations"] if op.key == key), None)
            if prior is not None and prior.phase == "applied":
                return {"outcome":"held", "correction_task_id":prior.readback.get("task_id"), "operation_key":key, "actions_attempted":0}
            anchor = self.board.read_task(self.scope["anchor_task_id"])
            action = self._action_from_intent(prior) if prior else Action(key, self.scope, {"kind":"paid_correction_v1","anchor_task_id":self.scope["anchor_task_id"],"plan_id":plan_id,"ticket_id":key,"tranche_id":tranche["tranche_id"],"source_review_id":review_id,"source_task_id":source_task,"task_id":source_task,"head_sha":head,"candidate":review["candidate_identity"],"finding_ids":tuple(item["finding_id"] for item in canonical_findings),"findings":canonical_findings,"native_parent":False}, "create_held", anchor.digest)
            event = {"event_id":f"{REVIEW_CORRECTIONS}:{key}","lineage_id":f"{self.scope['anchor_task_id']}:{GENERAL_ATTEMPT}","root_task_id":self.scope["anchor_task_id"],"finding_id":GENERAL_ATTEMPT,"generation":member.generation,"source_task_id":source_task,"source_kind":"native_operation","native_source_id":key,"count":1}
            try:
                stored = self.store.reserve_budgeted_repair_operation(self._intent_for(action), event, policy_limit=self.budget_policy.review_corrections) if prior is None else prior
            except Exception as error:
                return {"outcome":"held", "reason":str(error), "operation_key":key}
            if stored.phase == "unknown":
                result = self.board.verify_effect(action) if callable(getattr(self.board, "verify_effect", None)) else ActionResult(key,"unknown","correction create verifier unavailable",None); attempted = 0
            else:
                # A shared fixture board can be observed by fresh coordinators;
                # bind this exact lock-held callback immediately before its
                # faithful adapter-side claim, as the real adapter's composition
                # root does for its own instance.
                self.board.claim_create_attempt = self._claim_native_create_attempt
                try:
                    result = self.board.create_held(action, title="Correction: " + tranche["tranche_id"], body=json.dumps({"review_id":review_id,"head_sha":head,"findings":canonical_findings}, sort_keys=True), assignee=self._local_review_roles()[0], workspace="dir:" + str(git_adapter.primary_checkout), idempotency_key=key)
                except Exception as error:
                    result = ActionResult(key,"unknown",f"correction create exception: {error}",None)
                attempted = 1
            proof = self._snapshot_from_readback(self._portable_readback(result.readback))
            if result.outcome not in {"verified","no-op"} or proof is None or proof.native_task.get("status") != "blocked":
                return {"outcome":"partial","reason":result.details,"operation_key":key,"actions_attempted":attempted}
            self.store.ack_effect(self.scope,key,readback={"task_id":proof.native_task["id"],"head_sha":head,"review_id":review_id,"findings":list(canonical_findings),"snapshot":proof.to_dict()},outcome=result.outcome)
            self.store.register_member(ManagedMember(self.scope["board_id"],self.scope["anchor_task_id"],str(proof.native_task["id"]),"implementation",member.generation+1,tuple(item["finding_id"] for item in canonical_findings),"paid-correction:"+key))
            return {"outcome":"held","correction_task_id":proof.native_task["id"],"operation_key":key,"actions_attempted":attempted}

    def release_paid_correction(self, plan_id: str, review_id: str, *, git_adapter: Any) -> Mapping[str, Any]:
        """Separately admit one held paid-finding correction to implementation.

        The correction's immutable create receipt chooses its ticket identity; this
        wrapper never accepts a caller-selected task or finding set.  The normal
        release seam reserves the next implementation attempt before its single
        native unblock and keeps unknown effects reconciliation-only.
        """
        if type(plan_id) is not str or type(review_id) is not str or not plan_id or not review_id:
            raise ValueError("explicit plan and paid review identities are required")
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            token = self.store.read_accepted_plan(self.scope, plan_id)
            head = git_adapter.existing_execution_base(token["active_tranche"]["tranche_id"], token["base_sha"])
            source_reviews = [review for review in state["reviews"] if review.get("review_id") == review_id]
            if (len(source_reviews) != 1 or source_reviews[0].get("reviewer_role") != "paid"
                    or source_reviews[0].get("verdict") != "changes_requested"
                    or source_reviews[0].get("candidate_identity", {}).get("head_sha") != head
                    or source_reviews[0].get("native_review", {}).get("profile") != self._paid_review_role()
                    or not isinstance(source_reviews[0].get("findings"), list) or not source_reviews[0]["findings"]):
                return {"outcome": "held", "reason": "exact_current_paid_correction_source_required", "actions_attempted": 0}
            matches = [op for op in state["operations"] if op.effect == "create_held" and op.target.get("kind") == "paid_correction_v1"
                       and op.target.get("plan_id") == plan_id and op.target.get("source_review_id") == review_id]
            if len(matches) != 1:
                return {"outcome": "held", "reason": "exact_held_paid_correction_required", "actions_attempted": 0}
            correction = matches[0]
            if (correction.phase != "applied" or correction.target.get("ticket_id") != correction.key
                    or correction.target.get("head_sha") != head
                    or tuple(correction.target.get("finding_ids", ())) != tuple(sorted(item.get("finding_id") for item in source_reviews[0]["findings"]))):
                return {"outcome": "held", "reason": "paid_correction_creation_is_not_exactly_applied", "actions_attempted": 0}
            return self.release_active_piece(plan_id, correction.key, _already_locked=True)

    def submit_local_review(self, plan_id: str, ticket_id: str, candidate: Any, review: Mapping[str, Any]) -> Mapping[str, Any]:
        """Record owned fresh local-review evidence for a released correction/piece."""
        from .contracts import CandidateIdentity
        if type(plan_id) is not str or type(ticket_id) is not str or not plan_id or not ticket_id or not isinstance(candidate, CandidateIdentity) or not isinstance(review, Mapping):
            raise ValueError("explicit plan, ticket, candidate, and local review are required")
        _implementation, local = self._local_review_roles()
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            releases = [op for op in state["operations"] if op.effect == "release" and op.phase == "applied"
                        and op.target.get("plan_id") == plan_id and op.target.get("ticket_id") == ticket_id]
            if len(releases) != 1 or review.get("reviewer_role") != "local":
                return {"outcome": "held", "reason": "exact_released_piece_and_local_role_required"}
            task_id = releases[0].target.get("task_id"); native = review.get("native_review")
            if not isinstance(native, Mapping) or native.get("task_id") != task_id or native.get("profile") != local or review.get("candidate_identity") != candidate.to_dict():
                return {"outcome": "held", "reason": "local_review_candidate_or_provenance_mismatch"}
            handoff = self._verified_handoff(task_id, candidate, local)
            if handoff is None:
                return {"outcome": "held", "reason": "verified_local_review_handoff_missing"}
            if not self._frozen_candidate(candidate, review, handoff.get("frozen_handoff")):
                return {"outcome": "held", "reason": "trusted_git_freeze_unavailable"}
            reader = getattr(self.board, "read_scoped_run", None)
            run = reader(self.scope, task_id, native.get("run_id")) if callable(reader) else None
            card = self.board.read_task(task_id)
            observed_session = self._worker_session(run) if isinstance(run, Mapping) else None
            claims = [event for event in card.events if event.get("kind") == "claimed"
                      and str(event.get("run_id")) == str(native.get("run_id"))
                      and isinstance(event.get("payload"), Mapping)
                      and event["payload"].get("source_status") == "review"] if isinstance(card, BoardSnapshot) else []
            completions = [event for event in card.events if event.get("kind") == "completed"
                           and str(event.get("run_id")) == str(native.get("run_id"))] if isinstance(card, BoardSnapshot) else []
            if (not isinstance(run, Mapping) or str(run.get("id")) != native.get("run_id") or run.get("profile") != local
                    or run.get("status") not in {"completed", "done"} or not isinstance(card, BoardSnapshot)
                    or card.native_task.get("id") != task_id or card.native_task.get("status") != "done"
                    or card.native_task.get("assignee") != local
                    or not isinstance(native.get("session_id"), str) or not native.get("session_id")
                    or observed_session != native.get("session_id")
                    or native.get("run_id") == candidate.originating_run_id
                    or native.get("session_id") == handoff.get("implementation_session_id")
                    or len(claims) != 1 or len(completions) != 1):
                return {"outcome": "held", "reason": "owned_terminal_local_review_run_required"}
            self.store.record_candidate(self.scope, candidate)
            self.store.record_review(self.scope, candidate, review)
            return {"outcome": review.get("verdict"), "review_id": review.get("review_id")}

    def submit_paid_integrated_review(self, plan_id: str, review: Mapping[str, Any], *, git_adapter: Any) -> Mapping[str, Any]:
        """Persist a paid verdict only after exact held-request/run/revision readback."""
        from .contracts import CandidateIdentity
        if type(plan_id) is not str or not plan_id or not isinstance(review, Mapping):
            raise ValueError("explicit plan and structured paid review are required")
        paid = self._paid_review_role()
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            token = self.store.read_accepted_plan(self.scope, plan_id); tranche = token["active_tranche"]
            head = git_adapter.existing_execution_base(tranche["tranche_id"], token["base_sha"])
            requests = [op for op in state["operations"] if op.effect == "create_held" and op.phase == "applied"
                        and op.target.get("kind") == "paid_integrated_review_v1" and op.target.get("plan_id") == plan_id
                        and op.readback.get("head_sha") == head]
            if len(requests) != 1 or review.get("reviewer_role") != "paid":
                return {"outcome":"held", "reason":"exact_paid_review_request_required"}
            request = requests[0]; native = review.get("native_review")
            releases = [op for op in state["operations"] if op.effect == "release" and op.phase == "applied"
                        and op.target.get("task_id") == request.readback.get("task_id")
                        and op.target.get("request_id") == request.key and op.target.get("profile") == paid]
            if len(releases) != 1:
                return {"outcome":"held", "reason":"exact_paid_review_release_required"}
            if not isinstance(native, Mapping) or native.get("profile") != paid or native.get("task_id") != request.readback.get("task_id"):
                return {"outcome":"held", "reason":"paid_review_profile_or_task_mismatch"}
            candidate = CandidateIdentity.from_dict(request.readback["candidate"])
            if review.get("candidate_identity") != candidate.to_dict() or native.get("run_id") == candidate.originating_run_id:
                return {"outcome":"held", "reason":"paid_review_candidate_or_run_mismatch"}
            reader = getattr(self.board, "read_scoped_run", None)
            run = reader(self.scope, native.get("task_id"), native.get("run_id")) if callable(reader) else None
            card = self.board.read_task(native.get("task_id"))
            observed_session = self._worker_session(run) if isinstance(run, Mapping) else None
            if (not isinstance(run, Mapping) or str(run.get("id")) != native.get("run_id") or run.get("profile") != paid
                    or run.get("status") not in {"completed", "done", "running"} or not isinstance(card, BoardSnapshot)
                    or card.native_task.get("id") != native.get("task_id") or card.native_task.get("assignee") != paid
                    or not isinstance(native.get("session_id"), str) or not native.get("session_id")
                    or observed_session != native.get("session_id")):
                return {"outcome":"held", "reason":"trusted_paid_review_run_readback_missing"}
            if review.get("verdict") == "approved" and (run.get("status") not in {"completed", "done"} or card.native_task.get("status") != "done"):
                return {"outcome":"held", "reason":"paid_approval_not_terminal"}
            if review.get("verdict") == "changes_requested" and run.get("status") != "running":
                return {"outcome":"held", "reason":"paid_changes_request_not_owned_by_active_reviewer"}
            self.store.record_review(self.scope, candidate, review)
            return {"outcome":review["verdict"], "review_id":review.get("review_id"), "head_sha":head}

    def release_planner(self, *, request_id: str | None = None,
                        planning_profile: str | None = None) -> Mapping[str, Any]:
        """Reserve paid capacity, then release one exact managed held planner.

        This seam stops at native ready state; worker-run registration and
        paid-release binding are intentionally not integrated here.
        """
        from .planning_coordinator import request_from_payload
        profile = self._planning_roles()
        if planning_profile is not None and planning_profile != profile:
            raise ValueError("requested planner profile does not match configured planner role")
        if self.planning_profile is not None and self.planning_profile != profile:
            raise ValueError("configured planner profile changed")
        if self.planning_observer is None or not isinstance(self.budget_policy, BudgetPolicy):
            raise ValueError("trusted planning observer and explicit finite budgets are required")
        workspace = self.planning_workspace
        if (not isinstance(workspace, str) or not os.path.isabs(workspace)
                or os.path.realpath(workspace) != workspace or not os.path.isdir(workspace)
                or os.path.islink(workspace)):
            raise ValueError("trusted persistent planning workspace must be an existing canonical absolute directory")
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            pause = state["operator_intent"]
            if pause is not None and pause.active:
                raise ValueError("planner release is fenced by active pause/cancellation")
            observed = self.planning_observer(dict(self.scope))
            if not isinstance(observed, Mapping) or set(observed) != {"request"}:
                raise ValueError("trusted planning observer returned malformed observation")
            request = request_from_payload(observed["request"])
            if request.board_id != self.scope["board_id"] or request.anchor_id != self.scope["anchor_task_id"]:
                raise ValueError("trusted planning request has wrong scope")
            self._require_enrolled_held_anchor(state)
            identity = request.identity
            if request_id is not None and (type(request_id) is not str or not request_id.strip() or len(request_id) > 256):
                raise ValueError("request_id must be a non-empty string of at most 256 characters")
            creates = [op for op in state["operations"] if op.effect == "create_held" and op.phase == "applied"
                       and op.target.get("request_identity") == identity
                       and op.target.get("task_id") == self.scope["anchor_task_id"]
                       and op.target.get("request_id") == request_id]
            members = [m for m in state["members"] if m.role == "planner" and m.work_association == identity]
            if len(creates) != 1 or len(members) != 1:
                raise ValueError("exact held planner creation proof and request association are required")
            create, member = creates[0], members[0]
            proof = self._snapshot_from_readback(create.readback)
            if proof is None or proof.native_task.get("id") != member.task_id:
                raise ValueError("planner creation proof readback is invalid")
            marker, body = create.target.get("planner_marker"), create.target.get("create_body")
            current = self.board.read_task(member.task_id)
            canonical = json.dumps({"scope": self.scope, "task_id": member.task_id, "request_identity": identity,
                                    "generation": member.generation, "create_key": create.key, "action": "release_planner"},
                                   sort_keys=True, separators=(",", ":"))
            key = "planner-release:" + hashlib.sha256(canonical.encode()).hexdigest()
            prior_release = next((op for op in state["operations"] if op.key == key and op.effect == "release"), None)
            if prior_release is not None and prior_release.phase == "applied":
                released_proof = self._snapshot_from_readback(prior_release.readback)
                if (released_proof is None or current.digest != released_proof.digest
                        or current.native_task.get("id") != member.task_id
                        or current.native_task.get("status") not in {"ready", "todo"}
                        or not any(item.get("body") == f"UNBLOCK: {self.board._native_marker(self._action_from_intent(prior_release))}"
                                   for item in current.comments)):
                    raise ValueError("applied release readback no longer matches exact native state")
                return {"outcome": "released", "task_id": member.task_id, "operation_key": key, "actions_attempted": 0}
            if (prior_release is None and (current.digest != proof.digest or current.native_task.get("status") != "blocked"
                    or current.native_task.get("assignee") != profile or current.parents or current.runs
                    or self.board._workspace_routing(current.native_task, f"dir:{workspace}") is not None
                    or not isinstance(marker, Mapping) or marker.get("request_identity") != identity
                    or marker.get("profile") != profile or marker.get("workspace") != workspace
                    or not isinstance(body, str) or body not in current.native_task.get("body", ""))):
                raise ValueError("planner card differs from exact held creation readback")
            action = (self._action_from_intent(prior_release) if prior_release is not None else
                      Action(key, self.scope, {"task_id": member.task_id, "request_id": identity,
                                               "member_generation": member.generation, "profile": profile},
                             "release", current.digest))
            event = {"event_id": f"{PAID_CAPACITY}:{key}", "lineage_id": f"{self.scope['anchor_task_id']}:{GENERAL_ATTEMPT}",
                     "root_task_id": self.scope["anchor_task_id"], "finding_id": GENERAL_ATTEMPT,
                     "generation": member.generation, "source_task_id": member.task_id,
                     "source_kind": "native_operation", "native_source_id": key, "count": 1}
            stored = self.store.reserve_paid_release(self.scope, self._intent_for(action), event,
                                                     policy_limit=self.budget_policy.paid_capacity)
            if stored.phase == "applied":
                latest = self.board.read_task(member.task_id)
                if (latest.native_task.get("status") not in {"ready", "todo"}
                        or self.board._native_marker(action) not in json.dumps(latest.comments)):
                    raise ValueError("applied release lacks matching exact native marker/state")
                return {"outcome": "released", "task_id": member.task_id, "operation_key": key, "actions_attempted": 0}
            if stored.phase == "unknown":
                verifier = getattr(self.board, "verify_effect", None)
                result = verifier(action) if callable(verifier) else ActionResult(key, "unsupported", "marker verifier unavailable", None)
            else:
                self.store.begin_effect_attempt(self.scope, key)
                try:
                    result = self.board.release(action, member.task_id, f"local-first planner release {self.board._native_marker(action)}")
                except Exception as error:
                    result = ActionResult(key, "unknown", f"native release exception: {error}", None)
            readback = self._portable_readback(result.readback)
            self.store.record_effect_observation(self.scope, key, outcome=result.outcome, details=result.details, readback=readback)
            if result.outcome not in {"verified", "no-op"} or not self._readback_proves(action, result):
                return {"outcome": "partial", "reason": result.details, "task_id": member.task_id,
                        "operation_key": key, "actions_attempted": 0 if stored.phase == "unknown" else 1}
            assert readback is not None
            self.store.ack_effect(self.scope, key, readback=readback, outcome=result.outcome)
            return {"outcome": "released", "task_id": member.task_id, "operation_key": key,
                    "actions_attempted": 0 if stored.phase == "unknown" else 1}

    def register_planning_request(self, planner_task_id: str, *, request_id: str | None = None) -> Mapping[str, Any]:
        """Register only the canonical request freshly observed by trusted root."""
        from .planning_coordinator import request_from_payload
        from .decomposition_planner import request_payload
        planner_profile = self._planning_roles()
        if self.planning_observer is None:
            raise ValueError("trusted planning observer is not configured")
        if not isinstance(planner_task_id, str) or not planner_task_id:
            raise ValueError("planner task ID is required")
        with self.lock:
            self._assert_lock()
            pause = self.store.read_scope(self.scope)["operator_intent"]
            if pause is not None and pause.active:
                raise ValueError("planning registration is fenced by active pause/cancellation")
            members = [m for m in self._members() if m.task_id == planner_task_id and m.role == "planner"]
            if len(members) != 1:
                raise ValueError("planner task must be an exact managed planner member")
            env_task, run_id, session = (os.environ.get("HERMES_KANBAN_TASK"), os.environ.get("HERMES_KANBAN_RUN_ID"), os.environ.get("HERMES_SESSION_ID"))
            if env_task != planner_task_id or not run_id or not session:
                raise ValueError("planner registration requires the active worker-owned task, run, and session")
            run = self.board.read_scoped_run(self.scope, planner_task_id, run_id) if hasattr(self.board, "read_scoped_run") else None
            if not isinstance(run, Mapping):
                raise ValueError("exact native planner run observation is unavailable")
            self._exact_run(run, run_id=run_id, profile=planner_profile)
            if run.get("task_id", planner_task_id) != planner_task_id or run.get("status") not in {"running", "active"}:
                raise ValueError("planner registration requires the exact currently running native task")
            native_session = self._worker_session(run)
            if native_session is not None and native_session != session:
                raise ValueError("active worker session does not match native run receipt")
            observed = self.planning_observer(self.scope)
            if not isinstance(observed, Mapping) or set(observed) != {"request"}:
                raise ValueError("trusted planning observer returned malformed observation")
            request = request_from_payload(observed["request"])
            if request.board_id != self.scope["board_id"] or request.anchor_id != self.scope["anchor_task_id"]:
                raise ValueError("observed planning request has wrong scope")
            self._bind_planner_run(planner_task_id, run_id, session, planner_profile, request.identity, run)
            return self.store.register_planning_request(self.scope, request, planner_task_id, planner_profile, request_id=request_id)

    def _bind_planner_run(self, task_id: str, run_id: str, session: str, profile: str,
                          request_identity: str, run: Mapping[str, Any]) -> Mapping[str, Any]:
        """Bind a live worker run only to its exact applied paid release proof."""
        member = next((item for item in self._members() if item.task_id == task_id and item.role == "planner"), None)
        if member is None or member.work_association != request_identity:
            raise ValueError("planner run is not bound to the current managed request association")
        state = self.store.read_scope(self.scope)
        releases = [op for op in state["operations"] if op.effect == "release"
                    and op.phase == "applied" and op.outcome in {"verified", "no-op"}
                    and op.target == {"task_id": task_id, "request_id": request_identity,
                                      "member_generation": member.generation, "profile": profile}]
        if len(releases) != 1:
            raise ValueError("exact applied paid planner release proof is required before run binding")
        release = releases[0]
        proof = self._snapshot_from_readback(release.readback)
        if (proof is None or proof.native_task.get("id") != task_id
                or proof.native_task.get("status") not in {"ready", "todo"}
                or proof.native_task.get("assignee") != profile
                or not any(item.get("body") == f"UNBLOCK: {self.board._native_marker(self._action_from_intent(release))}"
                           for item in proof.comments)):
            raise ValueError("applied release does not contain an exact canonical native readback")
        current = self.board.read_task(task_id)
        current_runs = [item for item in current.runs if str(item.get("id")) == run_id]
        if (current.native_task.get("id") != task_id or current.native_task.get("assignee") != profile
                or len(current_runs) != 1 or current_runs[0].get("profile") != profile
                or current_runs[0].get("status") not in {"running", "active"}):
            raise ValueError("current native planner task/run attribution is not exact")
        binder = getattr(self.store, "bind_paid_release_to_native_run", None)
        if not callable(binder):
            raise ValueError("evidence store cannot bind a paid release to a native run")
        bound_run = dict(run)
        # The scoped reader authenticates task/run attribution but Hermes v0.21
        # omits task_id from its per-run payload and may encode the numeric ID.
        # Persist the already-verified request identities in canonical form.
        bound_run.update(id=run_id, task_id=task_id, profile=profile)
        binding = binder(self.scope, release.key, task_id, member.generation, profile, run_id, session, bound_run)
        if not isinstance(binding, Mapping):
            raise ValueError("evidence store returned malformed paid release binding")
        return binding

    def submit_plan(self, proposal_json: str, *, request_id: str | None = None) -> Mapping[str, Any]:
        """Accept evidence from the active native planner run; never creates work."""
        from .decomposition_planner import parse_proposal, request_payload
        from .planning_coordinator import request_from_payload, evidence_payload
        planner_profile = self._planning_roles()
        if self.planning_observer is None:
            raise ValueError("trusted planning observer and explicit planning profile are required")
        with self.lock:
            self._assert_lock()
            registration = self.store.read_planning_request(self.scope, request_id=request_id)
            task_id = registration.get("planner_task_id")
            profile = registration.get("planner_profile")
            if profile != planner_profile or not isinstance(task_id, str):
                raise ValueError("stored planner registration is malformed or profile-mismatched")
            member = next((m for m in self._members() if m.task_id == task_id), None)
            if member is None or member.role != "planner":
                raise ValueError("registered planner is not an exact managed member")
            observed = self.planning_observer(self.scope)
            if not isinstance(observed, Mapping) or set(observed) != {"request"}:
                raise ValueError("trusted planning observer returned malformed observation")
            request = request_from_payload(observed["request"])
            registered = request_from_payload(registration.get("request"))
            if request_payload(request) != request_payload(registered):
                raise ValueError("current trusted planning observation is stale")
            env_task, run_id = os.environ.get("HERMES_KANBAN_TASK"), os.environ.get("HERMES_KANBAN_RUN_ID")
            session = os.environ.get("HERMES_SESSION_ID")
            if env_task != task_id or not run_id or not session:
                raise ValueError("submission requires active worker-owned task, run, and session")
            intent = self.store.read_scope(self.scope)["operator_intent"]
            if intent is not None and intent.active:
                raise ValueError("planning submission is fenced by active pause/cancellation")
            run = self.board.read_scoped_run(self.scope, task_id, run_id) if hasattr(self.board, "read_scoped_run") else None
            if not isinstance(run, Mapping):
                raise ValueError("exact native planner run observation is unavailable")
            self._exact_run(run, run_id=run_id, profile=profile)
            if run.get("task_id", task_id) != task_id or run.get("status") not in {"running", "active"}:
                raise ValueError("planner submission requires the exact currently running native task")
            native_session = self._worker_session(run)
            if native_session is not None and native_session != session:
                raise ValueError("active worker session does not match native run receipt")
            self._bind_planner_run(task_id, run_id, session, profile, request.identity, run)
            proposal = parse_proposal(proposal_json, request)
            evidence = evidence_payload(request, proposal, planner_task_id=task_id, planner_run_id=run_id,
                                        planner_session_id=session, planner_profile=profile)
            return self.store.record_plan(self.scope, evidence)

    def accept_validated_plan(self, plan_id: str, *, request_id: str | None = None) -> Mapping[str, Any]:
        """Record bounded automatic acceptance of one fully validated planner proposal.

        This is coordinator-owned local evidence only. It performs no board mutation,
        budget admission, or planner-run binding.
        """
        from .planning_coordinator import (request_from_payload, request_payload,
            ActiveTrancheRoute, first_active_tranche_materialization)
        planner_profile = self._planning_roles()
        if self.planning_observer is None:
            raise ValueError("trusted planning observer is not configured")
        workspace = self.planning_workspace
        if (not isinstance(workspace, str) or not os.path.isabs(workspace)
                or os.path.realpath(workspace) != workspace or not os.path.isdir(workspace)
                or os.path.islink(workspace)):
            raise ValueError("trusted persistent planning workspace must be an existing canonical absolute directory")
        implementation, _reviewer = self._local_review_roles()
        if implementation == planner_profile:
            raise ValueError("implementation and planner roles must differ")
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            pause = state["operator_intent"]
            if pause is not None and pause.active:
                raise ValueError("plan acceptance is fenced by active pause/cancellation")
            registration = self.store.read_planning_request(self.scope, request_id=request_id)
            evidence = self.store.read_plan(self.scope, plan_id)
            request, proposal = __import__("local_first_orchestrator.planning_coordinator", fromlist=["reconstruct_evidence"]).reconstruct_evidence(evidence)
            observed = self.planning_observer(dict(self.scope))
            if not isinstance(observed, Mapping) or set(observed) != {"request"}:
                raise ValueError("trusted planning observer returned malformed observation")
            current_request = request_from_payload(observed["request"])
            registered_request = request_from_payload(registration["request"])
            if (request_payload(current_request) != request_payload(registered_request)
                    or request_payload(current_request) != request_payload(request)):
                raise ValueError("current trusted request, registration, and recorded plan differ")
            if (registration.get("planner_task_id") != evidence["planner"]["task_id"]
                    or registration.get("planner_profile") != planner_profile):
                raise ValueError("recorded planner provenance differs from registered planner")
            route = ActiveTrancheRoute(implementation, workspace)
            first_active_tranche_materialization(evidence, route)
            bindings = []
            for op in state["operations"]:
                if op.effect == "release" and op.phase == "applied" and op.outcome in {"verified", "no-op"}:
                    try:
                        binding = self.store.read_paid_release_run_binding(self.scope, op.key)
                    except KeyError:
                        continue
                    if (binding["task_id"], binding["run_id"], binding["session_id"], binding["profile"]) == (
                        evidence["planner"]["task_id"], evidence["planner"]["run_id"],
                        evidence["planner"]["session_id"], evidence["planner"]["profile"]):
                        bindings.append(binding)
            if len(bindings) != 1:
                raise ValueError("exact immutable paid planner release/run binding is required")
            binding = bindings[0]
            run_reader = getattr(self.board, "read_scoped_run", None)
            current_run = run_reader(self.scope, binding["task_id"], binding["run_id"]) if callable(run_reader) else None
            self._exact_run(current_run, run_id=binding["run_id"], profile=planner_profile)
            if (not isinstance(current_run, Mapping) or type(current_run.get("task_id")) is not str
                    or current_run.get("task_id") != binding["task_id"]
                    or current_run.get("status") not in {"running", "active"}):
                raise ValueError("planner run is no longer active; acceptance is stopped")
            native_session = self._worker_session(current_run)
            if native_session is not None and native_session != binding["session_id"]:
                raise ValueError("current native run session differs from immutable binding")
            current_task = self.board.read_task(binding["task_id"])
            matches = [item for item in current_task.runs if str(item.get("id")) == binding["run_id"]]
            if (type(current_task.native_task.get("id")) is not str
                    or current_task.native_task.get("id") != binding["task_id"]
                    or current_task.native_task.get("assignee") != planner_profile or len(matches) != 1
                    or matches[0].get("profile") != planner_profile
                    or matches[0].get("status") not in {"running", "active"}):
                raise ValueError("current planner task/run is not exact and active")
            return self.store.record_accepted_plan(self.scope, plan_id,
                route={"implementation_profile": implementation, "workspace": workspace}, request_id=request_id)

    def _local_review_roles(self) -> tuple[str, str]:
        """Return immutable operator-selected roles or fail closed for M3 work."""
        roles = self.configured_roles
        two = {"implementation_profile", "local_review_profile"}
        three = two | {"planning_profile"}
        four = three | {"paid_review_profile"}
        paid_only = two | {"paid_review_profile"}
        if set(roles) not in (two, three, paid_only, four):
            raise ValueError("configured implementation and local-review roles are required")
        implementation, reviewer = roles["implementation_profile"], roles["local_review_profile"]
        if (not isinstance(implementation, str) or not implementation
                or not isinstance(reviewer, str) or not reviewer
                or implementation == reviewer):
            raise ValueError("configured implementation and local-review roles must be distinct non-empty profiles")
        return implementation, reviewer

    def _paid_review_role(self) -> str:
        """Return a configured role that is independent of implementation/local review."""
        implementation, local = self._local_review_roles()
        paid = self.configured_roles.get("paid_review_profile")
        if not isinstance(paid, str) or not paid or paid in {implementation, local}:
            raise ValueError("configured distinct paid-review profile is required")
        return paid

    def _planning_roles(self) -> str:
        roles = self.configured_roles
        required = {"implementation_profile", "local_review_profile", "planning_profile"}
        allowed = required | {"paid_review_profile"}
        if set(roles) not in (required, allowed):
            raise ValueError("configured planning role is required")
        implementation, reviewer, planner = (roles[key] for key in
            ("implementation_profile", "local_review_profile", "planning_profile"))
        if any(not isinstance(role, str) or not role for role in (implementation, reviewer, planner)):
            raise ValueError("configured roles must be non-empty profiles")
        if len({implementation, reviewer, planner}) != 3:
            raise ValueError("planning, implementation, and local-review roles must be distinct")
        if "paid_review_profile" in roles:
            paid = roles["paid_review_profile"]
            if not isinstance(paid, str) or not paid or paid in {implementation, reviewer, planner}:
                raise ValueError("configured paid-review profile must be a distinct non-empty profile")
        if self.planning_profile is not None and self.planning_profile != planner:
            raise ValueError("explicit planning profile does not match configured role")
        return planner


    def _assert_native_lock(self, scope: Mapping[str, str], anchor_task_id: str) -> None:
        if validate_scope(scope) != self.scope or anchor_task_id != self.scope["anchor_task_id"]:
            raise ValueError("native mutation lock assertion has wrong scope")
        self.lock.assert_held()

    def _claim_native_create_attempt(self, scope: Mapping[str, str], operation_key: str) -> None:
        """The adapter invokes this immediately before its one native create."""
        if validate_scope(scope) != self.scope:
            raise ValueError("native create attempt has wrong scope")
        self._assert_lock()
        self.store.begin_effect_attempt(self.scope, operation_key)

    def _validate_board(self, board: Any) -> None:
        required = ("read_task", "hold", "release", "stop_run")
        if not all(callable(getattr(board, name, None)) for name in required):
            raise ValueError("board lacks the explicit coordinator protocol")
        # Fixtures must opt in.  Production accepts only the native adapter bound
        # to this exact scope and configured with its native authority callbacks.
        if getattr(board, "is_fake", False) is True:
            return
        from .hermes_board import HermesBoardAdapter
        if not isinstance(board, HermesBoardAdapter):
            raise ValueError("production coordinator requires HermesBoardAdapter")
        if board.board != self.scope["board_id"] or board.anchor_task_id != self.scope["anchor_task_id"]:
            raise ValueError("board adapter scope does not match coordinator scope")
        if not callable(board.managed_member_lookup):
            raise ValueError("native board adapter requires managed member lookup")

    def _assert_lock(self) -> None:
        self.lock.assert_held()

    def _members(self) -> tuple[Any, ...]:
        return self.store.read_scope(self.scope)["members"]

    def _read(self) -> tuple[BoardSnapshot, ...]:
        snapshots: list[BoardSnapshot] = []
        for member in self._members():
            snapshot = self.board.read_task(member.task_id)
            if not isinstance(snapshot, BoardSnapshot):
                raise ValueError("board adapter must return BoardSnapshot")
            snapshots.append(snapshot)
        return tuple(snapshots)

    @staticmethod
    def _state(snapshot: BoardSnapshot) -> str:
        return str(snapshot.native_task.get("status", ""))

    @staticmethod
    def _accepted_piece_raw_capture_matches(readback: Mapping[str, Any] | None,
                                            snapshot: BoardSnapshot) -> bool:
        """Require a raw show capture that is structurally tied to its normalized receipt."""
        if type(readback) is not dict:
            return False
        capture = readback.get("raw_capture_v1")
        if (type(capture) is not dict or set(capture) != {"kind", "show", "runs"}
                or capture.get("kind") != "hermes_kanban_raw_capture_v1"
                or type(capture.get("show")) is not dict or type(capture.get("runs")) is not list):
            return False
        show, runs = capture["show"], capture["runs"]
        if "show_has_runs" in show or "show_runs" in show:
            return False
        expected = Coordinator._plain_link_value(snapshot.to_dict())
        expected_task = expected["native_task"]
        shown_task = show.get("task")
        if (type(shown_task) is dict and "workspace_path" in shown_task
                and "workspace_path" not in expected_task
                and type(expected_task.get("workspace")) is str
                and expected_task["workspace"].startswith("dir:")
                and shown_task["workspace_path"] == expected_task["workspace"][4:]):
            expected_task["workspace_path"] = expected_task["workspace"][4:]
        return (shown_task == expected_task
                and show.get("parents") == [item["id"] for item in expected["parents"]]
                and show.get("children") == []
                and runs == expected["runs"]
                and show.get("comments", []) == expected["comments"]
                and show.get("events", []) == expected["events"]
                and (("attachments" not in show and not expected["attachments"])
                     or show.get("attachments") == expected["attachments"]))

    @staticmethod
    def _snapshot_from_readback(readback: Mapping[str, Any] | None) -> BoardSnapshot | None:
        if not isinstance(readback, Mapping):
            return None
        try:
            snapshot_data = {
                key: list(value) if key in {"parents", "runs", "comments", "events", "attachments"}
                and isinstance(value, (tuple, list)) else value
                for key, value in readback.items() if key != "raw_capture_v1"
            }
            return BoardSnapshot.from_dict(snapshot_data)
        except (TypeError, ValueError, KeyError):
            return None

    def _require_enrolled_held_anchor(self, state: Mapping[str, Any]) -> BoardSnapshot:
        """Require one enrolled root and fresh evidence of its supported native hold.

        Root associations remain opaque: identity is the exact scoped root member,
        not a guessed association literal. A durable hold receipt binds the prior
        supported hold; a fresh exact read prevents stale receipts, ready adoption,
        and live-run races from authorizing planner reservation or release.
        """
        anchor_id = self.scope["anchor_task_id"]
        roots = [member for member in state["members"] if member.role == "root"]
        if len(roots) != 1 or roots[0].task_id != anchor_id:
            raise ValueError("planner requires exactly one enrolled and held anchor root")
        # Read once before considering historical receipts.  A receipt is not
        # authority by itself: only an exact current native hold proof may be
        # selected, so a resumed/re-held anchor does not remain blocked by old
        # history and public acknowledgement cannot forge admission.
        current = self.board.read_task(anchor_id)
        verifier = getattr(self.board, "_verify_native_hold_release", None)
        if not isinstance(current, BoardSnapshot) or not callable(verifier):
            raise ValueError("planner requires production native hold verification")
        candidates = []
        for operation in state["operations"]:
            if (operation.effect != "hold" or operation.phase != "applied"
                    or operation.outcome not in {"verified", "no-op"}
                    or dict(operation.scope) != dict(self.scope)
                    or dict(operation.target) != {"task_id": anchor_id}):
                continue
            action = self._action_from_intent(operation)
            proof = self._snapshot_from_readback(operation.readback)
            if (action.scope != self.scope or action.effect != "hold"
                    or dict(action.target) != {"task_id": anchor_id}
                    or proof is None or proof.native_task.get("id") != anchor_id):
                continue
            proof_content, current_content = proof.to_dict(), current.to_dict()
            proof_content.pop("observed_at", None)
            current_content.pop("observed_at", None)
            # The digest is retained as an independent immutable bound; only
            # the read timestamp is intentionally observational.
            if (proof.digest != current.digest or proof_content != current_content
                    or verifier(action, current) is not None):
                continue
            candidates.append(operation)
        if len(candidates) != 1:
            raise ValueError("planner requires one supported enrolled and held anchor receipt")
        return current

    def status(self) -> dict[str, Any]:
        state, snapshots = self.store.read_scope(self.scope), self._read()
        native_tasks = {str(item.native_task["id"]): item for item in snapshots}
        if self.scope["anchor_task_id"] not in native_tasks:
            anchor = self.board.read_task(self.scope["anchor_task_id"])
            if not isinstance(anchor, BoardSnapshot):
                raise ValueError("board adapter must return BoardSnapshot")
            native_tasks[self.scope["anchor_task_id"]] = anchor
        telemetry = None if self.repository_telemetry is None else self.repository_telemetry(dict(self.scope))
        if telemetry is not None and not isinstance(telemetry, Mapping):
            raise ValueError("trusted repository telemetry must return a mapping")
        # ``read_scope`` deliberately returns immutable domain objects for
        # coordinator use.  Public status is a separate JSON contract: never
        # delegate its serialization to ``default=str`` because that turns a
        # frozen request-review handoff into an opaque Python repr.
        operations = [item.to_dict() for item in state["operations"]]
        handoffs = []
        for operation in operations:
            target = operation["target"]
            if operation["effect"] == "request_review":
                # Provisional evidence is deliberately not reviewer authority.
                handoffs.append({"state": "pending", "operation_key": operation["key"],
                                 "phase": operation["phase"], "task_id": target.get("task_id"),
                                 "reviewer_profile": target.get("reviewer_profile"),
                                 "review_marker": target.get("review_marker"),
                                 "reason": "awaiting_native_request_review_finalization"})
                continue
            frozen = target.get("frozen_handoff") if operation["effect"] == "finalize_request_review" else None
            if not isinstance(frozen, Mapping):
                continue
            candidate_data = frozen.get("candidate")
            if not isinstance(candidate_data, Mapping):
                available = False
            else:
                try:
                    from .contracts import CandidateIdentity
                    candidate = CandidateIdentity.from_dict(candidate_data)
                    synthetic_review = {
                        "checks": frozen.get("checks"), "checks_identity": frozen.get("checks_identity"),
                        "criterion_evidence": [{"criterion_id": item, "outcome": "pass", "evidence": "frozen"}
                                               for item in frozen.get("criterion_ids", ())],
                    }
                    available = self._frozen_candidate(candidate, synthetic_review, frozen)
                except (TypeError, ValueError, KeyError):
                    available = False
            if available:
                handoffs.append({"state": "finalized", "operation_key": target.get("operation_key"),
                                 "phase": operation["phase"], "task_id": target.get("task_id"),
                                 "reviewer_profile": target.get("reviewer_profile"),
                                 "review_marker": target.get("review_marker"),
                                 "frozen_handoff": frozen})
            else:
                handoffs.append({"state": "unavailable", "operation_key": operation["key"],
                                 "error": {"code": "invalid_frozen_local_review_handoff"}})
        public = {
            "scope": dict(state["scope"]),
            "members": [item.to_dict() for item in state["members"]],
            "candidates": [item.to_dict() for item in state["candidates"]],
            "reviews": list(state["reviews"]), "operations": operations,
            "effect_observations": list(state["effect_observations"]),
            "budget_events": list(state["budget_events"]),
            "budget_reconciliations": list(state["budget_reconciliations"]),
            "budget_net": list(state["budget_net"]),
            "operator_intent": None if state["operator_intent"] is None else state["operator_intent"].to_dict(),
            "native_tasks": {task_id: snapshot.to_dict() for task_id, snapshot in native_tasks.items()},
            "native_runs": [run for snapshot in native_tasks.values() for run in snapshot.to_dict()["runs"]],
            "repository_telemetry": None if telemetry is None else dict(telemetry),
            "review_handoffs": handoffs,
        }
        try:
            return json.loads(json.dumps(public, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError) as error:
            raise ValueError("public status contains non-JSON evidence") from error

    @staticmethod
    def _native_workspace(worktree: str) -> str:
        if not isinstance(worktree, str) or not worktree:
            raise ValueError("candidate worktree is required")
        if worktree == "scratch" or worktree.startswith(("dir:", "worktree:")):
            return worktree
        return f"dir:{worktree}"

    def _intent_for(self, action: Action) -> OperationIntent:
        return OperationIntent(action.key, action.scope, action.target, action.effect,
            action.expected_observed_identity,
            {"task_id": action.target.get("task_id"), "before_digest": action.expected_observed_identity},
            None, None, {"reconcile_only": True}, "pending")

    def _readback_proves(self, action: Action, result: ActionResult) -> bool:
        snapshot = self._snapshot_from_readback(result.readback)
        if snapshot is None:
            return False
        if action.effect != "create_held" and snapshot.native_task.get("id") != action.target.get("task_id"):
            return False
        if action.effect == "comment":
            marker, author = action.target.get("marker"), action.target.get("author")
            if (not isinstance(marker, str) or not marker
                    or not isinstance(author, str) or not author
                    or f"<!-- local-first-action:{action.key} -->" not in marker):
                return False
            # A task lane is not comment evidence.  The public readback must
            # bind this action's exact body and author to its exact task, with
            # one (not merely at-least-one) matching native comment.
            return sum(item.get("author") == author and item.get("body") == marker
                       for item in snapshot.comments) == 1
        status = self._state(snapshot)
        if action.effect == "hold":
            return status == "blocked" and not any(run.get("status") in {"active", "claimed", "running", "stopping"} for run in snapshot.runs)
        if action.effect == "release":
            return status in {"ready", "todo"}
        if action.effect == "stop_run":
            run_id = action.target.get("run_id")
            return isinstance(run_id, str) and any(run.get("id") == run_id and run.get("status") in {"cancelled", "completed", "done", "stopped"} for run in snapshot.runs)
        if action.effect == "create_held":
            return (status == "blocked" and not snapshot.parents
                    and not any(run.get("status") in {"active", "claimed", "running", "stopping"} for run in snapshot.runs))
        return False

    def _portable_readback(self, readback: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        """Detach immutable contract mappings before passing evidence to SQLite."""
        snapshot = self._snapshot_from_readback(readback)
        if snapshot is None:
            return None
        detached = snapshot.to_dict()
        if isinstance(readback, Mapping) and isinstance(readback.get("raw_capture_v1"), Mapping):
            detached["raw_capture_v1"] = self._plain_link_value(readback["raw_capture_v1"])
        return detached

    def _apply(self, action: Action) -> ActionResult:
        """Fence every durable write and acknowledge only an exact readback."""
        self._assert_lock()
        if action.effect == "stop_run":
            task_id, run_id = action.target.get("task_id"), action.target.get("run_id")
            if isinstance(task_id, str) and isinstance(run_id, str):
                observed = self.board.read_task(task_id)
                matching = next((run for run in observed.runs if run.get("id") == run_id), None)
                if (observed.digest != action.expected_observed_identity or matching is None
                        or matching.get("stop_supported") in {False, "false"}):
                    return ActionResult(action.key, "unsupported", "exact native stop capability is unavailable", observed.to_dict())
        try:
            stored = self.store.reserve_operation(self._intent_for(action))
        except Exception as error:
            return ActionResult(action.key, "conflict", str(error), None)
        if stored.phase == "unknown":
            return ActionResult(action.key, "unknown", "durable unknown effect requires read-only reconciliation", None)
        if stored.phase == "applied":
            return ActionResult(action.key, stored.outcome or "verified", "effect already durably acknowledged", stored.readback)
        try:
            self._assert_lock()
            self.store.begin_effect_attempt(self.scope, action.key)
            self._assert_lock()
            task_id = action.target.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                result = ActionResult(action.key, "conflict", "exact task target is required", None)
            elif action.effect == "hold": result = self.board.hold(action, task_id, "local-first operator containment")
            elif action.effect == "release": result = self.board.release(action, task_id, "local-first explicit resume")
            elif action.effect == "stop_run" and isinstance(action.target.get("run_id"), str): result = self.board.stop_run(action, task_id, action.target["run_id"], "local-first operator containment")
            elif action.effect == "comment":
                marker, author = action.target.get("marker"), action.target.get("author")
                if (not isinstance(marker, str) or not marker
                        or not isinstance(author, str) or author != NATIVE_COMMENT_AUTHOR
                        or f"<!-- local-first-action:{action.key} -->" not in marker):
                    result = ActionResult(action.key, "conflict", "comment requires exact native author and stable action marker", None)
                else:
                    comment = getattr(self.board, "comment", None)
                    result = (comment(action, task_id, marker) if callable(comment) else
                              ActionResult(action.key, "unsupported", "board does not support exact native comments", None))
            else: result = ActionResult(action.key, "unsupported", "exact supported effect target is required", None)
        except Exception as error:
            result = ActionResult(action.key, "unknown", f"native effect exception: {error}", None)
        readback = self._portable_readback(result.readback)
        self._assert_lock()
        self.store.record_effect_observation(self.scope, action.key, outcome=result.outcome, details=result.details, readback=readback)
        if result.outcome in {"verified", "no-op"} and self._readback_proves(action, result):
            assert readback is not None
            self._assert_lock()
            self.store.ack_effect(self.scope, action.key, readback=readback, outcome=result.outcome)
        return result

    @staticmethod
    def _action_from_intent(intent: OperationIntent) -> Action:
        return Action(intent.key, intent.scope, intent.target, intent.effect, intent.expected_observed_identity)

    @staticmethod
    def _pause_hold_generation(intent: PauseIntent) -> int | None:
        """Return the pause cycle whose holds a phase may consume."""
        if intent.resuming:
            return intent.generation - 1 if intent.generation > 0 else None
        return intent.generation

    @classmethod
    def _matches_current_pause_hold(cls, intent: OperationIntent, pause_intent: PauseIntent) -> bool:
        """Match only the hold receipt authorized by this persisted pause cycle."""
        task_id = intent.target.get("task_id")
        if intent.effect != "hold" or not isinstance(task_id, str):
            return False
        baseline = pause_intent.baseline_digests.get(task_id)
        if intent.expected_observed_identity != baseline:
            return False
        generation = cls._pause_hold_generation(pause_intent)
        if generation is None:
            return False
        versioned = f"hold:{task_id}:{baseline}:pause:{generation}"
        legacy = f"hold:{task_id}:{baseline}"
        # Only generation-zero journals may use the pre-generation key.  A
        # recurring digest from an older pause must not authorize this cycle.
        return intent.key == versioned or (generation == 0 and intent.key == legacy)

    def _verified_applied_digests(self, state: Mapping[str, Any], *,
                                  pause_intent: PauseIntent | None = None) -> dict[str, str]:
        """Return exact native digests, including a verified partial hold.

        A blocked task can still have an active worker, so that hold cannot ack as
        full containment.  Its immutable verified readback nevertheless explains
        the native digest and must not be mistaken for a human edit.
        """
        digests: dict[str, str] = {}
        operations = {intent.key: intent for intent in state["operations"]}

        def belongs_to_active_pause(intent: OperationIntent) -> bool:
            if pause_intent is None:
                return True
            return self._matches_current_pause_hold(intent, pause_intent)

        for intent in operations.values():
            if not belongs_to_active_pause(intent):
                continue
            snapshot = self._snapshot_from_readback(intent.readback)
            task_id = intent.target.get("task_id")
            if intent.phase == "applied" and snapshot is not None and isinstance(task_id, str) and snapshot.native_task.get("id") == task_id:
                digests[task_id] = snapshot.digest
        for observation in state["effect_observations"]:
            intent = operations.get(observation.get("operation_key"))
            snapshot = self._snapshot_from_readback(observation.get("readback"))
            if (intent is not None and belongs_to_active_pause(intent) and observation.get("outcome") in {"verified", "no-op"}
                    and snapshot is not None and snapshot.native_task.get("id") == intent.target.get("task_id")
                    and self._state(snapshot) == "blocked"):
                digests[str(intent.target["task_id"])] = snapshot.digest
        return digests

    def _unresolved_action(self, action: Action) -> bool:
        """Block only ambiguous effects; a pending reservation has never claimed a send."""
        return any(
            intent.effect == action.effect and dict(intent.target) == dict(action.target)
            for intent in self.store.pending_operations(self.scope) if intent.phase == "unknown"
        )

    def _next_unresolved_safe_action(self, actions: tuple[Action, ...]) -> Action | None:
        return next((action for action in actions if not self._unresolved_action(action)), None)

    def _resuming_phase_is_exact(self, state: Mapping[str, Any], snapshots: tuple[BoardSnapshot, ...],
                                  intent: PauseIntent) -> bool:
        """Accept only the persisted held/released mixture during a release phase."""
        members = {member.task_id for member in state["members"]
                   if member.board_id == self.scope["board_id"] and member.anchor_task_id == self.scope["anchor_task_id"]}
        if not members or set(intent.managed_task_ids) != members or set(intent.baseline_digests) != members:
            return False
        if set(intent.resuming_task_ids) != members or set(intent.resuming_action_keys) != members:
            return False
        observed = {str(snapshot.native_task.get("id")): snapshot for snapshot in snapshots}
        releases = {operation.key: operation for operation in state["operations"]
                    if operation.effect == "release" and operation.phase == "applied"}
        held_digests = {
            str(operation.target["task_id"]): proof.digest
            for operation in state["operations"]
            if operation.phase == "applied" and self._matches_current_pause_hold(operation, intent)
            and (proof := self._snapshot_from_readback(operation.readback)) is not None
            and proof.native_task.get("id") == operation.target.get("task_id")
            and self._state(proof) == "blocked"
        }
        for task_id in members:
            snapshot = observed.get(task_id)
            if snapshot is None or any(
                not isinstance(run.get("status"), str) or run.get("status") not in {
                    "active", "claimed", "running", "stopping", "cancelled", "completed", "done", "stopped", "blocked"
                }
                for run in snapshot.runs
            ) or any(run.get("status") in {"active", "claimed", "running", "stopping"} for run in snapshot.runs):
                return False
            status = self._state(snapshot)
            key = intent.resuming_action_keys[task_id]
            operation = releases.get(key)
            if status == "blocked":
                if operation is not None or snapshot.digest != held_digests.get(task_id, intent.baseline_digests[task_id]):
                    return False
                continue
            if status not in {"ready", "todo"} or operation is None or dict(operation.target) != {"task_id": task_id}:
                return False
            proof = self._snapshot_from_readback(operation.readback)
            if proof is None or proof.native_task.get("id") != task_id or proof.digest != snapshot.digest:
                return False
        return True

    def _reserved_correction_release(self, state: Mapping[str, Any], task_id: str) -> bool:
        """A persisted correction reservation authorizes its later native release.

        Admission is the budget-consuming decision.  A pause must not convert
        an already-reserved final correction into stranded blocked work merely
        because that reservation exhausted the root limit.
        """
        for operation in state["operations"]:
            if operation.effect != "create_held" or operation.phase != "applied":
                continue
            target = operation.target
            proof = self._snapshot_from_readback(operation.readback)
            if (target.get("correction_of") is None or proof is None
                    or proof.native_task.get("id") != task_id):
                continue
            if any(event.get("event_id") == f"{REVIEW_CORRECTIONS}:{operation.key}"
                   and event.get("finding_id") == GENERAL_ATTEMPT
                   for event in state["budget_events"]):
                return True
        return False

    def _resume_budget_exhausted(self, state: Mapping[str, Any], *, release_task_id: str | None = None) -> bool:
        """Gate new repairs, but not execution of an already-reserved correction."""
        if release_task_id is not None and self._reserved_correction_release(state, release_task_id):
            return False
        if self.budget_policy is None:
            return False
        finding_ids = {GENERAL_ATTEMPT}
        finding_ids.update(
            finding_id
            for member in state["members"]
            if member.board_id == self.scope["board_id"] and member.anchor_task_id == self.scope["anchor_task_id"]
            for finding_id in member.finding_ids
        )
        return any(not permit_action(self.budget_policy, self.store, self.scope, WORKFLOW_REPAIRS, finding_id=finding_id)
                   for finding_id in finding_ids)

    def _reconcile_unknown_effects(self) -> None:
        verifier = getattr(self.board, "verify_effect", None)
        for intent in self.store.pending_operations(self.scope):
            action = self._action_from_intent(intent)
            result: ActionResult
            try:
                candidate = verifier(action) if callable(verifier) else ActionResult(action.key, "unsupported", "adapter cannot verify exact native marker", None)
                result = candidate if isinstance(candidate, ActionResult) else ActionResult(action.key, "unknown", "adapter marker verifier returned malformed result", None)
            except Exception as error:
                result = ActionResult(action.key, "unknown", f"read-only marker verification failed: {error}", None)
            readback = self._portable_readback(result.readback)
            self._assert_lock()
            self.store.record_effect_observation(self.scope, action.key, outcome=result.outcome, details=result.details, readback=readback)
            if result.outcome in {"verified", "no-op"} and self._readback_proves(action, result):
                assert readback is not None
                self._assert_lock()
                self.store.ack_effect(self.scope, action.key, readback=readback, outcome=result.outcome)

    def _pause(self, *, stop: bool, cancellation: bool, apply_existing: bool = False,
               already_locked: bool = False) -> dict[str, Any]:
        with (nullcontext() if already_locked else self.lock):
            self._assert_lock()
            state, before = self.store.read_scope(self.scope), self._read()
            prior = state["operator_intent"]
            same_active = bool(prior and prior.active and prior.stop_requested == stop and prior.cancellation_requested == cancellation)
            generation = (0 if prior is None else prior.generation + (0 if same_active else 1))
            planned = plan_pause(self.scope, stop, generation=generation, cancellation=cancellation,
                                 members=state["members"], snapshots=before)
            intent = prior if same_active else planned.intent
            if not same_active:
                self.store.record_pause_cycle_observation(
                    intent, {str(snapshot.native_task["id"]): snapshot.to_dict() for snapshot in before},
                )
                self.store.set_operator_intent(intent)  # must precede any effect
            results: list[ActionResult] = []
            action = self._next_unresolved_safe_action(planned.actions) if (not same_active or apply_existing) else None
            if action is not None:
                results.append(self._apply(action))
            if stop and results and results[0].outcome in {"unsupported", "unknown"}:
                # Exact stop remains unproven.  Escalate only to an available hold;
                # do not replay or describe the unsupported run stop as success.
                fallback = plan_pause(self.scope, False, generation=generation, cancellation=cancellation,
                                      members=state["members"], snapshots=self._read()).actions
                next_fallback = self._next_unresolved_safe_action(fallback)
                if next_fallback is not None:
                    results.append(self._apply(next_fallback))
            after = self._read()
            contained = verify_containment(self.scope, state["members"], after, (), pause_intent=intent,
                                           prior_snapshots=before, effect_results=tuple(results))
            return {"outcome": "verified" if contained.outcome == "verified" else "partial",
                    "operator_intent": intent, "actions_attempted": len(results),
                    "active_workers": contained.report.active_workers, "report": contained.report,
                    "cancellation_requested": cancellation}

    def pause(self, *, stop: bool = False) -> dict[str, Any]: return self._pause(stop=stop, cancellation=False)
    def cancel(self) -> dict[str, Any]: return self._pause(stop=True, cancellation=True)

    def _adopt_paused_metadata_observation_locked(self, state: Mapping[str, Any],
                                                   snapshots: tuple[BoardSnapshot, ...],
                                                   intent: PauseIntent) -> dict[str, Any] | None:
        """Receipt one exact, complete-snapshot title-only paused edit.

        This is an observation, never an implicit clear.  The prior native
        snapshots must be retained in verified applied receipts; a digest alone
        cannot establish that an edit did not change topology or lifecycle data.
        """
        plan_ids = {operation.target.get("plan_id") for operation in state["operations"]
                    if operation.effect == "accept_validated_plan" and operation.phase == "applied"
                    and isinstance(operation.target.get("plan_id"), str)}
        if len(plan_ids) != 1:
            return None
        try:
            accepted = self.store.read_accepted_plan(self.scope, next(iter(plan_ids)))
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(accepted, Mapping):
            return None
        managed = {member.task_id for member in state["members"]}
        observed = {str(snapshot.native_task.get("id")): snapshot for snapshot in snapshots}
        if not managed or set(observed) != managed:
            return None
        if any(self._state(snapshot) != "blocked" or any(
                run.get("status") in {"active", "claimed", "running", "stopping"}
                or not isinstance(run.get("status"), str)
                for run in snapshot.runs
        ) for snapshot in snapshots):
            return None
        verified = self._verified_applied_digests(state, pause_intent=intent)
        prior: dict[str, BoardSnapshot] = {}
        for task_id in sorted(managed):
            expected = verified.get(task_id, intent.baseline_digests.get(task_id))
            current_holds = [operation for operation in state["operations"]
                             if operation.phase == "applied"
                             and self._matches_current_pause_hold(operation, intent)
                             and operation.target.get("task_id") == task_id
                             and operation.expected_observed_identity == intent.baseline_digests.get(task_id)
                             and (snapshot := self._snapshot_from_readback(operation.readback)) is not None
                             and snapshot.native_task.get("id") == task_id and snapshot.digest == expected]
            if not isinstance(expected, str):
                return None
            # A fresh hold must be the sole cycle-qualified receipt.  A member
            # held before this pause instead needs the complete snapshot captured
            # under this lock before the public pause was persisted; an old
            # applied receipt with a recurring digest is never a substitute.
            if current_holds:
                if len(current_holds) != 1:
                    return None
                prior_snapshot = self._snapshot_from_readback(current_holds[0].readback)
                if prior_snapshot is None:
                    return None
            else:
                generation = self._pause_hold_generation(intent)
                cycle = None if generation is None else self.store.read_pause_cycle_observation(intent, generation=generation)
                if cycle is None or (prior_snapshot := cycle.get(task_id)) is None or prior_snapshot.digest != expected:
                    return None
            prior[task_id] = prior_snapshot
        diffs: dict[str, dict[str, Any]] = {}
        for task_id, current in observed.items():
            before = prior[task_id].to_dict()
            after = current.to_dict()
            # Observation time and digest are representations of the native read,
            # not mutable authority.  Every other top-level field must compare.
            before.pop("observed_at"); before.pop("digest")
            after.pop("observed_at"); after.pop("digest")
            before_task, after_task = before.pop("native_task"), after.pop("native_task")
            if before != after or set(before_task) != set(after_task):
                return None
            changed = {field: {"before": before_task[field], "after": after_task[field]}
                       for field in before_task if before_task[field] != after_task[field]}
            # Do not treat a machine body, route, assignee/profile, or any opaque
            # native task field as metadata.  The complete snapshot comparison
            # above makes absence/presence drift a conflict too.
            if changed and (set(changed) != {"title"} or not all(
                    isinstance(value["before"], str) and isinstance(value["after"], str)
                    for value in changed.values())):
                return None
            if changed:
                diffs[task_id] = changed
        if not diffs:
            return None
        receipt = {"kind": "m5_paused_metadata_adoption_v2", "scope": dict(self.scope),
                   "accepted_plan_id": accepted.get("plan_id"),
                   "operator_generation": intent.generation,
                   "prior_observations": {task_id: prior[task_id].to_dict() for task_id in sorted(prior)},
                   "current_observations": {task_id: observed[task_id].to_dict() for task_id in sorted(observed)},
                   "exact_changed_fields": diffs}
        identity = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        self._record_recovery_journal(
            key="recovery-paused-metadata-adoption:" + identity,
            effect="recovery_paused_metadata_adoption", target={"accepted_plan_id": accepted.get("plan_id"),
                "operator_generation": intent.generation}, observed_identity=identity, readback=receipt,
        )
        decision = reconcile_operator_edits(self.scope, state["members"], (), snapshots, (),
                                             review_evidence=state["reviews"], pause_intent=intent)
        return {"outcome": "adopted", "report": decision.report, "actions_attempted": 0,
                "operator_intent": intent}

    def _adopt_paused_dependency_observation_locked(self, state: Mapping[str, Any],
                                                     intent: PauseIntent) -> dict[str, Any] | None:
        """Receipt one human-made, declared missing edge without sending a link.

        This deliberately admits the smallest useful topology repair: a clean
        held active tranche whose accepted DAG has exactly one edge and whose
        full raw before/current envelopes prove that exact native link event.
        Anything pending, unknown, live, extra, removed, or merely normalized is
        left for an operator rather than being treated as a repair.
        """
        from .accepted_dependency_links import validate_observed_multi_edge_transition
        from .planning_coordinator import ActiveTrancheRoute, first_active_tranche_materialization

        plan_ids = {op.target.get("plan_id") for op in state["operations"]
                    if op.effect == "accept_validated_plan" and op.phase == "applied"
                    and isinstance(op.target.get("plan_id"), str)}
        # This is human repair of a clean missing edge, never a way to supersede
        # a coordinator reservation, unknown claim, or already acknowledged link.
        if len(plan_ids) != 1 or any(op.effect == "link" for op in state["operations"]):
            return None
        plan_id = next(iter(plan_ids))
        try:
            token, source = self.store.read_accepted_plan(self.scope, plan_id), self.store.read_plan(self.scope, plan_id)
            implementation, _ = self._local_review_roles()
            material = first_active_tranche_materialization(source, ActiveTrancheRoute(
                token["route"]["implementation_profile"], token["route"]["workspace"]))
            declared = tuple((dependency, target.ticket_id) for target in material.targets
                             for dependency in target.declared_dependencies)
            if len(declared) != 1 or token["route"].get("implementation_profile") != implementation:
                return None
            by_ticket = {target.ticket_id: target for target in material.targets}
            if len([member for member in state["members"]
                    if member.role == "implementation" and member.generation == 0]) != len(by_ticket):
                return None
            receipts, frozen = {}, {}
            for target in material.targets:
                operations = [op for op in state["operations"] if op.key == target.operation_key
                              and op.effect == "create_held" and op.phase == "applied"]
                if len(operations) != 1:
                    return None
                readback = self._plain_link_value(operations[0].readback)
                capture = readback.get("raw_capture_v1") if type(readback) is dict else None
                if (type(capture) is not dict or set(capture) != {"kind", "show", "runs"}
                        or capture.get("kind") != "hermes_kanban_raw_capture_v1"
                        or type(capture.get("show")) is not dict or type(capture.get("runs")) is not list):
                    return None
                task_id = capture["show"].get("task", {}).get("id")
                if type(task_id) is not str:
                    return None
                raw = {**self._plain_link_value(capture["show"]), "runs": self._plain_link_value(capture["runs"]),
                       "show_has_runs": "runs" in capture["show"],
                       "show_runs": self._plain_link_value(capture["show"].get("runs")) if "runs" in capture["show"] else None}
                receipts[target.ticket_id] = raw
                frozen[task_id] = raw
            if {member.task_id for member in state["members"] if member.role == "implementation"
                and member.generation == 0} != set(frozen):
                return None
            reader = getattr(self.board, "read_accepted_active_tranche_raw_cards", None)
            if not callable(reader):
                return None
            current = self._plain_link_value(reader(self.scope, tuple(sorted(frozen))))
            # The validator proves full raw equality except the one declared
            # topology/event transition.  Its bounded window is the actual
            # existing native event time, never a fabricated coordinator send.
            edge = declared[0]
            target_task = receipts[edge[1]]["task"]["id"]
            events = current.get(target_task, {}).get("events", []) if type(current) is dict else []
            if type(events) is not list or not events or type(events[-1]) is not dict or type(events[-1].get("created_at")) is not int:
                return None
            event_time = events[-1]["created_at"]
            validate_observed_multi_edge_transition(
                trusted_accepted_source=self._plain_link_value(source), trusted_accepted_token=self._plain_link_value(token),
                frozen_create_receipts=receipts, authority=None, prior_cards=frozen, observed_cards=current,
                prior_applied_edges=(), edge=edge, command_window=(event_time, event_time))
        except (KeyError, TypeError, ValueError):
            return None
        affected = tuple(sorted(review.get("review_id") for review in state["reviews"]
                                if isinstance(review.get("review_id"), str)))
        receipt = {"kind": "m5_human_dependency_adoption_v3", "scope": dict(self.scope),
                   "accepted_plan_id": plan_id, "accepted_token": self._plain_link_value(token),
                   "accepted_source": self._plain_link_value(source), "operator_generation": intent.generation,
                   "declared_edge": {"source_ticket_id": edge[0], "target_ticket_id": edge[1],
                                     "source_task_id": receipts[edge[0]]["task"]["id"], "target_task_id": target_task},
                   "before_raw_cards": frozen, "current_raw_cards": current,
                   "invalidated_review_ids": affected}
        receipt["self_hash"] = "sha256:" + hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        identity = receipt["self_hash"]
        self._record_recovery_journal(key="recovery-human-dependency-adoption:" + identity,
            effect="recovery_human_dependency_adoption", target={"plan_id": plan_id, "edge": edge,
                "invalidated_review_ids": affected}, observed_identity=identity, readback=receipt)
        return {"outcome": "adopted", "actions_attempted": 0, "operator_intent": intent,
                "adopted_edge": edge, "invalidated_review_ids": affected}

    @staticmethod
    def _topology_invalidated_review_ids(state: Mapping[str, Any]) -> frozenset[str]:
        """Keep historical reviews visible while excluding them from new authority."""
        return frozenset(review_id for operation in state["operations"]
                         if operation.effect == "recovery_human_dependency_adoption"
                         and operation.phase == "applied"
                         for review_id in operation.target.get("invalidated_review_ids", ())
                         if isinstance(review_id, str))

    def _verified_paused_metadata_adoption_locked(self, state: Mapping[str, Any],
                                                   snapshots: tuple[BoardSnapshot, ...],
                                                   intent: PauseIntent) -> Mapping[str, Any] | None:
        """Return only an adoption receipt that still proves this exact readback."""
        # The immutable journal was produced only by the complete-snapshot
        # validator above.  Consumption rechecks its exact current observation
        # and generation; it must not re-run the writer while deciding resume.
        operations = [operation for operation in state["operations"]
                      if operation.effect in {"recovery_paused_metadata_adoption", "recovery_human_dependency_adoption"}
                      and operation.phase == "applied"]
        current = {str(snapshot.native_task.get("id")): snapshot.to_dict() for snapshot in snapshots}
        for operation in operations:
            receipt = operation.readback
            if (isinstance(receipt, Mapping)
                    and receipt.get("kind") == "m5_human_dependency_adoption_v3"
                    and receipt.get("scope") == self.scope
                    and receipt.get("operator_generation") == intent.generation):
                copied = self._plain_link_value(receipt)
                claimed_hash = copied.pop("self_hash", None)
                if (not isinstance(claimed_hash, str)
                        or claimed_hash != "sha256:" + hashlib.sha256(
                            json.dumps(copied, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()):
                    continue
                edge = copied.get("declared_edge")
                before = copied.get("before_raw_cards")
                recorded = copied.get("current_raw_cards")
                plan_id = copied.get("accepted_plan_id")
                reader = getattr(self.board, "read_accepted_active_tranche_raw_cards", None)
                if not (isinstance(edge, Mapping) and isinstance(before, dict) and isinstance(recorded, dict)
                        and isinstance(plan_id, str) and callable(reader)
                        and set(edge) == {"source_ticket_id", "target_ticket_id", "source_task_id", "target_task_id"}
                        and {edge["source_task_id"], edge["target_task_id"]} <= set(before) == set(recorded)):
                    continue
                try:
                    token = self._plain_link_value(self.store.read_accepted_plan(self.scope, plan_id))
                    source = self._plain_link_value(self.store.read_plan(self.scope, plan_id))
                    if copied.get("accepted_token") != token or copied.get("accepted_source") != source:
                        continue
                    fresh = self._plain_link_value(reader(self.scope, tuple(sorted(recorded))))
                    if fresh != recorded:
                        continue
                    events = fresh[edge["target_task_id"]]["events"]
                    if not isinstance(events, list) or not events or not isinstance(events[-1], dict):
                        continue
                    event_time = events[-1].get("created_at")
                    if not isinstance(event_time, int):
                        continue
                    from .accepted_dependency_links import validate_observed_multi_edge_transition
                    validate_observed_multi_edge_transition(
                        trusted_accepted_source=source, trusted_accepted_token=token,
                        frozen_create_receipts=before, authority=None, prior_cards=before,
                        observed_cards=fresh, prior_applied_edges=(),
                        edge=(edge["source_ticket_id"], edge["target_ticket_id"]),
                        command_window=(event_time, event_time),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                return receipt
            if not isinstance(receipt, Mapping):
                continue
            recorded_current = receipt.get("current_observations")
            exact_current = (
                isinstance(recorded_current, Mapping) and set(recorded_current) == set(current)
                and all((recorded := self._snapshot_from_readback(recorded_current[task_id])) is not None
                        and recorded.to_dict() == current[task_id] for task_id in current)
            )
            if (isinstance(receipt, Mapping)
                    and receipt.get("kind") == "m5_paused_metadata_adoption_v2"
                    and receipt.get("scope") == self.scope
                    and receipt.get("operator_generation") == intent.generation
                    and exact_current):
                return receipt
        return None

    def _reconcile_locked(self) -> dict[str, Any]:
        self._assert_lock()
        pending_before = tuple(self.store.pending_operations(self.scope))
        self._reconcile_unknown_effects()
        state, snapshots = self.store.read_scope(self.scope), self._read()
        # A comment is independent durable evidence, not containment.  When its
        # prior unknown attempt is now exactly proven and acknowledged, report
        # that completed recovery rather than letting unrelated lane containment
        # downgrade it to partial.  Any remaining operation stays fail-closed.
        if (any(operation.effect == "comment" and operation.phase == "unknown" for operation in pending_before)
                and not self.store.pending_operations(self.scope)):
            report = reconcile_operator_edits(
                self.scope, state["members"], (), snapshots, (), review_evidence=state["reviews"],
            ).report
            return {"outcome": "verified", "report": report, "actions_attempted": 0, "operator_intent": state["operator_intent"]}
        intent = state["operator_intent"]
        if intent is not None and intent.resuming:
            exact = self._resuming_phase_is_exact(state, snapshots, intent)
            if not exact:
                decision = reconcile_operator_edits(self.scope, state["members"], (), snapshots, (), review_evidence=state["reviews"], pause_intent=intent)
                return {"outcome": "partial" if self.store.pending_operations(self.scope) else "conflict",
                        "report": decision.report, "actions_attempted": 0, "operator_intent": intent}
            decision = ReconciliationDecision("verified", (), reconcile_operator_edits(
                self.scope, state["members"], (), snapshots, (), review_evidence=state["reviews"], pause_intent=intent,
            ).report)
            return {"outcome": "partial" if self.store.pending_operations(self.scope) else "verified",
                    "report": decision.report, "actions_attempted": 0, "operator_intent": intent}
        if (intent is not None and intent.active and not intent.cancellation_requested
                and not any(operation.phase == "unknown" for operation in self.store.pending_operations(self.scope))
                and native_digest_mismatches(
            self.scope, state["members"], snapshots, intent,
            verified_applied_digests=self._verified_applied_digests(state, pause_intent=intent),
        )):
            adopted = self._adopt_paused_dependency_observation_locked(state, intent)
            if adopted is None:
                adopted = self._adopt_paused_metadata_observation_locked(state, snapshots, intent)
            if adopted is not None:
                return adopted
            decision = reconcile_operator_edits(self.scope, state["members"], (), snapshots, (), review_evidence=state["reviews"], pause_intent=intent)
            return {"outcome": "conflict", "report": decision.report, "actions_attempted": 0, "operator_intent": intent}
        decision = reconcile_operator_edits(self.scope, state["members"], (), snapshots, (), candidate_evidence=(), review_evidence=state["reviews"], pause_intent=intent)
        return {"outcome": "partial" if self.store.pending_operations(self.scope) else decision.outcome,
                "report": decision.report, "actions_attempted": 0, "operator_intent": intent}

    def reconcile(self) -> dict[str, Any]:
        """Use exact marker readback to reconcile; unknown effects are never replayed."""
        with self.lock:
            return self._reconcile_locked()

    def resume(self, *, authorized_clear: bool = False) -> dict[str, Any]:
        if not isinstance(authorized_clear, bool): raise ValueError("authorized_clear must be a boolean")
        with self.lock:
            self._assert_lock()
            reconciliation = self._reconcile_locked()
            state = self.store.read_scope(self.scope)
            intent = state["operator_intent"]
            if intent is None or not intent.active:
                return {"outcome": "held", "reason": "no_active_pause_to_clear", "actions_attempted": 0}
            # A pending reservation has no durable send claim and may continue
            # only through its original public action.  Unknown remains strictly
            # reconciliation-only and is never a resend authorization.
            pending = tuple(self.store.pending_operations(self.scope))
            unknown = any(operation.phase == "unknown" for operation in pending)
            snapshots = self._read()
            # The eventual _apply reservation compares the full immutable action
            # before any native call.  Pending release intents can therefore reach
            # that exact gate; any mismatch remains a zero-send conflict.
            pending_safe = bool(pending) and all(
                operation.phase == "pending" and operation.effect == "release"
                for operation in pending
            )
            adopted_receipt = (self._verified_paused_metadata_adoption_locked(state, snapshots, intent)
                               if not intent.resuming else None)
            reconciliation_report = reconciliation.get("report")
            if reconciliation_report is None and adopted_receipt is not None:
                reconciliation_report = reconcile_operator_edits(
                    self.scope, state["members"], (), snapshots, (), review_evidence=state["reviews"], pause_intent=intent,
                ).report
            safe = (ReconciliationDecision("verified", (), reconciliation_report)
                    if reconciliation_report is not None and (reconciliation["outcome"] in {"verified", "partial"} or adopted_receipt is not None
                    or (pending_safe and not unknown)) else None)
            # Continue a persisted multi-member release without generating fresh
            # action identities.  The stored key is the proof identity.
            if intent.resuming:
                eligible = next((item for item in snapshots if self._state(item) == "blocked" and str(item.native_task["id"]) in state["operator_intent"].resuming_action_keys), None)
                exhausted = eligible is not None and self._resume_budget_exhausted(
                    state, release_task_id=str(eligible.native_task["id"]),
                )
                if unknown or exhausted or (reconciliation["outcome"] != "verified" and not pending_safe):
                    reason = "unknown_effects" if unknown else "budget_exhausted" if exhausted else "unsafe_human_edits"
                    return {"outcome": "held", "reason": reason, "actions_attempted": 0}
                eligible = next((item for item in snapshots if self._state(item) == "blocked" and str(item.native_task["id"]) in state["operator_intent"].resuming_action_keys), None)
                if eligible is not None:
                    task_id = str(eligible.native_task["id"])
                    action = Action(state["operator_intent"].resuming_action_keys[task_id], self.scope, {"task_id": task_id}, "release", eligible.digest)
                    result = self._apply(action)
                    if result.outcome not in {"verified", "no-op"} or not self._readback_proves(action, result):
                        return {"outcome": "partial", "reason": result.details, "actions_attempted": 1}
                    return {"outcome": "partial", "reason": "resuming_release_in_progress", "actions_attempted": 1}
                proofs = tuple(ActionResult(operation.key, "verified", "durable exact release readback", operation.readback)
                               for operation in state["operations"] if operation.phase == "applied" and operation.effect == "release" and operation.readback is not None)
                final = plan_resume(self.scope, pause_intent=state["operator_intent"], reconciliation=safe,
                    operator_authorized_resume=True, members=state["members"], snapshots=snapshots, effect_results=proofs)
                if not final.allowed or final.intent is None or final.intent.active:
                    return {"outcome": "partial", "reason": final.reason, "actions_attempted": 0}
                self._assert_lock()
                self.store.set_operator_intent(final.intent, authorized_clear=True, resume_decision=final)
                return {"outcome": "verified", "reason": "release_verified", "actions_attempted": 0}
            release_candidate = next((item for item in snapshots if self._state(item) == "blocked"), None)
            exhausted = self._resume_budget_exhausted(
                state, release_task_id=None if release_candidate is None else str(release_candidate.native_task["id"]),
            )
            decision = plan_resume(self.scope, pause_intent=state["operator_intent"], reconciliation=safe,
                unknown_effects=unknown, budget_exhausted=exhausted,
                unsafe_human_edits=reconciliation["outcome"] == "conflict" and not pending_safe and adopted_receipt is None,
                operator_authorized_resume=authorized_clear, members=state["members"], snapshots=snapshots)
            # A title-only adoption remains a pause until this explicit call.  Once
            # the ordinary resume plan has passed, bind its release generation to
            # the exact adopted readback rather than the obsolete hold digest.
            if adopted_receipt is not None:
                adopted_baseline = PauseIntent(
                    intent.scope, intent.origin, intent.generation, intent.stop_requested,
                    intent.cancellation_requested, active=True,
                    managed_task_ids=intent.managed_task_ids,
                    baseline_digests={task_id: snapshot.digest for task_id, snapshot in
                                      ((str(item.native_task.get("id")), item) for item in snapshots)},
                )
                decision = plan_resume(
                    self.scope, pause_intent=adopted_baseline, reconciliation=safe,
                    unknown_effects=unknown, budget_exhausted=exhausted,
                    operator_authorized_resume=authorized_clear, members=state["members"], snapshots=snapshots,
                )
            if not decision.allowed or decision.intent is None:
                return {"outcome": "held", "reason": decision.reason, "actions_attempted": 0}
            if decision.intent.resuming:
                self._assert_lock()
                self.store.set_operator_intent(decision.intent)
                action = decision.actions[0] if decision.actions else None
                if action is None:
                    return {"outcome": "partial", "reason": "resuming_release_in_progress", "actions_attempted": 0}
                result = self._apply(action)
                if result.outcome not in {"verified", "no-op"} or not self._readback_proves(action, result):
                    return {"outcome": "partial", "reason": result.details, "actions_attempted": 1}
                return {"outcome": "partial", "reason": "resuming_release_in_progress", "actions_attempted": 1}
            # No managed release plan or exact persisted native proof means no
            # authority to clear, even if legacy pure callers allow an empty scope.
            return {"outcome": "held", "reason": "resume_release_unverified", "actions_attempted": 0}

    def reject_late_result(self, run_id: str, *, run_generation: int | None = None) -> Any:
        return reject_late_result(self.scope, run_id, pause_intent=self.store.read_scope(self.scope)["operator_intent"], run_generation=run_generation)

    def _recover_one_observed_premature_done(self) -> dict[str, Any] | None:
        """At most once per tick, replace a proven done implementation with review.

        The candidate is decoded only from the trusted Git observer and must
        retain the exact native source run required by ``recover_premature_done``.
        This never pauses, adopts, releases, or mutates an unrelated card.
        """
        try:
            _, reviewer = self._local_review_roles()
            observed = self.git_observer(dict(self.scope)) if self.git_observer is not None else None
            from .contracts import CandidateIdentity
            candidate = CandidateIdentity.from_dict(observed["candidate"]) if isinstance(observed, Mapping) else None
        except (KeyError, TypeError, ValueError):
            return None
        if candidate is None or not self._trusted_candidate_observation(candidate):
            return None
        state = self.store.read_scope(self.scope)
        for member in state["members"]:
            if member.role == "implementation":
                source = self.board.read_task(member.task_id)
                if not isinstance(source, BoardSnapshot) or self._state(source) != "done":
                    continue
                if not any(str(run.get("id")) == candidate.originating_run_id for run in source.runs):
                    continue
                # A completed card is only premature when the exact observed
                # candidate has no recorded local approval.  Native done by
                # itself is never proof, but a prior verified verdict must not
                # generate another recovery review on each polling tick.
                if any(saved.get("reviewer_role") == "local"
                       and saved.get("verdict") == "approved"
                       and saved.get("candidate_identity") == candidate.to_dict()
                       and isinstance(saved.get("native_review"), Mapping)
                       and saved["native_review"].get("task_id") == member.task_id
                       for saved in state["reviews"]):
                    continue
                association = f"premature-done:{member.task_id}:{candidate.content_identity}"
                if any(existing.work_association == association for existing in state["members"]):
                    continue
                recovered = self.recover_premature_done(member.task_id, candidate, reviewer_profile=reviewer,
                                                         already_locked=True)
            elif member.role == "local_review":
                reviewed = self.board.read_task(member.task_id)
                if (not isinstance(reviewed, BoardSnapshot) or self._state(reviewed) != "done"
                        or any(saved.get("candidate_identity") == candidate.to_dict() for saved in state["reviews"])):
                    continue
                replacement_association = f"premature-done-replacement:{member.task_id}:{candidate.content_identity}"
                if any(existing.work_association == replacement_association for existing in state["members"]):
                    continue
                create = self._separate_review_intent(member.task_id, candidate, reviewer)
                source_task = None if create is None else create.target.get("source_task_id")
                if not isinstance(source_task, str):
                    continue
                recovered = self.recover_premature_done(source_task, candidate, reviewer_profile=reviewer,
                                                         replacement_for=member.task_id, already_locked=True)
            else:
                continue
            if recovered.get("outcome") == "held" and recovered.get("task_id"):
                return {"outcome": "held", "actions_attempted": 1, "task_id": recovered["task_id"]}
            return {"outcome": "held", "actions_attempted": 0,
                    "reason": recovered.get("reason", "premature_done_recovery_unverified")}
        for saved in state["reviews"]:
            if saved.get("verdict") != "changes_requested" or saved.get("candidate_identity") != candidate.to_dict():
                continue
            native = saved.get("native_review")
            review_task_id = native.get("task_id") if isinstance(native, Mapping) else None
            if not isinstance(review_task_id, str) or self._separate_review_intent(review_task_id, candidate, reviewer) is None:
                continue
            created = self.create_separate_correction(review_task_id, candidate, saved, already_locked=True)
            if created.get("task_id"):
                return {"outcome": "held", "actions_attempted": 1, "task_id": created["task_id"]}
            if created.get("reason") != "correction_already_registered":
                return {"outcome": "held", "actions_attempted": 0, "reason": created.get("reason", "correction_unverified")}
        for member in state["members"]:
            if member.role != "local_review":
                continue
            create = self._separate_review_intent(member.task_id, candidate, reviewer)
            if create is None:
                continue
            current = self.board.read_task(member.task_id)
            if not isinstance(current, BoardSnapshot) or self._state(current) != "blocked":
                continue
            result = self.release_separate_review(member.task_id, candidate, reviewer_profile=reviewer,
                                                  already_locked=True)
            if result.get("outcome") == "released":
                return {"outcome": "released", "actions_attempted": 1, "task_id": member.task_id}
            return {"outcome": "held", "actions_attempted": 0, "reason": result.get("reason", "release_unverified")}
        for member in state["members"]:
            if member.role != "implementation" or not member.work_association.startswith("separate-review-correction:"):
                continue
            before = self.board.read_task(member.task_id)
            if not isinstance(before, BoardSnapshot) or self._state(before) != "blocked":
                continue
            creates = [item for item in self.store.read_scope(self.scope)["operations"]
                       if item.effect == "create_held" and item.phase == "applied"
                       and item.target.get("association") == member.work_association
                       and (proof := self._snapshot_from_readback(item.readback)) is not None
                       and proof.native_task.get("id") == member.task_id]
            if len(creates) != 1 or not self._trusted_candidate_observation(candidate):
                return {"outcome": "held", "actions_attempted": 0, "reason": "correction_release_provenance_missing"}
            action = Action(f"release-correction:{creates[0].key}", self.scope,
                            {"task_id": member.task_id}, "release", before.digest)
            result = self._apply(action)
            if result.outcome not in {"verified", "no-op"} or not self._readback_proves(action, result):
                return {"outcome": "held", "actions_attempted": 1, "reason": "correction_release_unverified"}
            return {"outcome": "released", "actions_attempted": 1, "task_id": member.task_id}
        return None

    def tick(self) -> dict[str, Any]:
        """Acquire the singleton before every read, reconciliation, decision, and effect."""
        try:
            with self.lock:
                self._assert_lock()
                state = self.store.read_scope(self.scope)
                if state["operator_intent"] is None or not state["operator_intent"].active:
                    recovered = self._recover_one_observed_premature_done()
                    if recovered is not None:
                        return recovered
                    return {"outcome": "no-op", "actions_attempted": 0}
                reconciliation = self._reconcile_locked()
                if reconciliation["outcome"] in {"conflict", "unknown"}:
                    return {"outcome": "held", "actions_attempted": 0, "reason": reconciliation["outcome"]}
                if reconciliation["outcome"] == "adopted":
                    return reconciliation
                if state["operator_intent"].resuming:
                    # A normal poll must not re-hold members released by an
                    # explicitly authorized, durable multi-member resume.
                    return {"outcome": "partial", "actions_attempted": 0, "reason": "resuming_requires_explicit_resume"}
                return self._pause(stop=state["operator_intent"].stop_requested,
                                   cancellation=state["operator_intent"].cancellation_requested,
                                   apply_existing=True, already_locked=True)
        except Exception as error:
            return {"outcome": "held", "actions_attempted": 0, "reason": str(error)}

    def _record_recovery_journal(self, *, key: str, effect: str, target: Mapping[str, Any],
                                 observed_identity: str, readback: Mapping[str, Any]) -> bool:
        """Append one immutable, scoped recovery fact without a board mutation.

        Recovery reports and terminal notices are evidence, not a shadow ticket
        state.  Their stable keys make same-process and restart retries converge
        on one journal record before a correction/containment decision is made.
        """
        intent = OperationIntent(
            key, self.scope, dict(target), effect, observed_identity,
            {"kind": "m5_recovery_journal_v1", "scope": dict(self.scope)},
            "verified", dict(readback), {"reconcile_only": True}, "applied",
        )
        before = {operation.key for operation in self.store.read_scope(self.scope)["operations"]}
        self.store.reserve_operation(intent)
        return key not in before

    @staticmethod
    def _recovery_issue_target(issue: RecoveryIssue) -> dict[str, Any]:
        return {
            "kind": issue.kind, "task_id": issue.task_id, "run_id": issue.run_id,
            "finding_id": issue.finding_id, "generation": issue.generation,
            "candidate": None if issue.candidate is None else dict(issue.candidate),
            "details": dict(issue.details), "issue_key": deduplicate_issue(issue),
        }

    def report_issue(self, issue: RecoveryIssue) -> dict[str, Any]:
        """Durably deduplicate a scoped recovery report; reporting has no effect.

        The caller supplies a classified observation, never repair authority.
        Polling performs its own native observation before any supported action.
        """
        if type(issue) is not RecoveryIssue or dict(issue.scope) != self.scope:
            raise ValueError("report requires an exact scoped RecoveryIssue")
        with self.lock:
            self._assert_lock()
            key = "recovery-report:" + deduplicate_issue(issue)
            recorded = self._record_recovery_journal(
                key=key, effect="recovery_report", target=self._recovery_issue_target(issue),
                observed_identity=issue.observed_identity,
                readback={"issue": self._recovery_issue_target(issue), "source": "report"},
            )
            return {"outcome": "recorded" if recorded else "deduplicated", "operation_key": key,
                    "actions_attempted": 0}

    def _record_recovery_escalation(self, issue: RecoveryIssue, *,
                                    preserved_work: tuple[str, ...] | None = None,
                                    active_workers: tuple[str, ...] = (),
                                    attempted_actions: tuple[str, ...] = (),
                                    actions_attempted: int = 0) -> dict[str, Any]:
        """Persist one bounded actionable notice without inventing containment.

        Useful duplicate work and an unproven worker stop are deliberately not
        collapsed to the report's affected task.  The exact preserved cards and
        still-active native runs remain visible to the operator on every retry,
        while the stable notice key prevents alert churn.
        """
        if not isinstance(actions_attempted, int) or isinstance(actions_attempted, bool) or actions_attempted < 0:
            raise ValueError("recovery actions_attempted must be a non-negative integer")
        report = summarize_escalation(
            self.scope, (issue,), attempted_actions=attempted_actions,
            preserved_work=(issue.task_id,) if preserved_work is None else preserved_work,
            active_workers=active_workers,
        )
        key = "recovery-escalation:" + deduplicate_issue(issue)
        recorded = self._record_recovery_journal(
            key=key, effect="recovery_escalation_notice", target=self._recovery_issue_target(issue),
            observed_identity=issue.observed_identity, readback=report.to_dict(),
        )
        return {"outcome": "escalated", "operation_key": key, "new_notice": recorded,
                "actions_attempted": actions_attempted, "report": report}

    def _temporary_recovery_hold_bridge(self, state: Mapping[str, Any], plan_id: str, ticket_id: str,
                                        created: Any, member: Any, current: BoardSnapshot,
                                        expected_parent_ids: set[str]) -> Any | None:
        """Accept only the immutable recovery receipt that replaced this hold.

        A recovery hold is not a general unhold token: it must name this exact
        managed accepted piece, its applied native hold repair, and the fresh
        blocked readback.  Opaque card fields remain untouched and are covered by
        the recovery snapshot digest rather than discarded.
        """
        holds = [op for op in state["operations"] if op.effect == "recovery_automatic_hold"
                 and op.target.get("containment_kind") == "recoverable_original_task"
                 and op.target.get("task_id") == member.task_id]
        if len(holds) != 1:
            return None
        journal = holds[0]
        if journal.target.get("kind") != "dependent_early_ended_worker":
            return None
        repair_key = journal.target.get("repair_operation_key")
        repairs = [op for op in state["operations"] if op.key == repair_key and op.effect == "hold"
                   and op.phase == "applied" and op.target.get("task_id") == member.task_id]
        receipt = self._snapshot_from_readback(journal.readback.get("native_readback"))
        if (len(repairs) != 1 or receipt is None or receipt.native_task.get("id") != member.task_id
                or receipt.digest != current.digest or receipt.native_task.get("status") != "blocked"
                or {str(parent.get("id")) for parent in receipt.parents if isinstance(parent, Mapping)} != expected_parent_ids
                or receipt.runs or current.runs):
            return None
        original = self._snapshot_from_readback(created.readback)
        if original is None or original.native_task.get("id") != member.task_id:
            return None
        try:
            targets = self._accepted_tranche_targets(plan_id)
        except (KeyError, ValueError):
            return None
        accepted = [target for target in targets if target.ticket_id == ticket_id]
        if len(accepted) != 1 or created.key != accepted[0].operation_key:
            return None
        return journal

    def _record_automatic_recovery_hold(self, issue: RecoveryIssue, action: Action,
                                        result: ActionResult) -> None:
        """Record scoped containment without mislabeling redundant work as temporary.

        A duplicate containment hold is permanent until an operator adjudicates
        the canonical/redundant relationship.  The only temporary kind names a
        recoverable original task; it still needs an independently authorized
        release admission and exact current readback before any native unblock.
        """
        if action.effect != "hold":
            return
        containment_kind = ("permanent_redundant_containment"
                            if issue.kind == "duplicate_unstarted"
                            else "recoverable_original_task")
        key = "recovery-automatic-hold:" + deduplicate_issue(issue)
        self._record_recovery_journal(
            key=key, effect="recovery_automatic_hold",
            target={**self._recovery_issue_target(issue), "repair_operation_key": action.key,
                    "task_id": issue.task_id, "containment_kind": containment_kind}, observed_identity=issue.observed_identity,
            readback={"repair_reason": issue.kind, "repair_operation_key": action.key,
                      "containment_kind": containment_kind,
                      "native_readback": dict(self._portable_readback(result.readback) or {})},
        )

    def _clear_automatic_recovery_holds_if_coherent(self) -> Mapping[str, Any] | None:
        """Admit one exact temporary hold through the ordinary release boundary.

        Permanent duplicate containment is deliberately never considered.  The
        only recoverable case is a managed accepted piece whose immutable hold
        journal, accepted-plan reconstruction, fresh blocked receipt, dependency
        topology, scope, route, root, and capacity checks all reach
        ``release_active_piece`` under this poll's mutation lock.
        """
        state = self.store.read_scope(self.scope)
        holds = [op for op in state["operations"] if op.effect == "recovery_automatic_hold"]
        for held in holds:
            kind = held.target.get("containment_kind")
            if kind == "permanent_redundant_containment":
                return {"outcome": "held", "reason": "redundant_duplicate_containment_remains_held_pending_operator_adjudication", "actions_attempted": 0}
            if kind != "recoverable_original_task":
                return {"outcome": "held", "reason": "unsupported_temporary_recovery_hold_receipt", "actions_attempted": 0}
            task_id = held.target.get("task_id")
            creates = [op for op in state["operations"] if op.effect == "create_held" and op.phase == "applied"
                       and self._snapshot_from_readback(op.readback) is not None
                       and self._snapshot_from_readback(op.readback).native_task.get("id") == task_id]
            if len(creates) != 1:
                return {"outcome": "held", "reason": "temporary_recovery_hold_requires_exact_managed_creation", "actions_attempted": 0}
            plan_id, ticket_id = creates[0].target.get("plan_id"), creates[0].target.get("ticket_id")
            if not isinstance(plan_id, str) or not plan_id or not isinstance(ticket_id, str) or not ticket_id:
                return {"outcome": "held", "reason": "temporary_recovery_hold_requires_accepted_plan_ticket", "actions_attempted": 0}
            released = self.release_active_piece(plan_id, ticket_id, _already_locked=True)
            if released.get("outcome") != "released":
                return released
            clear_key = "recovery-automatic-clear:" + held.key
            self._record_recovery_journal(
                key=clear_key, effect="recovery_automatic_clear",
                target={"hold_operation_key": held.key, "release_operation_key": released.get("operation_key"),
                        "task_id": task_id, "plan_id": plan_id, "ticket_id": ticket_id},
                observed_identity=str(released.get("operation_key")), readback=dict(released),
            )
            return released
        return None

    def _continue_current_revision_paid_review(self, issue: RecoveryIssue) -> dict[str, Any] | None:
        """Route stale paid/correction evidence to a fresh current-head check.

        The route stops at a held review request: it never reuses stale findings,
        releases work, or accepts a revision. It is unavailable without trusted
        composition-root Git/check dependencies.
        """
        if issue.kind != "stale_approval" or self.recovery_git_adapter is None:
            return None
        observed = self.git_observer(dict(self.scope)) if self.git_observer is not None else None
        plan_id = observed.get("plan_id") if isinstance(observed, Mapping) else None
        if not isinstance(plan_id, str) or not plan_id or self.combined_check_runner is None:
            return None
        result = self.prepare_paid_integrated_review(plan_id, git_adapter=self.recovery_git_adapter)
        if result.get("outcome") == "held":
            return {"outcome": "continued_current_revision", "plan_id": plan_id,
                    "review_request_key": result.get("review_request_key"),
                    "head_sha": result.get("head_sha"),
                    "actions_attempted": result.get("actions_attempted", 0)}
        return None

    def _poll_issues_locked(self) -> tuple[RecoveryIssue, ...]:
        state = self.store.read_scope(self.scope)
        try:
            snapshots = self._read()
            evidence = {**state, "scope": self.scope,
                        "native_tasks": {str(item.native_task.get("id")): dict(item.native_task) for item in snapshots}}
            git = self.git_observer(dict(self.scope)) if self.git_observer is not None else {}
            if not isinstance(git, Mapping):
                return ()
            found: dict[str, RecoveryIssue] = {}
            for snapshot in snapshots:
                for issue in detect_issues(snapshot, evidence, git):
                    found[deduplicate_issue(issue)] = issue
            return tuple(found[key] for key in sorted(found))
        except (KeyError, TypeError, ValueError):
            return ()

    def poll(self) -> dict[str, Any]:
        """Startup/periodic recovery: inspect first, perform at most one safe repair.

        It never dispatches, releases, accepts, resumes, or creates work from a
        report alone. Missing local-review/verdict observations can only reuse
        the established trusted M3 candidate/run recovery seam. Unknown native
        effects remain in the existing verification-only path; an operator
        pause/cancel remains authoritative and is never auto-cleared.
        """
        try:
            with self.lock:
                self._assert_lock()
                state = self.store.read_scope(self.scope)
                intent = state["operator_intent"]
                if intent is not None and intent.active:
                    # A receipt-proven title-only edit is safe to surface to an
                    # operator from polling, but it remains paused and can never
                    # trigger a recovery repair or implicit release.
                    reconciled = self._reconcile_locked()
                    if reconciled["outcome"] == "adopted":
                        return reconciled
                    return {"outcome": "held", "reason": "operator_intent_active", "actions_attempted": 0}
                self._reconcile_unknown_effects()
                issues = self._poll_issues_locked()
                if not issues:
                    recovered = self._clear_automatic_recovery_holds_if_coherent()
                    if recovered is not None:
                        return recovered
                    return {"outcome": "no-op", "actions_attempted": 0}
                issue = issues[0]
                # Keep report dedup independent from the repair attempt. A
                # missed hook and concurrent reporter therefore converge before
                # any native containment write.
                self._record_recovery_journal(
                    key="recovery-report:" + deduplicate_issue(issue), effect="recovery_report",
                    target=self._recovery_issue_target(issue), observed_identity=issue.observed_identity,
                    readback={"issue": self._recovery_issue_target(issue), "source": "poll"},
                )
                # A report is never authority to choose between two useful
                # worktrees.  Preserve both exact cards and leave the ambiguous
                # portion visibly held for an operator decision.
                if issue.kind == "duplicate_useful_work":
                    canonical = issue.details.get("canonical_task_id")
                    preserved = tuple(dict.fromkeys(item for item in (issue.task_id, canonical)
                                                     if isinstance(item, str) and item))
                    return self._record_recovery_escalation(issue, preserved_work=preserved)

                # These routes reuse the established M3 recovery seam, which
                # independently obtains the trusted Git candidate and native
                # run provenance.  A model report alone cannot reach creation.
                if issue.kind in {"missing_candidate", "missing_local_review", "missing_review_verdict"}:
                    recovered = self._recover_one_observed_premature_done()
                    if recovered is not None:
                        return recovered
                    return self._record_recovery_escalation(issue)

                if issue.kind == "stale_approval":
                    continued = self._continue_current_revision_paid_review(issue)
                    if continued is not None:
                        return continued
                    return self._record_recovery_escalation(issue)

                # A downstream completion/dispatch race may be contained only
                # through an exact native run.  Do not manufacture a stop/kill
                # when the run identity or supported stop capability is absent.
                if issue.kind == "dependent_early_running":
                    current = self.board.read_task(issue.task_id)
                    active = tuple(str(run.get("id")) for run in current.runs
                                   if run.get("status") in {"active", "claimed", "running", "stopping"}
                                   and isinstance(run.get("id"), (str, int)))
                    if issue.run_id is None or str(issue.run_id) not in active:
                        return self._record_recovery_escalation(issue, active_workers=active)
                    matching = next((run for run in current.runs if str(run.get("id")) == issue.run_id), None)
                    if not isinstance(matching, Mapping) or matching.get("stop_supported") is not True:
                        return self._record_recovery_escalation(issue, active_workers=active)
                    action = Action("recovery:" + deduplicate_issue(issue)[:24] + ":stop_run", self.scope,
                                    {"task_id": issue.task_id, "run_id": issue.run_id,
                                     "finding_id": issue.finding_id, "generation": issue.generation},
                                    "stop_run", current.digest)
                elif issue.kind == "dependent_early_ambiguous_worker":
                    current = self.board.read_task(issue.task_id)
                    observed_runs = tuple(str(run.get("id")) for run in current.runs
                                          if isinstance(run.get("id"), (str, int)))
                    return self._record_recovery_escalation(issue, active_workers=observed_runs)
                else:
                    action = propose_repair(issue, {"workflow_recovery": {
                        "limit": 0 if self.budget_policy is None else self.budget_policy.workflow_repairs,
                        "used": 0,
                    }})
                # Unknown operations are already passed through the existing
                # verification-only reconciler above. Stale approvals likewise
                # remain invalid evidence, not a license to fabricate review.
                if action is None or issue.kind not in {"duplicate_unstarted", "dependent_early_running", "dependent_early_ended_worker"}:
                    return self._record_recovery_escalation(issue)
                if self.budget_policy is None:
                    return self._record_recovery_escalation(issue)
                repair_intent = self._intent_for(action)
                event = {
                    "event_id": f"{WORKFLOW_REPAIRS}:{action.key}",
                    "lineage_id": f"{self.scope['anchor_task_id']}:{GENERAL_ATTEMPT}",
                    "root_task_id": self.scope["anchor_task_id"], "finding_id": GENERAL_ATTEMPT,
                    "generation": issue.generation, "source_task_id": issue.task_id,
                    "source_kind": "native_operation", "native_source_id": action.key, "count": 1,
                }
                try:
                    admit_repair_operation(self.budget_policy, self.store, self.scope, repair_intent, event)
                except Exception:
                    return self._record_recovery_escalation(issue)
                result = self._apply(action)
                if result.outcome in {"verified", "no-op"} and self._readback_proves(action, result):
                    self._record_automatic_recovery_hold(issue, action, result)
                    return {"outcome": "repaired", "operation_key": action.key, "actions_attempted": 1,
                            "issue_key": deduplicate_issue(issue)}
                # The failed/ambiguous effect remains durable and any still-live
                # run is reported rather than being called contained.
                active = ()
                if issue.kind == "dependent_early_running":
                    after = self.board.read_task(issue.task_id)
                    active = tuple(str(run.get("id")) for run in after.runs
                                   if run.get("status") in {"active", "claimed", "running", "stopping"}
                                   and isinstance(run.get("id"), (str, int)))
                return self._record_recovery_escalation(
                    issue, active_workers=active, attempted_actions=(action.key,), actions_attempted=1)
        except Exception as error:
            return {"outcome": "held", "actions_attempted": 0, "reason": str(error)}

    def recover(self) -> dict[str, Any]:
        return self.poll()

    def enroll(self) -> dict[str, Any]:
        """Explicitly associate one inactive anchor after a verified native hold.

        Enrollment never adopts arbitrary board work: the configured opaque
        anchor is the only permissible root and it must be non-running both
        before and after the supported hold effect.
        """
        anchor_id = self.scope["anchor_task_id"]
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            roots = [member for member in state["members"] if member.role == "root"]
            if roots:
                if len(roots) == 1 and roots[0].task_id == anchor_id:
                    return {"outcome": "deduplicated", "task_id": anchor_id, "actions_attempted": 0}
                return {"outcome": "conflict", "reason": "scope already has a different root enrollment", "actions_attempted": 0}
            if state["members"]:
                return {"outcome": "conflict", "reason": "scope has managed members without a root", "actions_attempted": 0}
            anchor = self.board.read_task(anchor_id)
            if not isinstance(anchor, BoardSnapshot) or anchor.native_task.get("id") != anchor_id:
                return {"outcome": "conflict", "reason": "fresh exact native anchor is required", "actions_attempted": 0}
            if anchor.native_task.get("status") == "running" or any(
                    run.get("status") in {"active", "claimed", "running", "stopping"} for run in anchor.runs):
                return {"outcome": "held", "reason": "live anchor cannot be enrolled; pause and reconcile first", "actions_attempted": 0}
            action = Action("enroll-hold:" + anchor_id + ":" + anchor.digest, self.scope,
                            {"task_id": anchor_id}, "hold", anchor.digest)
            result = self._apply(action)
            if result.outcome not in {"verified", "no-op"} or not self._readback_proves(action, result):
                return {"outcome": "partial" if result.outcome == "partial" else result.outcome,
                        "reason": result.details, "operation_key": action.key, "actions_attempted": 1}
            proof = self._snapshot_from_readback(result.readback)
            if (proof is None or proof.native_task.get("id") != anchor_id
                    or proof.native_task.get("status") == "running"
                    or any(run.get("status") in {"active", "claimed", "running", "stopping"} for run in proof.runs)):
                return {"outcome": "partial", "reason": "anchor hold readback is incomplete", "operation_key": action.key, "actions_attempted": 1}
            self.store.register_member(ManagedMember(self.scope["board_id"], anchor_id, anchor_id,
                                                      "root", 0, (), "enrolled-anchor"))
            return {"outcome": "verified", "task_id": anchor_id, "operation_key": action.key,
                    "actions_attempted": 1}
    def _managed_task(self, task_id: str) -> None:
        if not isinstance(task_id, str) or task_id not in {member.task_id for member in self._members()}:
            raise ValueError("task must be an exact managed member")

    @staticmethod
    def _exact_run(run: Mapping[str, Any], *, run_id: str, profile: str) -> None:
        if str(run.get("id")) != run_id or run.get("profile") != profile:
            raise ValueError("native run/profile attribution does not match")

    @staticmethod
    def _worker_session(run: Mapping[str, Any]) -> str | None:
        metadata = run.get("metadata")
        session_id = metadata.get("worker_session_id") if isinstance(metadata, Mapping) else None
        return session_id if isinstance(session_id, str) and session_id else None

    def _active_worker_session(self, task_id: str, run_id: str) -> str:
        """Return the immutable public-entry session, never a later env read."""
        return self._current_native_worker_context(task_id=task_id, run_id=run_id).session_id

    def _scoped_run(self, task_id: str, run_id: str) -> Mapping[str, Any]:
        reader = getattr(self.board, "read_scoped_run", None)
        if not callable(reader):
            raise ValueError("board cannot verify exact native run attribution")
        try:
            result = reader(self.scope, task_id, run_id)
        except KeyError as error:
            raise ValueError("native_run_missing") from error
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("native_run_read_unavailable") from error
        if not isinstance(result, Mapping):
            raise ValueError("native_run_attribution_malformed")
        return result

    def _native_implementation_candidate(self, task_id: str, candidate: Any, *,
                                         run_id: str, session_id: str) -> Mapping[str, Any]:
        """Require the candidate producer to be the active native implementation worker."""
        implementation, _reviewer = self._local_review_roles()
        if candidate.originating_run_id != run_id:
            raise ValueError("candidate must originate from the active implementation run")
        run = self._scoped_run(task_id, run_id)
        card = self.board.read_task(task_id)
        if (not isinstance(card, BoardSnapshot) or card.native_task.get("id") != task_id
                or card.native_task.get("assignee") != implementation
                or card.native_task.get("status") not in {"running", "active"}
                or run.get("profile") != implementation or run.get("status") not in {"running", "active", "claimed"}
                or not any(str(item.get("id")) == run_id for item in card.runs)):
            raise ValueError("native_task_run_profile_mismatch")
        reader = getattr(self.board, "read_active_worker_identity", None)
        if callable(reader):
            receipt = reader(self.scope, task_id, run_id, session_id, implementation)
            if not isinstance(receipt, Mapping):
                raise ValueError("native_session_receipt_malformed")
            required = {"version", "task_id", "run_id", "session_id", "profile", "source"}
            if (not required <= set(receipt) or receipt.get("version") != 1
                    or receipt.get("task_id") != task_id or str(receipt.get("run_id")) != run_id
                    or receipt.get("session_id") != session_id or receipt.get("profile") != implementation
                    or receipt.get("source") != "native_run_metadata"):
                raise ValueError("native_session_receipt_malformed")
            return dict(receipt)
        # Synthetic adapters must expose the same native terminal metadata that
        # production eventually stamps.  They cannot stand in for a live native
        # session-store readback.
        if self._worker_session(run) != session_id:
            raise ValueError("native_session_unbound")
        return {"version": 1, "task_id": task_id, "run_id": run_id,
                "session_id": session_id, "profile": implementation,
                "source": "native_run_metadata"}

    def _provisional_implementation_candidate(self, task_id: str, candidate: Any, *,
                                               run_id: str, session_id: str) -> Mapping[str, Any]:
        """Observe the active worker tuple without upgrading its session to authority."""
        implementation, _reviewer = self._local_review_roles()
        if candidate.originating_run_id != run_id:
            raise ValueError("candidate must originate from the active implementation run")
        reader = getattr(self.board, "read_provisional_worker_context", None)
        if callable(reader):
            receipt = reader(self.scope, task_id, run_id, session_id, implementation)
            expected = {"version", "task_id", "run_id", "session_id", "profile", "source"}
            if (not isinstance(receipt, Mapping) or not expected <= set(receipt)
                    or receipt.get("version") != 1 or receipt.get("task_id") != task_id
                    or str(receipt.get("run_id")) != run_id or receipt.get("session_id") != session_id
                    or receipt.get("profile") != implementation
                    or receipt.get("source") != "active_worker_context"):
                raise ValueError("provisional_worker_context_malformed")
            return dict(receipt)
        # Test adapters without the native method still have to prove the exact
        # active task/run/profile; they never establish a session binding.
        run = self._scoped_run(task_id, run_id)
        card = self.board.read_task(task_id)
        if (not isinstance(card, BoardSnapshot) or card.native_task.get("id") != task_id
                or card.native_task.get("assignee") != implementation
                or card.native_task.get("status") not in {"running", "active"}
                or run.get("profile") != implementation
                or run.get("status") not in {"running", "active", "claimed"}
                or not any(str(item.get("id")) == run_id for item in card.runs)):
            raise ValueError("native_task_run_profile_mismatch")
        return {"version": 1, "task_id": task_id, "run_id": run_id,
                "session_id": session_id, "profile": implementation,
                "source": "active_worker_context"}

    def observe_candidate_checks(self) -> dict[str, Any]:
        """Return current evidence using the one public-entry worker snapshot."""
        from .contracts import CandidateIdentity
        context = self._ensure_native_worker_context()
        task_id, run_id = context.task_id, context.run_id
        session_id, board_id = context.session_id, context.board_id
        if board_id != self.scope["board_id"]:
            raise ValueError("candidate observation board differs from configured scope")
        if self.git_observer is None:
            raise ValueError("candidate observation requires configured trusted Git observer")
        observed = self.git_observer(dict(self.scope))
        if not isinstance(observed, Mapping):
            raise ValueError("trusted Git observer returned malformed observation")
        candidate = CandidateIdentity.from_dict(observed.get("candidate", {}))
        self._provisional_implementation_candidate(task_id, candidate, run_id=run_id, session_id=session_id)
        checks = observed.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError("candidate observation requires configured check records")
        return {"task_id": task_id, "run_id": run_id, "session_id": session_id,
                "board_id": board_id, "candidate": candidate.to_dict(), "checks": checks,
                "checks_identity": observed.get("checks_identity"), "criterion_ids": observed.get("criterion_ids")}

    def request_local_review_from_worker(self, *, operation_key: str, summary: str) -> dict[str, Any]:
        """Public worker entrypoint; caller supplies no candidate or provenance."""
        if not isinstance(operation_key, str) or not operation_key:
            raise ValueError("local review operation key must be a non-empty string")
        if not isinstance(summary, str) or not summary:
            raise ValueError("local review summary must be a non-empty string")
        observed = self.observe_candidate_checks()
        from .contracts import CandidateIdentity
        candidate = CandidateIdentity.from_dict(observed["candidate"])
        return self.request_local_review(
            observed["task_id"], candidate,
            implementation_profile=self.configured_roles.get("implementation_profile", ""),
            reviewer_profile=self.configured_roles.get("local_review_profile", ""),
            summary=summary, operation_key=operation_key, _context_captured=True)

    def request_local_review(self, task_id: str, candidate: Any, *, implementation_profile: str,
                             reviewer_profile: str, summary: str, operation_key: str,
                             _context_captured: bool = False) -> dict[str, Any]:
        """Durably propose a handoff which only the implementation worker can send."""
        if not _context_captured:
            self._ensure_native_worker_context()
        from .contracts import CandidateIdentity
        configured_implementation, configured_reviewer = self._local_review_roles()
        if not isinstance(candidate, CandidateIdentity):
            raise ValueError("candidate must be a CandidateIdentity")
        if not all(isinstance(value, str) and value for value in (implementation_profile, reviewer_profile, summary, operation_key)):
            raise ValueError("bounded non-empty handoff values are required")
        if (implementation_profile, reviewer_profile) != (configured_implementation, configured_reviewer):
            raise ValueError("local-review handoff profiles must match configured roles")
        self._managed_task(task_id)
        implementation = self._scoped_run(task_id, candidate.originating_run_id)
        self._exact_run(implementation, run_id=candidate.originating_run_id, profile=implementation_profile)
        if implementation.get("status") != "running":
            raise ValueError("worker-owned implementation run must be active")
        implementation_session = self._active_worker_session(task_id, candidate.originating_run_id)
        self._provisional_implementation_candidate(task_id, candidate,
                                                   run_id=candidate.originating_run_id,
                                                   session_id=implementation_session)
        # Capture complete observer authority while the implementation worker
        # still owns this exact native run.  The immutable intent below is the
        # only source a later reviewer may consume.
        if self.git_observer is None:
            raise ValueError("active implementation candidate/check observation is unavailable")
        try:
            observed = self.git_observer(dict(self.scope))
        except Exception as error:
            raise ValueError("active implementation candidate/check observation is unavailable") from error
        # The active native run has not yet been closed by
        # ``kanban_request_review``.  Its session environment is therefore a
        # *provisional* claim only; do not require (or invent) terminal run
        # metadata here.  The public finalizer binds it after the native
        # transition.
        frozen_handoff = self._validated_provisional_candidate_observation(
            observed, candidate, task_id=task_id, run_id=candidate.originating_run_id,
            session_id=implementation_session, implementation_profile=implementation_profile)
        if frozen_handoff is None:
            raise ValueError("active implementation candidate/check observation is unavailable")
        before = self.board.read_task(task_id)
        if not isinstance(before, BoardSnapshot):
            raise ValueError("board adapter must return BoardSnapshot")
        marker = {"operation_key": operation_key, "candidate": candidate.to_dict(),
                  "implementation_run_id": candidate.originating_run_id,
                  "implementation_profile": implementation_profile,
                  "implementation_session_id": implementation_session,
                  "reviewer_profile": reviewer_profile, "provisional_handoff": frozen_handoff}
        action = Action(operation_key, self.scope, {
            "task_id": task_id, "reviewer_profile": reviewer_profile, "summary": summary,
            "candidate_content_identity": candidate.content_identity,
            "provisional_handoff": frozen_handoff, "review_marker": marker,
        }, "request_review", before.digest)
        with self.lock:
            self._assert_lock()
            intent = self.store.read_scope(self.scope)["operator_intent"]
            if intent is not None and intent.active:
                return {"outcome": "held", "details": "operator pause or cancellation is active", "operation_key": operation_key}
            stored = self.store.reserve_operation(self._intent_for(action))
            if stored.phase == "applied":
                return {"outcome": "verified", "operation_key": operation_key}
            return {"outcome": "proposed", "operation_key": operation_key}

    @staticmethod
    def _validated_provisional_candidate_observation(observed: Any, candidate: Any, *, task_id: str,
                                                     run_id: str, session_id: str,
                                                     implementation_profile: str) -> dict[str, Any] | None:
        """Freeze worker-observed evidence without treating its live session as authority."""
        if not isinstance(observed, Mapping) or observed.get("candidate") != candidate.to_dict():
            return None
        checks, criterion_ids = observed.get("checks"), observed.get("criterion_ids")
        if (not isinstance(checks, (list, tuple)) or not checks or not isinstance(criterion_ids, (list, tuple))
                or not criterion_ids or list(criterion_ids) != sorted(set(criterion_ids))
                or not all(isinstance(item, str) and item for item in criterion_ids)):
            return None
        if any(not isinstance(check, Mapping) or set(check) != {"check_id", "outcome", "evidence"}
               or any(not isinstance(check.get(field), str) or not check[field]
                      for field in ("check_id", "outcome", "evidence"))
               or check.get("outcome") != "passed" for check in checks):
            return None
        try:
            from .evidence_store import _canonical_identity
            normalized = [dict(item) for item in checks]
            identity = _canonical_identity(normalized)
        except (TypeError, ValueError):
            return None
        if observed.get("checks_identity") != identity:
            return None
        return {"task_id": task_id, "implementation_run_id": run_id,
                "provisional_session_id": session_id, "implementation_profile": implementation_profile,
                "candidate": candidate.to_dict(), "checks": normalized,
                "checks_identity": identity, "criterion_ids": list(criterion_ids)}

    def finalize_local_review(self, operation_key: str) -> dict[str, Any]:
        """Read-only reconcile the worker-owned native transition into final authority.

        This never calls the native request-review command.  It is safe after a
        reviewer has claimed/completed the card because it locates the ended
        implementation run by exact operation marker and run id.
        """
        if type(operation_key) is not str or not operation_key:
            raise ValueError("local review operation key must be a non-empty string")
        with self.lock:
            self._assert_lock()
            state = self.store.read_scope(self.scope)
            requests = [op for op in state["operations"] if op.key == operation_key and op.effect == "request_review"]
            if len(requests) != 1:
                return {"outcome": "held", "reason": "exact_provisional_review_request_required", "operation_key": operation_key}
            request = requests[0]
            marker, provisional = request.target.get("review_marker"), request.target.get("provisional_handoff")
            if not isinstance(marker, Mapping) or not isinstance(provisional, Mapping):
                return {"outcome": "conflict", "reason": "provisional_review_record_malformed", "operation_key": operation_key}
            try:
                from .contracts import CandidateIdentity
                task_id = provisional.get("task_id")
                run_id = provisional.get("implementation_run_id")
                provisional_session = provisional.get("provisional_session_id")
                implementation_profile = provisional.get("implementation_profile")
                if not all(type(value) is str and value for value in (task_id, run_id, provisional_session, implementation_profile)):
                    raise ValueError("provisional identity is malformed")
                assert isinstance(task_id, str) and isinstance(run_id, str)
                assert isinstance(provisional_session, str) and isinstance(implementation_profile, str)
                candidate = CandidateIdentity.from_dict(provisional.get("candidate", {}))
                checked_provisional = self._validated_provisional_candidate_observation(
                        {"candidate": candidate.to_dict(), "checks": provisional.get("checks"),
                         "checks_identity": provisional.get("checks_identity"), "criterion_ids": provisional.get("criterion_ids")},
                        candidate, task_id=task_id, run_id=run_id,
                        session_id=provisional_session, implementation_profile=implementation_profile)
                if checked_provisional is None:
                    raise ValueError("provisional snapshot changed")
                observer = self.finalization_observer
                if observer is None:
                    # Compatibility for controlled direct coordinator fixtures.
                    # Production composition always supplies the environment-free
                    # trusted finalization observer below.
                    observer = lambda candidate_scope, _candidate: self.git_observer(candidate_scope) if self.git_observer else None
                fresh = observer(dict(self.scope), candidate)
                if not isinstance(fresh, Mapping):
                    raise ValueError("trusted finalization observation is malformed")
                fresh_candidate = CandidateIdentity.from_dict(fresh.get("candidate", {}))
                fresh_provisional = self._validated_provisional_candidate_observation(
                    fresh, fresh_candidate, task_id=task_id, run_id=run_id,
                    session_id=provisional_session, implementation_profile=implementation_profile)
                if (fresh_candidate.to_dict() != candidate.to_dict()
                        or fresh_provisional != checked_provisional):
                    return {"outcome": "conflict", "reason": "trusted_finalization_candidate_or_checks_changed",
                            "operation_key": operation_key}
                card = self.board.read_task(task_id)
                run = self._scoped_run(task_id, run_id)
            except (ValueError, KeyError, TypeError):
                return {"outcome": "held", "reason": "provisional_review_evidence_unavailable", "operation_key": operation_key}
            session = self._worker_session(run)
            events = [] if not isinstance(card, BoardSnapshot) else [event for event in card.events
                if event.get("kind") == "review_requested" and str(event.get("run_id")) == str(run.get("id"))
                and isinstance(event.get("payload"), Mapping)
                and event["payload"].get("implementer") == provisional.get("implementation_profile")
                and event["payload"].get("reviewer") == marker.get("reviewer_profile")]
            if (not isinstance(card, BoardSnapshot) or run.get("profile") != provisional.get("implementation_profile")
                    or run.get("status") not in {"completed", "done"} or run.get("outcome") != "review_requested"
                    or not isinstance(run.get("metadata"), Mapping) or run["metadata"].get("local_first_review") != dict(marker)
                    or session != provisional_session or len(events) != 1):
                return {"outcome": "held", "reason": "exact_native_review_transition_not_finalized", "operation_key": operation_key}
            if session is None:
                return {"outcome": "held", "reason": "exact_native_review_transition_not_finalized", "operation_key": operation_key}
            receipt = {"version": 1, "task_id": task_id, "run_id": run_id,
                       "session_id": session, "profile": implementation_profile, "source": "native_run_metadata"}
            frozen = self._validated_frozen_candidate_observation(
                {"candidate": candidate.to_dict(), "checks": provisional["checks"],
                 "checks_identity": provisional["checks_identity"], "criterion_ids": provisional["criterion_ids"]}, candidate,
                task_id=task_id, run_id=run_id, session_id=session,
                implementation_profile=implementation_profile, worker_receipt=receipt)
            if frozen is None:
                return {"outcome": "conflict", "reason": "finalized_review_snapshot_invalid", "operation_key": operation_key}
            final_key = "finalize-local-review:" + operation_key
            target = {"operation_key": operation_key, "task_id": task_id, "review_marker": dict(marker),
                      "frozen_handoff": frozen}
            existing = [op for op in state["operations"] if op.key == final_key]
            if existing:
                if len(existing) != 1 or dict(existing[0].target) != target:
                    return {"outcome": "conflict", "reason": "finalization_record_conflicts", "operation_key": operation_key}
                # A restart can encounter a legacy finalization receipt that was
                # reserved after the same verified native readback but never
                # acknowledged.  Reconcile only that exact pending receipt;
                # unknown/applied outcomes are never rewritten.
                if existing[0].phase == "pending":
                    self.store.ack_effect(self.scope, final_key, readback=card.to_dict(), outcome="verified")
                elif existing[0].phase != "applied":
                    return {"outcome": "held", "reason": "finalization_record_effect_unknown", "operation_key": operation_key}
                return {"outcome": "finalized", "operation_key": operation_key, "finalization_key": final_key}
            # The exact native transition is now a verified read-only receipt for
            # the worker-owned request; acknowledge it before exposing the final
            # immutable handoff so pause/recovery does not retain a false pending
            # native effect or try to resend it.
            if request.phase == "pending":
                self.store.ack_effect(self.scope, request.key, readback=card.to_dict(), outcome="verified")
            elif request.phase != "applied":
                return {"outcome": "held", "reason": "provisional_review_request_effect_unknown", "operation_key": operation_key}
            action = Action(final_key, self.scope, target, "finalize_request_review", request.expected_observed_identity)
            self.store.reserve_operation(self._intent_for(action))
            # Finalization is a fully verified, read-only receipt: its only
            # external observation is the exact native transition validated
            # above.  Leaving this local receipt pending would make a later
            # pause/resume mistake it for an unknown native effect and hold a
            # coherent, unchanged scope indefinitely.
            self.store.ack_effect(self.scope, final_key, readback=card.to_dict(), outcome="verified")
            return {"outcome": "finalized", "operation_key": operation_key, "finalization_key": final_key}

    def recover_premature_done(self, task_id: str, candidate: Any, *, reviewer_profile: str,
                               already_locked: bool = False, replacement_for: str | None = None) -> dict[str, Any]:
        """Create one held replacement review without linking it to a done card."""
        from .contracts import CandidateIdentity, ManagedMember
        configured_implementation, configured_reviewer = self._local_review_roles()
        if not isinstance(candidate, CandidateIdentity) or not isinstance(reviewer_profile, str) or not reviewer_profile:
            raise ValueError("candidate and local reviewer profile are required")
        if reviewer_profile != configured_reviewer:
            raise ValueError("replacement review profile must match configured local-review role")
        self._managed_task(task_id)
        if replacement_for is not None:
            self._managed_task(replacement_for)
            if not any(member.task_id == replacement_for and member.role == "local_review" for member in self._members()):
                raise ValueError("replacement source must be an exact managed local-review member")
        source = self.board.read_task(task_id)
        anchor = self.board.read_task(self.scope["anchor_task_id"])
        if not isinstance(source, BoardSnapshot) or not isinstance(anchor, BoardSnapshot):
            raise ValueError("board adapter must return BoardSnapshot")
        if self._state(source) != "done":
            return {"outcome": "held", "reason": "source_not_premature_done"}
        # A completed card has no worker-owned review handoff.  Preserve only a
        # candidate independently observed from Git and tied to its exact source
        # run before creating a replacement review.
        source_run = self._scoped_run(task_id, candidate.originating_run_id)
        source_profile = source_run.get("profile")
        source_session = self._worker_session(source_run)
        if (source_profile != configured_implementation or source_session is None
                or source_run.get("status") not in {"completed", "done"}):
            return {"outcome": "held", "reason": "premature_done_source_provenance_missing"}
        if self.git_observer is None:
            return {"outcome": "held", "reason": "trusted_git_freeze_unavailable"}
        try:
            source_observation = self.git_observer(dict(self.scope))
        except Exception:
            return {"outcome": "held", "reason": "trusted_git_freeze_unavailable"}
        frozen_handoff = self._validated_frozen_candidate_observation(
            source_observation, candidate, task_id=task_id, run_id=candidate.originating_run_id,
            session_id=source_session, implementation_profile=configured_implementation,
            allow_unbound_observation=True)
        if frozen_handoff is None:
            return {"outcome": "held", "reason": "trusted_git_freeze_unavailable"}
        canonical = json.dumps({"scope": self.scope, "source_task_id": task_id,
                                "candidate": candidate.to_dict(), "action": "premature_done_review",
                                "replacement_for": replacement_for},
                               sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        key = "premature-done-review:" + hashlib.sha256(canonical.encode()).hexdigest()
        association = (f"premature-done:{task_id}:{candidate.content_identity}" if replacement_for is None
                       else f"premature-done-replacement:{replacement_for}:{candidate.content_identity}")
        action = Action(key, self.scope, {
            "anchor_task_id": self.scope["anchor_task_id"], "source_task_id": task_id,
            "candidate": candidate.to_dict(), "reviewer_profile": reviewer_profile,
            "association": association, "replacement_for": replacement_for, "native_parent": False,
            "task_id": task_id, "source_run_id": candidate.originating_run_id,
            "source_profile": source_profile, "source_session_id": source_session,
            "frozen_handoff": frozen_handoff,
            "create_title": f"Local review: {task_id}",
            "create_body": f"Preserved candidate {candidate.content_identity} for completed source {task_id}.",
            "create_workspace": self._native_workspace(candidate.worktree), "create_idempotency_key": key,
        }, "create_held", anchor.digest)
        with (nullcontext() if already_locked else self.lock):
            self._assert_lock()
            pause = self.store.read_scope(self.scope)["operator_intent"]
            if pause is not None and pause.active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active"}
            try:
                if self.budget_policy is None:
                    return {"outcome": "held", "reason": "finite_workflow_repair_budget_required"}
                source_member = next(member for member in self._members() if member.task_id == task_id)
                event = {"event_id": f"{WORKFLOW_REPAIRS}:{key}",
                         "lineage_id": f"{self.scope['anchor_task_id']}:{GENERAL_ATTEMPT}",
                         "root_task_id": self.scope["anchor_task_id"], "finding_id": GENERAL_ATTEMPT,
                         "generation": source_member.generation, "source_task_id": task_id,
                         "source_kind": "native_operation", "native_source_id": key, "count": 1}
                stored = admit_repair_operation(self.budget_policy, self.store, self.scope, self._intent_for(action), event)
            except Exception as error:
                return {"outcome": "held", "reason": str(error)}
            if stored.phase == "unknown":
                verifier = getattr(self.board, "verify_effect", None)
                result = verifier(action) if callable(verifier) else ActionResult(key, "unsupported", "create marker verifier unavailable", None)
                readback = self._portable_readback(result.readback)
                self.store.record_effect_observation(self.scope, key, outcome=result.outcome, details=result.details, readback=readback)
                if result.outcome not in {"verified", "no-op"} or not self._readback_proves(action, result):
                    return {"outcome": "held", "reason": "durable_create_outcome_unknown"}
                assert readback is not None
                stored = self.store.ack_effect(self.scope, key, readback=readback, outcome=result.outcome)
            if stored.phase == "applied":
                created = self._snapshot_from_readback(stored.readback)
                if created is None:
                    return {"outcome": "held", "reason": "durable_create_readback_invalid"}
            else:
                try:
                    result = self.board.create_held(
                        action, title=action.target["create_title"], body=action.target["create_body"],
                        assignee=reviewer_profile, workspace=action.target["create_workspace"], idempotency_key=key,
                    )
                except Exception as error:
                    result = ActionResult(key, "unknown", f"native create exception: {error}", None)
                readback = self._portable_readback(result.readback)
                self.store.record_effect_observation(self.scope, key, outcome=result.outcome,
                                                     details=result.details, readback=readback)
                created = self._snapshot_from_readback(readback)
                if (result.outcome not in {"verified", "no-op"} or created is None
                        or self._state(created) != "blocked" or created.parents):
                    return {"outcome": "held", "reason": "separate_review_create_unverified"}
                self.store.ack_effect(self.scope, key, readback=created.to_dict(), outcome=result.outcome)
            review_member = ManagedMember(self.scope["board_id"], self.scope["anchor_task_id"],
                                          str(created.native_task["id"]), "local_review",
                                          source_member.generation, source_member.finding_ids, association)
            self.store.register_member(review_member)
            return {"outcome": "held", "task_id": review_member.task_id}

    def create_separate_correction(self, review_task_id: str, candidate: Any, review: Mapping[str, Any],
                                   *, already_locked: bool = False) -> dict[str, Any]:
        """Preserve a completed separate review's finding as one held implementation card."""
        from .contracts import CandidateIdentity, ManagedMember
        implementer, reviewer = self._local_review_roles()
        if not isinstance(candidate, CandidateIdentity) or not isinstance(review, Mapping):
            raise ValueError("candidate and stored separate review are required")
        self._managed_task(review_task_id)
        with (nullcontext() if already_locked else self.lock):
            self._assert_lock()
            pause = self.store.read_scope(self.scope)["operator_intent"]
            if pause is not None and pause.active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active"}
            state = self.store.read_scope(self.scope)
            if not any(saved == dict(review) for saved in state["reviews"]):
                return {"outcome": "held", "reason": "recorded_separate_changes_missing"}
            native = review.get("native_review")
            findings = review.get("findings")
            if (review.get("verdict") != "changes_requested" or not isinstance(native, Mapping)
                    or native.get("task_id") != review_task_id or native.get("profile") != reviewer
                    or not isinstance(native.get("run_id"), str) or not isinstance(native.get("session_id"), str)
                    or review.get("candidate_identity") != candidate.to_dict()
                    or not isinstance(findings, list) or not findings
                    or not all(isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
                               and item.get("finding_id") and isinstance(item.get("summary"), str) for item in findings)):
                return {"outcome": "held", "reason": "exact_correction_finding_required"}
            finding_ids = tuple(sorted(item["finding_id"] for item in findings))
            if len(set(finding_ids)) != len(finding_ids):
                return {"outcome": "held", "reason": "duplicate_correction_finding"}
            # One correction generation owns the complete review finding set.
            # Its immutable target—not only the human-readable body—binds every
            # finding that the single root-budget reservation represents.
            structured_findings = tuple(sorted((dict(item) for item in findings), key=lambda item: item["finding_id"]))
            member = next(member for member in state["members"] if member.task_id == review_task_id)
            association = f"separate-review-correction:{review_task_id}:{candidate.content_identity}:{member.generation + 1}"
            if any(member.work_association == association for member in state["members"]):
                return {"outcome": "held", "reason": "correction_already_registered"}
            separate_intent = self._separate_review_intent(review_task_id, candidate, reviewer)
            frozen_handoff = None if separate_intent is None else separate_intent.target.get("frozen_handoff")
            if (separate_intent is None
                    or not self._verified_separate_review(review_task_id, candidate, reviewer,
                                                          native.get("run_id"), native.get("session_id"))
                    or not self._frozen_candidate(candidate, review, frozen_handoff)):
                return {"outcome": "held", "reason": "terminal_separate_review_or_freeze_missing"}
            if self.budget_policy is None:
                return {"outcome": "held", "reason": "finite_correction_budget_required"}
            anchor = self.board.read_task(self.scope["anchor_task_id"])
            if not isinstance(anchor, BoardSnapshot):
                return {"outcome": "held", "reason": "anchor_read_unavailable"}
            canonical = json.dumps({"scope": self.scope, "review_task_id": review_task_id,
                                    "review_id": review["review_id"], "candidate": candidate.to_dict(),
                                    "findings": structured_findings}, sort_keys=True, separators=(",", ":"))
            key = "separate-review-correction:" + hashlib.sha256(canonical.encode()).hexdigest()
            action = Action(key, self.scope, {
                "anchor_task_id": self.scope["anchor_task_id"], "task_id": review_task_id,
                "source_task_id": review_task_id, "correction_of": review_task_id,
                "candidate": candidate.to_dict(), "finding_ids": finding_ids, "findings": structured_findings,
                "generation": member.generation + 1,
                "review_id": review["review_id"], "reviewer_profile": implementer,
                "association": association, "native_parent": False,
                "create_title": f"Correction: {review_task_id}",
                "create_body": "\n".join(f"Finding {item['finding_id']}: {item['summary']}" for item in structured_findings),
                "create_workspace": self._native_workspace(candidate.worktree), "create_idempotency_key": key,
            }, "create_held", anchor.digest)
            event = {"event_id": f"{REVIEW_CORRECTIONS}:{key}",
                     "lineage_id": f"{self.scope['anchor_task_id']}:{GENERAL_ATTEMPT}",
                     "root_task_id": self.scope["anchor_task_id"], "finding_id": GENERAL_ATTEMPT,
                     "generation": member.generation, "source_task_id": review_task_id,
                     "source_kind": "native_operation", "native_source_id": key, "count": 1}
            try:
                stored = admit_repair_operation(self.budget_policy, self.store, self.scope,
                                                self._intent_for(action), event)
            except Exception as error:
                return {"outcome": "held", "reason": str(error)}
            if stored.phase == "unknown":
                verifier = getattr(self.board, "verify_effect", None)
                possible = verifier(action) if callable(verifier) else None
                result = possible if isinstance(possible, ActionResult) else ActionResult(key, "unsupported", "marker verifier unavailable", None)
                readback = self._portable_readback(result.readback)
                self.store.record_effect_observation(self.scope, key, outcome=result.outcome,
                                                     details=result.details, readback=readback)
                if result.outcome not in {"verified", "no-op"} or not self._readback_proves(action, result):
                    return {"outcome": "held", "reason": "durable_correction_create_unknown"}
                assert readback is not None
                stored = self.store.ack_effect(self.scope, key, readback=readback, outcome=result.outcome)
            if stored.phase == "applied":
                created = self._snapshot_from_readback(stored.readback)
            else:
                try:
                    result = self.board.create_held(action, title=action.target["create_title"],
                                                    body=action.target["create_body"], assignee=implementer,
                                                    workspace=action.target["create_workspace"], idempotency_key=key)
                except Exception as error:
                    result = ActionResult(key, "unknown", f"native correction create exception: {error}", None)
                readback = self._portable_readback(result.readback)
                self.store.record_effect_observation(self.scope, key, outcome=result.outcome,
                                                     details=result.details, readback=readback)
                created = self._snapshot_from_readback(readback)
                if (result.outcome not in {"verified", "no-op"} or created is None
                        or not self._readback_proves(action, result)):
                    return {"outcome": "held", "reason": "correction_create_unverified"}
                self.store.ack_effect(self.scope, key, readback=created.to_dict(), outcome=result.outcome)
            if created is None or self._state(created) != "blocked" or created.parents:
                return {"outcome": "held", "reason": "correction_create_readback_invalid"}
            registered = ManagedMember(self.scope["board_id"], self.scope["anchor_task_id"],
                                       str(created.native_task["id"]), "implementation", member.generation + 1,
                                       finding_ids, association)
            self.store.register_member(registered)
            return {"outcome": "held", "task_id": registered.task_id}

    @staticmethod
    def _validated_frozen_candidate_observation(observed: Any, candidate: Any, *, task_id: str,
                                                run_id: str, session_id: str,
                                                implementation_profile: str,
                                                worker_receipt: Mapping[str, Any] | None = None,
                                                allow_unbound_observation: bool = False) -> dict[str, Any] | None:
        """Detach the implementation-owned candidate/check handoff once.

        This record is stored inside the immutable request-review intent.  It is
        intentionally complete so a later reviewer never has to (and must not)
        re-run the implementation observer from a different run/worktree.
        """
        if not isinstance(observed, Mapping) or observed.get("candidate") != candidate.to_dict():
            return None
        checks, criterion_ids = observed.get("checks"), observed.get("criterion_ids")
        if (not isinstance(checks, (list, tuple)) or not checks or not isinstance(criterion_ids, (list, tuple)) or not criterion_ids
                or not all(isinstance(item, str) and item for item in criterion_ids)
                or list(criterion_ids) != sorted(set(criterion_ids))):
            return None
        if any(not isinstance(check, Mapping) or set(check) != {"check_id", "outcome", "evidence"}
               or not all(isinstance(check.get(field), str) and check[field] for field in ("check_id", "outcome", "evidence"))
               or check.get("outcome") != "passed" for check in checks):
            return None
        try:
            from .evidence_store import _canonical_identity
            normalized_checks = [dict(check) for check in checks]
            checks_identity = _canonical_identity(normalized_checks)
        except (TypeError, ValueError):
            return None
        if observed.get("checks_identity") != checks_identity:
            return None
        receipt = worker_receipt
        # Separate premature-done recovery has already verified its terminal
        # source run/session before calling this helper; it is not a native
        # implementation-to-review handoff and must not invent a receipt.
        if receipt is None and allow_unbound_observation:
            return {"task_id": task_id, "implementation_run_id": run_id,
                    "implementation_session_id": session_id,
                    "implementation_profile": implementation_profile,
                    "candidate": candidate.to_dict(), "checks": normalized_checks,
                    "checks_identity": checks_identity, "criterion_ids": list(criterion_ids)}
        if receipt is None:
            return None
        receipt_expected = {"version", "task_id", "run_id", "session_id", "profile", "source"}
        if (not isinstance(receipt, Mapping) or not receipt_expected <= set(receipt)
                or receipt.get("version") != 1 or receipt.get("task_id") != task_id
                or str(receipt.get("run_id")) != run_id or receipt.get("session_id") != session_id
                or receipt.get("profile") != implementation_profile
                or receipt.get("source") != "native_run_metadata"):
            return None
        return {"task_id": task_id, "implementation_run_id": run_id,
                "implementation_session_id": session_id,
                "implementation_profile": implementation_profile,
                "worker_receipt": dict(receipt),
                "candidate": candidate.to_dict(), "checks": normalized_checks,
                "checks_identity": checks_identity, "criterion_ids": list(criterion_ids)}

    def _trusted_candidate_observation(self, candidate: Any) -> bool:
        """Legacy implementation-context check for separate recovery creation."""
        if self.git_observer is None:
            return False
        try:
            observed = self.git_observer(dict(self.scope))
        except Exception:
            return False
        return self._validated_frozen_candidate_observation(
            observed, candidate, task_id="separate-recovery", run_id=candidate.originating_run_id,
            session_id="separate-recovery", implementation_profile="separate-recovery",
            allow_unbound_observation=True) is not None

    def _frozen_candidate(self, candidate: Any, review: Mapping[str, Any], handoff: Any,
                          *, allow_unbound: bool = False) -> bool:
        """Bind a verdict to immutable evidence, with an explicit recovery-only mode."""
        expected = {"task_id", "implementation_run_id", "implementation_session_id",
                    "implementation_profile", "candidate", "checks", "checks_identity", "criterion_ids"}
        if not allow_unbound:
            expected = expected | {"worker_receipt"}
        if not isinstance(handoff, Mapping) or set(handoff) != expected:
            return False
        try:
            frozen = self._validated_frozen_candidate_observation(
                handoff, candidate, task_id=handoff["task_id"], run_id=handoff["implementation_run_id"],
                session_id=handoff["implementation_session_id"], implementation_profile=handoff["implementation_profile"],
                worker_receipt=handoff.get("worker_receipt"), allow_unbound_observation=allow_unbound)
        except (KeyError, TypeError):
            return False
        if frozen is None:
            return False
        canonical_handoff = {"task_id": handoff["task_id"], "implementation_run_id": handoff["implementation_run_id"],
                             "implementation_session_id": handoff["implementation_session_id"],
                             "implementation_profile": handoff["implementation_profile"],
                             "candidate": dict(handoff["candidate"]) if isinstance(handoff["candidate"], Mapping) else handoff["candidate"],
                             "checks": [dict(check) for check in handoff["checks"]] if isinstance(handoff["checks"], (list, tuple)) else handoff["checks"],
                             "checks_identity": handoff["checks_identity"],
                             "criterion_ids": list(handoff["criterion_ids"]) if isinstance(handoff["criterion_ids"], (list, tuple)) else handoff["criterion_ids"]}
        if not allow_unbound:
            canonical_handoff["worker_receipt"] = (dict(handoff["worker_receipt"])
                                                   if isinstance(handoff["worker_receipt"], Mapping)
                                                   else handoff["worker_receipt"])
        if frozen != canonical_handoff:
            return False
        review_checks = review.get("checks")
        if (not isinstance(review_checks, (list, tuple))
                or [dict(check) for check in review_checks] != frozen["checks"]
                or review.get("checks_identity") != frozen["checks_identity"]):
            return False
        criteria = review.get("criterion_evidence")
        if not isinstance(criteria, list) or len(criteria) != len(frozen["criterion_ids"]):
            return False
        if any(not isinstance(item, Mapping) or set(item) != {"criterion_id", "outcome", "evidence"}
               or not all(isinstance(item.get(field), str) and item[field] for field in ("criterion_id", "outcome", "evidence"))
               or item.get("outcome") not in {"pass", "fail"} for item in criteria):
            return False
        return [item["criterion_id"] for item in criteria] == frozen["criterion_ids"]

    def _verified_handoff(self, task_id: str, candidate: Any, expected_profile: str) -> Mapping[str, Any] | None:
        """Read native worker-owned run/event evidence; never replay its handoff."""
        try:
            configured_implementation, configured_reviewer = self._local_review_roles()
        except ValueError:
            return None
        if expected_profile != configured_reviewer:
            return None
        state = self.store.read_scope(self.scope)
        matches = [operation for operation in state["operations"]
                   if operation.effect == "finalize_request_review"
                   and operation.target.get("task_id") == task_id]
        if len(matches) != 1:
            return None
        final = matches[0]
        marker = final.target.get("review_marker")
        frozen_handoff = final.target.get("frozen_handoff")
        if (not isinstance(marker, Mapping) or marker.get("candidate") != candidate.to_dict()
                or marker.get("implementation_profile") != configured_implementation
                or marker.get("reviewer_profile") != configured_reviewer
                or not self._frozen_candidate(candidate, {"checks": frozen_handoff.get("checks") if isinstance(frozen_handoff, Mapping) else None,
                                                          "checks_identity": frozen_handoff.get("checks_identity") if isinstance(frozen_handoff, Mapping) else None,
                                                          "criterion_evidence": [{"criterion_id": item, "outcome": "pass", "evidence": "frozen"} for item in frozen_handoff.get("criterion_ids", ())] if isinstance(frozen_handoff, Mapping) else None}, frozen_handoff)):
            return None
        try:
            observed = self.board.read_task(task_id)
        except Exception:
            return None
        if not isinstance(observed, BoardSnapshot):
            return None
        implementation = next((run for run in observed.runs if str(run.get("id")) == candidate.originating_run_id), None)
        if not isinstance(implementation, Mapping):
            return None
        if (observed.native_task.get("status") not in {"review", "running", "done"}
                or observed.native_task.get("assignee") != expected_profile
                or implementation.get("profile") != marker.get("implementation_profile")
                or implementation.get("status") not in {"completed", "done"}
                or implementation.get("outcome") != "review_requested"):
            return None
        metadata = implementation.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("local_first_review") != dict(marker):
            return None
        implementation_session = self._worker_session(implementation)
        if implementation_session is None or marker.get("implementation_session_id") != implementation_session:
            return None
        handoff_events = [event for event in observed.events
                          if event.get("kind") == "review_requested" and str(event.get("run_id")) == candidate.originating_run_id
                          and isinstance(event.get("payload"), Mapping)
                          and event["payload"].get("implementer") == marker.get("implementation_profile")
                          and event["payload"].get("reviewer") == expected_profile]
        if len(handoff_events) != 1:
            return None
        handoff_index = next(index for index, event in enumerate(observed.events) if event is handoff_events[0])
        # Hermes returns task events in historical order.  A later review
        # request is a later candidate revision, so this candidate's completed
        # reviewer verdict cannot be accepted through the old handoff.
        if any(event.get("kind") == "review_requested" for event in observed.events[handoff_index + 1:]):
            return None
        return {"marker": marker, "implementation_session_id": implementation_session,
                "frozen_handoff": frozen_handoff}

    def _separate_review_intent(self, task_id: str, candidate: Any, profile: str) -> OperationIntent | None:
        state = self.store.read_scope(self.scope)
        member = next((item for item in state["members"] if item.task_id == task_id and item.role == "local_review"), None)
        if member is None:
            return None
        matches = [item for item in state["operations"] if item.effect == "create_held"
                   and item.phase == "applied" and item.readback is not None
                   and self._snapshot_from_readback(item.readback) is not None
                   and self._snapshot_from_readback(item.readback).native_task.get("id") == task_id
                   and item.target.get("candidate") == candidate.to_dict()
                   and item.target.get("reviewer_profile") == profile
                   and item.target.get("association") == member.work_association
                   and item.target.get("native_parent") is False]
        return matches[0] if len(matches) == 1 else None

    def release_separate_review(self, task_id: str, candidate: Any, *, reviewer_profile: str,
                                already_locked: bool = False) -> dict[str, Any]:
        """Release one exact held recovery review for native reviewer dispatch."""
        from .contracts import CandidateIdentity
        _, configured_reviewer = self._local_review_roles()
        if not isinstance(candidate, CandidateIdentity) or not isinstance(reviewer_profile, str) or not reviewer_profile:
            raise ValueError("candidate and local reviewer profile are required")
        if reviewer_profile != configured_reviewer:
            raise ValueError("replacement review profile must match configured local-review role")
        self._managed_task(task_id)
        with (nullcontext() if already_locked else self.lock):
            self._assert_lock()
            pause = self.store.read_scope(self.scope)["operator_intent"]
            if pause is not None and pause.active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active"}
            create = self._separate_review_intent(task_id, candidate, reviewer_profile)
            frozen_handoff = None if create is None else create.target.get("frozen_handoff")
            frozen_review = {"checks": frozen_handoff.get("checks") if isinstance(frozen_handoff, Mapping) else None,
                             "checks_identity": frozen_handoff.get("checks_identity") if isinstance(frozen_handoff, Mapping) else None,
                             "criterion_evidence": [{"criterion_id": item, "outcome": "pass", "evidence": "frozen"}
                                                    for item in frozen_handoff.get("criterion_ids", ())] if isinstance(frozen_handoff, Mapping) else None}
            if create is None or not self._frozen_candidate(candidate, frozen_review, frozen_handoff,
                                                            allow_unbound=True):
                return {"outcome": "held", "reason": "separate_review_association_or_freeze_missing"}
            before = self.board.read_task(task_id)
            if not isinstance(before, BoardSnapshot) or self._state(before) != "blocked" or before.parents or before.runs:
                return {"outcome": "held", "reason": "separate_review_not_held_and_idle"}
            action = Action(f"release-separate-review:{create.key}", self.scope, {"task_id": task_id}, "release", before.digest)
            result = self._apply(action)
            if result.outcome not in {"verified", "no-op"} or not self._readback_proves(action, result):
                return {"outcome": "held", "reason": "separate_review_release_unverified"}
            return {"outcome": "released", "task_id": task_id}

    def _verified_separate_review(self, task_id: str, candidate: Any, expected_profile: str,
                                  run_id: str, session_id: str, *, changes_requested: bool = False) -> bool:
        try:
            configured_implementation, configured_reviewer = self._local_review_roles()
        except ValueError:
            return False
        if expected_profile != configured_reviewer:
            return False
        create = self._separate_review_intent(task_id, candidate, expected_profile)
        if create is None:
            return False
        source_task = create.target.get("source_task_id")
        source_run_id = create.target.get("source_run_id")
        if not isinstance(source_task, str) or source_run_id != candidate.originating_run_id:
            return False
        try:
            source_run = self._scoped_run(source_task, source_run_id)
            reviewed = self.board.read_task(task_id)
        except Exception:
            return False
        expected_state = "running" if changes_requested else "done"
        if (not isinstance(reviewed, BoardSnapshot) or self._state(reviewed) != expected_state
                or reviewed.native_task.get("assignee") != expected_profile
                or source_run.get("profile") != configured_implementation
                or create.target.get("source_profile") != configured_implementation
                or self._worker_session(source_run) != create.target.get("source_session_id")):
            return False
        run = next((item for item in reviewed.runs if str(item.get("id")) == run_id), None)
        claims = [event for event in reviewed.events if event.get("kind") == "claimed"
                  and str(event.get("run_id")) == run_id and isinstance(event.get("payload"), Mapping)]
        completions = [event for event in reviewed.events if event.get("kind") == "completed" and str(event.get("run_id")) == run_id]
        reviewer_session = (self._active_worker_session(task_id, run_id) if changes_requested
                            else self._worker_session(run)) if isinstance(run, Mapping) else None
        # Older/native ordinary-ready claims omit source_status entirely.  That
        # omission is compatible only with the exact durable held-card create
        # and its exact durable release receipt; explicit contrary metadata is
        # never normalized into a ready claim.
        release_key = f"release-separate-review:{create.key}"
        releases = [operation for operation in self.store.read_scope(self.scope)["operations"]
                    if operation.key == release_key and operation.effect == "release"
                    and operation.phase == "applied" and dict(operation.target) == {"task_id": task_id}
                    and (proof := self._snapshot_from_readback(operation.readback)) is not None
                    and proof.native_task.get("id") == task_id and self._state(proof) in {"ready", "todo"}]
        claim_is_ready = (len(claims) == 1 and claims[0]["payload"].get("source_status") == "ready")
        claim_is_native_ready = (len(claims) == 1 and "source_status" not in claims[0]["payload"]
                                 and len(releases) == 1)
        return (isinstance(run, Mapping) and run.get("profile") == expected_profile
                and (run.get("status") == "running" if changes_requested else run.get("status") in {"completed", "done"})
                and reviewer_session == session_id
                and isinstance(session_id, str) and bool(session_id)
                and session_id != self._worker_session(source_run)
                and (claim_is_ready or claim_is_native_ready)
                and len(completions) == (0 if changes_requested else 1))

    def _prior_changes_are_reconciled(self, task_id: str, state: Mapping[str, Any]) -> bool:
        """Require each prior same-card rejection to have its exact native repair receipt."""
        member = next((item for item in state["members"] if item.task_id == task_id), None)
        if member is None:
            return False
        for prior in state["reviews"]:
            native = prior.get("native_review") if isinstance(prior, Mapping) else None
            if (not isinstance(native, Mapping) or prior.get("verdict") != "changes_requested"
                    or native.get("task_id") != task_id):
                continue
            run_id, session_id, profile = native.get("run_id"), native.get("session_id"), native.get("profile")
            findings = prior.get("findings")
            if (not all(isinstance(value, str) and value for value in (run_id, session_id, profile))
                    or not isinstance(findings, list) or not findings):
                return False
            finding_id_values: list[str] = []
            for item in findings:
                if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str):
                    finding_id_values.append(item["finding_id"])
            finding_ids = tuple(sorted(finding_id_values))
            if len(finding_ids) != len(findings) or len(set(finding_ids)) != len(finding_ids):
                return False
            operations = [operation for operation in state["operations"]
                          if operation.effect == "request_changes"
                          and operation.target.get("task_id") == task_id
                          and operation.target.get("candidate") == prior.get("candidate_identity")
                          and operation.target.get("review_id") == prior.get("review_id")
                          and operation.target.get("review_run_id") == run_id
                          and operation.target.get("reviewer_profile") == profile
                          and operation.target.get("reviewer_session_id") == session_id
                          and operation.target.get("reviewer_session_receipt") == session_id
                          and operation.target.get("finding_ids") == finding_ids
                          # Review record order is presentation order.  The
                          # immutable correction intent canonicalizes the full
                          # finding records by ID, so compare that same form.
                          and tuple(operation.target.get("findings", ()))
                          == tuple(sorted((dict(item) for item in findings), key=lambda item: item["finding_id"]))]
            if len(operations) != 1:
                return False
            operation = operations[0]
            event_id = f"{REVIEW_CORRECTIONS}:{operation.key}"
            if not any(event.get("event_id") == event_id
                       and event.get("native_source_id") == operation.key
                       and event.get("source_task_id") == task_id
                       and event.get("generation") == member.generation
                       and event.get("finding_id") == GENERAL_ATTEMPT
                       for event in state["budget_events"]):
                return False
            evidence = self._snapshot_from_readback(operation.readback)
            if operation.phase != "applied" or evidence is None:
                return False
            events = [event for event in evidence.events if event.get("kind") == "changes_requested"
                      and str(event.get("run_id")) == run_id and isinstance(event.get("payload"), Mapping)
                      and event["payload"].get("reason") == operation.target.get("reason")
                      and event["payload"].get("implementer") == operation.target.get("implementation_profile")
                      and event["payload"].get("reviewer") == profile]
            run = next((item for item in evidence.runs if str(item.get("id")) == run_id), None)
            if (len(events) != 1 or not isinstance(run, Mapping) or run.get("outcome") != "changes_requested"
                    or evidence.native_task.get("status") not in {"ready", "todo"}
                    or evidence.native_task.get("assignee") != operation.target.get("implementation_profile")):
                return False
        return True

    def submit_review(self, task_id: str, candidate: Any, review: Mapping[str, Any], *,
                      expected_profile: str) -> dict[str, Any]:
        """Persist a local verdict only after independent native provenance readback."""
        from .contracts import CandidateIdentity
        configured_implementation, configured_reviewer = self._local_review_roles()
        if not isinstance(candidate, CandidateIdentity) or not isinstance(expected_profile, str) or not expected_profile:
            raise ValueError("candidate and expected local-review profile are required")
        if expected_profile != configured_reviewer:
            raise ValueError("local-review verdict profile must match configured role")
        self._managed_task(task_id)
        if not isinstance(review, Mapping) or review.get("reviewer_role") != "local":
            raise ValueError("only structured local review evidence is accepted")
        native = review.get("native_review")
        if not isinstance(native, Mapping) or native.get("task_id") != task_id:
            raise ValueError("review must name the exact managed task")
        run_id, session_id = native.get("run_id"), native.get("session_id")
        if not isinstance(run_id, str) or not isinstance(session_id, str):
            raise ValueError("review requires exact native run and session")
        if native.get("profile") != expected_profile:
            return {"outcome": "held", "reason": "review_payload_profile_mismatch"}
        with self.lock:
            self._assert_lock()
            intent = self.store.read_scope(self.scope)["operator_intent"]
            if intent is not None and intent.active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active"}
            state = self.store.read_scope(self.scope)
            for prior in state["reviews"]:
                prior_native = prior.get("native_review")
                if (prior.get("candidate_identity") == candidate.to_dict()
                        and isinstance(prior_native, Mapping)
                        and prior_native.get("task_id") == task_id and prior_native.get("run_id") == run_id
                        and prior != dict(review)):
                    return {"outcome": "held", "reason": "review_run_verdict_conflict"}
            separate = self._separate_review_intent(task_id, candidate, expected_profile)
            handoff = None if separate is not None else self._verified_handoff(task_id, candidate, expected_profile)
            if handoff is None and separate is None:
                return {"outcome": "held", "reason": "verified_local_review_handoff_missing"}
            if separate is None and (not isinstance(handoff, Mapping)
                                    or not self._frozen_candidate(candidate, review, handoff.get("frozen_handoff"))):
                return {"outcome": "held", "reason": "trusted_git_freeze_unavailable"}
            if separate is not None and not self._frozen_candidate(
                    candidate, review, separate.target.get("frozen_handoff"), allow_unbound=True):
                return {"outcome": "held", "reason": "trusted_git_freeze_unavailable"}
            observed = self._scoped_run(task_id, run_id)
            self._exact_run(observed, run_id=run_id, profile=expected_profile)
            implementation = None if separate is not None else self._scoped_run(task_id, candidate.originating_run_id)
            implementation_session = None if implementation is None else self._worker_session(implementation)
            if implementation is not None and implementation.get("profile") != configured_implementation:
                return {"outcome": "held", "reason": "implementation_native_profile_mismatch"}
            if run_id == candidate.originating_run_id:
                raise ValueError("review requires a fresh native run")
            verdict = review.get("verdict")
            if verdict == "changes_requested":
                # Hermes only writes worker_session_id when the reviewer-owned
                # request-changes call closes this run.  Bind the verdict to the
                # active worker tool receipt now, while the run is still owned.
                reviewer_session = self._active_worker_session(task_id, run_id)
            elif verdict == "approved":
                # Approval is terminal evidence, so native run metadata must
                # already carry the reviewer session receipt.
                reviewer_session = self._worker_session(observed)
            else:
                return {"outcome": "held", "reason": "review_verdict_invalid"}
            if reviewer_session != session_id:
                return {"outcome": "held", "reason": "review_session_attribution_mismatch"}
            if separate is None and (not isinstance(implementation_session, str) or session_id == implementation_session):
                raise ValueError("review requires a fresh session")
            if handoff is not None and handoff.get("implementation_session_id") != implementation_session:
                return {"outcome": "held", "reason": "handoff_implementation_session_mismatch"}
            if verdict == "approved":
                permitted_run_states = {"completed", "done"}
            else:
                # A reviewer must record its structured findings before its own
                # native tool ends the active review run.
                permitted_run_states = {"running"}
            if observed.get("status") not in permitted_run_states:
                return {"outcome": "held", "reason": "review_native_run_incomplete"}
            # Re-read the terminal card and source run immediately before the
            # durable verdict write; status alone is never handoff proof.
            task_snapshot = self.board.read_task(task_id)
            if not isinstance(task_snapshot, BoardSnapshot):
                raise ValueError("board adapter must return BoardSnapshot")
            final_handoff = None if separate is not None else self._verified_handoff(task_id, candidate, expected_profile)
            if (separate is None and (final_handoff is None or final_handoff.get("implementation_session_id") != implementation_session)):
                return {"outcome": "held", "reason": "verified_local_review_handoff_missing"}
            if separate is not None and not self._verified_separate_review(task_id, candidate, expected_profile, run_id,
                                                                            session_id, changes_requested=verdict == "changes_requested"):
                return {"outcome": "held", "reason": "verified_separate_review_provenance_missing"}
            reviewer_claims = [event for event in task_snapshot.events
                               if event.get("kind") == "claimed" and str(event.get("run_id")) == run_id
                               and isinstance(event.get("payload"), Mapping)]
            if separate is None:
                reviewer_claims = [event for event in reviewer_claims
                                   if event["payload"].get("source_status") == "review"]
            elif not self._verified_separate_review(task_id, candidate, expected_profile, run_id, session_id,
                                                     changes_requested=verdict == "changes_requested"):
                reviewer_claims = []
            if len(reviewer_claims) != 1:
                return {"outcome": "held", "reason": "review_run_not_claimed_from_native_review" if separate is None else "separate_review_run_not_claimed_from_ready"}
            if verdict == "approved":
                if task_snapshot.native_task.get("status") != "done":
                    return {"outcome": "held", "reason": "review_approval_not_terminal_done"}
                completions = [event for event in task_snapshot.events
                               if event.get("kind") == "completed" and str(event.get("run_id")) == run_id]
                if len(completions) != 1:
                    return {"outcome": "held", "reason": "review_done_without_exact_completion_event"}
            if verdict == "approved" and not self._prior_changes_are_reconciled(task_id, state):
                return {"outcome": "held", "reason": "prior_native_correction_unreconciled"}
            stored = self.store.record_review(self.scope, candidate, review)
            if stored["verdict"] == "approved":
                return {"outcome": "accepted", "candidate": candidate.content_identity, "review_id": stored["review_id"]}
            return {"outcome": "changes_requested", "candidate": candidate.content_identity, "review_id": stored["review_id"]}

    def request_corrections(self, task_id: str, candidate: Any, review: Mapping[str, Any], *,
                            implementation_profile: str, operation_key: str, reason: str = "cover edge case") -> dict[str, Any]:
        """Reserve, but never send, a reviewer-owned native request-changes call."""
        from .contracts import CandidateIdentity
        configured_implementation, configured_reviewer = self._local_review_roles()
        if not isinstance(candidate, CandidateIdentity) or not isinstance(review, Mapping):
            raise ValueError("candidate and structured review are required")
        if not all(isinstance(value, str) and value for value in (implementation_profile, operation_key, reason)):
            raise ValueError("bounded correction values are required")
        if implementation_profile != configured_implementation:
            raise ValueError("correction implementation profile must match configured role")
        self._managed_task(task_id)
        native = review.get("native_review")
        if not isinstance(native, Mapping):
            raise ValueError("changes require a distinct local reviewer and exact native provenance")
        reviewer = native.get("profile")
        run_id = native.get("run_id")
        session_id = native.get("session_id")
        if (review.get("verdict") != "changes_requested" or native.get("task_id") != task_id
                or not all(isinstance(value, str) and value for value in (reviewer, run_id, session_id))
                or reviewer != configured_reviewer):
            raise ValueError("changes require a distinct local reviewer and exact native provenance")
        assert isinstance(reviewer, str) and isinstance(run_id, str) and isinstance(session_id, str)
        state = self.store.read_scope(self.scope)
        if not any(saved == dict(review) for saved in state["reviews"]):
            return {"outcome": "held", "reason": "recorded_changes_review_missing"}
        existing = [operation for operation in state["operations"]
                    if operation.key == operation_key and operation.effect == "request_changes"]
        if len(existing) == 1 and existing[0].phase == "applied":
            return {"outcome": "verified", "operation_key": operation_key}
        if len(existing) > 1:
            return {"outcome": "held", "reason": "correction_operation_ambiguous"}
        reviewer_run = self._scoped_run(task_id, run_id)
        self._exact_run(reviewer_run, run_id=run_id, profile=reviewer)
        if reviewer_run.get("status") != "running":
            return {"outcome": "held", "reason": "reviewer_owned_native_run_missing"}
        active_session = self._active_worker_session(task_id, run_id)
        if active_session != session_id:
            return {"outcome": "held", "reason": "reviewer_active_session_mismatch"}
        handoff = self._verified_handoff(task_id, candidate, reviewer)
        if handoff is None or handoff["marker"].get("implementation_profile") != implementation_profile:
            return {"outcome": "held", "reason": "verified_local_review_handoff_missing"}
        findings = review.get("findings")
        if (not isinstance(findings, list) or not findings
                or not all(isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
                           and item.get("finding_id") and isinstance(item.get("summary"), str) for item in findings)):
            return {"outcome": "held", "reason": "exact_correction_finding_required"}
        finding_ids = tuple(sorted(item["finding_id"] for item in findings))
        if len(set(finding_ids)) != len(finding_ids):
            return {"outcome": "held", "reason": "duplicate_correction_finding"}
        structured_findings = tuple(sorted((dict(item) for item in findings), key=lambda item: item["finding_id"]))
        before = self.board.read_task(task_id)
        if (not isinstance(before, BoardSnapshot) or before.native_task.get("status") != "running"
                or before.native_task.get("assignee") != reviewer):
            return {"outcome": "held", "reason": "reviewer_owned_native_run_missing"}
        action = Action(operation_key, self.scope, {
            "task_id": task_id, "candidate": candidate.to_dict(), "review_id": review.get("review_id"),
            "finding_ids": finding_ids, "findings": structured_findings,
            "review_run_id": run_id, "reviewer_profile": reviewer, "reviewer_session_id": session_id,
            "reviewer_session_receipt": active_session,
            "implementation_profile": implementation_profile, "reason": reason,
        }, "request_changes", before.digest)
        with self.lock:
            self._assert_lock()
            intent = self.store.read_scope(self.scope)["operator_intent"]
            if intent is not None and intent.active:
                return {"outcome": "held", "reason": "operator_pause_or_cancellation_active"}
            if self.budget_policy is None:
                return {"outcome": "held", "reason": "finite_correction_budget_required"}
            member = next(member for member in self._members() if member.task_id == task_id)
            event = {"event_id": f"{REVIEW_CORRECTIONS}:{operation_key}",
                     "lineage_id": f"{self.scope['anchor_task_id']}:{GENERAL_ATTEMPT}",
                     "root_task_id": self.scope["anchor_task_id"], "finding_id": GENERAL_ATTEMPT,
                     "generation": member.generation, "source_task_id": task_id,
                     "source_kind": "native_operation", "native_source_id": operation_key, "count": 1}
            try:
                stored = admit_repair_operation(self.budget_policy, self.store, self.scope, self._intent_for(action), event)
            except Exception as error:
                return {"outcome": "held", "reason": str(error)}
            return {"outcome": "verified" if stored.phase == "applied" else "proposed", "operation_key": operation_key}

    def reconcile_local_corrections(self, task_id: str, candidate: Any, *, operation_key: str) -> dict[str, Any]:
        """Read back one exact reviewer-owned request-changes transition."""
        from .contracts import CandidateIdentity
        configured_implementation, configured_reviewer = self._local_review_roles()
        if not isinstance(candidate, CandidateIdentity) or not isinstance(operation_key, str) or not operation_key:
            raise ValueError("candidate and correction operation key are required")
        self._managed_task(task_id)
        with self.lock:
            self._assert_lock()
            operations = [operation for operation in self.store.read_scope(self.scope)["operations"]
                          if operation.key == operation_key and operation.effect == "request_changes"]
            if len(operations) != 1:
                return {"outcome": "held", "reason": "correction_operation_missing"}
            operation = operations[0]
            target = operation.target
            if target.get("task_id") != task_id or target.get("candidate") != candidate.to_dict():
                return {"outcome": "held", "reason": "correction_operation_identity_mismatch"}
            if (target.get("implementation_profile") != configured_implementation
                    or target.get("reviewer_profile") != configured_reviewer):
                return {"outcome": "held", "reason": "correction_configured_role_mismatch"}
            if operation.phase == "applied":
                return {"outcome": "verified", "operation_key": operation_key}
            observed = self.board.read_task(task_id)
            if not isinstance(observed, BoardSnapshot):
                return {"outcome": "held", "reason": "native_correction_readback_unavailable"}
            run_id = target.get("review_run_id")
            events = [event for event in observed.events if event.get("kind") == "changes_requested"
                      and str(event.get("run_id")) == run_id and isinstance(event.get("payload"), Mapping)
                      and event["payload"].get("reason") == target.get("reason")
                      and event["payload"].get("implementer") == target.get("implementation_profile")
                      and event["payload"].get("reviewer") == target.get("reviewer_profile")
                      and event["payload"].get("status") == observed.native_task.get("status")]
            run = next((item for item in observed.runs if str(item.get("id")) == run_id), None)
            if (len(events) != 1 or not isinstance(run, Mapping) or str(run.get("id")) != run_id
                    or run.get("profile") != target.get("reviewer_profile")
                    or run.get("outcome") != "changes_requested" or run.get("status") != observed.native_task.get("status")
                    or target.get("reviewer_session_receipt") != target.get("reviewer_session_id")
                    or observed.native_task.get("status") not in {"ready", "todo"}
                    or observed.native_task.get("assignee") != target.get("implementation_profile")):
                return {"outcome": "held", "reason": "verified_native_correction_missing"}
            self.store.ack_effect(self.scope, operation_key, readback=observed.to_dict())
            return {"outcome": "verified", "operation_key": operation_key}


__all__ = ["Coordinator"]
