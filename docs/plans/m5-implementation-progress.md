# M5 implementation progress

## Scope and boundary

M5 is a fixture-only recovery/restart slice on the dirty historical M4 worktree at `a0405df5ed24c2b58352d0eb2be4c866a6d6db7b`. It registers no hook, UI, CLI, plugin, provider, profile, board, dispatcher, or live effect. Kanban remains lifecycle authority; the evidence store retains scoped immutable evidence, operation intents, budgets, and operator intent only.

## Temporary recovery-hold admission implemented

`Coordinator.poll()` now admits only a `dependent_early_ended_worker` recovery hold for one exact managed, accepted active-tranche piece. It reconstructs the accepted plan/ticket and routes the fresh receipt through `Coordinator.release_active_piece(..., _already_locked=True)`; that existing boundary rechecks the active operator intent, enrolled root, accepted route/dependencies, implementation profile, no active runs, and the finite implementation release budget. `Coordinator._temporary_recovery_hold_bridge()` accepts the changed blocked digest only when it exactly matches the immutable hold journal and applied hold repair; all other card drift remains held. A `recovery_automatic_clear` journal entry is appended only after the verified native release.

Excluded: operator pauses/cancellations are never cleared; permanent redundant-duplicate containment is never eligible; unknown effects, missing receipts, route/root/dependency disagreement, active runs, and opaque receipt drift remain held. This is fixture-only board transport and not native-provider evidence.

## Canonical acceptance ledger

| # | Scenario | Exact executed fixture gate |
| --- | --- | --- |
| 1 | bounded local work and paid review | `tests/test_m4_public_core_flow.py::test_public_m4_core_flow_uses_actual_effect_journals_and_real_git` |
| 2 | repeated correction rounds | `tests/test_local_review_loop.py::test_repeated_same_card_changes_are_worker_proposed_then_reconciled_before_fresh_candidate_can_accept` |
| 3 | premature done gets review | `tests/test_recovery.py::test_forged_done_review_pass_without_bound_identity_provenance_checks_or_criteria_is_replaced` |
| 4 | missing verdict cannot approve | `tests/test_recovery.py::test_done_review_without_usable_verdict_gets_bounded_replacement_review`; `tests/test_m4_public_core_flow.py::test_regression_serial_git_integration_requires_observed_local_approval` |
| 5 | changed head invalidates approval | `tests/test_m4_public_core_flow.py::test_regression_changed_correction_head_requires_fresh_checks_request_and_approval` (real temporary Git and CAS) |
| 6 | completion/dispatch race containment | `tests/test_m5_recovery_polling.py::test_dispatch_race_with_unstoppable_worker_reports_remaining_active_run`; `::test_ended_dependent_worker_is_held_after_exact_readback` |
| 7 | ambiguous native effect restart | `tests/test_m5_hold_release_restart.py` (8 hold/release close-open crash nodes); `tests/test_m5_create_git_restart.py` (8 held-create/real-Git-CAS close-open nodes); plus public-core accepted-link v2 nodes |
| 8 | concurrent reports / root budget | `tests/test_m5_concurrent_correction.py::test_m5_two_concurrent_paid_correction_coordinators_create_one_held_card_and_charge_once` (six repeated runs); `tests/test_m5_recovery_polling.py::test_concurrent_equivalent_reports_are_one_durable_recovery_generation`; `tests/test_budgets.py::test_replacement_and_reopen_do_not_reset_shared_root_finding_budget` |
| 9 | missed hook repaired by polling | `tests/test_m5_recovery_polling.py::test_poll_recovers_missed_duplicate_hook_after_reopen_with_one_budgeted_hold` |
| 10 | durable scoped pause and temporary recovery admission | `tests/test_m5_recovery_polling.py::test_temporary_managed_accepted_hold_repairs_then_releases_once_after_reopen`; `::test_temporary_managed_hold_never_overrides_operator_pause`; `::test_redundant_duplicate_containment_remains_held_after_reopen` |
| 11 | stop, partial work, late result | `tests/test_coordinator_m2.py::test_known_unsupported_exact_stop_reports_running_work_without_false_hold`; `tests/test_coordinator_m2.py::test_status_pause_restart_and_partial_stop_are_durable` |
| 12 | human edits reconcile | `tests/test_m5_recovery_polling.py::test_paused_title_edit_is_durably_adopted_then_explicitly_released_after_reopen`; `::test_paused_human_declared_edge_survives_reopen_and_explicit_public_resume` (three accepted cards, full raw before/current topology receipts, SQLite close/open, fresh receipt revalidation, explicit multi-card release, and stale local/paid approval refusal); `tests/test_coordinator_m2.py::test_restart_human_edit_against_durable_pause_baseline_fails_closed` |
| 13 | done/archive/malformed evidence is not approval | `tests/test_recovery.py::test_forged_done_review_pass_without_bound_identity_provenance_checks_or_criteria_is_replaced`; `tests/test_board_compatibility.py::test_done_and_archive_release_native_dependents` (lifecycle characterization only) |
| 14 | duplicate useful work preservation | `tests/test_m5_recovery_polling.py::test_useful_duplicate_is_preserved_in_one_visible_escalation` |
| 15 | bounded one-time escalation | `tests/test_m5_recovery_polling.py::test_exhausted_poll_records_one_durable_actionable_escalation_without_effect` |
| 16 | plugin-only / no cutover | Pending M6; M5 deliberately has no registration or activation path. |

