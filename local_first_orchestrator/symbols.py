from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .source_languages import is_test_path, language_for
from .ticket import MicroTicket


@dataclass(frozen=True)
class Symbol:
    path: str
    name: str
    text: str

    @property
    def identifier(self) -> str:
        return f"{self.path}::{self.name}"


@dataclass(frozen=True)
class SymbolSelection:
    primary: Symbol
    dependencies: tuple[Symbol, ...]
    callers: tuple[Symbol, ...]
    tests: tuple[Symbol, ...]
    scope_unverified: bool = False
    scope: str = "symbol"


def _python_symbols(path: str, source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    return tuple(node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))


# Deliberately conservative: only declarations beginning at column zero are
# accepted. This avoids claiming nested declarations without a JS parser.
_JS_DECLARATIONS = (
    re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\b"),
    re.compile(r"^(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)\b"),
    re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:(?:async\s+)?function\b|(?:async\s*)?(?:\([^\n]*\)|[A-Za-z_$][\w$]*)\s*=>)"),
)


def _javascript_symbols(path: str, source: str) -> tuple[str, ...]:
    names: list[str] = []
    for line in source.splitlines():
        if line[:1].isspace():
            continue
        candidate = line.split("//", 1)[0].strip()
        if not candidate or candidate.startswith(("/*", "*", "*/")):
            continue
        for pattern in _JS_DECLARATIONS:
            match = pattern.match(candidate)
            if match:
                names.append(match.group(1))
                break
    return tuple(dict.fromkeys(names))


_EXTRACTORS: dict[str, Callable[[str, str], tuple[str, ...]]] = {
    "python": _python_symbols,
    "javascript": _javascript_symbols,
    "typescript": _javascript_symbols,
}


def symbols_for(path: str, committed_content: str) -> tuple[str, ...]:
    """Return deterministic top-level symbols for a supported source file."""
    language = language_for(path)
    if language is None:
        return ()
    try:
        return _EXTRACTORS[language.name](path, committed_content)
    except (SyntaxError, UnicodeError):
        return ()


class SymbolIndex:
    """Small language-aware index; JS/TS extraction is intentionally conservative."""

    def __init__(self, repository: Path) -> None:
        self.repository = Path(repository).resolve()

    def _source(self, relative_path: str) -> str:
        return (self.repository / relative_path).read_text(encoding="utf-8")

    @staticmethod
    def _definitions(path: str, source: str) -> dict[str, Symbol]:
        language = language_for(path)
        if language is None:
            raise ValueError("unsupported language")
        if language.name != "python":
            return {name: Symbol(path, name, source) for name in symbols_for(path, source)}
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        return {node.name: Symbol(path, node.name, "".join(lines[node.lineno - 1:node.end_lineno]))
                for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    def select_for_ticket(self, ticket: MicroTicket) -> SymbolSelection:
        try:
            relative, name = ticket.primary_symbol.split("::", 1)
        except ValueError:
            relative, name = "", ""
        try:
            definitions = self._definitions(relative, self._source(relative))
            primary = definitions[name]
            language = language_for(relative)
            if language is None:
                raise ValueError("unsupported language")
            if language.name != "python":
                # Regex extraction cannot safely infer dependency/caller scope.
                return SymbolSelection(primary, (), (), ())
            primary_source = self._source(relative)
            tree = ast.parse(primary_source)
            target = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name)
            referenced = {node.func.id for node in ast.walk(target) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            dependencies = tuple(definitions[called] for called in sorted(referenced & definitions.keys()))
            callers: list[Symbol] = []
            tests: list[Symbol] = []
            for path in sorted(ticket.allowed_files):
                if language_for(path) is None or language_for(path).name != "python":
                    continue
                for candidate in self._definitions(path, self._source(path)).values():
                    node = ast.parse(candidate.text)
                    if not any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name for call in ast.walk(node)):
                        continue
                    (tests if is_test_path(path) else callers).append(candidate)
            return SymbolSelection(primary, dependencies, tuple(callers), tuple(tests))
        except (OSError, SyntaxError, KeyError, ValueError):
            language = language_for(relative)
            if (len(ticket.allowed_files) == 1 and not ticket.new_test_files
                    and relative == ticket.allowed_files[0]
                    and is_test_path(relative)
                    and language is not None
                    and language.name == "javascript"):
                try:
                    return SymbolSelection(Symbol(relative, "", self._source(relative)), (), (), (), False, "file")
                except (OSError, UnicodeError):
                    pass
            return SymbolSelection(Symbol("", "", ""), (), (), (), True)


def changed_symbols(repository: Path, relative_path: str, base_sha: str) -> set[str] | None:
    """Return changed top-level definitions, or None when scope is not provable."""
    if language_for(relative_path) is None:
        return None
    repo = Path(repository).resolve()
    try:
        before = subprocess.run(("git", "show", f"{base_sha}:{relative_path}"), cwd=repo, text=True, capture_output=True, check=True).stdout
        after = (repo / relative_path).read_text(encoding="utf-8")
        old_definitions = SymbolIndex._definitions(relative_path, before)
        new_definitions = SymbolIndex._definitions(relative_path, after)
    except (OSError, SyntaxError, UnicodeError, KeyError, ValueError, subprocess.CalledProcessError):
        return None
    if not before.strip() or (language_for(relative_path).name != "python" and after.strip() and not new_definitions and old_definitions):
        return None
    # Python compares each definition's exact source span. The conservative
    # JS/TS definitions contain the whole file, so a changed file reports all
    # its declarations rather than pretending body ownership is known.
    return {name for name in set(old_definitions) | set(new_definitions)
            if old_definitions.get(name) != new_definitions.get(name)}


def enforce_symbol_scope(repository: Path, changed_paths: list[str], ticket: MicroTicket, base_sha: str) -> tuple[tuple[str, ...], bool]:
    errors: list[str] = []
    unverified = False
    try:
        expected_path, expected_symbol = ticket.primary_symbol.split("::", 1)
    except ValueError:
        return ("invalid primary symbol",), True
    for path in changed_paths:
        if path in ticket.new_test_files:
            # There is no base symbol table for an authorized newly-created test.
            # File-level authorization is the bounded scope proof for this path.
            continue
        changed = changed_symbols(repository, path, base_sha)
        if changed is None:
            unverified = True
            continue
        if is_test_path(path):
            if len(changed) > 1:
                errors.append(f"symbol scope exceeded in test file: {path}")
        elif path != expected_path or changed - {expected_symbol}:
            errors.append(f"symbol scope exceeded in production file: {path}")
    return tuple(errors), unverified
