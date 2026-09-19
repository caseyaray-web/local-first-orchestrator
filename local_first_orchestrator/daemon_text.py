from __future__ import annotations

import re


_DAEMON_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|token|secret|api[_-]?key|private[_-]?key)\b"
    r"\s*(?:[:=]\s*|[-_])"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)"
)


def bounded_daemon_text(value: object, *, limit: int = 500) -> str | None:
    """Normalize and redact bounded daemon/operator text consistently."""
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    text = " ".join(text.split())
    text = _DAEMON_CREDENTIAL_ASSIGNMENT.sub("[REDACTED]", text)
    return text[:limit]
