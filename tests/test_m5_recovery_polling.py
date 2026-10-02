from __future__ import annotations

import hashlib
import json

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, CandidateIdentity, ManagedMember, PauseIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.recovery import RecoveryIssue
from tests.test_m4_public_core_flow import _install_board, _regression_core, _review, _snapshot
from tests.test_m4_active_piece_dependency_authority import _three_card_fixture


SCOPE = {"board_id": "board-m5", "anchor_task_id": "anchor-m5"}


def snapshot(task_id: str, status: str, digest: str, *, parents=(), runs=()) -> BoardSnapshot:
    return BoardSnapshot(
        {"id": task_id, "status": status}, tuple(parents), tuple(runs), (), (), (), "fixture-now", digest,
    )


class FixtureBoard:
    is_fake = True

    def __init__(self) -> None:
        self.cards = {
            "anchor-m5": snapshot("anchor-m5", "blocked", "anchor-held"),
            "work-a": snapshot("work-a", "ready", "work-a-ready"),
            "work-b": snapshot("work-b", "ready", "work-b-ready"),
        }
        self.holds = 0

    def read_task(self, task_id: str) -> BoardSnapshot:
        return self.cards[task_id]

    def hold(self, action, task_id: str, reason: str) -> ActionResult:
        self.holds += 1
        self.cards[task_id] = snapshot(task_id, "blocked", f"{task_id}-held")
        return ActionResult(action.key, "verified", reason, self.cards[task_id].to_dict())

    def release(self, action, task_id: str, reason: str) -> ActionResult:
        raise AssertionError("recovery poll must not release work")

    def stop_run(self, action, task_id: str, run_id: str, reason: str) -> ActionResult:
        raise AssertionError("recovery poll must not stop an unstarted duplicate")

    def verify_effect(self, action) -> ActionResult:
        card = self.cards[action.target["task_id"]]
        return ActionResult(action.key, "verified" if card.native_task["status"] == "blocked" else "unknown", "fixture readback", card.to_dict())


def coordinator(tmp_path, board: FixtureBoard, *, recovery_limit: int = 2):
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=not (tmp_path / "evidence.sqlite").exists())
    store.migrate()
    for member in (
        ManagedMember("board-m5", "anchor-m5", "anchor-m5", "root", 0, (), "root"),
        ManagedMember("board-m5", "anchor-m5", "work-a", "implementation", 0, (), "same-work"),
        ManagedMember("board-m5", "anchor-m5", "work-b", "implementation", 0, (), "same-work"),
    ):
        store.register_member(member)
    ctl = Coordinator(
        SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "coordinator.lock"),
        budget_policy=BudgetPolicy(2, 2, 2, recovery_limit, 1),
    )
    return ctl, store


def issue() -> RecoveryIssue:
    return RecoveryIssue(
        "duplicate_unstarted", SCOPE, "work-b", None, "same-finding", 0, None,
        {"canonical_task_id": "work-a"}, "work-b-ready",
    )


def test_concurrent_equivalent_reports_are_one_durable_recovery_generation(tmp_path):
    board = FixtureBoard()
    first, store = coordinator(tmp_path, board)
    try:
        assert first.report_issue(issue())["outcome"] == "recorded"
        second = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "other.lock"), budget_policy=BudgetPolicy(2, 2, 2, 2, 1))
        assert second.report_issue(issue())["outcome"] == "deduplicated"
        reports = [op for op in store.read_scope(SCOPE)["operations"] if op.effect == "recovery_report"]
        assert len(reports) == 1
        assert reports[0].phase == "applied"
    finally:
        store.close()


def test_poll_recovers_missed_duplicate_hook_after_reopen_with_one_budgeted_hold(tmp_path):
    board = FixtureBoard()
    first, store = coordinator(tmp_path, board)
    database = tmp_path / "evidence.sqlite"
    store.close()
    reopened = EvidenceStore.open(database)
    try:
        resumed = Coordinator(SCOPE, board=board, store=reopened, lock=instance_lock(tmp_path / "resumed.lock"), budget_policy=BudgetPolicy(2, 2, 2, 2, 1))
        result = resumed.poll()
        assert result["outcome"] == "repaired"
        assert board.holds == 1
        assert board.read_task("work-b").native_task["status"] == "blocked"
        assert resumed.poll()["actions_attempted"] == 0
        charged = [event for event in reopened.read_scope(SCOPE)["budget_events"] if event["event_id"].startswith("workflow_repairs:")]
        assert len(charged) == 1
    finally:
        reopened.close()


