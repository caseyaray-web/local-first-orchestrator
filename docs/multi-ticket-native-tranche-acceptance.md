# Multi-ticket scheduler-generated native tranche graph — live acceptance evidence

Date: 2026-09-11

## Result

The full multi-ticket scheduler-generated native tranche graph item is acceptance-complete for the representative real path.

Local First generated a two-ticket first tranche with a real dependency, projected both cards to a real Hermes board, let Hermes own native dependency readiness, reconciled two real dispatcher-owned worker runs with zero Local First implementation model calls, advanced the rolling tranche integration head ticket-by-ticket, checkpointed the tranche, authorized successor materialization through the existing paid-governor/scheduler checkpoint path using a deterministic injected approval adapter, exercised the registered real standard successor planner, projected the successor card, froze exact successor activation evidence, and separately live-verified that the successor runtime binding and Hermes worktree are anchored at the predecessor final integration SHA.

The deterministic checkpoint approval in this proof is intentionally **not** the later live-paid-provider acceptance item.

## Main acceptance environment

- root: `/tmp/lfo-multiticket-tranche-acceptance-v4`
- Hermes board: `lfo-multiticket-tranche-acceptance-v4`
- feature: `multi-feature-v4`
- T1: `TR1`
- T2: `TR2`
- original T1 baseline: `c2a9257bf006b71859ee0b2a114d2ec071da8f6e`

Generated T1 tickets:

- `T1-A` → Hermes `t_7c7aa72c`
- `T1-B` → Hermes `t_b2039b87`

Generated successor ticket:

- `T2-C` → Hermes `t_d219df31`

## Automatic generated-ticket activation

The scheduler now performs acknowledged generated-ticket activation as bounded `dependency_readiness` work rather than requiring the operator `activate-generated` command.

For independent generated tickets:

1. authoritative generated projection provenance is resolved;
2. repository/base runtime binding is persisted;
3. Hermes' canonical worktree is pre-materialized at the authoritative execution base;
4. the ticket can move to `ready_local` and be released for dispatcher execution.

For dependent generated tickets:

1. runtime binding is persisted while the local ticket remains `draft`;
2. the exact native Hermes parent graph must converge;
3. the handoff-marked blocked card is unblocked only after graph convergence;
4. Hermes owns `todo -> ready` based on native parent completion;
5. only after Hermes reports `ready` does Local First atomically record native release evidence and move the child `draft -> ready_local`.

Because Hermes `ready` was just observed authoritatively, the corresponding Local First ready-state projection is acknowledged inside the same transaction rather than sent back to Hermes. The evidence comment remains deliverable. This prevents a delayed Local First `unblock` from racing a fast worker that has already blocked for reconciliation.

## Native dependency proof

T1 dependency:

```text
T1-A -> T1-B
```

Hermes external parent identity for B:

```text
t_7c7aa72c
```

Durable graph evidence:

- child external id: `t_b2039b87`
- local dependency ids: `["T1-A"]`
- parent external ids: `["t_7c7aa72c"]`
- graph hash: `eb45f90a7b9bbe0cea2a8c52adfb5be3290e60e55617f9d8d7bbbc5067fd215d`

Before A executed:

- A was Hermes `ready`;
- B was Hermes `todo`;
- B had exact parent A;
- dispatcher dry-run selected only A.

After A reached Local First/Hermes completion, Hermes advanced B to `ready`. Local First recorded `native_dependency_releases` for B with `hermes_status = ready` and the exact graph hash above, then admitted B locally.

## Authoritative worktree base across dependent tickets

The live graph exposed a critical stale-base risk: without intervention Hermes would create a later dependent worker worktree from the repository's incidental checked-out branch rather than the Local First tranche integration head.

Local First now prepares Hermes' exact canonical task worktree before release:

```text
<canonical-repo>/.worktrees/<external-task-id>
```

on branch:

```text
wt/<external-task-id>
```

at the authoritative `GitWorktreeAdapter.resolve_execution_base(...)` SHA.

Replay verifies:

- exact worktree toplevel;
- common Git directory equals the canonical repository;
- exact branch;
- exact HEAD;
- clean status.

Drift fails closed.

In the live run, A's accepted commit was:

```text
03152253435c7b17c023375ac9eac895657bef74
```

Before B dispatch, B's prepared Hermes worktree HEAD was asserted to be exactly:

```text
03152253435c7b17c023375ac9eac895657bef74
```

Therefore B started from the accepted A integration head, not the original T1 baseline.

