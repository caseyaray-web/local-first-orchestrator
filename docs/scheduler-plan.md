# Local First scheduler completion plan

This document expands the scheduler item in `docs/design-v2.md` into a concrete implementation roadmap. It is ordered to preserve the project’s durability, idempotency, Hermes-ownership, and fail-closed recovery requirements.

The scheduler should be considered architecturally complete only after the lifecycle slices through scheduler-wide crash/recovery proof are complete. The daemon comes afterward and should remain a thin wrapper around a scheduler that is already safe when called one tick at a time.

## Current position

Already complete or substantially complete:

1. **Scheduler foundation — Complete**
   - bounded `process-next`
   - global tick lease and `busy` handling
   - pause handling
   - truthful dry-run preview
   - board/outbox stage ordering
   - dependency-readiness stage
   - scheduler claim/reclaim/finalization lifecycle
   - crash-safe started/completed/finalized markers

2. **Implementation stage — Complete for the current scheduler slice**
   - claims `ready_local` work
   - persists model invocation intent before inference
   - binds results to exact ticket, attempt, artifact, worktree, and diff
   - refuses to reinvoke unknown or terminal-failed invocations
   - recovers a completed invocation whose model-stage artifact persistence was interrupted
   - replays a persisted model stage without model reinvocation
   - stops before deterministic validation

3. **Deterministic validation stage — Complete for the current scheduler slice**
   - validation is independently claimable after implementation
   - candidate identity is bound to the exact implementation attempt/diff/worktree
   - deterministic validation evidence is persisted durably
   - crash/replay behavior is covered without crossing into review in the same tick

4. **Fresh review stage — Complete for the current scheduler slice**
   - review is independently claimable only after passing validation
   - review uses a fresh packet-only context
   - review claims bind both candidate identity and configured review execution policy
   - completed review invocations can be recovered before model-stage persistence without reinvocation
   - unknown started invocations fail closed
   - adapter-authored review artifacts are preserved intact
   - repair/acceptance remain separate later stages

5. **Repair-routing stage — Complete for the current scheduler slice**
   - failed validation remains `verifying` until a later routing tick decides repair vs triage
   - completed review output carries normalized verdict/findings/criteria into a later routing tick
   - routing decisions are persisted before repair/triage work is exposed
   - validation failures use stable fingerprints that ignore volatile paths, line numbers, and timestamps
   - repeated fingerprints and `max_attempts` route to triage exactly once
   - repair creates an attempt-scoped continuation on the same worktree/branch and carries failure evidence into the next implementation context
   - pass review is recorded without accepting the candidate in the routing tick
   - repair-routing effect completion and state transition are atomic and restart-finalizable

6. **Bounded triage/decomposition stage — Complete for the current scheduler slice**
   - only durable repair-routing decisions whose action is `triage` are eligible
   - triage uses the configured local decomposition route in planning-only `safe` mode
   - claim identity binds parent depth, unresolved criteria, failure evidence, ticket policy, and triage execution policy
   - valid triage proposals are persisted before child materialization/projection
   - existing child-count, depth, scope, criterion, readiness, and duplicate rules remain authoritative
   - ledger-assigned child IDs replace model-proposed IDs for durable identity
   - scheduler-created children inherit repository/runtime provenance and explicit criterion linkage
   - child materialization and create-card outbox enqueue are atomic
   - pending child-card projection prevents implementation from claiming the child
   - completed planner invocations and post-materialization crashes recover without duplicate inference, children, or outbox rows
   - `block` and `checkpoint` map to existing canonical states without creating children

7. **Acceptance / candidate-freeze stage — Complete for the current scheduler slice**
   - pass-reviewed work is frozen into immutable pre-commit evidence
   - live worktree/base/diff and validation/review artifact identities are rechecked before acceptance
   - acceptance remains independently replayable and creates no Git commit

8. **Git integration / commit stage — Complete for the current scheduler slice**
   - only immutable accepted candidates are eligible
   - commit launch intent is persisted before Git mutation
   - commit identity is checked against exact base, branch, authorized paths, message, and accepted diff
   - non-cache untracked content fails closed because its bytes are not bound by the accepted fingerprint
   - a post-commit restart recovers the exact child commit without creating a second commit
   - tranche integration-head advancement uses the existing compare-and-swap ref and is independently recoverable
   - append-only commit evidence is persisted before any later completion projection
   - the ticket remains `accepted`; completion is a separate later stage

9. **Completion / Hermes projection stage — Complete for the current scheduler slice**
   - only accepted tickets with immutable accepted-candidate and Git commit evidence are eligible
   - completion claim identity binds the accepted evidence hash, exact commit, branch, base/worktree, tranche, and integration-head provenance
   - legacy-compatible `accepted_evidence` is derived deterministically from immutable scheduler evidence
   - `accepted → done`, accepted evidence, and Hermes state/comment outbox intents are one SQLite transaction
   - completion effect persistence is atomic with the local terminal transition
   - post-effect restart finalizes without repeating completion
   - Hermes state/evidence delivery failures retry through existing outboxes without repeating model, validation, review, acceptance, or Git work

10. **Native dependency release stage — Complete for the current scheduler slice**
   - authoritative Hermes task IDs are resolved for every active dependent and parent
   - missing native parent→child edges are projected with Hermes `link` and exact parent-set equivalence is verified afterward
   - unexpected/ambiguous Hermes edges stop reconciliation rather than being silently changed
   - dependent tickets no longer enter the legacy ledger-local readiness path
   - release eligibility requires each parent’s Local First completion plus acknowledged Hermes `done` projection
   - Hermes `ready` is observed as the authoritative readiness result and persisted without moving the dependent to `ready_local`
   - graph-link and release-read restart paths re-read Hermes and avoid duplicate links or a second readiness truth source

