"""Durable runtime observations and bounded adaptive planner sizing.

Metrics are derived from authoritative ledger evidence. They never participate
in validation, acceptance, integration, or completion authority.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .ledger import Ledger


@dataclass(frozen=True)
class PlannerSizing:
    max_active_tickets: int = 4
    target_context_tokens: int = 20_000
    sample_count: int = 0
    reason: str = "baseline"

    def as_json(self) -> dict[str, Any]:
        return {
            "max_active_tickets": self.max_active_tickets,
            "target_context_tokens": self.target_context_tokens,
            "sample_count": self.sample_count,
            "reason": self.reason,
        }


class RuntimeMetricsStore:
    """Materialize and summarize append-only ticket observations."""

    MIN_SAMPLES = 6
    BASE_ACTIVE = 4
    MIN_ACTIVE = 2
    MAX_ACTIVE = 6
    BASE_CONTEXT = 20_000
    MIN_CONTEXT = 12_000
    MAX_CONTEXT = 28_000

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    @staticmethod
    def _contract_scope(row: Any) -> tuple[int, int]:
        """Return deterministic context-token estimate and declared path count."""
        contract = {
            "objective": row["objective"],
            "criterion_ids": json.loads(row["criterion_ids_json"] or "[]"),
            "primary_symbol": row["primary_symbol"],
            "allowed_files": json.loads(row["allowed_files_json"] or "[]"),
            "create_files": json.loads(row["create_files_json"] or "[]"),
            "new_test_files": json.loads(row["new_test_files_json"] or "[]"),
            "forbidden_changes": json.loads(row["forbidden_changes_json"] or "[]"),
            "patch_budget": json.loads(row["patch_budget_json"] or "{}"),
            "verification": json.loads(row["verification_json"] or "{}"),
            "risk": row["risk"],
            "review_required": bool(row["review_required"]),
            "max_attempts": row["max_attempts"],
            "dependencies": json.loads(row["dependencies_json"] or "[]"),
        }
        encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        declared = set(contract["allowed_files"]) | set(contract["create_files"]) | set(contract["new_test_files"])
        # Deterministic estimate only; provider token telemetry is not available
        # for all local model routes. Keep the estimate explicit and stable.
        context_tokens = max(1, (len(encoded) + 3) // 4)
        return context_tokens, len(declared)

    def materialize_completed(self, *, limit: int = 500) -> int:
        if not 1 <= limit <= 5000:
            raise ValueError("metrics materialization limit must be between 1 and 5000")
        rows = self.ledger.connection.execute(
            """SELECT t.*,e.created_at AS completed_at
               FROM tickets t
               JOIN events e ON e.id=(SELECT e2.id FROM events e2 WHERE e2.entity_type='ticket' AND e2.entity_id=t.id AND e2.to_state='done' ORDER BY e2.id DESC LIMIT 1)
               LEFT JOIN ticket_runtime_metrics m ON m.ticket_id=t.id
               WHERE t.state='done' AND m.ticket_id IS NULL
               ORDER BY e.created_at,t.id LIMIT ?""",
            (limit,),
        ).fetchall()
        written = 0
        for row in rows:
            ticket_id = str(row["id"])
            attempts = int(self.ledger.connection.execute("SELECT COUNT(*) FROM attempts WHERE ticket_id=?", (ticket_id,)).fetchone()[0])
            if attempts < 1:
                attempts = 1
            durations = self.ledger.connection.execute(
                """SELECT stage,COALESCE(SUM(duration_seconds),0.0) AS seconds
                   FROM model_invocations WHERE ticket_id=? AND status='completed' AND stage IN ('implementation','review') GROUP BY stage""",
                (ticket_id,),
            ).fetchall()
            by_stage = {str(item["stage"]): float(item["seconds"] or 0.0) for item in durations}
            context_tokens, declared_files = self._contract_scope(row)
            accepted = self.ledger.connection.execute("SELECT 1 FROM accepted_candidates WHERE ticket_id=? LIMIT 1", (ticket_id,)).fetchone() is not None
            with self.ledger._transaction() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO ticket_runtime_metrics(
                       ticket_id,feature_id,tranche_id,attempts,accepted,context_tokens,declared_files,
                       implementation_seconds,review_seconds,completed_at,recorded_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ticket_id,
                        row["feature_id"],
                        row["tranche_id"],
                        attempts,
                        1 if accepted else 0,
                        context_tokens,
                        declared_files,
                        by_stage.get("implementation", 0.0),
                        by_stage.get("review", 0.0),
                        int(row["completed_at"]),
                        self.ledger._now(),
                    ),
                )
            written += 1
        return written

    def summary(self, *, limit: int = 500) -> dict[str, Any]:
        paid = self.ledger.connection.execute(
            """SELECT COUNT(*) AS calls,
                      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_calls,
                      SUM(CASE WHEN status='unknown_outcome' THEN 1 ELSE 0 END) AS unknown_outcomes,
                      COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens
               FROM model_calls"""
        ).fetchone()
        paid_usage = {
            "calls": int(paid["calls"] or 0),
            "completed_calls": int(paid["completed_calls"] or 0),
            "unknown_outcomes": int(paid["unknown_outcomes"] or 0),
            "input_tokens": int(paid["input_tokens"] or 0),
            "output_tokens": int(paid["output_tokens"] or 0),
            "monetary_cost_available": False,
        }
        if not 1 <= limit <= 5000:
            raise ValueError("metrics summary limit must be between 1 and 5000")
        rows = self.ledger.connection.execute(
            "SELECT * FROM ticket_runtime_metrics ORDER BY completed_at DESC,ticket_id DESC LIMIT ?", (limit,)
        ).fetchall()
        count = len(rows)
        if not rows:
            return {
                "ticket_count": 0,
                "first_attempt_acceptance_rate": 0.0,
                "acceptance_rate": 0.0,
                "average_attempts": 0.0,
                "average_declared_files": 0.0,
                "context_tokens_estimate": {"p50": 0, "p95": 0},
                "implementation_seconds": {"p50": 0.0, "p95": 0.0},
                "review_seconds": {"p50": 0.0, "p95": 0.0},
                "paid_usage": paid_usage,
            }

        def percentile(values: list[float], fraction: float) -> float:
            values = sorted(values)
            index = max(0, min(len(values) - 1, int(round((len(values) - 1) * fraction))))
            return values[index]

        accepted = sum(int(row["accepted"]) for row in rows)
        first = sum(1 for row in rows if int(row["accepted"]) and int(row["attempts"]) == 1)
        contexts = [float(row["context_tokens"]) for row in rows]
        implementation = [float(row["implementation_seconds"]) for row in rows]
        review = [float(row["review_seconds"]) for row in rows]
        return {
            "ticket_count": count,
            "first_attempt_acceptance_rate": first / count,
            "acceptance_rate": accepted / count,
            "average_attempts": sum(int(row["attempts"]) for row in rows) / count,
            "average_declared_files": sum(int(row["declared_files"]) for row in rows) / count,
            "context_tokens_estimate": {"p50": int(percentile(contexts, 0.5)), "p95": int(percentile(contexts, 0.95))},
            "implementation_seconds": {"p50": percentile(implementation, 0.5), "p95": percentile(implementation, 0.95)},
            "review_seconds": {"p50": percentile(review, 0.5), "p95": percentile(review, 0.95)},
            "paid_usage": paid_usage,
        }

    def recommendation(self) -> PlannerSizing:
        metrics = self.summary()
        count = int(metrics["ticket_count"])
        if count < self.MIN_SAMPLES:
            return PlannerSizing(sample_count=count, reason="insufficient_samples")
        first = float(metrics["first_attempt_acceptance_rate"])
        attempts = float(metrics["average_attempts"])
        active = self.BASE_ACTIVE
        context = self.BASE_CONTEXT
        reason = "stable_baseline"
        if first < 0.55 or attempts >= 1.8:
            active -= 1
            context -= 4_000
            reason = "reduce_scope_after_rework"
        elif first >= 0.85 and attempts <= 1.2:
            active += 1
            context += 4_000
            reason = "expand_with_strong_first_attempt_rate"
        active = min(self.MAX_ACTIVE, max(self.MIN_ACTIVE, active))
        context = min(self.MAX_CONTEXT, max(self.MIN_CONTEXT, context))
        return PlannerSizing(active, context, count, reason)
