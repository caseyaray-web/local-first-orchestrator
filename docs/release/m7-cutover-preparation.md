# M7 cutover preparation: source candidate and bootstrap decisions

**Status: PRE-CUTOVER CANDIDATE — NO-GO for installation, activation, migration, restart, or data capture.** This record is preparation evidence only. It neither approves nor performs a live operation.

## Source-bound candidate

The candidate is an uncommitted, immutable-source snapshot formed from Git base `f768c12895856f018b714a4f56a7a8a8d0a3286b` plus exactly these reviewed working-tree paths:

1. `dashboard/dist/index.js`
2. `dashboard/plugin_api.py`
3. `docs/operator-guide.md`
4. `tests/fixtures/m6_browser/driver.cjs`
5. `tests/test_operator_api.py`

It excludes pre-existing `build/`, `local_first_orchestrator.egg-info/`, and `uv.lock`, and excludes this preparation record and generated reports so no report certifies itself. The candidate is not a Git commit and is not an approved release artifact.

Durable artifact copies (outside scratch retention):

```text
/home/ocadmin/.hermes/release-artifacts/local-first-orchestrator/m7-dashboard-precutover-f768c128-20261003/
```

The base archive, overlay manifest, verification report, Plugin Doctor output, sdist and wheel were copied byte-for-byte and their SHA-256 values rechecked. The preserved report retains the original build/verification paths below; it is not a new verification run.

Original private build/verification directory:

```text
/home/ocadmin/.hermes/cache/scratch/durableprofile/m7-dashboard-precutover-f768c128-20261003/
```

| Item | SHA-256 |
| --- | --- |
| Base `git archive` | `9ddd2acfd9d9864b7e24da0cb9b5c7046ea90ee9fa60dbbc771f39b09a0559c4` |
| Five-path source-overlay manifest | `5cc904dcadbf34dffb5bf986390687ff19a5c498e5e8e093cbb6cccb3af5d624` |
| sdist | `be41f903ebcb44ed9992064f8eb3f5d43ac61d6d766e8adf833931dec8279004` |
| wheel built from that sdist | `4389039bd29db3f6493c4ac4f9a80508225b6a4ed26c7aae1e2d4feb6bea3067` |

`source-overlay-manifest.json` records the base and overlay SHA-256 for all five paths. `verification-report.json` records artifact and validation results; neither belongs to the candidate source snapshot.

## Isolated package verification

The sdist was built from the archive-plus-overlay snapshot; the wheel was then built from that sdist, not from the checkout. In a new scratch virtual environment, `python -I` imported all 30 distributed `local_first_orchestrator` modules. A temporary `HERMES_HOME` reconstructed only the wheel's directory-plugin payload and native `hermes plugins doctor … --ci` read back **5 tools, 1 hook, and the native CLI**.

The verification used `HERMES_M0_CLI=''`; it did not invoke mutable native-fixture cases. No installed plugin, real Hermes home, board, provider, profile, service, or evidence store was used or changed.

## Read-only target gap

The target directory plugin remains untouched at:

```text
/home/ocadmin/.hermes/plugins/local-first-orchestrator
```

Its observed Git `HEAD` is `029da43a75d7629f09436703dcec82d787e05ebd`; its `plugin.yaml` SHA-256 is `fe03189c11ddebb5a665ae29ef83327e7653f683bb08f7cd5f667c480b31c600`. It is a dirty legacy checkout: `README.md` modified; seven legacy documentation files deleted; `.hermes/` and `.worktrees/` untracked. It imports the legacy `Ledger`, `LocalFirstController`, `OperatorConfig`, and `RuntimeMetricsStore`; its root registration exposes only the old native CLI and no tools/hooks.

The candidate instead registers five tools, one advisory hook, and a native CLI; its runtime is driven by `PluginConfig` and opens a plugin-owned `EvidenceStore`. There is no compatible in-place configuration or data translation. No legacy configuration or database content was read. No active process matching the orchestrator name was observed. The dashboard service is independently active with `HERMES_HOME=/home/ocadmin/.hermes` and does not set `LOCAL_FIRST_ORCHESTRATOR_CONFIG`; that is not evidence that a legacy controller is paused or that a candidate store exists.

## Decisions required before any authorized cutover

1. **Legacy disposition:** choose either an authorized, consistent archive of the identified legacy plugin/config/store or explicit preservation in place without claiming it is rollback-ready. Do not copy/import its ledger, controller state, claims, or runtime metrics into the candidate.
2. **New-state identity:** select a new, private, existing, non-symlink `state_root` and confirm it is distinct from the legacy ledger and Hermes `kanban.db`. The candidate's sole store path will be `<state_root>/evidence.sqlite3`; ordinary startup opens it only and fails if it is absent. A future first creation is a separately authorized explicit `initialize_store` action, not a migration.
3. **Trusted bootstrap file:** choose one absolute, regular JSON path for `LOCAL_FIRST_ORCHESTRATOR_CONFIG`. It must specify exactly version `1`; `state_root`; absolute executable `hermes_executable`; `hermes_home`; `kanban_home`; canonical repository/workspace roots; four distinct role profile names; all five finite budget categories; bounded polling interval; exact `board_id` and `anchor_task_id`; and sorted absolute check-command argv lists. Browser/API requests cannot supply these values.
4. **Scope and profiles:** the operator must name the single disposable bootstrap scope and four distinct profiles (`implementation`, `local review`, `planning`, `paid review`). No default anchor, board, profile, provider, or budget is inferred from the legacy plugin.
5. **Data boundary:** choose **fresh candidate EvidenceStore only**. Preserve legacy data separately if authorized; do not migrate, bridge, replay, or treat it as candidate evidence. Existing board effects, claims, provider receipts, and historical acceptance remain legacy facts requiring separate read-only reconciliation if cutover later reaches that gate.
6. **Authorization sequence:** after renewed independent review, separately authorize target capture, installation/enablement, explicit new-store bootstrap, disposable native proving, and any scope expansion. None follows from the preceding step.

## Open gates

The authorized parent native-fixture run now passes **226 tests, 0 skips**, including all 15 formerly skipped native cases; see [node-level evidence](m7-native-fixture-verification.md). Real worker/provider execution remains unproven. Minimum Hermes version/platform support is unknown, and this candidate is uncommitted. Those limits and the bootstrap, backup and authorization decisions above remain open; fixture success does not authorize live cutover.