11. **Tranche integration / checkpoint stage — Complete for the current scheduler slice**
   - all materialized tranche tickets must be `done` with accepted commit evidence
   - claim identity freezes ticket/commit membership, planning repository/base/snapshot provenance, and configured integration commands
   - the original planning snapshot hash and selected evidence blobs are revalidated before checkpointing
   - existing tranche completion evidence proves the serialized commit chain and current integration-head identity
   - integration commands run only against the clean final accepted worktree at the final integration commit
   - immutable checkpoint evidence records command results, completion hash, repository provenance, artifact hash, and deterministic decision
   - checkpoint effect replay finalizes without rerunning integration commands
   - no paid call and no next-tranche activation occurs in this stage

12. **Paid checkpoint / escalation stage — Complete for the current scheduler slice**
   - only immutable `ready_for_checkpoint` tranche evidence is eligible for paid checkpoint review
   - claim identity binds feature/tranche, checkpoint artifact/completion hashes, final integration SHA, purpose, provider, model, and registered profile provenance
   - the scheduler claim ID is the usage-governor request key, so one logical stage cannot obtain a second reservation
   - the existing governor atomically reserves budget before `model_calls` records an in-flight paid invocation and before provider execution
   - registered paid routes use Hermes packet-only `chat --toolsets safe` with explicit provider/model selectors
   - exact `{decision, rationale}` output is required and immutable paid evidence binds the reservation/model-call IDs
   - `escalate` from checkpoint review enables one distinct purpose-scoped escalation stage; approval/rejection never activate the next tranche in this milestone
   - budget exhaustion leaves the scheduler claim blocked until an explicit one-call approval is granted
   - ambiguous paid outcomes become durable `unknown_outcome` reservations and replay never calls the provider again
   - completed paid calls can be recovered after a crash before scheduler effect persistence without repeating the provider call

13. **Next-tranche activation stage — Complete for the current scheduler slice**
   - activation requires effective paid approval of the predecessor checkpoint; checkpoint `approve` or escalation `approve` after checkpoint `escalate`
   - the materialization claim freezes predecessor/successor ordinal identity, completion/checkpoint lineage, approval model-call identity, and repository identity
   - successor planning re-snapshots/re-plans at the predecessor's final integration SHA through the existing `PlanningCoordinator`
   - generated successor cards now carry the re-snapshot successor plan's repository/base/snapshot provenance rather than stale original-plan provenance
   - predecessor completion, successor activation, successor ticket creation, and generated-card outbox creation remain one existing ledger transaction
   - materialization evidence freezes the exact successor ticket set and re-snapshot hash
   - crash after local materialization but before scheduler effect persistence reuses the already-active successor and does not re-plan
   - final activation evidence is withheld until every successor card has an acknowledged external task ID and every dependent successor has matching immutable Hermes-native graph evidence
   - immutable activation evidence freezes successor ticket IDs, Hermes task IDs, dependency graph hashes, and the re-snapshot identity
   - partial card/link projection recovers through the existing idempotent generated-projection/native-graph stages without duplicate cards or links

14. **Scheduler-wide reconciliation model — Complete for the current scheduler slice**
   - reconciliation is derived from existing durable claims/evidence rather than stored as a second scheduler state machine
   - every scheduler claim is classified as not started, external outcome unknown, external effect completed/local incomplete, local completion/downstream incomplete, or fully finalized
   - each classification produces one explicit action: `retry`, `replay`, `reconcile`, `resume`, or `stop`
   - model invocations, paid reservations, Git intent/evidence, checkpoint/materialization evidence, scheduler effect timestamps, and existing outboxes remain authoritative
   - ambiguous or terminal model/paid outcomes classify as `stop` and preserve the existing stage-specific reconciliation errors rather than silently retrying
   - Git with a durable intent replays exact reconciliation; Git without an intent safely replays pre-mutation verification
   - completed stage effects classify as `reconcile` so finalization can occur without repeating the external effect
   - finalized stages with pending board/comment outboxes classify as downstream-incomplete `resume`
   - live `process-next` and read-only dry-run consult the same reconciliation classifier before normal stage work

15. **Scheduler-wide ordering policy — Complete for the current scheduler slice**
   - one explicit `SCHEDULER_STAGE_ORDER` / rank table defines the cross-class priority instead of relying on incidental branch placement
   - terminal/ambiguous reconciliation `stop` remains a safety fence before eligible work is considered
   - eligible work order is generated-card projection → state projection → evidence comment → recoverable expired claim → implementation → validation → review → repair routing → triage → acceptance → Git integration → completion → native dependency graph → native dependency release → tranche checkpoint → paid checkpoint → paid escalation → next-tranche materialization → next-tranche activation → root dependency-readiness admission
   - generated-card creation precedes state/comment projection because Local First must first establish the external Hermes task identity that later board effects target
   - state projection precedes evidence comments so authoritative state is projected before explanatory evidence for the same lifecycle progression
   - an expired recoverable lifecycle claim globally owns the lifecycle slot after external projection work; every non-matching lifecycle class is ineligible for that tick
   - recovery therefore cannot be starved by fresh implementation/validation/etc., while already-durable external effects are still drained first
   - fresh implementation precedes fresh validation across tickets, and the remaining fresh lifecycle stages follow the dependency-ordered roadmap
   - new root readiness admission is last so the scheduler drains existing owned work before admitting additional implementation work
   - dry-run and live execution use the same recovery selector and fresh-stage order; per-stage candidate SQL retains deterministic `created_at`/identity ordering

16. **Scheduler-wide concurrency proof — Complete for the current scheduler slice**
   - the proof uses independent Ledger connections and real threads so SQLite write contention is exercised instead of being hidden by a single Ledger object's in-process lock
   - all scheduler claim mutations run inside `BEGIN IMMEDIATE` transactions; the global tick lease serializes bounded `process-next` execution before any stage/provider side effect can start
   - overlapping live ticks prove one worker reaches implementation/model-launch while the competing scheduler returns `busy`; the winner selects the same deterministic `created_at,id` candidate as the ordering policy
   - concurrent expired tick takeover produces exactly one new lease owner/token
   - duplicate implementation-stage claims for one ticket produce exactly one durable claim/lease owner across separate connections
   - an expired implementation recovery claim wins over a simultaneously eligible validation stage for that same ticket, preserving lifecycle order
   - concurrent state-projection claims lease one outbox row exactly once; existing comment/generated outbox tests prove the same lease/idempotency pattern for those workers
   - the winning implementation runner persists exactly one model invocation before provider execution; overlapping scheduler workers cannot reach a second launch boundary
   - concurrent tranche integration-head updates use Git compare-and-swap so exactly one expected-old-head update wins and the loser fails closed
   - concurrent paid authorization through separate Ledger connections and one request key returns one reservation identity and creates one durable reservation row
   - the specialized comment/outbox, model-invocation, Git, and paid-governor race suites remain green alongside the scheduler-wide contention tests

