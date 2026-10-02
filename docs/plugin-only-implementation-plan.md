# Plugin-only Recoverable Local First: Implementation Plan for GPT 6 Sol

**Status:** implementation specification, not implemented behavior. This documentation task does not authorize execution, deployment, live enrollment, migration, or deletion of runtime data.

**Goal:** replace the ledger-centric controller with a plugin-only, board-native workflow that decomposes tranche requests, uses local implementation and fresh local review, obtains paid tranche review, repairs ordinary mistakes automatically, and offers simple human recovery when repair is exhausted.

**Architecture:** Hermes Kanban owns work state and dispatch; Git owns code revisions. One plugin coordinator reconciles their evidence, applies bounded native operations, and controls release of managed work. Small plugin persistence records evidence, operator intent, budgets and ambiguous operations, never a second task lifecycle.

**Tech stack:** existing Python package and Hermes plugin registration, supported Hermes CLI/tool APIs, Git, a small user-owned SQLite evidence/operation store, and the existing plain JavaScript dashboard with FastAPI routes. SQLite is a storage mechanism here, not a replacement board. No cryptographic recovery dependency is required.

**Canonical behavior:** [Plugin-only recoverable workflow](plugin-only-recoverable-workflow.md). That document wins on product behavior. This plan specifies implementation organization and acceptance. Do not inherit numerical policy defaults or authority requirements from obsolete designs.

## 1. Instructions to Sol

Read the canonical design, this entire plan, then inspect the installed host operations before coding. The file inventories below are exhaustive for tracked runtime/test files at the planning baseline and name proposed new files. They are a destination map, not permission to delete a subsystem before its reusable behavior is extracted.

Work in an isolated development checkout/branch. Do not use the loaded plugin directory as a live experimental runtime. Preserve existing untracked `.hermes/`, `.worktrees/`, ledgers, configuration, worktrees and artifacts. Capture source/data rollback locations before eventual cutover. Existing user work must not be reset or cleaned. No host source changes, monkey-patches, triggers, native DB writes, sudo, production model calls, or live dispatch during implementation tests.

Use tests first for each bounded change: write the failure case, exercise it, implement the behavior, then run the focused suite. Keep each implementation ticket independently understandable: exact paths, inputs/outputs, source-of-truth rule, non-goals, dependency, and verification. This plan intentionally gives pseudocode/signatures rather than implementation source.

Keep one approved workstream through the milestones. Do not solve missing host primitives by restoring the old ledger or inventing a host API. If a requested operation cannot be safely performed, provide a tested alternative or an explicit partial-recovery outcome. Review behavior at each milestone; do not claim runtime readiness from mocked tests alone.

## 2. Concrete destination architecture

```text
User CLI / dashboard                   Planner and reviewers
        |                                    |
        |                      native comments + plugin proposal tools
        +---------------- coordinator ----------------+
                              |
                    reconcile / decide / apply
                     /         |          \
            board adapter   evidence store   Git + checks
                 |          (no task lanes)       |
            Hermes CLI       operation IDs    persistent worktrees
                 |
           Native Kanban -> native dispatcher -> configured Hermes profiles
                 |
           hooks (hints only) + periodic snapshot reconciliation
```

Imports flow toward pure contracts and adapters. CLI, dashboard and tools call the same coordinator methods. Only `hermes_board.py` executes board CLI operations. Only `git_adapter.py` and hardened repository/validation helpers execute Git. Only `evidence_store.py` writes plugin SQLite. No module imports removed `Ledger`, `CanonicalState`, `LocalFirstController`, or private host DB functions. The coordinator never starts model subprocesses; native ticket assignment and native dispatch own agent runs.

### Core records (no duplicate ticket status)

- `TicketContract`: criterion IDs, objective, non-goals, allowed paths, verification commands, patch/context budgets; native board/task identity is separate from generated contract identity.
- `BoardSnapshot`: immutable observation of native task, parents, runs, comments, events and attachments; observed timestamp and digest. An observation is not a CAS guarantee.
- `ManagedMember`: board, anchor, native task ID, role, generation, original finding IDs and work association. No cached authoritative lane.
- `Candidate`: repository identity, worktree, base SHA, head SHA, content/diff identity, originating run and contract hash.
- `ReviewEvidence`: candidate/contract/check identities, local or paid role, native review task/run/session/profile, verdict, criterion evidence and findings.
- `OperationIntent`: stable key, intended native target/effect, expected observed identity, before evidence, outcome/readback, retry information. Operation phases may be pending/applied/unknown; these are effects, not task states.
- `PauseIntent`: scope, operator/automatic origin, generation, stop request and cancellation intent. Persist before attempting native holds.
- `BudgetEvent`: scope/finding generation, kind, native run or operation ID and count attribution; unique event identity prevents recounting.
- `Action` / `ActionResult`: typed proposed operation and verified/no-op/retryable/ambiguous/conflict/unsupported/partial outcome. No generic `set_state` API.

### Storage shape and limits

Use a new versioned evidence DB at an explicitly configured user-owned plugin state location, not the legacy ledger path or native `kanban.db`. Permit only: managed membership, candidate/review evidence, operation intents/observations, budget events, operator intents and necessary schema metadata. Native snapshots may be bounded diagnostic evidence, not a second continuously mirrored board. Large diffs/check output live as bounded artifact files with hashes and native attachment pointers.

Single coordinator process per configured instance; obtain an OS file lock. Commands/tools enqueue or execute bounded work under the same lock. A second process cannot begin concurrent managed ticket creation. Scope keys include board and anchor; never use bare task ID globally. Reconcile outstanding effect keys before retrying. Losing the lock stops mutations. Missing/corrupt store holds managed work where possible; do not reinitialize and forget prior budgets automatically. Recovery reconstructs only what native/Git evidence proves.

## 3. Native board contract and compatibility gate

Before implementing orchestration, record the actual target host CLI help and supported operation behavior in fixture tests. Pin a compatibility fingerprint/version in diagnostics, not a private source path in production code. The currently inspected source has post-commit hooks, model pre-tool hooks, same-card review, own-task worker mutation guards, and non-atomic creation idempotency. Treat those as tested capabilities, not permanent assumptions.

Required adapter operations: read/list tasks and runs, comments and dependencies; create held work; comment with markers; request review; request changes with correct ownership; return waiting review where supported; hold/unhold eligible work; cancel/reclaim exact owned run through supported host operations; link actual prerequisites before release; complete anchor only after evidence. If host CLI lacks one operation, expose `unsupported` and choose replacement review/correction cards or operator recovery. Do not implement it by SQL.

- `done` and `archived` both release native dependencies. Neither is approval.
- Completion may promote children and clean scratch before its hook. Hooks cannot prevent that race.
- A reviewer executes in `running`, with review provenance. Lane alone does not identify role.
- Same-card `request_changes` is not a cross-ticket reviewer power.
- Reopening done ancestors can invalidate descendants; exclude it from routine repair.
- A read followed by a CLI write is not an atomic snapshot-bound mutation. Re-read before and after, verify claims, and contain detected races. Do not claim no race is possible.
- Broad dispatch preview plus dispatch is not exact-task launch. Let native dispatch pick eligible tasks; the plugin controls managed release, not the host scheduler.

Create plans and generated cards held until their association, profile, workspace and real dependencies are verified. Unassigned is not a safe hold because host defaults may assign it. Select and test a sticky native hold. If holding a live/queued card loses a race, report active/uncontained work and attempt the supported stop path; never print full pause success from intention alone.

## 4. Normal workflow and interactions

### Enrollment and paid planning

`enroll(board, anchor)` accepts an explicitly selected non-running card after inspection. Reject live claim adoption in V1 with a recovery instruction; do not race the dispatcher to seize it. Hold the anchor through supported operations before creating managed work. A model-created planning ticket uses an existing configured paid profile; it returns a structured plan proposal rather than creating implementation cards independently.

Planning and release require exactly one enrolled root member whose task identity is the selected anchor, plus one current matching durable supported native hold receipt. Verify its scoped native marker, comment, and event against a fresh blocked snapshot and complete immutable receipt content, excluding only observation time. Historical stale hold receipts remain preserved and do not count as current authority; terminal hold-run history is allowed, but active workers are not. Root associations are opaque and are not interpreted as a literal. Missing, ambiguous, stale, ready, or live anchor evidence is a pre-reservation rejection; unknown effects remain reconciliation-only and are never resent.

