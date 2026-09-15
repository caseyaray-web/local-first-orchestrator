from __future__ import annotations

import json
import uuid
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .comment_delivery import CommentDeliveryWorker
from .execution_handoff import HANDOFF_MARKER
from .generated_projection import GeneratedProjectionWorker
from .ledger import Ledger
from .paid_model import PaidInvocationError
from .reconciliation import ReconciliationAction
from .state_projection import StateProjectionWorker
from .states import CanonicalState
from .runtime_metrics import RuntimeMetricsStore
from .evidence_hash import canonical_sha256


SCHEDULER_STAGE_ORDER: tuple[str, ...] = (
    "generated_projection",
    "state_projection",
    "evidence_comment",
    "recovery",
    "implementation",
    "validation",
    "review",
    "repair_routing",
    "triage",
    "acceptance",
    "git_integration",
    "completion",
    "native_dependency_graph",
    "native_dependency_release",
    "tranche_checkpoint",
    "paid_checkpoint",
    "paid_escalation",
    "next_tranche_materialize",
    "next_tranche_activation",
    "dependency_readiness",
)

SCHEDULER_STAGE_RANK = {stage: rank for rank, stage in enumerate(SCHEDULER_STAGE_ORDER)}


def scheduler_stage_rank(stage: str) -> int:
    if stage not in SCHEDULER_STAGE_RANK:
        raise KeyError(stage)
    return SCHEDULER_STAGE_RANK[stage]


