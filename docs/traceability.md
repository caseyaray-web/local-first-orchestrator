# Local-first orchestrator requirements traceability

**Audit basis:** `docs/design-v2.md`, `docs/scheduler-plan.md`, `docs/milestone-20-real-acceptance.md`, `docs/hermes-dispatch-execution-acceptance.md`, and `docs/multi-ticket-native-tranche-acceptance.md`.

**Current re-audit: 2026-09-11.** This file supersedes the 2026-09-08 pre-scheduler snapshot. The scheduler/daemon, representative real Local First-owned acceptance path, representative Hermes-dispatch reconciliation path, full scheduler-generated multi-ticket native tranche graph proof, deliberate failure-driven repair/triage proof, and live paid-provider checkpoint/escalation proof are complete. Remaining gaps are concentrated in broader live external-boundary crash coverage, operator lifecycle/recovery UX, and runtime metrics/adaptive sizing.

Status labels:

- **PASS** — implementation exists and the relevant completion condition has been demonstrated at the level required by the current design.
- **PARTIAL** — the mechanism exists, but a broader or explicitly required live proof/product surface remains open.
- **MISSING** — no meaningful implementation exists.

A fake/unit-only boundary is not promoted to live integration PASS unless the design item itself is implementation-only.

## Design-v2 implementation status

| Design item | Status | Current evidence | Remaining work |
|---|---|---|---|
| Scheduler / restartable `process-next` lifecycle | PASS | `ProcessNextScheduler`; canonical stage ordering; durable claims/reconciliation; scheduler crash/concurrency suites; `docs/scheduler-plan.md` milestones 1–18 | Broader live crash matrix is tracked separately below |
| Daemon wrapper | PASS | `SchedulerDaemon`; shared registered scheduler factory; health/status; pause/restart tests; scheduler milestone 19 | Operator lifecycle polish only |
| Real low-risk Local First-owned E2E | PASS | Real Hermes task through implementation, validation, fresh review, acceptance, Git integration, completion projection, restart reconciliation; `docs/milestone-20-real-acceptance.md` | No broader-path claim implied |
| Hermes dispatcher-owned execution reconciliation | PASS | Generated-card handoff marker, Hermes run polling/reconciliation, source-aware validation/review/Git/checkpoint, real dispatcher-owned worker proof; `docs/hermes-dispatch-execution-acceptance.md` | Exhaustive crash/failure variants remain under crash matrix |
| Multi-ticket native tranche graph / successor activation | PASS | Real generated T1 dependency chain, Hermes-native readiness, two dispatcher workers, rolling integration head, checkpoint, successor re-plan/materialize/project/activate; `docs/multi-ticket-native-tranche-acceptance.md` | No gap for representative acceptance scope |
| Live Hermes board integration | PARTIAL | Real task reads, card projection, state/comment delivery, marker lookup, terminal completion, native links/readiness, generated multi-ticket graph | Exhaustive live external-boundary crash/restart proof |
| Real local implementation model | PASS | Milestone 20 configured local implementation route; one bounded real implementation with durable invocation/diff/evidence | Deliberate real repair-failure proof is separate |
| Fresh independent review model | PASS | Separate review profile/process, schema-valid closed output, durable review evidence, live acceptance path | Deliberate failure/review-repair path remains separate |
| Crash/restart exactly-once behavior | PARTIAL | Full deterministic scheduler crash matrix; production `HermesBoardAdapter` ambiguity proofs for state, comment, and native dependency effects; generated-create, model, Git, checkpoint/materialization replay; live paid-provider completed-response replay and unknown-outcome no-repeat; see `docs/crash-matrix-hardening.md` and `docs/live-paid-provider-acceptance.md` | Repeat controlled termination against remaining configured live Hermes/model/Git/checkpoint boundaries where operationally safe |
| Evidence-comment reconciliation | PASS | Dedicated comment outbox + marker lookup; live post-Hermes/pre-ledger crash recovered without duplicate comment | Keep compatible with Hermes read contract changes |
| Paid checkpoint / escalation mechanism | PASS | Governor, reservation-before-call, purpose-scoped claims, one-call approval, production adapter, scheduler chaining, real checkpoint and escalation calls, completed-call replay, and real-provider unknown-outcome no-repeat; see `docs/live-paid-provider-acceptance.md` | None for representative configured route |
| Operator lifecycle / recovery UX | PARTIAL | `status --scheduler-detail`, `process-next`, `daemon`, inspect/reconciliation commands, `approve-paid`, dashboard pause/resume, registered runtime | Coherent documented init/pause/resume/retry/reject/reconcile/daemon/metrics surface |
| Runtime metrics / adaptive sizing | PARTIAL | Context/token bounds and some metrics infrastructure | Persist real-run outcome/cost/runtime measurements and feed sizing policy |
| Tranche integration / checkpoint | PASS | Immutable tranche evidence, deterministic integration commands, real multi-ticket checkpoint/successor activation, and separate real paid checkpoint operational proof | None for current design scope |
| Same-ticket repair | PASS | Bounded retry/fingerprint/attempt machinery, restart safety, and deliberate deterministic failure acceptance proof preserving ticket/worktree/branch provenance; see `docs/failure-driven-repair-triage-acceptance.md` | None for current design scope |
| Bounded triage/decomposition | PASS | Child-count/depth/scope/criterion/duplicate enforcement; scheduler-owned triage and replay; generated child projection; deliberate repeated-failure acceptance proof invokes triage exactly once after restart | None for current design scope |
| Deterministic validation / allowlists | PASS | Trusted verification commands, allowlist enforcement, durable stage evidence, live validation proof | None for current design scope |
| Context packet / token budget | PASS | Bounded packet construction and explicit budgeting | Runtime sizing feedback is tracked separately |
| Architecture packets / snapshot-bound activation | PASS | Feature/tranche/microticket contracts, snapshot binding/revalidation, successor re-snapshot at accepted integration SHA | None for current design scope |
| Paid-call reservation / budget governor | PASS | Atomic purpose-bound reservation and budget enforcement with replay-safe request keys; zero-budget live checkpoint stopped before provider execution and one-call approval authorized exactly one real call | None for current design scope |
| Ledger / state / provenance / audit | PASS | Attempts, bindings, invocations, artifacts, review/acceptance/Git evidence, outboxes, scheduler claims, reconciliation | None for current design scope |
| Git worktree isolation / reconciliation | PASS | Exact repository/base/worktree/diff checks, accepted-candidate freeze, commit intent/evidence, restart recovery | Exhaustive live crash matrix tracked separately |
| Board projection outbox / retry-safe effects | PASS | State/comment/generated-card outboxes, leases, idempotency/supersession, crash/retry coverage, live comment reconciliation | Exhaustive real-boundary matrix tracked separately |
| Discovery / compatibility | PASS | `docs/compatibility.md`; plugin architecture wraps Hermes-native surfaces | Keep current as Hermes evolves |