17. **Scheduler-wide crash matrix — Complete for the current scheduler slice**
   - every scheduler-owned work class except the meta `recovery` slot now has an explicit declarative crash policy
   - common durable boundaries are globally fixed as: claim/no effect → `retry`; durable external effect/local incomplete → `reconcile`; local stage complete/downstream incomplete → `resume`; fully finalized → `resume`
   - started model stages (`implementation`, `review`, `triage`) and paid stages (`paid_checkpoint`, `paid_escalation`) are fail-closed `stop` unless their authoritative invocation/reservation records prove a completed result
   - generated/state/comment projection, deterministic validation/routing/acceptance/completion/readiness, Git reconciliation, native dependency graph/release, tranche checkpoint, and next-tranche stages are explicitly replayable through their existing idempotent or immutable authorities
   - the crash-policy registry is tested against `SCHEDULER_STAGE_ORDER`, so adding a new scheduler work class without a crash policy fails the suite
   - injected-crash tests prove no duplicate model calls, validation evidence, review calls, repair/triage transitions, generated child/outbox rows, accepted candidates, Git commits, board effects, dependency/tranche activation, or paid calls/reservations

18. **Scheduler observability — Complete for the current scheduler slice**
   - `scheduler_observability()` is a bounded read-only projection over existing durable authorities; it does not persist a second metrics/state model
   - the snapshot reports current/next stage, selected ticket, claim ID/status, lease owner/expiry, claim attempt count, lifecycle attempt number, side-effect timestamps, and reconciliation state/action/reason
   - pending generated-card, state-projection, and evidence-comment counts plus bounded active outbox leases show outstanding board effects
   - latest runtime-stage artifact path/SHA/base identity, latest model invocation identity/status, review result identity, accepted-candidate hashes, Git intent/evidence, and paid reservation state are exposed when present
   - claim selection follows the same `preview_next()` / reconciliation choice when possible so the operator sees the boundary the scheduler would actually act on
   - the lightweight default `status` output remains unchanged; `status --scheduler-detail` opts into the deeper lifecycle snapshot
   - observability tests verify the snapshot performs zero SQLite writes and pinpoints both normal next-stage work and fail-closed model reconciliation

19. **Daemon wrapper — Complete for the current scheduler slice**
   - `SchedulerDaemon` is a thin operational loop over `ProcessNextScheduler.process_next()`; it owns no lifecycle transition, claim, recovery, ordering, or reconciliation semantics
   - one shared registered scheduler factory now constructs both `process-next --execute` and daemon ticks, so one-shot and continuous execution use the same runners, routes, leases, paid adapters, checkpoint logic, and next-tranche materialization path
   - each daemon iteration executes at most one proven scheduler tick, then sleeps on `no_work`/`idle`, `busy`, or `paused` according to bounded configurable delays
   - transient iteration failures use bounded exponential backoff; successful ticks reset the consecutive-error backoff counter
   - SIGINT/SIGTERM request graceful shutdown between bounded ticks; the daemon never aborts or rewrites an in-flight durable stage on its own
   - pause behavior is inherited from the existing scheduler: durable external effects may drain while new lifecycle claims remain blocked by ledger pause checks
   - daemon health reports iteration/success/idle/busy/paused/error counts, consecutive errors, last stage/ticket/status/error, and tick timestamps; status combines this with the Milestone 18 scheduler snapshot
   - CLI execution remains fail-closed: `daemon` requires registered runtime, `--execute`, `--allow-board-writes`, explicit Hermes executable/board access, and exposes bounded sleep/backoff/max-iteration controls
   - restart tests prove a fresh daemon over the same ledger simply continues the next durable board effect left by the previous daemon, with no daemon-specific recovery path

20. **Real end-to-end acceptance — Complete for the representative low-risk scheduler scope**
   - a dedicated real Hermes board/task was imported through the supported Local First CLI and driven by the production daemon/scheduler, not by direct lifecycle-row mutation
   - real local implementation used `custom:lm-studio` / `qwen3.8-27b@iq3_s` in an isolated Git worktree and produced exactly one completed implementation invocation
   - deterministic validation passed and persisted before review
   - real fresh review used a separate review profile/process with `openai-codex` / `gpt-5.6-terra`, produced exactly one structured `pass` verdict, and did not reuse the implementation profile home/tool loop
   - acceptance froze immutable candidate evidence before one real Git commit/integration-head advance
   - Hermes state/comment completion projection reached `done`, all workflow outboxes drained, and the final scheduler state was `no_work`
   - a real post-Hermes/pre-ledger comment-delivery crash was injected; restart reconciled the persisted marker with one remote delivery and no duplicate comment
   - a separate live Hermes parent/child probe verified exact native parent linkage and child `todo -> ready` advancement after parent completion
   - the representative low-risk path created zero paid reservations and zero paid checkpoint evidence
   - the real run exposed and fixed current-Hermes integration gaps in independent review profile wiring, comment marker reads, idempotent scheduled-state projection, and scheduled completion transition handling
   - detailed evidence is recorded in `docs/milestone-20-real-acceptance.md`; final repository validation is 735 tests / 179 subtests passing

The remaining work should proceed in the following order.

## 3. Deterministic validation stage — Complete

Make validation a first-class `process-next` stage rather than a helper hidden inside implementation.

Required slices:

- Add validation eligibility and dry-run preview for implementation candidates with a durable model-stage artifact and no completed validation stage.
- Add a durable validation claim bound to the exact ticket, attempt, implementation artifact, worktree, base SHA, and implementation diff hash.
- Freeze and re-check candidate identity before running validation so repository/worktree drift fails closed.
- Extract a validation-only controller operation that performs only deterministic checks and configured verification commands.
- Persist validation output as a durable stage artifact bound to the exact implementation candidate and validation policy.
- Apply the scheduler started/completed/finalized lifecycle to validation.
- Define restart behavior for: not started, command started/outcome unknown, validation completed/artifact persistence interrupted, validation artifact persisted/scheduler completion interrupted, and scheduler completion/finalization interrupted.
- Route outcomes without crossing into review in the same tick: pass becomes review-eligible; deterministic failure becomes repair-routing eligible; reconciliation-required conditions stop fail-closed.

Completion condition: a persisted implementation candidate can be picked up by a later `process-next`, validated against its exact attempt/diff/worktree and trusted verification policy, survive crash/restart without duplicate or ambiguous evidence, and end in a durable state from which review or repair can proceed.

## 4. Fresh review stage — Complete

Make fresh review independently claimable and restart-safe.

Required slices:

- Claim only candidates with completed deterministic validation.
- Build a fresh review context independent of the implementation conversation/tool loop.
- Bind review input to the exact validated diff and validation evidence.
- Invoke the configured review model through the existing strict review contract.
- Persist closed-schema review output before scheduler finalization.
- Apply started/completed/finalized stage semantics and replay completed review artifacts without reinvocation.
- Refuse unknown review invocations until reconciliation resolves them.
- Do not invoke repair in the same scheduler tick.

Completion condition: a validated candidate receives one fresh independent review, whose exact structured result is durably bound to that candidate and can be recovered without duplicate review calls.

**Current status:** Complete for this scheduler milestone. Review claims now bind candidate identity plus provider/model/timeout/schema execution identity; adapter-authored artifacts are preserved; completed invocations interrupted before model-stage persistence are recoverable without reinvocation; ambiguous started invocations still fail closed.

## 5. Repair-routing stage — Complete

Turn deterministic validation failures and blocking review findings into bounded same-ticket lifecycle decisions.

Required slices:

- Normalize the failure source into durable repair-routing input.
- Apply failure fingerprints and attempt counters.
- Route eligible failures back to the same ticket’s implementation lifecycle.
- Enforce `max_attempts` and repeated-fingerprint limits.
- Refuse automatic repair for policy-invalid or reconciliation-required conditions.
- Route exhausted/repeated failure to triage exactly once.
- Persist the routing decision before exposing new work.

Completion condition: failures cannot create uncontrolled retry loops, and every retry/triage decision is durable, bounded, and restart-safe.

**Current status:** Complete for this scheduler milestone. Repair routing is a ledger-derived stage with no model call of its own. It atomically records the decision and state transition, preserves same-ticket attempt continuity for repair, routes repeated/exhausted failures to triage, and leaves pass review candidates for the separate acceptance milestone.

## 6. Bounded triage/decomposition stage — Complete

Bring the existing triage policy machinery under scheduler control.

Required slices:

- Claim only tickets whose repair policy requires triage.
- Run bounded decomposition using the existing child-count, depth, criterion-mapping, and duplicate rules.
- Persist the decomposition proposal before projecting child work.
- Project generated children through existing retry-safe board mechanisms.
- Reconcile restart after child creation/projection without generating duplicates.
- Preserve parent state and criterion linkage exactly.

Completion condition: triage creates at most the permitted bounded child set, exactly once, with every child mapped to unresolved acceptance criteria and recoverable projection state.

**Current status:** Complete for this scheduler milestone. Triage is independently claimable only from a durable repair-routing decision, runs through a planning-only decomposition route, persists and recovers model provenance, materializes bounded children using ledger-assigned identities, atomically enqueues their retry-safe Hermes projections, preserves repository and criterion provenance, and blocks child implementation until external card creation is acknowledged. Restart coverage includes completed-invocation recovery and the post-materialization/pre-finalization boundary without duplicate inference or child creation.

## 7. Acceptance / candidate-freeze stage — Complete

Persist the final accepted candidate identity after review passes.

Required slices:

- Bind acceptance to the exact ticket, attempt, implementation diff, validation artifact, and review artifact.
- Re-check candidate/worktree identity before acceptance.
- Persist accepted evidence atomically with the acceptance state transition.
- Invalidate or stop if repository/worktree drift means the reviewed candidate is no longer the candidate being accepted.
- Make acceptance independently replayable without rerunning review.

Completion condition: there is one durable, immutable accepted-candidate identity that later Git integration can trust.

**Current status:** Complete for this scheduler milestone. A durable pass-routing decision becomes independently acceptance-eligible; acceptance re-checks the live worktree/root/base and exact implementation diff, validates the frozen review candidate plus implementation/validation/review artifacts and hashes, persists an append-only `accepted_candidates` record, and transitions `local_review → accepted` atomically with scheduler effect completion. No Git commit is created in this stage. Restart after effect completion finalizes without re-running the acceptance inspection, while candidate or artifact drift fails closed.

## 8. Git integration / commit stage — Complete

Move accepted work into the durable Git-integration lifecycle exactly once.

Required slices:

- Claim only accepted candidates.
- Verify canonical repository base/provenance before integration.
- Re-check the accepted diff/worktree identity.
- Create the commit using repository conventions without bypassing existing Git safeguards.
- Persist commit identity and provenance before external completion projection.
- Reconcile the crash states: commit not started; commit created but ledger not updated; ledger updated but later projection incomplete.
- Never create a second commit for the same accepted candidate on replay.

Completion condition: an accepted candidate produces exactly one durable commit identity, or stops for reconciliation when exact recovery cannot be proven.

