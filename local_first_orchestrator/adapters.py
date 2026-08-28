from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .states import CanonicalState


class BoardAdapter(Protocol):
    """The sole future boundary for projecting ledger state to any board."""

    def set_state(self, ticket_id: str, state: CanonicalState, *, idempotency_key: str) -> None: ...


@dataclass
class FakeBoardAdapter:
    """In-memory test adapter. It performs no I/O and cannot mutate Hermes."""

    states: dict[str, CanonicalState] = field(default_factory=dict)
    projections: list[tuple[str, CanonicalState]] = field(default_factory=list)
    idempotency_keys: set[str] = field(default_factory=set)
    fail_after_effect_once: bool = False

    def set_state(self, ticket_id: str, state: CanonicalState, *, idempotency_key: str) -> None:
        if idempotency_key not in self.idempotency_keys:
            self.idempotency_keys.add(idempotency_key)
            self.states[ticket_id] = state
            self.projections.append((ticket_id, state))
        if self.fail_after_effect_once:
            self.fail_after_effect_once = False
            raise RuntimeError("adversarial post-effect failure")
