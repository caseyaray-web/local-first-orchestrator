"""Versioned SQLite store for plugin evidence and external-operation reconciliation.

This module intentionally persists no native Kanban lifecycle projection.  Native
board state is always observed through an adapter; this database holds only the
plugin's bounded membership, evidence, intent, budget, and operator records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any, Callable, Mapping

from .budgets import CATEGORIES, GENERAL_ATTEMPT, IMPLEMENTATION_ATTEMPTS, PAID_CAPACITY
from .contracts import (
    ActionResult,
    BoardSnapshot,
    CandidateIdentity,
    ConflictError,
    ManagedMember,
    OperationIntent,
    PauseIntent,
    validate_scope,
)

_SCHEMA_VERSION = 5
_TABLES = frozenset(
    {
        "schema_metadata",
        "managed_members",
        "candidates",
        "review_evidence",
        "operation_intents",
        "budget_events",
        "budget_reconciliation_evidence",
        "effect_observations",
        "operator_intents",
        "plan_proposals",
        "paid_release_run_bindings",
    }
)
_TABLE_COLUMNS = {
    "schema_metadata": (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)),
    "managed_members": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("task_id", "TEXT", 1, 3), ("role", "TEXT", 1, 0), ("generation", "INTEGER", 1, 0), ("finding_ids_json", "TEXT", 1, 0), ("work_association", "TEXT", 1, 0)),
    "candidates": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("content_identity", "TEXT", 1, 3), ("identity_json", "TEXT", 1, 0)),
    "review_evidence": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("candidate_content_identity", "TEXT", 1, 0), ("review_id", "TEXT", 1, 3), ("evidence_json", "TEXT", 1, 0)),
    "operation_intents": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("operation_key", "TEXT", 1, 3), ("intent_json", "TEXT", 1, 0)),
    "budget_events": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("event_id", "TEXT", 1, 3), ("source_kind", "TEXT", 1, 0), ("native_source_id", "TEXT", 1, 0), ("event_json", "TEXT", 1, 0)),
    "budget_reconciliation_evidence": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("event_id", "TEXT", 1, 3), ("evidence_kind", "TEXT", 1, 4), ("evidence_json", "TEXT", 1, 0)),
    "effect_observations": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("operation_key", "TEXT", 1, 3), ("observation_identity", "TEXT", 1, 4), ("observation_json", "TEXT", 1, 0)),
    "operator_intents": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("intent_json", "TEXT", 1, 0)),
    "plan_proposals": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("plan_id", "TEXT", 1, 3), ("evidence_json", "TEXT", 1, 0)),
    "paid_release_run_bindings": (("board_id", "TEXT", 1, 1), ("anchor_task_id", "TEXT", 1, 2), ("release_operation_key", "TEXT", 1, 3), ("task_id", "TEXT", 1, 0), ("member_generation", "INTEGER", 1, 0), ("request_identity", "TEXT", 1, 0), ("native_run_id", "TEXT", 1, 0), ("native_session_id", "TEXT", 1, 0), ("native_profile", "TEXT", 1, 0), ("run_classification", "TEXT", 1, 0), ("native_run_json", "TEXT", 1, 0), ("native_run_sha256", "TEXT", 1, 0)),
}
_REVIEW_EVIDENCE_FOREIGN_KEYS = (
    ("candidates", "board_id", "board_id"),
    ("candidates", "anchor_task_id", "anchor_task_id"),
    ("candidates", "candidate_content_identity", "content_identity"),
)
_PLAN_TRIGGERS = {
    "plan_proposals_immutable_update": "CREATE TRIGGER plan_proposals_immutable_update BEFORE UPDATE ON plan_proposals BEGIN SELECT RAISE(ABORT, 'plan evidence is immutable'); END",
    "plan_proposals_immutable_delete": "CREATE TRIGGER plan_proposals_immutable_delete BEFORE DELETE ON plan_proposals BEGIN SELECT RAISE(ABORT, 'plan evidence is immutable'); END",
}
_PAID_BINDING_TRIGGERS = {
    "paid_release_run_bindings_immutable_update": "CREATE TRIGGER paid_release_run_bindings_immutable_update BEFORE UPDATE ON paid_release_run_bindings BEGIN SELECT RAISE(ABORT, 'paid release run binding is immutable'); END",
    "paid_release_run_bindings_immutable_delete": "CREATE TRIGGER paid_release_run_bindings_immutable_delete BEFORE DELETE ON paid_release_run_bindings BEGIN SELECT RAISE(ABORT, 'paid release run binding is immutable'); END",
}
_RECONCILIATION_TRIGGER_SQL = {
    "budget_reconciliation_evidence_immutable_update": "CREATE TRIGGER budget_reconciliation_evidence_immutable_update BEFORE UPDATE ON budget_reconciliation_evidence BEGIN SELECT RAISE(ABORT, 'budget reconciliation evidence is immutable'); END",
    "budget_reconciliation_evidence_immutable_delete": "CREATE TRIGGER budget_reconciliation_evidence_immutable_delete BEFORE DELETE ON budget_reconciliation_evidence BEGIN SELECT RAISE(ABORT, 'budget reconciliation evidence is immutable'); END",
    "effect_observations_immutable_update": "CREATE TRIGGER effect_observations_immutable_update BEFORE UPDATE ON effect_observations BEGIN SELECT RAISE(ABORT, 'effect observation evidence is immutable'); END",
    "effect_observations_immutable_delete": "CREATE TRIGGER effect_observations_immutable_delete BEFORE DELETE ON effect_observations BEGIN SELECT RAISE(ABORT, 'effect observation evidence is immutable'); END",
}


class EvidenceStoreError(RuntimeError):
    """Base error for unusable or invalid evidence-store state."""


class SchemaError(EvidenceStoreError):
    """The configured database is missing, corrupt, or has an unknown schema."""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("evidence must be finite JSON-compatible data") from error


def _decode(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise SchemaError("persisted evidence JSON is malformed") from error


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _canonical_identity(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha256(value: Any) -> str:
    return "sha256:" + _canonical_identity(value)


def _paid_release_sources(connection: sqlite3.Connection, board: str, anchor: str, key: str) -> tuple[OperationIntent, dict[str, Any], sqlite3.Row]:
    """Reconstruct and cross-check the complete immutable reservation source."""
    op = connection.execute("SELECT board_id,anchor_task_id,operation_key,intent_json FROM operation_intents WHERE board_id=? AND anchor_task_id=? AND operation_key=?", (board, anchor, key)).fetchone()
    if op is None:
        raise ValueError("paid release intent is missing")
    if (op["board_id"] != board or op["anchor_task_id"] != anchor or op["operation_key"] != key
            or not all(_nonempty_string(op[name]) for name in ("board_id", "anchor_task_id", "operation_key", "intent_json"))):
        raise ValueError("paid release intent row identity is malformed")
    raw_intent = op["intent_json"]
    intent_data = _decode(raw_intent)
    if not isinstance(intent_data, dict) or _json(intent_data) != raw_intent:
        raise ValueError("paid release intent is not canonical")
    intent = OperationIntent.from_dict(intent_data)
    if intent.to_dict() != intent_data:
        raise ValueError("paid release intent encoding is malformed")
    target = dict(intent.target)
    if (intent.key != key or dict(intent.scope) != {"board_id": board, "anchor_task_id": anchor}
            or intent.effect != "release" or set(target) != {"task_id", "request_id", "member_generation", "profile"}
            or not all(_nonempty_string(target.get(k)) for k in ("task_id", "request_id", "profile"))
            or type(target.get("member_generation")) is not int or target["member_generation"] < 0):
        raise ValueError("paid release intent identity is malformed")
    rows = connection.execute("SELECT board_id,anchor_task_id,event_id,source_kind,native_source_id,event_json FROM budget_events WHERE board_id=? AND anchor_task_id=? AND source_kind='native_operation' AND native_source_id=?", (board, anchor, key)).fetchall()
    if len(rows) != 1:
        raise ValueError("paid release must have exactly one native operation charge")
    row = rows[0]
    raw_event = row["event_json"]
    event = _decode(raw_event)
    required = {"event_id", "lineage_id", "root_task_id", "finding_id", "generation", "source_task_id", "source_kind", "native_source_id", "count"}
    expected_id = f"paid_capacity:{key}"
    if (not isinstance(event, dict) or not _nonempty_string(raw_event) or _json(event) != raw_event or set(event) != required
            or event != {"event_id": expected_id, "lineage_id": f"{anchor}:__general_attempt__", "root_task_id": anchor,
                         "finding_id": "__general_attempt__", "generation": target["member_generation"],
                         "source_task_id": target["task_id"], "source_kind": "native_operation", "native_source_id": key, "count": 1}
            or row["event_id"] != expected_id or row["source_kind"] != "native_operation" or row["native_source_id"] != key):
        raise ValueError("paid release charge does not reconstruct exactly")
    member = connection.execute("SELECT role,generation,work_association FROM managed_members WHERE board_id=? AND anchor_task_id=? AND task_id=?", (board, anchor, target["task_id"])).fetchone()
    if (member is None or not all(_nonempty_string(member[name]) for name in ("role", "work_association"))
            or member["role"] != "planner" or type(member["generation"]) is not int
            or member["generation"] != target["member_generation"] or member["work_association"] != target["request_id"]):
        raise ValueError("paid release member association is malformed")
    return intent, event, member


def _paid_capacity_total(connection: sqlite3.Connection, board: str, anchor: str) -> int:
    """Validate every scoped budget row before counting all paid-capacity charges."""
    rows = connection.execute("SELECT board_id,anchor_task_id,event_id,source_kind,native_source_id,event_json FROM budget_events WHERE board_id=? AND anchor_task_id=?", (board, anchor)).fetchall()
    total = 0
    required = {"event_id", "lineage_id", "root_task_id", "finding_id", "generation", "source_task_id", "source_kind", "native_source_id", "count"}
    for row in rows:
        try:
            raw = row["event_json"]
            event = _decode(raw)
            if (not isinstance(event, dict) or not _nonempty_string(raw) or _json(event) != raw or set(event) != required
                    or not all(_nonempty_string(event.get(name)) for name in ("event_id", "lineage_id", "root_task_id", "finding_id", "source_task_id", "source_kind", "native_source_id"))
                    or type(event.get("generation")) is not int or event["generation"] < 0
                    or type(event.get("count")) is not int or event["count"] <= 0
                    or event["source_kind"] not in {"native_run", "native_operation"}
                    or row["board_id"] != board or row["anchor_task_id"] != anchor
                    or row["event_id"] != event["event_id"] or row["source_kind"] != event["source_kind"]
                    or row["native_source_id"] != event["native_source_id"]
                    or event["root_task_id"] != anchor or event["lineage_id"] != f"{anchor}:{event['finding_id']}"):
                raise ValueError("budget event fields or row correspondence are malformed")
            member = connection.execute("SELECT generation,finding_ids_json FROM managed_members WHERE board_id=? AND anchor_task_id=? AND task_id=?", (board, anchor, event["source_task_id"])).fetchone()
            if member is None or type(member["generation"]) is not int or member["generation"] != event["generation"]:
                raise ValueError("budget event member generation is malformed")
            findings_raw = member["finding_ids_json"]
            findings = _decode(findings_raw)
            if not isinstance(findings, list) or _json(findings) != findings_raw or (event["finding_id"] != "__general_attempt__" and event["finding_id"] not in findings):
                raise ValueError("budget event member finding association is malformed")
            category, separator, suffix = event["event_id"].partition(":")
            if not separator or category not in CATEGORIES or not suffix or suffix != event["native_source_id"]:
                raise ValueError("budget event category or native source suffix is malformed")
            is_paid_capacity = category == PAID_CAPACITY
            if is_paid_capacity:
                if (event["finding_id"] != GENERAL_ATTEMPT or event["lineage_id"] != f"{anchor}:{GENERAL_ATTEMPT}"
                        or type(event["count"]) is not int or event["count"] != 1):
                    raise ValueError("paid capacity event must be one general-attempt charge")
                if event["source_kind"] == "native_operation":
                    source = connection.execute("SELECT intent_json FROM operation_intents WHERE board_id=? AND anchor_task_id=? AND operation_key=?", (board, anchor, event["native_source_id"])).fetchone()
                    if source is None:
                        raise ValueError("paid capacity operation intent is missing")
                    intent_data = _decode(source[0])
                    if not isinstance(intent_data, dict) or _json(intent_data) != source[0]:
                        raise ValueError("paid capacity operation intent is noncanonical")
                    intent = OperationIntent.from_dict(intent_data)
                    target = dict(intent.target)
                    if (intent.effect != "release" or intent.key != event["native_source_id"] or event["event_id"] != f"paid_capacity:{intent.key}"
                            or intent.phase not in {"pending", "unknown", "applied"} or target.get("task_id") != event["source_task_id"]
                            or target.get("member_generation") != event["generation"] or event["count"] != 1):
                        raise ValueError("paid capacity operation attribution is malformed")
                total += event["count"]
        except (TypeError, KeyError, ValueError, UnicodeError) as error:
            raise SchemaError("existing paid capacity evidence is malformed") from error
    return total


def _validate_paid_receipt(intent: OperationIntent) -> dict[str, Any]:
    """Require the canonical transport snapshot receipt and release proof."""
    try:
        receipt = intent.to_dict()["readback"]
        if not isinstance(receipt, dict) or set(receipt) != set(BoardSnapshot.__dataclass_fields__):
            raise ValueError("receipt fields mismatch")
        snapshot = BoardSnapshot.from_dict(receipt)
        target = dict(intent.target)
        task = dict(snapshot.native_task)
        if (task.get("id") != target["task_id"] or task.get("assignee") != target["profile"]
                or task.get("status") not in {"ready", "todo"}):
            raise ValueError("receipt task/profile/lane mismatch")
        marker_data = _json({"action_key": intent.key, "anchor_task_id": intent.scope["anchor_task_id"], "board_id": intent.scope["board_id"], "effect": "release"})
        marker = "<!-- local-first-native:v1:sha256:" + hashlib.sha256(marker_data.encode("utf-8")).hexdigest() + " -->"
        comments = [c for c in snapshot.comments if c.get("body") == f"UNBLOCK: {marker}"]
        if len(comments) != 1 or any(marker in str(c.get("body", "")) for c in snapshot.comments if c not in comments):
            raise ValueError("receipt release proof is not exact")
        return snapshot.to_dict()
    except (TypeError, KeyError, ValueError) as error:
        raise ConflictError("paid release requires an exact canonical BoardSnapshot release receipt") from error


def _validate_records(value: Any, *, fields: tuple[str, ...], name: str, outcomes: frozenset[str] | None = None) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"review evidence requires non-empty {name}")
    records: list[dict[str, str]] = []
    for record in value:
        if not isinstance(record, Mapping) or set(record) != set(fields) or not all(_nonempty_string(record.get(field)) for field in fields):
            raise ValueError(f"review evidence has invalid {name}")
        if outcomes is not None and record["outcome"] not in outcomes:
            raise ValueError(f"review evidence has invalid {name} outcome")
        records.append(dict(record))
    identifier = fields[0]
    if [record[identifier] for record in records] != sorted(record[identifier] for record in records) or len({record[identifier] for record in records}) != len(records):
        raise ValueError(f"review evidence {name} must have sorted unique identities")
    return records


def _scope_values(scope: Mapping[str, Any]) -> tuple[str, str]:
    valid = validate_scope(scope)
    return valid["board_id"], valid["anchor_task_id"]


def _validate_indexes(connection: sqlite3.Connection) -> None:
    """Require the fixed native-source index and reject every surplus index."""

    found_native_source_index = False
    for table in _TABLES:
        for row in connection.execute(f"PRAGMA index_list({table})"):
            if row["origin"] == "pk":
                continue
            if table == "paid_release_run_bindings" and row["unique"] and row["origin"] == "u" and tuple(column["name"] for column in connection.execute(f"PRAGMA index_info('{row['name']}')")) == ("board_id", "anchor_task_id", "native_run_id"):
                continue
            if table == "budget_events" and row["name"] == "budget_events_native_source_unique" and row["unique"] and tuple(column["name"] for column in connection.execute("PRAGMA index_info(budget_events_native_source_unique)")) == ("board_id", "anchor_task_id", "source_kind", "native_source_id"):
                found_native_source_index = True
                continue
            raise SchemaError("evidence-store schema indexes are unrecognized")
    if not found_native_source_index:
        raise SchemaError("evidence-store schema indexes are unrecognized")


def _normalized_sql(sql: str) -> str:
    """Normalize SQLite's harmless formatting/case differences, not semantics."""
    compact = " ".join(sql.split()).casefold()
    return re.sub(r"\s*([(),])\s*", r"\1", compact)