def test_exhausted_poll_records_one_durable_actionable_escalation_without_effect(tmp_path):
    board = FixtureBoard()
    ctl, store = coordinator(tmp_path, board, recovery_limit=0)
    try:
        first = ctl.poll()
        second = ctl.poll()
        assert first["outcome"] == "escalated"
        assert second["outcome"] == "escalated"
        assert board.holds == 0
        notices = [op for op in store.read_scope(SCOPE)["operations"] if op.effect == "recovery_escalation_notice"]
        assert len(notices) == 1
        assert notices[0].readback["required_operator_action"]
    finally:
        store.close()


def test_useful_duplicate_is_preserved_in_one_visible_escalation(tmp_path):
    from local_first_orchestrator.contracts import CandidateIdentity

    board = FixtureBoard()
    ctl, store = coordinator(tmp_path, board)
    try:
        # Candidate worktree suffix marks work-b as independently useful; recovery
        # must retain both it and its same-association peer rather than holding or
        # selecting either card.
        store.record_candidate(SCOPE, CandidateIdentity(
            "repo", "/work/work-b", "base", "head", "content", "diff", "run-b", "contract",
        ))
        result = ctl.poll()
        assert result["outcome"] == "escalated"
        assert result["report"].preserved_work == ("work-b", "work-a")
        assert board.holds == 0
        assert ctl.poll()["new_notice"] is False
    finally:
        store.close()


def test_dispatch_race_with_unstoppable_worker_reports_remaining_active_run(tmp_path):
    board = FixtureBoard()
    board.cards["work-b"] = snapshot(
        "work-b", "running", "work-b-running", parents=({"id": "work-a", "accepted": False},),
        runs=({"id": "run-b", "status": "running", "stop_supported": False},),
    )
    ctl, store = coordinator(tmp_path, board)
    try:
        result = ctl.poll()
        assert result["outcome"] == "escalated"
        assert result["actions_attempted"] == 0
        assert result["report"].active_workers == ("run-b",)
        assert board.holds == 0
        notices = [op for op in store.read_scope(SCOPE)["operations"] if op.effect == "recovery_escalation_notice"]
        assert len(notices) == 1
    finally:
        store.close()


def test_redundant_duplicate_containment_remains_held_after_reopen(tmp_path):
    board = FixtureBoard()
    ctl, store = coordinator(tmp_path, board)
    database = store.path
    try:
        repaired = ctl.poll()
        assert repaired["outcome"] == "repaired"
        state = store.read_scope(SCOPE)
        assert state["operator_intent"] is None
        holds = [op for op in state["operations"] if op.effect == "recovery_automatic_hold"]
        assert len(holds) == 1
        assert holds[0].readback["repair_reason"] == "duplicate_unstarted"
        assert holds[0].target["containment_kind"] == "permanent_redundant_containment"
    finally:
        store.close()

    # A duplicate's automatic hold is permanent containment, not a temporary
    # journal marker.  Reopen never unblocks it or clears it as "coherent".
    reopened = EvidenceStore.open(database)
    try:
        resumed = Coordinator(SCOPE, board=board, store=reopened,
                              lock=instance_lock(tmp_path / "resumed-auto-clear.lock"),
                              budget_policy=BudgetPolicy(2, 2, 2, 2, 1))
        cleared = resumed.poll()
        assert cleared["outcome"] == "held"
        assert cleared["reason"] == "redundant_duplicate_containment_remains_held_pending_operator_adjudication"
        state = reopened.read_scope(SCOPE)
        assert state["operator_intent"] is None
        clears = [op for op in state["operations"] if op.effect == "recovery_automatic_clear"]
        assert not clears
        assert board.read_task("work-b").native_task["status"] == "blocked"
    finally:
        reopened.close()


