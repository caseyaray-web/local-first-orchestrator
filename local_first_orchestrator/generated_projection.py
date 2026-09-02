from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from .decomposition import generated_projection_key
from .ledger import Ledger


class GeneratedProjectionAdapter(Protocol):
    timeout_seconds: int

    def create_microticket(self, title: str, body: str, *, idempotency_key: str) -> str: ...

    def get_task(self, task_id: str) -> Any: ...


class DeterministicProjectionError(ValueError):
    """Persisted data or returned board provenance cannot safely be adopted."""


class GeneratedProjectionCrash(RuntimeError):
    """Test-only abrupt worker-loss sentinel; leaves the durable lease intact."""


@dataclass(frozen=True)
class GeneratedProjectionDeliveryPolicy:
    lease_seconds: int = 45
    retry_delay: int = 30
    lease_margin_seconds: int = 10
    error_length_limit: int = 2_000
    writes_enabled: bool = True

    def validate_for(self, adapter: GeneratedProjectionAdapter) -> None:
        timeout = getattr(adapter, "timeout_seconds", None)
        if not isinstance(timeout, int) or timeout < 1:
            raise ValueError("adapter must expose a positive process timeout")
        if self.retry_delay < 1 or self.error_length_limit < 1 or self.lease_margin_seconds < 1:
            raise ValueError("retry delay, error limit, and lease margin must be positive")
        # The only external window is bounded create followed by bounded show.  The
        # remaining margin covers SQLite completion and scheduler/clock granularity.
        if self.lease_seconds < (2 * timeout) + self.lease_margin_seconds:
            raise ValueError("projection lease does not cover create + show horizon")


DeliveryStatus = Literal["no_work", "delivered", "retry_scheduled", "terminal_failed", "writes_disabled"]


@dataclass(frozen=True)
class GeneratedProjectionDeliveryResult:
    status: DeliveryStatus
    ticket_id: str | None = None
    event_id: int | None = None
    external_task_id: str | None = None
    next_attempt_at: int | None = None
    error: str | None = None


def _contract_from_body(body: str) -> dict[str, Any]:
    marker = "<!-- local-first-orchestrator -->"
    if body.count(marker) != 1:
        raise DeterministicProjectionError("ownership marker mismatch")
    native_fence = "```local-first-contract\n"
    escaped_fence = "```local-first-contract\\n"
    if body.count(native_fence) + body.count(escaped_fence) != 1:
        raise DeterministicProjectionError("local-first contract block mismatch")
    fence = native_fence if native_fence in body else escaped_fence
    terminator = "\n```" if fence == native_fence else "\\n```"
    try:
        encoded = body.split(fence, 1)[1].split(terminator, 1)[0]
        raw = json.loads(encoded)
    except (IndexError, json.JSONDecodeError) as exc:
        raise DeterministicProjectionError("malformed local-first contract") from exc
    if not isinstance(raw, dict):
        raise DeterministicProjectionError("local-first contract must be an object")
    return raw


def _expected_contract_identity(payload: dict[str, str], identity: dict[str, str]) -> dict[str, str]:
    return {
        "kind": "microticket",
        "orchestrator_ticket_id": payload["orchestrator_ticket_id"],
        "feature_id": payload["feature_id"],
        "tranche_id": payload["tranche_id"],
        "projection_key": payload["projection_key"],
        "repository_identity": identity["repository_identity"],
        "repo_base_sha": identity["repo_base_sha"],
        "repo_snapshot_hash": identity["repo_snapshot_hash"],
    }


def _verify_contract_identity(contract: dict[str, Any], payload: dict[str, str], identity: dict[str, str]) -> None:
    for name, value in _expected_contract_identity(payload, identity).items():
        if contract.get(name) != value:
            raise DeterministicProjectionError(f"projected local-first contract {name} mismatch")