Validate plan IDs, duplicate IDs, coverage, file limits, finite scope, dependencies and cycles. Use existing parsers but add missing checks. Bind plan to repository/base/contract. Plan changes invalidate affected downstream proposals. Only create active-tranche work; do not prebuild future tranches behind a gate that accidental done/archive can release.

The root objective and non-goals remain authoritative requirements. Every proposed tranche and ticket preserves every root non-goal, and accepted implementation-card bodies carry the original root objective, root non-goals, and original criterion statements alongside any tranche elaboration. Objective elaboration is not lexical contradiction detection: deterministic path, criterion, command, budget, dependency, and non-goal boundaries enforce scope; paid planning cannot authorize unrelated work, and original requirements remain binding through local and paid review.

### Implementation and local review

Build compact context for a persistent candidate worktree. Pin intended role/profile and actual run evidence. Native implementation handoff requests local review on the same card when legal. Plugin pre-tool hook catches ordinary wrong completion calls and returns a useful request-review instruction, but the evidence check is still mandatory because CLI/dashboard bypasses exist.

The local review must have a new native run/session, review the candidate revision and tests, and return structured findings. Never resume the implementing conversation as its own reviewer. The same underlying model is allowed. A local reviewer can request same-card changes through native ownership rules. After the implementation changes, repeat checks and fresh review.

If the card is already done, create a separate local-review card. Never reopen completed history just to make it fit the preferred loop. A result marked done without a valid verdict triggers bounded replacement review, not acceptance.

### Integration and paid review

Accept pieces only with passing checks and matching local-review evidence. Integrate serially into a dedicated tranche ref using expected-head updates. Do not automatically push/merge canonical branch. If integration changes content or introduces conflicts, resolve through correction work and rerun required checks/review; an old per-piece approval does not approve a different patch.

After all required pieces/corrections are integrated, validate the combined branch, freeze base/head and create paid review. Review findings can be posted directly as comments; managed repair creation goes through coordinator tools. Paid changes against completed tickets become correction tickets with finding lineage. Each correction passes local review, then integrated changes receive fresh paid review. Round completion is not tranche acceptance. Next tranche is materialized only after verified paid approval of the current head.

Do not complete an anchor whose existing unmanaged dependents would run against unavailable code; hold for an explicit integration/release decision. Native board freedom remains: if a user/model bypasses the plugin and starts unrelated work, report/contain managed consequences rather than claiming universal enforcement.

## 5. Recovery policy and pseudocode

```text
reconcile(scope):
    read current board, native runs, Git and plugin evidence
    resolve pending effects by exact marker/target/readback
    classify mismatches and affected work
    return observation plus bounded repair proposals

tick(scope):
    acquire instance mutation lock
    honor durable operator pause/cancel
    reconcile before choosing work
    select one next action (containment before new work)
    reserve stable operation and budget attribution
    apply supported operation
    read back exact target; verify outcome
    record evidence, then release only eligible work
    on ambiguity: reconcile; never blindly repeat

report_issue(report):
    validate scope and reporter run
    independently inspect alleged issue
    deduplicate by scope, finding and candidate generation
    queue recovery; return operation reference
```

Default repair mapping:

| Mismatch | Repair | Stop condition |
| --- | --- | --- |
| Done implementation lacks local verdict | Separate local review of preserved candidate | Candidate unavailable or retry budget exhausted |
| Done review lacks usable verdict | Replacement review of same candidate | Repeated malformed output / infrastructure failure |
| Approval is stale | Revalidate and review new head | Unexplained useful changes or scope expansion |
| Review requests corrections | Create bounded correction set; preserve original | Excess scope/count or correction cap |
| Restart after uncertain create/comment/link | Search stable markers/IDs and verify | Several plausible effects or conflicting identity |
| Duplicate unstarted cards | Hold redundant one without releasing its dependents | Both contain useful work |
| Dependent ran early | Stop/park affected work, preserve and revalidate | Cannot establish safe stop or candidate lineage |
| Native worker died | Native reclaim/retry if exact run proves failure | Live ownership ambiguity / cap |
| Human board changes | Adopt coherent state; invalidate stale evidence | Conflicting work or destructive remedy |

Separate budgets for implementation attempts, review corrections, infrastructure retries and workflow repairs. Identity of a replacement card never resets the originating limit. Use configurable finite limits; fixture values are not product defaults. Pausing and reconfiguring a profile do not reset consumed budgets. Paid capacity limits bound new paid dispatch; unknown started runs count conservatively until reconciled. Exhaustion holds affected work and emits one actionable notification. Do not automate paid escalation beyond explicitly configured authorization.

## 6. User and reviewer surfaces

All interfaces call the same coordinator and return structured results, with readable summaries. Proposed CLI prefix is `hermes local-first-orchestrator`; preserve standalone entry point only as the same handler, not a second implementation.

- `status --board B [--tranche ID] [--json]`: read-only observed state, candidate, next safe action, budgets, active workers and holds.
- `enroll --board B --task ID`: explicit opt-in and native hold handshake; no implicit board-wide adoption.
- `pause --board B (--tranche ID | --all-managed) [--stop]`: persist intent first, hold queued work; optionally stop/park active work. Report partial/uncontained result and exact remaining runs.
- `reconcile --board B (--tranche ID | --all-managed)`: inspect/adopt proven effects and return findings; never clear operator pause. Read-only board behavior unless a clearly named repair option is explicitly selected.
- `resume --board B (--tranche ID | --all-managed)`: validate the paused scope, clear only that operator intent when coherent, preserve budgets, release safely. Partial failure remains visible.
- `cancel --board B --tranche ID`: stop new release, contain managed work, invalidate late result eligibility, preserve evidence/worktrees. It is not archive/delete.
- `recover --board B --tranche ID`: explicitly run the same bounded repair logic; never overrides operator pause, scope or exhausted budget silently.
- `run`: one singleton coordinator loop, with signal shutdown and configurable polling; host lifecycle hooks merely wake it. No blocking model calls inside hook callbacks.

Implement explicit exit/result categories: successful, no-op, pending/partial, held/conflict, unsupported, invalid input. Do not use a success exit for uncontained pause-stop. Document exact values once implemented.

Plugin tools: `local_first_submit_plan`, `local_first_submit_review`, `local_first_request_corrections`, `local_first_report_issue`, `local_first_status`. Tie proposals to board/task/run/candidate, validate actual run before persistence, and acknowledge an operation ID. A tool should not execute a long repair inside the model turn. Comments alone can trigger inspection but do not grant approval or authority.

Dashboard: retain Local First page. Show managed anchors, local/paid review queues, current head, repair history, budgets and active/uncontained workers. Provide scoped pause, stop, reconcile and resume plus configurable planning/implementation/local-review/paid-review profiles. Show findings and preserved work paths. Do not accept arbitrary DB/executable/repository paths from browser input; bootstrap trusted roots locally. Disable mutation controls for stale observations and refresh after every result; backend rechecks regardless of UI state.

## 7. Delivery milestones (RED → GREEN → review)

### M0 — Compatibility and isolated harness

Inspect target host help, implement fixture adapters, characterize lane/claim/hold semantics and hook coverage. Tests must reproduce completion-before-hook, archive releasing dependencies, wrong-run refusal, review-source running, and creation races. Verify real host operations against a disposable board with stubbed/no model spawning. If isolation cannot be proven, stop; do not test against the default board. Deliver a capability report and concrete fallback for unsupported operations.

### M1 — Pure contracts and safe primitives

Extract ticket/plan/review contracts, hardened Git and deterministic validation. Remove legacy imports from extracted modules. Add tests for duplicate plan IDs, cycles, criteria coverage, malformed review, missing blockers, stale heads, allowed commands, timeout cleanup and preserved dirty work. No automatic board writes yet.

### M2 — Adapter, evidence and operator recovery first

Build supported board adapter, operation records, singleton lock and durable intents. Implement status/pause/stop/reconcile/resume on fixture work, including dispatcher races and incomplete stop. Prove restart and human edits are handled without resetting budgets. This is the foundation: do not ship automatic decomposition before this milestone passes.

### M3 — One piece with local review

Native implementer run → request review → new local reviewer → correction → repeat → accepted candidate. Also prove accidental done is recovered by a separate review that is released by a locked, evidence-checked poll; a negative separate verdict creates bounded parentless correction work charged once to the root review-correction budget while its immutable operation retains the complete structured finding set, then requires fresh implementation and local review. Add and fixture-test the role pre-tool guidance helper in M3 without relying on it for acceptance; runtime plugin registration is deferred to M6 and must not be activated by M3 source work. Verify profile/session/candidate attribution.

