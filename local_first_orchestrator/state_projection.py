from __future__ import annotations

from dataclasses import dataclass
import json
from collections.abc import Callable
from typing import Literal

from .adapters import BoardAdapter
from .execution_handoff import HANDOFF_MARKER
from .ledger import Ledger
from .states import CanonicalState


@dataclass(frozen=True)
class StateProjectionDeliveryResult:
    status: Literal["no_work", "delivered", "superseded", "retry_scheduled"]
    ticket_id: str | None = None
    event_id: int | None = None


class StateProjectionWorker:
    """Lease and deliver only causally current state intents.

    The final ledger freshness gate runs immediately before ``set_state``.  A
    transition observed before that call makes an old row historical only.
    """

    def __init__(self, ledger: Ledger, board: BoardAdapter, *, worker_id: str, lease_seconds: int = 60, fault_injector: Callable[[str], None] | None = None) -> None:
        self.ledger = ledger
        self.board = board
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.fault_injector = fault_injector

    def _fault(self, stage: str) -> None:
        if self.fault_injector:
            self.fault_injector(stage)

    def _reconciled_generated_done_noop(self, ticket_id: str, external_task_id: str, state: CanonicalState) -> bool:
        if state == CanonicalState.DONE:
            return False
        ticket = self.ledger.get_ticket(ticket_id)
        if ticket is None or ticket["external_id"] is not None:
            return False
        generated = self.ledger.connection.execute(
            """SELECT b.event_id FROM board_projection_outbox b JOIN events e ON e.id=b.event_id
               WHERE b.ticket_id=? AND b.operation='create_microticket' AND b.acknowledged_at IS NOT NULL
                 AND b.superseded_at IS NULL AND b.external_task_id=? AND e.entity_type='ticket' AND e.entity_id=?
                 AND e.event_type IN ('generated_microticket_created','generated_microticket_projection_recovered')""",
            (ticket_id, external_task_id, ticket_id),
        ).fetchall()
        if len(generated) != 1:
            return False
        reconciled = self.ledger.connection.execute(
            """SELECT 1 FROM hermes_execution_reconciliations
               WHERE ticket_id=? AND external_task_id=? AND run_status IN ('done','completed')
                 AND run_outcome IN ('completed','success','succeeded')
               ORDER BY created_at DESC LIMIT 1""",
            (ticket_id, external_task_id),
        ).fetchone()
        if reconciled is None:
            return False
        get_task = getattr(self.board, "get_task", None)
        if not callable(get_task):
            return False
        task = get_task(external_task_id)
        return str(task.status) == "done" and HANDOFF_MARKER in str(task.body or "")

    def deliver_one(self, *, now: int | None = None, ticket_id: str | None = None) -> StateProjectionDeliveryResult:
        row = self.ledger.claim_next_state_projection(self.worker_id, lease_seconds=self.lease_seconds, now=now, ticket_id=ticket_id)
        if row is None:
            return StateProjectionDeliveryResult("no_work")
        return self.deliver_claimed(str(row["ticket_id"]), int(row["event_id"]), now=now)

    def deliver_ticket_event(self, ticket_id: str, event_id: int, *, now: int | None = None) -> StateProjectionDeliveryResult:
        if not self.ledger.claim_state_projection(ticket_id, event_id, self.worker_id, lease_seconds=self.lease_seconds, now=now):
            return StateProjectionDeliveryResult("no_work", ticket_id, event_id)
        return self.deliver_claimed(ticket_id, event_id, now=now)

    def deliver_claimed(self, ticket_id: str, event_id: int, *, now: int | None = None) -> StateProjectionDeliveryResult:
        row = self.ledger.prepare_claimed_state_projection(ticket_id, event_id, self.worker_id, now=now)
        if row is None:
            return StateProjectionDeliveryResult("superseded", ticket_id, event_id)
        try:
            target_state = CanonicalState(str(row["state"]))
            external_task_id = str(row["external_task_id"])
            generated_done_noop = self._reconciled_generated_done_noop(ticket_id, external_task_id, target_state)
            if generated_done_noop:
                self.ledger.record_runtime_stage(
                    ticket_id,
                    f"state_projection_generated_done_noop-{event_id}",
                    json.dumps({"event_id": event_id, "external_task_id": external_task_id, "state": target_state.value}, sort_keys=True),
                )
            else:
                self.board.set_state(external_task_id, target_state, idempotency_key=str(row["idempotency_key"]))
        except Exception as exc:
            self.ledger.release_state_projection(ticket_id, event_id, self.worker_id, str(exc), now=now)
            raise
        self._fault("after_adapter_success")
        if not self.ledger.acknowledge_state_projection(ticket_id, event_id, self.worker_id, now=now):
            raise RuntimeError("state projection acknowledgement lost")
        self._fault("after_local_ack")
        return StateProjectionDeliveryResult("delivered", ticket_id, event_id)
