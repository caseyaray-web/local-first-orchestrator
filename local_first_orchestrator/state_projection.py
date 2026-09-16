from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Literal

from .adapters import BoardAdapter
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
            self.board.set_state(str(row["external_task_id"]), CanonicalState(str(row["state"])), idempotency_key=str(row["idempotency_key"]))
        except Exception as exc:
            self.ledger.release_state_projection(ticket_id, event_id, self.worker_id, str(exc), now=now)
            raise
        self._fault("after_adapter_success")
        if not self.ledger.acknowledge_state_projection(ticket_id, event_id, self.worker_id, now=now):
            raise RuntimeError("state projection acknowledgement lost")
        self._fault("after_local_ack")
        return StateProjectionDeliveryResult("delivered", ticket_id, event_id)
