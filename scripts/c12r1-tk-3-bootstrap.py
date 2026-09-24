#!/usr/bin/env -S /usr/bin/python3 -I -S
"""Root-owned, code-only bootstrap for one pinned C12 recovery snapshot.

This file must itself be installed root-owned and hash-checked before sudo
execution. Never run a checkout copy with root privileges. It imports only
Python's standard library; no project module is imported before publication.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

APPROVED_COMMIT = "@APPROVED_FULL_COMMIT@"  # Render after independent commit review.
REPOSITORY = Path("/home/ocadmin/.hermes/plugins/local-first-orchestrator")
BUNDLE = Path("/usr/local/lib/local-first-orchestrator/c12r1-tk-3")
LEDGER = Path("/home/ocadmin/.hermes/local-first-orchestrator/real-project-ledger.db")
CONFIG = Path("/home/ocadmin/.hermes/local-first-orchestrator/operator-config.json")
KEY = Path("/var/lib/local-first-c12r1-tk-3/private/key")
EXCHANGE = Path("/var/lib/local-first-c12r1-tk-3/exchange")
PYTHON = Path("/usr/bin/python3").resolve()
_TEMPLATE = "scripts/c12r1-tk-3-root-launcher.sh.in"
_MAX_MEMBERS = 300
_MAX_FILE = 4 * 1024 * 1024
_MAX_BYTES = 16 * 1024 * 1024


def _check_commit(commit: str) -> None:
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("installer requires one approved full SHA-1 Git commit")


def _safe_name(name: str, *, directory: bool) -> str:
    if not isinstance(name, str):
        raise ValueError("invalid archive member")
    value = name[:-1] if directory and name.endswith("/") else name
    parts = value.split("/")
    if (not value or value.startswith("/") or "\\" in value or "\x00" in value
        or any(part in {"", ".", ".."} for part in parts)):
        raise ValueError("unsafe archive member name")
    if parts[0] == "local_first_orchestrator":
        if len(parts) == 1 and not directory:
            raise ValueError("package root must be a directory")
    elif value not in {"scripts", _TEMPLATE} or (value == "scripts") != directory:
        raise ValueError("unexpected archive member")
    return value


def extract_checked_archive(stream: io.BytesIO, destination: Path) -> dict[str, str]:
    """Extract only regular files/dirs into a newly private destination."""
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    hashes: dict[str, str] = {}
    seen: set[str] = set()
    total = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if len(seen) >= _MAX_MEMBERS or not (member.isdir() or member.isfile()):
                raise ValueError("unsupported or excessive archive member")
            name = _safe_name(member.name, directory=member.isdir())
            if name in seen:
                raise ValueError("duplicate archive member")
            seen.add(name)
            path = destination.joinpath(*name.split("/"))
            if member.isdir():
                path.mkdir(mode=0o700, parents=False, exist_ok=False)
                continue
            if member.size < 0 or member.size > _MAX_FILE or total + member.size > _MAX_BYTES:
                raise ValueError("archive payload exceeds bound")
            if not path.parent.is_dir():
                raise ValueError("archive member precedes its directory")
            data = archive.extractfile(member)
            if data is None:
                raise ValueError("archive member has no data")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            digest = hashlib.sha256()
            try:
                remaining = member.size
                while remaining:
                    chunk = data.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError("truncated archive payload")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(fd, view):]
                    remaining -= len(chunk)
                os.fsync(fd)
            finally:
                os.close(fd)
            total += member.size
            hashes[name] = digest.hexdigest()
    if _TEMPLATE not in hashes or "local_first_orchestrator/__init__.py" not in hashes:
        raise ValueError("approved archive omitted required payload")
    return hashes


def _write_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, mode)


def _protected_root_directory(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise PermissionError("protected installation parent must be absolute")
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError("installation parent is not root-owned and protected")


def build_bundle(repo: Path, commit: str, destination: Path, *, ledger: Path, config: Path,
                 key: Path, exchange: Path, python_executable: Path) -> Path:
    """Code-only assembly; the root entry point pins all these arguments."""
    _check_commit(commit)
    if not repo.is_absolute() or not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise FileExistsError("bundle path must be new and absolute")
    if any(not path.is_absolute() or ".." in path.parts for path in (ledger, config, key, exchange, python_executable)):
        raise ValueError("runtime paths must be absolute and normalized")
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C", "GIT_NO_REPLACE_OBJECTS": "1",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1"}
    result = subprocess.run(("/usr/bin/git", "-C", str(repo), "archive", "--format=tar", commit, "--",
                             "local_first_orchestrator", _TEMPLATE), env=env, capture_output=True, timeout=60)
    if result.returncode or len(result.stdout) > 2 * _MAX_BYTES:
        raise ValueError("approved Git archive is unavailable or oversized")
    parent = destination.parent
    private = Path(tempfile.mkdtemp(prefix=".c12-assembly-", dir=parent))
    try:
        root = private / "source"
        hashes = extract_checked_archive(io.BytesIO(result.stdout), root)
        template = (root / _TEMPLATE).read_bytes()
        source_lines = [f"{hashes[name]}  {name}\n" for name in sorted(hashes) if name.startswith("local_first_orchestrator/") and name.endswith(".py")]
        source_manifest = "".join(source_lines).encode("ascii")
        _write_new(private / "source.sha256", source_manifest, 0o444)
        runtime = {"ledger_path": str(ledger), "config_path": str(config), "source_root": str(destination / "source"),
                   "python_executable": str(python_executable), "key_path": str(key), "exchange_parent": str(exchange)}
        runtime_bytes = (json.dumps(runtime, sort_keys=True, separators=(",", ":")) + "\n").encode()
        _write_new(private / "runtime.json", runtime_bytes, 0o600)
        python_info = python_executable.stat()
        if not stat.S_ISREG(python_info.st_mode) or python_info.st_mode & 0o022:
            raise PermissionError("system Python is not a protected executable")
        values = {
            "@SOURCE_ROOT@": str(destination / "source"),
            "@SOURCE_MANIFEST@": str(destination / "source.sha256"),
            "@SOURCE_MANIFEST_SHA256@": hashlib.sha256(source_manifest).hexdigest(),
            "@RUNTIME_MANIFEST@": str(destination / "runtime.json"),
            "@RUNTIME_MANIFEST_SHA256@": hashlib.sha256(runtime_bytes).hexdigest(),
            "@PYTHON_BIN@": str(python_executable),
            "@PYTHON_SHA256@": hashlib.sha256(python_executable.read_bytes()).hexdigest(),
            "@PYTHON_MODE@": format(stat.S_IMODE(python_info.st_mode), "o"),
            "@ID_BIN@": "/usr/bin/id", "@STAT_BIN@": "/usr/bin/stat",
            "@SHA256SUM_BIN@": "/usr/bin/sha256sum", "@GREP_BIN@": "/usr/bin/grep",
        }
        rendered = template.decode("utf-8")
        for token, value in values.items():
            if any(char in value for char in ("'", '"', "\n", "|", "&", "\\")):
                raise ValueError("unsafe installer substitution")
            rendered = rendered.replace(token, value)
        if "@SOURCE_ROOT@" in rendered or "@PYTHON_BIN@" in rendered:
            raise ValueError("unrendered protected launcher")
        _write_new(private / "launcher", rendered.encode(), 0o555)
        for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
            for filename in files:
                os.chmod(Path(current) / filename, 0o444)
            for name in dirs:
                os.chmod(Path(current) / name, 0o555)
            os.chmod(current, 0o555)
        os.chmod(private, 0o555)
        os.rename(private, destination)
        return destination
    except BaseException:
        if private.exists():
            os.chmod(private, 0o700)
            for current, dirs, _ in os.walk(private):
                for name in dirs:
                    os.chmod(Path(current) / name, 0o700)
            shutil.rmtree(private)
        raise


def main() -> int:
    if len(sys.argv) != 1 or os.geteuid() != 0:
        raise PermissionError("root-owned C12 installer takes no arguments and requires root")
    _check_commit(APPROVED_COMMIT)
    _protected_root_directory(BUNDLE.parent)
    _protected_root_directory(PYTHON.parent)
    build_bundle(REPOSITORY, APPROVED_COMMIT, BUNDLE, ledger=LEDGER, config=CONFIG,
                 key=KEY, exchange=EXCHANGE, python_executable=PYTHON)
    print("installed protected C12 bundle; verify bytes and modes before invoking launcher")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
