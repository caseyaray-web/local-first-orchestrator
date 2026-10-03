# M7 operator cutover checklist

**Status: release-readiness checklist only.** This file authorizes no installation, enablement, plugin reload, service restart/replacement, profile or provider change, ledger migration, board enrollment, dispatch, or native effect. Each **STOP** needs a separate operator authorization recorded for the selected target.

## Release package gate

- [ ] Pin the independently reviewed commit and exact wheel/sdist hashes. The current source fixed point is not a substitute for an approved release artifact.
- [ ] Run the complete fixture acceptance suite, package build, built-artifact import/discovery check, compatibility report, and the rollback rehearsal. Record commands, pass/fail/skip results and artifact hashes.
- [ ] Confirm the package contains the documented directory-plugin payload: `plugin.yaml`, root `__init__.py`, `dashboard/manifest.json`, `dashboard/plugin_api.py`, `dashboard/dist/index.js`, and `python/local_first_orchestrator/`.
- [ ] Confirm the target Hermes version/capabilities against `docs/compatibility-and-release.md`; do not infer support from a source-tree test.
- [ ] Obtain fresh independent code and specification review for the same commit and artifact hashes. A prior M6 review is historical evidence, not M7 acceptance.

**STOP — release approval required.** Do not install or copy the package until the operator approves this exact release artifact and target.

## Target inventory and backup planning (read-only)

- [ ] Identify the target `HERMES_HOME`, target Python environment, active Hermes executable/version, configured board/anchor scope, and any service/loop ownership. Do not read or archive a full profile configuration or `.env`; those can contain secrets.
- [ ] Inspect only installed plugin metadata needed to identify the current directory plugin: directory path, `plugin.yaml` hash, and file inventory. Do not treat a source checkout as the installed plugin.
- [ ] Record whether the installed plugin is unrelated to this release. It is not a proven rollback target merely because it exists.
- [ ] Select a **prior installed snapshot** only if the operator has authorized a target-local capture and its plugin files, trusted bootstrap file, and plugin-owned evidence database can be captured consistently. Otherwise leave rollback target as `not captured`; do not invent a fallback package.
- [ ] Prepare the approved target-local backup location with access controls. The backup boundary includes only the selected plugin directory artifact, named trusted bootstrap configuration, and plugin-owned `evidence.sqlite3` captured through SQLite backup. It excludes Hermes `kanban.db`, board data, profiles, provider credentials, and unrelated home files.

**STOP — live backup authorization required.** The fixture archive proves archive/restore mechanics only. It is not a backup of the installed plugin or any real state.

## Pre-activation cutover (after separate authorization)

- [ ] Pause/contain the existing authorized loop through its supported operator seam and read back its status. If exact stopped-run identity cannot be established, retain `partial/uncontained` and do not swap files.
- [ ] Capture and verify the authorized rollback package. Record its manifest, archive hash, config hash, plugin file hashes, SQLite integrity/schema validation, scope/cutoff, and active operator intent.
- [ ] Install only the approved wheel payload using the documented route: place its `*.data/data/local-first-orchestrator/` directory intact at `$HERMES_HOME/plugins/local-first-orchestrator/`; do not copy checkout files. Run `hermes plugins doctor "$HERMES_HOME/plugins/local-first-orchestrator" --ci` before enablement.
- [ ] Explicitly enable only the selected plugin in the selected target configuration, then perform a separately authorized reload/restart if required by the host. Read back discovery and registration.
- [ ] Confirm ordinary startup opens an existing plugin-owned evidence store and does not create/migrate one. Confirm no board enrollment, provider request, dispatch, or loop start has occurred.

**STOP — activation authorization required.** Plugin discovery/enablement is not enrollment or runtime authority.

## Disposable native proving gate (after separate authorization)

- [ ] Create/enroll only an operator-approved disposable/sample board or tranche with isolated state.
- [ ] Verify native runtime status and both configured implementation and local-review roles on the disposable scope. Record exact run/session/profile evidence and readbacks.
- [ ] Exercise pause/stop/reconcile/recovery according to documented partial/unknown behavior; do not turn unknown native effects into replay authority.
- [ ] Keep provider spend and any real board expansion disabled unless separately approved.

**STOP — expansion authorization required.** No production board enrollment, migration, provider execution, or scope expansion follows automatically from the disposable proving gate.

## Cutoff decision matrix

| Cutoff | Permitted package action | What cannot be claimed | Required next action |
| --- | --- | --- | --- |
| Before activation/effects | Restore the approved plugin files, trusted bootstrap, and consistent plugin-owned SQLite archive into the authorized target after containment. | No restoration of board/native state is needed or proved. | Verify package hashes, store schema/integrity, and disabled/paused target state. |
| Enabled, no enrollment/effects | File/config/store restoration is possible only after target containment and scope/readback verification. | Enablement rollback does not prove service/process containment. | Keep plugin disabled and verify no loop/board change before retrying activation. |
| Enrollment or native effects exist | **No automatic rollback.** Do not overwrite files and declare the board restored. | Plugin archive cannot undo native cards, runs, edges, comments, provider requests, or external effects. | Pause/contain; perform read-only receipt/run/card reconciliation by exact IDs; write a separately reviewed manual reconciliation plan. |
| Unknown/partial stop or receipt | **No swap and no replay.** | No proven safe rollback point exists. | Preserve evidence, keep the loop paused, and escalate to the authorized operator. |

## Final record

Record: authorization reference; commit/artifact hashes; exact target metadata (no secrets); installed plugin manifest hash before/after; rollback archive hash and manifest; selected cutoff; target scope; containment readback; native proof identifiers; and every remaining partial/unsupported result. “Checklist complete” is not a GO unless the independent reviews and the operator’s relevant STOP authorizations are present.
