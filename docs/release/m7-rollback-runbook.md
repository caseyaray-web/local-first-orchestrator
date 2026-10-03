# M7 rollback package and reconciliation runbook

**Status: fixture rehearsal procedure, not a live restoration command.** The M7 fixture package captures only a disposable directory-plugin artifact, a fixture trusted-bootstrap JSON file, and a transactionally consistent fixture plugin-owned `evidence.sqlite3`. It does not capture, read, modify, or restore Hermes profiles, secrets, services, providers, `kanban.db`, board state, or an installed plugin.

## Fixture archive contract

The test-only helper is `tests/fixtures/m7_rollback.py`. It is deliberately not shipped in the wheel and fails closed unless every source, archive, and restore destination is under one caller-provided fixture root.

Inputs:

1. a fixed allowlist of plugin directory artifact files (including `plugin.yaml`, root `__init__.py`, dashboard assets, and artifact-local Python package files);
2. one named trusted-bootstrap JSON file;
3. one existing, plugin-owned `evidence.sqlite3` validated by the public `EvidenceStore` schema checker;
4. a closed checkpoint object with exact scope, cutoff, native-effect status, installed-snapshot status, operator-intent status, and `fixture-only` restore policy.

The helper validates the source database with `EvidenceStore`, uses that store connection's SQLite backup API to produce a consistent copy, validates the copied database again, then hashes every archived file. It intentionally archives the SQLite backup file rather than a live `-wal` sidecar. It rejects caller-supplied source symlinks before path resolution, symlinked allowlisted plugin inputs, traversal and duplicate plugin allowlist paths, source/destination escape from the fixture root, unknown/missing checkpoint fields, existing restore destinations, unsafe or duplicate archive members, inventory drift, restored-file hash mismatch, and checkpoint mismatch at restore.

The archive metadata includes file hashes and this capture declaration:

```text
sqlite-backup-api-consistent; wal-sidecars-not-copied
```

No signature or new key authority is created: integrity is recorded as SHA-256 evidence for review, not a claim of independent authorization.

## Exercised disposable package

Run only in the isolated checkout:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI='' \
  uv run --offline --no-project --with pytest --with jsonschema \
  python -m pytest tests/test_m7_release_rollback.py -q -pno:cacheprovider -oaddopts=''
```

The focused file currently has seven fixture-only cases. They build an archive under pytest's scratch-root fixture, restore it to a new fixture-only directory, and prove:

- trusted config bytes and **every** allowlisted plugin artifact byte match after restore;
- a public `EvidenceStore` reopen/migration accepts the restored SQLite database and retains the active paused/stopped operator intent, member history, and immutable budget event;
- archive SHA-256 is stable across build/restore reporting;
- unknown and missing checkpoint keys plus a different cutoff are refused;
- duplicate/traversing allowlists and caller-supplied config/plugin-file symlinks are rejected at the intended build boundary, including a symlinked `dashboard/` ancestor for the allowlisted `dashboard/manifest.json` node before resolution can erase that fact;
- an archive with a duplicate member is rejected before restoration.

This is proof of the archive protocol only. It is not proof that a target package has been installed, an installed legacy plugin was backed up, a native board can be rolled back, or a live service can be stopped.

## Authorized target capture procedure (future; do not run under this M7 task)

**STOP — requires explicit target authorization and a reviewed release artifact.** Before any target write:

1. Perform read-only inventory of the target plugin directory metadata and `plugin.yaml` hash. The current installed directory may be a protected legacy checkout and differs from the release artifact; do not edit it or use it as an assumed rollback package.
2. Identify the exact plugin-owned evidence database and named trusted bootstrap selected by the operator. Do not glob/copy a Hermes home or read `.env`/profile configuration.
3. Pause and contain the selected loop through a supported seam; read back scoped state, exact run IDs, receipts, and containment result. A partial or unknown stop is a hard stop.
4. Capture plugin files, the selected bootstrap, and plugin-owned evidence with the same checked archive contract in a target-local approved backup directory. Record archive/file/config hashes, evidence schema/integrity result, scope, cutoff, and captured operator intent.
5. Read the archive back and validate it before changing plugin files/configuration. Do not start the coordinator as validation.

The target capture must preserve a concrete “prior installed snapshot” only when actually captured and hash-verified. Until then, the honest value is `not captured`.

## Rollback decision and reconciliation

### Before native enrollment/effects

**STOP — operator authorization required.** After confirmed containment, an approved captured package may restore the selected plugin files, bootstrap and plugin-owned evidence into the authorized target. Verify hashes, `EvidenceStore` integrity/schema, intended configuration state, disabled/paused loop state, and no native changes. This restores package-owned files; it does not validate host service management automatically.

### After enrollment or any native/provider effect

**STOP — no automatic restore.** A file/database archive is not a board rollback. It cannot undo native task creation, cards, links, comments, claim/runs, role activity, dispatch, provider charges, or remote effects. Do not overwrite plugin files and then call the system restored.

Perform a separate, reviewed manual reconciliation while paused:

1. preserve the selected rollback archive and current plugin evidence;
2. use documented read-only native `show --json` and `runs --json` on the exact authorized board/task IDs, plus exact stored operation markers/receipts;
3. classify each expected operation as proven applied, proven absent, unknown, partial containment, or conflicting—without resending an unknown operation;
4. compare receipt scope, immutable candidate/operation identity, run/session/profile evidence, and current native membership before proposing any compensation;
5. write a bounded operator decision for each discrepancy. Any desired board mutation, provider remediation, migration, or package swap requires a separately authorized procedure and fresh review.

Do not import an old ledger, infer receipts from card status, replay a missing effect, or use direct native SQLite writes. Keep `unknown`, `partial`, and `uncontained` visible until an authorized readback proves otherwise.

## Explicit non-goals

This runbook does not authorize plugin installation, Hermes restart/reload, systemd/service control, profile modification, real-board enrollment, provider use, legacy-ledger migration, board backup, board rollback, data deletion, or automatic post-effect reconciliation.
