# Hermes Kanban Local-First Orchestrator
## Version 2.0 — Hermes-Native Execution, Durable Trust, and Publishable Plugin Plan

**Status:** Current design authority  
**Date:** 2026-09-08  
**Supersedes:** `hermes-kanban-local-first-orchestrator-design-v1.2-sequencing-correction.md` and earlier design amendments where this document differs  
**Historical note:** the uploaded v1.2 sequencing-correction file internally identifies itself as “Version 1.1 — Microticket Generation Expansion.” This document treats that file as the prior design authority regardless of the embedded version-label mismatch.  
**Primary local implementation worker today:** Qwen 3.8 27B IQ3_S via Hermes profile routing  
**Target implementation-ticket size:** Luna-class worker sized  
**Higher-capability roles:** architecture/decomposition, high-risk review, tranche/final review, escalation

---

# 0. Design authority and reason for this revision

The original Local-First Orchestrator design was directionally correct:

- conceptual features should be decomposed before implementation;
- decomposition should be repository-grounded;
- executable leaves should be narrow and bounded;
- SQLite and retained artifacts should be the durable trust authority;
- model proposals should be validated deterministically;
- implementation should be reviewed independently;
- repairs should remain on the same logical ticket rather than recursively spawning defect trees;
- future tranche detail should remain bounded and be revalidated against the evolving repository.

Implementation has now proven enough of the system to correct several assumptions that were still abstract in the earlier design.

The most important change is the execution boundary:

```text
OLD MODEL

Local First
  plans
  schedules
  creates worktree
  invokes implementation model
  validates
  reviews
  integrates

Hermes Kanban
  mostly projects state / provides operator UI
```

The corrected architecture is:

```text
CURRENT MODEL

Local First
  owns contracts
  owns repository provenance
  owns the canonical dependency DAG
  owns validation/review/acceptance policy
  owns immutable evidence
  owns integration/completion authority
        ↓ projects executable graph
Hermes Kanban
  owns native dependency scheduling
  owns default profile assignment
  owns claims/workspaces/worker spawning
  owns implementation execution
        ↓ reconciled back
Local First
  validates
  reviews
  repairs/rereviews
  accepts
  integrates
  establishes completion authority
```

The guiding rule is:

> **Local First decides what work exists, what dependencies are real, and what constitutes trusted completion. Hermes decides when an eligible projected task runs and how its worker is launched.**

This revision also incorporates concrete lessons from C09.10 recovery, C11 contract admission, real decomposition attempts, planner safety failures, activation replay defects, runtime-binding hardening, and the first real C11 Hermes-dispatched implementation.

---

# A. Summary of substantive changes from the prior design

## Preserved

The following remain core design principles:

- `Feature → Tranche → Microticket → Attempt → Finding/Repair`;
- feature-level acceptance criteria remain authoritative;
- repository evidence is deterministic and bounded;
- decomposition models propose; deterministic code authorizes;
- only the active bounded tranche receives symbol-level implementation detail;
- local implementation leaves are narrowly scoped;
- review is fresh and independent;
- repair is bounded and same-ticket by default;
- accepted history is immutable;
- corrections are append-only;
- future tranches are re-snapshotted/revalidated;
- board comments are not authoritative evidence;
- SQLite + durable artifacts are the trust/evidence authority.

## Strengthened

Implementation has materially strengthened:

- feature admission;
- contract identity;
- predecessor completion binding;
- repository path disposition validation;
- plan-only persistence;
- activation idempotency;
- atomic planning-run finalization;
- planner tool isolation;
- planner repository-mutation tripwires;
- strict recursive output schema validation;
- planner request/response/provenance retention;
- runtime-binding replay/conflict handling;
- historical recovery and supplemental-correction authority;
- integration and recheck evidence.

## Corrected

The following earlier assumptions are superseded:

1. **Hermes is not merely a passive board projection.**  
   It is also the execution/scheduling substrate.

2. **Local First should not ordinarily duplicate Hermes worker dispatch.**  
   Hermes should assign profiles, claim tasks, create workspaces, and spawn workers.

3. **Generated Local First dependencies must be projected as native Hermes task links.**  
   Persisting dependencies only inside Local First contract text is insufficient.

4. **Dependency waiting is not represented by `blocked`.**  
   In Hermes, unfinished native parents keep a child in `todo`; when parents are `done`, Hermes can promote it to `ready`. `blocked` is for exceptional waits such as human/capability/transient holds.

5. **Blank worker profiles before dispatch are normal.**  
   The observed root C11 task was created unassigned and received `worker-code-local` lazily from `kanban.default_assignee` during dispatch.

6. **Hermes execution must be reconciled back into Local First.**  
   A board task becoming `done` is not, by itself, Local First accepted evidence.

7. **The old tranche integration sequencing is unsafe with native parent gating.**  
   Waiting to integrate until every tranche ticket is accepted can deadlock a dependency chain. Accepted predecessor tickets must advance the tranche integration lineage before their final completion releases dependent work.

8. **Planning and activation are separate authority transitions.**  
   A validated plan should be persistable without immediately materializing executable tickets.

---

# 1. Product goal

Build a Hermes-native orchestrator capable of receiving a complex repository-backed feature and autonomously progressing through:

```text
conceptual feature
→ immutable admission
→ repository snapshot
→ architecture/decomposition
→ strict plan validation
→ plan-only persistence
→ explicit activation
→ native Hermes dependency graph
→ Hermes implementation execution
→ Local First execution reconciliation
→ deterministic validation
→ fresh review
→ bounded repair
→ revalidation
→ rereview
→ acceptance
→ integration
→ dependent release
→ tranche verification/completion
→ next-tranche re-snapshot/materialization
→ feature completion
```

The orchestrator should minimize expensive frontier-model usage without making model capability assumptions part of permanent workflow structure.

A good implementation ticket should be small enough for a Luna-class workhorse even if the current configured local implementation worker is Qwen.

---

# 2. Non-goals

The orchestrator is not intended to:

- replace Hermes Kanban's dispatcher;
- build a second independent task scheduler;
- let a decomposition model choose worker profiles;
- use board comments as the evidence database;
- make mutable canonical checkouts the source of repository authority;
- permit model-generated arbitrary shell commands;
- trust a model's self-reported implementation success;
- create unbounded recursive defect-card trees;
- automatically rewrite accepted history;
- silently broaden feature scope;
- assume a large future ticket graph remains valid after repository changes;
- hard-code the author's private repository paths, model names, board slug, or machine configuration into a publishable plugin.

---

# 3. Authority model

## 3.1 Local First authority

Local First is authoritative for:

- feature identity;
- immutable feature contract;
- contract hash;
- admission provenance/hash;
- tranche identity;
- repository registration;
- authoritative repository/base/integration SHA;
- immutable repository evidence snapshots;
- architecture/decomposition decisions;
- canonical dependency DAG;
- acceptance-criterion mapping;
- path dispositions (`modify` / `create`);
- patch budgets;
- deterministic verification policy;
- risk policy;
- execution eligibility policy;
- runtime/attempt provenance;
- deterministic validation evidence;
- review artifacts and findings;
- repair generation;
- accepted evidence;
- accepted commit;
- tranche integration lineage;
- supplemental corrections;
- H1/H2 completion authority;
- feature/tranche completion.

SQLite and retained artifacts are the durable source of truth for these facts.

## 3.2 Hermes authority

Hermes is authoritative for its own operational execution state:

- board/task identity;
- native parent/task links;
- `todo` / `ready` scheduling;
- profile assignment;
- task claim;
- worker process/run identity;
- workspace path;
- worker lifecycle;
- native review/change state where used;
- dispatcher events.

Local First must ingest and bind those facts rather than invent them.

## 3.3 Projection rule

Hermes is the **execution projection** of Local First's authoritative graph.

A projected task can contain duplicated human-readable contract information, but that duplication does not supersede Local First ledger state.

## 3.4 Trust rule

A Hermes state transition such as:

```text
running → done
```

is operational evidence, not automatically Local First acceptance authority.

Local First completion requires its configured validation/review/integration policy to have succeeded.

---

# 4. Core hierarchy

```text
Feature
  ↓
Tranche
  ↓
Microticket
  ↓
Attempt / Hermes execution binding
  ↓
Validation stage
  ↓
Review generation
  ↓
Finding / repair generation
  ↓
Accepted candidate
  ↓
Integration lineage
```

A **Feature** owns product intent and acceptance criteria.

A **Tranche** is a bounded architectural/capability slice materialized against a specific repository state.

A **Microticket** is a narrow implementation unit.

An **Attempt** is a durable Local First representation of a concrete execution, ordinarily bound to a Hermes task/run/workspace.

A **Finding/Repair** remains associated with the same logical microticket unless a higher-level correction is required.

---

# 5. Conceptual feature intake and admission

A conceptual feature is never directly executable.

Before decomposition it must be admitted through a bounded deterministic operation.

## 5.1 Feature contract

A canonical feature contract contains at least:

```yaml
id: FEATURE-12
title: Complete the remaining check-in flow
objective: >
  Allow a user to complete check-in through the existing application flow
  and observe the resulting persisted state.
acceptance_criteria:
  - id: AC-12-1
    statement: A valid user can initiate check-in from the existing UI.
    verification_hint: integration test or fixture-backed UI/service test
non_goals:
  - Redesign unrelated account screens.
invariants:
  - Existing authentication behavior remains unchanged.
constraints:
  - Preserve current public API behavior unless explicitly changed.
source_revision: <source revision or content hash>
```

## 5.2 Admission persists immutable authority

Admission atomically establishes:

```text
feature
initial tranche
FeatureContract
repository provenance
repository snapshot identity
predecessor completion binding
admission provenance
```

Admission is idempotent for exact replay and fails closed on conflicting immutable identity.

