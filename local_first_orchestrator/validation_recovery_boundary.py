from __future__ import annotations

import hashlib
import fcntl
import inspect
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import final

from .native_release_approval import canonical_validation_recovery_bytes, verify_detached_signature
from .git_security import safe_git_argv, safe_git_env
from .operator_config import _locked_config, _parse_operator_config_bytes, load_operator_config


_CONSTRUCTOR_SENTINEL = object()
_ISSUER_SENTINEL = object()
_REGISTRY: dict[int, _CapabilityRecord] = {}
_ISSUER_REGISTRY: dict[int, _IssuerRecord] = {}
_REGISTRY_LOCK = threading.RLock()
# Threat model: these in-process capabilities defend against accidental/mis-scoped
# callers, stale/replayed authority, alternate connections, threads, and PIDs.
# Arbitrary code execution in this interpreter is explicitly trusted; Python
# object privacy cannot constitute a privilege boundary against such code.
SAME_PROCESS_ARBITRARY_CODE_TRUSTED = True


@dataclass
class _IssuerRecord:
    issuer: object
    controller: object
    ledger_connection: sqlite3.Connection
    owner_pid: int
    allowed_code_objects: tuple[object, ...]


@dataclass
class _PinnedPath:
    label: str
    path: Path
    fd: int
    identity: tuple[int, int, int]


@dataclass
class _CapabilityRecord:
    capability: object
    issuer: object
    owner_pid: int
    owner_thread: int
    ledger_connection: sqlite3.Connection
    config_path: Path
    config_identity: tuple[int, int, int]
    config_bytes: bytes
    config_fd: int
    config_writer_lock: object
    ledger_path: Path
    repository: Path
    ledger_identity: tuple[int, int]
    repository_identity: tuple[int, int]
    ledger_fd: int
    repository_fd: int
    workspace_lock_fd: int
    ticket_id: str
    attempt_number: int
    terminal_generation: int
    operator_id: str
    reason: str
    signer_fingerprint: str
    authority_hash: str
    signer_public_key: bytes
    document_json: str
    document_hash: str
    detached_signature: bytes
    expected_archive_ids: tuple[str, ...]
    operation_kind: str
    operation_id: str
    candidate_spec: dict[str, object] | None
    candidate_identity: dict[str, object] | None
    pinned_candidate_paths: tuple[_PinnedPath, ...]
    replay_validation_artifact_path: str
    replay_validation_artifact_sha256: str
    replay_compact_evidence: str
    transaction_token: object | None = None
    active: bool = True
    consumed: bool = False


@final
class _ExactValidationRecoveryCapability:
    __slots__ = ()

    def __init__(self, sentinel: object) -> None:
        if sentinel is not _CONSTRUCTOR_SENTINEL:
            raise TypeError("validation recovery capability is not publicly constructible")


@final
class _ControllerValidationRecoveryIssuer:
    __slots__ = ()

    def __init__(self, sentinel: object) -> None:
        if sentinel is not _ISSUER_SENTINEL:
            raise TypeError("validation recovery issuer is not publicly constructible")

    def __call__(self, **kwargs: object) -> tuple[object, str]:
        return _issue_from_registered_controller(self, **kwargs)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _lstat_identity(path: Path) -> tuple[int, int, int]:
    st = os.stat(path, follow_symlinks=False)
    return int(st.st_dev), int(st.st_ino), int(st.st_mode)


