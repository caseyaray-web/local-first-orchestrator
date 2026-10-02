# Parent-only native paid-review/correction characterization

**Exact node:** `tests/test_m4_native_paid_review_correction_topology.py::test_native_paid_review_and_correction_are_parentless_and_admitted_once`

Run this only from the authorized parent with a pinned installed Hermes executable:

```bash
cd /home/ocadmin/.hermes/dev-worktrees/local-first-orchestrator-m4
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. HERMES_M0_CLI=/absolute/path/to/pinned/hermes \
  uv run --offline --no-project --with pytest --with jsonschema python -m pytest -x -q \
  -p no:cacheprovider -o addopts='--tb=short' \
  tests/test_m4_native_paid_review_correction_topology.py::test_native_paid_review_and_correction_are_parentless_and_admitted_once
```

The fixture rejects `HERMES_DELEGATED_CHILD_CONTEXT` before any temporary home, board, database, Git repository, or CLI setup. It then uses only a disposable `tmp_path` root with isolated `HERMES_HOME` and `HERMES_KANBAN_HOME`, clears inherited board overrides through the established fixture, and invokes the pinned CLI through the production `HermesBoardAdapter`.

The test creates a canonical accepted plan, advances a real disposable Git integration ref, persists a single durable integration-chain receipt, and has the real `Coordinator` produce the paid-review and correction create/release payloads. It checks parentless held cards, exact stored target fields, paid-capacity and implementation-attempt reservations, the `unknown` claim phase immediately before each supported native unblock, and fresh adapter/CLI readback of the ready cards.

## Deliberate limitation

No provider, dispatcher, or worker executes. The paid `changes_requested` record is explicitly controlled **synthetic review input** and is stored directly only to supply the correction's immutable finding/revision source. It is not asserted to be an observed native reviewer run. The paid-review and correction held-create/release effects themselves are real parent-only coordinator → production adapter → pinned-CLI operations with fresh native readback.
