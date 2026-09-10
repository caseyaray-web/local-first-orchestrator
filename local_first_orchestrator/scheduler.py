from __future__ import annotations

import json
import uuid
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
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


@dataclass(frozen=True)
class ProcessNextPreview:
    status: str = "dry_run"
    next_stage: str = "no_work"
    ticket_id: str | None = None
    would_execute: bool = False
    would_write_board: bool = False


def preview_next(ledger: Ledger, *, now: int | None = None) -> ProcessNextPreview:
    """Read the next eligible control stage without claiming or mutating it."""
    now = Ledger._now() if now is None else now
    paused = ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
    if paused is None or paused["paused"]:
        return ProcessNextPreview(next_stage="paused")
    tick = ledger.connection.execute("SELECT lease_expires_at FROM scheduler_tick_lease WHERE id=1").fetchone()
    if tick is not None and int(tick["lease_expires_at"]) > now:
        return ProcessNextPreview(next_stage="busy")

    state = ledger.connection.execute(
        "SELECT ticket_id,event_id FROM board_projection_outbox WHERE operation='set_state' AND acknowledged_at IS NULL "
        "AND superseded_at IS NULL AND terminal_error IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
        "AND (lease_expires_at IS NULL OR lease_expires_at<=?) ORDER BY queued_at,event_id LIMIT 1",
        (now, now),
    ).fetchone()
    if state is not None:
        latest = ledger.connection.execute(
            "SELECT id FROM events WHERE entity_type='ticket' AND entity_id=? "
            "AND event_type IN ('state_transition','review_reconciliation_authorized') ORDER BY id DESC LIMIT 1",
            (state["ticket_id"],),
        ).fetchone()
        current = latest is None or int(latest["id"]) <= int(state["event_id"])
        return ProcessNextPreview(next_stage="state_projection", ticket_id=str(state["ticket_id"]), would_write_board=current)

    generated = ledger.connection.execute(
        "SELECT ticket_id FROM board_projection_outbox WHERE operation='create_microticket' AND terminal_error IS NULL "
        "AND acknowledged_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
        "AND (lease_expires_at IS NULL OR lease_expires_at<=?) ORDER BY queued_at LIMIT 1",
        (now, now),
    ).fetchone()
    if generated is not None:
        return ProcessNextPreview(next_stage="generated_projection", ticket_id=str(generated["ticket_id"]), would_write_board=True)

    comment = ledger.connection.execute(
        "SELECT ticket_id FROM evidence_comment_outbox WHERE status IN ('pending','retryable') "
        "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at,operation_id LIMIT 1",
        (now,),
    ).fetchone()
    if comment is not None:
        return ProcessNextPreview(next_stage="evidence_comment", ticket_id=str(comment["ticket_id"]), would_write_board=True)

    validation_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage LIKE 'validation:%' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if validation_replay is not None:
        return ProcessNextPreview(next_stage="validation", ticket_id=str(validation_replay["ticket_id"]), would_execute=True)
    validation = ledger.connection.execute(
        "SELECT t.id FROM tickets t JOIN model_stage_artifacts m ON m.ticket_id=t.id AND m.stage='implementation' "
        "AND m.attempt_number=(SELECT MAX(latest.attempt_number) FROM model_stage_artifacts latest WHERE latest.ticket_id=t.id AND latest.stage='implementation') "
        "WHERE t.state='implementing' AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('validation:' || m.attempt_number)) "
        "AND NOT EXISTS (SELECT 1 FROM runtime_stages r WHERE r.ticket_id=t.id AND r.stage=('validation-' || m.attempt_number)) "
        "ORDER BY t.created_at,t.id LIMIT 1"
    ).fetchone()
    if validation is not None:
        return ProcessNextPreview(next_stage="validation", ticket_id=str(validation["id"]), would_execute=True)

    review_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage LIKE 'review:%' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if review_replay is not None:
        return ProcessNextPreview(next_stage="review", ticket_id=str(review_replay["ticket_id"]), would_execute=True)
    review = ledger.connection.execute(
        "SELECT t.id FROM tickets t JOIN runtime_stages r ON r.ticket_id=t.id AND r.stage='validation_completed' "
        "WHERE t.state='local_review' AND EXISTS (SELECT 1 FROM scheduler_stage_claims v WHERE v.ticket_id=t.id AND v.stage=('validation:' || r.attempt_number) AND v.side_effect_completed_at IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('review:' || r.attempt_number)) "
        "ORDER BY t.created_at,t.id LIMIT 1"
    ).fetchone()
    if review is not None:
        return ProcessNextPreview(next_stage="review", ticket_id=str(review["id"]), would_execute=True)

    repair_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage LIKE 'repair_routing:%' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if repair_replay is not None:
        return ProcessNextPreview(next_stage="repair_routing", ticket_id=str(repair_replay["ticket_id"]), would_execute=True)
    repair = ledger.connection.execute("""
        SELECT t.id FROM tickets t
        WHERE ((
            t.state='verifying' AND EXISTS (
                SELECT 1 FROM runtime_stages r WHERE r.ticket_id=t.id AND r.stage=('validation-' || r.attempt_number)
                AND json_valid(r.detail)=1 AND json_extract(r.detail,'$.passed')=0
            )
        ) OR (
            t.state='local_review' AND EXISTS (
                SELECT 1 FROM model_stage_artifacts m WHERE m.ticket_id=t.id AND m.stage='review'
            )
        ))
        AND NOT EXISTS (
            SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage LIKE 'repair_routing:%'
        )
        ORDER BY t.created_at,t.id LIMIT 1
    """).fetchone()
    if repair is not None:
        return ProcessNextPreview(next_stage="repair_routing", ticket_id=str(repair["id"]), would_execute=True)

    implementation_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE (stage='implementation' OR stage LIKE 'implementation:%') AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if implementation_replay is not None:
        return ProcessNextPreview(next_stage="implementation", ticket_id=str(implementation_replay["ticket_id"]), would_execute=True)
    implementation = ledger.connection.execute(
        "SELECT t.id FROM tickets t JOIN runtime_bindings rb ON rb.ticket_id=t.id "
        "WHERE t.state IN (?,?) AND (t.lease_expires_at IS NULL OR t.lease_expires_at<=?) "
        "AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND (c.stage='implementation' OR c.stage LIKE 'implementation:%') AND c.status='claimed') "
        "ORDER BY t.created_at,t.id LIMIT 1",
        ("ready_local", "repairing", now),
    ).fetchone()
    if implementation is not None:
        return ProcessNextPreview(next_stage="implementation", ticket_id=str(implementation["id"]), would_execute=True)

    replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage='dependency_readiness' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if replay is not None:
        return ProcessNextPreview(next_stage="dependency_readiness", ticket_id=str(replay["ticket_id"]))
    readiness = ledger.connection.execute("""
        SELECT t.id FROM tickets t JOIN runtime_bindings rb ON rb.ticket_id=t.id
        WHERE t.state=? AND json_valid(t.dependencies_json)=1 AND json_type(t.dependencies_json)='array'
          AND NOT EXISTS (
            SELECT 1 FROM json_each(CASE WHEN json_valid(t.dependencies_json) THEN t.dependencies_json ELSE '[]' END) requested
            LEFT JOIN tickets dependency ON dependency.id=requested.value
            LEFT JOIN accepted_evidence evidence ON evidence.ticket_id=dependency.id
            WHERE dependency.id IS NULL OR dependency.state NOT IN (?, ?)
               OR (dependency.state=? AND evidence.ticket_id IS NULL)
          )
          AND NOT EXISTS (
            SELECT 1 FROM scheduler_stage_claims claim
            WHERE claim.ticket_id=t.id AND claim.stage='dependency_readiness'
          )
        ORDER BY t.created_at,t.id LIMIT 1
    """, ("draft", "accepted", "done", "done")).fetchone()
    if readiness is not None:
        return ProcessNextPreview(next_stage="dependency_readiness", ticket_id=str(readiness["id"]))
    return ProcessNextPreview()