## Original phase grouping

| Phase | Current status | Notes |
|---|---|---|
| Phase 0 — discovery / compatibility | PASS | Discovery and compatibility work complete |
| Phase 1 — ledger / deterministic controller | PASS for scheduler-owned lifecycle | Operator/product integration remains partial |
| Phase 2 — local implementation / validation | PASS | Representative bounded live execution demonstrated |
| Phase 3 — review / same-ticket repair | PASS | Independent live review plus deliberate deterministic failure-driven same-ticket repair acceptance proof |
| Phase 4 — bounded triage | PASS | Bounded triage implementation plus repeated-failure → restart → exactly-once triage acceptance proof |
| Phase 5 — architecture / checkpoint / paid governor | PASS | Architecture, checkpoint, governor, real paid checkpoint, escalation chaining, replay, and unknown-outcome stop proof complete |
| Phase 6 — symbol/context / metrics | PARTIAL | Bounded context complete; production metrics/adaptive loop pending |
| Hermes-native v2 execution/projection additions | PARTIAL overall | Scheduler, daemon, representative Local First E2E, dispatcher reconciliation, and multi-ticket graph proof complete; exhaustive live crash + paid proof remain |

## Original acceptance criteria reconciliation

| Criterion | Status | Evidence / note |
|---|---|---|
| Low-risk end-to-end feature | PASS | Real Milestone 20 acceptance |
| Controller / Local First sole trust authority | PASS for accepted path | Hermes may execute implementation, but Local First retains validation/review/acceptance/Git/checkpoint/finality authority |
| Same-ticket deterministic repair | PASS | Deliberate validation failure stays on the same ticket/worktree/branch and carries compact failure evidence into attempt 2 |
| Configured attempt limit | PASS | Repair routing enforces bounded attempts and repeated-fingerprint policy |
| Repeated failure routes once to triage | PASS | Deliberate repeated validation failure routes once to `needs_triage`; restart runs one triage invocation and materializes one child |
| Triage count/depth bounds | PASS | Enforced by triage/decomposition contracts |
| Child unresolved-criterion mapping | PASS | Criterion linkage enforced and persisted |
| Out-of-scope review suggestions nonblocking | PASS | Review normalization/routing policy |
| Allowlist scope validation | PASS | Deterministic validator + accepted-candidate identity checks |
| Fresh implementation/review contexts | PASS | Real implementation and separate fresh review process demonstrated |
| Packet budget | PASS | Context builder and policy bounds |
| Audit all lifecycle stages | PASS for current scheduler scope | Durable claims, artifacts, invocation/effect/Git/paid evidence and operator observability |
| No duplicate crash work | PARTIAL | Deterministic crash matrix complete; exhaustive live external-boundary matrix pending |
| Paid reservation / purpose | PASS | Governor and scheduler claim/request-key binding |
| Budget exhaustion stops rather than overspends | PASS | Live paid checkpoint started at zero budget, made no provider call, then executed exactly one call after explicit one-call approval |
| Pause / active inspection | PASS for scheduler operation | Ledger/dashboard pause plus scheduler-detail status; broader UX polish pending |
| Ticket branch commit / no unsafe default merge | PASS | Isolated worktrees and exact accepted Git integration evidence |
| Compatibility replacement | PASS | Hermes-native surfaces wrapped rather than replaced |

