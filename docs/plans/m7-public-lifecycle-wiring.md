# M7 Public Lifecycle Wiring Implementation Plan

> **For Hermes:** Implement feature-first in the isolated checkout; retain the existing focused public-core fixtures and obtain independent specification and quality review only after the whole slice is wired.

**Goal:** expose the already-implemented, evidence-bound Local First lifecycle through narrow trusted operator commands and worker-owned tools, so a configured disposable workflow can progress from enrollment through accepted-tranche successor authorization without callers forging provenance or invoking private domain methods.

**Architecture:** Keep `Coordinator` as the only lifecycle authority and use the existing composition root, configured roles, native board adapter, evidence store, locks, Git adapter, budgets, replay receipts, and readback gates. Add explicit versioned CLI/tool contracts that select persisted identities and delegate to public coordinator façades; do not add a generic method-call command, a second scheduler, provider calls, or autonomous advancement in `tick()`.

**Tech stack:** Python/argparse, existing plugin composition, SQLite evidence store, supported Hermes Kanban adapter, real disposable Git fixture, pytest/jsonschema.

**Fixed scope and non-goals**

- Work only in this plugin checkout. Do not modify Hermes core, profiles, services, installed plugin state, real boards, provider routes, or host Kanban data.
- Preserve all existing dirty paths and untracked `build/`, `local_first_orchestrator.egg-info/`, and `uv.lock`; no commit/push/reset/cleanup.
- `tick()` remains containment/reconciliation-oriented. The documented workflow is closed by deliberate operator commands plus worker-owned tool calls, not by inventing effectful polling policy.
- A plan proposal, a held created card, a release, a local approval, integration, paid-review completion, and tranche acceptance remain separate decisions. Never infer approval from `done`.
- Existing historical fixture evidence (including the recorded 226-native-fixture run) is not provider-backed operational proof and does not make M7/cutover GO. This new source plan supersedes no historical candidate hash. Bootstrap is a coordinator-locked operator transition: active pause/cancellation returns held before request construction; the evidence store atomically decides scope-wide first-bootstrap immutability across independent connections; composition pins `ls-tree` to the initial full SHA and rejects a final-HEAD mismatch before persistence. Planner limits are fixed code-defined bounds, not configurable bootstrap fields.

## Grounded lifecycle inventory

The existing domain API already provides the core transitions, but the public CLI currently exposes only `status`, `initialize-store`, `enroll`, `pause`, `reconcile`, `resume`, `cancel`, `recover`, and `run`; worker tools expose planning/local-review/correction/recovery/status only. The lifecycle gap is composition/wiring, not an absence of all domain operations.

| Lifecycle transition | Existing public domain method(s) | Required public surface |
|---|---|---|
| First-time state and root hold | `initialize_store`, `Coordinator.enroll()` | Existing `initialize-store`, `enroll` |
| Paid planner held-create/release | `prepare_planner(request_id=...)`, `release_planner(request_id=...)` | `prepare-planner`, `release-planner` |
| Bind claimed planner run to persisted request | `register_planning_request(planner_task_id, request_id=...)` | **New worker tool** `local_first_register_planning_request` (not a free-form CLI proof) |
| Persist validated planner proposal | `submit_plan(proposal_json, request_id=...)` | Existing `local_first_submit_plan` |
| Explicit accepted-plan decision | `accept_validated_plan(plan_id, request_id=...)` | `accept-plan` |
| Held active-piece creation/link/release | `prepare_active_piece(plan_id,ticket_id,request_id=...)`, `execute_accepted_dependency_link(plan_id,child_ticket_id,parent_ticket_id)`, `release_active_piece(plan_id,ticket_id)` | `prepare-piece`, `link-piece`, `release-piece` |
| Worker-owned local review and corrections | `request_local_review`, `submit_review`, `request_corrections`, `reconcile_local_corrections` | existing worker tools plus an identity-only operator reconciliation command if needed; no operator verdict submission |
| Integrate exactly reviewed candidate | `integrate_active_piece(plan_id,ticket_id,candidate,review_id=...,git_adapter=...)` | `integrate-piece --plan-id --ticket-id --review-id`, resolving candidate from durable evidence rather than parsing caller evidence |
| Paid review held-create/release/submission | `prepare_paid_integrated_review`, `release_paid_integrated_review`, `submit_paid_integrated_review` | `prepare-paid-review`, `release-paid-review`, **new worker tool** `local_first_submit_paid_review` |
| Paid findings correction create/release | `prepare_paid_correction`, `release_paid_correction` | `prepare-paid-correction --plan-id --review-id`, `release-paid-correction --plan-id --review-id` |
| Accepted tranche/successor decision | `accept_tranche(plan_id,review_id,git_adapter=...,authorize_successor=True)` | `accept-tranche --plan-id --review-id --authorize-successor` |

