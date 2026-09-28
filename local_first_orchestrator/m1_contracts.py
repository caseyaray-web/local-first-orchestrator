from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator, Mapping

from .source_languages import normalized_repository_path


MAX_REFERENCES_PER_TICKET = 2048
MAX_PLAN_REFERENCES = 16384
MAX_VERIFICATION_COMMANDS = 32
MAX_ARGV_MEMBERS = 32
MAX_ARG_LENGTH = 256
MAX_ARGV_BYTES = 4096


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _unique(values: tuple[str, ...]) -> bool:
    return len(values) == len(set(values))


@dataclass(frozen=True)
class PatchBudget:
    max_files: int
    max_changed_lines: int
    max_attempts: int

    def __post_init__(self) -> None:
        if any(type(x) is not int or x <= 0 for x in (self.max_files, self.max_changed_lines, self.max_attempts)):
            raise ValueError("patch budgets must be positive finite integers")


@dataclass(frozen=True)
class VerificationProfile:
    commands: tuple[tuple[str, ...], ...]
    timeout_seconds: int
    output_limit: int
    working_directory: str = "."

    def __post_init__(self) -> None:
        if not isinstance(self.commands, tuple) or not self.commands or len(self.commands) > MAX_VERIFICATION_COMMANDS or any(
            not isinstance(c, tuple) or not c or len(c) > MAX_ARGV_MEMBERS or any(
                not isinstance(a, str) or not a.strip() or len(a) > MAX_ARG_LENGTH for a in c
            )
            for c in self.commands
        ):
            raise ValueError("verification requires immutable non-empty command argv")
        if len(set(self.commands)) != len(self.commands):
            raise ValueError("verification commands must be unique")
        if sum(len(arg.encode("utf-8")) for command in self.commands for arg in command) > MAX_ARGV_BYTES:
            raise ValueError("verification argv exceeds byte limit")
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0 or type(self.output_limit) is not int or self.output_limit <= 0:
            raise ValueError("verification limits must be positive finite integers")
        if self.working_directory != "." and normalized_repository_path(self.working_directory) is None:
            raise ValueError("verification working directory must be repository-relative")


@dataclass(frozen=True)
class TicketContract:
    ticket_id: str
    objective: str
    criterion_ids: tuple[str, ...]
    non_goals: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    verification: VerificationProfile
    patch_budget: PatchBudget
    context_budget_tokens: int
    dependencies: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.verification) is not VerificationProfile or type(self.patch_budget) is not PatchBudget:
            raise ValueError("ticket verification and patch budget must be exact immutable contract types")
        if type(self.context_budget_tokens) is not int or self.context_budget_tokens <= 0:
            raise ValueError("context budget must be a positive finite integer")
        if self.schema_version != 1 or not self.ticket_id.strip() or not self.objective.strip():
            raise ValueError("invalid ticket identity, objective, or schema version")
        if not isinstance(self.criterion_ids, tuple) or not self.criterion_ids or any(not isinstance(x, str) or not x.strip() for x in self.criterion_ids) or not _unique(self.criterion_ids):
            raise ValueError("ticket criteria must be immutable, non-empty and unique")
        if not isinstance(self.allowed_paths, tuple) or not self.allowed_paths or not _unique(self.allowed_paths) or any(normalized_repository_path(p) is None for p in self.allowed_paths):
            raise ValueError("ticket paths must be unique normalized repository files")
        if len(self.allowed_paths) > self.patch_budget.max_files:
            raise ValueError("ticket paths exceed declared file budget")
        if (not isinstance(self.non_goals, tuple) or any(not isinstance(x, str) or not x.strip() for x in self.non_goals)
                or not isinstance(self.dependencies, tuple) or len(self.dependencies) > MAX_REFERENCES_PER_TICKET
                or any(not isinstance(x, str) or not x.strip() for x in self.dependencies)
                or not _unique(self.dependencies) or self.ticket_id in self.dependencies):
            raise ValueError("ticket non-goals/dependencies must be immutable non-empty strings and dependencies unique")

    def payload(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "ticket_id": self.ticket_id, "objective": self.objective,
                "criterion_ids": list(self.criterion_ids), "non_goals": list(self.non_goals),
                "allowed_paths": list(self.allowed_paths), "dependencies": list(self.dependencies),
                "patch_budget": {"max_files": self.patch_budget.max_files, "max_changed_lines": self.patch_budget.max_changed_lines, "max_attempts": self.patch_budget.max_attempts},
                "context_budget_tokens": self.context_budget_tokens,
                "verification": {"commands": [list(c) for c in self.verification.commands], "working_directory": self.verification.working_directory,
                                 "timeout_seconds": self.verification.timeout_seconds, "output_limit": self.verification.output_limit}}

    @property
    def contract_hash(self) -> str:
        return _hash(self.payload())


