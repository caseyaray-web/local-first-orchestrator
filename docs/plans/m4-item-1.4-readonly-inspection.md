# M4 Item 1.4 Read-only Accepted DAG Inspection Plan

**Goal:** Inspect the active accepted tranche’s complete declared dependency DAG from trusted accepted evidence and exact native raw reads without any store or board mutation.

**Scope:** Item 1.4 only. No link execution, reservation, acknowledgement, membership, budget, release, claim, dispatch, acceptance, successor, or runtime wiring.

## API and files

- Create `local_first_orchestrator/accepted_dependency_inspection.py`: bounded, plain-JSON validation of complete raw card observations and declared/proven edge comparison. It returns a frozen report containing `missing_declared_edges`, `proven_applied_edges`, `pending_or_unknown_edges`, and `conflicts`; it never returns approval/completion authority.
- Modify `local_first_orchestrator/hermes_board.py`: add `HermesBoardAdapter.read_accepted_active_tranche_raw_cards(scope, task_ids)`, a closed exact-scope `show --json` plus `runs --json` reader. It performs no mutating CLI command.
- Modify `local_first_orchestrator/coordinator.py`: add `Coordinator.inspect_accepted_active_tranche_dependencies(plan_id, *, request_id=None)`. Under the singleton lock, it reconstructs accepted source/token/route/root/current observer facts and exact managed create receipts, captures store/operator intent before and after adapter reads, and delegates only normalized immutable data to the pure inspection module. It reports uncertainty on changed observations; it never writes.
- Every newly acknowledged accepted-piece create receipt must retain a versioned `raw_capture_v1` containing the exact public `show` envelope and its separate exact `runs` response. The normalized `BoardSnapshot` remains available to existing consumers, but inspection must consume the frozen raw capture, never rebuild the creation baseline from typed fields; receipts lacking a valid raw capture fail closed before acknowledgement or membership registration. When inspection transports the separate runs result alongside `show`, it must preserve the original `show.runs` presence and value separately from the runs endpoint, so omitted, empty, or divergent show fields remain distinct.
- Inspection and applied-link replay must require exactly the supported immutable production prebarrier v2 kind and its complete canonical evidence fields plus exact before/after raw transition. Prebarrier v1, malformed receipts, and missing evidence remain diagnostic conflict, never proven-applied edges. This does not change accepted-piece v1/v2 payload compatibility. Legacy pending-operation admission is explicitly deferred from this correction by the operator; that path is not production readiness evidence and must be reconsidered before final cutover.
- A paused/cancelled inspection reads only persisted accepted-plan semantics, reports all declared edges as missing/unobserved, and returns the complete frozen diagnostic shape with no proven or pending edge claims. It does not call the planning observer or native adapter. Reader absence, malformed show/runs responses, and unavailable or partial observations return a deeply frozen `raw_observation_conflict` report with a bounded reason and zero proven edges; strict accepted-authority validation remains outside this reader-error boundary.
- Create `tests/test_m4_accepted_active_tranche_inspection.py`: TDD coverage for valid chain/fanout/fanin, already-applied first edge, missing edge, zero-edge card, opaque-field preservation, malformed/drifted card, pause, unknown/pending no-resend, future managed-member refusal, and exact full-store/no-native-write assertions.
- Modify `tests/test_m4_accepted_link_adapter.py`: production-adapter raw-read characterization proving the helper invokes only `show`/`runs` for exactly supplied IDs.

## TDD sequence

1. Write a failing adapter characterization for the absent raw-read helper; run it as RED.
2. Add the closed adapter read-only helper; rerun GREEN.
3. Write a failing coordinator inspection test with a valid held chain and unchanged database/native call dump; run RED.
4. Add the smallest pure report and coordinator wrapper; rerun GREEN.
5. Add one failing test at a time for missing/unknown edges, zero-edge cards, drift/opaque fields, pause, future members, and before/after uncertainty; keep each focused test green.
6. Run named pure files with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. uv run --offline --no-project --with pytest --with jsonschema python -m pytest -x -q -p no:cacheprovider -o addopts='--tb=short'`.
7. Parent-only after review: run the named disposable native fixture with `HERMES_M0_CLI` set. Child fixtures must skip before filesystem setup or subprocess work when the pinned CLI is unavailable.

## Non-goals and exit gate

The report may say an edge is declared-but-missing, proven-applied, pending/unknown, or conflicting. It must not say that the DAG is complete, approved, released, or safe to execute. Stop after independent specification review and then quality review; remaining M4 items 1.5–1.7 and items 2–7 are deferred.
