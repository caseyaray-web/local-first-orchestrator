# Hermes read-boundary contract

Inspected Hermes core commit `13ec674cdd22505ac27b9ec79c687f6459e9e187`.

- CLI parser: `hermes_cli/kanban.py` (global `--board` argument near line 232).
- Board tests: `tests/hermes_cli/test_kanban_boards.py` use `kanban --board <slug> list --json`.
- Comments are stored internally by `hermes_cli/kanban_db.py::list_comments`, but the audited `list`/`show --json` surface does not document complete comment text.

The plugin invokes only:

```text
<absolute-hermes> kanban --board <slug> list --json --status scheduled
<absolute-hermes> kanban --board <slug> show <task-id> --json
```

It consumes a list of task objects or `{ "task": { ... } }`, reading `id`, `title`, `body`, `status`, and optional `workspace_path`. Every call binds the configured slug explicitly; ambient board selection is prohibited.

Marker lookup through the real CLI is currently **UNSUPPORTED**: read commands do not establish that comment bodies are machine-readable. No SQLite fallback is used. This must be re-audited after Hermes updates or before real delivery support is claimed.
