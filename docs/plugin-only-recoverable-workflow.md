# Plugin-only, recoverable Local First workflow

## Status and precedence

This is the agreed replacement design direction, not a description of implemented behavior or an implementation authorization. The superseded board-first proposals and legacy operating guides have been removed from the active documentation. Their numerical budgets, authority machinery, and proposed host changes are not requirements for this design. See the [file-by-file implementation plan for GPT 6 Sol](plugin-only-implementation-plan.md) for implementation organization, interactions, migration and verification.

This document authorizes no migration, enrollment, dispatch, profile changes, service restart, existing-controller resume, or cutover. Existing ledgers and work are preserved. Command and tool names below are proposed interfaces, not existing CLI promises.

## Goal

Make quantized local models useful for bounded implementation, with paid Hermes profiles for planning and major reviews, while recovering from ordinary model and board mistakes without privileged intervention.

**Operating rule: detect mistakes, attempt bounded repair, verify recovery, and continue automatically. Ask the operator only when a safe next step cannot be established.**

The aim is recoverable automation, not an unbreakable board or a security boundary against arbitrary code running as the user's OS account.

## Non-negotiable boundaries

- Plugin-only implementation. No Hermes core changes, runtime monkey-patching of host functions, Kanban schema changes, database triggers, or direct SQL repairs.
- Hermes owns board tickets, statuses, dependencies, claims, runs, and worker dispatch. Use supported native operations and verify effects by reading back the exact target.
- No duplicate authoritative task lifecycle ledger. The plugin must not mirror a second ready/running/review/done state machine and try to restore the board to it.
- Ordinary operation and recovery run as the user's account. No sudo, root-custodied signing keys, protected interpreter bundle, or detached approval ceremony for normal review/correction work.
- Retain separate-session local review and paid tranche review, including repeated correction loops.
- Preserve partial work and evidence. No automatic destructive Git reset, useful-work deletion, or broad rewiring of unrelated tickets.
- Generic coding work does not authorize deployment, push, destructive migration, credential changes, or protected legacy recovery.

## Ownership and minimal persistence

| Concern | Owner |
| --- | --- |
| Work queue, lanes, dependencies, assignees, claims, runs, comments | Hermes Kanban |
| Candidate code, integrated tranche contents, base/head identities | Git |
| Decomposition validation, review routing, acceptance checks, recovery policy | Plugin |
| Plans, review judgments, findings, suggested repairs | Configured Hermes model profiles |
| Ambiguous/destructive recovery decisions and explicit review waivers | Human operator |

Prefer native comments, attachments, and structured handoff evidence for durable records. Plugin persistence is limited to information not adequately represented there: tranche membership, revision-bound review decisions, correction/recovery budgets, durable pause/cancellation intent, and operation identifiers or pending-effect records needed for reconciliation. Choose its exact storage during implementation design; do not introduce a duplicate ticket store by another name.

Kanban is authoritative for actual work state; a lane value is not proof that code passed review. A manual board change must not cause the plugin to restore an obsolete shadow state. Conversely, a manual move to done cannot fabricate approval evidence. Missing or inconsistent plugin evidence triggers reconstruction where unambiguous, otherwise a visible hold, never assumed acceptance or silently replenished budgets.

## End-to-end workflow

1. The operator submits/selects a tranche ticket for the managed workflow. Do not silently adopt unrelated existing board work.
2. A configured paid planning profile decomposes the request into tickets sized and scoped for the chosen local model. Validate acceptance coverage, scope, checks, dependencies, and finite work bounds before release.
3. Create only the work currently eligible to run. Tranche membership is an association, not automatically a native dependency edge.
4. A local implementation profile works in a persistent worktree and submits a candidate with checks and Git identity.
5. A fresh local-review session reviews that exact candidate. Prefer the native same-card review loop when the card's state and run ownership permit it. Fresh session is required; a different underlying model is not required.
6. Local review requests corrections until satisfied or a configured bound is reached. Every correction receives local review before acceptance.
7. Integrate accepted piece revisions into a dedicated tranche branch, then collect its actual base-to-head diff, commits, check results, ticket references, and unresolved findings. Do not rely solely on commit names reported by a model.
8. A configured paid Hermes review profile reviews the combined tranche against the original requirements.
9. Paid reviewers comment on affected tickets and request bounded corrections. The plugin coordinates ticket creation/return-to-work, preventing competing repair actions. Corrections pass local review, are integrated, and receive another paid tranche review.
10. Accept only the exact approved revision with required checks and findings resolved. Release subsequent tranche work only after that evidence is verified. Finishing a review task is not the same as approving the tranche.

