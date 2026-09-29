# M1: pure contracts and safe primitives (isolated source)

**Status:** M1 replacement source is being verified on `m1-safe-primitives`, based on M0 `f249699`. This branch does not preserve the legacy plugin runtime. The installed plugin, real board, providers, profiles and services are untouched. Importing the source-tree plugin entrypoint is safe, but `register()` explicitly refuses activation until the later M6/M7 gates.

## Planned contract layout

- `ticket.py` owns the immutable, versioned `TicketContract`, `PatchBudget`, `VerificationProfile`, `declared_ticket_paths`, `parse_contract` and `contract_payload`. Ticket identity includes every contract field. Verification command declarations have independent count, argv and byte ceilings; declarations do not authorize execution.
- `decomposition.py` owns `Criterion`, `TranchePlan`, `DecompositionPlan` and `PlanValidator`. Canonical plan hashes include full ticket payloads, tranches, criteria and coverage. The caller supplies trusted criteria, size ceilings and any known plan-ID/hash bindings. Conflicting reuse of a known plan ID fails, as do duplicate ticket/tranche IDs, invalid coverage, future dependencies and cycles; graph traversal is iterative.
- `review.py` owns `ReviewFinding`, `ReviewResult`, `failure_fingerprint`, `normalize_review`, `ReviewPacketBuilder.build` and `validate_review_identity`. Normalization requires the *expected* candidate SHA, matching contract identity, per-criterion evidence, and explicit blocking findings for repairs. A malformed or stale verdict cannot become approval through a lane move.
- These modules and the package exports do not import the legacy ledger/controller. The transitional `m1_contracts.py` has been removed. Old branch-only callers of legacy `MicroTicket`/`FeatureContract` are deliberately not maintained; replacing those calls is subsequent plan work, not an excuse to restore a second runtime.

## Safe Git and verification primitives

- `git_adapter.py` keeps bounded Git inspection/diff hashing, safe candidate ticket IDs, frozen worktree identity, ancestry/CAS integration checks and non-destructive worktree handling. It neither moves the default branch nor deletes useful work.
- `validation.py` takes `TicketContract` on its sole public `validate` path. The caller must supply trusted allowed commands and verification limits separately from the ticket. It checks candidate scope, changed-line/file budgets, secret patterns and repository/head identity, streams bounded snapshots and command output, enforces an aggregate deadline and terminates timed-out process groups. Ignored candidate files fail closed without deletion. Git administrative state (including linked-worktree metadata) is bounded and fingerprinted before/after checks; repositories beyond its 64 MiB/100,000-entry/15-second inspection ceiling fail closed rather than yielding evidence. Mutated candidates cannot generate a passing artifact; stable evidence uses exclusive candidate-bound files outside the worktree. Validation is not acceptance authority.

## Fixture evidence and stop boundary

- `uv run --no-project --with pytest python -m pytest tests/test_m1_contracts.py tests/test_m1_git_primitives.py tests/test_m1_verification.py -q --tb=short -o addopts=`: **39 passed**. Python compilation and `git diff --check` also passed. All Git fixtures use disposable repositories; no native board or model was called.
- The old full suite is expected to fail collection where legacy modules still import removed `MicroTicket`/`FeatureContract` symbols. The earlier 150-test legacy compatibility result applies to the previous transitional commit, **not** this source revision; it is not evidence that the replacement runtime works.
- M2 must build and fixture-verify the supported board adapter, operation records and operator recovery before decomposition automation. M3–M7 remain unimplemented and separately gated. No plugin registration, real enrollment, provider request, merge, push, service reload or cutover is authorized by this source work.
