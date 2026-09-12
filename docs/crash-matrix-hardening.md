# External-boundary crash matrix hardening

Date: 2026-09-11

This tranche strengthens the scheduler crash matrix at production adapter boundaries. It does not claim exhaustive live-service fault injection against a deployed Hermes or paid provider; it proves that the production adapter paths preserve the same replay/reconciliation guarantees already established by the deterministic scheduler matrix.

## Hardened boundaries

| Boundary | Ambiguous failure exercised | Recovery authority | Duplicate-effect result |
|---|---|---|---|
| Hermes state projection | remote state transition persists, transport raises before local acknowledgement | remote `show` state + state projection outbox | replay observes target state and does not issue a second state write |
| Hermes evidence comment | remote comment append persists, transport raises before local delivery record | persisted comment marker + evidence comment outbox | retry finds marker and records reconciled delivery without a second comment |
| Hermes native dependency link | remote parent/child edge persists, transport raises before local graph evidence | Hermes parent graph + scheduler claim | replay re-reads graph, skips already-present edge, and records graph evidence |
| Generated microticket create | remote create persists before worker crash | Hermes create idempotency key + generated projection outbox | existing production-adapter test replays same key and converges to one task |
| Model invocation | invocation start/completion journal | `model_invocations` | completed invocation is replayed from durable result; unknown started invocation stops |
| Git integration | commit intent / exact Git head evidence | `git_commit_intents` and accepted candidate evidence | replay reconciles exact commit rather than creating a second accepted integration |
| Paid checkpoint/escalation | completed, unknown, and budget-blocked calls | paid reservation + model call journal | completed call is reused; unknown outcome is never reinvoked; budget pause requires explicit approval |
| Tranche checkpoint/materialization | durable checkpoint/materialization evidence | tranche evidence tables | replay resumes from persisted effect and does not repeat successor planning |

## New production-adapter proofs

`tests/test_external_boundary_crash_hardening.py` exercises `HermesBoardAdapter` itself with stateful transport runners that persist the remote effect before raising an `OSError`. This is intentionally one layer stronger than an in-memory board double: command construction, adapter exception translation, worker retry/reconciliation, and ledger acknowledgement all participate.

`tests/test_process_next_scheduler.py::test_native_dependency_graph_recovers_real_adapter_link_transport_loss_without_duplicate_edge` performs the same production-adapter proof for Hermes native dependency linking.

## Safety invariants retained

1. Unknown model or paid-call outcomes remain fail-closed; they are not blindly replayed.
2. Replayable board/dependency effects must first re-read remote state and converge from durable identity.
3. Idempotency keys/markers are durable and derived from persisted intent, never regenerated after a crash.
4. Scheduler leases must cover the bounded external-effect horizon; production adapter tests use the same timeout/lease invariant as runtime.
5. A recovered remote effect is not considered locally complete until the corresponding ledger evidence is durably recorded.

## Remaining live-service proof

The remaining gap is environmental rather than a missing recovery mechanism: repeat this matrix against the configured live Hermes board/provider with controlled process termination at the same boundaries. Paid-provider live proof remains separately tracked because external billing/unknown-outcome behavior requires an explicitly authorized real call.
