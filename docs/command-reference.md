# CLI command reference

The standalone command and the Hermes-native command use the same parser and handlers:

```text
local-first-orchestrator ...
hermes local-first-orchestrator ...
```

All commands require a Local First ledger path:

```text
--database <ledger.db>
```

Unless noted otherwise, examples below use the Hermes-native form.

## Global options

| Option | Meaning |
|---|---|
| `--database <path>` | Required Local First ledger; never Hermes `kanban.db` |
| `--repository <path>` | Canonical repository for bootstrap/ad-hoc operations; default `.` |
| `--allow-repository <path>` | Exact allowed Git root; repeatable |
| `--worktree-root <path>` | Bootstrap/ad-hoc external worktree root |
| `--artifact-root <path>` | Bootstrap/ad-hoc artifact root |
| `--implementation-timeout-seconds <n>` | Bootstrap/ad-hoc implementation timeout |
| `--review-timeout-seconds <n>` | Bootstrap/ad-hoc review timeout |
| `--operator-config-path <path>` | Explicit persisted production registration |
| `--ad-hoc-runtime` | Explicit development-only runtime composition |
| `--hermes-executable <path/name>` | Hermes executable for board/dispatcher access |
| `--board <name>` | Hermes board for explicit board access |

See `docs/configuration.md` for semantics and trust boundaries.

## Schema and registration

### `migrate`

Create or update the Local First ledger schema.

```bash
hermes local-first-orchestrator --database <ledger> migrate
```

### `init`

Alias: `register-dashboard`.

Persist the production operator registration.

```bash
hermes local-first-orchestrator \
  --database <ledger> \
  --repository <repo> \
  --allow-repository <repo> \
  init [routing options]
```

Options:

```text
--config-path
--implementation-profile
--implementation-provider
--implementation-model
--review-profile
--review-provider
--review-model
--decomposition-local-profile
--decomposition-local-provider
--decomposition-local-model
--decomposition-standard-profile
--decomposition-standard-provider
--decomposition-standard-model
--paid-checkpoint-profile
--paid-checkpoint-provider
--paid-checkpoint-model
--paid-escalation-profile
--paid-escalation-provider
--paid-escalation-model
```

Model triples must be complete when optional routes are supplied.

## Status and observability

### `operator-status`

Return the bounded operator lifecycle read model plus scheduler detail.

```bash
hermes local-first-orchestrator --database <ledger> operator-status [--limit 25]
```

### `status`

Low-level ledger status.

Options:

```text
--active
--scheduler-detail
--show-cleanup-prerequisites
```

Example:

```bash
hermes local-first-orchestrator --database <ledger> \
  status --active --scheduler-detail
```

### `runtime-metrics`

Materialize any missing completed-ticket observations and print aggregate metrics plus adaptive decomposition sizing.

```bash
hermes local-first-orchestrator --database <ledger> \
  runtime-metrics [--limit 500]
```

### `inspect`

Inspect one Local First ticket.

```bash
hermes local-first-orchestrator --database <ledger> \
  inspect --task-id <ticket-id>
```

### `doctor`

Alias: `recovery-status`.

Read-only recovery diagnosis with bounded exact next-action hints where safe.

```bash
hermes local-first-orchestrator --database <ledger> \
  doctor [--limit 25]
```

## Lifecycle controls

### `pause`

Durably pause new scheduler work for maintenance/recovery/configuration changes.

```bash
hermes local-first-orchestrator --database <ledger> \
  pause --reason <reason> [--operator-id <id>]
```

### `resume`

Durably resume normal scheduler claims.

```bash
hermes local-first-orchestrator --database <ledger> \
  resume --reason <reason> [--operator-id <id>]
```

## Scheduler execution

### `process-next`

Run at most one durable scheduler stage.

Preview is the default:

```bash
hermes local-first-orchestrator --database <ledger> process-next
```

Execute:

```bash
hermes local-first-orchestrator \
  --database <ledger> \
  --hermes-executable hermes \
  --board <board> \
  process-next --execute --allow-board-writes
```

Additional options:

```text
--worker-id <id>
--planner-executable <executable>
```

Production `process-next` requires the registered runtime and rejects `--ad-hoc-runtime`.

### `daemon`

Repeatedly invoke the proven one-tick scheduler primitive.

```bash
hermes local-first-orchestrator \
  --database <ledger> \
  --hermes-executable hermes \
  --board <board> \
  daemon --execute --allow-board-writes
```

Options:

```text
--worker-id
--planner-executable
--idle-sleep-seconds
--busy-sleep-seconds
--error-backoff-seconds
--max-error-backoff-seconds
--max-iterations
```

The daemon requires both `--execute` and `--allow-board-writes`.

### `run-once`

Run the older one-ticket controller path.

```bash
hermes local-first-orchestrator --database <ledger> \
  run-once --task-id <task-id>
```

Dry-run is the effective default. Write-enabled execution requires:

```text
--execute --allow-board-writes
```

## Feature admission and planning

### `admit-feature-contract`

