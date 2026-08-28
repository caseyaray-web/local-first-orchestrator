from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from .ledger import Ledger
from .readiness import ReadinessError, validate_ticket
from .states import CanonicalState
from .ticket import MicroTicket, PatchBudget, VerificationProfile


class ArchitectureError(ValueError):
    pass


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArchitectureError(f"{name} must be a non-empty string")
    return value.strip()


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ArchitectureError(f"{name} must be a non-empty list")
    return value


def _ticket(raw: Mapping[str, Any]) -> MicroTicket:
    try:
        budget = raw["patch_budget"]
        commands = raw["verification_commands"]
        if not isinstance(budget, Mapping) or not isinstance(commands, list): raise ValueError
        return validate_ticket(MicroTicket(
            ticket_id=_text(raw.get("id"), "ticket.id"), objective=_text(raw.get("objective"), "ticket.objective"),
            criterion_ids=tuple(_list(raw.get("acceptance_criteria"), "ticket.acceptance_criteria")),
            primary_symbol=_text(raw.get("primary_symbol"), "ticket.primary_symbol"),
            allowed_files=tuple(_list(raw.get("allowed_files"), "ticket.allowed_files")),
            forbidden_changes=tuple(_list(raw.get("forbidden_changes"), "ticket.forbidden_changes")),
            patch_budget=PatchBudget(max_files=int(budget.get("max_files", 0)), max_changed_lines=int(budget.get("max_changed_lines", 0)), exception_reason=budget.get("exception_reason")),
            verification=VerificationProfile(commands=tuple(tuple(command) for command in commands)), risk=_text(raw.get("risk"), "ticket.risk"),
            review_required=raw.get("review_required") is True, max_attempts=int(raw.get("max_attempts", 0)), dependencies=tuple(raw.get("dependencies", [])),
        ))
    except (KeyError, TypeError, ValueError, ReadinessError) as exc:
        raise ArchitectureError(f"invalid microticket: {exc}") from exc


@dataclass(frozen=True)
class ImportedArchitecture:
    feature_id: str
    activated_ticket_ids: tuple[str, ...]

    def activate_next(self, ledger: Ledger, *, current_snapshot: str) -> tuple[str, ...]:
        packet = ledger.connection.execute("SELECT snapshot_sha, packet_json FROM architecture_packets WHERE feature_id=?", (self.feature_id,)).fetchone()
        if packet is None or packet["snapshot_sha"] != current_snapshot:
            raise ArchitectureError("current snapshot does not match packet snapshot; revalidation is required")
        with ledger._transaction() as conn:
            row = conn.execute("SELECT id FROM tickets WHERE feature_id=? AND state='draft' ORDER BY tranche_id, id LIMIT 1", (self.feature_id,)).fetchone()
            if row is None: return ()
            conn.execute("UPDATE tickets SET state='ready_local', updated_at=? WHERE id=?", (ledger._now(), row["id"]))
            ledger._append_event(conn, entity_type="ticket", entity_id=row["id"], event_type="tranche_ticket_activated", actor_id="controller", to_state="ready_local", payload={"snapshot": current_snapshot})
            return (str(row["id"]),)

    def checkpoint_packet(self, ledger: Ledger, tranche_id: str) -> dict[str, Any]:
        feature = ledger.connection.execute("SELECT objective FROM features WHERE id=?", (self.feature_id,)).fetchone()
        architecture = ledger.connection.execute("SELECT packet_json FROM architecture_packets WHERE feature_id=?", (self.feature_id,)).fetchone()
        if feature is None or architecture is None:
            raise ArchitectureError("feature architecture packet is unavailable")
        packet = json.loads(architecture["packet_json"])
        feature_contract = packet["feature"]
        criteria = ledger.connection.execute("SELECT id, statement, verification FROM acceptance_criteria WHERE feature_id=? ORDER BY id", (self.feature_id,)).fetchall()
        evidence = ledger.connection.execute("SELECT e.accepted_commit_sha, e.diff_summary, e.validation_summary FROM accepted_evidence e JOIN tickets t ON t.id=e.ticket_id WHERE t.tranche_id=? AND t.state='accepted' ORDER BY t.id", (tranche_id,)).fetchall()
        return {
            "feature_id": self.feature_id,
            "objective": feature["objective"],
            "non_goals": list(feature_contract["non_goals"]),
            "invariants": list(feature_contract["invariants"]),
            "decisions": list(packet["decisions"]),
            "risks": list(packet["risks"]),
            "criteria": [dict(row) for row in criteria],
            "accepted_commits": [dict(row) for row in evidence],
            "integration_validation": [row["validation_summary"] for row in evidence],
        }


