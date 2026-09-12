# Native Hermes integration

## Current project status — 2026-09-12

Native plugin discovery, registered operator configuration, real Hermes board reads/writes, retry-safe state/comment projection, comment-marker reconciliation, native dependency links/readiness semantics, the restartable scheduler, observability, and the daemon are implemented. Milestone 20 drove a representative real Local First-owned feature through implementation, deterministic validation, independent review, acceptance, Git commit, Local First completion, and Hermes `done`; it also proved one live ambiguous external-effect restart without duplicate comment delivery.

The major native-integration acceptance items are now complete for representative configured paths:

1. **Hermes-dispatched execution reconciliation — complete for representative dispatcher-owned execution:** generated Local First cards are Hermes-owned for implementation, automatically polled for blocked handoff runs, excluded from the Local First implementation launcher, reconciled into deterministic Local First attempts, and kept non-final until Local First acceptance/integration projects `done`. The real dispatcher proof completed with zero Local First implementation model calls; see `docs/hermes-dispatch-execution-acceptance.md`.
2. **Full scheduler-generated tranche graph proof — complete:** a real generated two-ticket tranche with exact Hermes-native dependency readiness was executed ticket-by-ticket by dispatcher workers, reconciled and integrated through a rolling Local First integration head, checkpointed, and followed by real standard-model successor materialization plus exact `next_tranche_activation`. Successor runtime binding/worktree preparation at the predecessor final integration SHA was separately re-verified after the final provenance fix. See `docs/multi-ticket-native-tranche-acceptance.md`.
3. **Representative live operational crash proof — complete:** a real Hermes ambiguous write, real local implementation ambiguity/replay, real fresh-review replay, actual Git commit/ref recovery, checkpoint artifact/integration-command replay, and live paid-provider replay/unknown-outcome stop behavior are now acceptance-proven. See `docs/live-external-boundary-crash-acceptance.md` and `docs/live-paid-provider-acceptance.md`.

The historical installation/dashboard notes below remain valid where they describe Hermes plugin mechanics; `docs/design-v2.md` is authoritative for current full-project status.



### Hermes execution reconciliation — completed dispatcher-owned path

The command:

```text
hermes local-first-orchestrator ... reconcile-hermes-execution --task-id <Hermes task id> [--run-id <run id>]
```

remains available as an explicit read-only reconciliation surface, but registered `process-next`/daemon execution now performs the normal generated-ticket handoff automatically. Local First-generated/triage cards carry `<!-- local-first-execution-handoff:v1 -->`, use Hermes `worktree` workspace mode, and are permanently excluded from Local First implementation claims. `ready_local`/`repairing` release to Hermes through `unblock`. Workers hand completed implementation back by blocking with reason exactly `local-first-awaiting-reconciliation`; Local First keeps the task blocked through validation/review/integration and alone performs final `done`.

Automatic reconciliation accepts only an unreconciled blocked sentinel run. It verifies exact generated-card identity, common Git repository, authoritative tranche base, worker `HEAD`, and base-relative binary diff; persists immutable execution evidence; creates/reuses exactly one attempt per Hermes run; and creates `adapter=hermes-dispatch` implementation-stage evidence without any Local First implementation model invocation. Subsequent blocked repair runs can become later Local First attempts.

The real proof dispatched Hermes task `t_6ddb14ba` to `worker-code-local`, reconciled run `1`, passed deterministic validation and independent review, committed `59b1b6c0da7bc7402d4c12758ccf2b268e0e5688`, finalized Local First, then projected Hermes `done`. Checkpointing also recovered correctly after Hermes removed its worker worktree by reconstructing from the immutable final commit. See `docs/hermes-dispatch-execution-acceptance.md`.



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
It has no database-path input and accepts no `database`, repository, provider,
or model request parameter. Before it is available, an operator registers the
one separate ledger and its runtime identity locally, for example:

```bash
hermes local-first-orchestrator --database /path/ledger.db \
  --repository /path/repo --allow-repository /path/repo \
  register-dashboard
```

That writes `~/.hermes/local-first-orchestrator/operator-config.json` (or the
explicit `--config-path`). The dashboard backend validates the registered
ledger, exact canonical Git repository, and exact allowlist before opening it.
The read model exposes only:

- bounded counters: `ready_local`, active `running`, `needs_triage`, `done`,
  and `outbox_pending`;
- at most 25 active `{ticket_id, state, feature_id, tranche_id}` records;
- the registered canonical repository/allowlist and implementation/review
  `{profile, provider, model}` identities; and
- `POST /pause` and `POST /resume`, which persist the admission flag with a
  bounded operator reason.

Pause denies new `ready_local` claims, including the generic controller
`execute()` admission path.  It does not interrupt tickets already in an
active execution state.  The API never opens or mutates Hermes' Kanban
database, writes a board, registers an LLM tool, or invokes a model.

Dashboard assets and backend routes are loaded only after this user plugin is
enabled; restart/rescan the dashboard after enabling it.
