# Local First operator guide (M6 replacement)

This guide describes the implemented **isolated** plugin surfaces. It does not authorize plugin enablement, board enrollment, dispatch, provider requests, profile changes, or modification of an existing Hermes/Local First database.

## Configuration

The command requires a trusted, local JSON configuration and the one configured native board/anchor scope:

```bash
local-first-orchestrator \
  --config /absolute/private/local-first.json \
  --board <configured-board-id> \
  --anchor-task-id <configured-anchor-id> \
  status
```

`--board` and `--anchor-task-id` must exactly match the `scope` in the JSON file. Browser and tool payloads cannot select the state root, Hermes executable/home, repository/workspace roots, role profiles, budget limits, or check commands.

The JSON schema is version `1` and has exactly these top-level fields:

```json
{
  "version": 1,
  "state_root": "/absolute/private/state-directory",
  "hermes_executable": "/absolute/path/to/hermes",
  "hermes_home": "/absolute/existing/hermes-home",
  "kanban_home": "/absolute/existing/kanban-home",
  "trusted_roots": {"repository": "/absolute/git-repository", "workspace": "/absolute/workspace"},
  "roles": {"implementation_profile": "...", "local_review_profile": "...", "planning_profile": "...", "paid_review_profile": "..."},
  "budgets": {"implementation_attempts": 1, "review_corrections": 1, "infrastructure_retries": 1, "workflow_repairs": 1, "paid_capacity": 1},
  "poll_interval_seconds": 15,
  "scope": {"board_id": "...", "anchor_task_id": "..."},
  "check_commands": [{"check_id": "checks", "argv": ["/absolute/executable", "arg"]}]
}
```

Directories and executables must already exist, be canonical non-symlink paths, and `state_root` must not be group/world accessible. Roles are distinct. Budgets are finite non-negative integers. The store is plugin-owned `evidence.sqlite3` under `state_root`; it is not Hermes `kanban.db` and is never created by normal command execution.

## Initialize a new isolated store

Only create the configured empty evidence store deliberately:

```bash
local-first-orchestrator --config /absolute/private/local-first.json \
  --board <configured-board-id> --anchor-task-id <configured-anchor-id> \
  initialize-store
```

This is the sole first-run initialization path. It does not enroll the anchor or write to a Hermes board.

## Supported commands

| Command | Effect |
| --- | --- |
| `status` | Read scoped coordinator/evidence status. |
| `initialize-store` | Explicitly create/migrate an empty plugin evidence store. |
| `bootstrap-planning --request-file PATH [--request-id ID]` | Persist one operator-owned initial planning request before a planner card or worker run exists. |
| `enroll` | Uses the implemented bounded native enrollment path; unsupported host capability is reported, never emulated. |
| `pause [--stop]` | Persist scoped pause; `--stop` requests supported native run stops and exposes partial containment. |
| `reconcile` | Read/reconcile known scoped evidence and native observations. |
| `finalize-local-review --operation-key KEY` | Read-only: finalize one provisional implementation-to-review handoff after its native worker-owned transition. It never sends `kanban_request_review`. |
| `resume [--authorized-clear]` | Resume only according to durable operator intent. |
| `cancel` | Persist cancellation and reject late results. |
| `recover` | Propose/apply only bounded deterministic recovery. |
| `run [--once]` | Explicit coordinator loop; `--once` is the fixture/operator smoke tick. |
| `prepare-planner [--request-id ID]` / `release-planner [--request-id ID]` | Create then explicitly release the exact held planner request. |
| `accept-plan --plan-id ID [--request-id ID]` | Persist plan acceptance only; it does not materialize or release a piece. |
| `prepare-piece --plan-id ID --ticket-id ID [--request-id ID]` / `link-piece --plan-id ID --child-ticket-id ID --parent-ticket-id ID` / `release-piece --plan-id ID --ticket-id ID` | Explicit held piece lifecycle transitions. |
| `integrate-piece --plan-id ID --ticket-id ID --review-id ID` | Integrate only the candidate recovered from the exact persisted local-review receipt. |
| `prepare-paid-review --plan-id ID` / `release-paid-review --plan-id ID` | Create then explicitly release the current integrated-head paid review. |
| `prepare-paid-correction --plan-id ID --review-id ID` / `release-paid-correction --plan-id ID --review-id ID` | Create then explicitly release a correction bound to paid findings. |
| `accept-tranche --plan-id ID --review-id ID --authorize-successor` | Persist exact paid approval and the explicit successor decision; successor materialization remains a separate admission. |