def _canonical_payload(row: dict[str, Any], identity: dict[str, str]) -> dict[str, str]:
    try:
        raw = json.loads(str(row["payload_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise DeterministicProjectionError("malformed generated projection payload") from exc
    if not isinstance(raw, dict):
        raise DeterministicProjectionError("generated projection payload must be an object")
    required = ("title", "body", "orchestrator_ticket_id", "feature_id", "tranche_id", "projection_key")
    if any(not isinstance(raw.get(name), str) or not raw[name] for name in required):
        raise DeterministicProjectionError("generated projection payload is incomplete")
    expected_key = generated_projection_key(identity["ticket_id"])
    if row["ticket_id"] != identity["ticket_id"] or raw["orchestrator_ticket_id"] != identity["ticket_id"]:
        raise DeterministicProjectionError("generated projection ticket identity mismatch")
    if raw["feature_id"] != identity["feature_id"]:
        raise DeterministicProjectionError("generated projection feature identity mismatch")
    if raw["tranche_id"] != identity["tranche_id"]:
        raise DeterministicProjectionError("generated projection tranche identity mismatch")
    if (raw["projection_key"], row["idempotency_key"], expected_key) != (expected_key, expected_key, expected_key):
        raise DeterministicProjectionError("generated projection key mismatch")
    _verify_contract_identity(_contract_from_body(raw["body"]), {name: raw[name] for name in required}, identity)
    return {name: raw[name] for name in required}


def _verify_shown_task(task_id: str, shown: Any, payload: dict[str, str], identity: dict[str, str]) -> None:
    if str(getattr(shown, "id", "")) != task_id:
        raise DeterministicProjectionError("shown task identity mismatch")
    _verify_contract_identity(_contract_from_body(str(getattr(shown, "body", ""))), payload, identity)


class GeneratedProjectionWorker:
    """Deliver at most one durable generated-card projection using an explicit board adapter."""

    def __init__(self, ledger: Ledger, adapter: GeneratedProjectionAdapter, policy: GeneratedProjectionDeliveryPolicy | None = None, *, worker_id: str, clock: Callable[[], int] | None = None, fault_injector: Callable[[str], None] | None = None) -> None:
        self.ledger = ledger
        self.adapter = adapter
        self.policy = policy or GeneratedProjectionDeliveryPolicy()
        self.policy.validate_for(adapter)
        self.worker_id = worker_id
        self.clock = clock or Ledger._now
        self.fault_injector = fault_injector

    def _fault(self, stage: str) -> None:
        if self.fault_injector:
            self.fault_injector(stage)

    def _terminal(self, row: dict[str, Any], error: Exception, now: int) -> GeneratedProjectionDeliveryResult:
        message = str(error)[: self.policy.error_length_limit]
        self.ledger.fail_generated_create_projection(str(row["ticket_id"]), int(row["event_id"]), self.worker_id, error=message, now=now)
        return GeneratedProjectionDeliveryResult("terminal_failed", str(row["ticket_id"]), int(row["event_id"]), error=message)

    def _retry(self, row: dict[str, Any], error: Exception, now: int) -> GeneratedProjectionDeliveryResult:
        message = str(error)[: self.policy.error_length_limit]
        next_attempt_at = now + self.policy.retry_delay
        self.ledger.retry_generated_create_projection(str(row["ticket_id"]), int(row["event_id"]), self.worker_id, error=message, next_attempt_at=next_attempt_at, now=now)
        return GeneratedProjectionDeliveryResult("retry_scheduled", str(row["ticket_id"]), int(row["event_id"]), next_attempt_at=next_attempt_at, error=message)

    def deliver_one(self) -> GeneratedProjectionDeliveryResult:
        if not self.policy.writes_enabled or not bool(getattr(self.adapter, "allow_writes", True)):
            return GeneratedProjectionDeliveryResult("writes_disabled")
        now = int(self.clock())
        row = self.ledger.claim_next_generated_create_projection(self.worker_id, lease_seconds=self.policy.lease_seconds, now=now)
        if row is None:
            return GeneratedProjectionDeliveryResult("no_work")
        try:
            identity = self.ledger.generated_projection_identity(str(row["ticket_id"]), int(row["event_id"]))
            payload = _canonical_payload(row, identity)
        except (DeterministicProjectionError, KeyError, ValueError) as exc:
            return self._terminal(row, DeterministicProjectionError(str(exc)), int(self.clock()))
        try:
            task_id = self.adapter.create_microticket(payload["title"], payload["body"], idempotency_key=payload["projection_key"])
            self._fault("after_create")
            shown = self.adapter.get_task(task_id)
        except GeneratedProjectionCrash:
            raise
        except Exception as exc:
            return self._retry(row, exc, int(self.clock()))
        try:
            _verify_shown_task(str(task_id), shown, payload, identity)
        except DeterministicProjectionError as exc:
            return self._terminal(row, exc, int(self.clock()))
        self.ledger.complete_generated_create_projection(str(row["ticket_id"]), int(row["event_id"]), self.worker_id, str(task_id), now=int(self.clock()))
        return GeneratedProjectionDeliveryResult("delivered", str(row["ticket_id"]), int(row["event_id"]), external_task_id=str(task_id))