Roles are configurable Hermes profiles, not hard-coded provider/model names. Record the actual review run/profile and candidate revision. Profile drift, unreadable candidates, or malformed verdicts do not mean approval.

Local acceptance does not imply canonical-branch merge, remote publication, or deployment. Existing external dependents of the anchor must be considered before completing it: do not release consumers that cannot access the accepted code.

## Native lane and dependency rules

Design against actual native operations, not unrestricted lane movement:

| Native behavior observed during investigation | Required treatment |
| --- | --- |
| `request_review` accepts ready/running with satisfied dependencies; live claims require ownership or explicit operator override | Use supported handoffs; do not clear another live run casually. |
| A review-lane task becomes running when its reviewer is claimed | Determine role from run provenance, not lane alone. |
| `request_changes` requires an active same-card review run and restores the implementer to ready or parent-gated todo | A paid tranche reviewer cannot use it on arbitrary sibling implementation tickets. |
| Waiting-review reopen is distinct from active-review request-changes | Choose recovery from actual state and ownership. |
| Done-ancestor reopening can invalidate descendants and reclaim their workers | Prefer correction tickets for completed work; never treat reopening as harmless. |
| Native links mean prerequisite to dependent, not container to member | Do not gate implementation behind its unfinished tranche anchor or corrections behind a blocked review they must resolve. |
| Done and archived prerequisites satisfy native dependency gating | Neither status alone proves review acceptance. |
| Adding parents to running dependents is normally refused | Build dependencies before release; contain active work before attempting supported repair. |
| Completion promotes dependents and may clean scratch workspaces before its hook fires | A post-completion watcher cannot guarantee prevention; persistent worktrees and controlled release are required. |

Normal local loop:

```text
ready -> running (implementation) -> review
      -> running (fresh local reviewer)
          -> request_changes -> ready/todo -> implementation again
          -> approved completion -> done
```

If a ticket is already done without adequate local review, leave its history intact and create a separate review ticket against the preserved candidate. If that review finds problems, create correction tickets. The plugin must still verify evidence before integration or downstream release.

## Plugin extension strategy and limits

Use plugin CLI/tools/dashboard routes for operator controls and structured review findings. Use `pre_tool_call` to catch normal mistakes, such as implementation-side completion that should request review. Keep native dispatcher ownership of claims and profile launches.

Tool interception is not a universal board-write guard: CLI, dashboard, direct Python, and direct registry dispatch are separate paths. Terminal command filtering is not robust OS isolation. A model with unrestricted same-user execution can bypass conventions or create unrelated work. Do not claim otherwise.

Post-commit lifecycle/dispatch hooks are wake-up hints, not atomic vetoes or durable exactly-once delivery. A singleton coordinator also reconciles periodically and on startup. Serialize managed ticket creation: native creation idempotency has a concurrent race and cannot replace coordinator serialization/readback.

Prefer withholding creation of future tranche work. Where queued work must exist, hold it using supported operations and verify the hold. An already-existing native dependency graph may still release on erroneous completion before the plugin reacts; detect, contain, and revalidate that work rather than claiming the race cannot happen.

Reviewers may directly comment. They should submit managed correction/recovery requests through plugin tools so the coordinator is the single writer for generated work. Independently created worker cards must be detected and assessed for membership/duplication; they are not automatically authorized work.

## Automatic detection and recovery

Check on relevant board events, worker/reviewer results, startup, and periodic reconciliation. Reviewers may also report an inconsistency through a proposed `report_workflow_issue(affected_ticket, observed_problem, proposed_recovery)` tool. Reports trigger inspection; model assertions are not proof or unrestricted repair authority.