For a running implementation, the coordinator may only register and later reconcile a handoff intent. The implementation worker must call `kanban_request_review` on its own run; reconciliation reads the public `show --json`/`runs --json` implementation-run metadata (`local_first_review`, `worker_session_id`) and the bound `review_requested` event. Do not substitute a coordinator CLI call or task-level reviewer/session fields. A reviewer must be a distinct profile and a fresh run/session claimed from `review`; acceptance remains blocked without trusted Git/candidate evidence or while paused/cancelled.

For reviewer-owned `kanban_request_changes`, Hermes v0.21.5 does not retain `worker_session_id` in the terminal run metadata. While that review run is active, require the tool-owned `HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, and `HERMES_SESSION_ID` to match the already-recorded review provenance, then durably receipt that session in the correction operation before the worker calls native request-changes. Reconciliation verifies the immutable run ID/profile, `changes_requested` outcome/event, and original implementer restoration without fabricating missing native metadata. Same-user process access is not independent authority: never accept a caller-supplied session value.

### M4 — Tranche planning, Git integration and paid review

Add paid structured planning, finite generated work, serial integration, combined tests, paid review, correction generation and repeated re-review. Verify future tranches do not exist until release. Prove a review round ending in changes does not release acceptance.

### M5 — Automatic recovery and restart matrix

Exercise every canonical acceptance scenario with failure injection before/after each native effect and evidence acknowledgement. Concurrent notifications create one correction generation. Missing hooks are recovered by polling. Native done/archive cannot fabricate evidence. Distinguish auto-hold clearing from operator pause. Exhausted recovery gives one clear user decision.

### M6 — UI, CLI, packaging and legacy removal

Wire dashboard/tool/CLI to the same coordinator. Replace operator configuration and remove unused crypto dependency after import verification. Delete legacy files only after behavior has been ported and no surviving imports/tests reference them. Regenerate command/config docs from implemented behavior; no stale examples. Run full replacement suite, packaging and plugin discovery checks in isolation. Existing runtime data remains untouched.

**Current M6 slice (isolated only):** `local_first_orchestrator.cli` is the sole new CLI destination and packaging entry point. It has explicit `initialize-store`, real root enrollment through durable native hold/readback, scoped operator controls, and a persistent `run` loop with an explicit `--once` smoke mode. Runtime composition opens existing evidence only; it never creates or migrates a missing store. The five proposal tools use scoped lazy composition and close it after each invocation; local review/correction calls pass configured roles to the coordinator, which verifies native run/session provenance. Native CLI registration uses Hermes' current `register_cli_command(name, help, setup_fn, handler_fn, description)` contract.

**Implemented/exercised in the current M6 isolated slice:** the dashboard/API uses the same scoped composition boundary and stale-observation guard; production Git/check/planning observers are configured only from trusted configuration; registration provides five lazy tools, the native CLI command, and the advisory pre-tool hook; the replacement command/config/operator documentation and explicit legacy-retirement inventory are present. The retained public-core M4/M5 fixture suite and a package-wide import/discovery regression exercise these replacement paths from a built artifact, not from the checkout.

**Still pending (do not call M6 complete):** independent review renewal; browser fixture verification beyond the recorded bounded dashboard evidence; and M7 release/cutover authorization. The isolated discovery fixture uses a temporary Hermes home, temporary artifact environment, explicit enabled-plugin configuration, and no provider/board/profile activation. It is not a live plugin installation, enrollment, dispatch, or service activation.

### M7 — Release readiness, not implicit cutover

Independent code/spec review, full fixture acceptance, compatibility report, rollback package and explicit operator cutover checklist. No live implementation calls, plugin reload, service replacement, ledger migration, or real board enrollment unless separately authorized. If a later cutover is authorized, enroll a disposable/sample tranche first, verify native runtime and both review roles, then expand scope deliberately.

Every milestone report names actual commands/results, changed files, remaining gaps and whether only fixtures ran. Completing an implementation checklist is not evidence of live deployment.

## 8. Verification strategy

Use standard project test execution (`python -m pytest -q -o addopts=`), `git diff --check`, Python compilation and `node --check dashboard/dist/index.js`. These are test commands, not deployment commands. Add a fixture suite that uses target Hermes with isolated home/board/worktree roots and no real provider endpoints; mocks alone cannot validate native transition constraints. Do not import/configure another real profile to make tests pass.

Create a coverage matrix mapping every canonical acceptance scenario to a test name. Include package import checks proving removed legacy modules, crypto signer logic and private host DB imports are absent from the replacement dependency graph. Verify install metadata and both command entry points without starting a coordinator. Browser-check the dashboard against a fixture backend and exercise partial pause/stop and stale data cases.

Safety assertions: no native SQL writes, host tree unchanged, no production board/config/ledger modifications, no external provider requests, no recursive worktree deletion, no user pause auto-cleared, no acceptance from lane status alone, no unbounded replacement-card generation.

## 9. File action inventory conventions

`MODIFY` means retain a path but rewrite/extract its responsibility. `CREATE` means new file. `DELETE` means remove from the replacement after the stated destination tests pass, not delete user data or source history. All runtime module paths below are under `local_first_orchestrator/`. Listed functions are proposed responsibilities/signatures, not a claim they already exist. Preserve existing small helpers only after checking their input contracts; avoid copying old authority behavior by accident.

The inventory follows in the next sections.

## 10. Runtime file-by-file destination

### MODIFY `local_first_orchestrator/__init__.py`

- **Goal:** Package identity only; no activation on import.
- **Functions/types:** Package version/export constants; no runtime side effects.
- **Interaction:** Imported by packaging and entry points.

### DELETE `local_first_orchestrator/adapters.py` (M6, after extraction)

- **Goal:** Old generic set_state and fake board abstraction; replace with typed native operations in hermes_board.py and the new fixture.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/admission.py` (M6, after extraction)

- **Goal:** Legacy feature admission/envelope authority; use native anchor enrollment and TicketContract.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/architecture.py` (M6, after extraction)

- **Goal:** Legacy imported architecture activation; move required criterion/non-goal validation into decomposition.py only.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/cli.py`

- **Goal:** Single operator command parser and renderer; remove legacy --database/signer/activation commands.
- **Functions/types:** register_cli(parser); run_command(args); main(); render_result(result). Parse explicit board/scope, call coordinator, return truthful exit category.
- **Interaction:** Composition root builds dependencies; dashboard uses same coordinator, not CLI parsing.

### MODIFY `local_first_orchestrator/comment_delivery.py`

- **Goal:** Marker-based native comment effect reconciliation, with minimal operation-store dependency.
- **Functions/types:** MarkerLookup; deliver_comment_once(intent,board,store); reconcile_comment(intent,snapshot). Found => verify/ack; unknown => hold/retry inspection; no blind duplicate post.
- **Interaction:** Evidence store owns intent; board adapter posts; port crash tests without Ledger.

### MODIFY `local_first_orchestrator/config.py`

- **Goal:** Validated immutable plugin policy and role settings, with versioned user-owned JSON persistence. Absorb safe config IO from operator_config.py without old ledger/signers.
- **Functions/types:** PluginConfig; RoleProfiles; BudgetPolicy; load_config(path); validate_config(config); save_config_atomic(config); resolve_state_root(). Reject invalid limits/roots; config changes affect future unclaimed work unless explicit recovery.
- **Interaction:** Consumed by composition, coordinator, profiles, budget checks and UI; inject paths in tests.

### MODIFY `local_first_orchestrator/context_packet.py`

- **Goal:** Compact revision-pinned context for local implementation and both review roles.
- **Functions/types:** ContextPacketBuilder.build(contract,candidate,budget); build_from_repository(snapshot,selection); write_artifacts(packet,root). Include full diff reference and explicit truncation; reject missing required content.
- **Interaction:** Uses ticket, symbols, repository snapshot; native card/attachments carry packet pointers.

### DELETE `local_first_orchestrator/controller.py` (M6, after extraction)

