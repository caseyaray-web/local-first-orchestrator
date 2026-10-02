# Native M5 restart proof boundary

`tests/test_m5_native_recovery_restart.py` is **parent-only**. Its fixture checks
`HERMES_DELEGATED_CHILD_CONTEXT` before creating a home, SQLite store, adapter,
or CLI subprocess. It requires a file-valued `HERMES_M0_CLI` and confines the
disposable board, `HERMES_HOME`, `HERMES_KANBAN_HOME`, workspace, lock, and
EvidenceStore under pytest `tmp_path`.

## Covered parent commands

Run only from the isolated worktree, with a pinned installed Hermes executable:

```bash
HERMES_M0_CLI=/absolute/path/to/hermes \
  /home/ocadmin/.hermes/hermes-agent/venv/bin/python -m pytest -q \
  tests/test_m5_native_recovery_restart.py
```

The first node builds an accepted two-card v2 plan through the public
Coordinator and adapter. It holds the pieces, executes one real disposable
`kanban link`, injects `BaseException` at `ack_effect` after the durable
validated-link receipt checkpoint, closes SQLite, then opens a fresh
EvidenceStore and Coordinator with the original trusted planning observer,
profile, workspace, roles, budget policy, and scope. It reads the native child
card with the CLI and proves the recovered public link call acknowledges the
checkpoint with zero further link commands, one applied link event, and no
additional member record.

The second node is deliberately narrower than provider/worker recovery. It
uses the supported public Coordinator pause/resume boundary: release one
planner card, issue the real adapter hold through `pause`, close/open SQLite,
prove the durable operator pause still blocks `tick`, then use explicit
`resume(authorized_clear=True)` to issue one native release per managed card
and verify native ready readback. The final readback-only resume clears the
intent; no acknowledged release is resent.

## Limits

This is not a live provider, activation, worker, dispatcher, or delegated-child
proof. It does not claim that an unavailable native recovery-poll hook can
detect a missing duplicate or automatically clear a temporary recovery hold.
Those behaviors retain their separate synthetic recovery-poll coverage. No
native SQL is written directly; synthetic accepted-plan evidence is used only
to establish the authorized Coordinator context, while link/hold/release and
readback use the production adapter and real disposable CLI.

When `HERMES_DELEGATED_CHILD_CONTEXT=1`, run the same node only to prove the
fixture guard:

```bash
HERMES_DELEGATED_CHILD_CONTEXT=1 HERMES_M0_CLI= \
  /home/ocadmin/.hermes/hermes-agent/venv/bin/python -m pytest -q \
  tests/test_m5_native_recovery_restart.py
```

Expected result once the repository imports: two skips before native fixture
setup. This repository's current shared runtime changes can independently
prevent collection; that import failure is not a successful skip proof.
