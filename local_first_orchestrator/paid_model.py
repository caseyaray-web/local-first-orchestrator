from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from .ledger import Ledger
from .usage_governor import PaidPurpose, UsageGovernor


class PaidModelAdapter(Protocol):
    is_paid: bool
    def invoke(self, feature_id: str, purpose: PaidPurpose, request_key: str, packet: dict[str, Any]) -> dict[str, Any]: ...


class PaidInvocationError(RuntimeError):
    pass


class InjectedPaidModelAdapter:
    """Test-only adapter. The injected callable is the sole possible invocation path."""
    is_paid = True
    cost_class = "paid"
    role = "decomposition"
    provider = "injected"
    model = "injected-paid-planner"
    profile = "fixture"
    routing_source = "injected-fixture"

    def __init__(self, ledger: Ledger, governor: UsageGovernor, runner: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.ledger, self.governor, self.runner = ledger, governor, runner

    def invoke(self, feature_id: str, purpose: PaidPurpose, request_key: str, packet: dict[str, Any]) -> dict[str, Any]:
        try:
            reservation = self.governor.authorize(feature_id, purpose, request_key)
        except (PermissionError, ValueError) as exc:
            raise PaidInvocationError(str(exc)) from exc
        existing = self.ledger.connection.execute(
            "SELECT status, response_artifact_json FROM model_calls WHERE reservation_id=?",
            (reservation.reservation_id,),
        ).fetchone()
        if existing is not None:
            if existing["status"] == "completed" and existing["response_artifact_json"]:
                return json.loads(existing["response_artifact_json"])
            # A durable in-flight call proves a process may have reached the provider.
            # Fail closed rather than guessing whether it is safe to repeat it.
            if existing["status"] == "in_flight":
                self.governor.unknown(reservation, "recovered in-flight model call")
                with self.ledger._transaction() as conn:
                    conn.execute("UPDATE model_calls SET status='unknown_outcome', updated_at=? WHERE reservation_id=?", (self.ledger._now(), reservation.reservation_id))
            raise PaidInvocationError("paid invocation outcome is unknown and will not be retried")
        call_id, now = uuid.uuid4().hex, self.ledger._now()
        try:
            with self.ledger._transaction() as conn:
                conn.execute("INSERT INTO model_calls(id, feature_id, purpose, reservation_id, status, request_artifact_json, created_at, updated_at) VALUES (?, ?, ?, ?, 'in_flight', ?, ?, ?)", (call_id, feature_id, purpose.value, reservation.reservation_id, json.dumps(packet, sort_keys=True), now, now))
        except sqlite3.IntegrityError:
            # Another process won the durable invocation claim.  This caller
            # must not speculate about whether that process reached the model.
            self.governor.unknown(reservation, "concurrent model-call claim")
            with self.ledger._transaction() as conn:
                conn.execute("UPDATE model_calls SET status='unknown_outcome', updated_at=? WHERE reservation_id=?", (self.ledger._now(), reservation.reservation_id))
            raise PaidInvocationError("paid invocation outcome is unknown and will not be retried")
        try:
            proposal = self.runner(packet)
            if not isinstance(proposal, dict):
                raise TypeError("paid runner must return a structured proposal object")
        except Exception as exc:
            self.governor.unknown(reservation, f"runner outcome ambiguous: {type(exc).__name__}")
            with self.ledger._transaction() as conn:
                conn.execute("UPDATE model_calls SET status='unknown_outcome', updated_at=? WHERE id=?", (self.ledger._now(), call_id))
            raise PaidInvocationError("paid invocation outcome is unknown and will not be retried") from exc
        self.governor.complete(reservation, input_tokens=0, output_tokens=0)
        with self.ledger._transaction() as conn:
            conn.execute("UPDATE model_calls SET status='completed', response_artifact_json=?, input_tokens=0, output_tokens=0, updated_at=? WHERE id=?", (json.dumps(proposal, sort_keys=True), self.ledger._now(), call_id))
        return proposal
