# Configuration guide

This document is the complete configuration reference for Local First Orchestrator.

The configuration model intentionally separates persistent production identity from per-invocation execution gates. Production runtime identity is registered once and stored locally; board writes and daemon execution still require explicit command-line authorization every time.

## Configuration layers

Local First has four distinct configuration layers:

1. **Hermes plugin/profile configuration** — whether the plugin is enabled and what Hermes profiles exist.
2. **Operator registration** — ledger, repository trust boundary, execution roots, timeouts, and model-role mappings.
3. **Invocation-time controls** — board name, Hermes executable, `--execute`, board-write authorization, worker id, daemon pacing.
4. **Ledger-owned operational state** — pause/resume, recovery state, paid approvals/reservations, scheduler claims, and runtime metrics.

Do not collapse these layers. In particular, a browser request cannot replace the registered ledger/repository configuration, and a persisted registration cannot silently authorize board writes.

## 1. Hermes plugin configuration

### Plugin location

Default-profile discovery expects this repository at:

```text
~/.hermes/plugins/local-first-orchestrator
```

Verify it:

```bash
hermes plugins doctor ~/.hermes/plugins/local-first-orchestrator --ci
```

Enable it:

```bash
hermes plugins enable local-first-orchestrator
```

The native command becomes:

```text
hermes local-first-orchestrator ...
```

### Named Hermes profiles

Hermes plugin enablement is profile-scoped. A named profile looks for plugins beneath its own profile plugin directory rather than the default-profile directory.

If a named profile must load this same source tree, explicitly link it into that profile's plugin directory and enable the plugin there. The plugin does not create this link automatically.

See `docs/native-hermes-integration.md` for the detailed discovery model.

### Plugin capabilities

`plugin.yaml` declares no privileged plugin capabilities. Local First does not require a Hermes-core patch, a model tool, MCP access, or an implicit board-write capability.

Board writes are governed by Local First's own explicit CLI gates.

## 2. Operator registration

The operator registration is the production runtime identity.

Default location:

```text
~/.hermes/local-first-orchestrator/operator-config.json
```

Override the location with either:

```text
--operator-config-path <path>
```

for a CLI invocation, or the environment variable:

```text
LOCAL_FIRST_OPERATOR_CONFIG=/path/to/operator-config.json
```

The environment variable changes the default path used by code that calls `default_config_path()`, including the dashboard backend.

### Register with `init`

`init` is an alias of the older `register-dashboard` command.

```bash
hermes local-first-orchestrator \
  --database <ledger.db> \
  --repository <canonical-repo> \
  --allow-repository <canonical-repo> \
  init [options]
```

Registration is an execution-authority write. The config is validated before it is atomically written.

### Full persisted JSON schema

A complete registration has this shape:

```json
{
  "ledger_path": "/absolute/path/local-first.db",
  "canonical_repository": "/absolute/path/repository",
  "repository_allowlist": [
    "/absolute/path/repository"
  ],
  "implementation": {
    "profile": "worker-code-local",
    "provider": "custom:lm-studio",
    "model": "example-model"
  },
  "review": {
    "profile": "worker-review",
    "provider": "example-provider",
    "model": "example-review-model"
  },
  "worktree_root": "/absolute/path/worktrees/<repo-id>",
  "artifact_root": "/absolute/path/artifacts/<repo-id>",
  "implementation_timeout_seconds": 300,
  "review_timeout_seconds": 900,
  "decomposition": {
    "local": {
      "profile": "planner-local",
      "provider": "example-provider",
      "model": "example-model"
    },
    "standard": {
      "profile": "planner-standard",
      "provider": "example-provider",
      "model": "example-model"
    }
  },
  "paid_checkpoint": {
    "profile": "checkpoint-profile",
    "provider": "example-provider",
    "model": "example-model"
  },
  "paid_escalation": {
    "profile": "escalation-profile",
    "provider": "example-provider",
    "model": "example-model"
  }
}
```

`decomposition`, `paid_checkpoint`, and `paid_escalation` are optional at the JSON-schema level, but production paths that require those routes fail closed when the necessary route is missing.

### `ledger_path`

Required.

Rules:

- must resolve to the Local First SQLite ledger;
- must exist when a saved config is loaded;
- must **not** be named `kanban.db`;
- should be stored outside the repository worktree unless you have an explicit reason otherwise.

Example:

```text
~/.hermes/local-first-orchestrator/my-project.db
```

Create/update its schema with:

```bash
hermes local-first-orchestrator --database <ledger> migrate
```

### `canonical_repository`

Required.

Rules:

- must resolve to an existing path;
- must be a Git checkout containing `.git`;
- must appear exactly in `repository_allowlist`.

Local First uses this checkout as the canonical repository identity and as the basis for integration/worktree operations.

