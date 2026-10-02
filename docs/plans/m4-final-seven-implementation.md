# M4 Final Seven Implementation Plan

> **Current operator direction:** Implement the remaining M4 features serially in the isolated worktree before building the milestone's focused test suite. Do not require per-seam RED/GREEN or independent acceptance after every preparation slice. Preserve existing tests and use lightweight smoke checks during implementation. Once M4 features are implemented, run focused critical-path/boundary verification, independent specification and quality reviews, and authorized disposable native proof before declaring M4 complete. The per-step RED/GREEN and per-slice review instructions below describe the earlier process and are superseded by this direction; functional requirements and isolation/effect safeguards remain binding. Implementation authorization includes correcting verified in-scope findings through completion, not production activation.

**Goal:** Deliver the remaining M4 workflow as seven serial, revision-bound plugin-only workstreams while preserving the board as lifecycle authority and Git as revision authority.

**Architecture:** The coordinator performs one durably reserved native effect at a time under the existing singleton lock. `EvidenceStore` records only immutable operation evidence and acknowledgements; `HermesBoardAdapter` executes supported CLI operations and exact readback. M4 only builds tranche planning, held materialization, dependency linking, serial integration, combined checks, and paid-review routing. M5 recovery matrix and M6 composition/surface wiring remain deferred.

**Tech stack:** Python, existing SQLite evidence store, supported Hermes CLI adapter, Git, pytest/jsonschema through offline uv.

---

## Global constraints