## 5.3 Contract hash versus admission hash

These identities are intentionally distinct:

```text
FeatureContract.contract_hash
=
canonical functional contract identity
```

```text
admission_hash
=
admission/provenance envelope identity
```

The plan activation pipeline consumes the canonical `FeatureContract.contract_hash`.

Admission metadata must not redefine that hash.

## 5.4 Predecessor authority

A feature/tranche may depend on prior completion evidence.

The admission layer supports at least:

```text
kind = h1
kind = recheck
```

and must not assume that a recheck row exists.

Predecessor completion is derived from durable Local First evidence, including generation and final integration SHA where applicable.

## 5.5 Allowed-path dispositions

An admitted contract distinguishes:

```text
modify:
  path must exist as a Git blob at authoritative base

create:
  path must be absent at authoritative base
```

Absolute paths, traversal, conflicting dispositions, duplicate paths, non-blob modify targets, and already-existing create targets fail closed.

---

# 6. Repository authority and evidence snapshot

The current working checkout is not repository authority.

## 6.1 Authoritative base

Every feature/tranche is bound to:

```text
canonical repository identity
authoritative base/integration SHA
feature contract hash
repository snapshot hash
```

Dirty files in a user's canonical checkout are not silently incorporated into planning or implementation authority.

## 6.2 Evidence snapshot

The deterministic mapper may use:

- `git ls-files`;
- directory structure;
- source/test roots;
- symbol extraction;
- term/reference searches;
- package/module manifests;
- existing repository instruction files;
- bounded source excerpts;
- known test companions.

Every included evidence item records provenance.

## 6.3 Evidence is bounded

The planner receives compact, targeted evidence rather than a repository dump.

Large files should be represented at symbol/excerpt level where possible.

## 6.4 Snapshot staleness

A plan bound to SHA A cannot be activated after the authoritative integration base has moved to SHA B unless it is revalidated/rematerialized according to policy.

---

# 7. Decomposition model

Decomposition remains conceptually two-stage:

```text
Stage A
feature → architecture/capability/tranche sequence

Stage B
active tranche → executable repository-grounded microtickets
```

A single model invocation may emit both, but the representation must preserve the distinction.

## 7.1 Detail horizon

Default behavior:

- create feature-level architecture decisions;
- define a coarse tranche sequence;
- fully materialize only the current active tranche;
- keep future tranches coarse;
- bound active microtickets;
- re-snapshot before future tranche materialization.

This prevents early implementation from invalidating a large queue of detailed tickets.

## 7.2 Ticket sizing

Implementation tickets should normally:

- own one architectural concern;
- target one primary symbol/boundary;
- touch roughly 1–3 meaningful production/test files;
- have an explicit allowlist;
- have deterministic verification;
- have bounded changed lines;
- contain no unresolved product/architecture choice;
- fit comfortably inside one Luna-class work session without compression where practical.

Do not force artificial procedural decomposition such as separate pseudocode/code/test tickets unless context or failure data justifies it.

---

# 8. Planner safety boundary

The planner is not an implementation worker.

## 8.1 Safe capability mode

Repository-backed planning runs with:

```text
safe/no-mutation tool capability
artifact-local scratch working directory
canonical repository passed explicitly as protected input
```

No terminal/file/Git/Kanban mutation surface should be available.

Prompt instructions are not considered sufficient isolation.

## 8.2 Protected repository tripwire

Before every planner invocation:

```text
protected-before.json
```

records at least:

- HEAD;
- porcelain status;
- hashes of dirty paths.

After every terminal planner outcome, including:

- valid response;
- malformed JSON;
- schema rejection;
- timeout;
- process failure;
- provider failure;

the planner records:

```text
protected-after.json
```

If the protected fingerprint changed:

```text
fail closed
no plan persistence
retain incident evidence
do not auto-clean
```

## 8.3 Retained planning artifacts

Persist:

```text
planner-request.json
planner-response.json
planner-provenance.json
protected-before.json
protected-after.json
normalized/validation artifact where applicable
```

## 8.4 Actual invocation provenance

Record the resolved:

```text
provider
model
profile
cost class
invocation mechanism
tool capability mode
planner working directory
```

Do not infer the model afterward from mutable global configuration.

---

# 9. Planner output contract and strict parsing

A model cannot be expected to infer an undocumented Python/internal type.

## 9.1 One canonical output-schema authority

A single schema definition should drive:

```text
model-facing output contract
deterministic parser required/optional keys
generated valid example
tests/fixtures where practical
```

Avoid independent hand-maintained prompt/parser/fixture field lists.

## 9.2 Strict closed schema

The current supported root requires:

```text
plan_version
feature_id
feature_contract_hash
repo_base_sha
repo_snapshot_hash
architecture_decisions
criterion_coverage
tranches
```

Supported optional root fields remain explicitly enumerated.

Tranche, microticket, `patch_budget`, and `verification` keys are likewise closed.

Unknown fields reject recursively.

## 9.3 Dynamic criterion coverage exception

`criterion_coverage` is the intentional dynamic-key mapping:

```text
criterion ID → array of ticket IDs
```

Its values remain strictly typed.

## 9.4 Input names versus output names

Where packet context uses names such as:

```text
feature.contract_hash
repository.base_sha
```

while output requires:

```text
feature_contract_hash
repo_base_sha
```

the model-facing contract must state the mapping explicitly.

## 9.5 JSON-only is not enough

The planner request must provide the exact schema, exact authorized paths/dispositions, and a generated/validated minimal example.

Do not merely say:

```text
return one DecompositionPlan JSON object
```

without defining `DecompositionPlan`.

## 9.6 Strict parser remains authority

Provider-side structured output may be added as defense in depth.

It never replaces deterministic parsing/validation.

---

# 10. Planner routing policy

A decomposition invocation should not accidentally inherit an arbitrary default worker merely because a CLI lane is named `local`.

Observed during C11:

```text
--planner-cost-class local
→ Hermes default identity
→ gpt-5.6-luna
```

The architecture should eventually expose an explicit decomposition/architecture route.

Conceptually:

```text
architecture/decomposition
→ configurable stronger profile/model

ordinary implementation
→ Hermes implementation profile/default

fresh low-risk review
→ configurable independent reviewer

high-risk/tranche/final review
→ configurable stronger reviewer
```

Model/provider/profile selection is operator/controller policy, never planner-output authority.

---

# 11. Deterministic plan validation

Before persistence/activation, validate at least:

1. schema;
2. feature identity;
3. feature contract hash;
4. repository base/snapshot hash;
5. criterion identities;
6. complete/deferred criterion coverage;
7. no scope expansion;
8. Qwen/Luna-ready ticket contract;
9. authorized files/dispositions;
10. symbol/path resolution;
11. dependency reference validity;
12. dependency DAG acyclicity;
13. no inactive-future prerequisite for active tranche;
14. patch-budget limits;
15. verification allowlist;
16. risk-policy rules;
17. unresolved choices;
18. ownership/overlap rules;
19. valid green intermediate sequencing;
20. active-ticket limit;
21. duplicate fingerprints;
22. no arbitrary model-selected board/shell mutation;
23. activation idempotency.

A rejected plan is durable evidence and creates no partial executable graph.

---

# 12. Plan-only persistence and activation

Planning and activation are separate authority transitions.

## 12.1 Plan-only generation

```text
admitted FeatureContract
→ repository snapshot
→ planner
→ strict parse
→ deterministic plan validation
→ persist plan as validated/pending activation
```

At this boundary:

```text
materialized tickets = 0
attempts = 0
Hermes child cards = 0
```

## 12.2 Activation

Later:

```text
exact persisted validated plan
→ materialize durable tickets/dependencies
→ promote plan active
→ finalize matching planning run
→ enqueue/project board graph
```

Activation does not invoke the planner again.

## 12.3 Atomic activation semantics

The durable activation transaction must prevent contradictions such as:

```text
tickets materialized
plan active
planning run still pending
```

Canonical runtime ticket ordering is ledger-derived and used consistently for:

- first activation;
- replay;
- planning-run evidence.

A failure during finalization rolls the whole activation transaction back.

---

# 13. Native Hermes dependency projection

This is a major correction to the earlier design.

Persisting the dependency DAG only inside Local First is insufficient.

## 13.1 Canonical graph

Local First owns the authoritative DAG.

Example:

```text
TK-1
  ↓
TK-2
  ↓
TK-3

TK-3 may additionally depend directly on TK-1
```

## 13.2 Hermes graph

The same true dependency edges must be projected as native Hermes parent/task links.

Do not convert mere preferred ordering into dependencies.

## 13.3 Hermes dependency semantics

For native parent links:

```text
unfinished parent(s)
→ child remains todo

all parents done
→ child may become ready

ready
→ dispatcher may assign/claim/run
```

Ordinary dependency waiting is not represented as `blocked`.

## 13.4 Safe projection sequence

To avoid children becoming runnable before graph construction completes:

```text
1. create all active-tranche cards non-runnable
2. persist/resolve all Local First ↔ Hermes task identities
3. create exact native parent links
4. verify graph equivalence
5. release/unblock cards
6. allow Hermes to compute root ready vs dependent todo
```

The exact non-runnable staging mechanism should use supported Hermes semantics.

## 13.5 Projection replay

Exact link replay is idempotent.

Conflicting/missing graph projection fails closed.

Projection failure:

- does not rerun decomposition;
- does not duplicate Local First tickets;
- does not repeat model work;
- remains recoverable from the outbox/reconciliation path.

---

# 14. Hermes scheduling and worker profiles

Hermes remains the execution scheduler.

## 14.1 Expected normal flow

Observed C11 root behavior:

```text
card created:
assignee = null

card becomes dispatchable
→ Hermes dispatcher
→ kanban.default_assignee
→ worker-code-local
→ claim
→ workspace
→ worker process
```

