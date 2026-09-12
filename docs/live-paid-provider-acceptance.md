# Live paid checkpoint / escalation acceptance

Date: 2026-09-11

## Result

The paid checkpoint/escalation integration has now been exercised against a real configured Hermes paid-provider route using `HermesPaidModelAdapter` with explicit non-secret route provenance:

- provider: `openai-codex`
- model: `gpt-5.6-sol`
- profile identity: `worker-architect-sol`

The acceptance run used isolated temporary repositories and ledgers and did not modify the operator registration at `~/.hermes/local-first-orchestrator/operator-config.json`.

## Proof A — budget pause, one-call approval, completed-call replay

A real checkpoint stage was prepared from deterministic tranche checkpoint evidence with checkpoint budget initially set to zero.

Observed lifecycle:

1. scheduler claimed the paid checkpoint stage;
2. budget exhaustion stopped before provider execution;
3. zero `model_calls` existed after the blocked attempt;
4. one explicit purpose-scoped operator approval granted exactly one checkpoint call;
5. the real provider was invoked once through packet-only safe Hermes chat with explicit provider/model selectors;
6. the provider returned a schema-valid `reject` decision;
7. the process was intentionally treated as crashed after the durable model-call response but before scheduler effect application;
8. a restarted scheduler reused the original claim and durable completed provider response;
9. no second provider call occurred; and
10. immutable paid checkpoint evidence was persisted from the recovered result.

Acceptance result:

`CHECKPOINT_OK {'decision': 'reject', 'claim_reused': True, 'completed_calls': 1, 'approval_calls': 1}`

The decision value is not itself an acceptance criterion. The proof concerns reservation, authorization, execution, durable response, and replay semantics.

## Proof B — escalation chaining to a real paid provider

A separate isolated fixture used a deterministic checkpoint adapter to produce the legitimate `escalate` decision required to make the scheduler's purpose-scoped escalation stage eligible. The escalation stage itself used the real configured paid provider route above.

Observed lifecycle:

1. immutable ready-for-checkpoint evidence existed;
2. one checkpoint reservation/call recorded `escalate`;
3. scheduler preview advanced to `paid_escalation`;
4. one real escalation provider call executed through `HermesPaidModelAdapter`;
5. the provider returned a schema-valid `reject` decision; and
6. exactly one completed escalation `model_call` and one immutable escalation evidence record existed.

Acceptance result:

`ESCALATION_OK {'decision': 'reject', 'completed_calls': 1}`

Again, approve/reject/escalate are all valid model outcomes; the acceptance target is correct purpose-scoped chaining and exactly-once paid execution.

## Proof C — real-provider unknown outcome never repeats

A third isolated checkpoint call exercised the dangerous ambiguity boundary with the real provider. The adapter runner allowed the real Hermes provider process to complete successfully, then deliberately discarded the returned result by raising a simulated transport-loss error before Local First could observe the response.

Observed lifecycle:

1. one real provider call executed;
2. Local First durably marked the reservation `unknown_outcome`;
3. no paid checkpoint evidence was fabricated from the lost result;
4. after scheduler restart, the same request remained fail-closed; and
5. the provider runner was not invoked a second time.

Acceptance result:

`UNKNOWN_OUTCOME_OK {'real_provider_calls': 1, 'restart_provider_calls': 0, 'unknown_outcome': 1}`

This proves the most important paid-side-effect invariant against a real provider boundary: an ambiguous completed call is never blindly repeated.

## Completion condition

The design-v2 paid operational completion condition is satisfied for the representative route:

- reservation occurs before paid execution;
- purpose is explicit and auditable;
- zero budget pauses without spending;
- one-call approval authorizes one call;
- completed-call replay does not spend twice;
- checkpoint-to-escalation chaining works;
- ambiguous real-provider completion becomes durable `unknown_outcome`; and
- restart performs no duplicate paid invocation.

Operator lifecycle UX and runtime metrics remain separate follow-on items.