## Dispatcher-owned execution for both T1 tickets

### T1-A

Hermes worker run:

- run id: `1`
- status/outcome: `blocked / blocked`
- summary: `local-first-awaiting-reconciliation`
- reconciled base: `c2a9257bf006b71859ee0b2a114d2ec071da8f6e`

Accepted commit:

```text
03152253435c7b17c023375ac9eac895657bef74
```

Integration head:

```text
c2a9257bf006b71859ee0b2a114d2ec071da8f6e
  -> 03152253435c7b17c023375ac9eac895657bef74
```

Hermes terminal projection later produced run `2` with summary `local-first projection ticket-event:75`; this was Local First completion projection, not a second implementation worker.

### T1-B

Hermes worker run:

- run id: `3`
- status/outcome: `blocked / blocked`
- summary: `local-first-awaiting-reconciliation`
- reconciled base: `03152253435c7b17c023375ac9eac895657bef74`

Accepted commit:

```text
eaf8a128ad3984e7d91fb61c9d90a99950ebd917
```

Integration head:

```text
03152253435c7b17c023375ac9eac895657bef74
  -> eaf8a128ad3984e7d91fb61c9d90a99950ebd917
```

Hermes terminal projection later produced run `4` with summary `local-first projection ticket-event:143`; again this was Local First completion projection.

Both T1 cards ended Hermes `done`, and B retained the exact native parent A.

## Model ownership proof

For the completed T1 graph:

- Local First implementation model invocations: **0**
- Local First review invocations: **2**
- review provider/model: `openai-codex / gpt-5.6-terra`
- both review invocations completed

Thus implementation remained dispatcher-owned for both tickets while validation/review/acceptance remained Local First-owned.

## T1 tranche completion and checkpoint

Final T1 integration SHA:

```text
eaf8a128ad3984e7d91fb61c9d90a99950ebd917
```

Tranche completion evidence hash:

```text
08f24ce4ba149748b063c90a8b39e24bc5c5bec23344a002a661b6600bcc0714
```

Checkpoint decision:

```text
ready_for_checkpoint
```

Checkpoint artifact SHA256:

```text
d6112987c8562dfae2f40a99328a312e707f836cbac4fa24274bdc65e5b0874f
```

## Successor authorization gate

Successor materialization remains guarded by effective paid-checkpoint approval.

For this graph proof, the public `UsageGovernor` + scheduler paid-checkpoint path was exercised with `InjectedPaidModelAdapter`; no live paid provider was called.

Durable evidence:

- purpose: `integration_checkpoint`
- model call id: `a878c78f1c9c4f3f91ed8695b3be380d`
- decision: `approve`

This approval exists only to prove the downstream materialization/activation stages. The separate live-paid-provider roadmap item remains open.

## Real standard successor planning

The registered standard decomposition route was exercised with:

- profile: `worker-code-terra`
- provider: `openai-codex`
- model: `gpt-5.6-terra`

The first live successor call revealed that `LocalDecompositionPlanner` lacked a successor-specific planner method and therefore asked the model to re-plan the full feature. Local First correctly rejected that output because completed criteria were reintroduced.

A dedicated `propose_next(...)` contract now constrains successor planning to:

- exactly one tranche;
- exactly the coarse successor criterion ids;
- no completed criteria;
- exact coarse tranche objective/capabilities;
- exact predecessor final integration SHA;
- no scope-change proposals.

The same safe-tool Hermes chat, strict parser, protected-repository fingerprinting, and provenance artifacts remain in force. Post-parse checks independently reject multiple tranches, criterion expansion, tranche criterion expansion, or scope-change proposals.

The real standard planner subsequently materialized exactly one TR2 ticket:

```text
T2-C
```

with only criterion `AC-C` and allowed file `gamma.py`.

Durable successor materialization:

- predecessor: `TR1`
- successor: `TR2`
- repository base: `eaf8a128ad3984e7d91fb61c9d90a99950ebd917`
- snapshot hash: `aad72ac88185349b0f3027871fa424751f85e5fa060ab33f89b429bb5ad2579f`
- ticket ids: `["T2-C"]`

## Successor projection recovery and activation

The first T2 create projection failed deterministically before any Hermes create because the projection verifier still compared the card contract against the original feature planning base. The card payload itself correctly contained the successor materialization base.

`generated_projection_identity()` now uses `next_tranche_materializations` repository/base/snapshot provenance for tickets in a successor tranche; first-tranche and correction provenance remain unchanged.

Because the failed outbox row had:

- no external task id;
- no acknowledgement;
- no active lease;
- deterministic pre-create failure only;

it was safe to recover explicitly after the verifier fix.

A new operator command:

```text
reopen-terminal-generated-projection --task-id <id> --event-id <id>
```

only reopens terminal generated-card rows when the current durable payload revalidates against current authoritative projection identity and there is no evidence of any external create. It never retries an ambiguous external effect and does not rewrite payload bytes.

The V4 recovery reopened event `174`, delivered the same create projection, and created Hermes successor task:

```text
t_d219df31
```

`next_tranche_activation` then completed with claim:

```text
cbbda58d1f274bc11335db7aceed2fc4
```

Durable activation evidence:

- predecessor: `TR1`
- successor: `TR2`
- ticket ids: `["T2-C"]`
- external task ids: `["t_d219df31"]`
- dependency graph hashes: `[]` (single independent successor)
- TR2 status: `active`
- TR2 base: `eaf8a128ad3984e7d91fb61c9d90a99950ebd917`

The projected TR2 Hermes card was `blocked` with no parents and embedded exactly:

- repo base `eaf8a128ad3984e7d91fb61c9d90a99950ebd917`
- snapshot `aad72ac88185349b0f3027871fa424751f85e5fa060ab33f89b429bb5ad2579f`

## Successor runtime-binding verification after final provenance fix

The V4 activation occurred before the final runtime-binding resolver fix, and runtime bindings are immutable by design. The incorrect V4 binding was therefore not edited or deleted.

A separate clean verification environment was created:

- root: `/tmp/lfo-successor-activation-verification`
- board: `lfo-successor-activation-verification`
- repository anchored at real V4 predecessor final integration commit `eaf8a128ad3984e7d91fb61c9d90a99950ebd917`

Fresh successor ticket:

```text
SV-C
```

Fresh external task:

```text
t_d68852cf
```

Production scheduler sequence:

1. generated projection delivered;
2. `next_tranche_activation` completed;
3. automatic generated activation ran as `dependency_readiness` and returned `activated_ready`.

Final runtime binding:

```text
starting_sha  = eaf8a128ad3984e7d91fb61c9d90a99950ebd917
canonical_sha = eaf8a128ad3984e7d91fb61c9d90a99950ebd917
```

Prepared Hermes worktree:

- branch: `wt/t_d68852cf`
- HEAD: `eaf8a128ad3984e7d91fb61c9d90a99950ebd917`

This separately proves the final successor generated-activation resolver uses immutable successor materialization provenance rather than the original feature baseline.

## Additional production changes exposed by the live graph

The real graph proof drove fixes that isolated tests had not exposed:

1. automatic scheduler-owned generated activation;
2. explicit dependent-card unblock only after native graph convergence;
3. atomic Local First admission on authoritative Hermes `ready`;
4. immediate acknowledgement of the redundant ready-state projection to avoid racing a fast worker handoff;
5. pre-materialization of Hermes worktrees at the rolling tranche integration head;
6. direct validated-plan successor contract/path authority support;
7. successor-specific real planner scope contract;
8. successor projection provenance based on materialization evidence;
9. safe operator recovery for deterministic pre-create projection failures;
10. successor runtime binding based on materialization provenance.

All retain fail-closed behavior for ambiguous external effects or provenance drift.

## Validation

Representative focused suites during this tranche included:

```text
38 passed
67 passed
59 passed
30 passed
58 passed, 3 subtests passed
81 passed, 22 subtests passed
77 passed, 22 subtests passed
```

Final complete repository validation:

```text
752 passed, 179 subtests passed in 67.91s
```

## Acceptance conclusion

The full multi-ticket scheduler-generated native tranche graph item is complete for the representative real path:

1. multiple generated cards are projected and activated without manual ticket lifecycle substitution;
2. exact native Hermes dependency graph/readiness is authoritative;
3. dispatcher-owned implementation is reconciled ticket-by-ticket without duplicate Local First implementation calls;
4. later dependent workers start from the rolling accepted integration head;
5. the first tranche integrates and checkpoints successfully;
6. successor materialization is gated by durable approval and uses the real registered standard planner against the predecessor final integration snapshot;
7. successor cards project with successor provenance and exact external identity;
8. `next_tranche_activation` freezes the successor card/graph evidence;
9. successor runtime binding/worktree preparation at the predecessor final integration SHA is live-verified.

The next broader v2 integration item is exhaustive live external-boundary crash/restart proof. Live paid-provider checkpoint/escalation operation remains the following separate item.
