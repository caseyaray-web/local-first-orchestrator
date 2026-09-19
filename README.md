# Local First Orchestrator

Local First Orchestrator is a Hermes-native hybrid coding workflow designed to make **quantized local models practical for real software-engineering work without paying frontier-model prices for every implementation step**.

In one sentence: **use stronger paid models to decide what should be built and whether it was built correctly, while using quantized local models to perform as much bounded implementation work as possible under deterministic supervision.**

## Why this exists

Quantized local coding models can be remarkably capable and inexpensive to run, but they generally require more supervision than larger paid models.

A local model may produce useful code while still:

- claiming the work is complete before the repository actually satisfies the task;
- repeatedly failing the same validation or correction;
- misunderstanding review feedback;
- making partial fixes without resolving the underlying failure;
- drifting after several repair attempts; or
- becoming stuck in a loop that a stronger model could resolve quickly.

The problem is often not that the local model cannot write the code. It can. The harder problem is that the model should not be trusted to decide, by itself, whether the work is actually finished.

Local First Orchestrator exists to provide that accountability layer.

## The hybrid workflow

The reference workflow deliberately assigns work according to both **model capability** and **cost**.

**Paid, higher-capability models are best used for:**

- feature planning;
- task decomposition;
- difficult architectural reasoning when needed;
- independent review; and
- escalation when a local implementation stops converging.

**Quantized local models are best used for:**

- bounded implementation tasks;
- deterministic-validation-driven corrections;
- review-driven corrections; and
- other repository work constrained by an explicit contract.

**Local First Orchestrator sits between them and enforces:**

- durable feature and microticket contracts;
- deterministic validation before acceptance;
- independent review requirements;
- attempt and failure tracking;
- bounded repair routing;
- repeated-failure detection and triage;
- escalation to stronger paid models when local work stops converging;
- Git integration and tranche checkpoints;
- crash/restart recovery;
- paid-call governance; and
- final completion authority.

Hermes continues to own native task dependencies, readiness, worker dispatch, board projection, and model profiles. Local First does not replace Hermes; it adds the durable acceptance and recovery layer that makes cheaper local implementation safer to automate.

## The economic goal

The goal is not to make a quantized local model behave like a frontier model.

The goal is to build a workflow in which it **does not need to**.

Implementation is usually the most repetitive and token-heavy part of a coding workflow. By giving that work to a local model inside a tightly bounded contract, the system can avoid consuming large amounts of subscription usage or API spend. Paid-model intelligence is reserved for places where higher-quality reasoning has the most leverage: planning, decomposition, review, and escalation.

When the local model succeeds, the workflow moves forward without spending paid-model capacity on the implementation itself. When it repeatedly fails, Local First does not let it loop indefinitely or accept its own claim of success; it routes the failure into triage or escalation so a stronger model can intervene.

> **Use paid intelligence to decide what should be done and whether it was done correctly. Use local compute to do as much of the bounded implementation as possible.**

The model-role configuration remains flexible, so operators can choose different Hermes profiles for any role. The split above is the intended reference architecture and the reason the plugin exists.

## A concrete example

Suppose a paid model decomposes a feature into three bounded implementation tickets.

Hermes schedules those tickets and dispatches a quantized local coding model to implement them.

The local model finishes the first ticket and says it is done. Local First does not treat that statement as completion evidence. It validates the repository deterministically and then requires an independent review before acceptance.

The second ticket fails validation. The local model receives compact failure evidence and gets a bounded correction attempt. If the correction succeeds, the workflow continues normally.

The third ticket repeatedly produces the same failure. Instead of issuing another identical local retry, Local First recognizes that the work is no longer converging and moves it into triage or escalation. A stronger paid model can then diagnose the issue, revise the plan, or provide higher-quality reasoning.

The local model still performed most of the implementation work. Paid-model usage was concentrated on the points where stronger reasoning provided the greatest value.

## Central design rule

> **A model's claim that work is complete is not completion evidence.**

Completion comes from durable state, deterministic validation, independent review, successful integration, and the explicit acceptance rules of the workflow.

That distinction is especially important when using smaller or quantized models, and it is the reason the orchestration layer exists.

## Current status

The design-v2 implementation roadmap is complete for the current bounded policy. Representative live acceptance has been performed for:

- Local First-owned implementation and review;
- Hermes-dispatched worker execution reconciled back into Local First;
- multi-ticket Hermes-native dependency graphs;
- repair and triage;
- paid checkpoint/escalation calls;
- crash/restart recovery across model, Hermes, Git, checkpoint, and paid-provider boundaries;
- operator lifecycle/recovery UX;
- runtime metrics and bounded adaptive decomposition sizing.