@dataclass(frozen=True)
class TrancheContract:
    tranche_id: str
    ordinal: int
    tickets: tuple[TicketContract, ...]
    criterion_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tranche_id, str) or not self.tranche_id.strip() or type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("tranche requires a non-empty ID and non-negative ordinal")
        if not isinstance(self.tickets, tuple) or any(not isinstance(ticket, TicketContract) for ticket in self.tickets):
            raise ValueError("tranche tickets must be immutable contracts")
        if not isinstance(self.criterion_ids, tuple) or any(not isinstance(c, str) or not c for c in self.criterion_ids):
            raise ValueError("tranche criterion IDs must be immutable strings")


@dataclass(frozen=True)
class PlanContract:
    plan_id: str
    schema_version: int
    tranches: tuple[TrancheContract, ...]
    criterion_coverage: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        if not isinstance(self.tranches, tuple) or not isinstance(self.criterion_coverage, Mapping):
            raise ValueError("plan tranches and criterion coverage must be immutable-compatible")
        if len(self.criterion_coverage) > MAX_PLAN_REFERENCES:
            raise ValueError("plan criterion count exceeds reference limit")
        coverage = {}
        for criterion, refs in self.criterion_coverage.items():
            if not isinstance(criterion, str) or not isinstance(refs, tuple) or any(not isinstance(ref, str) for ref in refs):
                raise ValueError("criterion coverage must map IDs to ticket-ID tuples")
            if len(refs) > MAX_REFERENCES_PER_TICKET:
                raise ValueError("criterion coverage exceeds per-criterion reference limit")
            coverage[criterion] = refs
        object.__setattr__(self, "criterion_coverage", MappingProxyType(coverage))

    @property
    def contract_hash(self) -> str:
        return _hash({"plan_id": self.plan_id, "schema_version": self.schema_version,
                      "tranches": [{"tranche_id": tr.tranche_id, "ordinal": tr.ordinal,
                                    "tickets": [t.payload() for t in tr.tickets], "criterion_ids": list(tr.criterion_ids)} for tr in self.tranches],
                      "criterion_coverage": {k: list(v) for k, v in sorted(self.criterion_coverage.items())}})


def validate_plan(
    plan: PlanContract, *, expected_criteria: set[str] | frozenset[str],
    max_tranches: int, max_tickets: int,
) -> tuple[str, ...]:
    if not expected_criteria or any(not isinstance(c, str) or not c for c in expected_criteria):
        raise ValueError("trusted expected criteria must be non-empty IDs")
    if (type(max_tranches) is not int or type(max_tickets) is not int
            or not 1 <= max_tranches <= 128 or not 1 <= max_tickets <= 2048):
        raise ValueError("trusted plan limits must be positive integers within hard ceilings")
    if len(plan.tranches) > max_tranches or sum(len(tr.tickets) for tr in plan.tranches) > max_tickets:
        return ("plan_limit_exceeded",)
    dependency_refs = sum(len(ticket.dependencies) for tranche in plan.tranches for ticket in tranche.tickets)
    coverage_refs = sum(len(refs) for refs in plan.criterion_coverage.values())
    if dependency_refs > MAX_PLAN_REFERENCES or coverage_refs > MAX_PLAN_REFERENCES:
        return ("plan_limit_exceeded",)
    errors: set[str] = set()
    if plan.schema_version != 1 or not plan.plan_id.strip():
        errors.add("invalid_plan_identity")
    tranche_ids = [t.tranche_id for t in plan.tranches]
    ticket_list = [ticket for tr in plan.tranches for ticket in tr.tickets]
    ticket_ids = [t.ticket_id for t in ticket_list]
    if len(tranche_ids) != len(set(tranche_ids)):
        errors.add("duplicate_tranche_id")
    ordinals = [t.ordinal for t in plan.tranches]
    if len(ordinals) != len(set(ordinals)):
        errors.add("duplicate_tranche_ordinal")
    if len(ticket_ids) != len(set(ticket_ids)):
        errors.add("duplicate_ticket_id")
    known_criteria = {c for tr in plan.tranches for c in tr.criterion_ids}
    if not expected_criteria <= known_criteria:
        errors.add("missing_required_criterion")
    if not known_criteria <= expected_criteria:
        errors.add("unknown_criterion")
    if set(plan.criterion_coverage) != known_criteria:
        errors.add("criterion_coverage_mismatch")
    for tr in plan.tranches:
        if len(tr.criterion_ids) != len(set(tr.criterion_ids)):
            errors.add("duplicate_tranche_criterion")
    ids = set(ticket_ids)
    ordinal_by_ticket = {ticket.ticket_id: tranche.ordinal for tranche in plan.tranches for ticket in tranche.tickets}
    graph = {t.ticket_id: set(t.dependencies) for t in ticket_list}
    for t in ticket_list:
        if not set(t.dependencies) <= ids:
            errors.add("missing_dependency")
        if any(ordinal_by_ticket.get(dependency, -1) > ordinal_by_ticket[t.ticket_id] for dependency in t.dependencies):
            errors.add("dependency_future_tranche")
    visited: set[str] = set()
    for root in graph:
        if root in visited:
            continue
        active: set[str] = set()
        stack: list[tuple[str, Iterator[str]]] = [(root, iter(dep for dep in graph[root] if dep in graph))]
        active.add(root)
        while stack:
            node, children = stack[-1]
            try:
                dep = next(children)
            except StopIteration:
                stack.pop()
                active.remove(node)
                visited.add(node)
                continue
            if dep in active:
                errors.add("dependency_cycle")
                break
            if dep not in visited:
                active.add(dep)
                stack.append((dep, iter(child for child in graph[dep] if child in graph)))
        if "dependency_cycle" in errors:
            break
    for criterion, refs in plan.criterion_coverage.items():
        if not refs or len(refs) != len(set(refs)) or any(ref not in ids for ref in refs):
            errors.add("invalid_criterion_coverage")
            continue
        ref_set = set(refs)
        for ticket in ticket_list:
            if ticket.ticket_id in ref_set and criterion not in ticket.criterion_ids:
                errors.add("criterion_ticket_mismatch")
    for tranche in plan.tranches:
        for ticket in tranche.tickets:
            for criterion in ticket.criterion_ids:
                if criterion not in tranche.criterion_ids or criterion not in expected_criteria:
                    errors.add("ticket_criterion_outside_tranche")
                if ticket.ticket_id not in plan.criterion_coverage.get(criterion, ()):
                    errors.add("ticket_criterion_uncovered")
    return tuple(sorted(errors))


