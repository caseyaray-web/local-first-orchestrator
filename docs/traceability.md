# Local-first orchestrator requirements traceability

**Audit basis:** design `doc_d1a46f29ef86_hermes-kanban-local-first-orchestrator-design.md`; package state before operational-runtime implementation.

Status labels: **PASS** means demonstrated operationally; **PARTIAL** means isolated implementation/fake-only evidence; **MISSING** means no implementation. A fake, interface, schema, or unit test alone is never PASS for board/model integration.

## Sections 7, 18, 23, and 27

| Requirement | Status | Implementation / test | Adapter | Remaining work |
|---|---|---|---|---|
| §7 import/create feature and architecture packet | PARTIAL | `architecture.import_architecture_packet`; `tests.test_phase5` | ledger only | Import real board card/feature mapping |
| §7 dependency-ready tranche selection | PARTIAL | `ImportedArchitecture.activate_next`; `test_phase5` | ledger only | Runtime scheduler and dependency-aware claim |
| §7 isolated worktree/context/local implementation/validation | PARTIAL | `GitWorktreeAdapter`, `ContextPacketBuilder`, `LocalQwenAdapter`, `DeterministicValidator`; `test_phase2` | fake model | Controller wiring and configured real call |
| §7 fresh review, bounded repair, commit | PARTIAL | `LocalReviewAdapter`, `SameTicketRepairCoordinator`; `test_phase3` | fake model | Runtime wiring/commit evidence |
| §7 tranche integration/checkpoint or automatic completion | PARTIAL | `checkpoint_packet`, `UsageGovernor`; `test_phase5` | fake paid adapter | Runtime policy and invocation |
| §7 failure fingerprint/triage bounds | PARTIAL | `failure_fingerprint`, `TriageCoordinator`; phase3/4 tests | ledger only | Runtime transitions/projection |
| §18 BoardAdapter/model/git/validation/governor boundaries | PARTIAL | protocols/adapters; phase1–5 tests | FakeBoardAdapter/fake runners | Real HermesBoardAdapter |
| §18 existing-script compatibility wrapping | PARTIAL | `docs/compatibility.md` | none | Real CLI integration wrapper |
| §23 restartable process-next algorithm | MISSING | none | none | Controller/scheduler implementation |
| §27 init/run-once/daemon/status/inspect/retry/reject/metrics commands | PARTIAL | `cli.py` supports migrate/status/pause/resume/recover only | ledger only | Full operator CLI |
| §27 pause/status/manual intervention audit invariants | PARTIAL | `Ledger.pause/resume/status`; `test_ledger` | ledger only | Runtime active-stage status and intervention commands |

## Section 29 phases and exit conditions

| Phase / exit condition | Status | Implementation / test | Adapter | Remaining work |
|---|---|---|---|---|
| 0 discovery/compatibility classification, preserve changes | PASS | `docs/compatibility.md`; core `git diff --check` | read-only source | Keep compatibility doc current |
| 1 ledger/controller exit: fake ticket restart-safe projection | PARTIAL | `Ledger`, `FakeBoardAdapter`; `test_ledger` | fake board | Real-board projection |
| 2 local implementation/validation exit | PARTIAL | git/context/validator/Qwen boundary; `test_phase2` | fake local runner | Operational controller call |
| 3 same-ticket repair exit | PARTIAL | review/convergence; `test_phase3` | fake local runner | Runtime repair loop |
| 4 bounded triage exit | PARTIAL | triage; `test_phase4` | ledger only | Runtime import/projection |
| 5 paid budget/ambiguous-outcome exit | PARTIAL | governor/paid adapter; `test_phase5` | injected fake paid runner | Configured paid CLI path |
| 6 symbol context/metrics exit | PARTIAL | symbols/metrics; `test_phase6` | fixture repo | Persisted runtime metrics |

## Section 30 acceptance criteria