Results are JSON. Exit code `0` means verified/recorded/deduplicated/no-op, `3` held, `4` partial, `5` conflict, `6` unsupported, `2` invalid input, and `7` unknown.

`status` is an operator read surface. Its `repository_telemetry` reports only the configured repository's current full `head_sha` and `clean` flag, or a structured unavailable error. It never runs configured checks and never invents a candidate, worker run, or review provenance. Candidate/check authority remains worker-only: it requires the trusted native task, run, session, and matching board environment, and rejects missing or mismatched worker context.

Planning bootstrap is an explicit operator step after `initialize-store` and before `prepare-planner`: `bootstrap-planning --request-file /absolute/request.json --request-id initial`. The version-1 request file has exactly `version`, `objective`, `non_goals`, `criteria`, and `authorized_paths`; each criterion is `{ "id": "...", "statement": "..." }`. The operator supplies the bounded objective/criteria/non-goals/path contract. Under the scoped coordinator lock, composition supplies configured scope, repository/workspace roots, roles, fixed code-defined planner limits, and check policy, then observes a clean configured Git HEAD. The tree is read by that full SHA and a final HEAD read rejects drift before persistence. Replaying identical bootstrap input is safe; changed input or a second request conflicts atomically even across independent evidence-store connections. An active pause/cancellation returns `held` before request/Git observation or persistence. Bootstrap creates no board card, worker registration, run binding, capacity charge, release, or plan acceptance.

Planner registration and paid-review verdicts are worker-owned tools, not CLI commands: `local_first_register_planning_request` derives its task/run/session from the native worker environment, while `local_first_submit_paid_review` accepts only an active configured paid-review worker's review record. `local_first_request_local_review` is the implementation worker's public preparation surface: it derives task/run/session/board from the native environment and resolves the configured Git candidate and checks itself; callers cannot submit candidate, check, profile, or session fields. It records an immutable **provisional** candidate/check snapshot and unique review marker, but the active session is not authority. The implementation worker then calls native `kanban_request_review` with that exact marker. An operator invokes `finalize-local-review --operation-key KEY` after the native transition; it reads the exact ended implementation run and `review_requested` event, verifies terminal `metadata.worker_session_id` and `metadata.local_first_review`, then writes a separate immutable finalized handoff. `local_first_status` exposes `review_handoffs` as `pending` before that step and `finalized` afterward. Only the finalized record is reviewer-readable authority; review submission rejects a changed candidate/check or missing finalization without re-running the implementation observer. Missing, forged, or cross-board context is rejected. Bootstrap does not replace planner registration: after explicit release, the native planner worker must still call `local_first_register_planning_request` before submitting the matching plan. Neither worker tool accepts a caller-selected task, run, profile, repository, workspace, candidate, or Git command. The normal `local_first_submit_plan`, local-review, and correction tools retain their existing ownership rules.

#### Active implementation-to-review handoff capability

The currently characterized native host exposes exact `worker_session_id` only in terminal run metadata. `local_first_request_local_review` may therefore prepare while its active environment matches task/run/board, but cannot make that session authoritative. A same-profile session row, profile/session timestamps, a first message, a work query, and environment values are not substitutes. Finalization fails closed unless the terminal run itself binds the same session and exact marker.

#### Revised review requirement: gate authority, not worker startup