This behavior is desirable.

## 14.2 Blank profile is not a defect

Dependent tickets may remain unassigned until they become dispatchable.

Do not require planner output or plan persistence to contain a worker profile merely to match today's board default.

## 14.3 Routing policy

If Local First later needs selective routing, routing is deterministic controller/operator configuration.

Possible policy dimensions:

- risk;
- language;
- repository;
- context size;
- ticket class;
- tranche;
- architecture/implementation/review purpose.

The decomposition model has no authority over these selections.

---

# 15. Hermes execution → Local First reconciliation

This is the primary missing runtime seam exposed by C11.

## 15.1 Problem demonstrated

A Hermes task may have:

```text
status = done
assigned profile = worker-code-local
workspace exists
worker execution completed
```

while Local First still shows:

```text
ticket = draft
runtime binding = absent
attempts = 0
```

That means execution happened operationally but was never bound into the durable trust lifecycle.

## 15.2 Required reconciliation

Local First must reconcile at least:

```text
Hermes task identity
Hermes run/delegation identity
assigned profile
workspace identity/path
starting/base SHA
canonical/integration SHA
execution start/end
worker result status
candidate/diff identity
```

## 15.3 Runtime binding semantics

A ticket-specific runtime binding is immutable.

```text
exact replay
→ return same row
→ preserve created_at

immutable mismatch
→ fail closed
```

Uniqueness races reload and verify the winning row.

## 15.4 Attempt semantics

A Local First attempt should ordinarily represent:

> the durable trust/evidence envelope around a concrete Hermes execution of a Local First microticket.

It should not imply that Local First itself spawned the model.

## 15.5 Restartability

Reconciliation must survive:

- Local First restart;
- Hermes worker completion while Local First is down;
- duplicate event/read processing;
- partial durable-stage application;
- projection/API read failure.

Existing persisted-stage/reconciliation patterns should be reused.

---

# 16. Deterministic validation

Worker completion is never sufficient.

A candidate must be frozen/bound and deterministically checked.

Validation includes:

```text
exact ticket/attempt identity
allowed path/disposition check
diff hash
patch budget
repository/worktree provenance
configured verification commands
command working directory
timeout/output limits
```

Model-selected arbitrary commands are forbidden.

Validation artifacts are durable and hash-bound.

---

# 17. Review, repair, and rereview

The review philosophy from the earlier design remains correct.

## 17.1 Fresh review

Review must be independent of implementation context.

The reviewer receives a bounded packet containing:

- contract criteria;
- frozen candidate;
- exact diff;
- deterministic verification evidence;
- selected repository context;
- explicit review schema.

## 17.2 Same-ticket review cycle

Target:

```text
implementation
→ deterministic validation
→ fresh review
    ├── PASS
    │     ↓
    │   accept
    │
    └── FINDINGS
          ↓
       request changes / repair generation
          ↓
       implementation worker repairs same ticket
          ↓
       validation
          ↓
       fresh rereview
```

Do not create recursive reviewer defect cards for ordinary implementation defects.

## 17.3 Budgets

Hard-cap:

- implementation attempts;
- review generations;
- repair generations;
- total model usage according to policy.

On exhaustion:

```text
block / checkpoint / escalate
```

No infinite loops.

## 17.4 Hermes same-card lifecycle

Where Hermes provides native request-review/request-changes semantics, use them rather than inventing parallel cards.

However, deterministic Local First evidence remains authoritative.

---

# 18. Completion semantics and native dependency release

A critical invariant is:

> **A Local First-generated parent task must not become final Hermes `done` merely because the implementation worker stopped.**

If `done` releases native dependents, it must represent completion strong enough to make downstream execution safe.

Target meaning:

```text
implementation finished
→ validate
→ review
→ repair/rereview if needed
→ accepted
→ integrated into authoritative tranche lineage
→ final Hermes done
→ native children become eligible
```

The exact enforcement hook still needs implementation/verification.

Prompt convention alone is weaker than deterministic lifecycle control.

---

# 19. Integration sequencing correction

This supersedes the prior rule:

> “After all active-tranche microtickets are accepted, merge them into the tranche integration branch.”

That ordering can deadlock when Hermes native parent links gate child execution.

## 19.1 Why the old sequence deadlocks

For:

```text
TK-1 → TK-2 → TK-3
```

if TK-1 cannot integrate until TK-2/TK-3 are accepted, but TK-2 cannot run until TK-1 is truly complete, progress stops.

## 19.2 Correct rolling integration sequence

For each dependency predecessor:

```text
TK-1 implementation
→ validation
→ review
→ accepted
→ integrate TK-1 onto tranche integration lineage
→ final TK-1 done
→ Hermes releases TK-2

TK-2
→ same lifecycle
→ advance integration lineage
→ final done
→ Hermes releases TK-3
```

## 19.3 Dependency satisfaction

The stronger Local First condition remains:

```text
accepted evidence exists
AND
accepted commit exists
AND
commit is in authoritative tranche integration lineage
```

Hermes `done` should be arranged to converge with that condition for Local First-generated implementation tasks.

## 19.4 Tranche completion after rolling integration

After all tranche tickets are individually accepted and integrated:

1. run tranche-level integration verification;
2. evaluate tranche criterion evidence;
3. run configured tranche-level review/checkpoint if required;
4. record final tranche integration SHA;
5. issue completion authority.

---

# 20. Future tranche revalidation

Before later tranche materialization:

```text
current integration SHA
→ fresh repository snapshot
→ architecture decision consistency check
→ remaining criterion mapping
→ materialize bounded next tranche
→ deterministic validation
→ plan persistence/activation
```

A new higher-capability architecture call is required only when deterministic revalidation cannot safely preserve prior decisions or risk policy requires escalation.

Future symbol-level leaves should not be pre-created far ahead of repository evolution.

---

# 21. Tranche and feature completion authority

Completion is durable evidence, not a board convention.

## 21.1 H1

Normal tranche completion produces canonical completion authority:

```text
H1
```

bound to the integration lineage/evidence.

## 21.2 Supplemental correction and H2

If a defect is found after accepted completion:

```text
H1
→ supplemental correction plan
→ correction microticket
→ implementation/review
→ integration
→ H2 recheck
```

Accepted history is not rewritten.

H2 derives from durable ledger/integration state rather than caller-provided claims.

## 21.3 Successor authorization

Successor features/tranches may bind to either the valid H1 completion or the later recheck generation as required by current authority.

---

# 22. Historical recovery

The R2/historical recovery machinery exists because earlier accepted work predated some current invariants.

It is an exceptional compatibility path, not the intended normal lifecycle.

Recovery rules include:

- append-only historical authorization;
- canonical authorization hash;
- preserved implementation diff identity;
- implementation attestation;
- worktree/branch/HEAD verification;
- fresh revalidation/review as required;
- fail-closed conflicts;
- no rewriting of prior accepted evidence.

New tickets should enter the modern normal lifecycle directly.

---

# 23. Pause semantics

Pause must be interpreted at the Local First control boundary.

```text
Local First paused
→ no autonomous Local First planning/activation/release/progression
```

Explicit bounded operator actions may still be permitted while the pause bit remains set.

Important:

> Pausing Local First does not automatically stop an already-`ready` Hermes card from being dispatched by Hermes.

Therefore projection/release operations must honor pause before exposing newly executable work.

---

# 24. Board/evidence boundary

Use Hermes for:

- task graph representation;
- operational state;
- dispatch;
- worker assignment;
- workspaces/runs;
- review workflow;
- operator visibility.

Use Local First for:

- immutable contracts;
- hashes;
- repository provenance;
- attempts;
- validation;
- review evidence;
- acceptance;
- integration;
- completion authority.

Comments may be useful for humans but are not required for correctness or recovery.

---

# 25. Implementation lessons proven so far

The following behaviors have been implemented/proven sufficiently to be treated as design constraints.

## 25.1 Feature admission

Implemented and hardened:

- bounded feature admission;
- exact replay;
- conflict failure;
- repository provenance;
- predecessor binding;
- H1-only and recheck predecessors;
- `modify` Git-blob verification;
- `create` absence verification;
- canonical feature contract hash;
- separate admission hash.

## 25.2 Plan-only persistence

Implemented:

```text
planner generation/validation
→ persisted pending plan
→ later explicit activation
```

without immediate ticket materialization.

## 25.3 Planning-run activation finalization

Activation now atomically:

```text
materializes tickets
promotes plan active
derives canonical ticket IDs from ledger
finalizes matching planning run
commits
```

Replay/order mismatches and partial finalization were explicitly hardened.

## 25.4 Planner incident and safety hardening

A real C11 planning attempt using an ordinary Hermes chat path produced implementation-like output and strongly correlated canonical-checkout changes.

The resulting hardening established:

- safe no-mutation toolset;
- scratch CWD;
- mandatory protected repository binding;
- before/after fingerprint;
- raw request/response/provenance;
- fail closed on mutation;
- no auto-clean;
- incident evidence preservation.

This incident should remain part of the planner threat model.

## 25.5 Strict planner schema

Unknown fields reject recursively at:

- root;
- tranche;
- microticket;
- patch budget;
- verification.

Dynamic criterion coverage remains intentionally supported.

The previous alternate C11 response shape proved that exact schema instructions must be generated into the planner request.

## 25.6 Runtime binding

Ticket runtime binding now supports:

```text
exact immutable replay
→ idempotent success

repository/base/canonical/ownership conflict
→ fail closed
```

with uniqueness-race recovery.

## 25.7 C09.10 recovery/completion

C09.10 demonstrated:

- accepted implementation;
- supplemental correction;
- fresh review;
- integration;
- H1/H2/recheck authority;
- cleanup/recovery provenance;
- append-only correction semantics.