def _path_snapshot(path: Path) -> dict[str, object]:
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return {"path": str(path), "missing": True}
    result: dict[str, object] = {
        "path": str(path),
        "st_dev": int(st.st_dev),
        "st_ino": int(st.st_ino),
        "st_mode": int(st.st_mode),
    }
    if stat.S_ISREG(st.st_mode):
        result["st_size"] = int(st.st_size)
    if stat.S_ISLNK(st.st_mode):
        result["symlink_target"] = os.readlink(path)
    elif stat.S_ISREG(st.st_mode):
        result["content_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _open_pinned_path(label: str, path: Path) -> _PinnedPath:
    identity = _lstat_identity(path)
    flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if stat.S_ISDIR(identity[2]):
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    fst = os.fstat(fd)
    fd_identity = (int(fst.st_dev), int(fst.st_ino), int(fst.st_mode))
    if fd_identity != identity:
        os.close(fd)
        raise PermissionError(f"validation recovery pinned path changed while opening: {label}")
    return _PinnedPath(label=label, path=path, fd=fd, identity=identity)


def _read_pinned_regular_file(path: Path) -> tuple[int, tuple[int, int, int], bytes]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise PermissionError("validation recovery operator config must be a regular file")
        identity = (int(fst.st_dev), int(fst.st_ino), int(fst.st_mode))
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if _lstat_identity(path) != identity:
            raise PermissionError("validation recovery operator config changed while being pinned")
        return fd, identity, b"".join(chunks)
    except Exception:
        os.close(fd)
        raise


def _isolated_candidate_diff(worktree: Path, base_sha: str, *, allowed_new_paths: tuple[str, ...]) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="local-first-recovery-index-") as temp:
        index_path = Path(temp) / "index"
        env = safe_git_env()
        env["GIT_INDEX_FILE"] = str(index_path)

        def git(*args: str) -> str:
            return subprocess.run(safe_git_argv(args), cwd=worktree, env=env, text=True, capture_output=True, check=True, timeout=30).stdout

        git("read-tree", base_sha)
        untracked = tuple(item for item in git("ls-files", "--others", "--exclude-standard", "-z").split("\0") if item)
        approved_new = tuple(sorted(set(untracked) & set(allowed_new_paths)))
        if approved_new:
            git("add", "-N", "--", *approved_new)
        diff = git("diff", "--binary", "--no-ext-diff", base_sha, "--")
        changed = tuple(item for item in git("diff", "--name-only", base_sha, "--").splitlines() if item)
    return {
        "diff_hash": hashlib.sha256(diff.encode()).hexdigest(),
        "changed_paths": changed,
        "untracked_paths": untracked,
    }


def _git_path(worktree: Path, *args: str) -> str:
    return subprocess.run(safe_git_argv(args), cwd=worktree, env=safe_git_env(), text=True, capture_output=True, check=True, timeout=15).stdout.strip()