Commands that select a plan/ticket/review use only bounded opaque IDs already persisted for the configured scope. They must reject unknown IDs, ambiguous current requests/reviews, stale/changed candidates, active pause/cancellation, role drift, unsupported native capability, and unknown effects before a board/Git effect. They return the coordinator's structured result and existing exit categories.

## Required commands and tools

### Operator CLI commands

All retain existing required global `--config`, `--board`, and `--anchor-task-id`; composition must reject a request scope different from the trusted config scope.

```text
prepare-planner [--request-id <opaque-id>]
release-planner [--request-id <opaque-id>]
accept-plan --plan-id <plan-id> [--request-id <opaque-id>]
prepare-piece --plan-id <plan-id> --ticket-id <ticket-id> [--request-id <opaque-id>]
link-piece --plan-id <plan-id> --child-ticket-id <ticket-id> --parent-ticket-id <ticket-id>
release-piece --plan-id <plan-id> --ticket-id <ticket-id>
integrate-piece --plan-id <plan-id> --ticket-id <ticket-id> --review-id <review-id>
prepare-paid-review --plan-id <plan-id>
release-paid-review --plan-id <plan-id>
prepare-paid-correction --plan-id <plan-id> --review-id <review-id>
release-paid-correction --plan-id <plan-id> --review-id <review-id>
accept-tranche --plan-id <plan-id> --review-id <review-id> --authorize-successor
```

`integrate-piece`, paid-review, paid-correction, and acceptance commands construct their `GitWorktreeAdapter` only from trusted configuration roots and resolve candidate/review data from the evidence store through new explicit coordinator façade methods. Do not expose `--candidate-json`, `--review-json`, a workspace override, role override, executable override, or arbitrary method/action name.

### Worker-owned tools

1. Add `local_first_register_planning_request` with only optional `request_id` beyond required scope. It derives native task/run/session/board from the trusted worker environment and calls the existing `register_planning_request`; the worker cannot select planner task/profile/run identity.
2. Add `local_first_submit_paid_review` with `plan_id` and structured `review` beyond required scope. The handler verifies active worker task/run/session/board, the configured **distinct** paid-review profile, and the persisted current paid-review request before calling a coordinator method that derives the frozen integrated candidate from that request. It must not let an operator or paid reviewer provide arbitrary candidate authority.
3. Retain existing local-review/correction ownership semantics. Do not make CLI `submit_review` or `submit_paid_review` commands and do not make the coordinator impersonate `kanban_request_review`/`kanban_request_changes`.

## Implementation tasks

### Task 1: Add coordinator public façades for persisted-identity lifecycle transitions

**Files**
- Modify: `local_first_orchestrator/coordinator.py`
- Test: `tests/test_m7_public_lifecycle_wiring.py` (new)

Add small public façade methods that accept only plan/ticket/review/request IDs and trusted injected Git adapter where needed. Each façade must load and validate the exact durable request, candidate, local review, paid-review request, and configured role before delegating to the existing transition methods listed above. Include a paid-review worker submission façade that obtains candidate/head from the exact held/released paid-review request, not a caller payload.

