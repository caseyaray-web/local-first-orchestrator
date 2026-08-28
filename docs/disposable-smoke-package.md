# Milestone 2 disposable-card smoke package

This package is documentation only. Do not execute it against a production repository.

## Disposable card

**Title:** `SMOKE disposable local-first runtime`

**Body:**

```text
<!-- local-first-orchestrator -->
Ownership: local-first-controller
```local-first-contract
{"objective":"Change disposable smoke value from bad to ok.","criterion_ids":["AC-1"],"primary_symbol":"app.py::value","allowed_files":["app.py"],"forbidden_changes":["No API change."],"patch_budget":{"max_files":1,"max_changed_lines":10},"verification":{"commands":[["python","-c","from app import value; assert value() == 'ok'"]]},"risk":"low","review_required":true,"max_attempts":2}
```

Use only a disposable repository such as `/tmp/local-first-smoke-repo` and a harmless one-line change.

## Commands

Repository allowlist configuration: `--repository /tmp/local-first-smoke-repo --allow-repository /tmp/local-first-smoke-repo`

Read-only import: `python -m local_first_orchestrator.cli --database /tmp/local-first-smoke-ledger.db --repository /tmp/local-first-smoke-repo import --task-id <CARD_ID>`

Dry run: `python -m local_first_orchestrator.cli --database /tmp/local-first-smoke-ledger.db --repository /tmp/local-first-smoke-repo run-once --task-id <LOCAL_TICKET_ID>`

Write-enabled single-card run: `python -m local_first_orchestrator.cli --database /tmp/local-first-smoke-ledger.db --repository /tmp/local-first-smoke-repo --allow-repository /tmp/local-first-smoke-repo run-once --task-id <LOCAL_TICKET_ID> --execute --allow-board-writes`

Ledger inspection: `python -m local_first_orchestrator.cli --database /tmp/local-first-smoke-ledger.db inspect --task-id <LOCAL_TICKET_ID>`

Git branch/commit inspection: `git -C /tmp/local-first-smoke-repo branch --all; git -C /tmp/local-first-smoke-repo log --oneline --all --decorate`

Hermes-card inspection: `hermes kanban show <CARD_ID> --json`

## Pause/recovery and rollback

Pause: stop invoking write-enabled `run-once`, then use the operator pause command when available. Recovery requires inspecting the ledger active stages, worktree path, recorded base SHA, and diff hash before resuming. Unknown model outcomes are reconciled manually and are never retried automatically.

Safe rollback: do not reset the default branch. Revert the accepted `local-first/<ticket-id>/attempt-*` commit in the disposable repository, inspect the ledger and Hermes card, and block the card if the external projection is inconsistent. Remove the disposable repository only after the ledger and card evidence have been retained.
