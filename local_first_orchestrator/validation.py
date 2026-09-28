from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .git_security import safe_git_argv, safe_git_env

from .ticket import MicroTicket, declared_ticket_paths
from .source_languages import is_supported_source, is_test_path, normalized_repository_path
from .symbols import contract_target_scope, enforce_symbol_scope


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
    documentation_suffixes = (".adoc", ".md", ".mdx", ".rst", ".txt")
    default_secret_assignment_patterns = (
        re.compile(r"(?im)^\s*(?:[A-Za-z][A-Za-z0-9_-]*[_-])?(?:api[_-]?key|secret|password|token|private[_-]?key)\s*[:=]\s*(?P<value>['\"][^'\"]+['\"]|\.\.\.|[^\s#]{8,})"),
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
        env = {key: os.environ[key] for key in self.environment_allowlist if key in os.environ}
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            message = f"verification launch failed: {type(exc).__name__}"
            return CommandEvidence(argv, self.LAUNCH_FAILURE_RETURN_CODE, time.monotonic()-started,
                                   "", self._redact(message[:output_limit]), len(message) > output_limit)
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        overflow = {"stdout": False, "stderr": False}
        timed_out = False
        cleanup_failed = False
        deadline = started + timeout_seconds
        cleanup_deadline = deadline
        phase = 0
        try:
            with selectors.DefaultSelector() as selector:
                assert process.stdout is not None and process.stderr is not None
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map() or process.poll() is None:
                    now = time.monotonic()
                    if phase == 0 and now >= deadline:
                        timed_out = True
                        phase, cleanup_deadline = 1, now + .5
                        try: os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError: pass
                    elif phase == 1 and now >= cleanup_deadline:
                        phase, cleanup_deadline = 2, now + .5
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                    elif phase == 2 and now >= cleanup_deadline:
                        cleanup_failed = True
                        break
                    cutoff = deadline if phase == 0 else cleanup_deadline
                    for key, _ in selector.select(timeout=min(.1, max(0, cutoff - time.monotonic()))):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        stream = key.data
                        remaining = max(0, output_limit - len(captured[stream]))
                        captured[stream].extend(chunk[:remaining])
                        overflow[stream] |= len(chunk) > remaining
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            try: process.wait(timeout=.5)
            except subprocess.TimeoutExpired: cleanup_failed = True
        finally:
            if process.stdout is not None: process.stdout.close()
            if process.stderr is not None: process.stderr.close()
        raw_out = captured["stdout"].decode("utf-8", errors="replace")
        raw_err = captured["stderr"].decode("utf-8", errors="replace")
        if timed_out and not raw_err: raw_err = "verification command timed out"
        if cleanup_failed: raw_err = "verification cleanup failed"
        code = self.TIMEOUT_RETURN_CODE if timed_out else process.returncode
        return CommandEvidence(argv, code, time.monotonic()-started,
                               self._redact(raw_out), self._redact(raw_err[:output_limit]),
                               overflow["stdout"] or overflow["stderr"] or len(raw_err) > output_limit)

    def validate_strict(
        self, worktree: Path, ticket: MicroTicket, *, base_sha: str,
        trusted_commands: tuple[tuple[str, ...], ...], expected_head_sha: str | None = None,
        trusted_max_timeout_seconds: int, trusted_max_output_limit: int,
    ) -> ValidationResult:
        """M1 boundary: authorize commands and budgets independently; pin candidate content."""
        if not 0 < trusted_max_timeout_seconds <= 3600 or not 0 < trusted_max_output_limit <= 1_000_000:
            raise ValidationError("trusted verification limits must be finite and positive")
        if not 0 < ticket.verification.timeout_seconds <= trusted_max_timeout_seconds:
            raise ValidationError("verification exceeds trusted timeout limit")
        if not 0 < ticket.verification.output_limit <= trusted_max_output_limit:
            raise ValidationError("verification exceeds trusted output limit")
        if not trusted_commands or not ticket.verification.commands or any(
            command not in trusted_commands for command in ticket.verification.commands
        ):
            raise ValidationError("verification commands are not in the trusted allowlist")
        worktree = Path(worktree).resolve(strict=True)
        paths = declared_ticket_paths(ticket)
        if any(normalized_repository_path(path) is None for path in paths):
            raise ValidationError("candidate paths must be normalized repository-relative files")

        artifact_root = self.artifact_root.resolve()
        if artifact_root == worktree or worktree in artifact_root.parents:
            raise ValidationError("validation artifacts must be outside the candidate worktree")

        def identity() -> tuple[str, str, tuple[tuple[str, str | None], ...]]:
            head = self._git(worktree, "rev-parse", "HEAD").strip()
            tracked_diff = self._git(worktree, "diff", "--binary", "HEAD", "--")
            tracked_paths = self._git(worktree, "diff", "--name-only", "-z", "HEAD", "--")
            untracked = self._git(worktree, "ls-files", "--others", "--exclude-standard", "-z", "--")
            ignored = self._git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--")
            extra = {path for path in (untracked + ignored).split("\0") if path}
            contents = []
            for path in sorted(set(paths) | extra | {p for p in tracked_paths.split("\0") if p}):
                candidate = worktree / path
                resolved = candidate.resolve()
                if candidate.is_symlink() or worktree not in resolved.parents:
                    raise ValidationError(f"candidate path escapes or is a symlink: {path}")
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.is_file() else None
                contents.append((path, digest))
            return head, hashlib.sha256(tracked_diff.encode()).hexdigest(), tuple(contents)

        before = identity()
        result = self.validate(worktree, ticket, base_sha=base_sha, expected_head_sha=expected_head_sha)
        if identity() != before:
            raise ValidationError("candidate content or head changed during verification; evidence is stale")
        return result

    def _git(self, path: Path, *args: str) -> str:
        """Run bounded, non-interactive internal Git inspection only."""
        env = safe_git_env()
        argv = safe_git_argv(args)
        try:
            completed = subprocess.run(argv, cwd=path, env=env, text=True, capture_output=True, timeout=15, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValidationError(f"git inspection timed out: {' '.join(args[:3])}") from exc
        if completed.returncode:
            detail = self._redact((completed.stderr or completed.stdout or "git command failed")[:2000]).strip()
            raise ValidationError(f"git inspection failed: {detail}")
        if len(completed.stdout) > 1_000_000:
            raise ValidationError("git inspection exceeded output limit; cannot pin complete candidate identity")
        return completed.stdout

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
        for pattern in self.default_secret_assignment_patterns:
            for match in pattern.finditer(content):
                value = match.group("value")
                if relative_path.casefold().endswith(self.documentation_suffixes):
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                        value = value[1:-1]
                    if value == "...":
                        continue
                return f"secret material detected in changed file: {relative_path}"
        return None

    def validate(self, worktree: Path, ticket: MicroTicket, *, base_sha: str, expected_head_sha: str | None = None) -> ValidationResult:
        worktree = Path(worktree).resolve()
        base_sha = self._validated_base_sha(base_sha)
        # Resolve before diffing: no revision expression reaches the diff parser.
        self._git(worktree, "rev-parse", "--verify", f"{base_sha}^{{commit}}")
        head = self._git(worktree, "rev-parse", "HEAD").strip()
        expected_head = base_sha if expected_head_sha is None else self._validated_base_sha(expected_head_sha)
        self._git(worktree, "rev-parse", "--verify", f"{expected_head}^{{commit}}")
        if head != expected_head:
            raise ValidationError("unexpected_head_movement: worktree HEAD differs from authorized validation head")
        actual_base = self._git(worktree, "merge-base", "HEAD", base_sha).strip()
        if actual_base != base_sha:
            raise ValidationError("worktree base SHA does not match recorded base")
        names = [p for p in self._git(worktree, "diff", "--name-only", base_sha, "--").splitlines() if p]
        # Ask Git for every untracked path explicitly. Directory-summary porcelain output can
        # hide nested symlinks, which must remain visible to the ticket allowlist and safety scan.
        untracked = self._git(worktree, "ls-files", "--others", "--exclude-standard", "-z", "--")
        for candidate in untracked.split("\0"):
            if not candidate or "__pycache__" in Path(candidate).parts:
                continue
            if candidate not in names:
                names.append(candidate)
        errors = ["no_changes: model produced no effective diff"] if not names else []
        allowed = set(ticket.allowed_files)
        declared_create = set(ticket.create_files)
        declared_new = declared_create | set(ticket.new_test_files)
        errors += [f"changed path outside allowlist: {p}" for p in names if p not in set(declared_ticket_paths(ticket))]
        for path in names:
            if path not in declared_new:
                continue
            if path in declared_create:
                if not is_supported_source(path) or is_test_path(path): errors.append(f"declared create path is not a supported non-test artifact: {path}")
            elif not is_supported_source(path) or not is_test_path(path):
                errors.append(f"declared new path is not a supported test artifact: {path}")
            candidate = (worktree / path).resolve()
            raw_candidate = worktree / path
            if worktree not in candidate.parents or raw_candidate.is_symlink() or not candidate.is_file():
                errors.append(f"declared new file is unsafe or missing: {path}")
            if self._git(worktree, "ls-tree", "-r", "--name-only", base_sha, "--", path).strip() == path:
                errors.append(f"declared new file existed at base: {path}")
        errors += [f"forbidden file type: {p}" for p in names if p.endswith(self.denied_suffixes)]
        errors += [error for path in names if (error := self._secret_scan_error(worktree, path))]
        if contract_target_scope(ticket) == "file":
            symbol_errors, scope_unverified = (), False
        else:
            symbol_errors, scope_unverified = enforce_symbol_scope(worktree, names, ticket, base_sha)
        errors.extend(symbol_errors)
        diff = self._git(worktree, "diff", "--numstat", base_sha, "--")
        numstat_rows = [line.split("\t") for line in diff.splitlines() if line]
        changed_lines = sum(int(parts[0]) + int(parts[1]) for parts in numstat_rows)
        numstat_paths = {parts[-1] for parts in numstat_rows if len(parts) >= 3}
        for path in (declared_new & set(names)) - numstat_paths:
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