Keep existing domain method signatures compatible. Do not move lifecycle logic into CLI or tools. Add a focused positive path asserting each façade reaches its existing effect/receipt and negative tests for forged candidate/review IDs, stale head, role mismatch, active pause, and ambiguous/unknown operation with no added operations, budget events, native calls, or Git advancement.

### Task 2: Wire bounded CLI parser and handlers

**Files**
- Modify: `local_first_orchestrator/cli.py`
- Modify: `local_first_orchestrator/composition.py` only if a small trusted Git-adapter factory is needed
- Test: `tests/test_m7_public_lifecycle_wiring.py`
- Modify: `docs/operator-guide.md`

Register precisely the commands above. Handlers build the configured runtime once, call only the façade matching the parsed command, render its structured result, and always close runtime. Create the Git adapter from `config.trusted_roots["repository"]` and `config.trusted_roots["workspace"]` (or the established configured attempt root); no CLI path/config/profile override is accepted.

Test parser contracts, exact method routing, runtime close on success/error, and fail-closed malformed/extra selector input. Ensure commands distinguish held materialization from release, paid approval from acceptance, and acceptance from successor authorization.

### Task 3: Add native-worker registration and paid-review tools

**Files**
- Modify: `local_first_orchestrator/plugin_tools.py`
- Modify: `local_first_orchestrator/coordinator.py`
- Test: `tests/test_m7_public_lifecycle_wiring.py`
- Extend: `tests/test_m6_regressions.py` only for plugin registration/runtime-close regression reuse

Add strict schemas and handlers for the two new tools. Both use `_runtime_for_args(..., require_worker=True)`, compare supplied scope with composed scope, derive native identity from environment, and close runtime in all paths. The paid tool must assert `paid_review_profile` is configured and distinct from implementation/local-review profiles; it must submit only an exact released review request’s current frozen head.

Test that missing/mismatched worker environment, board mismatch, forged task/run/session/profile/candidate inputs, and attempted replay after changed head fail closed. Test a valid synthetic worker environment records only the intended persistent evidence; native mutable transition tests remain in the authorized parent fixture context, never by disabling `HERMES_M0_CLI` guards.

### Task 4: Establish one compact full public lifecycle proof

**Files**
- Create: `tests/test_m7_public_lifecycle_wiring.py`
- Reuse/extend: `tests/test_m4_public_core_flow.py`
- Reuse: `tests/test_m4_planner_run_binding.py`, `tests/test_m4_plan_acceptance.py`, `tests/test_m4_active_release_integration.py`

Create one compact test using existing fixtures and a real disposable Git repository. Drive the actual public parser/tool handlers through: initialize/enroll; planner prepare/release; worker registration/submission; explicit acceptance; held piece materialization/link/release; worker-owned local review; real disposable-Git integration; paid review prepare/release/worker submission; paid changes; held correction prepare/release; fresh local review and integration; fresh paid approval; explicit tranche acceptance/successor authorization.

Label worker review evidence synthetic in the test name/docstring. Do not hand-insert already integrated/accepted operation rows to skip public wiring. Assert persistent request/artifact/evidence generated by the prior step supplies every subsequent instruction/selector. Add one restart/paused replay subcase that reopens the store and proves unknown effects are reconciled without resend, pause blocks release/acceptance, correction content requires fresh local and paid review, and old approval cannot accept the changed head.

### Task 5: Document only implemented public contract and renew review gates

**Files**
- Modify: `docs/operator-guide.md`
- Modify: `docs/plugin-only-recoverable-workflow.md` only to replace “proposed” wording for implemented commands/tools while preserving canonical requirements
- Modify: `docs/plans/m7-implementation-progress.md`
- Modify: `docs/release/m7-acceptance-matrix.md`

Document command ordering, required trusted config/scope, which commands are operator-owned versus worker-owned, result/exit categories, and recovery boundary. State explicitly that provider-backed execution, real-board enrollment, installation/cutover, profiles/services, and external dispatch remain pending and unauthorized.