def scheduler_observability(ledger: Ledger, *, now: int | None = None, signer_public_key: bytes | None = None, signer_fingerprint: str | None = None, board: Any | None = None) -> dict[str, Any]:
    """Return one bounded, read-only scheduler lifecycle snapshot.

    This is a projection over existing durable authorities.  It never writes or
    persists a second metrics/recovery state machine.
    """
    now = ledger._now() if now is None else now
    preview = preview_next(ledger, now=now, signer_public_key=signer_public_key, signer_fingerprint=signer_fingerprint, board=board)
    claim = None
    if preview.claim_id is not None:
        claim = ledger.connection.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (preview.claim_id,)).fetchone()
    if claim is None and preview.ticket_id is not None:
        claim = ledger.connection.execute(
            "SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND status='claimed' ORDER BY created_at,claim_id LIMIT 1",
            (preview.ticket_id,),
        ).fetchone()
    if claim is None:
        claim = ledger.connection.execute(
            "SELECT * FROM scheduler_stage_claims WHERE status='claimed' ORDER BY created_at,claim_id LIMIT 1"
        ).fetchone()
    claim_dict = None if claim is None else dict(claim)
    ticket_id = preview.ticket_id or (None if claim is None else str(claim["ticket_id"]))
    current_stage = None if claim is None else str(claim["stage"])

    attempt_number = None
    reconciliation = None
    if claim is not None:
        identity = {}
        try:
            identity = json.loads(str(claim["candidate_identity_json"] or "{}"))
        except json.JSONDecodeError:
            identity = {}
        if isinstance(identity.get("attempt_number"), int):
            attempt_number = int(identity["attempt_number"])
        if claim["lease_expires_at"] is not None and int(claim["lease_expires_at"]) <= now:
            decision = ledger.scheduler_reconciliation(str(claim["claim_id"]))
            reconciliation = {
                "state": decision.state.value,
                "action": decision.action.value,
                "reason": decision.reason,
                "evidence_kind": decision.evidence_kind,
            }

    if ticket_id is not None and attempt_number is None:
        row = ledger.connection.execute(
            "SELECT attempt_number FROM attempts WHERE ticket_id=? ORDER BY attempt_number DESC LIMIT 1", (ticket_id,)
        ).fetchone()
        if row is not None:
            attempt_number = int(row["attempt_number"])

    model = None
    runtime_stage = None
    review = None
    accepted = None
    git = None
    paid = None
    if ticket_id is not None:
        row = ledger.connection.execute(
            "SELECT invocation_id,attempt_number,stage,provider,model,status,started_at,completed_at,error_json,model_artifact "
            "FROM model_invocations WHERE ticket_id=? ORDER BY started_at DESC,invocation_id DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if row is not None:
            model = dict(row)
        row = ledger.connection.execute(
            "SELECT stage,detail,attempt_number,artifact_path,artifact_sha256,base_sha,created_at "
            "FROM runtime_stages WHERE ticket_id=? ORDER BY created_at DESC,stage DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if row is not None:
            runtime_stage = dict(row)
        row = ledger.connection.execute(
            "SELECT id,attempt_number,verdict,created_at FROM review_results WHERE ticket_id=? ORDER BY attempt_number DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if row is not None:
            review = dict(row)
        row = ledger.connection.execute(
            "SELECT attempt_number,candidate_fingerprint,implementation_artifact,implementation_artifact_sha256,validation_artifact,validation_artifact_sha256,review_artifact,review_artifact_sha256,review_result_id,evidence_hash,created_at "
            "FROM accepted_candidates WHERE ticket_id=?",
            (ticket_id,),
        ).fetchone()
        if row is not None:
            accepted = dict(row)
        row = ledger.connection.execute(
            "SELECT attempt_number,status,commit_sha,created_at,completed_at FROM git_commit_intents WHERE ticket_id=?",
            (ticket_id,),
        ).fetchone()
        evidence = ledger.connection.execute(
            "SELECT attempt_number,commit_sha,tranche_id,integration_head_before,integration_head_after,created_at FROM git_commit_evidence WHERE ticket_id=?",
            (ticket_id,),
        ).fetchone()
        if row is not None or evidence is not None:
            git = {"intent": None if row is None else dict(row), "evidence": None if evidence is None else dict(evidence)}

    if claim is not None:
        reservation = ledger.connection.execute(
            "SELECT id,feature_id,purpose,request_key,status,created_at,updated_at FROM paid_reservations WHERE request_key=? ORDER BY created_at DESC LIMIT 1",
            (str(claim["claim_id"]),),
        ).fetchone()
        if reservation is not None:
            paid = dict(reservation)

    effects = {
        "generated_projection": int(ledger.connection.execute(
            "SELECT COUNT(*) FROM board_projection_outbox WHERE operation='create_microticket' AND acknowledged_at IS NULL AND superseded_at IS NULL"
        ).fetchone()[0]),
        "state_projection": int(ledger.connection.execute(
            "SELECT COUNT(*) FROM board_projection_outbox WHERE operation='set_state' AND acknowledged_at IS NULL AND superseded_at IS NULL"
        ).fetchone()[0]),
        "evidence_comment": int(ledger.connection.execute(
            "SELECT COUNT(*) FROM evidence_comment_outbox WHERE status IN ('pending','retryable','delivering')"
        ).fetchone()[0]),
    }
    leased_effects = [dict(row) for row in ledger.connection.execute(
        "SELECT ticket_id,operation,lease_owner,lease_expires_at FROM board_projection_outbox "
        "WHERE acknowledged_at IS NULL AND superseded_at IS NULL AND lease_owner IS NOT NULL "
        "ORDER BY queued_at,event_id LIMIT 10"
    )]

    return {
        "observed_at": now,
        "paused": bool(ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()[0]),
        "current_stage": current_stage,
        "next_stage": preview.next_stage,
        "ticket_id": ticket_id,
        "claim": None if claim_dict is None else {
            "claim_id": claim_dict["claim_id"],
            "stage": claim_dict["stage"],
            "status": claim_dict["status"],
            "lease_owner": claim_dict["lease_owner"],
            "lease_expires_at": claim_dict["lease_expires_at"],
            "attempt_count": claim_dict["attempt_count"],
            "side_effect_started_at": claim_dict["side_effect_started_at"],
            "side_effect_completed_at": claim_dict["side_effect_completed_at"],
            "finalized_at": claim_dict["finalized_at"],
        },
        "attempt_number": attempt_number,
        "reconciliation": reconciliation,
        "pending_effects": effects,
        "leased_effects": leased_effects,
        "latest_runtime_stage": runtime_stage,
        "model_invocation": model,
        "review_result": review,
        "accepted_candidate": accepted,
        "git": git,
        "paid_reservation": paid,
    }


@dataclass(frozen=True)
class ProcessNextResult:
    status: str
    stage: str | None = None
    ticket_id: str | None = None
    claim_id: str | None = None
    reconciliation_action: str | None = None


@dataclass(frozen=True)
class ProcessNextPreview:
    status: str = "dry_run"
    next_stage: str = "no_work"
    ticket_id: str | None = None
    would_execute: bool = False
    would_write_board: bool = False
    claim_id: str | None = None
    reconciliation_action: str | None = None


def preview_next(ledger: Ledger, *, now: int | None = None, signer_public_key: bytes | None = None, signer_fingerprint: str | None = None, board: Any | None = None) -> ProcessNextPreview:
    """Read the next eligible control stage without claiming or mutating it."""
    now = Ledger._now() if now is None else now
    paused = ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
    if paused is None or paused["paused"]:
        return ProcessNextPreview(next_stage="paused")
    tick = ledger.connection.execute("SELECT lease_expires_at FROM scheduler_tick_lease WHERE id=1").fetchone()
    if tick is not None and int(tick["lease_expires_at"]) > now:
        return ProcessNextPreview(next_stage="busy")

    reconciliation = ledger.next_scheduler_reconciliation(now=now) if hasattr(ledger, "next_scheduler_reconciliation") else None
    if reconciliation is not None and reconciliation.action == ReconciliationAction.STOP:
        return ProcessNextPreview(
            next_stage="reconciliation_required",
            ticket_id=reconciliation.ticket_id,
            claim_id=reconciliation.claim_id,
            reconciliation_action=reconciliation.action.value,
        )
    migration = ledger.native_dependency_release_migration_required(signer_public_key=signer_public_key, signer_fingerprint=signer_fingerprint) if hasattr(ledger, "native_dependency_release_migration_required") else None
    if migration is not None:
        return ProcessNextPreview(next_stage="reconciliation_required", ticket_id=str(migration["ticket_id"]), reconciliation_action=ReconciliationAction.STOP.value)
    legacy = ledger.connection.execute("SELECT ticket_id,external_task_id,snapshot_hash FROM native_dependency_release_revalidations r JOIN native_dependency_releases l USING(ticket_id) WHERE json_extract(l.routing_authority_json,'$.profile') IS NULL").fetchall()
    if legacy:
        if board is None or not hasattr(board, "revalidation"):
            return ProcessNextPreview(next_stage="reconciliation_required", ticket_id=str(legacy[0]["ticket_id"]), reconciliation_action=ReconciliationAction.STOP.value)
        try:
            for row in legacy:
                with board.revalidation(str(row["ticket_id"]), str(row["external_task_id"])) as proof:
                    if canonical_sha256(asdict(proof.snapshot)) != str(row["snapshot_hash"]):
                        raise RuntimeError("board snapshot drift")
        except Exception:
            return ProcessNextPreview(next_stage="reconciliation_required", ticket_id=str(legacy[0]["ticket_id"]), reconciliation_action=ReconciliationAction.STOP.value)

    generated = ledger.connection.execute(
        "SELECT ticket_id FROM board_projection_outbox WHERE operation='create_microticket' AND terminal_error IS NULL "
        "AND acknowledged_at IS NULL AND superseded_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
        "AND (lease_expires_at IS NULL OR lease_expires_at<=?) ORDER BY queued_at LIMIT 1",
        (now, now),
    ).fetchone()
    if generated is not None:
        return ProcessNextPreview(next_stage="generated_projection", ticket_id=str(generated["ticket_id"]), would_write_board=True)

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

    comment = ledger.connection.execute(
        "SELECT ticket_id FROM evidence_comment_outbox WHERE status IN ('pending','retryable') "
        "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at,operation_id LIMIT 1",
        (now,),
    ).fetchone()
    if comment is not None:
        return ProcessNextPreview(next_stage="evidence_comment", ticket_id=str(comment["ticket_id"]), would_write_board=True)

    if reconciliation is not None:
        family = ledger._scheduler_stage_family(reconciliation.stage)
        return ProcessNextPreview(
            next_stage=family,
            ticket_id=reconciliation.ticket_id,
            would_execute=True,
            claim_id=reconciliation.claim_id,
            reconciliation_action=reconciliation.action.value,
        )

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
        "AND NOT EXISTS (SELECT 1 FROM board_projection_outbox b WHERE b.ticket_id=t.id AND b.operation='create_microticket' AND b.superseded_at IS NULL) "
        "ORDER BY t.created_at,t.id LIMIT 1",
        ("ready_local", "repairing", now),
    ).fetchone()
    if implementation is not None:
        return ProcessNextPreview(next_stage="implementation", ticket_id=str(implementation["id"]), would_execute=True)


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

    triage_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage LIKE 'triage:%' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if triage_replay is not None:
        return ProcessNextPreview(next_stage="triage", ticket_id=str(triage_replay["ticket_id"]), would_execute=True)
    triage = ledger.connection.execute("""
        SELECT t.id FROM tickets t
        JOIN runtime_stages r ON r.ticket_id=t.id AND r.stage=('repair-routing-' || r.attempt_number)
        WHERE t.state='needs_triage'
          AND json_valid(r.detail)=1
          AND json_extract(r.detail,'$.action')='triage'
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('triage:' || r.attempt_number))
        ORDER BY t.created_at,t.id,r.attempt_number DESC LIMIT 1
    """).fetchone()
    if triage is not None:
        return ProcessNextPreview(next_stage="triage", ticket_id=str(triage["id"]), would_execute=True)

    acceptance_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage LIKE 'acceptance:%' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if acceptance_replay is not None:
        return ProcessNextPreview(next_stage="acceptance", ticket_id=str(acceptance_replay["ticket_id"]), would_execute=True)
    acceptance = ledger.connection.execute("""
        SELECT t.id FROM tickets t
        JOIN runtime_stages route ON route.ticket_id=t.id AND route.stage=('repair-routing-' || route.attempt_number)
        JOIN review_results rr ON rr.ticket_id=t.id AND rr.attempt_number=route.attempt_number
        WHERE t.state='local_review'
          AND json_valid(route.detail)=1
          AND json_extract(route.detail,'$.action')='pass'
          AND rr.verdict='pass'
          AND NOT EXISTS (SELECT 1 FROM accepted_candidates ac WHERE ac.ticket_id=t.id)
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('acceptance:' || route.attempt_number))
        ORDER BY t.created_at,t.id LIMIT 1
    """).fetchone()
    if acceptance is not None:
        return ProcessNextPreview(next_stage="acceptance", ticket_id=str(acceptance["id"]), would_execute=True)

    git_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage LIKE 'git_integration:%' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if git_replay is not None:
        return ProcessNextPreview(next_stage="git_integration", ticket_id=str(git_replay["ticket_id"]), would_execute=True)
    git_candidate = ledger.connection.execute("""
        SELECT t.id FROM tickets t
        JOIN accepted_candidates ac ON ac.ticket_id=t.id
        WHERE t.state='accepted'
          AND NOT EXISTS (SELECT 1 FROM git_commit_evidence ge WHERE ge.ticket_id=t.id)
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('git_integration:' || ac.attempt_number))
        ORDER BY t.created_at,t.id LIMIT 1
    """).fetchone()
    if git_candidate is not None:
        return ProcessNextPreview(next_stage="git_integration", ticket_id=str(git_candidate["id"]), would_execute=True)

    completion_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage LIKE 'completion:%' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if completion_replay is not None:
        return ProcessNextPreview(next_stage="completion", ticket_id=str(completion_replay["ticket_id"]), would_execute=True)
    completion = ledger.connection.execute("""
        SELECT t.id FROM tickets t
        JOIN accepted_candidates ac ON ac.ticket_id=t.id
        JOIN git_commit_evidence ge ON ge.ticket_id=t.id AND ge.attempt_number=ac.attempt_number
        JOIN git_commit_intents gi ON gi.ticket_id=t.id AND gi.attempt_number=ac.attempt_number
        WHERE t.state='accepted'
          AND gi.status='completed'
          AND gi.commit_sha=ge.commit_sha
          AND NOT EXISTS (SELECT 1 FROM accepted_evidence ae WHERE ae.ticket_id=t.id)
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('completion:' || ac.attempt_number))
        ORDER BY t.created_at,t.id LIMIT 1
    """).fetchone()
    if completion is not None:
        return ProcessNextPreview(next_stage="completion", ticket_id=str(completion["id"]), would_execute=True)

    native_graph_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage='native_dependency_graph' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if native_graph_replay is not None:
        return ProcessNextPreview(next_stage="native_dependency_graph", ticket_id=str(native_graph_replay["ticket_id"]), would_execute=True, would_write_board=True)
    native_graph = ledger.connection.execute("""
        SELECT t.id FROM tickets t JOIN runtime_bindings rb ON rb.ticket_id=t.id
        WHERE t.state IN ('draft','ready_local')
          AND json_valid(t.dependencies_json)=1
          AND json_type(t.dependencies_json)='array'
          AND (json_array_length(t.dependencies_json)>0 OR 1=(
              SELECT COUNT(*) FROM board_projection_outbox root_projection
              JOIN events root_event ON root_event.id=root_projection.event_id
                AND root_event.entity_type='ticket' AND root_event.entity_id=t.id
                AND root_event.event_type IN ('generated_microticket_created','generated_microticket_projection_recovered')
              WHERE root_projection.ticket_id=t.id
                AND root_projection.operation='create_microticket'
                AND root_projection.acknowledged_at IS NOT NULL
                AND root_projection.external_task_id IS NOT NULL
                AND root_projection.superseded_at IS NULL))
          AND (t.external_id IS NOT NULL OR EXISTS (
              SELECT 1 FROM board_projection_outbox b WHERE b.ticket_id=t.id AND b.operation='create_microticket'
                AND b.acknowledged_at IS NOT NULL AND b.external_task_id IS NOT NULL AND b.superseded_at IS NULL))
          AND NOT EXISTS (
              SELECT 1 FROM json_each(t.dependencies_json) requested
              LEFT JOIN tickets dependency ON dependency.id=requested.value
              WHERE dependency.id IS NULL OR (
                  dependency.external_id IS NULL AND NOT EXISTS (
                      SELECT 1 FROM board_projection_outbox b WHERE b.ticket_id=dependency.id AND b.operation='create_microticket'
                        AND b.acknowledged_at IS NOT NULL AND b.external_task_id IS NOT NULL AND b.superseded_at IS NULL)))
          AND NOT EXISTS (SELECT 1 FROM native_dependency_graphs g WHERE g.ticket_id=t.id)
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage='native_dependency_graph')
        ORDER BY t.created_at,t.id LIMIT 1
    """).fetchone()
    if native_graph is not None:
        return ProcessNextPreview(next_stage="native_dependency_graph", ticket_id=str(native_graph["id"]), would_execute=True, would_write_board=True)

    native_release_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage='native_dependency_release' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if native_release_replay is not None:
        return ProcessNextPreview(next_stage="native_dependency_release", ticket_id=str(native_release_replay["ticket_id"]), would_execute=True)
    native_release = ledger.connection.execute("""
        SELECT t.id FROM tickets t JOIN native_dependency_graphs g ON g.ticket_id=t.id
        WHERE t.state IN ('draft','ready_local')
          AND NOT EXISTS (SELECT 1 FROM native_dependency_releases r WHERE r.ticket_id=t.id)
          AND json_valid(t.dependencies_json)=1 AND json_type(t.dependencies_json)='array'
          AND json_valid(g.local_dependency_ids_json)=1 AND json_type(g.local_dependency_ids_json)='array'
          AND json_array_length(t.dependencies_json)=json_array_length(g.local_dependency_ids_json)
          AND NOT EXISTS (SELECT 1 FROM json_each(t.dependencies_json) requested
                          WHERE requested.value NOT IN (SELECT value FROM json_each(g.local_dependency_ids_json)))
          AND (json_array_length(t.dependencies_json)>0 OR 1=(
              SELECT COUNT(*) FROM board_projection_outbox root_projection
              JOIN events root_event ON root_event.id=root_projection.event_id
                AND root_event.entity_type='ticket' AND root_event.entity_id=t.id
                AND root_event.event_type IN ('generated_microticket_created','generated_microticket_projection_recovered')
              WHERE root_projection.ticket_id=t.id
                AND root_projection.operation='create_microticket'
                AND root_projection.acknowledged_at IS NOT NULL
                AND root_projection.external_task_id IS NOT NULL
                AND root_projection.superseded_at IS NULL))
          AND NOT EXISTS (
              SELECT 1 FROM json_each(g.local_dependency_ids_json) requested
              LEFT JOIN tickets dependency ON dependency.id=requested.value
              LEFT JOIN accepted_evidence evidence ON evidence.ticket_id=dependency.id
              WHERE dependency.id IS NULL OR dependency.state!='done' OR evidence.ticket_id IS NULL OR NOT EXISTS (
                  SELECT 1 FROM events e JOIN board_projection_outbox b ON b.ticket_id=e.entity_id AND b.event_id=e.id
                  WHERE e.entity_type='ticket' AND e.entity_id=dependency.id AND e.event_type='state_transition' AND e.to_state='done'
                    AND b.operation='set_state' AND b.acknowledged_at IS NOT NULL))
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage='native_dependency_release')
        ORDER BY t.created_at,t.id LIMIT 1
    """).fetchone()
    if native_release is not None:
        return ProcessNextPreview(next_stage="native_dependency_release", ticket_id=str(native_release["id"]), would_execute=True)

    tranche_checkpoint_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage='tranche_checkpoint' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
        (now,),
    ).fetchone()
    if tranche_checkpoint_replay is not None:
        replay_ticket_id = str(tranche_checkpoint_replay["ticket_id"])
        replay_tranche = ledger.connection.execute("SELECT tranche_id FROM tickets WHERE id=?", (replay_ticket_id,)).fetchone()
        if replay_tranche is None:
            return ProcessNextPreview(next_stage="tranche_checkpoint", ticket_id=replay_ticket_id, would_execute=True)
        try:
            replay_identity = ledger._tranche_checkpoint_identity(ledger.connection, str(replay_tranche["tranche_id"]))
        except RuntimeError:
            return ProcessNextPreview(next_stage="tranche_checkpoint", ticket_id=replay_ticket_id, would_execute=True)
        if not ledger._tranche_checkpoint_h1_conflicts(ledger.connection, replay_identity):
            return ProcessNextPreview(next_stage="tranche_checkpoint", ticket_id=replay_ticket_id, would_execute=True)
    tranche_checkpoint_rows = ledger.connection.execute("""
        SELECT t.id AS ticket_id FROM tranches tr
        JOIN tickets t ON t.tranche_id=tr.id
        JOIN decomposition_plans p ON p.feature_id=tr.feature_id AND p.status='active'
        WHERE tr.status='active'
          AND p.repository_identity IS NOT NULL AND p.repo_base_sha IS NOT NULL
          AND p.repo_snapshot_hash IS NOT NULL AND p.repo_snapshot_manifest_json IS NOT NULL
          AND json_valid(tr.integration_commands_json)=1 AND json_type(tr.integration_commands_json)='array'
          AND NOT EXISTS (SELECT 1 FROM tranche_checkpoint_evidence c WHERE c.tranche_id=tr.id)
          AND NOT EXISTS (SELECT 1 FROM tickets x LEFT JOIN accepted_evidence ae ON ae.ticket_id=x.id WHERE x.tranche_id=tr.id AND (x.state!='done' OR ae.ticket_id IS NULL))
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.stage='tranche_checkpoint' AND json_extract(c.candidate_identity_json,'$.tranche_id')=tr.id)
        ORDER BY tr.feature_id,tr.ordinal,tr.id,t.created_at DESC,t.id DESC
    """).fetchall()
    for tranche_checkpoint in tranche_checkpoint_rows:
        ticket_id = str(tranche_checkpoint["ticket_id"])
        tranche_id_row = ledger.connection.execute("SELECT tranche_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if tranche_id_row is None:
            continue
        try:
            identity = ledger._tranche_checkpoint_identity(ledger.connection, str(tranche_id_row["tranche_id"]))
        except RuntimeError:
            continue
        if not ledger._tranche_checkpoint_h1_conflicts(ledger.connection, identity):
            return ProcessNextPreview(next_stage="tranche_checkpoint", ticket_id=ticket_id, would_execute=True)

    paid_checkpoint_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage='paid_checkpoint' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
        (now,),
    ).fetchone()
    if paid_checkpoint_replay is not None:
        return ProcessNextPreview(next_stage="paid_checkpoint", ticket_id=str(paid_checkpoint_replay["ticket_id"]), would_execute=True)
    paid_checkpoint = ledger.connection.execute("""
        SELECT t.id AS ticket_id FROM tranche_checkpoint_evidence c
        JOIN tickets t ON t.tranche_id=c.tranche_id
        WHERE c.decision='ready_for_checkpoint'
          AND NOT EXISTS (SELECT 1 FROM paid_checkpoint_evidence p WHERE p.tranche_id=c.tranche_id AND p.purpose='integration_checkpoint')
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims s WHERE s.stage='paid_checkpoint' AND json_extract(s.candidate_identity_json,'$.tranche_id')=c.tranche_id)
        ORDER BY c.created_at,c.tranche_id,t.created_at DESC,t.id DESC LIMIT 1
    """).fetchone()
    if paid_checkpoint is not None:
        return ProcessNextPreview(next_stage="paid_checkpoint", ticket_id=str(paid_checkpoint["ticket_id"]), would_execute=True)

    paid_escalation_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage='paid_escalation' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
        (now,),
    ).fetchone()
    if paid_escalation_replay is not None:
        return ProcessNextPreview(next_stage="paid_escalation", ticket_id=str(paid_escalation_replay["ticket_id"]), would_execute=True)
    paid_escalation = ledger.connection.execute("""
        SELECT t.id AS ticket_id FROM paid_checkpoint_evidence p
        JOIN tickets t ON t.tranche_id=p.tranche_id
        WHERE p.purpose='integration_checkpoint' AND p.decision='escalate'
          AND NOT EXISTS (SELECT 1 FROM paid_checkpoint_evidence e WHERE e.tranche_id=p.tranche_id AND e.purpose='escalation')
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims s WHERE s.stage='paid_escalation' AND json_extract(s.candidate_identity_json,'$.tranche_id')=p.tranche_id)
        ORDER BY p.created_at,p.tranche_id,t.created_at DESC,t.id DESC LIMIT 1
    """).fetchone()
    if paid_escalation is not None:
        return ProcessNextPreview(next_stage="paid_escalation", ticket_id=str(paid_escalation["ticket_id"]), would_execute=True)

    next_materialize_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage='next_tranche_materialize' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
        (now,),
    ).fetchone()
    if next_materialize_replay is not None:
        return ProcessNextPreview(next_stage="next_tranche_materialize", ticket_id=str(next_materialize_replay["ticket_id"]), would_execute=True)
    next_materialize = ledger.connection.execute("""
        SELECT t.id AS ticket_id FROM tranches tr
        JOIN tickets t ON t.tranche_id=tr.id
        JOIN tranche_checkpoint_evidence c ON c.tranche_id=tr.id AND c.decision='ready_for_checkpoint'
        JOIN paid_checkpoint_evidence p ON p.tranche_id=tr.id AND p.purpose='integration_checkpoint'
        WHERE tr.status='active'
          AND p.checkpoint_artifact_sha256=c.checkpoint_artifact_sha256
          AND p.checkpoint_completion_hash=c.completion_evidence_hash
          AND (p.decision='approve' OR (p.decision='escalate' AND EXISTS (
              SELECT 1 FROM paid_checkpoint_evidence e
              WHERE e.tranche_id=tr.id AND e.purpose='escalation' AND e.decision='approve'
                AND e.checkpoint_artifact_sha256=c.checkpoint_artifact_sha256
                AND e.checkpoint_completion_hash=c.completion_evidence_hash)))
          AND EXISTS (SELECT 1 FROM tranches nx WHERE nx.feature_id=tr.feature_id AND nx.ordinal=tr.ordinal+1 AND nx.status='planned')
          AND NOT EXISTS (SELECT 1 FROM next_tranche_materializations m WHERE m.predecessor_tranche_id=tr.id)
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims s WHERE s.stage='next_tranche_materialize' AND json_extract(s.candidate_identity_json,'$.predecessor_tranche_id')=tr.id)
        ORDER BY tr.feature_id,tr.ordinal,t.created_at DESC,t.id DESC LIMIT 1
    """).fetchone()
    if next_materialize is not None:
        return ProcessNextPreview(next_stage="next_tranche_materialize", ticket_id=str(next_materialize["ticket_id"]), would_execute=True)

    next_activation_replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage='next_tranche_activation' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
        (now,),
    ).fetchone()
    if next_activation_replay is not None:
        return ProcessNextPreview(next_stage="next_tranche_activation", ticket_id=str(next_activation_replay["ticket_id"]), would_execute=True)
    activation_candidate = ledger.connection.execute("""
        SELECT t.id AS ticket_id FROM next_tranche_materializations m
        JOIN tickets t ON t.tranche_id=m.successor_tranche_id
        WHERE NOT EXISTS (SELECT 1 FROM next_tranche_activation_evidence e WHERE e.successor_tranche_id=m.successor_tranche_id)
          AND NOT EXISTS (
              SELECT 1 FROM tickets x
              WHERE x.tranche_id=m.successor_tranche_id AND NOT EXISTS (
                  SELECT 1 FROM board_projection_outbox b
                  WHERE b.ticket_id=x.id AND b.operation='create_microticket'
                    AND b.acknowledged_at IS NOT NULL AND b.external_task_id IS NOT NULL AND b.superseded_at IS NULL))
          AND NOT EXISTS (
              SELECT 1 FROM tickets x
              WHERE x.tranche_id=m.successor_tranche_id AND json_array_length(x.dependencies_json)>0
                AND NOT EXISTS (SELECT 1 FROM native_dependency_graphs g WHERE g.ticket_id=x.id))
          AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims s WHERE s.stage='next_tranche_activation' AND json_extract(s.candidate_identity_json,'$.successor_tranche_id')=m.successor_tranche_id)
        ORDER BY m.created_at,m.successor_tranche_id,t.created_at DESC,t.id DESC LIMIT 1
    """).fetchone()
    if activation_candidate is not None:
        return ProcessNextPreview(next_stage="next_tranche_activation", ticket_id=str(activation_candidate["ticket_id"]), would_execute=True)

    replay = ledger.connection.execute(
        "SELECT ticket_id FROM scheduler_stage_claims WHERE stage='dependency_readiness' AND status='claimed' "
        "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
    ).fetchone()
    if replay is not None:
        return ProcessNextPreview(next_stage="dependency_readiness", ticket_id=str(replay["ticket_id"]))
    generated_activation = ledger.connection.execute("""
        SELECT DISTINCT t.id FROM tickets t
        JOIN board_projection_outbox b ON b.ticket_id=t.id AND b.operation='create_microticket'
        WHERE b.acknowledged_at IS NOT NULL AND b.external_task_id IS NOT NULL AND b.superseded_at IS NULL
          AND t.state='draft'
          AND NOT EXISTS (SELECT 1 FROM runtime_bindings rb WHERE rb.ticket_id=t.id)
        ORDER BY t.created_at,t.id LIMIT 1
    """).fetchone()
    if generated_activation is not None:
        return ProcessNextPreview(next_stage="dependency_readiness", ticket_id=str(generated_activation["id"]), would_execute=True)
    readiness = ledger.connection.execute("""
        SELECT t.id FROM tickets t JOIN runtime_bindings rb ON rb.ticket_id=t.id
        WHERE t.state=? AND json_valid(t.dependencies_json)=1 AND json_type(t.dependencies_json)='array'
          AND json_array_length(t.dependencies_json)=0
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


def preview_database(database: Path, *, now: int | None = None, operator_config: Any | None = None, board: Any | None = None) -> ProcessNextPreview:
    """Preview an existing migrated ledger through a strictly read-only handle."""
    try:
        path = Path(database).expanduser().resolve(strict=True)
    except FileNotFoundError:
        return ProcessNextPreview(next_stage="no_work")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        readonly_ledger = object.__new__(Ledger)
        readonly_ledger.connection = connection
        if operator_config is None:
            signer_public_key = signer_fingerprint = None
        else:
            try:
                signer_public_key = operator_config.signer_public_key_bytes
                signer_fingerprint = operator_config.operator_signing_key_fingerprint
            except (TypeError, ValueError):
                signer_public_key = signer_fingerprint = None
        return preview_next(readonly_ledger, now=now, signer_public_key=signer_public_key, signer_fingerprint=signer_fingerprint, board=board)
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
        hermes_execution_runner: Callable[[], dict[str, Any] | None] | None = None,
        generated_activation_runner: Callable[[], dict[str, Any] | None] | None = None,
        native_dependency_release_prepare_runner: Callable[[str, str], dict[str, Any]] | None = None,
        native_dependency_release_profile: str | None = None,
        native_dependency_release_repository: str | None = None,
        native_dependency_release_signer_public_key: bytes | None = None,
        native_dependency_release_signer_fingerprint: str | None = None,
        implementation_runner: Callable[[str], dict[str, Any]] | None = None,
        validation_runner: Callable[[str], dict[str, Any]] | None = None,
        review_runner: Callable[[str], dict[str, Any]] | None = None,
        review_execution_policy_hash: str | None = None,
        triage_runner: Callable[[str], dict[str, Any]] | None = None,
        triage_execution_policy_hash: str | None = None,
        acceptance_runner: Callable[[str], dict[str, Any]] | None = None,
        git_integration_runner: Callable[[str], dict[str, Any]] | None = None,
        tranche_checkpoint_runner: Callable[[str], dict[str, Any]] | None = None,
        paid_checkpoint_runner: Callable[[str], dict[str, Any]] | None = None,
        paid_checkpoint_route: tuple[str, str, str] | None = None,
        paid_escalation_runner: Callable[[str], dict[str, Any]] | None = None,
        paid_escalation_route: tuple[str, str, str] | None = None,
        next_tranche_materialize_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
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
        self.hermes_execution_runner = hermes_execution_runner
        self.generated_activation_runner = generated_activation_runner
        self.native_dependency_release_prepare_runner = native_dependency_release_prepare_runner
        self.native_dependency_release_profile = native_dependency_release_profile
        self.native_dependency_release_repository = native_dependency_release_repository
        self.native_dependency_release_signer_public_key = native_dependency_release_signer_public_key
        self.native_dependency_release_signer_fingerprint = native_dependency_release_signer_fingerprint
        self.implementation_runner = implementation_runner
        self.validation_runner = validation_runner
        self.review_runner = review_runner
        self.review_execution_policy_hash = review_execution_policy_hash
        self.triage_runner = triage_runner
        self.triage_execution_policy_hash = triage_execution_policy_hash
        self.acceptance_runner = acceptance_runner
        self.git_integration_runner = git_integration_runner
        self.tranche_checkpoint_runner = tranche_checkpoint_runner
        self.paid_checkpoint_runner = paid_checkpoint_runner
        self.paid_checkpoint_route = paid_checkpoint_route
        self.paid_escalation_runner = paid_escalation_runner
        self.paid_escalation_route = paid_escalation_route
        self.next_tranche_materialize_runner = next_tranche_materialize_runner
        if self.review_runner is not None and not self.review_execution_policy_hash:
            raise ValueError("process-next review runner requires a review execution policy hash")
        if self.triage_runner is not None and not self.triage_execution_policy_hash:
            raise ValueError("process-next triage runner requires a triage execution policy hash")
        if (self.paid_checkpoint_runner is None) != (self.paid_checkpoint_route is None):
            raise ValueError("process-next paid checkpoint runner and route must be configured together")
        if (self.paid_escalation_runner is None) != (self.paid_escalation_route is None):
            raise ValueError("process-next paid escalation runner and route must be configured together")

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
        def verify_current_native_release(ticket_id: str) -> None:
            row = self.ledger.connection.execute("SELECT * FROM native_dependency_release_revalidations WHERE ticket_id=?", (ticket_id,)).fetchone()
            if row is None:
                raise RuntimeError("native_dependency_release_reconciliation_required: revalidation evidence missing")
            snapshot = self.board.execution_snapshot(str(row["external_task_id"]))
            if snapshot.task.id != str(row["external_task_id"]) or canonical_sha256(asdict(snapshot)) != str(row["snapshot_hash"]):
                raise RuntimeError("native_dependency_release_reconciliation_required: current external snapshot drift")
            if snapshot.task.status not in {"scheduled", "blocked"} or any(value is not None for value in (snapshot.session_id, snapshot.started_at, snapshot.completed_at)):
                raise RuntimeError("native_dependency_release_reconciliation_required: current external task is unsafe")
            if snapshot.task.assignee != str(row["implementation_profile"]) or snapshot.task.workspace_kind != "worktree" or str(Path(str(snapshot.task.workspace_path)).expanduser().resolve()) != str(row["canonical_worktree_path"]):
                raise RuntimeError("native_dependency_release_reconciliation_required: current external routing drift")
            if (snapshot.branch_name or "") != str(row["branch"]) or (snapshot.base_sha or snapshot.task.base_sha) != str(row["base_sha"]) or (snapshot.repository_identity or snapshot.task.repository_identity) != str(row["repository_identity"]):
                raise RuntimeError("native_dependency_release_reconciliation_required: current external authority drift")
            forbidden = {"running", "completed", "success", "successful"}
            gate = "Local First execution gate: authoritative dependencies/runtime authorization not satisfied"
            for run in snapshot.runs:
                if run.status in forbidden or (run.outcome or "").lower() in forbidden or run.worker_pid is not None or run.profile is not None or run.metadata is not None:
                    raise RuntimeError("native_dependency_release_reconciliation_required: current external execution evidence exists")
                if run.status == "blocked" and not (run.outcome == "blocked" and str(run.summary or "") == gate and (run.started_at, run.ended_at) in {(None, None), (run.started_at, run.started_at)}):
                    raise RuntimeError("native_dependency_release_reconciliation_required: current external run is unsafe")
                if run.status == "spawn_failed" and run.outcome not in {None, "spawn_failed"}:
                    raise RuntimeError("native_dependency_release_reconciliation_required: current external run is unsafe")
                if run.status not in {"blocked", "spawn_failed"}:
                    raise RuntimeError("native_dependency_release_reconciliation_required: current external run is ambiguous")

        trusted_preview = preview_next(self.ledger, now=now, signer_public_key=self.native_dependency_release_signer_public_key, signer_fingerprint=self.native_dependency_release_signer_fingerprint, board=self.board)
        if trusted_preview.next_stage == "reconciliation_required":
            raise RuntimeError("native_dependency_release_reconciliation_required: trusted current board proof unavailable")
        migration = self.ledger.native_dependency_release_migration_required(signer_public_key=self.native_dependency_release_signer_public_key, signer_fingerprint=self.native_dependency_release_signer_fingerprint)
        if migration is not None:
            raise RuntimeError(
                "native_dependency_release_reconciliation_required: legacy release routing authority requires paused operator revalidation"
            )
        reconciliation = self.ledger.next_scheduler_reconciliation(now=now)
        if reconciliation is not None and reconciliation.action == ReconciliationAction.STOP:
            family = self.ledger._scheduler_stage_family(reconciliation.stage)
            if family in {"paid_checkpoint", "paid_escalation"}:
                raise PaidInvocationError(f"paid invocation unknown or terminal outcome requires reconciliation: {reconciliation.reason}")
            prefix = {
                "implementation": "execution_reconciliation_required",
                "review": "review_reconciliation_required",
                "triage": "triage_reconciliation_required",
            }.get(family, f"{family}_reconciliation_required")
            raise RuntimeError(f"{prefix}: {reconciliation.reason}")

        recovery_family = None if reconciliation is None else self.ledger._scheduler_stage_family(reconciliation.stage)

        def stage_allowed(stage: str) -> bool:
            return recovery_family is None or recovery_family == stage

        generated = GeneratedProjectionWorker(
            self.ledger, self.board, worker_id=execution_owner, clock=lambda: now
        ).deliver_one()
        if generated.status != "no_work":
            return ProcessNextResult(generated.status, "generated_projection", generated.ticket_id)

        state = StateProjectionWorker(
            self.ledger, self.board, worker_id=execution_owner, lease_seconds=self.lease_seconds
        ).deliver_one(now=now)
        if state.status != "no_work":
            return ProcessNextResult(state.status, "state_projection", state.ticket_id)

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

        if stage_allowed("implementation") and self.hermes_execution_runner is not None:
            external_result = self.hermes_execution_runner()
            if external_result is not None:
                return ProcessNextResult(
                    "reconciled_external",
                    "implementation",
                    str(external_result["ticket_id"]),
                )

        if stage_allowed("implementation") and self.implementation_runner is not None:
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


        if stage_allowed("validation") and self.validation_runner is not None:
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

        if stage_allowed("review") and self.review_runner is not None:
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
        ) if stage_allowed("repair_routing") else None
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

        if stage_allowed("triage") and self.triage_runner is not None:
            triage_claim = self.ledger.claim_next_scheduler_triage(
                execution_owner,
                lease_seconds=self.lease_seconds,
                triage_execution_policy_hash=str(self.triage_execution_policy_hash),
                now=now,
            )
            if triage_claim is not None:
                claim_id = str(triage_claim["claim_id"])
                ticket_id = str(triage_claim["ticket_id"])
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    triage_result = json.loads(str(current["result_json"]))
                else:
                    if current.get("side_effect_started_at") is not None:
                        try:
                            identity = json.loads(str(current.get("candidate_identity_json") or ""))
                            attempt_number = int(identity["attempt_number"])
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                            raise RuntimeError("triage_reconciliation_required: triage claim identity is malformed") from exc
                        model_stage = self.ledger.model_stage(ticket_id, attempt_number, "triage")
                        invocations = self.ledger.connection.execute(
                            "SELECT * FROM model_invocations WHERE ticket_id=? AND attempt_number=? AND stage='triage' ORDER BY started_at,invocation_id",
                            (ticket_id, attempt_number),
                        ).fetchall()
                        if model_stage is None and invocations and str(invocations[-1]["status"]) != "completed":
                            raise RuntimeError("triage_reconciliation_required: started triage outcome is unknown")
                    else:
                        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                    triage_result = self.triage_runner(ticket_id)
                    self.ledger.complete_scheduler_triage_effect(
                        claim_id, execution_owner, triage_result, now=now
                    )
                self.ledger.complete_scheduler_claim(
                    claim_id, execution_owner, triage_result, now=now
                )
                return ProcessNextResult("completed", "triage", ticket_id, claim_id)

        if stage_allowed("acceptance") and self.acceptance_runner is not None:
            acceptance_claim = self.ledger.claim_next_scheduler_acceptance(
                execution_owner, lease_seconds=self.lease_seconds, now=now
            )
            if acceptance_claim is not None:
                claim_id = str(acceptance_claim["claim_id"])
                ticket_id = str(acceptance_claim["ticket_id"])
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    acceptance_result = json.loads(str(current["result_json"]))
                else:
                    if current.get("side_effect_started_at") is None:
                        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                    inspected = self.acceptance_runner(ticket_id)
                    current = self.ledger.apply_scheduler_acceptance_effect(
                        claim_id,
                        execution_owner,
                        current_diff_hash=str(inspected["current_diff_hash"]),
                        now=now,
                    )
                    acceptance_result = json.loads(str(current["result_json"]))
                self.ledger.complete_scheduler_claim(
                    claim_id, execution_owner, acceptance_result, now=now
                )
                return ProcessNextResult("completed", "acceptance", ticket_id, claim_id)

        if stage_allowed("git_integration") and self.git_integration_runner is not None:
            git_claim = self.ledger.claim_next_scheduler_git_integration(
                execution_owner, lease_seconds=self.lease_seconds, now=now
            )
            if git_claim is not None:
                claim_id = str(git_claim["claim_id"])
                ticket_id = str(git_claim["ticket_id"])
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    git_result = json.loads(str(current["result_json"]))
                else:
                    if current.get("side_effect_started_at") is None:
                        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                    git_result = self.git_integration_runner(ticket_id)
                    current = self.ledger.apply_scheduler_git_integration_effect(
                        claim_id, execution_owner, git_result, now=now
                    )
                    git_result = json.loads(str(current["result_json"]))
                self.ledger.complete_scheduler_claim(
                    claim_id, execution_owner, git_result, now=now
                )
                return ProcessNextResult("completed", "git_integration", ticket_id, claim_id)

        completion_claim = self.ledger.claim_next_scheduler_completion(
            execution_owner, lease_seconds=self.lease_seconds, now=now
        ) if stage_allowed("completion") else None
        if completion_claim is not None:
            claim_id = str(completion_claim["claim_id"])
            ticket_id = str(completion_claim["ticket_id"])
            current = self.ledger.scheduler_claim(claim_id)
            if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                completion_result = json.loads(str(current["result_json"]))
            else:
                if current.get("side_effect_started_at") is None:
                    self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                current = self.ledger.apply_scheduler_completion_effect(
                    claim_id, execution_owner, now=now
                )
                completion_result = json.loads(str(current["result_json"]))
            self.ledger.complete_scheduler_claim(
                claim_id, execution_owner, completion_result, now=now
            )
            try:
                RuntimeMetricsStore(self.ledger).materialize_completed(limit=100)
            except Exception:
                # Metrics are derived observability only; completion authority must never depend on them.
                pass
            return ProcessNextResult("completed", "completion", ticket_id, claim_id)

        if (stage_allowed("native_dependency_graph") or stage_allowed("native_dependency_release")) and hasattr(self.board, "get_task") and hasattr(self.board, "link_dependency"):
            graph_claim = self.ledger.claim_next_scheduler_native_dependency_graph(
                execution_owner, lease_seconds=self.lease_seconds, now=now
            ) if stage_allowed("native_dependency_graph") else None
            if graph_claim is not None:
                claim_id = str(graph_claim["claim_id"])
                ticket_id = str(graph_claim["ticket_id"])
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    graph_result = json.loads(str(current["result_json"]))
                else:
                    if current.get("side_effect_started_at") is None:
                        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                    identity = json.loads(str(current["candidate_identity_json"]))
                    child_external_id = str(identity["child_external_id"])
                    expected_parents = sorted(str(row["external_task_id"]) for row in identity["parents"])
                    task = self.board.get_task(child_external_id)
                    actual_parents = sorted(set(getattr(task, "parents", ())))
                    extras = sorted(set(actual_parents) - set(expected_parents))
                    if extras:
                        raise RuntimeError("native_dependency_graph_reconciliation_required: Hermes graph has unexpected parents")
                    for parent_external_id in sorted(set(expected_parents) - set(actual_parents)):
                        self.board.link_dependency(parent_external_id, child_external_id)
                    task = self.board.get_task(child_external_id)
                    actual_parents = sorted(set(getattr(task, "parents", ())))
                    if actual_parents != expected_parents:
                        raise RuntimeError("native_dependency_graph_reconciliation_required: Hermes graph did not converge")
                    if expected_parents and HANDOFF_MARKER in str(getattr(task, "body", "")):
                        parker = getattr(self.board, "park_native_dependency_child", None)
                        if parker is None:
                            raise RuntimeError("native_dependency_graph_reconciliation_required: native park adapter is missing")
                        try:
                            parker(child_external_id, idempotency_key=f"native-graph-park:{identity['graph_hash']}")
                        except Exception:
                            # A transport error may have happened before or after the
                            # remote block. Read back, then retry the same idempotent
                            # park only when the child is still dispatchable.
                            task_after_failure = self.board.get_task(child_external_id)
                            if str(getattr(task_after_failure, "status", "")) != "blocked":
                                try:
                                    parker(child_external_id, idempotency_key=f"native-graph-park:{identity['graph_hash']}")
                                except Exception as retry_exc:
                                    raise RuntimeError("native_dependency_graph_reconciliation_required: child park outcome is unknown") from retry_exc
                            else:
                                task = task_after_failure
                        task = self.board.get_task(child_external_id)
                        if str(getattr(task, "status", "")) != "blocked":
                            raise RuntimeError("native_dependency_graph_reconciliation_required: child is not durably parked")
                    graph_result = {
                        "ticket_id": ticket_id,
                        "candidate_identity": identity,
                        "actual_parent_external_ids": actual_parents,
                        "hermes_status": str(getattr(task, "status", "")),
                    }
                    current = self.ledger.apply_scheduler_native_dependency_graph_effect(
                        claim_id, execution_owner, graph_result, now=now
                    )
                    graph_result = json.loads(str(current["result_json"]))
                self.ledger.complete_scheduler_claim(
                    claim_id, execution_owner, graph_result, now=now
                )
                return ProcessNextResult("completed", "native_dependency_graph", ticket_id, claim_id)

            release_claim = self.ledger.claim_next_scheduler_native_dependency_release(
                execution_owner, lease_seconds=self.lease_seconds, now=now,
                implementation_profile=self.native_dependency_release_profile,
                canonical_repository=self.native_dependency_release_repository,
            ) if stage_allowed("native_dependency_release") else None
            if release_claim is not None:
                claim_id = str(release_claim["claim_id"])
                ticket_id = str(release_claim["ticket_id"])
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    release_result = json.loads(str(current["result_json"]))
                else:
                    if current.get("side_effect_started_at") is None:
                        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                    identity = json.loads(str(current["candidate_identity_json"]))
                    graph = self.ledger.native_dependency_graph(ticket_id)
                    expected_parents = sorted(json.loads(str(graph["parent_external_ids_json"])))
                    child_external_id = str(identity["child_external_id"])
                    prepared: dict[str, Any] = {}
                    if self.native_dependency_release_prepare_runner is not None:
                        prepared = self.native_dependency_release_prepare_runner(ticket_id, child_external_id)
                    task = self.board.get_task(child_external_id)
                    if self.native_dependency_release_profile is not None:
                        prepared_workspace = str(prepared.get("workspace_path") or "")
                        if not prepared_workspace:
                            raise RuntimeError("native_dependency_release_reconciliation_required: prepared workspace is missing")
                        verifier = getattr(self.board, "verify_native_release_task", None)
                        if verifier is None:
                            raise RuntimeError("native_dependency_release_reconciliation_required: board authority verifier is missing")
                        routing = verifier(task, expected_workspace_path=prepared_workspace)
                        routing["canonical_repository"] = str(self.native_dependency_release_repository)
                    else:
                        routing = {}
                    actual_parents = sorted(set(getattr(task, "parents", ())))
                    if actual_parents != expected_parents:
                        raise RuntimeError("native_dependency_release_reconciliation_required: Hermes graph diverged")
                    if HANDOFF_MARKER in str(getattr(task, "body", "")) and str(getattr(task, "status", "")) == "blocked":
                        if self.native_dependency_release_profile is not None:
                            self.board.set_state(child_external_id, CanonicalState.READY_LOCAL, idempotency_key=f"native-release:{identity['parent_completion_hash']}", expected_routing=routing)
                        else:
                            self.board.set_state(child_external_id, CanonicalState.READY_LOCAL, idempotency_key=f"native-release:{identity['parent_completion_hash']}")
                        task = self.board.get_task(child_external_id)
                    hermes_status = str(getattr(task, "status", ""))
                    if hermes_status != "ready":
                        raise RuntimeError("native_dependency_release_reconciliation_required: Hermes did not expose child as ready")
                    release_result = {
                        "ticket_id": ticket_id,
                        "candidate_identity": identity,
                        "actual_parent_external_ids": actual_parents,
                        "hermes_status": hermes_status,
                        "prepared_execution": prepared,
                        "routing_authority": routing,
                    }
                    current = self.ledger.apply_scheduler_native_dependency_release_effect(
                        claim_id, execution_owner, release_result, now=now,
                        implementation_profile=self.native_dependency_release_profile,
                        canonical_repository=self.native_dependency_release_repository,
                    )
                    release_result = json.loads(str(current["result_json"]))
                self.ledger.complete_scheduler_claim(
                    claim_id, execution_owner, release_result, now=now
                )
                return ProcessNextResult("completed", "native_dependency_release", ticket_id, claim_id)

        if stage_allowed("tranche_checkpoint") and self.tranche_checkpoint_runner is not None:
            checkpoint_claim = self.ledger.claim_next_scheduler_tranche_checkpoint(
                execution_owner, lease_seconds=self.lease_seconds, now=now
            )
            if checkpoint_claim is not None:
                claim_id = str(checkpoint_claim["claim_id"])
                ticket_id = str(checkpoint_claim["ticket_id"])
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    checkpoint_result = json.loads(str(current["result_json"]))
                else:
                    if current.get("side_effect_started_at") is None:
                        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                    identity = json.loads(str(current["candidate_identity_json"]))
                    checkpoint_result = self.tranche_checkpoint_runner(str(identity["tranche_id"]))
                    current = self.ledger.apply_scheduler_tranche_checkpoint_effect(
                        claim_id, execution_owner, checkpoint_result, now=now
                    )
                    checkpoint_result = json.loads(str(current["result_json"]))
                self.ledger.complete_scheduler_claim(
                    claim_id, execution_owner, checkpoint_result, now=now
                )
                return ProcessNextResult("completed", "tranche_checkpoint", ticket_id, claim_id)

        for stage, purpose, runner, route in (
            ("paid_checkpoint", "integration_checkpoint", self.paid_checkpoint_runner, self.paid_checkpoint_route),
            ("paid_escalation", "escalation", self.paid_escalation_runner, self.paid_escalation_route),
        ):
            if not stage_allowed(stage):
                continue
            if runner is None or route is None:
                continue
            provider, model, profile = route
            paid_claim = self.ledger.claim_next_scheduler_paid_stage(
                execution_owner,
                lease_seconds=self.lease_seconds,
                purpose=purpose,
                provider=provider,
                model=model,
                profile=profile,
                now=now,
            )
            if paid_claim is None:
                continue
            claim_id = str(paid_claim["claim_id"])
            ticket_id = str(paid_claim["ticket_id"])
            current = self.ledger.scheduler_claim(claim_id)
            if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                paid_result = json.loads(str(current["result_json"]))
            else:
                if current.get("side_effect_started_at") is None:
                    self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                identity = json.loads(str(current["candidate_identity_json"]))
                paid_result = runner(str(identity["tranche_id"]))
                current = self.ledger.apply_scheduler_paid_stage_effect(claim_id, execution_owner, paid_result, now=now)
                paid_result = json.loads(str(current["result_json"]))
            self.ledger.complete_scheduler_claim(claim_id, execution_owner, paid_result, now=now)
            return ProcessNextResult("completed", stage, ticket_id, claim_id)

        if stage_allowed("next_tranche_materialize") and self.next_tranche_materialize_runner is not None:
            materialize_claim = self.ledger.claim_next_scheduler_next_tranche_materialize(
                execution_owner, lease_seconds=self.lease_seconds, now=now
            )
            if materialize_claim is not None:
                claim_id = str(materialize_claim["claim_id"])
                ticket_id = str(materialize_claim["ticket_id"])
                current = self.ledger.scheduler_claim(claim_id)
                if current.get("side_effect_completed_at") is not None and current.get("result_json"):
                    materialize_result = json.loads(str(current["result_json"]))
                else:
                    if current.get("side_effect_started_at") is None:
                        self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                    identity = json.loads(str(current["candidate_identity_json"]))
                    materialize_result = self.next_tranche_materialize_runner(identity)
                    current = self.ledger.apply_scheduler_next_tranche_materialize_effect(
                        claim_id, execution_owner, materialize_result, now=now
                    )
                    materialize_result = json.loads(str(current["result_json"]))
                self.ledger.complete_scheduler_claim(claim_id, execution_owner, materialize_result, now=now)
                return ProcessNextResult("completed", "next_tranche_materialize", ticket_id, claim_id)

        activation_claim = self.ledger.claim_next_scheduler_next_tranche_activation(
            execution_owner, lease_seconds=self.lease_seconds, now=now
        ) if stage_allowed("next_tranche_activation") else None
        if activation_claim is not None:
            claim_id = str(activation_claim["claim_id"])
            ticket_id = str(activation_claim["ticket_id"])
            current = self.ledger.scheduler_claim(claim_id)
            if current.get("side_effect_completed_at") is None:
                if current.get("side_effect_started_at") is None:
                    self.ledger.begin_scheduler_claim_effect(claim_id, execution_owner, now=now)
                current = self.ledger.apply_scheduler_next_tranche_activation_effect(claim_id, execution_owner, now=now)
            activation_result = json.loads(str(current["result_json"]))
            self.ledger.complete_scheduler_claim(claim_id, execution_owner, activation_result, now=now)
            return ProcessNextResult("completed", "next_tranche_activation", ticket_id, claim_id)

        if stage_allowed("dependency_readiness") and self.generated_activation_runner is not None:
            activation = self.generated_activation_runner()
            if activation is not None:
                return ProcessNextResult(
                    str(activation.get("status") or "completed"),
                    "dependency_readiness",
                    str(activation["ticket_id"]),
                )

        claim = self.ledger.claim_next_scheduler_readiness(
            execution_owner, lease_seconds=self.lease_seconds, now=now
        ) if stage_allowed("dependency_readiness") else None
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