Native reviewer dispatch may occur before Local First finalizes the implementation handoff. Starting a reviewer consumes worker/provider capacity but grants no evidence, approval, integration, correction, paid-review, or tranche-acceptance authority. No plugin-level dispatcher barrier is required.

The required sequence is provisional preparation → native worker-owned `kanban_request_review` → exact native event/terminal-run verification → immutable handoff finalization → authoritative reviewer evidence consumption/submission. Preparation may capture configured candidate/check data and a unique marker, but its claimed session is provisional until the exact native terminal handoff proves it. Profile/session timestamps and caller assertions remain insufficient ownership proof.

Public status must distinguish pending preparation from finalized evidence. Before finalization, the reviewer-facing authority route must return an explicit pending/held result rather than expose provisional records as authoritative. A reviewer may start and observe pending status, but must not submit an accepted verdict or modify the candidate. Pending waits must be bounded; timeout or missing/conflicting evidence yields held/blocked, not inferred approval. Readiness retries do not repeat the native handoff, reset budgets, or imply authorization for another dispatch.

Finalization must bind the exact scoped task/run/profile/session, native review event and unique marker to the unchanged prepared candidate/check snapshot. If a reviewer has already claimed the card, finalization must resolve the ended implementation run and handoff event rather than assume the implementation is still active or the card is still in the review lane. Missing or contradictory evidence fails closed. Unknown transition outcomes reconcile existing evidence without blind resend.

Review submission, integration, correction admission, paid-review advancement, and tranche acceptance remain gated by finalized immutable evidence plus their existing native ownership and approval requirements. A completion lane alone is not approval. Keep process-cleanup ownership separate: exact native task/run/PID/start evidence may prove which process can be stopped without establishing review-session authority.

Acceptance requires a joined public-path proof with an early-starting reviewer: pending evidence cannot authorize submission/integration; exact native terminal proof enables finalization; changed candidate/marker/run is rejected; and restart after native transition recovers without repeating it. No core Hermes modification, custom sandbox, or cross-tool command blacklist is required by this plugin-only workflow. Separately authorized native verification remains a cutover gate.

## Native plugin and dashboard

The root plugin registration exposes the same parser through `hermes local-first-orchestrator`. The plugin also registers proposal/status tools and advisory hooks without opening the evidence store or starting a coordinator during registration.

The wheel is a Python distribution, while Hermes discovers native plugins from a directory containing `plugin.yaml` and root `__init__.py`. Hermes does not automatically turn `pip install local-first-orchestrator` into a discovered directory plugin. For a supported offline artifact installation, install the wheel in the target Python environment, then place the wheel's `*.data/data/local-first-orchestrator/` payload intact at `$HERMES_HOME/plugins/local-first-orchestrator/` and explicitly enable `local-first-orchestrator` in that target home's `config.yaml`. The payload includes its artifact-local `python/local_first_orchestrator/` package; do not copy source checkout files or reuse a different host package. Validate the installed directory with `hermes plugins doctor "$HERMES_HOME/plugins/local-first-orchestrator" --ci`, then restart Hermes only when a separate activation authorization exists.

The mounted dashboard API provides `status`, `profiles`, `configuration`, `enroll`, and scoped `pause`, `stop`, `reconcile`, `resume`, `cancel`, and `recover` actions. Mutations require the current observation digest; stale observations return a conflict with fresh status. `profiles` returns only the four distinct profiles already assigned by trusted bootstrap and `enroll` can target only the configured anchor. Partial stops remain visible as uncontained workers.

`POST /configuration` (also exposed as `update_configuration`) requires the current configuration digest and accepts exactly `poll_interval_seconds` plus all configured budget categories. It can set a 1–3600 second poll interval and **tighten** non-negative limits only. It cannot increase a budget or reset consumed evidence. It writes only the server-selected trusted bootstrap file under the coordinator lock, validates it with `PluginConfig`, and never accepts a browser-selected configuration path, state root, repository/workspace, executable, Hermes home, board/anchor, profile, check command, or task ID.

