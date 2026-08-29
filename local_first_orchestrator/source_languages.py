from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
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
    SourceLanguage("javascript", (".js", ".jsx"), (re.compile(r"\.test\.[^.]+$"), re.compile(r"\.spec\.[^.]+$"))),
    SourceLanguage("typescript", (".ts", ".tsx"), (re.compile(r"\.test\.[^.]+$"), re.compile(r"\.spec\.[^.]+$"))),
)


def language_for(path: str) -> SourceLanguage | None:
    return next((language for language in _LANGUAGES if language.matches(path)), None)


def is_supported_source(path: str) -> bool:
    return language_for(path) is not None


def is_test_path(path: str) -> bool:
    language = language_for(path)
    return language.is_test(path) if language else False


def supported_languages() -> tuple[SourceLanguage, ...]:
    return _LANGUAGES
