# Local First operator guide (M6 replacement)

This guide describes the implemented **isolated** plugin surfaces. It does not authorize plugin enablement, board enrollment, dispatch, provider requests, profile changes, or modification of an existing Hermes/Local First database.

## Configuration

The command requires a trusted, local JSON configuration and the one configured native board/anchor scope:

```bash
local-first-orchestrator \
  --config /absolute/private/local-first.json \
  --board <configured-board-id> \
  --anchor-task-id <configured-anchor-id> \
  status
```

`--board` and `--anchor-task-id` must exactly match the `scope` in the JSON file. Browser and tool payloads cannot select the state root, Hermes executable/home, repository/workspace roots, role profiles, budget limits, or check commands.

The JSON schema is version `1` and has exactly these top-level fields:

```json
{
  "version": 1,
  "state_root": "/absolute/private/state-directory",
  "hermes_executable": "/absolute/path/to/hermes",
  "hermes_home": "/absolute/existing/hermes-home",
  "kanban_home": "/absolute/existing/kanban-home",
  "trusted_roots": {"repository": "/absolute/git-repository", "workspace": "/absolute/workspace"},
  "roles": {"implementation_profile": "...", "local_review_profile": "...", "planning_profile": "...", "paid_review_profile": "..."},
  "budgets": {"implementation_attempts": 1, "review_corrections": 1, "infrastructure_retries": 1, "workflow_repairs": 1, "paid_capacity": 1},
  "poll_interval_seconds": 15,
  "scope": {"board_id": "...", "anchor_task_id": "..."},
  "check_commands": [{"check_id": "checks", "argv": ["/absolute/executable", "arg"]}]
}
```

Directories and executables must already exist, be canonical non-symlink paths, and `state_root` must not be group/world accessible. Roles are distinct. Budgets are finite non-negative integers. The store is plugin-owned `evidence.sqlite3` under `state_root`; it is not Hermes `kanban.db` and is never created by normal command execution.

## Initialize a new isolated store

Only create the configured empty evidence store deliberately:

```bash
local-first-orchestrator --config /absolute/private/local-first.json \
  --board <configured-board-id> --anchor-task-id <configured-anchor-id> \
  initialize-store
```

This is the sole first-run initialization path. It does not enroll the anchor or write to a Hermes board.

## Supported commands

| Command | Effect |
| --- | --- |
| `status` | Read scoped coordinator/evidence status. |
| `initialize-store` | Explicitly create/migrate an empty plugin evidence store. |
| `enroll` | Uses the implemented bounded native enrollment path; unsupported host capability is reported, never emulated. |
| `pause [--stop]` | Persist scoped pause; `--stop` requests supported native run stops and exposes partial containment. |
| `reconcile` | Read/reconcile known scoped evidence and native observations. |
| `resume [--authorized-clear]` | Resume only according to durable operator intent. |
| `cancel` | Persist cancellation and reject late results. |
| `recover` | Propose/apply only bounded deterministic recovery. |
| `run [--once]` | Explicit coordinator loop; `--once` is the fixture/operator smoke tick. |

Results are JSON. Exit code `0` means verified/recorded/deduplicated/no-op, `3` held, `4` partial, `5` conflict, `6` unsupported, `2` invalid input, and `7` unknown.

## Native plugin and dashboard

The root plugin registration exposes the same parser through `hermes local-first-orchestrator`. The plugin also registers proposal/status tools and advisory hooks without opening the evidence store or starting a coordinator during registration.

The wheel is a Python distribution, while Hermes discovers native plugins from a directory containing `plugin.yaml` and root `__init__.py`. Hermes does not automatically turn `pip install local-first-orchestrator` into a discovered directory plugin. For a supported offline artifact installation, install the wheel in the target Python environment, then place the wheel's `*.data/data/local-first-orchestrator/` payload intact at `$HERMES_HOME/plugins/local-first-orchestrator/` and explicitly enable `local-first-orchestrator` in that target home's `config.yaml`. The payload includes its artifact-local `python/local_first_orchestrator/` package; do not copy source checkout files or reuse a different host package. Validate the installed directory with `hermes plugins doctor "$HERMES_HOME/plugins/local-first-orchestrator" --ci`, then restart Hermes only when a separate activation authorization exists.

The mounted dashboard API is `/api/plugins/local-first-orchestrator/status` plus scoped `pause`, `stop`, `reconcile`, `resume`, `cancel`, and `recover` actions. Mutations require the current observation digest; stale observations return a conflict with fresh status. Partial stops remain visible as uncontained workers. Dashboard payloads cannot edit configuration or trust boundaries in this M6 surface.

## Operational boundaries

- Native board operations use supported Hermes CLI/plugin adapters and read back effects. No native database SQL is written.
- No host monkey-patching, direct provider invocation, privileged recovery helper, or automatic worktree deletion is supported.
- A hold, failed stop, unknown effect, missing evidence, or unsupported capability is surfaced; it is not converted to success.
- Existing legacy stores, boards, worktrees, profiles, and user data are not migrated, resumed, or deleted.

## Status

M6 source and fixture verification are complete only to the extent recorded in `docs/plans/m6-implementation-progress.md`. Independent review and a separately authorized browser/live activation remain M7/cutover gates.
