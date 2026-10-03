# M6 implementation progress

**Status:** earlier M6 review evidence is historical and superseded by the current dashboard completion. The current isolated implementation awaits renewed independent review. This checkout has not been installed, enabled, enrolled against a live board, dispatched against a provider, or pointed at production Hermes state. M7 host activation/cutover remains paused.

## Implemented isolated surfaces

- [x] **Strict composition:** `PluginConfig` accepts only trusted local bootstrap values for the private state root, Hermes executable/home, trusted repository/workspace roots, distinct role profiles, fixed budgets, check commands, and one board/anchor scope. Browser, tool, and CLI request data cannot select them.
- [x] **Explicit store lifecycle:** ordinary `build_runtime()` opens an existing configured store only and fails closed when it is absent. `initialize_store()` is the sole explicit bootstrap/migration path; it does not enroll or perform native work.
- [x] **Runtime assembly:** `build_runtime()` constructs the existing `Coordinator`, `EvidenceStore`, singleton lock, budget policy, roles, and default `HermesBoardAdapter`. A board fake requires explicit test injection.
- [x] **CLI:** `local_first_orchestrator.cli` provides `status`, `initialize-store`, `enroll`, `pause [--stop]`, `reconcile`, `resume [--authorized-clear]`, `cancel`, `recover`, and `run [--once]`. It renders JSON outcomes with exit codes 0/2/3/4/5/6/7 for verified-or-recorded/invalid/held/partial/conflict/unsupported/unknown.
- [x] **Plugin registration:** the root entrypoint registers the native `local-first-orchestrator` CLI command, five structured tools, and the advisory `pre_tool_call` hook without configuration loading, SQLite access, board writes, provider calls, or coordinator start.
- [x] **Tools and hook:** registered tools are `local_first_submit_plan`, `local_first_submit_review`, `local_first_request_corrections`, `local_first_report_issue`, and `local_first_status`. They enforce composed scope; mutation tools require native worker task/run/session/board environment. The hook performs lazy configured membership lookup only when a managed implementation completion attempt is intercepted.
- [x] **Dashboard:** the mounted API exposes scoped status, trusted configured role profiles, configured-anchor enrollment, bounded configuration tightening, and `pause`, `stop`, `reconcile`, `resume`, `cancel`, and `recover` actions. Mutations require a current observation digest; configuration saves require their own digest and cannot alter trust boundaries, profiles, scopes, or increase budgets. The React bundle preserves an action error after its refresh instead of clearing it.
- [x] **Packaging/docs:** package assets declare the root plugin registration, manifest, dashboard API/bundle, and standalone CLI. The operator guide documents explicit bootstrap rather than implicit migration.

## Executed isolated evidence

- `tests/test_m6_composition_plugin_surfaces.py` exercises explicit store initialization, composition, explicit fake injection, registration without composition, strict configuration, package asset declarations, and temporary trusted-Git/check callbacks.
- Both independent acceptance reviews ran the pre-follow-up replacement suite: **1088 passed, 85 skipped, 0 failed**.
- After the portability follow-up, the replacement suite was rerun with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI='' uv run --offline --no-project --with pytest --with jsonschema python -m pytest -q -p no:cacheprovider -o addopts='--tb=short' tests/`: **1092 passed, 85 skipped, 0 failed**. The four additional cases cover executable selection through explicit configuration/PATH and actionable failure for missing or invalid configuration.
- The artifact regression uses executable `HERMES_TEST_CLI` when explicitly set, otherwise `hermes` from `PATH`; an invalid override fails rather than falling back or skipping discovery. The actual artifact test passed using the installed CLI on `PATH`. To pin a CLI in CI, set `HERMES_TEST_CLI=/absolute/path/to/hermes`. Temporary-home, installed-wheel import isolation, and native Plugin Doctor assertions remain intact.
- The parent mounted `dashboard/dist/index.js` with React 19.2.7 and a fixture Hermes SDK against the isolated dashboard API. A stop returned HTTP 200 with `partial`, `containment_unverified`, and one retained uncontained `run-1`; the rendered result remained visible. A deliberately stale pause produced actual POST 409 then GET 200, kept the error visible after refresh, enabled controls, and made no mutation. This is fixture browser proof only.

## Remaining gates

- [ ] **Renewed independent M6 review:** required after the dashboard profiles/configuration/enrollment completion; the former GO is historical, not acceptance of this current diff.
- [ ] **Host activation/cutover (M7, paused):** prove the packaged plugin in an authenticated host with the authorized installation/profile/board configuration. This includes neither live-provider execution nor production board enrollment today.
- [ ] **No implied migration:** existing boards, stores, worktrees, profiles, and provider state remain untouched; any installation, enrollment, dispatch, or activation requires separate authorization.
