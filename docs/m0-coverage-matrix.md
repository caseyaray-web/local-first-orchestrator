# Canonical acceptance coverage matrix (M0 baseline)

The scenario numbers refer to `plugin-only-recoverable-workflow.md` §Acceptance scenarios. **M0 verified** means native host behavior only; it does not establish the replacement plugin's end-to-end acceptance. `Planned` names are targets from the implementation plan and are not passing tests yet.

| Scenario | M0 host evidence | Replacement acceptance target / milestone |
| --- | --- | --- |
| 1. Plan → local implementation/review → paid integrated review | Review-source run: `test_dispatcher_spawns_fresh_review_stub_run` | Planned `test_tranche_workflow.py::test_paid_review_after_local_acceptance` (M4) |
| 2. Repeated correction rounds | Wrong-run refusal: `test_wrong_review_run_is_refused_without_changing_card` | Planned `test_tranche_workflow.py::test_corrections_get_local_then_paid_review` (M4) |
| 3. Premature done recovery | `test_done_and_archive_release_native_dependents`; `test_completed_hook_sees_already_released_child` | Planned `test_local_review_loop.py::test_premature_done_creates_separate_review` (M3) |
| 4. Missing review verdict | No lane approval established by M0 | Planned `test_review_contract.py::test_missing_verdict_is_not_approval` (M3–M5) |
| 5. Stale approval after head change | Not applicable to board-only M0 | Planned `test_review_contract.py::test_wrong_candidate_rejected` (M3–M5) |
| 6. Completion/dispatch race | `test_completed_hook_sees_already_released_child`; `test_block_after_dispatch_claim_does_not_stop_live_stub_worker` | Planned `test_coordinator_recovery.py::test_premature_release_contained_or_partial` (M5) |
| 7. Restart after ambiguous native effects | Sequential/concurrent create: `test_idempotency_key_reuses_existing_unarchived_card_sequentially`, `test_concurrent_create_idempotency_key_does_not_serialize` | Planned `test_crash_replay.py::test_ambiguous_outcome_not_repeated` (M5) |
| 8. Concurrent recovery and root budgets | Native concurrent-create risk established by M0 | Planned `test_coordinator_recovery.py::test_two_reports_one_repair` and `test_budget_accounting.py::test_replacement_does_not_reset_budget` (M5) |
| 9. Missing hook repaired by polling | `test_completed_hook_sees_already_released_child`; `test_isolated_profile_plugin_hook_loading` prove hooks are advisory | Planned `test_coordinator_recovery.py::test_periodic_scan_recovers_missed_hook` (M5) |
| 10. Durable scoped pause | Hold primitives: `test_held_creation_does_not_dispatch_or_touch_real_home`, `test_hold_and_release_are_explicit_native_transitions` | Planned `test_pause_resume.py::test_pause_survives_restart` (M2/M5) |
| 11. Stop, partial work, late results | `test_reclaim_stops_exact_stub_but_releases_card_to_ready`; `test_block_after_dispatch_claim_does_not_stop_live_stub_worker` | Planned `test_pause_resume.py::test_partial_stop_is_not_success` and `test_cancel_rejects_late_result` (M2/M5) |
| 12. Human board edits/reconcile | `test_held_creation_does_not_dispatch_or_touch_real_home` reads list/show/runs; M0 does **not** verify adoption of manual edits | Planned `test_pause_resume.py::test_manual_edits_reconcile` (M2/M5) |
| 13. Archive/done/malformed evidence not approval | `test_done_and_archive_release_native_dependents`; `test_non_review_run_cannot_request_changes` | Planned `test_review_contract.py::test_missing_verdict_is_not_approval` (M3) |
| 14. Duplicate useful work preserved | `test_concurrent_create_idempotency_key_does_not_serialize` proves duplicate creation, **not** preservation of useful work | Planned `test_recovery_policy.py::test_duplicate_useful_work_holds` (M5) |
| 15. Bounded escalation | No plugin budgets in M0 | Planned `test_recovery_policy.py::test_repeated_issue_escalates` (M5) |
| 16. Plugin-only/no cutover | Isolated fixture plugin: `test_isolated_profile_plugin_hook_loading`; no live board or provider use | Planned `test_plugin_surfaces.py::test_registration_is_side_effect_free`; `test_no_legacy_dependencies.py::test_new_dependency_graph` (M6–M7) |

CLI-help evidence is in `compatibility-cli-help.md`; capability limitations and fallback decisions are in `compatibility-and-release.md`.
