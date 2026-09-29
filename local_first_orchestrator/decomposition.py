from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator, Mapping

from .ticket import TicketContract, contract_payload

MAX_PLAN_REFERENCES = 16384
MAX_TICKETS = 2048
MAX_TRANCHES = 128


@dataclass(frozen=True)
class Criterion:
    criterion_id: str
    statement: str

    def __post_init__(self) -> None:
        if not isinstance(self.criterion_id, str) or not self.criterion_id.strip() or not isinstance(self.statement, str) or not self.statement.strip():
            raise ValueError("criterion requires non-empty ID and statement")


@dataclass(frozen=True)
class TranchePlan:
    tranche_id: str
    ordinal: int
    tickets: tuple[TicketContract, ...]
    criterion_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tranche_id, str) or not self.tranche_id.strip() or type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("tranche requires a non-empty ID and non-negative ordinal")
        if not isinstance(self.tickets, tuple) or any(type(ticket) is not TicketContract for ticket in self.tickets):
            raise ValueError("tranche tickets must be immutable contracts")
        if not isinstance(self.criterion_ids, tuple) or any(not isinstance(criterion, str) or not criterion.strip() for criterion in self.criterion_ids):
            raise ValueError("tranche criterion IDs must be immutable strings")


@dataclass(frozen=True)
class DecompositionPlan:
    plan_id: str
    schema_version: int
    tranches: tuple[TranchePlan, ...]
    criterion_coverage: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        if not isinstance(self.tranches, tuple) or any(type(tranche) is not TranchePlan for tranche in self.tranches) or not isinstance(self.criterion_coverage, Mapping):
            raise ValueError("plan tranches and criterion coverage must be immutable-compatible")
        if len(self.criterion_coverage) > MAX_PLAN_REFERENCES:
            raise ValueError("plan criterion count exceeds reference limit")
        coverage: dict[str, tuple[str, ...]] = {}
        for criterion, references in self.criterion_coverage.items():
            if not isinstance(criterion, str) or not criterion.strip() or not isinstance(references, tuple) or any(not isinstance(reference, str) or not reference.strip() for reference in references):
                raise ValueError("criterion coverage must map IDs to ticket-ID tuples")
            if len(references) > MAX_TICKETS:
                raise ValueError("criterion coverage exceeds per-criterion reference limit")
            coverage[criterion] = references
        object.__setattr__(self, "criterion_coverage", MappingProxyType(coverage))

    @property
    def contract_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "tranches": [
                {"tranche_id": tranche.tranche_id, "ordinal": tranche.ordinal,
                 "criterion_ids": list(tranche.criterion_ids),
                 "tickets": [contract_payload(ticket) for ticket in tranche.tickets]}
                for tranche in self.tranches
            ],
            "criterion_coverage": {key: list(value) for key, value in sorted(self.criterion_coverage.items())},
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class PlanValidator:
    def __init__(self, *, expected_criteria: frozenset[str], max_tranches: int, max_tickets: int) -> None:
        if not isinstance(expected_criteria, frozenset) or not expected_criteria or any(not isinstance(criterion, str) or not criterion.strip() for criterion in expected_criteria):
            raise ValueError("trusted expected criteria must be non-empty IDs")
        if type(max_tranches) is not int or type(max_tickets) is not int or not 1 <= max_tranches <= MAX_TRANCHES or not 1 <= max_tickets <= MAX_TICKETS:
            raise ValueError("trusted plan limits must be positive integers within hard ceilings")
        self.expected_criteria = expected_criteria
        self.max_tranches = max_tranches
        self.max_tickets = max_tickets

    def validate_tranche(self, tranche: TranchePlan, declared_scope: tuple[Criterion, ...]) -> tuple[str, ...]:
        if type(tranche) is not TranchePlan or not isinstance(declared_scope, tuple) or any(type(criterion) is not Criterion for criterion in declared_scope):
            raise ValueError("tranche and declared criterion scope are required")
        declared_ids = {criterion.criterion_id for criterion in declared_scope}
        errors: set[str] = set()
        if len(tranche.criterion_ids) != len(set(tranche.criterion_ids)):
            errors.add("duplicate_tranche_criterion")
        if not set(tranche.criterion_ids) <= declared_ids:
            errors.add("unknown_criterion")
        ticket_ids = [ticket.ticket_id for ticket in tranche.tickets]
        if len(ticket_ids) != len(set(ticket_ids)):
            errors.add("duplicate_ticket_id")
        for ticket in tranche.tickets:
            if not set(ticket.criterion_ids) <= set(tranche.criterion_ids):
                errors.add("ticket_criterion_outside_tranche")
            if any(dependency not in set(ticket_ids) for dependency in ticket.dependencies):
                errors.add("missing_dependency")
        return tuple(sorted(errors))

    def validate(self, plan: DecompositionPlan, *, known_plan_hashes: Mapping[str, str] | None = None) -> tuple[str, ...]:
        if type(plan) is not DecompositionPlan:
            raise ValueError("decomposition plan is required")
        if known_plan_hashes is not None and not isinstance(known_plan_hashes, Mapping):
            raise ValueError("known plan identities must be externally supplied mappings")
        if len(plan.tranches) > self.max_tranches or sum(len(tranche.tickets) for tranche in plan.tranches) > self.max_tickets:
            return ("plan_limit_exceeded",)
        dependency_refs = sum(len(ticket.dependencies) for tranche in plan.tranches for ticket in tranche.tickets)
        coverage_refs = sum(len(references) for references in plan.criterion_coverage.values())
        if dependency_refs > MAX_PLAN_REFERENCES or coverage_refs > MAX_PLAN_REFERENCES:
            return ("plan_limit_exceeded",)
        errors: set[str] = set()
        if plan.schema_version != 1 or not isinstance(plan.plan_id, str) or not plan.plan_id.strip():
            errors.add("invalid_plan_identity")
        if (known_plan_hashes is not None and isinstance(plan.plan_id, str)
                and plan.plan_id in known_plan_hashes and known_plan_hashes[plan.plan_id] != plan.contract_hash):
            errors.add("duplicate_plan_id")
        tranche_ids = [tranche.tranche_id for tranche in plan.tranches]
        ordinals = [tranche.ordinal for tranche in plan.tranches]
        tickets = [ticket for tranche in plan.tranches for ticket in tranche.tickets]
        ticket_ids = [ticket.ticket_id for ticket in tickets]
        if len(tranche_ids) != len(set(tranche_ids)):
            errors.add("duplicate_tranche_id")
        if len(ordinals) != len(set(ordinals)):
            errors.add("duplicate_tranche_ordinal")
        if len(ticket_ids) != len(set(ticket_ids)):
            errors.add("duplicate_ticket_id")
        known_criteria = {criterion for tranche in plan.tranches for criterion in tranche.criterion_ids}
        if not self.expected_criteria <= known_criteria:
            errors.add("missing_required_criterion")
        if not known_criteria <= self.expected_criteria:
            errors.add("unknown_criterion")
        if set(plan.criterion_coverage) != known_criteria:
            errors.add("criterion_coverage_mismatch")
        ids = set(ticket_ids)
        ordinal_by_ticket = {ticket.ticket_id: tranche.ordinal for tranche in plan.tranches for ticket in tranche.tickets}
        graph = {ticket.ticket_id: set(ticket.dependencies) for ticket in tickets}
        for ticket in tickets:
            if not set(ticket.dependencies) <= ids:
                errors.add("missing_dependency")
            if any(ordinal_by_ticket.get(dependency, -1) > ordinal_by_ticket[ticket.ticket_id] for dependency in ticket.dependencies):
                errors.add("dependency_future_tranche")
        if _has_cycle(graph):
            errors.add("dependency_cycle")
        for criterion, references in plan.criterion_coverage.items():
            if not references or len(references) != len(set(references)) or any(reference not in ids for reference in references):
                errors.add("invalid_criterion_coverage")
                continue
            for ticket in tickets:
                if ticket.ticket_id in references and criterion not in ticket.criterion_ids:
                    errors.add("criterion_ticket_mismatch")
        for tranche in plan.tranches:
            for ticket in tranche.tickets:
                if not set(ticket.criterion_ids) <= set(tranche.criterion_ids):
                    errors.add("ticket_criterion_outside_tranche")
                for criterion in ticket.criterion_ids:
                    if ticket.ticket_id not in plan.criterion_coverage.get(criterion, ()):
                        errors.add("ticket_criterion_uncovered")
        return tuple(sorted(errors))


def _has_cycle(graph: Mapping[str, set[str]]) -> bool:
    visited: set[str] = set()
    for root in graph:
        if root in visited:
            continue
        active = {root}
        stack: list[tuple[str, Iterator[str]]] = [(root, iter(dependency for dependency in graph[root] if dependency in graph))]
        while stack:
            node, children = stack[-1]
            try:
                dependency = next(children)
            except StopIteration:
                stack.pop()
                active.remove(node)
                visited.add(node)
                continue
            if dependency in active:
                return True
            if dependency not in visited:
                active.add(dependency)
                stack.append((dependency, iter(child for child in graph[dependency] if child in graph)))
    return False