The recovery cycle is:

```text
Detect -> reconcile current board/Git/run evidence
       -> contain affected work where needed
       -> choose a bounded supported repair
       -> apply and read back effects
       -> revalidate evidence -> continue
                              -> pause/escalate if unresolved
```

| Scenario | Automatic first response |
| --- | --- |
| Implementation marked done without local review | Preserve candidate; create missing independent local review; withhold piece acceptance. |
| Paid findings against completed work | Comment on originals; create bounded corrections; arrange local review and paid re-review. |
| Review card done without a usable verdict | Treat approval as missing; create replacement review of the same candidate within budget. |
| Code changed after approval | Old approval remains historical only; review the new revision before acceptance. |
| Interrupted/crashed review | Reconcile run outcome; use bounded native retry or a replacement review where the original lifecycle cannot resume. |
| Implementation crash | Preserve work; use supported native recovery within budget; inspect the candidate before accepting anything. |
| Restart during ticket creation or another board effect | Find and verify existing effects before retrying. An ambiguous outcome is not a license to replay blindly. |
| Clearly redundant, unstarted generated ticket | Hold it, retain canonical work, and record the relationship. Do not archive a dependency blindly: archive can release children. |
| Both duplicate tickets contain work | Preserve both; pause the ambiguous portion instead of discarding either. |
| Dependent started before prerequisite acceptance | Attempt supported stop/park of affected downstream work; preserve changes; repair prerequisite review and revalidate downstream work before release. |
| Human rearranged the board | Adopt coherent supported changes after reconciliation; invalidate stale approvals; never restore old topology merely to match cached expectations. |

Recovery cannot undo execution that already occurred. Claims of containment must be backed by observed run/process state. If the prerequisite candidate cannot be reconstructed, do not invent it from a ticket summary.

## Bounds and escalation

- One coordinator repair at a time per affected tranche, with serialization across shared resources.
- Separate implementation-attempt, infrastructure-retry, substantive review-correction, and workflow-recovery budgets.
- Count correction generations and replacement work against their originating tranche/finding. New ticket IDs do not reset the relevant budget.
- Repeated identical unresolved failures stop automatic retries; no unbounded ticket-generation loop.
- Exact numerical defaults remain an implementation-policy decision. Historical proposal numbers are not adopted implicitly.
- Verify every effect before dependent work continues. Resume automatically only after repair and validation succeed and no operator pause/cancellation remains.
- Human intervention is required for exhausted bounds, conflicting useful work, unsafe stop identity, missing irrecoverable evidence, destructive repair, unrelated work impact, or scope expansion.

Pause the affected tranche rather than the whole board where safe. Successful recovery gets a concise activity record, not an approval request. Failed recovery reports the problem, attempted repairs, preserved work, active/uncontained workers, and the precise human decision or action needed. Deduplicate notifications and bound auxiliary logs; retain essential native history/evidence.

## Proposed operator commands

Exact CLI syntax will be specified during implementation. These are semantic requirements, not current commands:

| Command | Behavior |
| --- | --- |
| `status` | Show active tranches/workers, reviews, holds, repair attempts, candidate revisions, and next safe actions. |
| `pause` | Persist an operator pause; stop new managed creation/release; hold managed queued work through supported operations. Running workers may finish and must be listed. |
| `pause --stop` | Also stop/park active managed work through supported cancellation/reclaim paths and preserve partial work. Report any worker that could not be safely stopped. |
| `reconcile` | Inspect real board, run, dependency, and Git state; report/adopt unambiguous evidence and identify remaining conflicts. Never silently resume an operator-paused workflow. |
| `resume` | Reconcile first; continue only coherent work with valid evidence and available budgets. Otherwise remain paused with actionable reasons. |

Commands should support explicit scope (one tranche or all plugin-managed work) without touching unrelated board work. Pause survives process/gateway restart. Stopping only the plugin process is not sufficient: Hermes dispatch is independent. If queued holds or worker termination cannot be verified, show a partial/uncontained pause, not success.