def preview_database(database: Path, *, now: int | None = None) -> ProcessNextPreview:
    """Preview an existing migrated ledger through a strictly read-only handle."""
    try:
        path = Path(database).expanduser().resolve(strict=True)
    except FileNotFoundError:
        return ProcessNextPreview(next_stage="no_work")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return preview_next(SimpleNamespace(connection=connection), now=now)  # type: ignore[arg-type]
    except sqlite3.DatabaseError:
        return ProcessNextPreview(next_stage="no_work")
    finally:
        if connection is not None:
            connection.close()


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
        implementation_runner: Callable[[str], dict[str, Any]] | None = None,
        validation_runner: Callable[[str], dict[str, Any]] | None = None,
        review_runner: Callable[[str], dict[str, Any]] | None = None,
        review_execution_policy_hash: str | None = None,
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
        self.implementation_runner = implementation_runner
        self.validation_runner = validation_runner
        self.review_runner = review_runner
        self.review_execution_policy_hash = review_execution_policy_hash
        if self.review_runner is not None and not self.review_execution_policy_hash:
            raise ValueError("process-next review runner requires a review execution policy hash")

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

        if self.validation_runner is not None:
            validation_claim = self.ledger.claim_next_scheduler_validation(
                execution_owner, lease_seconds=self.lease_seconds, now=now
            )
            if validation_claim is not None:
                claim_id = str(validation_claim["claim_id"])
                ticket_id = str(validation_claim["ticket_id"])
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    validation_result = json.loads(str(current["result_json"]))
                    self.ledger.complete_scheduler_validation_effect(
                        claim_id, execution_owner, validation_result, now=now
                    )
                else:
                    started = current.get("side_effect_started_at") is not None
                    if started:
                        identity: dict[str, Any] = {}
                        try:
                            identity = json.loads(str(current.get("candidate_identity_json") or ""))
                            evidence = self.ledger.runtime_stage(ticket_id, f"validation-{int(identity['attempt_number'])}")
                            detail = json.loads(str(evidence["detail"])) if evidence else {}
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            detail = {}
                        if detail.get("candidate_identity") != identity:
                            raise RuntimeError("validation_reconciliation_required: started validation outcome is unknown")
                    else:
                        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                    validation_result = self.validation_runner(ticket_id)
                    self.ledger.complete_scheduler_validation_effect(
                        claim_id, execution_owner, validation_result, now=now
                    )
                self.ledger.complete_scheduler_claim(
                    claim_id, execution_owner, validation_result, now=now
                )
                return ProcessNextResult("completed", "validation", ticket_id, claim_id)

        if self.review_runner is not None:
            review_claim = self.ledger.claim_next_scheduler_review(
                execution_owner,
                lease_seconds=self.lease_seconds,
                review_execution_policy_hash=str(self.review_execution_policy_hash),
                now=now,
            )
            if review_claim is not None:
                claim_id = str(review_claim["claim_id"])
                ticket_id = str(review_claim["ticket_id"])
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    review_result = json.loads(str(current["result_json"]))
                else:
                    if current.get("side_effect_started_at") is not None:
                        try:
                            identity = json.loads(str(current.get("candidate_identity_json") or ""))
                            attempt_number = int(identity["attempt_number"])
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                            raise RuntimeError("review_reconciliation_required: review claim identity is malformed") from exc
                        review_stage = self.ledger.model_stage(ticket_id, attempt_number, "review")
                        invocations = self.ledger.review_invocations(ticket_id, attempt_number)
                        if review_stage is None and invocations and str(invocations[-1]["status"]) != "completed":
                            raise RuntimeError("review_reconciliation_required: started review outcome is unknown")
                    else:
                        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                    review_result = self.review_runner(ticket_id)
                    self.ledger.complete_scheduler_review_effect(
                        claim_id, execution_owner, review_result, now=now
                    )
                self.ledger.complete_scheduler_claim(
                    claim_id, execution_owner, review_result, now=now
                )
                return ProcessNextResult("completed", "review", ticket_id, claim_id)

        repair_claim = self.ledger.claim_next_scheduler_repair_routing(
            execution_owner, lease_seconds=self.lease_seconds, now=now
        )
        if repair_claim is not None:
            claim_id = str(repair_claim["claim_id"])
            ticket_id = str(repair_claim["ticket_id"])
            current = self.ledger.scheduler_claim(claim_id)
            if current.get("side_effect_completed_at") is None:
                if current.get("side_effect_started_at") is None:
                    self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                current = self.ledger.apply_scheduler_repair_routing_effect(claim_id, execution_owner, now=now)
            result = json.loads(str(current["result_json"]))
            self.ledger.complete_scheduler_claim(claim_id, execution_owner, result, now=now)
            return ProcessNextResult("completed", "repair_routing", ticket_id, claim_id)

        if self.implementation_runner is not None:
            implementation_claim = self.ledger.claim_next_scheduler_implementation(
                execution_owner, lease_seconds=self.lease_seconds, now=now
            )
            if implementation_claim is not None:
                claim_id = str(implementation_claim["claim_id"])
                ticket_id = str(implementation_claim["ticket_id"])
                self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    implementation_result = json.loads(str(current["result_json"]))
                else:
                    implementation_result = self.implementation_runner(ticket_id)
                    self.ledger.complete_scheduler_implementation_effect(
                        claim_id, execution_owner, implementation_result, now=now
                    )
                self.ledger.complete_scheduler_claim(
                    claim_id, execution_owner, implementation_result, now=now
                )
                return ProcessNextResult(
                    "completed", "implementation", ticket_id, claim_id
                )

        claim = self.ledger.claim_next_scheduler_readiness(
            execution_owner, lease_seconds=self.lease_seconds, now=now
        )
        if claim is None:
            return ProcessNextResult("no_work")
        claim_id = str(claim["claim_id"])
        ticket_id = str(claim["ticket_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
        applied = self.ledger.apply_scheduler_readiness_effect(claim_id, execution_owner, now=now)
        result = {
            "status": "ready",
            "unresolved_dependency_ids": [],
        }
        if applied.get("result_json"):
            result = json.loads(str(applied["result_json"]))
        self.ledger.complete_scheduler_claim(
            claim_id, execution_owner, result, now=now
        )
        return ProcessNextResult(
            "completed",
            "dependency_readiness",
            ticket_id,
            claim_id,
        )
