# M6 focused fixture regressions

**Scope:** test and documentation evidence only. These cases use disposable Git repositories, SQLite stores, loopback-only HTTP, and explicitly injected fixture boards. They do not install or activate the plugin, access host board/provider state, or alter production runtime code.

## Regression nodes

1. `tests/test_m6_regressions.py::test_m6_rejected_registered_tool_runtime_is_closed_before_a_later_valid_call` invokes the **registered** `local_first_submit_plan` handler with a missing native-worker environment, then calls the actual registered status handler. It counts constructed and closed composed runtimes and checks a later singleton-lock acquisition. The assertion is intentionally red on the current runtime: `plugin_tools._runtime_for_args()` creates the runtime before validating worker environment, while the handlers only enter `try/finally` after that validation.
2. `tests/test_m6_regressions.py::test_m6_cli_rendered_outcomes_close_fixture_runtime` and `::test_m6_cli_exception_closes_runtime_and_returns_invalid_json` exercise `cli.main`/`run_command` with an actual temporary production-built Git/SQLite runtime and an explicit fixture board. They verify JSON/exit mappings for `partial` (4), `held` (3), and `conflict` (5), plus invalid JSON/exit 2 after a coordinator exception, store close, and lock reacquisition.
3. `tests/test_m6_regressions.py::test_m6_real_cli_loop_sigterm_restarts_with_persisted_pause` launches the real CLI `run` path in bounded subprocesses. It preconfigures only a fixture board, sends `SIGTERM`, verifies handler restoration and lock reacquisition, restarts with `run --once`, and reads the same temporary SQLite evidence store to retain operator pause and operation evidence. `HERMES_M0_CLI=''` is explicit for every child.
4. `tests/test_m6_browser_bundle.py::test_m6_actual_dashboard_bundle_partial_stop_and_stale_error_retention` runs the shipped `dashboard/dist/index.js` in a real React 19.2.7 root and headless Chromium. `tests/fixtures/m6_browser/server.py` serves that unchanged bundle and proxies only loopback API calls into `tests.test_operator_api.mounted_api.__wrapped__`; the mounted fixture owns its temporary Git repository, SQLite store, and fake board.

The browser driver proves all of the following on the rendered page:

- **Partial stop:** clicks **Pause and stop**, observes the actual HTTP 200 action response with `outcome: partial`, and retains visible `run-1` uncontained-worker evidence.
- **Stale action:** changes only the browser request's expected digest to zeros after the successful observation, observes the actual HTTP 409, waits for the bundle's follow-up HTTP 200 status refresh, verifies the visible 409 error remains and **Pause** is enabled, and reads fixture-only board writes before/after to prove no stale mutation.
- **Actual integration:** asserts the React SDK runtime is `19.2.7`; the harness imports `react` and `react-dom/client`, bundles it with `esbuild`, and waits for the actual plugin registration rather than using a string/static-DOM substitute.

## Browser fixture dependencies

`tests/fixtures/m6_browser/package.json` records the pinned, test-only dependencies:

- `playwright` 1.58.2
- `react` and `react-dom` 19.2.7
- `esbuild` 0.27.3

Do not commit `node_modules`, a browser cache, or a package lock to this Python/plugin checkout. Provision them in a disposable cache outside the checkout (the example uses Hermes scratch):

```sh
prefix="$HOME/.hermes/cache/scratch/m6-browser-node"
mkdir -p "$prefix"
npm install --prefix "$prefix" --no-save \
  playwright@1.58.2 react@19.2.7 react-dom@19.2.7 esbuild@0.27.3

# If the platform is supported by Playwright, keep browser downloads isolated too:
PLAYWRIGHT_BROWSERS_PATH="$HOME/.hermes/cache/scratch/m6-browser-browsers" \
  "$prefix/node_modules/.bin/playwright" install chromium
```

Use either Playwright's cached Chromium (with the same `PLAYWRIGHT_BROWSERS_PATH`) or an explicitly configured executable. Nothing is installed globally and the test never downloads packages or browsers itself:

```sh
export M6_BROWSER_NODE_MODULES="$HOME/.hermes/cache/scratch/m6-browser-node/node_modules"
export M6_BROWSER_EXECUTABLE=/path/to/chrome-or-chromium   # omit when Playwright cache is available
PYTHONPATH=. uv run --no-project --with pytest pytest -q tests/test_m6_browser_bundle.py
```

The node modules and an executable browser are capability prerequisites. If either is actually absent, the node is conditionally skipped with its missing-prerequisite reason; it is not an unconditional placeholder skip. The fixture launcher has no hard-coded home/scratch path, uses only arguments/environment plus `TemporaryDirectory`, binds HTTP only to `127.0.0.1`, and has bounded process cleanup.

## Current execution status

The tool-runtime regression is deliberately failing and is retained as evidence of a production lifecycle defect. Test-only authorization does not permit repairing `local_first_orchestrator/plugin_tools.py`. Browser availability and execution must be reported separately from that faithful red regression.
