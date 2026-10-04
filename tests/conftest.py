"""Test-only Hermes session-context binding for native-worker fixtures."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
import sys
from types import ModuleType

import pytest


_UNBOUND = object()
_environment_session_id: ContextVar[str | None] = ContextVar("environment_session_id", default=None)
_scoped_session_id: ContextVar[object | str] = ContextVar("scoped_session_id", default=_UNBOUND)


def _get_session_env(name: str, default=None):
    if name != "HERMES_SESSION_ID":
        return default
    scoped = _scoped_session_id.get()
    if scoped is not _UNBOUND:
        return scoped
    value = _environment_session_id.get()
    return default if value is None else value


@contextmanager
def _scoped_current_session_id(session_id: str):
    token = _scoped_session_id.set(session_id)
    try:
        yield
    finally:
        _scoped_session_id.reset(token)


# The plugin imports this supported API lazily at public entry.  Supply a
# contextvar-shaped test transport only when Hermes itself is not installed.
if "gateway.session_context" not in sys.modules:
    session_context = ModuleType("gateway.session_context")
    setattr(session_context, "get_session_env", _get_session_env)
    setattr(session_context, "scoped_current_session_id", _scoped_current_session_id)
    gateway = sys.modules.setdefault("gateway", ModuleType("gateway"))
    setattr(gateway, "session_context", session_context)
    sys.modules["gateway.session_context"] = session_context


@pytest.fixture
def scoped_current_session_id():
    return _scoped_current_session_id


@pytest.fixture(autouse=True)
def _bind_synthetic_hermes_session_context(monkeypatch):
    """Mirror fixture session writes into a task-local ContextVar and restore tokens."""
    initial = os.environ.get("HERMES_SESSION_ID")
    initial_token = _environment_session_id.set(initial)
    setenv = monkeypatch.setenv
    delenv = monkeypatch.delenv

    def set_bound_env(name, value, prepend=None):
        setenv(name, value, prepend=prepend)
        if name == "HERMES_SESSION_ID":
            _environment_session_id.set(os.environ.get(name))

    def delete_bound_env(name, raising=True):
        delenv(name, raising=raising)
        if name == "HERMES_SESSION_ID":
            _environment_session_id.set(None)

    monkeypatch.setattr(monkeypatch, "setenv", set_bound_env)
    monkeypatch.setattr(monkeypatch, "delenv", delete_bound_env)
    try:
        yield
    finally:
        _environment_session_id.reset(initial_token)
