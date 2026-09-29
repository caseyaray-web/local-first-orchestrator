"""Scoped, bounded coordinator for durable operator containment and recovery."""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Callable, Mapping

from .budgets import BudgetPolicy, WORKFLOW_REPAIRS, permit_action
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
                 budget_policy: BudgetPolicy | None = None) -> None:
        self.scope = validate_scope(scope)
        if board is None or store is None or not isinstance(lock, InstanceLock):
            raise ValueError("explicit board, store, and InstanceLock are required")
        self._validate_board(board)
        self.board, self.store, self.lock = board, store, lock
        if not getattr(board, "is_fake", False):
            # Never trust a caller-supplied no-op callback as the mutation fence.
            # The adapter and coordinator must share this exact lock instance.
            board.create_lock_assertion = self._assert_native_lock
        self.git_observer, self.budget_policy = git_observer, budget_policy

    def _assert_native_lock(self, scope: Mapping[str, str], anchor_task_id: str) -> None:
        if validate_scope(scope) != self.scope or anchor_task_id != self.scope["anchor_task_id"]:
            raise ValueError("native mutation lock assertion has wrong scope")
        self.lock.assert_held()

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

    def _intent_for(self, action: Action) -> OperationIntent:
        return OperationIntent(action.key, action.scope, action.target, action.effect,
            action.expected_observed_identity,
            {"task_id": action.target.get("task_id"), "before_digest": action.expected_observed_identity},
            None, None, {"reconcile_only": True}, "pending")

    def _readback_proves(self, action: Action, result: ActionResult) -> bool:
        snapshot = self._snapshot_from_readback(result.readback)
        if snapshot is None or snapshot.native_task.get("id") != action.target.get("task_id"):
            return False
        status = self._state(snapshot)
        if action.effect == "hold":
            return status == "blocked" and not any(run.get("status") in {"active", "claimed", "running", "stopping"} for run in snapshot.runs)
        if action.effect == "release":
            return status in {"ready", "todo"}
        if action.effect == "stop_run":
            run_id = action.target.get("run_id")
            return isinstance(run_id, str) and any(run.get("id") == run_id and run.get("status") in {"cancelled", "completed", "done", "stopped"} for run in snapshot.runs)
        return False

    def _portable_readback(self, readback: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        """Detach immutable contract mappings before passing evidence to SQLite."""
        snapshot = self._snapshot_from_readback(readback)
        return None if snapshot is None else snapshot.to_dict()

    def _apply(self, action: Action) -> ActionResult:
        """Fence every durable write and acknowledge only an exact readback."""
        self._assert_lock()
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
        digests: dict[str, str] = {}
        for intent in state["operations"]:
            snapshot = self._snapshot_from_readback(intent.readback)
            task_id = intent.target.get("task_id")
            if intent.phase == "applied" and snapshot is not None and isinstance(task_id, str) and snapshot.native_task.get("id") == task_id:
                digests[task_id] = snapshot.digest
        return digests

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
            if (not same_active or apply_existing) and planned.actions:
                results.append(self._apply(planned.actions[0]))
            if stop and results and results[0].outcome in {"unsupported", "unknown"}:
                # Exact stop remains unproven.  Escalate only to an available hold;
                # do not replay or describe the unsupported run stop as success.
                fallback = plan_pause(self.scope, False, generation=generation, cancellation=cancellation,
                                      members=state["members"], snapshots=self._read()).actions
                if fallback:
                    results.append(self._apply(fallback[0]))
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
        if intent is not None and intent.active and native_digest_mismatches(
            self.scope, state["members"], snapshots, intent,
            verified_applied_digests=self._verified_applied_digests(state),
        ):
            decision = reconcile_operator_edits(self.scope, state["members"], (), snapshots, (), review_evidence=state["reviews"], pause_intent=intent)
            return {"outcome": "conflict", "report": decision.report, "actions_attempted": 0, "operator_intent": intent}
        decision = reconcile_operator_edits(self.scope, state["members"], (), snapshots, (), candidate_evidence=(), review_evidence=state["reviews"], pause_intent=intent)
        # A durable resuming phase deliberately contains a mix of held and released
        # members; exact baseline/readback checks above make that transition safe.
        outcome = "verified" if intent is not None and intent.resuming and not self.store.pending_operations(self.scope) else decision.outcome
        return {"outcome": "partial" if self.store.pending_operations(self.scope) else outcome,
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
            unknown = bool(self.store.pending_operations(self.scope))
            exhausted = bool(self.budget_policy and not permit_action(self.budget_policy, self.store, self.scope, WORKFLOW_REPAIRS))
            safe = ReconciliationDecision("verified", (), reconciliation["report"]) if reconciliation["outcome"] == "verified" else None
            snapshots = self._read()
            # Continue a persisted multi-member release without generating fresh
            # action identities.  The stored key is the proof identity.
            if state["operator_intent"].resuming:
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

    def tick(self) -> dict[str, Any]:
        """Acquire the singleton before every read, reconciliation, decision, and effect."""
        try:
            with self.lock:
                self._assert_lock()
                state = self.store.read_scope(self.scope)
                if state["operator_intent"] is None or not state["operator_intent"].active:
                    return {"outcome": "no-op", "actions_attempted": 0}
                reconciliation = self._reconcile_locked()
                if reconciliation["outcome"] in {"conflict", "unknown"}:
                    return {"outcome": "held", "actions_attempted": 0, "reason": reconciliation["outcome"]}
                if self.store.pending_operations(self.scope):
                    return {"outcome": "partial", "actions_attempted": 0, "reason": "unresolved_effect_intent"}
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
    def submit_plan(self, *args: Any, **kwargs: Any) -> None: raise NotImplementedError("M3 planning is not implemented")
    def submit_review(self, *args: Any, **kwargs: Any) -> None: raise NotImplementedError("M3 review is not implemented")
    def request_corrections(self, *args: Any, **kwargs: Any) -> None: raise NotImplementedError("M3 corrections are not implemented")


__all__ = ["Coordinator"]
