from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .ledger import Ledger
from .readiness import ReadinessError, validate_ticket
from .states import CanonicalState
from .ticket import MicroTicket, PatchBudget, VerificationProfile


class TriageError(ValueError):
    pass


_CLASSIFICATIONS_WITH_CHILDREN = {"oversized_ticket", "architecture_gap", "implementation_defect"}
_CLASSIFICATIONS = _CLASSIFICATIONS_WITH_CHILDREN | {
    "missing_context", "ambiguous_requirement", "environment_failure",
    "credential_or_service_failure", "flaky_test", "merge_conflict", "model_failure",
}


@dataclass(frozen=True)
class TriagedChild:
    title: str
    resolves_criteria: tuple[str, ...]
    ticket: MicroTicket
    fingerprint: str


@dataclass(frozen=True)
class TriageResult:
    classification: str
    root_cause_evidence: str
    recommended_action: str
    children: tuple[TriagedChild, ...]


def _ticket_from_contract(raw: object) -> MicroTicket:
    if not isinstance(raw, dict):
        raise TriageError("child ticket must be an object")
    try:
        budget = raw["patch_budget"]
        verification = raw["verification"]
        if not isinstance(budget, dict) or not isinstance(verification, dict):
            raise TypeError
        return MicroTicket(
            ticket_id=str(raw["ticket_id"]), objective=str(raw["objective"]),
            criterion_ids=tuple(raw["criterion_ids"]), primary_symbol=str(raw["primary_symbol"]),
            allowed_files=tuple(raw["allowed_files"]), forbidden_changes=tuple(raw["forbidden_changes"]),
            patch_budget=PatchBudget(**budget),
            verification=VerificationProfile(
                commands=tuple(tuple(command) for command in verification["commands"]),
                working_directory=str(verification.get("working_directory", ".")),
            ),
            risk=str(raw["risk"]), review_required=bool(raw["review_required"]),
            max_attempts=int(raw["max_attempts"]), dependencies=tuple(raw["dependencies"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TriageError("child ticket contract is incomplete") from exc


def _fingerprint(ticket: MicroTicket, resolves: tuple[str, ...]) -> str:
    source = json.dumps({"contract": ticket.contract(), "resolves_criteria": resolves}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def normalize_triage(payload: object, parent: MicroTicket, *, parent_depth: int, max_children: int = 3, max_depth: int = 2, unresolved_criteria: set[str] | None = None) -> TriageResult:
    """Validate a classification-first triage proposal before any ledger mutation."""
    if not isinstance(payload, dict):
        raise TriageError("triage payload must be an object")
    classification = str(payload.get("classification", "")).strip()
    evidence = str(payload.get("root_cause_evidence", "")).strip()
    action = str(payload.get("recommended_action", "")).strip()
    children_raw = payload.get("children", [])
    if classification not in _CLASSIFICATIONS or not evidence:
        raise TriageError("classification and root cause evidence are required")
    if action not in {"decompose", "block", "checkpoint"}:
        raise TriageError("recommended action is invalid")
    if not isinstance(children_raw, list) or len(children_raw) > max_children:
        raise TriageError("children exceed maximum")
    if children_raw and (classification not in _CLASSIFICATIONS_WITH_CHILDREN or action != "decompose"):
        raise TriageError("classification must not create children")
    if action == "decompose" and not children_raw:
        raise TriageError("decomposition requires children")
    if children_raw and parent_depth >= max_depth:
        raise TriageError("maximum decomposition depth reached")
    unresolved = unresolved_criteria if unresolved_criteria is not None else set(parent.criterion_ids)
    children: list[TriagedChild] = []
    fingerprints: set[str] = set()
    for raw in children_raw:
        if not isinstance(raw, dict):
            raise TriageError("child must be an object")
        resolves = tuple(str(item) for item in raw.get("resolves_criteria", []))
        if not resolves or not set(resolves) <= unresolved:
            raise TriageError("child must map only unresolved parent criteria")
        ticket = _ticket_from_contract(raw.get("ticket"))
        if set(ticket.criterion_ids) - set(resolves):
            raise TriageError("child contract expands parent criterion scope")
        try:
            validate_ticket(ticket)
        except ReadinessError as exc:
            raise TriageError(f"child is not ready: {exc}") from exc
        fingerprint = _fingerprint(ticket, resolves)
        if fingerprint in fingerprints:
            raise TriageError("duplicate child fingerprint")
        fingerprints.add(fingerprint)
        children.append(TriagedChild(str(raw.get("id") or ticket.ticket_id), resolves, ticket, fingerprint))
    return TriageResult(classification, evidence, action, tuple(children))


class TriageCoordinator:
    """Bounded, ledger-only triage. Models propose; this controller mutates."""
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def apply(self, parent_ticket_id: str, result: TriageResult) -> list[str]:
        if result.recommended_action != "decompose" or result.classification not in _CLASSIFICATIONS_WITH_CHILDREN:
            raise TriageError("only validated decompositions can create children")
        parent = self.ledger.get_ticket(parent_ticket_id)
        if int(parent["depth"]) >= 2:
            raise TriageError("maximum decomposition depth reached")
        if not 1 <= len(result.children) <= 3:
            raise TriageError("children exceed maximum")
        unresolved = self.ledger.unresolved_criteria(parent_ticket_id)
        fingerprints: set[str] = set()
        for child in result.children:
            if not set(child.resolves_criteria) <= unresolved:
                raise TriageError("child must map only unresolved parent criteria")
            if set(child.ticket.criterion_ids) - set(child.resolves_criteria):
                raise TriageError("child contract expands parent criterion scope")
            try:
                validate_ticket(child.ticket)
            except ReadinessError as exc:
                raise TriageError(f"child is not ready: {exc}") from exc
            if child.fingerprint in fingerprints:
                raise TriageError("duplicate child fingerprint")
            fingerprints.add(child.fingerprint)
        return self.ledger.create_triaged_children(
            parent_ticket_id,
            [(child.title, child.ticket, child.fingerprint) for child in result.children],
            classification=result.classification,
            root_cause_evidence=result.root_cause_evidence,
        )

    def resolve_parent(self, parent_ticket_id: str) -> bool:
        """Resume only after all children accept; terminal child rejection blocks parent."""
        parent = self.ledger.get_ticket(parent_ticket_id)
        if parent["state"] != CanonicalState.NEEDS_TRIAGE.value or self.ledger.parent_is_paused(parent_ticket_id):
            return False
        child_states = [row["state"] for row in self.ledger.connection.execute(
            "SELECT state FROM tickets WHERE parent_ticket_id = ?", (parent_ticket_id,)
        ).fetchall()]
        if not child_states:
            return False
        if any(state == CanonicalState.REJECTED.value for state in child_states):
            self.ledger.transition(parent_ticket_id, CanonicalState.BLOCKED, payload={"reason": "triage_child_rejected"})
        elif all(state in {CanonicalState.ACCEPTED.value, CanonicalState.DONE.value} for state in child_states):
            self.ledger.transition(parent_ticket_id, CanonicalState.READY_LOCAL, payload={"reason": "triage_children_accepted", "requires_integration_revalidation": True})
        else:
            return False
        return True
