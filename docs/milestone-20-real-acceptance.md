# Milestone 20 — Real end-to-end acceptance evidence

Date: 2026-09-11

## Result

Milestone 20 passed against real configured integrations. One representative low-risk Local First feature proceeded from a real eligible Hermes task through real local implementation, deterministic validation, independent structured review, acceptance, Git commit evidence, Local First completion, Hermes completion projection, and exactly-once evidence-comment reconciliation. A separate live Hermes parent/child probe verified native dependency advancement. No paid reservation or paid checkpoint call was made.

## Isolated acceptance environment

The acceptance run was intentionally isolated from the existing operator registration and unrelated work:

- Hermes board: `lfo-m20-acceptance`
- canonical acceptance repository: `/tmp/local-first-m20-acceptance/repo`
- Local First ledger: `/tmp/local-first-m20-acceptance/ledger.db`
- Local First operator registration: `/tmp/local-first-m20-acceptance/operator-config.json`
- implementation profile: `worker-code-local`
- implementation provider/model: `custom:lm-studio` / `qwen3.8-27b@iq3_s`
- review profile used during the live run: an isolated copy of `worker-review-terra`, configured only for the acceptance run to allow the plugin's explicit provider/model override; the original profile was not modified and the temporary copy was removed after the run
- review provider/model: `openai-codex` / `gpt-5.6-terra`

The acceptance repository baseline commit was `d694bcc8f0dd9c298e221126cbea4f7156dd2c2d`.

## Representative feature

Real Hermes task:

- task id: `t_cc72b17f`
- title: `M20 acceptance: implement greeting`
- imported Local First ticket id: `bf3cfd9fee724171a854e3a5577e68ff`
- final Hermes status: `done`
- final Local First state: `done`
- final Hermes result: `local-first projection ticket-event:86`

The admitted contract allowed only `app.py`, required a bounded one-file patch, and deterministically verified:

```text
from app import greet
assert greet('Ada') == 'Hello, Ada!'
assert greet('Bob') == 'Hello, Bob!'
```

plus `python -m py_compile app.py`.

## Real implementation execution

The implementation stage used the configured local Hermes/Qwen route in an isolated Git worktree.

Durable invocation evidence:

- provider/model: `custom:lm-studio` / `qwen3.8-27b@iq3_s`
- exactly one completed implementation invocation
- implementation artifact SHA256: `580554ae71e25527e46d613557677fe080c4b26a09b800ba2d1e2d691482d9ed`
- implementation diff hash / accepted candidate fingerprint: `0954665049ed0388e709f9161a955ed00d354d24ea0c25204b866fa48c649dfe`

The produced diff was exactly:

```diff
 def greet(name):
-    return "hello"
+    return f"Hello, {name}!"
```

## Deterministic validation

Validation passed against the isolated worktree and was persisted before review.

- validation artifact SHA256: `df1dd87eba168b3fe49602bc74b89124bc151e5f24f8df45448feddb258331fd`
- the committed tree was independently re-extracted and both required validation commands passed again after completion

## Independent fresh review

The live acceptance audit found and fixed a production wiring defect before the workflow was allowed to continue: review provider/model routing was separate, but the review subprocess inherited the implementation profile's `HERMES_HOME`. `LocalQwenAdapter` now has a distinct `review_hermes_home`, and registered execution passes the registered review profile directory.

The real review then ran through the standalone tool-free `review_worker` under the isolated review profile:

- provider/model: `openai-codex` / `gpt-5.6-terra`
- exactly one completed review invocation
- review result id: `1`
- verdict: `pass`
- review artifact SHA256: `d23bd0f0779759e58bf303a463b2401b7ab546da54813c3c2ca6bf7bfbe61f32`

No implementation conversation or file/terminal tool loop was reused by the review subprocess.

## Acceptance and Git evidence

The accepted candidate was frozen before Git mutation.

- accepted evidence hash: `e17b96257e88eb2771c04c8cc28238f8f176b23bab8ab1bfd314851159952e15`
- accepted candidate fingerprint: `0954665049ed0388e709f9161a955ed00d354d24ea0c25204b866fa48c649dfe`

