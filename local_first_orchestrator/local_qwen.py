from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ModelResult:
    payload: dict[str, object]
    artifact_path: Path
    argv: tuple[str, ...]


Runner = Callable[..., subprocess.CompletedProcess[str]]


class LocalQwenAdapter:
    """Pure invocation boundary; callers alone may update the ledger or board."""
    def __init__(self, *, runner: Runner = subprocess.run, executable: str = "hermes", provider: str = "custom:lm-studio", model: str = "qwen3.8-27b@iq3_s") -> None:
        self.runner, self.executable, self.provider, self.model = runner, executable, provider, model

    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> ModelResult:
        if purpose not in {"implementation", "review"}:
            raise ValueError("unsupported local model purpose")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        argv = (self.executable, "chat", "--provider", self.provider, "--model", self.model, "--query", packet, "--quiet")
        kwargs = {"text": True, "capture_output": True, "timeout": 300, "check": False}
        if workdir is not None:
            kwargs["cwd"] = str(Path(workdir).resolve())
        completed = self.runner(argv, **kwargs)
        artifact = artifact_dir / f"{purpose}-result.json"
        artifact.write_text(json.dumps({"argv": argv, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, sort_keys=True), encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"local model exited {completed.returncode}; see {artifact}")
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            # Hermes chat emits ordinary text after a successful implementation
            # tool run. The worktree diff, not the narration, is authoritative.
            if purpose == "implementation":
                payload = {}
            else:
                raise ValueError("local model did not return JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("local model result must be an object")
        return ModelResult(payload, artifact, argv)