Distinguish operator pause, automatic recovery hold, and cancellation. The plugin may automatically clear its own recovery hold after verified repair, never an operator pause. Cancellation rejects late results and does not delete partial work or silently resume. An explicit human review waiver, if implemented, is recorded separately from model approval and normal lane changes.

Human repair fallback:

```text
pause --stop -> human edits board -> reconcile -> inspect -> resume
```

Ordinary repair must require neither sudo nor manual database editing.

## Compatibility work still required

Before implementation promises a working workflow, verify in isolated fixtures:

- Exact supported create/read/comment/hold/cancel/review operations and their CLI/API contracts on the target Hermes version.
- A race-aware managed-work hold protocol against the independent dispatcher; no nonexistent snapshot-CAS or exact-task-dispatch primitive may be assumed.
- Plugin hook loading in every participating profile/process; startup failure behavior and periodic reconciliation when hooks are missed.
- Minimal membership/evidence persistence and operation deduplication without a shadow task lifecycle.
- Structured review/finding schema, budget defaults, scope-selection syntax, and supported worker-stop verification.
- Serial Git integration, dirty/uncommitted candidate handling, and review identity across corrections.

Unsupported native transitions are handled by alternative supported work (such as replacement review/correction cards) or explicit partial recovery. Missing host hooks are not permission to patch Hermes.

## Acceptance scenarios

A future implementation is not complete until disposable fixtures demonstrate:

1. Tranche submission produces bounded local-model work, fresh local reviews, and paid review of the integrated revision.
2. Both review levels can request multiple correction rounds, with comments, usable finding links, fresh reviews, and bounded escalation.
3. Premature implementation completion automatically produces the missing review without accepting unreviewed code.
4. A review card completed without a verdict cannot approve the tranche; replacement review is bounded.
5. A changed head invalidates old approval for acceptance purposes.
6. Completion racing native downstream dispatch is detected; affected work is contained where possible, preserved, and revalidated. Failure to contain is reported honestly.
7. Restart after create/comment/link effects does not blindly duplicate work; ambiguous effects stop safely.
8. Concurrent recovery reports are serialized; correction ticket creation does not reset root budgets.
9. A missed hook is repaired by startup/periodic reconciliation.
10. Pause survives restart, holds managed queued work, lists running work, and leaves unrelated work alone.
11. Pause-and-stop verifies actual worker outcomes and retains partial work; late results cannot override cancellation or manual pause.
12. Human board edits can be reconciled and resumed without sudo, direct SQL edits, or rebuilding a second ledger.
13. Archived gates, malformed verdicts, missing evidence, and accidental done statuses do not count as approval.
14. Duplicate useful work is preserved; no automatic destructive reset or broad descendant reopening is used as routine repair.
15. Exhausted recovery produces one actionable escalation rather than endless retries or alerts.
16. Installation and execution require no host source changes or runtime monkey-patching; no live cutover occurs as a side effect of testing.

## Investigation references

Behavior was grounded in the installed Hermes sources, not exercised against the live board. Recheck compatibility before implementation; line numbers and interfaces can change.

- [Hermes Kanban documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)
- Host `hermes_cli/kanban_db.py`: `complete_task`, `request_review`, `request_changes`, `reopen_review_task`, `invalidate_descendants_for_parent_reopen`, `create_task`, `_parents_satisfied`.
- Host `tools/kanban_tools.py`: `_enforce_worker_task_ownership`, `_worker_guard`.
- Host `hermes_cli/kanban_db_dispatch.py`: `dispatch_once`, review dispatch, claim/spawn paths.
- Host `hermes_cli/plugins.py`, `hermes_cli/plugins_dispatch.py`, `model_tools.py`: plugin registration and tool/lifecycle-hook boundaries.
- Existing plugin `local_first_orchestrator/keyless_human_recovery.py`: root-owned protected recovery, explicitly excluded from this replacement's normal workflow.
- Superseded board-first proposals and legacy operating guides: historical context only, recoverable from the documentation backup or tracked Git history; not active implementation instructions.