Row 12 retains the title-only positive path and additionally credits one narrowly supported topology repair: a paused human may add exactly one missing edge declared by the accepted active-tranche DAG. The durable v3 adoption receipt retains complete raw before/current cards, accepted source/token, exact edge/event, and a self-hash; it neither sends a native link nor releases work. Body, route, status, opaque-field, cancellation, unknown effect, extra edge, removal, or any other topology change remains held.

## Native-effect / crash-reopen matrix

Only the first two columns are native I/O effect families. `combined_checks`, `tranche_accept`, and the `recovery_*` records are durable local coordinator/evidence effects and have no board send.

| Effect family and actual operation string | Before claim / pre-send | After claim, before native effect | After native effect, before ack | After ack / replay | Exact coverage / outcome |
| --- | --- | --- | --- | --- | --- |
| accepted-link v2 (`link`) | pending persists; one later send | unknown with no edge is partial, read-only, zero resend | exact pre-send identity/lower bound plus fresh raw transition verifies and acks, zero resend | reopened replay has zero link calls | public-core M5 four nodes in ledger row 7 |
| held-card create (`create_held`) | reservation/created identity is durable | generic journal marks unknown before fake-adapter create | marker readback reconciles unknown without another create | exact replay returns applied card | `tests/test_m4_planner_creation.py::test_lost_create_response_is_marker_reconciled_after_restart`; `tests/test_m2_lock_adapter.py::test_nonzero_no_effect_persists_unknown_and_restart_never_sends_second_create` |
| hold (`hold`) | intent/budget precede mutation | unknown is never re-sent | exact marker verification is read-only | restart clears only verified unknown | `tests/test_coordinator_m2.py::test_unknown_effect_is_never_resent_and_reconcile_is_read_only`; `::test_restart_reconciles_unknown_after_effect_with_exact_marker_without_resend` |
| release (`release`) | budgeted release intent precedes unblock | unknown remains reconciliation-only | exact unblock marker verifies after reopen | replay charges no extra implementation budget | `tests/test_m4_planner_release.py::test_lost_unblock_response_is_unknown_then_readonly_marker_reconciles_after_restart`; `tests/test_m4_public_core_flow.py::test_regression_named_piece_release_consumes_one_budget_unit_and_replays` |
| stop exact run (`stop_run`) | stop intent is journaled before adapter call | unsupported/unknown is not containment | only exact terminal readback permits held repair | no false terminal claim on repeat | `tests/test_coordinator_m2.py::test_known_unsupported_exact_stop_reports_running_work_without_false_hold`; `tests/test_m5_recovery_polling.py::test_dispatch_race_with_unstoppable_worker_reports_remaining_active_run` |
| comment (`comment`) | exact scoped task/author/marker intent is durable before dispatch | claimed unknown is reconciliation-only and never re-sent | exact task readback contains exactly one matching author/body marker | acknowledged replay returns the original applied receipt with no duplicate comment | `tests/test_m5_comment_handoff_restart.py::test_m5_comment_restart_boundaries_close_open_no_resend_and_no_budget_change`; `tests/test_hermes_board_adapter.py::test_comment_marker_is_idempotent_and_requires_author_and_exact_marker` |
| reviewer handoff (`request_review`) | operation intent precedes handoff | unverified handoff cannot approve | exact worker-owned native handoff/run is required | replay cannot synthesize approval | `tests/test_local_review_loop.py::test_submit_review_holds_when_no_exact_native_worker_handoff_exists` |
| Git integration CAS (`git_integrate`) | integration intent precedes Git mutation | incomplete intent is exact replay, not another mutation | real CAS state supplies the applied receipt | stale CAS/old approval is held | `tests/test_m4_public_core_flow.py::test_regression_changed_correction_head_requires_fresh_checks_request_and_approval`; `::test_regression_serial_git_integration_requires_observed_local_approval` |
| paid correction held create (`create_held`, `paid_correction_v1`) | root/finding budget admission and immutable key precede create | same key returns the existing held operation; no new charge | fake-adapter exact blocked snapshot is required before ack/member registration | repeated same key has one create and one budget source | `tests/test_m4_public_core_flow.py::test_m5_two_fresh_coordinators_share_one_held_paid_correction_generation_and_budget`; `tests/test_local_review_loop.py::test_repeated_same_card_changes_are_worker_proposed_then_reconciled_before_fresh_candidate_can_accept`; `tests/test_m4_native_paid_review_correction_topology.py::test_native_paid_review_and_correction_are_parentless_and_admitted_once` |
| automatic recovery record (`recovery_automatic_hold`, `recovery_automatic_clear`) | native hold intent/budget precede the held snapshot | n/a | a fresh coordinator reads the immutable temporary-hold receipt and uses the existing budgeted `release` admission only when the exact accepted member, scope/root/route/dependencies, profile, no-run state, and current blocked digest cohere | replay sees the applied release; permanent redundant containment has no release path | `tests/test_m5_recovery_polling.py::test_temporary_managed_accepted_hold_repairs_then_releases_once_after_reopen`; `::test_temporary_managed_hold_never_overrides_operator_pause`; `::test_redundant_duplicate_containment_remains_held_after_reopen` |
| report/escalation (`recovery_report`, `recovery_escalation_notice`) | no native I/O | n/a | n/a | stable keys deduplicate across coordinators/reopen | `tests/test_m5_recovery_polling.py::test_concurrent_equivalent_reports_are_one_durable_recovery_generation`; `::test_exhausted_poll_records_one_durable_actionable_escalation_without_effect` |