- Work only in `/home/ocadmin/.hermes/dev-worktrees/local-first-orchestrator-m4`; preserve its reviewed dirty changes and untracked `uv.lock`.
- Do not reset, clean, commit, install, update lockfiles, edit host sources/configuration/loaded plugins, call providers, dispatch work, or touch real boards.
- All mutable work is serial. Native disposable-board commands are proposed for the authorized parent only; a child keeps the native mutation guard intact.
- Every native mutation has exact accepted authority, a durable reserve-before-effect, singleton-lock assertion, attempt transition to `unknown`, exact effect-specific readback, and no blind resend from `unknown`. The two-card raw prebarrier is reserved for the first native link; Git integration instead uses expected-head CAS/revision evidence.
- Held-by-default is not a blanket workstream 1–5 prohibition: a card may be released only through a separately approved per-role implementation, paid-review, or correction admission boundary. No unrelated work is released, claimed, dispatched, or accepted.
- Run named tests only with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. uv run --offline --no-project --with pytest --with jsonschema python -m pytest -x -q -p no:cacheprovider -o addopts='--tb=short' ...`; exclude native mutations by their full test names.

## Workstream 1 — Dependency-link execution and reconciliation

**Objective:** Execute exactly one already-accepted tranche-zero prerequisite edge, then reconcile its exact two-card native receipt without creating/releasing/claiming work.

**Files:**
- Modify: `local_first_orchestrator/coordinator.py`
- Modify: `local_first_orchestrator/hermes_board.py` only for a demonstrated adapter-contract gap
- Modify: `tests/test_m4_active_piece_dependency_effect.py`
- Modify: `tests/test_m4_accepted_link_adapter.py`

1. Write a failing pure coordinator test for `pending -> unknown -> applied`: it must reconstruct only the existing `accepted_active_tranche_native_link_v1` proof, reserve before calling the adapter, make one supported `link source child` call, persist the exact readback, and leave members/budgets/cards-held unchanged.
2. Run that named test and record RED.
3. Add the narrow coordinator execution entry point plus trusted resolver/attempt-claim callbacks. The resolver reconstructs the exact accepted plan/route/root and immutable raw proof; the claim atomically changes only this pending operation to `unknown` under the held singleton lock.
4. Run GREEN for the first behavior.
5. Add one behavior at a time for: lost response/reopen (verify-only, zero resend); raw snapshot/topology drift at execution (conflict, zero send); operator pause/cancel before claim (zero send); unsupported adapter primitive (durable unknown/partial, zero release); and exact applied receipt replay (read-only, zero send/store mutation even when fresh observations/time windows differ).
6. Run pure named coordinator/adapter tests; the parent separately executes the proposed disposable-board command below. Stop for independent spec review, then independent quality review.

**Acceptance:** Unknown is never authorization to replay. Applied reconciliation reads the exact stored two-card acknowledgement and cannot create membership, budget, release, successor, claim, or acceptance effects.

## Workstream 2 — Safe active-piece release into implementation/local review

**Depends on:** approved workstream 1.

**Objective:** Add a separate release admission boundary for one exact held piece, with capacity/budget evidence and current hold/readback checks.

1. RED: active pause/cancel, stale held receipt, changed root/route, missing capacity receipt, and already-running card all prevent release.
2. GREEN: reserve release operation and capacity atomically, use supported unblock, read back exact ready state/marker, and record only the release receipt.
3. Do not claim, dispatch, request review, or infer a worker run. Local-review routing remains a later owned-worker boundary.
4. Run pure tests; parent runs only an authorized disposable-board release characterization after review.

## Workstream 3 — Serial Git integration bound to expected tranche HEAD

**Depends on:** approved workstream 2 and actual accepted local-review evidence.

**Objective:** Integrate one accepted piece at a time into the dedicated tranche ref using expected-head CAS.

1. RED: dirty candidate, stale expected head, conflict, wrong candidate/review identity, and non-fast-forward/head drift preserve work and create no replacement.
2. GREEN: freeze candidate/base/head/check evidence; integrate serially; update expected tranche head atomically; re-read the resulting ref. A changed cherry-pick, rebase, or conflict-resolved result has a new candidate identity: required checks and fresh local review run again, followed by a later paid review before acceptance/release; the prior approval is invalid for that changed content.
3. Do not merge/push canonical branches or delete/clean worktrees.
4. Run Git fixture tests and inspect the exact ref/result.

## Workstream 4 — Combined checks and exact-revision paid integrated review

**Depends on:** approved workstream 3 for all current-tranche pieces.

**Objective:** Freeze the integrated base/head, run deterministic combined checks, and create/route a held paid-review request for that exact revision.

1. RED: missing piece evidence, combined-check failure/timeout, revision drift, malformed verdict, wrong profile/run/session, and stale findings block review/approval.
2. GREEN: persist revision-bound check artifacts and paid-review request evidence; native worker ownership remains responsible for its own review transition.
3. A completed card/lane is never paid approval; no next tranche release.
4. Run parser/schema/check fixtures and controlled native review characterization only from parent.

## Workstream 5 — Bounded correction, reintegration, and re-review

**Depends on:** approved paid review from workstream 4 that contains valid findings.

**Objective:** Produce one bounded correction generation with immutable finding lineage, then require fresh local review, serial reintegration, checks, and another paid review.

1. RED: duplicate/reordered findings canonicalize idempotently; changed content, exhausted root/finding cap, ambiguous create, or concurrent repair request blocks another correction.
2. GREEN: atomically reserve correction budget/create intent; reconcile markers before resend; keep correction held until separately admitted; preserve all useful work.
3. Re-review the exact new integrated head; no acceptance from prior verdict.
4. Run pure recovery/correction fixtures and independent review.

## Workstream 6 — Tranche acceptance and authorized successor release

**Depends on:** approved fresh paid verdict from workstream 4 or 5 for exact current integrated head.

**Objective:** Record tranche acceptance and only then make a separately authorized successor materialization/release decision.

1. RED: changed head, unresolved finding, missing required check, invalid reviewer provenance, active pause/cancel, or unmanaged anchor dependent prevents acceptance/release.
2. GREEN: record immutable acceptance evidence, re-read dependency consequences, then materialize/release only the explicit successor tranche through its own held-effect protocol.
3. Do not complete anchor, merge/push canonical branch, or release unrelated board work.
4. Run acceptance and downstream-gating fixtures.

## Workstream 7 — End-to-end M4 proof

**Depends on:** workstreams 1–6 independently reviewed.

**Objective:** Prove the whole M4 path in a disposable isolated fixture: paid plan -> held pieces -> native links -> authorized releases -> local review evidence -> serial integration -> combined checks -> paid review -> bounded correction/re-review -> exact acceptance -> successor decision.

1. Map each canonical M4 scenario to a named pure/native fixture test and document fixture-only status.
2. Inject restart windows before/after every native operation acknowledgement, stale board/Git evidence, pause/cancel, unsupported operations, and lost CLI responses.
3. Run exact named pure suite, then the parent-controlled disposable native suite; capture command output, capability limitations, and source revision hashes.
4. Obtain independent specification review, then independent quality review; any correction renews both reviews.

## Explicitly deferred

- **M5:** exhaustive recovery/restart matrix, polling/missed-hook recovery, broad concurrent notification handling, and all canonical acceptance scenarios beyond the M4 path.
- **M6:** plugin hook/tool/dashboard/CLI composition, packaging/discovery, live configuration, loaded-plugin edits, and legacy removal.

## Proposed parent-controlled disposable native commands

Run only after independent review from the parent context, with the pinned installed CLI:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI=/home/ocadmin/.hermes/hermes-agent/venv/bin/hermes \
uv run --offline --no-project --with pytest --with jsonschema python -m pytest -x -q -p no:cacheprovider \
  -o addopts='--tb=short' \
  tests/test_m4_accepted_link_adapter.py::test_native_coordinator_accepted_first_edge_is_pending_unknown_applied_once

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI=/home/ocadmin/.hermes/hermes-agent/venv/bin/hermes \
uv run --offline --no-project --with pytest --with jsonschema python -m pytest -x -q -p no:cacheprovider \
  -o addopts='--tb=short' \
  tests/test_m4_native_first_edge_link_characterization.py::test_native_first_edge_link_characterization
```

These are disposable fixture mutations only; they are not child-agent commands and are not evidence of live activation. Workstream 1 is only the first-edge execution boundary, not completion of its planning, release, integration, paid-review, correction, or acceptance dependencies.
