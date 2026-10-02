"""Pure reconciliation findings and bounded recovery proposals.

This module observes immutable board, evidence, and Git snapshots.  It never calls
an adapter, changes a board, persists evidence, or runs Git commands.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping

from .contracts import Action, ActionResult, BoardSnapshot, CandidateIdentity, RecoveryReport, validate_scope

_USABLE_VERDICTS = frozenset({"pass", "repair", "escalate"})


@dataclass(frozen=True, slots=True)
class RecoveryIssue:
    """A deterministic finding tied to one board scope and observed identity."""

    kind: str
    scope: Mapping[str, str]
    task_id: str
    run_id: str | None
    finding_id: str
    generation: int
    candidate: Mapping[str, str] | None
    details: Mapping[str, Any]
    observed_identity: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", validate_scope(self.scope))
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("issue kind is required")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("issue task_id is required")
        if self.run_id is not None and (not isinstance(self.run_id, str) or not self.run_id):
            raise ValueError("issue run_id must be a non-empty string or None")
        if not isinstance(self.finding_id, str) or not self.finding_id:
            raise ValueError("issue finding_id is required")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("issue generation must be non-negative")
        if not isinstance(self.observed_identity, str) or not self.observed_identity:
            raise ValueError("issue observed identity is required")
        object.__setattr__(self, "details", dict(self.details))
        if self.candidate is not None:
            object.__setattr__(self, "candidate", dict(self.candidate))


def detect_issues(snapshot: BoardSnapshot, evidence: Mapping[str, Any], git: Mapping[str, Any]) -> tuple[RecoveryIssue, ...]:
    """Classify safe-to-recognize mismatches from already-read fixture evidence."""
    if type(snapshot) is not BoardSnapshot or not isinstance(evidence, Mapping) or not isinstance(git, Mapping):
        raise ValueError("snapshot, evidence, and git observations are required")
    supplied_scope = evidence.get("scope")
    scope = validate_scope(supplied_scope if supplied_scope is not None else _scope_from_task(snapshot.native_task))
    members = tuple(evidence.get("members", ()))
    candidates = tuple(evidence.get("candidates", ()))
    reviews = tuple(evidence.get("reviews", ()))
    issues: list[RecoveryIssue] = []
    task = snapshot.native_task
    task_id = _id(task) or scope["anchor_task_id"]
    member = _member_for(members, task_id)
    candidate = _candidate_for(candidates, task_id, member)

    # A lane is observation only; approval always requires matching usable evidence.
    if _status(task) == "done" and _role(member) == "implementation":
        if candidate is None:
            issues.append(_issue("missing_candidate", scope, task_id, _run_id(snapshot.runs), _generation(member), None, {}, snapshot.digest))
        elif not _has_usable_review(reviews, candidate):
            issues.append(_issue("missing_local_review", scope, task_id, candidate.originating_run_id, _generation(member), candidate, {}, snapshot.digest))
    if _status(task) == "done" and _role(member) in {"local_review", "review", "paid_review"}:
        reviewed_candidate = _candidate_for_review(reviews, candidates, task_id)
        if reviewed_candidate is None or not _has_usable_review_for_task(reviews, task_id, reviewed_candidate):
            issues.append(_issue("missing_review_verdict", scope, task_id, _run_id(snapshot.runs), _generation(member), reviewed_candidate, {}, snapshot.digest))

    if candidate is not None and _has_usable_review(reviews, candidate):
        observed_identity = git.get("candidate", git)
        if isinstance(observed_identity, Mapping):
            identity_fields = ("repository_identity", "worktree", "base_sha", "head_sha", "content_identity", "diff_identity", "originating_run_id", "contract_hash")
            mismatches = tuple(field for field in identity_fields if field in observed_identity and observed_identity[field] != getattr(candidate, field))
            if mismatches:
                issues.append(_issue("stale_approval", scope, task_id, candidate.originating_run_id, _generation(member), candidate, {"mismatched_identity_fields": mismatches}, snapshot.digest))

    for operation in tuple(evidence.get("operations", ())):
        if isinstance(operation, Mapping) and operation.get("phase") == "unknown":
            target = operation.get("target") if isinstance(operation.get("target"), Mapping) else {}
            operation_task = _id(target) or task_id
            effect = str(operation.get("effect", ""))
            kind = "unknown_create" if effect.startswith("create") else "unknown_operation"
            details = {
                "operation_key": operation.get("key", "unknown"),
                "operation_effect": effect,
                "marker": operation.get("marker", operation.get("key", "unknown")),
                "operation_target": dict(target),
            }
            issues.append(_issue(kind, scope, operation_task, None, _generation(member), None, details, snapshot.digest))

    issues.extend(_duplicate_issues(scope, members, candidates, snapshot.digest, evidence.get("native_tasks", {})))
    if _status(task) == "running" and any(parent.get("accepted") is False for parent in snapshot.parents):
        parent_ids = tuple(_id(parent) for parent in snapshot.parents if parent.get("accepted") is False and _id(parent))
        live_run = _run_id(snapshot.runs)
        known_terminal = {"cancelled", "completed", "done", "stopped", "failed", "rejected"}
        statuses = {run.get("status") for run in snapshot.runs}
        if live_run is not None:
            kind = "dependent_early_running"
        elif snapshot.runs and statuses <= known_terminal:
            # The worker is conclusively over, so one normal held-card repair is
            # safer than claiming a stop that cannot have happened.
            kind = "dependent_early_ended_worker"
        else:
            # Missing or unrecognized run state is not evidence of an ended
            # worker. Leave its exact IDs visible for a human decision.
            kind = "dependent_early_ambiguous_worker"
        issues.append(_issue(kind, scope, task_id, live_run, _generation(member), candidate, {"parent_task_ids": parent_ids}, snapshot.digest))
    if any(_is_human_edit(event, task_id) for event in snapshot.events):
        preserved_ids: set[str] = {task_id}
        for item in members:
            item_id = _id(item)
            if item_id is not None:
                preserved_ids.add(item_id)
        preserved = tuple(sorted(preserved_ids))
        issues.append(_issue("human_board_edit", scope, task_id, _run_id(snapshot.runs), _generation(member), candidate, {"preserve_task_ids": preserved}, snapshot.digest))
    priority = {"dependent_early_running": 0, "dependent_early_ended_worker": 0,
                "dependent_early_ambiguous_worker": 0}
    return tuple(sorted({deduplicate_issue(issue): issue for issue in issues}.values(), key=lambda item: (priority.get(item.kind, 1), item.kind, item.task_id, item.finding_id)))


def classify_issue(snapshot: BoardSnapshot, evidence: Mapping[str, Any], git: Mapping[str, Any]) -> tuple[RecoveryIssue, ...]:
    """Compatibility-free named classifier; identical to ``detect_issues``."""
    return detect_issues(snapshot, evidence, git)


def deduplicate_issue(issue: RecoveryIssue) -> str:
    """Stable scope/finding/candidate/generation identity, independent of lane data."""
    if type(issue) is not RecoveryIssue:
        raise ValueError("RecoveryIssue is required")
    source = "\x1f".join((issue.scope["board_id"], issue.scope["anchor_task_id"], issue.kind, issue.task_id, issue.finding_id, _candidate_fingerprint(issue.candidate), str(issue.generation)))
    return sha256(source.encode("utf-8")).hexdigest()


def propose_repair(issue: RecoveryIssue, budget: Mapping[str, Any]) -> Action | None:
    """Return at most one supported proposal; exhausted or unsafe cases escalate."""
    if type(issue) is not RecoveryIssue or not isinstance(budget, Mapping):
        raise ValueError("issue and budget are required")
    if _exhausted(budget):
        return None
    effect_by_kind = {
        "missing_local_review": "create_review",
        "missing_review_verdict": "create_replacement_review",
        "missing_candidate": "hold",
        "stale_approval": "create_review",
        "unknown_create": "reconcile_operation",
        "unknown_operation": "reconcile_operation",
        "duplicate_unstarted": "hold",
        "dependent_early_running": "stop_or_park",
        "dependent_early_ended_worker": "hold",
        "human_board_edit": "adopt_human_edit",
    }
    effect = effect_by_kind.get(issue.kind)
    if effect is None:
        return None
    target: dict[str, Any] = {"task_id": issue.task_id, "finding_id": issue.finding_id, "generation": issue.generation}
    if issue.run_id is not None:
        target["run_id"] = issue.run_id
    if issue.candidate is not None:
        target["candidate"] = dict(issue.candidate)
    target.update(issue.details)
    key = "recovery:" + deduplicate_issue(issue)[:24] + ":" + effect
    return Action(key, issue.scope, target, effect, issue.observed_identity)


def verify_repair(result: ActionResult) -> bool:
    """Accept only a verified action with exact non-empty readback evidence."""
    return type(result) is ActionResult and result.outcome == "verified" and bool(result.readback)


def summarize_escalation(scope: Mapping[str, Any], issues: tuple[RecoveryIssue, ...], *, attempted_actions: tuple[str, ...] = (), preserved_work: tuple[str, ...] = (), active_workers: tuple[str, ...] = ()) -> RecoveryReport:
    """Produce one actionable, deduplicated report without changing state."""
    checked_scope = validate_scope(scope)
    if not issues:
        raise ValueError("at least one issue is required")
    if any(type(issue) is not RecoveryIssue or dict(issue.scope) != checked_scope for issue in issues):
        raise ValueError("issues must belong to the supplied scope")
    unique = tuple(sorted({deduplicate_issue(issue): issue for issue in issues}.values(), key=lambda item: item.finding_id))
    problem = "; ".join(f"{issue.kind}:{issue.task_id}" for issue in unique)
    required = "Recovery budget exhausted or safe repair is unavailable; inspect preserved work and choose whether to resume, replace review, or keep the affected scope held."
    return RecoveryReport(checked_scope, problem, tuple(dict.fromkeys(attempted_actions)), tuple(dict.fromkeys(preserved_work or tuple(issue.task_id for issue in unique))), tuple(dict.fromkeys(active_workers)), required)


def _scope_from_task(task: Mapping[str, Any]) -> Mapping[str, str]:
    board_id, task_id = task.get("board_id"), task.get("anchor_task_id") or task.get("id")
    if not isinstance(board_id, str) or not isinstance(task_id, str):
        raise ValueError("evidence scope is required when board task lacks board and anchor identity")
    return {"board_id": board_id, "anchor_task_id": task_id}


def _issue(kind: str, scope: Mapping[str, str], task_id: str, run_id: str | None, generation: int, candidate: CandidateIdentity | None, details: Mapping[str, Any], observed: str) -> RecoveryIssue:
    detail_identity = json.dumps(details, sort_keys=True, separators=(",", ":"), default=str)
    finding = sha256((kind + "\x1f" + task_id + "\x1f" + _candidate_fingerprint(None if candidate is None else candidate.to_dict()) + "\x1f" + detail_identity + "\x1f" + str(generation)).encode()).hexdigest()[:24]
    return RecoveryIssue(kind, scope, task_id, run_id, finding, generation, None if candidate is None else candidate.to_dict(), details, observed)


def _id(value: Any) -> str | None:
    return value.get("id") if isinstance(value, Mapping) and isinstance(value.get("id"), str) and value.get("id") else None


def _status(task: Mapping[str, Any]) -> str:
    value = task.get("status", task.get("lane", ""))
    return value.lower() if isinstance(value, str) else ""


def _role(member: Any) -> str:
    return getattr(member, "role", "") if member is not None else ""


def _generation(member: Any) -> int:
    value = getattr(member, "generation", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _member_for(members: tuple[Any, ...], task_id: str) -> Any | None:
    return next((item for item in members if getattr(item, "task_id", None) == task_id), None)


def _candidate_for(candidates: tuple[Any, ...], task_id: str, member: Any) -> CandidateIdentity | None:
    exact = [item for item in candidates if type(item) is CandidateIdentity and (item.worktree.rstrip("/").endswith("/" + task_id) or item.originating_run_id == getattr(member, "run_id", None))]
    return exact[0] if exact else None


def _candidate_fingerprint(candidate: Mapping[str, Any] | None) -> str:
    if candidate is None:
        return "none"
    return sha256("\x1f".join(str(candidate.get(field, "")) for field in CandidateIdentity.__dataclass_fields__).encode("utf-8")).hexdigest()


def _review_candidate(item: Mapping[str, Any]) -> CandidateIdentity | None:
    payload = item.get("candidate", item.get("candidate_identity"))
    try:
        return CandidateIdentity.from_dict(payload) if isinstance(payload, Mapping) else None
    except ValueError:
        return None


def _review_has_exact_evidence(item: Mapping[str, Any], candidate: CandidateIdentity) -> bool:
    criteria = item.get("criterion_evidence")
    return (
        item.get("verdict") in _USABLE_VERDICTS
        and _review_candidate(item) == candidate
        and isinstance(item.get("check_identity"), str) and bool(item["check_identity"])
        and isinstance(item.get("native_review_provenance"), str) and bool(item["native_review_provenance"])
        and isinstance(criteria, (tuple, list)) and bool(criteria)
    )


def _has_usable_review(reviews: tuple[Any, ...], candidate: CandidateIdentity) -> bool:
    return any(isinstance(item, Mapping) and _review_has_exact_evidence(item, candidate) for item in reviews)


def _candidate_for_review(reviews: tuple[Any, ...], candidates: tuple[Any, ...], task_id: str) -> CandidateIdentity | None:
    observed = tuple(item for item in candidates if type(item) is CandidateIdentity)
    for item in reviews:
        if isinstance(item, Mapping) and item.get("task_id") == task_id:
            reviewed = _review_candidate(item)
            if reviewed is not None and reviewed in observed:
                return reviewed
    return None


def _has_usable_review_for_task(reviews: tuple[Any, ...], task_id: str, candidate: CandidateIdentity) -> bool:
    return any(isinstance(item, Mapping) and item.get("task_id") == task_id and _review_has_exact_evidence(item, candidate) for item in reviews)


def _run_id(runs: tuple[Mapping[str, Any], ...]) -> str | None:
    for run in runs:
        if run.get("status") in {"running", "claimed"} and _id(run):
            return _id(run)
    return None


def _duplicate_issues(scope: Mapping[str, str], members: tuple[Any, ...], candidates: tuple[Any, ...], observed: str,
                      native_tasks: Any = None) -> tuple[RecoveryIssue, ...]:
    groups: dict[tuple[str, int, str], list[Any]] = {}
    for item in members:
        if not hasattr(item, "task_id") or getattr(item, "role", "") != "implementation":
            continue
        groups.setdefault((getattr(item, "work_association", ""), _generation(item), _role(item)), []).append(item)
    found: list[RecoveryIssue] = []
    worked_ids = {item.worktree.rstrip("/").rsplit("/", 1)[-1] for item in candidates if type(item) is CandidateIdentity}
    for items in groups.values():
        if len(items) < 2:
            continue
        for redundant in sorted(items, key=lambda item: item.task_id)[1:]:
            current = native_tasks.get(redundant.task_id) if isinstance(native_tasks, Mapping) else None
            if isinstance(current, Mapping) and _status(current) in {"blocked", "done", "archived", "cancelled"}:
                continue
            kind = "duplicate_useful_work" if redundant.task_id in worked_ids else "duplicate_unstarted"
            found.append(_issue(kind, scope, redundant.task_id, None, _generation(redundant), None, {"canonical_task_id": sorted(item.task_id for item in items)[0]}, observed))
    return tuple(found)


def _is_human_edit(event: Mapping[str, Any], task_id: str) -> bool:
    return event.get("task_id", task_id) == task_id and event.get("actor") in {"human", "operator"} and event.get("kind") in {"board_edit", "manual_edit", "moved"}


def _exhausted(budget: Mapping[str, Any]) -> bool:
    value = budget.get("workflow_recovery", budget)
    if not isinstance(value, Mapping):
        return True
    limit, used = value.get("limit"), value.get("used", value.get("consumed", 0))
    return not isinstance(limit, int) or isinstance(limit, bool) or limit < 0 or not isinstance(used, int) or isinstance(used, bool) or used >= limit


__all__ = ["RecoveryIssue", "classify_issue", "deduplicate_issue", "detect_issues", "propose_repair", "summarize_escalation", "verify_repair"]
