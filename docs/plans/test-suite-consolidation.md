# Focused Local First regression suite consolidation

## Evidence

| Collection | Nodes | Result |
| --- | ---: | --- |
| Before (`/home/ocadmin/.hermes/cache/scratch/local-first-before-collect.txt`) | 1195 | historical baseline: 1110 passed, 85 skipped |
| First reduction | 1059 | historical: 974 passed, 85 skipped in 213.79s |
| Prior whole-suite reduction (`/home/ocadmin/.hermes/cache/scratch/local-first-whole-suite-consolidated-junit.xml`) | 218 | historical: 203 passed, 15 capability-skipped in 87.57s |

The current candidate collects 226 nodes, a reduction of 969 nodes (81.1%) from the 1195-node historical baseline. Its isolated full run returned 211 passed and 15 capability-skipped in 92.49s; JUnit: `/home/ocadmin/.hermes/cache/scratch/local-first-whole-suite-consolidated-m7-restart-recovery-final-junit.xml`. No discovery configuration, skip marker, capability gate, runtime module, or production validation changed. Tests were removed from collection; they were not hidden behind selection, skips, or loops.

## Retained compact suite

The suite is centered on public workflows and distinct failure/recovery boundaries:

- `test_m4_public_core_flow.py` — accepted-plan materialization, held/released pieces, worker-owned local review, serial real-Git integration, paid changes/correction/re-review, acceptance, and explicit successor release.
- `test_m4_recovery_review.py` plus `tests/fixtures/m4_recovery_review.py` — completed implementation recovery, bounded verdict-less separate-review replacement through EvidenceStore close/reopen and a fresh Coordinator, and the four canonical non-approval inputs.
- `test_m4_paid_review_acceptance_smoke.py` and `test_m4_active_release_integration.py` — direct paid-review/acceptance and serial-Git public smoke paths.
- `test_m5_recovery_polling.py`, `test_m5_effect_restart_matrix.py`, `test_m5_create_git_restart.py`, `test_m5_native_recovery_restart.py`, `test_m5_stop_restart.py`, and `test_m5_concurrent_correction.py` — durable unknown/no-resend, fresh-coordinator reopen, recovery hold/release, polling, budget lineage/concurrency, stop, and Git recovery boundaries.
- `test_m6_*`, `test_operator_api.py`, and `test_m7_release_rollback.py` — package/disposable-home Plugin Doctor, browser partial-stop and stale-error retention, public API lifecycle handling, and all seven rollback reproductions.

The retained helper-backed M4 files supply the fixtures used by those public flows. They remain deliberately until their fixture code can be moved without changing runtime code.

## Removed-to-retained coverage ledger

| Removed families/files | Retained proof | Behavior retained | Intentionally dropped granularity |
| --- | --- | --- | --- |
| `test_m1_contracts.py`, `test_m1_git_primitives.py`, `test_m1_verification.py`, `test_contracts.py` | public core real-Git flow; `test_m5_create_git_restart.py`; M6 package test | candidate advancement, bound workflow evidence, and packaged import path | isolated parser, command, filesystem, and Git hostile-input permutations |
| `test_evidence_store.py`, `test_budgets.py`, `test_m2_lock_adapter.py`, `test_coordinator_loop.py`, `test_coordinator_m2.py` | public core; M5 polling/concurrency/restart tests | durable effects, shared budget lineage, pause/cancel, fresh-store recovery | individual SQL/schema migration and lock-path robustness matrices |
| `test_board_compatibility.py`, `test_hermes_board_adapter.py`, `test_m3_hook_guidance.py`, `test_m3_native_coordinator.py`, `test_local_review_loop.py` | public core; paid-review smoke; M5 stop/recovery tests | held creation/release, local/paid review, correction, no blind resend | adapter method-by-method and native characterization variants |
| M4 `accepted_*`, `active_*_effect`, `coordinator_*`, `item14`, `native_*`, `piece_adapter`, `planner_adapter`, `planner_anchor_prerequisite`, `planner_release`, `paid_release_evidence`, `planning_correction_boundaries` helper suites | public core; active-release; paid-review smoke; retained M4 fixture helpers | tranche materialization, serial link/replay, release gating, correction and acceptance | raw-envelope, schema, hostile-value, and per-seam authority permutations |
| deleted `test_m4_planning_contract.py` and the prior expanded `test_m4_active_tranche_materialization.py` cases | compact replacements in the newly retained `test_m4_planning_contract.py` and existing `test_m4_active_tranche_materialization.py`; public core | nested `patch_budget` unknown-key refusal, retry ceiling, valid request/materialization, first-tranche-only behavior, normal route rejection | exhaustive parser/type/limit and route-spelling matrices |
| `test_m5_comment_*_restart.py`, `test_m5_hold_release_restart.py` | M5 public-link/create/Git/stop/native-recovery tests | restart opens a fresh coordinator and avoids duplicate effects | checkpoint permutations for each comment/hold family |
| `test_operator_controls.py`, `test_recovery.py`, `test_native_workspace_security.py`, `test_worktree_lifecycle.py` | public core, M5 polling/stop/restart, M6 API/package tests | pause/cancel pre-send fence, human adoption, containment, worktree use in real Git flow | isolated policy and malformed-input variants |

The compact parser/route regressions are `tests/test_m4_planning_contract.py` (nested ticket `patch_budget` unknown-key refusal and retry budget above the request ceiling) and `tests/test_m4_active_tranche_materialization.py::test_route_constructor_rejects_relative_workspace`.

### Explicit non-equivalence

This is not a claim that every old assertion remains. The removed low-value security/robustness permutations no longer each have a dedicated maintained test. The retained tests cover the named public behavior and distinct recovery boundaries; runtime validation was not weakened.

## Canonical milestone coverage

`docs/release/m7-acceptance-matrix.md` maps all sixteen canonical scenarios to retained nodes. The current JUnit XML audit found all named mappings, including scenarios 3, 4 (with a closed/reopened EvidenceStore and fresh Coordinator), and the four scenario-13 inputs; this fixture evidence does not establish the retained native capability skips. Native capability skips remain unproven when `HERMES_M0_CLI=''`; a lower skip count would never be treated as proof of native capability.
