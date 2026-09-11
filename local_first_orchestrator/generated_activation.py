from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .controller import RuntimeConfig
from .ledger import Ledger
from .repository_snapshot import canonical_json


class GeneratedActivationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GeneratedActivationContext:
    ticket_id: str
    feature_id: str
    tranche_id: str
    plan_id: str
    external_task_id: str
    repository_identity: str
    repository_path: Path
    starting_sha: str
    repo_snapshot_hash: str


def resolve_generated_activation_context(ticket_id: str, runtime_config: RuntimeConfig, ledger: Ledger) -> GeneratedActivationContext:
    rows = ledger.connection.execute("""
        SELECT t.id ticket_id,t.feature_id,t.tranche_id,tr.feature_id tranche_feature_id,tr.status tranche_status,
               p.id plan_id,p.status plan_status,p.repository_identity,p.repo_base_sha,p.repo_snapshot_hash,p.repo_snapshot_manifest_json,
               m.feature_id successor_feature_id,m.repository_identity successor_repository_identity,
               m.repo_base_sha successor_repo_base_sha,m.repo_snapshot_hash successor_repo_snapshot_hash,
               m.ticket_ids_json successor_ticket_ids_json,
               b.acknowledged_at,b.external_task_id,b.terminal_error,b.operation,e.entity_type,e.entity_id,e.event_type
        FROM tickets t JOIN tranches tr ON tr.id=t.tranche_id
        LEFT JOIN decomposition_plans p ON p.feature_id=t.feature_id AND p.status='active'
        LEFT JOIN next_tranche_materializations m ON m.successor_tranche_id=t.tranche_id
        LEFT JOIN events e ON e.entity_id=t.id AND e.entity_type='ticket' AND e.event_type='generated_microticket_created'
        LEFT JOIN board_projection_outbox b ON b.ticket_id=t.id AND b.event_id=e.id AND b.operation='create_microticket'
        WHERE t.id=?
    """, (ticket_id,)).fetchall()
    if not rows:
        raise GeneratedActivationError('ticket_not_found')
    if len(rows) != 1:
        raise GeneratedActivationError('ambiguous_generated_provenance')
    row = rows[0]
    if (row['entity_type'],row['entity_id'],row['event_type']) != ('ticket',ticket_id,'generated_microticket_created') or row['feature_id'] != row['tranche_feature_id']:
        raise GeneratedActivationError('invalid_generated_provenance')
    if row['tranche_status'] != 'active' or not row['plan_id']:
        raise GeneratedActivationError('inactive_or_wrong_tranche')
    if row['operation'] != 'create_microticket' or row['acknowledged_at'] is None:
        raise GeneratedActivationError('projection_incomplete')
    if row['terminal_error'] is not None: raise GeneratedActivationError('projection_terminal_failed')
    if not isinstance(row['external_task_id'],str) or not row['external_task_id']:
        raise GeneratedActivationError('missing_external_task_id')
    if row['successor_repo_base_sha'] is not None:
        if row['successor_feature_id'] != row['feature_id']:
            raise GeneratedActivationError('invalid_successor_provenance')
        try:
            successor_ticket_ids = json.loads(str(row['successor_ticket_ids_json']))
        except (TypeError,json.JSONDecodeError) as exc:
            raise GeneratedActivationError('invalid_successor_provenance') from exc
        if not isinstance(successor_ticket_ids,list) or ticket_id not in successor_ticket_ids:
            raise GeneratedActivationError('invalid_successor_provenance')
        repository_identity = row['successor_repository_identity']
        repo_base_sha = row['successor_repo_base_sha']
        repo_snapshot_hash = row['successor_repo_snapshot_hash']
        if not all(isinstance(value,str) and value for value in (repository_identity,repo_base_sha,repo_snapshot_hash)):
            raise GeneratedActivationError('missing_repository_provenance')
    else:
        values=tuple(row[x] for x in ('repository_identity','repo_base_sha','repo_snapshot_hash','repo_snapshot_manifest_json'))
        if not all(values): raise GeneratedActivationError('missing_repository_provenance')
        try: manifest=json.loads(row['repo_snapshot_manifest_json'])
        except (TypeError,json.JSONDecodeError): raise GeneratedActivationError('invalid_snapshot_provenance')
        if canonical_json(manifest) != row['repo_snapshot_manifest_json'] or hashlib.sha256(row['repo_snapshot_manifest_json'].encode()).hexdigest()!=row['repo_snapshot_hash'] or (manifest.get('repository_identity'),manifest.get('repo_base_sha')) != (row['repository_identity'],row['repo_base_sha']):
            raise GeneratedActivationError('invalid_snapshot_provenance')
        repository_identity = row['repository_identity']
        repo_base_sha = row['repo_base_sha']
        repo_snapshot_hash = row['repo_snapshot_hash']
    try: repository=runtime_config.canonical_repository(runtime_config.repository)
    except Exception as exc: raise GeneratedActivationError('repository_identity_mismatch') from exc
    if str(repository) != repository_identity: raise GeneratedActivationError('repository_identity_mismatch')
    try:
        sha=subprocess.run(('git','rev-parse','--verify',str(repo_base_sha)+'^{commit}'),cwd=repository,text=True,capture_output=True,check=True).stdout.strip()
    except subprocess.CalledProcessError as exc: raise GeneratedActivationError('missing_base_commit') from exc
    if sha != repo_base_sha: raise GeneratedActivationError('base_sha_mismatch')
    return GeneratedActivationContext(ticket_id,str(row['feature_id']),str(row['tranche_id']),str(row['plan_id']),row['external_task_id'],str(repository_identity),repository,sha,str(repo_snapshot_hash))


