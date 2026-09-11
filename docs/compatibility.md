# Hermes Kanban compatibility plan — Phase 0

> **Status note — 2026-09-11:** This file is the historical Phase-0 discovery/compatibility baseline, not the current implementation status. The architecture intentionally preserves the findings below, but many statements phrased as “future”, “later”, or “Phase 1” have since been implemented. Current status is tracked in `docs/design-v2.md`, the completed scheduler roadmap in `docs/scheduler-plan.md`, and real integration evidence in `docs/milestone-20-real-acceptance.md`.

### Current disposition of the original compatibility risks

- The separate Local First ledger remains authoritative for fine-grained lifecycle/provenance; Hermes remains the user-facing board.
- A real `HermesBoardAdapter`, retry-safe board/comment outboxes, live state/comment completion projection, native dependency links, and marker-based comment reconciliation are implemented and were exercised against a real Hermes board in Milestone 20.
- Isolated Git worktrees, base/diff provenance, dirty-tree refusal, deterministic validation, a real local implementation-model path, an independent review worker, paid-call governance, and the restartable scheduler/daemon now exist.
- Bounded triage/decomposition and idempotent generated-card projection are implemented; existing Hermes auto-decomposition remains outside controller ownership.
- Hermes-dispatched execution reconciliation is now implemented and live-proven for the representative generated-ticket path: generated tickets stay Hermes-owned for implementation, blocked handoff runs are automatically reconciled into Local First attempts, Local First does not launch a duplicate implementation model, and Hermes remains non-final until Local First validation/review/integration authorizes `done`.
- The full multi-ticket scheduler-generated native tranche graph is now live-proven: exact generated dependency projection, Hermes-native `todo -> ready`, dispatcher-owned execution for each ticket, rolling integration-head worktree preparation, tranche integration/checkpoint, real standard successor planning, successor card projection, exact activation evidence, and successor runtime binding at the predecessor final integration SHA are all exercised.
- The principal unresolved compatibility boundary is now **exhaustive live external-boundary crash/restart proof** across model, review, Git, board, dependency, checkpoint, and paid boundaries. Live paid-provider checkpoint/escalation operation remains a separate following item.


## Inspected runtime and entry points

This package is intentionally separate from `/home/ocadmin/.hermes/hermes-agent` and does not import it. The inventory below is based on the current source inspected on 2026-08-27.

| Existing surface | Evidence | Classification | Phase-1 decision |
| --- | --- | --- | --- |
| Board storage and lifecycle | `hermes_cli/kanban_db.py:1333-1527`, `3158`, `4617`, `4947`, `5114`, `5352`, `6246`, `6490`, `6652`, `6890`, `7513` | keep | Hermes remains the user-facing board and its SQLite schema is not changed. |
| CLI and slash-command board surface | `hermes_cli/kanban.py:1-28`, `216`, `1025`; `hermes_cli/main.py:5545`; `cli.py:11597`; `gateway/slash_commands.py:459` | keep | No core command is added. This project has its own `local-first-orchestrator` inspection CLI only. |
| Worker model-tool surface | `tools/kanban_tools.py:1-27`, `103-135`, `215-227`, `898-1021`, `1904-2405`; `run_agent.py:3953-3964` | wrap later | Future adapter calls must be controller-owned; Phase 1 ships only `BoardAdapter` protocol and `FakeBoardAdapter`. |
| Dashboard API | `plugins/kanban/dashboard/plugin_api.py:623-679`, `865-931`, `1344-1364`, `1740`, `1778` | wrap later | Dashboard remains an independent direct board writer until an explicit projection/outbox integration phase. |
| Gateway dispatcher and worker spawning | `gateway/kanban_watchers.py:1182-1198`, `1226-1255`, `1433-1484`, `1581-1652`; `hermes_cli/kanban_db.py:9808-9836` | keep/wrap later | Preserve its singleton/board locks and profile spawning. Do not enable this ledger to spawn a model or worker in Phase 1. |
| Review lifecycle | `hermes_cli/kanban_db.py:4739`, `5352`, `6490-6649`, `6652`; `hermes_cli/kanban.py:658-688`, `2418-2476`; `hermes_cli/goals.py:2115-2210` | keep/wrap later | Native `review` routing is retained; local fresh-session review is explicitly Phase 3. |
| Triage specification/decomposition | `hermes_cli/kanban_specify.py:1-29`, `142-190`; `gateway/kanban_watchers.py:1581-1652`; `hermes_cli/kanban_diagnostics.py:376-421` | refactor/wrap later | Existing auxiliary-LLM triage can create/flesh tasks today. Do not call it in Phase 1; controller must validate bounded child proposals before future adapter projection. |
| Existing delegated-subagent Git isolation | `tools/subagent_worktree.py:1-35`, `120-172` | keep as reference/wrap later | It is opt-in and specific to delegated subagents, not a per-ticket orchestrator attempt adapter. Phase 2 must supply dedicated worktree metadata and fail closed on dirty checkout conflicts. |

