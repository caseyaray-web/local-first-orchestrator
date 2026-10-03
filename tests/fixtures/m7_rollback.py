"""Fixture-only rollback archive helpers for the M7 cutover rehearsal.

These helpers intentionally reject paths outside a caller-owned fixture root.  They
are not installed with the plugin and do not inspect or change a Hermes home,
Kanban database, provider, or live plugin directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from local_first_orchestrator.evidence_store import EvidenceStore

_ARCHIVE_VERSION = 1
_METADATA_NAME = "rollback-metadata.json"
_CHECKPOINT_KEYS = frozenset(
    {
        "version",
        "scope",
        "cutoff",
        "native_effects",
        "installed_plugin_snapshot",
        "operator_intent",
        "restore_policy",
    }
)
_SCOPE_KEYS = frozenset({"board_id", "anchor_task_id"})


class RollbackArchiveError(ValueError):
    """The fixture archive is incomplete, inconsistent, or outside its root."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(root: Path, path: Path, *, require_exists: bool) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=require_exists)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise RollbackArchiveError("fixture path escapes fixture root") from error
    return resolved


def _relative_file(root: Path, path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise RollbackArchiveError("rollback input must be a regular non-symlink file")
    return path.relative_to(root).as_posix()


def _reject_symlink_path(path: Path, *, label: str) -> None:
    """Reject a caller-supplied symlink before resolution can erase that fact."""
    if path.is_symlink():
        raise RollbackArchiveError(f"{label} must not be a symlink")


def _validate_checkpoint(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(checkpoint, Mapping) or set(checkpoint) != _CHECKPOINT_KEYS:
        raise RollbackArchiveError("checkpoint keys are not the closed M7 fixture schema")
    scope = checkpoint["scope"]
    if not isinstance(scope, Mapping) or set(scope) != _SCOPE_KEYS:
        raise RollbackArchiveError("checkpoint scope is not exact")
    if checkpoint["version"] != 1 or not all(isinstance(scope[key], str) and scope[key] for key in _SCOPE_KEYS):
        raise RollbackArchiveError("checkpoint version or scope is invalid")
    for key in _CHECKPOINT_KEYS - {"version", "scope"}:
        if not isinstance(checkpoint[key], str) or not checkpoint[key]:
            raise RollbackArchiveError("checkpoint value is invalid")
    return json.loads(_canonical_json(dict(checkpoint)))


def _validate_evidence_store(path: Path) -> None:
    """Use the plugin's public store validator before and after SQLite backup."""
    with EvidenceStore.open(path, create_new=False) as store:
        store.migrate()


def _sqlite_backup(source: Path, destination: Path) -> None:
    """Make a transactionally consistent DB snapshot; WAL sidecars are not copied."""
    with EvidenceStore.open(source, create_new=False) as store:
        target = sqlite3.connect(destination)
        try:
            store.connection.backup(target)
        finally:
            target.close()
    _validate_evidence_store(destination)


def build_fixture_rollback_archive(
    *,
    fixture_root: Path,
    plugin_root: Path,
    plugin_files: Sequence[str],
    config_path: Path,
    evidence_path: Path,
    checkpoint: Mapping[str, Any],
    archive_path: Path,
) -> dict[str, Any]:
    """Archive an allowlisted fixture plugin/config/evidence restore point.

    The `archive_path` and every source must be under `fixture_root`; this is a
    deliberate fixture-only guard against using the helper on installed state.
    """
    root = fixture_root.resolve(strict=True)
    if not root.is_dir():
        raise RollbackArchiveError("fixture root must be a directory")
    checkpoint_data = _validate_checkpoint(checkpoint)
    _reject_symlink_path(plugin_root, label="plugin root")
    _reject_symlink_path(config_path, label="config input")
    _reject_symlink_path(evidence_path, label="evidence input")
    plugin = _within(root, plugin_root, require_exists=True)
    config = _within(root, config_path, require_exists=True)
    evidence = _within(root, evidence_path, require_exists=True)
    archive = _within(root, archive_path, require_exists=False)
    if archive.exists():
        raise RollbackArchiveError("fixture rollback archive already exists")
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.parent.chmod(0o700)
    if not plugin.is_dir() or not plugin_files:
        raise RollbackArchiveError("plugin directory and allowlist are required")
    _relative_file(root, config)
    _relative_file(root, evidence)
    _validate_evidence_store(evidence)

    copied: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for relative in plugin_files:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() in seen:
            raise RollbackArchiveError("plugin allowlist contains an unsafe or duplicate path")
        seen.add(candidate.as_posix())
        component = plugin
        for part in candidate.parts:
            component = component / part
            _reject_symlink_path(component, label="plugin allowlist input")
        source = _within(root, plugin / candidate, require_exists=True)
        if source.parent != (plugin / candidate).parent.resolve(strict=True):
            raise RollbackArchiveError("plugin allowlist resolution changed")
        _relative_file(root, source)
        copied.append((source, f"plugin/{candidate.as_posix()}"))

    stage = Path(tempfile.mkdtemp(prefix="m7-rollback-", dir=root))
    try:
        staged_evidence = stage / "state/evidence.sqlite3"
        staged_evidence.parent.mkdir(parents=True)
        staged_evidence.parent.chmod(0o700)
        _sqlite_backup(evidence, staged_evidence)
        staged_config = stage / "config/local-first.json"
        staged_config.parent.mkdir(parents=True)
        shutil.copyfile(config, staged_config)
        for source, archive_name in copied:
            target = stage / archive_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        file_names = ["config/local-first.json", "state/evidence.sqlite3", *(name for _, name in copied)]
        files = [{"path": name, "sha256": _sha256(stage / name)} for name in sorted(file_names)]
        metadata = {
            "archive_version": _ARCHIVE_VERSION,
            "checkpoint": checkpoint_data,
            "files": files,
            "sqlite_capture": "sqlite-backup-api-consistent; wal-sidecars-not-copied",
        }
        (stage / _METADATA_NAME).write_bytes(_canonical_json(metadata))
        with tarfile.open(archive, "x:gz") as bundle:
            for name in sorted([_METADATA_NAME, *file_names]):
                bundle.add(stage / name, arcname=name, recursive=False)
    finally:
        shutil.rmtree(stage)
    return {"archive": str(archive), "archive_sha256": _sha256(archive), "metadata": metadata}


def restore_fixture_rollback_archive(
    *, fixture_root: Path, archive_path: Path, destination: Path, expected_checkpoint: Mapping[str, Any]
) -> dict[str, Any]:
    """Restore and verify a fixture archive without overwriting a destination."""
    root = fixture_root.resolve(strict=True)
    archive = _within(root, archive_path, require_exists=True)
    target = _within(root, destination, require_exists=False)
    if target.exists():
        raise RollbackArchiveError("fixture restore destination already exists")
    checkpoint = _validate_checkpoint(expected_checkpoint)
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if any(not member.isfile() or Path(member.name).is_absolute() or ".." in Path(member.name).parts for member in members):
            raise RollbackArchiveError("archive contains an unsafe member")
        names = {member.name for member in members}
        if len(names) != len(members):
            raise RollbackArchiveError("archive contains duplicate members")
        if _METADATA_NAME not in names:
            raise RollbackArchiveError("archive metadata is missing")
        metadata_handle = bundle.extractfile(_METADATA_NAME)
        if metadata_handle is None:
            raise RollbackArchiveError("archive metadata cannot be read")
        metadata = json.loads(metadata_handle.read())
        if metadata.get("archive_version") != _ARCHIVE_VERSION or metadata.get("checkpoint") != checkpoint:
            raise RollbackArchiveError("archive checkpoint does not match the requested fixture restore")
        files = metadata.get("files")
        if not isinstance(files, list) or not files:
            raise RollbackArchiveError("archive file inventory is invalid")
        expected = {_METADATA_NAME}
        for record in files:
            if not isinstance(record, Mapping) or set(record) != {"path", "sha256"}:
                raise RollbackArchiveError("archive inventory entry is invalid")
            path = record["path"]
            if not isinstance(path, str) or path in expected or path.startswith("/") or ".." in Path(path).parts:
                raise RollbackArchiveError("archive inventory path is invalid")
            expected.add(path)
        if names != expected:
            raise RollbackArchiveError("archive has missing or unknown members")
        target.mkdir(parents=True)
        target.chmod(0o700)
        for record in files:
            member = bundle.getmember(record["path"])
            output = target / record["path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            # EvidenceStore requires each database parent to remain private.
            if record["path"] == "state/evidence.sqlite3":
                output.parent.chmod(0o700)
            source = bundle.extractfile(member)
            if source is None:
                raise RollbackArchiveError("archive member cannot be read")
            with output.open("xb") as handle:
                shutil.copyfileobj(source, handle)
            if _sha256(output) != record["sha256"]:
                raise RollbackArchiveError("restored file hash differs from archive inventory")
    _validate_evidence_store(target / "state/evidence.sqlite3")
    return {"destination": str(target), "archive_sha256": _sha256(archive), "metadata": metadata}
