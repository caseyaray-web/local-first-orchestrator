from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from .ledger import Ledger


class CommentAdapter(Protocol):
    """Injected external boundary for one persisted evidence comment."""

    def find_comment_marker(self, external_task_id: str, marker: str) -> str: ...

    def deliver_comment(self, external_task_id: str, comment: str, *, idempotency_key: str) -> None: ...


@dataclass(frozen=True)
class CommentDeliveryPolicy:
    lease_seconds: int = 60
    max_attempts: int = 3
    base_retry_delay: int = 10
    max_retry_delay: int = 300
    error_length_limit: int = 500
    writes_enabled: bool = True

    def __post_init__(self) -> None:
        if self.lease_seconds < 1 or self.max_attempts < 1:
            raise ValueError("lease_seconds and max_attempts must be positive")
        if self.base_retry_delay < 0 or self.max_retry_delay < 0:
            raise ValueError("retry delays cannot be negative")
        if self.max_retry_delay < self.base_retry_delay:
            raise ValueError("max_retry_delay must be at least base_retry_delay")
        if self.error_length_limit < 1:
            raise ValueError("error_length_limit must be positive")


DeliveryStatus = Literal["no_work", "delivered", "reconciled_delivered", "reconciliation_deferred", "retry_scheduled", "permanently_failed", "writes_disabled"]


@dataclass(frozen=True)
class CommentDeliveryResult:
    status: DeliveryStatus
    operation_id: str | None = None
    attempt_count: int | None = None
    next_attempt_at: int | None = None
    error: str | None = None


class CommentDeliveryWorker:
    """Deliver at most one comment; it owns no model or controller stages."""

    def __init__(self, ledger: Ledger, adapter: CommentAdapter, policy: CommentDeliveryPolicy | None = None, *, worker_id: str, clock: Callable[[], int] | None = None) -> None:
        self.ledger = ledger
        self.adapter = adapter
        self.policy = policy or CommentDeliveryPolicy()
        self.worker_id = worker_id
        self.clock = clock or Ledger._now

    def deliver_one(self) -> CommentDeliveryResult:
        if not self.policy.writes_enabled or not bool(getattr(self.adapter, "writes_enabled", True)):
            return CommentDeliveryResult("writes_disabled")

        now = int(self.clock())
        self.ledger.recover_expired_comment_leases(now=now)
        row = self.ledger.claim_next_comment(self.worker_id, lease_seconds=self.policy.lease_seconds, now=now)
        if row is None:
            return CommentDeliveryResult("no_work")

        operation_id = str(row["operation_id"])
        attempt_count = int(row["attempt_count"])
        marker = f"<!-- local-first-comment:{operation_id} -->"
        try:
            lookup = self.adapter.find_comment_marker(str(row["external_task_id"]), marker)
        except Exception as exc:
            self.ledger.mark_comment_retryable(operation_id, self.worker_id, f"reconciliation lookup failed: {exc}", next_attempt_at=now + self.policy.base_retry_delay, now=now, error_limit=self.policy.error_length_limit)
            return CommentDeliveryResult("reconciliation_deferred", operation_id, attempt_count, now + self.policy.base_retry_delay)
        if lookup == "found":
            self.ledger.mark_comment_delivered(operation_id, self.worker_id, now=now)
            return CommentDeliveryResult("reconciled_delivered", operation_id, attempt_count)
        if lookup != "not_found":
            self.ledger.mark_comment_retryable(operation_id, self.worker_id, f"reconciliation unavailable: {lookup}", next_attempt_at=now + self.policy.base_retry_delay, now=now, error_limit=self.policy.error_length_limit)
            return CommentDeliveryResult("reconciliation_deferred", operation_id, attempt_count, now + self.policy.base_retry_delay)
        try:
            self.adapter.deliver_comment(
                str(row["external_task_id"]),
                str(row["payload"]),
                idempotency_key=str(row["idempotency_key"]),
            )
        except Exception as exc:
            error = str(exc)
            ambiguous = "ambiguous" in error.lower()
            bounded_error = Ledger._safe_comment_error(error, limit=self.policy.error_length_limit)
            if attempt_count < self.policy.max_attempts:
                delay = min(self.policy.max_retry_delay, self.policy.base_retry_delay * (2 ** (attempt_count - 1)))
                next_attempt_at = now + delay
                self.ledger.mark_comment_retryable(operation_id, self.worker_id, error, next_attempt_at=next_attempt_at, now=now, error_limit=self.policy.error_length_limit)
                return CommentDeliveryResult("reconciliation_deferred" if ambiguous else "retry_scheduled", operation_id, attempt_count, next_attempt_at, bounded_error)
            self.ledger.mark_comment_permanently_failed(operation_id, self.worker_id, error, now=now, error_limit=self.policy.error_length_limit)
            return CommentDeliveryResult("permanently_failed", operation_id, attempt_count, error=bounded_error)

        self.ledger.mark_comment_delivered(operation_id, self.worker_id, now=now)
        return CommentDeliveryResult("delivered", operation_id, attempt_count)
