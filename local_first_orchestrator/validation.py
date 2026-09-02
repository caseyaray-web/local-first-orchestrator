from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .ticket import MicroTicket
from .source_languages import is_supported_source, is_test_path, normalized_repository_path
from .symbols import enforce_symbol_scope


class ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandEvidence:
    argv: tuple[str, ...]
    returncode: int
    duration_seconds: float
    stdout_summary: str
    stderr_summary: str
    truncated: bool = False


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    errors: tuple[str, ...]
    commands: tuple[CommandEvidence, ...]
    compact_evidence: str
    full_evidence_path: Path
    scope_unverified: bool = False


class DeterministicValidator:
    denied_suffixes = (".lock", ".pem", ".key", ".env")
    default_secret_assignment_patterns = (
        re.compile(r"(?im)^\s*(?:[A-Za-z][A-Za-z0-9_-]*[_-])?(?:api[_-]?key|secret|password|token|private[_-]?key)\s*[:=]\s*(?:['\"][^'\"]+['\"]|[^\s#]{8,})"),
    )
    TIMEOUT_RETURN_CODE = -124
    LAUNCH_FAILURE_RETURN_CODE = -127
    NOT_ALLOWLISTED_RETURN_CODE = -126

    def __init__(self, *, artifact_root: Path, environment_allowlist: tuple[str, ...] = ("PATH",), secret_patterns: tuple[str, ...] = ()) -> None:
        self.artifact_root, self.environment_allowlist, self.secret_patterns = Path(artifact_root), environment_allowlist, secret_patterns

    def run_verification_command(self, argv: tuple[str, ...], *, allowed_commands: tuple[tuple[str, ...], ...], cwd: Path, timeout_seconds: int, output_limit: int) -> CommandEvidence:
        """Return evidence always: -124 timeout, -127 launch, -126 rejected."""
        started=time.monotonic()
        if argv not in allowed_commands:
            return CommandEvidence(argv,self.NOT_ALLOWLISTED_RETURN_CODE,0.0,"","verification command rejected: not allowlisted")
        env={key: os.environ[key] for key in self.environment_allowlist if key in os.environ}
        process: subprocess.Popen[str] | None = None
        try:
            process=subprocess.Popen(argv,cwd=cwd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
            raw_out,raw_err=process.communicate(timeout=timeout_seconds); code=process.returncode
        except subprocess.TimeoutExpired as exc:
            raw_out=exc.stdout if isinstance(exc.stdout,str) else ""; raw_err=exc.stderr if isinstance(exc.stderr,str) else "verification command timed out"; code=self.TIMEOUT_RETURN_CODE
            if process is not None:
                try: os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError: pass
                try: process.communicate(timeout=.5)
                except subprocess.TimeoutExpired:
                    try: os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                    try: process.communicate(timeout=.5)
                    except subprocess.TimeoutExpired: raw_err="verification cleanup failed"
        except OSError as exc:
            raw_out=""; raw_err=f"verification launch failed: {type(exc).__name__}"; code=self.LAUNCH_FAILURE_RETURN_CODE
        truncated=len(raw_out)>output_limit or len(raw_err)>output_limit
        return CommandEvidence(argv,code,time.monotonic()-started,self._redact(raw_out[:output_limit]),self._redact(raw_err[:output_limit]),truncated)

    def _git(self, path: Path, *args: str) -> str:
        """Run bounded, non-interactive internal Git inspection only."""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "LESS": "FRX"}
        safe_args = args
        if args and args[0] == "diff":
            safe_args = ("diff", "--no-ext-diff", "--no-textconv", *args[1:])
        argv = ("git", "--no-pager", "-c", "core.pager=cat", "-c", "diff.external=false", *safe_args)
        try:
            completed = subprocess.run(argv, cwd=path, env=env, text=True, capture_output=True, timeout=15, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValidationError(f"git inspection timed out: {' '.join(args[:3])}") from exc
        if completed.returncode:
            detail = self._redact((completed.stderr or completed.stdout or "git command failed")[:2000]).strip()
            raise ValidationError(f"git inspection failed: {detail}")
        return completed.stdout[:1_000_000]

    @staticmethod
    def _validated_base_sha(base_sha: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            raise ValidationError("recorded base SHA must be a full lowercase commit hash")
        return base_sha

    def _redact(self, text: str) -> str:
        for secret in self.secret_patterns:
            text = re.sub(re.escape(secret), "[REDACTED]", text, flags=re.I)
        return text

    def _secret_scan_error(self, worktree: Path, relative_path: str) -> str | None:
        candidate = (worktree / relative_path).resolve()
        raw_candidate = worktree / relative_path
        if worktree not in candidate.parents or raw_candidate.is_symlink() or not candidate.is_file():
            return f"unable to safely scan changed content: {relative_path}"
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return f"unable to safely scan changed content: {relative_path}"
        if any(secret.casefold() in content.casefold() for secret in self.secret_patterns):
            return f"secret material detected in changed file: {relative_path}"
        if any(pattern.search(content) for pattern in self.default_secret_assignment_patterns):
            return f"secret material detected in changed file: {relative_path}"
        return None

    def validate(self, worktree: Path, ticket: MicroTicket, *, base_sha: str) -> ValidationResult:
        worktree = Path(worktree).resolve()
        base_sha = self._validated_base_sha(base_sha)
        # Resolve before diffing: no revision expression reaches the diff parser.
        self._git(worktree, "rev-parse", "--verify", f"{base_sha}^{{commit}}")
        head = self._git(worktree, "rev-parse", "HEAD").strip()
        if head != base_sha:
            raise ValidationError("unexpected_head_movement: worktree HEAD differs from recorded base")
        actual_base = self._git(worktree, "merge-base", "HEAD", base_sha).strip()
        if actual_base != base_sha:
            raise ValidationError("worktree base SHA does not match recorded base")
        names = [p for p in self._git(worktree, "diff", "--name-only", base_sha, "--").splitlines() if p]
        # Untracked generated/secrets must be rejected too; git diff alone hides them.
        for line in self._git(worktree, "status", "--porcelain=v1").splitlines():
            candidate = line[3:]
            if not candidate or "__pycache__" in Path(candidate).parts:
                continue
            raw = worktree / candidate
            if raw.is_dir() and not raw.is_symlink():
                for child in sorted(raw.rglob("*")):
                    if child.is_file() and not child.is_symlink():
                        relative = child.relative_to(worktree).as_posix()
                        if "__pycache__" not in child.parts and relative not in names:
                            names.append(relative)
            elif candidate not in names:
                names.append(candidate)
        errors = ["no_changes: model produced no effective diff"] if not names else []
        allowed = set(ticket.allowed_files)
        declared_new = set(ticket.new_test_files)
        errors += [f"changed path outside allowlist: {p}" for p in names if p not in allowed and p not in declared_new]
        for path in names:
            if path not in declared_new:
                continue
            if not is_supported_source(path) or not is_test_path(path):
                errors.append(f"declared new path is not a supported test artifact: {path}")
            candidate = (worktree / path).resolve()
            raw_candidate = worktree / path
            if worktree not in candidate.parents or raw_candidate.is_symlink() or not candidate.is_file():
                errors.append(f"declared new test file is unsafe or missing: {path}")
            if self._git(worktree, "ls-tree", "-r", "--name-only", base_sha, "--", path).strip() == path:
                errors.append(f"declared new test file existed at base: {path}")
        errors += [f"forbidden file type: {p}" for p in names if p.endswith(self.denied_suffixes)]
        errors += [error for path in names if (error := self._secret_scan_error(worktree, path))]
        symbol_errors, scope_unverified = enforce_symbol_scope(worktree, names, ticket, base_sha)
        errors.extend(symbol_errors)
        diff = self._git(worktree, "diff", "--numstat", base_sha, "--")
        changed_lines = sum(int(a) + int(d) for a, d, *_ in (line.split("\t") for line in diff.splitlines() if line))
        for path in declared_new & set(names):
            candidate = worktree / path
            try:
                content = candidate.read_text(encoding="utf-8")
                changed_lines += content.count("\n") + int(bool(content) and not content.endswith("\n"))
            except (OSError, UnicodeDecodeError):
                pass
        if len(names) > ticket.patch_budget.max_files: errors.append("changed file budget exceeded")
        if changed_lines > ticket.patch_budget.max_changed_lines: errors.append("changed line budget exceeded")
        records: list[CommandEvidence] = []
        env = {key: os.environ[key] for key in self.environment_allowlist if key in os.environ}
        if not errors:
            run_cwd = (worktree / ticket.verification.working_directory).resolve()
            if worktree not in run_cwd.parents and run_cwd != worktree:
                raise ValidationError("verification working directory escapes worktree")
            for argv in ticket.verification.commands:
                evidence=self.run_verification_command(argv,allowed_commands=ticket.verification.commands,cwd=run_cwd,timeout_seconds=ticket.verification.timeout_seconds,output_limit=ticket.verification.output_limit)
                records.append(evidence)
                if evidence.returncode: errors.append(f"verification command failed: {' '.join(argv)}")
        compact_parts = errors or ["validation passed"]
        if scope_unverified:
            compact_parts.append("scope_unverified: symbol analysis unavailable; review/checkpoint policy required")
        compact = self._redact("; ".join(compact_parts))
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        path = self.artifact_root / f"validation-{hashlib.sha256((str(worktree)+base_sha).encode()).hexdigest()[:12]}.json"
        path.write_text(json.dumps({"base_sha": base_sha, "changed_files": names, "changed_lines": changed_lines, "scope_unverified": scope_unverified, "errors": errors, "commands": [r.__dict__ for r in records]}, default=list, sort_keys=True), encoding="utf-8")
        return ValidationResult(not errors, tuple(errors), tuple(records), compact, path, scope_unverified)
