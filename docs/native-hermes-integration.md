# Native Hermes integration

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
It asks the operator for an existing separate-ledger path and exposes only:

- `GET /status` — bounded `paused`, `ready_local`, `running`, and
  `outbox_pending` counts; never ticket IDs, evidence, or board data.
- `POST /pause` and `POST /resume` — persist the admission flag in the ledger
  with a bounded operator reason.

Pause denies new `ready_local` claims, including the generic controller
`execute()` admission path.  It does not interrupt tickets already in an
active execution state.  The API never opens or mutates Hermes' Kanban
database, writes a board, registers an LLM tool, or invokes a model.

Dashboard assets and backend routes are loaded only after this user plugin is
enabled; restart/rescan the dashboard after enabling it.
