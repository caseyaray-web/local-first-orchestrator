from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .comment_delivery import CommentDeliveryWorker
from .generated_projection import GeneratedProjectionWorker
from .ledger import Ledger
from .state_projection import StateProjectionWorker


@dataclass(frozen=True)
class ProcessNextResult:
    status: str
    stage: str | None = None
    ticket_id: str | None = None
    claim_id: str | None = None


class ProcessNextScheduler:
    """Run at most one Local First control stage; Hermes still schedules workers."""

    def __init__(
        self,
        ledger: Ledger,
        board: Any,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("process-next requires a worker id and positive lease")
        timeout = getattr(board, "timeout_seconds", None)
        if type(timeout) is not int or timeout < 1:
            raise ValueError("process-next board must expose a positive timeout")
        if lease_seconds < (2 * timeout) + 10:
            raise ValueError("process-next lease does not cover the bounded external effect horizon")
        self.ledger = ledger
        self.board = board
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.clock = clock or Ledger._now

    def process_next(self) -> ProcessNextResult:
        now = int(self.clock())
        lease_token = uuid.uuid4().hex
        tick = self.ledger.claim_scheduler_tick(
            self.worker_id, lease_token, lease_seconds=self.lease_seconds, now=now
        )
        if tick != "claimed":
            return ProcessNextResult(tick)
        try:
            return self._process_claimed_tick(now, f"{self.worker_id}:{lease_token}")
        finally:
            self.ledger.release_scheduler_tick(self.worker_id, lease_token)

    def _process_claimed_tick(self, now: int, execution_owner: str) -> ProcessNextResult:
        state = StateProjectionWorker(
            self.ledger, self.board, worker_id=execution_owner, lease_seconds=self.lease_seconds
        ).deliver_one(now=now)
        if state.status != "no_work":
            return ProcessNextResult(state.status, "state_projection", state.ticket_id)

        generated = GeneratedProjectionWorker(
            self.ledger, self.board, worker_id=execution_owner, clock=lambda: now
        ).deliver_one()
        if generated.status != "no_work":
            return ProcessNextResult(generated.status, "generated_projection", generated.ticket_id)

        comment = CommentDeliveryWorker(
            self.ledger, self.board, worker_id=execution_owner, clock=lambda: now
        ).deliver_one()
        if comment.status != "no_work":
            row = self.ledger.comment_outbox(str(comment.operation_id)) if comment.operation_id else None
            return ProcessNextResult(
                comment.status,
                "evidence_comment",
                str(row["ticket_id"]) if row else None,
            )

        claim = self.ledger.claim_next_scheduler_readiness(
            execution_owner, lease_seconds=self.lease_seconds, now=now
        )
        if claim is None:
            return ProcessNextResult("no_work")
        readiness = self.ledger.admit_ticket_if_ready(str(claim["ticket_id"]))
        result = {
            "status": readiness.status,
            "unresolved_dependency_ids": list(readiness.unresolved_dependency_ids),
        }
        if readiness.status != "ready":
            raise RuntimeError(f"claimed readiness stage became ineligible: {readiness.status}")
        self.ledger.complete_scheduler_claim(
            str(claim["claim_id"]), execution_owner, result, now=now
        )
        return ProcessNextResult(
            "completed",
            "dependency_readiness",
            str(claim["ticket_id"]),
            str(claim["claim_id"]),
        )