## Core design principles

### 1. Hermes schedules; Local First decides completion

Hermes owns native task dependencies, readiness, worker dispatch, and board projection. Local First does not duplicate Hermes scheduling semantics.

Local First owns the acceptance path: contracts, deterministic validation, review evidence, Git integration, checkpoints, reconciliation, and finality. A Hermes worker finishing an implementation is not the same thing as the feature being accepted.

### 2. External effects are replay-safe or fail closed

Every boundary that can produce an irreversible or ambiguous effect is treated as a crash boundary. Completed effects are replayed from durable evidence where possible. Ambiguous outcomes are not blindly repeated.

Examples include:

- model invocations;
- Hermes board state/comment/dependency writes;
- Git commits and tranche-head advances;
- checkpoint artifacts and integration commands;
- paid provider calls.

### 3. Validation and finality are deterministic authority

Model output can propose implementation, review judgments, decomposition, triage, or checkpoint decisions. It does not bypass deterministic validation or rewrite completion authority.

Adaptive metrics follow the same rule: they can change planning hints inside hard bounds, but cannot relax validation, patch budgets, review, Git, paid-call controls, or finality.

### 4. Recovery is explicit and failure-class specific

There is no generic “retry whatever failed” operator command. An incomplete model invocation, a review-infrastructure failure, a terminal board projection, and a retired failed attempt have different safety requirements.

The operator UX therefore exposes diagnosis plus narrow recovery commands rather than a universal retry button.

### 5. Configuration is operator-owned and profile-based

The production runtime is registered once and persisted locally. Model-using roles are mapped to Hermes profiles. The Hermes dashboard can edit those role mappings while Local First is paused.

Repository trust boundaries remain explicit and are not editable through browser request parameters.

### 6. Observability is derived, not authoritative

Runtime metrics are append-only observations derived from already-authoritative ledger evidence. Metrics collection cannot block completion. Missing observations can be materialized later.

## Architecture at a glance

```text
                         Hermes
        ┌──────────────────────────────────────┐
        │ board / dependencies / readiness     │
        │ worker dispatch / Hermes profiles    │
        │ native plugin + dashboard host       │
        └──────────────────┬───────────────────┘
                           │
                  explicit adapter boundary
                           │
        ┌──────────────────▼───────────────────┐
        │        Local First Orchestrator       │
        │                                       │
        │ SQLite ledger                         │
        │ contracts + decomposition evidence    │
        │ implementation reconciliation         │
        │ deterministic validation              │
        │ independent review                    │
        │ repair / triage                       │
        │ Git integration / tranche checkpoint  │
        │ paid-call governance                  │
        │ metrics / adaptive planning hints     │
        │ completion / finality authority       │
        └──────────────────┬───────────────────┘
                           │
                 local Git repositories
                 worktrees + artifacts
```

## Main workflow

A normal scheduler-driven feature progresses through a bounded sequence of durable stages. Exact stages depend on ticket state and ownership, but the overall shape is:

```text
feature admission
  → decomposition / tranche materialization
  → Hermes dependency projection and readiness
  → implementation or Hermes worker handoff
  → deterministic validation
  → independent review
  → repair or triage when necessary
  → acceptance
  → Git integration
  → tranche checkpoint
  → optional paid checkpoint / escalation
  → successor tranche materialization
  → completion and Hermes done projection
```

Decomposition has two configured Hermes routing classes:

- `local`: lower-cost/local planning route;
- `standard`: standard planning route used where the scheduler requests the standard class, including standard planning, bounded triage/snapshot revalidation, and successor planning.

There is no separate architecture model role in the operator configuration; architecture decisions are part of decomposition output.

## Repository layout

```text
local_first_orchestrator/   Python package and orchestration implementation
dashboard/                  Hermes dashboard plugin UI + API
docs/                       operator guides and CLI/integration references
tests/                      unit/integration/acceptance regression suite
plugin.yaml                 Hermes plugin manifest
__init__.py                 Hermes native plugin registration
pyproject.toml              standalone Python package metadata
```

## Requirements

- Python 3.11+
- Git
- Hermes installed and available as `hermes` for native plugin/profile/board operation
- a Git repository to operate on
- a **separate** SQLite ledger path; never point Local First at Hermes `kanban.db`

The package itself has no third-party Python runtime dependencies declared in `pyproject.toml`; Hermes-hosted dashboard/API execution depends on the Hermes plugin host environment.

