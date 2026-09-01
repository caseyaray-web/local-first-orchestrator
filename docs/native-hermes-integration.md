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

## Dashboard extension plan (not implemented)

Do not add a dashboard until the ledger has a deliberate read-model API.
When that is needed, keep it inside this same repository at:

```text
dashboard/
  manifest.json
  dist/index.js
  plugin_api.py
```

Use a new `/local-first-orchestrator` tab or a page-scoped slot, with the
published dashboard IIFE SDK.  `plugin_api.py` should expose read-only,
validated summaries from the configured separate ledger database under
`/api/plugins/local-first-orchestrator/`; it must not open or mutate Hermes'
Kanban database.  Any future operator action must retain the controller's
explicit write flags and use a retryable projection/outbox boundary rather
than a direct dashboard-to-board write.