After implementation, freeze the public-surface inventory and request independent specification and quality review over the full dirty overlay. Reviewers must exercise every mounted new command/tool and the complete public proof, not only coordinator helpers. Resolve accepted in-scope findings, rerun all verification, then keep parent-native fixture verification and provider execution as separate gates.

## Acceptance inventory

| Requirement | Positive proof | Boundary proof |
|---|---|---|
| Planner can enter from supported public surface | CLI prepare/release plus worker registration/submission records one exact bound planner request/proposal | CLI cannot forge planner run/profile; no release without current held-root/effect proof |
| Explicit plan acceptance is distinct | `accept-plan` records acceptance only | No pieces/materialization/release follows from proposal or acceptance alone except explicitly commanded transition |
| Active piece flow is operational | prepare/link/release commands create/readback held cards then release eligible pieces | Future tranche, unaccepted ticket, pause/cancel, unknown create/link remain held/no resend |
| Local review authority stays worker-owned | Existing review/correction tools use real trusted worker env fixture contract | CLI/operator cannot submit a local verdict or spoof session/profile |
| Git integration is public and exact | `integrate-piece` advances real disposable tranche ref after persisted exact local review | Stale candidate/base or changed candidate makes zero advancement |
| Paid review is a separate authority | Distinct paid-profile worker tool submits exact frozen integrated-head review after explicit prepare/release | No paid verdict from CLI, local/implementation profile, stale request, or changed head |
| Paid corrections receive fresh cycle | Paid changes command path creates/release correction; correction has new local review, integration, and new paid review | Old local/paid approval cannot authorize correction head |
| Tranche acceptance/successor is explicit | `accept-tranche --authorize-successor` records exact paid approval and successor decision | Paid task `done`, release, or combined checks alone cannot materialize/accept successor |
| Replay/recovery remains safe | Reopen/paused replay subcase reconciles existing receipts | Unknown effect gets no resend; pause/cancel blocks late release/acceptance |
| Packaging/composition remains plugin-only | Full suite/import/discovery tests still pass with temporary fixture runtime | No Hermes core/edit/install/live board/profile/provider/service action |

## Verification commands

Run in the development checkout after implementation:

```bash
PYTHONPATH=. HERMES_M0_CLI='' uv run --offline --no-project --with pytest --with jsonschema python -m pytest -q tests/test_m7_public_lifecycle_wiring.py tests/test_m6_regressions.py tests/test_m4_public_core_flow.py
PYTHONPATH=. HERMES_M0_CLI='' uv run --offline --no-project --with pytest --with jsonschema python -m pytest -q
git diff --check
```

When the authorized parent fixture context is available, run the exact parent-native command with its pinned `HERMES_M0_CLI` and existing environment; do not clear the child guard to simulate it. If browser dependencies are available, use the established `NODE_PATH=/home/ocadmin/.hermes/cache/scratch/m6-browser-node/node_modules` and `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/home/ocadmin/.hermes/tools/chromium-1208/chrome-linux64/chrome` only for the existing dashboard proof. Provider-backed/real-worker execution is intentionally not part of this implementation stage.

## Dependency and blocker decisions

- **No new Hermes API/core dependency:** existing adapter operations and coordinator domain methods are sufficient for a manual explicit lifecycle. A generic autonomous `tick()` is rejected because it would create additional effect policy and obscure authority.
- **Paid review needs a new worker tool:** `submit_paid_integrated_review` exists, but there is no worker-owned public tool. It must preserve native environment/provenance and configured distinct profile binding.
- **Planner registration needs a new worker tool:** its current method requires trusted worker run/session context. An operator CLI command would either be unusable or invite forged provenance.
- **Public integration needs identity resolution:** current method takes a `CandidateIdentity`; the public CLI must not. Add façade/store lookup, not JSON evidence flags.
- **Operational provider proof remains blocked/pending:** this task must not call providers or dispatch real work. Isolated fixture and disposable-Git proof can establish wiring only.
- **No cutover decision:** current M7 documents retain NO-GO pending independent renewed reviews, provider/real-worker lifecycle proof under separate authorization, and compatibility/cutover decision.