**Current status:** Complete for this scheduler milestone. The stage claims only immutable accepted candidates, persists an immutable commit intent before Git mutation, re-checks repository/worktree/base/branch/diff/authorized-path identity, creates exactly one isolated child commit through `GitWorktreeAdapter.accept`, and records append-only `git_commit_evidence` before completion projection. Replays recover an exact single child commit when the process dies after `git commit`, recover an already-advanced tranche integration ref after CAS, and refuse ambiguous commits, missing launch intent, candidate drift, conflicting tranche heads, or untracked bytes that were not part of the accepted fingerprint. The ticket remains `accepted`, and no `done` transition or completion projection occurs in this stage.

## 9. Completion / Hermes projection stage — Complete

Project locally accepted and committed work back to Hermes without repeating prior side effects.

Required slices:

- Require the exact accepted evidence and commit identity before completion.
- Enqueue completion/evidence effects through the existing outbox mechanisms.
- Retry board projection without repeating model, validation, review, or Git work.
- Persist final local completion only according to the ownership semantics in `docs/design-v2.md`.
- Keep evidence delivery idempotent to the extent supported by the current Hermes read/write contract.

Completion condition: board-write failures are independently retryable and can never force a repeat of already-successful implementation, validation, review, or Git work.

**Current status:** Complete for this scheduler milestone. Completion claims only `accepted` tickets whose immutable `accepted_candidates`, completed `git_commit_intents`, `git_commit_evidence`, and attempt commit identity all agree. The completion effect deterministically materializes the existing `accepted_evidence` compatibility record, records the exact commit/evidence provenance, transitions `accepted → done`, and enqueues the existing Hermes state-projection and evidence-comment intents in the same SQLite transaction. If that transaction commits but scheduler finalization is interrupted, replay resumes from `done` and finalizes without duplicating the terminal transition or evidence. Hermes state/comment delivery then proceeds through the existing independently retryable outboxes, so remote write failures cannot repeat implementation, validation, review, acceptance, or Git integration.

## 10. Native dependency release stage — Complete

Complete the Hermes-native dependency/readiness handoff required by v2.

Required slices:

- Verify the projected Hermes dependency graph matches the Local First architecture graph for active work.
- After completion, allow Hermes-native semantics to expose newly unblocked work rather than creating a competing private readiness model.
- Reconcile Local First’s ledger projection with Hermes after restart.
- Detect graph divergence and stop instead of silently advancing inconsistent dependencies.

Completion condition: task completion releases dependent work through Hermes-native dependency semantics with graph equivalence verified and no second scheduler truth source.

**Current status:** Complete for this scheduler milestone. Local First now resolves each dependent ticket and declared parent to authoritative Hermes task IDs, projects missing native parent→child links through `hermes kanban link`, and persists immutable graph evidence only after `show --json` reports the exact expected parent set. Extra or ambiguous native parents fail closed. Dependent tickets are excluded from the legacy scheduler-local readiness claimant. After every parent reaches Local First `done` and that `done` projection is acknowledged by Hermes, a separate release claim revalidates the stored graph and completion lineage, re-reads Hermes, requires Hermes itself to expose the child as `ready`, and persists immutable release evidence without transitioning the child to `ready_local`. Restart after a partially observed/link-applied graph or started release claim re-reads Hermes and resumes without duplicate links. This completes scheduler ownership of native graph projection/release; live operational proof against a real Hermes board remains tracked separately.

## 11. Tranche integration and checkpoint stage — Complete

Bring tranche boundaries into the same one-stage-per-tick scheduler model.

Required slices:

- Detect when all required work in a tranche has reached the required terminal state.
- Revalidate the canonical repository against the architecture/tranche snapshot.
- Run tranche-level deterministic integration verification.
- Persist checkpoint packets and decisions as durable stage artifacts.
- Fail closed on repository drift or missing evidence.
- Do not activate the next tranche in the same durable stage unless the design explicitly allows the transition to be atomic.

Completion condition: a tranche can finish, integrate, checkpoint, and become eligible for next-tranche activation without bypassing repository-snapshot or evidence requirements.

**Current status:** Complete for this scheduler milestone. The scheduler now detects active tranches whose materialized tickets are all `done` with accepted commit evidence, binds the exact ticket/commit set plus active planning snapshot provenance and configured integration commands into a durable tranche-checkpoint claim, and executes deterministic checkpoint work against the canonical repository and final integrated worktree. The controller revalidates the stored planning snapshot hash/content at its original base, reuses `completion_evidence(...)` to prove the serialized integration chain and integration-head identity, requires the final accepted worktree to be clean at the final integration commit, and runs the tranche's configured integration commands with bounded captured output. The ledger independently validates command/result pairing and the derived decision, persists immutable `tranche_completion_evidence` plus immutable `tranche_checkpoint_evidence`, and binds both to a hashed checkpoint artifact. `ready_for_checkpoint` and `integration_failed` are durable decisions; neither paid checkpoint review nor next-tranche activation occurs in this stage. A completed effect can be finalized after restart without re-running integration commands.

## 12. Paid checkpoint / escalation stage — Complete

Integrate paid-model work under the existing usage governor.

Required slices:

- Reserve paid budget atomically before invocation.
- Bind every reservation to purpose, feature/tranche/ticket, model/provider, and scheduler stage.
- Persist invocation intent before the paid side effect.
- Persist result/failure provenance and reconcile unknown invocations without double-spending.
- Pause or block when budget is exhausted rather than overspending.
- Support explicit approval/rejection where required by the design.

Completion condition: restart or retry can never duplicate a paid call or spend outside the governor’s durable authorization.

**Current status:** Complete for this scheduler milestone. Paid checkpoint claims are created only from immutable deterministic tranche checkpoints whose decision is `ready_for_checkpoint`. The claim freezes feature/tranche identity, checkpoint artifact and completion hashes, final integration SHA, purpose, and registered provider/model/profile provenance; its deterministic claim ID is reused as the `UsageGovernor` request key. The existing governor therefore reserves budget atomically before a `model_calls` row is marked in flight and before the provider side effect. Production registered routes use `HermesPaidModelAdapter`, which invokes packet-only Hermes `chat --toolsets safe` with explicit provider/model selectors; the registered profile is retained as route provenance because Hermes chat has no profile CLI flag. Responses must be exactly `{decision, rationale}` with `approve`, `escalate`, or `reject`. Immutable `paid_checkpoint_evidence` binds the checkpoint lineage, scheduler claim, governor reservation, model-call record, route identity, response, and decision. An `escalate` checkpoint result enables one separate escalation-purpose claim/call. Budget exhaustion performs no provider call and leaves the claim blocked; the `approve-paid` operator command grants exactly one additional purpose-scoped call, after which expired-lease replay reuses the same claim/request key. Ambiguous provider outcomes are marked `unknown_outcome` and cannot be re-invoked; a provider call that completed before a crash but whose scheduler effect was not yet applied is recovered from the completed durable model-call response without a second provider call. Next-tranche activation remains a distinct later stage.

