# M0 native compatibility and release notes

**Status:** M0 host compatibility gate characterized in an isolated development worktree. This is **not** plugin runtime readiness, installation, enrollment, or a claim that M1–M7 are implemented. Unsupported atomic controls and their explicit fallbacks are listed below.

## Target and isolation

- Installed CLI inspected: Hermes Agent `v0.21.5+3816.gbac0c45` (upstream `bac0c45d`), from `hermes --version` at inspection. The launcher also reported its installation out of sync with `uv.lock` and attempted an interrupted source-update completion; the native fixture tests used the pinned installed executable path instead of the launcher.
- Source: `/home/ocadmin/.hermes/hermes-agent/hermes_cli/kanban_db.py` and `kanban_parser.py`; tests run under this host installation without modifying its source.
- **Critical isolation discovery:** `HERMES_HOME` alone does **not** redirect the shared Kanban root. `kanban_db.py::kanban_home` uses `HERMES_KANBAN_HOME` or the default root, independently of profile home. A read-only `boards list` with just `HERMES_HOME` exposed the real default-board counts. No write was made in that attempt. Set **both** `HERMES_HOME` and `HERMES_KANBAN_HOME` to the same disposable directory; clear inherited `HERMES_KANBAN_DB` and per-board root overrides; use explicit `--board m0-fixture` and assert the created DB path is under the disposable directory before any native mutations. Never use the default board for this test.
- No native worker model was executed: the dispatcher was exercised only with a process-stub `spawn_fn` or `dispatch --dry-run --json`. Stub child processes were stopped and reaped in test cleanup. No provider calls. An isolated fixture plugin registered a real completion observer to measure event order; no host source or installed plugin was changed. The creation-race test uses a process-local, test-scoped barrier at the native ID seam to force both callers past the non-transactional key lookup; it does not alter installed host files or runtime processes.

## Verified with isolated native tests

`tests/test_board_compatibility.py` is gated by `HERMES_M0_CLI` (an explicit path to the installed executable); without this env var the native tests skip, which **must not** be represented as a pass. The fixture sets a disposable Kanban root and creates a named board.

- `create --initial-status blocked --json` creates a blocked card with no run; it does not appear in a dry-run dispatch preview. `block`/`unblock` moves a ready card to blocked and back, and a dependent to `todo` while its parent is open.
- Both `complete --result ...` and `archive` promote a waiting dependent to `ready`. Completion from blocked/ready without result/summary evidence is rejected on this host. This prevents treating any lane change as review approval.
- The isolated fixture plugin observed that a completion hook sees the dependent already `ready`: in host `complete_task`, `recompute_ready` and workspace cleanup occur before `_fire_task_hook`. A post-commit hook cannot veto downstream eligibility.
- A normal `claim` command refuses a waiting `review` card (`status=review`); the dispatcher has a separate reviewer path. The isolated fixture invoked native `claim_review_task` without a model and observed a new `running` review run with the reviewer profile and review-sourced claimed event. `request-changes` on a blocked card refuses; with an active review run, a mismatched expected run ID refuses and leaves the card running.
- Sequential `create --idempotency-key ...` returns the original unarchived task identity. A forced simultaneous native create using independent DB connections produced distinct cards for the same key: the lookup is before the write transaction, so creation must be serialized and deduplicated by the plugin.
- A native dispatcher tick with a stub process confirmed a hold race: blocking after claim parks the board card but leaves the worker process alive. Native `reclaim_task` stopped a correctly fingerprinted local stub, but returned its card to `ready`, not a sticky hold; the next independent dispatcher tick could relaunch it. Exact stop-and-hold must therefore be treated as partial unless readback proves containment.
- A fixture plugin registered completion and pre-tool hooks in two separate isolated profile homes and unloaded cleanly. `model_tools.handle_function_call` was invoked without a model; its real pre-tool path returned the fixture block before a nonexistent completion could write. This proves the host plugin loading/interception surface, not loading of the *future replacement plugin* (which does not yet exist). Hook fire-site behavior was exercised on actual isolated completion.

## Operation surface and unsupported assumptions

`kanban create` supports `--initial-status blocked`, `--idempotency-key`, `--json`; `show`, `list`, `runs` support `--json`. The inspected CLI usage/help blocks are captured in [compatibility-cli-help.md](compatibility-cli-help.md); the canonical scenario-to-test matrix is [m0-coverage-matrix.md](m0-coverage-matrix.md). Native `comment`, `block`, `unblock`, `request-review`, `request-changes`, `reopen-review`, `link`, `complete`, `archive`, and `reclaim` do not promise JSON output. Apply via supported CLI, then read back `show --json` and `runs --json` on the exact board/task. `request-review --reviewer` and `--metadata` exist; `request-changes` is a same-card active-review verdict, not a sibling-card power. `reclaim TASK_ID` is not established as an exact-run stop; do not equate claim release with process death. `--force` on completion/review can override a live-claim guard and must not be the default plugin path.

**Fallback if a primitive remains unsupported:** never write native SQLite or monkey-patch the host. Use a separate held review/correction card for an illegal same-card transition; for an unverified stop, return a partial/uncontained pause with exact observed run identity and operator recovery. Serial creation and marker/readback are required because sequential idempotency is not concurrency safety.

## Verification

From the isolated `m0-compatibility` worktree, with `HERMES_M0_CLI` set to the pinned installed `venv/bin/hermes` (not the source-update launcher):

```sh
HERMES_M0_CLI=/home/ocadmin/.hermes/installs/a7aef1ff6a8fec87/environments/0a783c660bcc4d4d956dd71c2c88fbe4/venv/bin/hermes uv run --no-project --with pytest python -m pytest tests/test_board_compatibility.py -q -o addopts=
python -m py_compile tests/test_board_compatibility.py
git diff --check
```

The fixture suite returned **16 passed**; Python compilation and diff check passed. `test_documented_cli_help_matches_target_host` compares the captured usage/help blocks to the live target binary, making host drift visible. If `HERMES_M0_CLI` is unset, the native tests skip; do not call that a pass. Actual provider requests and production board writes are not part of this suite.

## M0 conclusion and next-stage constraints

The target host exposes enough supported board operations for an **observational, recoverable plugin** but does not offer atomic read-bound mutations, concurrent create-once, or exact-run stop-and-hold. These are capability limits, not missing M0 tests. The replacement must use the following explicit fallbacks:

- Create held with `--initial-status blocked`, verify the exact card, and serialize managed creations in a plugin-owned coordinator lock. After uncertain creation, reconcile the marker/idempotency key and inspect duplicates before retrying. A native key alone is insufficient.
- For a queued hold, re-read the exact task/run. If a claim won the race, report active/uncontained work and attempt supported reclaim; never claim a preventive hold from an observation. An existing native dependent can start before a completion hook reacts.
- For stop, bind the requested task to the observed run and host-local PID fingerprint before reclaim, then verify process exit and run/card readback. `reclaim TASK_ID` can release to `ready`/`review` and permit redispatch; it is not an atomic stop-plus-hold. If the identity changes, process survives, or immediate re-hold cannot be verified, return `partial`/`unsupported` and require operator recovery. Do not signal a bare or foreign PID.
- Use fresh replacement review/correction cards where native same-card ownership disallows a transition; do not reopen done ancestors as routine repair. No host SQL writes, monkey-patches in the replacement, privileged helper, or default-board fixture.

The next implementation milestones must test these policies end to end. Fixture plugin hook discovery does not prove the not-yet-written replacement plugin is loaded; M6 will verify its actual packaging and profile registration. M0 authorizes no M1/M2 runtime automation or real board enrollment by itself.
