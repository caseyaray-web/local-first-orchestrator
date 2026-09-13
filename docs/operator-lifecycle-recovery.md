# Operator lifecycle and recovery UX

Date: 2026-09-12

This is the supported operator workflow for a registered Local First runtime. The UX deliberately exposes a small lifecycle surface and keeps recovery actions stage-specific so ambiguous external effects cannot be retried blindly.

## Lifecycle surface

All examples assume the global runtime selectors precede the subcommand:

```text
local-first-orchestrator --database <ledger.db> [--operator-config-path <operator.json>] <command>
```

### 1. Initialize / register one runtime

Use `init` (alias of the older `register-dashboard`) to persist the authoritative ledger, canonical repository, repository allowlist, execution roots, local implementation/review routes, decomposition routes, and optional paid routes.

```text
... init --repository <repo> [route options]
```

Production scheduler execution consumes this persisted registration. Runtime path/model overrides are rejected rather than silently replacing it.

### Dashboard configuration and Hermes profiles

After the dashboard/plugin host reloads this version, the Local First plugin page includes an editable **Configuration** section. Model-using roles are selected by Hermes profile rather than by manually maintaining coupled provider/model strings.

The dashboard discovers profiles from Hermes, resolves each selected profile with `hermes profile show`, and persists the selected `profile` together with Hermes-reported `provider` and `model` provenance. Configuration writes require Local First to be paused and emit an `operator_configuration_updated` audit event.

Configurable role selectors are:

- local implementation;
- review;
- decomposition/local; and
- decomposition/standard (used by standard planning, bounded triage/snapshot revalidation, and successor planning where the scheduler requests the standard cost class);
- paid checkpoint; and
- paid escalation.

Implementation and review timeouts are editable on the same page. Repository, allowlist, worktree root, and artifact root remain visible but read-only in the browser because they define the trusted filesystem boundary; bootstrap/automation can still set them through `init`.

### 2. Inspect normal operation

`operator-status` returns the bounded operator read model plus scheduler lifecycle detail:

```text
... operator-status
```

It reports pause state, ready/running/triage/done counts, active ticket identities, projection backlog, recent failed-attempt reconciliation state, scheduler next stage, scheduler claims, and tick status without exposing unbounded artifacts/contracts.

For lower-level/raw reads, `status --scheduler-detail` and `inspect --task-id <id>` remain available.

### 3. Pause before manual recovery or maintenance

```text
... pause --reason "investigating review failure" --operator-id <operator>
```

Pause is durable, audited, and consumed by scheduler claim paths. Existing external effects are not undone. Recovery operations that require a stable control plane (for example failed-attempt reconciliation and review resume) already require the controller to be paused.

### 4. Diagnose recovery state

Use either name:

```text
... doctor
... recovery-status
```

The command is read-only. It reports bounded lists of:

- incomplete/ambiguous model invocations;
- review-infrastructure failures;
- terminal state-projection errors;
- terminal evidence-comment deliveries;
- retired-attempt cleanup prerequisites; and
- currently claimed scheduler stages.

It also emits `recommended_actions` containing the exact existing stage-specific command fragment where one is safe to recommend.

An incomplete model invocation intentionally does **not** receive a generic retry recommendation. Its next action is inspection because an external model may already have run; the invocation remains fail-closed until explicitly resolved by the applicable recovery procedure.

## Stage-specific recovery commands

There is intentionally no generic `retry` or generic `reject` mutation. Different failure classes have different external-side-effect and provenance requirements.

| Situation | Supported action |
|---|---|
| Failed implementation/runtime attempt with preserved forensic evidence | `reconcile-failed-attempt --task-id ... --classification ...` while paused |
| Retired attempt requires external cleanup | perform separately authorized cleanup, then `confirm-retired-attempt-cleanup --task-id ...` while paused |
| Review process infrastructure failed but validated candidate is unchanged | `resume-failed-review --task-id ...` while paused |
| Historical implementation requires integrity revalidation | `authorize-historical-revalidation`, then `revalidate-implementation` / `attest-historical-revalidation` as applicable |
| State projection intents are stale/missing | `reconcile-state-projections --task-id ...` (ledger-only) |
| Terminal generated-card create can safely be reopened | `reopen-terminal-generated-projection --task-id ... --event-id ...` |
| Dispatcher-owned Hermes run completed but Local First has not bound it | `reconcile-hermes-execution --task-id ... [--run-id ...]` |
| Paid budget intentionally needs one more call | `approve-paid --feature-id ... --purpose ... --reason ... --idempotency-key ...` |
| Candidate already reviewed/accepted and operator is performing a bounded manual continuation | use the explicit `apply-persisted-review`, `accept-reviewed-candidate`, or `integrate-accepted-candidate` command |

A domain `reject` outcome is produced by the relevant validation/review/checkpoint authority and persisted as evidence; the operator UX does not provide a broad button that rewrites a ticket to rejected without that evidence.

## Resume

After the recovery blocker is resolved and `doctor` is clean for the intended work:

```text
... resume --reason "review recovery authorized" --operator-id <operator>
```

Resume is durable and audited. It does not force a particular ticket or bypass scheduler ordering; it only re-enables normal claims.

## Bounded scheduler operation

### One tick

Dry-run preview remains the default:

```text
... process-next
```

Execution requires all explicit gates:

```text
... --hermes-executable hermes --board <board> process-next --execute --allow-board-writes
```

### Daemon

The daemon reuses the same one-tick scheduler primitive and registered runtime. It requires explicit execution and board-write authorization:

```text
... --hermes-executable hermes --board <board> daemon --execute --allow-board-writes
```

It supports bounded `--max-iterations` plus explicit idle/busy/error-backoff controls. On exit it prints daemon health and scheduler observability. `operator-status` can be used independently to inspect lifecycle state.

## Runtime metrics and adaptive sizing

`runtime-metrics` materializes any completed-ticket observations missed by a crash and prints the bounded runtime/outcome summary plus the current decomposition sizing recommendation:

```text
... runtime-metrics
```

The Hermes dashboard exposes the same summary and recommendation. Context-token values are deterministic ticket-contract estimates, not provider billing telemetry. Paid-call input/output tokens are shown where the provider persisted them; monetary cost is explicitly marked unavailable when no authoritative currency cost is supplied.

Adaptive sizing changes only decomposition planning hints after a minimum sample threshold. It does not relax patch budgets, deterministic validation, review, Git, checkpoint, or completion authority.

## Recovery principles

1. **Pause before invasive/manual recovery.** Commands whose invariants require pause enforce it in the controller/ledger, not merely in documentation.
2. **Inspect before retrying an ambiguous external effect.** Started-but-not-completed model invocations and unknown paid outcomes fail closed.
3. **Use the narrow command for the failure class.** Do not convert infrastructure failure into application rejection or generic retry.
4. **Resume does not override evidence.** It only permits scheduler claims again.
5. **Board writes remain explicitly gated.** Scheduler/daemon execution still requires both the Hermes board/executable and `--allow-board-writes`.
6. **All durable lifecycle mutations are auditable.** Pause/resume, recovery authorization, approvals, and reconciliations are ledger events/evidence.

## Acceptance

CLI acceptance tests cover durable/audited pause and resume, bounded operator status with scheduler detail, and recovery diagnosis of an intentionally incomplete model invocation with a fail-closed inspection recommendation. Existing tests continue to cover daemon execution gates, explicit board access, paid approval, and Hermes execution reconciliation.