- **Goal:** Monolithic duplicate lifecycle owner; replaced by coordinator.py and small domain helpers after selected Git/evidence functions are ported.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/corrections.py`

- **Goal:** Pure finding-to-correction contracts/validation; replace Ledger-backed CorrectionService.
- **Functions/types:** CorrectionSpec; CorrectionPlan; build_correction_plan(findings,contract,candidate); validate_correction_graph(plan); correction_operation_key(scope,finding,generation). Preserve red evidence/non-goals and root lineage.
- **Interaction:** Recovery/review routing proposes; coordinator serially creates held cards and verifies before release.

### MODIFY `local_first_orchestrator/daemon.py`

- **Goal:** One bounded coordinator loop, not a scheduler duplicating Hermes.
- **Functions/types:** CoordinatorLoop.run/tick/request_stop/health; instance_lock(); bounded_backoff(). On wake or interval call coordinator.tick; release lock on exit; persist pause elsewhere.
- **Interaction:** Hooks wake loop; CLI run starts loop explicitly. No agent spawn or provider invocation.

### MODIFY `local_first_orchestrator/daemon_text.py`

- **Goal:** Small bounded diagnostic text helpers.
- **Functions/types:** bounded_daemon_text(); validate_daemon_worker_id(); validate_daemon_status_label(). Remove obsolete stage vocabulary.
- **Interaction:** Used by daemon/status/error reporting only; no business state.

### MODIFY `local_first_orchestrator/decomposition.py`

- **Goal:** Pure tranche/plan contracts and deterministic structural validation; remove ledger activation/projection functions.
- **Functions/types:** Criterion; TranchePlan; DecompositionPlan; PlanValidator.validate/validate_tranche. Check duplicate IDs, full coverage, references, acyclicity, finite limits and declared scope.
- **Interaction:** Planner parser creates these records; planning coordinator requests native creation through main coordinator only.

### MODIFY `local_first_orchestrator/decomposition_planner.py`

- **Goal:** Planner schema, packet construction and strict response parsing, not model execution.
- **Functions/types:** planner_schema(); planner_contract(); planner_contract_hash(); packet(contract,snapshot,capacity); parse(payload); validate_proposal_identity(plan,request). Remove LocalDecompositionPlanner subprocess launcher and regex config resolver.
- **Interaction:** Paid native planning ticket submits payload via plugin tool; parsed result goes to plan validation.

### MODIFY `local_first_orchestrator/evidence_hash.py`

- **Goal:** Canonical identity of contracts, proposals, artifacts and operations.
- **Functions/types:** canonical_sha256(value); domain_hash(kind,version,value). Domain/version separate unlike-shaped records; no signatures.
- **Interaction:** Used by contracts/evidence/recovery to deduplicate, not as security isolation.

### DELETE `local_first_orchestrator/execution_handoff.py` (M6, after extraction)

- **Goal:** Legacy marker protocol binding plugin ticket states; replace versioned native contract/review proposal evidence.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/gateway_notification.py`

- **Goal:** Best-effort, bounded, deduplicated recovery alerts through configured supported Hermes messaging path.
- **Functions/types:** format_recovery_notice(report); notify_once(event_key,target,transport,store); reconcile_notification(intent). Unknown delivery is explicit; do not block code acceptance on a cosmetic alert failure.
- **Interaction:** Coordinator emits meaningful transitions; transport injectable, no credentials in artifacts.

### DELETE `local_first_orchestrator/generated_activation.py` (M6, after extraction)

- **Goal:** Legacy projected-card activation authority; native held-create/verify/release replaces it.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/generated_projection.py` (M6, after extraction)

- **Goal:** Outbox projecting shadow microtickets to board; keep verification/idempotency lessons in board/effect tests, not shadow projection.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/git_adapter.py`

- **Goal:** Persistent candidate worktrees and serial tranche integration, never useful-work deletion.
- **Functions/types:** Candidate; GitWorktreeAdapter.inspect_candidate/prepare_workspace/resolve_execution_base/freeze_candidate/advance_integration_head/integrate_piece/verify_integrated_head. Freeze rejects unexplained dirty content; CAS ref update; no automatic default-branch merge.
- **Interaction:** Validation checks content; evidence records base/head; coordinator plans conflict corrections. Remove forced teardown.

### MODIFY `local_first_orchestrator/git_security.py`

- **Goal:** Keep small tested noninteractive Git hardening.
- **Functions/types:** safe_git_env(); safe_git_argv(). Preserve hooks/pager/external diff/textconv protections; deliberate compatible configuration only.
- **Interaction:** All Git callers use this boundary; port tests unchanged where applicable.

### MODIFY `local_first_orchestrator/hermes_board.py`

- **Goal:** Only supported native-board operation boundary; replace old direct DB/authority and dispatch-preview code.
- **Functions/types:** BoardCapabilities; BoardSnapshot; HermesBoardAdapter.list_tasks/read_task/read_run/create_held/comment/request_review/request_changes/return_waiting_review/hold/release/stop_run/link/complete_anchor/verify_effect. Each mutation returns typed outcome and readback; unsupported is explicit.
- **Interaction:** Coordinator supplies board/scope/run and operation IDs; operations cannot infer authority from card prose. No broad dispatch helper.

### MODIFY `local_first_orchestrator/hermes_profiles.py`

- **Goal:** Discover and resolve existing profiles without creating or rewriting them.
- **Functions/types:** HermesProfile; list_profile_names(); show_profile(name); resolve_roles(config); verify_run_profile(expected,run). Repair text parsing/root assumptions against host compatibility tests.
- **Interaction:** Uses bounded CLI transport; coordinator pins planning/local implementation/local review/paid review roles.

### DELETE `local_first_orchestrator/historical_revalidation.py` (M6, after extraction)

- **Goal:** Legacy validation authorization/attestation recovery; new repair reconstructs ordinary candidate/check evidence.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/keyless_human_recovery.py` (M6, after extraction)

- **Goal:** Root-owned recovery launcher/payload not part of normal workflow.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/ledger.py` (M6, after extraction)

- **Goal:** Separate feature/ticket/stage/acceptance/projection lifecycle; replacement evidence_store.py must not reproduce it.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/local_qwen.py` (M6, after extraction)

- **Goal:** Direct legacy model invocation; native profile tasks own inference. Extract schemas if needed.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/metrics.py` (M6, after extraction)

- **Goal:** Adaptive sizing/outcome policy deferred; no corresponding V1 runtime.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/native_release_approval.py` (M6, after extraction)

- **Goal:** Detached signatures and protected release authority; retain only needed process-identity semantics in supported stop verification tests.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/native_workspace.py`

- **Goal:** Small path/identity safety helpers only; remove protected pinned-FD authority engine.
- **Functions/types:** canonical_native_workspace_path(); validate_native_workspace_path(); require_native_path_identity(). Derive supported host workspace from actual card; do not hard-code a layout contradicted by Hermes.
- **Interaction:** Git/adapter verify intended repo/workspace and symlinks; no .git administrative surgery.

### DELETE `local_first_orchestrator/operator_config.py` (M6, after extraction)

- **Goal:** Ledger/signer-heavy registration; safe atomic IO and role records move to config.py/hermes_profiles.py.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/paid_model.py` (M6, after extraction)

- **Goal:** Separate paid invocation transport; paid native profile tasks replace it.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/planning_coordinator.py`

- **Goal:** Thin domain service for validated plan evidence and active-tranche materialization proposals.
- **Functions/types:** prepare_planning_request(anchor,snapshot); inspect_plan(result); next_materialization(plan,evidence); verify_materialized_set(plan,cards). Return typed actions; do not run a second scheduler.
- **Interaction:** Main coordinator executes actions through effect store/board adapter; future tranches remain unmaterialized.

### MODIFY `local_first_orchestrator/readiness.py`

- **Goal:** Pure contract validity, not duplicate native readiness scheduling.
- **Functions/types:** ReadinessError; validate_ticket(contract). Validate paths, criteria, allowed commands, budgets and dependencies supplied by validated plan.
- **Interaction:** Used by planner/correction inputs; native Hermes still decides dispatch readiness.

### DELETE `local_first_orchestrator/reconciliation.py` (M6, after extraction)

- **Goal:** Legacy scheduler crash-state model; port failure scenarios into recovery.py/effect tests without those states.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/repository_snapshot.py`

- **Goal:** Immutable planning/candidate repository evidence using hardened Git reads.
- **Functions/types:** RepositorySnapshot; canonical_json(); snapshot(repo,base,allowed_paths); RepositoryPlanValidator.validate(plan,snapshot). Hash actual files/symbols; no raw unhardened Git or moving-HEAD assumption.
- **Interaction:** Feeds planning/context/validation; avoid huge manifests when only bounded paths are needed.

### DELETE `local_first_orchestrator/revalidation_boundary.py` (M6, after extraction)

- **Goal:** Cross-store private capability and native SQLite lock boundary; no direct DB coupling in replacement.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/review.py`

