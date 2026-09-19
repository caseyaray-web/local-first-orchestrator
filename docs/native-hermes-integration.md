# Native Hermes integration

## Current project status

Native plugin discovery, registered operator configuration, Hermes board reads/writes, retry-safe projection, native dependency links/readiness, the restartable scheduler, observability, and the daemon are implemented. Generated Local First cards can be executed by Hermes workers and reconciled back into Local First while Local First retains validation, review, integration, checkpoint, and finality authority.

### Hermes execution reconciliation — completed dispatcher-owned path

The command:

```text
hermes local-first-orchestrator ... reconcile-hermes-execution --task-id <Hermes task id> [--run-id <run id>]
```

remains available as an explicit read-only reconciliation surface, but registered `process-next`/daemon execution now performs the normal generated-ticket handoff automatically. Local First-generated/triage cards carry `<!-- local-first-execution-handoff:v1 -->`, use Hermes `worktree` workspace mode, and are permanently excluded from Local First implementation claims. `ready_local`/`repairing` release to Hermes through `unblock`. Workers hand completed implementation back by blocking with reason exactly `local-first-awaiting-reconciliation`; Local First keeps the task blocked through validation/review/integration and alone performs final `done`.

Automatic reconciliation accepts only an unreconciled blocked sentinel run. It verifies exact generated-card identity, common Git repository, authoritative tranche base, worker `HEAD`, and base-relative binary diff; persists immutable execution evidence; creates/reuses exactly one attempt per Hermes run; and creates `adapter=hermes-dispatch` implementation-stage evidence without any Local First implementation model invocation. Subsequent blocked repair runs can become later Local First attempts.

### Multi-ticket native tranche graph — completed live path

The scheduler now owns generated-ticket activation as part of its bounded `dependency_readiness` work. Independent generated tickets are bound and prepared automatically; dependent tickets are bound while local `draft`, projected into the exact Hermes native graph, unblocked only after graph convergence, and admitted locally only after Hermes itself reports the child `ready`. Native release records immutable evidence and atomically performs local `draft -> ready_local`; because Hermes readiness was just observed authoritatively, the corresponding state projection is acknowledged locally rather than sent back as a redundant remote unblock that could race a fast worker handoff.

Before any dispatcher-owned generated ticket becomes runnable, Local First prepares Hermes' canonical `<repo>/.worktrees/<task-id>` / `wt/<task-id>` worktree at the authoritative tranche execution base. For dependent tickets this is the current rolling integration head, so later workers cannot accidentally start from a stale canonical checkout. The live proof demonstrated `T1-A -> T1-B`, with B's worktree HEAD exactly equal to A's accepted commit before B dispatch.

Successor planning now has a dedicated `propose_next` request that constrains the real standard planner to exactly the selected coarse successor tranche and criterion set, excludes completed criteria, fixes the predecessor final integration SHA, and rejects scope expansion both in the prompt contract and after parsing. Successor generated-card projection, activation identity, and runtime binding all derive repository/base/snapshot provenance from immutable `next_tranche_materializations` evidence rather than the original feature baseline.

A new operator-only `reopen-terminal-generated-projection` command can reopen a deterministic pre-create projection failure only when no external task or acknowledgement exists, no lease is active, and the current durable payload now revalidates against authoritative identity. It never retries an ambiguous external create. This was used in the acceptance run after fixing a successor projection verifier bug that had failed before any Hermes create.

## Implemented surface

This repository is itself the Hermes user-plugin directory.  Native discovery
requires only the repository-root `plugin.yaml` and `__init__.py`; no copy,
move, install, or Hermes-core change is required.

The adapter registers the existing standalone command as:

```text
hermes local-first-orchestrator ...
```

`local_first_orchestrator.cli.register_cli()` builds the argument tree once and
`run_command()` executes it.  The existing standalone entry point still calls
the same functions, so both command names retain identical arguments and
behavior.  The integration adds no model tool, hook, board schema mutation, or
implicit board write.

## Verification

Use the real runtime contract rather than a manifest-only check:

```bash
hermes plugins doctor /home/ocadmin/.hermes/plugins/local-first-orchestrator --ci
hermes plugins enable local-first-orchestrator
hermes local-first-orchestrator --help
```

The manifest declares no capabilities.  It therefore does not request tool
overrides, model/profile overrides, MCP access, or any other privileged Hermes
host API.  Hermes still presents its generic tool-override consent prompt at
enable time; leave that ungranted.  The controller's own explicit
`--execute --allow-board-writes` and `project-generated --allow-board-writes`
checks remain the only write gates in this package.

Plugin discovery and enablement are profile-scoped.  At its required in-place
location (`~/.hermes/plugins/local-first-orchestrator`), Hermes discovers this
plugin for the **default** profile only.  A named profile uses
`~/.hermes/profiles/<profile>/plugins/` and does not scan the parent plugin
directory, so `hermes -p <profile> plugins enable local-first-orchestrator`
will first report that no such plugin exists.

If a named profile must use this same source without copying or moving it, an
operator can deliberately create a symlink from that profile's `plugins/`
directory to this repository, then enable the plugin in that profile.  This is
an explicit trust/linking operation and is intentionally not performed by the
plugin or this conversion.

A fresh CLI process sees a default-profile enablement change immediately.
Restart a gateway or begin a new chat session before expecting an enabled
plugin to load there.

## Dashboard extension

The in-place user-plugin dashboard mechanism supports an operator tab, so this
repository includes:

```text
dashboard/
  manifest.json
  dist/index.js
  plugin_api.py
```

The `Local First` tab uses the published dashboard IIFE SDK and calls the
authenticated, plugin-scoped API at `/api/plugins/local-first-orchestrator/`.
It has no database-path input and never accepts an arbitrary ledger or repository
trust root from browser request data. Before it is available, an operator
registers the one separate ledger and its runtime identity locally, for example:

```bash
hermes local-first-orchestrator --database /path/ledger.db \
  --repository /path/repo --allow-repository /path/repo \
  register-dashboard
```

That writes `~/.hermes/local-first-orchestrator/operator-config.json` (or the
explicit `--config-path`). The dashboard backend validates the registered
ledger, exact canonical Git repository, and exact allowlist before opening it.
The current dashboard surface exposes:

- bounded lifecycle counters and active-ticket status;
- durable pause/resume controls;
- the registered canonical repository, allowlist, worktree root, and artifact
  root as read-only trust-boundary information;
- Hermes-profile-backed role selectors for implementation, review,
  decomposition `local`, decomposition `standard`, paid checkpoint, and paid
  escalation;
- implementation/review timeout editing while paused; and
- runtime metrics plus the current bounded adaptive decomposition-sizing
  recommendation.

Configuration saves require the controller to be paused. The backend resolves
the selected Hermes profiles to current provider/model provenance and atomically
rewrites the registered operator config; repository/ledger trust roots remain
browser read-only. Successful writes are audited in the Local First ledger.

Pause denies new `ready_local` claims, including the generic controller
`execute()` admission path. It does not interrupt tickets already in an active
execution state. The API never opens or mutates Hermes' Kanban database,
registers an LLM tool, or performs an implicit board write. Profile discovery
uses Hermes profile inspection, while model execution remains owned by the
normal orchestration stages.

For the complete operator/runtime configuration reference see
`docs/configuration.md`; for the full command surface see
`docs/command-reference.md`.

Dashboard assets and backend routes are loaded only after this user plugin is
enabled; restart/rescan the dashboard after enabling it.
