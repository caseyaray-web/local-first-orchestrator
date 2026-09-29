import dataclasses
import json

import pytest

from local_first_orchestrator.contracts import (
    Action,
    ActionResult,
    BoardSnapshot,
    CandidateIdentity,
    ConflictError,
    ManagedMember,
    OperationIntent,
    PartialError,
    PauseIntent,
    RecoveryReport,
    UnknownError,
    UnsupportedError,
    validate_scope,
)


def scope():
    return {"board_id": "board-1", "anchor_task_id": "anchor-1"}


def test_board_snapshot_is_an_immutable_observation_with_bounded_serialization():
    snapshot = BoardSnapshot(
        native_task={"id": "task-1", "title": "work"},
        parents=({"id": "parent-1"},),
        runs=({"id": "run-1"},),
        comments=({"id": "comment-1"},),
        events=({"id": "event-1"},),
        attachments=({"id": "attachment-1"},),
        observed_at="2026-09-28T12:00:00Z",
        digest="sha256:observation",
    )

    assert json.loads(json.dumps(snapshot.to_dict())) == {
        "attachments": [{"id": "attachment-1"}],
        "comments": [{"id": "comment-1"}],
        "digest": "sha256:observation",
        "events": [{"id": "event-1"}],
        "native_task": {"id": "task-1", "title": "work"},
        "observed_at": "2026-09-28T12:00:00Z",
        "parents": [{"id": "parent-1"}],
        "runs": [{"id": "run-1"}],
    }
    assert BoardSnapshot.from_dict(snapshot.to_dict()) == snapshot
    with pytest.raises(ValueError, match="parents"):
        BoardSnapshot.from_dict({**snapshot.to_dict(), "parents": "not-a-list"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.digest = "changed"


def test_evidence_rejects_cycles_and_unbounded_nesting():
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="cyclic"):
        BoardSnapshot(native_task=cyclic, parents=(), runs=(), comments=(), events=(), attachments=(),
                      observed_at="now", digest="hash")
    deeply_nested = {}
    cursor = deeply_nested
    for _ in range(80):
        cursor["child"] = {}
        cursor = cursor["child"]
    with pytest.raises(ValueError, match="nesting"):
        BoardSnapshot(native_task=deeply_nested, parents=(), runs=(), comments=(), events=(), attachments=(),
                      observed_at="now", digest="hash")


def test_transport_lists_reject_strings_in_membership_and_recovery():
    member = ManagedMember("board-1", "anchor-1", "task-1", "implementation", 1, ("finding-1",), "piece-1")
    with pytest.raises(ValueError, match="finding_ids"):
        ManagedMember.from_dict({**member.to_dict(), "finding_ids": "abc"})
    report = RecoveryReport(scope(), "problem", ("inspect",), (), (), None)
    for field in ("attempted_actions", "preserved_work", "active_workers"):
        with pytest.raises(ValueError, match=field):
            RecoveryReport.from_dict({**report.to_dict(), field: "abc"})


def test_membership_and_candidate_record_only_evidence_not_a_board_lane():
    member = ManagedMember(
        board_id="board-1",
        anchor_task_id="anchor-1",
        task_id="task-1",
        role="implementation",
        generation=2,
        finding_ids=("finding-1",),
        work_association="piece-1",
    )
    candidate = CandidateIdentity(
        repository_identity="/repos/example",
        worktree="/worktrees/task-1",
        base_sha="base",
        head_sha="head",
        content_identity="content",
        diff_identity="diff",
        originating_run_id="run-1",
        contract_hash="contract",
    )

    assert member.to_dict() == {
        "board_id": "board-1",
        "anchor_task_id": "anchor-1",
        "task_id": "task-1",
        "role": "implementation",
        "generation": 2,
        "finding_ids": ["finding-1"],
        "work_association": "piece-1",
    }
    assert "lane" not in member.to_dict()
    assert CandidateIdentity.from_dict(candidate.to_dict()) == candidate


@pytest.mark.parametrize("bad_scope", [{}, {"task_id": "task-1"}, {"board_id": "board-1"}, {"anchor_task_id": "anchor-1"}])
def test_validate_scope_rejects_everything_except_board_and_anchor(bad_scope):
    with pytest.raises(ValueError):
        validate_scope(bad_scope)


def test_validate_scope_returns_a_detached_canonical_scope():
    source = scope()
    validated = validate_scope(source)
    source["board_id"] = "mutated"

    assert validated == {"board_id": "board-1", "anchor_task_id": "anchor-1"}


def test_operation_intent_requires_board_and_anchor_scope():
    fields = dict(key="hold:task-1:1", target={"task_id": "task-1"}, effect="hold",
                  expected_observed_identity="sha256:before", before_evidence={},
                  outcome=None, readback=None, retry={}, phase="pending")
    with pytest.raises(TypeError):
        OperationIntent(**fields)
    with pytest.raises(ValueError, match="scope"):
        OperationIntent(scope={"task_id": "task-1"}, **fields)


def test_operation_pause_recovery_and_action_use_effects_not_task_states():
    operation = OperationIntent(
        key="hold:task-1:1",
        scope=scope(),
        target={"task_id": "task-1"},
        effect="hold",
        expected_observed_identity="sha256:before",
        before_evidence={"digest": "sha256:before"},
        outcome="pending",
        readback=None,
        retry={"attempt": 1, "max_attempts": 2},
        phase="pending",
    )
    pause = PauseIntent(
        scope=scope(),
        origin="operator",
        generation=3,
        stop_requested=True,
        cancellation_requested=False,
    )
    report = RecoveryReport(
        scope=scope(),
        problem="missing verdict",
        attempted_actions=("inspect",),
        preserved_work=("task-1",),
        active_workers=("run-1",),
        required_operator_action="review ambiguity",
    )
    action = Action(
        key="hold:task-1:1",
        scope=scope(),
        target={"task_id": "task-1"},
        effect="hold",
        expected_observed_identity="sha256:before",
    )

    assert OperationIntent.from_dict(operation.to_dict()) == operation
    assert PauseIntent.from_dict(pause.to_dict()) == pause
    assert RecoveryReport.from_dict(report.to_dict()) == report
    assert Action.from_dict(action.to_dict()) == action
    with pytest.raises(ValueError, match="set_state"):
        Action(
            key="bad",
            scope=scope(),
            target={"task_id": "task-1"},
            effect="set_state",
            expected_observed_identity="sha256:before",
        )


@pytest.mark.parametrize(
    ("outcome", "error_type"),
    [
        ("conflict", ConflictError),
        ("unknown", UnknownError),
        ("unsupported", UnsupportedError),
        ("partial", PartialError),
    ],
)
def test_action_result_exposes_required_typed_error_outcomes(outcome, error_type):
    result = ActionResult(
        action_key="hold:task-1:1",
        outcome=outcome,
        details="native adapter response",
        readback={"id": "task-1"},
    )

    assert result.error is not None
    assert isinstance(result.error, error_type)
    assert ActionResult.from_dict(result.to_dict()) == result


@pytest.mark.parametrize("outcome", ["verified", "no-op", "retryable", "ambiguous"])
def test_action_result_accepts_non_error_outcomes(outcome):
    assert ActionResult("action-1", outcome, "observed", {"id": "task-1"}).error is None