@dataclass(frozen=True)
class GeneratedActivationResult:
    status: str
    ticket_id: str
    repository_path: Path | None = None
    starting_sha: str | None = None
    readiness_status: str | None = None


def _correction_plan_id(ticket_id: str, ledger: Ledger) -> str | None:
    rows = ledger.connection.execute("""
        SELECT sct.correction_plan_id
        FROM supplemental_correction_tickets sct
        JOIN supplemental_correction_plans sc ON sc.correction_plan_id=sct.correction_plan_id
        WHERE sct.ticket_id=?
    """, (ticket_id,)).fetchall()
    if len(rows) > 1:
        raise GeneratedActivationError("ambiguous_correction_provenance")
    return str(rows[0]["correction_plan_id"]) if rows else None


def activate_generated_ticket(ticket_id: str, runtime_config: RuntimeConfig, ledger: Ledger) -> GeneratedActivationResult:
    """Activate ordinary generated tickets or correction tickets by persisted provenance."""
    correction_plan_id = _correction_plan_id(ticket_id, ledger)
    if correction_plan_id is not None:
        from .corrections import CorrectionService
        try:
            repository = runtime_config.canonical_repository(runtime_config.repository)
        except Exception as exc:
            raise GeneratedActivationError("repository_identity_mismatch") from exc
        was_ready = ledger.get_ticket(ticket_id)["state"] == "ready_local"
        try:
            head = CorrectionService(ledger, repository).activate(ticket_id)
        except (KeyError, ValueError) as exc:
            raise GeneratedActivationError(str(exc)) from exc
        return GeneratedActivationResult("already_activated" if was_ready else "activated_ready", ticket_id, repository, head, "ready")
    context = resolve_generated_activation_context(ticket_id, runtime_config, ledger)
    existing = ledger.connection.execute("SELECT repository_path,starting_sha FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
    expected = (str(context.repository_path), context.starting_sha)
    if existing is None:
        ledger.bind_runtime(ticket_id, *expected)
    elif (existing['repository_path'], existing['starting_sha']) != expected:
        return GeneratedActivationResult('binding_conflict', ticket_id, context.repository_path, context.starting_sha)
    readiness = ledger.admit_ticket_if_ready(ticket_id)
    if ledger.get_ticket(ticket_id)['state'] == 'ready_local':
        return GeneratedActivationResult('already_activated' if existing is not None else 'activated_ready', ticket_id, context.repository_path, context.starting_sha, readiness.status)
    if readiness.status == 'waiting_on_dependencies':
        return GeneratedActivationResult('activated_waiting', ticket_id, context.repository_path, context.starting_sha, readiness.status)
    return GeneratedActivationResult('readiness_failed', ticket_id, context.repository_path, context.starting_sha, readiness.status)