def test_ended_dependent_worker_is_held_after_exact_readback(tmp_path):
    board = FixtureBoard()
    board.cards["work-b"] = snapshot(
        "work-b", "running", "work-b-running", parents=({"id": "work-a", "accepted": False},),
        runs=({"id": "run-b", "status": "completed", "stop_supported": True},),
    )
    ctl, store = coordinator(tmp_path, board)
    try:
        result = ctl.poll()
        assert result["outcome"] == "repaired"
        assert board.holds == 1
        assert board.read_task("work-b").native_task["status"] == "blocked"
        operation = next(op for op in store.read_scope(SCOPE)["operations"] if op.key == result["operation_key"])
        assert operation.effect == "hold"
        assert operation.phase == "applied"
    finally:
        store.close()


def test_temporary_managed_accepted_hold_repairs_then_releases_once_after_reopen(tmp_path, monkeypatch):
    """A real recovery hold may bridge one exact accepted held-card receipt."""
    ctl, store, board, cards, runs = _regression_core(tmp_path, monkeypatch)
    database = store.path
    try:
        held = ctl.prepare_active_tranche("plan-1")
        assert ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")["outcome"] == "linked"
        task_id = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == "TK-B")
        cards[task_id] = _snapshot(cards[task_id], status="running", assignee="implementer")
        runs[task_id] = ({"id": "ended-too-early", "status": "completed"},)

        original_read = board.read_task
        planner_task_id = next(member.task_id for member in store.read_scope(ctl.scope)["members"] if member.role == "planner")

        def observed_read_task(observed_task_id):
            if observed_task_id == planner_task_id:
                return BoardSnapshot({"id": planner_task_id, "status": "blocked", "assignee": "default"}, (), (), (), (), (), "fixture", "planner-held")
            current = original_read(observed_task_id)
            if observed_task_id != task_id:
                return current
            return BoardSnapshot(current.native_task, tuple({**parent, "accepted": False} for parent in current.parents),
                                 current.runs, current.comments, current.events, current.attachments,
                                 current.observed_at, current.digest)

        board.read_task = observed_read_task

        def hold(action, target, _reason):
            assert target == task_id
            cards[target] = _snapshot(cards[target], status="blocked", assignee="implementer")
            runs[target] = ()
            return ActionResult(action.key, "verified", "held after ended worker", board.read_task(target).to_dict())

        board.hold = hold
        repaired = ctl.poll()
        assert repaired["outcome"] == "repaired"
        assert cards[task_id].native_task["status"] == "blocked"
    finally:
        store.close()

    reopened = EvidenceStore.open(database)
    try:
        resumed = Coordinator(ctl.scope, board=board, store=reopened,
                              lock=instance_lock(tmp_path / "temporary-release.lock"),
                              budget_policy=BudgetPolicy(5, 5, 5, 5, 5),
                              configured_roles={"implementation_profile": "implementer", "local_review_profile": "local"})
        released = resumed.poll()
        assert released["outcome"] == "released" and released["actions_attempted"] == 1
        assert cards[task_id].native_task["status"] == "ready"
        assert resumed.poll()["actions_attempted"] == 0
        events = [event for event in reopened.read_scope(ctl.scope)["budget_events"]
                  if event["event_id"].startswith("implementation_attempts:")]
        assert len(events) == 1
    finally:
        reopened.close()


def test_temporary_managed_hold_never_overrides_operator_pause(tmp_path, monkeypatch):
    ctl, store, board, cards, runs = _regression_core(tmp_path, monkeypatch)
    try:
        held = ctl.prepare_active_tranche("plan-1")
        assert ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")["outcome"] == "linked"
        task_id = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == "TK-B")
        cards[task_id] = _snapshot(cards[task_id], status="running", assignee="implementer")
        runs[task_id] = ({"id": "ended-too-early", "status": "completed"},)

        original_read = board.read_task
        planner_task_id = next(member.task_id for member in store.read_scope(ctl.scope)["members"] if member.role == "planner")

        def observed_read_task(observed_task_id):
            if observed_task_id == planner_task_id:
                return BoardSnapshot({"id": planner_task_id, "status": "blocked", "assignee": "default"}, (), (), (), (), (), "fixture", "planner-held")
            current = original_read(observed_task_id)
            if observed_task_id != task_id:
                return current
            return BoardSnapshot(current.native_task, tuple({**parent, "accepted": False} for parent in current.parents),
                                 current.runs, current.comments, current.events, current.attachments,
                                 current.observed_at, current.digest)

        board.read_task = observed_read_task

        def hold(action, target, _reason):
            cards[target] = _snapshot(cards[target], status="blocked", assignee="implementer")
            runs[target] = ()
            return ActionResult(action.key, "verified", "held", board.read_task(target).to_dict())

        board.hold = hold
        assert ctl.poll()["outcome"] == "repaired"
        store.set_operator_intent(PauseIntent(ctl.scope, "operator", 1, False, False))
        result = ctl.poll()
        assert result == {"outcome": "held", "reason": "operator_intent_active", "actions_attempted": 0}
        assert cards[task_id].native_task["status"] == "blocked"
    finally:
        store.close()


