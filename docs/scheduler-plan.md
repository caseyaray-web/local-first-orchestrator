# Local First scheduler completion plan

This document expands the scheduler item in `docs/design-v2.md` into a concrete implementation roadmap. It is ordered to preserve the project’s durability, idempotency, Hermes-ownership, and fail-closed recovery requirements.

The scheduler should be considered architecturally complete only after the lifecycle slices through scheduler-wide crash/recovery proof are complete. The daemon comes afterward and should remain a thin wrapper around a scheduler that is already safe when called one tick at a time.

## Current position

Already complete or substantially complete:

1. **Scheduler foundation — Complete**
   - bounded `process-next`
   - global tick lease and `busy` handling
   - pause handling
   - truthful dry-run preview
   - board/outbox stage ordering
   - dependency-readiness stage
   - scheduler claim/reclaim/finalization lifecycle
   - crash-safe started/completed/finalized markers

2. **Implementation stage — Complete for the current scheduler slice**
   - claims `ready_local` work
   - persists model invocation intent before inference
   - binds results to exact ticket, attempt, artifact, worktree, and diff
   - refuses to reinvoke unknown or terminal-failed invocations
   - recovers a completed invocation whose model-stage artifact persistence was interrupted
   - replays a persisted model stage without model reinvocation
   - stops before deterministic validation

The remaining work should proceed in the following order.

## 3. Deterministic validation stage

Make validation a first-class `process-next` stage rather than a helper hidden inside implementation.

Required slices:

- Add validation eligibility and dry-run preview for implementation candidates with a durable model-stage artifact and no completed validation stage.
- Add a durable validation claim bound to the exact ticket, attempt, implementation artifact, worktree, base SHA, and implementation diff hash.
- Freeze and re-check candidate identity before running validation so repository/worktree drift fails closed.
- Extract a validation-only controller operation that performs only deterministic checks and configured verification commands.
- Persist validation output as a durable stage artifact bound to the exact implementation candidate and validation policy.
- Apply the scheduler started/completed/finalized lifecycle to validation.
- Define restart behavior for: not started, command started/outcome unknown, validation completed/artifact persistence interrupted, validation artifact persisted/scheduler completion interrupted, and scheduler completion/finalization interrupted.
- Route outcomes without crossing into review in the same tick: pass becomes review-eligible; deterministic failure becomes repair-routing eligible; reconciliation-required conditions stop fail-closed.

Completion condition: a persisted implementation candidate can be picked up by a later `process-next`, validated against its exact attempt/diff/worktree and trusted verification policy, survive crash/restart without duplicate or ambiguous evidence, and end in a durable state from which review or repair can proceed.

## 4. Fresh review stage

Make fresh review independently claimable and restart-safe.

Required slices:

- Claim only candidates with completed deterministic validation.
- Build a fresh review context independent of the implementation conversation/tool loop.
- Bind review input to the exact validated diff and validation evidence.
- Invoke the configured review model through the existing strict review contract.
- Persist closed-schema review output before scheduler finalization.
- Apply started/completed/finalized stage semantics and replay completed review artifacts without reinvocation.
- Refuse unknown review invocations until reconciliation resolves them.
- Do not invoke repair in the same scheduler tick.

Completion condition: a validated candidate receives one fresh independent review, whose exact structured result is durably bound to that candidate and can be recovered without duplicate review calls.

## 5. Repair-routing stage

Turn deterministic validation failures and blocking review findings into bounded same-ticket lifecycle decisions.

Required slices:

- Normalize the failure source into durable repair-routing input.
- Apply failure fingerprints and attempt counters.
- Route eligible failures back to the same ticket’s implementation lifecycle.
- Enforce `max_attempts` and repeated-fingerprint limits.
- Refuse automatic repair for policy-invalid or reconciliation-required conditions.
- Route exhausted/repeated failure to triage exactly once.
- Persist the routing decision before exposing new work.

Completion condition: failures cannot create uncontrolled retry loops, and every retry/triage decision is durable, bounded, and restart-safe.

## 6. Bounded triage/decomposition stage

Bring the existing triage policy machinery under scheduler control.

Required slices:

- Claim only tickets whose repair policy requires triage.
- Run bounded decomposition using the existing child-count, depth, criterion-mapping, and duplicate rules.
- Persist the decomposition proposal before projecting child work.
- Project generated children through existing retry-safe board mechanisms.
- Reconcile restart after child creation/projection without generating duplicates.
- Preserve parent state and criterion linkage exactly.