Those mechanisms should be reused, not duplicated, for future exceptional recovery.

---

# 26. Current C11 case study

C11 is the first feature materially exercising the new decomposition path.

## 26.1 Durable Local First graph

The materialized active tranche contains:

```text
TK-1
TK-2 depends on TK-1
TK-3 depends on TK-1 and TK-2
```

## 26.2 Projection defect discovered

The Hermes cards were created without native parent links:

```text
TK-1 parents = []
TK-2 parents = []
TK-3 parents = []
```

Therefore Hermes had no native awareness of the Local First dependency DAG.

## 26.3 Profile behavior clarified

TK-1 was created unassigned.

During normal Hermes dispatch:

```text
kanban.default_assignee
→ worker-code-local
```

was applied.

TK-2/TK-3 remaining unassigned before dispatch is therefore expected behavior, not a materialization bug.

## 26.4 Reconciliation gap demonstrated

Observed:

```text
Hermes TK-1:
done
worker-code-local
workspace allocated
```

while the inspected Local First state showed:

```text
TK-1:
draft
no runtime binding
no attempts
```

This demonstrates the missing Hermes-execution-to-Local-First reconciliation seam.

## 26.5 Immediate architectural conclusion

Do not replace Hermes dispatch.

Fix:

1. native dependency projection;
2. execution reconciliation;
3. implementation→validation→review handoff;
4. final `done` semantics;
5. rolling integration.

---

# 27. Revised implementation roadmap from current state

## Milestone 1 — Native DAG projection

Implement:

```text
validated Local First DAG
→ projected Hermes native parent links
```

Requirements:

- race-safe card creation;
- exact Local First↔Hermes task identity;
- idempotent link replay;
- graph equivalence verification;
- no duplicate edges;
- no cycle introduction;
- no unintentionally runnable dependent child;
- board projection failure recoverable without repeating planning.

Exit condition:

A 3-ticket fixture with `A → B → C` projects as native links such that Hermes naturally places A in `ready`, B/C in dependency-waiting `todo`, and later promotes each only after native parent completion.

## Milestone 2 — Hermes execution reconciliation

Bind a dispatcher-owned execution into Local First:

```text
Hermes task
→ assignment/run/workspace
→ Local First runtime binding
→ attempt
```

Requirements:

- restartable;
- exact replay;
- immutable conflicts fail;
- no duplicate attempt/model invocation;
- worktree/workspace provenance captured;
- stage artifacts persisted.

Exit condition:

A Hermes-run root microticket becomes one durable Local First attempt without Local First spawning a second worker.

## Milestone 3 — Post-implementation deterministic validation

When Hermes worker implementation ends:

```text
candidate
→ exact diff
→ scope/budget validation
→ configured commands
→ durable validation stage
```

Exit condition:

Implementation worker completion alone cannot finalize the task.

## Milestone 4 — Fresh review handoff

Route validated implementation into an independent review.

Requirements:

- fresh reviewer context;
- frozen candidate;
- strict review schema;
- durable review artifact;
- idempotent persisted-review application;
- review invocation concurrency guard.

Exit condition:

A validated candidate enters review exactly once and cannot release dependents before pass.

## Milestone 5 — Same-card repair/rereview

Use bounded repair:

```text
review finding
→ same logical ticket
→ implementation repair
→ validation
→ fresh rereview
```

Exit condition:

At least one fixture requires a repair and reaches pass without creating recursive defect cards.

## Milestone 6 — Rolling acceptance/integration + native child release

For each predecessor:

```text
review pass
→ accept
→ integrate
→ final Hermes done
→ native dependent becomes ready
```

Exit condition:

A multi-ticket dependency chain runs end-to-end without deadlock and without manually advancing child cards.

## Milestone 7 — Tranche completion

After all tickets integrated:

```text
tranche verification
→ criterion evidence
→ tranche review/checkpoint where required
→ H1
```

Exit condition:

One complete C11-like tranche reaches durable completion automatically.

## Milestone 8 — Next-tranche/feature progression

Re-snapshot and activate the next tranche or finalize the feature.

Exit condition:

A multi-tranche fixture completes with bounded detail horizon and no stale leaf reuse.

---

# 28. Full-system acceptance criteria

The orchestrator is considered operationally useful when all of the following hold.

1. A conceptual feature is never sent directly to an implementation worker.
2. Admission is immutable and replay-safe.
3. Feature acceptance criteria cannot be broadened by a planner.
4. Planning is repository-grounded and snapshot-bound.
5. Planner invocation cannot mutate the protected repository through available tools.
6. Any detected planner repository mutation fails closed and preserves evidence.
7. Planner output is validated against one canonical closed schema.
8. Invalid plans create no partial executable graph.
9. Plan persistence and activation are separate restartable stages.
10. Activation is idempotent.
11. Active-tranche dependencies are projected as native Hermes links.
12. Native Hermes graph equals the Local First DAG for projected tickets.
13. Dependent tickets cannot dispatch before their native parents are truly complete.
14. Hermes, not Local First, ordinarily assigns/claims/spawns implementation workers.
15. Worker profile is not controlled by planner output.
16. Every Hermes execution of a Local First ticket becomes one durable Local First attempt.
17. Duplicate reconciliation cannot create duplicate attempts.
18. Deterministic validation occurs before acceptance.
19. Review is independent/fresh.
20. Same-ticket repair is bounded.
21. Review failure does not recursively create uncontrolled defect cards.
22. A parent cannot reach final `done` before configured Local First acceptance/integration.
23. Accepted predecessors integrate before they release native children.
24. Integration lineage is authoritative and replay-safe.
25. Tranche-level verification runs after rolling ticket integration.
26. H1/H2 completion authority is derived from durable evidence.
27. Future tranche detail is revalidated against the current integration SHA.
28. Local First pause prevents new autonomous Local First releases.
29. Projection/API failure does not repeat model work.
30. Canonical checkout dirt is never silently cleaned or made authoritative.
31. Accepted history is immutable.
32. Supplemental corrections are append-only.

---

# 29. Fast follow — make this a publishable Hermes plugin

Once the full-system fixture above is green, the next priority is not additional orchestration cleverness. It is turning the working private plugin into a safe, installable, understandable Hermes product.

The publishable target should behave like a normal Hermes plugin:

```text
install
→ enable
→ open dashboard
→ configure project + model/profile policy
→ validate prerequisites
→ admit feature
→ observe execution
```

No source edits or private-path changes should be required.

---

# 30. Publishable plugin packaging

Target repository layout:

```text
local-first-orchestrator/
├── plugin.yaml
├── __init__.py
├── local_first_orchestrator/
│   ├── ...
│   └── migrations/
├── tests/
├── README.md
├── LICENSE
├── CHANGELOG.md
├── docs/
│   ├── architecture.md
│   ├── configuration.md
│   ├── recovery.md
│   └── troubleshooting.md
└── dashboard/
    ├── manifest.json
    ├── plugin_api.py
    └── dist/
        ├── index.js
        └── style.css
```

The same installation should expose:

- Hermes agent/plugin integration;
- CLI/controller surfaces;
- the Local First dashboard tab;
- dashboard backend API.

Do not require a fork of Hermes core.

---

# 31. Public configuration model

All environment-specific behavior must move out of source code.

Configuration should be namespaced under a plugin-specific Hermes config section, for example:

```yaml
plugins:
  entries:
    local-first-orchestrator:
      config:
        enabled: true
        paused: true

        storage:
          database_path: "${HERMES_HOME}/local-first-orchestrator/ledger.db"
          artifact_root: "${HERMES_HOME}/local-first-orchestrator/artifacts"
          worktree_root: "${HERMES_HOME}/local-first-orchestrator/worktrees"

        kanban:
          board: default
          projection_enabled: true
          native_dependency_links: true

        decomposition:
          profile: architect
          max_active_tranche_tickets: 5
          max_files_per_ticket: 3
          max_changed_lines_per_ticket: 200
          repository_evidence_budget: 50000

        implementation:
          routing_mode: kanban_default
          profile: null

        review:
          default_profile: reviewer-local
          high_risk_profile: reviewer-frontier
          max_review_generations: 3
          max_repair_generations: 2

        verification:
          command_allowlist:
            - "python -m unittest"
            - "pytest"
            - "npm test"
            - "npm run typecheck"
          default_timeout_seconds: 600
          default_output_limit: 50000

        usage:
          architecture_budget_enabled: true
          checkpoint_budget_enabled: true
          paid_retry_requires_authorization: true

        policy:
          require_review: true
          require_integration_before_done: true
          fail_closed_on_projection_drift: true
```

The exact storage location/key shape may change to match Hermes's supported plugin configuration conventions, but the separation of concerns should remain.

---

# 32. Project/repository configuration

A publishable plugin must support multiple repositories/projects.

Each configured project should define at least:

```yaml
projects:
  - id: private-household
    repository_path: /path/to/repository
    board: household
    integration_ref: refs/local-first/private-household/integration
    allowed_verification_profiles:
      - python
      - node
    architecture_profile: architect
    review_profile: reviewer
```

Required behaviors:

- canonicalize repository path;
- verify repository ownership/allowlist;
- create project-specific storage identity;
- never hard-code the developer's private application path;
- support a project-specific board;
- support project-specific risk/verification/routing overrides;
- detect repository relocation/config conflict;
- provide a dashboard “Test project” preflight.

---

# 33. Fully configurable dashboard page

The plugin should ship a first-class **Local First** dashboard tab.

Current Hermes dashboard plugins can ship a `dashboard/manifest.json`, bundled JavaScript UI, optional CSS, and `dashboard/plugin_api.py` routes in the same plugin directory.

The page should make the orchestrator usable without editing YAML by hand for ordinary configuration.

## 33.1 Overview