def test_paused_title_edit_is_durably_adopted_then_explicitly_released_after_reopen(tmp_path, monkeypatch):
    """A human title edit is a receipt-bound pause observation, never auto-release."""
    ctl, store, board, cards, runs = _regression_core(tmp_path, monkeypatch)
    database = store.path
    try:
        held = ctl.prepare_active_tranche("plan-1")
        task_id = held["pieces"][0]["task_id"]
        # Restore full current cards for the already-enrolled root/planner, then
        # let the ordinary pause write their held receipts too.
        original_read = board.read_task
        root_native = {"id": "anchor-A", "status": "ready", "assignee": "default", "title": "anchor",
                       "body": "fixture", "workspace": "dir:fixture", "workspace_path": "fixture"}
        cards["anchor-A"] = BoardSnapshot(root_native, (), (), (), (), (), "fixture",
                                           hashlib.sha256(json.dumps(root_native, sort_keys=True).encode()).hexdigest())
        planner_id = next(member.task_id for member in store.read_scope(ctl.scope)["members"] if member.role == "planner")
        planner_release = next(op for op in store.read_scope(ctl.scope)["operations"]
                               if op.effect == "release" and op.target.get("task_id") == planner_id)
        cards[planner_id] = ctl._snapshot_from_readback(planner_release.readback)
        assert cards[planner_id] is not None
        board.read_task = lambda task_id: original_read(task_id) if task_id not in cards else BoardSnapshot(
            cards[task_id].native_task, cards[task_id].parents, tuple(runs.get(task_id, ())),
            cards[task_id].comments, cards[task_id].events, cards[task_id].attachments,
            "fixture", cards[task_id].digest,
        )
        def release(action, observed_id, _reason):
            current = board.read_task(observed_id)
            cards[observed_id] = _snapshot(current, status="ready", assignee=current.native_task.get("assignee"))
            return ActionResult(action.key, "verified", "released", board.read_task(observed_id).to_dict())

        board.release = release

        def hold(action, observed_id, _reason):
            current = board.read_task(observed_id)
            cards[observed_id] = _snapshot(current, status="blocked", assignee=current.native_task.get("assignee"))
            return ActionResult(action.key, "verified", "held", board.read_task(observed_id).to_dict())
        board.hold = hold
        # Pause writes a complete prior native snapshot including an inert
        # historical blocked run; the later human edit must preserve it.
        cards[task_id] = _snapshot(cards[task_id], status="ready", assignee="implementer")
        runs[task_id] = ({"id": "historical-block", "status": "blocked"},)
        assert ctl.pause()["outcome"] == "partial"
        assert ctl.tick()["outcome"] == "partial"
        assert ctl.tick()["outcome"] == "verified"
        held_before_edit = board.read_task(task_id)
        state_before_edit = store.read_scope(ctl.scope)
        for mutation in ("body", "status", "parents", "dependencies"):
            altered_task = dict(held_before_edit.native_task)
            altered_parents = held_before_edit.parents
            if mutation == "body":
                altered_task["body"] = "human body edit"
            elif mutation == "status":
                altered_task["status"] = "ready"
            elif mutation == "parents":
                altered_parents = ({"id": "human-parent"},)
            else:
                altered_task["dependencies"] = ["human-child"]
            altered = BoardSnapshot(
                altered_task, altered_parents, held_before_edit.runs,
                held_before_edit.comments, held_before_edit.events, held_before_edit.attachments,
                held_before_edit.observed_at,
                hashlib.sha256(json.dumps(altered_task, sort_keys=True).encode()).hexdigest(),
            )
            snapshots = tuple(altered if snapshot.native_task.get("id") == task_id else snapshot
                              for snapshot in ctl._read())
            assert ctl._adopt_paused_metadata_observation_locked(
                state_before_edit, snapshots, state_before_edit["operator_intent"],
            ) is None, mutation
        native = dict(held_before_edit.native_task)
        native["title"] = native["title"] + " (human edit)"
        cards[task_id] = BoardSnapshot(
            native, held_before_edit.parents, held_before_edit.runs,
            held_before_edit.comments, held_before_edit.events, held_before_edit.attachments,
            held_before_edit.observed_at,
            hashlib.sha256(json.dumps(native, sort_keys=True).encode()).hexdigest(),
        )
        before_budgets = tuple(store.read_scope(ctl.scope)["budget_events"])
        assert ctl.reconcile()["outcome"] == "adopted"
        adoption = [op for op in store.read_scope(ctl.scope)["operations"]
                    if op.effect == "recovery_paused_metadata_adoption"]
        assert len(adoption) == 1
        assert adoption[0].readback["exact_changed_fields"] == {
            task_id: {"title": {"before": held_before_edit.native_task["title"], "after": native["title"]}}
        }
        assert tuple(store.read_scope(ctl.scope)["budget_events"]) == before_budgets
        assert board.read_task(task_id).runs == ({"id": "historical-block", "status": "blocked"},)
    finally:
        store.close()

    reopened = EvidenceStore.open(database)
    try:
        recovered = Coordinator(ctl.scope, board=board, store=reopened,
                                lock=instance_lock(tmp_path / "adopted-reopen.lock"),
                                budget_policy=ctl.budget_policy, configured_roles=ctl.configured_roles)
        polled = recovered.poll()
        assert polled["outcome"] == "adopted", polled
        assert board.read_task(task_id).native_task["status"] == "blocked"
        assert recovered._verified_paused_metadata_adoption_locked(
            reopened.read_scope(ctl.scope), recovered._read(), reopened.read_scope(ctl.scope)["operator_intent"],
        ) is not None
        exact_calls = []
        original_exact = recovered._resuming_phase_is_exact

        def capture_resuming_phase(state, snapshots, intent):
            result = original_exact(state, snapshots, intent)
            exact_calls.append({
                "exact": result,
                "intent_baselines": dict(intent.baseline_digests),
                "observed": {str(snapshot.native_task["id"]): snapshot.to_dict() for snapshot in snapshots},
                "release_readbacks": {
                    operation.key: operation.readback for operation in state["operations"]
                    if operation.effect == "release" and operation.phase == "applied"
                },
            })
            return result

        monkeypatch.setattr(recovered, "_resuming_phase_is_exact", capture_resuming_phase)
        release_count = len(reopened.read_scope(ctl.scope)["operator_intent"].managed_task_ids)
        for release_index in range(release_count):
            assert recovered.resume(authorized_clear=True) == {
                "outcome": "partial", "reason": "resuming_release_in_progress", "actions_attempted": 1,
            }, release_index
        assert len(exact_calls) == release_count - 1
        assert all(call["exact"] is True for call in exact_calls), exact_calls
        assert recovered.resume(authorized_clear=True) == {
            "outcome": "verified", "reason": "release_verified", "actions_attempted": 0,
        }
        assert recovered.resume(authorized_clear=True) == {
            "outcome": "held", "reason": "no_active_pause_to_clear", "actions_attempted": 0,
        }
        assert board.read_task(task_id).runs == ({"id": "historical-block", "status": "blocked"},)
        assert tuple(reopened.read_scope(ctl.scope)["budget_events"]) == before_budgets
        assert reopened.read_scope(ctl.scope)["operator_intent"].active is False
    finally:
        reopened.close()