## Installation and Hermes plugin discovery

This repository is designed to live directly at:

```text
~/.hermes/plugins/local-first-orchestrator
```

When it is in that location, Hermes can discover it as an in-place user plugin; no copy into Hermes core is required.

Verify and enable it:

```bash
hermes plugins doctor ~/.hermes/plugins/local-first-orchestrator --ci
hermes plugins enable local-first-orchestrator
hermes local-first-orchestrator --help
```

For the standalone command, install the package into your Python environment if desired:

```bash
python -m pip install -e .
local-first-orchestrator --help
```

The standalone command and `hermes local-first-orchestrator` use the same parser and handler.

Hermes plugin enablement is profile-scoped. See [docs/native-hermes-integration.md](docs/native-hermes-integration.md) for named-profile discovery details.

## Quick start

### 1. Create/migrate a separate ledger

```bash
hermes local-first-orchestrator \
  --database ~/.hermes/local-first-orchestrator/my-project.db \
  migrate
```

### 2. Register the production runtime

The registration contains the canonical repository, allowlist, execution roots, timeouts, and role routing.

```bash
hermes local-first-orchestrator \
  --database ~/.hermes/local-first-orchestrator/my-project.db \
  --repository /absolute/path/to/repo \
  --allow-repository /absolute/path/to/repo \
  init \
  --implementation-profile <profile> \
  --implementation-provider <provider> \
  --implementation-model <model> \
  --review-profile <profile> \
  --review-provider <provider> \
  --review-model <model> \
  --decomposition-local-profile <profile> \
  --decomposition-local-provider <provider> \
  --decomposition-local-model <model> \
  --decomposition-standard-profile <profile> \
  --decomposition-standard-provider <provider> \
  --decomposition-standard-model <model>
```

By default this writes:

```text
~/.hermes/local-first-orchestrator/operator-config.json
```

After bootstrap, the Hermes dashboard is the preferred way to change model-role profile routing.

See [docs/configuration.md](docs/configuration.md) for every field and option.

### 3. Restart the Hermes dashboard/plugin host

The dashboard/plugin host must reload to pick up plugin UI/backend changes. Once loaded, the **Local First** dashboard page shows lifecycle status, runtime metrics, and profile-based role configuration.

### 4. Inspect status

```bash
hermes local-first-orchestrator \
  --database ~/.hermes/local-first-orchestrator/my-project.db \
  operator-status
```

For recovery-oriented diagnosis:

```bash
hermes local-first-orchestrator \
  --database ~/.hermes/local-first-orchestrator/my-project.db \
  doctor
```

### 5. Run one scheduler tick

Preview is the default:

```bash
hermes local-first-orchestrator \
  --database ~/.hermes/local-first-orchestrator/my-project.db \
  process-next
```

Execution requires explicit board access and write authorization:

```bash
hermes local-first-orchestrator \
  --database ~/.hermes/local-first-orchestrator/my-project.db \
  --hermes-executable hermes \
  --board <board-name> \
  process-next \
  --execute \
  --allow-board-writes
```

### 6. Run the daemon

```bash
hermes local-first-orchestrator \
  --database ~/.hermes/local-first-orchestrator/my-project.db \
  --hermes-executable hermes \
  --board <board-name> \
  daemon \
  --execute \
  --allow-board-writes
```

The daemon repeatedly invokes the same bounded one-tick scheduler primitive. It does not create a second scheduling model.

## Hermes dashboard

The **Local First** dashboard page provides:

- pause/resume controls;
- ready/running/triage/done/outbox counters;
- bounded active-ticket status;
- Hermes-profile selectors for:
  - local implementation;
  - review;
  - decomposition `local`;
  - decomposition `standard`;
  - paid checkpoint;
  - paid escalation;
- implementation/review timeout editing;
- read-only repository, allowlist, worktree, and artifact-root display;
- runtime metrics and current adaptive decomposition sizing recommendation.

Profile changes require Local First to be paused. On save, the backend resolves the selected Hermes profile to its current provider/model provenance and writes the operator config atomically.

The dashboard never accepts arbitrary ledger/database paths from browser requests.

## Pause, recovery, and resume

Pause before invasive manual recovery or configuration changes:

```bash
hermes local-first-orchestrator --database <ledger> \
  pause --reason "maintenance" --operator-id <operator>
```

Inspect recovery state:

```bash
hermes local-first-orchestrator --database <ledger> doctor
```

Perform the narrow recovery action recommended for the actual failure class, then resume:

