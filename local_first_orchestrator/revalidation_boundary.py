from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, final

from .evidence_hash import canonical_sha256


_CONSTRUCTOR_SENTINEL = object()
_REGISTRY: dict[int, _CapabilityRecord] = {}
_REGISTRY_LOCK = threading.RLock()


@dataclass
class _CapabilityRecord:
    capability: object
    adapter: Any
    board_db_path: str
    configured_board_db_path: str
    filesystem_identity: tuple[int, int]
    connection: sqlite3.Connection
    transaction_mode: str
    owner_pid: int
    owner_thread: int
    local_first_ticket_id: str
    external_task_id: str
    board_name: str
    snapshot: Any
    snapshot_hash: str
    nonce: bytes
    active: bool = True


@final
class _ExactPrivateCapability:
    __slots__ = ("snapshot", "_adapter", "_connection", "_local_first_ticket_id", "_external_task_id")

    def __init__(self, sentinel: object, adapter: Any, connection: sqlite3.Connection, local_first_ticket_id: str, external_task_id: str, snapshot: Any) -> None:
        if sentinel is not _CONSTRUCTOR_SENTINEL:
            raise TypeError("trusted board revalidation capability is not publicly constructible")
        self.snapshot = snapshot
        self._adapter = adapter
        self._connection = connection
        self._local_first_ticket_id = local_first_ticket_id
        self._external_task_id = external_task_id

    # Compatibility seam for old direct-adapter tests. Ledger does not invoke it.
    def _verify_for_ledger(self, task_id: str, external_task_id: str) -> None:
        validate_and_consume_revalidation_capability(
            self, task_id=task_id, external_task_id=external_task_id,
            expected_snapshot_hash=canonical_sha256(asdict(self.snapshot)),
        )


def _canonical_existing_path(path: Any) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise PermissionError("trusted board revalidation board DB path identity is unavailable") from exc
    if not resolved.is_file():
        raise PermissionError("trusted board revalidation board DB path identity is invalid")
    return resolved


def _database_list_path(connection: sqlite3.Connection) -> Path:
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except sqlite3.DatabaseError as exc:
        raise PermissionError("trusted board revalidation board DB identity is unavailable") from exc
    for row in rows:
        if str(row[1]) == "main" and row[2]:
            return _canonical_existing_path(row[2])
    raise PermissionError("trusted board revalidation board DB identity is unavailable")


def _verify_board_identity(record: _CapabilityRecord) -> None:
    configured = _canonical_existing_path(record.configured_board_db_path)
    try:
        configured_stat = configured.stat()
        opened = _database_list_path(record.connection)
        opened_stat = opened.stat()
    except OSError as exc:
        raise PermissionError("trusted board revalidation board DB path identity is unavailable") from exc
    current_identity = (configured_stat.st_dev, configured_stat.st_ino)
    opened_identity = (opened_stat.st_dev, opened_stat.st_ino)
    if (
        str(configured) != record.board_db_path
        or current_identity != record.filesystem_identity
        or opened != configured
        or opened_identity != record.filesystem_identity
    ):
        raise PermissionError("trusted board revalidation board DB identity drift")


def create_revalidation_capability(
    adapter: Any,
    connection: sqlite3.Connection,
    path: Any,
    local_first_ticket_id: str,
    external_task_id: str,
    snapshot: Any,
    board_name: str,
    configured_path: Any | None = None,
) -> _ExactPrivateCapability:
    canonical_path = _canonical_existing_path(path)
    configured = Path(configured_path if configured_path is not None else path).expanduser()
    stat = canonical_path.stat()
    capability = _ExactPrivateCapability(_CONSTRUCTOR_SENTINEL, adapter, connection, local_first_ticket_id, external_task_id, snapshot)
    with _REGISTRY_LOCK:
        record = _CapabilityRecord(
            capability=capability,
            adapter=adapter,
            board_db_path=str(canonical_path),
            configured_board_db_path=str(configured),
            filesystem_identity=(stat.st_dev, stat.st_ino),
            connection=connection,
            transaction_mode="IMMEDIATE",
            owner_pid=os.getpid(),
            owner_thread=threading.get_ident(),
            local_first_ticket_id=local_first_ticket_id,
            external_task_id=external_task_id,
            board_name=board_name,
            snapshot=snapshot,
            snapshot_hash=canonical_sha256(asdict(snapshot)),
            nonce=secrets.token_bytes(32),
        )
        _verify_board_identity(record)
        _REGISTRY[id(capability)] = record
    return capability


def validate_and_consume_revalidation_capability(capability: object, *, task_id: str, external_task_id: str, expected_snapshot_hash: str) -> None:
    if type(capability) is not _ExactPrivateCapability:
        raise PermissionError("native release revalidation requires an exact trusted board capability")
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(id(capability))
        if record is None or record.capability is not capability or not record.active:
            raise PermissionError("trusted board revalidation capability is inactive or unregistered")

        def reject(message: str) -> None:
            record.active = False
            del _REGISTRY[id(capability)]
            raise PermissionError(message)

        if os.getpid() != record.owner_pid or threading.get_ident() != record.owner_thread:
            reject("trusted board revalidation capability owner mismatch")
        if task_id != record.local_first_ticket_id or external_task_id != record.external_task_id or expected_snapshot_hash != record.snapshot_hash:
            reject("trusted board revalidation capability authority mismatch")
        if not record.connection.in_transaction or record.transaction_mode != "IMMEDIATE":
            reject("trusted board revalidation transaction is not active")
        try:
            _verify_board_identity(record)
            locking_mode = str(record.connection.execute("PRAGMA locking_mode").fetchone()[0]).lower()
        except PermissionError:
            record.active = False
            del _REGISTRY[id(capability)]
            raise
        except (sqlite3.DatabaseError, TypeError, IndexError) as exc:
            record.active = False
            del _REGISTRY[id(capability)]
            raise PermissionError("trusted board revalidation lock proof is unavailable") from exc
        if locking_mode not in {"normal", "exclusive"}:
            reject("trusted board revalidation lock proof is invalid")
        try:
            current = record.adapter._snapshot_from_connection(record.connection, record.external_task_id)
        except Exception:
            record.active = False
            del _REGISTRY[id(capability)]
            raise
        if current != record.snapshot:
            record.active = False
            del _REGISTRY[id(capability)]
            raise RuntimeError("native release revalidation board snapshot drift before ledger commit")
        record.active = False
        del _REGISTRY[id(capability)]


def revoke_revalidation_capability(capability: object) -> None:
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(id(capability))
        if record is not None and record.capability is capability:
            record.active = False
            del _REGISTRY[id(capability)]
