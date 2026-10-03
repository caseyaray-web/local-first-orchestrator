# M7 canonical acceptance matrix

This maps the sixteen scenarios in `docs/plugin-only-recoverable-workflow.md` §Acceptance scenarios to retained collected nodes. **Fixture passed** is isolated test evidence, never a live board/provider claim. Native/runtime and provider gates remain open unless separately authorized and executed.

| # | Canonical scenario | Retained fixture evidence |
| --- | --- | --- |
| 1 | Bounded work, fresh local reviews, paid integrated review | `test_public_m4_core_flow_uses_actual_effect_journals_and_real_git`; `test_paid_review_checks_correction_and_exact_acceptance_smoke` |
| 2 | Repeated local and paid correction rounds with bounded escalation | `test_public_m4_core_flow_uses_actual_effect_journals_and_real_git` |
| 3 | Premature completion produces review without acceptance | `test_recover_premature_done_tick_creates_one_held_review_then_releases_once` |
| 4 | Verdict-less completed review cannot approve | `test_done_separate_review_without_verdict_gets_one_budgeted_replacement_then_releases` — closes/reopens EvidenceStore, constructs a fresh Coordinator, then proves one replacement, unchanged repair budget/member history, release, and later no-op recovery |
| 5 | Changed head invalidates old approval | `test_regression_changed_correction_head_requires_fresh_checks_request_and_approval` |
| 6 | Completion/dispatch race is contained or honestly partial | `test_dispatch_race_with_unstoppable_worker_reports_remaining_active_run` |
| 7 | Restart after effects avoids blind duplicate work | `test_m5_reopen_after_v2_link_effect_before_receipt_checkpoint_stays_partial_without_resend`; `test_m5_held_create_after_native_effect_before_ack_reopens_by_exact_marker_without_duplicate` |
| 8 | Concurrent recovery is serialized and budgets persist | `test_m5_true_concurrent_report_generation_uses_two_store_connections_and_one_lock`; `test_m5_two_fresh_coordinators_share_one_held_paid_correction_generation_and_budget` |
| 9 | Polling repairs a missed hook | `test_poll_recovers_missed_duplicate_hook_after_reopen_with_one_budgeted_hold` |
| 10 | Scoped durable pause holds managed work only | `test_regression_prepared_link_rechecks_operator_intent_before_send`; `test_temporary_managed_hold_never_overrides_operator_pause` |
| 11 | Pause-and-stop retains partial work and rejects late results | `test_m5_supported_stop_run_crash_reopen_never_resends_unknown_and_requires_terminal_readback[before_claim]`, `[after_claim_before_effect]`, `[after_effect_before_ack]`, `[after_ack]` |
| 12 | Human board edits reconcile without SQL or shadow ledger | `test_paused_human_declared_edge_survives_reopen_and_explicit_public_resume` |
| 13 | Done/archive/malformed evidence is not approval | `test_nonapproval_inputs_do_not_persist_local_approval[done_without_claim]`; `[archived]`; `[malformed_verdict]`; `[missing_evidence]` |
| 14 | Useful duplicate work is preserved | `test_useful_duplicate_is_preserved_in_one_visible_escalation` |
| 15 | Exhaustion creates one actionable escalation | `test_exhausted_poll_records_one_durable_actionable_escalation_without_effect` |
| 16 | Plugin-only artifact, no test-side cutover | `test_built_distribution_imports_every_module_and_registers_from_temporary_hermes_home`; `test_m6_actual_dashboard_bundle_partial_stop_and_stale_error_retention` |

## Run status

The current isolated run collected **226 nodes** and returned **211 passed, 15 skipped in 92.49s**. JUnit: `/home/ocadmin/.hermes/cache/scratch/local-first-whole-suite-consolidated-m7-restart-recovery-final-junit.xml`. XML audit found every named canonical mapping above, including scenario 4's fresh-Coordinator reopen recovery, all four scenario-11 checkpoint nodes, and all four scenario-13 non-approval nodes; each passed. The historical 1195-node baseline, 1059-node first reduction, and prior 218-node run remain historical. Native capability skips under `HERMES_M0_CLI=''` remain unproven, not acceptance.
