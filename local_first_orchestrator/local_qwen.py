from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ModelResult:
    payload: dict[str, object]
    artifact_path: Path
    argv: tuple[str, ...]


class MalformedReviewOutput(ValueError):
    """Strictly rejected response with retained, non-verdict provenance."""
    def __init__(self, message: str, artifact_path: Path) -> None:
        super().__init__(message)
        self.artifact_path = artifact_path


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
    def __init__(self, *, runner: Runner = subprocess.run, executable: str = "hermes", provider: str = LOCAL_QWEN_PROVIDER, model: str = LOCAL_QWEN_MODEL, hermes_home: Path | None = None, review_llm: Any | None = None, implementation_timeout_seconds: int = 300, review_timeout_seconds: int = 300) -> None:
        if not isinstance(implementation_timeout_seconds, int) or not 1 <= implementation_timeout_seconds <= 21_600:
            raise ValueError("invalid_implementation_timeout_seconds")
        if not isinstance(review_timeout_seconds, int) or not 1 <= review_timeout_seconds <= 21_600:
            raise ValueError("invalid_review_timeout_seconds")
        self.runner, self.executable, self.provider, self.model = runner, executable, provider, model
        self.hermes_home = Path(hermes_home or Path.home() / ".hermes" / "profiles" / "worker-code-local").resolve()
        self.review_llm = review_llm
        self.implementation_timeout_seconds = implementation_timeout_seconds
        self.review_timeout_seconds = review_timeout_seconds
        self.review_provider, self.review_model = provider, model

    def _invoke_review(self, packet: str, artifact_dir: Path) -> ModelResult:
        if self.review_llm is not None:
            # Injection is intentionally in-process so unit tests never need a
            # configured Hermes profile. Production always uses the worker.
            response = self.review_llm.complete_structured(
                instructions=("Perform an isolated local code review. Return only the review proposal; "
                              "do not call tools or request additional context."),
                input=[{"type": "text", "text": packet}],
                json_schema=REVIEW_JSON_SCHEMA,
                json_mode=True,
                schema_name="local_first_review",
                provider=self.review_provider,
                model=self.review_model,
                temperature=0,
                purpose="local_first_review",
                timeout=self.review_timeout_seconds,
            )
            content_type, parsed, argv = getattr(response, "content_type", None), getattr(response, "parsed", None), ()
        else:
            # A review is one isolated structured inference process, rooted at
            # the narrow worker profile. No environment is mutated globally;
            # no project workdir or agent/tool loop is passed to the child.
            argv = (sys.executable, "-m", "local_first_orchestrator.review_worker")
            completed = self.runner(
                argv,
                input=json.dumps({"packet": packet, "provider": self.review_provider, "model": self.review_model, "timeout_seconds": self.review_timeout_seconds}),
                text=True,
                capture_output=True,
                timeout=self.review_timeout_seconds,
                check=False,
                cwd=str(Path(__file__).resolve().parent.parent),
                env={
                    **{key: value for key, value in os.environ.items() if key not in {"TERMINAL_CWD", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"}},
                    "HERMES_HOME": str(self.hermes_home),
                },
            )
            if completed.returncode:
                raise RuntimeError(f"local structured review exited {completed.returncode}: {completed.stderr.strip()}")
            try:
                result = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise ValueError("local review did not return structured JSON") from exc
            if not isinstance(result, dict) or set(result) != {"content_type", "parsed"}:
                raise ValueError("local review did not return structured JSON")
            content_type, parsed = result["content_type"], result["parsed"]
        raw_artifact = artifact_dir / ("review-malformed-" + __import__("hashlib").sha256(json.dumps({"content_type": content_type, "parsed": parsed}, sort_keys=True, default=str).encode()).hexdigest() + ".json")
        try:
            if content_type != "json":
                raise ValueError("local review did not return structured JSON")
            payload = _require_exact_review_payload(parsed)
        except ValueError as exc:
            raw_artifact.write_text(json.dumps({"content_type": content_type, "parsed": parsed}, sort_keys=True, default=str), encoding="utf-8")
            raise MalformedReviewOutput("local review did not return structured JSON", raw_artifact) from exc
        artifact = artifact_dir / "review-result.json"
        artifact.write_text(json.dumps({"provider": self.review_provider, "model": self.review_model, "payload": payload}, sort_keys=True), encoding="utf-8")
        return ModelResult(payload, artifact, tuple(argv))

    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> ModelResult:
        if purpose not in {"implementation", "review"}:
            raise ValueError("unsupported local model purpose")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if purpose == "review":
            return self._invoke_review(packet, artifact_dir)
        argv = [self.executable, "chat", "--provider", self.provider, "--model", self.model, "--query", packet, "--quiet"]
        kwargs = {"text": True, "capture_output": True, "timeout": self.implementation_timeout_seconds, "check": False}
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