Show:

- plugin enabled status;
- Local First pause state;
- dispatcher/board connectivity;
- ledger path;
- artifact path;
- current projects;
- active features;
- active tranches;
- runnable/running/reviewing/blocked counts;
- unresolved reconciliation issues;
- latest failed safety check;
- usage/budget status.

Primary controls:

```text
Pause Local First
Resume Local First
Run reconciliation
Validate configuration
Open active board
```

Dangerous controls require confirmation.

## 33.2 Projects

CRUD UI for project/repository registrations:

- project ID/name;
- repository path;
- board;
- integration ref/branch policy;
- source/test roots;
- repository allowlist/ownership checks;
- per-project verification profiles;
- per-project routing overrides;
- active/inactive state.

Actions:

```text
Validate repository
Capture test snapshot
Check board
Check native-link support
Check Git permissions
```

No destructive repository operation should be part of “Test project.”

## 33.3 Model & profile routing

A dedicated routing section should expose roles rather than internal implementation classes:

```text
Architecture / decomposition
Implementation
Fresh review
High-risk review
Tranche/final review
Escalation
```

For each role show/select:

- Hermes profile;
- provider/model if policy allows an explicit override;
- local/paid classification;
- fallback/escalation route;
- per-run budget where applicable.

Default implementation should support:

```text
Use Kanban board default assignee
```

so the plugin does not unnecessarily duplicate Hermes routing policy.

Planner output has no access to these controls.

## 33.4 Decomposition policy

Editable:

- max active tranche tickets;
- max files per ticket;
- max changed lines;
- evidence/context budget;
- allowed source/test roots;
- ticket-risk thresholds;
- overlap policy;
- create/modify path policy;
- future-tranche detail horizon;
- unresolved-question behavior;
- planner retry policy.

Show explanatory help text because these settings materially affect safety and cost.

## 33.5 Validation & review policy

Editable:

- deterministic verification profiles;
- command allowlist;
- timeout/output limits;
- required validation stages;
- required review;
- review profile by risk;
- max review generations;
- max repair generations;
- max implementation attempts;
- tranche-level review requirement;
- final integration review requirement;
- fail-closed thresholds.

Command configuration must distinguish trusted operator-configured commands from model-supplied text.

## 33.6 Usage & budget policy

Editable:

- paid architecture budget;
- checkpoint budget;
- escalation budget;
- maximum paid calls per feature/tranche;
- ambiguous-call retry behavior;
- whether invalid planner output may be retried automatically;
- per-role model/cost limits.

Display:

- current feature usage;
- recent paid reservations;
- unresolved reservations;
- reason for last denied authorization.

## 33.7 Feature/plan inspector

The dashboard should expose a read-oriented feature explorer:

```text
Feature
  ├── contract
  ├── repository snapshot
  ├── decomposition plan
  ├── tranche graph
  ├── microtickets
  ├── attempts
  ├── validation
  ├── reviews/repairs
  ├── accepted commits
  └── completion evidence
```

Useful actions:

- inspect raw/normalized planner response;
- inspect validation reasons;
- inspect native Hermes task-link mapping;
- compare Local First DAG to Hermes graph;
- view attempt/run/workspace identity;
- view diff hash/accepted commit;
- view H1/H2 evidence.

The dashboard should not make mutable artifact files themselves authoritative.

## 33.8 Reconciliation/health page

Show explicit drift categories:

```text
Local First ticket missing Hermes card
Hermes card missing Local First identity
dependency-link mismatch
Hermes run not bound to attempt
attempt missing workspace/run
board says done but Local First not accepted
accepted commit not in integration lineage
stale plan
unresolved outbox event
planner mutation incident
runtime-binding conflict
```

Each row should identify:

- affected entity;
- expected state;
- observed state;
- safe automatic repair availability;
- whether human action is required.

Provide a bounded “Reconcile safe items” action.

Never hide conflicting authority behind an automatic “fix everything” button.

## 33.9 Evidence and audit

Expose:

- immutable event history;
- request/response artifacts;
- hashes;
- accepted evidence;
- integration lineage;
- model/provider/profile provenance;
- usage reservations;
- recovery authorizations.

Allow export of a diagnostic bundle with secrets excluded.

## 33.10 Advanced settings

Advanced settings may include:

- storage paths;
- snapshot/evidence limits;
- artifact retention;
- concurrency caps;
- polling/reconciliation cadence;
- outbox retry policy;
- logging level;
- compatibility flags;
- historical recovery tools.

Hide these behind an advanced section so ordinary setup remains understandable.

---

# 34. Dashboard backend API

The UI should use a narrow plugin backend API under the plugin's Hermes dashboard namespace.

Representative routes:

```text
GET  /status
GET  /config
PUT  /config

GET  /projects
POST /projects
PUT  /projects/{id}
DELETE /projects/{id}
POST /projects/{id}/validate

GET  /features
GET  /features/{id}
GET  /features/{id}/graph

GET  /reconciliation
POST /reconciliation/run

GET  /artifacts/{safe-id}
GET  /usage

POST /pause
POST /resume
```

Design requirements:

- validate every config mutation server-side;
- use atomic config writes;
- preserve unknown Hermes config keys;
- never write secrets into dashboard-visible config;
- return machine-readable validation errors;
- expose read-only diagnostics separately from mutating actions;
- require explicit confirmation tokens/semantics for destructive/high-authority actions where appropriate;
- path parameters may never become arbitrary filesystem reads.

Because Hermes dashboard plugin API routes are designed for the local dashboard environment, the plugin must avoid assuming the dashboard API is an internet-facing authenticated control plane.

---

# 35. Secret handling

The Local First dashboard should not become a credential manager.

Use Hermes's existing provider/profile credential mechanisms.

Rules:

- API keys/tokens remain in Hermes-managed secret/environment surfaces;
- plugin config stores profile/provider references, not secret values;
- diagnostic export strips secret-shaped values;
- dashboard never returns provider credentials;
- plugin pack/config examples contain only non-secret defaults.

---

# 36. Plugin capabilities and consent

Before publication, enumerate every Hermes capability the plugin genuinely requires.

Prefer the narrowest possible capability set.

If the plugin needs host-level model/profile overrides or other privileged surfaces, declare them in `plugin.yaml` so Hermes's normal consent flow applies.

The plugin should degrade clearly when an optional capability is not granted.

No update should silently expand authority.

---

# 37. Installation and distribution

Target supported install paths:

```text
hermes plugins install owner/repo
hermes plugins install owner/repo --ref <immutable-commit>
```

and eventually a community-index entry.

Release requirements:

- semantic version;
- changelog;
- pinned release commit;
- tested minimum Hermes/API version;
- migration notes;
- capability declaration;
- license;
- screenshots;
- README quick start.

A community-index submission should point to an immutable release commit.

---

# 38. First-run setup experience

A new user should be able to:

1. install plugin;
2. enable plugin;
3. launch `hermes dashboard`;
4. open Local First;
5. add repository/project;
6. choose a Kanban board;
7. choose architecture/review profiles;
8. leave implementation routing on board default or select a profile;
9. validate prerequisites;
10. run a dry-run fixture;
11. keep autonomous progression paused until they explicitly enable it.

The first-run page should explain the authority model:

```text
Local First controls graph/trust.
Hermes controls worker execution.
```

---

# 39. Configuration migrations and versioning

The plugin must support upgrades without asking users to rebuild their ledger.

Add:

- explicit schema version;
- idempotent SQLite migrations;
- config schema version;
- config migration/normalization;
- pre-upgrade backup guidance;
- compatibility check against Hermes API/plugin version;
- read-only migration dry run where practical.

Unknown/newer schema versions fail closed.

---

# 40. Observability and metrics

Publishable operation needs diagnostics beyond logs.

Track at least:

## Planning

- plans proposed;
- plans rejected;
- validation reason frequencies;
- planner model/profile;
- planner mutation-tripwire incidents;
- stale-plan rate.

## Ticket quality

- first-attempt implementation success;
- average changed files/lines;
- validation failure categories;
- repair generations;
- review finding categories;
- context/ticket-size correlation.

## Runtime

- active attempts;
- reconciliation lag;
- duplicate/conflict events;
- outbox retry counts;
- Hermes graph drift;
- average implementation/review latency.

## Cost

- paid architecture calls;
- paid review/escalation calls;
- token/cost estimates where available;
- denied reservations;
- budget utilization.

Metrics should improve policy tuning without becoming acceptance authority.

---

# 41. Publishable security hardening

Before public release:

1. remove all private absolute paths;
2. remove private project IDs/data from fixtures;
3. use temporary fixture repositories;
4. validate every user-supplied path;
5. prohibit traversal/symlink escape where relevant;
6. preserve repository allowlist/ownership rules;
7. sanitize artifact download routes;
8. sanitize diagnostic exports;
9. avoid shell-string construction where argv is available;
10. validate configured verification commands against trusted policy;
11. fail closed on malformed config;
12. cap model/runtime concurrency;
13. cap artifact/log output;
14. document dashboard network-exposure assumptions;
15. document that capability consent is not a sandbox;
16. provide a threat-model document.

---

# 42. Test matrix for publishable release

## Unit

- configuration schema;
- config migrations;
- repository registration;
- path validation;
- routing resolution;
- policy precedence;
- dashboard API validation;
- capability absence;
- artifact-safe lookup;
- serialization.

## Integration

- clean Hermes user install;
- plugin enable/disable;
- dashboard discovery;
- dashboard config round trip;
- multi-project isolation;
- multi-board isolation;
- project validation;
- native task-link projection;
- Hermes worker reconciliation;
- pause/resume;
- restart recovery;
- schema migration;
- plugin upgrade.

## E2E

A public fixture repository should prove:

```text
install plugin
→ configure from dashboard
→ admit conceptual feature
→ decompose
→ activate
→ native Hermes DAG
→ dispatcher executes root
→ Local First reconciles
→ validation
→ review
→ repair
→ rereview
→ accept/integrate
→ native child release
→ tranche completion
```

Run the fixture using fake/deterministic model adapters in CI and a documented optional real-model smoke test.

---

# 43. Documentation required for publication

Ship:

## README

- what problem the plugin solves;
- architecture diagram;
- install;
- enable;
- quick start;
- dashboard screenshot;
- supported Hermes version;
- limitations.

## Configuration reference

Every dashboard/config setting:

- type;
- default;
- risk;
- reload behavior;
- project override behavior.

## Architecture

Explain:

```text
Local First = graph/trust authority
Hermes = execution scheduler
```

and why both ledgers/states exist.

## Recovery guide

Procedures for:

- stale projections;
- missing links;
- orphan Hermes run;
- runtime-binding conflict;
- planner mutation incident;
- failed validation;
- repair exhaustion;
- stale plan;
- integration conflict.

## Security/threat model

Include:

- model tool isolation;
- repository mutation tripwire;
- secret handling;
- dashboard exposure;
- plugin capabilities;
- path controls;
- accepted-history immutability.

## Contributor guide

- dev install;
- fixture repos;
- test commands;
- migration rules;
- release process.

---

# 44. Dashboard usability acceptance criteria

The dashboard is “fully configurable” when an ordinary user can configure every supported non-secret operational choice without editing plugin source and without needing YAML for normal setup.

Minimum acceptance:

1. Create/edit/remove a project.
2. Choose board.
3. Choose architecture/decomposition profile.
4. Choose implementation routing mode.
5. Choose review/escalation profiles.
6. Set ticket-size/decomposition limits.
7. Set validation/verification policy.
8. Set repair/review attempt limits.
9. Set paid usage budgets.
10. Pause/resume Local First.
11. Validate repository and board configuration.
12. See native dependency graph.
13. See Local First↔Hermes reconciliation health.
14. Inspect plan/attempt/review/integration evidence.
15. See why a ticket is not progressing.
16. Export a safe diagnostics bundle.
17. Restore/reset a setting to documented default.
18. Detect invalid settings before they are committed.
19. Never display or store provider secrets.
20. Preserve advanced config values it does not edit.

---

# 45. Publishable plugin release gate

Do not publish merely because the current private workflow works.

Release candidate requires:

```text
Core orchestration E2E green
+
native Hermes DAG projection green
+
Hermes execution reconciliation green
+
same-card review/repair green
+
rolling integration green
+
multi-project config green
+
dashboard config/health green
+
migration/upgrade tests green
+
private-path/data scrub complete
+
security review complete
+
documentation complete
```

Then:

1. tag/release;
2. install from a clean Hermes environment using the public path;
3. run the public E2E fixture;
4. pin the tested commit;
5. submit community plugin index entry if desired.

---

# 46. Implementation order including fast follow

## Core completion

```text
1. Native Hermes dependency projection
2. Hermes execution → Local First attempt reconciliation
3. Post-implementation deterministic validation
4. Fresh review handoff
5. Same-card repair/rereview
6. Rolling acceptance + integration
7. Native dependent release
8. Tranche completion/H1
9. Next-tranche progression
10. Full conceptual-feature E2E
```

## Fast follow: publishability

```text
11. Remove machine/private-path assumptions
12. Public config schema + project registry
13. Hermes profile/routing configuration abstraction
14. Dashboard backend API
15. Local First dashboard tab
16. Overview + Projects + Routing + Policy pages
17. Reconciliation + Evidence/Diagnostics pages
18. Config migration/versioning
19. Plugin capability declarations
20. Public fixture repositories/tests
21. Security/threat-model pass
22. README/config/recovery docs
23. Clean-install/upgrade E2E
24. Versioned release
25. Community plugin index submission
```

---

# 47. Current recommended next work

Resume implementation with **native Hermes dependency projection**.

Do not spend the next slice assigning profiles to TK-2/TK-3.

The generic defect is:

```text
Local First dependency DAG exists
but Hermes native task links do not
```

The intended fix is:

```text
create projected active-tranche cards safely
→ resolve Hermes task IDs
→ project exact native parent links
→ verify graph equivalence
→ release
→ let Hermes determine todo/ready
```

After that, implement **Hermes execution reconciliation** so an ordinary dispatcher-owned worker run becomes a durable Local First attempt and flows through validation/review rather than bypassing the trust lifecycle.

---

# 48. Hermes compatibility references checked for this revision

The publishability/dashboard sections were aligned with the current public Hermes plugin/dashboard surface as of 2026-09-08:

- Hermes plugin system and distribution:
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/plugins.md
- Extending the Hermes dashboard:
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/extending-the-dashboard.md
- Hermes Kanban behavior/dashboard:
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md

Relevant current Hermes capabilities include:

- user plugins under `$HERMES_HOME/plugins` / `~/.hermes/plugins`;
- `plugin.yaml` agent plugins;
- opt-in enable/disable behavior;
- install/update/remove flows;
- pinned commit installs;
- capability declarations/consent;
- community plugin index;
- dashboard extensions via `dashboard/manifest.json`;
- a pre-built JavaScript dashboard bundle;
- optional `dashboard/plugin_api.py`;
- native Kanban dependency/task links;
- dashboard plugin discovery without a Hermes core fork.

These are integration targets, not Local First trust authority.

---

# 49. Final architectural statement

The target system is not:

> “a second coding-agent runtime layered on top of Hermes.”

It is:

> **a durable planning, evidence, safety, and trust controller that turns complex work into a correct Hermes-executable graph and refuses to treat implementation as complete until deterministic validation, review, repair, and integration policy are satisfied.**

Hermes should remain excellent at running workers.

Local First should remain excellent at deciding what those workers are allowed to do next, preserving why those decisions were made, and proving that the resulting work is safe enough to advance the graph.

---

# 50. Current implementation status — 2026-09-09

This section records the current estimated implementation state of the repository against this design. It is intentionally conservative: **Complete** means the repository contains the required mechanism with convincing implementation/test evidence; **Partial** means substantial implementation exists but one or more required operational, integration, or end-to-end proofs remain; **Not started** means the design requirement is not yet represented as a working repository capability.

The items below are ordered by the sequence that currently appears most useful for completing the system, with missing orchestration and end-to-end proof first, then integration hardening, then already-built foundations.

## Status legend

- **Complete** — implemented with repository evidence sufficient for the current design requirement.
- **Partial** — materially implemented, but still missing required integration, operational proof, or a portion of the designed behavior.
- **Not started** — no working implementation of the required capability is currently evident.

## Recommended completion order

### 1. Restartable `process-next` scheduler — **Complete for implementation; live acceptance pending**

