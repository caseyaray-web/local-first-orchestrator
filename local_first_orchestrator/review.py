from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .ledger import Ledger
from .local_qwen import LocalQwenAdapter
from .states import CanonicalState
from .ticket import MicroTicket


@dataclass(frozen=True)
class ReviewFinding:
    criterion_id: str
    file: str
    symbol: str
    evidence: str
    minimal_repair: str
    verification: str
    fingerprint: str


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    criterion_results: tuple[dict[str, str], ...]
    findings: tuple[ReviewFinding, ...]
    suggestions: tuple[str, ...]
    raw: dict[str, object]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def failure_fingerprint(ticket_id: str, stage: str, criterion_id: str, file: str, symbol: str, error: str) -> str:
    """Hash stable defect identity, excluding volatile locations and timestamps."""
    normalized = error.lower()
    normalized = re.sub(r"\b(?:line|ln)\s*\d+\b", "", normalized)
    normalized = re.sub(r"\b\d{4}-\d\d-\d\d(?:[t ]\d\d:\d\d:\d\d(?:\.\d+)?z?)?\b", "", normalized)
    normalized = re.sub(r"(?:[a-z]:)?/(?:[^\s:]+/)+[^\s:]+", "<path>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" :,-")
    source = "\x1f".join((ticket_id, stage, criterion_id, file, symbol, normalized))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def normalize_review(payload: object, ticket: MicroTicket) -> ReviewResult:
    """Strictly accept only criterion- and allowlist-scoped repair blockers."""
    raw = payload if isinstance(payload, dict) else {}
    verdict = _text(raw.get("verdict"))
    if verdict not in {"pass", "repair", "escalate"}:
        raise ValueError("review verdict must be pass, repair, or escalate")
    criteria: list[dict[str, str]] = []
    for item in raw.get("criterion_results", []):
        if not isinstance(item, dict):
            continue
        criterion_id, status, evidence = _text(item.get("criterion_id")), _text(item.get("status")), _text(item.get("evidence"))
        if criterion_id in ticket.criterion_ids and status in {"pass", "fail"} and evidence:
            criteria.append({"criterion_id": criterion_id, "status": status, "evidence": evidence})
    if verdict == "pass":
        passed_criteria = {item["criterion_id"] for item in criteria if item["status"] == "pass"}
        if passed_criteria != set(ticket.criterion_ids):
            raise ValueError("pass review must include pass evidence for every criterion")
    findings: list[ReviewFinding] = []
    suggestions = [_text(item) for item in raw.get("suggestions", []) if _text(item)]
    for item in raw.get("findings", []):
        if not isinstance(item, dict):
            continue
        criterion_id, file, symbol = _text(item.get("criterion_id")), _text(item.get("file")), _text(item.get("symbol"))
        evidence, repair, verification, fingerprint_input = (_text(item.get(key)) for key in ("evidence", "minimal_repair", "verification", "fingerprint_input"))
        is_blocking = _text(item.get("severity")) == "blocking"
        valid = is_blocking and criterion_id in ticket.criterion_ids and file in ticket.allowed_files and bool(symbol and evidence and repair and verification and fingerprint_input)
        if not valid:
            suggestions.append("review finding downgraded: malformed or outside ticket criterion/file scope")
            continue
        findings.append(ReviewFinding(criterion_id, file, symbol, evidence, repair, verification, failure_fingerprint(ticket.ticket_id, "review", criterion_id, file, symbol, fingerprint_input)))
    return ReviewResult(verdict, tuple(criteria), tuple(findings), tuple(suggestions), raw)


class ReviewPacketBuilder:
    """Builds a fresh review context with only the review contract inputs."""
    def build(self, ticket: MicroTicket, *, diff: str, selected_files: Mapping[str, str], validation_evidence: str) -> str:
        if not diff.strip() or not selected_files or not validation_evidence.strip():
            raise ValueError("diff, selected files, and validation evidence are mandatory")
        sections = [
            ("ticket_contract", json.dumps(ticket.contract(), sort_keys=True)),
            ("current_diff", diff),
            *[(f"selected_file:{name}", text) for name, text in sorted(selected_files.items())],
            ("validation_evidence", validation_evidence[:4000]),
            ("review_instruction", "Return one JSON object matching verdict, criterion_results, findings, and suggestions. Review only existing criteria and allowed files."),
        ]
        return "\n\n".join(f"## {name}\n{value}" for name, value in sections)


class LocalReviewAdapter:
    def __init__(self, model: LocalQwenAdapter) -> None:
        self.model = model

    def review(self, ticket: MicroTicket, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> ReviewResult:
        # Review is packet-only: never grant the reviewer an editable worktree cwd.
        return normalize_review(self.model.invoke("review", packet, artifact_dir=artifact_dir).payload, ticket)


class SameTicketRepairCoordinator:
    """Ledger-only convergence policy; it never creates work or board tickets."""
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def apply(self, ticket_id: str, attempt_number: int, review: ReviewResult) -> str:
        state = CanonicalState(self.ledger.get_ticket(ticket_id)["state"])
        if state == CanonicalState.NEEDS_TRIAGE:
            return "triage"
        if state != CanonicalState.LOCAL_REVIEW:
            raise ValueError("review can only be applied from local_review")
        self.ledger.record_review(ticket_id, attempt_number, review)
        if review.verdict == "pass":
            # A clean review closes an implementation attempt; optional suggestions do not.
            if not review.suggestions:
                self.ledger.ensure_attempt(ticket_id, attempt_number)
            for result in review.criterion_results:
                if result["status"] == "pass":
                    self.ledger.set_criterion_status(ticket_id, result["criterion_id"], "accepted", evidence=result["evidence"])
            self.ledger.transition(ticket_id, CanonicalState.ACCEPTED, payload={"review_verdict": "pass"})
            return "accepted"
        if review.verdict == "escalate":
            self.ledger.transition(ticket_id, CanonicalState.NEEDS_TRIAGE, payload={"review_verdict": "escalate"})
            return "triage"
        if not review.findings:
            # A repair verdict with no valid blocking finding is non-blocking.
            self.ledger.transition(ticket_id, CanonicalState.ACCEPTED, payload={"review_verdict": "repair", "downgraded_to_suggestions": True})
            return "accepted"
        self.ledger.ensure_attempt(ticket_id, attempt_number)
        repeated = False
        for finding in review.findings:
            occurrence = self.ledger.record_review_finding(ticket_id, attempt_number, finding)
            repeated = repeated or occurrence >= 2
        max_attempts = int(self.ledger.get_ticket(ticket_id)["max_attempts"])
        if repeated or attempt_number >= max_attempts:
            self.ledger.transition(ticket_id, CanonicalState.NEEDS_TRIAGE, payload={"review_verdict": "repair", "repeated_fingerprint": repeated, "attempt_number": attempt_number})
            return "triage"
        self.ledger.transition(ticket_id, CanonicalState.REPAIRING, payload={"review_verdict": "repair", "attempt_number": attempt_number})
        return "repair"