### `repository_allowlist`

Required, non-empty list of exact Git checkout roots.

Every entry must contain `.git`.

The canonical repository must be one of these exact roots. This is a trust boundary, not a fuzzy parent-directory allowlist.

CLI form:

```text
--allow-repository <path>
```

Repeat the flag for multiple allowed roots.

The browser dashboard intentionally cannot edit this list.

### `implementation`

Required `ModelRegistration`:

```json
{
  "profile": "...",
  "provider": "...",
  "model": "..."
}
```

This is the Local First implementation route used by Local First-owned implementation stages. Hermes-dispatched generated tickets can instead be implemented by Hermes workers and reconciled back into Local First without a Local First implementation-model call.

CLI bootstrap flags:

```text
--implementation-profile
--implementation-provider
--implementation-model
```

CLI defaults:

- profile: `worker-code-local`
- provider/model: the package's configured local-Qwen defaults.

After bootstrap, prefer selecting the Hermes profile from the dashboard; the dashboard resolves provider/model from Hermes rather than asking you to maintain the triple manually.

### `review`

Required `ModelRegistration` for fresh independent review.

CLI bootstrap flags:

```text
--review-profile
--review-provider
--review-model
```

CLI default profile is `worker-code-local`, with the same local provider/model defaults used by bootstrap. This is only a default; for production you can select a dedicated Hermes review profile in the dashboard.

### `decomposition`

Optional mapping of cost class to `ModelRegistration`.

Supported operational cost classes are:

```text
local
standard
```

`local` is the lower-cost/local decomposition route.

`standard` is the standard route used whenever the planner/scheduler explicitly requests the standard class, including standard decomposition and successor planning. Bounded triage/snapshot-revalidation flows also use the standard configured planning route where applicable.

CLI bootstrap flags:

```text
--decomposition-local-profile
--decomposition-local-provider
--decomposition-local-model

--decomposition-standard-profile
--decomposition-standard-provider
--decomposition-standard-model
```

For each route, profile/provider/model must be supplied together. Partial triples are rejected.

There is no separate operator-configurable architecture role. Architecture decisions are part of decomposition output.

### `paid_checkpoint`

Optional `ModelRegistration` for the paid integration-checkpoint stage.

CLI bootstrap flags:

```text
--paid-checkpoint-profile
--paid-checkpoint-provider
--paid-checkpoint-model
```

If omitted, the production scheduler has no configured paid checkpoint provider route.

### `paid_escalation`

Optional `ModelRegistration` for paid escalation.

CLI bootstrap flags:

```text
--paid-escalation-profile
--paid-escalation-provider
--paid-escalation-model
```

If omitted, the production scheduler has no configured paid escalation provider route.

### `worktree_root`

Required for an execution-capable persisted registration.

If not specified during `init`, Local First derives a repository-specific default beneath:

```text
~/.hermes/local-first-orchestrator/worktrees/<stable-repository-id>
```

The stable repository id is derived from the canonical resolved repository path.

CLI bootstrap override:

```text
--worktree-root <path>
```

The dashboard displays this path but does not edit it.

### `artifact_root`

Required for an execution-capable registration.

Default:

```text
~/.hermes/local-first-orchestrator/artifacts/<stable-repository-id>
```

CLI bootstrap override:

```text
--artifact-root <path>
```

The dashboard displays this path but does not edit it.

### `implementation_timeout_seconds`

Required for an execution-capable registration.

CLI bootstrap flag:

```text
--implementation-timeout-seconds <seconds>
```

`init` default: `300` seconds.

The dashboard allows this value to be edited while Local First is paused. Dashboard validation accepts `1..86400`; lower-level runtime configuration applies its own safety checks.

### `review_timeout_seconds`

Required for an execution-capable registration.

CLI bootstrap flag:

```text
--review-timeout-seconds <seconds>
```

`init` default: `900` seconds.

The dashboard allows this value to be edited while Local First is paused. Dashboard validation accepts `1..86400`.

## 3. Hermes profile-backed dashboard configuration

The Local First dashboard discovers available Hermes profiles using Hermes profile commands and resolves selected profiles server-side.

Configurable selectors:

- Local implementation
- Review
- Decomposition · local
- Decomposition · standard
- Paid checkpoint
- Paid escalation

When a selection is saved, Local First stores:

```text
profile
provider
model
```

using Hermes-reported metadata at save time.

This provides durable routing provenance while keeping Hermes as the source of truth for profile definitions.

### Save requirements

Configuration writes require Local First to be paused.

Pause from the dashboard or CLI:

```bash
hermes local-first-orchestrator --database <ledger> \
  pause --reason "change model routing"
```

Save the dashboard configuration, then resume:

```bash
hermes local-first-orchestrator --database <ledger> \
  resume --reason "configuration updated"
```

A successful dashboard save emits an `operator_configuration_updated` ledger event.

### Browser read-only fields

The dashboard intentionally does not edit:

- ledger path;
- canonical repository;
- repository allowlist;
- worktree root;
- artifact root.

These values define execution/trust boundaries and must be changed through deliberate local registration/bootstrap rather than browser request data.

## 4. CLI global runtime options

These options appear before the subcommand.

### `--database`

Required for all commands.

Selects the Local First ledger for the invocation. For production commands, it must match the registered runtime identity where the controller enforces registration consistency.

### `--repository`

Default: `.`

Canonical repository for ad-hoc/bootstrap operations. Production registered execution reads the canonical repository from operator configuration rather than accepting arbitrary runtime replacement.

### `--allow-repository`

Repeatable exact allowed Git root for ad-hoc/bootstrap operations.

If omitted during `init`, the canonical repository becomes the sole allowlisted root.

### `--worktree-root` / `--artifact-root`

Development/bootstrap overrides.

The CLI help labels these as ad-hoc-mode options, but `init` also consumes them to establish the persisted execution roots.

### `--implementation-timeout-seconds` / `--review-timeout-seconds`

Development/bootstrap timeout overrides. `init` persists supplied values.

### `--operator-config-path`

Explicit operator-config file for the invocation.

Use this when you deliberately manage multiple registrations or do not want the default location.

### `--ad-hoc-runtime`

Explicit development-only runtime composition.

Production scheduler paths such as `process-next`, `daemon`, feature planning, and recovery commands reject ad-hoc composition where registered runtime authority is required.

### `--hermes-executable`

Explicit Hermes executable for commands that access the Hermes board or dispatcher read contract.

Common value:

```text
hermes
```

Required for write-enabled `process-next`/`daemon`, generated projection, and Hermes execution reconciliation.

### `--board`

Hermes board name for explicit board access.

Required for write-enabled scheduler/daemon execution and board projection/reconciliation commands.

## 5. Scheduler execution configuration

### Preview one tick

`process-next` is dry-run by default:

```bash
hermes local-first-orchestrator --database <ledger> process-next
```

No board-write permission is implied.

### Execute one tick

Requires all of:

- registered operator runtime;
- `--hermes-executable`;
- `--board`;
- subcommand `--execute`;
- subcommand `--allow-board-writes`.

Example:

```bash
hermes local-first-orchestrator \
  --database <ledger> \
  --hermes-executable hermes \
  --board <board> \
  process-next --execute --allow-board-writes
```

### Daemon

The daemon requires the same explicit execution/write gates.

Daemon options:

| Option | Default | Meaning |
|---|---:|---|
| `--worker-id` | `local-first-daemon` | scheduler/lease identity |
| `--planner-executable` | `hermes` | executable used for decomposition planning |
| `--idle-sleep-seconds` | `1.0` | delay after no-work tick |
| `--busy-sleep-seconds` | `0.25` | delay after productive tick |
| `--error-backoff-seconds` | `1.0` | initial delay after an error |
| `--max-error-backoff-seconds` | `30.0` | maximum error delay |
| `--max-iterations` | unset | optional bounded daemon run |

The daemon reuses the same `process-next` scheduler primitive.

## 6. Pause/resume configuration behavior

Pause state is durable ledger state, not a dashboard-only toggle.

```bash
hermes local-first-orchestrator --database <ledger> \
  pause --reason <reason> [--operator-id <id>]
```

While paused, new scheduler admission/claim behavior is stopped where required, and manual recovery/configuration operations can run against a stable control plane.

Resume:

```bash
hermes local-first-orchestrator --database <ledger> \
  resume --reason <reason> [--operator-id <id>]
```

Resume does not override evidence or force a stage; it merely permits normal scheduling to continue.

## 7. Paid-call policy and approvals

Paid provider routes are configured separately from paid-call authorization.

A configured route says **which Hermes profile/provider/model** can service a paid stage. The usage governor decides **whether another paid call is authorized**.

Paid purposes currently recognized by the ledger governor:

```text
architecture
integration_checkpoint
escalation
```

The `architecture` purpose is used by paid planning/governance internals; it is not a dashboard architecture role.

Grant exactly one additional call:

```bash
hermes local-first-orchestrator --database <ledger> \
  approve-paid \
  --feature-id <feature> \
  --purpose <purpose> \
  --reason <reason> \
  --idempotency-key <unique-key> \
  [--operator-id <id>]
```

Rules:

- approval grants exactly one call;
- purpose and feature are scoped;
- reason is required;
- idempotency key is required;
- unknown provider outcome fails closed and must not be blindly repeated.

## 8. Runtime metrics/adaptive sizing configuration