The dashboard's **Runtime metrics** card restores the historical operational summary using only current scoped evidence and the current native-board readback: observed run lanes, operation phases, and recorded reviews. Review counters and queues derive from persisted `reviewer_role` (`local` or `paid`). **Budget usage by finding** renders each recorded `(root_task_id, finding_id, category)` ledger row as its net consumption, configured per-finding/category limit, and remaining capacity; the displayed total is explicitly a cross-row consumption sum, never an aggregate enforcement ceiling. Categories without ledger rows are reported as such rather than implying a charge or an empty finding. It explicitly reports coordinator heartbeat and worker-process liveness as `Unknown` because the mounted observer does not persist either signal; an active native run lane is not treated as proof of a live process. It does not estimate provider costs or invent runtime samples.

Blank, non-finite, non-integer, or out-of-range numeric fields are rejected inline before the browser sends a configuration request. Budget `0` is an intentional valid tightening; poll interval remains 1–3600 seconds.

## Operational boundaries

- Native board operations use supported Hermes CLI/plugin adapters and read back effects. No native database SQL is written.
- No host monkey-patching, direct provider invocation, privileged recovery helper, or automatic worktree deletion is supported.
- A hold, failed stop, unknown effect, missing evidence, or unsupported capability is surfaced; it is not converted to success.
- Existing legacy stores, boards, worktrees, profiles, and user data are not migrated, resumed, or deleted.

### Worker execution safety is an external boundary

Local First coordinates workflow authority, evidence, budgets, and supported native lifecycle operations. It is not a filesystem sandbox or a general-purpose command security engine. Configured trusted roots, workspace routing, accepted path limits, disposable Hermes homes, and command approval checks do not confine arbitrary worker shell or Python execution on the host.

When risky worker commands require enforced containment, the operator must provide an established OS/container sandbox outside this plugin. Restrict writable mounts to the authorized workspace and disposable runtime state, isolate temporary/cache storage, and grant only the provider/network access required for the run. Verify that the chosen environment preserves supported native task/run/session ownership before dispatch; sandbox compatibility is not implied by fixture or provider success. Without that boundary, execution relies on the operator's trust in the worker and its host privileges and must not be described as sandboxed.

Do not add a plugin-level recursive-delete detector, cross-tool command blacklist, or custom sandbox to compensate for host execution limitations. Hermes approval controls remain useful defense in depth, but a blocked shell spelling is not proof that the equivalent operation is impossible through Python or another tool. Workers should report denied destructive operations rather than reformulate them; this instruction is behavioral guidance, not an enforcement guarantee. Do not weaken approvals to complete a rehearsal.

Prepare authorized test execution so routine checks do not demand destructive cleanup: disable Python bytecode and pytest cache generation where appropriate, locate needed temporary files under disposable state, and configure repository-local commit identity before dispatch. Cancellation must still expose partial containment and verify exact native run/process state; persisting cancellation intent alone does not prove worker termination. These are harness/lifecycle responsibilities, not replacements for an external sandbox.

## M7 release readiness and cutover boundary

The [M7 cutover checklist](release/m7-cutover-checklist.md) and [rollback package runbook](release/m7-rollback-runbook.md) are release-readiness artifacts only. They require explicit STOP authorization for target capture, installation/enablement, disposable native proving, scope expansion, and any post-effect reconciliation. The fixture rollback archive covers only an allowlisted plugin artifact, named trusted bootstrap, and SQLite-backup-API copy of plugin-owned evidence; it cannot restore `kanban.db`, native cards/runs, providers, profiles, services, or an installed legacy plugin.

## Status

M6 source and fixture verification are complete only to the extent recorded in `docs/plans/m6-implementation-progress.md`. Fresh independent review and separately authorized live activation remain paused M7/cutover gates.
