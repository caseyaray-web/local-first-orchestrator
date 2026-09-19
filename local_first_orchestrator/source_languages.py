from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


def normalized_repository_path(path: str) -> str | None:
    """Return a canonical repo-relative file path, or None when unsafe."""
    if not isinstance(path, str) or not path or "\\" in path:
        return None
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    normalized = candidate.as_posix()
    return normalized if normalized == path and not normalized.endswith("/") else None
import re


@dataclass(frozen=True)
class SourceLanguage:
    name: str
    extensions: tuple[str, ...]
    test_patterns: tuple[re.Pattern[str], ...]

    def matches(self, path: str) -> bool:
        return PurePosixPath(path).suffix.lower() in self.extensions

    def is_test(self, path: str) -> bool:
        name = PurePosixPath(path).name
        return any(pattern.search(name) for pattern in self.test_patterns) or "/tests/" in f"/{path}"


_LANGUAGES = (
    SourceLanguage("python", (".py",), (re.compile(r"^test_"), re.compile(r"_test\.py$"))),
    SourceLanguage("javascript", (".js", ".jsx", ".mjs"), (re.compile(r"^test-"), re.compile(r"\.test\.[^.]+$"), re.compile(r"\.spec\.[^.]+$"))),
    SourceLanguage("typescript", (".ts", ".tsx"), (re.compile(r"\.test\.[^.]+$"), re.compile(r"\.spec\.[^.]+$"))),
)


def language_for(path: str) -> SourceLanguage | None:
    return next((language for language in _LANGUAGES if language.matches(path)), None)


def is_supported_source(path: str) -> bool:
    return language_for(path) is not None


def is_supported_repository_file(path: str) -> bool:
    """Files eligible for immutable manifest evidence and ordinary edit scope."""
    return is_supported_source(path) or path == "package.json"


def is_test_path(path: str) -> bool:
    language = language_for(path)
    if language is None:
        return False
    candidate = PurePosixPath(path)
    if len(candidate.parts) >= 2 and candidate.parts[0] == "scripts" and candidate.name.startswith("verify-"):
        return True
    return language.is_test(path)


def supported_languages() -> tuple[SourceLanguage, ...]:
    return _LANGUAGES