| # | Status | Implementation / test | Adapter | Remaining work |
|---|---|---|---|---|
| 1 low-risk end-to-end local feature | MISSING | phase pieces only | fake | Full runtime E2E |
| 2 controller sole authority | PARTIAL | ledger/governor boundaries | fake/ledger | Real board adapter/controller |
| 3 same-ticket deterministic repair | PARTIAL | `SameTicketRepairCoordinator`; `test_phase3` | fake | Runtime E2E |
| 4 configured attempt limit | PARTIAL | review coordinator; `test_phase3` | ledger | Runtime loop |
| 5 repeated failure one triage | PARTIAL | phase3 test | ledger | Runtime projection |
| 6 triage count/depth | PASS | `TriageCoordinator`; `test_phase4` | ledger | Integrate runtime |
| 7 child unresolved-criterion mapping | PASS | `normalize_triage`; `test_phase4` | ledger | Integrate runtime |
| 8 out-of-scope suggestions nonblocking | PASS | `normalize_review`; `test_phase3` | fake review | Integrate runtime |
| 9 allowlist scope validation | PASS | validator; phase2/6 tests | fixture git | Runtime evidence |
| 10 fresh implementation/review contexts | PARTIAL | context/review builders; tests phase2/3 | fake local | Actual configured Qwen process |
| 11 packet budget | PASS | context builder; phase2 tests | none | Runtime metric recording |
| 12 audit all stages | PARTIAL | events/artifacts/model calls | fake/ledger | Runtime stage audit |
| 13 no duplicate crash work | PARTIAL | ledger/outbox/paid tests | fake board/model | Runtime restart E2E |
| 14 paid reservation/purpose | PASS | governor; phase5 tests | injected runner | Configure production adapter |
| 15 exhaustion pauses work | PARTIAL | governor denial | fake | Controller transition needs_checkpoint |
| 16 pause/active CLI inspection | PARTIAL | ledger CLI | ledger | Runtime active-stage CLI |
| 17 ticket branch commit/no default merge | PARTIAL | git adapter; phase2 test | fixture git | Runtime E2E |
| 18 compatibility replacement | PARTIAL | compatibility note | none | Operational wrapper |

## Operational Runtime Milestone 1

| Requirement | Status | Implementation / test | Adapter | Remaining work |
|---|---|---|---|---|
| Read-only Hermes scheduled-card discovery | PASS | `HermesBoardAdapter.import_candidates/get_task`; `test_runtime_milestone1` | fake Hermes CLI fixture | Live read-only smoke only |
| Selected import, idempotency, restart safety | PASS | `LocalFirstController.import_card`; runtime tests | fake Hermes CLI + real temp ledger | Live dry-run smoke |
| Dry-run forbids model, board, repository effects | PASS | `LocalFirstController.dry_run`, CLI `run-once --dry-run`; runtime test | fake Hermes CLI | Keep write mode absent |
| Malformed JSON/CLI failure fail closed | PASS | `HermesBoardAdapter._run`; runtime test | fake Hermes CLI | Add error UX later |
| Scheduled ownership rule | PASS | Hermes `hermes_cli/kanban_db.py::has_spawnable_ready` lines 9554–9558 and dispatch query lines 10036–10040 select only `status = 'ready'`; scheduled cards are absent from both selections. | actual Hermes source inspected | Recheck on Hermes upgrades |
| write-enabled execution / daemon | MISSING | intentionally unavailable | none | Later milestone only |

## Operational Runtime Milestone 2

| Requirement | Status | Evidence | Remaining limitation |
|---|---|---|---|
| Exact allowlisted repository binding + recorded SHA | PASS | `RuntimeConfig.canonical_repository`, `runtime_bindings`, Milestone 2 tests | CLI configuration must be supplied per invocation |
| Explicit `--execute` + `--allow-board-writes` gate | PASS | CLI and `LocalFirstController.execute` | no daemon |
| Single low-risk card vertical slice | PASS (fake) | temp Git repo: worktree, implementation, validation, fresh review, commit, outbox | live smoke deliberately not run |
| Non-ready projection | PASS | adapter maps active local states to `schedule`, terminal to `complete`/`block`; test asserts no `ready` projection | re-audit against Hermes changes |
| Outbox retry after commit | PASS | `Ledger.project_ticket`, `board_projection_outbox`, `evidence_comments`; `test_board_failure_after_commit_does_not_repeat_model_or_commit`, `test_projection_outbox_retries_post_effect_failure_with_one_board_projection` | evidence comment is bounded/idempotent |
| Repair/restart coverage | PASS | `LocalFirstController.execute`, `InjectedCrash`, `model_stage_artifacts`, `tests/test_runtime_milestone2.py` and `tests/test_runtime_milestone2_operational.py` | live smoke deliberately not run |
| Exhaustion/verification failure | PASS | `DeterministicValidator` launch/timeout handling, bounded attempts and triage transitions; operational integration tests | no automatic decomposition |
| Crash-safe stage replay | PARTIAL | persisted implementation/review provenance and worktree/diff reconciliation in `Ledger`/`LocalFirstController`; operational replay tests pass | restart coverage is fake-only; no live smoke |
| Evidence-comment projection | PARTIAL | `Ledger.project_ticket`, `HermesBoardAdapter.add_comment`, `evidence_comments` | comment delivery bypasses a dedicated outbox row; no failure/idempotence integration test |
| Process-level Hermes fixture | MISSING | no `tests/fixtures/fake_hermes.py` exists in the current package | add only in a later milestone |
| daemon mode | MISSING | deliberately excluded | later milestone |