A bounded `process-next` controller operation now exists. It serializes ticks with a restart-recoverable lease and routes root-ticket local readiness, native Hermes dependency-graph projection/release, implementation, deterministic validation, fresh review, repair routing, bounded triage/decomposition, pre-commit acceptance, Git integration/commit, local completion/Hermes projection intent creation, deterministic tranche integration/checkpoint creation, paid checkpoint/escalation, and two-phase next-tranche materialization/activation verification as independent durable stages alongside generated-card, state-projection, and evidence-comment delivery work. Implementation persists model-launch intent before inference and recovers completed work without duplicate invocation; validation binds exact candidate/diff/worktree identity and leaves failures for a later routing tick; review is packet-only and binds the validated candidate plus configured provider/model/timeout/schema identity; repair routing atomically records review/validation outcomes, applies stable failure fingerprints and attempt limits, routes eligible failures back to the same ticket with preserved worktree continuity and failure evidence, and sends repeated/exhausted failures to triage exactly once. Triage is claimable only from such durable routing decisions, uses the configured local decomposition route in planning-only mode, persists proposal/model provenance before bounded child application, assigns durable child identities in the ledger, atomically enqueues retry-safe Hermes child-card creation, preserves runtime/criterion provenance, and prevents child implementation until card creation is acknowledged. Acceptance is a separate ledger-owned stage after a pass-routing decision: it re-checks the live worktree/root/base and exact candidate diff, binds implementation/validation/review artifacts and hashes into an append-only accepted-candidate record, and atomically transitions the ticket to `accepted` without creating a Git commit. Git integration then claims only that immutable accepted candidate, persists commit launch intent before mutation, creates or exactly recovers one isolated accepted commit, advances the controller-owned tranche integration ref with compare-and-swap when applicable, and persists append-only commit/integration evidence while leaving the ticket `accepted`. Completion then requires the immutable accepted/Git evidence to agree, deterministically materializes the compatibility `accepted_evidence` record, atomically transitions `accepted → done`, and enqueues Hermes state/evidence outbox intents in that same local transaction; remote projection retries are therefore independent and cannot repeat prior model or Git work. Paid checkpoint/escalation claims bind immutable tranche checkpoint lineage, purpose, provider/model/profile provenance, and use the deterministic scheduler claim ID as the usage-governor request key so reservation precedes provider execution and replay cannot double-spend. Budget exhaustion leaves the claim blocked for explicit approval, ambiguous paid outcomes are durably `unknown_outcome` and never reinvoked, and completed paid calls can be recovered after a crash before scheduler effect persistence from their durable model-call response. Scheduler-wide reconciliation is now a derived decision layer over those existing durable records rather than a second state machine: claims classify as not started, external outcome unknown, external effect complete/local record incomplete, local completion/downstream projection incomplete, or fully finalized, with explicit retry/replay/reconcile/resume/stop actions. Model/paid terminal ambiguity stops before normal work using the existing stage-specific reconciliation errors, while exact-recovery/deterministic stages fall through to their established replay handlers; read-only dry-run consults the same classifier. Scheduler-wide ordering is now explicit rather than incidental: a canonical rank table orders generated-card projection, state projection, evidence comments, recoverable expired claims, implementation, validation, review, repair, triage, acceptance, Git, completion, native dependency graph/release, tranche checkpoint, paid checkpoint/escalation, next-tranche materialization/activation, and finally new root readiness admission. External projection drains before recovery, the Milestone 14 recovery selector gates every non-matching lifecycle stage for that tick, and fresh implementation precedes fresh validation only when no recoverable claim owns the lifecycle slot. Scheduler-wide concurrency is now proven against independent SQLite connections and real thread contention rather than a single Ledger lock: overlapping global ticks admit one bounded executor, expired tick takeover has one winner, duplicate same-ticket stage claims produce one owner, recovery excludes a competing later stage for the same ticket, deterministic cross-ticket selection holds under overlap, state/comment outboxes lease once, one scheduler reaches the model-launch boundary, Git integration-head updates have one compare-and-swap winner, and paid request-key authorization is cross-connection idempotent. No second lock authority was introduced; these proofs exercise the existing `BEGIN IMMEDIATE`, lease, idempotency, launch-record, usage-governor, and Git-CAS boundaries. Scheduler-wide crash behavior is now complete and declarative: every scheduler work class has an explicit started-effect policy, common durable boundaries map to retry/reconcile/resume, model and paid stages stop on unknown started outcomes, and deterministic/idempotent stages replay through their existing durable authorities. The crash-policy registry is checked against the canonical scheduler stage order so coverage cannot silently drift when new work classes are added. Scheduler observability is now available as a bounded read-only projection over the same durable authorities: current/next stage and selected ticket/claim/lease, lifecycle attempt, reconciliation reason/action, pending board effects and leases, latest stage artifact, model invocation state, review/accepted-candidate identity, Git commit/integration identity, and paid reservation state. The default status remains lightweight; `status --scheduler-detail` opts into this deeper lifecycle boundary snapshot without writes or duplicate persisted metrics. A thin continuous daemon now wraps that exact one-tick scheduler rather than introducing another control authority: one shared registered scheduler factory is used by both `process-next --execute` and daemon ticks; the daemon adds only idle/busy/paused sleep, bounded exponential error backoff, graceful signal-driven stop between ticks, health counters/timestamps, and status composition. Restart tests prove a new daemon/Ledger instance simply continues the next durable scheduler effect with no daemon-specific recovery logic. Ambiguous pre-existing commits, missing launch intent, repository/worktree/diff drift, conflicting integration heads, completion-evidence drift, paid-call identity drift, and untracked bytes outside the accepted fingerprint fail closed. The registered CLI keeps dry-run as the default and requires both execution and board-write gates for live processing. Repository tests cover dependency exclusion, malformed legacy dependency data, overlapping ticks, expired-lease restart, stage-by-stage outbox delivery, implementation/validation/review/repair-routing/triage/acceptance/Git-integration/completion/paid-checkpoint preview and recovery, candidate and execution-policy drift, stable repeated-failure detection, retry continuity, triage boundedness/projection identity, immutable acceptance evidence, exact commit recovery, tranche-ref recovery, atomic terminal completion, Hermes projection retry, planning-snapshot revalidation, deterministic tranche integration failure, checkpoint-artifact replay, paid budget exhaustion/approval, escalation chaining, unknown paid-outcome no-repeat, completed-paid-call replay, re-snapshot successor materialization, materialization crash replay, exact successor card/native-graph activation verification, canonical cross-class ordering, external-projection/recovery anti-starvation, independent-view deterministic stage selection, independent-connection scheduler contention, tick/stage/outbox lease exclusion, Git CAS and paid-reservation races, full crash-policy coverage, injected stage-boundary restart/replay/stop behavior, model-stage crash boundaries, and read-only operator observability for exact scheduler lifecycle boundaries. The scheduler implementation and representative real low-risk end-to-end acceptance are complete. The live acceptance run used a real Hermes board, the configured local implementation model, an independent review profile/model process, isolated Git worktrees, deterministic validation, restart/reconciliation, native dependency behavior, and final Hermes completion; detailed evidence is in `docs/milestone-20-real-acceptance.md`. Hermes-dispatched execution reconciliation, exhaustive live external-boundary crash proof, and real paid-provider operational proof remain separate broader integration items.

See `docs/scheduler-plan.md` for the dependency-ordered scheduler completion roadmap, slice exit conditions, crash/concurrency proof requirements, and daemon sequencing.

**Completion condition:** a dependency-aware claim loop exists, persists stage transitions before side effects, survives process restart, and can resume without duplicating model calls, board effects, merges, or paid calls.

### 2. Daemon / continuous orchestration loop — **Complete for representative live acceptance**

`SchedulerDaemon` now repeatedly invokes the same registered `ProcessNextScheduler` factory used by one-shot execution. It owns no lifecycle semantics: each iteration performs at most one bounded scheduler tick, then applies only operational sleep/backoff policy. `no_work`/idle, busy, and paused outcomes use bounded delays; transient iteration failures use bounded exponential backoff; SIGINT/SIGTERM request stop between durable ticks; health reports iteration/outcome/error counters plus last stage/ticket/status/error timestamps; and daemon status composes the existing scheduler observability snapshot. CLI execution remains explicitly gated by registered runtime, `--execute`, `--allow-board-writes`, and explicit Hermes board access. A restart test closes one daemon/Ledger after state projection and proves a fresh daemon/Ledger continues the pending evidence-comment effect, demonstrating there is no daemon-specific recovery path.

**Completion condition:** a long-running process safely drives the restartable scheduler, honors pause state, exposes health/status, and recovers from interruption without manual reconstruction.

### 3. Full low-risk end-to-end acceptance path — **Complete for the representative scheduler scope**

A dedicated real Hermes task on an isolated acceptance board was imported through the supported Local First CLI and driven by the production daemon. A real configured local implementation model changed one allowed file in an isolated Git worktree, deterministic validation passed, a genuinely separate review profile/model process returned a schema-valid pass verdict, immutable acceptance evidence was frozen, one Git commit/integration-head update was recorded, Local First and Hermes both reached `done`, and all workflow projection/comment outboxes drained. The committed tree independently re-passed the acceptance checks, no paid post-architecture reservation/call occurred, a live crash after remote comment delivery reconciled exactly once on restart, and live Hermes dependency semantics advanced a child to `ready` after its parent completed. Exact IDs, hashes, commands, fixes, and boundaries are recorded in `docs/milestone-20-real-acceptance.md`.

**Completion condition:** one representative low-risk feature completes through the entire real workflow with auditable evidence and no hidden manual stage substitutions.

### 4. Hermes-native dependency graph projection and release — **Partial (scheduler implementation complete; native Hermes semantics live-proven)**

The scheduler now resolves authoritative Local First↔Hermes task identities, projects missing native parent links with Hermes `link`, verifies the exact native parent set, records immutable graph evidence, excludes dependent tickets from ledger-local readiness admission, and after acknowledged parent completion requires Hermes itself to report the child `ready` before recording release evidence. Graph divergence stops reconciliation, and restart paths re-read Hermes rather than duplicating links. A live Milestone 20 probe now verifies the current Hermes parent link and readiness semantics (`todo` while a parent is open, then `ready` after the parent completes). What remains for this broader design item is a full scheduler-generated active-tranche graph exercise together with the downstream Hermes-dispatched execution reconciliation path.

**Completion condition:** projected active-tranche cards receive exact Hermes parent/dependency links, graph equivalence is verified, release leaves readiness determination to Hermes as designed, and the behavior is demonstrated against the live integration.

### 5. Hermes execution reconciliation — **Partial**

The v2.0 design requires ordinary dispatcher-owned Hermes worker runs to become durable Local First attempts that continue through Local First validation/review rather than bypassing the trust lifecycle. The repository has durable attempts, provenance, and reconciliation machinery, but this Hermes-native execution handoff is not yet demonstrated as a complete production path.

**Completion condition:** a Hermes-dispatched worker run is detected, bound to the correct Local First attempt, reconciled into the ledger, and forced through deterministic validation/review before graph advancement.

### 6. Live Hermes read/write integration proof — **Partial, substantially proven by Milestone 20**

Milestone 20 exercised the real Hermes CLI/API surface for task reads, scheduled ownership, state projection, comments, marker lookup, terminal completion, native dependency links, and retry/reconciliation after an ambiguous comment-delivery crash. The run exposed and fixed stale adapter assumptions around comment-body availability, already-scheduled idempotence, and scheduled completion. Remaining live proof for this broader item is concentrated in paths not exercised by the representative run, especially a complete scheduler-generated tranche/card graph plus Hermes-dispatched execution reconciliation.

**Completion condition:** read, create/project, link, comment, complete/block, and retry/reconcile paths are verified against a real Hermes instance without direct Kanban-database mutation.

### 7. Real local implementation-model execution — **Complete for representative bounded execution**

Milestone 20 used the configured `custom:lm-studio` / `qwen3.8-27b@iq3_s` route to complete a bounded one-file ticket in an isolated Git worktree. Durable invocation provenance, the implementation artifact, diff hash, validation artifact, accepted-candidate fingerprint, and final Git commit were all persisted and audited; exactly one implementation invocation completed across the restart sequence.

**Completion condition:** a real local model successfully executes a bounded ticket in an isolated worktree, with invocation provenance, artifacts, diff hashing, and deterministic verification persisted.

### 8. Real fresh review-model execution — **Complete for representative fresh review**

Milestone 20 live-audited review independence and found a production profile-root wiring defect before proceeding. `LocalQwenAdapter` now carries a distinct `review_hermes_home`, and registered execution supplies the registered review profile directory. A real standalone tool-free review worker then ran under an isolated review profile with `openai-codex` / `gpt-5.6-terra`, emitted valid closed-schema JSON with verdict `pass`, produced one durable review artifact/result, and drove acceptance without reusing the implementation profile home or tool loop.