```bash
hermes local-first-orchestrator --database <ledger> \
  resume --reason "recovery complete" --operator-id <operator>
```

See [docs/operator-lifecycle-recovery.md](docs/operator-lifecycle-recovery.md) for the complete recovery model.

## Paid checkpoint and escalation governance

Paid routes are optional and must be explicitly configured. Paid calls use reservation-first durable accounting and fail closed on unknown outcomes.

When a feature exhausts its configured call allowance, an operator can grant exactly one additional call for one feature/purpose:

```bash
hermes local-first-orchestrator --database <ledger> \
  approve-paid \
  --feature-id <feature-id> \
  --purpose integration_checkpoint \
  --reason "operator-approved checkpoint" \
  --idempotency-key <unique-key>
```

Valid purposes currently include `architecture`, `integration_checkpoint`, and `escalation`. `architecture` here is a paid-governor purpose retained by the planning subsystem; it is **not** a separate dashboard architecture role.

## Runtime metrics and adaptive sizing

Materialize missing completed-ticket observations and inspect the current sizing recommendation:

```bash
hermes local-first-orchestrator --database <ledger> runtime-metrics
```

Metrics include attempts, acceptance outcome, deterministic context-size estimates, declared scope, implementation/review durations, and paid-call/token usage where available.

After a minimum sample count, outcome history can adjust only decomposition planning hints within hard bounds. It cannot alter validation or finality.

## Configuration summary

Production configuration has four layers:

1. **Hermes plugin/profile configuration** — plugin enablement and Hermes profile definitions.
2. **Local First operator registration** — ledger, canonical repository, allowlist, execution roots, timeouts, and role mappings.
3. **Invocation-time board controls** — board name, Hermes executable, execute/write gates, daemon timing.
4. **Ledger-owned operational policy** — pause state, paid approvals/reservations, runtime evidence, recovery state, and adaptive metrics.

The complete reference is in [docs/configuration.md](docs/configuration.md).

## Command reference

See [docs/command-reference.md](docs/command-reference.md) for the complete grouped CLI reference.

Common commands:

| Command | Purpose |
|---|---|
| `migrate` | Create/update the Local First ledger schema |
| `init` | Persist the production operator registration |
| `operator-status` | Bounded lifecycle and scheduler status |
| `doctor` | Read-only recovery diagnosis and recommended actions |
| `pause` / `resume` | Durable lifecycle control |
| `process-next` | Preview or execute one durable scheduler stage |
| `daemon` | Repeated bounded scheduler ticks |
| `admit-feature-contract` | Persist one feature contract without planning |
| `plan-feature` | Produce and persist one validated decomposition plan |
| `runtime-metrics` | Backfill/inspect runtime metrics and adaptive sizing |
| `approve-paid` | Grant one feature/purpose-specific paid call |

## Trust and safety boundaries

Local First deliberately keeps several boundaries explicit:

- the Local First ledger must not be Hermes `kanban.db`;
- the canonical repository must be an exact allowlisted Git checkout;
- production execution uses the persisted operator registration rather than arbitrary runtime overrides;
- board writes require explicit `--allow-board-writes`;
- scheduler/daemon execution requires explicit `--execute`;
- model invocation ambiguity fails closed;
- paid-call ambiguity fails closed;
- browser configuration cannot alter repository trust roots;
- metrics cannot alter acceptance/finality authority;
- generated Hermes work is not marked `done` until Local First completes its acceptance/integration path.

## Testing

Run the full suite:

```bash
python -m pytest -q -o addopts=
```

Useful hygiene checks:

```bash
git diff --check
python -m compileall -q local_first_orchestrator dashboard tests
node --check dashboard/dist/index.js
```

## Documentation map

Start here, then use the focused references as needed:

- [Configuration guide](docs/configuration.md) — complete operator/runtime/profile configuration reference.
- [Command reference](docs/command-reference.md) — all CLI commands grouped by purpose.
- [Operator lifecycle and recovery](docs/operator-lifecycle-recovery.md) — pause/resume, diagnosis, recovery workflows, daemon operation.
- [Native Hermes integration](docs/native-hermes-integration.md) — plugin discovery, board/dispatcher integration, native graph behavior.
- [Detached native-release approval](docs/native-release-detached-approval.md) — operator-controlled signing boundary for native release revalidation.

## Development status and scope

The current design-v2 capability roadmap is implemented and acceptance-reconciled. Remaining work should be treated as release engineering, compatibility maintenance, additional provider/adapter acceptance, UX refinement, or future design evolution—not as an incomplete core orchestrator stage.
