# M0 native compatibility and release notes (in progress)

**Status:** partial M0 characterization in an isolated development worktree. No plugin runtime has been changed, installed, reloaded or enrolled. This is not an M0 pass, a release, or permission to operate the real board.

## Target and isolation

- Installed CLI inspected: Hermes Agent `v0.21.5+3816.gbac0c45` (upstream `bac0c45d`), from `hermes --version` at inspection. The launcher also reported its installation out of sync with `uv.lock` and attempted an interrupted source-update completion; the native fixture tests used the pinned installed executable path instead of the launcher.
- Source: `/home/ocadmin/.hermes/hermes-agent/hermes_cli/kanban_db.py` and `kanban_parser.py`; tests run under this host installation without modifying its source.
- **Critical isolation discovery:** `HERMES_HOME` alone does **not** redirect the shared Kanban root. `kanban_db.py::kanban_home` uses `HERMES_KANBAN_HOME` or the default root, independently of profile home. A read-only `boards list` with just `HERMES_HOME` exposed the real default-board counts. No write was made in that attempt. Set **both** `HERMES_HOME` and `HERMES_KANBAN_HOME` to the same disposable directory; clear inherited `HERMES_KANBAN_DB` and per-board root overrides; use explicit `--board m0-fixture` and assert the created DB path is under the disposable directory before any native mutations. Never use the default board for this test.
- No native worker dispatch was executed: `dispatch --dry-run --json` only. No provider calls. A fixture test calls public host functions on an isolated DB with a patched completion hook to observe event order; it does not modify host source or the real native DB.

## Verified with isolated native tests

`tests/test_board_compatibility.py` is gated by `HERMES_M0_CLI` (an explicit path to the installed executable); without this env var the native tests skip, which **must not** be represented as a pass. The fixture sets a disposable Kanban root and creates a named board.

- `create --initial-status blocked --json` creates a blocked card with no run; it does not appear in a dry-run dispatch preview. `block`/`unblock` moves a ready card to blocked and back, and a dependent to `todo` while its parent is open.
- Both `complete --result ...` and `archive` promote a waiting dependent to `ready`. Completion from blocked/ready without result/summary evidence is rejected on this host. This prevents treating any lane change as review approval.
- The completion lifecycle hook observes the dependent already `ready`: in host `complete_task`, `recompute_ready` and workspace cleanup occur before `_fire_task_hook`. A post-commit hook cannot veto downstream eligibility.
- A normal `claim` command refuses a waiting `review` card (`status=review`); the dispatcher has a separate reviewer path. The isolated fixture invoked the native `claim_review_task` operation without spawning a worker and observed a new `running` review run with the reviewer profile and a claimed event sourced from review. `request-changes` on a blocked card refuses for lack of an active review run.
- Sequential `create --idempotency-key ...` returns the original unarchived task identity. This test does **not** prove concurrent creation exactly-once.

## Operation surface and unsupported assumptions

`kanban create` supports `--initial-status blocked`, `--idempotency-key`, `--json`; `show`, `list`, `runs` support `--json`. Native `comment`, `block`, `unblock`, `request-review`, `request-changes`, `reopen-review`, `link`, `complete`, `archive`, and `reclaim` do not promise JSON output. Apply via supported CLI, then read back `show --json` and `runs --json` on the exact board/task. `request-review --reviewer` and `--metadata` exist; `request-changes` is a same-card active-review verdict, not a sibling-card power. `reclaim TASK_ID` is not established as an exact-run stop; do not equate claim release with process death. `--force` on completion/review can override a live-claim guard and must not be the default plugin path.

**Fallback if a primitive remains unsupported:** never write native SQLite or monkey-patch the host. Use a separate held review/correction card for an illegal same-card transition; for an unverified stop, return a partial/uncontained pause with exact observed run identity and operator recovery. Serial creation and marker/readback are required because sequential idempotency is not concurrency safety.

## Remaining M0 gates

- Test review dispatch/run provenance with a stubbed worker spawn under a provably isolated home; normal `claim` cannot model this.
- Exercise a live/queued hold race and exact-run reclaim/stop verification, including a failure-to-contain result.
- Reproduce concurrent native creation race deterministically or establish its limitation with a stress/controlled fixture; do not claim the idempotency key is atomic from sequential tests.
- Test hook loading and readback on the target plugin registration path without starting a real gateway or model.
- Add fixture adapter/fault-injection coverage for read/write ambiguity and a capability fingerprint independent of private source paths.

Until these are verified, M0 remains **open**; no M1/M2 runtime automation or real board enrollment is authorized by this work.
