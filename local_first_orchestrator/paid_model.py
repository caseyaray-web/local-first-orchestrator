from __future__ import annotations

import json
import sqlite3
import subprocess
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


class PaidProviderRejectedError(PaidInvocationError):
    def __init__(self, kind: str, detail: str, *, retry_after_at: int | None, retryable: bool, failure_count: int) -> None:
        super().__init__(f"paid provider rejected request: {kind}")
        self.kind = kind
        self.detail = detail
        self.retry_after_at = retry_after_at
        self.retryable = retryable
        self.failure_count = failure_count


class _ConfirmedProviderRejection(RuntimeError):
    def __init__(self, kind: str, detail: str, *, retry_after_seconds: int, retryable: bool = True) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds
        self.retryable = retryable


class InjectedPaidModelAdapter:
    """Test-only adapter. The injected callable is the sole possible invocation path."""
    is_paid = True
    cost_class = "paid"
    role = "decomposition"
    provider = "injected"
    model = "injected-paid-planner"
    profile = "fixture"
    routing_source = "injected-fixture"
    planner_contract_hash = "injected-fixture-contract"

    def __init__(
        self,
        ledger: Ledger,
        governor: UsageGovernor,
        runner: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        provider_alert_target: str | None = None,
        provider_alert_failure_threshold: int = 3,
    ) -> None:
        if provider_alert_failure_threshold < 1:
            raise ValueError("provider alert threshold must be positive")
        self.ledger, self.governor, self.runner = ledger, governor, runner
        self.provider_alert_target = provider_alert_target.strip() if isinstance(provider_alert_target, str) and provider_alert_target.strip() else None
        self.provider_alert_failure_threshold = provider_alert_failure_threshold

    def _provider_alert_ticket_id(self, request_key: str, packet: dict[str, Any]) -> str | None:
        ticket_id = packet.get("ticket_id")
        if isinstance(ticket_id, str) and self.ledger.connection.execute("SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone() is not None:
            return ticket_id
        claim = self.ledger.connection.execute("SELECT ticket_id FROM scheduler_stage_claims WHERE claim_id=?", (request_key,)).fetchone()
        if claim is not None:
            return str(claim["ticket_id"])
        tranche_id = packet.get("tranche_id")
        if isinstance(tranche_id, str):
            row = self.ledger.connection.execute("SELECT id FROM tickets WHERE tranche_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (tranche_id,)).fetchone()
            if row is not None:
                return str(row["id"])
        return None

    def invoke(self, feature_id: str, purpose: PaidPurpose, request_key: str, packet: dict[str, Any]) -> dict[str, Any]:
        try:
            reservation = self.governor.authorize(feature_id, purpose, request_key)
        except (PermissionError, ValueError) as exc:
            raise PaidInvocationError(str(exc)) from exc
        existing = self.ledger.connection.execute(
            "SELECT * FROM model_calls WHERE reservation_id=?",
            (reservation.reservation_id,),
        ).fetchone()
        now = self.ledger._now()
        if existing is not None:
            if existing["status"] == "completed" and existing["response_artifact_json"]:
                return json.loads(existing["response_artifact_json"])
            if existing["status"] == "provider_rejected":
                call_id = str(existing["id"])
                with self.ledger._transaction() as conn:
                    changed = conn.execute(
                        "UPDATE model_calls SET status='in_flight',provider_attempt_count=provider_attempt_count+1,updated_at=? WHERE id=? AND status='provider_rejected'",
                        (now, call_id),
                    )
                    if changed.rowcount != 1:
                        raise PaidInvocationError("paid provider retry claim changed concurrently")
            else:
                # A durable in-flight call proves a process may have reached the provider.
                # Fail closed rather than guessing whether it is safe to repeat it.
                if existing["status"] == "in_flight":
                    self.governor.unknown(reservation, "recovered in-flight model call")
                    with self.ledger._transaction() as conn:
                        conn.execute("UPDATE model_calls SET status='unknown_outcome', updated_at=? WHERE reservation_id=?", (now, reservation.reservation_id))
                raise PaidInvocationError("paid invocation outcome is unknown and will not be retried")
        else:
            call_id = uuid.uuid4().hex
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
        except _ConfirmedProviderRejection as exc:
            retry_after_at = self.ledger._now() + exc.retry_after_seconds if exc.retryable else None
            alert_ticket_id = self._provider_alert_ticket_id(request_key, packet) if self.provider_alert_target is not None else None
            failure_count = self.governor.provider_rejected(
                reservation,
                call_id,
                kind=exc.kind,
                detail=exc.detail,
                retryable=exc.retryable,
                retry_after_at=retry_after_at,
                alert_ticket_id=alert_ticket_id,
                alert_target=self.provider_alert_target,
                alert_provider=str(getattr(self, "provider", "paid-provider")),
                alert_model=str(getattr(self, "model", "paid-model")),
                alert_failure_threshold=self.provider_alert_failure_threshold,
            )
            raise PaidProviderRejectedError(
                exc.kind,
                exc.detail,
                retry_after_at=retry_after_at,
                retryable=exc.retryable,
                failure_count=failure_count,
            ) from exc
        except Exception as exc:
            self.governor.unknown(reservation, f"runner outcome ambiguous: {type(exc).__name__}")
            with self.ledger._transaction() as conn:
                conn.execute("UPDATE model_calls SET status='unknown_outcome', updated_at=? WHERE id=?", (self.ledger._now(), call_id))
            raise PaidInvocationError("paid invocation outcome is unknown and will not be retried") from exc
        self.governor.complete(reservation, input_tokens=0, output_tokens=0)
        with self.ledger._transaction() as conn:
            conn.execute("UPDATE model_calls SET status='completed', response_artifact_json=?, input_tokens=0, output_tokens=0, updated_at=? WHERE id=?", (json.dumps(proposal, sort_keys=True), self.ledger._now(), call_id))
        return proposal


class HermesPaidModelAdapter(InjectedPaidModelAdapter):
    """Governor-backed paid Hermes chat adapter with packet-only safe-tool execution."""

    is_paid = True
    cost_class = "paid"
    role = "checkpoint"
    routing_source = "operator-config.paid"

    def __init__(
        self,
        ledger: Ledger,
        governor: UsageGovernor,
        *,
        executable: str,
        provider: str,
        model: str,
        profile: str,
        timeout_seconds: int = 900,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        provider_alert_target: str | None = None,
        provider_alert_failure_threshold: int = 3,
    ) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (executable, provider, model, profile)):
            raise ValueError("paid Hermes route requires executable, provider, model, and profile")
        if timeout_seconds < 1:
            raise ValueError("paid Hermes timeout must be positive")
        self.executable = executable
        self.provider = provider
        self.model = model
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        self.process_runner = runner
        super().__init__(
            ledger,
            governor,
            self._invoke_hermes,
            provider_alert_target=provider_alert_target,
            provider_alert_failure_threshold=provider_alert_failure_threshold,
        )

    def _invoke_hermes(self, packet: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(packet, sort_keys=True, separators=(",", ":"))
        argv = (
            self.executable,
            "chat",
            "--toolsets",
            "safe",
            "--provider",
            self.provider,
            "--model",
            self.model,
            "--query",
            payload,
            "--quiet",
        )
        result = self.process_runner(argv, text=True, capture_output=True, timeout=self.timeout_seconds, check=False)
        if result.returncode:
            detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
            lowered = detail.lower()
            if any(marker in lowered for marker in ("429", "rate limit", "too many requests", "rate_limit")):
                raise _ConfirmedProviderRejection("rate_limited", detail or "provider rate limited request", retry_after_seconds=60)
            if any(marker in lowered for marker in ("quota", "insufficient_quota", "usage limit", "usage_limit", "billing hard limit", "out of credits", "credit balance")):
                raise _ConfirmedProviderRejection("quota_exhausted", detail or "provider quota exhausted", retry_after_seconds=900)
            if any(marker in lowered for marker in ("401", "403", "unauthorized", "forbidden", "invalid api key", "authentication failed")):
                raise _ConfirmedProviderRejection("authentication_rejected", detail or "provider authentication rejected", retry_after_seconds=0, retryable=False)
            raise RuntimeError(f"paid Hermes invocation failed with status {result.returncode}")
        try:
            response = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            raise RuntimeError("paid Hermes invocation returned malformed JSON") from exc
        if not isinstance(response, dict):
            raise RuntimeError("paid Hermes invocation must return one JSON object")
        return response