There is currently no operator knob for arbitrary adaptive-sizing values. The bounded policy is code-defined to prevent metrics from becoming an uncontrolled acceptance or scheduling authority.

Baseline planning hints:

```text
active tranche max tickets: 4
target context tokens:      20,000
```

Minimum sample count before adaptation:

```text
6 completed-ticket observations
```

Current recommendations:

- high rework / weak first-attempt acceptance: `3` active tickets, `16,000` target context;
- strong first-attempt acceptance: `5` active tickets, `24,000` target context;
- otherwise baseline.

Absolute hard bounds inside the planner integration:

```text
active tickets: 2..6
context target: 12,000..28,000
```

These are planning hints only. Patch budgets and deterministic acceptance limits do not change.

Inspect the current recommendation:

```bash
hermes local-first-orchestrator --database <ledger> runtime-metrics
```

## 9. Model-role mapping reference

| Role | Config key | Hermes profile-backed | Runtime use |
|---|---|---|---|
| Local implementation | `implementation` | yes | Local First implementation stage / repair implementation |
| Independent review | `review` | yes | review stage |
| Decomposition local | `decomposition.local` | yes | local/lower-cost decomposition route |
| Decomposition standard | `decomposition.standard` | yes | standard planning, successor planning, standard planning-related flows |
| Paid checkpoint | `paid_checkpoint` | yes | integration checkpoint paid stage |
| Paid escalation | `paid_escalation` | yes | paid escalation stage |

There is intentionally no separate architecture role in this table.

## 10. Example: bootstrap then manage from dashboard

Bootstrap a complete registration:

```bash
hermes local-first-orchestrator \
  --database ~/.hermes/local-first-orchestrator/example.db \
  --repository /srv/project \
  --allow-repository /srv/project \
  init \
  --implementation-profile worker-code-local \
  --implementation-provider custom:lm-studio \
  --implementation-model <local-model> \
  --review-profile worker-review \
  --review-provider <provider> \
  --review-model <review-model> \
  --decomposition-local-profile planner-local \
  --decomposition-local-provider <provider> \
  --decomposition-local-model <model> \
  --decomposition-standard-profile planner-standard \
  --decomposition-standard-provider <provider> \
  --decomposition-standard-model <model>
```

Then:

1. restart/reload the Hermes dashboard/plugin host;
2. open the **Local First** page;
3. pause Local First;
4. choose Hermes profiles from the role selectors;
5. save configuration;
6. resume Local First.

The saved JSON retains resolved provider/model provenance for the selected profiles.

## 11. Multiple projects

The operator config represents one registered runtime identity.

For multiple independent projects, use separate:

- Local First ledger files;
- operator-config files;
- canonical repositories;
- worktree/artifact roots.

Select a non-default config with `--operator-config-path` or `LOCAL_FIRST_OPERATOR_CONFIG`.

Do not reuse one ledger for unrelated repository identities unless the design explicitly calls for that relationship.

## 12. Troubleshooting configuration

### `operator dashboard is not registered`

No operator config exists at the selected/default path. Run `init` or set the correct `LOCAL_FIRST_OPERATOR_CONFIG` / `--operator-config-path`.

### `execution_runtime_not_configured`

The persisted config is legacy/incomplete or contains only part of the runtime quartet. Re-run `init` with execution roots/timeouts or replace the registration with a complete one.

### `canonical repository must be an exact allowlisted root`

Add the canonical repository itself to `--allow-repository` and re-register.

### `canonical repository and allowlist entries must be Git checkouts`

Every configured root must contain `.git`.

### `decomposition route is not configured for cost class`

Configure the required `local` or `standard` decomposition route via `init` or the dashboard.

### Dashboard configuration save returns a pause error

Pause Local First before changing model-role routing or timeouts.

### Hermes profiles unavailable in dashboard

Verify `hermes profile list` and `hermes profile show <profile>` work for the Hermes environment hosting the plugin. The dashboard backend resolves those commands server-side.

### Scheduler refuses execution

For production `process-next`/daemon execution verify:

- registered runtime exists;
- `--hermes-executable` is supplied;
- `--board` is supplied;
- `--execute` is supplied;
- `--allow-board-writes` is supplied.

### Paid call reports unknown outcome

Do not repeat it manually. Unknown outcomes are intentionally fail-closed and require reconciliation/operational investigation rather than a blind retry.

## Related documentation

- `README.md` — project overview and quick start.
- `docs/command-reference.md` — complete CLI command reference.
- `docs/operator-lifecycle-recovery.md` — recovery workflows.
- `docs/native-hermes-integration.md` — plugin and Hermes integration details.
- `docs/design-v2.md` — detailed design and invariants.
- `docs/traceability.md` — implementation/acceptance status.
