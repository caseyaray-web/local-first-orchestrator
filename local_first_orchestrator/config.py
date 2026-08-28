from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class OrchestratorConfig:
    database: Path
    poll_interval_seconds: int = 15
    local_worker_concurrency: int = 1

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OrchestratorConfig":
        source = raw.get("orchestrator", raw)
        if not isinstance(source, Mapping):
            raise ValueError("orchestrator configuration must be a mapping")
        raw_database = str(source.get("database", "")).strip()
        if not raw_database:
            raise ValueError("orchestrator.database is required")
        database = Path(raw_database).expanduser()
        if database.name == "kanban.db":
            raise ValueError("orchestrator database must be distinct from Hermes kanban.db")
        interval = int(source.get("poll_interval_seconds", 15))
        workers = int(source.get("local_worker_concurrency", 1))
        if interval < 1 or workers != 1:
            raise ValueError("poll_interval_seconds must be positive and local_worker_concurrency must be 1")
        return cls(database=database, poll_interval_seconds=interval, local_worker_concurrency=workers)

    @classmethod
    def from_file(cls, path: Path) -> "OrchestratorConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("configuration JSON must be an object")
        return cls.from_mapping(raw)
