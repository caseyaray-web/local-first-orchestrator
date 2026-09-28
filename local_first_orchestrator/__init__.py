"""Transitional package exports; importing pure M1 contracts has no legacy side effects.

The legacy names remain available lazily until the M6 cutover removes them.
"""
from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "Ledger": "ledger",
    "CanonicalState": "states",
    "InvalidTransition": "states",
    "CommentAdapter": "comment_delivery",
    "CommentDeliveryPolicy": "comment_delivery",
    "CommentDeliveryResult": "comment_delivery",
    "CommentDeliveryWorker": "comment_delivery",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value
