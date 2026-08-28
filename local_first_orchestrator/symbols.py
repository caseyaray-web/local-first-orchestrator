from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


class SymbolIndex:
    """Small, deterministic Python AST index; other languages fail closed as unverified."""

    def __init__(self, repository: Path) -> None:
        self.repository = Path(repository).resolve()

    def _source(self, relative_path: str) -> str:
        return (self.repository / relative_path).read_text(encoding="utf-8")

    @staticmethod
    def _definitions(path: str, source: str) -> dict[str, Symbol]:
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        symbols: dict[str, Symbol] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start, end = node.lineno - 1, node.end_lineno
                symbols[node.name] = Symbol(path, node.name, "".join(lines[start:end]))
        return symbols

    def select_for_ticket(self, ticket: MicroTicket) -> SymbolSelection:
        try:
            relative, name = ticket.primary_symbol.split("::", 1)
            if not relative.endswith(".py"):
                raise ValueError("unsupported language")
            primary_source = self._source(relative)
            definitions = self._definitions(relative, primary_source)
            primary = definitions[name]
            tree = ast.parse(primary_source)
            target = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name)
            referenced = {node.func.id for node in ast.walk(target) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            dependencies = tuple(definitions[called] for called in sorted(referenced & definitions.keys()))
            callers: list[Symbol] = []
            tests: list[Symbol] = []
            for path in sorted(ticket.allowed_files):
                if not path.endswith(".py"):
                    continue
                source = self._source(path)
                for candidate in self._definitions(path, source).values():
                    node = ast.parse(candidate.text)
                    calls_primary = any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name for call in ast.walk(node))
                    if not calls_primary:
                        continue
                    if Path(path).name.startswith("test_") or "/tests/" in path:
                        tests.append(candidate)
                    elif candidate.identifier != primary.identifier:
                        callers.append(candidate)
            return SymbolSelection(primary, dependencies, tuple(callers), tuple(tests))
        except (OSError, SyntaxError, KeyError, ValueError):
            return SymbolSelection(Symbol("", "", ""), (), (), (), True)


def changed_symbols(repository: Path, relative_path: str, base_sha: str) -> set[str] | None:
    """Return changed top-level Python definitions, or None where scope cannot be proven."""
    if not relative_path.endswith(".py"):
        return None
    repo = Path(repository).resolve()
    try:
        before = subprocess.run(("git", "show", f"{base_sha}:{relative_path}"), cwd=repo, text=True, capture_output=True, check=True).stdout
        after = (repo / relative_path).read_text(encoding="utf-8")
        old = SymbolIndex._definitions(relative_path, before)
        new = SymbolIndex._definitions(relative_path, after)
    except (OSError, SyntaxError, subprocess.CalledProcessError):
        return None
    names = set(old) | set(new)
    return {name for name in names if old.get(name) != new.get(name)}


def enforce_symbol_scope(repository: Path, changed_paths: list[str], ticket: MicroTicket, base_sha: str) -> tuple[tuple[str, ...], bool]:
    """Enforce one primary production symbol and one test symbol when Python is parseable."""
    errors: list[str] = []
    unverified = False
    expected_path, expected_symbol = ticket.primary_symbol.split("::", 1)
    for path in changed_paths:
        changed = changed_symbols(repository, path, base_sha)
        if changed is None:
            unverified = True
            continue
        is_test = Path(path).name.startswith("test_") or "/tests/" in path
        if is_test:
            if len(changed) > 1:
                errors.append(f"symbol scope exceeded in test file: {path}")
        elif path != expected_path or changed - {expected_symbol}:
            errors.append(f"symbol scope exceeded in production file: {path}")
    return tuple(errors), unverified
