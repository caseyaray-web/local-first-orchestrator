from __future__ import annotations

HANDOFF_MARKER = "<!-- local-first-execution-handoff:v1 -->"
HANDOFF_SENTINEL = "local-first-awaiting-reconciliation"
HANDOFF_BLOCK_KIND = "needs_input"

HANDOFF_INSTRUCTIONS = f"""{HANDOFF_MARKER}
Local First owns validation, review, integration, and final completion for this task.
Implement and test the contract in the assigned workspace, but DO NOT mark this Hermes task done/complete.
When implementation is ready for Local First validation, block this task with kind `{HANDOFF_BLOCK_KIND}` and reason exactly `{HANDOFF_SENTINEL}`.
Do not unblock or complete the task yourself after that handoff.
"""


def attach_execution_handoff(body: str) -> str:
    if HANDOFF_MARKER in body:
        return body
    return f"{HANDOFF_INSTRUCTIONS}\n{body}"
