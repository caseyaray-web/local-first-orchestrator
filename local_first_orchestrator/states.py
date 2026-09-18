from __future__ import annotations

from enum import StrEnum


class CanonicalState(StrEnum):
    DRAFT = "draft"
    NEEDS_ARCHITECTURE = "needs_architecture"
    READY_LOCAL = "ready_local"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    LOCAL_REVIEW = "local_review"
    REPAIRING = "repairing"
    ACCEPTED = "accepted"
    NEEDS_HUMAN_TEST = "needs_human_test"
    NEEDS_CHECKPOINT = "needs_checkpoint"
    NEEDS_TRIAGE = "needs_triage"
    BLOCKED = "blocked"
    DONE = "done"
    REJECTED = "rejected"
    REVERTED = "reverted"


class InvalidTransition(ValueError):
    pass


_ALLOWED: dict[CanonicalState, frozenset[CanonicalState]] = {
    CanonicalState.DRAFT: frozenset({CanonicalState.NEEDS_ARCHITECTURE, CanonicalState.READY_LOCAL, CanonicalState.BLOCKED, CanonicalState.REJECTED}),
    CanonicalState.NEEDS_ARCHITECTURE: frozenset({CanonicalState.READY_LOCAL, CanonicalState.BLOCKED, CanonicalState.REJECTED}),
    CanonicalState.READY_LOCAL: frozenset({CanonicalState.IMPLEMENTING, CanonicalState.BLOCKED}),
    CanonicalState.IMPLEMENTING: frozenset({CanonicalState.VERIFYING, CanonicalState.REPAIRING, CanonicalState.NEEDS_TRIAGE, CanonicalState.BLOCKED}),
    CanonicalState.VERIFYING: frozenset({CanonicalState.LOCAL_REVIEW, CanonicalState.ACCEPTED, CanonicalState.REPAIRING, CanonicalState.NEEDS_TRIAGE, CanonicalState.BLOCKED}),
    CanonicalState.LOCAL_REVIEW: frozenset({CanonicalState.ACCEPTED, CanonicalState.REPAIRING, CanonicalState.NEEDS_CHECKPOINT, CanonicalState.NEEDS_TRIAGE}),
    CanonicalState.REPAIRING: frozenset({CanonicalState.IMPLEMENTING, CanonicalState.NEEDS_TRIAGE, CanonicalState.BLOCKED}),
    CanonicalState.ACCEPTED: frozenset({CanonicalState.DONE, CanonicalState.NEEDS_HUMAN_TEST, CanonicalState.NEEDS_CHECKPOINT, CanonicalState.REVERTED}),
    CanonicalState.NEEDS_HUMAN_TEST: frozenset({CanonicalState.DONE, CanonicalState.REPAIRING, CanonicalState.NEEDS_CHECKPOINT, CanonicalState.BLOCKED}),
    CanonicalState.NEEDS_CHECKPOINT: frozenset({CanonicalState.DONE, CanonicalState.REPAIRING, CanonicalState.NEEDS_TRIAGE, CanonicalState.BLOCKED, CanonicalState.REJECTED}),
    CanonicalState.NEEDS_TRIAGE: frozenset({CanonicalState.READY_LOCAL, CanonicalState.LOCAL_REVIEW, CanonicalState.REPAIRING, CanonicalState.NEEDS_CHECKPOINT, CanonicalState.BLOCKED, CanonicalState.REJECTED}),
    CanonicalState.BLOCKED: frozenset({CanonicalState.DRAFT, CanonicalState.READY_LOCAL, CanonicalState.NEEDS_TRIAGE, CanonicalState.REPAIRING, CanonicalState.REJECTED}),
    CanonicalState.DONE: frozenset(),
    CanonicalState.REJECTED: frozenset(),
    CanonicalState.REVERTED: frozenset(),
}


def validate_transition(current: CanonicalState, target: CanonicalState) -> None:
    if target not in _ALLOWED[current]:
        raise InvalidTransition(f"{current.value} -> {target.value} is not allowed")