## 13. Next-tranche activation stage — Complete

Make activation a durable graph-projection operation.

Required slices:

- Require the prior tranche’s checkpoint/approval evidence.
- Bind activation to the current canonical repository snapshot.
- Materialize the next tranche’s exact Hermes-native parent/dependency graph.
- Verify graph equivalence after projection.
- Recover from partial projection without duplicate cards or links.

Completion condition: the next tranche is activated exactly once against the expected repository state and exact dependency graph.

**Current status:** Complete for this scheduler milestone. Activation is implemented as two bounded scheduler phases so no scheduler lease is held across Hermes projection. `next_tranche_materialize` is eligible only when the predecessor has immutable deterministic checkpoint evidence and an effective paid approval: a direct checkpoint `approve`, or an escalation-purpose `approve` following checkpoint `escalate`. The claim binds predecessor/successor ordinals, checkpoint/completion hashes, approval model-call provenance, final integration SHA, and repository identity. The registered standard decomposition route then invokes the existing `PlanningCoordinator.materialize_next_tranche(...)`, which re-snapshots and re-plans the successor at the predecessor's final integration SHA. The underlying ledger handoff still atomically completes the predecessor, activates exactly the next ordinal, creates the successor tickets, and queues idempotent generated-card projections. During this milestone a pre-existing bug was corrected so successor generated-card contracts now use the newly validated successor plan's repository/base/snapshot provenance instead of the original decomposition snapshot. Immutable materialization evidence freezes that re-snapshot hash and exact successor ticket set. If the process dies after the handoff transaction but before scheduler effect persistence, replay recognizes the completed-predecessor/active-successor state and the coordinator returns `already_materialized` without invoking the planner again. `next_tranche_activation` is a separate ledger-only verification phase and is not claimable until every successor card is acknowledged with an external Hermes task ID and every dependent successor ticket has immutable native dependency-graph evidence matching its Local First dependency contract and Hermes parent IDs. Only then is immutable activation evidence written for the exact successor ticket IDs, external task IDs, graph hashes, and snapshot identity. Existing generated-card and native-graph retry semantics handle partial projection without duplicate cards or links.

## 14. Scheduler-wide reconciliation model — Complete

Unify recovery semantics across all scheduler stages without creating a second state machine.

Every stage should be classifiable as one of:

1. not started;
2. started / external outcome unknown;
3. external effect completed / local stage record incomplete;
4. local stage completion recorded / downstream projection incomplete;
5. fully finalized.

Required work:

- Define the authoritative durable evidence for each stage and each state above.
- Reuse existing invocation, outbox, Git, and artifact records rather than inventing parallel truth.
- Make `process-next` choose resume, replay, reconcile, retry, or stop from persisted evidence alone.
- Ensure terminal external failures do not silently become automatic retries.

Completion condition: after process death at any supported boundary, the next scheduler invocation can deterministically decide the safe next action from durable state.

**Current status:** Complete for this scheduler milestone. A shared derived reconciliation model now classifies durable scheduler claims into the five required states and returns one of `resume`, `replay`, `reconcile`, `retry`, or `stop`. The classifier does not persist a parallel recovery state: it reads the existing scheduler claim lifecycle plus the authoritative evidence already owned by each subsystem. Model-backed stages inspect durable `model_invocations`; paid stages inspect purpose/request-key-bound `paid_reservations`; Git integration inspects `git_commit_intents` and append-only commit evidence; native dependency, tranche checkpoint, and next-tranche stages inspect their existing immutable evidence; deterministic/local stages safely replay from their frozen claim identity; and completed claims inspect the existing board/comment outboxes for downstream projection work. A `stop` decision is applied before normal scheduler work and retains the established stage-specific reconciliation errors so ambiguous or terminal external outcomes cannot become automatic retries. Exact-recovery and deterministic stages continue into their existing replay/reconcile handlers. Read-only `preview_database()` now uses the same classifier through a read-only Ledger shell, so dry-run and live execution derive the same recovery decision without migrations or writes.

## 15. Scheduler-wide ordering policy — Complete

Define and test one canonical ordering when multiple classes of work are eligible.

The final order should explicitly cover at least:

- pending generated-card projections;
- pending state projections;
- pending evidence comments;
- incomplete/recoverable lifecycle claims;
- implementation;
- validation;
- review;
- repair routing;
- triage;
- acceptance;
- Git integration;
- completion projection;
- dependency release;
- tranche/checkpoint work;
- paid work;
- next-tranche activation;
- new dependency-readiness admission.

The exact order may be refined as implementation progresses, but it must be deterministic and justified by durability/ownership dependencies.

Completion condition: two schedulers presented with the same durable state choose the same next eligible stage, and pending recovery/external effects are not starved by newly admitted work.

