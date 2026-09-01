from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ModelResult:
    payload: dict[str, object]
    artifact_path: Path
    argv: tuple[str, ...]


Runner = Callable[..., subprocess.CompletedProcess[str]]


LOCAL_QWEN_PROVIDER = "custom:lm-studio"
LOCAL_QWEN_MODEL = "qwen3.8-27b@iq3_s"

# This is deliberately an exact, closed response contract.  The reviewer is
# not an agent turn: it receives one packet and returns one parsed proposal.
REVIEW_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "criterion_results", "findings", "suggestions"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "repair", "escalate"]},
        "criterion_results": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["criterion_id", "status", "evidence"],
                "properties": {
                    "criterion_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["pass", "fail"]},
                    "evidence": {"type": "string"},
                },
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["criterion_id", "severity", "file", "symbol", "evidence", "minimal_repair", "verification", "fingerprint_input"],
                "properties": {
                    "criterion_id": {"type": "string"}, "severity": {"type": "string", "enum": ["blocking"]},
                    "file": {"type": "string"}, "symbol": {"type": "string"}, "evidence": {"type": "string"},
                    "minimal_repair": {"type": "string"}, "verification": {"type": "string"}, "fingerprint_input": {"type": "string"},
                },
            },
        },
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
}


def _require_exact_review_payload(payload: object) -> dict[str, object]:
    """Reject malformed output without aliases, coercion, or text extraction."""
    if not isinstance(payload, dict) or set(payload) != {"verdict", "criterion_results", "findings", "suggestions"}:
        raise ValueError("local review structured JSON does not match schema")
    if payload["verdict"] not in {"pass", "repair", "escalate"}:
        raise ValueError("local review structured JSON does not match schema")
    if not isinstance(payload["suggestions"], list) or not all(isinstance(item, str) for item in payload["suggestions"]):
        raise ValueError("local review structured JSON does not match schema")
    fields = {
        "criterion_results": ({"criterion_id", "status", "evidence"}, {"status": {"pass", "fail"}}),
        "findings": ({"criterion_id", "severity", "file", "symbol", "evidence", "minimal_repair", "verification", "fingerprint_input"}, {"severity": {"blocking"}}),
    }
    for key, (required, enums) in fields.items():
        value = payload[key]
        if not isinstance(value, list):
            raise ValueError("local review structured JSON does not match schema")
        for item in value:
            if not isinstance(item, dict) or set(item) != required or not all(isinstance(part, str) for part in item.values()):
                raise ValueError("local review structured JSON does not match schema")
            if any(item[name] not in allowed for name, allowed in enums.items()):
                raise ValueError("local review structured JSON does not match schema")
    return payload


class LocalQwenAdapter:
    """Pure invocation boundary; callers alone may update the ledger or board."""
    def __init__(self, *, runner: Runner = subprocess.run, executable: str = "hermes", provider: str = LOCAL_QWEN_PROVIDER, model: str = LOCAL_QWEN_MODEL, hermes_home: Path | None = None, review_llm: Any | None = None) -> None:
        self.runner, self.executable, self.provider, self.model = runner, executable, provider, model
        self.hermes_home = Path(hermes_home or Path.home() / ".hermes" / "profiles" / "worker-code-local").resolve()
        self.review_llm = review_llm

    def _review_llm(self) -> Any:
        if self.review_llm is not None:
            return self.review_llm
        # Import only for the review invocation: standalone ledger operations
        # remain importable without Hermes core on PYTHONPATH.
        from agent.plugin_llm import PluginLlm
        self.review_llm = PluginLlm(plugin_id="local-first-orchestrator")
        return self.review_llm

    def _invoke_review(self, packet: str, artifact_dir: Path) -> ModelResult:
        response = self._review_llm().complete_structured(
            instructions=("Perform an isolated local code review. Return only the review proposal; "
                          "do not call tools or request additional context."),
            input=[{"type": "text", "text": packet}],
            json_schema=REVIEW_JSON_SCHEMA,
            json_mode=True,
            schema_name="local_first_review",
            provider=self.provider,
            model=self.model,
            temperature=0,
            purpose="local_first_review",
        )
        if getattr(response, "content_type", None) != "json":
            raise ValueError("local review did not return structured JSON")
        payload = _require_exact_review_payload(getattr(response, "parsed", None))
        artifact = artifact_dir / "review-result.json"
        artifact.write_text(json.dumps({"provider": self.provider, "model": self.model, "payload": payload}, sort_keys=True), encoding="utf-8")
        return ModelResult(payload, artifact, ())

    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> ModelResult:
        if purpose not in {"implementation", "review"}:
            raise ValueError("unsupported local model purpose")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if purpose == "review":
            return self._invoke_review(packet, artifact_dir)
        argv = [self.executable, "chat", "--provider", self.provider, "--model", self.model, "--query", packet, "--quiet"]
        kwargs = {"text": True, "capture_output": True, "timeout": 300, "check": False}
        if workdir is not None:
            attempt = Path(workdir).resolve()
            kwargs["cwd"] = str(attempt)
            kwargs["env"] = {**os.environ, "HERMES_HOME": str(self.hermes_home), "TERMINAL_CWD": str(attempt)}
            if purpose == "implementation":
                argv[2:2] = ["--toolsets", "file,terminal", "--in", str(attempt)]
        completed = self.runner(tuple(argv), **kwargs)
        artifact = artifact_dir / f"{purpose}-result.json"
        artifact.write_text(json.dumps({"argv": argv, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, sort_keys=True), encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"local model exited {completed.returncode}; see {artifact}")
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            # Hermes chat emits ordinary text after a successful implementation
            # tool run. The worktree diff, not the narration, is authoritative.
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("local model result must be an object")
        return ModelResult(payload, artifact, tuple(argv))