## Scheduler milestone reconciliation

All twenty scheduler milestones in `docs/scheduler-plan.md` are complete for their stated representative acceptance scope:

1. foundation;
2. implementation;
3. deterministic validation;
4. fresh review;
5. repair routing;
6. bounded triage/decomposition;
7. candidate freeze;
8. Git integration;
9. completion projection;
10. native dependency release;
11. tranche checkpoint;
12. paid checkpoint/escalation mechanism;
13. next-tranche activation;
14. scheduler-wide reconciliation;
15. deterministic ordering;
16. concurrency proof;
17. deterministic crash matrix;
18. observability;
19. daemon wrapper;
20. real representative end-to-end acceptance.

The older traceability entries that marked restartable `process-next`, daemon mode, live board projection, configured real implementation/review, and runtime E2E as missing are therefore obsolete.

## Post-scheduler acceptance proofs

| Proof | Status | Evidence |
|---|---|---|
| Representative Local First-owned real scheduler path | PASS | `docs/milestone-20-real-acceptance.md` |
| Representative Hermes dispatcher-owned execution path | PASS | `docs/hermes-dispatch-execution-acceptance.md` |
| Full generated multi-ticket native tranche graph and successor activation | PASS | `docs/multi-ticket-native-tranche-acceptance.md` |
| Deliberate failure-driven same-ticket repair and exactly-once triage | PASS | `docs/failure-driven-repair-triage-acceptance.md` |
| Live paid checkpoint/escalation integration and unknown-outcome no-repeat | PASS | `docs/live-paid-provider-acceptance.md` |

## Remaining dependency-ordered work

1. **Broader live external-boundary crash proof.** Repeat the hardened production-adapter matrix with controlled termination against the remaining configured live Hermes/model/Git/checkpoint boundaries where operationally safe. The paid-provider ambiguity boundary is now live-proven.
2. **Operator lifecycle/recovery UX.** Consolidate and document initialization, inspection, pause/resume, retry/reject/reconcile, paid approval, daemon lifecycle, and diagnostics.
3. **Runtime metrics and adaptive sizing.** Persist real outcome/runtime/cost measurements and apply them to bounded ticket/context sizing policy.

This list is the current reconciliation target. It intentionally does not reopen scheduler milestones already completed and acceptance-proven.