**Current status:** Complete for this scheduler milestone. The scheduler now exports a canonical `SCHEDULER_STAGE_ORDER` and rank mapping covering every required work class. A terminal or ambiguous reconciliation decision remains a fail-closed safety fence before normal work; among eligible work, the order is: generated-card projection, state projection, evidence comment, recoverable expired lifecycle claim, implementation, validation, review, repair routing, triage, acceptance, Git integration, completion, native dependency graph, native dependency release, tranche checkpoint, paid checkpoint, paid escalation, next-tranche materialization, next-tranche activation, then root dependency-readiness admission. The first three positions reflect board ownership dependencies: generated child cards establish external task identity, authoritative state is then projected, and evidence comments follow. The global recovery slot is selected from the Milestone 14 reconciliation model; when present, live execution gates every non-matching lifecycle class for that tick, so newly eligible implementation or later work cannot bypass an incomplete recoverable claim. Only after external projection and recovery work is exhausted does fresh lifecycle admission follow the canonical stage order. Root readiness remains last so new implementation work cannot starve already-owned lifecycle work. Dry-run was reordered to the same policy, and tests prove generated→state→comment precedence, external projection before recovery, recovery before fresh implementation, implementation before fresh validation, explicit rank uniqueness, and identical stage/ticket choice from independent scheduler views of the same durable state.

## 16. Scheduler-wide concurrency proof — Complete

Exercise competing scheduler processes and leases across the complete lifecycle.

Required cases:

- overlapping global ticks;
- expired tick leases;
- duplicate stage claims;
- competing workers for the same ticket;
- competing stages for the same ticket;
- cross-ticket deterministic selection;
- outbox retry overlap;
- model invocation overlap prevention;
- Git commit race prevention;
- paid reservation race prevention.

Completion condition: concurrent scheduler processes cannot produce duplicate stage effects or violate ticket lifecycle ordering.

**Current status:** Complete for this scheduler milestone. No new lock authority was required. The scheduler already combines a restartable global tick lease with stage/outbox leases and Ledger transactions that acquire SQLite's write lock via `BEGIN IMMEDIATE`; external-effect subsystems add their own exact authority boundaries such as model launch records, Git integration-head compare-and-swap, outbox row leases/idempotency keys, and paid request-key reservations. The new scheduler-wide contention suite opens independent Ledger connections against the same database and races real threads, proving: overlapping global ticks execute one model-launch path while the competitor returns `busy`; expired tick takeover has one winner; duplicate same-ticket implementation claims produce one durable owner; an expired implementation recovery claim excludes a competing validation stage for that ticket; deterministic cross-ticket selection still picks the canonical `created_at,id` candidate under overlap; state outbox retries lease one row once; paid authorization with one request key is cross-connection idempotent; and concurrent Git integration-head updates admit one CAS winner and one conflict. Existing comment-outbox/delivery, invocation-lifecycle, paid-governor, and Git suites were run with this proof and remain green. Because the scheduler lease horizon is required to exceed the bounded external-effect horizon, a live provider call is not expected to outlive its owning tick/stage lease; stale/expired recovery is therefore handled through the reconciliation model rather than overlapping a second bounded effect.

## 17. Scheduler-wide crash matrix — Complete

Inject process death at every durable boundary for every scheduler-owned stage.

The matrix must prove no duplicate:

- implementation model call;
- validation evidence;
- review model call;
- repair/triage routing;
- generated child work;
- accepted candidate;
- Git commit;
- board effect;
- dependency/tranche activation;
- paid call/reservation.

Completion condition: every injected crash either resumes safely, replays an already-durable result, or stops with an explicit reconciliation requirement. No boundary may produce an ambiguous automatic duplicate side effect.

**Current status:** Complete for this scheduler milestone. `SCHEDULER_CRASH_POLICIES` now makes the safe started-effect action explicit for every scheduler-owned work class and is checked for exact coverage against the canonical scheduler stage order (excluding only the meta recovery slot). The shared durable-boundary actions are fixed as retry before an effect starts, reconcile when the external/durable effect is complete but scheduler-local completion is not, resume when local completion is recorded but downstream projection remains, and resume after full finalization. Model-backed implementation/review/triage and paid checkpoint/escalation remain fail-closed on a started-but-unknown outcome; deterministic/local stages and exact/idempotent external authorities replay instead. The matrix is paired with existing injected-failure tests covering completed and ambiguous model invocations, validation persistence, review replay, repair/triage restart, child materialization/outbox idempotency, acceptance finalization, Git commit/tranche-ref recovery, completion/outbox restart, native dependency link replay, tranche checkpoint artifact replay, next-tranche materialization replay, paid unknown-outcome no-repeat, and completed paid-call replay. The targeted matrix run passes 106 tests / 21 subtests, and the complete repository suite passes 720 tests / 179 subtests.

## 18. Scheduler observability — Complete

Expose enough durable state for an operator to understand exactly where orchestration is stopped.

Required data should include:

- current/next scheduler stage;
- current claim and ticket;
- lease owner and expiry;
- attempt number where relevant;
- latest durable stage artifact;
- reconciliation-required reason;
- pending board/outbox effects;
- model invocation state;
- validation/review result identity;
- Git commit identity when present;
- paid reservation state when present.

Completion condition: an operator can inspect the system and identify the exact durable lifecycle boundary without reconstructing state from logs.

**Current status:** Complete for this scheduler milestone. The scheduler now exposes a single bounded `scheduler_observability()` read model that composes the same durable sources used for execution/reconciliation rather than introducing a parallel metrics database. It reports the selected current/next stage and ticket, current claim identity/status, lease owner/expiry, claim attempt count, lifecycle attempt number, side-effect start/completion/finalization timestamps, and any expired-claim reconciliation classification/action/reason. It also reports pending generated-card/state/comment effects and bounded active board-effect leases; latest durable runtime-stage artifact identity (path/SHA/base); latest model invocation provider/model/status/artifact/error identity; review result identity; accepted-candidate artifact hashes/evidence hash; Git intent/evidence/commit/integration-head identity; and paid reservation request/status when the selected claim owns one. Claim selection follows `preview_next()` where possible so operator inspection matches the next scheduler decision. The existing lightweight `status` response remains the default; `status --scheduler-detail` adds this deeper snapshot. Focused observability/scheduler tests pass 37 tests, and the full repository suite passes 725 tests / 179 subtests.

## 19. Daemon wrapper — Complete

Build the daemon only after one-shot `process-next` is safe across the full lifecycle.

Required slices:

- repeatedly invoke the one-tick scheduler primitive;
- backoff/sleep when there is no work or transient external failure;
- honor pause state without terminating in-flight durable work incorrectly;
- graceful shutdown;
- health/status reporting;
- restart on process failure without daemon-specific recovery logic.

