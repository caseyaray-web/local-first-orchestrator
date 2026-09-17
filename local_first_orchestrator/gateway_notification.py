from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable

from .ledger import Ledger


@dataclass(frozen=True)
class GatewayNotificationResult:
    status: str
    ticket_id: str | None = None
    operation_id: str | None = None


class HermesGatewayNotificationWorker:
    """Durable delivery of terminal Local First notifications through `hermes send`."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        executable: str,
        worker_id: str,
        lease_seconds: int = 60,
        timeout_seconds: int = 60,
        retry_delay_seconds: int = 30,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], int] = Ledger._now,
    ) -> None:
        if not executable or not worker_id or lease_seconds < 1 or timeout_seconds < 1 or retry_delay_seconds < 0:
            raise ValueError("gateway notification worker requires executable, worker id, and positive bounds")
        self.ledger = ledger
        self.executable = executable
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.timeout_seconds = timeout_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.runner = runner
        self.clock = clock

    def deliver_one(self) -> GatewayNotificationResult:
        now = int(self.clock())
        self.ledger.recover_expired_gateway_notification_leases(now=now)
        row = self.ledger.claim_next_gateway_notification(self.worker_id, lease_seconds=self.lease_seconds, now=now)
        if row is None:
            return GatewayNotificationResult("no_work")
        operation_id = str(row["operation_id"])
        ticket_id = str(row["ticket_id"])
        argv = (
            self.executable,
            "send",
            "--to",
            str(row["target"]),
            "--json",
            str(row["payload"]),
        )
        try:
            result = self.runner(argv, text=True, capture_output=True, timeout=self.timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            if not self.ledger.mark_gateway_notification_delivery_unknown(
                operation_id,
                self.worker_id,
                "hermes send timed out after delivery may have started",
                now=now,
            ):
                raise RuntimeError("gateway notification timeout could not be durably marked ambiguous")
            return GatewayNotificationResult("delivery_unknown", ticket_id, operation_id)
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and payload.get("ok") is False:
                self.ledger.mark_gateway_notification_retryable(
                    operation_id,
                    self.worker_id,
                    "hermes send returned unsuccessful JSON",
                    next_attempt_at=now + self.retry_delay_seconds,
                    now=now,
                )
                return GatewayNotificationResult("retryable", ticket_id, operation_id)
            if self.ledger.mark_gateway_notification_delivered(operation_id, self.worker_id, now=now):
                return GatewayNotificationResult("delivered", ticket_id, operation_id)
            if not self.ledger.mark_gateway_notification_delivery_unknown(
                operation_id,
                self.worker_id,
                "hermes send returned success but delivery lease was no longer finalizable",
                now=now,
            ):
                raise RuntimeError("gateway notification delivery lost its lease and ambiguity could not be recorded")
            return GatewayNotificationResult("delivery_unknown", ticket_id, operation_id)
        error = f"hermes send failed with status {result.returncode}: {(result.stderr or result.stdout or '').strip()}"
        if result.returncode == 2:
            self.ledger.mark_gateway_notification_permanently_failed(operation_id, self.worker_id, error, now=now)
            return GatewayNotificationResult("permanently_failed", ticket_id, operation_id)
        self.ledger.mark_gateway_notification_retryable(
            operation_id,
            self.worker_id,
            error,
            next_attempt_at=now + self.retry_delay_seconds,
            now=now,
        )
        return GatewayNotificationResult("retryable", ticket_id, operation_id)