**Completion condition:** a real review model receives a fresh review packet, cannot inherit the implementation conversation/tool loop, emits valid closed-schema review output, and drives repair/acceptance correctly.

### 9. Crash/restart and exactly-once operational proof — **Partial**

The ledger, outbox, attempt provenance, stage artifacts, and reconciliation design are strong. Fake-runtime tests cover important retry cases, but the full crash matrix has not been demonstrated across real external model and Hermes effects.

**Completion condition:** forced interruption at each durable boundary proves that restart does not duplicate model calls, commits, board effects, review decisions, merges, or paid reservations.

### 10. Evidence-comment reconciliation — **Complete for live Hermes marker reconciliation**

Current Hermes `kanban show --json` exposes comment bodies. `HermesBoardAdapter.find_comment_marker()` now uses that supported read surface. In the Milestone 20 live proof, a worker was deliberately crashed after Hermes accepted a marked evidence comment but before Local First recorded delivery; restart found the existing marker, returned `reconciled_delivered`, kept the remote marker count at one, and finalized the outbox row without duplicate delivery.

**Completion condition:** Hermes exposes or the plugin obtains a supported idempotent mechanism for locating previously delivered evidence comments, and duplicate delivery is demonstrably prevented.

### 11. Paid checkpoint/model operational integration — **Partial (scheduler/production route complete; live provider proof pending)**

The scheduler now routes deterministic `ready_for_checkpoint` evidence into purpose-scoped paid checkpoint and escalation stages using the existing usage governor. Claim identity binds feature/tranche/checkpoint lineage plus provider/model/profile provenance, the deterministic scheduler claim ID is the governor request key, and the production `HermesPaidModelAdapter` invokes packet-only safe Hermes chat with explicit provider/model selectors. Durable reservations precede provider execution, completed responses are bound to immutable paid evidence, budget exhaustion performs no provider call until an explicit one-call `approve-paid` authorization is recorded, and ambiguous outcomes are marked `unknown_outcome` and never reinvoked for that request. What remains for this broader design item is operational proof against a real paid provider and the corresponding operator/UI workflow.

**Completion condition:** every paid invocation is atomically reserved before execution, purpose-scoped, auditable, budget-limited, proven to pause rather than overspend when the budget is exhausted, and demonstrated against the live paid-provider integration.

### 12. Operator lifecycle commands and recovery UX — **Partial**

The CLI already exposes substantial functionality for running, inspecting, admitting/planning, and reconciling work. The designed operator surface is not yet fully represented by coherent `init`, generic retry/reject, approval, metrics, and daemon lifecycle commands.

**Completion condition:** the operator can initialize, inspect, pause/resume, retry/reject/reconcile, approve paid work, inspect metrics, and run/stop the daemon through documented stable commands or equivalent Hermes-native UI actions.

### 13. Runtime metrics and adaptive sizing feedback loop — **Partial**

Symbol/metrics-related infrastructure exists, but the design's outcome-driven sizing loop depends on durable production measurements across real tickets, attempts, review outcomes, reverts, and token/runtime costs.

**Completion condition:** metrics are persisted from real runs, surfaced to operators, and used to adjust ticket/context sizing within explicit policy bounds.

### 14. Tranche integration and checkpoint flow — **Partial**

Architecture packets, tranches, snapshot-bound activation, and checkpoint concepts exist. The remaining work is full integration proof across tranche completion, repository revalidation, graph projection, and checkpoint decisions.

**Completion condition:** a multi-ticket tranche completes, integrates, validates against the expected repository state, checkpoints correctly, and activates the next tranche without bypassing trust policy.

### 15. Same-ticket repair machinery — **Partial (runtime path live-proven; failure-driven repair live proof pending)**

Bounded repair logic, failure fingerprints, same-ticket retry continuity, retry limits, and repeated-failure routing are implemented and covered by deterministic scheduler/restart tests. Milestone 20 removed the earlier uncertainty about whether the implementation/review/runtime stack itself works under real configured integrations: the real local implementation model, deterministic validator, independent review worker, acceptance, Git, daemon restart, and Hermes projection path all completed successfully. What remains for this item is narrower and explicit: deliberately produce a real deterministic implementation or validation failure, prove a bounded same-ticket repair attempt preserves prior-attempt provenance, and demonstrate limit exhaustion routing once into triage/escalation without duplicate work.

**Completion condition:** real deterministic failures trigger bounded same-ticket repair, preserve attempt history, stop at configured limits, and route repeated failure exactly once to triage/escalation.

### 16. Fresh independent review contracts — **Complete for representative live review**

The strict review schema, criterion mapping, blocking/nonblocking finding distinction, fresh-review worker boundary, and operational independence are implemented and live-proven. Milestone 20 identified and fixed the remaining profile-root coupling before acceptance: registered review execution now receives a distinct `review_hermes_home`. A standalone tool-free review worker then ran under a separate review profile/process with `openai-codex` / `gpt-5.6-terra`, consumed the frozen review packet, emitted valid closed-schema output with verdict `pass`, produced durable review evidence, and drove acceptance without inheriting the implementation profile home or tool loop.

**Completion condition:** real review execution is demonstrably fresh, independent, schema-valid, and authoritative only within the criterion/finding rules defined by this design.

### 17. Bounded triage/decomposition — **Complete**

The repository contains triage/decomposition contracts and enforcement for child count, recursion/depth limits, criterion mapping, duplicate handling, and bounded parent behavior. This is one of the stronger completed policy areas.

### 18. Deterministic validation and allowlist enforcement — **Complete**

Verification commands come from the trusted ticket/contract path rather than free-form reviewer invention, and changed-file/allowlist enforcement is implemented. Validation evidence is represented as a durable stage artifact.

### 19. Context-packet and token-budget enforcement — **Complete**

The repository contains bounded context-packet construction and explicit token/context budgeting for routine local-model work, supporting the design's local-first cost and determinism goals.

### 20. Architecture packets and snapshot-bound activation — **Complete**

Structured architecture packets, feature/tranche/microticket planning data, repository snapshot binding, and revalidation requirements are implemented. Activation is guarded against silently drifting repository state.

### 21. Paid-call reservation and budget governor — **Complete**

The core policy mechanism for atomic paid-call reservation, purpose binding, and budget governance exists. The remaining item above concerns live operational integration rather than the underlying governor design.

### 22. Ledger, state machine, provenance, and audit events — **Complete**

The local orchestration ledger is a mature part of the implementation. It tracks attempts, runtime bindings, model invocations, stage artifacts, review candidates, accepted evidence, state events, projection intent, and reconciliation data. This durable record is the foundation for fail-closed recovery.

### 23. Git worktree isolation and reconciliation — **Complete**

Implementation attempts use isolated Git worktrees rather than the canonical checkout, with expected-base checks, clean-state checks, diff/provenance handling, and reconciliation safeguards.

### 24. Board projection outbox and retry-safe effects — **Complete**

The project contains an outbox-oriented pattern for projecting durable Local First outcomes to Hermes so an external board-write failure does not require repeating successful model or Git work. Fake-runtime tests cover the core retry-safety behavior.

### 25. Phase 0 discovery and compatibility work — **Complete**

The compatibility/discovery phase is complete: existing interfaces were inventoried, compatibility decisions were documented, and the plugin architecture was built around Hermes rather than replacing it.

## Phase-level summary

Using the original implementation-phase grouping represented in the repository traceability material:

| Phase | Current estimate |
|---|---|
| Phase 0 — Discovery and compatibility | **Complete** |
| Phase 1 — Ledger and deterministic controller | **Complete for scheduler-owned lifecycle; broader operator/product integration still partial** |
| Phase 2 — Local implementation and validation | **Complete for representative bounded live execution** |
| Phase 3 — Local review and same-ticket repair | **Partial overall; independent live review complete, failure-driven repair live proof pending** |
| Phase 4 — Bounded triage | **Partial overall; core policy mechanisms complete, real failure-triggered triage proof pending** |
| Phase 5 — Architecture/checkpoint and paid governor | **Partial; scheduler/governor complete, full tranche and live paid-provider proof pending** |
| Phase 6 — Symbol-aware context and metrics | **Partial; bounded context complete, production feedback loop pending** |
| Hermes-native v2 execution/projection additions | **Partial overall; scheduler/daemon/representative live acceptance complete, Hermes-dispatched execution reconciliation and live paid-provider proof remain open** |

## Current overall assessment

The repository is no longer in an early implementation phase. Most safety-critical primitives are present: durable state, isolated attempts, deterministic validation, bounded repair and triage, structured review, architecture contracts, context budgeting, paid-call governance, and retry-safe projection machinery.

The dependency-ordered scheduler, daemon, and representative real low-risk Local First-owned acceptance path are now complete. The main unfinished work is concentrated in broader operational integration. The next dependency in the full v2 plan is **Hermes execution reconciliation**: dispatcher-owned Hermes worker runs must be detected, bound to the correct Local First attempt and provenance, and forced back through deterministic Local First validation/review before dependency advancement. After that, the remaining major proof sequence is a full scheduler-generated native tranche graph exercise, exhaustive live external-boundary crash injection, live paid-provider checkpoint/escalation operation, then the remaining operator/metrics/productization work.

The scheduler roadmap is **acceptance-complete for the representative low-risk Local First-owned path** demonstrated in Milestone 20. The broader v2 design remains substantially implemented but not fully operationally complete until Hermes-dispatched execution reconciliation, full-tranche live proof, live paid-provider proof, exhaustive external crash proof, and the remaining operator/metrics items are demonstrated.