- **Goal:** Pure strict review schema, packet and verdict normalization; remove LocalReviewAdapter/SameTicketRepairCoordinator.
- **Functions/types:** ReviewFinding; ReviewResult; failure_fingerprint(); normalize_review(payload,contract); ReviewPacketBuilder.build(candidate,checks,contract); validate_review_identity(result,expected). Pass requires all criteria and exact evidence; repair with missing valid blockers is not implicit approval.
- **Interaction:** Review routing records native session/run; coordinator controls acceptance and repair.

### DELETE `local_first_orchestrator/review_worker.py` (M6, after extraction)

- **Goal:** Direct PluginLlm invocation bypasses desired native review run; structured parsing goes to review.py.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/runtime_metrics.py` (M6, after extraction)

- **Goal:** Ledger metrics backfill/recommendations deferred; simple derived status comes from coordinator.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/scheduler.py` (M6, after extraction)

- **Goal:** Legacy stage claims/ordering over plugin ledger; Hermes dispatch plus coordinator tick replaces it.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/signer_enrollment.py` (M6, after extraction)

- **Goal:** Signer configuration/enrollment ceremony no longer required.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/source_languages.py`

- **Goal:** Keep multi-language path/test classification; avoid unsupported semantic certainty.
- **Functions/types:** SourceLanguage; language_for(); normalized_repository_path(); is_supported_source/is_supported_repository_file/is_test_path/supported_languages.
- **Interaction:** Repository/context/symbol validation; unknown languages require explicit conservative handling.

### DELETE `local_first_orchestrator/state_projection.py` (M6, after extraction)

- **Goal:** Second-state-to-board status projection; removed entirely.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/states.py` (M6, after extraction)

- **Goal:** CanonicalState mirrors task lifecycle; native status remains in observations, not plugin transitions.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/symbols.py`

- **Goal:** Keep bounded source symbol discovery and scope checks.
- **Functions/types:** Symbol; SymbolSelection; SymbolIndex.select_for_ticket(); symbols_for(); changed_symbols(); contract_target_scope(); enforce_symbol_scope(). Adapt TicketContract input.
- **Interaction:** Context and validation use helpers; parser uncertainty is exposed in evidence, not silently green.

### MODIFY `local_first_orchestrator/ticket.py`

- **Goal:** Immutable implementation contract types; no workflow lane or ownership state.
- **Functions/types:** TicketContract (adapt MicroTicket); PatchBudget; VerificationProfile; declared_ticket_paths(); parse_contract(); contract_payload(). Use schema version and canonical hash.
- **Interaction:** Shared input for planning, context, validation and review; native IDs are associations not shadow rows.

### DELETE `local_first_orchestrator/tranche_completion.py` (M6, after extraction)

- **Goal:** Ledger-specific finality evidence; revision-bound piece/tranche acceptance moves to review_routing.py and coordinator.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/triage.py` (M6, after extraction)

- **Goal:** Legacy model triage/child creation/state transitions; deterministic recovery.py handles known faults; exhausted/ambiguous work goes to human or explicitly configured escalation.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### DELETE `local_first_orchestrator/usage_governor.py` (M6, after extraction)

- **Goal:** Legacy reservation/approval machinery removed; finite native-run paid/retry accounting lives in budgets.py.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/validation.py`

- **Goal:** Keep deterministic checks independent of acceptance; adapt native contracts/candidates.
- **Functions/types:** CommandEvidence; ValidationResult; DeterministicValidator.run_verification_command/validate. Verify before/after candidate identity; constrain argv/cwd/env/time/output; terminate process group on timeout; scope/secret/budget checks.
- **Interaction:** Coordinator calls checks before review/integration; artifacts attach to native tasks and evidence store.

### DELETE `local_first_orchestrator/validation_recovery_boundary.py` (M6, after extraction)

- **Goal:** Signed/pinned protected recovery capabilities; out of scope for ordinary-user repair.
- **Functions retained here:** none. Do not leave a compatibility shim that reopens the legacy ledger.
- **Interaction:** remove all imports/CLI routes/tests that require this runtime after replacement acceptance; preserve external runtime data.

### MODIFY `local_first_orchestrator/worktree_lifecycle.py`

- **Goal:** Non-destructive worktree inspection and retention decisions.
- **Functions/types:** inspect_workspace(workspace); classify_preservation(candidate,evidence); list_retained_work(scope). V1 has no automatic force removal; output optional operator cleanup advice.
- **Interaction:** Pause/cancel/status preserve and expose work; cleanup is separate authorization.


## 11. New runtime files

### CREATE `local_first_orchestrator/composition.py`

- **Goal:** Explicit dependency wiring and isolated fixture injection.
- **Functions/types:** build_runtime(config,adapters=None); close_runtime(runtime). No reads/writes on module import; never opens legacy ledger.
- **Interaction:** CLI/dashboard/tools obtain the same Coordinator; plugin registration stays lightweight.

### CREATE `local_first_orchestrator/coordinator.py`

- **Goal:** Single managed-work decision/application entry point; small methods delegate domain logic.
- **Functions/types:** Coordinator.enroll/status/tick/report_issue/submit_plan/submit_review/request_corrections/pause/reconcile/resume/cancel/recover. observe -> policy -> action -> verified effect; one bounded action per tick.
- **Interaction:** Only module coordinating board, store, review/recovery/plan/Git. No arbitrary SQL or model execution.

### CREATE `local_first_orchestrator/contracts.py`

- **Goal:** Transport-independent observations/actions/results shared at boundaries.
- **Functions/types:** BoardSnapshot; ManagedMember; CandidateIdentity; OperationIntent; PauseIntent; RecoveryReport; Action; ActionResult; validate_scope(). Typed errors: conflict/unknown/unsupported/partial.
- **Interaction:** Adapters serialize; domain functions consume; store persists only permitted evidence fields.

### CREATE `local_first_orchestrator/evidence_store.py`

- **Goal:** Small evidence/operation store, not task lifecycle ledger.
- **Functions/types:** EvidenceStore.open/migrate/register_member/record_candidate/record_review/reserve_operation/observe_effect/ack_effect/pending_operations/record_budget_event/set_operator_intent/read_scope. Unique keys and transactions for own state only.
- **Interaction:** Coordinator holds singleton; native state always read from adapter; schema tests forbid shadow task/status projections.

### CREATE `local_first_orchestrator/budgets.py`

- **Goal:** Finite shared-root attempt/correction/recovery accounting.
- **Functions/types:** BudgetPolicy; classify_run_start(); record_once(); remaining(); permit_action(); explain_exhaustion(). Separate actual native started runs from pre-start failures; persist no-reset lineage.
- **Interaction:** Evidence store records events; coordinator checks before release/retry, including replacement cards.

### CREATE `local_first_orchestrator/review_routing.py`

- **Goal:** Native local/paid review handoffs and evidence acceptance checks.
- **Functions/types:** prepare_review_request(); choose_review_action(snapshot,candidate); collect_review_evidence(); verify_fresh_session(); eligible_piece(); eligible_tranche(). Wrong/missing verdict proposes recovery, not completion.
- **Interaction:** Uses pure review.py + board observations; coordinator applies actions. No direct inference.

### CREATE `local_first_orchestrator/recovery.py`

- **Goal:** Deterministic mismatch classifiers and bounded repair proposals.
- **Functions/types:** detect_issues(snapshot,evidence,git); classify_issue(); deduplicate_issue(); propose_repair(issue,budget); verify_repair(result); summarize_escalation(). Containment precedes corrections.
- **Interaction:** Returns actions to coordinator; no independent loop or competing creator.

### CREATE `local_first_orchestrator/operator_controls.py`

- **Goal:** Durable scoped pause/cancel/resume behavior against independent dispatch.
- **Functions/types:** plan_pause(scope,stop); plan_stop(run); verify_containment(scope); reconcile_operator_edits(); can_resume(); reject_late_result(). Preserve useful work and incomplete-stop reports.
- **Interaction:** Coordinator persists intent first; adapter performs supported native effects; dashboard/CLI share results.

### CREATE `local_first_orchestrator/plugin_hooks.py`