Completion condition: triage creates at most the permitted bounded child set, exactly once, with every child mapped to unresolved acceptance criteria and recoverable projection state.

## 7. Acceptance / candidate-freeze stage

Persist the final accepted candidate identity after review passes.

Required slices:

- Bind acceptance to the exact ticket, attempt, implementation diff, validation artifact, and review artifact.
- Re-check candidate/worktree identity before acceptance.
- Persist accepted evidence atomically with the acceptance state transition.
- Invalidate or stop if repository/worktree drift means the reviewed candidate is no longer the candidate being accepted.
- Make acceptance independently replayable without rerunning review.

Completion condition: there is one durable, immutable accepted-candidate identity that later Git integration can trust.

## 8. Git integration / commit stage

Move accepted work into the durable Git-integration lifecycle exactly once.

Required slices:

- Claim only accepted candidates.
- Verify canonical repository base/provenance before integration.
- Re-check the accepted diff/worktree identity.
- Create the commit using repository conventions without bypassing existing Git safeguards.
- Persist commit identity and provenance before external completion projection.
- Reconcile the crash states: commit not started; commit created but ledger not updated; ledger updated but later projection incomplete.
- Never create a second commit for the same accepted candidate on replay.

Completion condition: an accepted candidate produces exactly one durable commit identity, or stops for reconciliation when exact recovery cannot be proven.

## 9. Completion / Hermes projection stage

Project locally accepted and committed work back to Hermes without repeating prior side effects.

Required slices:

- Require the exact accepted evidence and commit identity before completion.
- Enqueue completion/evidence effects through the existing outbox mechanisms.
- Retry board projection without repeating model, validation, review, or Git work.
- Persist final local completion only according to the ownership semantics in `docs/design-v2.md`.
- Keep evidence delivery idempotent to the extent supported by the current Hermes read/write contract.

Completion condition: board-write failures are independently retryable and can never force a repeat of already-successful implementation, validation, review, or Git work.

## 10. Native dependency release stage

Complete the Hermes-native dependency/readiness handoff required by v2.

Required slices:

- Verify the projected Hermes dependency graph matches the Local First architecture graph for active work.
- After completion, allow Hermes-native semantics to expose newly unblocked work rather than creating a competing private readiness model.
- Reconcile Local First’s ledger projection with Hermes after restart.
- Detect graph divergence and stop instead of silently advancing inconsistent dependencies.

Completion condition: task completion releases dependent work through Hermes-native dependency semantics with graph equivalence verified and no second scheduler truth source.

## 11. Tranche integration and checkpoint stage

Bring tranche boundaries into the same one-stage-per-tick scheduler model.

Required slices:

- Detect when all required work in a tranche has reached the required terminal state.
- Revalidate the canonical repository against the architecture/tranche snapshot.
- Run tranche-level deterministic integration verification.
- Persist checkpoint packets and decisions as durable stage artifacts.
- Fail closed on repository drift or missing evidence.
- Do not activate the next tranche in the same durable stage unless the design explicitly allows the transition to be atomic.

Completion condition: a tranche can finish, integrate, checkpoint, and become eligible for next-tranche activation without bypassing repository-snapshot or evidence requirements.

## 12. Paid checkpoint / escalation stage

Integrate paid-model work under the existing usage governor.

Required slices:

- Reserve paid budget atomically before invocation.
- Bind every reservation to purpose, feature/tranche/ticket, model/provider, and scheduler stage.
- Persist invocation intent before the paid side effect.
- Persist result/failure provenance and reconcile unknown invocations without double-spending.
- Pause or block when budget is exhausted rather than overspending.
- Support explicit approval/rejection where required by the design.

Completion condition: restart or retry can never duplicate a paid call or spend outside the governor’s durable authorization.

## 13. Next-tranche activation stage

Make activation a durable graph-projection operation.

Required slices:

- Require the prior tranche’s checkpoint/approval evidence.
- Bind activation to the current canonical repository snapshot.
- Materialize the next tranche’s exact Hermes-native parent/dependency graph.
- Verify graph equivalence after projection.
- Recover from partial projection without duplicate cards or links.

Completion condition: the next tranche is activated exactly once against the expected repository state and exact dependency graph.

## 14. Scheduler-wide reconciliation model

Unify recovery semantics across all scheduler stages without creating a second state machine.

Every stage should be classifiable as one of:

1. not started;
2. started / external outcome unknown;
3. external effect completed / local stage record incomplete;
4. local stage completion recorded / downstream projection incomplete;
5. fully finalized.