def import_architecture_packet(ledger: Ledger, raw: Mapping[str, Any], *, max_active_tickets: int, current_snapshot: str) -> ImportedArchitecture:
    if raw.get("version") != 1 or max_active_tickets < 1 or len(current_snapshot) != 40:
        raise ArchitectureError("unsupported packet version, activation limit, or snapshot")
    feature = raw.get("feature")
    if not isinstance(feature, Mapping): raise ArchitectureError("feature must be an object")
    feature_id = _text(feature.get("id"), "feature.id")
    objective, title = _text(feature.get("objective"), "feature.objective"), _text(feature.get("title"), "feature.title")
    for field in ("non_goals", "invariants"): _list(feature.get(field), f"feature.{field}")
    criteria = _list(raw.get("acceptance_criteria"), "acceptance_criteria")
    criterion_ids = set()
    for criterion in criteria:
        if not isinstance(criterion, Mapping): raise ArchitectureError("criterion must be object")
        cid = _text(criterion.get("id"), "criterion.id")
        if cid in criterion_ids: raise ArchitectureError("duplicate criterion id")
        criterion_ids.add(cid); _text(criterion.get("statement"), "criterion.statement"); _text(criterion.get("verification"), "criterion.verification")
    decisions = _list(raw.get("decisions"), "decisions")
    for decision in decisions:
        if not isinstance(decision, Mapping): raise ArchitectureError("decision must be object")
        for field in ("id", "decision", "rationale", "alternatives_rejected"):
            value = decision.get(field)
            if field == "alternatives_rejected": _list(value, f"decision.{field}")
            else: _text(value, f"decision.{field}")
    risks = _list(raw.get("risks"), "risks")
    for risk in risks:
        if not isinstance(risk, Mapping): raise ArchitectureError("risk must be object")
        _text(risk.get("category"), "risk.category"); _text(risk.get("handling"), "risk.handling")
    tranches = _list(raw.get("tranches"), "tranches")
    parsed: list[tuple[Mapping[str, Any], list[MicroTicket]]] = []
    seen_tickets: set[str] = set()
    for tranche in tranches:
        if not isinstance(tranche, Mapping): raise ArchitectureError("tranche must be object")
        _text(tranche.get("id"), "tranche.id"); _text(tranche.get("objective"), "tranche.objective")
        if tranche.get("base_sha") != current_snapshot: raise ArchitectureError("tranche base SHA does not match explicit snapshot")
        _list(tranche.get("integration_verification"), "tranche.integration_verification")
        tickets = [_ticket(item) for item in _list(tranche.get("microtickets"), "tranche.microtickets")]
        for item in tickets:
            if item.ticket_id in seen_tickets or not set(item.criterion_ids) <= criterion_ids: raise ArchitectureError("ticket IDs and criterion references must be unique and existing")
            seen_tickets.add(item.ticket_id)
        parsed.append((tranche, tickets))
    activated: list[str] = []
    with ledger._transaction() as conn:
        now = ledger._now()
        conn.execute("INSERT INTO features(id, title, objective, status, architecture_version, integration_base_sha, created_at, updated_at) VALUES (?, ?, ?, 'draft', 1, ?, ?, ?)", (feature_id, title, objective, current_snapshot, now, now))
        conn.execute("INSERT INTO architecture_packets(feature_id, version, snapshot_sha, packet_json, created_at) VALUES (?, 1, ?, ?, ?)", (feature_id, current_snapshot, json.dumps(raw, sort_keys=True), now))
        for criterion in criteria: conn.execute("INSERT INTO acceptance_criteria(id, feature_id, statement, verification) VALUES (?, ?, ?, ?)", (criterion["id"], feature_id, criterion["statement"], criterion["verification"]))
        for ordinal, (tranche, tickets) in enumerate(parsed):
            tid = tranche["id"]; conn.execute("INSERT INTO tranches(id, feature_id, ordinal, status, base_sha, integration_commands_json) VALUES (?, ?, ?, 'draft', ?, ?)", (tid, feature_id, ordinal, tranche["base_sha"], json.dumps(tranche["integration_verification"])))
            for item in tickets:
                state = CanonicalState.READY_LOCAL.value if len(activated) < max_active_tickets else CanonicalState.DRAFT.value
                if state == CanonicalState.READY_LOCAL.value: activated.append(item.ticket_id)
                contract = item.contract()
                conn.execute("INSERT INTO tickets(id, external_id, feature_id, tranche_id, title, objective, criterion_ids_json, primary_symbol, allowed_files_json, forbidden_changes_json, patch_budget_json, verification_json, risk, review_required, max_attempts, dependencies_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (item.ticket_id, item.ticket_id, feature_id, tid, item.ticket_id, contract["objective"], json.dumps(contract["criterion_ids"]), contract["primary_symbol"], json.dumps(contract["allowed_files"]), json.dumps(contract["forbidden_changes"]), json.dumps(contract["patch_budget"]), json.dumps(contract["verification"]), contract["risk"], int(item.review_required), item.max_attempts, json.dumps(contract["dependencies"]), state, now, now))
                ledger._append_event(conn, entity_type="ticket", entity_id=item.ticket_id, event_type="architecture_ticket_imported", actor_id="controller", to_state=state, payload={"feature_id": feature_id, "tranche_id": tid})
        ledger._append_event(conn, entity_type="feature", entity_id=feature_id, event_type="architecture_imported", actor_id="controller", payload={"version": 1, "snapshot": current_snapshot})
    return ImportedArchitecture(feature_id, tuple(activated))
