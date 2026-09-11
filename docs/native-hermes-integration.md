# Native Hermes integration

## Current project status — 2026-09-11

Native plugin discovery, registered operator configuration, real Hermes board reads/writes, retry-safe state/comment projection, comment-marker reconciliation, native dependency links/readiness semantics, the restartable scheduler, observability, and the daemon are implemented. Milestone 20 drove a representative real Local First-owned feature through implementation, deterministic validation, independent review, acceptance, Git commit, Local First completion, and Hermes `done`; it also proved one live ambiguous external-effect restart without duplicate comment delivery.

The native-integration work that remains is narrower than the original bootstrap scope:

1. **Hermes-dispatched execution reconciliation (in progress):** the explicit read-only Hermes run snapshot plus replay-safe durable handoff into one Local First attempt is implemented. The remaining work is automatic dispatcher-run detection/ownership, preventing premature Hermes `done`, and live proof that a dispatcher-owned worker candidate flows through Local First validation/review without spawning a second implementation worker.
2. **Full scheduler-generated tranche graph proof:** exercise generated cards plus exact native dependency projection/release across a real active tranche and successor activation.
3. **Broader live operational proof:** exhaustively crash/restart real external boundaries and exercise the paid checkpoint/escalation route against a live paid provider.

The historical installation/dashboard notes below remain valid where they describe Hermes plugin mechanics; `docs/design-v2.md` is authoritative for current full-project status.



### Hermes execution reconciliation — first implemented slice

The command:

```text
hermes local-first-orchestrator ... reconcile-hermes-execution --task-id <Hermes task id> [--run-id <run id>]
```

reads Hermes only; it does not require board-write permission and never launches implementation. A completed non-projection Hermes run is accepted only when its task maps exactly to one Local First ticket, its workspace is attached to the configured Git repository, and its `HEAD` descends from the authoritative execution base. The exact base-relative binary diff is hashed, an immutable JSON execution artifact is persisted, and one atomic ledger transaction creates/reuses the Local First attempt, records `adapter=hermes-dispatch` implementation-stage evidence, and transitions `ready_local -> implementing`. Existing deterministic validation/review then owns the trust lifecycle.

Replaying the same Hermes run returns the same attempt. Multiple unreconciled completed runs require an explicit run id. Local First projection-generated runs are excluded. If Hermes already reports the task final `done`, reconciliation stops because Local First completion authority may already have been bypassed.

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