def _lexical_tree_manifest(root: Path, *, excluded_top_level: tuple[str, ...] = (".git",)) -> list[dict[str, object]]:
    root = Path(root).absolute()
    manifest: list[dict[str, object]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        relative = current.relative_to(root)
        if relative.parts and relative.parts[0] in excluded_top_level:
            continue
        entry = _path_snapshot(current)
        entry["lexical_relative_path"] = "." if not relative.parts else relative.as_posix()
        manifest.append(entry)
        try:
            st = os.stat(current, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        children = sorted(current.iterdir(), key=lambda item: os.fsencode(item.name), reverse=True)
        stack.extend(children)
    return sorted(manifest, key=lambda entry: os.fsencode(str(entry["lexical_relative_path"])))


def _git_admin_manifest(git_dir: Path, common_git_dir: Path) -> list[dict[str, object]]:
    candidates: set[Path] = set()
    for base in {git_dir, common_git_dir}:
        for name in ("HEAD", "index", "config", "packed-refs", "commondir", "gitdir", "shallow"):
            candidates.add(base / name)
        refs = base / "refs"
        if refs.exists():
            candidates.update(path for path in refs.rglob("*") if path.exists() or path.is_symlink())
    manifest: list[dict[str, object]] = []
    for path in sorted(candidates, key=lambda item: os.fsencode(str(item.absolute()))):
        if not path.exists() and not path.is_symlink():
            continue
        entry = _path_snapshot(path)
        entry["lexical_path"] = str(path.absolute())
        manifest.append(entry)
    return manifest


def _acquire_workspace_recovery_lock(repository: Path) -> int:
    lock_path = repository.parent / f".{repository.name}.local-first-recovery.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except Exception:
        os.close(fd)
        raise

def validation_recovery_candidate_snapshot(
    worktree: Path,
    *,
    base_sha: str,
    allowed_new_paths: tuple[str, ...],
    implementation_artifact: Path,
    validation_policy_hash: str,
) -> dict[str, object]:
    worktree = Path(worktree).resolve(strict=True)
    live_root = _git_path(worktree, "rev-parse", "--show-toplevel")
    live_head = _git_path(worktree, "rev-parse", "HEAD")
    git_dir = Path(_git_path(worktree, "rev-parse", "--absolute-git-dir"))
    try:
        common_raw = _git_path(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        common_git_dir = Path(common_raw)
    except subprocess.CalledProcessError:
        common_raw = _git_path(worktree, "rev-parse", "--git-common-dir")
        common_git_dir = Path(common_raw)
        if not common_git_dir.is_absolute():
            common_git_dir = (worktree / common_git_dir).absolute()
    frozen = _isolated_candidate_diff(worktree, base_sha, allowed_new_paths=allowed_new_paths)
    implementation_artifact = Path(implementation_artifact).resolve(strict=True)
    dot_git = worktree / ".git"
    git_head = git_dir / "HEAD"
    git_index = Path(_git_path(worktree, "rev-parse", "--git-path", "index"))
    if not git_index.is_absolute():
        git_index = (worktree / git_index).absolute()
    git_config = Path(_git_path(worktree, "rev-parse", "--git-path", "config"))
    if not git_config.is_absolute():
        git_config = (worktree / git_config).absolute()
    changed_identities = {str(relative): _path_snapshot(worktree / str(relative)) for relative in sorted(set(frozen["changed_paths"]))}
    return {
        "top_level_path": live_root,
        "head_sha": live_head,
        "diff_hash": str(frozen["diff_hash"]),
        "changed_paths": list(frozen["changed_paths"]),
        "untracked_paths": list(frozen["untracked_paths"]),
        "lexical_tree_manifest": _lexical_tree_manifest(worktree),
        "git_admin_manifest": _git_admin_manifest(git_dir, common_git_dir),
        "implementation_artifact_path": str(implementation_artifact),
        "implementation_artifact_sha256": hashlib.sha256(implementation_artifact.read_bytes()).hexdigest(),
        "validation_policy_hash": validation_policy_hash,
        "filesystem_identity": {
            "worktree": _path_snapshot(worktree),
            "dot_git": _path_snapshot(dot_git),
            "git_dir": _path_snapshot(git_dir),
            "common_git_dir": _path_snapshot(common_git_dir),
            "git_head": _path_snapshot(git_head),
            "git_index": _path_snapshot(git_index),
            "git_config": _path_snapshot(git_config),
            "implementation_artifact": _path_snapshot(implementation_artifact),
            "changed_paths": changed_identities,
        },
    }


def _candidate_pinned_paths(snapshot: dict[str, object]) -> tuple[tuple[str, Path], ...]:
    fs = snapshot.get("filesystem_identity")
    if not isinstance(fs, dict):
        raise PermissionError("validation recovery candidate filesystem identity is malformed")
    items: list[tuple[str, Path]] = []
    for label in ("worktree", "dot_git", "git_dir", "common_git_dir", "git_head", "git_index", "git_config", "implementation_artifact"):
        entry = fs.get(label)
        if not isinstance(entry, dict) or entry.get("missing") is True or not isinstance(entry.get("path"), str):
            raise PermissionError(f"validation recovery candidate required path is unavailable: {label}")
        items.append((label, Path(str(entry["path"]))))
    changed = fs.get("changed_paths")
    if not isinstance(changed, dict):
        raise PermissionError("validation recovery candidate changed-path identity is malformed")
    for relative, entry in sorted(changed.items()):
        if isinstance(entry, dict) and entry.get("missing") is not True and isinstance(entry.get("path"), str):
            items.append((f"changed:{relative}", Path(str(entry["path"]))))
    return tuple(items)


def _pin_candidate_snapshot(snapshot: dict[str, object]) -> tuple[_PinnedPath, ...]:
    pinned: list[_PinnedPath] = []
    try:
        for label, path in _candidate_pinned_paths(snapshot):
            pinned.append(_open_pinned_path(label, path))
        return tuple(pinned)
    except Exception:
        for item in pinned:
            os.close(item.fd)
        raise


def _revalidate_pinned_paths(paths: tuple[_PinnedPath, ...]) -> None:
    for item in paths:
        fst = os.fstat(item.fd)
        current_fd = (int(fst.st_dev), int(fst.st_ino), int(fst.st_mode))
        if current_fd != item.identity:
            raise PermissionError(f"validation recovery pinned identity handle drift: {item.label}")
        try:
            current_path = _lstat_identity(item.path)
        except FileNotFoundError as exc:
            raise PermissionError(f"validation recovery pinned path disappeared: {item.label}") from exc
        if current_path != item.identity:
            raise PermissionError(f"validation recovery pinned path filesystem identity drift: {item.label}")


def _path_identity(path: Path, *, directory: bool = False) -> tuple[int, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        return int(st.st_dev), int(st.st_ino)
    finally:
        os.close(fd)


def _open_identity_handle(path: Path, *, directory: bool = False) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    st = os.fstat(fd)
    return fd, (int(st.st_dev), int(st.st_ino))


def _sqlite_main_identity(connection: sqlite3.Connection) -> tuple[Path, tuple[int, int]]:
    rows = connection.execute("PRAGMA database_list").fetchall()
    main = next((row for row in rows if str(row[1]) == "main"), None)
    if main is None or not str(main[2] or ""):
        raise PermissionError("validation recovery active SQLite connection has no durable main database")
    path = Path(str(main[2])).expanduser().resolve(strict=True)
    proc_fds = Path("/proc/self/fd")
    if not proc_fds.is_dir():
        raise PermissionError("validation recovery cannot prove active SQLite connection filesystem identity")
    identities: set[tuple[int, int]] = set()
    expected = str(path)
    for entry in proc_fds.iterdir():
        try:
            target = os.readlink(entry)
            if target.endswith(" (deleted)"):
                target = target[:-10]
            if os.path.abspath(target) != expected:
                continue
            st = os.stat(entry)
        except (FileNotFoundError, OSError):
            continue
        identities.add((int(st.st_dev), int(st.st_ino)))
    if len(identities) != 1:
        raise PermissionError("validation recovery cannot uniquely identify active SQLite connection target")
    return path, next(iter(identities))


def _close_record_handles(record: _CapabilityRecord) -> None:
    for fd_name in ("config_fd", "ledger_fd", "repository_fd", "workspace_lock_fd"):
        fd = getattr(record, fd_name, -1)
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
            setattr(record, fd_name, -1)
    for item in record.pinned_candidate_paths:
        try:
            os.close(item.fd)
        except OSError:
            pass
    lock = getattr(record, "config_writer_lock", None)
    if lock is not None:
        try:
            lock.__exit__(None, None, None)
        except Exception:
            pass
        record.config_writer_lock = None


def _fresh_external_authority(config_path: Path, ledger_path: Path, repository: Path):
    config = load_operator_config(config_path).validated(require_ledger=True)
    if config.ledger_path.resolve(strict=True) != ledger_path.resolve(strict=True):
        raise PermissionError("validation recovery operator config ledger identity mismatch")
    if config.canonical_repository.resolve(strict=True) != repository.resolve(strict=True):
        raise PermissionError("validation recovery operator config repository identity mismatch")
    if not config.operator_signing_public_key or not config.operator_signing_key_fingerprint:
        raise PermissionError("validation recovery requires configured external signer authority")
    return config


def _register_validation_recovery_controller(controller: object, ledger_connection: sqlite3.Connection) -> object:
    controller_type = type(controller)
    if controller_type.__module__ != "local_first_orchestrator.controller" or controller_type.__name__ != "LocalFirstController":
        raise PermissionError("validation recovery issuer registration requires the exact trusted controller")
    ledger = getattr(controller, "ledger", None)
    if ledger is None or getattr(ledger, "connection", None) is not ledger_connection:
        raise PermissionError("validation recovery issuer registration requires the controller ledger connection")
    issuer = _ControllerValidationRecoveryIssuer(_ISSUER_SENTINEL)
    with _REGISTRY_LOCK:
        _ISSUER_REGISTRY[id(issuer)] = _IssuerRecord(
            issuer=issuer,
            controller=controller,
            ledger_connection=ledger_connection,
            owner_pid=os.getpid(),
            allowed_code_objects=(
                controller_type.recover_terminal_validation_controller_defect.__code__,
                controller_type.reconcile_validation_controller_defect_completion_marker.__code__,
            ),
        )
    return issuer


def _issue_from_registered_controller(issuer: object, **kwargs: object) -> tuple[object, str]:
    if type(issuer) is not _ControllerValidationRecoveryIssuer:
        raise PermissionError("validation recovery issuance requires an exact registered controller issuer")
    with _REGISTRY_LOCK:
        issuer_record = _ISSUER_REGISTRY.get(id(issuer))
    if issuer_record is None or issuer_record.issuer is not issuer or issuer_record.owner_pid != os.getpid():
        raise PermissionError("validation recovery controller issuer is inactive or unregistered")
    frame = inspect.currentframe()
    controller_frame = frame.f_back.f_back if frame is not None and frame.f_back is not None and frame.f_back.f_back is not None else None
    if controller_frame is None or controller_frame.f_code not in issuer_record.allowed_code_objects or controller_frame.f_locals.get("self") is not issuer_record.controller:
        raise PermissionError("validation recovery capability issuance requires exact registered controller call provenance")
    return _issue_validation_recovery_capability(issuer_record, **kwargs)


def _issue_validation_recovery_capability(issuer_record: _IssuerRecord, **raw: object) -> tuple[object, str]:
    required = (
        "config_path", "ledger_path", "repository", "runtime_binding", "ticket_id", "attempt_number",
        "terminal_generation", "operator_id", "reason", "approval_document", "detached_signature",
        "expected_archive_ids", "operation_kind",
    )
    missing = [name for name in required if name not in raw]
    if missing:
        raise TypeError(f"validation recovery issuance missing required fields: {', '.join(missing)}")
    ledger_connection = issuer_record.ledger_connection
    config_path = Path(raw["config_path"]).expanduser().absolute()
    if config_path.is_symlink():
        raise PermissionError("validation recovery operator config path must not be a symlink")
    config_path = config_path.resolve(strict=True)
    ledger_path = Path(raw["ledger_path"]).resolve(strict=True)
    repository = Path(raw["repository"]).resolve(strict=True)
    runtime_binding = raw["runtime_binding"]
    ticket_id = str(raw["ticket_id"])
    attempt_number = int(raw["attempt_number"])
    terminal_generation = int(raw["terminal_generation"])
    operator_id = str(raw["operator_id"])
    reason = str(raw["reason"])
    approval_document = raw["approval_document"]
    detached_signature = bytes(raw["detached_signature"])
    expected_archive_ids = tuple(raw["expected_archive_ids"])
    operation_kind = str(raw["operation_kind"])
    if operation_kind not in {"recover", "marker_reconcile"}:
        raise ValueError("validation recovery operation kind is invalid")
    if not isinstance(approval_document, dict):
        raise ValueError("validation recovery signed approval is malformed")
    config = _fresh_external_authority(config_path, ledger_path, repository)
    public_key = config.signer_public_key_bytes
    fingerprint = str(config.operator_signing_key_fingerprint)
    authority_hash = str(config.operator_authority_hash)
    if runtime_binding["operator_signer_fingerprint"] != fingerprint or runtime_binding["operator_authority_hash"] != authority_hash:
        raise PermissionError("validation recovery runtime binding conflicts with external operator authority")
    canonical = canonical_validation_recovery_bytes(approval_document)
    verify_detached_signature(canonical, detached_signature, public_key, fingerprint)
    authority = approval_document.get("authority")
    if not isinstance(authority, dict):
        raise ValueError("validation recovery signed authority is malformed")
    if (
        approval_document.get("operator_id") != operator_id
        or approval_document.get("reason") != reason
        or authority.get("ticket_id") != ticket_id
        or type(authority.get("attempt_number")) is not int
        or authority.get("attempt_number") != attempt_number
        or type(authority.get("terminal_generation")) is not int
        or authority.get("terminal_generation") != terminal_generation
        or authority.get("signer_fingerprint") != fingerprint
        or authority.get("runtime_authority_hash") != authority_hash
        or authority.get("expected_archive_ids") != list(expected_archive_ids)
    ):
        raise ValueError("validation recovery signed approval conflicts with external authority")

    candidate_spec = raw.get("candidate_spec")
    candidate_identity = raw.get("candidate_identity")
    replay_path = str(raw.get("replay_validation_artifact_path") or "")
    replay_sha = str(raw.get("replay_validation_artifact_sha256") or "")
    replay_evidence = str(raw.get("replay_compact_evidence") or "")
    pinned_candidate_paths: tuple[_PinnedPath, ...] = ()
    workspace_lock_fd = -1
    if operation_kind == "recover":
        if not isinstance(candidate_spec, dict) or not isinstance(candidate_identity, dict):
            raise PermissionError("validation recovery issuance requires controller-owned candidate identity")
        live_candidate = validation_recovery_candidate_snapshot(
            Path(str(candidate_spec["worktree_path"])),
            base_sha=str(candidate_spec["base_sha"]),
            allowed_new_paths=tuple(str(value) for value in candidate_spec["allowed_new_paths"]),
            implementation_artifact=Path(str(candidate_spec["implementation_artifact_path"])),
            validation_policy_hash=str(candidate_spec["validation_policy_hash"]),
        )
        if live_candidate != candidate_identity:
            raise PermissionError("validation recovery candidate changed before authority issuance")
        allowed_new = {str(value) for value in candidate_spec["allowed_new_paths"]}
        unauthorized_untracked = sorted(set(str(value) for value in live_candidate.get("untracked_paths", ())) - allowed_new)
        if unauthorized_untracked:
            raise PermissionError(f"validation recovery candidate contains unauthorized untracked paths: {unauthorized_untracked}")
        if not replay_path or not replay_sha or not replay_evidence.strip():
            raise PermissionError("validation recovery issuance requires passing controller preflight evidence")
        replay_artifact = Path(replay_path).resolve(strict=True)
        if not replay_artifact.is_file() or hashlib.sha256(replay_artifact.read_bytes()).hexdigest() != replay_sha:
            raise PermissionError("validation recovery controller preflight artifact identity mismatch")
        pinned_candidate_paths = _pin_candidate_snapshot(candidate_identity)
        workspace_lock_fd = _acquire_workspace_recovery_lock(repository)
        live_after_pin = validation_recovery_candidate_snapshot(
            Path(str(candidate_spec["worktree_path"])),
            base_sha=str(candidate_spec["base_sha"]),
            allowed_new_paths=tuple(str(value) for value in candidate_spec["allowed_new_paths"]),
            implementation_artifact=Path(str(candidate_spec["implementation_artifact_path"])),
            validation_policy_hash=str(candidate_spec["validation_policy_hash"]),
        )
        if live_after_pin != candidate_identity:
            for item in pinned_candidate_paths:
                os.close(item.fd)
            if workspace_lock_fd >= 0:
                os.close(workspace_lock_fd)
            raise PermissionError("validation recovery candidate changed while authority was being pinned")
    elif candidate_spec is not None or candidate_identity is not None:
        raise ValueError("validation marker reconciliation must not carry a candidate identity")

    config_writer_lock = _locked_config(config_path, write=False)
    config_writer_lock.__enter__()
    ledger_fd = repository_fd = config_fd = -1
    try:
        config_fd, config_identity, config_bytes = _read_pinned_regular_file(config_path)
        ledger_fd, ledger_identity = _open_identity_handle(ledger_path)
        repository_fd, repository_identity = _open_identity_handle(repository, directory=True)
        connection_path, connection_identity = _sqlite_main_identity(ledger_connection)
        if connection_path != ledger_path or connection_identity != ledger_identity:
            raise PermissionError("validation recovery active SQLite connection identity mismatch")
    except Exception:
        for fd in (config_fd, ledger_fd, repository_fd, workspace_lock_fd):
            if fd >= 0:
                os.close(fd)
        for item in pinned_candidate_paths:
            os.close(item.fd)
        config_writer_lock.__exit__(None, None, None)
        raise

    document_hash = hashlib.sha256(canonical).hexdigest()
    operation_payload = {
        "kind": operation_kind,
        "ticket_id": ticket_id,
        "attempt_number": attempt_number,
        "terminal_generation": terminal_generation,
        "operator_id": operator_id,
        "reason": reason,
        "document_hash": document_hash,
        "expected_archive_ids": list(expected_archive_ids),
        "candidate_identity_hash": _canonical_sha256(candidate_identity) if candidate_identity is not None else None,
        "replay_validation_artifact_path": replay_path,
        "replay_validation_artifact_sha256": replay_sha,
        "replay_compact_evidence_sha256": hashlib.sha256(replay_evidence.encode("utf-8")).hexdigest() if replay_evidence else "",
    }
    operation_id = _canonical_sha256(operation_payload)
    capability = _ExactValidationRecoveryCapability(_CONSTRUCTOR_SENTINEL)
    record = _CapabilityRecord(
        capability=capability,
        issuer=issuer_record.issuer,
        owner_pid=os.getpid(),
        owner_thread=threading.get_ident(),
        ledger_connection=ledger_connection,
        config_path=config_path,
        config_identity=config_identity,
        config_bytes=config_bytes,
        config_fd=config_fd,
        config_writer_lock=config_writer_lock,
        ledger_path=ledger_path,
        repository=repository,
        ledger_identity=ledger_identity,
        repository_identity=repository_identity,
        ledger_fd=ledger_fd,
        repository_fd=repository_fd,
        workspace_lock_fd=workspace_lock_fd,
        ticket_id=ticket_id,
        attempt_number=attempt_number,
        terminal_generation=terminal_generation,
        operator_id=operator_id,
        reason=reason,
        signer_fingerprint=fingerprint,
        authority_hash=authority_hash,
        signer_public_key=public_key,
        document_json=canonical.decode("utf-8"),
        document_hash=document_hash,
        detached_signature=detached_signature,
        expected_archive_ids=expected_archive_ids,
        operation_kind=operation_kind,
        operation_id=operation_id,
        candidate_spec=dict(candidate_spec) if isinstance(candidate_spec, dict) else None,
        candidate_identity=dict(candidate_identity) if isinstance(candidate_identity, dict) else None,
        pinned_candidate_paths=pinned_candidate_paths,
        replay_validation_artifact_path=replay_path,
        replay_validation_artifact_sha256=replay_sha,
        replay_compact_evidence=replay_evidence,
    )
    with _REGISTRY_LOCK:
        _REGISTRY[id(capability)] = record
    return capability, operation_id


def _revalidate_config(record: _CapabilityRecord) -> None:
    fst = os.fstat(record.config_fd)
    if (int(fst.st_dev), int(fst.st_ino), int(fst.st_mode)) != record.config_identity:
        raise PermissionError("validation recovery operator config identity handle drift")
    if _lstat_identity(record.config_path) != record.config_identity:
        raise PermissionError("validation recovery operator config filesystem identity drift")
    if record.config_path.read_bytes() != record.config_bytes:
        raise PermissionError("validation recovery operator config byte identity drift")
    fresh = _parse_operator_config_bytes(record.config_bytes, record.config_path).validated(require_ledger=True)
    if (
        fresh.operator_signing_key_fingerprint != record.signer_fingerprint
        or fresh.operator_authority_hash != record.authority_hash
        or fresh.signer_public_key_bytes != record.signer_public_key
        or fresh.ledger_path.resolve(strict=True) != record.ledger_path
        or fresh.canonical_repository.resolve(strict=True) != record.repository
    ):
        raise PermissionError("validation recovery external operator authority drift")


def _revalidate_record_identity(
    record: _CapabilityRecord,
    *,
    ledger_path: Path,
    repository: Path,
    ledger_connection: sqlite3.Connection,
    validation_policy_hash: str | None = None,
) -> None:
    if ledger_connection is not record.ledger_connection:
        raise PermissionError("validation recovery exact Ledger.connection object identity mismatch")
    resolved_ledger = ledger_path.resolve(strict=True)
    resolved_repository = repository.resolve(strict=True)
    if record.ledger_path != resolved_ledger or record.repository != resolved_repository:
        raise PermissionError("validation recovery authority capability path identity mismatch")
    live_ledger = _path_identity(resolved_ledger)
    live_repository = _path_identity(resolved_repository, directory=True)
    if live_ledger != record.ledger_identity or live_repository != record.repository_identity:
        raise PermissionError("validation recovery authority capability filesystem identity mismatch")
    ledger_stat = os.fstat(record.ledger_fd)
    repository_stat = os.fstat(record.repository_fd)
    if (int(ledger_stat.st_dev), int(ledger_stat.st_ino)) != record.ledger_identity or (int(repository_stat.st_dev), int(repository_stat.st_ino)) != record.repository_identity:
        raise PermissionError("validation recovery authority capability identity handle mismatch")
    connection_path, connection_identity = _sqlite_main_identity(ledger_connection)
    if connection_path != record.ledger_path or connection_identity != record.ledger_identity:
        raise PermissionError("validation recovery active SQLite connection identity mismatch")
    _revalidate_config(record)
    if record.operation_kind == "recover":
        assert record.candidate_spec is not None and record.candidate_identity is not None
        expected_policy = str(record.candidate_spec["validation_policy_hash"])
        if validation_policy_hash is None or validation_policy_hash != expected_policy:
            raise PermissionError("validation recovery validation-policy identity drift")
        _revalidate_pinned_paths(record.pinned_candidate_paths)
        live_candidate = validation_recovery_candidate_snapshot(
            Path(str(record.candidate_spec["worktree_path"])),
            base_sha=str(record.candidate_spec["base_sha"]),
            allowed_new_paths=tuple(str(value) for value in record.candidate_spec["allowed_new_paths"]),
            implementation_artifact=Path(str(record.candidate_spec["implementation_artifact_path"])),
            validation_policy_hash=validation_policy_hash,
        )
        if live_candidate != record.candidate_identity:
            raise PermissionError("validation recovery candidate identity drift at transaction boundary")
        # Git probes above are themselves outside the pinned-fd reads. Recheck
        # every no-follow path and the locked config after those probes so the
        # final caller fence is anchored to the same objects immediately before
        # SQLite commit.
        _revalidate_pinned_paths(record.pinned_candidate_paths)
        _revalidate_config(record)
        replay = Path(record.replay_validation_artifact_path).resolve(strict=True)
        if not replay.is_file() or hashlib.sha256(replay.read_bytes()).hexdigest() != record.replay_validation_artifact_sha256:
            raise PermissionError("validation recovery controller preflight evidence drift")


def validate_and_consume_validation_recovery_capability(
    capability: object,
    *,
    operation_id: str,
    operation_kind: str,
    operation_reason: str,
    transaction_token: object,
    ledger_path: Path,
    repository: Path,
    runtime_binding: object,
    ticket_id: str,
    attempt_number: int,
    terminal_generation: int,
    operator_id: str,
    expected_archive_ids: tuple[str, ...],
    ledger_connection: sqlite3.Connection,
    replay_validation_artifact_path: str = "",
    replay_validation_artifact_sha256: str = "",
    replay_compact_evidence: str = "",
    validation_policy_hash: str | None = None,
) -> dict[str, object]:
    if type(capability) is not _ExactValidationRecoveryCapability:
        raise PermissionError("validation recovery requires an exact controller-owned authority capability")
    if transaction_token is None:
        raise PermissionError("validation recovery requires an active trusted transaction")
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(id(capability))
        if record is None or record.capability is not capability or not record.active:
            raise PermissionError("validation recovery authority capability is inactive or unregistered")

        def reject(message: str) -> None:
            record.active = False
            _REGISTRY.pop(id(capability), None)
            _close_record_handles(record)
            raise PermissionError(message)

        if os.getpid() != record.owner_pid or threading.get_ident() != record.owner_thread:
            reject("validation recovery authority capability owner mismatch")
        if ledger_connection is not record.ledger_connection:
            reject("validation recovery exact Ledger.connection object identity mismatch")
        if (
            record.operation_id != operation_id
            or record.operation_kind != operation_kind
            or record.reason != operation_reason
            or record.ledger_path != ledger_path.resolve(strict=True)
            or record.repository != repository.resolve(strict=True)
            or record.ticket_id != ticket_id
            or record.attempt_number != attempt_number
            or record.terminal_generation != terminal_generation
            or record.operator_id != operator_id
            or record.expected_archive_ids != expected_archive_ids
        ):
            reject("validation recovery immutable operation identity mismatch")
        if operation_kind == "recover" and (
            Path(record.replay_validation_artifact_path).resolve(strict=True) != Path(replay_validation_artifact_path).resolve(strict=True)
            or record.replay_validation_artifact_sha256 != replay_validation_artifact_sha256
            or record.replay_compact_evidence != replay_compact_evidence
        ):
            reject("validation recovery controller preflight identity mismatch")
        try:
            _revalidate_record_identity(
                record,
                ledger_path=ledger_path,
                repository=repository,
                ledger_connection=ledger_connection,
                validation_policy_hash=validation_policy_hash,
            )
        except (OSError, PermissionError, subprocess.CalledProcessError) as exc:
            reject(str(exc))
        if runtime_binding["operator_signer_fingerprint"] != record.signer_fingerprint or runtime_binding["operator_authority_hash"] != record.authority_hash:
            reject("validation recovery runtime binding conflicts with external operator authority")
        verify_detached_signature(record.document_json.encode("utf-8"), record.detached_signature, record.signer_public_key, record.signer_fingerprint)
        record.active = False
        record.consumed = True
        record.transaction_token = transaction_token
        return {
            "operation_id": record.operation_id,
            "document_json": record.document_json,
            "document_hash": record.document_hash,
            "detached_signature": record.detached_signature,
            "signer_fingerprint": record.signer_fingerprint,
            "authority_hash": record.authority_hash,
            "expected_archive_ids": list(record.expected_archive_ids),
            "reason": record.reason,
            "candidate_identity": record.candidate_identity,
        }


def revalidate_consumed_validation_recovery_capability(
    capability: object,
    *,
    operation_id: str,
    transaction_token: object,
    ledger_path: Path,
    repository: Path,
    ledger_connection: sqlite3.Connection,
    validation_policy_hash: str | None = None,
) -> None:
    if type(capability) is not _ExactValidationRecoveryCapability:
        raise PermissionError("validation recovery requires an exact controller-owned authority capability")
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(id(capability))
        if record is None or record.capability is not capability or not record.consumed:
            raise PermissionError("validation recovery consumed authority capability is unavailable")
        if os.getpid() != record.owner_pid or threading.get_ident() != record.owner_thread:
            raise PermissionError("validation recovery authority capability owner mismatch")
        if record.operation_id != operation_id or record.transaction_token is not transaction_token:
            raise PermissionError("validation recovery trusted transaction provenance mismatch")
        _revalidate_record_identity(
            record,
            ledger_path=ledger_path,
            repository=repository,
            ledger_connection=ledger_connection,
            validation_policy_hash=validation_policy_hash,
        )


def revoke_validation_recovery_capability(capability: object) -> None:
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(id(capability))
        if record is not None and record.capability is capability:
            record.active = False
            del _REGISTRY[id(capability)]
            _close_record_handles(record)
