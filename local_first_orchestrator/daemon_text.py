from __future__ import annotations

import re


_DAEMON_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|token|secret|api[_-]?key|private[_-]?key)\b"
    r"\s*(?:[:=]\s*|[-_])"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)"
)
_DAEMON_BEARER_AUTH = re.compile(
    r"(?i)\b(?:authorization\s*:\s*)?bearer\s+"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)"
)
_DAEMON_BASIC_AUTH = re.compile(
    r"(?i)\b(?:authorization\s*:\s*)?basic\s+"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)"
)
_DAEMON_CREDENTIAL_URL = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@"
)
_DAEMON_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DAEMON_STATUS_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")


def bounded_daemon_text(value: object, *, limit: int = 500) -> str | None:
    """Normalize and redact bounded daemon/operator text consistently."""
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    text = " ".join(text.split())
    text = _DAEMON_CREDENTIAL_ASSIGNMENT.sub("[REDACTED]", text)
    text = _DAEMON_BEARER_AUTH.sub("[REDACTED]", text)
    text = _DAEMON_BASIC_AUTH.sub("[REDACTED]", text)
    text = _DAEMON_CREDENTIAL_URL.sub(r"\1[REDACTED]@", text)
    return text[:limit]


def validate_daemon_worker_id(value: object) -> str:
    """Return one exact safe daemon worker identity or fail closed."""
    if not isinstance(value, str) or not _DAEMON_WORKER_ID.fullmatch(value):
        raise ValueError("worker_id must be 1-128 ASCII characters using letters, digits, '.', '_', ':', or '-'")
    return value


def validate_daemon_status_label(value: object) -> str:
    """Return one exact safe required daemon status/state label or fail closed."""
    if not isinstance(value, str) or not _DAEMON_STATUS_LABEL.fullmatch(value):
        raise ValueError("daemon status must be 1-80 ASCII characters using letters, digits, '.', '_', ':', or '-'")
    return value