The daemon should not own lifecycle semantics that are absent from `process-next`.

Completion condition: killing and restarting the daemon is operationally equivalent to stopping and later calling the already-safe one-tick scheduler again.

**Current status:** Complete for this scheduler milestone. `SchedulerDaemon` repeatedly instantiates the exact same registered `ProcessNextScheduler` composition used by `process-next --execute` and invokes one bounded tick per iteration. It adds only operational concerns: idle/busy/paused sleep, bounded exponential error backoff, graceful SIGINT/SIGTERM stop requests, health counters/timestamps, and final health plus scheduler-observability reporting. It does not inspect or mutate lifecycle state independently of the one-tick scheduler. The CLI remains fail-closed and requires registered runtime, `--execute`, `--allow-board-writes`, and explicit Hermes executable/board access; polling/backoff and optional `--max-iterations` are operator controls only. Pause handling remains scheduler-owned, so already-durable projection work can finish while paused and new lifecycle claims remain blocked. Tests prove idle/busy/paused sleep behavior, backoff/reset semantics, graceful stop between ticks, health/status output, parser/permission gates, and restart equivalence across a durable state-projection → evidence-comment boundary using a brand-new Ledger/daemon instance. Focused daemon/CLI/scheduler tests pass 38 tests, and the full repository suite passes 732 tests / 179 subtests.

## 20. Real end-to-end acceptance — Complete

Prove the finished scheduler against real configured integrations.

Required proof:

- actual Hermes board/task/dependency behavior;
- actual configured local implementation model;
- actual independent review model;
- real isolated Git worktree and commit path;
- deterministic validation;
- crash/restart at representative high-risk boundaries;
- completion of a representative low-risk feature;
- no unnecessary paid post-architecture calls;
- auditable evidence for every durable stage.

Completion condition: one representative low-risk feature proceeds from eligible Hermes work through implementation, validation, review, accepted commit, completion projection, and dependency advancement under the v2 ownership model, including restart proof, without hidden manual lifecycle substitutions.

**Current status:** Complete for the representative low-risk scheduler acceptance scope. A dedicated real Hermes task (`t_cc72b17f`) on board `lfo-m20-acceptance` was imported through the supported CLI and driven by the production daemon against an isolated Git repository and Local First ledger. The configured local implementation model completed one bounded worktree edit; deterministic validation passed; an independent review profile/process produced one schema-valid `pass` verdict; acceptance froze candidate evidence; Git produced commit `a90f1515801ee75314a829d0fc79d00a43fc3703`; Local First and Hermes both reached `done`; all workflow state/comment outboxes drained; and the completed commit independently re-passed the acceptance validation commands. A real crash was injected after Hermes accepted an evidence comment but before local acknowledgment; restart reconciled the existing marker with exactly one remote comment. A separate real Hermes dependency probe verified native parent linkage and child readiness advancement after the parent reached `done`. The low-risk path created zero paid reservations/calls. The live exercise also drove production fixes for separate review `HERMES_HOME`, current Hermes comment-marker reads, idempotent already-scheduled projections, and the supported `scheduled -> unblock -> complete` terminal transition. See `docs/milestone-20-real-acceptance.md` for exact IDs, hashes, commands, restart evidence, and remaining broader design boundaries.

## Scheduler milestone sequence

The intended implementation sequence is:

`foundation → implementation → validation → review → repair routing → triage → acceptance → Git integration → completion projection → dependency release → tranche/checkpoint → paid stages → next-tranche activation → reconciliation hardening → deterministic ordering → concurrency proof → crash matrix → observability → daemon → real E2E acceptance`

All twenty dependency-ordered scheduler milestones are complete for the representative v2 Local First scheduler acceptance scope. See `docs/milestone-20-real-acceptance.md` for the live integration evidence and the broader design items that remain intentionally separate.

## Post-scheduler roadmap status and next full-plan work

The twenty dependency-ordered scheduler milestones are complete for the representative Local First-owned low-risk path. Milestone 20 proved the scheduler/daemon against a real Hermes board, real configured local implementation model, independent review profile/model process, isolated Git worktree/commit flow, deterministic validation, live dependency semantics, and a real ambiguous comment-delivery crash/restart boundary. This document therefore no longer has a scheduler milestone 21.

The next work comes from the broader v2 design, in this order:

1. **Hermes execution reconciliation — Complete for representative dispatcher-owned execution.** Automatic generated-ticket ownership, dispatcher-run polling, blocked handoff/finality control, replay-safe run → attempt reconciliation, source-aware validation/review/Git/checkpoint handling, same-ticket Hermes-owned repair polling, and a real dispatcher-owned worker proof are complete. Detailed evidence is in `docs/hermes-dispatch-execution-acceptance.md`.
2. **Full scheduler-generated native tranche graph proof — next.** Exercise a real multi-ticket active tranche created/projected by Local First, verify exact native dependency equivalence and readiness release, reconcile dispatcher-owned execution for each ticket, integrate the tranche, checkpoint it, and activate the successor tranche.
3. **Exhaustive live external-boundary crash proof.** Extend the scheduler-wide crash matrix from deterministic/fake-runtime proof plus the Milestone 20 live comment crash to real model, review, Git, board, dependency, checkpoint, and paid boundaries.
4. **Live paid checkpoint/escalation integration.** Demonstrate reservation-before-call, budget pause/approval, completed-call replay, escalation chaining, and unknown-outcome no-repeat against a real paid provider.
5. **Operator lifecycle/recovery UX.** Consolidate initialization, inspect/status, pause/resume, retry/reject/reconcile, paid approval, daemon lifecycle, and diagnostics into a coherent documented operator surface.
6. **Runtime metrics and adaptive sizing.** Persist real-run performance/outcome measurements and use them to adjust ticket/context sizing within explicit policy bounds.

The authoritative implementation-status detail for these broader items remains `docs/design-v2.md`; the live scheduler acceptance evidence is `docs/milestone-20-real-acceptance.md`.
