"""Hermes profile discovery for operator-facing role routing.

The Local First config stores profile/provider/model provenance, but Hermes remains
the source of truth for profile definitions. Dashboard/CLI callers therefore
resolve a selected profile through the Hermes CLI at save time rather than
asking operators to maintain coupled provider/model strings manually.
"""
from __future__ import annotations

import re
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .operator_config import ModelRegistration


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class HermesProfile:
    profile: str
    provider: str
    model: str
    gateway: str | None = None
    alias: str | None = None

    def registration(self) -> ModelRegistration:
        return ModelRegistration(self.profile, self.provider, self.model)

    def as_json(self) -> dict[str, str | None]:
        return {
            "profile": self.profile,
            "provider": self.provider,
            "model": self.model,
            "gateway": self.gateway,
            "alias": self.alias,
        }


def _run(runner: Runner, argv: Sequence[str], *, timeout_seconds: int) -> str:
    try:
        completed = runner(tuple(argv), text=True, capture_output=True, timeout=timeout_seconds)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Hermes profile discovery failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "Hermes profile command failed").strip()
        raise RuntimeError(detail[:500])
    return completed.stdout


def list_profile_names(*, executable: str = "hermes", runner: Runner = subprocess.run, timeout_seconds: int = 15) -> tuple[str, ...]:
    output = _run(runner, (executable, "profile", "list"), timeout_seconds=timeout_seconds)
    names: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if (
            not line
            or line.startswith("Profile")
            or line.startswith("⚠")
            or set(line) <= {"─", "-", " ", "◆"}
        ):
            continue
        line = line.lstrip("◆").strip()
        match = re.match(r"(?P<name>[^\s]+)\s+[^\s]+", line)
        if not match:
            continue
        name = match.group("name").strip()
        if name and name not in names:
            names.append(name)
    if not names:
        raise RuntimeError("Hermes profile discovery returned no profiles")
    return tuple(names)


def show_profile(profile: str, *, executable: str = "hermes", runner: Runner = subprocess.run, timeout_seconds: int = 15) -> HermesProfile:
    profile = profile.strip()
    if not profile or len(profile) > 240 or any(ch.isspace() for ch in profile):
        raise ValueError("Hermes profile name must be one bounded token")
    output = _run(runner, (executable, "profile", "show", profile), timeout_seconds=timeout_seconds)
    model_match = re.search(r"^Model:\s+(?P<model>.+?)\s+\((?P<provider>[^()]+)\)\s*$", output, re.MULTILINE)
    if model_match is None:
        raise RuntimeError(f"Hermes profile {profile!r} did not expose model/provider metadata")
    gateway_match = re.search(r"^Gateway:\s+(?P<gateway>\S+)\s*$", output, re.MULTILINE)
    alias_match = re.search(r"^Alias:\s+(?P<alias>[^→\n]+)", output, re.MULTILINE)
    return HermesProfile(
        profile=profile,
        provider=model_match.group("provider").strip(),
        model=model_match.group("model").strip(),
        gateway=gateway_match.group("gateway").strip() if gateway_match else None,
        alias=alias_match.group("alias").strip() if alias_match else None,
    )


def review_profile_identity(profile: str, *, executable: str = "hermes", runner: Runner = subprocess.run, timeout_seconds: int = 15, profile_root: Path | None = None) -> dict[str, object]:
    """Resolve one Hermes review profile live and fingerprint only non-secret routing files."""
    resolved = show_profile(profile, executable=executable, runner=runner, timeout_seconds=timeout_seconds)
    root = Path(profile_root).resolve() if profile_root is not None else Path.home() / ".hermes" / "profiles" / profile
    routing_files: dict[str, str | None] = {}
    for name in ("profile.yaml", "config.yaml"):
        path = root / name
        routing_files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    identity: dict[str, object] = {
        "profile": resolved.profile,
        "provider": resolved.provider,
        "model": resolved.model,
        "routing_files": routing_files,
    }
    identity["fingerprint"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return identity


def discover_profiles(*, executable: str = "hermes", runner: Runner = subprocess.run, timeout_seconds: int = 15) -> tuple[HermesProfile, ...]:
    return tuple(show_profile(name, executable=executable, runner=runner, timeout_seconds=timeout_seconds) for name in list_profile_names(executable=executable, runner=runner, timeout_seconds=timeout_seconds))


def resolve_registration(profile: str, *, executable: str = "hermes", runner: Runner = subprocess.run, timeout_seconds: int = 15) -> ModelRegistration:
    return show_profile(profile, executable=executable, runner=runner, timeout_seconds=timeout_seconds).registration()