- **Goal:** Fast advisory event handlers and model-tool guardrails.
- **Functions/types:** register_hooks(ctx); on_board_event(); on_dispatch_tick(); pre_tool_call(event); worker_context(scope). Deduplicate wake hints; reject normal unsupported completion with a clear handoff instruction.
- **Interaction:** No blocking Git/model/long recovery in callbacks; daemon polling covers missing hooks.

### CREATE `local_first_orchestrator/plugin_tools.py`

- **Goal:** Reviewer/planner structured proposal tools and status.
- **Functions/types:** register_tools(ctx); submit_plan(args,run); submit_review(args,run); request_corrections(args,run); report_issue(args,run); status(args). Validate provenance and return durable operation reference.
- **Interaction:** Composition/coordinator handle scope/effects; tools cannot authorize arbitrary profiles/paths or override pause.

## 12. Entry points, dashboard, packaging, scripts and documentation

| File | Action | Goal and functions/contents | Interactions and verification |
| --- | --- | --- | --- |
| `__init__.py` | MODIFY | `register(ctx)` registers CLI, proposal tools, fast hooks and bounded worker guidance. No coordinator starts on import/registration. | Call plugin_tools/plugin_hooks registration; lazily compose runtime on explicit commands. Test registration in each intended profile/process. |
| `plugin.yaml` | MODIFY | Describe recoverable board-native behavior and actual supported plugin API version; preserve plugin identity. No functions. | Plugin doctor/discovery fixture proves required capabilities; no permission to patch host. |
| `pyproject.toml` | MODIFY | Preserve entry point/package discovery; remove cryptography only after signer imports are gone; declare real test/runtime dependencies. No functions. | Build/install in isolated environment; standalone and Hermes CLI use same parser. |
| `.gitignore` | MODIFY | Exclude generated local state, artifacts and build caches deliberately; retain tests and docs. No functions. | Do not hide source changes or delete currently untracked user directories. |
| `dashboard/manifest.json` | MODIFY | Replace separate-ledger description, retain route/plugin identity and actual entry/API paths. No functions. | Dashboard discovery smoke test. |
| `dashboard/plugin_api.py` | MODIFY | Replace Ledger/controller routes with `status`, `profiles`, `update_configuration`, `enroll`, `pause`, `reconcile`, `resume`, `cancel`, `recover` handlers and bounded request schemas. | All call coordinator; validate scope/auth using host dashboard mechanisms; expose partial outcomes, never accept arbitrary trusted-root overrides. |
| `dashboard/dist/index.js` | MODIFY | Retain plain JS host component entry; adapt `configDraft`, `ProfileSelect`, `LocalFirstPage`, `refresh`, `act`, `saveConfiguration`. Add scope selector, issue/worker/review lists and partial-stop rendering; remove adaptive metrics and signer/revalidation UX. | Talks only plugin API, refreshes after mutations; node syntax + fixture browser tests. No new frontend toolchain required. |
| `scripts/c12r1-tk-3-bootstrap.py` | DELETE in M6 | Protected installation helper is outside new workflow; no surviving functions. | Preserve historical source via Git/backup; do not execute/uninstall privileged live components. |
| `scripts/c12r1-tk-3-root-launcher.sh.in` | DELETE in M6 | Root launcher template is no longer a product requirement; no surviving entry point. | Deleting repo template does not authorize modifying `/usr/local` or `/etc`. |
| `README.md` | MODIFY now / refresh M6 | Current direction and honest implementation status; links to behavior and this plan. Later add only tested install/run examples. No functions. | Do not present new proposed commands as available before implementation. |
| `docs/plugin-only-recoverable-workflow.md` | MODIFY narrowly now, preserve | Canonical product behavior, recovery scope and acceptance scenarios; remove references to deleted historical docs. | Governs this plan and eventual acceptance tests. |
| `docs/plugin-only-implementation-plan.md` | CREATE now, maintain | This file: target inventory, responsibilities, milestones and verification. | Sol updates proven capability decisions here before relying on changed interfaces. |
| `docs/operator-guide.md` | CREATE in M6, not a placeholder now | Actual tested command syntax, scoped pause/stop outcomes, native lane limitations, user repair walkthrough, diagnostics and no-sudo recovery. No functions. | Derived from CLI/API tests; no legacy database/signer command examples. |
| `docs/compatibility-and-release.md` | CREATE in M0, finish M7 | Supported host fingerprint/capabilities, unavailable primitives and fallbacks, fixture evidence, isolated test commands, cutover/rollback checklist. No functions. | Separates source readiness from live activation; preserve exact test evidence. |

### Obsolete docs removed in this documentation change

Each file below is DELETE now, after an external recovery backup. They document the retired implementation or conflicting proposal. No runtime code/data is deleted by this documentation cleanup. Tracked versions remain in Git history; untracked proposals must also be backed up.

| File | Why removed / new destination | Functions |
| --- | --- | --- |
| `docs/board-first-simplified-design.md` | Superseded proposal with ledger-era authority and policy assumptions; canonical behavior and this plan replace it. | None; documentation. |
| `docs/board-first-wayfinder.md` | Superseded decision map; referenced live board cards are not modified by deleting this file. | None; documentation. |
| `docs/command-reference.md` | Commands require legacy ledger and recovery stages; future operator-guide documents new tested CLI. | None; documentation. |
| `docs/configuration.md` | Legacy registration, signer/ledger authority and old roles; future operator-guide describes new config. | None; documentation. |
| `docs/native-hermes-integration.md` | Describes old projection/handoff/controller ownership; this plan and compatibility report replace it. | None; documentation. |
| `docs/operator-lifecycle-recovery.md` | Ledger-centric recovery procedures conflict with ordinary automatic recovery. | None; documentation. |
| `docs/native-release-detached-approval.md` | Signed legacy recovery is excluded from new normal workflow. | None; documentation. |
| `docs/c12r1-tk-3-keyless-human-recovery.md` | Root-owned helper documentation is no longer relevant to replacement. | None; documentation. |
| `docs/c12r1-tk-3-protected-launcher-install.md` | Privileged installer documentation is outside replacement scope. | None; documentation. |

## 13. Test file inventory and migration

Existing tests are not blindly preserved or deleted to obtain a green suite. MODIFY means rewrite fixtures/assertions against the new public behavior while preserving useful cases. DELETE means retire assertions about removed architecture after transferring any relevant fault scenario to the named replacement suite. Function names shown are representative target cases; add edge cases required by the canonical acceptance scenarios. No test may connect to real provider endpoints or live board homes.

