from __future__ import annotations

HANDOFF_MARKER = "<!-- local-first-execution-handoff:v1 -->"
HANDOFF_SENTINEL = "local-first-awaiting-reconciliation"
HANDOFF_BLOCK_KIND = "needs_input"

HANDOFF_INSTRUCTIONS = f"""{HANDOFF_MARKER}
Local First owns validation, review, integration, and final completion for this task.
Implement and test the contract in the assigned workspace. When implementation is ready for Local First validation, move this Hermes task to Review with `hermes kanban request-review`.
Review is an implementation handoff only: do not claim that Local First validation, independent review, integration, or final completion has passed.
If the task is accidentally marked Done instead, Local First will treat that only as a recoverable implementation handoff and will still run its own validation and review gates.
Legacy recovery marker: `{HANDOFF_SENTINEL}`.
"""


def attach_execution_handoff(body: str) -> str:
    if HANDOFF_MARKER in body:
        return body
    return f"{HANDOFF_INSTRUCTIONS}\n{body}"