Persist one feature contract without planning or activation.

```bash
hermes local-first-orchestrator --database <ledger> \
  admit-feature-contract --spec-file <feature-spec.json>
```

Requires registered runtime.

### `plan-feature`

Generate and persist one validated decomposition plan without activation.

```bash
hermes local-first-orchestrator --database <ledger> \
  plan-feature \
  --feature-id <feature-id> \
  [--planner-executable hermes] \
  [--planner-cost-class local|standard]
```

`--planner-cost-class` selects between configured decomposition routes; it never selects implementation routing.

### `revalidate-feature-snapshot`

Append deterministic repository-snapshot revalidation for a feature.

```bash
hermes local-first-orchestrator --database <ledger> \
  revalidate-feature-snapshot \
  --feature-id <feature-id> \
  [--planner-executable hermes]
```

Uses the configured standard decomposition route.

## Implementation, validation, review, and integration controls

These commands are intentionally narrow. They are operator/recovery surfaces, not shortcuts around the normal scheduler.

### `implementation-only`

Alias: `implement-only`.

Run implementation and deterministic validation only; never review or accept.

```bash
hermes local-first-orchestrator --database <ledger> \
  implementation-only --task-id <ticket-id>
```

### `authorize-historical-revalidation`

Authorize one exact historical implementation for a future integrity gate.

```bash
hermes local-first-orchestrator --database <ledger> \
  authorize-historical-revalidation \
  --task-id <ticket-id> \
  --attempt-number <n> \
  [--operator-id <id>] \
  [--reason <reason>]
```

This command does not itself revalidate.

### `revalidate-implementation`

Revalidate an existing historical implementation; never rerun implementation or review.

```bash
hermes local-first-orchestrator --database <ledger> \
  revalidate-implementation \
  --task-id <ticket-id> \
  --attempt-number <n> \
  [--operator-id <id>]
```

### `attest-historical-revalidation`

Attest one preserved historical implementation after the required integrity path.

```bash
hermes local-first-orchestrator --database <ledger> \
  attest-historical-revalidation \
  --task-id <ticket-id> \
  --attempt-number <n> \
  [--operator-id <id>]
```

### `apply-persisted-review`

Apply one already-persisted review result without verdict disposition.

```bash
hermes local-first-orchestrator --database <ledger> \
  apply-persisted-review \
  --task-id <ticket-id> \
  --attempt-number <n>
```

### `accept-reviewed-candidate`

Accept one reviewed candidate without advancing integration.

```bash
hermes local-first-orchestrator --database <ledger> \
  accept-reviewed-candidate \
  --task-id <ticket-id> \
  --attempt-number <n>
```

### `integrate-accepted-candidate`

Fast-forward one accepted candidate into tranche integration.

```bash
hermes local-first-orchestrator --database <ledger> \
  integrate-accepted-candidate \
  --task-id <ticket-id> \
  --attempt-number <n>
```

## Generated-ticket and Hermes projection controls

### `project-generated`

Deliver one generated-card projection to Hermes.

Requires explicit board access and write permission:

```bash
hermes local-first-orchestrator \
  --database <ledger> \
  --hermes-executable hermes \
  --board <board> \
  project-generated --allow-board-writes
```

### `activate-generated`

Activate one generated ticket locally after the required identity/readiness conditions.

```bash
hermes local-first-orchestrator \
  --database <ledger> \
  --repository <repo> \
  activate-generated <ticket-id>
```

### `reconcile-state-projections`

Ledger-only state-projection reconciliation.

```bash
hermes local-first-orchestrator --database <ledger> \
  reconcile-state-projections --task-id <ticket-id>
```

It supersedes stale local intents and ensures the current durable state intent; it does not blindly issue remote writes.

### `reopen-terminal-generated-projection`

Reopen one deterministic terminal generated-card projection only when no external create occurred and the current durable payload revalidates.

```bash
hermes local-first-orchestrator --database <ledger> \
  reopen-terminal-generated-projection \
  --task-id <ticket-id> \
  --event-id <event-id>
```

It never retries an ambiguous external create.

### `recover-generated-projection`

While the controller is paused, read-verify and supersede one acknowledged pre-native generated card that reached Hermes `done` before reconciliation, or remains inertly `blocked` without worker execution. This records immutable recovery evidence and a fresh `board-create:v2` outbox intent; it does not write to Hermes, create an attempt, accept work, or change ticket state.

```bash
hermes local-first-orchestrator \
  --database <ledger> \
  --hermes-executable hermes \
  --board <board> \
  recover-generated-projection \
  --task-id <local-first-ticket-id> \
  --event-id <acknowledged-create-event-id> \
  --operator-id <operator> \
  --reason <reason>
```

Deliver the replacement separately with `project-generated --allow-board-writes`. Exact replay is idempotent; changed snapshot, status, external identity, operator, or reason fails closed. Existing attempts, model/review/acceptance/integration authority, active scheduler claims, pending board effects, leases, or multiple current create identities also fail closed.

