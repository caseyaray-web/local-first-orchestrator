# M4 fixture regression map

This compact fixture-only round uses the existing synthetic public board transport, temporary SQLite evidence store, and real temporary Git worktree/CAS where integration is exercised. It does not invoke native CLI, provider, host, or plugin effects.

| Behavior | Test node |
| --- | --- |
| Accepted tranche materializes its pieces held; no active-piece release is implicit. | `tests/test_m4_public_core_flow.py::test_regression_held_materialization_does_not_auto_release` |
| Declared `TK-A -> TK-B` link is durable once and applied replay does not add a second link/write. | `tests/test_m4_public_core_flow.py::test_regression_declared_serial_link_is_applied_once_and_replays` |
| Explicit named release readies one held piece, consumes one implementation budget unit, and replays without another effect. | `tests/test_m4_public_core_flow.py::test_regression_named_piece_release_consumes_one_budget_unit_and_replays` |
| A non-accepted ticket cannot release or mutate the fixture state. | `tests/test_m4_public_core_flow.py::test_regression_unaccepted_piece_release_has_no_effect` |
| Paid integrated review remains held until the complete serial integration chain exists; it does not run combined checks early. | `tests/test_m4_public_core_flow.py::test_regression_paid_review_waits_for_complete_serial_integration` |
| Serial real-Git CAS integration stays held until observed local approval, then advances the integration head. | `tests/test_m4_public_core_flow.py::test_regression_serial_git_integration_requires_observed_local_approval` |
| A nonzero trusted combined check runs only after all real serial integrations, then blocks paid-review creation and tranche acceptance. | `tests/test_m4_public_core_flow.py::test_regression_combined_checks_failure_blocks_paid_review_and_acceptance` |
| An authorized correction advances the real integration ref; the changed head receives fresh checks and a new paid request, and an older approval cannot accept it. | `tests/test_m4_public_core_flow.py::test_regression_changed_correction_head_requires_fresh_checks_request_and_approval` |
| An actually accepted parent and distinct accepted successor materialize successor pieces held, with no implicit implementation budget or release before the explicit named release. | `tests/test_m4_public_core_flow.py::test_regression_accepted_parent_successor_stays_held_until_named_release` |
| Changes-requested correction, fresh local review/reintegration, next paid review, acceptance, and explicitly released accepted successor are retained in the end-to-end fixture. | `tests/test_m4_public_core_flow.py::test_public_m4_core_flow_uses_actual_effect_journals_and_real_git` |

Focused command:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI= uv run --offline --no-project --with pytest --with jsonschema python -m pytest -x -q -p no:cacheprovider -o addopts='--tb=short' tests/test_m4_public_core_flow.py tests/test_m4_accepted_dependency_preparation.py tests/test_m4_active_release_integration.py tests/test_m4_paid_review_acceptance_smoke.py
```
