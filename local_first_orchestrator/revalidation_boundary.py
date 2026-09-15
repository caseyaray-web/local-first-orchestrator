from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from dataclasses import asdict, dataclass
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
    filesystem_identity: tuple[int, int]
    connection: sqlite3.Connection
    transaction_mode: str
    owner_pid: int
    owner_thread: int
    task_id: str
    board_name: str
    external_task_id: str
    snapshot: Any
    snapshot_hash: str
    nonce: bytes
    active: bool = True


@final
class _ExactPrivateCapability:
    __slots__ = ("snapshot", "_adapter", "_connection", "_task_id")

    def __init__(self, sentinel: object, adapter: Any, connection: sqlite3.Connection, task_id: str, snapshot: Any) -> None:
        if sentinel is not _CONSTRUCTOR_SENTINEL:
            raise TypeError("trusted board revalidation capability is not publicly constructible")
        self.snapshot = snapshot
        self._adapter = adapter
        self._connection = connection
        self._task_id = task_id

    # Compatibility seam for old direct-adapter tests. Ledger does not invoke it.
    def _verify_for_ledger(self, task_id: str, external_task_id: str) -> None:
        validate_and_consume_revalidation_capability(
            self, task_id=task_id, external_task_id=external_task_id,
            expected_snapshot_hash=canonical_sha256(asdict(self.snapshot)),
        )


def create_revalidation_capability(adapter: Any, connection: sqlite3.Connection, path: Any, task_id: str, snapshot: Any, board_name: str) -> _ExactPrivateCapability:
    stat = path.stat()
    capability = _ExactPrivateCapability(_CONSTRUCTOR_SENTINEL, adapter, connection, task_id, snapshot)
    with _REGISTRY_LOCK:
        _REGISTRY[id(capability)] = _CapabilityRecord(
            capability=capability, adapter=adapter, board_db_path=str(path),
            filesystem_identity=(stat.st_dev, stat.st_ino), connection=connection,
            transaction_mode="IMMEDIATE", owner_pid=os.getpid(), owner_thread=threading.get_ident(),
            task_id=task_id, board_name=board_name, external_task_id=snapshot.task.id,
            snapshot=snapshot, snapshot_hash=canonical_sha256(asdict(snapshot)),
            nonce=secrets.token_bytes(32),
        )
    return capability


def validate_and_consume_revalidation_capability(capability: object, *, task_id: str, external_task_id: str, expected_snapshot_hash: str) -> None:
    if type(capability) is not _ExactPrivateCapability:
        raise PermissionError("native release revalidation requires an exact trusted board capability")
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(id(capability))
        if record is None or record.capability is not capability or not record.active:
            raise PermissionError("trusted board revalidation capability is inactive or unregistered")
        if os.getpid() != record.owner_pid or threading.get_ident() != record.owner_thread:
            raise PermissionError("trusted board revalidation capability owner mismatch")
        if external_task_id != record.external_task_id or expected_snapshot_hash != record.snapshot_hash:
            raise PermissionError("trusted board revalidation capability authority mismatch")
        if not record.connection.in_transaction or record.transaction_mode != "IMMEDIATE":
            raise PermissionError("trusted board revalidation transaction is not active")
        try:
            locking_mode = str(record.connection.execute("PRAGMA locking_mode").fetchone()[0]).lower()
        except (sqlite3.DatabaseError, TypeError, IndexError) as exc:
            raise PermissionError("trusted board revalidation lock proof is unavailable") from exc
        if locking_mode not in {"normal", "exclusive"}:
            raise PermissionError("trusted board revalidation lock proof is invalid")
        current = record.adapter._snapshot_from_connection(record.connection, record.task_id)
        if current != record.snapshot:
            raise RuntimeError("native release revalidation board snapshot drift before ledger commit")
        record.active = False


def revoke_revalidation_capability(capability: object) -> None:
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(id(capability))
        if record is not None and record.capability is capability:
            record.active = False
            del _REGISTRY[id(capability)]