`hold_task` and `unhold_task` are store-supported legacy aliases, not coordinator/adapter effect strings in this M5 path. `request_changes` and `reopen_review` are adapter methods but are reviewer-owned native transitions, not coordinator `Action.effect` values; their lifecycle evidence is verified rather than synthesized by M5.

### Executed M5 restart coverage

The dedicated M5 restart fixtures prove 8 reviewer-owned `request_changes`/
reopen nodes, 4 initial implementation-worker `request_review` nodes, and 4
coordinator-owned comment nodes in `tests/test_m5_comment_handoff_restart.py`.
Each comment boundary uses a real SQLite close/open and fresh coordinator:
before durable claim sends once after reopen; post-claim/pre-send remains
unknown with zero send; post-effect/pre-ack is read-only reconciled by exact
task, `local-first-orchestrator` author, and stable action marker; post-ack replay uses the
durable receipt without another comment. Provider/CLI transport remains outside
this fixture slice.

Remaining family×checkpoint gaps outside the executed restart gates:

| Family | Checkpoint | Status / reason |
| --- | --- | --- |
| `stop_run` | before claim / after claim / after native effect / after ack | Fixture-supported close/open proof in `tests/test_m5_stop_restart.py`: pre-claim later sends once; claimed unknown stays running/zero-resend; terminal readback acks without resend; acknowledged replay rejects stale digest without resend. Production CLI remains explicitly unsupported. |
| `request_changes` | all four | Reviewer-owned adapter transition, not a coordinator M5 `Action.effect`; no M5 resend authority. |
| `reopen_review` | all four | Reviewer-owned adapter transition, not a coordinator M5 `Action.effect`; no M5 resend authority. |
| comment | all four | Passing direct coordinator close/open proof in `tests/test_m5_comment_handoff_restart.py::test_m5_comment_restart_boundaries_close_open_no_resend_and_no_budget_change`: durable claim before board dispatch, exact task/author/marker readback proof, and unknown no-resend recovery. |
| initial worker-owned `request_review` | all four | Passing close/open proof in `tests/test_m5_comment_handoff_restart.py::test_m5_initial_worker_owned_request_review_restart_boundaries`: before intent; reservation before native worker transition; after exact native metadata/event handoff before ack; and post-ack reopen. It verifies `local_first_review`, the implementation session, distinct reviewer profile, and zero coordinator board handoff calls. |

