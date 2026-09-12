# Failure-driven repair / triage acceptance

Date: 2026-09-11

## Acceptance target

Demonstrate in one scheduler-owned scenario that a deliberate deterministic failure:

1. stays on the same ticket for bounded repair;
2. preserves worktree/branch provenance;
3. feeds durable failure evidence into the repair attempt;
4. routes to triage when the configured/repeated-failure bound is reached;
5. survives a scheduler restart before triage;
6. invokes triage exactly once; and
7. materializes exactly one authoritative child and one generated-card outbox entry.

## Proof

`tests/test_invocation_lifecycle.py::InvocationLifecycleTests::test_failure_driven_repair_then_restart_triages_exactly_once_with_provenance`

The fixture uses a real temporary Git repository and Local First worktree/controller path. The implementation model deliberately writes a value that fails the configured deterministic verification command on attempt 1 and again on attempt 2.

Observed lifecycle:

`implementation(1) -> validation(1 fail) -> repair_routing(1 repair) -> implementation(2) -> validation(2 fail) -> repair_routing(2 triage) -> restart -> state_projection -> triage`

Assertions include:

- attempt 2 remains the same Local First ticket;
- attempt 2 reuses attempt 1's worktree and branch;
- the second implementation packet contains compact failure evidence from the prior failed validation;
- repeated stable failure fingerprinting drives the second routing decision to `triage`;
- only one `needs_triage` transition is recorded;
- no triage model call occurs before the restart;
- after restart, pending state projection is drained before triage, preserving scheduler ordering;
- exactly one triage invocation is journaled;
- exactly one child ticket is created;
- exactly one `create_microticket` projection row is queued for that child; and
- no second triage claim can be acquired after completion.

## Scope

This is a representative deterministic failure-path acceptance proof. It intentionally does not require a flaky external model or uncontrolled provider failure: the failure is induced at the trusted deterministic validation boundary, so the routing and provenance behavior is reproducible and auditable.

External-provider crash ambiguity remains covered separately by the model invocation and external-boundary crash hardening rules. Paid-provider behavior remains a separate acceptance tranche.