def test_paused_human_dependency_repair_is_durably_adopted_without_native_resend(tmp_path, monkeypatch):
    """A human-created declared edge is observed during public reconcile only."""
    ctl, store, board, cards, runs = _regression_core(tmp_path, monkeypatch)
    database = store.path
    try:
        held = ctl.prepare_active_tranche("plan-1")
        source = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == "TK-A")
        target = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == "TK-B")
        planner = next(member.task_id for member in store.read_scope(ctl.scope)["members"] if member.role == "planner")
        planner_release = next(op for op in store.read_scope(ctl.scope)["operations"]
                               if op.effect == "release" and op.target.get("task_id") == planner)
        cards[planner] = ctl._snapshot_from_readback(planner_release.readback)
        original_read = board.read_task
        board.read_task = lambda task_id: (BoardSnapshot({"id": planner, "status": "blocked", "assignee": "default"},
            (), (), (), (), (), "fixture", "planner-held") if task_id == planner else original_read(task_id))
        managed = tuple(member.task_id for member in store.read_scope(ctl.scope)["members"])
        store.set_operator_intent(PauseIntent(ctl.scope, "operator", 7, False, False, active=True,
                                  managed_task_ids=managed,
                                  baseline_digests={task_id: board.read_task(task_id).digest for task_id in managed}))
        from local_first_orchestrator.contracts import Action
        assert board.link(Action("human-link", ctl.scope, {}, "link", "human"), source, target).outcome == "verified"
        current = board.read_task(target)
        cards[target] = BoardSnapshot(current.native_task, current.parents, current.runs, current.comments,
                                      current.events, current.attachments, current.observed_at, "human-repaired-digest")
        assert ctl.reconcile()["outcome"] == "adopted"
        receipt = next(op.readback for op in store.read_scope(ctl.scope)["operations"]
                       if op.effect == "recovery_human_dependency_adoption")
        assert receipt["declared_edge"]["source_task_id"] == source
        assert receipt["before_raw_cards"][target]["parents"] == ()
        assert receipt["current_raw_cards"][target]["parents"] == (source,)
        assert receipt["self_hash"].startswith("sha256:")
        assert not [op for op in store.read_scope(ctl.scope)["operations"] if op.effect == "link"]
    finally:
        store.close()


