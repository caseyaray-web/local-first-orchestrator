# M7 authorized parent native fixture verification

**PASS for the maintained disposable native-fixture gate; not release or cutover approval.**

Pinned source: `f768c12895856f018b714a4f56a7a8a8d0a3286b` plus the five dashboard overlay files bound by manifest `5cc904dcadbf34dffb5bf986390687ff19a5c498e5e8e093cbb6cccb3af5d624`. All five current file hashes match that candidate. Later evidence-document changes are outside the package snapshot.

Full maintained suite: **226 passed, 0 skipped, 0 failures/errors**. Parent execution duration: **1766.53 seconds**. All 15 formerly skipped native nodes passed; no child mutation guard was disabled. The initial foreground attempt timed out at 420 seconds without JUnit; its fixture state was preserved, and a separate bounded background run completed.

Executable: `/home/ocadmin/.hermes/hermes-agent/venv/bin/hermes`, Hermes `v0.21.5+6146.g46904a3`. Both `HERMES_HOME` and `HERMES_KANBAN_HOME` were directed into disposable roots; inherited DB/workspace/attachment/log/profile/task/run overrides were cleared. Fixture board IDs and stores are isolated. Native claim transitions are controlled fixture operations, not worker spawning or provider-backed execution.

Durable evidence: `/home/ocadmin/.hermes/release-artifacts/local-first-orchestrator/m7-native-parent-verification-20261003`. Includes JUnit, full output, execution metadata, runner and node-level verification report with SHA-256 values. Original fixture state remains under `/home/ocadmin/.hermes/cache/scratch/m7-native-parent-verification-20261003` (scratch retention applies). Dashboard PID 1875 and gateway PID 1886 remained unchanged; neither service was restarted.

## Previously skipped native nodes

| Node | Result | Seconds |
| --- | --- | ---: |
| `tests/test_m4_active_piece_dependency_authority.py::test_native_first_edge_authority_uses_production_adapter` | Passed | 229.902 |
| `tests/test_m4_active_piece_preparation.py::test_native_active_tranche_batch_and_replay_are_held_only` | Passed | 225.301 |
| `tests/test_m4_active_piece_preparation.py::test_native_active_piece_create_replay_and_later_tranche_rejection` | Passed | 120.736 |
| `tests/test_m4_active_piece_preparation.py::test_native_active_piece_failure_recovery_is_read_only_and_digest_bound[lost_create_response]` | Passed | 105.642 |
| `tests/test_m4_active_piece_preparation.py::test_native_active_piece_failure_recovery_is_read_only_and_digest_bound[post_ack_member_failure]` | Passed | 110.693 |
| `tests/test_m4_plan_acceptance.py::test_controlled_paid_planner_proposal_is_accepted_and_replayed` | Passed | 78.718 |
| `tests/test_m4_planner_creation.py::test_prepare_planner_creates_one_held_card_and_reconciles_after_restart` | Passed | 88.831 |
| `tests/test_m4_planner_creation.py::test_prepare_planner_rejects_stale_request_profile_and_active_pause` | Passed | 40.013 |
| `tests/test_m4_planner_creation.py::test_lost_create_response_is_marker_reconciled_after_restart` | Passed | 63.277 |
| `tests/test_m4_planner_creation.py::test_member_insert_crash_recovery_verifies_exact_native_card[unchanged]` | Passed | 67.422 |
| `tests/test_m4_planner_creation.py::test_member_insert_crash_recovery_verifies_exact_native_card[native-comment-drift]` | Passed | 69.734 |
| `tests/test_m4_planner_run_binding.py::test_applied_paid_release_binds_native_run_without_a_second_charge` | Passed | 77.581 |
| `tests/test_m4_planner_run_binding.py::test_managed_planner_without_release_proof_fails_closed_before_accounting` | Passed | 41.658 |
| `tests/test_m5_native_recovery_restart.py::test_parent_native_v2_link_checkpoint_recovery_acks_without_resend` | Passed | 186.569 |
| `tests/test_m5_native_recovery_restart.py::test_parent_native_pause_resume_restarts_once_with_operator_pause_preserved` | Passed | 172.415 |

## Remaining boundaries

Real worker/session/provider lifecycle and spend are not proved by these fixtures. Target backup, new bootstrap/state-root decisions, explicit initialization, installation/enablement and cutover remain separately authorized gates. Other Hermes versions/platforms remain untested; this is exact-host evidence only. No commit or push was performed.