| Existing file | Action | Goal / replacement | Target functions |
| --- | --- | --- | --- |
| `tests/__init__.py` | MODIFY | Test package marker. Test discovery. | `No functions or import-time initialization.` |
| `tests/hermes_board_fixture.py` | MODIFY | Fixture builders for native-shaped task/run/event snapshots and scripted effect failures. All unit suites; do not model unsupported native transitions as legal. | `make_board(); make_run(); inject_effect_failure(); assert_no_live_effects().` |
| `tests/test_acceptance_only.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_tranche_workflow.py and test_review_contract.py for revision-bound acceptance. | `None in retired file` |
| `tests/test_atomic_bundle.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_effects.py and test_crash_replay.py; drop shadow-row/projection-transaction assertions. | `None in retired file` |
| `tests/test_board_commit_boundary_red.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_effects.py and test_crash_replay.py; drop shadow-row/projection-transaction assertions. | `None in retired file` |
| `tests/test_c12_pinned_bootstrap.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_c12_protected_launcher.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_cli_generated_projection.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_effects.py and test_crash_replay.py; drop shadow-row/projection-transaction assertions. | `None in retired file` |
| `tests/test_comment_crash_recovery.py` | MODIFY | Test `comment_delivery.py`: marker recovery with no duplicate post. | `test_restart_after_comment_before_ack()` |
| `tests/test_comment_delivery.py` | MODIFY | Test `comment_delivery.py`: post then verify before acknowledging. | `test_comment_effect_readback()` |
| `tests/test_comment_outbox.py` | MODIFY | Test `evidence_store.py`: only effect intent, no plugin ticket state or projection. | `test_minimal_comment_intent_lifecycle()` |
| `tests/test_comment_reconciliation.py` | MODIFY | Test `comment_delivery.py`: uncertain native effects do not silently succeed. | `test_conflicting_comment_effect_holds()` |
| `tests/test_completion_hash_integrity.py` | MODIFY | Test `review_routing.py`: approval tied to exact contract/check/candidate identity. | `test_changed_head_invalidates_approval()` |
| `tests/test_controller_canonical_isolation.py` | MODIFY | Test `git_adapter.py`: serial dedicated integration and preserved user checkout. | `test_integration_never_mutates_canonical_branch()` |
| `tests/test_correction_plan_parser.py` | MODIFY | Test `corrections.py`: bounded correction proposals and declared finding lineage. | `test_reject_invalid_correction_graph()` |
| `tests/test_create_files.py` | MODIFY | Test `validation.py`: declared creation paths and safe candidate contents. | `test_new_file_scope_and_symlink_rejection()` |
| `tests/test_criterion_coverage.py` | MODIFY | Test `decomposition.py`: complete criterion coverage without silent expansion. | `test_plan_covers_each_criterion()` |
| `tests/test_decomposition.py` | MODIFY | Test `decomposition.py`: pure structural validation; drop ledger activation assertions. | `test_plan_ids_dependencies_and_cycles()` |
| `tests/test_decomposition_planner.py` | MODIFY | Test `decomposition_planner.py`: schema rejection, repository/contract identity and bounded proposal. | `test_strict_paid_plan_response()` |
| `tests/test_execution_safety.py` | MODIFY | Test `validation.py / coordinator.py`: safe command execution and explicit managed scope. | `test_no_live_effects_without_scope()` |
| `tests/test_external_boundary_crash_hardening.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_crash_replay.py for intent/effect/readback crash scenarios. | `None in retired file` |
| `tests/test_failed_attempt_reconciliation.py` | MODIFY | Test `recovery.py`: reconcile exact native attempt before retrying. | `test_failed_run_preserves_candidate()` |
| `tests/test_feature_admission.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_enrollment.py and test_evidence_store.py; drop duplicate feature/ticket-lifecycle assertions. | `None in retired file` |
| `tests/test_generated_activation.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_effects.py and test_crash_replay.py; drop shadow-row/projection-transaction assertions. | `None in retired file` |
| `tests/test_generated_activation_context.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_effects.py and test_crash_replay.py; drop shadow-row/projection-transaction assertions. | `None in retired file` |
| `tests/test_generated_microticket_e2e.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_effects.py and test_crash_replay.py; drop shadow-row/projection-transaction assertions. | `None in retired file` |
| `tests/test_generated_projection_delivery.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_effects.py and test_crash_replay.py; drop shadow-row/projection-transaction assertions. | `None in retired file` |
| `tests/test_hermes_execution_reconciliation.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_compatibility.py and test_coordinator_recovery.py for native run/snapshot reconciliation. | `None in retired file` |
| `tests/test_hermes_process_read.py` | MODIFY | Test `hermes_board.py`: native CLI task/run/comment/event parsing and bounded failure handling. | `test_lossless_native_snapshot()` |
| `tests/test_hermes_profiles.py` | MODIFY | Test `hermes_profiles.py`: target CLI parsing, roles and provenance mismatch. | `test_profile_resolution_and_run_match()` |
| `tests/test_historical_revalidation.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_historical_revalidation_authorization.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_invocation_lifecycle.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_budget_accounting.py and test_local_review_loop.py for native-run identity and limits. | `None in retired file` |
| `tests/test_keyless_human_recovery.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_ledger.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_enrollment.py and test_evidence_store.py; drop duplicate feature/ticket-lifecycle assertions. | `None in retired file` |
| `tests/test_manual_adoption.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_enrollment.py and test_evidence_store.py; drop duplicate feature/ticket-lifecycle assertions. | `None in retired file` |
| `tests/test_marker_lookup.py` | MODIFY | Test `comment_delivery.py`: found/absent/unavailable marker outcomes. | `test_unknown_marker_does_not_repost()` |
| `tests/test_modern_signer_enrollment.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_multilanguage_repository_evidence.py` | MODIFY | Test `repository_snapshot.py`: supported source evidence and conservative unknown handling. | `test_multilanguage_evidence()` |
| `tests/test_native_release_approval_red.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_native_release_authority_step1.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_native_release_revalidation.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_native_workspace_security.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_git_integration.py for ordinary path/identity/preservation cases, not protected-FD authority. | `None in retired file` |
| `tests/test_new_test_files.py` | MODIFY | Test `validation.py`: scope and evidence for newly created tests. | `test_declared_test_files_are_checked()` |
| `tests/test_operator_api.py` | MODIFY | Test `dashboard/plugin_api.py`: shared coordinator behavior, safe paths and actionable API errors. | `test_scoped_pause_partial_outcome()` |
| `tests/test_operator_config.py` | MODIFY | Test `config.py`: new schema, role/limit validation, atomic writes and old-config rejection. | `test_config_roundtrip_and_invalid_policy()` |
| `tests/test_operator_signer_enrollment_red.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_patch_budget_policy.py` | MODIFY | Test `ticket.py`: explicit finite configured patch limits. | `test_candidate_respects_patch_budget()` |
| `tests/test_persisted_review_apply.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_tranche_workflow.py and test_review_contract.py for revision-bound acceptance. | `None in retired file` |
| `tests/test_phase2.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_validation_git_safety.py and test_structured_verification.py for checks; test_git_integration.py for Git/context cases. | `None in retired file` |
| `tests/test_phase3.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_review_contract.py for verdict/finding normalization; test_local_review_loop.py for correction behavior. | `None in retired file` |
| `tests/test_phase4.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_budget_accounting.py and test_recovery_policy.py for bounded failure/escalation semantics. | `None in retired file` |
| `tests/test_phase5.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_tranche_workflow.py for integrated review and release behavior. | `None in retired file` |
| `tests/test_phase6.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_context_packets.py and test_tranche_workflow.py for bounded packets/integration. | `None in retired file` |
| `tests/test_planner_contract.py` | MODIFY | Test `decomposition_planner.py`: strict request/response identity without authority ceremony. | `test_contract_hash_and_schema_identity()` |
| `tests/test_planner_routing.py` | MODIFY | Test `planning_coordinator.py`: no implementation/local-model fallback for configured paid planning. | `test_paid_planning_profile_is_explicit()` |
| `tests/test_planning_coordinator.py` | MODIFY | Test `planning_coordinator.py`: proposals and verified held cards without Ledger. | `test_materialize_only_current_tranche()` |
| `tests/test_process_identity.py` | MODIFY | Test `operator_controls.py`: supported stop ownership, actual run identity and incomplete containment. | `test_wrong_or_reused_pid_is_not_stopped()` |
| `tests/test_process_next_scheduler.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_reconciliation_snapshot_evidence.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_compatibility.py and test_coordinator_recovery.py for native run/snapshot reconciliation. | `None in retired file` |
| `tests/test_registered_runtime_cli.py` | MODIFY | Test `cli.py`: same standalone/Hermes command behavior and legacy argument rejection. | `test_cli_uses_new_runtime_without_ledger()` |
| `tests/test_release_routing_evidence.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_recovery_policy.py; inspect existing scenarios before removal and preserve relevant ordinary failure behavior. | `None in retired file` |
| `tests/test_repository_provenance.py` | MODIFY | Test `repository_snapshot.py`: allowlisted repository/base and candidate provenance. | `test_plan_bound_to_repository_base()` |
| `tests/test_repository_snapshot.py` | MODIFY | Test `repository_snapshot.py`: hardened deterministic snapshot evidence. | `test_snapshot_hash_is_revision_bound()` |
| `tests/test_review_reconciliation.py` | MODIFY | Test `review_routing.py`: missing/malformed evidence and bounded replacement. | `test_done_review_without_verdict_retries()` |
| `tests/test_review_retry_launch.py` | MODIFY | Test `review_routing.py`: same candidate/profile, native dispatcher ownership. | `test_retry_uses_fresh_native_run()` |
| `tests/test_routing_authority_boundary.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_runtime_binding.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_runtime_metrics.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_runtime_milestone1.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_runtime_milestone2.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_runtime_milestone2_operational.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_scheduler_concurrency.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_scheduler_crash_matrix.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_scheduler_daemon.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_scheduler_observability.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_scheduler_ordering.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_scheduler_reconciliation.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_scheduler_tranche_checkpoint.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_coordinator_recovery.py and test_pause_resume.py; drop duplicate scheduler/metrics-state assertions. | `None in retired file` |
| `tests/test_snapshot_revalidation.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_stale_routing_recovery.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_recovery_policy.py; inspect existing scenarios before removal and preserve relevant ordinary failure behavior. | `None in retired file` |
| `tests/test_state_projection_supersession.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_board_effects.py and test_crash_replay.py; drop shadow-row/projection-transaction assertions. | `None in retired file` |
| `tests/test_structured_verification.py` | MODIFY | Test `validation.py`: argv allowlist, cwd/env/output bounds and cleanup. | `test_timeout_cleans_process_group()` |
| `tests/test_supplemental_corrections.py` | MODIFY | Test `corrections.py`: finding lineage, sibling DAG and no blocked-parent deadlock. | `test_correction_preserves_root_budget()` |
| `tests/test_terra_activation_continuation_blockers.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_terra_revalidation_regressions.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_no_legacy_dependencies.py; keep stale-evidence cases in test_recovery_policy.py, not signer/capability assertions. | `None in retired file` |
| `tests/test_ticket_contract.py` | MODIFY | Test `ticket.py`: strict contract parsing and canonical hash. | `test_native_contract_roundtrip()` |
| `tests/test_ticket_escalation.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_budget_accounting.py and test_local_review_loop.py for native-run identity and limits. | `None in retired file` |
| `tests/test_ticket_readiness.py` | MODIFY | Test `readiness.py`: pure validity with native dependency ownership. | `test_contract_validation_not_scheduling()` |
| `tests/test_tranche_completion_rechecks.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_tranche_workflow.py and test_review_contract.py for revision-bound acceptance. | `None in retired file` |
| `tests/test_tranche_handoff.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_tranche_workflow.py and test_review_contract.py for revision-bound acceptance. | `None in retired file` |
| `tests/test_tranche_landing.py` | DELETE after case port | Legacy architecture test; port applicable cases to test_tranche_workflow.py and test_review_contract.py for revision-bound acceptance. | `None in retired file` |
| `tests/test_validation_git_safety.py` | MODIFY | Test `git_security.py / validation.py`: hooks/config/external diff/textconv protection. | `test_git_disables_external_execution()` |
| `tests/test_worktree_lifecycle.py` | MODIFY | Test `worktree_lifecycle.py`: no force-delete recovery and truthful retained-work report. | `test_partial_worktree_is_preserved()` |