## Focused verification

```text
PYTHONPATH=. HERMES_M0_CLI='' uv run --offline --no-project --with pytest --with jsonschema python -m pytest -q tests/test_m5_stop_restart.py tests/test_m5_create_git_restart.py
12 passed in 6.01s
```

```text
PYTHONPATH=. HERMES_DELEGATED_CHILD_CONTEXT=1 HERMES_M0_CLI='' uv run --offline --no-project --with pytest --with jsonschema python -m pytest -q tests/test_m5_native_recovery_restart.py
2 skipped in 0.14s
```

```text
PYTHONPATH=. HERMES_M0_CLI='' uv run --offline --no-project --with pytest --with jsonschema python -m pytest -q tests/test_m5_comment_handoff_restart.py::test_m5_initial_worker_owned_request_review_restart_boundaries
4 passed in 0.93s
```

```text
PYTHONPATH=. HERMES_M0_CLI='' uv run --offline --no-project --with pytest --with jsonschema python -m pytest -q -o addopts= tests/test_m5_comment_handoff_restart.py tests/test_m5_comment_review_restart.py tests/test_m5_hold_release_restart.py tests/test_m5_create_git_restart.py tests/test_m5_stop_restart.py tests/test_m5_concurrent_correction.py tests/test_m5_recovery_polling.py tests/test_m5_effect_restart_matrix.py tests/test_m4_public_core_flow.py
63 passed in 33.18s
```

```text
HERMES_M0_CLI='' uv run --offline --no-project --with pytest --with jsonschema python -m pytest -x -q -p no:cacheprovider -o addopts='--tb=short' tests/test_m5_concurrent_correction.py  # repeated six times
6 × 1 passed (1.32–1.38s per run)
```

Additional inherited focused nodes:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI= uv run --offline --no-project --with pytest --with jsonschema python -m pytest -q -p no:cacheprovider -o addopts='--tb=short' tests/test_coordinator_m2.py::test_status_pause_restart_and_partial_stop_are_durable tests/test_coordinator_m2.py::test_known_unsupported_exact_stop_reports_running_work_without_false_hold tests/test_coordinator_m2.py::test_restart_human_edit_against_durable_pause_baseline_fails_closed tests/test_local_review_loop.py::test_repeated_same_card_changes_are_worker_proposed_then_reconciled_before_fresh_candidate_can_accept tests/test_budgets.py::test_replacement_and_reopen_do_not_reset_shared_root_finding_budget tests/test_m2_lock_adapter.py::test_nonzero_no_effect_persists_unknown_and_restart_never_sends_second_create
6 passed in 1.80s
```

The focused M5 additions use a real SQLite close/open and a fresh `Coordinator`; they do not reuse the prior coordinator after the crash boundary. The public-core route uses real temporary Git and CAS, while board transport remains fixture-only.

## Final bounded acceptance

M5 is complete within the isolated fixture scope. Independent specification review returned conditional GO with 65 focused tests passing in 35.19s; independent quality review returned GO. The native condition has now been satisfied by parent-controlled disposable fixtures:

- Native v2 link checkpoint recovery: passed in `proc_6b1627936100` (that two-node run subsequently failed a pause-fixture order assertion; it was not a full-suite pass).
- Native pause/resume across SQLite reopen: `proc_3f570925b635`, exit 0, **1 passed in 164.53s**. The fixture verifies task-ID release order, exactly two unblock calls, and retained inactive operator intent after readback-only clear.

The final native fixture changes correct role-order and deleted-intent assumptions only; no runtime guard was loosened. Comment recovery uses the shared native author `local-first-orchestrator`, with an actual adapter over fake CLI transport regression proving post-effect/pre-ack reconciliation without another send. Historical blocked runs are known inert pause/resume history, not successful stop evidence.

## Boundaries remaining outside M5 acceptance

1. Real provider-backed review/worker lifecycle and real native readback for every effect remain unproven. Production stop remains explicitly unsupported; fixture stop coverage does not establish native worker termination.
2. M6 owns plugin registration/cutover. No installed plugin, live board, provider, profile or configuration activation is authorized by this completion.
3. The historical mapped wrapper is not crash-matrix evidence; only dedicated injected close/reopen tests are credited. All dirty M4/M5 work remains preserved and uncommitted.
