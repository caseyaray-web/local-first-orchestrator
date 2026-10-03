# M7 isolated release-candidate compatibility report

**Release decision: NO-GO for production/cutover.** This is reproducible fixture and packaging evidence for an uncommitted working overlay. It does not authorize activation.

## Current source and artifacts

| Item | Value |
| --- | --- |
| Base branch / commit | `m4-tranche-workflow` / `0dacc97d2cdd2369519f43d60ef6168d1bb7ae84` |
| Remote `origin/m4-tranche-workflow` observed | `0dacc97d2cdd2369519f43d60ef6168d1bb7ae84` |
| Historical working source | Git archive of the base commit plus the explicit five-file overlay below; **not committed** |
| Historical overlay manifest | `/home/ocadmin/.hermes/cache/scratch/m7-current-packaging.asgwgfil/source-overlay-manifest.json`; SHA-256 `12a65742a833621092fcd4e515646a4756c78f8b8191b272b37eb52f729b656d` |
| Historical overlay files | `tests/fixtures/m7_rollback.py`, `tests/test_m7_release_rollback.py`, `docs/release/m7-rollback-runbook.md`, `docs/plans/m7-implementation-progress.md`, `docs/release/m7-acceptance-matrix.md` |
| Historical sdist | `/home/ocadmin/.hermes/cache/scratch/m7-current-packaging.asgwgfil/dist/local_first_orchestrator-0.1.0.tar.gz`; SHA-256 `6242a62a70fcdaec604a491f2ddf043873710f17a240ec0a77697f380feb4ceb` |
| Historical wheel from that sdist | `/home/ocadmin/.hermes/cache/scratch/m7-current-packaging.asgwgfil/dist/local_first_orchestrator-0.1.0-py3-none-any.whl`; SHA-256 `d34ffa39176fa4668f1289fdb052bd047372332c926d246d2d17a8b42e420cab` |
| Distributed Python modules | 30; the historical full suite passed the fresh-wheel temporary-home native Plugin Doctor import/registration test (five tools, one hook). The archive-overlay rebuild above was package construction only. |

The source snapshot was produced by `git archive 0dacc97…`, then overlaying only those five paths. It excludes the later consolidation tests and this changed report, so its manifest and package hashes are historical and must not be treated as fresh artifacts for the current overlay. This generated compatibility report and the manifest are expressly excluded from the snapshot so the report does not self-certify its own hash. The manifest records a SHA-256 for each included overlay file and, where applicable, its base-commit bytes. No statement here treats the overlay as a commit.

The package build commands were:

```sh
uv build --offline --sdist --out-dir "$OUT/dist" "$SNAPSHOT"
uv build --offline --wheel --out-dir "$OUT/dist" "$OUT/dist/local_first_orchestrator-0.1.0.tar.gz"
```

`$SNAPSHOT` was the archive-plus-allowlisted-overlay directory above. Pre-existing untracked `build/`, `local_first_orchestrator.egg-info/`, and `uv.lock` were neither overlaid nor used as source inputs.

## Full isolated fixture result

```sh
M6_BROWSER_NODE_MODULES=/home/ocadmin/.hermes/cache/scratch/m6-browser-node/node_modules \
M6_BROWSER_EXECUTABLE=/home/ocadmin/.hermes/tools/chromium-1208/chrome-linux64/chrome \
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI='' \
uv run --offline --no-project --with pytest --with jsonschema python -m pytest \
  -q -pno:cacheprovider -oaddopts='--tb=short' \
  --junitxml=/home/ocadmin/.hermes/cache/scratch/m7-current-junit.xml tests/
# historical pre-consolidation result: 1110 passed, 85 skipped in 208.53s
```

That command/result and the package hashes above are historical pre-consolidation evidence, not a claim about the current dirty overlay. The current whole-suite command used the same environment and returned **211 passed, 15 skipped in 92.49s** across **226 collected nodes**; JUnit is `/home/ocadmin/.hermes/cache/scratch/local-first-whole-suite-consolidated-m7-restart-recovery-final-junit.xml`. The 1195-node baseline, 1059-node first reduction, and prior 218-node / 203-pass result are historical; `docs/plans/test-suite-consolidation.md` maps the complete removed-file families to retained public and recovery proof. Scenario 4's retained node now closes/reopens EvidenceStore, constructs a fresh Coordinator, and proves the replacement/member/budget history survives release and a later no-op recovery. The browser capability was configured and the shipped-bundle test passed. The historical fresh-wheel temporary-home native Plugin Doctor import/registration test (five tools, one hook) is not fresh package evidence for this changed overlay. `HERMES_M0_CLI=''` intentionally left 15 retained mutable native-fixture cases skipped; these are unproven, not passes. The canonical matrix's current XML audit found its named nodes passed, but provider-backed model execution was neither configured nor attempted. The focused rollback file remains historical evidence (**7 passed in 1.89s**), including the `dashboard/` ancestor-symlink regression.

## Tested compatibility envelope

| Dimension | Actual result |
| --- | --- |
| OS | Linux `7.0.0-34-generic`, x86_64 |
| Python | `3.14.7` |
| uv | `0.11.14` |
| Node | `v26.7.0` |
| Hermes tested | `Hermes Agent v0.21.5+6146.g46904a3 (2026.9.24)`, upstream `46904a3b` |
| Browser capability | configured via the stated local node-modules and Chromium paths |
| macOS / Windows | untested; no compatibility claim |
| Minimum Hermes version | unknown; one Linux host is not a version matrix |

No assertion is made for other Hermes versions or hosts.

## Remaining release gates

1. Renew independent code and specification reviews against this exact working overlay/artifact evidence.
2. Run approved native mutable-fixture verification in its authorized parent context and report every pass, skip, and unrun required node separately.
3. Establish an authorized version/platform matrix before claiming a minimum Hermes version or non-Linux support.
4. Obtain explicit target-capture and cutover authorization before any installation, enrollment, native effect, provider action, service operation, migration, or restoration.

No live board, profile, provider, service, installed plugin, ledger, Git commit, or repository remote was changed while producing this report.
