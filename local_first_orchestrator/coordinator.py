"""Scoped, bounded coordinator for durable operator containment and recovery."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .budgets import GENERAL_ATTEMPT, PAID_CAPACITY, REVIEW_CORRECTIONS, BudgetPolicy, WORKFLOW_REPAIRS, admit_repair_operation, permit_action
from .contracts import Action, ActionResult, BoardSnapshot, OperationIntent, PauseIntent, validate_scope
from .daemon import InstanceLock
from .operator_controls import (
    ReconciliationDecision, native_digest_mismatches, plan_pause, plan_resume,
    reconcile_operator_edits, reject_late_result, verify_containment,
)


class Coordinator:
    """Owns the durable fence around at most one native containment effect."""

    def __init__(self, scope: Mapping[str, Any], *, board: Any, store: Any,
                 lock: InstanceLock, git_observer: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None,
                 budget_policy: BudgetPolicy | None = None,
                 configured_roles: Mapping[str, str] | None = None,
                 planning_observer: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None,
                 planning_profile: str | None = None,
                 planning_workspace: str | None = None) -> None:
        self.scope = validate_scope(scope)
        if board is None or store is None or not isinstance(lock, InstanceLock):
            raise ValueError("explicit board, store, and InstanceLock are required")
        self._validate_board(board)
        self.board, self.store, self.lock = board, store, lock
        if not getattr(board, "is_fake", False):
            # Never trust a caller-supplied no-op callback as the mutation fence.
            # The adapter and coordinator must share this exact lock instance.
            board.create_lock_assertion = self._assert_native_lock
            board.claim_create_attempt = self._claim_native_create_attempt
            board.accepted_piece_create_lookup = self._accepted_piece_description
        if configured_roles is not None and not isinstance(configured_roles, Mapping):
            raise ValueError("configured roles must be a mapping")
        self.configured_roles = MappingProxyType(dict(configured_roles or {}))
        self.git_observer, self.budget_policy = git_observer, budget_policy
        # Trusted composition-root dependency. The callback observes current
        # repository, base, contract and approved objective/configuration itself.
        self.planning_observer = planning_observer
        self.planning_profile = planning_profile
        self.planning_workspace = planning_workspace

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
        built = accepted_active_tranche_create_payload(thaw(token), evidence, tickets[0])
        if intent.target.get("accepted_token_key") != built.target["accepted_token_key"]:
            raise ValueError("reserved token identity does not match rebuilt payload")
        return type("AcceptedPieceDescription", (), {
            "title": built.title, "body": built.body, "target": built.target,
            "idempotency_key": built.idempotency_key, "plan_evidence": evidence,
        })()

    def prepare_active_piece(self, plan_id: str, ticket_id: str, *, request_id: str | None = None) -> Mapping[str, Any]:
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
        with self.lock:
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
            payload = __import__("local_first_orchestrator.planning_coordinator", fromlist=["accepted_active_tranche_create_payload"]).accepted_active_tranche_create_payload(
                thaw_accepted(token), evidence, target)
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
            if prior.phase == "applied":
                verifier = getattr(self.board, "verify_effect", None)
                if not callable(verifier):
                    return {"outcome":"partial", "reason":"read-only applied receipt verifier unavailable", "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":0}
                result = verifier(native_action)
            elif prior.phase == "unknown":
                verifier = getattr(self.board, "verify_effect", None)
                if callable(verifier) and action.target.get("kind") == "accepted_active_tranche_piece_v1":
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
                result = self.board.create_held(native_action, title=built.title, body=built.body,
                    assignee=implementation, workspace=f"dir:{workspace}", idempotency_key=operation_key)
            readback = self._portable_readback(result.readback)
            self.store.record_effect_observation(self.scope, operation_key, outcome=result.outcome, details=result.details, readback=readback)
            proof = self._snapshot_from_readback(readback)
            if result.outcome not in {"verified", "no-op"} or proof is None:
                return {"outcome":"partial", "reason":result.details, "plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
            if prior.phase != "applied":
                self.store.ack_effect(self.scope, operation_key, readback=proof.to_dict(), outcome=result.outcome)
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
            except (OSError, RuntimeError) as error:
                return {"outcome":"partial", "reason":f"membership registration failed after native acknowledgement: {error}","plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}
            return {"outcome":"held", "task_id":member.task_id,"plan_id":plan_id,"ticket_id":ticket_id,"operation_key":operation_key,"actions_attempted":attempted}

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
            anchor = self.board.read_task(self.scope["anchor_task_id"])
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
        if set(roles) not in (two, three):
            raise ValueError("configured implementation and local-review roles are required")
        implementation, reviewer = roles["implementation_profile"], roles["local_review_profile"]
        if (not isinstance(implementation, str) or not implementation
                or not isinstance(reviewer, str) or not reviewer
                or implementation == reviewer):
            raise ValueError("configured implementation and local-review roles must be distinct non-empty profiles")
        return implementation, reviewer

    def _planning_roles(self) -> str:
        roles = self.configured_roles
        if set(roles) != {"implementation_profile", "local_review_profile", "planning_profile"}:
            raise ValueError("configured planning role is required")
        implementation, reviewer, planner = (roles[key] for key in
            ("implementation_profile", "local_review_profile", "planning_profile"))
        if any(not isinstance(role, str) or not role for role in (implementation, reviewer, planner)):
            raise ValueError("configured roles must be non-empty profiles")
        if len({implementation, reviewer, planner}) != 3:
            raise ValueError("planning, implementation, and local-review roles must be distinct")
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
    def _snapshot_from_readback(readback: Mapping[str, Any] | None) -> BoardSnapshot | None:
        if not isinstance(readback, Mapping):
            return None
        try:
            return BoardSnapshot.from_dict({
                key: list(value) if key in {"parents", "runs", "comments", "events", "attachments"} and isinstance(value, (tuple, list)) else value
                for key, value in readback.items()
            })
        except (TypeError, ValueError, KeyError):
            return None

    def status(self) -> dict[str, Any]:
        state, snapshots = self.store.read_scope(self.scope), self._read()
        native_tasks = {str(item.native_task["id"]): item for item in snapshots}
        if self.scope["anchor_task_id"] not in native_tasks:
            anchor = self.board.read_task(self.scope["anchor_task_id"])
            if not isinstance(anchor, BoardSnapshot):
                raise ValueError("board adapter must return BoardSnapshot")
            native_tasks[self.scope["anchor_task_id"]] = anchor
        git = None if self.git_observer is None else self.git_observer(dict(self.scope))
        if git is not None and not isinstance(git, Mapping):
            raise ValueError("trusted git observer must return a mapping")
        return {**state, "native_tasks": native_tasks,
                "native_runs": tuple(run for item in native_tasks.values() for run in item.runs),
                "git_observation": git}

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
        return None if snapshot is None else snapshot.to_dict()

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

    def _verified_applied_digests(self, state: Mapping[str, Any]) -> dict[str, str]:
        """Return exact native digests, including a verified partial hold.

        A blocked task can still have an active worker, so that hold cannot ack as
        full containment.  Its immutable verified readback nevertheless explains
        the native digest and must not be mistaken for a human edit.
        """
        digests: dict[str, str] = {}
        operations = {intent.key: intent for intent in state["operations"]}
        for intent in operations.values():
            snapshot = self._snapshot_from_readback(intent.readback)
            task_id = intent.target.get("task_id")
            if intent.phase == "applied" and snapshot is not None and isinstance(task_id, str) and snapshot.native_task.get("id") == task_id:
                digests[task_id] = snapshot.digest
        for observation in state["effect_observations"]:
            intent = operations.get(observation.get("operation_key"))
            snapshot = self._snapshot_from_readback(observation.get("readback"))
            if (intent is not None and intent.effect == "hold" and observation.get("outcome") in {"verified", "no-op"}
                    and snapshot is not None and snapshot.native_task.get("id") == intent.target.get("task_id")
                    and self._state(snapshot) == "blocked"):
                digests[str(intent.target["task_id"])] = snapshot.digest
        return digests

    def _unresolved_action(self, action: Action) -> bool:
        """Never replay an ambiguous target under a fresh observation key."""
        return any(
            intent.effect == action.effect and dict(intent.target) == dict(action.target)
            for intent in self.store.pending_operations(self.scope)
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
            if operation.effect == "hold" and operation.phase == "applied"
            and operation.expected_observed_identity == intent.baseline_digests.get(str(operation.target.get("task_id")))
            and (proof := self._snapshot_from_readback(operation.readback)) is not None
            and proof.native_task.get("id") == operation.target.get("task_id")
            and self._state(proof) == "blocked"
        }
        for task_id in members:
            snapshot = observed.get(task_id)
            if snapshot is None or any(
                not isinstance(run.get("status"), str) or run.get("status") not in {
                    "active", "claimed", "running", "stopping", "cancelled", "completed", "done", "stopped"
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

    def _reconcile_locked(self) -> dict[str, Any]:
        self._assert_lock()
        self._reconcile_unknown_effects()
        state, snapshots = self.store.read_scope(self.scope), self._read()
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
        if intent is not None and intent.active and native_digest_mismatches(
            self.scope, state["members"], snapshots, intent,
            verified_applied_digests=self._verified_applied_digests(state),
        ):
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
            unknown = bool(self.store.pending_operations(self.scope))
            safe = ReconciliationDecision("verified", (), reconciliation["report"]) if reconciliation["outcome"] == "verified" else None
            snapshots = self._read()
            # Continue a persisted multi-member release without generating fresh
            # action identities.  The stored key is the proof identity.
            if intent.resuming:
                eligible = next((item for item in snapshots if self._state(item) == "blocked" and str(item.native_task["id"]) in state["operator_intent"].resuming_action_keys), None)
                exhausted = eligible is not None and self._resume_budget_exhausted(
                    state, release_task_id=str(eligible.native_task["id"]),
                )
                if unknown or exhausted or reconciliation["outcome"] != "verified":
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
                unknown_effects=unknown, budget_exhausted=exhausted, unsafe_human_edits=reconciliation["outcome"] == "conflict",
                operator_authorized_resume=authorized_clear, members=state["members"], snapshots=snapshots)
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
                if state["operator_intent"].resuming:
                    # A normal poll must not re-hold members released by an
                    # explicitly authorized, durable multi-member resume.
                    return {"outcome": "partial", "actions_attempted": 0, "reason": "resuming_requires_explicit_resume"}
                return self._pause(stop=state["operator_intent"].stop_requested,
                                   cancellation=state["operator_intent"].cancellation_requested,
                                   apply_existing=True, already_locked=True)
        except Exception as error:
            return {"outcome": "held", "actions_attempted": 0, "reason": str(error)}

    def recover(self) -> dict[str, Any]: return self.tick()
    def enroll(self, *args: Any, **kwargs: Any) -> None: raise NotImplementedError("M2 enrollment is not implemented")
    def report_issue(self, *args: Any, **kwargs: Any) -> None: raise NotImplementedError("M2 recovery reporting is not implemented")
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

    @staticmethod
    def _active_worker_session(task_id: str, run_id: str) -> str:
        """Read the native tool's own worker context, never caller payload.

        Hermes exposes the session only to the active worker until
        ``kanban_request_review`` closes the run and stamps its metadata.  The
        same task/run environment is the native lifecycle ownership fence, so a
        controller process or a sibling worker cannot nominate a session here.
        """
        task = os.environ.get("HERMES_KANBAN_TASK")
        active_run = os.environ.get("HERMES_KANBAN_RUN_ID")
        session = os.environ.get("HERMES_SESSION_ID")
        if task != task_id or active_run != run_id or not isinstance(session, str) or not session:
            raise ValueError("local-review handoff reservation requires the active worker-owned tool session")
        return session

    def _scoped_run(self, task_id: str, run_id: str) -> Mapping[str, Any]:
        reader = getattr(self.board, "read_scoped_run", None)
        if not callable(reader):
            raise ValueError("board cannot verify exact native run attribution")
        result = reader(self.scope, task_id, run_id)
        if not isinstance(result, Mapping):
            raise ValueError("board returned malformed native run attribution")
        return result

    def request_local_review(self, task_id: str, candidate: Any, *, implementation_profile: str,
                             reviewer_profile: str, summary: str, operation_key: str) -> dict[str, Any]:
        """Durably propose a handoff which only the implementation worker can send."""
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
        before = self.board.read_task(task_id)
        if not isinstance(before, BoardSnapshot):
            raise ValueError("board adapter must return BoardSnapshot")
        marker = {"operation_key": operation_key, "candidate": candidate.to_dict(),
                  "implementation_run_id": candidate.originating_run_id,
                  "implementation_profile": implementation_profile,
                  "implementation_session_id": implementation_session,
                  "reviewer_profile": reviewer_profile}
        action = Action(operation_key, self.scope, {
            "task_id": task_id, "reviewer_profile": reviewer_profile, "summary": summary,
            "candidate_content_identity": candidate.content_identity,
            "review_marker": marker,
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
        if not self._trusted_candidate_observation(candidate):
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
                if getattr(self.board, "is_fake", False):
                    self.store.begin_effect_attempt(self.scope, key)
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
            if (self._separate_review_intent(review_task_id, candidate, reviewer) is None
                    or not self._verified_separate_review(review_task_id, candidate, reviewer,
                                                          native.get("run_id"), native.get("session_id"))
                    or not self._frozen_candidate(candidate, review)):
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
                if getattr(self.board, "is_fake", False):
                    self.store.begin_effect_attempt(self.scope, key)
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

    def _trusted_candidate_observation(self, candidate: Any) -> bool:
        if self.git_observer is None:
            return False
        try:
            observed = self.git_observer(dict(self.scope))
        except Exception:
            return False
        if not isinstance(observed, Mapping) or observed.get("candidate") != candidate.to_dict():
            return False
        checks = observed.get("checks")
        return isinstance(checks, list) and bool(checks) and all(
            isinstance(check, Mapping) and check.get("outcome") == "passed" for check in checks
        )

    def _frozen_candidate(self, candidate: Any, review: Mapping[str, Any]) -> bool:
        """Bind a verdict to observer-owned candidate, checks, and contract scope.

        ``git_observer`` is a composition-root trust boundary: it must read the
        frozen candidate and deterministic validation output itself.  Reviewer
        text can attest to neither.  There is intentionally no fallback when a
        trusted observer is unavailable.
        """
        if self.git_observer is None:
            return False
        try:
            observed = self.git_observer(dict(self.scope))
        except Exception:
            return False
        if not isinstance(observed, Mapping):
            return False
        frozen, checks, criterion_ids = (observed.get("candidate"), observed.get("checks"),
                                         observed.get("criterion_ids"))
        if not isinstance(frozen, Mapping) or dict(frozen) != candidate.to_dict():
            return False
        if (not isinstance(checks, list) or not checks or not isinstance(criterion_ids, list) or not criterion_ids
                or not all(isinstance(item, str) and item for item in criterion_ids)
                or criterion_ids != sorted(set(criterion_ids))):
            return False
        try:
            from .evidence_store import _canonical_identity
            checks_identity = _canonical_identity(checks)
        except (TypeError, ValueError):
            return False
        if (observed.get("checks_identity") != checks_identity
                or review.get("checks") != checks
                or review.get("checks_identity") != checks_identity):
            return False
        if any(not isinstance(check, Mapping) or set(check) != {"check_id", "outcome", "evidence"}
               or not all(isinstance(check.get(field), str) and check[field] for field in ("check_id", "outcome", "evidence"))
               or check.get("outcome") != "passed" for check in checks):
            return False
        criteria = review.get("criterion_evidence")
        if not isinstance(criteria, list) or len(criteria) != len(criterion_ids):
            return False
        if any(not isinstance(item, Mapping) or set(item) != {"criterion_id", "outcome", "evidence"}
               or not all(isinstance(item.get(field), str) and item[field] for field in ("criterion_id", "outcome", "evidence"))
               or item.get("outcome") not in {"pass", "fail"} for item in criteria):
            return False
        return [item["criterion_id"] for item in criteria] == criterion_ids

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
                   if operation.effect == "request_review"
                   and operation.target.get("task_id") == task_id
                   and operation.target.get("reviewer_profile") == expected_profile
                   and operation.target.get("candidate_content_identity") == candidate.content_identity]
        if len(matches) != 1:
            return None
        marker = matches[0].target.get("review_marker")
        if (not isinstance(marker, Mapping) or marker.get("candidate") != candidate.to_dict()
                or marker.get("implementation_profile") != configured_implementation
                or marker.get("reviewer_profile") != configured_reviewer):
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
        intent = matches[0]
        if intent.phase == "pending":
            try:
                self.store.ack_effect(self.scope, intent.key, readback=observed.to_dict())
                self.store.record_candidate(self.scope, candidate)
            except Exception:
                return None
        elif intent.phase != "applied":
            return None
        return {"marker": marker, "implementation_session_id": implementation_session}

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
            if create is None or not self._trusted_candidate_observation(candidate):
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
            if not self._frozen_candidate(candidate, review):
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
