from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .ticket import MicroTicket


class ContextBudgetError(ValueError):
    pass


@dataclass(frozen=True)
class ContextPacket:
    text: str
    manifest: dict[str, object]


@dataclass(frozen=True)
class ContextPacketArtifacts:
    packet_path: Path
    manifest_path: Path
    packet_hash: str
    manifest_hash: str


class ContextPacketBuilder:
    def __init__(self, target_tokens: int = 20_000, max_tokens: int = 30_000) -> None:
        if not 1 <= target_tokens <= max_tokens:
            raise ValueError("context budgets must be positive and ordered")
        self.target_tokens, self.max_tokens = target_tokens, max_tokens

    @staticmethod
    def _tokens(text: str) -> int:
        return max(1, (len(text) + 3) // 4)

    def build(self, ticket: MicroTicket, selected_files: Mapping[str, str], *, repository_rules: str, failure_evidence: str = "") -> ContextPacket:
        required = [("repository_rules", "rules", repository_rules), ("ticket_contract", "ledger", json.dumps(ticket.contract(), sort_keys=True))]
        if not repository_rules.strip() or not selected_files:
            raise ContextBudgetError("rules and selected source/test files are mandatory")
        sections = required + [("source_file", name, text) for name, text in sorted(selected_files.items())]
        if failure_evidence:
            sections.append(("failure_evidence", "compact", failure_evidence[:4000]))
        built, manifest_sections, total, truncated = [], [], 0, []
        for kind, source, content in sections:
            estimate = self._tokens(content)
            if total + estimate > self.max_tokens:
                if kind in {"repository_rules", "ticket_contract", "source_file"}:
                    raise ContextBudgetError(f"mandatory {kind} cannot fit safely")
                truncated.append(kind)
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()
            built.append(f"## {kind}: {source}\n{content}")
            manifest_sections.append({"kind": kind, "source": source, "content_hash": digest, "token_estimate": estimate})
            total += estimate
        manifest = {"ticket_id": ticket.ticket_id, "purpose": "implementation", "sections": manifest_sections,
                    "total_token_estimate": total, "target_tokens": self.target_tokens, "max_tokens": self.max_tokens,
                    "truncated_sections": truncated}
        return ContextPacket("\n\n".join(built), manifest)

    def build_from_repository(self, ticket: MicroTicket, repository: Path, *, repository_rules: str, index: object | None = None, failure_evidence: str = "") -> ContextPacket:
        """Build a compact packet from AST-selected symbols, not a repository dump."""
        from .symbols import SymbolIndex

        symbol_index = index if isinstance(index, SymbolIndex) else SymbolIndex(Path(repository))
        selection = symbol_index.select_for_ticket(ticket)
        if selection.scope_unverified:
            raise ContextBudgetError("primary symbol cannot be determined safely")
        selected = {selection.primary.path: selection.primary.text}
        for symbol in (*selection.dependencies, *selection.callers, *selection.tests):
            selected.setdefault(symbol.path, "")
            selected[symbol.path] += ("\n" if selected[symbol.path] else "") + symbol.text
        packet = self.build(
            ticket, selected,
            repository_rules=(repository_rules + "\nNew test files are absent from the base and must be created exactly at: " + ", ".join(ticket.new_test_files)) if ticket.new_test_files else repository_rules,
            failure_evidence=failure_evidence,
        )
        packet.manifest["scope_verification"] = "verified"
        for section in packet.manifest["sections"]:
            if section["kind"] == "source_file":
                section["kind"] = "source_symbol"
        return packet

    def write_artifacts(self, packet: ContextPacket, *, artifact_root: Path) -> ContextPacketArtifacts:
        """Persist a built packet and its manifest under deterministic safe filenames."""
        packet_bytes = packet.text.encode("utf-8")
        manifest_text = json.dumps(packet.manifest, sort_keys=True, separators=(",", ":"))
        manifest_bytes = manifest_text.encode("utf-8")
        packet_hash = hashlib.sha256(packet_bytes).hexdigest()
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        ticket_id = str(packet.manifest.get("ticket_id", "ticket"))
        safe_ticket_id = re.sub(r"[^A-Za-z0-9._-]+", "-", ticket_id).strip(".-") or "ticket"
        artifact_root = Path(artifact_root)
        artifact_root.mkdir(parents=True, exist_ok=True)
        packet_path = artifact_root / f"{safe_ticket_id}-{packet_hash[:16]}.packet.txt"
        manifest_path = artifact_root / f"{safe_ticket_id}-{manifest_hash[:16]}.manifest.json"
        packet_path.write_bytes(packet_bytes)
        manifest_path.write_bytes(manifest_bytes)
        return ContextPacketArtifacts(packet_path, manifest_path, packet_hash, manifest_hash)
