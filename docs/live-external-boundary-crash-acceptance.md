# Live external-boundary crash acceptance

Date: 2026-09-12

## Scope

This acceptance tranche closes the remaining representative crash/restart proof across the configured external-effect classes. It combines previously recorded live Hermes and paid-provider ambiguity runs with new controlled real local-model invocations and existing real Git/checkpoint side-effect crash tests.

This is a representative operational proof, not a claim that every possible Hermes mutation or infrastructure failure has been killed at every instruction boundary. The completion criterion is that every materially different external-effect class has at least one real or production-path ambiguity/replay proof and that unknown outcomes fail closed rather than duplicate work.

## 1. Live Hermes remote-effect ambiguity

Milestone 20 already exercised a real Hermes board comment delivery in which the remote comment existed after the process-loss boundary but the local outbox had not yet recorded delivery. Restart found the durable marker through Hermes and acknowledged the existing effect without posting a second comment.

Evidence: `docs/milestone-20-real-acceptance.md`.

The production adapter hardening tranche additionally covers state projection and native dependency-link ambiguity through `HermesBoardAdapter` command construction, exception translation, remote-state reread, and ledger acknowledgement paths.

Evidence: `docs/crash-matrix-hardening.md`.

## 2. Real local implementation model — ambiguous outcome stops

The configured implementation route (`worker-code-local`, `custom:lm-studio`, `qwen3.8-27b@iq3_s`) was invoked through the real `LocalQwenAdapter`/Hermes subprocess path in an isolated temporary repository and ledger.

The runner allowed the real implementation subprocess to return successfully and then deliberately terminated controller control flow before Local First could journal invocation completion.

Observed result:

`LIVE_IMPL_UNKNOWN_OK {'real_calls': 1, 'restart_calls': 0, 'status': 'started'}`

On restart, the durable invocation remained `started`; Local First treated the outcome as ambiguous and refused a second model invocation. This proves fail-closed behavior at the real configured local-model boundary.

## 3. Real local implementation model — durable completion replays

A second isolated run injected controller death at `implementation_invocation_completed`, after the real model response and durable invocation completion record but before implementation-stage persistence.

Observed result:

`LIVE_IMPL_REPLAY_OK {'real_calls': 1, 'restart_calls': 0, 'status': 'completed'}`

Restart recovered the completed invocation and finalized implementation evidence without calling the model again.

## 4. Real fresh review — durable completion replays

The same configured local adapter's fresh-review worker was exercised as a separate structured subprocess. Controller death was injected at `review_invocation_completed`, after the durable review invocation result but before review-stage persistence.

Observed result:

`LIVE_REVIEW_REPLAY_OK {'real_calls': 1, 'restart_calls': 0, 'status': 'completed'}`

Restart rebuilt the review stage from the durable completed invocation and made no second review-process call.

## 5. Real Git mutation recovery

The repository suite already exercises Git crash boundaries against actual temporary Git repositories/worktrees rather than a fake Git adapter:

- commit exists after `git commit` but before Local First ledger completion: restart recovers exactly that one commit and does not create a second child commit;
- tranche integration ref has advanced before Local First completes the stage: restart reconciles the exact CAS result rather than advancing again.

Focused validation:

`4 passed` across the two Git and two checkpoint restart tests listed below.

Relevant tests:

- `InvocationLifecycleTests::test_scheduler_git_integration_recovers_commit_created_before_ledger_update_without_second_commit`
- `InvocationLifecycleTests::test_scheduler_git_integration_recovers_after_tranche_head_advanced`

## 6. Real checkpoint artifact / integration-command recovery

Tranche checkpoint tests execute real integration commands and write the real checkpoint artifact in a temporary repository/artifact tree.

Two crash boundaries are covered:

- scheduler side effect is durably applied but not finalized: restart finalizes without rerunning integration;
- checkpoint artifact exists after integration commands but before ledger apply: restart consumes the existing artifact and does not execute the integration command again.

Relevant tests:

- `SchedulerTrancheCheckpointTests::test_completed_effect_restarts_without_rerunning_integration`
- `SchedulerTrancheCheckpointTests::test_artifact_written_before_ledger_apply_replays_without_rerunning_commands`

## 7. Real paid-provider ambiguity

The live paid-provider acceptance separately proved both completed-response replay and a real provider call whose successful response was deliberately discarded. The latter became durable `unknown_outcome`; restart made zero additional provider calls.

Evidence: `docs/live-paid-provider-acceptance.md`.

## Completion assessment

Representative external-effect classes now have operational restart evidence:

| Effect class | Representative proof | Restart behavior |
|---|---|---|
| Hermes remote write | real ambiguous comment + production-adapter state/dependency ambiguity | reread/reconcile; no duplicate write |
| local implementation model | real configured model, ambiguous post-return loss | stop; zero reinvocation |
| local implementation model | real configured model, durable completion before stage apply | replay durable result; zero reinvocation |
| independent review process | real configured fresh-review subprocess, durable completion before stage apply | replay durable result; zero reinvocation |
| Git commit/ref mutation | actual temporary Git repo/worktree | exact commit/CAS reconciliation; no duplicate commit/ref advance |
| tranche checkpoint/integration command | actual subprocess command + artifact | reuse durable effect/artifact; no rerun |
| paid provider | real configured provider | completed response replay or durable unknown-outcome stop; zero duplicate spend |

The broader crash-hardening roadmap item is therefore complete for the representative configured system. Future adapter/provider changes should add equivalent boundary probes as regression acceptance, but they are maintenance work rather than an open v2 implementation dependency.

The remaining v2/productization work is operator lifecycle/recovery UX and runtime metrics/adaptive sizing.