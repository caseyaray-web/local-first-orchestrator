from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from .ledger import Ledger


class PaidPurpose(StrEnum):
    ARCHITECTURE = "architecture"
    INTEGRATION_CHECKPOINT = "integration_checkpoint"
    ESCALATION = "escalation"


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    feature_id: str
    purpose: PaidPurpose
    request_key: str


@dataclass(frozen=True)
class Approval:
    approval_id: str
    feature_id: str
    purpose: PaidPurpose
    calls: int


class UsageGovernor:
    """SQLite-backed fail-closed paid-call authorization; it never invokes providers."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def configure(self, feature_id: str, *, architecture: int, checkpoint: int, escalation: int) -> None:
        if min(architecture, checkpoint, escalation) < 0:
            raise ValueError("budget limits must be non-negative")
        now = self.ledger._now()
        with self.ledger._transaction() as conn:
            conn.execute("INSERT INTO paid_budgets(feature_id, architecture_limit, checkpoint_limit, escalation_limit, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(feature_id) DO UPDATE SET architecture_limit=excluded.architecture_limit, checkpoint_limit=excluded.checkpoint_limit, escalation_limit=excluded.escalation_limit, updated_at=excluded.updated_at", (feature_id, architecture, checkpoint, escalation, now, now))
            self.ledger._append_event(conn, entity_type="feature", entity_id=feature_id, event_type="paid_budget_configured", actor_id="controller", payload={"architecture": architecture, "checkpoint": checkpoint, "escalation": escalation})

    @staticmethod
    def _column(purpose: PaidPurpose) -> str:
        return {PaidPurpose.ARCHITECTURE: "architecture_limit", PaidPurpose.INTEGRATION_CHECKPOINT: "checkpoint_limit", PaidPurpose.ESCALATION: "escalation_limit"}[purpose]

    def authorize(self, feature_id: str, purpose: PaidPurpose, request_key: str) -> Reservation:
        if not request_key.strip():
            raise ValueError("request key is required")
        now = self.ledger._now()
        with self.ledger._transaction() as conn:
            existing = conn.execute("SELECT * FROM paid_reservations WHERE feature_id=? AND purpose=? AND request_key=?", (feature_id, purpose.value, request_key)).fetchone()
            if existing:
                if existing["status"] == "unknown_outcome":
                    raise PermissionError("paid invocation has unknown outcome and must not be repeated")
                if existing["status"] in {"in_flight", "completed"}:
                    return Reservation(existing["id"], feature_id, purpose, request_key)
                raise PermissionError("reservation was released; use an explicit new request key")
            budget = conn.execute("SELECT * FROM paid_budgets WHERE feature_id=?", (feature_id,)).fetchone()
            if budget is None:
                raise PermissionError("no paid budget configured")
            used = int(conn.execute("SELECT COUNT(*) FROM paid_reservations WHERE feature_id=? AND purpose=? AND status IN ('in_flight', 'completed', 'unknown_outcome')", (feature_id, purpose.value)).fetchone()[0])
            approvals = int(conn.execute("SELECT COALESCE(SUM(calls), 0) FROM paid_approvals WHERE feature_id=? AND purpose=?", (feature_id, purpose.value)).fetchone()[0])
            if used >= int(budget[self._column(purpose)]) + approvals:
                self.ledger._append_event(conn, entity_type="feature", entity_id=feature_id, event_type="paid_budget_exhausted", actor_id="governor", payload={"purpose": purpose.value, "request_key": request_key})
                raise PermissionError("paid budget exhausted; needs_checkpoint")
            reservation_id = uuid.uuid4().hex
            conn.execute("INSERT INTO paid_reservations(id, feature_id, purpose, request_key, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'in_flight', ?, ?)", (reservation_id, feature_id, purpose.value, request_key, now, now))
            self.ledger._append_event(conn, entity_type="feature", entity_id=feature_id, event_type="paid_reserved", actor_id="governor", payload={"reservation_id": reservation_id, "purpose": purpose.value, "request_key": request_key})
            return Reservation(reservation_id, feature_id, purpose, request_key)

    def _finish(self, reservation: Reservation, status: str, reason: str | None = None) -> None:
        with self.ledger._transaction() as conn:
            row = conn.execute("SELECT status FROM paid_reservations WHERE id=?", (reservation.reservation_id,)).fetchone()
            if row is None or row["status"] != "in_flight":
                raise ValueError("reservation is not in flight")
            conn.execute("UPDATE paid_reservations SET status=?, reason=?, updated_at=? WHERE id=?", (status, reason, self.ledger._now(), reservation.reservation_id))
            self.ledger._append_event(conn, entity_type="feature", entity_id=reservation.feature_id, event_type=f"paid_{status}", actor_id="governor", payload={"reservation_id": reservation.reservation_id, "purpose": reservation.purpose.value, "reason": reason})

    def complete(self, reservation: Reservation, *, input_tokens: int, output_tokens: int) -> None:
        self._finish(reservation, "completed")

    def release(self, reservation: Reservation, reason: str) -> None:
        self._finish(reservation, "released", reason)

    def unknown(self, reservation: Reservation, reason: str) -> None:
        self._finish(reservation, "unknown_outcome", reason)

    def approve(self, feature_id: str, purpose: PaidPurpose, actor_id: str, reason: str, idempotency_key: str, *, calls: int = 1) -> Approval:
        if calls != 1 or not reason.strip() or not idempotency_key.strip():
            raise ValueError("approval must grant exactly one purpose-specific call with reason and idempotency key")
        with self.ledger._transaction() as conn:
            existing = conn.execute("SELECT * FROM paid_approvals WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                if existing["feature_id"] != feature_id or existing["purpose"] != purpose.value:
                    raise ValueError("approval idempotency key conflicts")
                return Approval(existing["id"], feature_id, purpose, int(existing["calls"]))
            approval_id = uuid.uuid4().hex
            conn.execute("INSERT INTO paid_approvals(id, feature_id, purpose, calls, actor_id, reason, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (approval_id, feature_id, purpose.value, calls, actor_id, reason, idempotency_key, self.ledger._now()))
            self.ledger._append_event(conn, entity_type="feature", entity_id=feature_id, event_type="paid_approved", actor_id=actor_id, payload={"approval_id": approval_id, "purpose": purpose.value, "calls": calls, "reason": reason})
            return Approval(approval_id, feature_id, purpose, calls)

    def usage(self, feature_id: str, purpose: PaidPurpose) -> dict[str, int]:
        rows = self.ledger.connection.execute("SELECT status, COUNT(*) count FROM paid_reservations WHERE feature_id=? AND purpose=? GROUP BY status", (feature_id, purpose.value)).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        return {"completed": counts.get("completed", 0), "released": counts.get("released", 0), "in_flight": counts.get("in_flight", 0), "unknown_outcome": counts.get("unknown_outcome", 0)}
