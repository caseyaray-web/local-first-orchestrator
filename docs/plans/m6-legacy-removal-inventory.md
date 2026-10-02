# M6 legacy removal inventory

This is the explicit M6 source-retirement record. Removal applies only to repository source/tests/docs in this checkout. It does **not** delete or migrate Hermes boards, `kanban.db`, legacy Local First databases, worktrees, profiles, plugin enablement, or any user data. Each removed source was parsed/read and SHA-256 recorded before unlinking; the pre-delete hash report is retained in the M6 execution transcript.

## Replacement dependency graph

The supported entrypoints are root `__init__.py` registration, `local_first_orchestrator.cli`, `composition`, `config`, `plugin_tools`, `plugin_hooks`, `dashboard/plugin_api.py`, and `dashboard/dist/index.js`. They compose `Coordinator`, `EvidenceStore`, and `HermesBoardAdapter`; a package-wide AST import regression imports every distributed `local_first_orchestrator` module in a subprocess after removal. It must contain no legacy ledger/controller/scheduler/projection/signer/private-host-DB runtime module, direct `cryptography` import, or `m6_cli`/`legacy_cli` wrapper.

## Runtime modules retired

| Removed path(s) | Why retired | Replacement and retained tests |
| --- | --- | --- |
| `adapters.py`, `controller.py`, `ledger.py`, `scheduler.py`, `states.py`, `state_projection.py`, `generated_projection.py`, `generated_activation.py`, `execution_handoff.py` | Shadow lifecycle, state projection, and scheduler authority. | `contracts.py`, `coordinator.py`, `evidence_store.py`, `hermes_board.py`, `operator_controls.py`; `test_m6_composition_plugin_surfaces.py`, `test_operator_api.py`, M4/M5 public-core and recovery tests. |
| `admission.py`, `architecture.py`, `decomposition.py` legacy activation helpers, `triage.py`, `tranche_completion.py`, `reconciliation.py`, `runtime_metrics.py`, `metrics.py` | Legacy feature/microticket lifecycle, adaptive authority, and completion model. | Native anchor enrollment, `budgets.py`, `recovery.py`, `planning_coordinator.py`, `decomposition_planner.py`; M4 planning/accepted-dependency tests and M5 restart tests. |
| `local_qwen.py`, `paid_model.py`, `review_worker.py`, `usage_governor.py` | Direct model/provider invocation and legacy paid accounting. | Native role tasks, `plugin_tools.py`, `budgets.py`, `review.py`; `test_m6_composition_plugin_surfaces.py` and M4/M5 role/review tests. |
| `native_release_approval.py`, `signer_enrollment.py`, `validation_recovery_boundary.py`, `revalidation_boundary.py`, `historical_revalidation.py`, `keyless_human_recovery.py` | Detached signer/protected/private-SQL recovery authority is not part of ordinary plugin repair. | `recovery.py`, `operator_controls.py`, typed evidence-store readback; `test_operator_api.py` partial-stop/stale-observation coverage and M5 recovery tests. |
| `operator_config.py` | Legacy registration/config ceremony. | `config.py::PluginConfig`; `test_m6_composition_plugin_surfaces.py`. |
| `comment_delivery.py`, `gateway_notification.py` | Legacy ledger-backed external delivery workers. | No automatic notification sender is retained; bounded recovery and scoped status remain in `recovery.py`, `coordinator.py`, and `plugin_tools.py`. |
| `corrections.py` | Legacy correction-plan/microticket materializer. | Native correction reservation/reconciliation is in `coordinator.py` with immutable `contracts.py` evidence and `evidence_store.py`; M4/M5 correction topology/restart tests cover it. |
| `hermes_profiles.py` | Legacy profile scraping and `ModelRegistration` bridge. | Configured roles are validated by `config.py`; coordinator native run/session provenance is checked through `hermes_board.py` and `evidence_store.py`. |
| `context_packet.py`, `readiness.py`, `repository_snapshot.py`, `symbols.py` | Legacy ticket/context/snapshot/symbol authority. | New M4 planner parsing remains in the retained `decomposition_planner.py` contract/parser; it does not recreate `MicroTicket` or `FeatureContract` compatibility types. |

`MicroTicket`/`FeatureContract` contracts are retired with the legacy lifecycle modules, not resurrected through a compatibility bridge. New public contracts remain the M4/M5 `contracts.py` and coordinator/evidence interfaces. `decomposition_planner.py` is retained as the canonical planner schema/parser boundary; its plan wire format is not the retired feature/microticket contract.

## Scripts and documents retired

| Removed path(s) | Why retired | Replacement |
| --- | --- | --- |
| `scripts/c12r1-tk-3-bootstrap.py`, `scripts/c12r1-tk-3-root-launcher.sh.in` | Root-owned installer/launcher outside plugin-only scope. | No privileged substitute; ordinary repair is documented in `docs/operator-guide.md`. |
| `docs/command-reference.md`, `docs/configuration.md`, `docs/operator-lifecycle-recovery.md`, `docs/native-hermes-integration.md` | Described legacy ledger, registration, scheduler, signer, and direct-model commands. | `README.md` and `docs/operator-guide.md`. |
| `docs/native-release-detached-approval.md`, `docs/c12r1-tk-3-keyless-human-recovery.md`, `docs/c12r1-tk-3-protected-launcher-install.md` | Described retired signature/root-owned capabilities. | This inventory plus explicit unsupported boundaries in the operator guide. |

## Legacy test retirement

Every test importing a retired module is removed rather than skipped. Its former scenario is mapped to the current fixture tests by category: lifecycle/projection tests map to M4 accepted-dependency and `test_operator_api.py`; crash/restart tests map to the M5 `test_m5_*` suite; signer/capability tests map to this inventory's dependency-negative check; direct-model/legacy planner tests map to `test_m6_composition_plugin_surfaces.py`. Legacy comment/gateway delivery is deliberately not ported because automatic external notification is outside the replacement's scoped coordinator authority; correction behavior maps to M4/M5 native correction tests; legacy context/readiness/snapshot/symbol selection maps to the retained canonical planner parser. This avoids collecting tests that depend on eliminated contracts while preserving M4/M5 behavior.

Removed legacy-import test paths are enumerated by the post-removal AST check and must be zero references. They include all historical `test_phase*`, `test_runtime_*`, `test_scheduler_*`, projection/activation/ledger/signer/revalidation tests, and every remaining test whose import target is one of the retired modules above. No fixture connects to a real profile, board home, provider, or user store.

## Required checks after removal

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI='' \
  uv run --offline --no-project --with pytest --with jsonschema \
  python -m pytest --collect-only -q -pno:cacheprovider -oaddopts=''

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI='' \
  uv run --offline --no-project --with pytest --with jsonschema \
  python -m pytest -q -pno:cacheprovider -oaddopts=''
```

Packaging must additionally prove a wheel/sdist includes `plugin.yaml`, root `__init__.py`, dashboard manifest/API/JS, and the standalone entry point. The discovery regression must build a fresh wheel, install it into a temporary isolated environment, reconstruct the documented `HERMES_HOME/plugins/<name>` root only from wheel data files, explicitly enable it in a temporary Hermes config, and obtain Plugin Manager/Doctor readback of the native CLI, five tools, and advisory hook. M6 remains incomplete until independent review and separately authorized browser/activation checks; neither is performed by cleanup.