Required work:

- Define the authoritative durable evidence for each stage and each state above.
- Reuse existing invocation, outbox, Git, and artifact records rather than inventing parallel truth.
- Make `process-next` choose resume, replay, reconcile, retry, or stop from persisted evidence alone.
- Ensure terminal external failures do not silently become automatic retries.

Completion condition: after process death at any supported boundary, the next scheduler invocation can deterministically decide the safe next action from durable state.

## 15. Scheduler-wide ordering policy

Define and test one canonical ordering when multiple classes of work are eligible.

The final order should explicitly cover at least:

- pending generated-card projections;
- pending state projections;
- pending evidence comments;
- incomplete/recoverable lifecycle claims;
- implementation;
- validation;
- review;
- repair routing;
- triage;
- acceptance;
- Git integration;
- completion projection;
- dependency release;
- tranche/checkpoint work;
- paid work;
- next-tranche activation;
- new dependency-readiness admission.

The exact order may be refined as implementation progresses, but it must be deterministic and justified by durability/ownership dependencies.

Completion condition: two schedulers presented with the same durable state choose the same next eligible stage, and pending recovery/external effects are not starved by newly admitted work.

## 16. Scheduler-wide concurrency proof

Exercise competing scheduler processes and leases across the complete lifecycle.

Required cases:

- overlapping global ticks;
- expired tick leases;
- duplicate stage claims;
- competing workers for the same ticket;
- competing stages for the same ticket;
- cross-ticket deterministic selection;
- outbox retry overlap;
- model invocation overlap prevention;
- Git commit race prevention;
- paid reservation race prevention.

Completion condition: concurrent scheduler processes cannot produce duplicate stage effects or violate ticket lifecycle ordering.

## 17. Scheduler-wide crash matrix

Inject process death at every durable boundary for every scheduler-owned stage.

The matrix must prove no duplicate:

- implementation model call;
- validation evidence;
- review model call;
- repair/triage routing;
- generated child work;
- accepted candidate;
- Git commit;
- board effect;
- dependency/tranche activation;
- paid call/reservation.

Completion condition: every injected crash either resumes safely, replays an already-durable result, or stops with an explicit reconciliation requirement. No boundary may produce an ambiguous automatic duplicate side effect.

## 18. Scheduler observability

Expose enough durable state for an operator to understand exactly where orchestration is stopped.

Required data should include:

- current/next scheduler stage;
- current claim and ticket;
- lease owner and expiry;
- attempt number where relevant;
- latest durable stage artifact;
- reconciliation-required reason;
- pending board/outbox effects;
- model invocation state;
- validation/review result identity;
- Git commit identity when present;
- paid reservation state when present.

Completion condition: an operator can inspect the system and identify the exact durable lifecycle boundary without reconstructing state from logs.

## 19. Daemon wrapper

Build the daemon only after one-shot `process-next` is safe across the full lifecycle.

Required slices:

- repeatedly invoke the one-tick scheduler primitive;
- backoff/sleep when there is no work or transient external failure;
- honor pause state without terminating in-flight durable work incorrectly;
- graceful shutdown;
- health/status reporting;
- restart on process failure without daemon-specific recovery logic.

The daemon should not own lifecycle semantics that are absent from `process-next`.

Completion condition: killing and restarting the daemon is operationally equivalent to stopping and later calling the already-safe one-tick scheduler again.

## 20. Real end-to-end acceptance

Prove the finished scheduler against real configured integrations.

Required proof:

- actual Hermes board/task/dependency behavior;
- actual configured local implementation model;
- actual independent review model;
- real isolated Git worktree and commit path;
- deterministic validation;
- crash/restart at representative high-risk boundaries;
- completion of a representative low-risk feature;
- no unnecessary paid post-architecture calls;
- auditable evidence for every durable stage.

Completion condition: one representative low-risk feature proceeds from eligible Hermes work through implementation, validation, review, accepted commit, completion projection, and dependency advancement under the v2 ownership model, including restart proof, without hidden manual lifecycle substitutions.

## Scheduler milestone sequence

The intended implementation sequence is:

`foundation → implementation → validation → review → repair routing → triage → acceptance → Git integration → completion projection → dependency release → tranche/checkpoint → paid stages → next-tranche activation → reconciliation hardening → deterministic ordering → concurrency proof → crash matrix → observability → daemon → real E2E acceptance`

The current implementation has completed the first two scheduler milestones. The next milestone is **deterministic validation**.