def test_explicit_resume_rejects_human_adoption_receipt_after_later_raw_edit(tmp_path, monkeypatch):
    """A valid adopted human edge becomes stale when a later raw body changes."""
    ctl, store, board, cards, runs = _regression_core(tmp_path, monkeypatch)
    database = store.path
    try:
        held = ctl.prepare_active_tranche("plan-1")
        source = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == "TK-A")
        target = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == "TK-B")
        planner = next(member.task_id for member in store.read_scope(ctl.scope)["members"] if member.role == "planner")
        planner_release = next(op for op in store.read_scope(ctl.scope)["operations"]
                               if op.effect == "release" and op.target.get("task_id") == planner)
        cards[planner] = ctl._snapshot_from_readback(planner_release.readback)
        original_read = board.read_task
        board.read_task = lambda task_id: (BoardSnapshot({"id": planner, "status": "blocked", "assignee": "default"},
            (), (), (), (), (), "fixture", "planner-held") if task_id == planner else original_read(task_id))
        managed = tuple(member.task_id for member in store.read_scope(ctl.scope)["members"])
        store.set_operator_intent(PauseIntent(ctl.scope, "operator", 7, False, False, active=True,
                                  managed_task_ids=managed,
                                  baseline_digests={task_id: board.read_task(task_id).digest for task_id in managed}))
        link_calls = []
        original_link = board.link
        def counted_human_link(action, source_task_id, target_task_id):
            link_calls.append((action.key, source_task_id, target_task_id))
            return original_link(action, source_task_id, target_task_id)
        board.link = counted_human_link
        assert board.link(Action("human-link", ctl.scope, {}, "link", "human"), source, target).outcome == "verified"
        current = board.read_task(target)
        cards[target] = BoardSnapshot(current.native_task, current.parents, current.runs, current.comments,
                                      current.events, current.attachments, current.observed_at, "human-edge-digest")
        assert ctl.reconcile()["outcome"] == "adopted"
        state = store.read_scope(ctl.scope)
        assert len([op for op in state["operations"] if op.effect == "recovery_human_dependency_adoption"]) == 1
        budgets_before = tuple(state["budget_events"])
        changed_task = dict(cards[target].native_task)
        changed_task["body"] = "later human raw edit"
        cards[target] = BoardSnapshot(changed_task, cards[target].parents, cards[target].runs,
                                      cards[target].comments, cards[target].events, cards[target].attachments,
                                      cards[target].observed_at, "later-human-body-digest")
    finally:
        store.close()

    reopened = EvidenceStore.open(database)
    try:
        recovered = Coordinator(ctl.scope, board=board, store=reopened,
                                lock=instance_lock(tmp_path / "stale-adoption.lock"),
                                budget_policy=ctl.budget_policy, configured_roles=ctl.configured_roles)
        reconciled = recovered.reconcile()
        assert reconciled["outcome"] == "conflict"
        resumed = recovered.resume(authorized_clear=True)
        assert resumed["outcome"] == "held" and resumed["actions_attempted"] == 0
        state = reopened.read_scope(ctl.scope)
        assert state["operator_intent"].active is True
        assert tuple(state["budget_events"]) == budgets_before
        assert len([op for op in state["operations"] if op.effect == "recovery_human_dependency_adoption"]) == 1
        assert link_calls == [("human-link", source, target)]
    finally:
        reopened.close()


