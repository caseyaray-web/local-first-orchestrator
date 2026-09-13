# Runtime metrics and adaptive sizing acceptance

Date: 2026-09-12

## Result

The final v2 implementation tranche is complete for bounded runtime metrics and adaptive decomposition sizing.

## Durable observations

A new append-only `ticket_runtime_metrics` ledger table freezes one observation per completed ticket. Materialization is idempotent and can be rerun after a crash; metrics are derived from already-authoritative ticket, attempt, model-invocation, acceptance, and completion evidence.

Each ticket observation records:

- attempt count;
- accepted/not-accepted outcome;
- deterministic context-size token estimate derived from the normalized ticket contract;
- declared file count;
- completed implementation model duration;
- completed review model duration;
- completion and materialization timestamps.

The table is append-only by database trigger. Metrics failure is intentionally non-authoritative: scheduler completion first finishes through the existing acceptance/finality path, then opportunistically materializes missing metrics. Any failure in metrics collection is non-blocking and can be backfilled later with `runtime-metrics`.

## Paid usage / cost telemetry

The operator summary also aggregates the existing paid `model_calls` ledger:

- total paid calls;
- completed paid calls;
- unknown outcomes;
- provider-reported input tokens where available; and
- provider-reported output tokens where available.

The current provider contract does not persist authoritative monetary cost for every route, so the metrics output explicitly reports `monetary_cost_available: false` rather than inventing dollar estimates.

## Bounded adaptive policy

Adaptive sizing applies only to decomposition planning hints. It does **not** change deterministic validation, patch-budget policy, acceptance authority, review authority, Git authority, or finality.

Baseline planner limits are:

- active tranche max tickets: 4;
- target context tokens: 20,000.

No adaptation occurs before six completed-ticket observations exist.

With sufficient samples:

- weak first-attempt performance / repeated rework reduces the planner hint to 3 active tickets and 16,000 target context tokens;
- strong first-attempt performance expands the hint to 5 active tickets and 24,000 target context tokens;
- otherwise the baseline remains unchanged.

The implementation additionally hard-bounds any recommendation to 2–6 active tickets and 12,000–28,000 target context tokens.

Both initial and successor decomposition packet builders consume the recommendation. The exact adaptive-sizing recommendation is persisted in planner provenance, while the request bytes bind the actual packet limits for replay/reconciliation.

## Operator visibility

`runtime-metrics` materializes any missing completed-ticket observations and returns:

- aggregate runtime/outcome metrics;
- paid usage/token metrics; and
- the current adaptive-sizing recommendation with sample count and reason.

The Hermes dashboard status payload and Local First dashboard page also expose the runtime summary and current planner recommendation. The UI labels context tokens as deterministic estimates rather than provider billing telemetry.

## Acceptance tests

`tests/test_runtime_metrics.py` proves:

- completed-ticket materialization is idempotent;
- persisted observations are append-only;
- deterministic scope/context measurements are frozen;
- poor outcome history produces bounded scope reduction;
- strong first-attempt history produces bounded expansion; and
- decomposition packets and planner provenance receive the recommendation.

Existing decomposition and scheduler regression suites prove that adaptive metadata does not break successor replay or completion behavior.

## Completion assessment

The final design-v2 roadmap item is satisfied for the current configured system: runtime/outcome/token measurements are durable and operator-visible, and prior outcomes feed bounded decomposition ticket/context sizing without entering the trust/finality path.