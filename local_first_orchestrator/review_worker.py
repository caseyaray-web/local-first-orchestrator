"""Standalone, tool-free structured-review worker.

This module is deliberately launched in a fresh Python process by
``LocalQwenAdapter``.  Its only host integration is ``PluginLlm``; it does
not construct an agent or expose any tool loop.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from .local_qwen import REVIEW_JSON_SCHEMA


def _request() -> dict[str, Any]:
    try:
        raw: Any = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise ValueError("review worker request must be JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"packet", "timeout_seconds"}:
        raise ValueError("review worker request has an invalid shape")
    if not isinstance(raw["packet"], str) or not raw["packet"] or not isinstance(raw["timeout_seconds"], int) or not 1 <= raw["timeout_seconds"] <= 21_600:
        raise ValueError("review worker request has an invalid shape")
    return raw


def main() -> int:
    try:
        request = _request()
        # This is host-owned, bounded inference only.  Do not replace it with
        # a Hermes chat/agent invocation or a direct provider client.
        from agent.plugin_llm import PluginLlm

        response = PluginLlm(plugin_id="local-first-orchestrator").complete_structured(
            instructions=("Perform an isolated local code review. Return only the review proposal; "
                          "do not call tools or request additional context."),
            input=[{"type": "text", "text": request["packet"]}],
            json_schema=REVIEW_JSON_SCHEMA,
            json_mode=True,
            schema_name="local_first_review",
            temperature=0,
            purpose="local_first_review",
            timeout=request["timeout_seconds"],
        )
        print(json.dumps({"content_type": response.content_type, "parsed": response.parsed}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"local structured review failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
