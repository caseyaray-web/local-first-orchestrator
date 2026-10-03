# M7 isolated release-readiness progress

**Decision: NO-GO for cutover.** The current candidate is an uncommitted overlay on base commit `0dacc97d2cdd2369519f43d60ef6168d1bb7ae84`, not an installation, enrollment, dispatch, provider run, migration, reload, service change, or operator cutover.

## Current evidence

- The current isolated full run collected **226 nodes** and returned **211 passed, 15 skipped in 92.49s**; JUnit: `/home/ocadmin/.hermes/cache/scratch/local-first-whole-suite-consolidated-m7-restart-recovery-final-junit.xml`. The historical 1195-node / 1110-pass baseline, 1059-node / 974-pass first reduction, and 218-node / 203-pass prior reduction remain historical; the current whole-suite removed-file ledger is in [the consolidation map](test-suite-consolidation.md).
- Scenario 4 now closes and reopens its temporary EvidenceStore after its one held verdict-less replacement, builds a fresh Coordinator around the retained board, releases exactly once, and verifies member and repair-budget history stay unchanged through a second fresh recovery no-op.
- Focused M7 rollback boundaries passed: **7 passed in 1.89s**. They exercise round-trip restoration, exact checkpoint rejection, all allowlisted artifact bytes, reopened paused/stopped intent, a persisted budget/member history, duplicate/traversal allowlists, source symlinks (including a `dashboard/` ancestor symlink for `dashboard/manifest.json`), and duplicate archive members.
- The previously recorded scratch source snapshot, five-file overlay manifest, and package hashes predate this dirty test/documentation overlay (including the consolidation tests) and are historical only; they are not current-artifact claims.
- Pre-existing untracked `build/`, `local_first_orchestrator.egg-info/`, and `uv.lock` remain excluded and preserved.

## Scope and limits

- No commit, push, reset, cleanup, activation, installed-plugin/profile change, board enrollment, native mutation, provider execution, service action, or migration was performed.
- The native mutable-fixture capability remained deliberately disabled (`HERMES_M0_CLI=''`). Its 15 retained skips are open gates, not passes; the 85 historical skipped nodes belonged to the larger historical suite. No provider-backed execution was attempted.
- Independent M7 code and specification reviews previously returned NO-GO. The ancestor-symlink finding is corrected in this overlay, but renewed final reviews remain required; this document does not convert a correction into acceptance.
- Linux/Hermes `v0.21.5+6146.g46904a3` is the only characterized environment. No minimum-version or cross-platform compatibility claim is made.

## Remaining gates

1. Fresh independent code and specification reviews of the complete current overlay and artifacts.
2. Authorized parent-context native mutable-fixture verification, with node-level passed/skipped/unrun results.
3. An authorized compatibility/version matrix and explicit target-capture/cutover decision.

M7 release preparation remains distinct from cutover. No GO is implied.