@dataclass(frozen=True)
class ReviewResult:
    ticket_id: str
    contract_hash: str
    candidate_sha: str
    verdict: str
    criterion_results: tuple[Mapping[str, str], ...]
    findings: tuple[Mapping[str, str], ...]
    escalation_reason: str | None = None


def normalize_review(payload: object, contract: TicketContract, expected_candidate_sha: str) -> ReviewResult:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_candidate_sha):
        raise ValueError("expected candidate must be a full lowercase Git SHA")
    if not isinstance(payload, dict) or payload.get("ticket_id") != contract.ticket_id or payload.get("contract_hash") != contract.contract_hash or payload.get("candidate_sha") != expected_candidate_sha:
        raise ValueError("review candidate identity mismatch")
    verdict = payload.get("verdict")
    if verdict not in {"pass", "repair", "escalate"}:
        raise ValueError("invalid review verdict")
    raw_results = payload.get("criterion_results", ())
    if not isinstance(raw_results, (list, tuple)):
        raise ValueError("malformed criterion results")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            raise ValueError("malformed criterion result")
        cid, status, evidence = item.get("criterion_id"), item.get("status"), item.get("evidence")
        if cid not in contract.criterion_ids or cid in seen or status not in {"pass", "fail"} or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("unknown, duplicate, or malformed review criterion")
        seen.add(cid)
        results.append({"criterion_id": cid, "status": status, "evidence": evidence.strip()})
    findings_raw = payload.get("findings", ())
    if not isinstance(findings_raw, (list, tuple)):
        raise ValueError("malformed review findings")
    findings: list[dict[str, str]] = []
    for item in findings_raw:
        if not isinstance(item, dict):
            raise ValueError("malformed review finding")
        cid = item.get("criterion_id")
        if cid not in contract.criterion_ids or item.get("file") not in contract.allowed_paths:
            raise ValueError("review finding outside contract scope")
        if any(not isinstance(item.get(k), str) or not item[k].strip() for k in ("symbol", "evidence", "minimal_repair", "verification")):
            raise ValueError("malformed blocking review finding")
        if item.get("severity") != "blocking":
            raise ValueError("repair findings must be explicitly blocking")
        if not any(r["criterion_id"] == cid and r["status"] == "fail" for r in results):
            raise ValueError("blocking finding must match failed criterion evidence")
        findings.append({k: item[k].strip() for k in ("criterion_id", "file", "symbol", "evidence", "minimal_repair", "verification")})
    if verdict == "pass" and (seen != set(contract.criterion_ids) or any(x["status"] != "pass" for x in results) or findings):
        raise ValueError("pass requires evidence for every criterion and no blockers")
    if verdict == "repair" and not findings:
        raise ValueError("repair requires a valid blocking finding")
    if verdict == "repair" and any(x["status"] == "fail" and x["criterion_id"] not in {f["criterion_id"] for f in findings} for x in results):
        raise ValueError("failed criterion is missing a valid blocker")
    escalation_reason = payload.get("reason") if verdict == "escalate" else None
    if verdict == "escalate" and (not isinstance(escalation_reason, str) or not escalation_reason.strip()):
        raise ValueError("escalation requires an actionable reason")
    return ReviewResult(contract.ticket_id, contract.contract_hash, expected_candidate_sha, verdict,
                        tuple(MappingProxyType(item) for item in results),
                        tuple(MappingProxyType(item) for item in findings),
                        escalation_reason.strip() if isinstance(escalation_reason, str) else None)
