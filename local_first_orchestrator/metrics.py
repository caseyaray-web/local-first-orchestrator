from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Iterable


@dataclass(frozen=True)
class TicketOutcome:
    ticket_id: str
    attempts: int
    accepted: bool
    reverted: bool
    context_tokens: int
    changed_symbols: int


@dataclass(frozen=True)
class SizingRecommendation:
    max_production_symbols: int
    reduce_scope: bool
    rationale: str


class MetricsCollector:
    """Pure aggregation over persisted/loaded ticket outcomes; no model decisions."""

    @staticmethod
    def _percentile(values: list[int], percentile: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * percentile))]

    def report(self, outcomes: Iterable[TicketOutcome]) -> dict[str, object]:
        rows = list(outcomes)
        total = len(rows)
        accepted = [row for row in rows if row.accepted]
        first_attempt = [row for row in rows if row.accepted and row.attempts == 1]
        tokens = [row.context_tokens for row in rows]
        return {
            "ticket_count": total,
            "first_attempt_acceptance_rate": len(first_attempt) / total if total else 0.0,
            "acceptance_rate": len(accepted) / total if total else 0.0,
            "revert_rate": sum(row.reverted for row in rows) / total if total else 0.0,
            "average_attempts": mean([row.attempts for row in rows]) if rows else 0.0,
            "context_tokens": {"p50": self._percentile(tokens, 0.5), "p95": self._percentile(tokens, 0.95)},
            "changed_symbols": {"average": mean([row.changed_symbols for row in rows]) if rows else 0.0},
        }


class AdaptiveSizingPolicy:
    """Conservative, configurable experiment gate; observations never mutate tickets."""

    def __init__(self, *, minimum_comparable_tickets: int = 20, promote_rate: float = 0.85, reduce_rate: float = 0.70) -> None:
        if minimum_comparable_tickets < 1 or not 0 <= reduce_rate <= promote_rate <= 1:
            raise ValueError("invalid sizing policy thresholds")
        self.minimum_comparable_tickets = minimum_comparable_tickets
        self.promote_rate = promote_rate
        self.reduce_rate = reduce_rate

    def evaluate(self, outcomes: Iterable[TicketOutcome]) -> SizingRecommendation:
        rows = list(outcomes)
        if len(rows) < self.minimum_comparable_tickets:
            return SizingRecommendation(1, False, "insufficient comparable tickets; retain conservative one-symbol profile")
        first_attempt_rate = sum(row.accepted and row.attempts == 1 for row in rows) / len(rows)
        revert_rate = sum(row.reverted for row in rows) / len(rows)
        if first_attempt_rate >= self.promote_rate and revert_rate == 0:
            return SizingRecommendation(2, False, "eligible for a bounded two-symbol sizing experiment")
        if first_attempt_rate < self.reduce_rate:
            return SizingRecommendation(1, True, "first-attempt acceptance is below the reduction threshold")
        return SizingRecommendation(1, False, "retain conservative one-symbol profile")