## Current board schema and direct mutation points

Current Hermes `tasks` has one `status` column, claim lock/expiry fields, workspace/branch metadata, task graph links, events, runs, attachments and notification subscriptions (`hermes_cli/kanban_db.py:1333-1527`). The dispatcher atomically claims `ready -> running` and writes a run/event (`4617-4736`); it separately supports `review -> running` (`4739-4827`).

Direct board-mutation entry points that a later real adapter must isolate are:

- `kanban_db.create_task`, `link_tasks`, `add_comment`, `claim_task`, `claim_review_task`, `release_stale_claims`, `reclaim_task`, `complete_task`, `block_task`, `request_review`, `request_changes`, `unblock_task`, and `archive_task`.
- `tools/kanban_tools.py` mutation handlers, notably `_handle_request_review` and `_handle_request_changes`, connect directly to `kanban_db` at lines 215-227.
- `plugins/kanban/dashboard/plugin_api.py` routes call `kanban_db.create_task` directly at lines 623-679 and other direct state endpoints call complete/block/review/unblock at lines 900-931 and 1344-1364.
- CLI/gateway command handlers route to the same `kanban_db` functions, while the gateway dispatcher calls `kanban_db.dispatch_once`.
- Triage paths are mutation-capable: `hermes_cli/kanban_specify.py` calls an auxiliary LLM then moves `triage -> todo`; the gateway auto-decomposer invokes `hermes_cli.kanban_decompose.decompose_task`.

## State mapping

The ledger keeps canonical state separately; projection is deliberately not active in Phase 1.

| Hermes board status | Canonical state mapping | Notes |
| --- | --- | --- |
| `todo` | `draft` / `needs_architecture` | Depends on readiness and parent gating, which native board does not encode separately. |
| `ready` | `ready_local` | Only after the future readiness validator passes. |
| `running` | `implementing`, `verifying`, `local_review`, or `repairing` | Native board has one execution column; ledger event/stage state disambiguates it. |
| `review` | `local_review` or external review handoff | Native review is a worker-routing lifecycle; it is not equivalent to a future local model review in all cases. |
| `blocked` | `blocked`, `needs_human_test`, or `needs_checkpoint` | Reason/policy determines the canonical state. |
| `triage` | `needs_triage` | Existing paths may invoke an auxiliary LLM; Phase 1 does not. |
| `done` | `done` | Only after canonical acceptance/completion policy. |
| `archived` | no automatic canonical projection | Preservation/archive is a board retention concern; canonical `rejected`/`reverted` require an explicit immutable event. |

## Why a separate ledger and adapter projection

Hermes Kanban is the established shared user-facing board and has active dispatcher, dashboard, tool, CLI, and gateway writers. The controller needs a more granular and restart-safe authoritative record for canonical stages, immutable transition evidence, attempt uniqueness, finding fingerprints, stage idempotency, and controller pause state. Altering `~/.hermes/kanban.db` would couple new orchestration semantics to all existing writers and risk live tickets.

Therefore this package stores Phase-1 state only in its configured distinct SQLite database. A `BoardAdapter` is the sole future write boundary, and Phase 1 provides only a no-I/O fake adapter. Later projection must use a retryable outbox keyed by ledger event, so a failed board projection cannot repeat model work.

## Unresolved compatibility and risk boundaries

1. A real adapter must reconcile different native status semantics, review claims, dependency promotion, and manual dashboard drag/drop without overwriting operator actions.
2. Existing triage specification/auto-decompose performs auxiliary-model calls and creates tasks. It must remain disabled from controller execution until bounded child contracts, criteria linkage, and idempotent projection exist.
3. Existing dispatcher spawning and the delegated-subagent worktree helper are not a safe ticket-attempt runner. Phase 2 needs isolated ticket worktrees, recorded base/diff hashes, dirty-tree refusal, and no default-branch auto-merge.
4. Native `task_events` are auditable but do not represent all canonical stages. Projection drift reconciliation and event ordering remain later work.
5. The package currently accepts JSON configuration only, deliberately avoiding a dependency on Hermes configuration/loaders. A future integration can add an explicit config bridge without editing `config.yaml` or importing profile credentials.
6. No local or paid model adapter, usage governor, validation runner, review adapter, Git adapter, or real board adapter exists in Phase 1.