### New test/support files (CREATE)

| File | Goal / collaborators | Representative functions or fixtures |
| --- | --- | --- |
| `tests/conftest.py` | scratch homes/boards/worktrees, fake provider endpoints, injectable clock and mutation lock. | `isolated_runtime; native_board; forbid_live_effects` |
| `tests/native_hermes_fixture.py` | actual target CLI under isolated home with stubbed worker spawn, no live profiles or endpoints. | `create_isolated_board; run_native; simulate_worker_run; assert_isolation` |
| `tests/test_board_compatibility.py` | actual native lane/claim/hold/stop capabilities, post-completion timing, archived prerequisites. | `test_native_review_source; test_complete_promotes_before_hook; test_archive_releases_native_child; test_hold_race` |
| `tests/test_board_effects.py` | typed adapter operations and exact readback, no direct DB writes. | `test_effect_verified; test_stale_run_rejected; test_unsupported_operation` |
| `tests/test_evidence_store.py` | permitted evidence schema, durable intents, unique operation keys and corruption handling. | `test_no_shadow_ticket_lifecycle; test_pause_survives_restart; test_unknown_effect_preserved` |
| `tests/test_enrollment.py` | explicit held-card opt-in, existing dependents and rejection of live adoption. | `test_no_implicit_enrollment; test_live_claim_requires_pause; test_anchor_dependent_release_is_held` |
| `tests/test_budget_accounting.py` | distinct bounds, root lineage and replacement cards, conservative paid starts. | `test_replacement_does_not_reset_budget; test_duplicate_run_not_counted_twice; test_paid_unknown_is_not_free` |
| `tests/test_review_contract.py` | strict verdict and identity, criterion evidence and no implicit pass. | `test_missing_verdict_is_not_approval; test_wrong_candidate_rejected; test_repair_without_valid_blocker_is_not_pass` |
| `tests/test_context_packets.py` | small-model context bounds and revision pinning. | `test_packet_bounds; test_required_context_not_silently_truncated` |
| `tests/test_git_integration.py` | serial CAS integration, dirty/conflicting work retention and supported worktree paths. | `test_cas_rejects_drift; test_conflict_preserves_work; test_no_force_teardown` |
| `tests/test_local_review_loop.py` | native implementation/fresh local review/repeated changes and premature done repair. | `test_fresh_session_required; test_changes_repeat; test_premature_done_creates_separate_review` |
| `tests/test_tranche_workflow.py` | paid planning through local pieces and combined paid corrections. | `test_paid_review_after_local_acceptance; test_corrections_get_local_then_paid_review; test_next_tranche_not_created_early` |
| `tests/test_recovery_policy.py` | known mismatch repairs, preserve ambiguous useful work, bounded escalation. | `test_duplicate_useful_work_holds; test_stale_approval_rereviews; test_repeated_issue_escalates` |
| `tests/test_coordinator_recovery.py` | one writer, missed hooks, automatic repair and incident dedup. | `test_two_reports_one_repair; test_periodic_scan_recovers_missed_hook; test_auto_hold_clears_only_after_verified_repair` |
| `tests/test_crash_replay.py` | crash before/after board effect, Git ref change and evidence ack. | `test_restart_after_create; test_restart_after_integration; test_ambiguous_outcome_not_repeated` |
| `tests/test_pause_resume.py` | operator vs automatic holds, real stop outcomes, human edits and late results. | `test_pause_survives_restart; test_partial_stop_is_not_success; test_manual_edits_reconcile; test_cancel_rejects_late_result` |
| `tests/test_plugin_surfaces.py` | registration, pre-tool guard coverage and bypass recovery, proposal provenance. | `test_registration_is_side_effect_free; test_wrong_completion_guard; test_bypass_still_requires_review; test_foreign_run_proposal_rejected` |
| `tests/test_notifications.py` | deduplicated successful-repair/escalation notices and bounded unknown delivery. | `test_one_notice_per_incident; test_alert_failure_does_not_fabricate_recovery` |
| `tests/test_no_legacy_dependencies.py` | no legacy imports, private DB writes, host monkey patches or signer requirement. | `test_new_dependency_graph; test_no_privileged_recovery; test_import_does_not_activate` |
| `tests/test_dashboard_workflow.py` | fixture UI/API parity, partial pause, role configuration and refreshed observations. | `test_scoped_controls; test_partial_stop_visible; test_invalid_role_rejected` |

### Test migration rule

Do not infer a test's value from its filename alone. The inventory names a destination for every existing test file; read its actual cases during that milestone, transfer any still-required scenario, then remove obsolete assertions. A signed-authority test may contain a useful stale-head case even though its old mechanism is removed. Record the transfer in the implementation commit/review notes. Fixture evidence, not keeping legacy tests green by importing the old controller, determines completion.

## 14. Cutover and deletion checklist

Before deleting runtime code in M6, enumerate remaining imports and command routes, verify extracted tests, build the package, and confirm new configuration does not open a legacy ledger. Document old source revision and state backup locations. Remove source files with explicit paths, not recursive project cleanup. Native boards, Git branches, existing `.worktrees`, `.hermes`, SQLite databases and operator profile settings are never deletion targets in this plan.

Do not migrate paused legacy work automatically. A later explicit adoption procedure must inspect each selected native ticket and preserved work, reconstruct supported evidence or schedule fresh review, and retain unresolved old data. Removing obsolete code does not grant authority to alter or resume the currently installed service. A loaded plugin may still use this checkout: source replacement is performed in isolation and activated only at authorized cutover.

## 15. Completion report Sol must deliver

- File action inventory reconciled with actual diff, including any justified additions/deletions.
- Canonical acceptance scenario → exact passing test mapping.
- Native compatibility outcomes, unsupported operations and fallback behavior.
- Verified separation of operator pause, recovery hold and cancellation.
- Evidence that restart/dedup/budget/preservation scenarios pass without sudo or shadow board state.
- Packaging, plugin discovery, dashboard and CLI results, with fixture/live distinction.
- Remaining operator policy choices (finite budget defaults, authorized paid escalation and polling cadence) explicitly documented rather than silently inherited.
- Clear stop boundary: code ready is not installed, enrolled, running or accepted on a real project until separately verified after authorization.
