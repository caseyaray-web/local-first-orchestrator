# Local First Orchestrator

A Hermes plugin for scoped, board-native Local First coordination. Hermes remains the source of truth for native tasks, runs, dependencies, and dispatch. The plugin records only its own bounded evidence, operator intent, and replay-safe operation receipts; it does not maintain a shadow task lifecycle or write Hermes databases directly.

## Current status

M6 is an **isolated source and fixture milestone**. It has not enabled this plugin, enrolled a live board, started a coordinator, called a provider, or changed any live Hermes/profile/data state. The dashboard’s mounted/browser fixture coverage is implemented; fresh independent review and any activation remain paused M7/cutover work.

## Supported M6 surfaces

- one CLI parser for standalone `local-first-orchestrator` and native `hermes local-first-orchestrator`;
- strict versioned plugin configuration and explicit evidence-store initialization;
- scoped status, enrollment, pause/stop, reconcile, resume, cancel, recover, and bounded loop commands;
- five scoped planner/reviewer proposal/status tools plus non-blocking advisory hooks;
- plugin dashboard status, configured role-profile display, configured-anchor enrollment, and bounded configuration tightening with stale-observation protection;
- wheel/sdist payloads containing the plugin root registration, manifest, dashboard manifest/API, and dashboard JavaScript.

## Isolated operator use

Use only an explicitly prepared private configuration and its exact scope:

```bash
local-first-orchestrator \
  --config /absolute/private/local-first.json \
  --board <configured-board-id> \
  --anchor-task-id <configured-anchor-id> \
  status
```

Initialize a new plugin-owned evidence store only when that is the intended isolated setup:

```bash
local-first-orchestrator \
  --config /absolute/private/local-first.json \
  --board <configured-board-id> \
  --anchor-task-id <configured-anchor-id> \
  initialize-store
```

Normal commands open an existing store only. They never create a missing store, alter `kanban.db`, select arbitrary filesystem roots from request data, or implicitly enable/activate the plugin.

See [the operator guide](docs/operator-guide.md) for configuration fields, command outcomes, dashboard behavior, and limitations. The removal inventory and retained replacement test mapping are in [docs/plans/m6-legacy-removal-inventory.md](docs/plans/m6-legacy-removal-inventory.md).

## Development verification

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI='' \
  uv run --offline --no-project --with pytest --with jsonschema \
  python -m pytest -q -pno:cacheprovider -oaddopts=''

uv build --offline --wheel --sdist --out-dir "$TMPDIR/local-first-orchestrator-dist"
```

These commands are fixture/package checks, not activation or deployment.
