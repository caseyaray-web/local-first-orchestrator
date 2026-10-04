# M7 bounded worker-context diagnostic probes

**Status: diagnostic implementation tested; neither procedure below has been executed with the new diagnostic artifact. Fresh independent artifact review and separate parent authorization are required.**

These procedures distinguish synthetic handler evidence from a proposed live-worker probe. They do not authorize Hermes-core changes, production installation, real-board enrollment, service changes, or a full workflow rehearsal. Earlier artifact/fixture reports remain historical and do not establish live acceptance of this diagnostic version.

## Provider-free synthetic handler probe

Use a newly built, digest-pinned artifact in an isolated registration harness. Register its actual public tools with a runtime-factory sentinel that refuses composition. Supply synthetic task/run/session/board context through the supported test context binding, explicitly omitting one field per case. Invoke the closed-schema `local_first_request_local_review` handler.

The response must be:

```json
{"error":"native_worker_context_unbound","missing_native_context_fields":["<logical field>"],"ok":false,"outcome":"invalid_or_held"}
```

Allowed field names are exactly `task_id`, `run_id`, `session_id`, and `board_id`. The session is read through Hermes's supported `gateway.session_context.get_session_env`; do not add a stale process-global fallback. Never include identity values or raw environment dumps in diagnostic error output.

This synthetic path requires no Kanban dispatch, provider, native board mutation, or installed production plugin. It proves diagnostic shape and rejection before composition only. It does **not** identify the missing field in a real worker or prove that native dispatch supplies the required context. If a case unexpectedly reaches the sentinel, fail the probe; do not compose a runtime or reserve evidence.

## Proposed separately authorized live-worker probe

A live AI-worker probe necessarily permits a bounded provider invocation. It cannot be described as provider-free. Before execution, obtain explicit parent authorization for the disposable setup, selected worker/provider route, provider/run/time budget, and cleanup scope. Do not silently alter credentials or model/provider choice.

1. Build and independently review the exact diagnostic artifact; record its digest and selected supported Hermes executable.
2. Create a fresh disposable Hermes home, Kanban root/board, workspace, and plugin evidence-store root. Controlled plugin installation and enablement are permitted **only in that disposable home** under this separate authorization. Do not reuse prior rehearsal or production state.
3. Prepare enough supported scoped lifecycle state for a real worker to invoke the actual registered tool. Do not fabricate native run/session evidence. Keep delegated-child native mutation guards intact; native dispatch belongs to the authorized parent.
4. Instruct the worker to make one diagnostic tool call and stop on a missing-context response. Capture the response with field names only. Do not instruct it to implement code, run checks, request native review, or retry.
5. For a missing-context response, the handler must fail before runtime composition or evidence reservation. Verify this from scoped evidence and the implementation boundary; provider execution itself has occurred and must be reported.
6. A successful context capture can reach normal runtime composition. Therefore this live path cannot promise zero composition for every response. Stop the probe on unexpected success or a different error; inspect any resulting scoped effects without assuming they are absent. Never treat a capture result as approval, finalized handoff, or rehearsal success.
7. Verify cleanup using exact task/run/PID/start ownership and supported native readback. Unknown ownership or effects remain partial/held. Preserve evidence and do not blindly resend.

No live probe is authorized by this document alone. Report synthetic execution, live provider execution, context capture, and lifecycle acceptance as separate claims.
