from __future__ import annotations

import hashlib
import base64
import binascii
import json
from cryptography.exceptions import InvalidSignature
import re
import sqlite3
import subprocess
import time
import uuid
from threading import RLock
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .adapters import BoardAdapter
from .readiness import ReadinessError, validate_ticket
from .states import CanonicalState, validate_transition
from .evidence_hash import canonical_sha256
from .historical_revalidation import authorization_hash, authorization_identity, authorization_hash_from_row, attestation_hash, attestation_identity, attestation_hash_from_row, historical_validation_hash, historical_validation_identity, historical_validation_result_hash
from .ticket import MicroTicket
from .revalidation_boundary import validate_and_consume_revalidation_capability
from .native_release_approval import snapshot_authority

class _LegacyRevalidationCompatibility(Exception):
    pass

def _stable_scheduler_failure_fingerprint(ticket_id: str, stage: str, evidence: str) -> str:
    """Hash stable failure identity while excluding volatile locations and timestamps."""
    normalized = evidence.lower()
    normalized = re.sub(r"\b(?:line|ln)\s*\d+\b", "", normalized)
    normalized = re.sub(r"\b\d{4}-\d\d-\d\d(?:[t ]\d\d:\d\d:\d\d(?:\.\d+)?z?)?\b", "", normalized)
    normalized = re.sub(r"(?:[a-z]:)?/(?:[^\s:]+/)+[^\s:]+", "<path>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" :,-")
    return hashlib.sha256("\x1f".join((ticket_id, stage, normalized)).encode()).hexdigest()


def _is_json_int(value: object) -> bool:
    return type(value) is int


def _is_json_number(value: object) -> bool:
    return type(value) in (int, float)


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(type(item) is str for item in value)


def _is_command_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for command in value:
        if type(command) is not dict:
            return False
        if not isinstance(command.get("argv"), list) or not all(type(argument) is str for argument in command["argv"]):
            return False
        if not _is_json_int(command.get("returncode")) or not _is_json_number(command.get("duration_seconds")):
            return False
        if type(command.get("stdout_summary")) is not str or type(command.get("stderr_summary")) is not str or type(command.get("truncated")) is not bool:
            return False
    return True


def _sqlite_historical_authorization_hash(ticket_id: str, attempt_number: int, base_sha: str, repository_identity: str, target_file: str, failure_classification: str, failure_evidence_identity: str, implementation_invocation_id: str, operator_id: str, reason: str) -> str:
    return authorization_hash(authorization_identity(ticket_id=ticket_id, attempt_number=attempt_number, base_sha=base_sha, repository_identity=repository_identity, target_file=target_file, failure_classification=failure_classification, failure_evidence_identity=failure_evidence_identity, implementation_invocation_id=implementation_invocation_id, operator_id=operator_id, reason=reason))


def _sqlite_historical_attestation_hash(ticket_id: str, attempt_number: int, base_sha: str, repository_identity: str, implementation_invocation_id: str, implementation_artifact: str, implementation_diff_hash: str, worktree_path: str, worktree_diff_hash: str, authorization_hash_value: str, operator_id: str) -> str:
    return attestation_hash(attestation_identity(ticket_id=ticket_id, attempt_number=attempt_number, base_sha=base_sha, repository_identity=repository_identity, implementation_invocation_id=implementation_invocation_id, implementation_artifact=implementation_artifact, implementation_diff_hash=implementation_diff_hash, worktree_path=worktree_path, worktree_diff_hash=worktree_diff_hash, authorization_hash=authorization_hash_value, operator_id=operator_id))


def _sqlite_historical_validation_hash(ticket_id: str, attempt_number: int, authorization_hash_value: str, attestation_hash_value: str, base_sha: str, implementation_diff_hash: str, validation_profile_hash: str) -> str:
    return historical_validation_hash(historical_validation_identity(ticket_id=ticket_id, attempt_number=attempt_number, authorization_hash=authorization_hash_value, attestation_hash=attestation_hash_value, base_sha=base_sha, implementation_diff_hash=implementation_diff_hash, validation_profile_hash=validation_profile_hash))


def _sqlite_historical_validation_result_hash(ticket_id: str, attempt_number: int, authorization_hash_value: str, attestation_hash_value: str, base_sha: str, implementation_diff_hash: str, validation_profile_hash: str, artifact_sha256: str, passed: int, compact_evidence: str) -> str:
    return historical_validation_result_hash(ticket_id=ticket_id, attempt_number=attempt_number, authorization_hash=authorization_hash_value, attestation_hash=attestation_hash_value, base_sha=base_sha, implementation_diff_hash=implementation_diff_hash, validation_profile_hash=validation_profile_hash, artifact_sha256=artifact_sha256, passed=bool(passed), compact_evidence=compact_evidence)


@dataclass(frozen=True)
class TicketReadinessResult:
    status: str
    unresolved_dependency_ids: tuple[str, ...] = ()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS controller_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_tick_lease (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    lease_owner TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    lease_expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_stage_claims (
    claim_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('claimed', 'completed', 'failed')),
    lease_owner TEXT,
    lease_expires_at INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    result_json TEXT,
    last_error TEXT,
    side_effect_started_at INTEGER,
    side_effect_completed_at INTEGER,
    finalized_at INTEGER,
    candidate_identity_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(ticket_id, stage)
);
CREATE TABLE IF NOT EXISTS features (
    id TEXT PRIMARY KEY,
    external_id TEXT UNIQUE,
    title TEXT NOT NULL,
    objective TEXT,
    status TEXT NOT NULL,
    risk TEXT,
    architecture_version INTEGER,
    integration_base_sha TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tranches (
    id TEXT PRIMARY KEY,
    feature_id TEXT NOT NULL REFERENCES features(id),
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    base_sha TEXT,
    integration_commands_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(feature_id, ordinal)
);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    external_id TEXT UNIQUE,
    feature_id TEXT REFERENCES features(id),
    tranche_id TEXT REFERENCES tranches(id),
    parent_ticket_id TEXT REFERENCES tickets(id),
    depth INTEGER NOT NULL DEFAULT 0 CHECK (depth >= 0),
    title TEXT NOT NULL,
    objective TEXT,
    state TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_claimable ON tickets(state, lease_expires_at, created_at);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    outcome TEXT,
    failure_fingerprint TEXT,
    base_sha TEXT,
    branch TEXT,
    worktree_path TEXT,
    pre_diff_hash TEXT,
    post_diff_hash TEXT,
    accepted_commit_sha TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE(ticket_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    stage TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    UNIQUE(ticket_id, attempt_number, stage)
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_id INTEGER REFERENCES attempts(id),
    fingerprint TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    UNIQUE(ticket_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS review_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(ticket_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS review_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_findings_fingerprint ON review_findings(ticket_id, fingerprint);
CREATE TABLE IF NOT EXISTS criterion_statuses (
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    criterion_id TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(ticket_id, criterion_id)
);
CREATE TABLE IF NOT EXISTS triage_children (
    parent_ticket_id TEXT NOT NULL REFERENCES tickets(id),
    child_ticket_id TEXT NOT NULL REFERENCES tickets(id),
    fingerprint TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(parent_ticket_id, child_ticket_id),
    UNIQUE(parent_ticket_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id, id);
CREATE TABLE IF NOT EXISTS board_projections (
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    event_id INTEGER NOT NULL REFERENCES events(id),
    state TEXT NOT NULL,
    projected_at INTEGER NOT NULL,
    PRIMARY KEY(ticket_id, event_id)
);
CREATE TABLE IF NOT EXISTS board_projection_outbox (
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    event_id INTEGER NOT NULL REFERENCES events(id),
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    queued_at INTEGER NOT NULL,
    acknowledged_at INTEGER,
    superseded_at INTEGER,
    superseded_by_event_id INTEGER REFERENCES events(id),
    supersession_reason TEXT,
    PRIMARY KEY(ticket_id, event_id)
);
CREATE TABLE IF NOT EXISTS generated_projection_recoveries (
    recovery_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    superseded_event_id INTEGER NOT NULL REFERENCES events(id),
    superseded_external_task_id TEXT NOT NULL,
    superseded_idempotency_key TEXT NOT NULL,
    replacement_event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
    replacement_idempotency_key TEXT NOT NULL UNIQUE,
    operator_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    observed_status TEXT NOT NULL CHECK(observed_status IN ('done','blocked')),
    observed_snapshot_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(ticket_id, superseded_event_id),
    UNIQUE(ticket_id, replacement_event_id)
);
CREATE TRIGGER IF NOT EXISTS generated_projection_recoveries_immutable_update
BEFORE UPDATE ON generated_projection_recoveries BEGIN SELECT RAISE(ABORT, 'generated projection recoveries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS generated_projection_recoveries_immutable_delete
BEFORE DELETE ON generated_projection_recoveries BEGIN SELECT RAISE(ABORT, 'generated projection recoveries are append-only'); END;
CREATE TABLE IF NOT EXISTS acceptance_criteria (
    id TEXT NOT NULL, feature_id TEXT NOT NULL REFERENCES features(id),
    statement TEXT NOT NULL, verification TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    PRIMARY KEY(feature_id, id)
);
CREATE TABLE IF NOT EXISTS architecture_packets (
    feature_id TEXT PRIMARY KEY REFERENCES features(id), version INTEGER NOT NULL,
    snapshot_sha TEXT NOT NULL, packet_json TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paid_budgets (
    feature_id TEXT PRIMARY KEY, architecture_limit INTEGER NOT NULL, checkpoint_limit INTEGER NOT NULL,
    escalation_limit INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paid_reservations (
    id TEXT PRIMARY KEY, feature_id TEXT NOT NULL, purpose TEXT NOT NULL, request_key TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    UNIQUE(feature_id, purpose, request_key)
);
CREATE TABLE IF NOT EXISTS paid_approvals (
    id TEXT PRIMARY KEY, feature_id TEXT NOT NULL, purpose TEXT NOT NULL, calls INTEGER NOT NULL CHECK(calls > 0),
    actor_id TEXT NOT NULL, reason TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY, feature_id TEXT NOT NULL, purpose TEXT NOT NULL,
    reservation_id TEXT NOT NULL REFERENCES paid_reservations(id), status TEXT NOT NULL,
    request_artifact_json TEXT NOT NULL, response_artifact_json TEXT, input_tokens INTEGER, output_tokens INTEGER,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    UNIQUE(reservation_id)
);
CREATE TABLE IF NOT EXISTS runtime_bindings (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id), repository_path TEXT NOT NULL,
    starting_sha TEXT NOT NULL, ownership_verified INTEGER NOT NULL, created_at INTEGER NOT NULL,
    operator_signer_fingerprint TEXT, operator_authority_hash TEXT
);
CREATE TRIGGER IF NOT EXISTS runtime_bindings_signer_pair_required
BEFORE UPDATE ON runtime_bindings
WHEN (NEW.operator_signer_fingerprint IS NULL) != (NEW.operator_authority_hash IS NULL)
BEGIN SELECT RAISE(ABORT, 'runtime binding signer fields must be paired'); END;
CREATE TRIGGER IF NOT EXISTS runtime_bindings_signer_immutable
BEFORE UPDATE ON runtime_bindings
WHEN (OLD.operator_signer_fingerprint IS NOT NULL OR OLD.operator_authority_hash IS NOT NULL)
 AND (OLD.operator_signer_fingerprint IS NOT NEW.operator_signer_fingerprint
      OR OLD.operator_authority_hash IS NOT NEW.operator_authority_hash)
BEGIN SELECT RAISE(ABORT, 'runtime binding signer authority is immutable'); END;
CREATE TABLE IF NOT EXISTS runtime_signer_enrollment_intents (
    enrollment_key TEXT PRIMARY KEY, operator_id TEXT NOT NULL, reason TEXT NOT NULL,
    ticket_ids_json TEXT NOT NULL, public_key_fingerprint TEXT NOT NULL,
    authority_hash TEXT NOT NULL, old_config_hash TEXT NOT NULL, new_config_hash TEXT NOT NULL,
    selected_bindings_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending_config','config_written','finalized','invalidated')),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, config_identity_json TEXT,
 document_json TEXT, document_hash TEXT, detached_signature TEXT, ledger_identity TEXT,
 old_config_identity_json TEXT, nonce TEXT, new_config_bytes TEXT
 );
CREATE TABLE IF NOT EXISTS runtime_signer_enrollments (
    enrollment_key TEXT NOT NULL REFERENCES runtime_signer_enrollment_intents(enrollment_key),
    ticket_id TEXT NOT NULL REFERENCES tickets(id), operator_id TEXT NOT NULL, reason TEXT NOT NULL,
    old_binding_identity_json TEXT NOT NULL, new_binding_identity_json TEXT NOT NULL,
    public_key_fingerprint TEXT NOT NULL, authority_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL, evidence_hash TEXT NOT NULL,
    config_identity_json TEXT,
    PRIMARY KEY(enrollment_key, ticket_id)
);
CREATE TRIGGER IF NOT EXISTS runtime_signer_enrollments_immutable_update
BEFORE UPDATE ON runtime_signer_enrollments BEGIN SELECT RAISE(ABORT, 'runtime signer enrollments are append-only'); END;
CREATE TRIGGER IF NOT EXISTS runtime_signer_enrollments_immutable_delete
BEFORE DELETE ON runtime_signer_enrollments BEGIN SELECT RAISE(ABORT, 'runtime signer enrollments are append-only'); END;
CREATE TABLE IF NOT EXISTS runtime_stages (
    ticket_id TEXT NOT NULL REFERENCES tickets(id), stage TEXT NOT NULL, detail TEXT NOT NULL,
    attempt_number INTEGER, artifact_path TEXT, artifact_sha256 TEXT, base_sha TEXT,
    created_at INTEGER NOT NULL, PRIMARY KEY(ticket_id, stage)
);
CREATE TABLE IF NOT EXISTS model_stage_artifacts (
    ticket_id TEXT NOT NULL REFERENCES tickets(id), attempt_number INTEGER NOT NULL,
    stage TEXT NOT NULL, purpose TEXT NOT NULL, adapter TEXT NOT NULL,
    request_hash TEXT NOT NULL, response_artifact TEXT NOT NULL, worktree_path TEXT NOT NULL,
    base_sha TEXT NOT NULL, diff_hash TEXT NOT NULL, completed_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed', UNIQUE(ticket_id, attempt_number, stage)
);
CREATE TABLE IF NOT EXISTS hermes_execution_reconciliations (
    external_task_id TEXT NOT NULL,
    hermes_run_id INTEGER NOT NULL,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    run_status TEXT NOT NULL,
    run_outcome TEXT,
    session_id TEXT,
    branch_name TEXT,
    workspace_path TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    diff_hash TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(external_task_id, hermes_run_id),
    UNIQUE(ticket_id, attempt_number),
    UNIQUE(snapshot_hash)
);
CREATE TABLE IF NOT EXISTS model_invocations (
    invocation_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL, stage TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    packet_hash TEXT NOT NULL, worktree_path TEXT NOT NULL, timeout_seconds INTEGER NOT NULL,
    started_at INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('started','completed','timeout','process_error','malformed_output')),
    completed_at INTEGER, duration_seconds REAL, error_json TEXT, model_artifact TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_invocations_incomplete ON model_invocations(status, ticket_id);
CREATE INDEX IF NOT EXISTS idx_model_invocations_attempt_stage ON model_invocations(ticket_id, attempt_number, stage, started_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_invocations_one_started_review ON model_invocations(ticket_id, attempt_number, stage) WHERE stage='review' AND status='started';
CREATE TABLE IF NOT EXISTS ticket_runtime_metrics (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
    feature_id TEXT,
    tranche_id TEXT,
    attempts INTEGER NOT NULL CHECK(attempts >= 1),
    accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
    context_tokens INTEGER NOT NULL CHECK(context_tokens >= 0),
    declared_files INTEGER NOT NULL CHECK(declared_files >= 0),
    implementation_seconds REAL NOT NULL CHECK(implementation_seconds >= 0),
    review_seconds REAL NOT NULL CHECK(review_seconds >= 0),
    completed_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS ticket_runtime_metrics_immutable_update
BEFORE UPDATE ON ticket_runtime_metrics BEGIN SELECT RAISE(ABORT, 'ticket runtime metrics are append-only'); END;
CREATE TRIGGER IF NOT EXISTS ticket_runtime_metrics_immutable_delete
BEFORE DELETE ON ticket_runtime_metrics BEGIN SELECT RAISE(ABORT, 'ticket runtime metrics are append-only'); END;
CREATE TABLE IF NOT EXISTS review_candidates (
    ticket_id TEXT NOT NULL REFERENCES tickets(id), attempt_number INTEGER NOT NULL,
    candidate_fingerprint TEXT NOT NULL, validation_evidence TEXT NOT NULL,
    implementation_invocation_id TEXT, runtime_identity_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('review_pending','review_infrastructure_failed','review_completed')),
    last_outcome TEXT, historical_review_attempted INTEGER NOT NULL DEFAULT 0 CHECK(historical_review_attempted IN (0,1)), historical_provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    PRIMARY KEY(ticket_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS review_retry_authorizations (
    authorization_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), attempt_number INTEGER NOT NULL,
    candidate_fingerprint TEXT NOT NULL, failed_invocation_id TEXT NOT NULL REFERENCES model_invocations(invocation_id),
    operator_id TEXT NOT NULL, authorized_at INTEGER NOT NULL, consumed_invocation_id TEXT REFERENCES model_invocations(invocation_id), consumed_at INTEGER,
    UNIQUE(ticket_id, attempt_number, failed_invocation_id)
);
CREATE TABLE IF NOT EXISTS failed_attempt_reconciliations (
    ticket_id TEXT NOT NULL REFERENCES tickets(id), retired_attempt_number INTEGER NOT NULL,
    classification TEXT NOT NULL, previous_ticket_state TEXT NOT NULL, resulting_ticket_state TEXT NOT NULL,
    operator_id TEXT NOT NULL, runtime_identity_json TEXT NOT NULL, retry_base_sha TEXT NOT NULL,
    prospective_next_attempt_number INTEGER NOT NULL, cleanup_required INTEGER NOT NULL CHECK(cleanup_required IN (0,1)),
    forensic_artifact_paths_json TEXT NOT NULL DEFAULT '[]', reconciled_at INTEGER NOT NULL,
    PRIMARY KEY(ticket_id, retired_attempt_number),
    UNIQUE(ticket_id, prospective_next_attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_failed_attempt_reconciliations_ticket ON failed_attempt_reconciliations(ticket_id, reconciled_at);
CREATE TABLE IF NOT EXISTS retired_attempt_cleanup_confirmations (
    ticket_id TEXT NOT NULL REFERENCES tickets(id), retired_attempt_number INTEGER NOT NULL,
    operator_id TEXT NOT NULL, checked_paths_json TEXT NOT NULL, confirmed_at INTEGER NOT NULL,
    PRIMARY KEY(ticket_id, retired_attempt_number),
    FOREIGN KEY(ticket_id, retired_attempt_number)
        REFERENCES failed_attempt_reconciliations(ticket_id, retired_attempt_number)
);
CREATE TABLE IF NOT EXISTS evidence_comment_outbox (
    operation_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), event_id INTEGER NOT NULL,
    external_task_id TEXT NOT NULL, operation_kind TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL, status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT, lease_expires_at INTEGER, next_attempt_at INTEGER, last_error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, delivered_at INTEGER,
 terminal_owner TEXT,
    UNIQUE(ticket_id, event_id, operation_kind)
);
CREATE TABLE IF NOT EXISTS evidence_comments (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id), comment TEXT NOT NULL,
    artifact_location TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS accepted_evidence (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id), accepted_commit_sha TEXT NOT NULL,
    diff_summary TEXT NOT NULL, validation_summary TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS accepted_candidates (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    implementation_artifact TEXT NOT NULL,
    implementation_artifact_sha256 TEXT NOT NULL,
    validation_artifact TEXT NOT NULL,
    validation_artifact_sha256 TEXT NOT NULL,
    review_artifact TEXT NOT NULL,
    review_artifact_sha256 TEXT NOT NULL,
    review_result_id INTEGER NOT NULL,
    evidence_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS accepted_candidates_immutable_update
BEFORE UPDATE ON accepted_candidates BEGIN SELECT RAISE(ABORT, 'accepted candidates are append-only'); END;
CREATE TRIGGER IF NOT EXISTS accepted_candidates_immutable_delete
BEFORE DELETE ON accepted_candidates BEGIN SELECT RAISE(ABORT, 'accepted candidates are append-only'); END;
CREATE TABLE IF NOT EXISTS git_commit_intents (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    accepted_evidence_hash TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    commit_message TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('started','completed')),
    commit_sha TEXT,
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE TRIGGER IF NOT EXISTS git_commit_intents_immutable_identity
BEFORE UPDATE OF attempt_number,accepted_evidence_hash,candidate_fingerprint,base_sha,worktree_path,commit_message ON git_commit_intents
BEGIN SELECT RAISE(ABORT, 'git commit intent identity is immutable'); END;
CREATE TABLE IF NOT EXISTS git_commit_evidence (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    accepted_evidence_hash TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    commit_message TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    tranche_id TEXT,
    integration_head_before TEXT NOT NULL,
    integration_head_after TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS git_commit_evidence_immutable_update
BEFORE UPDATE ON git_commit_evidence BEGIN SELECT RAISE(ABORT, 'git commit evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS git_commit_evidence_immutable_delete
BEFORE DELETE ON git_commit_evidence BEGIN SELECT RAISE(ABORT, 'git commit evidence is append-only'); END;
CREATE TABLE IF NOT EXISTS native_dependency_graphs (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
    child_external_id TEXT NOT NULL,
    local_dependency_ids_json TEXT NOT NULL,
    parent_external_ids_json TEXT NOT NULL,
    graph_hash TEXT NOT NULL,
    verified_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS native_dependency_graphs_immutable_update
BEFORE UPDATE ON native_dependency_graphs BEGIN SELECT RAISE(ABORT, 'native dependency graph evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS native_dependency_graphs_immutable_delete
BEFORE DELETE ON native_dependency_graphs BEGIN SELECT RAISE(ABORT, 'native dependency graph evidence is append-only'); END;
CREATE TABLE IF NOT EXISTS native_dependency_releases (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
    graph_hash TEXT NOT NULL,
    child_external_id TEXT NOT NULL,
    parent_completion_hash TEXT NOT NULL,
    routing_authority_json TEXT NOT NULL DEFAULT '{}',
    hermes_status TEXT NOT NULL,
    observed_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS native_dependency_releases_immutable_update
BEFORE UPDATE ON native_dependency_releases BEGIN SELECT RAISE(ABORT, 'native dependency release evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS native_dependency_releases_immutable_delete
BEFORE DELETE ON native_dependency_releases BEGIN SELECT RAISE(ABORT, 'native dependency release evidence is append-only'); END;
CREATE TABLE IF NOT EXISTS native_dependency_release_revalidations (
    revalidation_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    release_graph_hash TEXT NOT NULL,
    release_child_external_id TEXT NOT NULL,
    release_parent_completion_hash TEXT NOT NULL,
    release_routing_authority_json TEXT NOT NULL,
    release_hermes_status TEXT NOT NULL,
    release_observed_at INTEGER NOT NULL,
    projection_event_id INTEGER NOT NULL,
    projection_key TEXT NOT NULL,
    external_task_id TEXT NOT NULL,
    implementation_profile TEXT NOT NULL,
    repository_identity TEXT NOT NULL,
    canonical_worktree_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    revalidation_event_id INTEGER REFERENCES events(id),
    event_key TEXT,
    evidence_hash TEXT,
    approval_document_json TEXT,
    approval_document_hash TEXT,
    detached_signature TEXT,
    signer_fingerprint TEXT,
    snapshot_schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE TRIGGER IF NOT EXISTS native_dependency_release_revalidations_immutable_update
BEFORE UPDATE ON native_dependency_release_revalidations BEGIN SELECT RAISE(ABORT, 'native dependency release revalidations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS native_dependency_release_revalidations_immutable_delete
BEFORE DELETE ON native_dependency_release_revalidations BEGIN SELECT RAISE(ABORT, 'native dependency release revalidations are append-only'); END;
CREATE TABLE IF NOT EXISTS native_dependency_release_revalidation_supersessions (
    old_revalidation_id TEXT PRIMARY KEY REFERENCES native_dependency_release_revalidations(revalidation_id),
    new_revalidation_id TEXT NOT NULL UNIQUE REFERENCES native_dependency_release_revalidations(revalidation_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    reason TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    old_snapshot_hash TEXT NOT NULL,
    new_snapshot_hash TEXT NOT NULL,
    old_snapshot_schema_version INTEGER NOT NULL,
    new_snapshot_schema_version INTEGER NOT NULL,
    old_approval_document_json TEXT NOT NULL,
    new_approval_document_json TEXT NOT NULL,
    old_approval_document_hash TEXT NOT NULL,
    new_approval_document_hash TEXT NOT NULL,
    detached_signature TEXT NOT NULL,
    event_id INTEGER NOT NULL REFERENCES events(id),
    evidence_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS native_dependency_release_revalidation_supersessions_immutable_update
BEFORE UPDATE ON native_dependency_release_revalidation_supersessions BEGIN SELECT RAISE(ABORT, 'native release revalidation supersessions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS native_dependency_release_revalidation_supersessions_immutable_delete
BEFORE DELETE ON native_dependency_release_revalidation_supersessions BEGIN SELECT RAISE(ABORT, 'native release revalidation supersessions are append-only'); END;
CREATE TABLE IF NOT EXISTS native_release_activation_intents (
    request_key TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    revalidation_id TEXT NOT NULL REFERENCES native_dependency_release_revalidations(revalidation_id),
    external_task_id TEXT NOT NULL,
    pre_activation_snapshot_hash TEXT NOT NULL,
    implementation_profile TEXT NOT NULL,
    repository_identity TEXT NOT NULL,
    canonical_worktree_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    activation_marker TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    effect_snapshot_hash TEXT,
    pre_activation_snapshot_json TEXT,
    post_activation_snapshot_json TEXT,
    approval_document_json TEXT,
    approval_document_hash TEXT,
    detached_signature TEXT,
    signer_fingerprint TEXT,
    board_path TEXT,
    board_dev INTEGER,
    board_ino INTEGER,
    activation_event_id INTEGER REFERENCES events(id),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_native_release_activation_revalidation
    ON native_release_activation_intents(revalidation_id);
CREATE TRIGGER IF NOT EXISTS native_release_activation_intents_immutable_identity
BEFORE UPDATE ON native_release_activation_intents
WHEN OLD.ticket_id IS NOT NEW.ticket_id OR OLD.revalidation_id IS NOT NEW.revalidation_id
 OR OLD.external_task_id IS NOT NEW.external_task_id OR OLD.pre_activation_snapshot_hash IS NOT NEW.pre_activation_snapshot_hash
 OR OLD.implementation_profile IS NOT NEW.implementation_profile OR OLD.repository_identity IS NOT NEW.repository_identity
 OR OLD.canonical_worktree_path IS NOT NEW.canonical_worktree_path OR OLD.branch IS NOT NEW.branch OR OLD.base_sha IS NOT NEW.base_sha
 OR OLD.operator_id IS NOT NEW.operator_id OR OLD.reason IS NOT NEW.reason OR OLD.activation_marker IS NOT NEW.activation_marker
 OR OLD.created_at IS NOT NEW.created_at OR OLD.approval_document_json IS NOT NEW.approval_document_json
 OR OLD.approval_document_hash IS NOT NEW.approval_document_hash OR OLD.detached_signature IS NOT NEW.detached_signature
 OR OLD.signer_fingerprint IS NOT NEW.signer_fingerprint OR OLD.board_path IS NOT NEW.board_path
 OR OLD.board_dev IS NOT NEW.board_dev OR OLD.board_ino IS NOT NEW.board_ino
BEGIN SELECT RAISE(ABORT, 'native release activation intent identity is immutable'); END;
CREATE TABLE IF NOT EXISTS native_release_activation_evidence (
    request_key TEXT PRIMARY KEY REFERENCES native_release_activation_intents(request_key),
    ticket_id TEXT NOT NULL,
    revalidation_id TEXT NOT NULL,
    activation_marker TEXT NOT NULL,
    post_activation_snapshot_hash TEXT NOT NULL,
    post_activation_snapshot_json TEXT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    evidence_hash TEXT NOT NULL,
    acknowledged_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS native_release_activation_evidence_immutable_update
BEFORE UPDATE ON native_release_activation_evidence BEGIN SELECT RAISE(ABORT, 'native release activation evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS native_release_activation_evidence_immutable_delete
BEFORE DELETE ON native_release_activation_evidence BEGIN SELECT RAISE(ABORT, 'native release activation evidence is append-only'); END;
CREATE TABLE IF NOT EXISTS native_release_activation_running_observations (
    observation_id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL REFERENCES native_release_activation_intents(request_key),
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    external_task_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    process_identity_json TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    profile TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    event_id INTEGER NOT NULL REFERENCES events(id),
    evidence_hash TEXT NOT NULL,
    UNIQUE(request_key), UNIQUE(snapshot_hash), UNIQUE(event_id)
);
CREATE TRIGGER IF NOT EXISTS native_release_activation_running_observations_immutable_update
BEFORE UPDATE ON native_release_activation_running_observations BEGIN SELECT RAISE(ABORT, 'native release running observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS native_release_activation_running_observations_immutable_delete
BEFORE DELETE ON native_release_activation_running_observations BEGIN SELECT RAISE(ABORT, 'native release running observations are append-only'); END;
CREATE TABLE IF NOT EXISTS tranche_completion_evidence (
    tranche_id TEXT PRIMARY KEY REFERENCES tranches(id), root_planning_sha TEXT NOT NULL,
    final_integration_sha TEXT NOT NULL, accepted_ticket_ids_json TEXT NOT NULL,
    accepted_commit_shas_json TEXT NOT NULL, evidence_hash TEXT NOT NULL, completed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tranche_completion_rechecks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tranche_id TEXT NOT NULL REFERENCES tranches(id), generation INTEGER NOT NULL,
    previous_generation INTEGER NOT NULL, previous_evidence_hash TEXT NOT NULL,
    correction_plan_ids_json TEXT NOT NULL, accepted_ticket_ids_json TEXT NOT NULL,
    accepted_commit_shas_json TEXT NOT NULL, current_integration_sha TEXT NOT NULL,
    repository_identity TEXT NOT NULL, repo_base_sha TEXT NOT NULL, repo_snapshot_hash TEXT NOT NULL,
    unresolved_correction_count INTEGER NOT NULL, status TEXT NOT NULL, evidence_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, recorded_at INTEGER NOT NULL,
    UNIQUE(tranche_id, generation)
);
CREATE TRIGGER IF NOT EXISTS tranche_completion_evidence_immutable_update
BEFORE UPDATE ON tranche_completion_evidence BEGIN SELECT RAISE(ABORT, 'tranche completion evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS tranche_completion_evidence_immutable_delete
BEFORE DELETE ON tranche_completion_evidence BEGIN SELECT RAISE(ABORT, 'tranche completion evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS tranche_completion_evidence_hash_integrity
BEFORE INSERT ON tranche_completion_evidence
WHEN NEW.evidence_hash != canonical_completion_hash(NEW.tranche_id,NEW.root_planning_sha,NEW.final_integration_sha,NEW.accepted_ticket_ids_json,NEW.accepted_commit_shas_json)
BEGIN SELECT RAISE(ABORT, 'tranche completion evidence hash mismatch'); END;
CREATE TABLE IF NOT EXISTS tranche_checkpoint_evidence (
    tranche_id TEXT PRIMARY KEY REFERENCES tranches(id),
    feature_id TEXT NOT NULL REFERENCES features(id),
    completion_evidence_hash TEXT NOT NULL,
    final_integration_sha TEXT NOT NULL,
    repository_identity TEXT NOT NULL,
    planning_base_sha TEXT NOT NULL,
    planning_snapshot_hash TEXT NOT NULL,
    integration_commands_json TEXT NOT NULL,
    integration_results_json TEXT NOT NULL,
    checkpoint_artifact TEXT NOT NULL,
    checkpoint_artifact_sha256 TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('ready_for_checkpoint','integration_failed')),
    created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS tranche_checkpoint_evidence_immutable_update
BEFORE UPDATE ON tranche_checkpoint_evidence BEGIN SELECT RAISE(ABORT, 'tranche checkpoint evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS tranche_checkpoint_evidence_immutable_delete
BEFORE DELETE ON tranche_checkpoint_evidence BEGIN SELECT RAISE(ABORT, 'tranche checkpoint evidence is immutable'); END;
CREATE TABLE IF NOT EXISTS paid_checkpoint_evidence (
    tranche_id TEXT NOT NULL REFERENCES tranches(id),
    feature_id TEXT NOT NULL REFERENCES features(id),
    checkpoint_artifact_sha256 TEXT NOT NULL,
    checkpoint_completion_hash TEXT NOT NULL,
    scheduler_claim_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('integration_checkpoint','escalation')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    profile TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    model_call_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approve','escalate','reject')),
    rationale TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(tranche_id,purpose)
);
CREATE TRIGGER IF NOT EXISTS paid_checkpoint_evidence_immutable_update
BEFORE UPDATE ON paid_checkpoint_evidence BEGIN SELECT RAISE(ABORT, 'paid checkpoint evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS paid_checkpoint_evidence_immutable_delete
BEFORE DELETE ON paid_checkpoint_evidence BEGIN SELECT RAISE(ABORT, 'paid checkpoint evidence is immutable'); END;
CREATE TABLE IF NOT EXISTS next_tranche_materializations (
    predecessor_tranche_id TEXT PRIMARY KEY REFERENCES tranches(id),
    successor_tranche_id TEXT NOT NULL REFERENCES tranches(id),
    feature_id TEXT NOT NULL REFERENCES features(id),
    approval_purpose TEXT NOT NULL,
    approval_model_call_id TEXT NOT NULL,
    predecessor_completion_hash TEXT NOT NULL,
    repository_identity TEXT NOT NULL,
    repo_base_sha TEXT NOT NULL,
    repo_snapshot_hash TEXT NOT NULL,
    ticket_ids_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS next_tranche_materializations_immutable_update
BEFORE UPDATE ON next_tranche_materializations BEGIN SELECT RAISE(ABORT, 'next tranche materialization is immutable'); END;
CREATE TRIGGER IF NOT EXISTS next_tranche_materializations_immutable_delete
BEFORE DELETE ON next_tranche_materializations BEGIN SELECT RAISE(ABORT, 'next tranche materialization is immutable'); END;
CREATE TABLE IF NOT EXISTS next_tranche_activation_evidence (
    successor_tranche_id TEXT PRIMARY KEY REFERENCES tranches(id),
    predecessor_tranche_id TEXT NOT NULL,
    feature_id TEXT NOT NULL,
    repo_snapshot_hash TEXT NOT NULL,
    ticket_ids_json TEXT NOT NULL,
    external_task_ids_json TEXT NOT NULL,
    dependency_graph_hashes_json TEXT NOT NULL,
    activated_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS next_tranche_activation_evidence_immutable_update
BEFORE UPDATE ON next_tranche_activation_evidence BEGIN SELECT RAISE(ABORT, 'next tranche activation evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS next_tranche_activation_evidence_immutable_delete
BEFORE DELETE ON next_tranche_activation_evidence BEGIN SELECT RAISE(ABORT, 'next tranche activation evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS tranche_completion_rechecks_immutable_update
BEFORE UPDATE ON tranche_completion_rechecks BEGIN SELECT RAISE(ABORT, 'tranche completion rechecks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS tranche_completion_rechecks_immutable_delete
BEFORE DELETE ON tranche_completion_rechecks BEGIN SELECT RAISE(ABORT, 'tranche completion rechecks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS tranche_completion_rechecks_hash_integrity
BEFORE INSERT ON tranche_completion_rechecks
WHEN NEW.evidence_hash != canonical_recheck_hash(NEW.tranche_id,NEW.generation,NEW.previous_generation,NEW.previous_evidence_hash,NEW.correction_plan_ids_json,NEW.accepted_ticket_ids_json,NEW.accepted_commit_shas_json,NEW.current_integration_sha,NEW.repository_identity,NEW.repo_base_sha,NEW.repo_snapshot_hash,NEW.unresolved_correction_count,NEW.status)
BEGIN SELECT RAISE(ABORT, 'tranche completion recheck hash mismatch'); END;
CREATE TRIGGER IF NOT EXISTS events_immutable_update
BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_immutable_delete
BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TABLE IF NOT EXISTS historical_revalidation_authorizations (
    authorization_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    base_sha TEXT NOT NULL,
    repository_identity TEXT NOT NULL,
    target_file TEXT NOT NULL,
    failure_classification TEXT NOT NULL,
    failure_evidence_identity TEXT NOT NULL,
    implementation_invocation_id TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    authorization_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    UNIQUE(ticket_id, attempt_number)
);
CREATE TRIGGER IF NOT EXISTS historical_revalidation_authorizations_immutable_update
BEFORE UPDATE ON historical_revalidation_authorizations BEGIN SELECT RAISE(ABORT, 'historical revalidation authorizations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS historical_revalidation_authorizations_immutable_delete
BEFORE DELETE ON historical_revalidation_authorizations BEGIN SELECT RAISE(ABORT, 'historical revalidation authorizations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS historical_revalidation_authorizations_hash_integrity
BEFORE INSERT ON historical_revalidation_authorizations
WHEN NEW.authorization_hash IS NULL OR NEW.authorization_hash != canonical_historical_authorization_hash(NEW.ticket_id,NEW.attempt_number,NEW.base_sha,NEW.repository_identity,NEW.target_file,NEW.failure_classification,NEW.failure_evidence_identity,NEW.implementation_invocation_id,NEW.operator_id,NEW.reason)
BEGIN SELECT RAISE(ABORT, 'historical revalidation authorization hash mismatch'); END;
CREATE TABLE IF NOT EXISTS historical_revalidation_attestations (
    attestation_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL,
    base_sha TEXT NOT NULL,
    repository_identity TEXT NOT NULL,
    implementation_invocation_id TEXT NOT NULL,
    implementation_artifact TEXT NOT NULL,
    implementation_diff_hash TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    worktree_diff_hash TEXT NOT NULL,
    authorization_hash TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    attestation_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    UNIQUE(ticket_id, attempt_number)
);
CREATE TRIGGER IF NOT EXISTS historical_revalidation_attestations_immutable_update
BEFORE UPDATE ON historical_revalidation_attestations BEGIN SELECT RAISE(ABORT, 'historical revalidation attestations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS historical_revalidation_attestations_immutable_delete
BEFORE DELETE ON historical_revalidation_attestations BEGIN SELECT RAISE(ABORT, 'historical revalidation attestations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS historical_revalidation_attestations_hash_integrity
BEFORE INSERT ON historical_revalidation_attestations
WHEN NEW.attestation_hash IS NULL OR NEW.attestation_hash != canonical_historical_attestation_hash(NEW.ticket_id,NEW.attempt_number,NEW.base_sha,NEW.repository_identity,NEW.implementation_invocation_id,NEW.implementation_artifact,NEW.implementation_diff_hash,NEW.worktree_path,NEW.worktree_diff_hash,NEW.authorization_hash,NEW.operator_id)
BEGIN SELECT RAISE(ABORT, 'historical revalidation attestation hash mismatch'); END;
CREATE TABLE IF NOT EXISTS historical_revalidation_validation_claims (
    claim_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), attempt_number INTEGER NOT NULL,
    authorization_hash TEXT NOT NULL, attestation_hash TEXT NOT NULL, base_sha TEXT NOT NULL,
    implementation_diff_hash TEXT NOT NULL, validation_profile_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE(ticket_id, attempt_number)
);
CREATE TRIGGER IF NOT EXISTS historical_revalidation_validation_claims_immutable_update
BEFORE UPDATE ON historical_revalidation_validation_claims BEGIN SELECT RAISE(ABORT, 'historical validation claims are append-only'); END;
CREATE TRIGGER IF NOT EXISTS historical_revalidation_validation_claims_immutable_delete
BEFORE DELETE ON historical_revalidation_validation_claims BEGIN SELECT RAISE(ABORT, 'historical validation claims are append-only'); END;
CREATE TABLE IF NOT EXISTS historical_revalidation_validation_results (
    result_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL UNIQUE REFERENCES historical_revalidation_validation_claims(claim_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(id), attempt_number INTEGER NOT NULL, authorization_hash TEXT NOT NULL,
    attestation_hash TEXT NOT NULL, base_sha TEXT NOT NULL, implementation_diff_hash TEXT NOT NULL,
    validation_profile_hash TEXT NOT NULL, artifact_path TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0,1)), compact_evidence TEXT NOT NULL, result_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL, UNIQUE(ticket_id, attempt_number)
);
CREATE TRIGGER IF NOT EXISTS historical_revalidation_validation_results_immutable_update
BEFORE UPDATE ON historical_revalidation_validation_results BEGIN SELECT RAISE(ABORT, 'historical validation results are append-only'); END;
CREATE TRIGGER IF NOT EXISTS historical_revalidation_validation_results_immutable_delete
BEFORE DELETE ON historical_revalidation_validation_results BEGIN SELECT RAISE(ABORT, 'historical validation results are append-only'); END;
CREATE TRIGGER IF NOT EXISTS historical_revalidation_validation_results_claim_integrity
BEFORE INSERT ON historical_revalidation_validation_results
WHEN NOT EXISTS (SELECT 1 FROM historical_revalidation_validation_claims c WHERE c.claim_id=NEW.claim_id AND c.ticket_id=NEW.ticket_id AND c.attempt_number=NEW.attempt_number AND c.authorization_hash=NEW.authorization_hash AND c.attestation_hash=NEW.attestation_hash AND c.base_sha=NEW.base_sha AND c.implementation_diff_hash=NEW.implementation_diff_hash AND c.validation_profile_hash=NEW.validation_profile_hash)
BEGIN SELECT RAISE(ABORT, 'historical validation result claim mismatch'); END;
CREATE TRIGGER IF NOT EXISTS historical_revalidation_validation_results_hash_integrity
BEFORE INSERT ON historical_revalidation_validation_results
WHEN NEW.result_hash IS NULL OR NEW.result_hash != canonical_historical_validation_result_hash(NEW.ticket_id,NEW.attempt_number,NEW.authorization_hash,NEW.attestation_hash,NEW.base_sha,NEW.implementation_diff_hash,NEW.validation_profile_hash,NEW.artifact_sha256,NEW.passed,NEW.compact_evidence)
BEGIN SELECT RAISE(ABORT, 'historical validation result hash mismatch'); END;
"""


def _recheck_evidence_payload(row: Any) -> dict[str, Any]:
    """Return the canonical persisted payload used by new re-check rows."""
    return {
        "tranche_id": str(row["tranche_id"]),
        "generation": int(row["generation"]),
        "previous_generation": int(row["previous_generation"]),
        "previous_evidence_hash": str(row["previous_evidence_hash"]),
        "correction_plan_ids": json.loads(row["correction_plan_ids_json"]),
        "accepted_ticket_ids": json.loads(row["accepted_ticket_ids_json"]),
        "accepted_commit_shas": json.loads(row["accepted_commit_shas_json"]),
        "current_integration_sha": str(row["current_integration_sha"]),
        "repository_identity": str(row["repository_identity"]),
        "repo_base_sha": str(row["repo_base_sha"]),
        "repo_snapshot_hash": str(row["repo_snapshot_hash"]),
        "unresolved_correction_count": int(row["unresolved_correction_count"]),
        "status": str(row["status"]),
    }


def _recheck_evidence_hash(row: Any) -> str:
    return _hash_recheck_payload(_recheck_evidence_payload(row))


def _hash_recheck_payload(payload: dict[str, Any]) -> str:
    return canonical_sha256(payload)


def _completion_evidence_payload(row: Any) -> dict[str, Any]:
    return {"tranche_id": str(row["tranche_id"]), "root_planning_sha": str(row["root_planning_sha"]),
            "final_integration_sha": str(row["final_integration_sha"]),
            "accepted_ticket_ids": json.loads(row["accepted_ticket_ids_json"]),
            "accepted_commit_shas": json.loads(row["accepted_commit_shas_json"])}


def _completion_evidence_hash(row: Any) -> str:
    return _hash_recheck_payload(_completion_evidence_payload(row))


def _sqlite_completion_hash(tranche_id: str, root: str, final: str, tickets: str, commits: str) -> str:
    return _completion_evidence_hash({"tranche_id": tranche_id, "root_planning_sha": root, "final_integration_sha": final,
                                      "accepted_ticket_ids_json": tickets, "accepted_commit_shas_json": commits})


def _sqlite_recheck_hash(tranche_id: str, generation: int, previous_generation: int, previous_hash: str,
                         plan_ids: str, ticket_ids: str, commits: str, head: str, repository: str,
                         base: str, snapshot: str, unresolved: int, status: str) -> str:
    return _hash_recheck_payload({"tranche_id": str(tranche_id), "generation": int(generation),
        "previous_generation": int(previous_generation), "previous_evidence_hash": str(previous_hash),
        "correction_plan_ids": json.loads(plan_ids), "accepted_ticket_ids": json.loads(ticket_ids),
        "accepted_commit_shas": json.loads(commits), "current_integration_sha": str(head),
        "repository_identity": str(repository), "repo_base_sha": str(base), "repo_snapshot_hash": str(snapshot),
        "unresolved_correction_count": int(unresolved), "status": str(status)})


class Ledger:
    """Standalone SQLite ledger. It deliberately has no Hermes imports."""

    _PROJECTABLE_STATES = frozenset({
        CanonicalState.NEEDS_ARCHITECTURE.value, CanonicalState.READY_LOCAL.value,
        CanonicalState.ACCEPTED.value, CanonicalState.NEEDS_HUMAN_TEST.value,
        CanonicalState.NEEDS_CHECKPOINT.value, CanonicalState.NEEDS_TRIAGE.value,
        CanonicalState.BLOCKED.value, CanonicalState.DONE.value,
        CanonicalState.REJECTED.value, CanonicalState.REVERTED.value,
        CanonicalState.LOCAL_REVIEW.value,
    })

    def __init__(self, database: Path, *, failure_injector: Any | None = None) -> None:
        self.database = Path(database)
        if self.database.name == "kanban.db" or self.database.resolve(strict=False).name == "kanban.db":
            raise ValueError("ledger database must be distinct from Hermes kanban.db")
        self.connection = sqlite3.connect(self.database, isolation_level=None, check_same_thread=False)
        self.connection.create_function("canonical_completion_hash", 5, _sqlite_completion_hash)
        self.connection.create_function("canonical_recheck_hash", 13, _sqlite_recheck_hash)
        self.connection.create_function("canonical_historical_authorization_hash", 10, _sqlite_historical_authorization_hash)
        self.connection.create_function("canonical_historical_attestation_hash", 11, _sqlite_historical_attestation_hash)
        self.connection.create_function("canonical_historical_validation_hash", 7, _sqlite_historical_validation_hash)
        self.connection.create_function("canonical_historical_validation_result_hash", 10, _sqlite_historical_validation_result_hash)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._lock = RLock()
        self.failure_injector = failure_injector

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def snapshot_revalidation_hash(*, feature_id: str, feature_contract_hash: str, repository_identity: str, repo_base_sha: str, source_snapshot_hash: str, target_snapshot_hash: str, generation: int) -> str:
        value = {"feature_id": feature_id, "feature_contract_hash": feature_contract_hash, "repository_identity": repository_identity, "repo_base_sha": repo_base_sha, "source_snapshot_hash": source_snapshot_hash, "target_snapshot_hash": target_snapshot_hash, "generation": generation}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def feature_snapshot_authority(self, feature_id: str) -> dict[str, Any]:
        contract = self.connection.execute("SELECT * FROM feature_contracts WHERE feature_id=?", (feature_id,)).fetchone()
        if contract is None: raise ValueError("feature contract authority is missing")
        current = {"feature_id": feature_id, "feature_contract_hash": contract["contract_hash"], "repository_identity": contract["repository_identity"], "repo_base_sha": contract["repo_base_sha"], "snapshot_hash": contract["repo_snapshot_hash"], "generation": 0}
        rows = self.connection.execute("SELECT * FROM feature_repository_snapshot_revalidations WHERE feature_id=? ORDER BY generation", (feature_id,)).fetchall()
        for row in rows:
            expected = self.snapshot_revalidation_hash(feature_id=row["feature_id"], feature_contract_hash=row["feature_contract_hash"], repository_identity=row["repository_identity"], repo_base_sha=row["repo_base_sha"], source_snapshot_hash=row["source_snapshot_hash"], target_snapshot_hash=row["target_snapshot_hash"], generation=int(row["generation"]))
            if row["feature_contract_hash"] != current["feature_contract_hash"] or row["repository_identity"] != current["repository_identity"] or row["repo_base_sha"] != current["repo_base_sha"] or row["source_snapshot_hash"] != current["snapshot_hash"] or int(row["generation"]) != current["generation"] + 1 or row["revalidation_hash"] != expected:
                raise ValueError("invalid feature snapshot revalidation chain")
            current = {**current, "snapshot_hash": row["target_snapshot_hash"], "generation": int(row["generation"]), "revalidation_hash": row["revalidation_hash"]}
        return current

    def append_feature_snapshot_revalidation(self, *, feature_id: str, feature_contract_hash: str, repository_identity: str, repo_base_sha: str, source_snapshot_hash: str, target_snapshot_hash: str, generation: int, revalidation_hash: str) -> dict[str, Any]:
        requested = (feature_id, feature_contract_hash, repository_identity, repo_base_sha, source_snapshot_hash, target_snapshot_hash, generation, revalidation_hash)
        existing = self.connection.execute("SELECT * FROM feature_repository_snapshot_revalidations WHERE feature_id=? AND generation=?", (feature_id, generation)).fetchone()
        if existing is not None:
            values = (existing["feature_id"], existing["feature_contract_hash"], existing["repository_identity"], existing["repo_base_sha"], existing["source_snapshot_hash"], existing["target_snapshot_hash"], int(existing["generation"]), existing["revalidation_hash"])
            expected = self.snapshot_revalidation_hash(feature_id=existing["feature_id"], feature_contract_hash=existing["feature_contract_hash"], repository_identity=existing["repository_identity"], repo_base_sha=existing["repo_base_sha"], source_snapshot_hash=existing["source_snapshot_hash"], target_snapshot_hash=existing["target_snapshot_hash"], generation=int(existing["generation"]))
            if existing["revalidation_hash"] != expected or values != requested: raise ValueError("conflicting snapshot revalidation")
            return dict(existing)
        expected = self.snapshot_revalidation_hash(feature_id=feature_id, feature_contract_hash=feature_contract_hash, repository_identity=repository_identity, repo_base_sha=repo_base_sha, source_snapshot_hash=source_snapshot_hash, target_snapshot_hash=target_snapshot_hash, generation=generation)
        if revalidation_hash != expected: raise ValueError("snapshot revalidation hash mismatch")
        authority = self.feature_snapshot_authority(feature_id)
        if (feature_contract_hash, repository_identity, repo_base_sha, source_snapshot_hash, generation) != (authority["feature_contract_hash"], authority["repository_identity"], authority["repo_base_sha"], authority["snapshot_hash"], authority["generation"] + 1):
            raise ValueError("snapshot revalidation source or generation conflicts")
        now = int(time.time())
        try:
            with self._transaction() as c:
                c.execute("INSERT INTO feature_repository_snapshot_revalidations(feature_id,feature_contract_hash,repository_identity,repo_base_sha,source_snapshot_hash,target_snapshot_hash,generation,revalidation_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (feature_id, feature_contract_hash, repository_identity, repo_base_sha, source_snapshot_hash, target_snapshot_hash, generation, revalidation_hash, now))
        except sqlite3.IntegrityError:
            raced = self.connection.execute("SELECT * FROM feature_repository_snapshot_revalidations WHERE feature_id=? AND generation=?", (feature_id, generation)).fetchone()
            if raced is None: raise
            values = (raced["feature_id"], raced["feature_contract_hash"], raced["repository_identity"], raced["repo_base_sha"], raced["source_snapshot_hash"], raced["target_snapshot_hash"], int(raced["generation"]), raced["revalidation_hash"])
            if values != requested or raced["revalidation_hash"] != self.snapshot_revalidation_hash(feature_id=raced["feature_id"], feature_contract_hash=raced["feature_contract_hash"], repository_identity=raced["repository_identity"], repo_base_sha=raced["repo_base_sha"], source_snapshot_hash=raced["source_snapshot_hash"], target_snapshot_hash=raced["target_snapshot_hash"], generation=int(raced["generation"])): raise ValueError("conflicting snapshot revalidation")
            return dict(raced)
        return dict(self.connection.execute("SELECT * FROM feature_repository_snapshot_revalidations WHERE feature_id=? AND generation=?", (feature_id, generation)).fetchone())

    def migrate(self) -> None:
        self.connection.executescript(_SCHEMA)
        activation_intent_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(native_release_activation_intents)")}
        for name in ("pre_activation_snapshot_json", "post_activation_snapshot_json"):
            if name not in activation_intent_columns:
                self.connection.execute(f"ALTER TABLE native_release_activation_intents ADD COLUMN {name} TEXT")
        activation_evidence_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(native_release_activation_evidence)")}
        if "post_activation_snapshot_json" not in activation_evidence_columns:
            self.connection.execute("ALTER TABLE native_release_activation_evidence ADD COLUMN post_activation_snapshot_json TEXT")
        observation_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(native_release_activation_running_observations)")}
        if "snapshot_json" not in observation_columns:
            self.connection.execute("ALTER TABLE native_release_activation_running_observations ADD COLUMN snapshot_json TEXT")
        tick_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(scheduler_tick_lease)")}
        if "lease_token" not in tick_columns:
            with self._transaction() as conn:
                conn.execute("ALTER TABLE scheduler_tick_lease ADD COLUMN lease_token TEXT")
                # Pre-fencing leases cannot prove ownership after restart. Dropping
                # only this ephemeral row is safer than letting it block or release
                # a token-fenced tick; durable stage/outbox claims remain intact.
                conn.execute("DELETE FROM scheduler_tick_lease")
        self.connection.executescript("""
            CREATE TRIGGER IF NOT EXISTS scheduler_tick_lease_token_insert
            BEFORE INSERT ON scheduler_tick_lease
            WHEN NEW.lease_token IS NULL OR typeof(NEW.lease_token) != 'text' OR length(NEW.lease_token) = 0
            BEGIN SELECT RAISE(ABORT, 'scheduler tick lease token is required'); END;
            CREATE TRIGGER IF NOT EXISTS scheduler_tick_lease_token_update
            BEFORE UPDATE ON scheduler_tick_lease
            WHEN NEW.lease_token IS NULL OR typeof(NEW.lease_token) != 'text' OR length(NEW.lease_token) = 0
            BEGIN SELECT RAISE(ABORT, 'scheduler tick lease token is required'); END;
        """)
        binding_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_bindings)")}
        if "canonical_sha" not in binding_columns:
            self.connection.execute("ALTER TABLE runtime_bindings ADD COLUMN canonical_sha TEXT")
        enrollment_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_signer_enrollment_intents)")}
        if "new_config_bytes" not in enrollment_columns:
            self.connection.execute("ALTER TABLE runtime_signer_enrollment_intents ADD COLUMN new_config_bytes TEXT")
        runtime_stage_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_stages)")}
        for column, definition in (("attempt_number", "INTEGER"), ("artifact_path", "TEXT"), ("artifact_sha256", "TEXT"), ("base_sha", "TEXT")):
            if column not in runtime_stage_columns:
                self.connection.execute(f"ALTER TABLE runtime_stages ADD COLUMN {column} {definition}")
        recheck_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tranche_completion_rechecks)")}
        if "evidence_hash" not in recheck_columns:
            self.connection.execute("ALTER TABLE tranche_completion_rechecks ADD COLUMN evidence_hash TEXT")
        # Older ledgers allowed NULL hashes.  DDL cannot add a NOT NULL
        # constraint in-place on SQLite, so reconcile rows transactionally and
        # enforce the same invariant with an insert trigger thereafter.
        with self._transaction() as conn:
            legacy_rows = conn.execute("SELECT * FROM tranche_completion_rechecks WHERE evidence_hash IS NULL").fetchall()
            if legacy_rows:
                conn.execute("DROP TRIGGER IF EXISTS tranche_completion_rechecks_immutable_update")
                for row in legacy_rows:
                    conn.execute(
                        "UPDATE tranche_completion_rechecks SET evidence_hash=? WHERE id=?",
                        (_recheck_evidence_hash(row), row["id"]),
                    )
                conn.execute("""CREATE TRIGGER tranche_completion_rechecks_immutable_update
                    BEFORE UPDATE ON tranche_completion_rechecks
                    BEGIN SELECT RAISE(ABORT, 'tranche completion rechecks are append-only'); END""")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS tranche_completion_rechecks_hash_required
                BEFORE INSERT ON tranche_completion_rechecks
                WHEN NEW.evidence_hash IS NULL OR typeof(NEW.evidence_hash) != 'text'
                  OR length(NEW.evidence_hash) != 64
                  OR NEW.evidence_hash GLOB '*[^0123456789abcdef]*'
                  OR lower(NEW.evidence_hash) != NEW.evidence_hash
                BEGIN SELECT RAISE(ABORT, 'tranche completion recheck evidence hash is invalid'); END""")
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS feature_contracts (feature_id TEXT PRIMARY KEY, contract_hash TEXT NOT NULL, contract_json TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS feature_repository_snapshot_revalidations (
            feature_id TEXT NOT NULL REFERENCES feature_contracts(feature_id),
            feature_contract_hash TEXT NOT NULL,
            repository_identity TEXT NOT NULL,
            repo_base_sha TEXT NOT NULL,
            source_snapshot_hash TEXT NOT NULL,
            target_snapshot_hash TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation > 0),
            revalidation_hash TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(feature_id, generation),
            UNIQUE(feature_id, source_snapshot_hash, target_snapshot_hash, generation)
        );
        CREATE TABLE IF NOT EXISTS decomposition_plans (id TEXT PRIMARY KEY, feature_id TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE, plan_json TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, activated_at INTEGER, repository_identity TEXT, repo_base_sha TEXT, repo_snapshot_hash TEXT, repo_snapshot_manifest_json TEXT, UNIQUE(feature_id, fingerprint));
        CREATE TABLE IF NOT EXISTS planning_runs (
            request_key TEXT PRIMARY KEY, feature_id TEXT NOT NULL, contract_hash TEXT NOT NULL,
            repo_base_sha TEXT NOT NULL, repo_snapshot_hash TEXT NOT NULL, planner_identity TEXT NOT NULL,
            cost_class TEXT NOT NULL, status TEXT NOT NULL, response_artifact TEXT,
            structural_reasons_json TEXT NOT NULL DEFAULT '[]', repository_reasons_json TEXT NOT NULL DEFAULT '[]',
            plan_id TEXT, ticket_ids_json TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tranche_criteria (tranche_id TEXT NOT NULL, criterion_id TEXT NOT NULL, PRIMARY KEY(tranche_id, criterion_id));
        CREATE TABLE IF NOT EXISTS ticket_criteria (ticket_id TEXT NOT NULL, criterion_id TEXT NOT NULL, PRIMARY KEY(ticket_id, criterion_id));
        CREATE TABLE IF NOT EXISTS supplemental_correction_plans (
            correction_plan_id TEXT PRIMARY KEY, finding_key TEXT NOT NULL UNIQUE,
            feature_id TEXT NOT NULL REFERENCES features(id), tranche_id TEXT NOT NULL REFERENCES tranches(id),
            source_kind TEXT NOT NULL, source_reference TEXT NOT NULL, finding_fingerprint TEXT NOT NULL,
            finding_summary TEXT NOT NULL, observed_integration_head TEXT NOT NULL,
            repository_identity TEXT NOT NULL, base_sha TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
            plan_json TEXT NOT NULL, status TEXT NOT NULL, ordinal INTEGER NOT NULL, created_at INTEGER NOT NULL, materialized_at INTEGER,
            UNIQUE(tranche_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS supplemental_correction_predecessors (
            correction_plan_id TEXT NOT NULL REFERENCES supplemental_correction_plans(correction_plan_id),
            ticket_id TEXT NOT NULL REFERENCES tickets(id), accepted_commit_sha TEXT NOT NULL,
            PRIMARY KEY(correction_plan_id, ticket_id)
        );
        CREATE TABLE IF NOT EXISTS supplemental_correction_tickets (
            correction_plan_id TEXT NOT NULL REFERENCES supplemental_correction_plans(correction_plan_id),
            ticket_id TEXT PRIMARY KEY REFERENCES tickets(id), ordinal INTEGER NOT NULL, admission_head TEXT,
            UNIQUE(correction_plan_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS correction_ticket_predecessors (
            correction_ticket_id TEXT NOT NULL REFERENCES tickets(id), ticket_id TEXT NOT NULL REFERENCES tickets(id),
            accepted_commit_sha TEXT NOT NULL, PRIMARY KEY(correction_ticket_id, ticket_id)
        );
        """)
        # Unique indexes are re-runnable so ledgers migrated before the
        # supplemental-correction schema still gain durable collision guards.
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_supplemental_correction_plans_ordinal ON supplemental_correction_plans(tranche_id, ordinal)")
        plan_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(decomposition_plans)")}
        for name in ("repository_identity", "repo_base_sha", "repo_snapshot_hash", "repo_snapshot_manifest_json"):
            if name not in plan_columns:
                self.connection.execute(f"ALTER TABLE decomposition_plans ADD COLUMN {name} TEXT")
        feature_contract_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(feature_contracts)")}
        for name in ("repository_identity", "repo_base_sha", "repo_snapshot_hash", "repo_snapshot_manifest_json", "predecessor_tranche_id", "predecessor_authority_kind", "predecessor_generation", "predecessor_final_integration_sha", "predecessor_evidence_hash", "admission_hash"):
            if name not in feature_contract_columns:
                self.connection.execute(f"ALTER TABLE feature_contracts ADD COLUMN {name} TEXT")
        tranche_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tranches)")}
        for name in ("title", "objective", "criterion_ids_json"):
            if name not in tranche_columns:
                self.connection.execute(f"ALTER TABLE tranches ADD COLUMN {name} TEXT")
        self.connection.executescript("""
            CREATE TRIGGER IF NOT EXISTS feature_contracts_immutable_update
            BEFORE UPDATE ON feature_contracts BEGIN SELECT RAISE(ABORT, 'feature contracts are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS feature_contracts_immutable_delete
            BEFORE DELETE ON feature_contracts BEGIN SELECT RAISE(ABORT, 'feature contracts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS feature_snapshot_revalidations_immutable_update
            BEFORE UPDATE ON feature_repository_snapshot_revalidations BEGIN SELECT RAISE(ABORT, 'snapshot revalidations are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS feature_snapshot_revalidations_immutable_delete
            BEFORE DELETE ON feature_repository_snapshot_revalidations BEGIN SELECT RAISE(ABORT, 'snapshot revalidations are append-only'); END;
        """)
        run_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(planning_runs)")}
        for name in ("repository_identity", "repo_snapshot_manifest_json"):
            if name not in run_columns:
                self.connection.execute(f"ALTER TABLE planning_runs ADD COLUMN {name} TEXT")
        for name in ("planner_role", "planner_provider", "planner_model", "planner_profile", "planner_routing_source", "planner_contract_hash"):
            if name not in run_columns:
                self.connection.execute(f"ALTER TABLE planning_runs ADD COLUMN {name} TEXT")
        reconciliation_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(failed_attempt_reconciliations)")}
        if "forensic_artifact_paths_json" not in reconciliation_columns:
            self.connection.execute("ALTER TABLE failed_attempt_reconciliations ADD COLUMN forensic_artifact_paths_json TEXT NOT NULL DEFAULT '[]'")
        # Pre-review-retry ledgers keyed invocations by stage. Preserve their
        # forensic rows while allowing multiple review transports for one
        # implementation attempt.
        invocation_indexes = self.connection.execute("PRAGMA index_list(model_invocations)").fetchall()
        legacy_stage_key = any(
            row["unique"] and row["name"] != "idx_model_invocations_one_started_review" and [part["name"] for part in self.connection.execute(f"PRAGMA index_info({row['name']})")] == ["ticket_id", "attempt_number", "stage"]
            for row in invocation_indexes
        )
        if legacy_stage_key:
            self.connection.executescript("""
            ALTER TABLE model_invocations RENAME TO model_invocations_legacy;
            CREATE TABLE model_invocations (
                invocation_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
                attempt_number INTEGER NOT NULL, stage TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                packet_hash TEXT NOT NULL, worktree_path TEXT NOT NULL, timeout_seconds INTEGER NOT NULL,
                started_at INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('started','completed','timeout','process_error','malformed_output')),
                completed_at INTEGER, duration_seconds REAL, error_json TEXT, model_artifact TEXT
            );
            INSERT INTO model_invocations SELECT * FROM model_invocations_legacy;
            DROP TABLE model_invocations_legacy;
            CREATE INDEX IF NOT EXISTS idx_model_invocations_incomplete ON model_invocations(status, ticket_id);
            CREATE INDEX IF NOT EXISTS idx_model_invocations_attempt_stage ON model_invocations(ticket_id, attempt_number, stage, started_at);
            """)
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_model_invocations_one_started_review ON model_invocations(ticket_id, attempt_number, stage) WHERE stage='review' AND status='started'")
        self.connection.executescript("""
        CREATE TRIGGER IF NOT EXISTS hermes_execution_reconciliations_immutable_update
        BEFORE UPDATE ON hermes_execution_reconciliations BEGIN SELECT RAISE(ABORT, 'Hermes execution reconciliations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS hermes_execution_reconciliations_immutable_delete
        BEFORE DELETE ON hermes_execution_reconciliations BEGIN SELECT RAISE(ABORT, 'Hermes execution reconciliations are immutable'); END;
        """)
        # Phase 2 is additive: preserve Phase 1 ledgers already created.
        ticket_columns = {
            "criterion_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "primary_symbol": "TEXT",
            "allowed_files_json": "TEXT NOT NULL DEFAULT '[]'",
            "create_files_json": "TEXT NOT NULL DEFAULT '[]'",
            "new_test_files_json": "TEXT NOT NULL DEFAULT '[]'",
            "forbidden_changes_json": "TEXT NOT NULL DEFAULT '[]'",
            "patch_budget_json": "TEXT NOT NULL DEFAULT '{}'",
            "verification_json": "TEXT NOT NULL DEFAULT '{}'",
            "risk": "TEXT",
            "review_required": "INTEGER NOT NULL DEFAULT 1",
            "max_attempts": "INTEGER NOT NULL DEFAULT 2",
            "dependencies_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        existing = {row["name"] for row in self.connection.execute("PRAGMA table_info(tickets)")}
        for name, definition in ticket_columns.items():
            if name not in existing:
                self.connection.execute(f"ALTER TABLE tickets ADD COLUMN {name} {definition}")
        if "depth" not in existing:
            self.connection.execute("ALTER TABLE tickets ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")
        feature_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(features)")}
        for name, definition in {"risk": "TEXT", "architecture_version": "INTEGER", "integration_base_sha": "TEXT"}.items():
            if name not in feature_columns:
                self.connection.execute(f"ALTER TABLE features ADD COLUMN {name} {definition}")
        tranche_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tranches)")}
        for name, definition in {"base_sha": "TEXT", "integration_commands_json": "TEXT NOT NULL DEFAULT '[]'"}.items():
            if name not in tranche_columns:
                self.connection.execute(f"ALTER TABLE tranches ADD COLUMN {name} {definition}")
        attempt_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(attempts)")}
        for name in ("base_sha", "branch", "worktree_path", "pre_diff_hash", "post_diff_hash", "accepted_commit_sha"):
            if name not in attempt_columns:
                self.connection.execute(f"ALTER TABLE attempts ADD COLUMN {name} TEXT")
        comment_columns={row["name"] for row in self.connection.execute("PRAGMA table_info(evidence_comment_outbox)")}
        for name,definition in {"lease_expires_at":"INTEGER","next_attempt_at":"INTEGER","terminal_owner":"TEXT"}.items():
            if name not in comment_columns: self.connection.execute(f"ALTER TABLE evidence_comment_outbox ADD COLUMN {name} {definition}")
        release_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(native_dependency_releases)")}
        if "routing_authority_json" not in release_columns:
            self.connection.execute("ALTER TABLE native_dependency_releases ADD COLUMN routing_authority_json TEXT NOT NULL DEFAULT '{}'")
        revalidation_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(native_dependency_release_revalidations)")}
        for name, definition in (("revalidation_event_id", "INTEGER"), ("event_key", "TEXT"), ("evidence_hash", "TEXT"), ("approval_document_json", "TEXT"), ("approval_document_hash", "TEXT"), ("detached_signature", "TEXT"), ("signer_fingerprint", "TEXT"), ("snapshot_schema_version", "INTEGER NOT NULL DEFAULT 1")):
            if name not in revalidation_columns:
                self.connection.execute(f"ALTER TABLE native_dependency_release_revalidations ADD COLUMN {name} {definition}")
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_native_release_revalidation_document ON native_dependency_release_revalidations(ticket_id, approval_document_hash) WHERE approval_document_hash IS NOT NULL")
        activation_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(native_release_activation_intents)")}
        for name, definition in {
            "approval_document_json": "TEXT", "approval_document_hash": "TEXT", "detached_signature": "TEXT",
            "signer_fingerprint": "TEXT", "board_path": "TEXT", "board_dev": "INTEGER", "board_ino": "INTEGER",
        }.items():
            if name not in activation_columns:
                self.connection.execute(f"ALTER TABLE native_release_activation_intents ADD COLUMN {name} {definition}")
        activation_trigger = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='native_release_activation_intents_immutable_identity'").fetchone()
        if activation_trigger is not None and "BEFORE UPDATE OF" in str(activation_trigger["sql"]):
            self.connection.executescript("""
        DROP TRIGGER IF EXISTS native_release_activation_intents_immutable_identity;
        CREATE TRIGGER native_release_activation_intents_immutable_identity
        BEFORE UPDATE ON native_release_activation_intents
        WHEN OLD.ticket_id IS NOT NEW.ticket_id OR OLD.revalidation_id IS NOT NEW.revalidation_id
          OR OLD.external_task_id IS NOT NEW.external_task_id OR OLD.pre_activation_snapshot_hash IS NOT NEW.pre_activation_snapshot_hash
          OR OLD.implementation_profile IS NOT NEW.implementation_profile OR OLD.repository_identity IS NOT NEW.repository_identity
          OR OLD.canonical_worktree_path IS NOT NEW.canonical_worktree_path OR OLD.branch IS NOT NEW.branch OR OLD.base_sha IS NOT NEW.base_sha
          OR OLD.operator_id IS NOT NEW.operator_id OR OLD.reason IS NOT NEW.reason OR OLD.activation_marker IS NOT NEW.activation_marker
          OR OLD.created_at IS NOT NEW.created_at OR OLD.approval_document_json IS NOT NEW.approval_document_json
          OR OLD.approval_document_hash IS NOT NEW.approval_document_hash OR OLD.detached_signature IS NOT NEW.detached_signature
          OR OLD.signer_fingerprint IS NOT NEW.signer_fingerprint OR OLD.board_path IS NOT NEW.board_path
          OR OLD.board_dev IS NOT NEW.board_dev OR OLD.board_ino IS NOT NEW.board_ino
        BEGIN SELECT RAISE(ABORT, 'native release activation intent identity is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS native_release_activation_intents_no_delete
        BEFORE DELETE ON native_release_activation_intents BEGIN SELECT RAISE(ABORT, 'native release activation intents are append-only'); END;
        """)
        binding_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_bindings)")}
        for name in ("operator_signer_fingerprint", "operator_authority_hash"):
            if name not in binding_columns:
                self.connection.execute(f"ALTER TABLE runtime_bindings ADD COLUMN {name} TEXT")
        enrollment_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_signer_enrollment_intents)")}
        for name, definition in {
            "document_json": "TEXT", "document_hash": "TEXT", "detached_signature": "TEXT",
            "ledger_identity": "TEXT", "old_config_identity_json": "TEXT", "nonce": "TEXT", "config_identity_json": "TEXT",
        }.items():
            if name not in enrollment_columns:
                self.connection.execute(f"ALTER TABLE runtime_signer_enrollment_intents ADD COLUMN {name} {definition}")
        evidence_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_signer_enrollments)")}
        for name, definition in {"document_json": "TEXT", "document_hash": "TEXT", "detached_signature": "TEXT", "config_identity_json": "TEXT"}.items():
            if name not in evidence_columns:
                self.connection.execute(f"ALTER TABLE runtime_signer_enrollments ADD COLUMN {name} {definition}")
        trigger = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='runtime_signer_enrollment_intents_immutable_identity'").fetchone()
        if trigger is not None and "NEW.status IN ('config_written','finalized')" not in str(trigger["sql"]):
            self.connection.executescript("""
            DROP TRIGGER runtime_signer_enrollment_intents_immutable_identity;
            DROP TRIGGER runtime_signer_enrollment_intents_status_transition;
            DROP TRIGGER runtime_signer_enrollment_intents_no_delete;
            """)
        self.connection.executescript("""
        CREATE TRIGGER IF NOT EXISTS runtime_signer_enrollment_intents_immutable_identity
        BEFORE UPDATE ON runtime_signer_enrollment_intents
        WHEN OLD.enrollment_key IS NOT NEW.enrollment_key OR OLD.operator_id IS NOT NEW.operator_id
         OR OLD.reason IS NOT NEW.reason OR OLD.ticket_ids_json IS NOT NEW.ticket_ids_json
         OR OLD.public_key_fingerprint IS NOT NEW.public_key_fingerprint OR OLD.authority_hash IS NOT NEW.authority_hash
         OR OLD.old_config_hash IS NOT NEW.old_config_hash OR OLD.new_config_hash IS NOT NEW.new_config_hash
         OR OLD.new_config_bytes IS NOT NEW.new_config_bytes
         OR OLD.selected_bindings_json IS NOT NEW.selected_bindings_json OR OLD.created_at IS NOT NEW.created_at
         OR OLD.document_json IS NOT NEW.document_json OR OLD.document_hash IS NOT NEW.document_hash
         OR OLD.detached_signature IS NOT NEW.detached_signature OR OLD.ledger_identity IS NOT NEW.ledger_identity
         OR OLD.old_config_identity_json IS NOT NEW.old_config_identity_json OR OLD.nonce IS NOT NEW.nonce
         OR (OLD.config_identity_json IS NOT NEW.config_identity_json AND NOT (OLD.config_identity_json IS NULL AND NEW.config_identity_json IS NOT NULL AND NEW.status IN ('config_written','finalized')))
        BEGIN SELECT RAISE(ABORT, 'runtime signer enrollment intent identity is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS runtime_signer_enrollment_intents_status_transition
        BEFORE UPDATE OF status ON runtime_signer_enrollment_intents
        WHEN NOT ((OLD.status='pending_config' AND NEW.status IN ('pending_config','config_written','invalidated'))
               OR (OLD.status='config_written' AND NEW.status IN ('config_written','finalized','invalidated'))
               OR (OLD.status='finalized' AND NEW.status IN ('finalized','invalidated'))
               OR (OLD.status='invalidated' AND NEW.status='invalidated'))
        BEGIN SELECT RAISE(ABORT, 'invalid runtime signer enrollment intent transition'); END;
        CREATE TRIGGER IF NOT EXISTS runtime_signer_enrollment_intents_no_delete
        BEFORE DELETE ON runtime_signer_enrollment_intents BEGIN SELECT RAISE(ABORT, 'runtime signer enrollment intents are append-only'); END;
        """)
        self.connection.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_native_release_revalidation_event
            ON native_dependency_release_revalidations(revalidation_event_id)
            WHERE revalidation_event_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_native_release_revalidation_event_key
            ON native_dependency_release_revalidations(event_key)
            WHERE event_key IS NOT NULL;
        CREATE TRIGGER IF NOT EXISTS native_release_revalidation_events_immutable_update
        BEFORE UPDATE ON events
        WHEN OLD.event_type='native_dependency_release_revalidated'
        BEGIN SELECT RAISE(ABORT, 'native release revalidation events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS native_release_revalidation_events_immutable_delete
        BEFORE DELETE ON events
        WHEN OLD.event_type='native_dependency_release_revalidated'
        BEGIN SELECT RAISE(ABORT, 'native release revalidation events are immutable'); END;
        """)
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_model_calls_reservation ON model_calls(reservation_id)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS review_retry_authorizations (authorization_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), attempt_number INTEGER NOT NULL, candidate_fingerprint TEXT NOT NULL, failed_invocation_id TEXT NOT NULL REFERENCES model_invocations(invocation_id), operator_id TEXT NOT NULL, authorized_at INTEGER NOT NULL, consumed_invocation_id TEXT REFERENCES model_invocations(invocation_id), consumed_at INTEGER, UNIQUE(ticket_id, attempt_number, failed_invocation_id))")
        candidate_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(review_candidates)")}
        if "historical_provenance_json" not in candidate_columns: self.connection.execute("ALTER TABLE review_candidates ADD COLUMN historical_provenance_json TEXT NOT NULL DEFAULT '{}'")
        retry_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(review_retry_authorizations)")}
        for name, definition in {"consumed_invocation_id": "TEXT", "consumed_at": "INTEGER"}.items():
            if name not in retry_columns: self.connection.execute(f"ALTER TABLE review_retry_authorizations ADD COLUMN {name} {definition}")
        projection_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(board_projection_outbox)")}
        for name, definition in {"payload_json": "TEXT NOT NULL DEFAULT '{}'", "operation": "TEXT NOT NULL DEFAULT 'set_state'", "external_task_id": "TEXT", "lease_owner": "TEXT", "lease_expires_at": "INTEGER", "next_attempt_at": "INTEGER", "attempt_count": "INTEGER NOT NULL DEFAULT 0", "last_error": "TEXT", "terminal_error": "TEXT", "superseded_at": "INTEGER", "superseded_by_event_id": "INTEGER", "supersession_reason": "TEXT"}.items():
            if name not in projection_columns:
                self.connection.execute(f"ALTER TABLE board_projection_outbox ADD COLUMN {name} {definition}")
        scheduler_claim_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(scheduler_stage_claims)")}
        for name, definition in {
            "side_effect_started_at": "INTEGER",
            "side_effect_completed_at": "INTEGER",
            "finalized_at": "INTEGER",
            "candidate_identity_json": "TEXT",
        }.items():
            if name not in scheduler_claim_columns:
                self.connection.execute(f"ALTER TABLE scheduler_stage_claims ADD COLUMN {name} {definition}")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_state_projection_claimable ON board_projection_outbox(operation, acknowledged_at, superseded_at, next_attempt_at, lease_expires_at, queued_at)")
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            (self._now(),),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO controller_state(id, paused, updated_at) VALUES (1, 0, ?)",
            (self._now(),),
        )

    @staticmethod
    def _now() -> int:
        return int(time.time())

    def _transaction(self) -> Iterator[sqlite3.Connection]:
        class Transaction:
            def __init__(self, owner: "Ledger", conn: sqlite3.Connection) -> None:
                self.owner = owner
                self.conn = conn
            def __enter__(self) -> sqlite3.Connection:
                self.owner._lock.acquire()
                self.conn.execute("BEGIN IMMEDIATE")
                return self.conn
            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                try:
                    self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    self.owner._lock.release()
                return False
        return Transaction(self, self.connection)  # type: ignore[return-value]

    def create_ticket(self, *, title: str, state: CanonicalState = CanonicalState.DRAFT, external_id: str | None = None, contract: dict[str, Any] | None = None) -> str:
        ticket_id = uuid.uuid4().hex
        now = self._now()
        with self._transaction() as conn:
            contract = contract or {}
            conn.execute(
                "INSERT INTO tickets(id, external_id, title, objective, criterion_ids_json, primary_symbol, allowed_files_json, create_files_json, new_test_files_json, forbidden_changes_json, patch_budget_json, verification_json, risk, review_required, max_attempts, dependencies_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, external_id, title, contract.get("objective"), json.dumps(contract.get("criterion_ids", [])), contract.get("primary_symbol"), json.dumps(contract.get("allowed_files", [])), json.dumps(contract.get("create_files", [])), json.dumps(contract.get("new_test_files", [])), json.dumps(contract.get("forbidden_changes", [])), json.dumps(contract.get("patch_budget", {})), json.dumps(contract.get("verification", {})), contract.get("risk"), int(contract.get("review_required", True)), contract.get("max_attempts", 2), json.dumps(contract.get("dependencies", [])), state.value, now, now),
            )
        return ticket_id

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return ticket_id

    def admit_imported_ticket(
        self, *, title: str, external_id: str, contract: dict[str, Any],
        repository_path: str, starting_sha: str, feature_id: str | None = None,
        tranche_id: str | None = None, operator_signer_fingerprint: str | None = None,
        operator_authority_hash: str | None = None,
    ) -> str:
        """Atomically admit one external ticket and bind its immutable runtime identity."""
        if not external_id or not repository_path or not starting_sha:
            raise ValueError("imported ticket identity is incomplete")
        requested_binding = (
            str(repository_path), str(starting_sha), str(starting_sha), 1,
            operator_signer_fingerprint, operator_authority_hash,
        )
        now = self._now()
        with self._transaction() as conn:
            resolved_feature = feature_id
            if tranche_id is not None:
                tranche = conn.execute("SELECT id, feature_id FROM tranches WHERE id=?", (tranche_id,)).fetchone()
                if tranche is None:
                    raise ValueError("card tranche_id does not exist")
                tranche_feature = str(tranche["feature_id"])
                if resolved_feature is not None and resolved_feature != tranche_feature:
                    raise ValueError("card tranche_id does not belong to feature_id")
                resolved_feature = resolved_feature or tranche_feature
            if resolved_feature is not None:
                feature = conn.execute("SELECT id FROM features WHERE id=?", (resolved_feature,)).fetchone()
                if feature is None:
                    raise ValueError("card feature_id does not exist")

            existing = conn.execute("SELECT * FROM tickets WHERE external_id=?", (external_id,)).fetchone()
            if existing is None:
                ticket_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO tickets(id, external_id, feature_id, tranche_id, title, objective, criterion_ids_json, primary_symbol, allowed_files_json, create_files_json, new_test_files_json, forbidden_changes_json, patch_budget_json, verification_json, risk, review_required, max_attempts, dependencies_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ticket_id, external_id, resolved_feature, tranche_id, title,
                        contract.get("objective"), json.dumps(contract.get("criterion_ids", [])), contract.get("primary_symbol"),
                        json.dumps(contract.get("allowed_files", [])), json.dumps(contract.get("create_files", [])),
                        json.dumps(contract.get("new_test_files", [])), json.dumps(contract.get("forbidden_changes", [])),
                        json.dumps(contract.get("patch_budget", {})), json.dumps(contract.get("verification", {})),
                        contract.get("risk"), int(contract.get("review_required", True)), contract.get("max_attempts", 2),
                        json.dumps(contract.get("dependencies", [])), CanonicalState.READY_LOCAL.value, now, now,
                    ),
                )
            else:
                ticket_id = str(existing["id"])
                existing_feature = None if existing["feature_id"] is None else str(existing["feature_id"])
                existing_tranche = None if existing["tranche_id"] is None else str(existing["tranche_id"])
                if existing_feature not in {None, resolved_feature} or existing_tranche not in {None, tranche_id}:
                    raise ValueError("card feature/tranche identity conflicts with existing ticket")
                if (existing_feature is not None and resolved_feature is None) or (existing_tranche is not None and tranche_id is None):
                    raise ValueError("card feature/tranche identity conflicts with existing ticket")
                conn.execute(
                    "UPDATE tickets SET feature_id=COALESCE(feature_id,?), tranche_id=COALESCE(tranche_id,?) WHERE id=?",
                    (resolved_feature, tranche_id, ticket_id),
                )

            binding = conn.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
            if binding is None:
                conn.execute(
                    "INSERT INTO runtime_bindings(ticket_id, repository_path, starting_sha, canonical_sha, ownership_verified, created_at, operator_signer_fingerprint, operator_authority_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ticket_id, requested_binding[0], requested_binding[1], requested_binding[2], requested_binding[3], now, requested_binding[4], requested_binding[5]),
                )
            else:
                actual = (
                    str(binding["repository_path"]), str(binding["starting_sha"]), str(binding["canonical_sha"]),
                    int(binding["ownership_verified"]), binding["operator_signer_fingerprint"], binding["operator_authority_hash"],
                )
                if actual != requested_binding:
                    raise ValueError("conflicting runtime binding")
            return ticket_id

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise KeyError(ticket_id)
        return dict(row)

    def events_for(self, ticket_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM events WHERE entity_type = 'ticket' AND entity_id = ? ORDER BY id", (ticket_id,)).fetchall()
        return [dict(row) for row in rows]

    def _append_event(self, conn: sqlite3.Connection, *, entity_type: str, entity_id: str, event_type: str, actor_id: str, from_state: str | None = None, to_state: str | None = None, payload: dict[str, Any] | None = None) -> int:
        cursor = conn.execute(
            "INSERT INTO events(entity_type, entity_id, event_type, from_state, to_state, actor_type, actor_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, 'controller', ?, ?, ?)",
            (entity_type, entity_id, event_type, from_state, to_state, actor_id, json.dumps(payload or {}, sort_keys=True), self._now()),
        )
        return int(cursor.lastrowid)

    def _inject_failure(self, point: str) -> None:
        """Deterministic test-only fault seam."""
        if self.failure_injector is not None:
            self.failure_injector(point)

    def _resolve_external_task_id_in_transaction(self, conn: sqlite3.Connection, ticket_id: str) -> str:
        """Resolve the sole board identity; generated tickets never fall back to their ledger ID."""
        ticket = conn.execute("SELECT id, external_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if ticket is None: raise KeyError(ticket_id)
        generated = conn.execute(
            "SELECT e.id FROM events e WHERE e.entity_type='ticket' AND e.entity_id=? "
            "AND e.event_type IN ('generated_microticket_created','generated_microticket_projection_recovered')", (ticket_id,)
        ).fetchall()
        if not generated:
            # Pre-generated/imported tickets historically persist their board ID on
            # tickets.external_id. The final fallback keeps legacy fake/internal
            # board fixtures working; it is never available to generated tickets.
            return str(ticket["external_id"] or ticket_id)
        rows = conn.execute(
            "SELECT b.external_task_id FROM events e JOIN board_projection_outbox b "
            "ON b.ticket_id=e.entity_id AND b.event_id=e.id "
            "WHERE e.entity_type='ticket' AND e.entity_id=? "
            "AND e.event_type IN ('generated_microticket_created','generated_microticket_projection_recovered') "
            "AND b.operation='create_microticket' AND b.acknowledged_at IS NOT NULL AND b.superseded_at IS NULL",
            (ticket_id,),
        ).fetchall()
        ids = {str(row["external_task_id"]) for row in rows if isinstance(row["external_task_id"], str) and row["external_task_id"]}
        if len(ids) != 1:
            raise ValueError("external_projection_identity_missing" if not ids else "external_projection_identity_conflict")
        external_task_id = ids.pop()
        if ticket["external_id"] is not None and str(ticket["external_id"]) != external_task_id:
            raise ValueError("external_projection_identity_conflict")
        return external_task_id

    def resolve_external_task_id(self, ticket_id: str) -> str:
        """Read the deterministic board target without board scraping or mutation."""
        return self._resolve_external_task_id_in_transaction(self.connection, ticket_id)

    @staticmethod
    def _comment_payload(ticket_id: str, state: str, operation_id: str, evidence: str) -> str:
        import re
        safe = re.sub(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=[REDACTED]", evidence)[:800]
        return (f"Local-first ticket {ticket_id} | state={state} | {safe}\n<!-- local-first-comment:{operation_id} -->")[:1000]

    @classmethod
    def _is_projectable_state_event(cls, event: sqlite3.Row) -> bool:
        return (event["entity_type"] == "ticket" and event["event_type"] in {"state_transition", "review_reconciliation_authorized"}
                and event["to_state"] is not None and str(event["to_state"]) in cls._PROJECTABLE_STATES)

    def _supersede_older_state_projections_in_transaction(self, conn: sqlite3.Connection, ticket_id: str, current_event_id: int) -> int:
        """Retain obsolete state intents as audit history, but make them ineligible."""
        return conn.execute(
            "UPDATE board_projection_outbox SET superseded_at=?, superseded_by_event_id=?, supersession_reason=?, "
            "lease_owner=NULL, lease_expires_at=NULL WHERE ticket_id=? AND operation='set_state' "
            "AND acknowledged_at IS NULL AND superseded_at IS NULL AND event_id < ?",
            (self._now(), current_event_id, "newer_authoritative_state_event", ticket_id, current_event_id),
        ).rowcount

    def _enqueue_projection_bundle_in_transaction(self, conn: sqlite3.Connection, *, ticket_id: str, event_id: int, evidence: str, state_payload: dict[str, Any] | None = None, external_task_id: str | None = None) -> dict[str, Any]:
        ticket = conn.execute("SELECT id, external_id, state FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if ticket is None: raise KeyError(ticket_id)
        event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if event is None: raise KeyError(f"event {event_id}")
        if event["entity_id"] != ticket_id or not self._is_projectable_state_event(event):
            raise ValueError("event is not a projectable ticket state transition")
        state = str(event["to_state"]); state_key = f"ticket-event:{event_id}"
        state_payload_json = json.dumps(state_payload or {}, sort_keys=True, separators=(",", ":"))
        resolved_task_id = self._resolve_external_task_id_in_transaction(conn, ticket_id)
        if external_task_id is not None and external_task_id != resolved_task_id:
            raise ValueError("external_projection_identity_conflict")
        existing_state = conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        if existing_state is not None:
            if (existing_state["state"], existing_state["idempotency_key"], existing_state["payload_json"]) != (state, state_key, state_payload_json):
                raise ValueError("projection state intent conflicts with persisted intent")
            if existing_state["external_task_id"] not in {None, resolved_task_id}:
                raise ValueError("external_projection_identity_conflict")
        else:
            conn.execute("INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,external_task_id) VALUES (?,?,?,?,?,?,?)", (ticket_id, event_id, state, state_payload_json, state_key, self._now(), resolved_task_id))
        # This is intentionally inside the transition/enqueue transaction.  It
        # never touches evidence comments, create projections, or acknowledgements.
        self._supersede_older_state_projections_in_transaction(conn, ticket_id, event_id)
        self._inject_failure("after_state_intent")
        comment_key = f"evidence-comment:{ticket_id}:{event_id}"; comment_id = hashlib.sha256(comment_key.encode()).hexdigest()[:32]
        payload = self._comment_payload(ticket_id, state, comment_id, evidence)
        task_id = resolved_task_id
        existing_comment = conn.execute("SELECT * FROM evidence_comment_outbox WHERE operation_id=?", (comment_id,)).fetchone()
        if existing_comment is not None:
            if (existing_comment["ticket_id"], int(existing_comment["event_id"]), existing_comment["external_task_id"], existing_comment["idempotency_key"], existing_comment["payload"]) != (ticket_id, event_id, task_id, comment_key, payload):
                raise ValueError("evidence comment intent conflicts with persisted intent")
        else:
            conn.execute("INSERT INTO evidence_comment_outbox(operation_id,ticket_id,event_id,external_task_id,operation_kind,idempotency_key,payload,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (comment_id, ticket_id, event_id, task_id, "evidence_comment", comment_key, payload, "pending", self._now(), self._now()))
        self._inject_failure("after_comment_intent")
        return {"state": dict(conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()), "comment": dict(conn.execute("SELECT * FROM evidence_comment_outbox WHERE operation_id=?", (comment_id,)).fetchone())}

    def enqueue_projection_bundle(self, ticket_id: str, event_id: int, evidence: str, *, state_payload: dict[str, Any] | None = None, external_task_id: str | None = None) -> dict[str, Any]:
        """Persist state and evidence-comment intents as one SQLite transaction."""
        with self._transaction() as conn:
            result = self._enqueue_projection_bundle_in_transaction(conn, ticket_id=ticket_id, event_id=event_id, evidence=evidence, state_payload=state_payload, external_task_id=external_task_id)
        return result

    def projection_reconciliation_report(self) -> list[dict[str, Any]]:
        """Read-only report for legacy events or intents missing their pair."""
        rows = self.connection.execute("""
            SELECT 'state_without_comment' AS problem, b.ticket_id, b.event_id
            FROM board_projection_outbox b LEFT JOIN evidence_comment_outbox c ON c.ticket_id=b.ticket_id AND c.event_id=b.event_id
            WHERE c.operation_id IS NULL
            UNION ALL
            SELECT 'comment_without_state', c.ticket_id, c.event_id
            FROM evidence_comment_outbox c LEFT JOIN board_projection_outbox b ON b.ticket_id=c.ticket_id AND b.event_id=c.event_id
            WHERE b.ticket_id IS NULL
            UNION ALL
            SELECT 'projectable_event_without_bundle', e.entity_id, e.id
            FROM events e LEFT JOIN board_projection_outbox b ON b.ticket_id=e.entity_id AND b.event_id=e.id
            LEFT JOIN evidence_comment_outbox c ON c.ticket_id=e.entity_id AND c.event_id=e.id
            WHERE e.entity_type='ticket' AND e.event_type IN ('state_transition','review_reconciliation_authorized') AND e.to_state IS NOT NULL AND e.to_state IN ('needs_architecture','ready_local','accepted','needs_human_test','needs_checkpoint','needs_triage','blocked','done','rejected','reverted','local_review') AND (b.ticket_id IS NULL OR c.operation_id IS NULL)
        """).fetchall()
        return [dict(row) for row in rows]

    def transition(self, ticket_id: str, target: CanonicalState, *, actor_id: str = "controller", payload: dict[str, Any] | None = None) -> None:
        with self._transaction() as conn:
            row = conn.execute("SELECT state FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if row is None:
                raise KeyError(ticket_id)
            current = CanonicalState(row["state"])
            validate_transition(current, target)
            now = self._now()
            changed = conn.execute("UPDATE tickets SET state = ?, updated_at = ? WHERE id = ? AND state = ?", (target.value, now, ticket_id, current.value))
            if changed.rowcount != 1:
                raise RuntimeError("ticket changed concurrently")
            event_id = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="state_transition", actor_id=actor_id, from_state=current.value, to_state=target.value, payload=payload)
            self._inject_failure("after_event_creation")
            if target.value in self._PROJECTABLE_STATES:
                self._enqueue_projection_bundle_in_transaction(conn, ticket_id=ticket_id, event_id=event_id, evidence=f"state={target.value}", state_payload=payload)

    def claim_ticket(self, owner: str, *, lease_seconds: int, now: int | None = None) -> str | None:
        now = self._now() if now is None else now
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id = 1").fetchone()
            if paused is None or paused["paused"]:
                return None
            row = conn.execute("SELECT id FROM tickets WHERE state = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?) ORDER BY created_at, id LIMIT 1", (CanonicalState.READY_LOCAL.value, now)).fetchone()
            if row is None:
                return None
            ticket_id = str(row["id"])
            changed = conn.execute("UPDATE tickets SET state = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ? WHERE id = ? AND state = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)", (CanonicalState.IMPLEMENTING.value, owner, now + lease_seconds, now, ticket_id, CanonicalState.READY_LOCAL.value, now))
            if changed.rowcount != 1:
                return None
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="lease_claimed", actor_id=owner, from_state=CanonicalState.READY_LOCAL.value, to_state=CanonicalState.IMPLEMENTING.value, payload={"lease_expires_at": now + lease_seconds})
            return ticket_id

    def recover_expired_leases(self, *, now: int | None = None) -> list[str]:
        now = self._now() if now is None else now
        recovered: list[str] = []
        with self._transaction() as conn:
            rows = conn.execute("SELECT id, lease_owner FROM tickets WHERE state = ? AND lease_expires_at <= ? ORDER BY id", (CanonicalState.IMPLEMENTING.value, now)).fetchall()
            for row in rows:
                ticket_id = str(row["id"])
                conn.execute("UPDATE tickets SET state = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?", (CanonicalState.READY_LOCAL.value, now, ticket_id))
                self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="lease_recovered", actor_id="recovery", from_state=CanonicalState.IMPLEMENTING.value, to_state=CanonicalState.READY_LOCAL.value, payload={"expired_owner": row["lease_owner"]})
                recovered.append(ticket_id)
        return recovered

    def record_stage(self, ticket_id: str, attempt_number: int, stage: str, idempotency_key: str) -> bool:
        with self._transaction() as conn:
            try:
                conn.execute("INSERT INTO stage_runs(ticket_id, attempt_number, stage, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?)", (ticket_id, attempt_number, stage, idempotency_key, self._now()))
            except sqlite3.IntegrityError:
                return False
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="stage_started", actor_id="controller", payload={"attempt_number": attempt_number, "stage": stage, "idempotency_key": idempotency_key})
            return True

    def stage_count(self, ticket_id: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM stage_runs WHERE ticket_id = ?", (ticket_id,)).fetchone()[0])

    def ensure_attempt(self, ticket_id: str, attempt_number: int) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO attempts(ticket_id, attempt_number, created_at) VALUES (?, ?, ?)",
                (ticket_id, attempt_number, self._now()),
            )

    def attempt_count(self, ticket_id: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM attempts WHERE ticket_id = ?", (ticket_id,)).fetchone()[0])

    def next_attempt_number(self, ticket_id: str) -> int:
        """Historical attempts, including retired failures, are never reused."""
        return int(self.connection.execute("SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts WHERE ticket_id=?", (ticket_id,)).fetchone()[0])

    def hermes_execution_reconciliation(self, external_task_id: str, hermes_run_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM hermes_execution_reconciliations WHERE external_task_id=? AND hermes_run_id=?",
            (external_task_id, hermes_run_id),
        ).fetchone()
        return dict(row) if row else None

    def record_hermes_execution_reconciliation(
        self, *, external_task_id: str, hermes_run_id: int, ticket_id: str, attempt_number: int,
        run_status: str, run_outcome: str | None, session_id: str | None, branch_name: str | None,
        workspace_path: str, base_sha: str, head_sha: str, diff_hash: str, artifact_path: str,
        snapshot_hash: str,
    ) -> dict[str, Any]:
        requested = (
            external_task_id, int(hermes_run_id), ticket_id, int(attempt_number), run_status,
            run_outcome, session_id, branch_name, workspace_path, base_sha, head_sha, diff_hash,
            artifact_path, snapshot_hash,
        )
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM hermes_execution_reconciliations WHERE external_task_id=? AND hermes_run_id=?",
                (external_task_id, hermes_run_id),
            ).fetchone()
            if existing is not None:
                actual = tuple(existing[key] for key in (
                    "external_task_id", "hermes_run_id", "ticket_id", "attempt_number", "run_status",
                    "run_outcome", "session_id", "branch_name", "workspace_path", "base_sha", "head_sha",
                    "diff_hash", "artifact_path", "snapshot_hash",
                ))
                if actual != requested:
                    raise RuntimeError("hermes_execution_reconciliation_conflict")
                return dict(existing)
            ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if ticket is None:
                raise RuntimeError("hermes_execution_ticket_binding_conflict")
            if self._resolve_external_task_id_in_transaction(conn, ticket_id) != external_task_id:
                raise RuntimeError("hermes_execution_ticket_binding_conflict")
            if ticket["state"] not in (CanonicalState.READY_LOCAL.value, CanonicalState.IMPLEMENTING.value, CanonicalState.REPAIRING.value):
                raise RuntimeError("hermes_execution_ticket_state_conflict")
            attempt = conn.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            empty_hash = hashlib.sha256(b"").hexdigest()
            if attempt is None:
                conn.execute(
                    "INSERT INTO attempts(ticket_id,attempt_number,base_sha,branch,worktree_path,pre_diff_hash,post_diff_hash,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (ticket_id, attempt_number, base_sha, branch_name or f"hermes-run-{hermes_run_id}", workspace_path, empty_hash, diff_hash, self._now()),
                )
            else:
                actual_attempt = (attempt["base_sha"], attempt["worktree_path"], attempt["post_diff_hash"])
                if actual_attempt != (base_sha, workspace_path, diff_hash):
                    raise RuntimeError("hermes_execution_attempt_conflict")
            stage = conn.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='implementation'", (ticket_id, attempt_number)).fetchone()
            if stage is None:
                conn.execute(
                    "INSERT INTO model_stage_artifacts(ticket_id,attempt_number,stage,purpose,adapter,request_hash,response_artifact,worktree_path,base_sha,diff_hash,completed_at,status) VALUES (?,?,'implementation','implementation','hermes-dispatch',?,?,?,?,?,?,'completed')",
                    (ticket_id, attempt_number, snapshot_hash, artifact_path, workspace_path, base_sha, diff_hash, self._now()),
                )
            elif (stage["adapter"], stage["request_hash"], stage["response_artifact"], stage["worktree_path"], stage["base_sha"], stage["diff_hash"]) != ("hermes-dispatch", snapshot_hash, artifact_path, workspace_path, base_sha, diff_hash):
                raise RuntimeError("hermes_execution_implementation_stage_conflict")
            if ticket["state"] != CanonicalState.IMPLEMENTING.value:
                conn.execute("UPDATE tickets SET state=?,updated_at=? WHERE id=?", (CanonicalState.IMPLEMENTING.value, self._now(), ticket_id))
            conn.execute(
                "INSERT INTO hermes_execution_reconciliations(external_task_id,hermes_run_id,ticket_id,attempt_number,run_status,run_outcome,session_id,branch_name,workspace_path,base_sha,head_sha,diff_hash,artifact_path,snapshot_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*requested, self._now()),
            )
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="hermes_execution_reconciled", actor_id="controller", payload={"external_task_id": external_task_id, "hermes_run_id": hermes_run_id, "attempt_number": attempt_number, "snapshot_hash": snapshot_hash, "diff_hash": diff_hash})
            return dict(conn.execute("SELECT * FROM hermes_execution_reconciliations WHERE external_task_id=? AND hermes_run_id=?", (external_task_id, hermes_run_id)).fetchone())

    def record_manual_implementation_adoption(
        self, *, ticket_id: str, attempt_number: int, operator_id: str, reason: str,
        branch: str, workspace_path: str, base_sha: str, diff_hash: str,
        artifact_path: str, artifact_sha256: str,
    ) -> dict[str, Any]:
        """Durably adopt an existing implementation without inventing model/worker provenance."""
        if not operator_id.strip() or not reason.strip():
            raise ValueError("manual adoption requires operator identity and reason")
        if attempt_number < 1 or not all(isinstance(value, str) and value for value in (branch, workspace_path, base_sha, diff_hash, artifact_path, artifact_sha256)):
            raise ValueError("manual adoption identity is incomplete")
        detail = json.dumps({
            "schema": "manual-implementation-adoption/v1",
            "ticket_id": ticket_id,
            "attempt_number": attempt_number,
            "operator_id": operator_id,
            "reason": reason,
            "branch": branch,
            "workspace_path": workspace_path,
            "base_sha": base_sha,
            "diff_hash": diff_hash,
            "artifact_path": artifact_path,
            "artifact_sha256": artifact_sha256,
        }, sort_keys=True, separators=(",", ":"))
        empty_hash = hashlib.sha256(b"").hexdigest()
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or not paused["paused"]:
                raise PermissionError("manual implementation adoption requires Local First paused")
            ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if ticket is None:
                raise KeyError(ticket_id)
            existing_stage = conn.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='implementation'", (ticket_id, attempt_number)).fetchone()
            existing_adoption = conn.execute("SELECT * FROM runtime_stages WHERE ticket_id=? AND stage=?", (ticket_id, f"manual-adoption-{attempt_number}" )).fetchone()
            if existing_stage is not None or existing_adoption is not None:
                if existing_stage is None or existing_adoption is None:
                    raise RuntimeError("manual_adoption_reconciliation_required: partial durable adoption")
                expected_stage = ("manual-adoption", artifact_sha256, artifact_path, workspace_path, base_sha, diff_hash)
                actual_stage = tuple(existing_stage[key] for key in ("adapter", "request_hash", "response_artifact", "worktree_path", "base_sha", "diff_hash"))
                if actual_stage != expected_stage or str(existing_adoption["detail"]) != detail or str(existing_adoption["artifact_sha256"] or "") != artifact_sha256:
                    raise RuntimeError("manual_adoption_reconciliation_required: identity conflict")
                return {"ticket_id": ticket_id, "attempt_number": attempt_number, "status": "already_adopted", "detail": detail}
            if ticket["state"] != CanonicalState.READY_LOCAL.value:
                raise RuntimeError("manual adoption requires ready_local ticket")
            if conn.execute("SELECT 1 FROM attempts WHERE ticket_id=?", (ticket_id,)).fetchone():
                raise RuntimeError("manual adoption requires ticket with no prior attempts")
            if conn.execute("SELECT 1 FROM model_invocations WHERE ticket_id=?", (ticket_id,)).fetchone():
                raise RuntimeError("manual adoption refuses tickets with model invocation history")
            if conn.execute("SELECT 1 FROM accepted_evidence WHERE ticket_id=?", (ticket_id,)).fetchone():
                raise RuntimeError("manual adoption refuses accepted ticket")
            now = self._now()
            conn.execute(
                "INSERT INTO attempts(ticket_id,attempt_number,base_sha,branch,worktree_path,pre_diff_hash,post_diff_hash,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, attempt_number, base_sha, branch, workspace_path, empty_hash, diff_hash, now),
            )
            conn.execute(
                "INSERT INTO model_stage_artifacts(ticket_id,attempt_number,stage,purpose,adapter,request_hash,response_artifact,worktree_path,base_sha,diff_hash,completed_at,status) VALUES (?,?,'implementation','implementation','manual-adoption',?,?,?,?,?,?,'completed')",
                (ticket_id, attempt_number, artifact_sha256, artifact_path, workspace_path, base_sha, diff_hash, now),
            )
            conn.execute(
                "INSERT INTO runtime_stages(ticket_id,stage,detail,attempt_number,artifact_path,artifact_sha256,base_sha,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, f"manual-adoption-{attempt_number}", detail, attempt_number, artifact_path, artifact_sha256, base_sha, now),
            )
            conn.execute(
                "INSERT INTO runtime_stages(ticket_id,stage,detail,attempt_number,artifact_path,artifact_sha256,base_sha,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, f"implementation-{attempt_number}", detail, attempt_number, artifact_path, artifact_sha256, base_sha, now),
            )
            conn.execute(
                "INSERT INTO runtime_stages(ticket_id,stage,detail,attempt_number,artifact_path,artifact_sha256,base_sha,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, "implementation_completed", detail, attempt_number, artifact_path, artifact_sha256, base_sha, now),
            )
            conn.execute("UPDATE tickets SET state=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?", (CanonicalState.IMPLEMENTING.value, now, ticket_id))
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="manual_implementation_adopted", actor_id=operator_id, from_state=CanonicalState.READY_LOCAL.value, to_state=CanonicalState.IMPLEMENTING.value, payload={"attempt_number": attempt_number, "reason": reason, "artifact_sha256": artifact_sha256, "diff_hash": diff_hash})
            return {"ticket_id": ticket_id, "attempt_number": attempt_number, "status": "adopted", "detail": detail}

    def failed_attempt_reconciliation(self, ticket_id: str, retired_attempt_number: int | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM failed_attempt_reconciliations WHERE ticket_id=?"
        values: tuple[Any, ...] = (ticket_id,)
        if retired_attempt_number is not None:
            query += " AND retired_attempt_number=?"; values += (retired_attempt_number,)
        query += " ORDER BY retired_attempt_number DESC LIMIT 1"
        row = self.connection.execute(query, values).fetchone()
        return dict(row) if row else None

    def reconciliation_status(self, ticket_id: str) -> dict[str, Any] | None:
        row = self.failed_attempt_reconciliation(ticket_id)
        if row is None: return None
        confirmed = self.connection.execute(
            "SELECT confirmed_at FROM retired_attempt_cleanup_confirmations WHERE ticket_id=? AND retired_attempt_number=?",
            (ticket_id, row["retired_attempt_number"]),
        ).fetchone()
        return {"retired_attempt": int(row["retired_attempt_number"]), "next_attempt": int(row["prospective_next_attempt_number"]), "cleanup_required": bool(row["cleanup_required"]), "cleanup_confirmed": confirmed is not None, "classification": str(row["classification"]), "retry_base_sha": str(row["retry_base_sha"]), "forensic_artifact_paths": json.loads(row["forensic_artifact_paths_json"])}

    def cleanup_prerequisites(self) -> list[dict[str, Any]]:
        """Read-only operator view of reconciled attempts awaiting verified cleanup."""
        rows = self.connection.execute(
            "SELECT r.ticket_id, r.retired_attempt_number, r.prospective_next_attempt_number, "
            "r.cleanup_required, r.forensic_artifact_paths_json, a.worktree_path, a.branch, "
            "c.confirmed_at FROM failed_attempt_reconciliations r "
            "JOIN attempts a ON a.ticket_id=r.ticket_id AND a.attempt_number=r.retired_attempt_number "
            "LEFT JOIN retired_attempt_cleanup_confirmations c "
            "ON c.ticket_id=r.ticket_id AND c.retired_attempt_number=r.retired_attempt_number "
            "WHERE r.cleanup_required=1 ORDER BY r.reconciled_at, r.ticket_id"
        )
        return [{**dict(row), "forensic_artifact_paths": json.loads(row["forensic_artifact_paths_json"]), "cleanup_confirmed": row["confirmed_at"] is not None} for row in rows]

    def cleanup_confirmed(self, ticket_id: str, retired_attempt_number: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM retired_attempt_cleanup_confirmations WHERE ticket_id=? AND retired_attempt_number=?",
            (ticket_id, retired_attempt_number),
        ).fetchone() is not None

    def reconcile_failed_attempt(self, ticket_id: str, *, operator_id: str, classification: str, retry_base_sha: str, runtime_identity: dict[str, Any], forensic_artifact_paths: tuple[str, ...] = ()) -> dict[str, Any]:
        """Explicitly retire one failed attempt without cleanup or evidence deletion."""
        allowed = {"runtime_infrastructure_failure", "model_timeout", "process_error", "validation_failure", "review_exhaustion"}
        if classification not in allowed:
            raise ValueError("unsupported_failed_attempt_classification")
        if not isinstance(operator_id, str) or not operator_id.strip() or not isinstance(retry_base_sha, str) or not retry_base_sha:
            raise ValueError("invalid_failed_attempt_reconciliation")
        if not all(isinstance(path, str) and path for path in forensic_artifact_paths):
            raise ValueError("invalid_forensic_artifact_paths")
        encoded_identity = json.dumps(runtime_identity, sort_keys=True, separators=(",", ":"))
        encoded_artifacts = json.dumps(sorted(set(forensic_artifact_paths)), separators=(",", ":"))
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or not paused["paused"]: raise PermissionError("failed-attempt reconciliation requires Local First paused")
            ticket = conn.execute("SELECT state FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if ticket is None: raise KeyError(ticket_id)
            attempt = conn.execute("SELECT * FROM attempts WHERE ticket_id=? ORDER BY attempt_number DESC LIMIT 1", (ticket_id,)).fetchone()
            if attempt is None: raise ValueError("failed-attempt reconciliation requires historical attempt")
            retired = int(attempt["attempt_number"])
            existing = conn.execute("SELECT * FROM failed_attempt_reconciliations WHERE ticket_id=? AND retired_attempt_number=?", (ticket_id, retired)).fetchone()
            if existing is not None:
                if (existing["classification"], existing["retry_base_sha"], existing["runtime_identity_json"], existing["forensic_artifact_paths_json"]) != (classification, retry_base_sha, encoded_identity, encoded_artifacts):
                    raise ValueError("failed-attempt reconciliation conflicts with durable evidence")
                if ticket["state"] != CanonicalState.READY_LOCAL.value:
                    raise ValueError("reconciled ticket state drift requires investigation")
                return {**dict(existing), "status": "already_reconciled"}
            previous_state = str(ticket["state"])
            if previous_state == CanonicalState.BLOCKED.value:
                pass
            elif previous_state == CanonicalState.NEEDS_TRIAGE.value and classification == "validation_failure":
                # Validation exhaustion is admitted only from a complete, same-attempt
                # implementation/validation pair.  These checks deliberately use the
                # durable ledger, not the caller's claimed reason or artifact path.
                implementation_stage = conn.execute(
                    "SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='implementation'",
                    (ticket_id, retired),
                ).fetchone()
                implementation_invocation = conn.execute(
                    "SELECT * FROM model_invocations WHERE ticket_id=? AND attempt_number=? AND stage='implementation' ORDER BY started_at DESC LIMIT 1",
                    (ticket_id, retired),
                ).fetchone()
                validation_stage = conn.execute(
                    "SELECT * FROM runtime_stages WHERE ticket_id=? AND stage=?",
                    (ticket_id, f"validation-{retired}"),
                ).fetchone()
                if implementation_stage is None or implementation_stage["status"] != "completed" or implementation_invocation is None or implementation_invocation["status"] != "completed":
                    raise ValueError("validation-failure reconciliation requires completed implementation")
                try:
                    if validation_stage is None or validation_stage["attempt_number"] != retired or validation_stage["base_sha"] != implementation_stage["base_sha"] or not isinstance(validation_stage["artifact_path"], str) or not isinstance(validation_stage["artifact_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", validation_stage["artifact_sha256"]):
                        raise ValueError("missing or mismatched durable validation artifact binding")
                    artifact_path = Path(validation_stage["artifact_path"])
                    expected_name = f"validation-{hashlib.sha256((str(implementation_stage['worktree_path']) + str(implementation_stage['base_sha'])).encode()).hexdigest()[:12]}.json"
                    expected_path = Path(str(implementation_stage["response_artifact"])).parent / expected_name
                    artifact_bytes = artifact_path.read_bytes()
                    if artifact_path != expected_path or hashlib.sha256(artifact_bytes).hexdigest() != validation_stage["artifact_sha256"]:
                        raise ValueError("validation artifact integrity binding mismatch")
                    record = json.loads(str(validation_stage["detail"]))
                    if not isinstance(record, dict):
                        raise ValueError("validation record is not an object")
                    artifact = json.loads(artifact_bytes.decode("utf-8"))
                    errors = artifact["errors"]
                    if (
                        record.get("attempt_number") != retired
                        or record.get("artifact_path") != str(artifact_path)
                        or record.get("artifact_sha256") != validation_stage["artifact_sha256"]
                        or not _is_json_int(record.get("attempt_number"))
                        or record.get("completed") is not True
                        or record.get("passed") is not False
                        or type(record.get("completed")) is not bool
                        or type(record.get("passed")) is not bool
                        or not isinstance(record.get("compact_evidence"), str)
                        or record["compact_evidence"].strip().startswith("validation passed")
                        or not isinstance(artifact, dict)
                        or artifact.get("base_sha") != implementation_stage["base_sha"]
                        or not _is_string_list(artifact.get("changed_files"))
                        or not _is_json_int(artifact.get("changed_lines"))
                        or type(artifact.get("scope_unverified")) is not bool
                        or not isinstance(errors, list)
                        or not errors
                        or not all(isinstance(error, str) and error.strip() for error in errors)
                        or not _is_command_list(artifact.get("commands"))
                    ):
                        raise ValueError("invalid structured validation failure evidence")
                except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    raise ValueError("validation-failure reconciliation requires valid structured validation failure evidence") from exc
                if conn.execute("SELECT 1 FROM review_candidates WHERE ticket_id=?", (ticket_id,)).fetchone():
                    raise ValueError("validation-failure reconciliation cannot retire a ticket with review candidate")
                if conn.execute("SELECT 1 FROM review_results WHERE ticket_id=?", (ticket_id,)).fetchone() or conn.execute("SELECT 1 FROM model_invocations WHERE ticket_id=? AND stage='review'", (ticket_id,)).fetchone():
                    raise ValueError("validation-failure reconciliation cannot retire after review activity")
                if conn.execute("SELECT 1 FROM accepted_evidence WHERE ticket_id=?", (ticket_id,)).fetchone() or conn.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND accepted_commit_sha IS NOT NULL", (ticket_id,)).fetchone():
                    raise ValueError("accepted ticket cannot retire an attempt")
                if conn.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND attempt_number>?", (ticket_id, retired)).fetchone():
                    raise ValueError("validation-failure reconciliation cannot retire an attempt with repair history")
            else:
                raise ValueError("failed-attempt reconciliation requires blocked ticket")
            if conn.execute("SELECT 1 FROM accepted_evidence WHERE ticket_id=?", (ticket_id,)).fetchone(): raise ValueError("accepted ticket cannot retire an attempt")
            if conn.execute("SELECT 1 FROM model_invocations WHERE ticket_id=? AND status='started'", (ticket_id,)).fetchone(): raise ValueError("incomplete model invocation requires explicit resolution")
            if attempt["outcome"] not in {None, "failed"}: raise ValueError("attempt is not eligible for failed-attempt reconciliation")
            next_attempt = self.next_attempt_number(ticket_id)
            now = self._now()
            conn.execute("UPDATE attempts SET outcome='failed_retired' WHERE ticket_id=? AND attempt_number=?", (ticket_id, retired))
            conn.execute("INSERT INTO failed_attempt_reconciliations(ticket_id,retired_attempt_number,classification,previous_ticket_state,resulting_ticket_state,operator_id,runtime_identity_json,retry_base_sha,prospective_next_attempt_number,cleanup_required,forensic_artifact_paths_json,reconciled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (ticket_id,retired,classification,previous_state,CanonicalState.READY_LOCAL.value,operator_id,encoded_identity,retry_base_sha,next_attempt,1,encoded_artifacts,now))
            conn.execute("UPDATE tickets SET state=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=? AND state=?", (CanonicalState.READY_LOCAL.value,now,ticket_id,previous_state))
            reconciliation_event = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="failed_attempt_reconciled", actor_id=operator_id, payload={"retired_attempt":retired,"classification":classification,"next_attempt":next_attempt,"retry_base_sha":retry_base_sha,"cleanup_required":True,"forensic_artifact_paths":json.loads(encoded_artifacts),"runtime_identity":runtime_identity})
            transition_event = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="state_transition", actor_id=operator_id, from_state=previous_state, to_state=CanonicalState.READY_LOCAL.value, payload={"reason":"failed_attempt_reconciled","retired_attempt":retired,"next_attempt":next_attempt,"cleanup_required":True})
            self._enqueue_projection_bundle_in_transaction(conn, ticket_id=ticket_id, event_id=transition_event, evidence=f"failed attempt {retired} retired; cleanup required before attempt {next_attempt}")
            return {"ticket_id":ticket_id,"retired_attempt_number":retired,"prospective_next_attempt_number":next_attempt,"retry_base_sha":retry_base_sha,"cleanup_required":True,"status":"reconciled","event_id":reconciliation_event}

    def confirm_retired_attempt_cleanup(self, ticket_id: str, *, retired_attempt_number: int, operator_id: str, checked_paths: tuple[str, ...]) -> dict[str, Any]:
        """Persist a separate operator attestation after controller-side absence checks."""
        if not operator_id.strip() or not all(isinstance(path, str) and path for path in checked_paths):
            raise ValueError("invalid_cleanup_confirmation")
        encoded_paths = json.dumps(sorted(set(checked_paths)), separators=(",", ":"))
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or not paused["paused"]: raise PermissionError("cleanup confirmation requires Local First paused")
            reconciliation = conn.execute("SELECT * FROM failed_attempt_reconciliations WHERE ticket_id=? AND retired_attempt_number=?", (ticket_id, retired_attempt_number)).fetchone()
            if reconciliation is None: raise ValueError("attempt is not retired")
            existing = conn.execute("SELECT * FROM retired_attempt_cleanup_confirmations WHERE ticket_id=? AND retired_attempt_number=?", (ticket_id, retired_attempt_number)).fetchone()
            if existing is not None:
                if existing["checked_paths_json"] != encoded_paths: raise ValueError("cleanup confirmation conflicts with durable evidence")
                return {**dict(existing), "status":"already_confirmed"}
            conn.execute("INSERT INTO retired_attempt_cleanup_confirmations(ticket_id,retired_attempt_number,operator_id,checked_paths_json,confirmed_at) VALUES (?,?,?,?,?)", (ticket_id,retired_attempt_number,operator_id,encoded_paths,self._now()))
            event_id = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="retired_attempt_cleanup_confirmed", actor_id=operator_id, payload={"retired_attempt":retired_attempt_number,"checked_paths":json.loads(encoded_paths)})
            return {"ticket_id":ticket_id,"retired_attempt_number":retired_attempt_number,"status":"confirmed","event_id":event_id}

    def bind_runtime(self, ticket_id: str, repository_path: str, starting_sha: str, canonical_sha: str | None = None, ownership_verified: int = 1, operator_signer_fingerprint: str | None = None, operator_authority_hash: str | None = None) -> dict[str, Any]:
        requested = (str(repository_path), str(starting_sha), str(canonical_sha or starting_sha), int(ownership_verified), operator_signer_fingerprint, operator_authority_hash)
        def verify(row: sqlite3.Row) -> dict[str, Any]:
            actual = (str(row["repository_path"]), str(row["starting_sha"]), str(row["canonical_sha"]), int(row["ownership_verified"]), row["operator_signer_fingerprint"], row["operator_authority_hash"])
            if actual != requested: raise ValueError("conflicting runtime binding")
            return dict(row)
        try:
            with self._transaction() as conn:
                existing = conn.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
                if existing is not None: return verify(existing)
                conn.execute("INSERT INTO runtime_bindings(ticket_id, repository_path, starting_sha, canonical_sha, ownership_verified, created_at, operator_signer_fingerprint, operator_authority_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (ticket_id, *requested[:4], self._now(), *requested[4:]))
                return verify(conn.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone())
        except sqlite3.IntegrityError:
            existing = self.connection.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
            if existing is None: raise
            return verify(existing)

    def persist_validated_decomposition_plan(self, *, plan_id: str, feature_id: str, fingerprint: str, plan_json: str, repository_identity: str, repo_base_sha: str, repo_snapshot_hash: str, repo_snapshot_manifest_json: str) -> str:
        """Persist one validated plan without activating its tranche or tickets."""
        now = self._now()
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM decomposition_plans WHERE feature_id=?", (feature_id,)).fetchall()
            for row in existing:
                if (row["fingerprint"], row["repository_identity"], row["repo_base_sha"], row["repo_snapshot_hash"], row["repo_snapshot_manifest_json"]) != (fingerprint, repository_identity, repo_base_sha, repo_snapshot_hash, repo_snapshot_manifest_json):
                    raise ValueError("conflicting durable decomposition plan")
                if row["status"] not in ("validated_pending_activation", "active"):
                    raise ValueError("conflicting durable decomposition plan state")
                return str(row["id"])
            conn.execute("INSERT INTO decomposition_plans(id,feature_id,fingerprint,plan_json,status,created_at,activated_at,repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (plan_id, feature_id, fingerprint, plan_json, "validated_pending_activation", now, None, repository_identity, repo_base_sha, repo_snapshot_hash, repo_snapshot_manifest_json))
        return plan_id

    def _finalize_planning_run_activation(self, conn: sqlite3.Connection, request_key: str, *, plan_id: str, ticket_ids: tuple[str, ...]) -> None:
        row = conn.execute("SELECT * FROM planning_runs WHERE request_key=?", (request_key,)).fetchone()
        if row is None:
            raise ValueError("planning run is missing")
        bound_plan_id = row["plan_id"]
        if bound_plan_id is None or bound_plan_id != plan_id:
            raise ValueError("planning run plan identity conflicts")
        plan = conn.execute("SELECT * FROM decomposition_plans WHERE id=? AND feature_id=?", (bound_plan_id, row["feature_id"])).fetchone()
        if plan is None or plan["status"] != "active":
            raise ValueError("decomposition plan is not active")
        try:
            plan_payload = json.loads(plan["plan_json"])["plan"]
            active_tranche_id = next(item["id"] for item in plan_payload["tranches"] if int(item["ordinal"]) == 0)
        except (KeyError, TypeError, ValueError, StopIteration) as exc:
            raise ValueError("active decomposition tranche is invalid") from exc
        actual_rows = conn.execute("SELECT t.id FROM tickets AS t JOIN tranches AS tr ON tr.id=t.tranche_id WHERE t.feature_id=? AND tr.ordinal=? ORDER BY t.id", (row["feature_id"], 0)).fetchall()
        actual_ticket_ids = tuple(str(item["id"]) for item in actual_rows)
        if not actual_ticket_ids:
            raise ValueError("active decomposition plan has no materialized tickets")
        if tuple(ticket_ids) != actual_ticket_ids:
            raise ValueError("planning run ticket identity conflicts")
        if row["status"] == "activated":
            if json.loads(row["ticket_ids_json"]) != list(actual_ticket_ids):
                raise ValueError("activated planning run conflicts")
            return
        if row["status"] != "validated_pending_activation":
            raise ValueError("planning run is not pending activation")
        conn.execute("UPDATE planning_runs SET status='activated', plan_id=?, ticket_ids_json=?, updated_at=? WHERE request_key=?", (bound_plan_id, json.dumps(list(actual_ticket_ids), separators=(",", ":")), self._now(), request_key))

    def finalize_planning_run_activation(self, request_key: str, *, plan_id: str, ticket_ids: tuple[str, ...]) -> None:
        """Finalize only against the exact active plan and ledger ticket set."""
        with self._transaction() as conn:
            self._finalize_planning_run_activation(conn, request_key, plan_id=plan_id, ticket_ids=ticket_ids)

    def runtime_binding(self, ticket_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
        if row is None: raise KeyError(f"no runtime binding for {ticket_id}")
        return dict(row)

    def historical_revalidation_authorization(self, ticket_id: str, attempt_number: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM historical_revalidation_authorizations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        return dict(row) if row else None

    def create_historical_revalidation_authorization(self, *, ticket_id: str, attempt_number: int, base_sha: str, repository_identity: str, target_file: str, failure_classification: str, failure_evidence_identity: str, implementation_invocation_id: str, operator_id: str, reason: str) -> dict[str, Any]:
        fields = authorization_identity(ticket_id=ticket_id, attempt_number=attempt_number, base_sha=base_sha, repository_identity=repository_identity, target_file=target_file, failure_classification=failure_classification, failure_evidence_identity=failure_evidence_identity, implementation_invocation_id=implementation_invocation_id, operator_id=operator_id, reason=reason)
        authorization_digest = authorization_hash(fields)
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM historical_revalidation_authorizations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            if existing is not None:
                if str(existing["authorization_hash"]) != authorization_digest:
                    raise ValueError("conflicting historical revalidation authorization")
                return dict(existing)
            authorization_id = uuid.uuid4().hex
            conn.execute("INSERT INTO historical_revalidation_authorizations(authorization_id,ticket_id,attempt_number,base_sha,repository_identity,target_file,failure_classification,failure_evidence_identity,implementation_invocation_id,operator_id,reason,authorization_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (authorization_id, ticket_id, attempt_number, base_sha, repository_identity, target_file, failure_classification, failure_evidence_identity, implementation_invocation_id, operator_id, reason, authorization_digest, self._now()))
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="historical_revalidation_authorized", actor_id=operator_id, payload={"authorization_id": authorization_id, "attempt_number": attempt_number, "base_sha": base_sha, "target_file": target_file, "failure_classification": failure_classification, "implementation_invocation_id": implementation_invocation_id, "authorization_hash": authorization_digest})
            return dict(conn.execute("SELECT * FROM historical_revalidation_authorizations WHERE authorization_id=?", (authorization_id,)).fetchone())

    def historical_revalidation_attestation(self, ticket_id: str, attempt_number: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM historical_revalidation_attestations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        return dict(row) if row else None

    def historical_revalidation_validation_claim(self, ticket_id: str, attempt_number: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM historical_revalidation_validation_claims WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        return dict(row) if row else None

    def historical_revalidation_validation_result(self, ticket_id: str, attempt_number: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM historical_revalidation_validation_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        return dict(row) if row else None

    def claim_historical_revalidation_validation(self, *, ticket_id: str, attempt_number: int, authorization_hash_value: str, attestation_hash_value: str, base_sha: str, implementation_diff_hash: str, validation_profile_hash: str) -> dict[str, Any]:
        identity = historical_validation_identity(ticket_id=ticket_id, attempt_number=attempt_number, authorization_hash=authorization_hash_value, attestation_hash=attestation_hash_value, base_sha=base_sha, implementation_diff_hash=implementation_diff_hash, validation_profile_hash=validation_profile_hash)
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM historical_revalidation_validation_claims WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            if existing is not None:
                if any(str(existing[field]) != str(identity[field]) for field in identity):
                    raise RuntimeError("conflicting historical validation execution identity")
                if conn.execute("SELECT 1 FROM historical_revalidation_validation_results WHERE claim_id=?", (existing["claim_id"],)).fetchone():
                    return {**dict(existing), "status": "completed"}
                raise RuntimeError("historical validation execution is incomplete; reconciliation required")
            claim_id = uuid.uuid4().hex
            conn.execute("INSERT INTO historical_revalidation_validation_claims(claim_id,ticket_id,attempt_number,authorization_hash,attestation_hash,base_sha,implementation_diff_hash,validation_profile_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (claim_id, ticket_id, attempt_number, authorization_hash_value, attestation_hash_value, base_sha, implementation_diff_hash, validation_profile_hash, self._now()))
            return dict(conn.execute("SELECT * FROM historical_revalidation_validation_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def record_historical_revalidation_validation_result(self, *, claim_id: str, ticket_id: str, attempt_number: int, authorization_hash_value: str, attestation_hash_value: str, base_sha: str, implementation_diff_hash: str, validation_profile_hash: str, artifact_path: str, artifact_sha256: str, passed: bool, compact_evidence: str) -> dict[str, Any]:
        result_hash = historical_validation_result_hash(ticket_id=ticket_id, attempt_number=attempt_number, authorization_hash=authorization_hash_value, attestation_hash=attestation_hash_value, base_sha=base_sha, implementation_diff_hash=implementation_diff_hash, validation_profile_hash=validation_profile_hash, artifact_sha256=artifact_sha256, passed=passed, compact_evidence=compact_evidence)
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM historical_revalidation_validation_claims WHERE claim_id=?", (claim_id,)).fetchone()
            expected_claim = historical_validation_identity(ticket_id=ticket_id, attempt_number=attempt_number, authorization_hash=authorization_hash_value, attestation_hash=attestation_hash_value, base_sha=base_sha, implementation_diff_hash=implementation_diff_hash, validation_profile_hash=validation_profile_hash)
            if claim is None or any(str(claim[field]) != str(expected_claim[field]) for field in expected_claim):
                raise RuntimeError("historical validation result claim mismatch")
            existing = conn.execute("SELECT * FROM historical_revalidation_validation_results WHERE claim_id=?", (claim_id,)).fetchone()
            if existing is not None:
                if str(existing["result_hash"]) != result_hash or int(existing["passed"]) != int(passed):
                    raise RuntimeError("conflicting historical validation result")
                return dict(existing)
            conn.execute("INSERT INTO historical_revalidation_validation_results(result_id,claim_id,ticket_id,attempt_number,authorization_hash,attestation_hash,base_sha,implementation_diff_hash,validation_profile_hash,artifact_path,artifact_sha256,passed,compact_evidence,result_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, claim_id, ticket_id, attempt_number, authorization_hash_value, attestation_hash_value, base_sha, implementation_diff_hash, validation_profile_hash, artifact_path, artifact_sha256, int(passed), compact_evidence, result_hash, self._now()))
            return dict(conn.execute("SELECT * FROM historical_revalidation_validation_results WHERE claim_id=?", (claim_id,)).fetchone())

    def create_historical_revalidation_attestation(self, *, ticket_id: str, attempt_number: int, base_sha: str, repository_identity: str, implementation_invocation_id: str, implementation_artifact: str, implementation_diff_hash: str, worktree_path: str, worktree_diff_hash: str, authorization_hash_value: str, operator_id: str) -> dict[str, Any]:
        fields = attestation_identity(ticket_id=ticket_id, attempt_number=attempt_number, base_sha=base_sha, repository_identity=repository_identity, implementation_invocation_id=implementation_invocation_id, implementation_artifact=implementation_artifact, implementation_diff_hash=implementation_diff_hash, worktree_path=worktree_path, worktree_diff_hash=worktree_diff_hash, authorization_hash=authorization_hash_value, operator_id=operator_id)
        digest = attestation_hash(fields)
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM historical_revalidation_attestations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            if existing is not None:
                if str(existing["attestation_hash"]) != digest:
                    raise ValueError("conflicting historical revalidation attestation")
                return dict(existing)
            attestation_id = uuid.uuid4().hex
            conn.execute("INSERT INTO historical_revalidation_attestations(attestation_id,ticket_id,attempt_number,base_sha,repository_identity,implementation_invocation_id,implementation_artifact,implementation_diff_hash,worktree_path,worktree_diff_hash,authorization_hash,operator_id,attestation_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (attestation_id, ticket_id, attempt_number, base_sha, repository_identity, implementation_invocation_id, implementation_artifact, implementation_diff_hash, worktree_path, worktree_diff_hash, authorization_hash_value, operator_id, digest, self._now()))
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="historical_revalidation_attested", actor_id=operator_id, payload={"attestation_id": attestation_id, "attempt_number": attempt_number, "implementation_invocation_id": implementation_invocation_id, "implementation_diff_hash": implementation_diff_hash, "worktree_diff_hash": worktree_diff_hash, "authorization_hash": authorization_hash_value, "attestation_hash": digest})
            return dict(conn.execute("SELECT * FROM historical_revalidation_attestations WHERE attestation_id=?", (attestation_id,)).fetchone())

    def evaluate_ticket_readiness(self, ticket_id: str) -> TicketReadinessResult:
        row = self.connection.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None: return TicketReadinessResult("missing_ticket")
        if row["state"] == CanonicalState.READY_LOCAL.value: return TicketReadinessResult("ready")
        if row["state"] != CanonicalState.DRAFT.value: return TicketReadinessResult("wrong_state")
        binding=self.connection.execute("SELECT repository_path,starting_sha FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
        if binding is None: return TicketReadinessResult("missing_runtime_binding")
        if not binding["repository_path"] or not binding["starting_sha"]: return TicketReadinessResult("invalid_runtime_binding")
        try:
            from .controller import ticket_from_ledger
            validate_ticket(ticket_from_ledger(dict(row)))
        except Exception: return TicketReadinessResult("invalid_ticket")
        dependencies=tuple(sorted(set(json.loads(row["dependencies_json"]))))
        if ticket_id in dependencies: return TicketReadinessResult("invalid_ticket")
        unresolved=[]
        for dependency in dependencies:
            dep=self.connection.execute("SELECT state FROM tickets WHERE id=?", (dependency,)).fetchone()
            if dep is None: return TicketReadinessResult("missing_dependency", (dependency,))
            if dep["state"] == CanonicalState.ACCEPTED.value:
                continue
            # Controllers project a locally accepted commit through DONE.  A
            # generated dependent may proceed only when that terminal state has
            # durable accepted evidence, never merely because a ticket is done.
            if dep["state"] == CanonicalState.DONE.value and self.accepted_commit(dependency):
                continue
            unresolved.append(dependency)
        return TicketReadinessResult("waiting_on_dependencies", tuple(unresolved)) if unresolved else TicketReadinessResult("ready")

    def admit_ticket_if_ready(self, ticket_id: str) -> TicketReadinessResult:
        result=self.evaluate_ticket_readiness(ticket_id)
        if result.status != "ready" or self.get_ticket(ticket_id)["state"] == CanonicalState.READY_LOCAL.value: return result
        self.transition(ticket_id, CanonicalState.READY_LOCAL, actor_id="readiness", payload={"reason":"dependencies_satisfied"})
        return TicketReadinessResult("ready")

    def claim_scheduler_tick(self, owner: str, lease_token: str, *, lease_seconds: int, now: int | None = None) -> str:
        """Serialize bounded ticks and recover automatically after process loss."""
        if not owner or not lease_token or lease_seconds < 1:
            raise ValueError("scheduler tick requires an owner, token, and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or paused["paused"]:
                return "paused"
            current = conn.execute("SELECT * FROM scheduler_tick_lease WHERE id=1").fetchone()
            if current is not None and int(current["lease_expires_at"]) > now:
                return "busy"
            conn.execute(
                "INSERT INTO scheduler_tick_lease(id,lease_owner,lease_token,lease_expires_at,updated_at) VALUES (1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET lease_owner=excluded.lease_owner,lease_token=excluded.lease_token,lease_expires_at=excluded.lease_expires_at,updated_at=excluded.updated_at",
                (owner, lease_token, now + lease_seconds, now),
            )
            self._append_event(conn, entity_type="controller", entity_id="scheduler", event_type="scheduler_tick_claimed", actor_id=owner, payload={"lease_expires_at": now + lease_seconds, "recovered": current is not None and int(current["lease_expires_at"]) <= now})
            return "claimed"

    def release_scheduler_tick(self, owner: str, lease_token: str) -> bool:
        with self._transaction() as conn:
            changed = conn.execute("DELETE FROM scheduler_tick_lease WHERE id=1 AND lease_owner=? AND lease_token=?", (owner, lease_token))
            if changed.rowcount:
                self._append_event(conn, entity_type="controller", entity_id="scheduler", event_type="scheduler_tick_released", actor_id=owner)
            return changed.rowcount == 1

    def scheduler_claim(self, claim_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
        if row is None:
            raise KeyError(claim_id)
        return dict(row)

    @staticmethod
    def _scheduler_stage_family(stage: str) -> str:
        for prefix in ("implementation", "validation", "review", "repair_routing", "triage", "acceptance", "git_integration", "completion"):
            if stage == prefix or stage.startswith(prefix + ":"):
                return prefix
        return stage

    def scheduler_reconciliation(self, claim_id: str):
        from .reconciliation import ReconciliationAction, ReconciliationState, SchedulerReconciliationDecision

        claim = self.connection.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
        if claim is None:
            raise KeyError(claim_id)
        stage = str(claim["stage"])
        family = self._scheduler_stage_family(stage)
        ticket_id = str(claim["ticket_id"])
        evidence: dict[str, Any] = {}

        pending_projection = self.connection.execute(
            """
            SELECT
              EXISTS(SELECT 1 FROM board_projection_outbox WHERE ticket_id=? AND acknowledged_at IS NULL AND superseded_at IS NULL) AS board_pending,
              EXISTS(SELECT 1 FROM evidence_comment_outbox WHERE ticket_id=? AND status NOT IN ('delivered','reconciled_delivered')) AS comment_pending
            """,
            (ticket_id, ticket_id),
        ).fetchone()

        if claim["status"] == "completed":
            if pending_projection and (int(pending_projection["board_pending"]) or int(pending_projection["comment_pending"])):
                return SchedulerReconciliationDecision(
                    claim_id, ticket_id, stage,
                    ReconciliationState.LOCAL_STAGE_COMPLETION_RECORDED_DOWNSTREAM_INCOMPLETE,
                    ReconciliationAction.RESUME,
                    "outbox",
                    "local stage is finalized; downstream projection remains durable and independently retryable",
                    {"board_pending": bool(pending_projection["board_pending"]), "comment_pending": bool(pending_projection["comment_pending"])},
                )
            return SchedulerReconciliationDecision(
                claim_id, ticket_id, stage,
                ReconciliationState.FULLY_FINALIZED,
                ReconciliationAction.RESUME,
                "scheduler_stage_claims",
                "scheduler claim is fully finalized",
                {},
            )

        if claim["side_effect_completed_at"] is not None:
            return SchedulerReconciliationDecision(
                claim_id, ticket_id, stage,
                ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE,
                ReconciliationAction.RECONCILE,
                "scheduler_stage_claims",
                "stage effect is durably completed but scheduler finalization is incomplete",
                {"result_json": claim["result_json"]},
            )

        if claim["side_effect_started_at"] is None:
            return SchedulerReconciliationDecision(
                claim_id, ticket_id, stage,
                ReconciliationState.NOT_STARTED,
                ReconciliationAction.RETRY,
                "scheduler_stage_claims",
                "claim exists but no side effect was durably started",
                {},
            )

        identity = {}
        if claim["candidate_identity_json"]:
            try:
                identity = json.loads(str(claim["candidate_identity_json"]))
            except json.JSONDecodeError:
                return SchedulerReconciliationDecision(
                    claim_id, ticket_id, stage,
                    ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN,
                    ReconciliationAction.STOP,
                    "scheduler_stage_claims",
                    "claim identity is malformed and cannot be reconciled automatically",
                    {},
                )

        if family in {"implementation", "review", "triage"}:
            attempt = identity.get("attempt_number")
            if isinstance(attempt, int):
                invocation = self.connection.execute(
                    "SELECT * FROM model_invocations WHERE ticket_id=? AND attempt_number=? AND stage=? ORDER BY started_at DESC,invocation_id DESC LIMIT 1",
                    (ticket_id, attempt, family),
                ).fetchone()
            else:
                invocation = self.connection.execute(
                    "SELECT * FROM model_invocations WHERE ticket_id=? AND stage=? ORDER BY started_at DESC,invocation_id DESC LIMIT 1",
                    (ticket_id, family),
                ).fetchone()
            if invocation is not None:
                evidence = {"invocation_id": str(invocation["invocation_id"]), "status": str(invocation["status"])}
                if invocation["status"] == "completed":
                    return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE, "model_invocations", "model invocation completed; reconcile durable artifact/result into the scheduler stage", evidence)
                if invocation["status"] == "started":
                    return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.STOP, "model_invocations", "model invocation outcome is unknown; automatic retry would risk duplicate inference", evidence)
                return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.STOP, "model_invocations", "model invocation ended terminally and requires explicit recovery policy", evidence)

        if family in {"paid_checkpoint", "paid_escalation"}:
            reservation = self.connection.execute("SELECT * FROM paid_reservations WHERE request_key=? ORDER BY created_at DESC LIMIT 1", (claim_id,)).fetchone()
            if reservation is not None:
                evidence = {"reservation_id": str(reservation["id"]), "status": str(reservation["status"])}
                if reservation["status"] == "completed":
                    return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE, "paid_reservations", "paid reservation completed; reconcile the durable model-call response without provider reinvocation", evidence)
                if reservation["status"] in {"unknown_outcome", "in_flight"}:
                    return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.STOP, "paid_reservations", "paid provider outcome is ambiguous; automatic retry is forbidden", evidence)
                return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.STOP, "paid_reservations", "paid reservation is terminal and requires a new explicitly authorized request", evidence)
            return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.NOT_STARTED, ReconciliationAction.RETRY, "paid_reservations", "no paid reservation exists, so no provider side effect was authorized", {})

        if family == "git_integration":
            commit = self.connection.execute("SELECT * FROM git_commit_evidence WHERE ticket_id=?", (ticket_id,)).fetchone()
            intent = self.connection.execute("SELECT * FROM git_commit_intents WHERE ticket_id=?", (ticket_id,)).fetchone()
            if commit is not None:
                return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE, "git_commit_evidence", "accepted commit evidence already exists; finalize/reconcile without another commit", {"commit_sha": str(commit["commit_sha"])})
            if intent is not None:
                return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.REPLAY, "git_commit_intents", "Git mutation may have occurred; replay exact commit reconciliation from durable intent", {"intent_status": str(intent["status"]), "commit_sha": intent["commit_sha"]})
            return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.REPLAY, "git_commit_intents", "no durable Git mutation intent exists; replay pre-mutation verification safely", {})

        durable_stage_evidence = {
            "tranche_checkpoint": ("tranche_checkpoint_evidence", "tranche_id", identity.get("tranche_id")),
            "next_tranche_materialize": ("next_tranche_materializations", "predecessor_tranche_id", identity.get("predecessor_tranche_id")),
            "next_tranche_activation": ("next_tranche_activation_evidence", "successor_tranche_id", identity.get("successor_tranche_id")),
            "native_dependency_graph": ("native_dependency_graphs", "ticket_id", ticket_id),
            "native_dependency_release": ("native_dependency_releases", "ticket_id", ticket_id),
        }.get(family)
        if durable_stage_evidence is not None and durable_stage_evidence[2]:
            table, key, value = durable_stage_evidence
            row = self.connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
            if row is not None:
                return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE, table, "authoritative stage evidence exists; reconcile scheduler completion from durable evidence", {"identity": value})
            return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.REPLAY, table, "stage uses exact/idempotent reconciliation; replay against authoritative external/local evidence", {"identity": value})

        if family in {"validation", "repair_routing", "acceptance", "completion"}:
            return SchedulerReconciliationDecision(claim_id, ticket_id, stage, ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.REPLAY, "scheduler_stage_claims", "stage is deterministic/local and can safely replay from frozen claim identity", {})

        return SchedulerReconciliationDecision(
            claim_id, ticket_id, stage,
            ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN,
            ReconciliationAction.STOP,
            "scheduler_stage_claims",
            "stage outcome cannot be classified safely from registered durable evidence",
            {},
        )

    def next_scheduler_reconciliation(self, *, now: int | None = None):
        now = self._now() if now is None else now
        rows = self.connection.execute(
            "SELECT claim_id,stage,candidate_identity_json FROM scheduler_stage_claims WHERE status='claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at<=? ORDER BY created_at,claim_id",
            (now,),
        ).fetchall()
        for row in rows:
            if str(row["stage"]) == "tranche_checkpoint":
                identity = json.loads(str(row["candidate_identity_json"] or "{}"))
                try:
                    current = self._tranche_checkpoint_identity(self.connection, str(identity.get("tranche_id") or ""))
                except RuntimeError:
                    pass
                else:
                    if self._tranche_checkpoint_h1_conflicts(self.connection, current):
                        # The tranche-checkpoint claimer durably finalizes this
                        # stale H1-generation claim instead of replaying it.
                        continue
            return self.scheduler_reconciliation(str(row["claim_id"]))
        return None

    def _triage_claim_identity(self, row: sqlite3.Row | dict[str, Any], *, attempt_number: int, failure_evidence: str, triage_execution_policy_hash: str) -> dict[str, Any]:
        if not triage_execution_policy_hash:
            raise ValueError("triage execution policy hash is required")
        accepted = self.connection.execute(
            "SELECT criterion_id FROM criterion_statuses WHERE ticket_id=? AND status='accepted' ORDER BY criterion_id",
            (row["id"],),
        ).fetchall()
        criteria = set(json.loads(row["criterion_ids_json"]))
        unresolved = sorted(criteria - {str(item["criterion_id"]) for item in accepted})
        ticket_policy_hash = self._validation_policy_hash(row)
        return {
            "ticket_id": str(row["id"]),
            "attempt_number": int(attempt_number),
            "parent_depth": int(row["depth"]),
            "unresolved_criteria": unresolved,
            "failure_evidence_hash": hashlib.sha256(failure_evidence.encode()).hexdigest(),
            "ticket_policy_hash": ticket_policy_hash,
            "triage_execution_policy_hash": triage_execution_policy_hash,
        }

    def claim_next_scheduler_triage(self, owner: str, *, lease_seconds: int, triage_execution_policy_hash: str, now: int | None = None) -> dict[str, Any] | None:
        """Claim one policy-routed needs-triage ticket for planning-only triage."""
        if not owner or lease_seconds < 1 or not triage_execution_policy_hash:
            raise ValueError("triage scheduler claim requires owner, lease, and execution policy")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute(
                "SELECT * FROM scheduler_stage_claims WHERE stage LIKE 'triage:%' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
                (now,),
            ).fetchone()
            if replay is not None:
                ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (replay["ticket_id"],)).fetchone()
                if ticket is None:
                    raise RuntimeError("triage_reconciliation_required: ticket missing")
                try:
                    identity = json.loads(str(replay["candidate_identity_json"] or ""))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("triage_reconciliation_required: claim identity malformed") from exc
                routing = conn.execute(
                    "SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage=?",
                    (replay["ticket_id"], f"repair-routing-{int(identity['attempt_number'])}"),
                ).fetchone()
                if routing is None:
                    raise RuntimeError("triage_reconciliation_required: routing evidence missing")
                try:
                    routing_detail = json.loads(str(routing["detail"]))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("triage_reconciliation_required: routing evidence malformed") from exc
                if routing_detail.get("action") != "triage":
                    raise RuntimeError("triage_reconciliation_required: ticket is no longer triage-routable")
                expected = self._triage_claim_identity(
                    ticket,
                    attempt_number=int(identity["attempt_number"]),
                    failure_evidence=str(routing_detail.get("failure_evidence") or ""),
                    triage_execution_policy_hash=triage_execution_policy_hash,
                )
                if identity != expected:
                    raise RuntimeError("triage_reconciliation_required: claim identity drift")
                changed = conn.execute(
                    "UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?",
                    (owner, now + lease_seconds, now, replay["claim_id"], now),
                )
                if changed.rowcount != 1:
                    return None
                conn.execute("UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?", (owner, now + lease_seconds, now, replay["ticket_id"]))
                self._append_event(conn, entity_type="ticket", entity_id=str(replay["ticket_id"]), event_type="scheduler_stage_reclaimed", actor_id=owner, payload={"claim_id": replay["claim_id"], "stage": "triage", "lease_expires_at": now + lease_seconds})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())

            row = conn.execute("""
                SELECT t.*, r.detail AS routing_detail, r.attempt_number AS routing_attempt
                FROM tickets t
                JOIN runtime_stages r ON r.ticket_id=t.id AND r.stage=('repair-routing-' || r.attempt_number)
                WHERE t.state=?
                  AND json_valid(r.detail)=1
                  AND json_extract(r.detail,'$.action')='triage'
                  AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('triage:' || r.attempt_number))
                ORDER BY t.created_at,t.id,r.attempt_number DESC LIMIT 1
            """, (CanonicalState.NEEDS_TRIAGE.value,)).fetchone()
            if row is None:
                return None
            attempt_number = int(row["routing_attempt"])
            routing_detail = json.loads(str(row["routing_detail"]))
            identity = self._triage_claim_identity(
                row,
                attempt_number=attempt_number,
                failure_evidence=str(routing_detail.get("failure_evidence") or ""),
                triage_execution_policy_hash=triage_execution_policy_hash,
            )
            encoded_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            stage = f"triage:{attempt_number}"
            claim_id = hashlib.sha256((stage + ":" + encoded_identity).encode()).hexdigest()[:32]
            changed = conn.execute(
                "UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND state=? AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (owner, now + lease_seconds, now, row["id"], CanonicalState.NEEDS_TRIAGE.value, now),
            )
            if changed.rowcount != 1:
                return None
            conn.execute(
                "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,'claimed',?,?,1,?,?,?)",
                (claim_id, row["id"], stage, owner, now + lease_seconds, encoded_identity, now, now),
            )
            self._append_event(conn, entity_type="ticket", entity_id=str(row["id"]), event_type="scheduler_stage_claimed", actor_id=owner, payload={"claim_id": claim_id, "stage": "triage", "candidate_identity": identity})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def complete_scheduler_triage_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or not str(claim["stage"]).startswith("triage:"):
                raise ValueError("scheduler claim is not triage")
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            identity = json.loads(str(claim["candidate_identity_json"] or ""))
            if result.get("candidate_identity") != identity:
                raise RuntimeError("triage result candidate identity does not match scheduler claim")
            attempt_number = int(identity["attempt_number"])
            model_stage = conn.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='triage'", (claim["ticket_id"], attempt_number)).fetchone()
            applied = conn.execute("SELECT * FROM runtime_stages WHERE ticket_id=? AND stage=?", (claim["ticket_id"], f"triage-applied-{attempt_number}")).fetchone()
            if model_stage is None or applied is None:
                raise RuntimeError("triage_reconciliation_required: triage output/application is not durably recorded")
            identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if str(model_stage["diff_hash"]) != identity_hash:
                raise RuntimeError("triage_reconciliation_required: triage model stage identity drift")
            artifact = Path(str(model_stage["response_artifact"] or ""))
            if not artifact.is_file() or str(result.get("triage_artifact") or "") != str(artifact):
                raise RuntimeError("triage_reconciliation_required: triage artifact is missing or conflicts")
            try:
                envelope = json.loads(artifact.read_text(encoding="utf-8"))
                raw_payload = envelope["payload"]
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("triage_reconciliation_required: triage artifact is invalid") from exc
            proposal_hash = hashlib.sha256(json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if proposal_hash != str(result.get("proposal_hash") or ""):
                raise RuntimeError("triage_reconciliation_required: triage proposal hash conflicts")
            if str(applied["detail"]) != encoded:
                raise RuntimeError("triage_reconciliation_required: applied triage result conflicts")
            if claim["side_effect_completed_at"] is not None:
                if claim["result_json"] != encoded:
                    raise RuntimeError("triage completed effect result conflicts")
                return dict(claim)
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL",
                (now, encoded, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(conn, entity_type="ticket", entity_id=str(claim["ticket_id"]), event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id": claim_id, "stage": "triage", "result": result})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def claim_next_scheduler_acceptance(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        """Claim one reviewed pass candidate for pre-commit acceptance/freeze."""
        if not owner or lease_seconds < 1:
            raise ValueError("acceptance scheduler claim requires owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute(
                "SELECT * FROM scheduler_stage_claims WHERE stage LIKE 'acceptance:%' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
                (now,),
            ).fetchone()
            if replay is not None:
                changed = conn.execute(
                    "UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?",
                    (owner, now + lease_seconds, now, replay["claim_id"], now),
                )
                if changed.rowcount != 1:
                    return None
                conn.execute("UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?", (owner, now + lease_seconds, now, replay["ticket_id"]))
                self._append_event(conn, entity_type="ticket", entity_id=str(replay["ticket_id"]), event_type="scheduler_stage_reclaimed", actor_id=owner, payload={"claim_id": replay["claim_id"], "stage": "acceptance", "lease_expires_at": now + lease_seconds})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())

            row = conn.execute("""
                SELECT t.id, rr.attempt_number, rc.candidate_fingerprint, rc.runtime_identity_json,
                       rr.id AS review_result_id, rr.verdict, rr.payload_json
                FROM tickets t
                JOIN runtime_stages route ON route.ticket_id=t.id AND route.stage=('repair-routing-' || route.attempt_number)
                JOIN review_results rr ON rr.ticket_id=t.id AND rr.attempt_number=route.attempt_number
                JOIN review_candidates rc ON rc.ticket_id=t.id AND rc.attempt_number=route.attempt_number
                WHERE t.state='local_review'
                  AND json_valid(route.detail)=1
                  AND json_extract(route.detail,'$.action')='pass'
                  AND rr.verdict='pass'
                  AND NOT EXISTS (SELECT 1 FROM accepted_candidates ac WHERE ac.ticket_id=t.id)
                  AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('acceptance:' || rr.attempt_number))
                ORDER BY t.created_at,t.id LIMIT 1
            """).fetchone()
            if row is None:
                return None
            identity = {
                "ticket_id": str(row["id"]),
                "attempt_number": int(row["attempt_number"]),
                "candidate_fingerprint": str(row["candidate_fingerprint"]),
                "review_result_id": int(row["review_result_id"]),
                "review_payload_hash": hashlib.sha256(str(row["payload_json"]).encode()).hexdigest(),
                "runtime_identity_hash": hashlib.sha256(str(row["runtime_identity_json"]).encode()).hexdigest(),
            }
            encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            stage = f"acceptance:{identity['attempt_number']}"
            claim_id = hashlib.sha256((stage + ":" + encoded).encode()).hexdigest()[:32]
            if conn.execute("UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND state='local_review' AND (lease_expires_at IS NULL OR lease_expires_at<=?)", (owner,now+lease_seconds,now,row["id"],now)).rowcount != 1:
                return None
            conn.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,'claimed',?,?,1,?,?,?)", (claim_id,row["id"],stage,owner,now+lease_seconds,encoded,now,now))
            self._append_event(conn, entity_type="ticket", entity_id=str(row["id"]), event_type="scheduler_stage_claimed", actor_id=owner, payload={"claim_id":claim_id,"stage":"acceptance","candidate_identity":identity})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def apply_scheduler_acceptance_effect(self, claim_id: str, owner: str, *, current_diff_hash: str, now: int | None = None) -> dict[str, Any]:
        """Atomically freeze the exact reviewed candidate without creating a Git commit."""
        now = self._now() if now is None else now
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or not str(claim["stage"]).startswith("acceptance:"):
                raise ValueError("scheduler claim is not acceptance")
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            if claim["side_effect_completed_at"] is not None:
                return dict(claim)
            try:
                identity = json.loads(str(claim["candidate_identity_json"] or ""))
                attempt_number = int(identity["attempt_number"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("acceptance_reconciliation_required: claim identity malformed") from exc
            ticket_id = str(claim["ticket_id"])
            ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            attempt = conn.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id,attempt_number)).fetchone()
            implementation = conn.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='implementation'", (ticket_id,attempt_number)).fetchone()
            validation = conn.execute("SELECT * FROM runtime_stages WHERE ticket_id=? AND stage=?", (ticket_id,f"validation-{attempt_number}")).fetchone()
            review_stage = conn.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='review'", (ticket_id,attempt_number)).fetchone()
            review = conn.execute("SELECT * FROM review_results WHERE ticket_id=? AND attempt_number=?", (ticket_id,attempt_number)).fetchone()
            candidate = conn.execute("SELECT * FROM review_candidates WHERE ticket_id=? AND attempt_number=?", (ticket_id,attempt_number)).fetchone()
            route = conn.execute("SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage=?", (ticket_id,f"repair-routing-{attempt_number}")).fetchone()
            if any(item is None for item in (ticket, attempt, implementation, validation, review_stage, review, candidate, route)):
                raise RuntimeError("acceptance_reconciliation_required: acceptance evidence is incomplete")
            if ticket["state"] != CanonicalState.LOCAL_REVIEW.value or review["verdict"] != "pass":
                raise RuntimeError("acceptance_reconciliation_required: ticket/review is not pass-eligible")
            try:
                route_detail = json.loads(str(route["detail"])); validation_detail = json.loads(str(validation["detail"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("acceptance_reconciliation_required: acceptance evidence is malformed") from exc
            if route_detail.get("action") != "pass" or not bool(validation_detail.get("passed")):
                raise RuntimeError("acceptance_reconciliation_required: pass routing/validation missing")
            if str(candidate["candidate_fingerprint"]) != str(identity["candidate_fingerprint"]) or str(current_diff_hash) != str(identity["candidate_fingerprint"]):
                raise RuntimeError("acceptance_reconciliation_required: candidate worktree drift")
            if int(review["id"]) != int(identity["review_result_id"]) or hashlib.sha256(str(review["payload_json"]).encode()).hexdigest() != str(identity["review_payload_hash"]):
                raise RuntimeError("acceptance_reconciliation_required: review result drift")
            if hashlib.sha256(str(candidate["runtime_identity_json"]).encode()).hexdigest() != str(identity["runtime_identity_hash"]):
                raise RuntimeError("acceptance_reconciliation_required: runtime identity drift")

            implementation_artifact = Path(str(implementation["response_artifact"] or ""))
            validation_artifact = Path(str(validation["artifact_path"] or ""))
            review_artifact = Path(str(review_stage["response_artifact"] or ""))
            if not implementation_artifact.is_file() or not validation_artifact.is_file() or not review_artifact.is_file():
                raise RuntimeError("acceptance_reconciliation_required: acceptance artifact missing")
            implementation_sha = hashlib.sha256(implementation_artifact.read_bytes()).hexdigest()
            validation_sha = hashlib.sha256(validation_artifact.read_bytes()).hexdigest()
            review_sha = hashlib.sha256(review_artifact.read_bytes()).hexdigest()
            if validation_sha != str(validation["artifact_sha256"]):
                raise RuntimeError("acceptance_reconciliation_required: validation artifact drift")
            validation_identity = validation_detail.get("candidate_identity")
            expected_validation_identity = {
                "ticket_id": ticket_id,
                "attempt_number": attempt_number,
                "implementation_artifact": str(implementation_artifact),
                "implementation_artifact_sha256": implementation_sha,
                "worktree_path": str(attempt["worktree_path"] or ""),
                "base_sha": str(attempt["base_sha"] or implementation["base_sha"] or ""),
                "implementation_diff_hash": str(candidate["candidate_fingerprint"]),
                "validation_policy_hash": self._validation_policy_hash(ticket),
            }
            if validation_identity != expected_validation_identity or str(implementation["diff_hash"]) != str(candidate["candidate_fingerprint"]):
                raise RuntimeError("acceptance_reconciliation_required: implementation/validation identity drift")
            if str(review_stage["diff_hash"]) != str(candidate["candidate_fingerprint"]):
                raise RuntimeError("acceptance_reconciliation_required: review candidate drift")
            try:
                review_envelope = json.loads(review_artifact.read_text(encoding="utf-8"))
                review_payload = json.loads(str(review["payload_json"]))
            except (OSError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("acceptance_reconciliation_required: invalid review artifact") from exc
            if not isinstance(review_envelope, dict) or review_envelope.get("payload") != review_payload:
                raise RuntimeError("acceptance_reconciliation_required: review artifact/result drift")
            evidence = {
                "ticket_id": ticket_id,
                "attempt_number": attempt_number,
                "candidate_fingerprint": str(candidate["candidate_fingerprint"]),
                "base_sha": str(attempt["base_sha"] or implementation["base_sha"] or ""),
                "worktree_path": str(attempt["worktree_path"] or ""),
                "implementation_artifact": str(implementation_artifact),
                "implementation_artifact_sha256": implementation_sha,
                "validation_artifact": str(validation_artifact),
                "validation_artifact_sha256": validation_sha,
                "review_artifact": str(review_artifact),
                "review_artifact_sha256": review_sha,
                "review_result_id": int(review["id"]),
            }
            evidence_hash = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            existing = conn.execute("SELECT * FROM accepted_candidates WHERE ticket_id=?", (ticket_id,)).fetchone()
            if existing is not None:
                if str(existing["evidence_hash"]) != evidence_hash:
                    raise RuntimeError("acceptance_reconciliation_required: accepted candidate conflicts")
            else:
                conn.execute("INSERT INTO accepted_candidates(ticket_id,attempt_number,candidate_fingerprint,base_sha,worktree_path,implementation_artifact,implementation_artifact_sha256,validation_artifact,validation_artifact_sha256,review_artifact,review_artifact_sha256,review_result_id,evidence_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ticket_id,attempt_number,evidence["candidate_fingerprint"],evidence["base_sha"],evidence["worktree_path"],evidence["implementation_artifact"],implementation_sha,evidence["validation_artifact"],validation_sha,evidence["review_artifact"],review_sha,evidence["review_result_id"],evidence_hash,now))
                validate_transition(CanonicalState.LOCAL_REVIEW, CanonicalState.ACCEPTED)
                if conn.execute("UPDATE tickets SET state=?,updated_at=? WHERE id=? AND state=?", (CanonicalState.ACCEPTED.value,now,ticket_id,CanonicalState.LOCAL_REVIEW.value)).rowcount != 1:
                    raise RuntimeError("ticket changed concurrently")
                event_id = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="state_transition", actor_id="acceptance", from_state=CanonicalState.LOCAL_REVIEW.value, to_state=CanonicalState.ACCEPTED.value, payload={"attempt_number":attempt_number,"candidate_fingerprint":evidence["candidate_fingerprint"],"evidence_hash":evidence_hash})
                if CanonicalState.ACCEPTED.value in self._PROJECTABLE_STATES:
                    self._enqueue_projection_bundle_in_transaction(conn, ticket_id=ticket_id, event_id=event_id, evidence=f"candidate accepted {evidence_hash}")
            result = {**evidence, "evidence_hash": evidence_hash}
            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
            if conn.execute("UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL", (now,encoded,now,claim_id,owner,now)).rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id":claim_id,"stage":"acceptance","result":result})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def accepted_candidate(self, ticket_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM accepted_candidates WHERE ticket_id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def git_commit_intent(self, ticket_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM git_commit_intents WHERE ticket_id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def git_commit_evidence(self, ticket_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM git_commit_evidence WHERE ticket_id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def native_dependency_graph(self, ticket_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM native_dependency_graphs WHERE ticket_id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def native_dependency_release(self, ticket_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM native_dependency_releases WHERE ticket_id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def _signer_enrollment_integrity_blocker(self, *, signer_public_key: bytes, signer_fingerprint: str, fresh_config_raw: bytes | None, fresh_config_identity: dict[str, Any] | None, fresh_config_obj: dict[str, Any] | None, config_path: Path | None) -> dict[str, Any] | None:
        """Validate the signed enrollment envelope before any release fallback runs.

        Every ledger copy is an assertion about one canonical signed document.  The
        signer key and the freshly read operator config are the only authority; DB
        fields, including evidence authority_hash values, are never inputs to the
        expected identity.
        """
        try:
            from .native_release_approval import fingerprint_public_key
            from .signer_enrollment import parse_enrollment_document, _repository_identity
            from .operator_config import load_operator_config
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            fresh_repository_identity = None
            if config_path is not None:
                fresh_repository_identity = _repository_identity(load_operator_config(Path(config_path)).canonical_repository)
            expected_authority_hash = hashlib.sha256(signer_fingerprint.encode("ascii")).hexdigest()
            if fingerprint_public_key(signer_public_key) != signer_fingerprint:
                return {"ticket_id": "", "reason": "signer enrollment reconciliation required: signer fingerprint does not match fresh signer key"}
            if fresh_config_obj is not None and (fresh_config_obj.get("operator_signing_key_fingerprint") != signer_fingerprint or fresh_config_obj.get("operator_signing_public_key") != base64.b64encode(signer_public_key).decode("ascii")):
                return {"ticket_id": "", "reason": "signer enrollment reconciliation required: fresh signer config authority differs"}
            intents = self.connection.execute("SELECT * FROM runtime_signer_enrollment_intents WHERE status IN ('pending_config','config_written','finalized') ORDER BY enrollment_key").fetchall()
            for intent in intents:
                ticket_hint = str(intent["ticket_ids_json"] or "")
                try:
                    document_raw = str(intent["document_json"]).encode("utf-8")
                    document, canonical = parse_enrollment_document(document_raw)
                    if canonical != document_raw or hashlib.sha256(canonical).hexdigest() != str(intent["document_hash"]):
                        raise ValueError("intent signed document bytes or hash drift")
                    signature_b64 = str(intent["detached_signature"])
                    signature = base64.b64decode(signature_b64, validate=True)
                    Ed25519PublicKey.from_public_bytes(signer_public_key).verify(signature, canonical)
                    selected = document["ticket_ids"]
                    selected_json = json.loads(str(intent["ticket_ids_json"]))
                    signed_selected = document["binding_projection_release_identities"]
                    persisted_selected = json.loads(str(intent["selected_bindings_json"]))
                    expected = {
                        "operator_id": document["operator_id"], "reason": document["reason"],
                        "ticket_ids_json": json.dumps(selected, separators=(",", ":")),
                        "public_key_fingerprint": signer_fingerprint, "authority_hash": expected_authority_hash,
                        "old_config_hash": document["old_config_hash"], "new_config_hash": document["new_config_hash"],
                        "new_config_bytes": document["new_config_bytes"], "selected_bindings_json": json.dumps(signed_selected, sort_keys=True, separators=(",", ":")),
                        "ledger_identity": document["ledger_identity"], "old_config_identity_json": json.dumps(document["old_config_identity"], sort_keys=True, separators=(",", ":")),
                        "nonce": document["nonce"],
                    }
                    for field, value in expected.items():
                        actual = intent[field]
                        if field in {"selected_bindings_json", "old_config_identity_json"}:
                            if json.loads(str(actual)) != json.loads(value): raise ValueError(f"intent {field} drift")
                        elif str(actual) != str(value):
                            if field in {"new_config_hash", "new_config_bytes"}:
                                raise ValueError("signed new config differs from intent")
                            raise ValueError(f"intent {field} drift")
                    if selected_json != selected or persisted_selected != signed_selected or intent["status"] != "finalized":
                        raise ValueError("intent status or signed ticket set drift")
                    if fresh_config_raw is not None:
                        signed_new = base64.b64decode(document["new_config_bytes"], validate=True)
                        if signed_new != fresh_config_raw or hashlib.sha256(signed_new).hexdigest() != document["new_config_hash"]:
                            raise ValueError("signed config bytes differ from fresh config")
                    if fresh_config_identity is not None and json.loads(str(intent["config_identity_json"] or "null")) != fresh_config_identity:
                        raise ValueError("finalized config identity drift")
                    evidence = self.connection.execute("SELECT * FROM runtime_signer_enrollments WHERE enrollment_key=? ORDER BY ticket_id", (intent["enrollment_key"],)).fetchall()
                    if len(evidence) != len(selected) or {str(row["ticket_id"]) for row in evidence} != set(str(ticket) for ticket in selected) or len({str(row["ticket_id"]) for row in evidence}) != len(selected):
                        raise ValueError("enrollment evidence is missing, duplicated, or cross-linked")
                    events = self.connection.execute("SELECT * FROM events WHERE entity_type='controller' AND entity_id='controller' AND event_type='runtime_signer_enrollment_completed' AND json_extract(payload_json,'$.enrollment_key')=? ORDER BY id", (intent["enrollment_key"],)).fetchall()
                    if len(events) != 1:
                        raise ValueError("enrollment completion event is missing or duplicated")
                    if intent["updated_at"] < events[0]["created_at"] or any(row["created_at"] != intent["created_at"] for row in evidence):
                        raise ValueError("enrollment finalized timestamps drifted")
                    event_payload = json.loads(str(events[0]["payload_json"]))
                    from .signer_enrollment import validate_completion_event_payload
                    validate_completion_event_payload(
                        event_payload,
                        enrollment_key=str(intent["enrollment_key"]),
                        document=document_raw,
                        detached_signature=str(intent["detached_signature"]),
                        config_identity=json.loads(str(intent["config_identity_json"])),
                        ticket_ids=list(selected),
                        public_key_fingerprint=signer_fingerprint,
                        operator_id=document["operator_id"],
                        reason=document["reason"],
                    )
                    if str(events[0]["actor_id"]) != document["operator_id"]:
                        raise ValueError("enrollment completion event actor drift")
                    for evidence_row in evidence:
                        if any(evidence_row[field] != intent[field] for field in ("document_json", "document_hash", "detached_signature")):
                            raise ValueError("per-ticket signed document drift")
                        if evidence_row["operator_id"] != document["operator_id"] or evidence_row["reason"] != document["reason"] or evidence_row["public_key_fingerprint"] != signer_fingerprint or evidence_row["authority_hash"] != expected_authority_hash:
                            raise ValueError("per-ticket signer identity drift")
                        expected_binding = signed_selected.get(str(evidence_row["ticket_id"]))
                        if not isinstance(expected_binding, dict): raise ValueError("signed ticket cross-link drift")
                        old_binding = json.loads(str(evidence_row["old_binding_identity_json"]))
                        new_binding = json.loads(str(evidence_row["new_binding_identity_json"]))
                        binding = self.connection.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (evidence_row["ticket_id"],)).fetchone()
                        if binding is None or old_binding != expected_binding.get("binding"):
                            raise ValueError("binding evidence does not match signed authority")
                        if fresh_repository_identity is None or expected_binding["binding"].get("repository_identity") != fresh_repository_identity:
                            raise ValueError("canonical repository identity drift")
                        immutable = ("ticket_id", "repository_path", "starting_sha", "canonical_sha", "ownership_verified")
                        expected_new = {key: binding[key] for key in immutable} | {"repository_identity": expected_binding["binding"]["repository_identity"], "operator_signer_fingerprint": signer_fingerprint, "operator_authority_hash": expected_authority_hash}
                        if new_binding != expected_new or any(binding[key] != expected_binding["binding"].get(key) for key in immutable) or binding["operator_signer_fingerprint"] != signer_fingerprint or binding["operator_authority_hash"] != expected_authority_hash:
                            raise ValueError("runtime binding authority or immutable identity drift")
                        if evidence_row["config_identity_json"] != intent["config_identity_json"]:
                            raise ValueError("evidence config identity drift")
                except Exception as exc:
                    return {"ticket_id": ticket_hint, "reason": f"signer enrollment reconciliation required: {exc}"}
        except Exception as exc:
            return {"ticket_id": "", "reason": f"signer enrollment reconciliation required: {exc}"}
        return None

    def native_dependency_release_migration_required(self, *, signer_public_key: bytes | None = None, signer_fingerprint: str | None = None, config_path: Path | None = None, require_activation: bool = True, board: Any | None = None) -> dict[str, Any] | None:
        """Find legacy releases that cannot authorize downstream execution."""
        rows = self.connection.execute("SELECT * FROM native_dependency_releases ORDER BY ticket_id").fetchall()
        # A supersession marker is authority only when its complete signed envelope
        # and immutable event linkage survive independent verification.
        if signer_public_key is not None and signer_fingerprint:
            for link in self.connection.execute("SELECT * FROM native_dependency_release_revalidation_supersessions ORDER BY old_revalidation_id").fetchall():
                try:
                    old = self.connection.execute("SELECT * FROM native_dependency_release_revalidations WHERE revalidation_id=?", (link["old_revalidation_id"],)).fetchone()
                    new = self.connection.execute("SELECT * FROM native_dependency_release_revalidations WHERE revalidation_id=?", (link["new_revalidation_id"],)).fetchone()
                    event = self.connection.execute("SELECT * FROM events WHERE id=? AND entity_type='controller' AND entity_id='controller' AND event_type='native_dependency_release_revalidation_superseded'", (link["event_id"],)).fetchone()
                    if old is None or new is None or event is None or str(old["ticket_id"]) != str(link["ticket_id"]) or str(new["ticket_id"]) != str(link["ticket_id"]):
                        raise ValueError("supersession rows or event linkage are incomplete")
                    from .native_release_approval import parse_approval_document, verify_detached_signature
                    old_raw = str(old["approval_document_json"] or "").encode(); new_raw = str(new["approval_document_json"] or "").encode()
                    if hashlib.sha256(old_raw).hexdigest() != str(old["approval_document_hash"]) or hashlib.sha256(new_raw).hexdigest() != str(new["approval_document_hash"]):
                        raise ValueError("supersession signed document hash mismatch")
                    parse_approval_document(old_raw); parse_approval_document(new_raw)
                    verify_detached_signature(old_raw, base64.b64decode(str(old["detached_signature"]), validate=True), signer_public_key, str(old["signer_fingerprint"]))
                    verify_detached_signature(new_raw, base64.b64decode(str(new["detached_signature"]), validate=True), signer_public_key, str(new["signer_fingerprint"]))
                    payload = json.loads(str(event["payload_json"]))
                    evidence = payload.get("evidence") if isinstance(payload, dict) else None
                    expected_link = {"old_revalidation_id": str(old["revalidation_id"]), "new_revalidation_id": str(new["revalidation_id"]), "ticket_id": str(link["ticket_id"]), "reason": str(link["reason"]), "operator_id": str(link["operator_id"]), "old_snapshot_hash": str(old["snapshot_hash"]), "new_snapshot_hash": str(new["snapshot_hash"]), "old_snapshot_schema_version": int(old["snapshot_schema_version"] or 1), "new_snapshot_schema_version": int(new["snapshot_schema_version"] or 1), "old_approval_document_json": str(old["approval_document_json"] or ""), "new_approval_document_json": str(new["approval_document_json"] or ""), "old_approval_document_hash": str(old["approval_document_hash"] or ""), "new_approval_document_hash": str(new["approval_document_hash"] or "")}
                    if payload.get("evidence_hash") != str(link["evidence_hash"]) or evidence != expected_link or canonical_sha256(evidence) != str(link["evidence_hash"]) or str(link["detached_signature"]) != str(new["detached_signature"]):
                        raise ValueError("supersession event evidence is forged")
                except Exception as exc:
                    return {"ticket_id": str(link["ticket_id"]), "reason": f"native release revalidation supersession is invalid: {exc}"}

        missing_signer = self.connection.execute("""
            SELECT r.ticket_id FROM native_dependency_releases r
            JOIN runtime_bindings b ON b.ticket_id=r.ticket_id
            WHERE (b.operator_signer_fingerprint IS NULL OR b.operator_authority_hash IS NULL)
              AND json_valid(r.routing_authority_json)=1 AND json(r.routing_authority_json)='{}'
            ORDER BY r.ticket_id LIMIT 1
        """).fetchone()
        if missing_signer is not None:
            return {"ticket_id": str(missing_signer["ticket_id"]), "reason": "legacy release requires completed signer enrollment"}
        if (signer_public_key is None) != (signer_fingerprint is None):
            signer_public_key = None
            signer_fingerprint = None
        if signer_public_key is not None:
            try:
                from .native_release_approval import fingerprint_public_key
                if fingerprint_public_key(signer_public_key) != signer_fingerprint:
                    signer_public_key = None
                    signer_fingerprint = None
            except (TypeError, ValueError):
                signer_public_key = None
                signer_fingerprint = None
        fresh_config_hash = None
        fresh_config_identity = None
        fresh_config_raw = None
        fresh_config_obj = None
        fresh_repository_identity = None
        if config_path is not None and signer_fingerprint is not None:
            try:
                from .operator_config import _raw_config
                fresh_config_raw, fresh_obj, fresh_config_identity = _raw_config(Path(config_path))
                fresh_config_obj = fresh_obj
                from .signer_enrollment import _repository_identity
                from .operator_config import load_operator_config
                fresh_loaded = load_operator_config(Path(config_path))
                fresh_repository_identity = _repository_identity(fresh_loaded.canonical_repository)
                fresh_config_hash = hashlib.sha256(fresh_config_raw).hexdigest()
                if fresh_obj.get("operator_signing_key_fingerprint") != signer_fingerprint or fresh_loaded.signer_public_key_bytes != signer_public_key:
                    return {"ticket_id": str(rows[0]["ticket_id"]) if rows else "", "reason": "signer enrollment reconciliation required: fresh external signer config differs"}
            except (OSError, ValueError):
                return {"ticket_id": str(rows[0]["ticket_id"]) if rows else "", "reason": "signer enrollment reconciliation required: fresh external signer config is unavailable"}
        integrity_blocker = None
        if signer_public_key is not None and signer_fingerprint:
            integrity_blocker = self._signer_enrollment_integrity_blocker(
                signer_public_key=signer_public_key,
                signer_fingerprint=signer_fingerprint,
                fresh_config_raw=fresh_config_raw,
                fresh_config_identity=fresh_config_identity,
                fresh_config_obj=fresh_config_obj,
                config_path=config_path,
            )
        if integrity_blocker is not None:
            return integrity_blocker
        # Activation status is never authority.  Acknowledgement is valid only
        # when the immutable intent, one evidence row, and one immutable event
        # form the exact same signed envelope.
        for activation in self.connection.execute("SELECT * FROM native_release_activation_intents WHERE status='acknowledged' ORDER BY request_key").fetchall():
            try:
                evidence = self.connection.execute("SELECT * FROM native_release_activation_evidence WHERE request_key=?", (activation["request_key"],)).fetchall()
                if len(evidence) != 1:
                    return {"ticket_id": str(activation["ticket_id"]), "reason": "native release activation acknowledgement evidence is missing or duplicated"}
                ev = evidence[0]
                event = self.connection.execute("SELECT * FROM events WHERE id=? AND entity_type='controller' AND entity_id='controller' AND event_type='native_dependency_release_activation_acknowledged'", (ev["event_id"],)).fetchall()
                if len(event) != 1:
                    return {"ticket_id": str(activation["ticket_id"]), "reason": "native release activation acknowledgement event is missing or forged"}
                payload = json.loads(str(event[0]["payload_json"]))
                expected = {"request_key": str(activation["request_key"]), "ticket_id": str(activation["ticket_id"]), "revalidation_id": str(activation["revalidation_id"]), "activation_marker": str(activation["activation_marker"]), "post_activation_snapshot_hash": str(activation["effect_snapshot_hash"]), "post_activation_snapshot_json": ev["post_activation_snapshot_json"], "evidence_hash": str(ev["evidence_hash"])}
                if any(ev[key] != activation[key] for key in ("request_key", "ticket_id", "revalidation_id", "activation_marker")) or ev["post_activation_snapshot_hash"] != activation["effect_snapshot_hash"] or event[0]["id"] != ev["event_id"] or canonical_sha256({k: expected[k] for k in expected if k != "evidence_hash"}) != str(ev["evidence_hash"]) or payload != expected:
                    return {"ticket_id": str(activation["ticket_id"]), "reason": "native release activation acknowledgement evidence or event hash mismatch"}
                if board is not None:
                    from .native_release_approval import canonical_snapshot_json, validate_activation_continuation_snapshot, validate_activation_post_snapshot
                    if not ev["post_activation_snapshot_json"]:
                        return {"ticket_id": str(activation["ticket_id"]), "reason": "native release activation canonical post snapshot is missing"}
                    current = board.execution_snapshot(str(activation["external_task_id"]))
                    current_json = canonical_snapshot_json(current)
                    if hashlib.sha256(current_json.encode("utf-8")).hexdigest() != str(ev["post_activation_snapshot_hash"]):
                        pre = json.loads(str(activation["pre_activation_snapshot_json"] or "null"))
                        marker = bool(board.activation_marker_present(str(activation["external_task_id"]), str(activation["activation_marker"])))
                        try:
                            validate_activation_post_snapshot(pre, json.loads(str(ev["post_activation_snapshot_json"])), marker_present=marker, marker=str(activation["activation_marker"]))
                            observation = self.connection.execute("SELECT * FROM native_release_activation_running_observations WHERE request_key=?", (activation["request_key"],)).fetchone()
                            continuation = validate_activation_continuation_snapshot(
                                json.loads(str(ev["post_activation_snapshot_json"])), json.loads(current_json),
                                acknowledged_at=int(ev["acknowledged_at"]), profile=str(activation["implementation_profile"]),
                                workspace_path=str(activation["canonical_worktree_path"]), branch=str(activation["branch"]),
                                repository_identity=str(activation["repository_identity"]), base_sha=str(activation["base_sha"]),
                                handoff_summary="local-first-awaiting-reconciliation",
                                prior_running_observation=None if observation is None else dict(observation),
                            )
                        except Exception as exc:
                            return {"ticket_id": str(activation["ticket_id"]), "reason": f"native release activation board continuation is invalid: {exc}"}
                        if continuation == "terminal":
                            return {"ticket_id": str(activation["ticket_id"]), "reason": "authorized terminal Hermes handoff requires reconciliation"}
                    else:
                        if current_json != str(ev["post_activation_snapshot_json"]):
                            return {"ticket_id": str(activation["ticket_id"]), "reason": "native release activation board post snapshot drift"}
                        pre = json.loads(str(activation["pre_activation_snapshot_json"] or "null"))
                        marker = bool(board.activation_marker_present(str(activation["external_task_id"]), str(activation["activation_marker"])))
                        validate_activation_post_snapshot(pre, json.loads(current_json), marker_present=marker, marker=str(activation["activation_marker"]))
                from .native_release_approval import parse_approval_document, verify_detached_signature
                raw = str(activation["approval_document_json"] or "").encode("utf-8")
                sig = base64.b64decode(str(activation["detached_signature"] or ""), validate=True)
                if hashlib.sha256(raw).hexdigest() != str(activation["approval_document_hash"]) or str(activation["signer_fingerprint"]) != str(signer_fingerprint):
                    raise ValueError("activation signed document hash or signer drift")
                document = parse_approval_document(raw)
                signed = document["authority"]
                expected_signed = {"ticket_id": str(activation["ticket_id"]), "revalidation_id": str(activation["revalidation_id"]), "external_task_id": str(activation["external_task_id"]), "implementation_profile": str(activation["implementation_profile"]), "repository_identity": str(activation["repository_identity"]), "canonical_worktree_path": str(activation["canonical_worktree_path"]), "branch": str(activation["branch"]), "base_sha": str(activation["base_sha"]), "pre_snapshot_hash": str(activation["pre_activation_snapshot_hash"]), "activation_marker": str(activation["activation_marker"])}
                if any(signed.get(key) != value for key, value in expected_signed.items()) or signed.get("board", {}).get("path") != str(activation["board_path"]) or int(signed.get("board", {}).get("dev", -1)) != int(activation["board_dev"]) or int(signed.get("board", {}).get("ino", -1)) != int(activation["board_ino"]):
                    raise ValueError("activation signed document does not match intent")
                verify_detached_signature(raw, sig, signer_public_key, str(activation["signer_fingerprint"]))
            except Exception as exc:
                return {"ticket_id": str(activation["ticket_id"]), "reason": f"native release activation authority is invalid: {exc}"}
        # A caller-supplied key/fingerprint is insufficient for an otherwise
        # valid activation. Migration and scheduling must freshly reload the
        # external signer configuration file.
        if self.connection.execute("SELECT 1 FROM native_release_activation_intents WHERE status='acknowledged' LIMIT 1").fetchone() is not None and config_path is None:
            return {"ticket_id": str(rows[0]["ticket_id"]) if rows else "", "reason": "native release activation requires freshly loaded external signer configuration"}
        for row in rows:
            try:
                authority = json.loads(str(row["routing_authority_json"] or "{}"))
            except json.JSONDecodeError:
                authority = None
            if not isinstance(authority, dict) or not authority.get("profile") or not authority.get("canonical_repository"):
                ticket_id = str(row["ticket_id"])
                if signer_public_key is None or not signer_fingerprint:
                    return {"ticket_id": ticket_id, "reason": "legacy release requires externally registered signer authority"}
                # A non-NULL binding is not enrollment authority.  Require one
                # complete, signed, immutable enrollment envelope for the whole
                # selected ticket set before legacy execution can proceed.
                try:
                    enrollments = self.connection.execute("""
                        SELECT e.*, i.status, i.ticket_ids_json, i.selected_bindings_json, i.old_config_hash, i.new_config_hash,
                               i.new_config_bytes AS intent_new_config_bytes, i.config_identity_json AS intent_config_identity_json,
                               i.old_config_identity_json AS intent_old_config_identity_json,
                               i.document_json AS intent_document_json, i.document_hash AS intent_document_hash,
                               i.detached_signature AS intent_detached_signature,
                               i.public_key_fingerprint AS intent_fingerprint, i.authority_hash AS intent_authority_hash
                        FROM runtime_signer_enrollments e
                        JOIN runtime_signer_enrollment_intents i ON i.enrollment_key=e.enrollment_key
                        WHERE e.ticket_id=?
                    """, (ticket_id,)).fetchall()
                    if len(enrollments) != 1:
                        # Pre-enrollment fixtures may already contain a complete
                        # independently signed native-release revalidation. Keep
                        # that older authenticated evidence readable; unsigned or
                        # partial direct-SQL upgrades still stop below.
                        legacy_signed = self.connection.execute("SELECT 1 FROM native_dependency_release_revalidations WHERE ticket_id=? AND signer_fingerprint=? AND detached_signature IS NOT NULL AND approval_document_hash IS NOT NULL", (ticket_id, signer_fingerprint)).fetchone()
                        if legacy_signed is None:
                            return {"ticket_id": ticket_id, "reason": "legacy release signer enrollment evidence is missing, pending, or duplicated"}
                        raise _LegacyRevalidationCompatibility
                    enrollment = enrollments[0]
                    from .signer_enrollment import parse_enrollment_document
                    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
                    signed_document_raw = str(enrollment["document_json"]).encode("utf-8")
                    signed_document, canonical_document = parse_enrollment_document(signed_document_raw)
                    signed_new_raw = base64.b64decode(str(signed_document["new_config_bytes"]), validate=True)
                    signed_new_hash = hashlib.sha256(signed_new_raw).hexdigest()
                    signed_new_b64 = base64.b64encode(signed_new_raw).decode("ascii")
                    signed_signature = base64.b64decode(str(enrollment["detached_signature"]), validate=True)
                    Ed25519PublicKey.from_public_bytes(signer_public_key).verify(signed_signature, canonical_document)
                    if (canonical_document != signed_document_raw
                            or hashlib.sha256(canonical_document).hexdigest() != str(enrollment["document_hash"])
                            or signed_new_hash != str(signed_document["new_config_hash"])
                            or str(enrollment["new_config_hash"]) != signed_new_hash
                            or str(enrollment["intent_new_config_bytes"] or "") != signed_new_b64
                            or json.loads(str(enrollment["intent_old_config_identity_json"] or "null")) != signed_document["old_config_identity"]
                            or (fresh_config_raw is not None and (signed_new_hash != fresh_config_hash or signed_new_raw != fresh_config_raw))):
                        return {"ticket_id": ticket_id, "reason": "signer enrollment reconciliation required: signed new config bytes/hash differ from persisted or live config"}
                    if signed_document.get("new_fingerprint") != signer_fingerprint:
                        return {"ticket_id": ticket_id, "reason": "signer enrollment reconciliation required: signed signer identity differs"}
                    selected_ids = tuple(json.loads(str(enrollment["ticket_ids_json"])))
                    if not selected_ids or len(set(selected_ids)) != len(selected_ids):
                        return {"ticket_id": ticket_id, "reason": "legacy signer enrollment ticket set is invalid"}
                    all_evidence = self.connection.execute("SELECT * FROM runtime_signer_enrollments WHERE enrollment_key=? ORDER BY ticket_id", (enrollment["enrollment_key"],)).fetchall()
                    if enrollment["status"] != "finalized" or (fresh_config_hash is not None and enrollment["new_config_hash"] != fresh_config_hash) or tuple(str(x) for x in selected_ids) != tuple(str(x["ticket_id"]) for x in all_evidence) or len(all_evidence) != len(selected_ids):
                        return {"ticket_id": ticket_id, "reason": "signer enrollment reconciliation required: finalized enrollment state or config hash is not authoritative"}
                    if any(x["document_hash"] != enrollment["document_hash"] or x["detached_signature"] != enrollment["detached_signature"] or x["document_json"] != enrollment["document_json"] or str(x["config_identity_json"] or "") != str(enrollment["config_identity_json"] or "") for x in all_evidence):
                        return {"ticket_id": ticket_id, "reason": "signer enrollment reconciliation required: per-ticket enrollment evidence document or config identity drift"}
                    from .signer_enrollment import parse_enrollment_document
                    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
                    document = parse_enrollment_document(str(enrollment["document_json"]))[0]
                    document_bytes = str(enrollment["document_json"]).encode("utf-8")
                    if hashlib.sha256(document_bytes).hexdigest() != str(enrollment["document_hash"]):
                        return {"ticket_id": ticket_id, "reason": "legacy signer enrollment document hash drift"}
                    signed_selected = document.get("binding_projection_release_identities")
                    persisted_selected = json.loads(str(enrollment["selected_bindings_json"]))
                    if signed_selected != persisted_selected:
                        return {"ticket_id": ticket_id, "reason": "signed selected bindings differ from enrollment authority"}
                    if document.get("new_fingerprint") != signer_fingerprint or document.get("new_fingerprint") != enrollment["intent_fingerprint"] or document.get("new_fingerprint") != enrollment["public_key_fingerprint"]:
                        return {"ticket_id": ticket_id, "reason": "legacy signer enrollment fingerprint drift"}
                    signature = base64.b64decode(str(enrollment["detached_signature"]), validate=True)
                    Ed25519PublicKey.from_public_bytes(signer_public_key).verify(signature, document_bytes)
                    from .signer_enrollment import validate_completion_event_payload
                    event_rows = self.connection.execute("SELECT actor_id,payload_json FROM events WHERE entity_type='controller' AND entity_id='controller' AND event_type='runtime_signer_enrollment_completed'").fetchall()
                    matching = []
                    expected_event_config_identity = json.loads(str(enrollment["config_identity_json"] or "null"))
                    for event in event_rows:
                        payload = json.loads(str(event["payload_json"]))
                        if not isinstance(payload, dict) or payload.get("enrollment_key") != enrollment["enrollment_key"]:
                            continue
                        try:
                            validate_completion_event_payload(
                                payload,
                                enrollment_key=str(enrollment["enrollment_key"]),
                                document=document_bytes,
                                detached_signature=str(enrollment["detached_signature"]),
                                config_identity=expected_event_config_identity,
                                ticket_ids=list(selected_ids),
                                public_key_fingerprint=str(enrollment["public_key_fingerprint"]),
                                operator_id=str(enrollment["operator_id"]),
                                reason=str(enrollment["reason"]),
                            )
                            if str(event["actor_id"]) == str(enrollment["operator_id"]):
                                matching.append(payload)
                        except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error):
                            continue
                    if len(matching) != 1 or document.get("ticket_ids") != list(selected_ids) or document.get("old_config_hash") != enrollment["old_config_hash"]:
                        return {"ticket_id": ticket_id, "reason": "signer enrollment reconciliation required: enrollment event is missing, forged, or outside signed config authority"}
                    binding = self.connection.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
                    evidence = self.connection.execute("SELECT * FROM runtime_signer_enrollments WHERE enrollment_key=? AND ticket_id=?", (enrollment["enrollment_key"], ticket_id)).fetchone()
                    new_binding = json.loads(str(evidence["new_binding_identity_json"]))
                    if any(new_binding.get(key) != binding[key] for key in ("repository_path", "starting_sha", "canonical_sha", "ownership_verified", "operator_signer_fingerprint", "operator_authority_hash")) or new_binding.get("repository_identity") != fresh_repository_identity:
                        return {"ticket_id": ticket_id, "reason": "legacy signer binding identity drift"}
                    expected_selected = signed_selected.get(ticket_id) if isinstance(signed_selected, dict) else None
                    if not isinstance(expected_selected, dict):
                        return {"ticket_id": ticket_id, "reason": "signed binding projection release evidence mismatch"}
                    if json.loads(str(evidence["old_binding_identity_json"])) != expected_selected.get("binding"):
                        return {"ticket_id": ticket_id, "reason": "enrollment evidence binding differs from signed authority"}
                    if expected_selected != {"binding": {k: binding[k] for k in ("ticket_id", "repository_path", "starting_sha", "canonical_sha", "ownership_verified")} | {"repository_identity": fresh_repository_identity}, "projection": {k: expected_selected.get("projection", {}).get(k) for k in ("event_id", "idempotency_key", "external_task_id")}, "release": expected_selected.get("release")}:
                        return {"ticket_id": ticket_id, "reason": "signed binding projection release evidence mismatch"}
                    current_release = self.connection.execute("SELECT * FROM native_dependency_releases WHERE ticket_id=?", (ticket_id,)).fetchone()
                    current_projection = self.connection.execute("""
                        SELECT b.event_id,b.idempotency_key,b.external_task_id,e.entity_type,e.entity_id,e.event_type
                        FROM board_projection_outbox b JOIN events e ON e.id=b.event_id
                        WHERE b.ticket_id=? AND b.operation='create_microticket' AND b.acknowledged_at IS NOT NULL
                          AND b.superseded_at IS NULL AND b.external_task_id IS NOT NULL
                    """, (ticket_id,)).fetchall()
                    current_release_identity = None if current_release is None else {k: current_release[k] for k in ("ticket_id", "graph_hash", "child_external_id", "parent_completion_hash", "routing_authority_json", "hermes_status")}
                    current_projection_identity = None if len(current_projection) != 1 else {
                        "event_id": int(current_projection[0]["event_id"]), "idempotency_key": str(current_projection[0]["idempotency_key"]), "external_task_id": str(current_projection[0]["external_task_id"])
                    }
                    if expected_selected.get("release") != current_release_identity or expected_selected.get("projection") != current_projection_identity or (current_projection and (str(current_projection[0]["entity_type"]) != "ticket" or str(current_projection[0]["entity_id"]) != ticket_id or str(current_projection[0]["event_type"]) not in {"generated_microticket_created", "generated_microticket_projection_recovered"})):
                        return {"ticket_id": ticket_id, "reason": "signer enrollment reconciliation required: signed ticket, projection, or release identity drift"}
                    if fresh_config_identity is not None:
                        if (json.loads(str(enrollment["intent_config_identity_json"] or "null")) != fresh_config_identity
                                or json.loads(str(enrollment["config_identity_json"] or "null")) != fresh_config_identity
                                or json.loads(str(evidence["config_identity_json"] or "null")) != fresh_config_identity):
                            return {"ticket_id": ticket_id, "reason": "signer enrollment reconciliation required: post-write config identity differs from finalized evidence"}
                except _LegacyRevalidationCompatibility:
                    pass
                except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error, InvalidSignature):
                    return {"ticket_id": ticket_id, "reason": "legacy signer enrollment signature or evidence is invalid"}
                current = self.connection.execute("""
                    SELECT r.*, b.event_id AS current_projection_event_id, b.idempotency_key AS current_projection_key,
                           b.external_task_id AS current_external_task_id, e.event_type AS current_projection_event_type,
                           rb.repository_path AS binding_repository_path,
                           re.id AS linked_event_id, re.entity_type AS linked_event_entity_type,
                           re.entity_id AS linked_event_entity_id, re.event_type AS linked_event_type,
                           re.payload_json AS linked_event_payload
                    FROM native_dependency_release_revalidations r
                    JOIN board_projection_outbox b ON b.ticket_id=r.ticket_id AND b.operation='create_microticket'
                      AND b.superseded_at IS NULL AND b.acknowledged_at IS NOT NULL AND b.external_task_id IS NOT NULL
                    JOIN events e ON e.id=b.event_id AND e.entity_type='ticket' AND e.entity_id=r.ticket_id
                    JOIN runtime_bindings rb ON rb.ticket_id=r.ticket_id
                    LEFT JOIN events re ON re.id=r.revalidation_event_id
                    WHERE r.ticket_id=? AND NOT EXISTS (SELECT 1 FROM native_dependency_release_revalidation_supersessions s WHERE s.old_revalidation_id=r.revalidation_id)
                """, (ticket_id,)).fetchall()
                if board is not None:
                    try:
                        live = board.execution_snapshot(str(current[0]["external_task_id"])) if len(current) == 1 else None
                        live_root = snapshot_authority(live)
                        if live_root["task"].get("status") not in {"scheduled", "blocked"} or canonical_sha256(live_root) != str(current[0]["snapshot_hash"]):
                            return {"ticket_id": ticket_id, "reason": "legacy release current full raw Hermes snapshot drift"}
                    except Exception as exc:
                        return {"ticket_id": ticket_id, "reason": f"legacy release current full raw Hermes snapshot unavailable: {exc}"}
                valid = [item for item in current if self._native_release_revalidation_row_valid(item, row, signer_public_key=signer_public_key, signer_fingerprint=signer_fingerprint)]
                linked_keys = {str(item["event_key"]) for item in current if item["event_key"] is not None}
                linked_events = self.connection.execute(
                    "SELECT id,payload_json FROM events WHERE entity_type='controller' AND entity_id='controller' AND event_type='native_dependency_release_revalidated'"
                ).fetchall()
                matching_events = []
                for event in linked_events:
                    try:
                        event_payload = json.loads(str(event["payload_json"]))
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if event_payload.get("event_key") in linked_keys:
                        matching_events.append(event)
                if len(matching_events) != 1:
                    valid = []
                if len(current) != 1 or len(valid) != 1:
                    return {"ticket_id": ticket_id, "reason": "legacy release routing authority requires paused operator revalidation"}
                if require_activation:
                    activation = self.connection.execute("SELECT status FROM native_release_activation_intents WHERE ticket_id=? AND revalidation_id=?", (ticket_id, valid[0]["revalidation_id"])).fetchall()
                    if len(activation) != 1 or activation[0]["status"] != "acknowledged":
                        return {"ticket_id": ticket_id, "reason": "legacy release requires acknowledged native release activation"}
        return None

    @staticmethod
    def _native_release_evidence_document(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": str(row["ticket_id"]),
            "revalidation_id": str(row["revalidation_id"]),
            "event_key": str(row["event_key"]),
            "release": {
                "graph_hash": str(row["release_graph_hash"]),
                "child_external_id": str(row["release_child_external_id"]),
                "parent_completion_hash": str(row["release_parent_completion_hash"]),
                "routing_authority_json": str(row["release_routing_authority_json"]),
                "hermes_status": str(row["release_hermes_status"]),
                "observed_at": int(row["release_observed_at"]),
            },
            "projection_event_id": int(row["projection_event_id"]),
            "projection_key": str(row["projection_key"]),
            "external_task_id": str(row["external_task_id"]),
            "implementation_profile": str(row["implementation_profile"]),
            "repository_identity": str(row["repository_identity"]),
            "canonical_worktree_path": str(row["canonical_worktree_path"]),
            "branch": str(row["branch"]),
            "base_sha": str(row["base_sha"]),
            "snapshot_hash": str(row["snapshot_hash"]),
            "operator_id": str(row["operator_id"]),
            "reason": str(row["reason"]),
        }

    @classmethod
    def _native_release_revalidation_row_valid(cls, row: sqlite3.Row, release: sqlite3.Row, *, signer_public_key: bytes | None = None, signer_fingerprint: str | None = None) -> bool:
        try:
            authority = json.loads(str(row["release_routing_authority_json"]))
            payload = json.loads(str(row["linked_event_payload"]))
            document = cls._native_release_evidence_document(row)
            projection_event_id = int(row["projection_event_id"])
            current_projection_event_id = int(row["current_projection_event_id"])
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return False
        expected_event_key = "native-release-revalidated:" + str(row["revalidation_id"])
        expected_payload = {"event_key": expected_event_key, "revalidation_id": str(row["revalidation_id"]), "evidence_hash": str(row["evidence_hash"]), "evidence": document}
        expected_payload.update({"approval_document_hash": str(row["approval_document_hash"]), "signer_fingerprint": str(row["signer_fingerprint"])})
        try:
            from .native_release_approval import parse_approval_document, verify_detached_signature
            if signer_public_key is None or not signer_fingerprint:
                return False
            if not isinstance(row["approval_document_json"], str) or not row["approval_document_json"].strip():
                return False
            if not isinstance(row["approval_document_hash"], str) or not row["approval_document_hash"].strip():
                return False
            if not isinstance(row["detached_signature"], str) or not row["detached_signature"].strip():
                return False
            if not isinstance(row["signer_fingerprint"], str) or row["signer_fingerprint"] != signer_fingerprint:
                return False
            raw = row["approval_document_json"].encode("utf-8")
            if hashlib.sha256(raw).hexdigest() != row["approval_document_hash"]:
                return False
            approval = parse_approval_document(raw)
            signature = base64.b64decode(row["detached_signature"], validate=True)
            if len(signature) != 64:
                return False
            verify_detached_signature(raw, signature, signer_public_key, signer_fingerprint)
            if approval["operator_id"] != str(row["operator_id"]) or approval["reason"] != str(row["reason"]):
                return False
            approval_authority = approval["authority"]
            if (approval_authority.get("ticket_id") != str(row["ticket_id"])
                    or approval_authority.get("implementation_profile") != str(row["implementation_profile"])
                    or approval_authority.get("repository_identity") != str(row["repository_identity"])
                    or approval_authority.get("canonical_worktree_path") != str(row["canonical_worktree_path"])
                    or approval_authority.get("branch") != str(row["branch"])
                    or approval_authority.get("base_sha") != str(row["base_sha"])
                    or approval_authority.get("snapshot_hash") != str(row["snapshot_hash"])
                    or int(approval_authority.get("snapshot_schema_version", 1)) != int(row["snapshot_schema_version"] or 1)
                    or approval_authority.get("projection") != {"event_id": int(row["projection_event_id"]), "key": str(row["projection_key"]), "external_id": str(row["external_task_id"])}
                    or approval_authority.get("release") != dict(release)):
                return False
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            return False
        result = (
            isinstance(authority, dict) and authority == {}
            and str(row["release_graph_hash"]) == str(release["graph_hash"])
            and str(row["release_child_external_id"]) == str(release["child_external_id"])
            and str(row["release_parent_completion_hash"]) == str(release["parent_completion_hash"])
            and str(row["release_hermes_status"]) == str(release["hermes_status"])
            and projection_event_id == current_projection_event_id
            and str(row["projection_key"]).strip() == str(row["current_projection_key"])
            and str(row["external_task_id"]) == str(row["current_external_task_id"])
            and str(row["current_projection_event_type"]) in {"generated_microticket_created", "generated_microticket_projection_recovered"}
            and str(row["repository_identity"]) == str(row["binding_repository_path"])
            and str(row["external_task_id"]) == str(release["child_external_id"])
            and str(row["repository_identity"]).strip() != ""
            and str(row["canonical_worktree_path"]).strip() != ""
            and str(row["branch"]).strip() != ""
            and str(row["linked_event_entity_type"]) == "controller"
            and str(row["linked_event_entity_id"]) == "controller"
            and str(row["linked_event_type"]) == "native_dependency_release_revalidated"
            and row["linked_event_id"] is not None
            and str(row["linked_event_id"]) == str(row["revalidation_event_id"])
            and str(row["event_key"]) == expected_event_key
            and payload == expected_payload
            and str(row["evidence_hash"]) == canonical_sha256(document)
        )
        return result

    def prepare_native_release_activation_intent(self, *, ticket_id: str, revalidation_id: str, external_task_id: str, pre_activation_snapshot_hash: str, implementation_profile: str, repository_identity: str, canonical_worktree_path: str, branch: str, base_sha: str, operator_id: str, reason: str, request_key: str, approval_document_json: str | None = None, approval_document_hash: str | None = None, detached_signature: bytes | None = None, signer_fingerprint: str | None = None, board_path: str | None = None, board_dev: int | None = None, board_ino: int | None = None, pre_activation_snapshot_json: str | None = None) -> dict[str, Any]:
        """Persist one paused activation intent before any Hermes board effect."""
        values = (ticket_id, revalidation_id, external_task_id, pre_activation_snapshot_hash, implementation_profile, repository_identity, canonical_worktree_path, branch, base_sha, operator_id, reason, request_key)
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError("native release activation requires complete identity, reason, and request key")
        marker = f"local-first-native-release-activation:{revalidation_id}:{request_key}"
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or not bool(paused["paused"]):
                raise PermissionError("native release activation requires Local First paused")
            row = conn.execute("SELECT * FROM native_dependency_release_revalidations WHERE revalidation_id=? AND ticket_id=?", (revalidation_id, ticket_id)).fetchone()
            if row is None:
                raise ValueError("native release activation requires the exact revalidation")
            existing = conn.execute("SELECT * FROM native_release_activation_intents WHERE request_key=? OR revalidation_id=?", (request_key, revalidation_id)).fetchall()
            if existing:
                if len(existing) != 1:
                    raise RuntimeError("native release activation intent is duplicated")
                prior = existing[0]
                if tuple(prior[key] for key in ("ticket_id","revalidation_id","external_task_id","pre_activation_snapshot_hash","implementation_profile","repository_identity","canonical_worktree_path","branch","base_sha","operator_id","reason","activation_marker")) != (*values[:-1], marker):
                    raise ValueError("native release activation replay conflicts")
                return dict(prior)
            now = self._now()
            event_id = self._append_event(conn, entity_type="controller", entity_id="controller", event_type="native_dependency_release_activation_intent_created", actor_id=operator_id, payload={"request_key": request_key, "revalidation_id": revalidation_id, "ticket_id": ticket_id, "activation_marker": marker})
            conn.execute("""INSERT INTO native_release_activation_intents
                (request_key,ticket_id,revalidation_id,external_task_id,pre_activation_snapshot_hash,implementation_profile,repository_identity,canonical_worktree_path,branch,base_sha,operator_id,reason,activation_marker,status,pre_activation_snapshot_json,approval_document_json,approval_document_hash,detached_signature,signer_fingerprint,board_path,board_dev,board_ino,activation_event_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?)""", (request_key, *values[:-1], marker, pre_activation_snapshot_json, approval_document_json, approval_document_hash, None if detached_signature is None else base64.b64encode(detached_signature).decode("ascii"), signer_fingerprint, board_path, board_dev, board_ino, event_id, now, now))
            return dict(conn.execute("SELECT * FROM native_release_activation_intents WHERE request_key=?", (request_key,)).fetchone())

    def acknowledge_native_release_activation(self, request_key: str, *, post_activation_snapshot_hash: str, post_activation_snapshot_json: str | None = None) -> dict[str, Any]:
        if not isinstance(request_key, str) or not request_key.strip() or not isinstance(post_activation_snapshot_hash, str) or not post_activation_snapshot_hash.strip():
            raise ValueError("native release activation acknowledgement requires identity and snapshot")
        with self._transaction() as conn:
            intent = conn.execute("SELECT * FROM native_release_activation_intents WHERE request_key=?", (request_key,)).fetchone()
            if intent is None:
                raise KeyError(request_key)
            if not all(intent[key] for key in ("approval_document_json", "approval_document_hash", "detached_signature", "signer_fingerprint", "board_path", "board_dev", "board_ino")):
                raise PermissionError("native release activation acknowledgement requires signed approval authority")
            if intent["status"] == "acknowledged":
                return dict(intent)
            if intent["status"] not in {"pending", "effect_applied"}:
                raise RuntimeError("native release activation intent is not replayable")
            evidence = {"request_key": request_key, "ticket_id": intent["ticket_id"], "revalidation_id": intent["revalidation_id"], "activation_marker": intent["activation_marker"], "post_activation_snapshot_hash": post_activation_snapshot_hash, "post_activation_snapshot_json": post_activation_snapshot_json}
            evidence_hash = canonical_sha256(evidence)
            event_id = self._append_event(conn, entity_type="controller", entity_id="controller", event_type="native_dependency_release_activation_acknowledged", actor_id=intent["operator_id"], payload={**evidence, "evidence_hash": evidence_hash})
            self._inject_failure("after_ack_event")
            conn.execute("INSERT INTO native_release_activation_evidence(request_key,ticket_id,revalidation_id,activation_marker,post_activation_snapshot_hash,post_activation_snapshot_json,event_id,evidence_hash,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,?)", (*evidence.values(), event_id, evidence_hash, self._now()))
            self._inject_failure("after_ack_evidence")
            conn.execute("UPDATE native_release_activation_intents SET status='acknowledged',effect_snapshot_hash=?,post_activation_snapshot_json=?,updated_at=? WHERE request_key=?", (post_activation_snapshot_hash, post_activation_snapshot_json, self._now(), request_key))
            self._inject_failure("after_ack_commit")
            return dict(conn.execute("SELECT * FROM native_release_activation_intents WHERE request_key=?", (request_key,)).fetchone())

    def mark_native_release_activation_effect(self, request_key: str, snapshot_hash: str, snapshot_json: str | None = None) -> None:
        with self._transaction() as conn:
            row = conn.execute("SELECT status FROM native_release_activation_intents WHERE request_key=?", (request_key,)).fetchone()
            if row is None: raise KeyError(request_key)
            if row["status"] == "acknowledged": return
            if row["status"] != "pending": raise RuntimeError("native release activation effect state is not pending")
            conn.execute("UPDATE native_release_activation_intents SET status='effect_applied',effect_snapshot_hash=?,post_activation_snapshot_json=?,updated_at=? WHERE request_key=?", (snapshot_hash, snapshot_json, self._now(), request_key))

    def record_native_release_activation_running_observation(self, *, request_key: str, ticket_id: str, external_task_id: str, snapshot_hash: str, snapshot_json: str, run_id: int, session_id: str, process_identity: dict[str, Any], observed_at: int, profile: str, workspace_path: str, branch: str, event_payload: dict[str, Any]) -> dict[str, Any]:
        """Append the first trusted running observation; never accept a terminal shortcut."""
        if type(run_id) is not int or run_id < 0 or type(observed_at) is not int or observed_at < 0 or not all(isinstance(v, str) and v.strip() for v in (request_key, ticket_id, external_task_id, snapshot_hash, session_id, profile, workspace_path, branch)):
            raise ValueError("native running observation identity is malformed")
        if not isinstance(process_identity, dict) or set(process_identity) != {"pid", "start_ticks", "uid", "exe", "cmdline"}:
            raise ValueError("native running observation process identity is malformed")
        with self._transaction() as conn:
            intent = conn.execute("SELECT * FROM native_release_activation_intents WHERE request_key=? AND ticket_id=? AND external_task_id=? AND status='acknowledged'", (request_key, ticket_id, external_task_id)).fetchone()
            if intent is None:
                raise RuntimeError("native running observation requires acknowledged activation")
            prior = conn.execute("SELECT * FROM native_release_activation_running_observations WHERE request_key=?", (request_key,)).fetchone()
            if prior is not None:
                return dict(prior)
            evidence = {"request_key": request_key, "ticket_id": ticket_id, "external_task_id": external_task_id, "snapshot_hash": snapshot_hash, "snapshot_json": snapshot_json, "run_id": run_id, "session_id": session_id, "pid": process_identity["pid"], "process_identity": process_identity, "observed_at": observed_at, "profile": profile, "workspace_path": workspace_path, "branch": branch}
            evidence_hash = canonical_sha256(evidence)
            event_id = self._append_event(conn, entity_type="controller", entity_id="controller", event_type="native_release_activation_running_observed", actor_id="local-first-orchestrator", payload={"evidence": evidence, "evidence_hash": evidence_hash, "event_payload": event_payload})
            observation_id = "native-running:" + evidence_hash
            conn.execute("INSERT INTO native_release_activation_running_observations (observation_id,request_key,ticket_id,external_task_id,snapshot_hash,snapshot_json,run_id,session_id,pid,process_identity_json,observed_at,profile,workspace_path,branch,event_id,evidence_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (observation_id, request_key, ticket_id, external_task_id, snapshot_hash, snapshot_json, run_id, session_id, process_identity["pid"], json.dumps(process_identity, sort_keys=True, separators=(",", ":")), observed_at, profile, workspace_path, branch, event_id, evidence_hash))
            return dict(conn.execute("SELECT * FROM native_release_activation_running_observations WHERE request_key=?", (request_key,)).fetchone())

    def record_native_release_revalidation(self, *, ticket_id: str, projection_event_id: int,
                                           projection_key: str, external_task_id: str,
                                           implementation_profile: str, repository_identity: str,
                                           canonical_worktree_path: str, branch: str, base_sha: str,
                                           snapshot_hash: str, operator_id: str, reason: str,
                                           approval_document_json: str, approval_document_hash: str,
                                           detached_signature: bytes, signer_fingerprint: str,
                                           signer_public_key: bytes,
                                           _trusted_board_capability: Any | None = None) -> dict[str, Any]:
        if not all(isinstance(value, str) and value.strip() for value in (ticket_id, projection_key, external_task_id, implementation_profile, repository_identity, canonical_worktree_path, branch, base_sha, snapshot_hash, operator_id, reason)):
            raise ValueError("native release revalidation requires non-empty identity and reason")
        if _trusted_board_capability is None:
            raise PermissionError("native release revalidation requires a trusted board transaction")
        from .native_release_approval import parse_approval_document, verify_detached_signature
        if not isinstance(approval_document_json, str) or not approval_document_json.strip() or not isinstance(approval_document_hash, str) or not approval_document_hash.strip() or not isinstance(detached_signature, bytes) or not isinstance(signer_fingerprint, str) or not signer_fingerprint.strip() or not isinstance(signer_public_key, bytes):
            raise ValueError("native release revalidation requires complete external signer authority")
        document_bytes = approval_document_json.encode("utf-8")
        approval = parse_approval_document(document_bytes)
        if hashlib.sha256(document_bytes).hexdigest() != approval_document_hash:
            raise ValueError("native release approval document hash mismatch")
        verify_detached_signature(document_bytes, detached_signature, signer_public_key, signer_fingerprint)
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or not bool(paused["paused"]):
                raise PermissionError("native release revalidation requires Local First paused")
            release = conn.execute("SELECT * FROM native_dependency_releases WHERE ticket_id=?", (ticket_id,)).fetchone()
            if release is None:
                raise ValueError("native release revalidation requires an existing release")
            try:
                if json.loads(str(release["routing_authority_json"] or "{}")) != {}:
                    raise ValueError("native release revalidation requires a legacy release")
            except json.JSONDecodeError as exc:
                raise ValueError("native release routing authority is malformed") from exc
            projection = conn.execute("""
                SELECT b.*,e.event_type,e.entity_type,e.entity_id FROM board_projection_outbox b JOIN events e ON e.id=b.event_id
                WHERE b.ticket_id=? AND b.operation='create_microticket' AND b.superseded_at IS NULL
                  AND b.acknowledged_at IS NOT NULL AND b.external_task_id IS NOT NULL
            """, (ticket_id,)).fetchall()
            if len(projection) != 1 or int(projection[0]["event_id"]) != projection_event_id or projection[0]["idempotency_key"] != projection_key or projection[0]["external_task_id"] != external_task_id or projection[0]["event_type"] not in {"generated_microticket_created", "generated_microticket_projection_recovered"}:
                raise ValueError("native release revalidation requires exactly one current acknowledged generated projection")
            binding = conn.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
            if binding is None or str(binding["repository_path"]) != repository_identity:
                raise ValueError("native release revalidation runtime identity mismatch")
            if conn.execute("SELECT 1 FROM attempts WHERE ticket_id=? UNION SELECT 1 FROM model_invocations WHERE ticket_id=? UNION SELECT 1 FROM model_stage_artifacts WHERE ticket_id=? UNION SELECT 1 FROM review_candidates WHERE ticket_id=? UNION SELECT 1 FROM review_results WHERE ticket_id=? UNION SELECT 1 FROM review_findings WHERE ticket_id=? UNION SELECT 1 FROM accepted_candidates WHERE ticket_id=? UNION SELECT 1 FROM accepted_evidence WHERE ticket_id=? UNION SELECT 1 FROM git_commit_intents WHERE ticket_id=? UNION SELECT 1 FROM git_commit_evidence WHERE ticket_id=? UNION SELECT 1 FROM hermes_execution_reconciliations WHERE ticket_id=?", (ticket_id,)*11).fetchone() is not None:
                raise ValueError("native release revalidation refuses lifecycle evidence")
            if (approval["operator_id"] != operator_id or approval["reason"] != reason
                    or approval["authority"].get("ticket_id") != ticket_id
                    or approval["authority"].get("implementation_profile") != implementation_profile
                    or approval["authority"].get("repository_identity") != repository_identity
                    or approval["authority"].get("canonical_worktree_path") != canonical_worktree_path
                    or approval["authority"].get("branch") != branch
                    or approval["authority"].get("base_sha") != base_sha
                    or approval["authority"].get("snapshot_hash") != snapshot_hash
                    or approval["authority"].get("projection") != {"event_id": projection_event_id, "key": projection_key, "external_id": external_task_id}
                    or approval["authority"].get("release") != dict(release)):
                raise ValueError("native release approval document is stale or copied")
            if conn.execute("SELECT 1 FROM scheduler_stage_claims WHERE ticket_id=? AND status='claimed' UNION SELECT 1 FROM tickets WHERE id=? AND lease_owner IS NOT NULL", (ticket_id, ticket_id)).fetchone() is not None:
                raise ValueError("native release revalidation refuses active lease or claim")
            validate_and_consume_revalidation_capability(
                _trusted_board_capability, task_id=ticket_id, external_task_id=external_task_id,
                expected_snapshot_hash=snapshot_hash,
            )
            values = (implementation_profile, repository_identity, canonical_worktree_path, branch, base_sha, snapshot_hash, operator_id, reason)
            approval_hash = approval_document_hash
            # Exact signed-document lookup is the compatibility path for old deterministic IDs.
            exact = conn.execute("SELECT * FROM native_dependency_release_revalidations WHERE ticket_id=? AND approval_document_hash=?", (ticket_id, approval_hash)).fetchone()
            if exact is not None:
                if conn.execute("SELECT 1 FROM native_dependency_release_revalidation_supersessions WHERE old_revalidation_id=?", (exact["revalidation_id"],)).fetchone() is not None:
                    raise ValueError("revalidation replay conflicts")
                linked = conn.execute("""
                    SELECT r.*, e.id AS linked_event_id, e.entity_type AS linked_event_entity_type,
                           e.entity_id AS linked_event_entity_id, e.event_type AS linked_event_type,
                           e.payload_json AS linked_event_payload,
                           b.event_id AS current_projection_event_id, b.idempotency_key AS current_projection_key,
                           b.external_task_id AS current_external_task_id, pe.event_type AS current_projection_event_type,
                           rb.repository_path AS binding_repository_path
                    FROM native_dependency_release_revalidations r JOIN events e ON e.id=r.revalidation_event_id
                    JOIN board_projection_outbox b ON b.ticket_id=r.ticket_id AND b.operation='create_microticket' AND b.superseded_at IS NULL AND b.acknowledged_at IS NOT NULL
                    JOIN events pe ON pe.id=b.event_id JOIN runtime_bindings rb ON rb.ticket_id=r.ticket_id
                    WHERE r.revalidation_id=?
                """, (exact["revalidation_id"],)).fetchone()
                if linked is None or not self._native_release_revalidation_row_valid(linked, release, signer_public_key=signer_public_key, signer_fingerprint=signer_fingerprint):
                    raise RuntimeError("native release revalidation reconciliation required")
                return dict(exact)
            existing_ticket = conn.execute("SELECT * FROM native_dependency_release_revalidations WHERE ticket_id=? AND NOT EXISTS (SELECT 1 FROM native_dependency_release_revalidation_supersessions s WHERE s.old_revalidation_id=native_dependency_release_revalidations.revalidation_id)", (ticket_id,)).fetchone()
            schema_version = int(approval["authority"].get("snapshot_schema_version", 1))
            old_schema = 1
            if existing_ticket is not None:
                old_schema = int(existing_ticket["snapshot_schema_version"] or 1)
                immutable_keys = ("projection_event_id", "projection_key", "external_task_id", "implementation_profile", "repository_identity", "canonical_worktree_path", "branch", "base_sha")
                if tuple(existing_ticket[key] for key in immutable_keys) != (projection_event_id, projection_key, external_task_id, *values[:5]):
                    raise ValueError("revalidation replay conflicts")
                if old_schema == schema_version or existing_ticket["snapshot_hash"] == snapshot_hash:
                    raise ValueError("revalidation replay conflicts")
                if conn.execute("SELECT 1 FROM native_release_activation_intents WHERE ticket_id=? AND revalidation_id=? AND status IN ('pending','acknowledged')", (ticket_id, existing_ticket["revalidation_id"])).fetchone() is not None:
                    raise ValueError("activated or pending revalidation cannot be superseded")
                linked = conn.execute("""SELECT r.*,e.id AS linked_event_id,e.entity_type AS linked_event_entity_type,e.entity_id AS linked_event_entity_id,e.event_type AS linked_event_type,e.payload_json AS linked_event_payload,b.event_id AS current_projection_event_id,b.idempotency_key AS current_projection_key,b.external_task_id AS current_external_task_id,pe.event_type AS current_projection_event_type,rb.repository_path AS binding_repository_path FROM native_dependency_release_revalidations r JOIN events e ON e.id=r.revalidation_event_id JOIN board_projection_outbox b ON b.ticket_id=r.ticket_id AND b.operation='create_microticket' AND b.superseded_at IS NULL AND b.acknowledged_at IS NOT NULL JOIN events pe ON pe.id=b.event_id JOIN runtime_bindings rb ON rb.ticket_id=r.ticket_id WHERE r.revalidation_id=?""", (existing_ticket["revalidation_id"],)).fetchone()
                if linked is None or not self._native_release_revalidation_row_valid(linked, release, signer_public_key=signer_public_key, signer_fingerprint=signer_fingerprint):
                    raise RuntimeError("native release revalidation reconciliation required")
            revalidation_id = approval_hash
            event_key = "native-release-revalidated:" + revalidation_id
            evidence_row = {"ticket_id": ticket_id, "revalidation_id": revalidation_id, "event_key": event_key, "release_graph_hash": release["graph_hash"], "release_child_external_id": release["child_external_id"], "release_parent_completion_hash": release["parent_completion_hash"], "release_routing_authority_json": release["routing_authority_json"], "release_hermes_status": release["hermes_status"], "release_observed_at": release["observed_at"], "projection_event_id": projection_event_id, "projection_key": projection_key, "external_task_id": external_task_id, "implementation_profile": implementation_profile, "repository_identity": repository_identity, "canonical_worktree_path": canonical_worktree_path, "branch": branch, "base_sha": base_sha, "snapshot_hash": snapshot_hash, "operator_id": operator_id, "reason": reason}
            evidence = self._native_release_evidence_document(evidence_row)
            evidence_hash = canonical_sha256(evidence)
            event_payload = {"event_key": event_key, "revalidation_id": revalidation_id, "evidence_hash": evidence_hash, "evidence": evidence}
            event_payload.update({"approval_document_hash": approval_document_hash, "signer_fingerprint": signer_fingerprint})
            prior = conn.execute("SELECT * FROM native_dependency_release_revalidations WHERE revalidation_id=?", (revalidation_id,)).fetchone()
            if prior is not None:
                if conn.execute("SELECT 1 FROM native_dependency_release_revalidation_supersessions WHERE old_revalidation_id=?", (prior["revalidation_id"],)).fetchone() is not None:
                    raise ValueError("revalidation replay conflicts")
                expected = tuple(prior[key] for key in ("implementation_profile", "repository_identity", "canonical_worktree_path", "branch", "base_sha", "snapshot_hash", "operator_id", "reason"))
                if expected != values:
                    raise ValueError("revalidation replay conflicts")
                if str(prior["ticket_id"]) != ticket_id or str(prior["event_key"] or "") != event_key or str(prior["evidence_hash"] or "") != evidence_hash:
                    raise ValueError("revalidation replay conflicts")
                return dict(prior)
            if existing_ticket is None and conn.execute("SELECT 1 FROM native_dependency_release_revalidations WHERE ticket_id=?", (ticket_id,)).fetchone() is not None:
                raise ValueError("native release revalidation duplicate or drifted")
            event_id = self._append_event(conn, entity_type="controller", entity_id="controller", event_type="native_dependency_release_revalidated", actor_id=operator_id, payload=event_payload)
            conn.execute("""INSERT INTO native_dependency_release_revalidations
                (revalidation_id,ticket_id,release_graph_hash,release_child_external_id,release_parent_completion_hash,release_routing_authority_json,release_hermes_status,release_observed_at,projection_event_id,projection_key,external_task_id,implementation_profile,repository_identity,canonical_worktree_path,branch,base_sha,snapshot_hash,operator_id,reason,created_at,revalidation_event_id,event_key,evidence_hash,approval_document_json,approval_document_hash,detached_signature,signer_fingerprint,snapshot_schema_version)
                VALUES (?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?)""", (revalidation_id,ticket_id,release["graph_hash"],release["child_external_id"],release["parent_completion_hash"],release["routing_authority_json"],release["hermes_status"],release["observed_at"],projection_event_id,projection_key,external_task_id,*values,self._now(),event_id,event_key,evidence_hash,approval_document_json,approval_document_hash,base64.b64encode(detached_signature).decode("ascii") if detached_signature else None,signer_fingerprint,schema_version))
            if existing_ticket is not None:
                supersession_evidence = {"old_revalidation_id": str(existing_ticket["revalidation_id"]), "new_revalidation_id": revalidation_id, "ticket_id": ticket_id, "reason": reason, "operator_id": operator_id, "old_snapshot_hash": str(existing_ticket["snapshot_hash"]), "new_snapshot_hash": snapshot_hash, "old_snapshot_schema_version": old_schema, "new_snapshot_schema_version": schema_version, "old_approval_document_json": str(existing_ticket["approval_document_json"] or ""), "new_approval_document_json": approval_document_json, "old_approval_document_hash": str(existing_ticket["approval_document_hash"] or ""), "new_approval_document_hash": approval_document_hash}
                supersession_hash = canonical_sha256(supersession_evidence)
                supersession_event = self._append_event(conn, entity_type="controller", entity_id="controller", event_type="native_dependency_release_revalidation_superseded", actor_id=operator_id, payload={"evidence": supersession_evidence, "evidence_hash": supersession_hash, "detached_signature": base64.b64encode(detached_signature).decode("ascii")})
                conn.execute("INSERT INTO native_dependency_release_revalidation_supersessions (old_revalidation_id,new_revalidation_id,ticket_id,reason,operator_id,old_snapshot_hash,new_snapshot_hash,old_snapshot_schema_version,new_snapshot_schema_version,old_approval_document_json,new_approval_document_json,old_approval_document_hash,new_approval_document_hash,detached_signature,event_id,evidence_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (existing_ticket["revalidation_id"],revalidation_id,ticket_id,reason,operator_id,existing_ticket["snapshot_hash"],snapshot_hash,old_schema,schema_version,existing_ticket["approval_document_json"],approval_document_json,existing_ticket["approval_document_hash"],approval_document_hash,base64.b64encode(detached_signature).decode("ascii"),supersession_event,supersession_hash,self._now()))
            self._inject_failure("after_native_release_revalidation")
            return dict(conn.execute("SELECT * FROM native_dependency_release_revalidations WHERE revalidation_id=?", (revalidation_id,)).fetchone())

    def _native_dependency_graph_identity(self, conn: sqlite3.Connection, ticket_id: str) -> dict[str, Any]:
        ticket = conn.execute("SELECT id,dependencies_json FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if ticket is None:
            raise KeyError(ticket_id)
        try:
            dependencies = tuple(sorted(set(json.loads(str(ticket["dependencies_json"])))))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("native_dependency_graph_reconciliation_required: invalid dependency contract") from exc
        if ticket_id in dependencies or not all(isinstance(value, str) and value for value in dependencies):
            raise RuntimeError("native_dependency_graph_reconciliation_required: invalid dependency contract")
        if not dependencies:
            root_projections = conn.execute("""
                SELECT b.event_id FROM board_projection_outbox b
                JOIN events e ON e.id=b.event_id AND e.entity_type='ticket' AND e.entity_id=?
                  AND e.event_type IN ('generated_microticket_created','generated_microticket_projection_recovered')
                WHERE b.ticket_id=? AND b.operation='create_microticket'
                  AND b.acknowledged_at IS NOT NULL AND b.external_task_id IS NOT NULL
                  AND b.superseded_at IS NULL
            """, (ticket_id, ticket_id)).fetchall()
            if len(root_projections) != 1:
                raise RuntimeError("native_dependency_graph_reconciliation_required: generated root provenance missing")
        child_external_id = self._resolve_external_task_id_in_transaction(conn, ticket_id)
        parents: list[dict[str, str]] = []
        for dependency_id in dependencies:
            if conn.execute("SELECT 1 FROM tickets WHERE id=?", (dependency_id,)).fetchone() is None:
                raise RuntimeError("native_dependency_graph_reconciliation_required: missing dependency ticket")
            parents.append(
                {
                    "ticket_id": dependency_id,
                    "external_task_id": self._resolve_external_task_id_in_transaction(conn, dependency_id),
                }
            )
        parent_external_ids = [row["external_task_id"] for row in parents]
        if child_external_id in parent_external_ids or len(set(parent_external_ids)) != len(parent_external_ids):
            raise RuntimeError("native_dependency_graph_reconciliation_required: ambiguous external task identity")
        core = {
            "ticket_id": ticket_id,
            "child_external_id": child_external_id,
            "parents": parents,
        }
        encoded = json.dumps(core, sort_keys=True, separators=(",", ":"))
        return {**core, "graph_hash": hashlib.sha256(encoded.encode()).hexdigest()}

    def claim_next_scheduler_native_dependency_graph(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        if not owner or lease_seconds < 1:
            raise ValueError("native dependency graph claim requires owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute(
                "SELECT * FROM scheduler_stage_claims WHERE stage='native_dependency_graph' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
                (now,),
            ).fetchone()
            if replay is not None:
                identity = self._native_dependency_graph_identity(conn, str(replay["ticket_id"]))
                if json.loads(str(replay["candidate_identity_json"] or "{}")) != identity:
                    raise RuntimeError("native_dependency_graph_reconciliation_required: claim identity drift")
                changed = conn.execute(
                    "UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?",
                    (owner, now + lease_seconds, now, replay["claim_id"], now),
                )
                if changed.rowcount != 1:
                    return None
                self._append_event(conn, entity_type="ticket", entity_id=str(replay["ticket_id"]), event_type="scheduler_stage_reclaimed", actor_id=owner, payload={"claim_id": replay["claim_id"], "stage": "native_dependency_graph", "lease_expires_at": now + lease_seconds})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())

            candidates = conn.execute("""
                SELECT t.id FROM tickets t
                JOIN runtime_bindings rb ON rb.ticket_id=t.id
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
                  AND NOT EXISTS (SELECT 1 FROM native_dependency_graphs g WHERE g.ticket_id=t.id)
                  AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage='native_dependency_graph')
                ORDER BY t.created_at,t.id LIMIT 100
            """).fetchall()
            for candidate in candidates:
                ticket_id = str(candidate["id"])
                try:
                    identity = self._native_dependency_graph_identity(conn, ticket_id)
                except (KeyError, RuntimeError):
                    continue
                encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
                claim_id = hashlib.sha256(("native_dependency_graph:" + encoded).encode()).hexdigest()[:32]
                conn.execute(
                    "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,'native_dependency_graph','claimed',?,?,1,?,?,?)",
                    (claim_id, ticket_id, owner, now + lease_seconds, encoded, now, now),
                )
                self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_claimed", actor_id=owner, payload={"claim_id": claim_id, "stage": "native_dependency_graph", "candidate_identity": identity})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())
            return None

    def apply_scheduler_native_dependency_graph_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or claim["stage"] != "native_dependency_graph":
                raise ValueError("scheduler claim is not native dependency graph")
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            if claim["side_effect_completed_at"] is not None:
                if claim["result_json"] != encoded:
                    raise RuntimeError("native dependency graph completed result conflicts")
                return dict(claim)
            identity = self._native_dependency_graph_identity(conn, str(claim["ticket_id"]))
            if json.loads(str(claim["candidate_identity_json"] or "{}")) != identity or result.get("candidate_identity") != identity:
                raise RuntimeError("native_dependency_graph_reconciliation_required: result identity drift")
            ordered_parent_external_ids = [row["external_task_id"] for row in identity["parents"]]
            expected_parents = sorted(ordered_parent_external_ids)
            if sorted(result.get("actual_parent_external_ids") or []) != expected_parents:
                raise RuntimeError("native_dependency_graph_reconciliation_required: Hermes graph mismatch")
            existing = conn.execute("SELECT * FROM native_dependency_graphs WHERE ticket_id=?", (claim["ticket_id"],)).fetchone()
            values = (
                str(identity["child_external_id"]),
                json.dumps([row["ticket_id"] for row in identity["parents"]], sort_keys=True, separators=(",", ":")),
                json.dumps(ordered_parent_external_ids, sort_keys=True, separators=(",", ":")),
                str(identity["graph_hash"]),
            )
            if existing is not None:
                if tuple(existing[key] for key in ("child_external_id", "local_dependency_ids_json", "parent_external_ids_json", "graph_hash")) != values:
                    raise RuntimeError("native_dependency_graph_reconciliation_required: graph evidence conflicts")
            else:
                conn.execute(
                    "INSERT INTO native_dependency_graphs(ticket_id,child_external_id,local_dependency_ids_json,parent_external_ids_json,graph_hash,verified_at) VALUES (?,?,?,?,?,?)",
                    (claim["ticket_id"], *values, now),
                )
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL",
                (now, encoded, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(conn, entity_type="ticket", entity_id=str(claim["ticket_id"]), event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id": claim_id, "stage": "native_dependency_graph", "result": result})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def _native_dependency_release_identity(self, conn: sqlite3.Connection, ticket_id: str, *, implementation_profile: str | None = None, canonical_repository: str | None = None) -> dict[str, Any]:
        graph = conn.execute("SELECT * FROM native_dependency_graphs WHERE ticket_id=?", (ticket_id,)).fetchone()
        if graph is None:
            raise RuntimeError("native_dependency_release_reconciliation_required: verified graph missing")
        try:
            current_graph = self._native_dependency_graph_identity(conn, ticket_id)
        except (KeyError, ValueError, RuntimeError) as exc:
            raise RuntimeError("native_dependency_release_reconciliation_required: graph provenance drift") from exc
        try:
            dependencies = tuple(json.loads(str(graph["local_dependency_ids_json"])))
            parent_external_ids = tuple(json.loads(str(graph["parent_external_ids_json"])))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("native_dependency_release_reconciliation_required: graph evidence malformed") from exc
        if len(dependencies) != len(parent_external_ids):
            raise RuntimeError("native_dependency_release_reconciliation_required: graph evidence malformed")
        expected_dependencies = tuple(row["ticket_id"] for row in current_graph["parents"])
        expected_parent_external_ids = tuple(row["external_task_id"] for row in current_graph["parents"])
        if (
            dependencies != expected_dependencies
            or parent_external_ids != expected_parent_external_ids
            or str(graph["child_external_id"]) != str(current_graph["child_external_id"])
            or str(graph["graph_hash"]) != str(current_graph["graph_hash"])
        ):
            raise RuntimeError("native_dependency_release_reconciliation_required: graph contract drift")
        completions: list[dict[str, Any]] = []
        for dependency_id, parent_external_id in zip(dependencies, parent_external_ids):
            dependency = conn.execute("SELECT state FROM tickets WHERE id=?", (dependency_id,)).fetchone()
            evidence = conn.execute("SELECT accepted_commit_sha FROM accepted_evidence WHERE ticket_id=?", (dependency_id,)).fetchone()
            projected = conn.execute("""
                SELECT e.id AS event_id,b.idempotency_key,b.external_task_id,b.acknowledged_at
                FROM events e JOIN board_projection_outbox b ON b.ticket_id=e.entity_id AND b.event_id=e.id
                WHERE e.entity_type='ticket' AND e.entity_id=? AND e.event_type='state_transition' AND e.to_state='done'
                  AND b.operation='set_state' AND b.acknowledged_at IS NOT NULL
                ORDER BY e.id DESC LIMIT 1
            """, (dependency_id,)).fetchone()
            if dependency is None or dependency["state"] != CanonicalState.DONE.value or evidence is None or projected is None:
                raise RuntimeError("native_dependency_release_reconciliation_required: parent completion not remotely acknowledged")
            if str(projected["external_task_id"] or "") != str(parent_external_id):
                raise RuntimeError("native_dependency_release_reconciliation_required: parent external identity drift")
            completions.append(
                {
                    "ticket_id": str(dependency_id),
                    "external_task_id": str(parent_external_id),
                    "accepted_commit_sha": str(evidence["accepted_commit_sha"]),
                    "done_event_id": int(projected["event_id"]),
                    "projection_idempotency_key": str(projected["idempotency_key"]),
                }
            )
        core = {
            "ticket_id": ticket_id,
            "graph_hash": str(graph["graph_hash"]),
            "child_external_id": str(graph["child_external_id"]),
            "parent_completions": completions,
        }
        completion_hash = hashlib.sha256(json.dumps(completions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        identity = {**core, "parent_completion_hash": completion_hash}
        if implementation_profile is not None or canonical_repository is not None:
            if not implementation_profile or not canonical_repository:
                raise RuntimeError("native_dependency_release_reconciliation_required: routing authority is incomplete")
            identity["routing_authority"] = {"profile": implementation_profile, "canonical_repository": str(canonical_repository)}
        return identity

    def claim_next_scheduler_native_dependency_release(self, owner: str, *, lease_seconds: int, now: int | None = None, implementation_profile: str | None = None, canonical_repository: str | None = None) -> dict[str, Any] | None:
        if not owner or lease_seconds < 1:
            raise ValueError("native dependency release claim requires owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute(
                "SELECT * FROM scheduler_stage_claims WHERE stage='native_dependency_release' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
                (now,),
            ).fetchone()
            if replay is not None:
                identity = self._native_dependency_release_identity(conn, str(replay["ticket_id"]), implementation_profile=implementation_profile, canonical_repository=canonical_repository)
                if json.loads(str(replay["candidate_identity_json"] or "{}")) != identity:
                    raise RuntimeError("native_dependency_release_reconciliation_required: claim identity drift")
                changed = conn.execute(
                    "UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?",
                    (owner, now + lease_seconds, now, replay["claim_id"], now),
                )
                if changed.rowcount != 1:
                    return None
                self._append_event(conn, entity_type="ticket", entity_id=str(replay["ticket_id"]), event_type="scheduler_stage_reclaimed", actor_id=owner, payload={"claim_id": replay["claim_id"], "stage": "native_dependency_release", "lease_expires_at": now + lease_seconds})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())

            candidates = conn.execute("""
                SELECT t.id FROM tickets t
                JOIN native_dependency_graphs g ON g.ticket_id=t.id
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
                  AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage='native_dependency_release')
                ORDER BY t.created_at,t.id LIMIT 100
            """).fetchall()
            for candidate in candidates:
                ticket_id = str(candidate["id"])
                try:
                    identity = self._native_dependency_release_identity(conn, ticket_id, implementation_profile=implementation_profile, canonical_repository=canonical_repository)
                except RuntimeError:
                    continue
                encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
                claim_id = hashlib.sha256(("native_dependency_release:" + encoded).encode()).hexdigest()[:32]
                conn.execute(
                    "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,'native_dependency_release','claimed',?,?,1,?,?,?)",
                    (claim_id, ticket_id, owner, now + lease_seconds, encoded, now, now),
                )
                self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_claimed", actor_id=owner, payload={"claim_id": claim_id, "stage": "native_dependency_release", "candidate_identity": identity})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())
            return None

    def apply_scheduler_native_dependency_release_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None, implementation_profile: str | None = None, canonical_repository: str | None = None) -> dict[str, Any]:
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or claim["stage"] != "native_dependency_release":
                raise ValueError("scheduler claim is not native dependency release")
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            if claim["side_effect_completed_at"] is not None:
                if claim["result_json"] != encoded:
                    raise RuntimeError("native dependency release completed result conflicts")
                return dict(claim)
            identity = self._native_dependency_release_identity(conn, str(claim["ticket_id"]), implementation_profile=implementation_profile, canonical_repository=canonical_repository)
            if json.loads(str(claim["candidate_identity_json"] or "{}")) != identity or result.get("candidate_identity") != identity:
                raise RuntimeError("native_dependency_release_reconciliation_required: result identity drift")
            graph = conn.execute("SELECT * FROM native_dependency_graphs WHERE ticket_id=?", (claim["ticket_id"],)).fetchone()
            expected_parents = sorted(json.loads(str(graph["parent_external_ids_json"])))
            if sorted(result.get("actual_parent_external_ids") or []) != expected_parents:
                raise RuntimeError("native_dependency_release_reconciliation_required: Hermes graph diverged")
            if str(result.get("hermes_status") or "") != "ready":
                raise RuntimeError("native_dependency_release_reconciliation_required: Hermes did not expose child as ready")
            expected_routing = identity.get("routing_authority", {})
            if result.get("routing_authority") != expected_routing:
                raise RuntimeError("native_dependency_release_reconciliation_required: routing authority evidence drift")
            existing = conn.execute("SELECT * FROM native_dependency_releases WHERE ticket_id=?", (claim["ticket_id"],)).fetchone()
            values = (
                str(identity["graph_hash"]),
                str(identity["child_external_id"]),
                str(identity["parent_completion_hash"]),
                json.dumps(expected_routing, sort_keys=True, separators=(",", ":")),
                "ready",
            )
            if existing is not None:
                if tuple(existing[key] for key in ("graph_hash", "child_external_id", "parent_completion_hash", "routing_authority_json", "hermes_status")) != values:
                    raise RuntimeError("native_dependency_release_reconciliation_required: release evidence conflicts")
            else:
                conn.execute(
                    "INSERT INTO native_dependency_releases(ticket_id,graph_hash,child_external_id,parent_completion_hash,routing_authority_json,hermes_status,observed_at) VALUES (?,?,?,?,?,?,?)",
                    (claim["ticket_id"], *values, now),
                )
            ticket = conn.execute("SELECT state FROM tickets WHERE id=?", (claim["ticket_id"],)).fetchone()
            if ticket is None:
                raise RuntimeError("native_dependency_release_reconciliation_required: child ticket missing")
            if ticket["state"] == CanonicalState.DRAFT.value:
                validate_transition(CanonicalState.DRAFT, CanonicalState.READY_LOCAL)
                changed_state = conn.execute(
                    "UPDATE tickets SET state=?,updated_at=? WHERE id=? AND state=?",
                    (CanonicalState.READY_LOCAL.value, now, claim["ticket_id"], CanonicalState.DRAFT.value),
                )
                if changed_state.rowcount != 1:
                    raise RuntimeError("native_dependency_release_reconciliation_required: child changed concurrently")
                event_id = self._append_event(
                    conn,
                    entity_type="ticket",
                    entity_id=str(claim["ticket_id"]),
                    event_type="state_transition",
                    actor_id="native_dependency_release",
                    from_state=CanonicalState.DRAFT.value,
                    to_state=CanonicalState.READY_LOCAL.value,
                    payload={"graph_hash": values[0], "hermes_status": "ready"},
                )
                bundle = self._enqueue_projection_bundle_in_transaction(
                    conn,
                    ticket_id=str(claim["ticket_id"]),
                    event_id=event_id,
                    evidence=f"state={CanonicalState.READY_LOCAL.value}",
                    state_payload={"graph_hash": values[0], "hermes_status": "ready"},
                )
                state_intent = bundle["state"]
                if state_intent["acknowledged_at"] is None:
                    acknowledged = conn.execute(
                        "UPDATE board_projection_outbox SET acknowledged_at=?,lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=NULL,last_error=NULL "
                        "WHERE ticket_id=? AND event_id=? AND operation='set_state' AND acknowledged_at IS NULL",
                        (now, claim["ticket_id"], event_id),
                    )
                    if acknowledged.rowcount != 1:
                        raise RuntimeError("native_dependency_release_reconciliation_required: ready projection acknowledgement failed")
            elif ticket["state"] != CanonicalState.READY_LOCAL.value:
                raise RuntimeError("native_dependency_release_reconciliation_required: child state drift")
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL",
                (now, encoded, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(conn, entity_type="ticket", entity_id=str(claim["ticket_id"]), event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id": claim_id, "stage": "native_dependency_release", "result": result})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def claim_next_scheduler_completion(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        """Claim one accepted, durably committed candidate for local completion."""
        if not owner or lease_seconds < 1:
            raise ValueError("completion scheduler claim requires owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute(
                "SELECT * FROM scheduler_stage_claims WHERE stage LIKE 'completion:%' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
                (now,),
            ).fetchone()
            if replay is not None:
                ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (replay["ticket_id"],)).fetchone()
                accepted = conn.execute("SELECT * FROM accepted_candidates WHERE ticket_id=?", (replay["ticket_id"],)).fetchone()
                git_evidence = conn.execute("SELECT * FROM git_commit_evidence WHERE ticket_id=?", (replay["ticket_id"],)).fetchone()
                if ticket is None or accepted is None or git_evidence is None:
                    raise RuntimeError("completion_reconciliation_required: completion authority is missing")
                expected_state = CanonicalState.DONE.value if replay["side_effect_completed_at"] is not None else CanonicalState.ACCEPTED.value
                if ticket["state"] != expected_state:
                    raise RuntimeError("completion_reconciliation_required: completion state does not match claim progress")
                try:
                    identity = json.loads(str(replay["candidate_identity_json"] or ""))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("completion_reconciliation_required: claim identity malformed") from exc
                expected = self._scheduler_completion_identity(accepted, git_evidence)
                if identity != expected:
                    raise RuntimeError("completion_reconciliation_required: claim identity drift")
                changed = conn.execute(
                    "UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?",
                    (owner, now + lease_seconds, now, replay["claim_id"], now),
                )
                if changed.rowcount != 1:
                    return None
                conn.execute(
                    "UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?",
                    (owner, now + lease_seconds, now, replay["ticket_id"]),
                )
                self._append_event(
                    conn,
                    entity_type="ticket",
                    entity_id=str(replay["ticket_id"]),
                    event_type="scheduler_stage_reclaimed",
                    actor_id=owner,
                    payload={"claim_id": replay["claim_id"], "stage": "completion", "lease_expires_at": now + lease_seconds},
                )
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())

            row = conn.execute("""
                SELECT t.id,ac.attempt_number
                FROM tickets t
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
            if row is None:
                return None
            accepted = conn.execute("SELECT * FROM accepted_candidates WHERE ticket_id=?", (row["id"],)).fetchone()
            git_evidence = conn.execute("SELECT * FROM git_commit_evidence WHERE ticket_id=?", (row["id"],)).fetchone()
            identity = self._scheduler_completion_identity(accepted, git_evidence)
            encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            stage = f"completion:{identity['attempt_number']}"
            claim_id = hashlib.sha256((stage + ":" + encoded).encode()).hexdigest()[:32]
            changed = conn.execute(
                "UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND state='accepted' AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (owner, now + lease_seconds, now, row["id"], now),
            )
            if changed.rowcount != 1:
                return None
            conn.execute(
                "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,'claimed',?,?,1,?,?,?)",
                (claim_id, row["id"], stage, owner, now + lease_seconds, encoded, now, now),
            )
            self._append_event(
                conn,
                entity_type="ticket",
                entity_id=str(row["id"]),
                event_type="scheduler_stage_claimed",
                actor_id=owner,
                payload={"claim_id": claim_id, "stage": "completion", "candidate_identity": identity},
            )
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    @staticmethod
    def _scheduler_completion_identity(accepted: sqlite3.Row, git_evidence: sqlite3.Row) -> dict[str, Any]:
        return {
            "ticket_id": str(accepted["ticket_id"]),
            "attempt_number": int(accepted["attempt_number"]),
            "accepted_evidence_hash": str(accepted["evidence_hash"]),
            "candidate_fingerprint": str(accepted["candidate_fingerprint"]),
            "base_sha": str(accepted["base_sha"]),
            "worktree_path": str(accepted["worktree_path"]),
            "commit_sha": str(git_evidence["commit_sha"]),
            "branch": str(git_evidence["branch"]),
            "tranche_id": None if git_evidence["tranche_id"] is None else str(git_evidence["tranche_id"]),
            "integration_head_before": str(git_evidence["integration_head_before"]),
            "integration_head_after": str(git_evidence["integration_head_after"]),
        }

    def apply_scheduler_completion_effect(self, claim_id: str, owner: str, *, now: int | None = None) -> dict[str, Any]:
        """Atomically record local completion and enqueue Hermes projection intents."""
        now = self._now() if now is None else now
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or not str(claim["stage"]).startswith("completion:"):
                raise ValueError("scheduler claim is not completion")
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            if claim["side_effect_completed_at"] is not None:
                return dict(claim)
            try:
                identity = json.loads(str(claim["candidate_identity_json"] or ""))
            except json.JSONDecodeError as exc:
                raise RuntimeError("completion_reconciliation_required: claim identity malformed") from exc
            ticket_id = str(claim["ticket_id"])
            ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            accepted = conn.execute("SELECT * FROM accepted_candidates WHERE ticket_id=?", (ticket_id,)).fetchone()
            git_evidence = conn.execute("SELECT * FROM git_commit_evidence WHERE ticket_id=?", (ticket_id,)).fetchone()
            git_intent = conn.execute("SELECT * FROM git_commit_intents WHERE ticket_id=?", (ticket_id,)).fetchone()
            attempt = None if accepted is None else conn.execute(
                "SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?",
                (ticket_id, accepted["attempt_number"]),
            ).fetchone()
            if ticket is None or accepted is None or git_evidence is None or git_intent is None or attempt is None:
                raise RuntimeError("completion_reconciliation_required: completion evidence is incomplete")
            if ticket["state"] != CanonicalState.ACCEPTED.value:
                raise RuntimeError("completion_reconciliation_required: ticket is not accepted")
            expected = self._scheduler_completion_identity(accepted, git_evidence)
            if identity != expected:
                raise RuntimeError("completion_reconciliation_required: completion identity drift")
            if (
                git_intent["status"] != "completed"
                or str(git_intent["commit_sha"] or "") != str(git_evidence["commit_sha"])
                or str(attempt["accepted_commit_sha"] or "") != str(git_evidence["commit_sha"])
                or str(git_evidence["accepted_evidence_hash"]) != str(accepted["evidence_hash"])
                or str(git_evidence["candidate_fingerprint"]) != str(accepted["candidate_fingerprint"])
                or str(git_evidence["base_sha"]) != str(accepted["base_sha"])
                or str(git_evidence["worktree_path"]) != str(accepted["worktree_path"])
                or str(git_evidence["integration_head_after"]) != str(git_evidence["commit_sha"])
            ):
                raise RuntimeError("completion_reconciliation_required: Git/acceptance evidence drift")
            ticket_tranche = None if ticket["tranche_id"] is None else str(ticket["tranche_id"])
            evidence_tranche = None if git_evidence["tranche_id"] is None else str(git_evidence["tranche_id"])
            if ticket_tranche != evidence_tranche:
                raise RuntimeError("completion_reconciliation_required: tranche identity drift")

            diff_summary = json.dumps(
                {
                    "candidate_fingerprint": str(accepted["candidate_fingerprint"]),
                    "base_sha": str(accepted["base_sha"]),
                    "worktree_path": str(accepted["worktree_path"]),
                    "branch": str(git_evidence["branch"]),
                    "commit_message": str(git_evidence["commit_message"]),
                    "accepted_evidence_hash": str(accepted["evidence_hash"]),
                    "integration_head_before": str(git_evidence["integration_head_before"]),
                    "integration_head_after": str(git_evidence["integration_head_after"]),
                    "tranche_id": evidence_tranche,
                },
                sort_keys=True,
            )
            validation_summary = json.dumps(
                {
                    "accepted_evidence_hash": str(accepted["evidence_hash"]),
                    "validation_artifact": str(accepted["validation_artifact"]),
                    "validation_artifact_sha256": str(accepted["validation_artifact_sha256"]),
                    "review_artifact": str(accepted["review_artifact"]),
                    "review_artifact_sha256": str(accepted["review_artifact_sha256"]),
                    "review_result_id": int(accepted["review_result_id"]),
                },
                sort_keys=True,
            )
            existing = conn.execute("SELECT * FROM accepted_evidence WHERE ticket_id=?", (ticket_id,)).fetchone()
            expected_evidence = (
                str(git_evidence["commit_sha"]),
                diff_summary,
                validation_summary,
            )
            if existing is not None:
                actual = (str(existing["accepted_commit_sha"]), str(existing["diff_summary"]), str(existing["validation_summary"]))
                if actual != expected_evidence:
                    raise RuntimeError("completion_reconciliation_required: accepted evidence conflicts")
                raise RuntimeError("completion_reconciliation_required: accepted evidence exists before atomic completion")
            conn.execute(
                "INSERT INTO accepted_evidence(ticket_id,accepted_commit_sha,diff_summary,validation_summary,created_at) VALUES (?,?,?,?,?)",
                (ticket_id, *expected_evidence, now),
            )
            self._append_event(
                conn,
                entity_type="ticket",
                entity_id=ticket_id,
                event_type="accepted_evidence_recorded",
                actor_id="completion",
                payload={
                    "attempt_number": int(accepted["attempt_number"]),
                    "commit_sha": str(git_evidence["commit_sha"]),
                    "candidate_fingerprint": str(accepted["candidate_fingerprint"]),
                    "accepted_evidence_hash": str(accepted["evidence_hash"]),
                    "integration_advanced": evidence_tranche is not None,
                },
            )
            validate_transition(CanonicalState.ACCEPTED, CanonicalState.DONE)
            changed = conn.execute(
                "UPDATE tickets SET state=?,updated_at=? WHERE id=? AND state=?",
                (CanonicalState.DONE.value, now, ticket_id, CanonicalState.ACCEPTED.value),
            )
            if changed.rowcount != 1:
                raise RuntimeError("ticket changed concurrently")
            state_payload = {
                "attempt_number": int(accepted["attempt_number"]),
                "accepted_commit_sha": str(git_evidence["commit_sha"]),
                "accepted_evidence_hash": str(accepted["evidence_hash"]),
            }
            event_id = self._append_event(
                conn,
                entity_type="ticket",
                entity_id=ticket_id,
                event_type="state_transition",
                actor_id="completion",
                from_state=CanonicalState.ACCEPTED.value,
                to_state=CanonicalState.DONE.value,
                payload=state_payload,
            )
            bundle = self._enqueue_projection_bundle_in_transaction(
                conn,
                ticket_id=ticket_id,
                event_id=event_id,
                evidence=f"completed commit={git_evidence['commit_sha']} accepted_evidence={accepted['evidence_hash']}",
                state_payload=state_payload,
            )
            result = {
                "ticket_id": ticket_id,
                "attempt_number": int(accepted["attempt_number"]),
                "candidate_identity": identity,
                "accepted_commit_sha": str(git_evidence["commit_sha"]),
                "accepted_evidence_hash": str(accepted["evidence_hash"]),
                "state_event_id": int(event_id),
                "state_projection_idempotency_key": str(bundle["state"]["idempotency_key"]),
                "evidence_comment_idempotency_key": str(bundle["comment"]["idempotency_key"]),
            }
            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL",
                (now, encoded, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(
                conn,
                entity_type="ticket",
                entity_id=ticket_id,
                event_type="scheduler_stage_effect_completed",
                actor_id=owner,
                payload={"claim_id": claim_id, "stage": "completion", "result": result},
            )
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())


    def claim_next_scheduler_git_integration(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        """Claim one immutable accepted candidate for commit/integration only."""
        if not owner or lease_seconds < 1:
            raise ValueError("git integration scheduler claim requires owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute(
                "SELECT * FROM scheduler_stage_claims WHERE stage LIKE 'git_integration:%' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1",
                (now,),
            ).fetchone()
            if replay is not None:
                ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (replay["ticket_id"],)).fetchone()
                accepted = conn.execute("SELECT * FROM accepted_candidates WHERE ticket_id=?", (replay["ticket_id"],)).fetchone()
                attempt = None if accepted is None else conn.execute(
                    "SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?",
                    (replay["ticket_id"], accepted["attempt_number"]),
                ).fetchone()
                if ticket is None or accepted is None or attempt is None or ticket["state"] != CanonicalState.ACCEPTED.value:
                    raise RuntimeError("git_integration_reconciliation_required: accepted candidate authority is missing")
                try:
                    identity = json.loads(str(replay["candidate_identity_json"] or ""))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("git_integration_reconciliation_required: claim identity malformed") from exc
                expected = {
                    "ticket_id": str(ticket["id"]),
                    "attempt_number": int(accepted["attempt_number"]),
                    "accepted_evidence_hash": str(accepted["evidence_hash"]),
                    "candidate_fingerprint": str(accepted["candidate_fingerprint"]),
                    "base_sha": str(accepted["base_sha"]),
                    "worktree_path": str(accepted["worktree_path"]),
                    "branch": str(attempt["branch"] or ""),
                    "tranche_id": None if ticket["tranche_id"] is None else str(ticket["tranche_id"]),
                    "commit_message": f"local-first: {ticket['title']}",
                }
                if identity != expected:
                    raise RuntimeError("git_integration_reconciliation_required: claim identity drift")
                changed = conn.execute(
                    "UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?",
                    (owner, now + lease_seconds, now, replay["claim_id"], now),
                )
                if changed.rowcount != 1:
                    return None
                conn.execute(
                    "UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?",
                    (owner, now + lease_seconds, now, replay["ticket_id"]),
                )
                self._append_event(
                    conn,
                    entity_type="ticket",
                    entity_id=str(replay["ticket_id"]),
                    event_type="scheduler_stage_reclaimed",
                    actor_id=owner,
                    payload={"claim_id": replay["claim_id"], "stage": "git_integration", "lease_expires_at": now + lease_seconds},
                )
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())

            row = conn.execute("""
                SELECT t.id,t.title,t.tranche_id,ac.attempt_number,ac.evidence_hash,ac.candidate_fingerprint,
                       ac.base_sha,ac.worktree_path,a.branch
                FROM tickets t
                JOIN accepted_candidates ac ON ac.ticket_id=t.id
                JOIN attempts a ON a.ticket_id=t.id AND a.attempt_number=ac.attempt_number
                WHERE t.state='accepted'
                  AND NOT EXISTS (SELECT 1 FROM git_commit_evidence ge WHERE ge.ticket_id=t.id)
                  AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('git_integration:' || ac.attempt_number))
                ORDER BY t.created_at,t.id LIMIT 1
            """).fetchone()
            if row is None:
                return None
            identity = {
                "ticket_id": str(row["id"]),
                "attempt_number": int(row["attempt_number"]),
                "accepted_evidence_hash": str(row["evidence_hash"]),
                "candidate_fingerprint": str(row["candidate_fingerprint"]),
                "base_sha": str(row["base_sha"]),
                "worktree_path": str(row["worktree_path"]),
                "branch": str(row["branch"] or ""),
                "tranche_id": None if row["tranche_id"] is None else str(row["tranche_id"]),
                "commit_message": f"local-first: {row['title']}",
            }
            encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            stage = f"git_integration:{identity['attempt_number']}"
            claim_id = hashlib.sha256((stage + ":" + encoded).encode()).hexdigest()[:32]
            changed = conn.execute(
                "UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND state='accepted' AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (owner, now + lease_seconds, now, row["id"], now),
            )
            if changed.rowcount != 1:
                return None
            conn.execute(
                "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,'claimed',?,?,1,?,?,?)",
                (claim_id, row["id"], stage, owner, now + lease_seconds, encoded, now, now),
            )
            self._append_event(
                conn,
                entity_type="ticket",
                entity_id=str(row["id"]),
                event_type="scheduler_stage_claimed",
                actor_id=owner,
                payload={"claim_id": claim_id, "stage": "git_integration", "candidate_identity": identity},
            )
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def start_git_commit_intent(self, ticket_id: str, candidate_identity: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        """Persist immutable commit launch intent before Git can be mutated."""
        now = self._now() if now is None else now
        required = ("attempt_number", "accepted_evidence_hash", "candidate_fingerprint", "base_sha", "worktree_path", "commit_message")
        if candidate_identity.get("ticket_id") != ticket_id or any(not candidate_identity.get(key) for key in required):
            raise ValueError("invalid git commit intent identity")
        with self._transaction() as conn:
            accepted = conn.execute("SELECT * FROM accepted_candidates WHERE ticket_id=?", (ticket_id,)).fetchone()
            if accepted is None:
                raise RuntimeError("git_integration_reconciliation_required: accepted candidate is missing")
            expected = (
                int(accepted["attempt_number"]),
                str(accepted["evidence_hash"]),
                str(accepted["candidate_fingerprint"]),
                str(accepted["base_sha"]),
                str(accepted["worktree_path"]),
            )
            incoming = (
                int(candidate_identity["attempt_number"]),
                str(candidate_identity["accepted_evidence_hash"]),
                str(candidate_identity["candidate_fingerprint"]),
                str(candidate_identity["base_sha"]),
                str(candidate_identity["worktree_path"]),
            )
            if incoming != expected:
                raise RuntimeError("git_integration_reconciliation_required: accepted candidate intent identity drift")
            values = (*incoming, str(candidate_identity["commit_message"]))
            existing = conn.execute("SELECT * FROM git_commit_intents WHERE ticket_id=?", (ticket_id,)).fetchone()
            if existing is not None:
                existing_values = tuple(existing[key] for key in ("attempt_number", "accepted_evidence_hash", "candidate_fingerprint", "base_sha", "worktree_path", "commit_message"))
                if existing_values != values:
                    raise RuntimeError("git_integration_reconciliation_required: conflicting git commit intent")
                return dict(existing)
            conn.execute(
                "INSERT INTO git_commit_intents(ticket_id,attempt_number,accepted_evidence_hash,candidate_fingerprint,base_sha,worktree_path,commit_message,status,created_at) VALUES (?,?,?,?,?,?,?,'started',?)",
                (ticket_id, *values, now),
            )
            self._append_event(
                conn,
                entity_type="ticket",
                entity_id=ticket_id,
                event_type="git_commit_intent_started",
                actor_id="controller",
                payload={
                    "attempt_number": values[0],
                    "accepted_evidence_hash": values[1],
                    "candidate_fingerprint": values[2],
                    "base_sha": values[3],
                    "worktree_path": values[4],
                    "commit_message": values[5],
                },
            )
            return dict(conn.execute("SELECT * FROM git_commit_intents WHERE ticket_id=?", (ticket_id,)).fetchone())

    def apply_scheduler_git_integration_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        """Atomically persist exact commit/integration evidence and finish the scheduler effect."""
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or not str(claim["stage"]).startswith("git_integration:"):
                raise ValueError("scheduler claim is not git integration")
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            if claim["side_effect_completed_at"] is not None:
                if claim["result_json"] != encoded:
                    raise RuntimeError("git integration completed effect result conflicts")
                return dict(claim)
            try:
                identity = json.loads(str(claim["candidate_identity_json"] or ""))
            except json.JSONDecodeError as exc:
                raise RuntimeError("git_integration_reconciliation_required: claim identity malformed") from exc
            if result.get("candidate_identity") != identity:
                raise RuntimeError("git_integration_reconciliation_required: result candidate identity drift")
            ticket_id = str(claim["ticket_id"])
            commit_sha = str(result.get("commit_sha") or "")
            before = str(result.get("integration_head_before") or "")
            after = str(result.get("integration_head_after") or "")
            if not commit_sha or not before or after != commit_sha:
                raise RuntimeError("git_integration_reconciliation_required: incomplete commit/integration result")
            intent = conn.execute("SELECT * FROM git_commit_intents WHERE ticket_id=?", (ticket_id,)).fetchone()
            accepted = conn.execute("SELECT * FROM accepted_candidates WHERE ticket_id=?", (ticket_id,)).fetchone()
            attempt = conn.execute(
                "SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?",
                (ticket_id, int(identity["attempt_number"])),
            ).fetchone()
            ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if intent is None or accepted is None or attempt is None or ticket is None or ticket["state"] != CanonicalState.ACCEPTED.value:
                raise RuntimeError("git_integration_reconciliation_required: commit authority is incomplete")
            intent_identity = tuple(intent[key] for key in ("attempt_number", "accepted_evidence_hash", "candidate_fingerprint", "base_sha", "worktree_path", "commit_message"))
            claim_identity = (
                int(identity["attempt_number"]),
                str(identity["accepted_evidence_hash"]),
                str(identity["candidate_fingerprint"]),
                str(identity["base_sha"]),
                str(identity["worktree_path"]),
                str(identity["commit_message"]),
            )
            if intent_identity != claim_identity:
                raise RuntimeError("git_integration_reconciliation_required: commit intent identity drift")
            accepted_identity = (
                int(accepted["attempt_number"]),
                str(accepted["evidence_hash"]),
                str(accepted["candidate_fingerprint"]),
                str(accepted["base_sha"]),
                str(accepted["worktree_path"]),
            )
            expected_accepted_identity = (
                int(identity["attempt_number"]),
                str(identity["accepted_evidence_hash"]),
                str(identity["candidate_fingerprint"]),
                str(identity["base_sha"]),
                str(identity["worktree_path"]),
            )
            ticket_tranche = None if ticket["tranche_id"] is None else str(ticket["tranche_id"])
            if accepted_identity != expected_accepted_identity or str(attempt["branch"] or "") != str(identity["branch"]) or ticket_tranche != identity.get("tranche_id"):
                raise RuntimeError("git_integration_reconciliation_required: accepted/attempt/tranche identity drift")
            evidence_values = (
                ticket_id,
                int(identity["attempt_number"]),
                str(identity["accepted_evidence_hash"]),
                str(identity["candidate_fingerprint"]),
                str(identity["base_sha"]),
                str(identity["worktree_path"]),
                str(identity["branch"]),
                str(identity["commit_message"]),
                commit_sha,
                None if identity.get("tranche_id") is None else str(identity["tranche_id"]),
                before,
                after,
            )
            evidence_keys = (
                "ticket_id", "attempt_number", "accepted_evidence_hash", "candidate_fingerprint", "base_sha",
                "worktree_path", "branch", "commit_message", "commit_sha", "tranche_id",
                "integration_head_before", "integration_head_after",
            )
            existing = conn.execute("SELECT * FROM git_commit_evidence WHERE ticket_id=?", (ticket_id,)).fetchone()
            if existing is not None:
                if tuple(existing[key] for key in evidence_keys) != evidence_values:
                    raise RuntimeError("git_integration_reconciliation_required: conflicting git commit evidence")
            else:
                conn.execute(
                    "INSERT INTO git_commit_evidence(ticket_id,attempt_number,accepted_evidence_hash,candidate_fingerprint,base_sha,worktree_path,branch,commit_message,commit_sha,tranche_id,integration_head_before,integration_head_after,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*evidence_values, now),
                )
            if attempt["accepted_commit_sha"] not in (None, commit_sha):
                raise RuntimeError("git_integration_reconciliation_required: attempt commit identity conflicts")
            conn.execute(
                "UPDATE attempts SET accepted_commit_sha=? WHERE ticket_id=? AND attempt_number=? AND (accepted_commit_sha IS NULL OR accepted_commit_sha=?)",
                (commit_sha, ticket_id, int(identity["attempt_number"]), commit_sha),
            )
            if intent["status"] == "completed" and intent["commit_sha"] != commit_sha:
                raise RuntimeError("git_integration_reconciliation_required: completed intent commit conflicts")
            conn.execute(
                "UPDATE git_commit_intents SET status='completed',commit_sha=?,completed_at=COALESCE(completed_at,?) WHERE ticket_id=?",
                (commit_sha, now, ticket_id),
            )
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL",
                (now, encoded, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(
                conn,
                entity_type="ticket",
                entity_id=ticket_id,
                event_type="scheduler_stage_effect_completed",
                actor_id=owner,
                payload={"claim_id": claim_id, "stage": "git_integration", "result": result},
            )
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def claim_next_scheduler_repair_routing(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        """Claim one failed validation or completed review for routing only."""
        if not owner or lease_seconds < 1:
            raise ValueError("scheduler claim requires an owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute("SELECT * FROM scheduler_stage_claims WHERE stage LIKE 'repair_routing:%' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,)).fetchone()
            if replay is not None:
                if conn.execute("UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?", (owner, now + lease_seconds, now, replay["claim_id"], now)).rowcount != 1:
                    return None
                conn.execute("UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?", (owner, now + lease_seconds, now, replay["ticket_id"]))
                self._append_event(conn, entity_type="ticket", entity_id=str(replay["ticket_id"]), event_type="scheduler_stage_reclaimed", actor_id=owner, payload={"claim_id": replay["claim_id"], "stage": "repair_routing", "lease_expires_at": now + lease_seconds})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())
            row = conn.execute("""
                SELECT t.id, t.state,
                       COALESCE((SELECT MAX(r.attempt_number) FROM runtime_stages r WHERE r.ticket_id=t.id AND r.stage LIKE 'validation-%'),
                                (SELECT MAX(m.attempt_number) FROM model_stage_artifacts m WHERE m.ticket_id=t.id AND m.stage='review')) AS attempt_number
                FROM tickets t
                WHERE (
                    t.state='verifying' AND EXISTS (
                        SELECT 1 FROM runtime_stages r WHERE r.ticket_id=t.id AND r.stage=('validation-' || r.attempt_number)
                        AND json_valid(r.detail)=1 AND json_extract(r.detail,'$.passed')=0
                    )
                ) OR (
                    t.state='local_review' AND EXISTS (
                        SELECT 1 FROM model_stage_artifacts m WHERE m.ticket_id=t.id AND m.stage='review'
                    )
                )
                ORDER BY t.created_at,t.id LIMIT 1
            """).fetchone()
            if row is None or row["attempt_number"] is None:
                return None
            ticket_id = str(row["id"]); attempt_number = int(row["attempt_number"]); stage = f"repair_routing:{attempt_number}"
            if conn.execute("SELECT 1 FROM scheduler_stage_claims WHERE ticket_id=? AND stage=?", (ticket_id, stage)).fetchone():
                return None
            claim_id = hashlib.sha256(f"{stage}:{ticket_id}".encode()).hexdigest()[:32]
            conn.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,created_at,updated_at) VALUES (?,?,?,'claimed',?,?,1,?,?)", (claim_id,ticket_id,stage,owner,now+lease_seconds,now,now))
            conn.execute("UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?", (owner,now+lease_seconds,now,ticket_id))
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_claimed", actor_id=owner, payload={"claim_id":claim_id,"stage":"repair_routing","attempt_number":attempt_number})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def apply_scheduler_repair_routing_effect(self, claim_id: str, owner: str, *, now: int | None = None) -> dict[str, Any]:
        """Derive and persist one repair/pass/triage decision atomically."""
        now = self._now() if now is None else now
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or not str(claim["stage"]).startswith("repair_routing:"):
                raise ValueError("scheduler claim is not repair routing")
            if claim["side_effect_completed_at"] is not None:
                return dict(claim)
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")

            ticket_id = str(claim["ticket_id"])
            attempt_number = int(str(claim["stage"]).split(":", 1)[1])
            ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if ticket is None:
                raise KeyError(ticket_id)
            state = str(ticket["state"])
            max_attempts = int(ticket["max_attempts"])
            result: dict[str, Any]

            if state == CanonicalState.VERIFYING.value:
                validation = conn.execute("SELECT * FROM runtime_stages WHERE ticket_id=? AND stage=?", (ticket_id, f"validation-{attempt_number}")).fetchone()
                if validation is None:
                    raise RuntimeError("repair_routing_reconciliation_required: failed validation evidence is missing")
                try:
                    detail = json.loads(str(validation["detail"]))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("repair_routing_reconciliation_required: validation evidence is malformed") from exc
                if bool(detail.get("passed")):
                    raise RuntimeError("repair_routing_reconciliation_required: passing validation is not repair-routable")
                compact = str(detail.get("compact_evidence") or "")
                if not compact:
                    raise RuntimeError("repair_routing_reconciliation_required: validation failure evidence is empty")
                fingerprint = _stable_scheduler_failure_fingerprint(ticket_id, "validation", compact)
                repeated = False
                for prior in conn.execute("SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage LIKE 'validation-%' AND attempt_number<?", (ticket_id, attempt_number)):
                    try:
                        prior_detail = json.loads(str(prior["detail"]))
                    except json.JSONDecodeError:
                        continue
                    if bool(prior_detail.get("passed")):
                        continue
                    prior_compact = str(prior_detail.get("compact_evidence") or "")
                    prior_fingerprint = _stable_scheduler_failure_fingerprint(ticket_id, "validation", prior_compact)
                    repeated = repeated or prior_fingerprint == fingerprint
                action = "triage" if repeated or attempt_number >= max_attempts else "repair"
                result = {"ticket_id":ticket_id,"attempt_number":attempt_number,"source":"validation","action":action,"failure_fingerprint":fingerprint,"repeated_fingerprint":repeated,"failure_evidence":compact}
            elif state == CanonicalState.LOCAL_REVIEW.value:
                review_claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage=? AND side_effect_completed_at IS NOT NULL AND result_json IS NOT NULL", (ticket_id, f"review:{attempt_number}")).fetchone()
                if review_claim is None:
                    raise RuntimeError("repair_routing_reconciliation_required: completed review result is missing")
                try:
                    review_result = json.loads(str(review_claim["result_json"]))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("repair_routing_reconciliation_required: review result is malformed") from exc
                verdict = str(review_result.get("review_verdict") or "")
                findings = review_result.get("findings") or []
                criterion_results = review_result.get("criterion_results") or []
                suggestions = review_result.get("suggestions") or []
                review_raw = review_result.get("review_raw") or {}
                if verdict not in {"pass","repair","escalate"} or not isinstance(findings, list) or not isinstance(criterion_results, list):
                    raise RuntimeError("repair_routing_reconciliation_required: normalized review result is incomplete")
                repeated = any(
                    isinstance(finding, dict) and finding.get("fingerprint") and conn.execute("SELECT 1 FROM review_findings WHERE ticket_id=? AND fingerprint=? LIMIT 1", (ticket_id, finding["fingerprint"])).fetchone() is not None
                    for finding in findings
                )
                if verdict == "pass" or (verdict == "repair" and not findings):
                    action = "pass"
                elif verdict == "escalate" or repeated or attempt_number >= max_attempts:
                    action = "triage"
                else:
                    action = "repair"
                result = {"ticket_id":ticket_id,"attempt_number":attempt_number,"source":"review","action":action,"review_verdict":verdict,"repeated_fingerprint":repeated,"failure_fingerprint":next((str(item.get("fingerprint")) for item in findings if isinstance(item, dict) and item.get("fingerprint")), None),"failure_evidence":"; ".join(str(item.get("evidence") or "") for item in findings if isinstance(item, dict) and item.get("evidence")),"suggestions":suggestions}
                conn.execute("INSERT OR REPLACE INTO review_results(ticket_id,attempt_number,verdict,payload_json,created_at) VALUES (?,?,?,?,?)", (ticket_id,attempt_number,verdict,json.dumps(review_raw,sort_keys=True),now))
                for finding in findings:
                    if not isinstance(finding, dict) or not finding.get("fingerprint"):
                        continue
                    conn.execute("INSERT INTO review_findings(ticket_id,attempt_number,fingerprint,payload_json,created_at) VALUES (?,?,?,?,?)", (ticket_id,attempt_number,str(finding["fingerprint"]),json.dumps(finding,sort_keys=True),now))
                for criterion in criterion_results:
                    if isinstance(criterion, dict) and criterion.get("criterion_id") and criterion.get("status") == "fail":
                        conn.execute("INSERT INTO criterion_statuses(ticket_id,criterion_id,status,evidence,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(ticket_id,criterion_id) DO UPDATE SET status=excluded.status,evidence=excluded.evidence,updated_at=excluded.updated_at", (ticket_id,str(criterion["criterion_id"]),"open",str(criterion.get("evidence") or "review failure"),now))
            else:
                raise RuntimeError("repair_routing_reconciliation_required: ticket state is not routable")

            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
            stage_name = f"repair-routing-{attempt_number}"
            existing_stage = conn.execute("SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage=?", (ticket_id, stage_name)).fetchone()
            if existing_stage is not None and str(existing_stage["detail"]) != encoded:
                raise RuntimeError("repair_routing_reconciliation_required: durable routing decision conflicts")
            conn.execute("INSERT OR IGNORE INTO runtime_stages(ticket_id,stage,detail,attempt_number,created_at) VALUES (?,?,?,?,?)", (ticket_id,stage_name,encoded,attempt_number,now))

            target = CanonicalState.REPAIRING if result["action"] == "repair" else CanonicalState.NEEDS_TRIAGE if result["action"] == "triage" else None
            if target is not None:
                current = CanonicalState(state)
                validate_transition(current, target)
                changed = conn.execute("UPDATE tickets SET state=?,updated_at=? WHERE id=? AND state=?", (target.value,now,ticket_id,current.value))
                if changed.rowcount != 1:
                    raise RuntimeError("ticket changed concurrently")
                event_id = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="state_transition", actor_id="repair_router", from_state=current.value, to_state=target.value, payload=result)
                if target.value in self._PROJECTABLE_STATES:
                    self._enqueue_projection_bundle_in_transaction(conn, ticket_id=ticket_id, event_id=event_id, evidence=f"repair routing={result['action']}", state_payload=result)
                if target == CanonicalState.REPAIRING:
                    implementation = conn.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='implementation'", (ticket_id,attempt_number)).fetchone()
                    attempt = conn.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id,attempt_number)).fetchone()
                    if implementation is None or attempt is None or not attempt["worktree_path"]:
                        raise RuntimeError("repair_routing_reconciliation_required: repair candidate provenance is incomplete")
                    next_attempt = attempt_number + 1
                    if next_attempt > max_attempts:
                        raise RuntimeError("repair_routing_reconciliation_required: repair exceeds max attempts")
                    conn.execute("INSERT OR IGNORE INTO attempts(ticket_id,attempt_number,base_sha,branch,worktree_path,pre_diff_hash,created_at) VALUES (?,?,?,?,?,?,?)", (ticket_id,next_attempt,attempt["base_sha"],attempt["branch"],attempt["worktree_path"],implementation["diff_hash"],now))
                    conn.execute("UPDATE attempts SET outcome='repair_requested',failure_fingerprint=? WHERE ticket_id=? AND attempt_number=?", (result.get("failure_fingerprint"),ticket_id,attempt_number))
                    result["next_attempt_number"] = next_attempt
                    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
                    conn.execute("UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage=?", (encoded,ticket_id,stage_name))

            if conn.execute("UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL", (now,encoded,now,claim_id,owner,now)).rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id":claim_id,"stage":"repair_routing","result":result})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def claim_next_scheduler_implementation(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        """Claim one ready-local implementation stage, including expired replay."""
        if not owner or lease_seconds < 1:
            raise ValueError("scheduler claim requires an owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or paused["paused"]:
                return None
            replay = conn.execute(
                "SELECT * FROM scheduler_stage_claims WHERE (stage='implementation' OR stage LIKE 'implementation:%') AND status='claimed' "
                "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
            ).fetchone()
            if replay is not None:
                changed = conn.execute(
                    "UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? "
                    "WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?",
                    (owner, now + lease_seconds, now, replay["claim_id"], now),
                )
                if changed.rowcount != 1:
                    return None
                conn.execute(
                    "UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND state=?",
                    (owner, now + lease_seconds, now, replay["ticket_id"], CanonicalState.IMPLEMENTING.value),
                )
                self._append_event(conn, entity_type="ticket", entity_id=str(replay["ticket_id"]), event_type="scheduler_stage_reclaimed", actor_id=owner, payload={"claim_id": replay["claim_id"], "stage": replay["stage"], "lease_expires_at": now + lease_seconds})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())

            candidate = conn.execute(
                "SELECT t.id,t.state FROM tickets t JOIN runtime_bindings rb ON rb.ticket_id=t.id "
                "WHERE t.state IN (?,?) AND (t.lease_expires_at IS NULL OR t.lease_expires_at<=?) "
                "AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND (c.stage='implementation' OR c.stage LIKE 'implementation:%') AND c.status='claimed') "
                "AND NOT EXISTS (SELECT 1 FROM board_projection_outbox b WHERE b.ticket_id=t.id AND b.operation='create_microticket' AND b.superseded_at IS NULL) "
                "ORDER BY t.created_at,t.id LIMIT 1",
                (CanonicalState.READY_LOCAL.value, CanonicalState.REPAIRING.value, now),
            ).fetchone()
            if candidate is None:
                return None
            ticket_id = str(candidate["id"])
            source_state = CanonicalState(str(candidate["state"]))
            prior_claims = int(conn.execute("SELECT COUNT(*) AS n FROM scheduler_stage_claims WHERE ticket_id=? AND (stage='implementation' OR stage LIKE 'implementation:%')", (ticket_id,)).fetchone()["n"])
            if prior_claims == 0:
                stage_name = "implementation"
            else:
                latest_attempt = conn.execute("SELECT MAX(attempt_number) AS n FROM attempts WHERE ticket_id=?", (ticket_id,)).fetchone()["n"]
                if latest_attempt is None:
                    raise RuntimeError("repair implementation requires a durable attempt identity")
                stage_name = f"implementation:{int(latest_attempt)}"
            claim_id = hashlib.sha256(f"{stage_name}:{ticket_id}".encode()).hexdigest()[:32]
            changed = conn.execute(
                "UPDATE tickets SET state=?,lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND state=? AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (CanonicalState.IMPLEMENTING.value, owner, now + lease_seconds, now, ticket_id, source_state.value, now),
            )
            if changed.rowcount != 1:
                return None
            conn.execute(
                "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,created_at,updated_at) VALUES (?,?,?,'claimed',?,?,1,?,?)",
                (claim_id, ticket_id, stage_name, owner, now + lease_seconds, now, now),
            )
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="lease_claimed", actor_id=owner, from_state=source_state.value, to_state=CanonicalState.IMPLEMENTING.value, payload={"lease_expires_at": now + lease_seconds, "scheduler_claim_id": claim_id})
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_claimed", actor_id=owner, payload={"claim_id": claim_id, "stage": stage_name, "lease_expires_at": now + lease_seconds})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def hermes_execution_candidates(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT DISTINCT t.id AS ticket_id,t.state,b.external_task_id,t.created_at "
            "FROM tickets t JOIN board_projection_outbox b ON b.ticket_id=t.id AND b.operation='create_microticket' "
            "WHERE b.acknowledged_at IS NOT NULL AND b.external_task_id IS NOT NULL AND b.superseded_at IS NULL "
            "AND t.state IN (?,?,?) "
            "ORDER BY t.created_at,t.id,b.external_task_id",
            (CanonicalState.READY_LOCAL.value, CanonicalState.IMPLEMENTING.value, CanonicalState.REPAIRING.value),
        ).fetchall()
        return [dict(row) for row in rows]

    def generated_activation_candidates(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT DISTINCT t.id AS ticket_id,b.external_task_id,t.created_at "
            "FROM tickets t JOIN board_projection_outbox b ON b.ticket_id=t.id AND b.operation='create_microticket' "
            "WHERE b.acknowledged_at IS NOT NULL AND b.external_task_id IS NOT NULL AND b.superseded_at IS NULL "
            "AND t.state=? "
            "AND NOT EXISTS (SELECT 1 FROM runtime_bindings rb WHERE rb.ticket_id=t.id) "
            "ORDER BY t.created_at,t.id,b.external_task_id",
            (CanonicalState.DRAFT.value,),
        ).fetchall()
        return [dict(row) for row in rows]

    def complete_scheduler_implementation_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        """Persist model-stage completion before scheduler-claim finalization."""
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None:
                raise KeyError(claim_id)
            if claim["stage"] != "implementation" and not str(claim["stage"]).startswith("implementation:"):
                raise ValueError("scheduler claim is not implementation")
            if claim["side_effect_completed_at"] is not None:
                return dict(claim)
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            ticket_id = str(claim["ticket_id"])
            if str(result.get("ticket_id") or "") != ticket_id:
                raise RuntimeError("implementation result ticket does not match scheduler claim")
            try:
                attempt_number = int(result["attempt_number"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("implementation result is missing a valid attempt number") from exc
            model_stage = conn.execute(
                "SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='implementation'",
                (ticket_id, attempt_number),
            ).fetchone()
            if model_stage is None:
                raise RuntimeError("implementation model stage is not durably recorded")
            if str(result.get("implementation_artifact") or "") != str(model_stage["response_artifact"]):
                raise RuntimeError("implementation result artifact does not match durable model stage")
            if str(result.get("diff_hash") or "") != str(model_stage["diff_hash"]):
                raise RuntimeError("implementation result diff does not match durable model stage")
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL",
                (now, encoded, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id": claim_id, "stage": "implementation", "result": result})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def _validation_policy_hash(self, ticket: Any) -> str:
        policy = {
            "criterion_ids_json": ticket["criterion_ids_json"],
            "allowed_files_json": ticket["allowed_files_json"],
            "forbidden_changes_json": ticket["forbidden_changes_json"],
            "patch_budget_json": ticket["patch_budget_json"],
            "verification_json": ticket["verification_json"],
            "new_test_files_json": ticket["new_test_files_json"],
            "create_files_json": ticket["create_files_json"],
        }
        return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def review_policy_hash(self, ticket: Any, execution_policy_hash: str) -> str:
        """Bind review claims to ticket policy plus configured review execution identity."""
        if not execution_policy_hash:
            raise ValueError("review execution policy hash is required")
        policy = {
            "ticket_policy_hash": self._validation_policy_hash(ticket),
            "review_execution_policy_hash": execution_policy_hash,
        }
        return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def claim_next_scheduler_validation(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        """Claim one exact implementation candidate for deterministic validation."""
        if not owner or lease_seconds < 1:
            raise ValueError("scheduler claim requires an owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or paused["paused"]:
                return None
            replay = conn.execute(
                "SELECT * FROM scheduler_stage_claims WHERE stage LIKE 'validation:%' AND status='claimed' "
                "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,)
            ).fetchone()
            if replay is not None:
                changed = conn.execute(
                    "UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? "
                    "WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?",
                    (owner, now + lease_seconds, now, replay["claim_id"], now),
                )
                if changed.rowcount != 1:
                    return None
                conn.execute(
                    "UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND lease_owner=?",
                    (owner, now + lease_seconds, now, replay["ticket_id"], replay["lease_owner"]),
                )
                self._append_event(conn, entity_type="ticket", entity_id=str(replay["ticket_id"]), event_type="scheduler_stage_reclaimed", actor_id=owner, payload={"claim_id": replay["claim_id"], "stage": "validation", "lease_expires_at": now + lease_seconds})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())
            candidate = conn.execute(
                "SELECT t.*,m.attempt_number,m.response_artifact,m.worktree_path,m.base_sha,m.diff_hash "
                "FROM tickets t JOIN model_stage_artifacts m ON m.ticket_id=t.id AND m.stage='implementation' "
                "AND m.attempt_number=(SELECT MAX(latest.attempt_number) FROM model_stage_artifacts latest WHERE latest.ticket_id=t.id AND latest.stage='implementation') "
                "WHERE t.state=? AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('validation:' || m.attempt_number)) "
                "AND NOT EXISTS (SELECT 1 FROM runtime_stages r WHERE r.ticket_id=t.id AND r.stage=('validation-' || m.attempt_number)) "
                "ORDER BY t.created_at,t.id LIMIT 1",
                (CanonicalState.IMPLEMENTING.value,),
            ).fetchone()
            if candidate is None:
                return None
            ticket_id = str(candidate["id"])
            implementation_artifact = Path(str(candidate["response_artifact"]))
            if not implementation_artifact.is_file():
                raise RuntimeError("validation_reconciliation_required: implementation artifact is missing")
            implementation_artifact_sha256 = hashlib.sha256(implementation_artifact.read_bytes()).hexdigest()
            identity = {
                "ticket_id": ticket_id,
                "attempt_number": int(candidate["attempt_number"]),
                "implementation_artifact": str(candidate["response_artifact"]),
                "implementation_artifact_sha256": implementation_artifact_sha256,
                "worktree_path": str(candidate["worktree_path"]),
                "base_sha": str(candidate["base_sha"]),
                "implementation_diff_hash": str(candidate["diff_hash"]),
                "validation_policy_hash": self._validation_policy_hash(candidate),
            }
            claim_id = hashlib.sha256(("validation:" + json.dumps(identity, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()[:32]
            changed = conn.execute(
                "UPDATE tickets SET state=?,lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND state=?",
                (CanonicalState.VERIFYING.value, owner, now + lease_seconds, now, ticket_id, CanonicalState.IMPLEMENTING.value),
            )
            if changed.rowcount != 1:
                return None
            conn.execute(
                "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,'claimed',?,?,1,?,?,?)",
                (claim_id, ticket_id, f"validation:{identity['attempt_number']}", owner, now + lease_seconds, json.dumps(identity, sort_keys=True, separators=(",", ":")), now, now),
            )
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_claimed", actor_id=owner, from_state=CanonicalState.IMPLEMENTING.value, to_state=CanonicalState.VERIFYING.value, payload={"claim_id": claim_id, "stage": "validation", "candidate_identity": identity})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def complete_scheduler_validation_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        """Require an identity-bound persisted validation artifact before completion."""
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None:
                raise KeyError(claim_id)
            if not str(claim["stage"]).startswith("validation:"):
                raise ValueError("scheduler claim is not validation")
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            identity = json.loads(str(claim["candidate_identity_json"] or ""))
            if result.get("candidate_identity") != identity:
                raise RuntimeError("validation result candidate identity does not match scheduler claim")
            stage = conn.execute("SELECT * FROM runtime_stages WHERE ticket_id=? AND stage=?", (claim["ticket_id"], f"validation-{identity['attempt_number']}")).fetchone()
            validation_artifact = Path(str(stage["artifact_path"] or "")) if stage is not None else None
            if (
                stage is None
                or stage["artifact_path"] != result.get("validation_artifact")
                or stage["artifact_sha256"] != result.get("validation_artifact_sha256")
                or validation_artifact is None
                or not validation_artifact.is_file()
                or hashlib.sha256(validation_artifact.read_bytes()).hexdigest() != str(stage["artifact_sha256"])
            ):
                raise RuntimeError("validation_reconciliation_required: validation artifact is not durably recorded")
            try:
                stage_detail = json.loads(str(stage["detail"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("validation artifact detail is malformed") from exc
            if stage_detail.get("candidate_identity") != identity:
                raise RuntimeError("validation artifact candidate identity does not match scheduler claim")
            if claim["side_effect_completed_at"] is not None:
                if claim["result_json"] != encoded:
                    raise RuntimeError("validation completed effect result conflicts")
                return dict(claim)
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL",
                (now, encoded, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(conn, entity_type="ticket", entity_id=str(claim["ticket_id"]), event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id": claim_id, "stage": "validation", "result": result})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def claim_next_scheduler_review(self, owner: str, *, lease_seconds: int, review_execution_policy_hash: str, now: int | None = None) -> dict[str, Any] | None:
        """Claim one exact passing validation candidate for packet-only review."""
        if not owner or lease_seconds < 1:
            raise ValueError("scheduler claim requires an owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute("SELECT * FROM scheduler_stage_claims WHERE stage LIKE 'review:%' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,)).fetchone()
            if replay is not None:
                try:
                    replay_identity = json.loads(str(replay["candidate_identity_json"] or ""))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("review_reconciliation_required: review claim identity is malformed") from exc
                replay_ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (replay["ticket_id"],)).fetchone()
                if replay_ticket is None or replay_identity.get("review_policy_hash") != self.review_policy_hash(replay_ticket, review_execution_policy_hash):
                    raise RuntimeError("review_reconciliation_required: review execution policy drift")
                if conn.execute("UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?", (owner, now + lease_seconds, now, replay["claim_id"], now)).rowcount != 1:
                    return None
                conn.execute("UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?", (owner, now + lease_seconds, now, replay["ticket_id"]))
                self._append_event(conn, entity_type="ticket", entity_id=str(replay["ticket_id"]), event_type="scheduler_stage_reclaimed", actor_id=owner, payload={"claim_id": replay["claim_id"], "stage": "review", "lease_expires_at": now + lease_seconds})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())
            row = conn.execute("""
                SELECT t.*, r.detail, r.artifact_path, r.artifact_sha256, m.response_artifact, m.diff_hash,
                       mi.invocation_id,
                       he.external_task_id AS hermes_external_task_id,
                       he.hermes_run_id,
                       CASE
                         WHEN mi.invocation_id IS NOT NULL THEN mi.invocation_id
                         WHEN he.hermes_run_id IS NOT NULL THEN ('hermes-run:' || he.external_task_id || ':' || he.hermes_run_id)
                       END AS implementation_execution_id
                FROM tickets t
                JOIN runtime_stages r ON r.ticket_id=t.id AND r.stage='validation_completed'
                JOIN model_stage_artifacts m ON m.ticket_id=t.id AND m.attempt_number=r.attempt_number AND m.stage='implementation'
                LEFT JOIN model_invocations mi ON mi.ticket_id=t.id AND mi.attempt_number=r.attempt_number AND mi.stage='implementation' AND mi.status='completed'
                LEFT JOIN hermes_execution_reconciliations he ON he.ticket_id=t.id AND he.attempt_number=r.attempt_number
                WHERE t.state=? AND EXISTS (SELECT 1 FROM scheduler_stage_claims v WHERE v.ticket_id=t.id AND v.stage=('validation:' || r.attempt_number) AND v.side_effect_completed_at IS NOT NULL)
                AND ((mi.invocation_id IS NOT NULL AND he.hermes_run_id IS NULL) OR (mi.invocation_id IS NULL AND he.hermes_run_id IS NOT NULL))
                AND NOT EXISTS (SELECT 1 FROM scheduler_stage_claims c WHERE c.ticket_id=t.id AND c.stage=('review:' || r.attempt_number))
                ORDER BY t.created_at,t.id LIMIT 1
            """, (CanonicalState.LOCAL_REVIEW.value,)).fetchone()
            if row is None:
                return None
            try:
                validation = json.loads(str(row["detail"]))
                attempt_number = int(validation["candidate_identity"]["attempt_number"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("review_reconciliation_required: validation evidence is malformed") from exc
            artifact = Path(str(row["artifact_path"] or ""))
            if not bool(validation.get("passed")) or not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != str(row["artifact_sha256"]):
                raise RuntimeError("review_reconciliation_required: validation evidence is incomplete")
            if str(validation["candidate_identity"].get("implementation_diff_hash")) != str(row["diff_hash"]):
                raise RuntimeError("review_reconciliation_required: validated candidate drift")
            ticket_id = str(row["id"])
            policy = self.review_policy_hash(row, review_execution_policy_hash)
            identity = {"ticket_id": ticket_id, "attempt_number": attempt_number, "implementation_diff_hash": str(row["diff_hash"]), "validation_artifact": str(row["artifact_path"]), "validation_artifact_sha256": str(row["artifact_sha256"]), "validation_evidence_hash": hashlib.sha256(str(validation.get("compact_evidence", "")).encode()).hexdigest(), "review_policy_hash": policy}
            candidate = conn.execute("SELECT * FROM review_candidates WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            encoded_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            if candidate is None:
                conn.execute("INSERT INTO review_candidates(ticket_id,attempt_number,candidate_fingerprint,validation_evidence,implementation_invocation_id,runtime_identity_json,status,historical_review_attempted,historical_provenance_json,created_at,updated_at) VALUES (?,?,?,?,?,?, 'review_pending',0,'{}',?,?)", (ticket_id,attempt_number,str(row["diff_hash"]),str(validation["compact_evidence"]),str(row["implementation_execution_id"]),encoded_identity,now,now))
                self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="validated_review_candidate_frozen", actor_id="scheduler", payload={"attempt_number":attempt_number,"candidate_fingerprint":str(row["diff_hash"])})
            elif candidate["candidate_fingerprint"] != str(row["diff_hash"]) or candidate["validation_evidence"] != str(validation["compact_evidence"]) or candidate["runtime_identity_json"] != encoded_identity:
                raise RuntimeError("review_reconciliation_required: review candidate identity drift")
            claim_id = hashlib.sha256(("review:" + encoded_identity).encode()).hexdigest()[:32]
            if conn.execute("UPDATE tickets SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND state=?", (owner,now+lease_seconds,now,ticket_id,CanonicalState.LOCAL_REVIEW.value)).rowcount != 1:
                return None
            conn.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,'claimed',?,?,1,?,?,?)", (claim_id,ticket_id,f"review:{attempt_number}",owner,now+lease_seconds,encoded_identity,now,now))
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_claimed", actor_id=owner, payload={"claim_id":claim_id,"stage":"review","candidate_identity":identity})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def complete_scheduler_review_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        """Complete only after the exact review model stage has been persisted."""
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or not str(claim["stage"]).startswith("review:"):
                raise ValueError("scheduler claim is not review")
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            identity = json.loads(str(claim["candidate_identity_json"] or ""))
            if result.get("candidate_identity") != identity:
                raise RuntimeError("review result candidate identity does not match scheduler claim")
            stage = conn.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='review'", (claim["ticket_id"], identity["attempt_number"])).fetchone()
            artifact = Path(str(stage["response_artifact"] or "")) if stage is not None else None
            if stage is None or not artifact.is_file() or str(stage["diff_hash"]) != str(identity["implementation_diff_hash"]):
                raise RuntimeError("review_reconciliation_required: review output is not durably recorded")
            if claim["side_effect_completed_at"] is not None:
                if claim["result_json"] != encoded: raise RuntimeError("review completed effect result conflicts")
                return dict(claim)
            if conn.execute("UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL", (now,encoded,now,claim_id,owner,now)).rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(conn, entity_type="ticket", entity_id=str(claim["ticket_id"]), event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id":claim_id,"stage":"review","result":result})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def claim_next_scheduler_readiness(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        """Claim one dependency-ready admission stage, including expired replay."""
        if not owner or lease_seconds < 1:
            raise ValueError("scheduler claim requires an owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or paused["paused"]:
                return None
            replay = conn.execute(
                "SELECT * FROM scheduler_stage_claims WHERE stage='dependency_readiness' AND status='claimed' "
                "AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,),
            ).fetchone()
            if replay is not None:
                changed = conn.execute(
                    "UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? "
                    "WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?",
                    (owner, now + lease_seconds, now, replay["claim_id"], now),
                )
                if changed.rowcount != 1:
                    return None
                self._append_event(conn, entity_type="ticket", entity_id=str(replay["ticket_id"]), event_type="scheduler_stage_reclaimed", actor_id=owner, payload={"claim_id": replay["claim_id"], "stage": replay["stage"], "lease_expires_at": now + lease_seconds})
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())
            candidate = conn.execute("""
                SELECT t.id FROM tickets t
                JOIN runtime_bindings rb ON rb.ticket_id=t.id
                WHERE t.state=?
                  AND json_valid(t.dependencies_json)=1
                  AND json_type(t.dependencies_json)='array'
                  AND json_array_length(t.dependencies_json)=0
                  AND NOT EXISTS (
                    SELECT 1 FROM json_each(CASE WHEN json_valid(t.dependencies_json) THEN t.dependencies_json ELSE '[]' END) requested
                    LEFT JOIN tickets dependency ON dependency.id=requested.value
                    LEFT JOIN accepted_evidence evidence ON evidence.ticket_id=dependency.id
                    WHERE dependency.id IS NULL
                       OR dependency.state NOT IN (?, ?)
                       OR (dependency.state=? AND evidence.ticket_id IS NULL)
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM scheduler_stage_claims claim
                    WHERE claim.ticket_id=t.id AND claim.stage='dependency_readiness'
                  )
                ORDER BY t.created_at,t.id LIMIT 1
            """, (CanonicalState.DRAFT.value, CanonicalState.ACCEPTED.value, CanonicalState.DONE.value, CanonicalState.DONE.value)).fetchone()
            if candidate is None:
                return None
            ticket_id = str(candidate["id"])
            claim_id = hashlib.sha256(f"dependency_readiness:{ticket_id}".encode()).hexdigest()[:32]
            conn.execute(
                "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,created_at,updated_at) "
                "VALUES (?,?,?,'claimed',?,?,1,?,?)",
                (claim_id, ticket_id, "dependency_readiness", owner, now + lease_seconds, now, now),
            )
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="scheduler_stage_claimed", actor_id=owner, payload={"claim_id": claim_id, "stage": "dependency_readiness", "lease_expires_at": now + lease_seconds})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def begin_scheduler_claim_effect(self, claim_id: str, owner: str, *, now: int | None = None) -> dict[str, Any]:
        """Durably mark a claimed stage as started before any stage side effect."""
        now = self._now() if now is None else now
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if row is None:
                raise KeyError(claim_id)
            if row["status"] == "completed":
                return dict(row)
            if row["side_effect_completed_at"] is not None:
                return dict(row)
            if row["side_effect_started_at"] is not None:
                return dict(row)
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET side_effect_started_at=?,updated_at=? "
                "WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_started_at IS NULL",
                (now, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(
                conn,
                entity_type="ticket",
                entity_id=str(row["ticket_id"]),
                event_type="scheduler_stage_effect_started",
                actor_id=owner,
                payload={"claim_id": claim_id, "stage": row["stage"]},
            )
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def apply_scheduler_readiness_effect(self, claim_id: str, owner: str, *, now: int | None = None) -> dict[str, Any]:
        """Apply readiness and its durable completion marker atomically.

        The prior begin marker lives in a separate transaction.  Therefore a
        crash before this transaction leaves an explicit started/unknown claim;
        a crash after commit always leaves both the ticket transition/outbox and
        side_effect_completed_at visible together.
        """
        now = self._now() if now is None else now
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None:
                raise KeyError(claim_id)
            if claim["stage"] != "dependency_readiness":
                raise ValueError("scheduler claim is not dependency readiness")
            if claim["status"] == "completed" or claim["side_effect_completed_at"] is not None:
                return dict(claim)
            if claim["side_effect_started_at"] is None:
                raise RuntimeError("scheduler claim effect was not durably started")
            if claim["lease_owner"] != owner or claim["lease_expires_at"] is None or int(claim["lease_expires_at"]) <= now:
                raise PermissionError("scheduler claim lease is not owned")

            ticket_id = str(claim["ticket_id"])
            ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket["state"] == CanonicalState.READY_LOCAL.value:
                result = {"status": "ready", "unresolved_dependency_ids": []}
            else:
                if ticket["state"] != CanonicalState.DRAFT.value:
                    raise RuntimeError(f"claimed readiness stage became ineligible: wrong_state")
                binding = conn.execute("SELECT repository_path,starting_sha FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
                if binding is None or not binding["repository_path"] or not binding["starting_sha"]:
                    raise RuntimeError("claimed readiness stage became ineligible: invalid_runtime_binding")
                try:
                    from .controller import ticket_from_ledger
                    validate_ticket(ticket_from_ledger(dict(ticket)))
                    dependencies = tuple(sorted(set(json.loads(ticket["dependencies_json"]))))
                except Exception as exc:
                    raise RuntimeError("claimed readiness stage became ineligible: invalid_ticket") from exc
                if ticket_id in dependencies:
                    raise RuntimeError("claimed readiness stage became ineligible: invalid_ticket")
                if dependencies:
                    raise RuntimeError("claimed readiness stage became ineligible: native_dependency_release_required")
                unresolved: list[str] = []
                for dependency in dependencies:
                    dep = conn.execute("SELECT state FROM tickets WHERE id=?", (dependency,)).fetchone()
                    if dep is None:
                        unresolved.append(dependency)
                        continue
                    if dep["state"] == CanonicalState.ACCEPTED.value:
                        continue
                    accepted = conn.execute("SELECT 1 FROM accepted_evidence WHERE ticket_id=? LIMIT 1", (dependency,)).fetchone()
                    if dep["state"] == CanonicalState.DONE.value and accepted is not None:
                        continue
                    unresolved.append(dependency)
                if unresolved:
                    raise RuntimeError("claimed readiness stage became ineligible: waiting_on_dependencies")

                target = CanonicalState.READY_LOCAL
                current = CanonicalState(ticket["state"])
                validate_transition(current, target)
                changed = conn.execute(
                    "UPDATE tickets SET state=?,updated_at=? WHERE id=? AND state=?",
                    (target.value, now, ticket_id, current.value),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("ticket changed concurrently")
                event_id = self._append_event(
                    conn,
                    entity_type="ticket",
                    entity_id=ticket_id,
                    event_type="state_transition",
                    actor_id="readiness",
                    from_state=current.value,
                    to_state=target.value,
                    payload={"reason": "dependencies_satisfied"},
                )
                self._inject_failure("after_event_creation")
                if target.value in self._PROJECTABLE_STATES:
                    self._enqueue_projection_bundle_in_transaction(
                        conn,
                        ticket_id=ticket_id,
                        event_id=event_id,
                        evidence=f"state={target.value}",
                        state_payload={"reason": "dependencies_satisfied"},
                    )
                result = {"status": "ready", "unresolved_dependency_ids": []}

            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? "
                "WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NULL",
                (now, encoded, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(
                conn,
                entity_type="ticket",
                entity_id=ticket_id,
                event_type="scheduler_stage_effect_completed",
                actor_id=owner,
                payload={"claim_id": claim_id, "stage": "dependency_readiness", "result": result},
            )
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def complete_scheduler_claim(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if row is None:
                raise KeyError(claim_id)
            if row["status"] == "completed":
                if row["result_json"] != encoded:
                    raise RuntimeError("scheduler claim result conflicts")
                return dict(row)
            if row["side_effect_completed_at"] is None:
                raise RuntimeError("scheduler claim side effect is not durably completed")
            if row["result_json"] is not None and row["result_json"] != encoded:
                raise RuntimeError("scheduler claim result conflicts")
            changed = conn.execute(
                "UPDATE scheduler_stage_claims SET status='completed',lease_owner=NULL,lease_expires_at=NULL,result_json=?,finalized_at=?,updated_at=? "
                "WHERE claim_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>? AND side_effect_completed_at IS NOT NULL",
                (encoded, now, now, claim_id, owner, now),
            )
            if changed.rowcount != 1:
                raise PermissionError("scheduler claim lease is not owned")
            self._append_event(conn, entity_type="ticket", entity_id=str(row["ticket_id"]), event_type="scheduler_stage_completed", actor_id=owner, payload={"claim_id": claim_id, "stage": row["stage"], "result": result})
            if row["stage"] == "implementation" or str(row["stage"]).startswith("implementation:") or str(row["stage"]).startswith("validation:") or str(row["stage"]).startswith("review:") or str(row["stage"]).startswith("repair_routing:") or str(row["stage"]).startswith("triage:") or str(row["stage"]).startswith("acceptance:") or str(row["stage"]).startswith("git_integration:") or str(row["stage"]).startswith("completion:") or row["stage"] in {"tranche_checkpoint", "paid_checkpoint", "paid_escalation", "next_tranche_materialize", "next_tranche_activation"}:
                conn.execute(
                    "UPDATE tickets SET lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? AND lease_owner=?",
                    (now, row["ticket_id"], owner),
                )
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def record_runtime_stage(self, ticket_id: str, stage: str, detail: str, *, attempt_number: int | None = None, artifact_path: str | None = None, artifact_sha256: str | None = None, base_sha: str | None = None) -> bool:
        with self._transaction() as conn:
            try: conn.execute("INSERT INTO runtime_stages(ticket_id, stage, detail, attempt_number, artifact_path, artifact_sha256, base_sha, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (ticket_id, stage, detail, attempt_number, artifact_path, artifact_sha256, base_sha, self._now()))
            except sqlite3.IntegrityError: return False
            return True

    def runtime_stage(self, ticket_id: str, stage: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM runtime_stages WHERE ticket_id=? AND stage=?", (ticket_id, stage)).fetchone()
        return dict(row) if row else None

    def model_stage(self, ticket_id: str, attempt_number: int, stage: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage=?", (ticket_id, attempt_number, stage)).fetchone()
        return dict(row) if row else None

    def record_model_stage(self, ticket_id: str, attempt_number: int, stage: str, *, purpose: str, adapter: str, request_hash: str, response_artifact: str, worktree_path: str, base_sha: str, diff_hash: str) -> bool:
        with self._transaction() as conn:
            try:
                conn.execute("INSERT INTO model_stage_artifacts(ticket_id,attempt_number,stage,purpose,adapter,request_hash,response_artifact,worktree_path,base_sha,diff_hash,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (ticket_id,attempt_number,stage,purpose,adapter,request_hash,response_artifact,worktree_path,base_sha,diff_hash,self._now()))
            except sqlite3.IntegrityError:
                return False
            return True

    def start_model_invocation(self, *, invocation_id: str, ticket_id: str, attempt_number: int, stage: str, provider: str, model: str, packet_hash: str, worktree_path: str, timeout_seconds: int) -> dict[str, Any]:
        """Durably record launch intent before a subprocess can be started."""
        if not all(isinstance(value, str) and value for value in (invocation_id, ticket_id, stage, provider, model, packet_hash, worktree_path)) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
            raise ValueError("invalid_model_invocation")
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
            if existing is not None:
                if existing["invocation_id"] != invocation_id or existing["status"] != "started":
                    raise RuntimeError("model_invocation_reconciliation_required")
                return dict(existing)
            conn.execute("INSERT INTO model_invocations(invocation_id,ticket_id,attempt_number,stage,provider,model,packet_hash,worktree_path,timeout_seconds,started_at,status) VALUES (?,?,?,?,?,?,?,?,?,?, 'started')", (invocation_id,ticket_id,attempt_number,stage,provider,model,packet_hash,worktree_path,timeout_seconds,self._now()))
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="model_invocation_started", actor_id="controller", payload={"invocation_id":invocation_id,"attempt_number":attempt_number,"stage":stage})
        return self.model_invocation(invocation_id)

    def finish_model_invocation(self, invocation_id: str, *, status: str, duration_seconds: float, error: dict[str, Any] | None = None, model_artifact: str | None = None) -> dict[str, Any]:
        if status not in {"completed", "timeout", "process_error", "malformed_output"}:
            raise ValueError("invalid_model_invocation_status")
        if duration_seconds < 0:
            raise ValueError("invalid_model_invocation_duration")
        encoded = json.dumps(error, sort_keys=True) if error is not None else None
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
            if row is None: raise KeyError(invocation_id)
            if row["status"] != "started":
                if (row["status"], row["model_artifact"], row["error_json"]) != (status, model_artifact, encoded):
                    raise RuntimeError("conflicting_model_invocation_result")
                return dict(row)
            conn.execute("UPDATE model_invocations SET status=?, completed_at=?, duration_seconds=?, error_json=?, model_artifact=? WHERE invocation_id=? AND status='started'", (status,self._now(),duration_seconds,encoded,model_artifact,invocation_id))
            self._append_event(conn, entity_type="ticket", entity_id=str(row["ticket_id"]), event_type="model_invocation_finished", actor_id="controller", payload={"invocation_id":invocation_id,"attempt_number":row["attempt_number"],"stage":row["stage"],"status":status})
        return self.model_invocation(invocation_id)

    def model_invocation(self, invocation_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        if row is None: raise KeyError(invocation_id)
        return dict(row)

    def incomplete_model_invocations(self, ticket_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM model_invocations WHERE status='started'"
        values: tuple[Any, ...] = ()
        if ticket_id is not None:
            query += " AND ticket_id=?"; values = (ticket_id,)
        return [dict(row) for row in self.connection.execute(query + " ORDER BY started_at, invocation_id", values)]

    def invocation_for_stage(self, ticket_id: str, attempt_number: int, stage: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM model_invocations WHERE ticket_id=? AND attempt_number=? AND stage=? ORDER BY started_at DESC, invocation_id DESC LIMIT 1", (ticket_id, attempt_number, stage)).fetchone()
        return dict(row) if row else None

    def review_invocations(self, ticket_id: str, attempt_number: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM model_invocations WHERE ticket_id=? AND attempt_number=? AND stage='review' ORDER BY started_at, invocation_id", (ticket_id, attempt_number))]

    _MAX_REVIEW_INVOCATIONS = 3

    def has_valid_review_verdict(self, ticket_id: str, attempt_number: int) -> bool:
        # review_results is the substantive coordinator record; a completed
        # review stage is also durable schema-accepted evidence after a crash
        # before coordinator application.
        if self.connection.execute("SELECT 1 FROM review_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone():
            return True
        return self.connection.execute("SELECT 1 FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='review'", (ticket_id, attempt_number)).fetchone() is not None

    def review_reconciliation_status(self, ticket_id: str, attempt_number: int | None = None) -> dict[str, Any]:
        candidate = self.review_candidate(ticket_id, attempt_number)
        if candidate is None:
            return {"classification": "no_review_attempt", "retry_eligible": False, "candidate": None, "latest_invocation_status": None, "review_invocation_count": 0, "latest_response_artifact": None}
        attempt = int(candidate["attempt_number"])
        invocations = self.review_invocations(ticket_id, attempt)
        latest = invocations[-1] if invocations else None
        valid_stage = self.connection.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=? AND stage='review'", (ticket_id, attempt)).fetchone()
        applied = self.connection.execute("SELECT * FROM review_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt)).fetchone()
        valid = valid_stage is not None or applied is not None
        newest = self.review_candidate(ticket_id)
        if newest is not None and int(newest["attempt_number"]) != attempt:
            classification, eligible = "ambiguous_review_history", False
        elif applied is not None:
            classification, eligible = "valid_review_applied", False
        elif valid_stage is not None:
            classification, eligible = "valid_review_stage_pending_application", False
        elif latest is None:
            classification, eligible = "no_review_attempt", True
        elif latest["status"] == "started":
            classification, eligible = "review_in_flight", False
        elif latest["status"] == "completed":
            classification, eligible = "ambiguous_review_history", False
        elif len(invocations) >= self._MAX_REVIEW_INVOCATIONS:
            classification, eligible = "review_retry_exhausted", False
        else:
            classification, eligible = "review_invocation_failed_no_verdict", True
        return {"classification": classification, "retry_eligible": eligible, "candidate": candidate, "latest_invocation_status": latest["status"] if latest else None, "latest_invocation_id": latest["invocation_id"] if latest else None, "latest_response_artifact": latest["model_artifact"] if latest else None, "review_invocation_count": len(invocations), "valid_review_verdict": valid, "retry_budget": self._MAX_REVIEW_INVOCATIONS}

    def authorize_review_retry(self, ticket_id: str, *, operator_id: str, candidate_fingerprint: str) -> dict[str, Any]:
        with self._transaction() as conn:
            status = self.review_reconciliation_status(ticket_id)
            candidate = status["candidate"]
            if candidate is None or candidate["candidate_fingerprint"] != candidate_fingerprint:
                raise ValueError("validated candidate fingerprint mismatch")
            if self.get_ticket(ticket_id)["state"] != CanonicalState.LOCAL_REVIEW.value:
                raise PermissionError("review retry requires local_review")
            if status["classification"] == "review_in_flight":
                raise PermissionError("review invocation already in flight")
            if status["classification"] != "review_invocation_failed_no_verdict":
                raise PermissionError("review retry is not eligible")
            failed = str(status["latest_invocation_id"])
            existing = conn.execute("SELECT * FROM review_retry_authorizations WHERE ticket_id=? AND attempt_number=? AND failed_invocation_id=?", (ticket_id, candidate["attempt_number"], failed)).fetchone()
            if existing is not None:
                return dict(existing)
            authorization_id = uuid.uuid4().hex
            conn.execute("INSERT INTO review_retry_authorizations(authorization_id,ticket_id,attempt_number,candidate_fingerprint,failed_invocation_id,operator_id,authorized_at) VALUES (?,?,?,?,?,?,?)", (authorization_id, ticket_id, candidate["attempt_number"], candidate_fingerprint, failed, operator_id, self._now()))
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="review_retry_authorized", actor_id=operator_id, payload={"attempt_number":candidate["attempt_number"], "failed_invocation_id":failed})
            return dict(conn.execute("SELECT * FROM review_retry_authorizations WHERE authorization_id=?", (authorization_id,)).fetchone())

    def launch_authorized_review(self, authorization_id: str, *, invocation_id: str, provider: str, model: str, packet_hash: str, worktree_path: str, timeout_seconds: int) -> dict[str, Any]:
        """Atomically consume one authorization and create its sole invocation."""
        with self._transaction() as conn:
            auth = conn.execute("SELECT * FROM review_retry_authorizations WHERE authorization_id=?", (authorization_id,)).fetchone()
            if auth is None: raise KeyError("review retry authorization")
            if auth["consumed_invocation_id"] is not None:
                return dict(conn.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (auth["consumed_invocation_id"],)).fetchone())
            status = self.review_reconciliation_status(str(auth["ticket_id"]), int(auth["attempt_number"]))
            if status["classification"] != "review_invocation_failed_no_verdict": raise PermissionError("review retry is no longer eligible")
            if int(status["review_invocation_count"]) >= self._MAX_REVIEW_INVOCATIONS: raise PermissionError("review retry budget exhausted")
            now = self._now()
            conn.execute("INSERT INTO model_invocations(invocation_id,ticket_id,attempt_number,stage,provider,model,packet_hash,worktree_path,timeout_seconds,started_at,status) VALUES (?,?,?,?,?,?,?,?,?,?, 'started')", (invocation_id,auth["ticket_id"],auth["attempt_number"],"review",provider,model,packet_hash,worktree_path,timeout_seconds,now))
            changed = conn.execute("UPDATE review_retry_authorizations SET consumed_invocation_id=?,consumed_at=? WHERE authorization_id=? AND consumed_invocation_id IS NULL", (invocation_id,now,authorization_id))
            if changed.rowcount != 1: raise RuntimeError("review authorization consumption lost")
            self._append_event(conn, entity_type="ticket", entity_id=str(auth["ticket_id"]), event_type="model_invocation_started", actor_id="controller", payload={"invocation_id":invocation_id,"attempt_number":auth["attempt_number"],"stage":"review","retry_authorization_id":authorization_id})
            return self.model_invocation(invocation_id)

    def recover_stale_review_invocation(self, invocation_id: str, *, now: int, stale_after_seconds: int) -> bool:
        if stale_after_seconds < 1: raise ValueError("invalid stale threshold")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
            if row is None or row["stage"] != "review" or row["status"] != "started" or int(row["started_at"]) + stale_after_seconds > now: return False
            conn.execute("UPDATE model_invocations SET status='process_error',completed_at=?,duration_seconds=?,error_json=? WHERE invocation_id=? AND status='started'", (now,float(now-int(row["started_at"])),json.dumps({"type":"ReviewInvocationAbandoned","message":"review invocation exceeded stale threshold"},sort_keys=True),invocation_id))
            self._append_event(conn,entity_type="ticket",entity_id=str(row["ticket_id"]),event_type="model_invocation_finished",actor_id="recovery",payload={"invocation_id":invocation_id,"attempt_number":row["attempt_number"],"stage":"review","status":"process_error","reason":"abandoned"})
            return True

    def _historical_review_bridge_valid(self, conn: sqlite3.Connection, ticket_id: str, attempt_number: int, candidate: sqlite3.Row) -> bool:
        """Require the complete immutable R2 chain before bridging needs_triage."""
        auth = conn.execute("SELECT * FROM historical_revalidation_authorizations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        attestation = conn.execute("SELECT * FROM historical_revalidation_attestations WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        claim = conn.execute("SELECT * FROM historical_revalidation_validation_claims WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        result = conn.execute("SELECT * FROM historical_revalidation_validation_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
        if any(row is None for row in (auth, attestation, claim, result)):
            return False
        try:
            if authorization_hash_from_row(auth) != auth["authorization_hash"] or attestation_hash_from_row(attestation) != attestation["attestation_hash"]:
                return False
            claim_identity = historical_validation_identity(ticket_id=claim["ticket_id"], attempt_number=claim["attempt_number"], authorization_hash=claim["authorization_hash"], attestation_hash=claim["attestation_hash"], base_sha=claim["base_sha"], implementation_diff_hash=claim["implementation_diff_hash"], validation_profile_hash=claim["validation_profile_hash"])
            if claim["authorization_hash"] != auth["authorization_hash"] or claim["attestation_hash"] != attestation["attestation_hash"] or claim["attempt_number"] != attempt_number:
                return False
            if claim["base_sha"] != attestation["base_sha"] or claim["implementation_diff_hash"] != attestation["implementation_diff_hash"]:
                return False
            if historical_validation_result_hash(ticket_id=result["ticket_id"], attempt_number=result["attempt_number"], authorization_hash=result["authorization_hash"], attestation_hash=result["attestation_hash"], base_sha=result["base_sha"], implementation_diff_hash=result["implementation_diff_hash"], validation_profile_hash=result["validation_profile_hash"], artifact_sha256=result["artifact_sha256"], passed=bool(result["passed"]), compact_evidence=result["compact_evidence"]) != result["result_hash"]:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        provenance = json.loads(str(candidate["historical_provenance_json"]))
        return (
            candidate["status"] == "review_pending" and int(result["passed"]) == 1 and
            auth["authorization_hash"] == provenance.get("authorization_hash") and
            attestation["attestation_hash"] == provenance.get("attestation_hash") and
            claim["claim_id"] == provenance.get("validation_claim_id") and
            result["result_id"] == provenance.get("validation_result_id") and
            result["result_hash"] == provenance.get("validation_result_hash") and
            str(candidate["candidate_fingerprint"]) == str(result["implementation_diff_hash"]) and
            str(auth["implementation_invocation_id"]) == str(candidate["implementation_invocation_id"])
        )

    def apply_persisted_review(self, ticket_id: str, attempt_number: int) -> dict[str, Any]:
        """Persist a normalized review result without applying its verdict."""
        status = self.review_reconciliation_status(ticket_id, attempt_number)
        if status["classification"] == "valid_review_applied":
            row = self.connection.execute("SELECT * FROM review_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            return {"verdict": row["verdict"], "status": "already_applied"}
        if status["classification"] != "valid_review_stage_pending_application":
            raise PermissionError("persisted review application is not eligible")
        stage = self.model_stage(ticket_id, attempt_number, "review")
        candidate = self.review_candidate(ticket_id, attempt_number)
        if stage is None or candidate is None or stage["diff_hash"] != candidate["candidate_fingerprint"]:
            raise ValueError("persisted review stage conflicts with frozen candidate")
        from .controller import ticket_from_ledger
        from .review import normalize_review
        artifact = Path(stage["response_artifact"])
        if not artifact.is_file():
            raise ValueError("persisted review artifact missing")
        review = normalize_review(json.loads(artifact.read_text(encoding="utf-8")).get("payload", {}), ticket_from_ledger(self.get_ticket(ticket_id)))
        with self._transaction() as conn:
            current = conn.execute("SELECT state FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if current is None:
                raise KeyError(ticket_id)
            if current["state"] == CanonicalState.NEEDS_TRIAGE.value:
                if not self._historical_review_bridge_valid(conn, ticket_id, attempt_number, conn.execute("SELECT * FROM review_candidates WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()):
                    raise PermissionError("needs_triage review application requires complete historical revalidation authority")
                now = self._now()
                changed = conn.execute("UPDATE tickets SET state=?, updated_at=? WHERE id=? AND state=?", (CanonicalState.LOCAL_REVIEW.value, now, ticket_id, CanonicalState.NEEDS_TRIAGE.value))
                if changed.rowcount != 1:
                    raise RuntimeError("ticket changed concurrently")
                event_id = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="state_transition", actor_id="local-reviewer", from_state=CanonicalState.NEEDS_TRIAGE.value, to_state=CanonicalState.LOCAL_REVIEW.value, payload={"attempt_number": attempt_number, "application_only": True})
                self._enqueue_projection_bundle_in_transaction(conn, ticket_id=ticket_id, event_id=event_id, evidence="historical review application ready", state_payload={"attempt_number": attempt_number})
            elif current["state"] != CanonicalState.LOCAL_REVIEW.value:
                raise PermissionError("persisted review application requires local_review or governed historical needs_triage")
            existing = conn.execute("SELECT * FROM review_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            if existing is not None:
                if existing["verdict"] != review.verdict or existing["payload_json"] != json.dumps(review.raw, sort_keys=True):
                    raise RuntimeError("conflicting persisted review result")
                return {"verdict": existing["verdict"], "status": "already_applied"}
            conn.execute("INSERT INTO review_results(ticket_id, attempt_number, verdict, payload_json, created_at) VALUES (?, ?, ?, ?, ?)", (ticket_id, attempt_number, review.verdict, json.dumps(review.raw, sort_keys=True), self._now()))
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="review_recorded", actor_id="local_reviewer", payload={"attempt_number": attempt_number, "verdict": review.verdict, "application_only": True})
        return {"verdict": review.verdict, "status": "applied_only"}

    def freeze_review_candidate(self, ticket_id: str, attempt_number: int, *, candidate_fingerprint: str, validation_evidence: str, implementation_invocation_id: str | None, runtime_identity: dict[str, Any], historical_review_attempted: bool = False, historical_provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        if not candidate_fingerprint or not validation_evidence:
            raise ValueError("invalid_review_candidate")
        now = self._now(); identity = json.dumps(runtime_identity, sort_keys=True, separators=(",", ":")); provenance = json.dumps(historical_provenance or {}, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM review_candidates WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            if row is not None:
                if row["candidate_fingerprint"] != candidate_fingerprint or row["validation_evidence"] != validation_evidence or row["historical_provenance_json"] != provenance:
                    raise RuntimeError("review_candidate_conflicts_with_validated_evidence")
                return dict(row)
            conn.execute("INSERT INTO review_candidates(ticket_id,attempt_number,candidate_fingerprint,validation_evidence,implementation_invocation_id,runtime_identity_json,status,historical_review_attempted,historical_provenance_json,created_at,updated_at) VALUES (?,?,?,?,?,?, 'review_pending',?,?,?,?)", (ticket_id,attempt_number,candidate_fingerprint,validation_evidence,implementation_invocation_id,identity,int(historical_review_attempted),provenance,now,now))
            self._append_event(conn,entity_type="ticket",entity_id=ticket_id,event_type="validated_review_candidate_frozen",actor_id="controller",payload={"attempt_number":attempt_number,"candidate_fingerprint":candidate_fingerprint,"historical_review_attempted":historical_review_attempted})
        return self.review_candidate(ticket_id, attempt_number) or {}

    def review_candidate(self, ticket_id: str, attempt_number: int | None = None) -> dict[str, Any] | None:
        q="SELECT * FROM review_candidates WHERE ticket_id=?"; values: tuple[Any,...]=(ticket_id,)
        if attempt_number is not None: q += " AND attempt_number=?"; values=(ticket_id,attempt_number)
        row=self.connection.execute(q+" ORDER BY attempt_number DESC LIMIT 1",values).fetchone()
        return dict(row) if row else None

    def record_review_infrastructure_failure(self, ticket_id: str, attempt_number: int, *, outcome: str) -> None:
        if outcome not in {"review_timeout","review_process_error","review_malformed_output"}: raise ValueError("invalid_review_infrastructure_outcome")
        with self._transaction() as conn:
            changed=conn.execute("UPDATE review_candidates SET status='review_infrastructure_failed',last_outcome=?,updated_at=? WHERE ticket_id=? AND attempt_number=?",(outcome,self._now(),ticket_id,attempt_number))
            if not changed.rowcount: raise ValueError("review candidate missing")

    def authorize_review_resume(self, ticket_id: str, *, operator_id: str, candidate_fingerprint: str, runtime_identity: dict[str, Any]) -> dict[str, Any]:
        with self._transaction() as conn:
            paused=conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone(); row=conn.execute("SELECT * FROM review_candidates WHERE ticket_id=? ORDER BY attempt_number DESC LIMIT 1",(ticket_id,)).fetchone(); ticket=conn.execute("SELECT state FROM tickets WHERE id=?",(ticket_id,)).fetchone()
            if paused is None or not paused["paused"]: raise PermissionError("review reconciliation requires Local First paused")
            if row is None or ticket is None or ticket["state"] != CanonicalState.NEEDS_TRIAGE.value or row["status"] != "review_infrastructure_failed": raise ValueError("ticket is not awaiting review-infrastructure reconciliation")
            if row["candidate_fingerprint"] != candidate_fingerprint: raise ValueError("validated candidate fingerprint mismatch")
            if row["runtime_identity_json"] != json.dumps(runtime_identity,sort_keys=True,separators=(",", ":")): raise ValueError("runtime provenance mismatch")
            if conn.execute("SELECT 1 FROM accepted_evidence WHERE ticket_id=?",(ticket_id,)).fetchone(): raise ValueError("accepted evidence already exists")
            if conn.execute("SELECT 1 FROM review_results WHERE ticket_id=? AND attempt_number=?",(ticket_id,row["attempt_number"])).fetchone(): raise ValueError("valid review verdict already exists")
            if conn.execute("SELECT 1 FROM model_invocations WHERE ticket_id=? AND status='started'",(ticket_id,)).fetchone(): raise ValueError("incomplete model invocation requires explicit resolution")
            now=self._now()
            conn.execute("UPDATE review_candidates SET status='review_pending',updated_at=? WHERE ticket_id=? AND attempt_number=?", (now, ticket_id, row["attempt_number"]))
            conn.execute("UPDATE tickets SET state=?,updated_at=? WHERE id=?", (CanonicalState.LOCAL_REVIEW.value, now, ticket_id))
            event_id = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="review_reconciliation_authorized", actor_id=operator_id, from_state=CanonicalState.NEEDS_TRIAGE.value, to_state=CanonicalState.LOCAL_REVIEW.value, payload={"attempt_number":row["attempt_number"], "candidate_fingerprint":candidate_fingerprint})
            self._enqueue_projection_bundle_in_transaction(conn, ticket_id=ticket_id, event_id=event_id, evidence="review reconciliation authorized")
        return self.review_candidate(ticket_id, int(row["attempt_number"])) or {}

    def stage_rows(self, ticket_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? ORDER BY attempt_number, completed_at", (ticket_id,))]

    def enqueue_evidence_comment(self, ticket_id: str, event_id: int, evidence: str) -> dict[str, Any]:
        """Legacy compatibility entry point; projectable events use the atomic bundle."""
        event = self.connection.execute("SELECT event_type, entity_type, entity_id, to_state FROM events WHERE id=?", (event_id,)).fetchone()
        if event is not None:
            if event["entity_type"] != "ticket" or event["entity_id"] != ticket_id or event["event_type"] != "state_transition" or event["to_state"] not in self._PROJECTABLE_STATES:
                raise ValueError("event is not projectable; comment-only intent is forbidden")
            return self.enqueue_projection_bundle(ticket_id, event_id, evidence)
        import re
        row=self.get_ticket(ticket_id); now=self._now(); key=f"evidence-comment:{ticket_id}:{event_id}"
        operation_id=hashlib.sha256(key.encode()).hexdigest()[:32]
        safe=re.sub(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+",r"\1=[REDACTED]",evidence)[:800]
        payload=(f"Local-first ticket {ticket_id} | state={row['state']} | {safe}\n<!-- local-first-comment:{operation_id} -->")[:1000]
        with self._transaction() as conn:
            target = self._resolve_external_task_id_in_transaction(conn, ticket_id)
            conn.execute("INSERT OR IGNORE INTO evidence_comment_outbox(operation_id,ticket_id,event_id,external_task_id,operation_kind,idempotency_key,payload,status,created_at,updated_at) VALUES (?,?,?,?,? ,?,?, 'pending',?,?)",(operation_id,ticket_id,event_id,target,'evidence_comment',key,payload,now,now))
        return self.comment_outbox(operation_id)

    def comment_outbox(self, operation_id: str) -> dict[str, Any]:
        row=self.connection.execute("SELECT * FROM evidence_comment_outbox WHERE operation_id=?",(operation_id,)).fetchone()
        if row is None: raise KeyError(operation_id)
        return dict(row)

    def resolve_claimed_comment_target(self, operation_id: str, owner: str) -> str:
        """Retarget replayable legacy comment rows from authoritative projection provenance."""
        with self._transaction() as conn:
            row = conn.execute("SELECT ticket_id,status,lease_owner FROM evidence_comment_outbox WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None: raise KeyError(operation_id)
            if row["status"] != "delivering" or row["lease_owner"] != owner: raise PermissionError("comment delivery is not owned")
            target = self._resolve_external_task_id_in_transaction(conn, str(row["ticket_id"]))
            conn.execute("UPDATE evidence_comment_outbox SET external_task_id=?,updated_at=? WHERE operation_id=?", (target,self._now(),operation_id))
            return target

    def claim_comment(self, operation_id: str, owner: str, *, lease_seconds: int = 60, now: int | None = None) -> bool:
        now=self._now() if now is None else now
        with self._transaction() as conn:
            changed=conn.execute("UPDATE evidence_comment_outbox SET status='delivering',lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE operation_id=? AND status IN ('pending','retryable') AND (next_attempt_at IS NULL OR next_attempt_at<=?)",(owner,now+lease_seconds,now,operation_id,now))
            return changed.rowcount==1

    @staticmethod
    def _safe_comment_error(error: str, *, limit: int = 500) -> str:
        import re
        safe = re.sub(
            r"(?i)(password|token|secret|api[_-]?key|authorization|cookie)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            str(error),
        )
        safe = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", safe)
        return safe[:limit]

    def claim_next_comment(self, owner: str, *, lease_seconds: int = 60, now: int | None = None) -> dict[str, Any] | None:
        """Atomically claim the oldest pending or due retryable comment."""
        now = self._now() if now is None else now
        with self._transaction() as conn:
            changed = conn.execute(
                """UPDATE evidence_comment_outbox
                   SET status='delivering', lease_owner=?, lease_expires_at=?,
                       attempt_count=attempt_count+1, updated_at=?
                 WHERE operation_id = (
                       SELECT operation_id FROM evidence_comment_outbox
                        WHERE status IN ('pending','retryable')
                          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                        ORDER BY created_at, operation_id LIMIT 1)
                   AND status IN ('pending','retryable')
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)""",
                (owner, now + lease_seconds, now, now, now),
            )
            if changed.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM evidence_comment_outbox WHERE operation_id = (SELECT operation_id FROM evidence_comment_outbox WHERE lease_owner=? AND status='delivering' AND updated_at=? ORDER BY operation_id LIMIT 1)",
                (owner, now),
            ).fetchone()
            return dict(row) if row is not None else None

    def mark_comment_delivered(self, operation_id: str, owner: str, *, now: int | None = None) -> bool:
        now=self._now() if now is None else now
        with self._transaction() as conn:
            row=conn.execute("SELECT status,lease_owner,lease_expires_at,terminal_owner FROM evidence_comment_outbox WHERE operation_id=?",(operation_id,)).fetchone()
            if row is None: return False
            if row['status']=='delivered': return row['terminal_owner']==owner
            if row['status']!='delivering' or row['lease_owner']!=owner or row['lease_expires_at'] is None or row['lease_expires_at']<=now: return False
            return conn.execute("UPDATE evidence_comment_outbox SET status='delivered',delivered_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=NULL,last_error=NULL,terminal_owner=? WHERE operation_id=? AND status='delivering' AND lease_owner=? AND lease_expires_at>?",(now,now,owner,operation_id,owner,now)).rowcount==1

    def mark_comment_retryable(self, operation_id: str, owner: str, error: str, *, next_attempt_at: int | None = None, now: int | None = None, error_limit: int = 500) -> bool:
        now=self._now() if now is None else now
        next_attempt_at = now if next_attempt_at is None else next_attempt_at
        with self._transaction() as conn:
            return conn.execute("UPDATE evidence_comment_outbox SET status='retryable',last_error=?,lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,updated_at=?,delivered_at=NULL,terminal_owner=NULL WHERE operation_id=? AND status='delivering' AND lease_owner=? AND lease_expires_at>?",(self._safe_comment_error(error, limit=error_limit),next_attempt_at,now,operation_id,owner,now)).rowcount==1

    def mark_comment_permanently_failed(self, operation_id: str, owner: str, error: str, *, now: int | None = None, error_limit: int = 500) -> bool:
        now=self._now() if now is None else now
        with self._transaction() as conn:
            row=conn.execute("SELECT status,lease_owner,lease_expires_at,terminal_owner FROM evidence_comment_outbox WHERE operation_id=?",(operation_id,)).fetchone()
            if row is None: return False
            if row['status']=='permanently_failed': return row['terminal_owner']==owner
            return conn.execute("UPDATE evidence_comment_outbox SET status='permanently_failed',last_error=?,lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=NULL,updated_at=?,terminal_owner=? WHERE operation_id=? AND status='delivering' AND lease_owner=? AND lease_expires_at>?",(self._safe_comment_error(error, limit=error_limit),now,owner,operation_id,owner,now)).rowcount==1

    def recover_expired_comment_leases(self, *, now: int | None = None, reason: str | None = None) -> list[str]:
        now=self._now() if now is None else now
        with self._transaction() as conn:
            rows=conn.execute("SELECT operation_id FROM evidence_comment_outbox WHERE status='delivering' AND lease_expires_at<=?",(now,)).fetchall()
            if reason is None:
                conn.execute("UPDATE evidence_comment_outbox SET status='retryable',lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,updated_at=? WHERE status='delivering' AND lease_expires_at<=?",(now,now,now))
            else:
                conn.execute("UPDATE evidence_comment_outbox SET status='retryable',lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,last_error=?,updated_at=? WHERE status='delivering' AND lease_expires_at<=?",(now,self._safe_comment_error(reason),now,now))
            return [str(row['operation_id']) for row in rows]

    def set_evidence_comment(self, ticket_id: str, comment: str, artifact_location: str | None = None) -> bool:
        with self._transaction() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO evidence_comments(ticket_id,comment,artifact_location,created_at) VALUES (?,?,?,?)", (ticket_id, comment[:4000], artifact_location, self._now()))
            return cur.rowcount == 1

    def evidence_comment(self, ticket_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM evidence_comments WHERE ticket_id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def accepted_commit(self, ticket_id: str) -> str | None:
        row = self.connection.execute("SELECT accepted_commit_sha FROM accepted_evidence WHERE ticket_id=?", (ticket_id,)).fetchone()
        return str(row["accepted_commit_sha"]) if row else None

    def claim_specific(self, ticket_id: str, owner: str, lease_seconds: int, now: int | None = None) -> bool:
        now = self._now() if now is None else now
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id = 1").fetchone()
            if paused is None or paused["paused"]:
                return False
            changed = conn.execute("UPDATE tickets SET state=?, lease_owner=?, lease_expires_at=?, updated_at=? WHERE id=? AND state=? AND (lease_expires_at IS NULL OR lease_expires_at<=?)", (CanonicalState.IMPLEMENTING.value, owner, now+lease_seconds, now, ticket_id, CanonicalState.READY_LOCAL.value, now))
            if changed.rowcount != 1: return False
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="lease_claimed", actor_id=owner, from_state=CanonicalState.READY_LOCAL.value, to_state=CanonicalState.IMPLEMENTING.value, payload={"lease_expires_at":now+lease_seconds})
            return True

    def claim_specific_operator(self, ticket_id: str, owner: str, lease_seconds: int, now: int | None = None) -> bool:
        """Claim exactly one named ticket for an explicit operator operation."""
        now = self._now() if now is None else now
        with self._transaction() as conn:
            changed = conn.execute("UPDATE tickets SET state=?, lease_owner=?, lease_expires_at=?, updated_at=? WHERE id=? AND state=? AND (lease_expires_at IS NULL OR lease_expires_at<=?)", (CanonicalState.IMPLEMENTING.value, owner, now+lease_seconds, now, ticket_id, CanonicalState.READY_LOCAL.value, now))
            if changed.rowcount != 1: return False
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="lease_claimed", actor_id=owner, from_state=CanonicalState.READY_LOCAL.value, to_state=CanonicalState.IMPLEMENTING.value, payload={"lease_expires_at":now+lease_seconds,"operator_authorized":True})
            return True

    def record_review(self, ticket_id: str, attempt_number: int, review: Any) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO review_results(ticket_id, attempt_number, verdict, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, attempt_number, review.verdict, json.dumps(review.raw, sort_keys=True), self._now()),
            )
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="review_recorded", actor_id="local_reviewer", payload={"attempt_number": attempt_number, "verdict": review.verdict})

    def record_review_finding(self, ticket_id: str, attempt_number: int, finding: Any) -> int:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO review_findings(ticket_id, attempt_number, fingerprint, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, attempt_number, finding.fingerprint, json.dumps(finding.__dict__, sort_keys=True), self._now()),
            )
            count = int(conn.execute("SELECT COUNT(*) FROM review_findings WHERE ticket_id = ? AND fingerprint = ?", (ticket_id, finding.fingerprint)).fetchone()[0])
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="review_finding_recorded", actor_id="local_reviewer", payload={"attempt_number": attempt_number, "fingerprint": finding.fingerprint, "occurrence": count})
            return count

    def set_criterion_status(self, ticket_id: str, criterion_id: str, status: str, *, evidence: str) -> None:
        if status not in {"accepted", "open"}:
            raise ValueError("unsupported criterion status")
        with self._transaction() as conn:
            previous = conn.execute("SELECT status FROM criterion_statuses WHERE ticket_id = ? AND criterion_id = ?", (ticket_id, criterion_id)).fetchone()
            conn.execute(
                "INSERT INTO criterion_statuses(ticket_id, criterion_id, status, evidence, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(ticket_id, criterion_id) DO UPDATE SET status=excluded.status, evidence=excluded.evidence, updated_at=excluded.updated_at",
                (ticket_id, criterion_id, status, evidence, self._now()),
            )
            if previous is None or previous["status"] != status:
                self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="criterion_closed" if status == "accepted" else "criterion_reopened", actor_id="controller", payload={"criterion_id": criterion_id, "evidence": evidence})

    def criterion_status(self, ticket_id: str, criterion_id: str) -> str | None:
        row = self.connection.execute("SELECT status FROM criterion_statuses WHERE ticket_id = ? AND criterion_id = ?", (ticket_id, criterion_id)).fetchone()
        return str(row["status"]) if row else None

    def unresolved_criteria(self, ticket_id: str) -> set[str]:
        ticket = self.get_ticket(ticket_id)
        criteria = set(json.loads(ticket["criterion_ids_json"]))
        rows = self.connection.execute(
            "SELECT criterion_id FROM criterion_statuses WHERE ticket_id = ? AND status = 'accepted'",
            (ticket_id,),
        ).fetchall()
        return criteria - {str(row["criterion_id"]) for row in rows}

    def parent_is_paused(self, parent_ticket_id: str) -> bool:
        terminal = (CanonicalState.ACCEPTED.value, CanonicalState.DONE.value, CanonicalState.REJECTED.value)
        row = self.connection.execute(
            "SELECT 1 FROM tickets WHERE parent_ticket_id = ? AND state NOT IN (?, ?, ?) LIMIT 1",
            (parent_ticket_id, *terminal),
        ).fetchone()
        return row is not None

    @staticmethod
    def _triage_child_fingerprint(ticket: MicroTicket) -> str:
        source = json.dumps(
            {"contract": ticket.contract(), "resolves_criteria": ticket.criterion_ids},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def create_triaged_children(self, parent_ticket_id: str, children: list[tuple[str, MicroTicket, str]], *, classification: str, root_cause_evidence: str, project_children: bool = False) -> list[str]:
        """Atomically create controller-owned children; no board/model I/O occurs here."""
        now = self._now()
        with self._transaction() as conn:
            parent = conn.execute("SELECT state, depth, feature_id, tranche_id FROM tickets WHERE id = ?", (parent_ticket_id,)).fetchone()
            if parent is None:
                raise KeyError(parent_ticket_id)
            if parent["state"] != CanonicalState.NEEDS_TRIAGE.value:
                raise ValueError("triage children require a needs_triage parent")
            if int(parent["depth"]) >= 2:
                raise ValueError("maximum decomposition depth reached")
            if len(children) > 3:
                raise ValueError("triage children exceed maximum")
            for _, child_ticket, fingerprint in children:
                if fingerprint != self._triage_child_fingerprint(child_ticket):
                    raise ValueError("triage child fingerprint does not match its contract")
            existing_rows = conn.execute(
                "SELECT child_ticket_id, fingerprint FROM triage_children WHERE parent_ticket_id = ? ORDER BY child_ticket_id",
                (parent_ticket_id,),
            ).fetchall()
            incoming_fingerprints = {fingerprint for _, _, fingerprint in children}
            existing_fingerprints = {str(row["fingerprint"]) for row in existing_rows}
            if existing_rows:
                if incoming_fingerprints != existing_fingerprints or len(children) != len(existing_rows):
                    raise ValueError("triage decomposition already exists with different children")
                by_fingerprint = {str(row["fingerprint"]): str(row["child_ticket_id"]) for row in existing_rows}
                if project_children:
                    parent_binding = conn.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (parent_ticket_id,)).fetchone()
                    if parent_binding is None:
                        raise ValueError("triage parent runtime binding is missing")
                    for child_id in by_fingerprint.values():
                        child_binding = conn.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (child_id,)).fetchone()
                        if child_binding is None or any(
                            child_binding[name] != parent_binding[name]
                            for name in ("repository_path", "starting_sha", "canonical_sha", "ownership_verified")
                        ):
                            raise ValueError("triage decomposition exists without matching scheduler runtime provenance")
                        event = conn.execute(
                            "SELECT id FROM events WHERE entity_type='ticket' AND entity_id=? AND event_type='triage_child_created' ORDER BY id DESC LIMIT 1",
                            (child_id,),
                        ).fetchone()
                        if event is None or conn.execute(
                            "SELECT 1 FROM board_projection_outbox WHERE ticket_id=? AND event_id=? AND operation='create_microticket'",
                            (child_id, event["id"]),
                        ).fetchone() is None:
                            raise ValueError("triage decomposition exists without scheduler projection provenance")
                return [by_fingerprint[fingerprint] for _, _, fingerprint in children]
            parent_criteria = set(json.loads(self.get_ticket(parent_ticket_id)["criterion_ids_json"]))
            accepted_rows = conn.execute(
                "SELECT criterion_id FROM criterion_statuses WHERE ticket_id = ? AND status = 'accepted'",
                (parent_ticket_id,),
            ).fetchall()
            unresolved = parent_criteria - {str(row["criterion_id"]) for row in accepted_rows}
            created: list[str] = []
            from .triage import triage_child_payload
            for title, child_ticket, fingerprint in children:
                try:
                    validate_ticket(child_ticket)
                except ReadinessError as exc:
                    raise ValueError(f"triage child is not ready: {exc}") from exc
                if not child_ticket.criterion_ids or not set(child_ticket.criterion_ids) <= unresolved:
                    raise ValueError("triage child must map only unresolved parent criteria")
                contract = child_ticket.contract()
                child_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO tickets(id, feature_id, tranche_id, parent_ticket_id, depth, title, objective, criterion_ids_json, primary_symbol, allowed_files_json, new_test_files_json, forbidden_changes_json, patch_budget_json, verification_json, risk, review_required, max_attempts, dependencies_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (child_id, parent["feature_id"], parent["tranche_id"], parent_ticket_id, int(parent["depth"]) + 1, title, contract.get("objective"), json.dumps(contract.get("criterion_ids", [])), contract.get("primary_symbol"), json.dumps(contract.get("allowed_files", [])), json.dumps(contract.get("new_test_files", [])), json.dumps(contract.get("forbidden_changes", [])), json.dumps(contract.get("patch_budget", {})), json.dumps(contract.get("verification", {})), contract.get("risk"), int(contract.get("review_required", True)), contract.get("max_attempts", 2), json.dumps(contract.get("dependencies", [])), CanonicalState.READY_LOCAL.value, now, now),
                )
                binding = conn.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (parent_ticket_id,)).fetchone()
                if project_children:
                    if binding is None:
                        raise ValueError("triage parent runtime binding is missing")
                    conn.execute(
                        "INSERT INTO runtime_bindings(ticket_id,repository_path,starting_sha,canonical_sha,ownership_verified,created_at) VALUES (?,?,?,?,?,?)",
                        (child_id, binding["repository_path"], binding["starting_sha"], binding["canonical_sha"], binding["ownership_verified"], now),
                    )
                for criterion_id in child_ticket.criterion_ids:
                    conn.execute("INSERT INTO ticket_criteria(ticket_id,criterion_id) VALUES (?,?)", (child_id, criterion_id))
                conn.execute(
                    "INSERT INTO triage_children(parent_ticket_id, child_ticket_id, fingerprint, created_at) VALUES (?, ?, ?, ?)",
                    (parent_ticket_id, child_id, fingerprint, now),
                )
                event_id = self._append_event(conn, entity_type="ticket", entity_id=child_id, event_type="triage_child_created", actor_id="controller", to_state=CanonicalState.READY_LOCAL.value, payload={"parent_ticket_id": parent_ticket_id, "fingerprint": fingerprint, "resolves_criteria": list(child_ticket.criterion_ids)})
                if project_children:
                    payload = triage_child_payload(parent_ticket_id, child_id, title, child_ticket)
                    self._enqueue_generated_create_projection_in_transaction(
                        conn,
                        ticket_id=child_id,
                        event_id=event_id,
                        payload=payload,
                        idempotency_key=payload["projection_key"],
                    )
                created.append(child_id)
            self._append_event(conn, entity_type="ticket", entity_id=parent_ticket_id, event_type="triage_children_created", actor_id="controller", payload={"classification": classification, "root_cause_evidence": root_cause_evidence, "child_ids": created})
            return created

    def triage_projection_identity(self, ticket_id: str, event_id: int) -> dict[str, str]:
        row = self.connection.execute(
            "SELECT t.id AS ticket_id,t.parent_ticket_id,e.entity_type,e.entity_id,e.event_type "
            "FROM tickets t JOIN events e ON e.id=? WHERE t.id=?",
            (event_id, ticket_id),
        ).fetchone()
        if row is None or (row["entity_type"], row["entity_id"], row["event_type"]) != ("ticket", ticket_id, "triage_child_created"):
            raise KeyError("authoritative triage projection identity missing")
        if not isinstance(row["parent_ticket_id"], str) or not row["parent_ticket_id"]:
            raise ValueError("authoritative triage projection parent identity missing")
        return {"kind": "triage_microticket", "ticket_id": str(row["ticket_id"]), "parent_ticket_id": str(row["parent_ticket_id"])}

    def pause(self, actor_id: str, *, reason: str) -> None:
        self._set_pause(True, actor_id, reason)

    def resume(self, actor_id: str, *, reason: str) -> None:
        self._set_pause(False, actor_id, reason)

    def _set_pause(self, paused: bool, actor_id: str, reason: str) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE controller_state SET paused = ?, updated_at = ? WHERE id = 1", (int(paused), self._now()))
            self._append_event(conn, entity_type="controller", entity_id="controller", event_type="paused" if paused else "resumed", actor_id=actor_id, payload={"reason": reason})

    def generated_projection_identity(self, ticket_id: str, event_id: int) -> dict[str, Any]:
        """Return authoritative identity for one generated-ticket create event."""
        row = self.connection.execute("""
            SELECT t.id AS ticket_id, t.feature_id, t.tranche_id,
                   tr.feature_id AS tranche_feature_id, f.id AS resolved_feature_id,
                   e.entity_type, e.entity_id, e.event_type
            FROM tickets t JOIN tranches tr ON tr.id=t.tranche_id
            JOIN features f ON f.id=t.feature_id JOIN events e ON e.id=?
            WHERE t.id=?
        """, (event_id, ticket_id)).fetchone()
        if row is None or row["entity_type"] != "ticket" or row["entity_id"] != ticket_id or row["event_type"] not in {"generated_microticket_created", "generated_microticket_projection_recovered"}:
            raise KeyError("authoritative generated projection identity missing")
        recovery = None
        if row["event_type"] == "generated_microticket_projection_recovered":
            recovery = self.connection.execute(
                "SELECT replacement_idempotency_key FROM generated_projection_recoveries WHERE ticket_id=? AND replacement_event_id=?",
                (ticket_id, event_id),
            ).fetchone()
            if recovery is None:
                raise KeyError("authoritative generated projection recovery identity missing")
        if row["feature_id"] != row["tranche_feature_id"] or row["feature_id"] != row["resolved_feature_id"]:
            raise ValueError("authoritative generated projection identity conflicts")
        successor = self.connection.execute(
            "SELECT repository_identity,repo_base_sha,repo_snapshot_hash FROM next_tranche_materializations WHERE successor_tranche_id=?",
            (row["tranche_id"],),
        ).fetchall()
        normal = successor if successor else self.connection.execute(
            "SELECT id,repository_identity,repo_base_sha,repo_snapshot_hash FROM decomposition_plans "
            "WHERE feature_id=? AND status='active' ORDER BY id", (row["feature_id"],)
        ).fetchall()
        correction = self.connection.execute("""
            SELECT sct.correction_plan_id, sc.feature_id, sc.tranche_id,
                   sc.repository_identity, sc.base_sha AS repo_base_sha,
                   sc.snapshot_hash AS repo_snapshot_hash
            FROM supplemental_correction_tickets sct
            JOIN supplemental_correction_plans sc ON sc.correction_plan_id=sct.correction_plan_id
            WHERE sct.ticket_id=?
        """, (ticket_id,)).fetchall()
        if len(correction) > 1:
            raise ValueError("authoritative generated projection correction provenance conflicts")
        if correction:
            source = correction[0]
            if (source["feature_id"], source["tranche_id"]) != (row["feature_id"], row["tranche_id"]):
                raise ValueError("authoritative generated projection correction identity conflicts")
            if len(normal) > 1:
                raise ValueError("authoritative generated projection provenance conflicts")
            if normal and tuple(source[x] for x in ("repository_identity", "repo_base_sha", "repo_snapshot_hash")) != tuple(normal[0][x] for x in ("repository_identity", "repo_base_sha", "repo_snapshot_hash")):
                raise ValueError("authoritative generated projection provenance conflicts")
            provenance = tuple(source[x] for x in ("repository_identity", "repo_base_sha", "repo_snapshot_hash"))
        else:
            if len(normal) != 1:
                raise ValueError("authoritative generated projection normal provenance missing or ambiguous")
            provenance = tuple(normal[0][x] for x in ("repository_identity", "repo_base_sha", "repo_snapshot_hash"))
        if not all(isinstance(value, str) and value for value in provenance):
            raise ValueError("authoritative generated projection provenance missing")
        result = {"ticket_id": str(row["ticket_id"]), "feature_id": str(row["feature_id"]), "tranche_id": str(row["tranche_id"]),
                  "repository_identity": str(provenance[0]), "repo_base_sha": str(provenance[1]), "repo_snapshot_hash": str(provenance[2])}
        if recovery is not None:
            result["projection_key"] = str(recovery["replacement_idempotency_key"])
            result["projection_generation"] = "recovery-v2"
        return result

    def reconcile_generated_projection(self, ticket_id: str, event_id: int) -> dict[str, Any]:
        """Refresh one never-attempted generated projection from persisted ledger truth.

        This is deliberately narrower than delivery: it cannot claim, create, show,
        acknowledge, or alter an attempted/leased projection.
        """
        from .decomposition import generated_card_payload, generated_projection_key
        from .generated_projection import DeterministicProjectionError, _canonical_payload
        from .ticket import MicroTicket, PatchBudget, VerificationProfile

        with self._transaction() as conn:
            row = conn.execute("""
                SELECT b.*, t.id AS resolved_ticket_id, t.state AS ticket_state, t.feature_id, t.tranche_id,
                       t.objective, t.criterion_ids_json, t.primary_symbol, t.allowed_files_json, t.create_files_json, t.new_test_files_json AS ticket_new_test_files_json,
                       t.forbidden_changes_json, t.patch_budget_json, t.verification_json, t.risk, t.review_required,
                       t.max_attempts, t.dependencies_json, e.entity_type, e.entity_id, e.event_type
                FROM board_projection_outbox b
                JOIN tickets t ON t.id=b.ticket_id
                JOIN events e ON e.id=b.event_id
                WHERE b.ticket_id=? AND b.event_id=?
            """, (ticket_id, event_id)).fetchone()
            if row is None:
                raise ValueError("generated projection reconciliation target missing")
            if (row["operation"], row["entity_type"], row["entity_id"], row["event_type"]) != ("create_microticket", "ticket", ticket_id, "generated_microticket_created"):
                raise ValueError("generated projection reconciliation provenance is ineligible")
            if row["acknowledged_at"] is not None or row["external_task_id"] is not None:
                raise ValueError("generated projection reconciliation requires no external effect")
            if row["lease_owner"] is not None or row["lease_expires_at"] is not None:
                raise ValueError("generated projection reconciliation requires an unleased row")
            if row["terminal_error"] is not None or row["next_attempt_at"] is not None or int(row["attempt_count"] or 0) != 0:
                raise ValueError("generated projection reconciliation requires a never-attempted row")
            if row["ticket_state"] != "draft":
                raise ValueError("generated projection reconciliation requires a draft ticket")
            identity = self.generated_projection_identity(ticket_id, event_id)
            expected_key = generated_projection_key(ticket_id)
            if row["idempotency_key"] != expected_key:
                raise ValueError("generated projection reconciliation idempotency mismatch")
            provenance = (identity["repository_identity"], identity["repo_base_sha"], identity["repo_snapshot_hash"])
            verification = json.loads(row["verification_json"])
            ticket = MicroTicket(ticket_id, row["objective"], tuple(json.loads(row["criterion_ids_json"])), row["primary_symbol"], tuple(json.loads(row["allowed_files_json"])), tuple(json.loads(row["forbidden_changes_json"])), PatchBudget(**json.loads(row["patch_budget_json"])), VerificationProfile(tuple(tuple(command) for command in verification["commands"]), verification.get("working_directory", "."), int(verification.get("timeout_seconds", 60)), int(verification.get("output_limit", 20000))), row["risk"], bool(row["review_required"]), int(row["max_attempts"]), tuple(json.loads(row["dependencies_json"])), tuple(json.loads(row["ticket_new_test_files_json"] or "[]")), tuple(json.loads(row["create_files_json"] or "[]")))
            feature = type("PersistedFeature", (), {"id": str(row["feature_id"])})()
            tranche = type("PersistedTranche", (), {"id": str(row["tranche_id"])})()
            payload = generated_card_payload(feature, tranche, ticket, repository_identity=str(provenance[0]), repo_base_sha=str(provenance[1]), repo_snapshot_hash=str(provenance[2]))
            candidate = dict(row)
            candidate["payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            identity = {"ticket_id": ticket_id, "feature_id": str(row["feature_id"]), "tranche_id": str(row["tranche_id"]), "repository_identity": str(provenance[0]), "repo_base_sha": str(provenance[1]), "repo_snapshot_hash": str(provenance[2])}
            try:
                _canonical_payload(candidate, identity)
            except DeterministicProjectionError as exc:
                raise ValueError("generated projection reconciliation qualification failed") from exc
            changed = conn.execute("UPDATE board_projection_outbox SET payload_json=? WHERE ticket_id=? AND event_id=? AND operation='create_microticket' AND acknowledged_at IS NULL AND external_task_id IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL AND terminal_error IS NULL AND next_attempt_at IS NULL AND attempt_count=0", (candidate["payload_json"], ticket_id, event_id)).rowcount
            if changed != 1:
                raise ValueError("generated projection reconciliation lost eligibility")
            return payload

    def recover_generated_projection(self, *, ticket_id: str, superseded_event_id: int,
                                     superseded_external_task_id: str, observed_status: str,
                                     observed_snapshot_hash: str, operator_id: str, reason: str) -> dict[str, Any]:
        """Supersede one bypassed generated-card identity without changing ticket authority."""
        from .decomposition import generated_card_payload
        from .ticket import MicroTicket, PatchBudget, VerificationProfile

        if observed_status not in {"done", "blocked"}:
            raise ValueError("recoverable Hermes status required")
        if not operator_id.strip() or not reason.strip():
            raise ValueError("operator identity and reason required")
        if len(observed_snapshot_hash) != 64 or any(ch not in "0123456789abcdef" for ch in observed_snapshot_hash):
            raise ValueError("canonical snapshot hash required")
        with self._transaction() as conn:
            paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
            if paused is None or not bool(paused["paused"]):
                raise RuntimeError("controller must be paused for projection recovery")
            row = conn.execute("""
                SELECT b.*,t.state AS ticket_state,t.feature_id,t.tranche_id,t.objective,
                       t.criterion_ids_json,t.primary_symbol,t.allowed_files_json,t.create_files_json,
                       t.new_test_files_json AS ticket_new_test_files_json,t.forbidden_changes_json,
                       t.patch_budget_json,t.verification_json,t.risk,t.review_required,t.max_attempts,
                       t.dependencies_json,e.entity_type,e.entity_id,e.event_type
                FROM board_projection_outbox b JOIN tickets t ON t.id=b.ticket_id
                JOIN events e ON e.id=b.event_id
                WHERE b.ticket_id=? AND b.event_id=?
            """, (ticket_id, superseded_event_id)).fetchone()
            if row is None or (row["operation"], row["entity_type"], row["entity_id"], row["event_type"]) != ("create_microticket", "ticket", ticket_id, "generated_microticket_created"):
                raise ValueError("generated projection recovery target is ineligible")
            prior = conn.execute(
                "SELECT * FROM generated_projection_recoveries WHERE ticket_id=? AND superseded_event_id=?",
                (ticket_id, superseded_event_id),
            ).fetchone()
            if prior is not None:
                requested = (superseded_external_task_id, operator_id, reason, observed_status, observed_snapshot_hash)
                recorded = tuple(prior[name] for name in (
                    "superseded_external_task_id", "operator_id", "reason", "observed_status", "observed_snapshot_hash"
                ))
                if requested != recorded:
                    raise ValueError("generated projection recovery replay conflicts")
                return dict(prior)
            current_creates = conn.execute(
                "SELECT event_id,external_task_id,acknowledged_at FROM board_projection_outbox "
                "WHERE ticket_id=? AND operation='create_microticket' AND superseded_at IS NULL",
                (ticket_id,),
            ).fetchall()
            if len(current_creates) != 1 or int(current_creates[0]["event_id"]) != superseded_event_id:
                raise ValueError("generated projection recovery requires exactly one current create identity")
            if row["ticket_state"] != "draft" or row["acknowledged_at"] is None or row["external_task_id"] != superseded_external_task_id or row["superseded_at"] is not None:
                raise ValueError("generated projection recovery target is not current acknowledged draft")
            if row["lease_owner"] is not None or row["lease_expires_at"] is not None:
                raise ValueError("generated projection recovery target is leased")
            authority_tables = (
                "attempts", "model_stage_artifacts", "hermes_execution_reconciliations",
                "model_invocations", "review_candidates", "accepted_candidates", "accepted_evidence",
                "git_commit_intents", "git_commit_evidence",
            )
            if any(conn.execute(f"SELECT 1 FROM {table} WHERE ticket_id=? LIMIT 1", (ticket_id,)).fetchone() is not None for table in authority_tables):
                raise ValueError("generated projection recovery refuses existing lifecycle authority")
            if conn.execute("SELECT 1 FROM scheduler_stage_claims WHERE ticket_id=? AND status='claimed' LIMIT 1", (ticket_id,)).fetchone() is not None:
                raise ValueError("generated projection recovery refuses active scheduler authority")
            if conn.execute("SELECT 1 FROM board_projection_outbox WHERE ticket_id=? AND operation!='create_microticket' AND acknowledged_at IS NULL AND superseded_at IS NULL LIMIT 1", (ticket_id,)).fetchone() is not None:
                raise ValueError("generated projection recovery refuses pending board effects")
            if conn.execute("SELECT 1 FROM evidence_comment_outbox WHERE ticket_id=? AND status IN ('pending','retryable','delivering') LIMIT 1", (ticket_id,)).fetchone() is not None:
                raise ValueError("generated projection recovery refuses pending board effects")
            recovery_material = json.dumps({"ticket_id": ticket_id, "event_id": superseded_event_id,
                "external_task_id": superseded_external_task_id, "snapshot_hash": observed_snapshot_hash,
                "status": observed_status}, sort_keys=True, separators=(",", ":"))
            recovery_id = hashlib.sha256(recovery_material.encode()).hexdigest()[:24]
            replacement_key = f"board-create:v2:{ticket_id}:{recovery_id}"
            existing = conn.execute("SELECT * FROM generated_projection_recoveries WHERE recovery_id=?", (recovery_id,)).fetchone()
            if existing is not None:
                return dict(existing)
            identity = self.generated_projection_identity(ticket_id, superseded_event_id)
            verification = json.loads(row["verification_json"])
            ticket = MicroTicket(ticket_id, row["objective"], tuple(json.loads(row["criterion_ids_json"])), row["primary_symbol"], tuple(json.loads(row["allowed_files_json"])), tuple(json.loads(row["forbidden_changes_json"])), PatchBudget(**json.loads(row["patch_budget_json"])), VerificationProfile(tuple(tuple(command) for command in verification["commands"]), verification.get("working_directory", "."), int(verification.get("timeout_seconds", 60)), int(verification.get("output_limit", 20000))), row["risk"], bool(row["review_required"]), int(row["max_attempts"]), tuple(json.loads(row["dependencies_json"])), tuple(json.loads(row["ticket_new_test_files_json"] or "[]")), tuple(json.loads(row["create_files_json"] or "[]")))
            feature = type("PersistedFeature", (), {"id": str(row["feature_id"])})()
            tranche = type("PersistedTranche", (), {"id": str(row["tranche_id"])})()
            payload = generated_card_payload(feature, tranche, ticket,
                repository_identity=identity["repository_identity"], repo_base_sha=identity["repo_base_sha"],
                repo_snapshot_hash=identity["repo_snapshot_hash"], projection_key=replacement_key,
                projection_generation="recovery-v2")
            replacement_event_id = self._append_event(conn, entity_type="ticket", entity_id=ticket_id,
                event_type="generated_microticket_projection_recovered", actor_id=operator_id,
                payload={"recovery_id": recovery_id, "superseded_event_id": superseded_event_id,
                         "superseded_external_task_id": superseded_external_task_id,
                         "observed_status": observed_status, "observed_snapshot_hash": observed_snapshot_hash,
                         "reason": reason})
            conn.execute("INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation) VALUES (?,?,?,?,?,?,'create_microticket')",
                (ticket_id, replacement_event_id, "draft", json.dumps(payload, sort_keys=True, separators=(",", ":")), replacement_key, self._now()))
            now = self._now()
            changed = conn.execute("UPDATE board_projection_outbox SET superseded_at=?,superseded_by_event_id=?,supersession_reason=? WHERE ticket_id=? AND event_id=? AND superseded_at IS NULL",
                (now, replacement_event_id, "operator_recovery_pre_native_projection", ticket_id, superseded_event_id)).rowcount
            if changed != 1:
                raise ValueError("generated projection recovery lost eligibility")
            conn.execute("INSERT INTO generated_projection_recoveries(recovery_id,ticket_id,superseded_event_id,superseded_external_task_id,superseded_idempotency_key,replacement_event_id,replacement_idempotency_key,operator_id,reason,observed_status,observed_snapshot_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (recovery_id, ticket_id, superseded_event_id, superseded_external_task_id, row["idempotency_key"], replacement_event_id, replacement_key, operator_id, reason, observed_status, observed_snapshot_hash, now))
            return dict(conn.execute("SELECT * FROM generated_projection_recoveries WHERE recovery_id=?", (recovery_id,)).fetchone())

    def _enqueue_generated_create_projection_in_transaction(self, conn: sqlite3.Connection, *, ticket_id: str, event_id: int, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        """Enqueue a generated-card intent using the caller's transaction."""
        ticket = conn.execute("SELECT id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if ticket is None:
            raise KeyError(ticket_id)
        event = conn.execute("SELECT entity_type, entity_id, event_type FROM events WHERE id=?", (event_id,)).fetchone()
        if event is None or event["entity_type"] != "ticket" or event["entity_id"] != ticket_id or event["event_type"] not in {"generated_microticket_created", "triage_child_created"}:
            raise ValueError("event is not an authorized microticket creation")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        existing_key = conn.execute("SELECT * FROM board_projection_outbox WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing_key is not None and (existing_key["ticket_id"], int(existing_key["event_id"])) != (ticket_id, event_id):
            raise ValueError("create projection conflicts")
        row = conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        if row is not None:
            if (row["operation"], row["payload_json"], row["idempotency_key"]) != ("create_microticket", encoded, idempotency_key):
                raise ValueError("create projection conflicts")
            return dict(row)
        conn.execute("INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation) VALUES (?,?,?,?,?,?, 'create_microticket')", (ticket_id, event_id, "draft", encoded, idempotency_key, self._now()))
        self._inject_failure("after_generated_projection")
        return dict(conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone())

    def enqueue_generated_create_projection(self, ticket_id: str, event_id: int, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        with self._transaction() as conn:
            return self._enqueue_generated_create_projection_in_transaction(conn, ticket_id=ticket_id, event_id=event_id, payload=payload, idempotency_key=idempotency_key)

    def claim_next_generated_create_projection(self, owner: str, *, lease_seconds: int=60, now: int|None=None) -> dict[str, Any]|None:
        now=self._now() if now is None else now
        with self._transaction() as conn:
            row=conn.execute("SELECT ticket_id,event_id FROM board_projection_outbox WHERE operation='create_microticket' AND terminal_error IS NULL AND acknowledged_at IS NULL AND superseded_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?) AND (lease_expires_at IS NULL OR lease_expires_at<=?) ORDER BY queued_at LIMIT 1",(now,now)).fetchone()
            if not row:return None
            changed=conn.execute("UPDATE board_projection_outbox SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1 WHERE ticket_id=? AND event_id=? AND operation='create_microticket' AND terminal_error IS NULL AND acknowledged_at IS NULL AND superseded_at IS NULL AND (lease_expires_at IS NULL OR lease_expires_at<=?)",(owner,now+lease_seconds,row['ticket_id'],row['event_id'],now))
            if not changed.rowcount:return None
            return dict(conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",(row['ticket_id'],row['event_id'])).fetchone())

    def retry_generated_create_projection(self, ticket_id: str, event_id: int, owner: str, *, error: str, next_attempt_at: int, now: int | None = None) -> None:
        now=self._now() if now is None else now
        with self._transaction() as conn:
            changed=conn.execute("UPDATE board_projection_outbox SET last_error=?,next_attempt_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE ticket_id=? AND event_id=? AND operation='create_microticket' AND acknowledged_at IS NULL AND lease_owner=? AND lease_expires_at>?",(error[:2000],next_attempt_at,ticket_id,event_id,owner,now))
            if not changed.rowcount: raise PermissionError('create projection lease not owned')

    def fail_generated_create_projection(self, ticket_id: str, event_id: int, owner: str, *, error: str, now: int | None = None) -> None:
        """Terminally fail a deterministic create-projection conflict."""
        now=self._now() if now is None else now
        with self._transaction() as conn:
            changed=conn.execute("UPDATE board_projection_outbox SET terminal_error=?,last_error=?,lease_owner=NULL,lease_expires_at=NULL WHERE ticket_id=? AND event_id=? AND operation='create_microticket' AND terminal_error IS NULL AND acknowledged_at IS NULL AND lease_owner=? AND lease_expires_at>?",(error[:2000],error[:2000],ticket_id,event_id,owner,now))
            if not changed.rowcount: raise PermissionError('create projection lease not owned')

    def reopen_terminal_generated_projection(self, ticket_id: str, event_id: int) -> dict[str, Any]:
        """Explicitly reopen a deterministic create failure that never reached Hermes.

        Recovery is allowed only after the current durable payload verifies against
        current authoritative projection identity and only when no external task,
        acknowledgement, or active lease exists. This never repairs/changes payload
        bytes and never retries an ambiguous external create.
        """
        from .generated_projection import DeterministicProjectionError, _canonical_payload
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id,event_id)).fetchone()
            if row is None or row["operation"] != "create_microticket":
                raise KeyError("create projection")
            if row["terminal_error"] is None:
                return dict(row)
            if row["acknowledged_at"] is not None or row["external_task_id"] is not None or row["lease_owner"] is not None or row["lease_expires_at"] is not None:
                raise RuntimeError("generated_projection_recovery_required: projection may have external effects")
            event = conn.execute("SELECT event_type FROM events WHERE id=?", (event_id,)).fetchone()
            if event is None:
                raise RuntimeError("generated_projection_recovery_required: event missing")
            try:
                identity = self.triage_projection_identity(ticket_id,event_id) if event["event_type"] == "triage_child_created" else self.generated_projection_identity(ticket_id,event_id)
                _canonical_payload(dict(row), identity)
            except (DeterministicProjectionError, KeyError, ValueError) as exc:
                raise RuntimeError("generated_projection_recovery_required: current payload still invalid") from exc
            changed = conn.execute(
                "UPDATE board_projection_outbox SET terminal_error=NULL,last_error=NULL,next_attempt_at=NULL WHERE ticket_id=? AND event_id=? AND operation='create_microticket' AND terminal_error IS NOT NULL AND acknowledged_at IS NULL AND external_task_id IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL",
                (ticket_id,event_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("generated_projection_recovery_required: projection changed concurrently")
            return dict(conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id,event_id)).fetchone())

    def complete_generated_create_projection(self, ticket_id: str, event_id: int, owner: str, external_task_id: str, *, now: int | None = None) -> None:
        if not isinstance(external_task_id,str) or not external_task_id: raise ValueError('external task id required')
        now=self._now() if now is None else now
        with self._transaction() as conn:
            row=conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",(ticket_id,event_id)).fetchone()
            if row is None or row['operation']!='create_microticket': raise KeyError('create projection')
            if row['acknowledged_at'] is not None:
                if row['external_task_id']!=external_task_id: raise ValueError('external task id conflicts')
                return
            changed=conn.execute("UPDATE board_projection_outbox SET external_task_id=?,acknowledged_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE ticket_id=? AND event_id=? AND lease_owner=? AND lease_expires_at>?",(external_task_id,now,ticket_id,event_id,owner,now))
            if not changed.rowcount: raise PermissionError('create projection lease not owned')

    def status(self) -> dict[str, Any]:
        paused = self.connection.execute("SELECT paused FROM controller_state WHERE id = 1").fetchone()
        states = self.connection.execute("SELECT state, COUNT(*) AS count FROM tickets GROUP BY state ORDER BY state").fetchall()
        tick = self.connection.execute("SELECT lease_owner,lease_expires_at,updated_at FROM scheduler_tick_lease WHERE id=1").fetchone()
        claims = self.connection.execute("SELECT claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,updated_at FROM scheduler_stage_claims WHERE status='claimed' ORDER BY created_at,claim_id").fetchall()
        return {
            "paused": bool(paused["paused"]) if paused else False,
            "tickets": {row["state"]: row["count"] for row in states},
            "scheduler_tick": dict(tick) if tick else None,
            "scheduler_claims": [dict(row) for row in claims],
        }

    def operator_status(self, *, active_limit: int = 25) -> dict[str, Any]:
        """Return a bounded, non-evidence dashboard read model.

        IDs are limited to active tickets so an operator can identify the current
        stage and feature/tranche without exposing contracts, artifacts, or an
        unbounded history.
        """
        if not 1 <= active_limit <= 100:
            raise ValueError("active_limit must be between 1 and 100")
        paused = self.connection.execute("SELECT paused FROM controller_state WHERE id = 1").fetchone()
        state_counts = {row["state"]: int(row["count"]) for row in self.connection.execute(
            "SELECT state, COUNT(*) AS count FROM tickets GROUP BY state"
        )}
        active_states = (
            CanonicalState.IMPLEMENTING.value,
            CanonicalState.VERIFYING.value,
            CanonicalState.LOCAL_REVIEW.value,
            CanonicalState.REPAIRING.value,
        )
        active = [dict(row) for row in self.connection.execute(
            "SELECT id AS ticket_id, state, feature_id, tranche_id FROM tickets "
            "WHERE state IN (?, ?, ?, ?) ORDER BY updated_at, id LIMIT ?",
            (*active_states, active_limit),
        )]
        outbox_pending = self.connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM board_projection_outbox WHERE acknowledged_at IS NULL AND superseded_at IS NULL) + "
            "(SELECT COUNT(*) FROM evidence_comment_outbox WHERE status IN ('pending', 'retryable', 'delivering'))"
        ).fetchone()[0]
        pending_state_projections = self.connection.execute("SELECT COUNT(*) FROM board_projection_outbox WHERE operation='set_state' AND acknowledged_at IS NULL AND superseded_at IS NULL").fetchone()[0]
        superseded_state_projections = self.connection.execute("SELECT COUNT(*) FROM board_projection_outbox WHERE operation='set_state' AND superseded_at IS NOT NULL").fetchone()[0]
        reconciliations = [{**dict(row), "cleanup_confirmed": row["cleanup_confirmed_at"] is not None} for row in self.connection.execute(
            "SELECT r.ticket_id, r.retired_attempt_number AS retired_attempt, r.prospective_next_attempt_number AS next_attempt, "
            "r.cleanup_required, r.classification, r.retry_base_sha, c.confirmed_at AS cleanup_confirmed_at "
            "FROM failed_attempt_reconciliations r LEFT JOIN retired_attempt_cleanup_confirmations c "
            "ON c.ticket_id=r.ticket_id AND c.retired_attempt_number=r.retired_attempt_number "
            "ORDER BY r.reconciled_at DESC, r.ticket_id LIMIT ?", (active_limit,)
        )]
        return {
            "paused": bool(paused["paused"]) if paused else False,
            "ready_local": state_counts.get(CanonicalState.READY_LOCAL.value, 0),
            "running": sum(state_counts.get(state, 0) for state in active_states),
            "needs_triage": state_counts.get(CanonicalState.NEEDS_TRIAGE.value, 0),
            "done": state_counts.get(CanonicalState.DONE.value, 0),
            "active": active,
            "active_truncated": len(active) == active_limit and sum(state_counts.get(state, 0) for state in active_states) > active_limit,
            "outbox_pending": int(outbox_pending),
            "pending_state_projections": int(pending_state_projections),
            "superseded_state_projections": int(superseded_state_projections),
            "failed_attempt_reconciliations": reconciliations,
        }

    def plan_projection(self, ticket_id: str, *, evidence: str | None = None, state_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Plan a projection without adapters or external commands."""
        row = self.connection.execute("SELECT id, state FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None: raise KeyError(ticket_id)
        event = self.connection.execute("SELECT id FROM events WHERE entity_type='ticket' AND entity_id=? AND event_type='state_transition' AND to_state=? ORDER BY id DESC LIMIT 1", (ticket_id, row["state"])).fetchone()
        if event is None or str(row["state"]) not in self._PROJECTABLE_STATES: return None
        event_id = int(event["id"])
        state_row = self.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        comment_row = self.connection.execute("SELECT * FROM evidence_comment_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
        if state_row is not None and comment_row is not None:
            return {"state": dict(state_row), "comment": dict(comment_row)}
        result = self.enqueue_projection_bundle(ticket_id, event_id, evidence or f"state={row['state']}", state_payload=state_payload)
        self.record_runtime_stage(ticket_id, "projection_enqueued", result["state"]["idempotency_key"])
        return result

    def reconcile_state_projections(self, ticket_id: str) -> dict[str, Any]:
        """Durably retire stale state intents and restore a missing current intent.

        This is ledger-only: it never changes the ticket, domain events, comments,
        leases, or any external board state.
        """
        with self._transaction() as conn:
            ticket = conn.execute("SELECT state FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if ticket is None:
                raise KeyError(ticket_id)
            current = conn.execute(
                "SELECT * FROM events WHERE entity_type='ticket' AND entity_id=? AND "
                "event_type IN ('state_transition','review_reconciliation_authorized') "
                "AND to_state=? ORDER BY id DESC LIMIT 1",
                (ticket_id, ticket["state"]),
            ).fetchone()
            if current is None or not self._is_projectable_state_event(current):
                return {"ticket_id": ticket_id, "current_projection_event_id": None, "superseded_count": 0, "current_intent": None}
            event_id = int(current["id"])
            existing = conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
            if existing is None:
                target = self._resolve_external_task_id_in_transaction(conn, ticket_id)
                conn.execute(
                    "INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation,external_task_id) VALUES (?,?,?,?,?,?, 'set_state',?)",
                    (ticket_id, event_id, str(current["to_state"]), "{}", f"ticket-event:{event_id}", self._now(), target),
                )
            elif (existing["operation"], existing["state"], existing["idempotency_key"]) != ("set_state", str(current["to_state"]), f"ticket-event:{event_id}"):
                raise ValueError("current state projection conflicts with authoritative event")
            count = self._supersede_older_state_projections_in_transaction(conn, ticket_id, event_id)
            current_intent = conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
            return {"ticket_id": ticket_id, "current_projection_event_id": event_id, "superseded_count": count, "current_intent": dict(current_intent)}

    def claim_state_projection(self, ticket_id: str, event_id: int, owner: str, *, lease_seconds: int = 60, now: int | None = None) -> bool:
        now = self._now() if now is None else now
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE board_projection_outbox SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1 "
                "WHERE ticket_id=? AND event_id=? AND operation='set_state' AND acknowledged_at IS NULL "
                "AND superseded_at IS NULL AND terminal_error IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (owner, now + lease_seconds, ticket_id, event_id, now, now),
            )
            return changed.rowcount == 1

    def claim_next_state_projection(self, owner: str, *, lease_seconds: int = 60, now: int | None = None) -> dict[str, Any] | None:
        now = self._now() if now is None else now
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT ticket_id,event_id FROM board_projection_outbox WHERE operation='set_state' AND acknowledged_at IS NULL "
                "AND superseded_at IS NULL AND terminal_error IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "AND (lease_expires_at IS NULL OR lease_expires_at<=?) ORDER BY queued_at,event_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            changed = conn.execute(
                "UPDATE board_projection_outbox SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1 "
                "WHERE ticket_id=? AND event_id=? AND operation='set_state' AND acknowledged_at IS NULL AND superseded_at IS NULL "
                "AND terminal_error IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?) AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (owner, now + lease_seconds, row["ticket_id"], row["event_id"], now, now),
            )
            if changed.rowcount != 1:
                return None
            claimed = conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (row["ticket_id"], row["event_id"])).fetchone()
            return dict(claimed) if claimed else None

    def prepare_claimed_state_projection(self, ticket_id: str, event_id: int, owner: str, *, now: int | None = None) -> dict[str, Any] | None:
        """Final causal freshness gate immediately before a state adapter call."""
        now = self._now() if now is None else now
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone()
            if row is None or row["operation"] != "set_state" or row["acknowledged_at"] is not None:
                return None
            if row["superseded_at"] is not None:
                return None
            if row["lease_owner"] != owner or row["lease_expires_at"] is None or row["lease_expires_at"] <= now:
                raise PermissionError("state projection lease is not owned")
            current = conn.execute(
                "SELECT id FROM events WHERE entity_type='ticket' AND entity_id=? AND event_type IN ('state_transition','review_reconciliation_authorized') "
                "ORDER BY id DESC LIMIT 1", (ticket_id,)
            ).fetchone()
            if row["superseded_at"] is not None or (current is not None and int(current["id"]) > event_id):
                newer = int(current["id"]) if current is not None else event_id
                conn.execute("UPDATE board_projection_outbox SET superseded_at=COALESCE(superseded_at,?),superseded_by_event_id=COALESCE(superseded_by_event_id,?),supersession_reason=COALESCE(supersession_reason,?),lease_owner=NULL,lease_expires_at=NULL WHERE ticket_id=? AND event_id=?", (now, newer, "newer_authoritative_state_event", ticket_id, event_id))
                return None
            target = self._resolve_external_task_id_in_transaction(conn, ticket_id)
            if row["external_task_id"] not in {None, target}:
                raise ValueError("external_projection_identity_conflict")
            if row["external_task_id"] is None:
                conn.execute("UPDATE board_projection_outbox SET external_task_id=? WHERE ticket_id=? AND event_id=?", (target, ticket_id, event_id))
            return dict(conn.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id, event_id)).fetchone())

    def acknowledge_state_projection(self, ticket_id: str, event_id: int, owner: str, *, now: int | None = None) -> bool:
        now = self._now() if now is None else now
        with self._transaction() as conn:
            return conn.execute("UPDATE board_projection_outbox SET acknowledged_at=?,lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=NULL,last_error=NULL WHERE ticket_id=? AND event_id=? AND operation='set_state' AND acknowledged_at IS NULL AND lease_owner=? AND lease_expires_at>?", (now, ticket_id, event_id, owner, now)).rowcount == 1

    def release_state_projection(self, ticket_id: str, event_id: int, owner: str, error: str, *, now: int | None = None) -> bool:
        now = self._now() if now is None else now
        with self._transaction() as conn:
            return conn.execute("UPDATE board_projection_outbox SET lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,last_error=? WHERE ticket_id=? AND event_id=? AND operation='set_state' AND acknowledged_at IS NULL AND superseded_at IS NULL AND lease_owner=? AND lease_expires_at>?", (now, error[:2000], ticket_id, event_id, owner, now)).rowcount == 1

    def project_ticket(self, ticket_id: str, adapter: BoardAdapter) -> bool:
        """Compatibility one-ticket delivery path using the causal state worker."""
        report = self.reconcile_state_projections(ticket_id)
        event_id = report["current_projection_event_id"]
        if event_id is None:
            return False
        from .state_projection import StateProjectionWorker
        result = StateProjectionWorker(self, adapter, worker_id="project-ticket").deliver_ticket_event(ticket_id, int(event_id))
        if result.status != "delivered":
            return False
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO board_projections(ticket_id,event_id,state,projected_at) SELECT ticket_id,event_id,state,? FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (self._now(), ticket_id, event_id))
        return True

    def persist_accepted_candidate(self, ticket_id: str, attempt_number: int, accepted_commit_sha: str, *, candidate_fingerprint: str, diff_summary: str, validation_summary: str, allow_nonpass: bool = False) -> dict[str, Any]:
        """Atomically bind an exact accepted commit and terminal lifecycle state."""
        with self._transaction() as conn:
            ticket = conn.execute("SELECT state FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            attempt = conn.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            candidate = conn.execute("SELECT * FROM review_candidates WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            review = conn.execute("SELECT * FROM review_results WHERE ticket_id=? AND attempt_number=?", (ticket_id, attempt_number)).fetchone()
            existing = conn.execute("SELECT * FROM accepted_evidence WHERE ticket_id=?", (ticket_id,)).fetchone()
            if existing is not None:
                if existing["accepted_commit_sha"] != accepted_commit_sha:
                    raise RuntimeError("conflicting accepted evidence")
                return dict(existing)
            if ticket is None or ticket["state"] not in {CanonicalState.LOCAL_REVIEW.value, CanonicalState.ACCEPTED.value} or attempt is None or candidate is None or review is None:
                raise PermissionError("accepted candidate authority is incomplete")
            if candidate["candidate_fingerprint"] != candidate_fingerprint or (review["verdict"] != "pass" and not (allow_nonpass and review["verdict"] == "repair")):
                raise PermissionError("accepted candidate identity or verdict mismatch")
            if json.loads(review["payload_json"]).get("findings"):
                raise PermissionError("blocking review findings remain")
            if attempt["accepted_commit_sha"] is not None and attempt["accepted_commit_sha"] != accepted_commit_sha:
                raise RuntimeError("conflicting attempt accepted commit")
            now = self._now()
            conn.execute("INSERT INTO accepted_evidence(ticket_id,accepted_commit_sha,diff_summary,validation_summary,created_at) VALUES (?,?,?,?,?)", (ticket_id, accepted_commit_sha, diff_summary, validation_summary, now))
            conn.execute("UPDATE attempts SET accepted_commit_sha=? WHERE ticket_id=? AND attempt_number=? AND (accepted_commit_sha IS NULL OR accepted_commit_sha=?)", (accepted_commit_sha, ticket_id, attempt_number, accepted_commit_sha))
            event_id = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="accepted_evidence_recorded", actor_id="controller", payload={"attempt_number": attempt_number, "commit_sha": accepted_commit_sha, "candidate_fingerprint": candidate_fingerprint, "integration_advanced": False})
            current = CanonicalState(ticket["state"])
            targets = (CanonicalState.ACCEPTED, CanonicalState.DONE) if current == CanonicalState.LOCAL_REVIEW else (CanonicalState.DONE,)
            for target in targets:
                validate_transition(current, target)
                changed = conn.execute("UPDATE tickets SET state=?, updated_at=? WHERE id=? AND state=?", (target.value, now, ticket_id, current.value))
                if changed.rowcount != 1:
                    raise RuntimeError("ticket changed concurrently")
                event_id = self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="state_transition", actor_id="controller", from_state=current.value, to_state=target.value, payload={"attempt_number": attempt_number, "accepted_commit_sha": accepted_commit_sha, "integration_advanced": False})
                if target.value in self._PROJECTABLE_STATES:
                    self._enqueue_projection_bundle_in_transaction(conn, ticket_id=ticket_id, event_id=event_id, evidence=f"state={target.value}", state_payload={"attempt_number": attempt_number, "accepted_commit_sha": accepted_commit_sha})
                current = target
            return dict(conn.execute("SELECT * FROM accepted_evidence WHERE ticket_id=?", (ticket_id,)).fetchone())

    def record_accepted_evidence(self, ticket_id: str, accepted_commit_sha: str, diff_summary: str, validation_summary: str, *, local_reasoning: str | None = None) -> None:
        """Persist only checkpoint-safe accepted evidence; local reasoning is discarded."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO accepted_evidence(ticket_id, accepted_commit_sha, diff_summary, validation_summary, created_at) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, accepted_commit_sha, diff_summary, validation_summary, self._now()),
            )
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="accepted_evidence_recorded", actor_id="controller", payload={"commit_sha": accepted_commit_sha})

    def admit_feature_contract(self, spec: Any, *, repository_identity: str, repo_base_sha: str, repo_snapshot_hash: str, repo_snapshot_manifest_json: str, predecessor: dict[str, Any] | None) -> Any:
        """Atomically admit one immutable feature, initial tranche, and contract."""
        manifest = json.loads(repo_snapshot_manifest_json)
        if hashlib.sha256(repo_snapshot_manifest_json.encode()).hexdigest() != repo_snapshot_hash:
            raise ValueError("repository snapshot hash mismatch")
        predecessor = dict(predecessor or {})
        contract_payload = {"spec": spec.canonical_payload, "repository_identity": repository_identity, "repo_base_sha": repo_base_sha, "repo_snapshot_hash": repo_snapshot_hash, "repo_snapshot_manifest_json": repo_snapshot_manifest_json, "predecessor": predecessor}
        admission_hash = canonical_sha256(contract_payload)
        contract_hash = spec.contract.contract_hash
        contract_json = json.dumps(contract_payload, sort_keys=True, separators=(",", ":"))
        criteria_json = json.dumps([criterion.id for criterion in spec.acceptance_criteria], separators=(",", ":"))
        with self._transaction() as conn:
            existing_feature = conn.execute("SELECT * FROM features WHERE id=?", (spec.feature_id,)).fetchone()
            existing_tranche = conn.execute("SELECT * FROM tranches WHERE id=?", (spec.tranche_id,)).fetchone()
            existing_contract = conn.execute("SELECT * FROM feature_contracts WHERE feature_id=?", (spec.feature_id,)).fetchone()
            if existing_feature or existing_tranche or existing_contract:
                if not (existing_feature and existing_tranche and existing_contract and existing_tranche["feature_id"] == spec.feature_id and existing_contract["contract_hash"] == contract_hash and existing_contract["admission_hash"] == admission_hash):
                    raise ValueError("conflicting feature admission identity or contract")
                return {"feature_id": spec.feature_id, "tranche_id": spec.tranche_id, "contract_hash": contract_hash, "admission_hash": admission_hash, "repository_identity": repository_identity, "repo_base_sha": repo_base_sha, "repo_snapshot_hash": repo_snapshot_hash, "predecessor_tranche_id": predecessor.get("tranche_id"), "predecessor_authority_kind": predecessor.get("kind"), "predecessor_generation": predecessor.get("generation")}
            if conn.execute("SELECT 1 FROM tranches WHERE feature_id=? AND ordinal=0", (spec.feature_id,)).fetchone():
                raise ValueError("feature already has an initial tranche")
            now = self._now()
            conn.execute("INSERT INTO features(id,title,objective,status,integration_base_sha,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (spec.feature_id, spec.feature_title, spec.objective, "planned", repo_base_sha, now, now))
            conn.execute("INSERT INTO tranches(id,feature_id,ordinal,status,base_sha,title,objective,criterion_ids_json) VALUES (?,?,?,?,?,?,?,?)", (spec.tranche_id, spec.feature_id, 0, "planned", repo_base_sha, spec.tranche_title, spec.objective, criteria_json))
            for criterion in spec.acceptance_criteria:
                conn.execute("INSERT INTO tranche_criteria(tranche_id,criterion_id) VALUES (?,?)", (spec.tranche_id, criterion.id))
            conn.execute("INSERT INTO feature_contracts(feature_id,contract_hash,contract_json,created_at,repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json,predecessor_tranche_id,predecessor_authority_kind,predecessor_generation,predecessor_final_integration_sha,predecessor_evidence_hash,admission_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (spec.feature_id, contract_hash, contract_json, now, repository_identity, repo_base_sha, repo_snapshot_hash, repo_snapshot_manifest_json, predecessor.get("tranche_id"), predecessor.get("kind"), predecessor.get("generation"), predecessor.get("final_integration_sha"), predecessor.get("evidence_hash"), admission_hash))
            self._append_event(conn, entity_type="feature", entity_id=spec.feature_id, event_type="feature_contract_admitted", actor_id="controller", payload={"tranche_id": spec.tranche_id, "contract_hash": contract_hash, "repo_base_sha": repo_base_sha, "predecessor": predecessor})
        return {"feature_id": spec.feature_id, "tranche_id": spec.tranche_id, "contract_hash": contract_hash, "admission_hash": admission_hash, "repository_identity": repository_identity, "repo_base_sha": repo_base_sha, "repo_snapshot_hash": repo_snapshot_hash, "predecessor_tranche_id": predecessor.get("tranche_id"), "predecessor_authority_kind": predecessor.get("kind"), "predecessor_generation": predecessor.get("generation")}

    def tranche_completion(self, tranche_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM tranche_completion_evidence WHERE tranche_id=?", (tranche_id,)).fetchone()
        return dict(row) if row else None

    def tranche_checkpoint(self, tranche_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM tranche_checkpoint_evidence WHERE tranche_id=?", (tranche_id,)).fetchone()
        return dict(row) if row else None

    def paid_checkpoint(self, tranche_id: str, purpose: str = "integration_checkpoint") -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM paid_checkpoint_evidence WHERE tranche_id=? AND purpose=?", (tranche_id, purpose)).fetchone()
        return dict(row) if row else None

    def next_tranche_materialization(self, predecessor_tranche_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM next_tranche_materializations WHERE predecessor_tranche_id=?", (predecessor_tranche_id,)).fetchone()
        return dict(row) if row else None

    def next_tranche_activation(self, successor_tranche_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM next_tranche_activation_evidence WHERE successor_tranche_id=?", (successor_tranche_id,)).fetchone()
        return dict(row) if row else None

    def _effective_paid_approval(self, conn: sqlite3.Connection, tranche_id: str) -> sqlite3.Row:
        checkpoint_authority = conn.execute("SELECT * FROM tranche_checkpoint_evidence WHERE tranche_id=?", (tranche_id,)).fetchone()
        if checkpoint_authority is None:
            raise RuntimeError("next_tranche_activation_reconciliation_required: checkpoint authority missing")
        checkpoint = conn.execute("SELECT * FROM paid_checkpoint_evidence WHERE tranche_id=? AND purpose='integration_checkpoint'", (tranche_id,)).fetchone()
        if checkpoint is None:
            raise RuntimeError("next_tranche_activation_reconciliation_required: checkpoint approval missing")
        if str(checkpoint["checkpoint_artifact_sha256"]) != str(checkpoint_authority["checkpoint_artifact_sha256"]) or str(checkpoint["checkpoint_completion_hash"]) != str(checkpoint_authority["completion_evidence_hash"]):
            raise RuntimeError("next_tranche_activation_reconciliation_required: checkpoint approval lineage drift")
        if checkpoint["decision"] == "approve":
            return checkpoint
        if checkpoint["decision"] != "escalate":
            raise RuntimeError("next_tranche_activation_reconciliation_required: checkpoint was not approved")
        escalation = conn.execute("SELECT * FROM paid_checkpoint_evidence WHERE tranche_id=? AND purpose='escalation'", (tranche_id,)).fetchone()
        if escalation is None or escalation["decision"] != "approve":
            raise RuntimeError("next_tranche_activation_reconciliation_required: escalation approval missing")
        if str(escalation["checkpoint_artifact_sha256"]) != str(checkpoint_authority["checkpoint_artifact_sha256"]) or str(escalation["checkpoint_completion_hash"]) != str(checkpoint_authority["completion_evidence_hash"]):
            raise RuntimeError("next_tranche_activation_reconciliation_required: escalation approval lineage drift")
        return escalation

    def _next_tranche_materialize_identity(self, conn: sqlite3.Connection, predecessor_tranche_id: str) -> dict[str, Any]:
        predecessor = conn.execute("SELECT * FROM tranches WHERE id=?", (predecessor_tranche_id,)).fetchone()
        checkpoint = conn.execute("SELECT * FROM tranche_checkpoint_evidence WHERE tranche_id=?", (predecessor_tranche_id,)).fetchone()
        completion = conn.execute("SELECT * FROM tranche_completion_evidence WHERE tranche_id=?", (predecessor_tranche_id,)).fetchone()
        if predecessor is None or checkpoint is None or completion is None or checkpoint["decision"] != "ready_for_checkpoint":
            raise RuntimeError("next_tranche_activation_reconciliation_required: predecessor checkpoint authority missing")
        approval = self._effective_paid_approval(conn, predecessor_tranche_id)
        successor = conn.execute("SELECT * FROM tranches WHERE feature_id=? AND ordinal=?", (predecessor["feature_id"], int(predecessor["ordinal"]) + 1)).fetchone()
        if successor is None or successor["status"] not in {"planned", "active"}:
            raise RuntimeError("next_tranche_activation_reconciliation_required: successor tranche missing")
        active = [str(row["id"]) for row in conn.execute("SELECT id FROM tranches WHERE feature_id=? AND status='active' ORDER BY ordinal,id", (predecessor["feature_id"],)).fetchall()]
        if predecessor["status"] == "active":
            if active != [predecessor_tranche_id]:
                raise RuntimeError("next_tranche_activation_reconciliation_required: active tranche conflict")
        elif predecessor["status"] == "completed" and successor["status"] == "active":
            if active != [str(successor["id"])]:
                raise RuntimeError("next_tranche_activation_reconciliation_required: recovered active tranche conflict")
        else:
            raise RuntimeError("next_tranche_activation_reconciliation_required: tranche handoff state invalid")
        return {
            "feature_id": str(predecessor["feature_id"]),
            "predecessor_tranche_id": predecessor_tranche_id,
            "predecessor_ordinal": int(predecessor["ordinal"]),
            "successor_tranche_id": str(successor["id"]),
            "successor_ordinal": int(successor["ordinal"]),
            "checkpoint_artifact_sha256": str(checkpoint["checkpoint_artifact_sha256"]),
            "completion_evidence_hash": str(completion["evidence_hash"]),
            "final_integration_sha": str(completion["final_integration_sha"]),
            "repository_identity": str(checkpoint["repository_identity"]),
            "approval_purpose": str(approval["purpose"]),
            "approval_model_call_id": str(approval["model_call_id"]),
            "approval_response_sha256": hashlib.sha256(str(approval["response_json"]).encode()).hexdigest(),
        }

    def claim_next_scheduler_next_tranche_materialize(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        if not owner or lease_seconds < 1:
            raise ValueError("next tranche materialize claim requires owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            replay = conn.execute("SELECT * FROM scheduler_stage_claims WHERE stage='next_tranche_materialize' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,)).fetchone()
            if replay is not None:
                identity = json.loads(str(replay["candidate_identity_json"] or "{}"))
                if identity != self._next_tranche_materialize_identity(conn, str(identity.get("predecessor_tranche_id") or "")):
                    raise RuntimeError("next_tranche_activation_reconciliation_required: materialize claim drift")
                if conn.execute("UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?", (owner,now+lease_seconds,now,replay["claim_id"],now)).rowcount != 1:
                    return None
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())
            rows = conn.execute("SELECT id FROM tranches WHERE status='active' AND NOT EXISTS (SELECT 1 FROM next_tranche_materializations m WHERE m.predecessor_tranche_id=tranches.id) ORDER BY feature_id,ordinal,id").fetchall()
            for row in rows:
                try:
                    identity = self._next_tranche_materialize_identity(conn, str(row["id"]))
                except RuntimeError:
                    continue
                encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
                claim_id = hashlib.sha256(("next_tranche_materialize:" + encoded).encode()).hexdigest()[:32]
                ticket = conn.execute("SELECT id FROM tickets WHERE tranche_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (row["id"],)).fetchone()
                if ticket is None:
                    continue
                conn.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,'next_tranche_materialize','claimed',?,?,1,?,?,?)", (claim_id,ticket["id"],owner,now+lease_seconds,encoded,now,now))
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())
            return None

    def apply_scheduler_next_tranche_materialize_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or claim["stage"] != "next_tranche_materialize" or claim["side_effect_started_at"] is None:
                raise RuntimeError("next tranche materialize claim is not active")
            if claim["lease_owner"] != owner or int(claim["lease_expires_at"] or 0) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            if claim["side_effect_completed_at"] is not None:
                if claim["result_json"] != encoded: raise RuntimeError("next tranche materialize result conflicts")
                return dict(claim)
            identity = json.loads(str(claim["candidate_identity_json"] or "{}"))
            if result.get("candidate_identity") != identity:
                raise RuntimeError("next_tranche_activation_reconciliation_required: materialize result identity drift")
            successor = conn.execute("SELECT * FROM tranches WHERE id=?", (identity["successor_tranche_id"],)).fetchone()
            predecessor = conn.execute("SELECT * FROM tranches WHERE id=?", (identity["predecessor_tranche_id"],)).fetchone()
            ticket_ids = result.get("ticket_ids")
            if predecessor is None or successor is None or predecessor["status"] != "completed" or successor["status"] != "active" or not isinstance(ticket_ids,list) or not ticket_ids:
                raise RuntimeError("next_tranche_activation_reconciliation_required: materialized tranche state invalid")
            durable_ids = [str(row["id"]) for row in conn.execute("SELECT id FROM tickets WHERE tranche_id=? ORDER BY id", (successor["id"],)).fetchall()]
            if sorted(ticket_ids) != durable_ids:
                raise RuntimeError("next_tranche_activation_reconciliation_required: materialized ticket identity drift")
            snapshot_hash = str(result.get("repo_snapshot_hash") or "")
            repo_base_sha = str(result.get("repo_base_sha") or "")
            repository_identity = str(result.get("repository_identity") or "")
            if not snapshot_hash or repo_base_sha != identity["final_integration_sha"] or repository_identity != identity["repository_identity"]:
                raise RuntimeError("next_tranche_activation_reconciliation_required: repository snapshot drift")
            values=(identity["predecessor_tranche_id"],identity["successor_tranche_id"],identity["feature_id"],identity["approval_purpose"],identity["approval_model_call_id"],identity["completion_evidence_hash"],repository_identity,repo_base_sha,snapshot_hash,json.dumps(durable_ids,separators=(",",":")))
            existing = conn.execute("SELECT * FROM next_tranche_materializations WHERE predecessor_tranche_id=?", (identity["predecessor_tranche_id"],)).fetchone()
            keys=("predecessor_tranche_id","successor_tranche_id","feature_id","approval_purpose","approval_model_call_id","predecessor_completion_hash","repository_identity","repo_base_sha","repo_snapshot_hash","ticket_ids_json")
            if existing is None:
                conn.execute("INSERT INTO next_tranche_materializations(predecessor_tranche_id,successor_tranche_id,feature_id,approval_purpose,approval_model_call_id,predecessor_completion_hash,repository_identity,repo_base_sha,repo_snapshot_hash,ticket_ids_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*values,now))
            elif tuple(existing[k] for k in keys) != values:
                raise RuntimeError("next_tranche_activation_reconciliation_required: materialization evidence conflicts")
            conn.execute("UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=?", (now,encoded,now,claim_id))
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def _next_tranche_activation_identity(self, conn: sqlite3.Connection, successor_tranche_id: str) -> dict[str, Any]:
        materialization = conn.execute("SELECT * FROM next_tranche_materializations WHERE successor_tranche_id=?", (successor_tranche_id,)).fetchone()
        if materialization is None:
            raise RuntimeError("next_tranche_activation_reconciliation_required: materialization evidence missing")
        try:
            ticket_ids = list(json.loads(str(materialization["ticket_ids_json"])))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("next_tranche_activation_reconciliation_required: materialization ticket identity malformed") from exc
        if not ticket_ids or not all(isinstance(value,str) and value for value in ticket_ids):
            raise RuntimeError("next_tranche_activation_reconciliation_required: materialization ticket identity malformed")
        external_task_ids: list[str] = []
        graph_hashes: list[dict[str,str]] = []
        for ticket_id in ticket_ids:
            ticket = conn.execute("SELECT * FROM tickets WHERE id=? AND tranche_id=?", (ticket_id,successor_tranche_id)).fetchone()
            if ticket is None:
                raise RuntimeError("next_tranche_activation_reconciliation_required: successor ticket missing")
            external_id = self._resolve_external_task_id_in_transaction(conn, ticket_id)
            projections = conn.execute("SELECT acknowledged_at,external_task_id FROM board_projection_outbox WHERE ticket_id=? AND operation='create_microticket' AND superseded_at IS NULL", (ticket_id,)).fetchall()
            if len(projections) != 1 or projections[0]["acknowledged_at"] is None or str(projections[0]["external_task_id"] or "") != external_id:
                raise RuntimeError("next_tranche_activation_reconciliation_required: successor card projection not acknowledged")
            external_task_ids.append(external_id)
            dependencies = sorted(set(json.loads(str(ticket["dependencies_json"]))))
            if dependencies:
                graph = conn.execute("SELECT * FROM native_dependency_graphs WHERE ticket_id=?", (ticket_id,)).fetchone()
                if graph is None:
                    raise RuntimeError("next_tranche_activation_reconciliation_required: native dependency graph not verified")
                if json.loads(str(graph["local_dependency_ids_json"])) != dependencies:
                    raise RuntimeError("next_tranche_activation_reconciliation_required: native dependency contract drift")
                expected_parent_external_ids = [self._resolve_external_task_id_in_transaction(conn, dep) for dep in dependencies]
                if json.loads(str(graph["parent_external_ids_json"])) != expected_parent_external_ids or str(graph["child_external_id"]) != external_id:
                    raise RuntimeError("next_tranche_activation_reconciliation_required: native dependency graph identity drift")
                graph_hashes.append({"ticket_id":ticket_id,"graph_hash":str(graph["graph_hash"])})
        return {
            "feature_id": str(materialization["feature_id"]),
            "predecessor_tranche_id": str(materialization["predecessor_tranche_id"]),
            "successor_tranche_id": successor_tranche_id,
            "repo_snapshot_hash": str(materialization["repo_snapshot_hash"]),
            "ticket_ids": ticket_ids,
            "external_task_ids": external_task_ids,
            "dependency_graph_hashes": graph_hashes,
        }

    def claim_next_scheduler_next_tranche_activation(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        if not owner or lease_seconds < 1:
            raise ValueError("next tranche activation claim requires owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            replay = conn.execute("SELECT * FROM scheduler_stage_claims WHERE stage='next_tranche_activation' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,)).fetchone()
            if replay is not None:
                identity = json.loads(str(replay["candidate_identity_json"] or "{}"))
                if identity != self._next_tranche_activation_identity(conn, str(identity.get("successor_tranche_id") or "")):
                    raise RuntimeError("next_tranche_activation_reconciliation_required: activation claim drift")
                if conn.execute("UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?", (owner,now+lease_seconds,now,replay["claim_id"],now)).rowcount != 1:
                    return None
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())
            rows = conn.execute("SELECT successor_tranche_id FROM next_tranche_materializations m WHERE NOT EXISTS (SELECT 1 FROM next_tranche_activation_evidence e WHERE e.successor_tranche_id=m.successor_tranche_id) ORDER BY created_at,successor_tranche_id").fetchall()
            for row in rows:
                successor_id = str(row["successor_tranche_id"])
                try:
                    identity = self._next_tranche_activation_identity(conn, successor_id)
                except RuntimeError:
                    continue
                encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
                claim_id = hashlib.sha256(("next_tranche_activation:" + encoded).encode()).hexdigest()[:32]
                ticket_id = str(identity["ticket_ids"][-1])
                conn.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,'next_tranche_activation','claimed',?,?,1,?,?,?)", (claim_id,ticket_id,owner,now+lease_seconds,encoded,now,now))
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())
            return None

    def apply_scheduler_next_tranche_activation_effect(self, claim_id: str, owner: str, *, now: int | None = None) -> dict[str, Any]:
        now = self._now() if now is None else now
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or claim["stage"] != "next_tranche_activation" or claim["side_effect_started_at"] is None:
                raise RuntimeError("next tranche activation claim is not active")
            if claim["lease_owner"] != owner or int(claim["lease_expires_at"] or 0) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            identity = json.loads(str(claim["candidate_identity_json"] or "{}"))
            if identity != self._next_tranche_activation_identity(conn, str(identity["successor_tranche_id"])):
                raise RuntimeError("next_tranche_activation_reconciliation_required: activation identity drift")
            values=(identity["successor_tranche_id"],identity["predecessor_tranche_id"],identity["feature_id"],identity["repo_snapshot_hash"],json.dumps(identity["ticket_ids"],separators=(",",":")),json.dumps(identity["external_task_ids"],separators=(",",":")),json.dumps(identity["dependency_graph_hashes"],sort_keys=True,separators=(",",":")))
            existing = conn.execute("SELECT * FROM next_tranche_activation_evidence WHERE successor_tranche_id=?", (identity["successor_tranche_id"],)).fetchone()
            keys=("successor_tranche_id","predecessor_tranche_id","feature_id","repo_snapshot_hash","ticket_ids_json","external_task_ids_json","dependency_graph_hashes_json")
            if existing is None:
                conn.execute("INSERT INTO next_tranche_activation_evidence(successor_tranche_id,predecessor_tranche_id,feature_id,repo_snapshot_hash,ticket_ids_json,external_task_ids_json,dependency_graph_hashes_json,activated_at) VALUES (?,?,?,?,?,?,?,?)", (*values,now))
            elif tuple(existing[k] for k in keys) != values:
                raise RuntimeError("next_tranche_activation_reconciliation_required: activation evidence conflicts")
            result={"ticket_id":str(claim["ticket_id"]),"candidate_identity":identity,"successor_tranche_id":identity["successor_tranche_id"]}
            encoded=json.dumps(result,sort_keys=True,separators=(",",":"))
            if claim["side_effect_completed_at"] is None:
                conn.execute("UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=?", (now,encoded,now,claim_id))
            elif claim["result_json"] != encoded:
                raise RuntimeError("next tranche activation result conflicts")
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def _paid_stage_identity(self, conn: sqlite3.Connection, tranche_id: str, *, purpose: str, provider: str, model: str, profile: str) -> dict[str, Any]:
        if purpose not in {"integration_checkpoint", "escalation"}:
            raise ValueError("invalid paid scheduler purpose")
        checkpoint = conn.execute("SELECT * FROM tranche_checkpoint_evidence WHERE tranche_id=?", (tranche_id,)).fetchone()
        if checkpoint is None or checkpoint["decision"] != "ready_for_checkpoint":
            raise RuntimeError("paid_checkpoint_reconciliation_required: deterministic checkpoint is not ready")
        tranche = conn.execute("SELECT * FROM tranches WHERE id=?", (tranche_id,)).fetchone()
        if tranche is None:
            raise RuntimeError("paid_checkpoint_reconciliation_required: tranche missing")
        prior = None
        if purpose == "escalation":
            prior = conn.execute("SELECT * FROM paid_checkpoint_evidence WHERE tranche_id=? AND purpose='integration_checkpoint'", (tranche_id,)).fetchone()
            if prior is None or prior["decision"] != "escalate":
                raise RuntimeError("paid_checkpoint_reconciliation_required: escalation was not requested")
        identity = {
            "feature_id": str(tranche["feature_id"]),
            "tranche_id": tranche_id,
            "purpose": purpose,
            "provider": provider,
            "model": model,
            "profile": profile,
            "checkpoint_artifact": str(checkpoint["checkpoint_artifact"]),
            "checkpoint_artifact_sha256": str(checkpoint["checkpoint_artifact_sha256"]),
            "checkpoint_completion_hash": str(checkpoint["completion_evidence_hash"]),
            "final_integration_sha": str(checkpoint["final_integration_sha"]),
        }
        if prior is not None:
            identity["prior_checkpoint_response_sha256"] = hashlib.sha256(str(prior["response_json"]).encode()).hexdigest()
            identity["prior_checkpoint_decision"] = str(prior["decision"])
        return identity

    def claim_next_scheduler_paid_stage(self, owner: str, *, lease_seconds: int, purpose: str, provider: str, model: str, profile: str, now: int | None = None) -> dict[str, Any] | None:
        if not owner or lease_seconds < 1 or not all(value and isinstance(value, str) for value in (purpose, provider, model, profile)):
            raise ValueError("paid scheduler claim requires owner, route, purpose, and positive lease")
        stage = "paid_checkpoint" if purpose == "integration_checkpoint" else "paid_escalation"
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute("SELECT * FROM scheduler_stage_claims WHERE stage=? AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (stage, now)).fetchone()
            if replay is not None:
                old = json.loads(str(replay["candidate_identity_json"] or "{}"))
                current = self._paid_stage_identity(conn, str(old.get("tranche_id") or ""), purpose=purpose, provider=provider, model=model, profile=profile)
                if old != current:
                    raise RuntimeError("paid_checkpoint_reconciliation_required: claim identity drift")
                if conn.execute("UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?", (owner,now+lease_seconds,now,replay["claim_id"],now)).rowcount != 1:
                    return None
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())
            if purpose == "integration_checkpoint":
                rows = conn.execute("SELECT tranche_id FROM tranche_checkpoint_evidence c WHERE c.decision='ready_for_checkpoint' AND NOT EXISTS (SELECT 1 FROM paid_checkpoint_evidence p WHERE p.tranche_id=c.tranche_id AND p.purpose='integration_checkpoint') ORDER BY c.created_at,c.tranche_id").fetchall()
            else:
                rows = conn.execute("SELECT tranche_id FROM paid_checkpoint_evidence p WHERE p.purpose='integration_checkpoint' AND p.decision='escalate' AND NOT EXISTS (SELECT 1 FROM paid_checkpoint_evidence e WHERE e.tranche_id=p.tranche_id AND e.purpose='escalation') ORDER BY p.created_at,p.tranche_id").fetchall()
            for row in rows:
                tranche_id = str(row["tranche_id"])
                identity = self._paid_stage_identity(conn, tranche_id, purpose=purpose, provider=provider, model=model, profile=profile)
                encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
                claim_id = hashlib.sha256((stage + ":" + encoded).encode()).hexdigest()[:32]
                if conn.execute("SELECT 1 FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone():
                    continue
                ticket = conn.execute("SELECT id FROM tickets WHERE tranche_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (tranche_id,)).fetchone()
                if ticket is None:
                    raise RuntimeError("paid_checkpoint_reconciliation_required: tranche has no ticket anchor")
                conn.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,'claimed',?,?,1,?,?,?)", (claim_id,ticket["id"],stage,owner,now+lease_seconds,encoded,now,now))
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())
            return None

    def apply_scheduler_paid_stage_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or claim["stage"] not in {"paid_checkpoint", "paid_escalation"} or claim["side_effect_started_at"] is None:
                raise RuntimeError("paid scheduler claim is not active")
            if claim["lease_owner"] != owner or int(claim["lease_expires_at"] or 0) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            if claim["side_effect_completed_at"] is not None:
                if claim["result_json"] != encoded:
                    raise RuntimeError("paid scheduler result conflicts")
                return dict(claim)
            identity = json.loads(str(claim["candidate_identity_json"] or "{}"))
            if result.get("candidate_identity") != identity:
                raise RuntimeError("paid_checkpoint_reconciliation_required: result identity drift")
            purpose = str(identity["purpose"])
            current = self._paid_stage_identity(conn, str(identity["tranche_id"]), purpose=purpose, provider=str(identity["provider"]), model=str(identity["model"]), profile=str(identity["profile"]))
            if current != identity:
                raise RuntimeError("paid_checkpoint_reconciliation_required: paid claim authority drift")
            decision = result.get("decision")
            rationale = result.get("rationale")
            response = result.get("response")
            if decision not in {"approve", "escalate", "reject"} or not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000 or not isinstance(response, dict):
                raise RuntimeError("paid_checkpoint_reconciliation_required: invalid paid response")
            canonical_response = json.dumps(response, sort_keys=True, separators=(",", ":"))
            if response != {"decision": decision, "rationale": rationale}:
                raise RuntimeError("paid_checkpoint_reconciliation_required: response schema mismatch")
            reservation = conn.execute("SELECT * FROM paid_reservations WHERE feature_id=? AND purpose=? AND request_key=?", (identity["feature_id"],purpose,claim_id)).fetchone()
            if reservation is None or reservation["status"] != "completed":
                raise RuntimeError("paid_checkpoint_reconciliation_required: paid reservation is not completed")
            call = conn.execute("SELECT * FROM model_calls WHERE reservation_id=?", (reservation["id"],)).fetchone()
            if call is None or call["status"] != "completed" or str(call["response_artifact_json"] or "") != json.dumps(response, sort_keys=True):
                raise RuntimeError("paid_checkpoint_reconciliation_required: paid model call evidence drift")
            values=(identity["tranche_id"],identity["feature_id"],identity["checkpoint_artifact_sha256"],identity["checkpoint_completion_hash"],claim_id,claim_id,purpose,identity["provider"],identity["model"],identity["profile"],reservation["id"],call["id"],canonical_response,decision,rationale.strip())
            existing = conn.execute("SELECT * FROM paid_checkpoint_evidence WHERE tranche_id=? AND purpose=?", (identity["tranche_id"],purpose)).fetchone()
            keys=("tranche_id","feature_id","checkpoint_artifact_sha256","checkpoint_completion_hash","scheduler_claim_id","request_key","purpose","provider","model","profile","reservation_id","model_call_id","response_json","decision","rationale")
            if existing is None:
                conn.execute("INSERT INTO paid_checkpoint_evidence(tranche_id,feature_id,checkpoint_artifact_sha256,checkpoint_completion_hash,scheduler_claim_id,request_key,purpose,provider,model,profile,reservation_id,model_call_id,response_json,decision,rationale,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values,now))
            elif tuple(existing[key] for key in keys) != values:
                raise RuntimeError("paid_checkpoint_reconciliation_required: paid evidence conflicts")
            conn.execute("UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=?", (now,encoded,now,claim_id))
            self._append_event(conn, entity_type="tranche", entity_id=str(identity["tranche_id"]), event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id":claim_id,"stage":claim["stage"],"purpose":purpose,"decision":decision,"reservation_id":reservation["id"],"model_call_id":call["id"]})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def claim_next_scheduler_tranche_checkpoint(self, owner: str, *, lease_seconds: int, now: int | None = None) -> dict[str, Any] | None:
        if not owner or lease_seconds < 1:
            raise ValueError("tranche checkpoint claim requires owner and positive lease")
        now = self._now() if now is None else now
        with self._transaction() as conn:
            if conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()["paused"]:
                return None
            replay = conn.execute("SELECT * FROM scheduler_stage_claims WHERE stage='tranche_checkpoint' AND status='claimed' AND lease_expires_at<=? ORDER BY created_at,claim_id LIMIT 1", (now,)).fetchone()
            if replay is not None:
                identity = json.loads(str(replay["candidate_identity_json"] or "{}"))
                current = self._tranche_checkpoint_identity(conn, str(identity.get("tranche_id") or ""))
                if self._tranche_checkpoint_h1_conflicts(conn, current):
                    reason = "tranche checkpoint superseded by post-H1 completion authority"
                    result = json.dumps({
                        "status": "failed",
                        "reason": reason,
                        "claim_id": str(replay["claim_id"]),
                        "tranche_id": str(current["tranche_id"]),
                    }, sort_keys=True, separators=(",", ":"))
                    conn.execute(
                        "UPDATE scheduler_stage_claims SET status='failed',lease_owner=NULL,lease_expires_at=NULL,last_error=?,result_json=?,finalized_at=?,updated_at=? WHERE claim_id=? AND status='claimed'",
                        (reason, result, now, now, replay["claim_id"]),
                    )
                    self._append_event(
                        conn,
                        entity_type="tranche",
                        entity_id=str(current["tranche_id"]),
                        event_type="scheduler_stage_reconciled",
                        actor_id=owner,
                        payload={"claim_id": str(replay["claim_id"]), "stage": "tranche_checkpoint", "outcome": "failed", "reason": reason},
                    )
                    replay = None
                if replay is None:
                    pass
                elif identity != current:
                    raise RuntimeError("tranche_checkpoint_reconciliation_required: claim identity drift")
                elif conn.execute("UPDATE scheduler_stage_claims SET lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE claim_id=? AND status='claimed' AND lease_expires_at<=?", (owner,now+lease_seconds,now,replay["claim_id"],now)).rowcount != 1:
                    return None
                elif replay is not None:
                    return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (replay["claim_id"],)).fetchone())
            rows = conn.execute("SELECT id FROM tranches WHERE status='active' AND NOT EXISTS (SELECT 1 FROM tranche_checkpoint_evidence c WHERE c.tranche_id=tranches.id) ORDER BY feature_id,ordinal,id").fetchall()
            for row in rows:
                try:
                    identity = self._tranche_checkpoint_identity(conn, str(row["id"]))
                except RuntimeError:
                    continue
                if self._tranche_checkpoint_h1_conflicts(conn, identity):
                    continue
                claim_ticket_id = str(identity["ticket_ids"][-1])
                if conn.execute("SELECT 1 FROM scheduler_stage_claims WHERE ticket_id=? AND stage='tranche_checkpoint'", (claim_ticket_id,)).fetchone():
                    continue
                encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
                claim_id = hashlib.sha256(("tranche_checkpoint:" + encoded).encode()).hexdigest()[:32]
                conn.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES (?,?,'tranche_checkpoint','claimed',?,?,1,?,?,?)", (claim_id,claim_ticket_id,owner,now+lease_seconds,encoded,now,now))
                return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())
            return None

    def _tranche_checkpoint_identity(self, conn: sqlite3.Connection, tranche_id: str) -> dict[str, Any]:
        tranche = conn.execute("SELECT * FROM tranches WHERE id=?", (tranche_id,)).fetchone()
        if tranche is None or tranche["status"] != "active" or not tranche["base_sha"]:
            raise RuntimeError("tranche_checkpoint_reconciliation_required: tranche is not active")
        plan = conn.execute("SELECT repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json FROM decomposition_plans WHERE feature_id=? AND status='active' ORDER BY activated_at DESC LIMIT 1", (tranche["feature_id"],)).fetchone()
        if plan is None or not all(isinstance(plan[k], str) and plan[k] for k in ("repository_identity","repo_base_sha","repo_snapshot_hash","repo_snapshot_manifest_json")):
            raise RuntimeError("tranche_checkpoint_reconciliation_required: planning snapshot authority missing")
        tickets = conn.execute("SELECT t.id,t.state,ae.accepted_commit_sha FROM tickets t LEFT JOIN accepted_evidence ae ON ae.ticket_id=t.id WHERE t.tranche_id=? AND t.id NOT LIKE 'tranche:%' ORDER BY t.created_at,t.id", (tranche_id,)).fetchall()
        if not tickets or any(row["state"] != CanonicalState.DONE.value or not row["accepted_commit_sha"] for row in tickets):
            raise RuntimeError("tranche_checkpoint_reconciliation_required: tranche work incomplete")
        commands = json.loads(str(tranche["integration_commands_json"] or "[]"))
        if not isinstance(commands, list) or any(not isinstance(c, list) or not c or not all(isinstance(x,str) and x for x in c) for c in commands):
            raise RuntimeError("tranche_checkpoint_reconciliation_required: integration commands malformed")
        return {"tranche_id":tranche_id,"feature_id":str(tranche["feature_id"]),"tranche_base_sha":str(tranche["base_sha"]),"repository_identity":str(plan["repository_identity"]),"planning_base_sha":str(plan["repo_base_sha"]),"planning_snapshot_hash":str(plan["repo_snapshot_hash"]),"planning_snapshot_manifest_json":str(plan["repo_snapshot_manifest_json"]),"integration_commands":commands,"ticket_ids":[str(r["id"]) for r in tickets],"accepted_commit_shas":[str(r["accepted_commit_sha"]) for r in tickets]}

    def _tranche_checkpoint_h1_conflicts(self, conn: sqlite3.Connection, identity: dict[str, Any]) -> bool:
        existing = conn.execute(
            "SELECT root_planning_sha,final_integration_sha,accepted_ticket_ids_json,accepted_commit_shas_json "
            "FROM tranche_completion_evidence WHERE tranche_id=?",
            (identity["tranche_id"],),
        ).fetchone()
        if existing is None:
            return False
        expected = (
            identity["tranche_base_sha"],
            identity["accepted_commit_shas"][-1],
            json.dumps(identity["ticket_ids"], separators=(",", ":")),
            json.dumps(identity["accepted_commit_shas"], separators=(",", ":")),
        )
        return tuple(existing[key] for key in (
            "root_planning_sha", "final_integration_sha", "accepted_ticket_ids_json", "accepted_commit_shas_json"
        )) != expected

    def apply_scheduler_tranche_checkpoint_effect(self, claim_id: str, owner: str, result: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        now = self._now() if now is None else now
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction() as conn:
            claim = conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None or claim["stage"] != "tranche_checkpoint" or claim["side_effect_started_at"] is None:
                raise RuntimeError("tranche checkpoint claim is not active")
            if claim["lease_owner"] != owner or int(claim["lease_expires_at"] or 0) <= now:
                raise PermissionError("scheduler claim lease is not owned")
            if claim["side_effect_completed_at"] is not None:
                if claim["result_json"] != encoded: raise RuntimeError("tranche checkpoint result conflicts")
                return dict(claim)
            identity = json.loads(str(claim["candidate_identity_json"]))
            if result.get("candidate_identity") != identity or self._tranche_checkpoint_identity(conn, str(identity["tranche_id"])) != identity:
                raise RuntimeError("tranche_checkpoint_reconciliation_required: result identity drift")
            completion = result.get("completion") or {}
            if completion.get("tranche_id") != identity["tranche_id"] or completion.get("accepted_ticket_ids") != identity["ticket_ids"] or completion.get("accepted_commit_shas") != identity["accepted_commit_shas"]:
                raise RuntimeError("tranche_checkpoint_reconciliation_required: completion evidence drift")
            completion = dict(completion); completion["accepted_ticket_ids_json"] = json.dumps(completion["accepted_ticket_ids"],separators=(",",":")); completion["accepted_commit_shas_json"] = json.dumps(completion["accepted_commit_shas"],separators=(",",":")); expected_hash = _completion_evidence_hash(completion)
            if completion.get("evidence_hash") != expected_hash: raise RuntimeError("tranche_checkpoint_reconciliation_required: completion hash invalid")
            integration_results = result.get("integration_results")
            if not isinstance(integration_results, list) or len(integration_results) > len(identity["integration_commands"]):
                raise RuntimeError("tranche_checkpoint_reconciliation_required: integration results malformed")
            for index, entry in enumerate(integration_results):
                if not isinstance(entry, dict) or entry.get("index") != index or entry.get("command") != identity["integration_commands"][index] or not isinstance(entry.get("returncode"), int):
                    raise RuntimeError("tranche_checkpoint_reconciliation_required: integration result identity drift")
            failed = next((entry for entry in integration_results if int(entry["returncode"]) != 0), None)
            expected_decision = "integration_failed" if failed is not None else "ready_for_checkpoint"
            if failed is None and len(integration_results) != len(identity["integration_commands"]):
                raise RuntimeError("tranche_checkpoint_reconciliation_required: integration results incomplete")
            if result.get("decision") != expected_decision:
                raise RuntimeError("tranche_checkpoint_reconciliation_required: checkpoint decision drift")
            artifact = Path(str(result.get("checkpoint_artifact") or ""))
            if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != result.get("checkpoint_artifact_sha256"):
                raise RuntimeError("tranche_checkpoint_reconciliation_required: checkpoint artifact drift")
            try:
                artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("tranche_checkpoint_reconciliation_required: checkpoint artifact malformed") from exc
            if artifact_payload.get("tranche_id") != identity["tranche_id"] or artifact_payload.get("feature_id") != identity["feature_id"] or artifact_payload.get("candidate_identity") != identity or artifact_payload.get("integration_commands") != identity["integration_commands"] or artifact_payload.get("completion") != completion or artifact_payload.get("integration_results") != integration_results or artifact_payload.get("decision") != expected_decision:
                raise RuntimeError("tranche_checkpoint_reconciliation_required: checkpoint artifact identity drift")
            existing = conn.execute("SELECT * FROM tranche_completion_evidence WHERE tranche_id=?", (identity["tranche_id"],)).fetchone()
            values=(identity["tranche_id"],completion["root_planning_sha"],completion["final_integration_sha"],completion["accepted_ticket_ids_json"],completion["accepted_commit_shas_json"],completion["evidence_hash"])
            if existing is None: conn.execute("INSERT INTO tranche_completion_evidence(tranche_id,root_planning_sha,final_integration_sha,accepted_ticket_ids_json,accepted_commit_shas_json,evidence_hash,completed_at) VALUES (?,?,?,?,?,?,?)", (*values,now))
            elif tuple(existing[k] for k in ("tranche_id","root_planning_sha","final_integration_sha","accepted_ticket_ids_json","accepted_commit_shas_json","evidence_hash")) != values: raise RuntimeError("tranche_checkpoint_reconciliation_required: completion evidence conflicts")
            row = conn.execute("SELECT * FROM tranche_checkpoint_evidence WHERE tranche_id=?", (identity["tranche_id"],)).fetchone()
            cp=(identity["tranche_id"],identity["feature_id"],completion["evidence_hash"],completion["final_integration_sha"],identity["repository_identity"],identity["planning_base_sha"],identity["planning_snapshot_hash"],json.dumps(identity["integration_commands"],separators=(",",":")),json.dumps(integration_results,sort_keys=True,separators=(",",":")),str(artifact),str(result["checkpoint_artifact_sha256"]),expected_decision)
            if row is None: conn.execute("INSERT INTO tranche_checkpoint_evidence(tranche_id,feature_id,completion_evidence_hash,final_integration_sha,repository_identity,planning_base_sha,planning_snapshot_hash,integration_commands_json,integration_results_json,checkpoint_artifact,checkpoint_artifact_sha256,decision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*cp,now))
            elif tuple(row[k] for k in ("tranche_id","feature_id","completion_evidence_hash","final_integration_sha","repository_identity","planning_base_sha","planning_snapshot_hash","integration_commands_json","integration_results_json","checkpoint_artifact","checkpoint_artifact_sha256","decision")) != cp: raise RuntimeError("tranche_checkpoint_reconciliation_required: checkpoint evidence conflicts")
            conn.execute("UPDATE scheduler_stage_claims SET side_effect_completed_at=?,result_json=?,updated_at=? WHERE claim_id=?", (now,encoded,now,claim_id))
            self._append_event(conn, entity_type="tranche", entity_id=str(identity["tranche_id"]), event_type="scheduler_stage_effect_completed", actor_id=owner, payload={"claim_id":claim_id,"stage":"tranche_checkpoint","decision":expected_decision,"completion_evidence_hash":completion["evidence_hash"],"checkpoint_artifact_sha256":result["checkpoint_artifact_sha256"]})
            return dict(conn.execute("SELECT * FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,)).fetchone())

    def record_tranche_completion(self, completion: dict[str, Any]) -> None:
        now = self._now()
        completion = dict(completion)
        completion["evidence_hash"] = _completion_evidence_hash(completion)
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM tranche_completion_evidence WHERE tranche_id=?", (completion["tranche_id"],)).fetchone()
            keys = ("tranche_id", "root_planning_sha", "final_integration_sha", "accepted_ticket_ids_json", "accepted_commit_shas_json", "evidence_hash")
            values = tuple(completion[k] for k in keys)
            if existing:
                if tuple(existing[k] for k in keys) != values: raise ValueError("conflicting tranche completion evidence")
                return
            conn.execute("INSERT INTO tranche_completion_evidence(tranche_id,root_planning_sha,final_integration_sha,accepted_ticket_ids_json,accepted_commit_shas_json,evidence_hash,completed_at) VALUES (?,?,?,?,?,?,?)", (*values, now))

    def record_tranche_completion_recheck(self, tranche_id: str, repository: Path) -> dict[str, Any]:
        """Append one successful correction re-check generation.

        The original completion row is never updated.  A re-check is eligible
        only when all supplemental correction work is resolved, and replaying
        the same canonical evidence returns the existing row.
        """
        if type(tranche_id) is not str or not tranche_id:
            raise ValueError("completion re-check tranche is invalid")
        repository = Path(repository).resolve(strict=True)
        tranche = self.connection.execute("SELECT feature_id FROM tranches WHERE id=?", (tranche_id,)).fetchone()
        if tranche is None:
            raise ValueError("completion re-check tranche is missing")
        plans = self.connection.execute("SELECT * FROM supplemental_correction_plans WHERE tranche_id=? ORDER BY ordinal", (tranche_id,)).fetchall()
        if not plans:
            raise ValueError("completion re-check requires correction plans")
        plan_ids = [str(row["correction_plan_id"]) for row in plans]
        ticket_rows = self.connection.execute("""
            SELECT sct.correction_plan_id,sct.ticket_id,t.state,ae.accepted_commit_sha,
                   sc.feature_id,sc.tranche_id,sc.repository_identity,sc.base_sha,sc.snapshot_hash
            FROM supplemental_correction_tickets sct
            JOIN supplemental_correction_plans sc ON sc.correction_plan_id=sct.correction_plan_id
            JOIN tickets t ON t.id=sct.ticket_id
            LEFT JOIN accepted_evidence ae ON ae.ticket_id=t.id
            WHERE sct.correction_plan_id IN (%s) ORDER BY sc.ordinal,sct.ordinal
        """ % ",".join("?" for _ in plan_ids), plan_ids).fetchall()
        if len(ticket_rows) != sum(self.connection.execute("SELECT COUNT(*) FROM supplemental_correction_tickets WHERE correction_plan_id=?", (plan_id,)).fetchone()[0] for plan_id in plan_ids):
            raise ValueError("completion re-check correction membership is incomplete")
        if not ticket_rows:
            raise ValueError("completion re-check requires correction tickets")
        commits: list[str] = []
        ticket_ids: list[str] = []
        provenance: tuple[str, str, str] | None = None
        for row in ticket_rows:
            current_provenance = (row["repository_identity"], row["base_sha"], row["snapshot_hash"])
            if row["feature_id"] != tranche["feature_id"] or row["tranche_id"] != tranche_id or current_provenance[0] != str(repository) or not all(isinstance(value, str) and value for value in current_provenance):
                raise ValueError("completion re-check correction provenance mismatch")
            if provenance is None:
                provenance = (str(current_provenance[0]), str(current_provenance[1]), str(current_provenance[2]))
            elif provenance != tuple(str(value) for value in current_provenance):
                raise ValueError("completion re-check correction provenance conflicts")
            if row["state"] != CanonicalState.DONE.value or not isinstance(row["accepted_commit_sha"], str) or not row["accepted_commit_sha"]:
                raise ValueError("completion re-check has unresolved correction work")
            ticket_ids.append(str(row["ticket_id"])); commits.append(str(row["accepted_commit_sha"]))
        assert provenance is not None
        head_result = subprocess.run(("git", "show-ref", "--verify", "--hash", f"refs/local-first/tranches/{tranche_id}/integration-head"), cwd=repository, text=True, capture_output=True)
        if head_result.returncode:
            raise ValueError("completion re-check integration head is unavailable")
        head = subprocess.run(("git", "rev-parse", "--verify", head_result.stdout.strip() + "^{commit}"), cwd=repository, text=True, capture_output=True, check=True).stdout.strip()
        for commit in commits:
            if subprocess.run(("git", "merge-base", "--is-ancestor", commit, head), cwd=repository, text=True, capture_output=True).returncode != 0:
                raise ValueError("completion re-check accepted commit is outside integration lineage")
        previous = self.connection.execute("SELECT * FROM tranche_completion_rechecks WHERE tranche_id=? ORDER BY generation DESC LIMIT 1", (tranche_id,)).fetchone()
        if previous is not None and previous["current_integration_sha"] == head and json.loads(previous["correction_plan_ids_json"]) == plan_ids and json.loads(previous["accepted_ticket_ids_json"]) == ticket_ids and json.loads(previous["accepted_commit_shas_json"]) == commits:
            return dict(previous)
        original = self.connection.execute("SELECT evidence_hash FROM tranche_completion_evidence WHERE tranche_id=?", (tranche_id,)).fetchone()
        if previous is None:
            if original is None:
                raise ValueError("completion re-check previous completion is missing")
            previous_generation, previous_hash = 0, str(original["evidence_hash"])
        else:
            previous_generation, previous_hash = int(previous["generation"]), str(previous["evidence_hash"])
        generation = previous_generation + 1
        payload = {"tranche_id": tranche_id, "generation": generation, "previous_generation": previous_generation, "previous_evidence_hash": previous_hash, "correction_plan_ids": plan_ids, "accepted_ticket_ids": ticket_ids, "accepted_commit_shas": commits, "current_integration_sha": head, "repository_identity": provenance[0], "repo_base_sha": provenance[1], "repo_snapshot_hash": provenance[2], "unresolved_correction_count": 0, "status": "recheck_passed"}
        evidence_hash = _hash_recheck_payload(payload)
        expected_key = "tranche-recheck:v1:" + evidence_hash
        with self._transaction() as conn:
            previous = conn.execute("SELECT * FROM tranche_completion_rechecks WHERE idempotency_key=?", (expected_key,)).fetchone()
            if previous is not None:
                return dict(previous)
            values = (tranche_id, generation, previous_generation, previous_hash,
                      json.dumps(plan_ids, separators=(",", ":")), json.dumps(ticket_ids, separators=(",", ":")),
                      json.dumps(commits, separators=(",", ":")), head, provenance[0], provenance[1], provenance[2], 0, "recheck_passed", evidence_hash, expected_key, self._now())
            conn.execute("INSERT INTO tranche_completion_rechecks(tranche_id,generation,previous_generation,previous_evidence_hash,correction_plan_ids_json,accepted_ticket_ids_json,accepted_commit_shas_json,current_integration_sha,repository_identity,repo_base_sha,repo_snapshot_hash,unresolved_correction_count,status,evidence_hash,idempotency_key,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            return dict(conn.execute("SELECT * FROM tranche_completion_rechecks WHERE idempotency_key=?", (expected_key,)).fetchone())

    def tranche_completion_rechecks(self, tranche_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM tranche_completion_rechecks WHERE tranche_id=? ORDER BY generation", (tranche_id,)).fetchall()]

    def latest_tranche_completion(self, tranche_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM tranche_completion_rechecks WHERE tranche_id=? ORDER BY generation DESC LIMIT 1", (tranche_id,)).fetchone()
        if row is not None:
            return {"kind": "recheck", **dict(row)}
        row = self.connection.execute("SELECT * FROM tranche_completion_evidence WHERE tranche_id=?", (tranche_id,)).fetchone()
        return {"kind": "completion", **dict(row)} if row is not None else None

    def materialize_next_tranche(self, *, feature: Any, tranche: Any, plan: Any, completion: dict[str, Any]) -> tuple[str, ...]:
        """Atomically freeze completion and create next-tranche draft tickets."""
        from .decomposition import generated_card_payload
        now = self._now()
        with self._transaction() as conn:
            active = conn.execute("SELECT * FROM tranches WHERE feature_id=? AND status='active' ORDER BY ordinal", (feature.id,)).fetchall()
            if len(active) != 1 or int(active[0]["ordinal"]) + 1 != int(conn.execute("SELECT ordinal FROM tranches WHERE id=?", (tranche.id,)).fetchone()[0]):
                raise ValueError("active tranche handoff conflict")
            existing = conn.execute("SELECT * FROM tranche_completion_evidence WHERE tranche_id=?", (active[0]["id"],)).fetchone()
            keys = ("tranche_id", "root_planning_sha", "final_integration_sha", "accepted_ticket_ids_json", "accepted_commit_shas_json", "evidence_hash")
            values = (active[0]["id"], completion["root_planning_sha"], completion["final_integration_sha"], completion["accepted_ticket_ids_json"], completion["accepted_commit_shas_json"], completion["evidence_hash"])
            if existing:
                if tuple(existing[k] for k in keys) != values: raise ValueError("conflicting tranche completion evidence")
            else:
                conn.execute("INSERT INTO tranche_completion_evidence(tranche_id,root_planning_sha,final_integration_sha,accepted_ticket_ids_json,accepted_commit_shas_json,evidence_hash,completed_at) VALUES (?,?,?,?,?,?,?)", (*values, now))
            conn.execute("UPDATE tranches SET status='completed' WHERE id=?", (active[0]["id"],))
            target = conn.execute("SELECT * FROM tranches WHERE feature_id=? AND id=?", (feature.id, tranche.id)).fetchone()
            if target is None or int(target["ordinal"]) != int(active[0]["ordinal"]) + 1 or target["status"] not in {"planned", "active"}: raise ValueError("next tranche is missing or conflicting")
            if not all(isinstance(value, str) and value for value in (plan.repository_identity, plan.repo_base_sha, plan.repo_snapshot_hash)):
                raise ValueError("next tranche repository provenance missing")
            conn.execute("UPDATE tranches SET status='active', base_sha=? WHERE id=?", (plan.repo_base_sha, tranche.id))
            created = []
            for t in tranche.microtickets:
                existing_ticket = conn.execute("SELECT id FROM tickets WHERE id=?", (t.ticket_id,)).fetchone()
                if existing_ticket: continue
                q = t.contract()
                conn.execute("INSERT INTO tickets(id,feature_id,tranche_id,title,objective,criterion_ids_json,primary_symbol,allowed_files_json,create_files_json,new_test_files_json,forbidden_changes_json,patch_budget_json,verification_json,risk,review_required,max_attempts,dependencies_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (t.ticket_id, feature.id, tranche.id, t.ticket_id, q["objective"], json.dumps(q["criterion_ids"]), q["primary_symbol"], json.dumps(q["allowed_files"]), json.dumps(q.get("create_files", [])), json.dumps(q.get("new_test_files", [])), json.dumps(q["forbidden_changes"]), json.dumps(q["patch_budget"]), json.dumps(q["verification"]), q["risk"], int(t.review_required), t.max_attempts, json.dumps(q["dependencies"]), "draft", now, now))
                for cid in t.criterion_ids: conn.execute("INSERT INTO ticket_criteria VALUES (?,?)", (t.ticket_id, cid))
                payload = generated_card_payload(feature, tranche, t, repository_identity=str(plan.repository_identity), repo_base_sha=str(plan.repo_base_sha), repo_snapshot_hash=str(plan.repo_snapshot_hash))
                event_id = self._append_event(conn, entity_type="ticket", entity_id=t.ticket_id, event_type="generated_microticket_created", actor_id="controller", to_state="draft", payload={"feature_id": feature.id, "tranche_id": tranche.id, "projection_key": payload["projection_key"]})
                self._enqueue_generated_create_projection_in_transaction(conn, ticket_id=t.ticket_id, event_id=event_id, payload=payload, idempotency_key=payload["projection_key"])
                created.append(t.ticket_id)
            return tuple(created)