### `prepare-native-release-activation` / `activate-native-release`

While paused, activation uses a two-step external Ed25519 approval boundary. Preparation is read-only and emits canonical bytes binding the exact signed revalidation, scheduled pre-snapshot, board path/device/inode, routing, and scheduled-to-ready transition. Sign those bytes outside the plugin. The activation command requires the 64-byte detached signature and never reads or creates a private key.

```bash
hermes local-first-orchestrator --database <ledger> --hermes-executable <absolute-hermes> --board <board> \
  prepare-native-release-activation --task-id <local-first-ticket> --revalidation-id <signed-revalidation-id> \
  --operator-id <operator> --reason <reason> --request-key <unique-request-key> --output-file approval.json
# sign approval.json externally, producing signature.bin
hermes local-first-orchestrator --database <ledger> --hermes-executable <absolute-hermes> --board <board> \
  activate-native-release --task-id <local-first-ticket> --revalidation-id <signed-revalidation-id> \
  --operator-id <operator> --reason <reason> --request-key <unique-request-key> \
  --approval-file approval.json --signature-file signature.bin --allow-board-writes
```

The command requires the registered runtime, explicit board-write authorization, a fresh signer configuration, the exact scheduled board snapshot, exact board inode, matching routing, and one signed evidence/event acknowledgement. Replays classify exact scheduled pre-state before effect, or exact ready post-state plus the exact marker after effect; a marker alone is never authority.

### `reconcile-hermes-execution`

Bind a completed dispatcher-owned Hermes worker run into a Local First attempt without launching implementation.

```bash
hermes local-first-orchestrator \
  --database <ledger> \
  --hermes-executable hermes \
  --board <board> \
  reconcile-hermes-execution \
  --task-id <hermes-task-id> \
  [--run-id <run-id>]
```

If multiple unreconciled completed runs exist, `--run-id` is required.

## Failure/recovery commands

### `reconcile-failed-attempt`

Explicitly retire a blocked failed attempt. It performs no implementation, cleanup, or external side effect.

```bash
hermes local-first-orchestrator --database <ledger> \
  reconcile-failed-attempt \
  --task-id <ticket-id> \
  --classification <classification> \
  [--operator-id <id>] \
  [--forensic-artifact-path <path> ...]
```

Allowed classifications:

```text
runtime_infrastructure_failure
model_timeout
process_error
validation_failure
review_exhaustion
```

### `confirm-retired-attempt-cleanup`

Verify separately-authorized cleanup; this command never removes files itself.

```bash
hermes local-first-orchestrator --database <ledger> \
  confirm-retired-attempt-cleanup \
  --task-id <ticket-id> \
  [--operator-id <id>]
```

### `resume-failed-review`

Authorize a review-only retry for an unchanged validated candidate.

```bash
hermes local-first-orchestrator --database <ledger> \
  resume-failed-review \
  --task-id <ticket-id> \
  [--operator-id <id>]
```

Use while paused as described in the operator recovery guide.

## Supplemental correction workflow

### `correction-plan`

Alias: `create-correction-plan`.

Validate and persist a supplemental post-acceptance correction plan from JSON. It does not materialize, unpause, or execute anything.

```bash
hermes local-first-orchestrator --database <ledger> \
  correction-plan --plan-file <plan.json>
```

### `correction-materialize`

Alias: `materialize-correction`.

Materialize one persisted correction plan into draft microtickets with normal durable board-projection intent.

```bash
hermes local-first-orchestrator --database <ledger> \
  correction-materialize --correction-plan-id <id>
```

It does not unpause or execute tickets.

## Paid-call control

### `approve-paid`

Grant exactly one additional paid call for one feature and purpose.

```bash
hermes local-first-orchestrator --database <ledger> \
  approve-paid \
  --feature-id <feature-id> \
  --purpose architecture|integration_checkpoint|escalation \
  --reason <reason> \
  --idempotency-key <key> \
  [--operator-id <id>]
```

The idempotency key is durable and purpose/feature scoped. Unknown paid-provider outcomes fail closed.

`architecture` is a paid-governor planning purpose, not a separate dashboard model role.

## Import / legacy board flow

### `import`

Import one Hermes task/card into the Local First ledger using the explicit board adapter path.

```bash
hermes local-first-orchestrator \
  --database <ledger> \
  --repository <repo> \
  import --task-id <hermes-task-id>
```

This is part of the older controller/import surface. The scheduler-generated native flow is preferred for the current design.

## Recommended operator workflow

For routine operation:

```text
operator-status
process-next (preview)
process-next --execute --allow-board-writes
runtime-metrics
```

For long-running operation:

```text
daemon --execute --allow-board-writes
```

For recovery:

```text
pause
doctor
<one narrow recovery command>
doctor
resume
```

Avoid using narrow recovery commands as a substitute for normal scheduler flow.

## See also

- `README.md`
- `docs/configuration.md`
- `docs/operator-lifecycle-recovery.md`
- `docs/native-hermes-integration.md`
- `docs/design-v2.md`
