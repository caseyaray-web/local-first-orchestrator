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
    row = ledger.connection.execute("""
        SELECT t.id ticket_id,t.feature_id,t.tranche_id,tr.feature_id tranche_feature_id,tr.status tranche_status,
               p.id plan_id,p.status plan_status,p.repository_identity,p.repo_base_sha,p.repo_snapshot_hash,p.repo_snapshot_manifest_json,
               b.acknowledged_at,b.external_task_id,b.terminal_error,b.operation,e.entity_type,e.entity_id,e.event_type
        FROM tickets t JOIN tranches tr ON tr.id=t.tranche_id
        LEFT JOIN decomposition_plans p ON p.feature_id=t.feature_id AND p.status='active'
        LEFT JOIN events e ON e.entity_id=t.id AND e.entity_type='ticket' AND e.event_type='generated_microticket_created'
        LEFT JOIN board_projection_outbox b ON b.ticket_id=t.id AND b.event_id=e.id AND b.operation='create_microticket'
        WHERE t.id=?
    """, (ticket_id,)).fetchone()
    if row is None: raise GeneratedActivationError('ticket_not_found')
    if (row['entity_type'],row['entity_id'],row['event_type']) != ('ticket',ticket_id,'generated_microticket_created') or row['feature_id'] != row['tranche_feature_id']:
        raise GeneratedActivationError('invalid_generated_provenance')
    if row['tranche_status'] != 'active' or not row['plan_id']:
        raise GeneratedActivationError('inactive_or_wrong_tranche')
    if row['operation'] != 'create_microticket' or row['acknowledged_at'] is None:
        raise GeneratedActivationError('projection_incomplete')
    if row['terminal_error'] is not None: raise GeneratedActivationError('projection_terminal_failed')
    if not isinstance(row['external_task_id'],str) or not row['external_task_id']:
        raise GeneratedActivationError('missing_external_task_id')
    values=tuple(row[x] for x in ('repository_identity','repo_base_sha','repo_snapshot_hash','repo_snapshot_manifest_json'))
    if not all(values): raise GeneratedActivationError('missing_repository_provenance')
    try: manifest=json.loads(row['repo_snapshot_manifest_json'])
    except (TypeError,json.JSONDecodeError): raise GeneratedActivationError('invalid_snapshot_provenance')
    if canonical_json(manifest) != row['repo_snapshot_manifest_json'] or hashlib.sha256(row['repo_snapshot_manifest_json'].encode()).hexdigest()!=row['repo_snapshot_hash'] or (manifest.get('repository_identity'),manifest.get('repo_base_sha')) != (row['repository_identity'],row['repo_base_sha']):
        raise GeneratedActivationError('invalid_snapshot_provenance')
    try: repository=runtime_config.canonical_repository(runtime_config.repository)
    except Exception as exc: raise GeneratedActivationError('repository_identity_mismatch') from exc
    if str(repository) != row['repository_identity']: raise GeneratedActivationError('repository_identity_mismatch')
    try:
        sha=subprocess.run(('git','rev-parse','--verify',str(row['repo_base_sha'])+'^{commit}'),cwd=repository,text=True,capture_output=True,check=True).stdout.strip()
    except subprocess.CalledProcessError as exc: raise GeneratedActivationError('missing_base_commit') from exc
    if sha != row['repo_base_sha']: raise GeneratedActivationError('base_sha_mismatch')
    return GeneratedActivationContext(ticket_id,str(row['feature_id']),str(row['tranche_id']),str(row['plan_id']),row['external_task_id'],row['repository_identity'],repository,sha,row['repo_snapshot_hash'])
