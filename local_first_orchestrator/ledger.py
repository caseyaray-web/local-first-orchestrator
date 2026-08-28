from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from threading import RLock
from pathlib import Path
from typing import Any, Iterator

from .adapters import BoardAdapter
from .readiness import ReadinessError, validate_ticket
from .states import CanonicalState, validate_transition
from .ticket import MicroTicket


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
    idempotency_key TEXT NOT NULL UNIQUE,
    queued_at INTEGER NOT NULL,
    acknowledged_at INTEGER,
    PRIMARY KEY(ticket_id, event_id)
);
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
    starting_sha TEXT NOT NULL, ownership_verified INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_stages (
    ticket_id TEXT NOT NULL REFERENCES tickets(id), stage TEXT NOT NULL, detail TEXT NOT NULL,
    created_at INTEGER NOT NULL, PRIMARY KEY(ticket_id, stage)
);
CREATE TABLE IF NOT EXISTS model_stage_artifacts (
    ticket_id TEXT NOT NULL REFERENCES tickets(id), attempt_number INTEGER NOT NULL,
    stage TEXT NOT NULL, purpose TEXT NOT NULL, adapter TEXT NOT NULL,
    request_hash TEXT NOT NULL, response_artifact TEXT NOT NULL, worktree_path TEXT NOT NULL,
    base_sha TEXT NOT NULL, diff_hash TEXT NOT NULL, completed_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed', UNIQUE(ticket_id, attempt_number, stage)
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
CREATE TRIGGER IF NOT EXISTS events_immutable_update
BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_immutable_delete
BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
"""


class Ledger:
    """Standalone SQLite ledger. It deliberately has no Hermes imports."""

    def __init__(self, database: Path) -> None:
        self.database = Path(database)
        if self.database.name == "kanban.db" or self.database.resolve(strict=False).name == "kanban.db":
            raise ValueError("ledger database must be distinct from Hermes kanban.db")
        self.connection = sqlite3.connect(self.database, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._lock = RLock()

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        self.connection.executescript(_SCHEMA)
        # Phase 2 is additive: preserve Phase 1 ledgers already created.
        ticket_columns = {
            "criterion_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "primary_symbol": "TEXT",
            "allowed_files_json": "TEXT NOT NULL DEFAULT '[]'",
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
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_model_calls_reservation ON model_calls(reservation_id)")
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
                "INSERT INTO tickets(id, external_id, title, objective, criterion_ids_json, primary_symbol, allowed_files_json, forbidden_changes_json, patch_budget_json, verification_json, risk, review_required, max_attempts, dependencies_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, external_id, title, contract.get("objective"), json.dumps(contract.get("criterion_ids", [])), contract.get("primary_symbol"), json.dumps(contract.get("allowed_files", [])), json.dumps(contract.get("forbidden_changes", [])), json.dumps(contract.get("patch_budget", {})), json.dumps(contract.get("verification", {})), contract.get("risk"), int(contract.get("review_required", True)), contract.get("max_attempts", 2), json.dumps(contract.get("dependencies", [])), state.value, now, now),
            )
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
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="state_transition", actor_id=actor_id, from_state=current.value, to_state=target.value, payload=payload)

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

    def bind_runtime(self, ticket_id: str, repository_path: str, starting_sha: str) -> None:
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO runtime_bindings(ticket_id, repository_path, starting_sha, ownership_verified, created_at) VALUES (?, ?, ?, 1, ?)", (ticket_id, repository_path, starting_sha, self._now()))

    def runtime_binding(self, ticket_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
        if row is None: raise KeyError(f"no runtime binding for {ticket_id}")
        return dict(row)

    def record_runtime_stage(self, ticket_id: str, stage: str, detail: str) -> bool:
        with self._transaction() as conn:
            try: conn.execute("INSERT INTO runtime_stages(ticket_id, stage, detail, created_at) VALUES (?, ?, ?, ?)", (ticket_id, stage, detail, self._now()))
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

    def stage_rows(self, ticket_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM model_stage_artifacts WHERE ticket_id=? ORDER BY attempt_number, completed_at", (ticket_id,))]

    def enqueue_evidence_comment(self, ticket_id: str, event_id: int, evidence: str) -> dict[str, Any]:
        import re
        row=self.get_ticket(ticket_id); now=self._now(); key=f"evidence-comment:{ticket_id}:{event_id}"
        operation_id=hashlib.sha256(key.encode()).hexdigest()[:32]
        safe=re.sub(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+",r"\1=[REDACTED]",evidence)[:800]
        payload=(f"Local-first ticket {ticket_id} | state={row['state']} | {safe}\n<!-- local-first-comment:{operation_id} -->")[:1000]
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO evidence_comment_outbox(operation_id,ticket_id,event_id,external_task_id,operation_kind,idempotency_key,payload,status,created_at,updated_at) VALUES (?,?,?,?,? ,?,?, 'pending',?,?)",(operation_id,ticket_id,event_id,str(row.get('external_id') or ticket_id),'evidence_comment',key,payload,now,now))
        return self.comment_outbox(operation_id)

    def comment_outbox(self, operation_id: str) -> dict[str, Any]:
        row=self.connection.execute("SELECT * FROM evidence_comment_outbox WHERE operation_id=?",(operation_id,)).fetchone()
        if row is None: raise KeyError(operation_id)
        return dict(row)

    def claim_comment(self, operation_id: str, owner: str, *, lease_seconds: int = 60, now: int | None = None) -> bool:
        now=self._now() if now is None else now
        with self._transaction() as conn:
            changed=conn.execute("UPDATE evidence_comment_outbox SET status='delivering',lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE operation_id=? AND status IN ('pending','retryable') AND (next_attempt_at IS NULL OR next_attempt_at<=?)",(owner,now+lease_seconds,now,operation_id,now))
            return changed.rowcount==1

    @staticmethod
    def _safe_comment_error(error: str) -> str:
        import re
        safe = re.sub(
            r"(?i)(password|token|secret|api[_-]?key|authorization|cookie)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            str(error),
        )
        safe = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", safe)
        return safe[:500]

    def mark_comment_delivered(self, operation_id: str, owner: str, *, now: int | None = None) -> bool:
        now=self._now() if now is None else now
        with self._transaction() as conn:
            row=conn.execute("SELECT status,lease_owner,lease_expires_at,terminal_owner FROM evidence_comment_outbox WHERE operation_id=?",(operation_id,)).fetchone()
            if row is None: return False
            if row['status']=='delivered': return row['terminal_owner']==owner
            if row['status']!='delivering' or row['lease_owner']!=owner or row['lease_expires_at'] is None or row['lease_expires_at']<=now: return False
            return conn.execute("UPDATE evidence_comment_outbox SET status='delivered',delivered_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=NULL,last_error=NULL,terminal_owner=? WHERE operation_id=? AND status='delivering' AND lease_owner=? AND lease_expires_at>?",(now,now,owner,operation_id,owner,now)).rowcount==1

    def mark_comment_retryable(self, operation_id: str, owner: str, error: str, *, next_attempt_at: int | None = None, now: int | None = None) -> bool:
        now=self._now() if now is None else now
        next_attempt_at = now if next_attempt_at is None else next_attempt_at
        with self._transaction() as conn:
            return conn.execute("UPDATE evidence_comment_outbox SET status='retryable',last_error=?,lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,updated_at=?,delivered_at=NULL,terminal_owner=NULL WHERE operation_id=? AND status='delivering' AND lease_owner=? AND lease_expires_at>?",(self._safe_comment_error(error),next_attempt_at,now,operation_id,owner,now)).rowcount==1

    def mark_comment_permanently_failed(self, operation_id: str, owner: str, error: str, *, now: int | None = None) -> bool:
        now=self._now() if now is None else now
        with self._transaction() as conn:
            row=conn.execute("SELECT status,lease_owner,lease_expires_at,terminal_owner FROM evidence_comment_outbox WHERE operation_id=?",(operation_id,)).fetchone()
            if row is None: return False
            if row['status']=='permanently_failed': return row['terminal_owner']==owner
            return conn.execute("UPDATE evidence_comment_outbox SET status='permanently_failed',last_error=?,lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=NULL,updated_at=?,terminal_owner=? WHERE operation_id=? AND status='delivering' AND lease_owner=? AND lease_expires_at>?",(self._safe_comment_error(error),now,owner,operation_id,owner,now)).rowcount==1

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
            changed = conn.execute("UPDATE tickets SET state=?, lease_owner=?, lease_expires_at=?, updated_at=? WHERE id=? AND state=? AND (lease_expires_at IS NULL OR lease_expires_at<=?)", (CanonicalState.IMPLEMENTING.value, owner, now+lease_seconds, now, ticket_id, CanonicalState.READY_LOCAL.value, now))
            if changed.rowcount != 1: return False
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="lease_claimed", actor_id=owner, from_state=CanonicalState.READY_LOCAL.value, to_state=CanonicalState.IMPLEMENTING.value, payload={"lease_expires_at":now+lease_seconds})
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

    def create_triaged_children(self, parent_ticket_id: str, children: list[tuple[str, MicroTicket, str]], *, classification: str, root_cause_evidence: str) -> list[str]:
        """Atomically create controller-owned children; no board/model I/O occurs here."""
        now = self._now()
        with self._transaction() as conn:
            parent = conn.execute("SELECT state, depth FROM tickets WHERE id = ?", (parent_ticket_id,)).fetchone()
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
                return [by_fingerprint[fingerprint] for _, _, fingerprint in children]
            parent_criteria = set(json.loads(self.get_ticket(parent_ticket_id)["criterion_ids_json"]))
            accepted_rows = conn.execute(
                "SELECT criterion_id FROM criterion_statuses WHERE ticket_id = ? AND status = 'accepted'",
                (parent_ticket_id,),
            ).fetchall()
            unresolved = parent_criteria - {str(row["criterion_id"]) for row in accepted_rows}
            created: list[str] = []
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
                    "INSERT INTO tickets(id, parent_ticket_id, depth, title, objective, criterion_ids_json, primary_symbol, allowed_files_json, forbidden_changes_json, patch_budget_json, verification_json, risk, review_required, max_attempts, dependencies_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (child_id, parent_ticket_id, int(parent["depth"]) + 1, title, contract.get("objective"), json.dumps(contract.get("criterion_ids", [])), contract.get("primary_symbol"), json.dumps(contract.get("allowed_files", [])), json.dumps(contract.get("forbidden_changes", [])), json.dumps(contract.get("patch_budget", {})), json.dumps(contract.get("verification", {})), contract.get("risk"), int(contract.get("review_required", True)), contract.get("max_attempts", 2), json.dumps(contract.get("dependencies", [])), CanonicalState.READY_LOCAL.value, now, now),
                )
                conn.execute(
                    "INSERT INTO triage_children(parent_ticket_id, child_ticket_id, fingerprint, created_at) VALUES (?, ?, ?, ?)",
                    (parent_ticket_id, child_id, fingerprint, now),
                )
                self._append_event(conn, entity_type="ticket", entity_id=child_id, event_type="triage_child_created", actor_id="controller", to_state=CanonicalState.READY_LOCAL.value, payload={"parent_ticket_id": parent_ticket_id, "fingerprint": fingerprint, "resolves_criteria": list(child_ticket.criterion_ids)})
                created.append(child_id)
            self._append_event(conn, entity_type="ticket", entity_id=parent_ticket_id, event_type="triage_children_created", actor_id="controller", payload={"classification": classification, "root_cause_evidence": root_cause_evidence, "child_ids": created})
            return created

    def pause(self, actor_id: str, *, reason: str) -> None:
        self._set_pause(True, actor_id, reason)

    def resume(self, actor_id: str, *, reason: str) -> None:
        self._set_pause(False, actor_id, reason)

    def _set_pause(self, paused: bool, actor_id: str, reason: str) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE controller_state SET paused = ?, updated_at = ? WHERE id = 1", (int(paused), self._now()))
            self._append_event(conn, entity_type="controller", entity_id="controller", event_type="paused" if paused else "resumed", actor_id=actor_id, payload={"reason": reason})

    def status(self) -> dict[str, Any]:
        paused = self.connection.execute("SELECT paused FROM controller_state WHERE id = 1").fetchone()
        states = self.connection.execute("SELECT state, COUNT(*) AS count FROM tickets GROUP BY state ORDER BY state").fetchall()
        return {"paused": bool(paused["paused"]) if paused else False, "tickets": {row["state"]: row["count"] for row in states}}

    def project_ticket(self, ticket_id: str, adapter: BoardAdapter) -> bool:
        row = self.connection.execute("SELECT id, state FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise KeyError(ticket_id)
        event = self.connection.execute("SELECT id FROM events WHERE entity_type = 'ticket' AND entity_id = ? AND to_state = ? ORDER BY id DESC LIMIT 1", (ticket_id, row["state"])).fetchone()
        if event is None:
            return False
        event_id = int(event["id"])
        existing = self.connection.execute("SELECT 1 FROM board_projections WHERE ticket_id=? AND event_id=?", (ticket_id,event_id)).fetchone()
        outbox = self.connection.execute("SELECT state, idempotency_key FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id,event_id)).fetchone()
        did_work = existing is None
        if existing is None:
            with self._transaction() as conn:
                conn.execute("INSERT OR IGNORE INTO board_projection_outbox(ticket_id,event_id,state,idempotency_key,queued_at) VALUES (?,?,?,?,?)", (ticket_id,event_id,row["state"],f"ticket-event:{event_id}",self._now()))
            self.record_runtime_stage(ticket_id,"projection_enqueued",f"ticket-event:{event_id}")
            outbox = self.connection.execute("SELECT state, idempotency_key FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket_id,event_id)).fetchone()
            if outbox is None: return False
            if self.connection.execute("SELECT acknowledged_at FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",(ticket_id,event_id)).fetchone()[0] is None:
                adapter.set_state(ticket_id, CanonicalState(outbox["state"]), idempotency_key=outbox["idempotency_key"])
                with self._transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO board_projections(ticket_id,event_id,state,projected_at) VALUES (?,?,?,?)",(ticket_id,event_id,outbox["state"],self._now()))
                    conn.execute("UPDATE board_projection_outbox SET acknowledged_at=? WHERE ticket_id=? AND event_id=?",(self._now(),ticket_id,event_id))
        if self.evidence_comment(ticket_id) is None:
            ticket=self.get_ticket(ticket_id)
            self.enqueue_evidence_comment(ticket_id,event_id,f"attempts={self.attempt_count(ticket_id)}; state={ticket['state']}")
        return did_work

    def record_accepted_evidence(self, ticket_id: str, accepted_commit_sha: str, diff_summary: str, validation_summary: str, *, local_reasoning: str | None = None) -> None:
        """Persist only checkpoint-safe accepted evidence; local reasoning is discarded."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO accepted_evidence(ticket_id, accepted_commit_sha, diff_summary, validation_summary, created_at) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, accepted_commit_sha, diff_summary, validation_summary, self._now()),
            )
            self._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="accepted_evidence_recorded", actor_id="controller", payload={"commit_sha": accepted_commit_sha})