def test_paused_human_declared_edge_survives_reopen_and_explicit_public_resume(tmp_path, monkeypatch):
    """A three-card accepted DAG repair is revalidated after restart, never title-adopted."""
    ctl, store, board, cards = _three_card_fixture(tmp_path, monkeypatch)
    database = store.path
    runs = _install_board(ctl, store, board, cards)
    try:
        held = ctl.prepare_active_tranche("plan-1")
        assert held["outcome"] == "held" and held["completed"] == held["total"] == 3
        tasks = {piece["ticket_id"]: piece["task_id"] for piece in held["pieces"]}
        planner = next(member.task_id for member in store.read_scope(ctl.scope)["members"] if member.role == "planner")
        planner_release = next(op for op in store.read_scope(ctl.scope)["operations"]
                               if op.effect == "release" and op.target.get("task_id") == planner)
        cards[planner] = ctl._snapshot_from_readback(planner_release.readback)
        assert cards[planner] is not None
        anchor_native = {"id": "anchor-A", "status": "ready", "assignee": "default", "title": "anchor",
                         "body": "fixture", "workspace": f"dir:{tmp_path}", "workspace_path": str(tmp_path)}
        cards["anchor-A"] = BoardSnapshot(anchor_native, (), (), (), (), (), "fixture",
                                           hashlib.sha256(json.dumps(anchor_native, sort_keys=True).encode()).hexdigest())
        original_read = board.read_task
        board.read_task = lambda task_id: original_read(task_id) if task_id not in cards else BoardSnapshot(
            cards[task_id].native_task, cards[task_id].parents, tuple(runs.get(task_id, ())),
            cards[task_id].comments, cards[task_id].events, cards[task_id].attachments, "fixture", cards[task_id].digest,
        )

        def hold(action, task_id, _reason):
            current = board.read_task(task_id)
            cards[task_id] = _snapshot(current, status="blocked", assignee=current.native_task.get("assignee"))
            return ActionResult(action.key, "verified", "held", board.read_task(task_id).to_dict())

        def release(action, task_id, _reason):
            current = board.read_task(task_id)
            status = "todo" if task_id == tasks["TK-B"] else "ready"
            cards[task_id] = _snapshot(current, status=status, assignee=current.native_task.get("assignee"))
            return ActionResult(action.key, "verified", "released", board.read_task(task_id).to_dict())

        board.hold, board.release = hold, release
        assert ctl.pause()["outcome"] == "partial"
        while True:
            paused = ctl.tick()
            if paused["outcome"] == "verified":
                break
            assert paused["outcome"] == "partial", paused
        state = store.read_scope(ctl.scope)
        assert set(state["operator_intent"].managed_task_ids) == set(cards)
        before_budget_events = tuple(state["budget_events"])
        raw_before = board.read_accepted_active_tranche_raw_cards(ctl.scope, tuple(sorted(tasks.values())))
        assert set(raw_before) == set(tasks.values())
        assert all(card["task"]["status"] == "blocked" and card["runs"] == [] for card in raw_before.values())

        candidate = CandidateIdentity("fixture-repo", str(tmp_path), "base", "head", "topology-content", "topology-diff", "implementation-topology", "topology-contract")
        store.record_review(ctl.scope, candidate, _review("stale-local", candidate, role="local", task_id=tasks["TK-A"], run_id="local-topology", session="local-topology-session"))
        store.record_review(ctl.scope, candidate, _review("stale-paid", candidate, role="paid", task_id=tasks["TK-B"], run_id="paid-topology", session="paid-topology-session"))
        native_link_calls = []
        original_link = board.link
        def counted_link(action, source_task_id, target_task_id):
            native_link_calls.append((action.key, source_task_id, target_task_id))
            return original_link(action, source_task_id, target_task_id)
        board.link = counted_link
        assert board.link(Action("human-declared-edge", ctl.scope, {}, "link", "human"), tasks["TK-A"], tasks["TK-B"]).outcome == "verified"
        current = board.read_task(tasks["TK-B"])
        cards[tasks["TK-B"]] = BoardSnapshot(current.native_task, current.parents, current.runs, current.comments,
                                               current.events, current.attachments, current.observed_at, "human-topology-digest")
        assert ctl.reconcile()["outcome"] == "adopted"
        state = store.read_scope(ctl.scope)
        adoption = next(op for op in state["operations"] if op.effect == "recovery_human_dependency_adoption")
        receipt = adoption.readback
        assert set(receipt["before_raw_cards"]) == set(tasks.values())
        assert ctl._plain_link_value(receipt["before_raw_cards"]) == raw_before
        assert ctl._plain_link_value(receipt["current_raw_cards"]) == board.read_accepted_active_tranche_raw_cards(ctl.scope, tuple(sorted(tasks.values())))
        assert receipt["declared_edge"] == {"source_ticket_id": "TK-A", "target_ticket_id": "TK-B",
                                             "source_task_id": tasks["TK-A"], "target_task_id": tasks["TK-B"]}
        assert receipt["self_hash"].startswith("sha256:")
        assert native_link_calls == [("human-declared-edge", tasks["TK-A"], tasks["TK-B"])]
        assert not [op for op in state["operations"] if op.effect == "link"]
    finally:
        store.close()

    reopened = EvidenceStore.open(database)
    try:
        recovered = Coordinator(ctl.scope, board=board, store=reopened,
                                lock=instance_lock(tmp_path / "topology-adoption-reopen.lock"),
                                budget_policy=ctl.budget_policy, configured_roles=ctl.configured_roles)
        assert recovered.reconcile()["outcome"] == "adopted"
        state = reopened.read_scope(ctl.scope)
        intent = state["operator_intent"]
        assert recovered._verified_paused_metadata_adoption_locked(state, recovered._read(), intent) is not None
        assert tuple(state["budget_events"]) == before_budget_events
        assert receipt["invalidated_review_ids"] == ("stale-local", "stale-paid")
        for _ in range(len(intent.managed_task_ids)):
            assert recovered.resume(authorized_clear=True)["outcome"] == "partial"
        assert recovered.resume(authorized_clear=True) == {"outcome": "verified", "reason": "release_verified", "actions_attempted": 0}
        assert reopened.read_scope(ctl.scope)["operator_intent"].active is False
        assert recovered.integrate_active_piece("plan-1", "TK-A", candidate, review_id="stale-local", git_adapter=object()) == {
            "outcome": "held", "reason": "human_dependency_adoption_invalidated_review_eligibility"}
        assert recovered.accept_tranche("plan-1", "stale-paid", git_adapter=object()) == {
            "outcome": "held", "reason": "human_dependency_adoption_invalidated_review_eligibility"}
        assert board.read_task(tasks["TK-B"]).native_task["status"] == "todo"
        assert all(board.read_task(task_id).native_task["status"] == "ready"
                   for task_id in intent.managed_task_ids if task_id != tasks["TK-B"])
        assert tuple(reopened.read_scope(ctl.scope)["budget_events"]) == before_budget_events
        assert recovered.poll()["actions_attempted"] == 0
        assert native_link_calls == [("human-declared-edge", tasks["TK-A"], tasks["TK-B"])]
        assert len([op for op in reopened.read_scope(ctl.scope)["operations"]
                   if op.effect == "recovery_human_dependency_adoption"]) == 1
    finally:
        reopened.close()