Git integration then produced one durable commit:

- commit: `a90f1515801ee75314a829d0fc79d00a43fc3703`
- integration head before: `d694bcc8f0dd9c298e221126cbea4f7156dd2c2d`
- integration head after: `a90f1515801ee75314a829d0fc79d00a43fc3703`
- commit message: `local-first: M20 acceptance: implement greeting`

The commit exists in the real Git object database and its archived tree passed the acceptance validation commands after completion.

## Real Hermes projection fixes proven by the run

The live run exposed three additional stale assumptions in `HermesBoardAdapter`, all fixed and regression-tested before resuming the same durable ledger:

1. **Scheduled nonterminal projection must be idempotent.** Local First intentionally keeps owned Hermes cards `scheduled`. Re-scheduling an already-scheduled card fails in current Hermes. The adapter now reads current task state first and treats an already matching scheduled/blocked/done status as satisfied.
2. **Hermes comments are machine-readable.** Current `hermes kanban show --json` exposes comment bodies. `find_comment_marker()` now reads those comments and returns `FOUND` / `NOT_FOUND` instead of permanently reporting `UNSUPPORTED`.
3. **Scheduled completion requires `unblock` before `complete`.** Current Hermes refuses `complete` on a scheduled task and `promote` does not accept scheduled tasks. The adapter now uses the supported audited transition `scheduled -> unblock --reason -> complete` for final Local First completion.

The same durable acceptance ticket was resumed after each fix; implementation, validation, review, acceptance, and Git were not repeated.

## Real crash/restart and exactly-once comment proof

A real external-effect crash was injected after Hermes accepted an evidence comment but before Local First recorded delivery.

- crash-probe operation id: `f1b8c8ed282853b3f289cf3eafe1aacc`
- remote marker count immediately after injected crash: `1`
- local outbox state immediately after crash: `delivering`
- restarted worker result: `reconciled_delivered`
- remote marker count after restart: `1`
- local outbox state after restart: `delivered`

This proves the live Hermes comment-marker reconciliation path prevents duplicate delivery after an ambiguous process-loss boundary.

## Real native dependency behavior

A separate live board probe verified current Hermes native dependency semantics:

- parent task: `t_999352da`
- child task: `t_a1697170`
- child parent set included exactly `t_999352da`
- while the parent was open, unblocking the child resulted in `todo`
- after the parent completed as `done`, Hermes advanced the child to `ready`
- after proof, the child was re-parked as `scheduled` so the dispatcher could not execute it

This verifies the external readiness behavior that Local First's native dependency-release stage relies on.

## Paid-call audit

The low-risk acceptance path made no unnecessary paid post-architecture call:

- `paid_reservations`: `0`
- `paid_checkpoint_evidence`: `0`

## Final external and durable state

At terminal audit:

- Hermes acceptance task: `done`
- Local First acceptance ticket: `done`
- exactly one completed implementation invocation
- exactly one completed review invocation
- review verdict: `pass`
- accepted candidate evidence present
- Git intent/evidence completed
- pending generated-card projections: `0`
- pending state projections: `0`
- pending evidence comments from the workflow: `0`
- all workflow evidence-comment rows delivered
- scheduler next stage after completion: `no_work`

## Repository validation after production fixes

Focused live-adapter regression suites passed after the fixes. Final complete repository validation:

```text
735 passed, 179 subtests passed in 68.54s
```

## Acceptance conclusion

The scheduler milestone sequence is acceptance-complete for the representative low-risk path required by Milestone 20. The live run used real Hermes board operations, a real configured local implementation model, a genuinely separate review profile/model process, isolated Git worktrees, deterministic validation, restart/reconciliation, native dependency semantics, and final board completion without hidden manual lifecycle substitution or paid post-architecture calls.

Broader design items that intentionally remain separate from this milestone include Hermes-dispatched worker reconciliation into Local First attempts, exhaustive live crash injection at every external boundary, and live paid-provider checkpoint/escalation operation.