def _canonical_table_sql(table: str) -> str:
    columns = _TABLE_COLUMNS[table]
    primary = tuple(name for name, _, _, position in columns if position)
    definitions = []
    for name, kind, notnull, position in columns:
        definition = f"{name} {kind}"
        if notnull:
            definition += " NOT NULL"
        if table == "schema_metadata" and position:
            definition += " PRIMARY KEY"
        definitions.append(definition)
    if table != "schema_metadata":
        definitions.append(f"PRIMARY KEY ({', '.join(primary)})")
    if table == "review_evidence":
        definitions.append("FOREIGN KEY (board_id, anchor_task_id, candidate_content_identity) REFERENCES candidates (board_id, anchor_task_id, content_identity)")
    if table == "paid_release_run_bindings":
        definitions.append("UNIQUE (board_id, anchor_task_id, native_run_id)")
    return f"CREATE TABLE {table} ({', '.join(definitions)})"


def _validate_tables(connection: sqlite3.Connection, tables: frozenset[str]) -> None:
    rows = {row["name"]: row["sql"] for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if set(rows) != tables or any(rows[name] is None or _normalized_sql(rows[name]) != _normalized_sql(_canonical_table_sql(name)) for name in tables):
        raise SchemaError("evidence-store schema table definitions, constraints, or foreign keys are unrecognized")


def _validate_views(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
        raise SchemaError("evidence-store schema views are unrecognized")


class EvidenceStore:
    """A deliberately narrow, transaction-backed evidence store."""

    def __init__(self, connection: sqlite3.Connection, path: Path, *, created: bool, read_native_run: Callable[[Mapping[str, str], str, str], Mapping[str, Any]] | None = None) -> None:
        self.connection = connection
        self.path = path
        self._created = created
        self._migrated = False
        self._read_native_run = read_native_run

    @classmethod
    def open(cls, path: str | Path, *, create_new: bool = False, read_native_run: Callable[[Mapping[str, str], str, str], Mapping[str, Any]] | None = None) -> "EvidenceStore":
        """Open an existing store or exclusively create an initial fixture store."""
        if not isinstance(create_new, bool):
            raise ValueError("create_new must be a boolean")
        if read_native_run is not None and not callable(read_native_run):
            raise ValueError("read_native_run must be a callable trusted native reader")
        database = Path(os.path.abspath(os.path.expanduser(str(path))))
        parent = database.parent
        try:
            parent_stat = parent.stat()
        except OSError as error:
            raise SchemaError("evidence-store parent directory does not exist") from error
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise SchemaError("evidence-store parent directory does not exist")
        if parent_stat.st_uid != os.getuid():
            raise SchemaError("evidence-store parent is not owned by the current user")
        if stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise SchemaError("evidence-store parent directory must be private")
        if database.is_symlink():
            raise SchemaError("evidence-store path must be a regular file")
        existed = database.exists()
        if existed:
            if not database.is_file():
                raise SchemaError("evidence-store path must be a regular file")
            database_stat = database.stat()
            if database_stat.st_uid != os.getuid():
                raise SchemaError("evidence-store is not owned by the current user")
            if create_new:
                raise SchemaError("evidence-store already exists")
        elif not create_new:
            raise SchemaError("evidence-store does not exist")
        else:
            try:
                descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError as error:
                raise SchemaError("evidence-store already exists") from error
            except OSError as error:
                raise SchemaError("cannot create evidence-store database") from error
            os.close(descriptor)
        try:
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if existed and connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                connection.close()
                raise SchemaError("evidence-store integrity check failed")
        except sqlite3.DatabaseError as error:
            raise SchemaError("cannot open evidence-store database") from error
        return cls(connection, database, created=not existed, read_native_run=read_native_run)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _require_schema(self) -> None:
        if self._migrated:
            return
        names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if names != _TABLES:
            raise SchemaError("evidence-store schema is missing or unrecognized")
        _validate_views(self.connection)
        _validate_tables(self.connection, _TABLES)
        for table, expected_columns in _TABLE_COLUMNS.items():
            columns = tuple(
                (row["name"], row["type"].upper(), row["notnull"], row["pk"])
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            )
            if columns != expected_columns:
                raise SchemaError("evidence-store schema columns or primary key are unrecognized")
        foreign_keys = tuple(
            (row["table"], row["from"], row["to"])
            for row in self.connection.execute("PRAGMA foreign_key_list(review_evidence)")
        )
        if foreign_keys != _REVIEW_EVIDENCE_FOREIGN_KEYS:
            raise SchemaError("evidence-store schema foreign keys are unrecognized")
        _validate_indexes(self.connection)
        metadata = tuple(tuple(row) for row in self.connection.execute("SELECT key, value FROM schema_metadata ORDER BY key"))
        if metadata != (("schema_version", str(_SCHEMA_VERSION)),):
            raise SchemaError("evidence-store schema version is unsupported")
        triggers = {row["name"]: row["sql"] for row in self.connection.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")}
        expected_triggers = {**_RECONCILIATION_TRIGGER_SQL, **_PLAN_TRIGGERS, **_PAID_BINDING_TRIGGERS}
        if set(triggers) != set(expected_triggers) or any(" ".join(triggers[name].split()).upper() != " ".join(sql.split()).upper() for name, sql in expected_triggers.items()):
            raise SchemaError("evidence-store schema reconciliation triggers are unrecognized")
        self._migrated = True

    def migrate(self) -> None:
        """Validate historical schemas before atomically upgrading to version four."""
        names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if names:
            metadata = tuple(tuple(row) for row in self.connection.execute("SELECT key, value FROM schema_metadata ORDER BY key")) if "schema_metadata" in names else ()
            v4 = _TABLES - {"paid_release_run_bindings"}
            v1 = v4 - {"plan_proposals", "budget_reconciliation_evidence", "effect_observations"}
            v2 = v4 - {"plan_proposals", "effect_observations"}
            v3 = v4 - {"plan_proposals"}
            versions = {1: v1, 2: v2, 3: v3, 4: v4}
            version = next((number for number, expected_names in versions.items() if names == expected_names and metadata == (("schema_version", str(number)),)), None)
            if names == _TABLES:
                self._require_schema()
                return
            if version is not None:
                expected_tables = versions[version]
                _validate_views(self.connection)
                _validate_tables(self.connection, expected_tables)
                for table, columns in _TABLE_COLUMNS.items():
                    if table not in expected_tables:
                        continue
                    actual = tuple((row["name"], row["type"].upper(), row["notnull"], row["pk"]) for row in self.connection.execute(f"PRAGMA table_info({table})"))
                    if actual != columns:
                        raise SchemaError("evidence-store schema columns or primary key are unrecognized")
                foreign_keys = tuple((row["table"], row["from"], row["to"]) for row in self.connection.execute("PRAGMA foreign_key_list(review_evidence)"))
                if foreign_keys != _REVIEW_EVIDENCE_FOREIGN_KEYS:
                    raise SchemaError("evidence-store schema foreign keys are unrecognized")
                expected_triggers = {}
                if version >= 2:
                    expected_triggers.update({name: sql for name, sql in _RECONCILIATION_TRIGGER_SQL.items() if name.startswith("budget_")})
                if version >= 3:
                    expected_triggers.update({name: sql for name, sql in _RECONCILIATION_TRIGGER_SQL.items() if name.startswith("effect_")})
                if version >= 4:
                    expected_triggers.update(_PLAN_TRIGGERS)
                triggers = {row["name"]: row["sql"] for row in self.connection.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")}
                if set(triggers) != set(expected_triggers) or any(" ".join(triggers[name].split()).upper() != " ".join(sql.split()).upper() for name, sql in expected_triggers.items()):
                    raise SchemaError("evidence-store schema triggers are unrecognized")
                _validate_indexes(self.connection)
                try:
                    self.connection.execute("BEGIN IMMEDIATE")
                    if version == 1:
                        self.connection.execute("CREATE TABLE budget_reconciliation_evidence (board_id TEXT NOT NULL, anchor_task_id TEXT NOT NULL, event_id TEXT NOT NULL, evidence_kind TEXT NOT NULL, evidence_json TEXT NOT NULL, PRIMARY KEY (board_id, anchor_task_id, event_id, evidence_kind))")
                        for sql in _RECONCILIATION_TRIGGER_SQL.values():
                            if sql.startswith("CREATE TRIGGER budget_"):
                                self.connection.execute(sql)
                    if version <= 2:
                        self.connection.execute("CREATE TABLE effect_observations (board_id TEXT NOT NULL, anchor_task_id TEXT NOT NULL, operation_key TEXT NOT NULL, observation_identity TEXT NOT NULL, observation_json TEXT NOT NULL, PRIMARY KEY (board_id, anchor_task_id, operation_key, observation_identity))")
                        for name, sql in _RECONCILIATION_TRIGGER_SQL.items():
                            if name.startswith("effect_"):
                                self.connection.execute(sql)
                    if version <= 3:
                        self.connection.execute(_canonical_table_sql("plan_proposals"))
                        for sql in _PLAN_TRIGGERS.values():
                            self.connection.execute(sql)
                    self.connection.execute(_canonical_table_sql("paid_release_run_bindings"))
                    for sql in _PAID_BINDING_TRIGGERS.values():
                        self.connection.execute(sql)
                    self.connection.execute("UPDATE schema_metadata SET value='5' WHERE key='schema_version'")
                    self._migrated = False
                    self._require_schema()
                    self.connection.commit()
                except BaseException:
                    self.connection.rollback()
                    self._migrated = False
                    raise
                return
            raise SchemaError("existing database has no recognized evidence-store schema")
        if not self._created:
            raise SchemaError("existing database has no recognized evidence-store schema")
        try:
            with self.connection:
                self.connection.executescript(
                    """
                    CREATE TABLE schema_metadata (
                        key TEXT NOT NULL PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE managed_members (
                        board_id TEXT NOT NULL,
                        anchor_task_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        finding_ids_json TEXT NOT NULL,
                        work_association TEXT NOT NULL,
                        PRIMARY KEY (board_id, anchor_task_id, task_id)
                    );
                    CREATE TABLE candidates (
                        board_id TEXT NOT NULL,
                        anchor_task_id TEXT NOT NULL,
                        content_identity TEXT NOT NULL,
                        identity_json TEXT NOT NULL,
                        PRIMARY KEY (board_id, anchor_task_id, content_identity)
                    );
                    CREATE TABLE review_evidence (
                        board_id TEXT NOT NULL,
                        anchor_task_id TEXT NOT NULL,
                        candidate_content_identity TEXT NOT NULL,
                        review_id TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        PRIMARY KEY (board_id, anchor_task_id, review_id),
                        FOREIGN KEY (board_id, anchor_task_id, candidate_content_identity)
                            REFERENCES candidates (board_id, anchor_task_id, content_identity)
                    );
                    CREATE TABLE operation_intents (
                        board_id TEXT NOT NULL,
                        anchor_task_id TEXT NOT NULL,
                        operation_key TEXT NOT NULL,
                        intent_json TEXT NOT NULL,
                        PRIMARY KEY (board_id, anchor_task_id, operation_key)
                    );
                    CREATE TABLE budget_events (
                        board_id TEXT NOT NULL,
                        anchor_task_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        native_source_id TEXT NOT NULL,
                        event_json TEXT NOT NULL,
                        PRIMARY KEY (board_id, anchor_task_id, event_id)
                    );
                    CREATE UNIQUE INDEX budget_events_native_source_unique
                    ON budget_events (board_id, anchor_task_id, source_kind, native_source_id);
                    CREATE TABLE budget_reconciliation_evidence (
                        board_id TEXT NOT NULL,
                        anchor_task_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        evidence_kind TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        PRIMARY KEY (board_id, anchor_task_id, event_id, evidence_kind)
                    );
                    CREATE TABLE operator_intents (
                        board_id TEXT NOT NULL,
                        anchor_task_id TEXT NOT NULL,
                        intent_json TEXT NOT NULL,
                        PRIMARY KEY (board_id, anchor_task_id)
                    );
                    CREATE TABLE effect_observations (
                        board_id TEXT NOT NULL,
                        anchor_task_id TEXT NOT NULL,
                        operation_key TEXT NOT NULL,
                        observation_identity TEXT NOT NULL,
                        observation_json TEXT NOT NULL,
                        PRIMARY KEY (board_id, anchor_task_id, operation_key, observation_identity)
                    );
                    CREATE TRIGGER budget_reconciliation_evidence_immutable_update
                    BEFORE UPDATE ON budget_reconciliation_evidence
                    BEGIN SELECT RAISE(ABORT, 'budget reconciliation evidence is immutable'); END;
                    CREATE TRIGGER budget_reconciliation_evidence_immutable_delete
                    BEFORE DELETE ON budget_reconciliation_evidence
                    BEGIN SELECT RAISE(ABORT, 'budget reconciliation evidence is immutable'); END;
                    CREATE TRIGGER effect_observations_immutable_update
                    BEFORE UPDATE ON effect_observations
                    BEGIN SELECT RAISE(ABORT, 'effect observation evidence is immutable'); END;
                    CREATE TRIGGER effect_observations_immutable_delete
                    BEFORE DELETE ON effect_observations
                    BEGIN SELECT RAISE(ABORT, 'effect observation evidence is immutable'); END;
                    CREATE TABLE plan_proposals (board_id TEXT NOT NULL, anchor_task_id TEXT NOT NULL, plan_id TEXT NOT NULL, evidence_json TEXT NOT NULL, PRIMARY KEY (board_id, anchor_task_id, plan_id));
                    CREATE TRIGGER plan_proposals_immutable_update BEFORE UPDATE ON plan_proposals BEGIN SELECT RAISE(ABORT, 'plan evidence is immutable'); END;
                    CREATE TRIGGER plan_proposals_immutable_delete BEFORE DELETE ON plan_proposals BEGIN SELECT RAISE(ABORT, 'plan evidence is immutable'); END;
                    CREATE TABLE paid_release_run_bindings (board_id TEXT NOT NULL, anchor_task_id TEXT NOT NULL, release_operation_key TEXT NOT NULL, task_id TEXT NOT NULL, member_generation INTEGER NOT NULL, request_identity TEXT NOT NULL, native_run_id TEXT NOT NULL, native_session_id TEXT NOT NULL, native_profile TEXT NOT NULL, run_classification TEXT NOT NULL, native_run_json TEXT NOT NULL, native_run_sha256 TEXT NOT NULL, PRIMARY KEY (board_id, anchor_task_id, release_operation_key), UNIQUE (board_id, anchor_task_id, native_run_id));
                    CREATE TRIGGER paid_release_run_bindings_immutable_update BEFORE UPDATE ON paid_release_run_bindings BEGIN SELECT RAISE(ABORT, 'paid release run binding is immutable'); END;
                    CREATE TRIGGER paid_release_run_bindings_immutable_delete BEFORE DELETE ON paid_release_run_bindings BEGIN SELECT RAISE(ABORT, 'paid release run binding is immutable'); END;
                    INSERT INTO schema_metadata(key, value) VALUES ('schema_version', '5');
                    """
                )
        except sqlite3.DatabaseError as error:
            raise SchemaError("could not migrate evidence-store") from error
        self._migrated = True

    def record_plan(self, scope: Mapping[str, Any], evidence: Mapping[str, Any]) -> Mapping[str, Any]:
        from .planning_coordinator import reconstruct_evidence
        self._require_schema()
        board, anchor = _scope_values(scope)
        request, proposal = reconstruct_evidence(evidence)
        if request.board_id != board or request.anchor_id != anchor:
            raise ConflictError("plan evidence request scope does not match store scope")
        payload = _json(dict(evidence))
        plan_id = proposal.plan.plan_id
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT evidence_json FROM plan_proposals WHERE board_id=? AND anchor_task_id=? AND plan_id=?", (board, anchor, plan_id)).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise ConflictError("plan identity conflicts with existing immutable evidence")
                self.connection.commit()
                return dict(evidence)
            self.connection.execute("INSERT INTO plan_proposals VALUES (?, ?, ?, ?)", (board, anchor, plan_id, payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return dict(evidence)

    def read_plan(self, scope: Mapping[str, Any], plan_id: str) -> Mapping[str, Any]:
        from .planning_coordinator import reconstruct_evidence
        self._require_schema()
        board, anchor = _scope_values(scope)
        if not _nonempty_string(plan_id):
            raise ValueError("plan_id must be non-empty")
        row = self.connection.execute("SELECT evidence_json FROM plan_proposals WHERE board_id=? AND anchor_task_id=? AND plan_id=?", (board, anchor, plan_id)).fetchone()
        if row is None:
            raise KeyError("plan evidence is not recorded in this scope")
        raw = row[0]
        value = _decode(raw)
        if _json(value) != raw:
            raise SchemaError("stored plan evidence is not canonical")
        try:
            request, proposal = reconstruct_evidence(value)
        except (ValueError, TypeError, UnicodeError) as error:
            raise SchemaError("stored plan evidence failed reconstruction") from error
        if request.board_id != board or request.anchor_id != anchor or proposal.plan.plan_id != plan_id:
            raise SchemaError("stored plan evidence identity does not match its row")
        return value

    def record_accepted_plan(self, scope: Mapping[str, Any], plan_id: str, *, route: Mapping[str, Any], request_id: str | None = None) -> Mapping[str, Any]:
        """Atomically record coordinator-validated acceptance, never a native effect."""
        from .planning_coordinator import reconstruct_evidence, ActiveTrancheRoute, first_active_tranche_materialization
        self._require_schema()
        board, anchor = _scope_values(scope)
        evidence = self.read_plan(scope, plan_id)
        request, proposal = reconstruct_evidence(evidence)
        from .planning_coordinator import request_from_payload, request_payload
        registration = self.read_planning_request(scope, request_id=request_id)
        registered = request_from_payload(registration["request"])
        if request_payload(registered) != request_payload(request):
            raise ConflictError("accepted plan differs from registered request")
        binding_rows = self.connection.execute("SELECT release_operation_key FROM paid_release_run_bindings WHERE board_id=? AND anchor_task_id=?", (board, anchor)).fetchall()
        bindings = [self.read_paid_release_run_binding(scope, row[0]) for row in binding_rows]
        matched = [b for b in bindings if (b["task_id"], b["run_id"], b["session_id"], b["profile"]) ==
                   (evidence["planner"]["task_id"], evidence["planner"]["run_id"], evidence["planner"]["session_id"], evidence["planner"]["profile"])]
        if len(matched) != 1:
            raise ConflictError("accepted plan lacks exact registered request and paid run binding")
        root_rows = self.connection.execute("SELECT task_id,role FROM managed_members WHERE board_id=? AND anchor_task_id=? AND role='root'", (board, anchor)).fetchall()
        if len(root_rows) != 1 or root_rows[0]["task_id"] != anchor:
            raise ConflictError("acceptance requires exactly one enrolled scope root")
        member = self.connection.execute("SELECT role,work_association FROM managed_members WHERE board_id=? AND anchor_task_id=? AND task_id=?", (board, anchor, evidence["planner"]["task_id"])).fetchone()
        if member is None or member["role"] != "planner" or member["work_association"] != request.identity:
            raise ConflictError("accepted plan planner membership is inconsistent")
        if not isinstance(route, Mapping) or set(route) != {"implementation_profile", "workspace"}:
            raise ValueError("acceptance route has an invalid schema")
        first_active_tranche_materialization(evidence, ActiveTrancheRoute(route["implementation_profile"], route["workspace"]))
        tranche = proposal.plan.tranches[0]
        digest = _sha256({"schema_version": 1, "plan_id": plan_id, "request_identity": request.identity,
            "proposal_hash": proposal.proposal_hash, "plan_contract_hash": proposal.plan.contract_hash,
            "planner": dict(evidence["planner"]), "repository_identity": request.repository_identity,
            "base_sha": request.base_sha, "snapshot_hash": request.snapshot_hash,
            "root_contract_hash": request.root_contract_hash,
            "active_tranche": {"tranche_id": tranche.tranche_id, "ordinal": 0},
            "route": dict(route)})
        token = {"schema_version": 1, "plan_id": plan_id, "request_identity": request.identity,
            "proposal_hash": proposal.proposal_hash, "plan_contract_hash": proposal.plan.contract_hash,
            "planner": dict(evidence["planner"]), "repository_identity": request.repository_identity,
            "base_sha": request.base_sha, "snapshot_hash": request.snapshot_hash,
            "root_contract_hash": request.root_contract_hash,
            "active_tranche": {"tranche_id": tranche.tranche_id, "ordinal": 0},
            "route": dict(route), "acceptance_identity": digest}
        key = "accept-plan:" + digest
        intent = OperationIntent(key, scope, {"plan_id": plan_id, "token": token}, "accept_validated_plan", digest,
            {"source_plan_id": plan_id, "authority_writer": "record_accepted_plan:v1"}, "verified", {"token": token}, {}, "applied")
        payload = _json(intent.to_dict())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute("SELECT operation_key,intent_json FROM operation_intents WHERE board_id=? AND anchor_task_id=?", (board, anchor)).fetchall()
            existing = []
            for row in rows:
                parsed = OperationIntent.from_dict(_decode(row["intent_json"]))
                if parsed.effect == "accept_validated_plan" and parsed.target.get("plan_id") == plan_id:
                    existing.append((row["operation_key"], row["intent_json"], parsed))
            if len(existing) > 1:
                raise SchemaError("duplicate plan acceptance receipts")
            if existing:
                if existing[0][0] != key or existing[0][1] != payload:
                    raise ConflictError("accepted plan conflicts with immutable authority receipt")
            else:
                self.connection.execute("INSERT INTO operation_intents VALUES (?, ?, ?, ?)", (board, anchor, key, payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return self.read_accepted_plan(scope, plan_id)

    def read_accepted_plan(self, scope: Mapping[str, Any], plan_id: str) -> Mapping[str, Any]:
        """Strictly reconstruct the accepted authority receipt and its immutable sources."""
        self._require_schema()
        board, anchor = _scope_values(scope)
        rows = self.connection.execute("SELECT operation_key,intent_json FROM operation_intents WHERE board_id=? AND anchor_task_id=?", (board, anchor)).fetchall()
        matches = []
        for row in rows:
            try: intent = OperationIntent.from_dict(_decode(row["intent_json"]))
            except (TypeError, ValueError) as error: raise SchemaError("acceptance intent malformed") from error
            if intent.effect == "accept_validated_plan" and intent.target.get("plan_id") == plan_id: matches.append((row["operation_key"], intent))
        if not matches: raise KeyError("accepted plan authority does not exist")
        if len(matches) != 1: raise SchemaError("duplicate accepted plan authority")
        key, intent = matches[0]
        if intent.phase != "applied" or intent.outcome != "verified" or intent.readback != {"token": intent.target.get("token")} or set(intent.target) != {"plan_id", "token"}:
            raise SchemaError("accepted plan receipt phase or duplicated payload is corrupt")
        try:
            token = intent.target["token"]
            evidence = self.read_plan(scope, plan_id)
            request, proposal = __import__("local_first_orchestrator.planning_coordinator", fromlist=["reconstruct_evidence"]).reconstruct_evidence(evidence)
            route = token["route"]
            expected = self.record_accepted_plan  # do not invoke writer during read
            from .planning_coordinator import first_active_tranche_materialization, ActiveTrancheRoute
            first_active_tranche_materialization(evidence, ActiveTrancheRoute(route["implementation_profile"], route["workspace"]))
            tranche = proposal.plan.tranches[0]
            expected_fields = {"schema_version": 1, "plan_id": plan_id, "request_identity": request.identity,
                "proposal_hash": proposal.proposal_hash, "plan_contract_hash": proposal.plan.contract_hash,
                "planner": evidence["planner"], "repository_identity": request.repository_identity,
                "base_sha": request.base_sha, "snapshot_hash": request.snapshot_hash,
                "root_contract_hash": request.root_contract_hash,
                "active_tranche": {"tranche_id": tranche.tranche_id, "ordinal": 0}, "route": dict(route)}
            if set(token) != set(expected_fields) | {"acceptance_identity"} or any(token.get(k) != v for k,v in expected_fields.items()): raise ValueError("token/source mismatch")
            digest = _sha256(expected_fields)
            if token["acceptance_identity"] != digest or key != "accept-plan:" + digest or intent.expected_observed_identity != digest: raise ValueError("authority digest mismatch")
            if intent.before_evidence != {"source_plan_id": plan_id, "authority_writer": "record_accepted_plan:v1"}: raise ValueError("source reference or authority provenance mismatch")
            binding_rows = self.connection.execute("SELECT release_operation_key FROM paid_release_run_bindings WHERE board_id=? AND anchor_task_id=?", (board, anchor)).fetchall()
            bound = [self.read_paid_release_run_binding(scope, item[0]) for item in binding_rows]
            planner = token["planner"]
            if len([item for item in bound if (item["task_id"], item["run_id"], item["session_id"], item["profile"]) == (planner["task_id"], planner["run_id"], planner["session_id"], planner["profile"])]) != 1:
                raise ValueError("planner paid-release source mismatch")
            root_rows = self.connection.execute("SELECT task_id FROM managed_members WHERE board_id=? AND anchor_task_id=? AND role='root'", (board, anchor)).fetchall()
            if len(root_rows) != 1 or root_rows[0]["task_id"] != anchor:
                raise ValueError("scope root source mismatch")
            member = self.connection.execute("SELECT role,work_association FROM managed_members WHERE board_id=? AND anchor_task_id=? AND task_id=?", (board, anchor, planner["task_id"])).fetchone()
            if member is None or member["role"] != "planner" or member["work_association"] != request.identity:
                raise ValueError("planner member source mismatch")
            from .planning_coordinator import _deep_freeze
            return _deep_freeze(token)
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise SchemaError("accepted plan authority failed strict reconstruction") from error

    def plan_evidence(self, scope: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        self._require_schema()
        board, anchor = _scope_values(scope)
        rows = self.connection.execute("SELECT plan_id FROM plan_proposals WHERE board_id=? AND anchor_task_id=? ORDER BY plan_id LIMIT 65", (board, anchor)).fetchall()
        if len(rows) > 64:
            raise SchemaError("plan evidence list exceeds bounded limit; request an exact plan ID")
        return tuple(self.read_plan(scope, row[0]) for row in rows)

    def register_member(self, member: ManagedMember) -> ManagedMember:
        self._require_schema()
        board, anchor = _scope_values({"board_id": member.board_id, "anchor_task_id": member.anchor_task_id})
        payload = member.to_dict()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT role, generation, finding_ids_json, work_association FROM managed_members "
                "WHERE board_id = ? AND anchor_task_id = ? AND task_id = ?",
                (board, anchor, member.task_id),
            ).fetchone()
            if row is not None:
                old = {**validate_scope({"board_id": board, "anchor_task_id": anchor}), "task_id": member.task_id,
                       "role": row["role"], "generation": row["generation"],
                       "finding_ids": _decode(row["finding_ids_json"]), "work_association": row["work_association"]}
                if old != payload:
                    raise ConflictError("managed member identity conflicts with existing evidence")
                self.connection.commit()
                return member
            self.connection.execute(
                "INSERT INTO managed_members VALUES (?, ?, ?, ?, ?, ?, ?)",
                (board, anchor, member.task_id, member.role, member.generation,
                 _json(list(member.finding_ids)), member.work_association),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return member

    def record_candidate(self, scope: Mapping[str, Any], candidate: CandidateIdentity) -> CandidateIdentity:
        self._require_schema()
        board, anchor = _scope_values(scope)
        payload = _json(candidate.to_dict())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT identity_json FROM candidates WHERE board_id = ? AND anchor_task_id = ? AND content_identity = ?",
                (board, anchor, candidate.content_identity),
            ).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise ConflictError("candidate identity conflicts with existing evidence")
                self.connection.commit()
                return candidate
            self.connection.execute("INSERT INTO candidates VALUES (?, ?, ?, ?)", (board, anchor, candidate.content_identity, payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return candidate

    def record_review(self, scope: Mapping[str, Any], candidate: CandidateIdentity, review: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_schema()
        board, anchor = _scope_values(scope)
        required = {"review_id", "candidate_identity", "reviewer_role", "native_review", "checks", "checks_identity", "verdict", "criterion_evidence", "findings"}
        if not isinstance(review, Mapping) or set(review) != required or not _nonempty_string(review.get("review_id")):
            raise ValueError("review evidence has an invalid stored shape")
        try:
            review_candidate = CandidateIdentity.from_dict(review["candidate_identity"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("review evidence requires a complete candidate_identity") from error
        if review_candidate != candidate:
            raise ConflictError("review candidate binding conflicts with the recorded candidate")
        if review.get("reviewer_role") not in {"local", "paid"}:
            raise ValueError("review evidence reviewer_role must be local or paid")
        native_review = review.get("native_review")
        if not isinstance(native_review, Mapping) or set(native_review) != {"task_id", "run_id", "session_id", "profile"} or not all(_nonempty_string(value) for value in native_review.values()):
            raise ValueError("review evidence requires native review provenance")
        if native_review["run_id"] == candidate.originating_run_id:
            raise ConflictError("review evidence must come from an independent native run")
        checks = _validate_records(review["checks"], fields=("check_id", "outcome", "evidence"), name="checks", outcomes=frozenset({"passed", "failed", "skipped"}))
        if review.get("checks_identity") != _canonical_identity(checks):
            raise ValueError("review evidence checks_identity does not match canonical checks")
        criteria = _validate_records(review["criterion_evidence"], fields=("criterion_id", "outcome", "evidence"), name="criterion_evidence", outcomes=frozenset({"pass", "fail", "not_applicable"}))
        findings = review.get("findings")
        if not isinstance(findings, list):
            raise ValueError("review evidence findings must be a list")
        normalized_findings: list[dict[str, str]] = []
        for finding in findings:
            if not isinstance(finding, Mapping) or set(finding) != {"finding_id", "criterion_id", "severity", "summary"} or not all(_nonempty_string(finding.get(field)) for field in ("finding_id", "criterion_id", "severity", "summary")) or finding["severity"] not in {"blocker", "major", "minor"}:
                raise ValueError("review evidence has invalid findings")
            normalized_findings.append(dict(finding))
        failed = {criterion["criterion_id"] for criterion in criteria if criterion["outcome"] == "fail"}
        finding_criteria = {finding["criterion_id"] for finding in normalized_findings}
        if len({finding["finding_id"] for finding in normalized_findings}) != len(normalized_findings) or not finding_criteria <= failed:
            raise ValueError("review evidence findings must bind failed criteria")
        if review.get("verdict") == "approved" and not failed and not normalized_findings:
            pass
        elif review.get("verdict") == "changes_requested" and failed and failed <= finding_criteria:
            pass
        else:
            raise ValueError("review evidence verdict, criteria, and findings are inconsistent")
        payload = _json(dict(review))
        with self.connection:
            candidate_payload = _json(candidate.to_dict())
            candidate_row = self.connection.execute(
                "SELECT identity_json FROM candidates WHERE board_id = ? AND anchor_task_id = ? AND content_identity = ?",
                (board, anchor, candidate.content_identity),
            ).fetchone()
            if candidate_row is not None and candidate_row[0] != candidate_payload:
                raise ConflictError("candidate identity conflicts with existing evidence")
            row = self.connection.execute(
                "SELECT evidence_json FROM review_evidence WHERE board_id = ? AND anchor_task_id = ? AND review_id = ?",
                (board, anchor, review["review_id"]),
            ).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise ConflictError("review identity conflicts with existing evidence")
                return dict(review)
            if candidate_row is None:
                self.connection.execute("INSERT INTO candidates VALUES (?, ?, ?, ?)", (board, anchor, candidate.content_identity, candidate_payload))
            self.connection.execute(
                "INSERT INTO review_evidence VALUES (?, ?, ?, ?, ?)",
                (board, anchor, candidate.content_identity, review["review_id"], payload),
            )
        return dict(review)

    def register_planning_request(self, scope: Mapping[str, Any], request: Any, planner_task_id: str, planner_profile: str | None = None, *, request_id: str | None = None) -> Mapping[str, Any]:
        """Bind one canonical planner request to one managed native planner task.

        The operation-intent journal is used as the immutable evidence container;
        no lifecycle state is mirrored and no schema migration is needed.
        """
        from .decomposition_planner import PlanningRequest, request_payload
        self._require_schema()
        board, anchor = _scope_values(scope)
        if type(request) is not PlanningRequest:
            raise ValueError("PlanningRequest required")
        payload = request_payload(request)
        if request.board_id != board or request.anchor_id != anchor:
            raise ConflictError("planning request scope does not match registration scope")
        if not _nonempty_string(planner_task_id):
            raise ValueError("planner task ID must be non-empty")
        if planner_profile is None or not _nonempty_string(planner_profile):
            raise ValueError("planner profile must be non-empty")
        if request_id is not None and not _nonempty_string(request_id):
            raise ValueError("request_id must be non-empty when supplied")
        member = self.connection.execute("SELECT role FROM managed_members WHERE board_id=? AND anchor_task_id=? AND task_id=?", (board, anchor, planner_task_id)).fetchone()
        if member is None or member["role"] != "planner":
            raise ConflictError("planner task is not an enrolled planner member")
        identity = {"request": payload, "planner_task_id": planner_task_id, "planner_profile": planner_profile}
        scope_identity = {"board_id": board, "anchor_task_id": anchor}
        key = "planning-request:" + _canonical_identity(scope_identity)
        if request_id is not None:
            key += ":" + _canonical_identity({"request_id": request_id})
        intent = OperationIntent(key, {"board_id": board, "anchor_task_id": anchor}, identity, "register_planning_request", request.identity, {"request_identity": request.identity, "planner_task_id": planner_task_id, "planner_profile": planner_profile}, "verified", {"immutable": True, "request_identity": request.identity}, {"immutable": True}, "applied")
        stored = self.reserve_operation(intent)
        return stored.to_dict()["target"]

    def read_planning_request(self, scope: Mapping[str, Any], *, request_id: str | None = None) -> Mapping[str, Any]:
        self._require_schema()
        board, anchor = _scope_values(scope)
        if request_id is not None and not _nonempty_string(request_id):
            raise ValueError("request_id must be non-empty when supplied")
        key = "planning-request:" + _canonical_identity({"board_id": board, "anchor_task_id": anchor})
        if request_id is not None:
            key += ":" + _canonical_identity({"request_id": request_id})
        intent = self._operation({"board_id": board, "anchor_task_id": anchor}, key)
        if intent.effect != "register_planning_request" or intent.phase != "applied" or intent.outcome != "verified":
            raise SchemaError("planning request registration evidence is malformed")
        from .planning_coordinator import request_from_payload
        target = intent.to_dict()["target"]
        if intent.readback != {"immutable": True, "request_identity": intent.expected_observed_identity} or intent.before_evidence != {"request_identity": intent.expected_observed_identity, "planner_task_id": target.get("planner_task_id"), "planner_profile": target.get("planner_profile")}:
            raise SchemaError("planning request registration readback is malformed")
        try:
            if set(target) != {"request", "planner_task_id", "planner_profile"}:
                raise ValueError("registration target fields mismatch")
            if not _nonempty_string(target["planner_task_id"]) or not _nonempty_string(target["planner_profile"]):
                raise ValueError("registration planner binding is malformed")
            request = request_from_payload(target["request"])
            member = self.connection.execute("SELECT role FROM managed_members WHERE board_id=? AND anchor_task_id=? AND task_id=?", (board, anchor, target["planner_task_id"])).fetchone()
            if member is None or member["role"] != "planner":
                raise ValueError("registration planner is not an enrolled planner")
            if request.identity != intent.expected_observed_identity or request.board_id != board or request.anchor_id != anchor:
                raise ValueError("registration identity or scope is inconsistent")
        except (TypeError, KeyError, ValueError) as error:
            raise SchemaError("planning request registration evidence is malformed") from error
        return target

    def reserve_operation(self, intent: OperationIntent) -> OperationIntent:
        self._require_schema()
        self._reject_local_authority_effect(intent)
        board, anchor = _scope_values(intent.scope)
        payload = _json(intent.to_dict())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT intent_json FROM operation_intents WHERE board_id = ? AND anchor_task_id = ? AND operation_key = ?",
                (board, anchor, intent.key),
            ).fetchone()
            if row is not None:
                stored = OperationIntent.from_dict(_decode(row[0]))
                stored_identity = stored.to_dict()
                requested_identity = intent.to_dict()
                for field in ("outcome", "readback", "phase"):
                    stored_identity.pop(field)
                    requested_identity.pop(field)
                if stored_identity != requested_identity:
                    raise ConflictError("operation key conflicts with existing intent")
                if stored.phase == "unknown":
                    raise ConflictError("operation effect is unknown and must be reconciled before retry")
                self.connection.commit()
                return stored
            self.connection.execute("INSERT INTO operation_intents VALUES (?, ?, ?, ?)", (board, anchor, intent.key, payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return intent

    def reserve_budgeted_repair_operation(
        self, intent: OperationIntent, event: Mapping[str, Any], *, policy_limit: int,
    ) -> OperationIntent:
        """Atomically reserve a classified repair operation and its one-unit charge."""
        self._require_schema()
        self._reject_local_authority_effect(intent)
        board, anchor = _scope_values(intent.scope)
        if not isinstance(policy_limit, int) or isinstance(policy_limit, bool) or policy_limit < 0:
            raise ValueError("budget policy limit must be a non-negative integer")
        required = {"event_id", "lineage_id", "root_task_id", "finding_id", "generation", "source_task_id", "source_kind", "native_source_id", "count"}
        if not isinstance(event, Mapping) or set(event) != required:
            raise ValueError("budget event has an invalid attribution shape")
        if not all(_nonempty_string(event[field]) for field in ("event_id", "lineage_id", "root_task_id", "finding_id", "source_task_id", "source_kind", "native_source_id")):
            raise ValueError("budget event requires non-empty identity and source")
        if event["source_kind"] != "native_operation" or event["count"] != 1:
            raise ValueError("budgeted repair event requires one native operation source")
        if not isinstance(event["generation"], int) or isinstance(event["generation"], bool) or event["generation"] < 0:
            raise ValueError("budget event generation must be a non-negative integer")
        if event["root_task_id"] != anchor or event["lineage_id"] != f"{anchor}:{event['finding_id']}":
            raise ConflictError("budgeted repair event has conflicting root or lineage")
        if event["native_source_id"] != intent.key:
            raise ConflictError("budgeted repair event must bind the reserved operation key")
        task_id = intent.target.get("task_id")
        if not isinstance(task_id, str) or task_id != event["source_task_id"]:
            raise ConflictError("budgeted repair event must bind the reserved operation task")
        member = self.connection.execute(
            "SELECT generation, finding_ids_json FROM managed_members WHERE board_id = ? AND anchor_task_id = ? AND task_id = ?",
            (board, anchor, task_id),
        ).fetchone()
        if member is None or member["generation"] != event["generation"]:
            raise ConflictError("budgeted repair event does not match a registered member generation")
        if event["finding_id"] != "__general_attempt__" and event["finding_id"] not in _decode(member["finding_ids_json"]):
            raise ConflictError("budgeted repair event finding is not associated with source member")
        operation_payload, event_payload = _json(intent.to_dict()), _json(dict(event))
        category = event["event_id"].split(":", 1)[0]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT intent_json FROM operation_intents WHERE board_id = ? AND anchor_task_id = ? AND operation_key = ?",
                (board, anchor, intent.key),
            ).fetchone()
            if row is not None:
                stored = OperationIntent.from_dict(_decode(row[0]))
                stored_identity, requested_identity = stored.to_dict(), intent.to_dict()
                for field in ("outcome", "readback", "phase"):
                    stored_identity.pop(field)
                    requested_identity.pop(field)
                if stored_identity != requested_identity:
                    raise ConflictError("operation key conflicts with existing intent")
                charge = self.connection.execute(
                    "SELECT event_json FROM budget_events WHERE board_id = ? AND anchor_task_id = ? AND event_id = ?",
                    (board, anchor, event["event_id"]),
                ).fetchone()
                if charge is None or charge["event_json"] != event_payload:
                    raise ConflictError("existing repair operation lacks its exact budget admission")
                self.connection.commit()
                return stored
            source = self.connection.execute(
                "SELECT 1 FROM budget_events WHERE board_id = ? AND anchor_task_id = ? AND source_kind = ? AND native_source_id = ?",
                (board, anchor, "native_operation", intent.key),
            ).fetchone()
            if source is not None:
                raise ConflictError("budgeted repair operation key already has immutable source evidence")
            category_events = tuple(
                (stored["event_id"], stored)
                for stored in (_decode(row[0]) for row in self.connection.execute("SELECT event_json FROM budget_events WHERE board_id = ? AND anchor_task_id = ?", (board, anchor)))
                if stored["finding_id"] == event["finding_id"] and stored["event_id"].split(":", 1)[0] == category
            )
            reconciled_ids = {row[0] for row in self.connection.execute(
                "SELECT event_id FROM budget_reconciliation_evidence WHERE board_id = ? AND anchor_task_id = ? AND evidence_kind = 'pre_start_proof'",
                (board, anchor),
            )}
            charged = sum(stored["count"] for _, stored in category_events)
            released = sum(stored["count"] for event_id, stored in category_events if event_id in reconciled_ids)
            if charged - released + 1 > policy_limit:
                raise ConflictError("budget policy limit is exhausted")
            self.connection.execute("INSERT INTO operation_intents VALUES (?, ?, ?, ?)", (board, anchor, intent.key, operation_payload))
            self.connection.execute("INSERT INTO budget_events VALUES (?, ?, ?, ?, ?, ?)", (board, anchor, event["event_id"], "native_operation", intent.key, event_payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return intent

    def reserve_paid_release(
        self, scope: Mapping[str, Any], intent: OperationIntent,
        event: Mapping[str, Any], *, policy_limit: int,
    ) -> OperationIntent:
        """Atomically journal a paid-release intent and retain its capacity charge.

        This only reserves evidence before native I/O; it neither performs nor
        authorizes a release. Caller-supplied data is validated as attribution,
        not treated as native verification.
        """
        self._require_schema()
        board, anchor = _scope_values(scope)
        if not isinstance(intent, OperationIntent):
            raise ValueError("paid release requires an OperationIntent")
        self._reject_local_authority_effect(intent)
        if not isinstance(policy_limit, int) or isinstance(policy_limit, bool) or policy_limit < 0:
            raise ValueError("paid release policy limit must be a non-negative integer")
        if intent.effect != "release" or dict(intent.scope) != {"board_id": board, "anchor_task_id": anchor}:
            raise ConflictError("paid release intent effect or scope is invalid")
        target = dict(intent.target)
        fields = {"task_id", "request_id", "member_generation", "profile"}
        if set(target) != fields or not all(_nonempty_string(target.get(k)) for k in ("task_id", "request_id", "profile")):
            raise ValueError("paid release target must bind task, request, generation, and profile")
        generation = target["member_generation"]
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("paid release member generation must be a non-negative integer")
        if intent.phase != "pending" or intent.outcome is not None or intent.readback is not None:
            raise ConflictError("paid release reservation must be pending before native I/O")
        if not isinstance(event, Mapping) or set(event) != {"event_id", "lineage_id", "root_task_id", "finding_id", "generation", "source_task_id", "source_kind", "native_source_id", "count"}:
            raise ValueError("paid release event has an invalid attribution shape")
        if not all(_nonempty_string(event.get(k)) for k in ("event_id", "lineage_id", "root_task_id", "finding_id", "source_task_id", "source_kind", "native_source_id")):
            raise ValueError("paid release event requires non-empty attribution identities")
        if event["source_kind"] != "native_operation" or event["native_source_id"] != intent.key or type(event["count"]) is not int or event["count"] != 1:
            raise ConflictError("paid release event must charge one unit to its operation key")
        if not isinstance(event["generation"], int) or isinstance(event["generation"], bool) or event["generation"] < 0:
            raise ValueError("paid release event generation must be a non-negative integer")
        if event["event_id"] != f"paid_capacity:{intent.key}" or event["root_task_id"] != anchor or event["finding_id"] != "__general_attempt__" or event["lineage_id"] != f"{anchor}:__general_attempt__":
            raise ConflictError("paid release event category, root, or lineage is invalid")
        if event["source_task_id"] != target["task_id"] or event["generation"] != generation:
            raise ConflictError("paid release event planner task or generation differs from intent")
        op_payload, event_payload = _json(intent.to_dict()), _json(dict(event))
        member = self.connection.execute(
            "SELECT role, generation, work_association FROM managed_members WHERE board_id=? AND anchor_task_id=? AND task_id=?",
            (board, anchor, target["task_id"]),
        ).fetchone()
        if member is None or member["role"] not in {"planner", "paid_review"} or member["generation"] != generation:
            raise ConflictError("paid release task is not the matching managed paid member")
        if member["work_association"] != target["request_id"]:
            raise ConflictError("paid release request does not match planner work association")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            old = self.connection.execute("SELECT intent_json FROM operation_intents WHERE board_id=? AND anchor_task_id=? AND operation_key=?", (board, anchor, intent.key)).fetchone()
            charge = self.connection.execute("SELECT event_json FROM budget_events WHERE board_id=? AND anchor_task_id=? AND event_id=?", (board, anchor, event["event_id"])).fetchone()
            if old is not None:
                stored = OperationIntent.from_dict(_decode(old[0]))
                stored_identity, requested_identity = stored.to_dict(), intent.to_dict()
                for field in ("outcome", "readback", "phase"):
                    stored_identity.pop(field)
                    requested_identity.pop(field)
                if stored_identity != requested_identity or charge is None or charge[0] != event_payload:
                    raise ConflictError("paid release retry conflicts with existing intent or charge")
                self.connection.commit()
                return stored
            source = self.connection.execute("SELECT event_json FROM budget_events WHERE board_id=? AND anchor_task_id=? AND source_kind=? AND native_source_id=?", (board, anchor, "native_operation", intent.key)).fetchone()
            if source is not None or charge is not None:
                raise ConflictError("paid release source or event identity is already attributed")
            charged = _paid_capacity_total(self.connection, board, anchor)
            if charged + 1 > policy_limit:
                raise ConflictError("paid release capacity policy limit is exhausted")
            self.connection.execute("INSERT INTO budget_events VALUES (?, ?, ?, ?, ?, ?)", (board, anchor, event["event_id"], "native_operation", intent.key, event_payload))
            self.connection.execute("INSERT INTO operation_intents VALUES (?, ?, ?, ?)", (board, anchor, intent.key, op_payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return intent

    def reserve_active_piece_release(self, scope: Mapping[str, Any], intent: OperationIntent, event: Mapping[str, Any], *, policy_limit: int) -> OperationIntent:
        """Atomically admit one held implementation piece before its native unblock."""
        self._require_schema()
        board, anchor = _scope_values(scope)
        target = dict(intent.target)
        target_fields = {"task_id", "ticket_id", "plan_id", "member_generation", "profile", "held_digest"}
        event_fields = {"event_id", "lineage_id", "root_task_id", "finding_id", "generation", "source_task_id", "source_kind", "native_source_id", "count"}
        if (intent.effect != "release" or dict(intent.scope) != {"board_id": board, "anchor_task_id": anchor}
                or set(target) != target_fields or intent.phase != "pending" or intent.outcome is not None or intent.readback is not None):
            raise ConflictError("active-piece release intent is malformed")
        if (not all(_nonempty_string(target.get(k)) for k in ("task_id", "ticket_id", "plan_id", "profile", "held_digest"))
                or type(target["member_generation"]) is not int or target["member_generation"] < 0):
            raise ConflictError("active-piece release target is malformed")
        if (not isinstance(event, Mapping) or set(event) != event_fields
                or event.get("event_id") != f"{IMPLEMENTATION_ATTEMPTS}:{intent.key}"
                or event.get("lineage_id") != f"{anchor}:{GENERAL_ATTEMPT}"
                or event.get("root_task_id") != anchor or event.get("finding_id") != GENERAL_ATTEMPT
                or event.get("source_kind") != "native_operation" or event.get("native_source_id") != intent.key
                or event.get("source_task_id") != target["task_id"] or event.get("generation") != target["member_generation"]
                or event.get("count") != 1):
            raise ConflictError("active-piece release budget attribution is malformed")
        if not isinstance(policy_limit, int) or isinstance(policy_limit, bool) or policy_limit < 0:
            raise ValueError("active-piece release policy limit must be non-negative")
        member = self.connection.execute("SELECT role,generation FROM managed_members WHERE board_id=? AND anchor_task_id=? AND task_id=?", (board, anchor, target["task_id"])).fetchone()
        if member is None or member["role"] != "implementation" or member["generation"] != target["member_generation"]:
            raise ConflictError("active-piece release member is not the exact held implementation member")
        payload, event_payload = _json(intent.to_dict()), _json(dict(event))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            old = self.connection.execute("SELECT intent_json FROM operation_intents WHERE board_id=? AND anchor_task_id=? AND operation_key=?", (board, anchor, intent.key)).fetchone()
            charge = self.connection.execute("SELECT event_json FROM budget_events WHERE board_id=? AND anchor_task_id=? AND event_id=?", (board, anchor, event["event_id"])).fetchone()
            if old is not None:
                stored = OperationIntent.from_dict(_decode(old[0]))
                previous, requested = stored.to_dict(), intent.to_dict()
                for field in ("outcome", "readback", "phase"):
                    previous.pop(field); requested.pop(field)
                if previous != requested or charge is None or charge[0] != event_payload:
                    raise ConflictError("active-piece release retry conflicts with immutable admission")
                self.connection.commit()
                return stored
            rows = (_decode(row[0]) for row in self.connection.execute("SELECT event_json FROM budget_events WHERE board_id=? AND anchor_task_id=?", (board, anchor)))
            used = sum(row["count"] for row in rows if row["event_id"].split(":", 1)[0] == IMPLEMENTATION_ATTEMPTS and row["finding_id"] == GENERAL_ATTEMPT)
            if used + 1 > policy_limit:
                raise ConflictError("implementation attempt capacity is exhausted")
            self.connection.execute("INSERT INTO operation_intents VALUES (?, ?, ?, ?)", (board, anchor, intent.key, payload))
            self.connection.execute("INSERT INTO budget_events VALUES (?, ?, ?, ?, ?, ?)", (board, anchor, event["event_id"], "native_operation", intent.key, event_payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return intent

    def bind_paid_release_to_native_run(
        self, scope: Mapping[str, Any], release_operation_key: str, task_id: str,
        member_generation: int, profile: str, run_id: str, session_id: str,
        native_run: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Immutably corroborate one already charged, applied release with a run.

        This records evidence only; ownership/authorization of the native run is
        the coordinator's responsibility. The observed run payload is immutable.
        """
        from .budgets import classify_run_start
        self._require_schema()
        board, anchor = _scope_values(scope)
        if not all(_nonempty_string(value) for value in (release_operation_key, task_id, profile, run_id, session_id)):
            raise ValueError("release, task, profile, run, and session identities must be non-empty strings")
        if not isinstance(member_generation, int) or isinstance(member_generation, bool) or member_generation < 0:
            raise ValueError("member_generation must be a non-negative integer")
        if not isinstance(native_run, Mapping):
            raise ValueError("native_run must be a mapping")
        run = dict(native_run)
        if not all(_nonempty_string(run.get(field)) for field in ("id", "task_id", "profile", "status")):
            raise ConflictError("native run requires exact non-empty id, task, profile, and status")
        if run["id"] != run_id or run["task_id"] != task_id or run["profile"] != profile:
            raise ConflictError("native run identity differs from requested binding")
        if "worker_session_id" in run and run["worker_session_id"] != session_id:
            raise ConflictError("native run session differs from requested binding")
        classification = classify_run_start(run)
        if classification == "pre_start_failure":
            raise ConflictError("a pre-start failure cannot be bound to paid capacity")
        canonical = _json(run)
        digest = _sha256(run)
        request_identity = None
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            intent_row = self.connection.execute("SELECT intent_json FROM operation_intents WHERE board_id=? AND anchor_task_id=? AND operation_key=?", (board, anchor, release_operation_key)).fetchone()
            if intent_row is None:
                raise ConflictError("paid release reservation is missing")
            intent = OperationIntent.from_dict(_decode(intent_row[0]))
            target = dict(intent.target)
            event_row = self.connection.execute("SELECT event_json FROM budget_events WHERE board_id=? AND anchor_task_id=? AND source_kind='native_operation' AND native_source_id=?", (board, anchor, release_operation_key)).fetchone()
            if (intent.effect != "release" or intent.phase != "applied" or intent.outcome not in {"verified", "no-op"}
                    or target.get("task_id") != task_id or target.get("member_generation") != member_generation
                    or target.get("profile") != profile or not _nonempty_string(target.get("request_id")) or event_row is None):
                raise ConflictError("binding requires the exact applied paid release and reservation")
            _validate_paid_receipt(intent)
            intent, event, member = _paid_release_sources(self.connection, board, anchor, release_operation_key)
            if intent.phase != "applied" or intent.outcome not in {"verified", "no-op"}:
                raise ConflictError("paid release reservation event is malformed")
            request_identity = target["request_id"]

            existing = self.connection.execute("SELECT * FROM paid_release_run_bindings WHERE board_id=? AND anchor_task_id=? AND release_operation_key=?", (board, anchor, release_operation_key)).fetchone()
            identity = (task_id, member_generation, request_identity, run_id, session_id, profile, classification, canonical, digest)
            if existing is not None:
                stored = tuple(existing[k] for k in ("task_id", "member_generation", "request_identity", "native_run_id", "native_session_id", "native_profile", "run_classification", "native_run_json", "native_run_sha256"))
                if stored != identity:
                    raise ConflictError("paid release binding conflicts with immutable existing evidence")
                self.connection.commit()
                return self.read_paid_release_run_binding(scope, release_operation_key)
            self.connection.execute("INSERT INTO paid_release_run_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (board, anchor, release_operation_key, *identity))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return self.read_paid_release_run_binding(scope, release_operation_key)

    def read_paid_release_run_binding(self, scope: Mapping[str, Any], release_operation_key: str) -> Mapping[str, Any]:
        """Read and strictly validate a binding against its immutable source rows."""
        self._require_schema()
        board, anchor = _scope_values(scope)
        if not _nonempty_string(release_operation_key):
            raise ValueError("release_operation_key must be non-empty")
        row = self.connection.execute("SELECT * FROM paid_release_run_bindings WHERE board_id=? AND anchor_task_id=? AND release_operation_key=?", (board, anchor, release_operation_key)).fetchone()
        if row is None:
            raise KeyError("paid release run binding does not exist")
        try:
            required_text = ("board_id", "anchor_task_id", "release_operation_key", "task_id", "request_identity", "native_run_id", "native_session_id", "native_profile", "run_classification", "native_run_json", "native_run_sha256")
            if any(not _nonempty_string(row[field]) for field in required_text) or type(row["member_generation"]) is not int or row["member_generation"] < 0:
                raise ValueError("binding row fields are malformed")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", row["native_run_id"]):
                raise ValueError("binding run identifier is malformed")
            raw = row["native_run_json"]
            run = _decode(raw)
            if not isinstance(run, dict) or _json(run) != raw or _sha256(run) != row["native_run_sha256"]:
                raise ValueError("paid release binding run payload or hash is corrupt")
            if not all(_nonempty_string(run.get(field)) for field in ("id", "task_id", "profile", "status")):
                raise ValueError("native run fields are malformed")
            intent, event, member = _paid_release_sources(self.connection, board, anchor, release_operation_key)
            target = dict(intent.target)
            _validate_paid_receipt(intent)
            if (intent.effect != "release" or intent.phase != "applied" or intent.outcome not in {"verified", "no-op"}
                    or target != {"task_id": row["task_id"], "request_id": row["request_identity"], "member_generation": row["member_generation"], "profile": row["native_profile"]}

                    or member is None or member["role"] != "planner" or member["generation"] != row["member_generation"] or member["work_association"] != row["request_identity"]
                    or run.get("id") != row["native_run_id"] or run.get("task_id") != row["task_id"] or run.get("profile") != row["native_profile"]
                    or ("worker_session_id" in run and (not _nonempty_string(run["worker_session_id"]) or run["worker_session_id"] != row["native_session_id"]))):
                raise ValueError("binding identities do not reconstruct from immutable source evidence")
            from .budgets import classify_run_start
            if classify_run_start(run) != row["run_classification"] or row["run_classification"] == "pre_start_failure":
                raise ValueError("binding run classification is inconsistent")
        except (TypeError, KeyError, ValueError, IndexError, UnicodeError) as error:
            raise SchemaError("paid release binding source evidence is malformed") from error
        return {"scope": {"board_id": board, "anchor_task_id": anchor}, "release_operation_key": release_operation_key,
                "task_id": row["task_id"], "member_generation": row["member_generation"], "request_identity": row["request_identity"],
                "run_id": row["native_run_id"], "session_id": row["native_session_id"], "profile": row["native_profile"],
                "classification": row["run_classification"], "native_run": run, "native_run_sha256": row["native_run_sha256"]}

    def _operation(self, scope: Mapping[str, Any], key: str) -> OperationIntent:
        board, anchor = _scope_values(scope)
        row = self.connection.execute(
            "SELECT intent_json FROM operation_intents WHERE board_id = ? AND anchor_task_id = ? AND operation_key = ?",
            (board, anchor, key),
        ).fetchone()
        if row is None:
            raise KeyError("operation intent is not reserved in this scope")
        return OperationIntent.from_dict(_decode(row[0]))

    def begin_effect_attempt(self, scope: Mapping[str, Any], key: str) -> OperationIntent:
        """Durably fence a supported native effect before external I/O.

        The transaction changes ``pending`` to conservative ``unknown`` before
        any provider call.  A crash after the commit is therefore fail-closed:
        later workers must reconcile, never resend the scoped idempotency key.
        """
        self._require_schema()
        board, anchor = _scope_values(scope)
        supported = frozenset({"create_held", "hold", "hold_task", "release", "unhold_task", "stop_run", "comment", "link", "request_review", "git_integrate"})
        self._reject_local_authority_effect(self._operation({"board_id": board, "anchor_task_id": anchor}, key))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._operation({"board_id": board, "anchor_task_id": anchor}, key)
            if current.effect not in supported:
                raise ConflictError("effect attempt is unsupported for durable fencing")
            if current.phase == "unknown":
                raise ConflictError("operation effect is unknown and must be reconciled before retry")
            if current.phase != "pending":
                raise ConflictError("only a pending effect can begin an attempt")
            attempted = OperationIntent(
                **{**current.to_dict(), "outcome": "ambiguous", "readback": None, "phase": "unknown"}
            )
            self.connection.execute(
                "UPDATE operation_intents SET intent_json = ? WHERE board_id = ? AND anchor_task_id = ? AND operation_key = ?",
                (_json(attempted.to_dict()), board, anchor, key),
            )
            self.connection.commit()
            return attempted
        except BaseException:
            self.connection.rollback()
            raise

    def record_pre_send_attempt(self, scope: Mapping[str, Any], key: str, *,
                                native_operation_identity: Mapping[str, Any], command_started: int) -> dict[str, Any]:
        """Append the v2 command lower bound before a supported native send.

        This is immutable timing evidence, not an observation or effect authority.
        A pending operation may retain it after a crash before the unknown claim;
        an unknown operation can be verified only when the exact same evidence is
        present on reopen.
        """
        self._require_schema()
        board, anchor = _scope_values(scope)
        operation = self._operation({"board_id": board, "anchor_task_id": anchor}, key)
        if (operation.effect != "link" or operation.target.get("kind") != "accepted_active_tranche_native_link_v2"
                or operation.phase not in {"pending", "unknown"}):
            raise ConflictError("pre-send timing is only supported for a v2 accepted link")
        if not isinstance(native_operation_identity, Mapping) or type(command_started) is not int or command_started < 0:
            raise ValueError("pre-send timing evidence is malformed")
        identity = dict(native_operation_identity)
        required = {"action_key", "effect", "source_task_id", "target_task_id"}
        if (set(identity) != required or identity["action_key"] != key or identity["effect"] != "link"
                or not all(_nonempty_string(identity[name]) for name in required)):
            raise ValueError("pre-send native operation identity is malformed")
        observation = {"kind": "accepted_active_tranche_native_link_v2_pre_send",
                       "operation_key": key, "native_operation_identity": identity,
                       "command_started": command_started}
        payload, observation_id = _json(observation), _canonical_identity(observation)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute("INSERT OR IGNORE INTO effect_observations VALUES (?, ?, ?, ?, ?)",
                                    (board, anchor, key, observation_id, payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return observation

    def read_pre_send_attempt(self, scope: Mapping[str, Any], key: str) -> dict[str, Any] | None:
        """Return one exact v2 timing record; legacy/ambiguous journals return none."""
        self._require_schema()
        board, anchor = _scope_values(scope)
        rows = self.connection.execute(
            "SELECT observation_json FROM effect_observations WHERE board_id=? AND anchor_task_id=? AND operation_key=?",
            (board, anchor, key),
        ).fetchall()
        matches = []
        for row in rows:
            value = _decode(row[0])
            if isinstance(value, dict) and value.get("kind") == "accepted_active_tranche_native_link_v2_pre_send":
                matches.append(value)
        if len(matches) != 1:
            return None
        value = matches[0]
        try:
            identity = value["native_operation_identity"]
            if (set(value) != {"kind", "operation_key", "native_operation_identity", "command_started"}
                    or value["operation_key"] != key or not isinstance(identity, dict)
                    or type(value["command_started"]) is not int or value["command_started"] < 0):
                return None
            return {"kind": value["kind"], "operation_key": value["operation_key"],
                    "native_operation_identity": dict(identity), "command_started": value["command_started"]}
        except (KeyError, TypeError):
            return None

    def record_validated_link_receipt_checkpoint(self, scope: Mapping[str, Any], key: str,
                                                 receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Durably retain one already-validated v2 link completion receipt.

        This is deliberately separate from generic observation timing.  It is
        written only after the coordinator has validated the adapter's exact
        returned transition and before acknowledgement, so an unknown operation
        can never be acknowledged from a later equivalent native edge alone.
        """
        self._require_schema()
        board, anchor = _scope_values(scope)
        operation = self._operation({"board_id": board, "anchor_task_id": anchor}, key)
        if (operation.effect != "link" or operation.target.get("kind") != "accepted_active_tranche_native_link_v2"
                or operation.phase != "unknown" or not isinstance(receipt, Mapping)):
            raise ConflictError("validated receipt checkpoint requires an attempted v2 accepted link")
        checkpoint = {"kind": "accepted_active_tranche_native_link_v2_receipt_checkpoint",
                      "operation_key": key, "receipt": dict(receipt)}
        payload, identity = _json(checkpoint), _canonical_identity(checkpoint)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                "SELECT observation_json FROM effect_observations WHERE board_id=? AND anchor_task_id=? AND operation_key=?",
                (board, anchor, key),
            ).fetchall()
            prior = [_decode(row[0]) for row in rows]
            matches = [item for item in prior if isinstance(item, dict)
                       and item.get("kind") == checkpoint["kind"]]
            if len(matches) > 1 or (matches and matches[0] != checkpoint):
                raise ConflictError("validated receipt checkpoint conflicts with existing immutable evidence")
            if not matches:
                self.connection.execute("INSERT INTO effect_observations VALUES (?, ?, ?, ?, ?)",
                                        (board, anchor, key, identity, payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return checkpoint

    def read_validated_link_receipt_checkpoint(self, scope: Mapping[str, Any], key: str) -> dict[str, Any] | None:
        """Reconstruct exactly one completion checkpoint; malformed history is unusable."""
        self._require_schema()
        board, anchor = _scope_values(scope)
        rows = self.connection.execute(
            "SELECT observation_json FROM effect_observations WHERE board_id=? AND anchor_task_id=? AND operation_key=?",
            (board, anchor, key),
        ).fetchall()
        matches = []
        for row in rows:
            value = _decode(row[0])
            if isinstance(value, dict) and value.get("kind") == "accepted_active_tranche_native_link_v2_receipt_checkpoint":
                matches.append(value)
        if len(matches) != 1:
            return None
        checkpoint = matches[0]
        if (set(checkpoint) != {"kind", "operation_key", "receipt"} or checkpoint.get("operation_key") != key
                or not isinstance(checkpoint.get("receipt"), dict)
                or _json(checkpoint) != _json({"kind": checkpoint["kind"], "operation_key": key,
                                                "receipt": checkpoint["receipt"]})):
            return None
        return {"kind": checkpoint["kind"], "operation_key": key, "receipt": dict(checkpoint["receipt"])}

    def record_effect_observation(self, scope: Mapping[str, Any], key: str, *, outcome: str, details: str, readback: Mapping[str, Any] | None) -> dict[str, Any]:
        """Append exact native result evidence without falsifying journal phase."""
        self._require_schema()
        board, anchor = _scope_values(scope)
        if not _nonempty_string(outcome) or not _nonempty_string(details):
            raise ValueError("effect observation requires non-empty outcome and details")
        if readback is not None and not isinstance(readback, Mapping):
            raise ValueError("effect observation readback must be a mapping or null")
        ActionResult(key, outcome, details, readback)
        self._reject_local_authority_effect(self._operation({"board_id": board, "anchor_task_id": anchor}, key))
        observation = {"operation_key": key, "outcome": outcome, "details": details,
                       "readback": None if readback is None else dict(readback)}
        payload = _json(observation)
        identity = _canonical_identity(observation)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT OR IGNORE INTO effect_observations VALUES (?, ?, ?, ?, ?)",
                (board, anchor, key, identity, payload),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return observation

    def record_pause_cycle_observation(self, intent: PauseIntent,
                                       snapshots: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
        """Append exact pre-pause snapshots as immutable local evidence, not an ack."""
        self._require_schema()
        board, anchor = _scope_values(intent.scope)
        if (not intent.active or intent.resuming or not intent.managed_task_ids
                or set(snapshots) != set(intent.managed_task_ids)
                or set(intent.baseline_digests) != set(intent.managed_task_ids)):
            raise ValueError("pause-cycle observation must bind one complete active pause")
        observations: dict[str, dict[str, Any]] = {}
        for task_id in sorted(intent.managed_task_ids):
            raw = snapshots.get(task_id)
            if not isinstance(raw, Mapping):
                raise ValueError("pause-cycle observation snapshot is malformed")
            snapshot = BoardSnapshot.from_dict(raw)
            canonical = snapshot.to_dict()
            if canonical != dict(raw) or snapshot.native_task.get("id") != task_id or snapshot.digest != intent.baseline_digests[task_id]:
                raise ValueError("pause-cycle observation does not match the persisted baseline")
            observations[task_id] = canonical
        observation = {
            "kind": "local_pause_observation_v1",
            "scope": {"board_id": board, "anchor_task_id": anchor},
            "generation": intent.generation,
            "baseline_digests": dict(intent.baseline_digests),
            "observations": observations,
        }
        payload, identity = _json(observation), _canonical_identity(observation)
        key = f"local-pause-observation:v1:{intent.generation}"
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                "SELECT observation_json FROM effect_observations WHERE board_id=? AND anchor_task_id=? AND operation_key=?",
                (board, anchor, key),
            ).fetchall()
            if len(rows) > 1 or (rows and rows[0][0] != payload):
                raise ConflictError("pause-cycle observation conflicts with immutable evidence")
            if not rows:
                self.connection.execute("INSERT INTO effect_observations VALUES (?, ?, ?, ?, ?)",
                                        (board, anchor, key, identity, payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return observation

    def read_pause_cycle_observation(self, intent: PauseIntent, *, generation: int) -> Mapping[str, BoardSnapshot] | None:
        """Return one completely baseline-bound cycle-start snapshot set, or none."""
        self._require_schema()
        board, anchor = _scope_values(intent.scope)
        if type(generation) is not int or generation < 0:
            raise ValueError("pause-cycle generation must be a non-negative integer")
        key = f"local-pause-observation:v1:{generation}"
        rows = self.connection.execute(
            "SELECT observation_json FROM effect_observations WHERE board_id=? AND anchor_task_id=? AND operation_key=?",
            (board, anchor, key),
        ).fetchall()
        if len(rows) != 1:
            return None
        try:
            raw = rows[0][0]
            value = _decode(raw)
            expected = {"kind", "scope", "generation", "baseline_digests", "observations"}
            if (not isinstance(value, dict) or set(value) != expected or _json(value) != raw
                    or value["kind"] != "local_pause_observation_v1"
                    or value["scope"] != {"board_id": board, "anchor_task_id": anchor}
                    or value["generation"] != generation
                    or value["baseline_digests"] != dict(intent.baseline_digests)
                    or not isinstance(value["observations"], dict)
                    or set(value["observations"]) != set(intent.managed_task_ids)):
                return None
            snapshots: dict[str, BoardSnapshot] = {}
            for task_id in intent.managed_task_ids:
                item = value["observations"][task_id]
                snapshot = BoardSnapshot.from_dict(item)
                if (snapshot.to_dict() != item or snapshot.native_task.get("id") != task_id
                        or snapshot.digest != intent.baseline_digests.get(task_id)):
                    return None
                snapshots[task_id] = snapshot
            return snapshots
        except (KeyError, TypeError, ValueError, SchemaError):
            return None

    def observe_effect(self, scope: Mapping[str, Any], key: str, *, outcome: str | None, readback: Mapping[str, Any] | None, phase: str) -> OperationIntent:
        self._require_schema()
        if phase == "applied" and (outcome not in {"verified", "no-op"} or not isinstance(readback, Mapping) or not readback):
            raise ValueError("applied effects require a successful outcome and non-empty verified readback")
        if phase == "unknown" and (outcome != "ambiguous" or readback is not None):
            raise ValueError("unknown effects require an ambiguous outcome without verified readback")
        board, anchor = _scope_values(scope)
        self._reject_local_authority_effect(self._operation({"board_id": board, "anchor_task_id": anchor}, key))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._operation(scope, key)
            updated = OperationIntent(**{**current.to_dict(), "outcome": outcome, "readback": readback, "phase": phase})
            if current.phase == "applied":
                if current == updated:
                    self.connection.commit()
                    return current
                raise ConflictError("an applied effect cannot be replaced by a later observation")
            if current.phase == "unknown" and phase != "applied":
                raise ConflictError("an unknown effect must be reconciled to applied, not replayed")
            self.connection.execute("UPDATE operation_intents SET intent_json = ? WHERE board_id = ? AND anchor_task_id = ? AND operation_key = ?", (_json(updated.to_dict()), board, anchor, key))
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def ack_effect(self, scope: Mapping[str, Any], key: str, *, readback: Mapping[str, Any], outcome: str = "verified") -> OperationIntent:
        self._reject_local_authority_effect(self._operation(scope, key))
        return self.observe_effect(scope, key, outcome=outcome, readback=readback, phase="applied")

    @staticmethod
    def _reject_local_authority_effect(intent: OperationIntent) -> None:
        if intent.effect == "accept_validated_plan":
            raise ConflictError("local plan acceptance authority cannot enter generic native-effect or admission APIs")

    def pending_operations(self, scope: Mapping[str, Any]) -> tuple[OperationIntent, ...]:
        self._require_schema()
        board, anchor = _scope_values(scope)
        rows = self.connection.execute(
            "SELECT intent_json FROM operation_intents WHERE board_id = ? AND anchor_task_id = ? ORDER BY operation_key",
            (board, anchor),
        )
        return tuple(intent for row in rows if (intent := OperationIntent.from_dict(_decode(row[0]))).phase != "applied")

    def record_budget_event(self, scope: Mapping[str, Any], event: Mapping[str, Any], *, policy_limit: int | None = None, run_classification: str | None = None) -> Mapping[str, Any]:
        self._require_schema()
        board, anchor = _scope_values(scope)
        if isinstance(event, Mapping) and event.get("source_kind") == "native_operation" and _nonempty_string(event.get("native_source_id")):
            source = self.connection.execute("SELECT intent_json FROM operation_intents WHERE board_id=? AND anchor_task_id=? AND operation_key=?", (board, anchor, event["native_source_id"])).fetchone()
            if source is not None:
                self._reject_local_authority_effect(OperationIntent.from_dict(_decode(source[0])))
        required = {"event_id", "lineage_id", "root_task_id", "finding_id", "generation", "source_task_id", "source_kind", "native_source_id", "count"}
        if not isinstance(event, Mapping) or set(event) != required:
            raise ValueError("budget event has an invalid attribution shape")
        if not all(_nonempty_string(event[field]) for field in ("event_id", "lineage_id", "root_task_id", "finding_id", "source_task_id", "source_kind", "native_source_id")):
            raise ValueError("budget event requires non-empty identity and source")
        if event["source_kind"] not in {"native_run", "native_operation"}:
            raise ValueError("budget event source_kind is unsupported")
        if not isinstance(event["generation"], int) or isinstance(event["generation"], bool) or event["generation"] < 0:
            raise ValueError("budget event generation must be a non-negative integer")
        if not isinstance(event["count"], int) or isinstance(event["count"], bool) or event["count"] <= 0:
            raise ValueError("budget event count must be a positive integer")
        if policy_limit is not None and (not isinstance(policy_limit, int) or isinstance(policy_limit, bool) or policy_limit < 0):
            raise ValueError("budget policy limit must be a non-negative integer")
        if run_classification is not None and run_classification not in {"confirmed_started", "unknown_active"}:
            raise ValueError("budget charge classification is unsupported")
        if event["root_task_id"] != anchor:
            raise ConflictError("budget event root task must be the scope anchor")
        expected_lineage = f"{anchor}:{event['finding_id']}"
        if event["lineage_id"] != expected_lineage:
            raise ConflictError("budget event lineage must be derived from anchor and finding")
        member = self.connection.execute(
            "SELECT generation, finding_ids_json FROM managed_members WHERE board_id = ? AND anchor_task_id = ? AND task_id = ?",
            (board, anchor, event["source_task_id"]),
        ).fetchone()
        if member is None:
            raise ConflictError("budget event source task is not a registered managed member")
        if member["generation"] != event["generation"]:
            raise ConflictError("budget event generation does not match source member")
        if event["finding_id"] != "__general_attempt__" and event["finding_id"] not in _decode(member["finding_ids_json"]):
            raise ConflictError("budget event finding is not associated with source member")
        payload = _json(dict(event))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT event_json FROM budget_events WHERE board_id = ? AND anchor_task_id = ? AND event_id = ?", (board, anchor, event["event_id"])).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise ConflictError("budget event conflicts with existing lineage evidence")
                self.connection.commit()
                return dict(event)
            source = self.connection.execute("SELECT event_json FROM budget_events WHERE board_id=? AND anchor_task_id=? AND source_kind=? AND native_source_id=?", (board, anchor, event["source_kind"], event["native_source_id"])).fetchone()
            if source is not None:
                raise ConflictError("budget event conflicts with immutable native source evidence")
            if policy_limit is not None:
                category = event["event_id"].split(":", 1)[0]
                category_events = tuple(
                    (stored["event_id"], stored)
                    for stored in (_decode(row[0]) for row in self.connection.execute("SELECT event_json FROM budget_events WHERE board_id=? AND anchor_task_id=?", (board, anchor)))
                    if stored["finding_id"] == event["finding_id"] and stored["event_id"].split(":", 1)[0] == category
                )
                charged = sum(stored["count"] for _, stored in category_events)
                reconciled_ids = {
                    row[0]
                    for row in self.connection.execute(
                        "SELECT event_id FROM budget_reconciliation_evidence WHERE board_id=? AND anchor_task_id=? AND evidence_kind='pre_start_proof'",
                        (board, anchor),
                    )
                }
                released = sum(stored["count"] for event_id, stored in category_events if event_id in reconciled_ids)
                if charged - released + event["count"] > policy_limit:
                    raise ConflictError("budget policy limit is exhausted")
            self.connection.execute("INSERT INTO budget_events VALUES (?, ?, ?, ?, ?, ?)", (board, anchor, event["event_id"], event["source_kind"], event["native_source_id"], payload))
            if run_classification is not None:
                self.connection.execute("INSERT INTO budget_reconciliation_evidence VALUES (?, ?, ?, 'charge', ?)", (board, anchor, event["event_id"], _json({"classification": run_classification, "source_task_id": event["source_task_id"], "generation": event["generation"], "finding_id": event["finding_id"], "native_run_id": event["native_source_id"]})))
            self.connection.commit()
        except sqlite3.IntegrityError as error:
            self.connection.rollback()
            existing = self.connection.execute(
                "SELECT event_json FROM budget_events WHERE board_id = ? AND anchor_task_id = ? AND event_id = ?",
                (board, anchor, event["event_id"]),
            ).fetchone()
            if existing is not None and existing["event_json"] == payload:
                return dict(event)
            raise ConflictError("budget event conflicts with immutable native source evidence") from error
        except BaseException:
            self.connection.rollback()
            raise
        return dict(event)

    def reconcile_unknown_budget_run(self, scope: Mapping[str, Any], *, source_task_id: str, generation: int, finding_id: str, native_run_id: str) -> bool:
        """Append only a trusted, exact native re-read proving the run never started."""
        self._require_schema()
        board, anchor = _scope_values(scope)
        if self._read_native_run is None:
            raise ConflictError("budget reconciliation requires a trusted native run reader")
        try:
            run = self._read_native_run({"board_id": board, "anchor_task_id": anchor}, source_task_id, native_run_id)
        except Exception as error:
            raise ConflictError("budget reconciliation trusted native run reread failed") from error
        if not isinstance(run, Mapping):
            raise ConflictError("budget reconciliation trusted native run reread is malformed")
        native_run = dict(run)
        observed_run_id = native_run.get("id")
        if observed_run_id != native_run_id:
            raise ConflictError("budget reconciliation trusted native run identity does not match the charged source")
        if native_run.get("task_id") != source_task_id or native_run.get("board_id") != board or native_run.get("anchor_task_id") != anchor:
            raise ConflictError("budget reconciliation trusted native run provenance does not exactly bind task, board, and anchor")
        if native_run.get("status") not in {"failed", "rejected", "cancelled"} or native_run.get("started_at", "missing") is not None or native_run.get("started") is True or native_run.get("start_time") not in (None, ""):
            raise ConflictError("budget reconciliation requires conclusive trusted native pre-start proof")
        proof = {"source_task_id": source_task_id, "generation": generation, "finding_id": finding_id, "native_run_id": native_run_id, "native_run": native_run, "native_run_sha256": _sha256(native_run)}
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT event_id FROM budget_events WHERE board_id=? AND anchor_task_id=? AND source_kind='native_run' AND native_source_id=?", (board, anchor, native_run_id)).fetchone()
            if row is None:
                raise ConflictError("budget reconciliation native run is not charged")
            charge = self.connection.execute("SELECT evidence_json FROM budget_reconciliation_evidence WHERE board_id=? AND anchor_task_id=? AND event_id=? AND evidence_kind='charge'", (board, anchor, row["event_id"])).fetchone()
            if charge is None or _decode(charge[0]) != {"classification": "unknown_active", "source_task_id": source_task_id, "generation": generation, "finding_id": finding_id, "native_run_id": native_run_id}:
                raise ConflictError("budget reconciliation does not exactly bind an unknown charge")
            prior = self.connection.execute("SELECT 1 FROM budget_reconciliation_evidence WHERE board_id=? AND anchor_task_id=? AND event_id=? AND evidence_kind='pre_start_proof'", (board, anchor, row["event_id"])).fetchone()
            if prior is not None:
                self.connection.commit()
                return False
            self.connection.execute("INSERT INTO budget_reconciliation_evidence VALUES (?, ?, ?, 'pre_start_proof', ?)", (board, anchor, row["event_id"], _json(proof)))
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def budget_reconciled_event_ids(self, scope: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        self._require_schema()
        board, anchor = _scope_values(scope)
        return tuple(_decode(row[0]) for row in self.connection.execute("SELECT b.event_json FROM budget_events b JOIN budget_reconciliation_evidence r ON r.board_id=b.board_id AND r.anchor_task_id=b.anchor_task_id AND r.event_id=b.event_id WHERE b.board_id=? AND b.anchor_task_id=? AND r.evidence_kind='pre_start_proof' ORDER BY b.event_id", (board, anchor)))

    def set_operator_intent(self, intent: PauseIntent, *, authorized_clear: bool = False,
                            resume_decision: Any = None) -> PauseIntent:
        """Persist an active pause or a proven, explicitly authorized clear."""
        self._require_schema()
        if not isinstance(authorized_clear, bool):
            raise ValueError("authorized_clear must be a boolean")
        board, anchor = _scope_values(intent.scope)
        payload = _json(intent.to_dict())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT intent_json FROM operator_intents WHERE board_id = ? AND anchor_task_id = ?", (board, anchor)).fetchone()
            previous = None if row is None else PauseIntent.from_dict(_decode(row[0]))
            if not intent.active:
                if previous != intent:
                    if previous is None or not previous.active:
                        raise ConflictError("operator clear requires an existing active intent")
                    if intent.generation != previous.generation + 1:
                        raise ConflictError("operator clear generation must advance exactly once")
                if not authorized_clear:
                    raise ConflictError("operator clear requires explicit authorization")
                from .operator_controls import ResumeDecision
                if not isinstance(resume_decision, ResumeDecision) or not resume_decision.allowed or resume_decision.intent != intent:
                    raise ConflictError("operator clear requires a matching authorized resume decision")
                if previous == intent:
                    self.connection.commit()
                    return intent
            if previous is not None:
                if previous == intent:
                    self.connection.commit()
                    return previous
                if intent.active and intent.generation <= previous.generation:
                    raise ConflictError("operator intent generation must advance")
                self.connection.execute("UPDATE operator_intents SET intent_json = ? WHERE board_id = ? AND anchor_task_id = ?", (payload, board, anchor))
            else:
                self.connection.execute("INSERT INTO operator_intents VALUES (?, ?, ?)", (board, anchor, payload))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return intent

    def read_scope(self, scope: Mapping[str, Any]) -> dict[str, Any]:
        self._require_schema()
        board, anchor = _scope_values(scope)
        members = tuple(ManagedMember.from_dict({"board_id": board, "anchor_task_id": anchor, "task_id": row["task_id"], "role": row["role"], "generation": row["generation"], "finding_ids": _decode(row["finding_ids_json"]), "work_association": row["work_association"]}) for row in self.connection.execute("SELECT * FROM managed_members WHERE board_id = ? AND anchor_task_id = ? ORDER BY task_id", (board, anchor)))
        candidates = tuple(CandidateIdentity.from_dict(_decode(row[0])) for row in self.connection.execute("SELECT identity_json FROM candidates WHERE board_id = ? AND anchor_task_id = ? ORDER BY content_identity", (board, anchor)))
        reviews = tuple(_decode(row[0]) for row in self.connection.execute("SELECT evidence_json FROM review_evidence WHERE board_id = ? AND anchor_task_id = ? ORDER BY review_id", (board, anchor)))
        operations = tuple(OperationIntent.from_dict(_decode(row[0])) for row in self.connection.execute("SELECT intent_json FROM operation_intents WHERE board_id = ? AND anchor_task_id = ? ORDER BY operation_key", (board, anchor)))
        effect_observations = tuple(
            _decode(row[0]) for row in self.connection.execute(
                "SELECT observation_json FROM effect_observations WHERE board_id = ? AND anchor_task_id = ? "
                "ORDER BY operation_key, observation_identity LIMIT 64", (board, anchor)
            )
        )
        budgets = tuple(_decode(row[0]) for row in self.connection.execute("SELECT event_json FROM budget_events WHERE board_id = ? AND anchor_task_id = ? ORDER BY event_id", (board, anchor)))
        reconciliations = tuple(
            {"event_id": row["event_id"], "evidence_kind": row["evidence_kind"], "evidence": _decode(row["evidence_json"])}
            for row in self.connection.execute("SELECT event_id, evidence_kind, evidence_json FROM budget_reconciliation_evidence WHERE board_id = ? AND anchor_task_id = ? ORDER BY event_id, evidence_kind", (board, anchor))
        )
        reconciled_event_ids = {record["event_id"] for record in reconciliations if record["evidence_kind"] == "pre_start_proof"}
        net_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        for event in budgets:
            category = event["event_id"].split(":", 1)[0]
            key = (event["root_task_id"], event["finding_id"], category)
            row_net = net_rows.setdefault(key, {"root_task_id": key[0], "finding_id": key[1], "category": key[2], "charged": 0, "reconciled": 0, "net": 0})
            row_net["charged"] += event["count"]
            if event["event_id"] in reconciled_event_ids:
                row_net["reconciled"] += event["count"]
            row_net["net"] = row_net["charged"] - row_net["reconciled"]
        budget_net = tuple(net_rows[key] for key in sorted(net_rows))
        row = self.connection.execute("SELECT intent_json FROM operator_intents WHERE board_id = ? AND anchor_task_id = ?", (board, anchor)).fetchone()
        return {"scope": {"board_id": board, "anchor_task_id": anchor}, "members": members, "candidates": candidates, "reviews": reviews, "operations": operations, "effect_observations": effect_observations, "budget_events": budgets, "budget_reconciliations": reconciliations, "budget_net": budget_net, "operator_intent": None if row is None else PauseIntent.from_dict(_decode(row[0]))}


__all__ = ["EvidenceStore", "EvidenceStoreError", "SchemaError"]
