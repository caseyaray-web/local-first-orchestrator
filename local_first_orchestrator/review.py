from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .ticket import TicketContract

_SHA = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class ReviewFinding:
    criterion_id: str
    file: str
    symbol: str
    evidence: str
    minimal_repair: str
    verification: str

    def __post_init__(self) -> None:
        if any(not isinstance(getattr(self, name), str) or not getattr(self, name).strip() for name in ("criterion_id", "file", "symbol", "evidence", "minimal_repair", "verification")):
            raise ValueError("review finding fields must be non-empty strings")


@dataclass(frozen=True)
class ReviewResult:
    ticket_id: str
    contract_hash: str
    candidate_sha: str
    verdict: str
    criterion_results: tuple[Mapping[str, str], ...]
    findings: tuple[ReviewFinding, ...]
    escalation_reason: str | None = None


def failure_fingerprint(result: ReviewResult) -> str | None:
    if type(result) is not ReviewResult:
        raise ValueError("review result is required")
    if result.verdict != "repair":
        return None
    source = "\x1f".join(
        [result.ticket_id, result.contract_hash, result.candidate_sha]
        + ["\x1e".join((finding.criterion_id, finding.file, finding.symbol, _normalized_text(finding.evidence), finding.minimal_repair, finding.verification)) for finding in result.findings]
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def normalize_review(payload: object, contract: TicketContract, *, expected_candidate_sha: str) -> ReviewResult:
    if type(contract) is not TicketContract:
        raise ValueError("ticket contract is required")
    if not isinstance(expected_candidate_sha, str) or not _SHA.fullmatch(expected_candidate_sha):
        raise ValueError("expected candidate must be a full lowercase Git SHA")
    if not isinstance(payload, Mapping):
        raise ValueError("review payload must be an object")
    candidate_sha = payload.get("candidate_sha")
    if payload.get("ticket_id") != contract.ticket_id or payload.get("contract_hash") != contract.contract_hash or candidate_sha != expected_candidate_sha:
        raise ValueError("review candidate identity mismatch")
    verdict = payload.get("verdict")
    if verdict not in {"pass", "repair", "escalate"}:
        raise ValueError("invalid review verdict")
    raw_results = payload.get("criterion_results", ())
    if not isinstance(raw_results, (list, tuple)):
        raise ValueError("malformed criterion results")
    results: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, Mapping):
            raise ValueError("malformed criterion result")
        criterion_id, status, evidence = item.get("criterion_id"), item.get("status"), item.get("evidence")
        if criterion_id not in contract.criterion_ids or criterion_id in seen or status not in {"pass", "fail"} or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("unknown, duplicate, or malformed review criterion")
        seen.add(criterion_id)
        results.append(MappingProxyType({"criterion_id": criterion_id, "status": status, "evidence": evidence.strip()}))
    raw_findings = payload.get("findings", ())
    if not isinstance(raw_findings, (list, tuple)):
        raise ValueError("malformed review findings")
    findings: list[ReviewFinding] = []
    for item in raw_findings:
        if not isinstance(item, Mapping):
            raise ValueError("malformed review finding")
        if item.get("severity", "blocking") != "blocking":
            raise ValueError("repair findings must be explicitly blocking")
        finding = ReviewFinding(
            criterion_id=item.get("criterion_id"), file=item.get("file"), symbol=item.get("symbol"),
            evidence=item.get("evidence"), minimal_repair=item.get("minimal_repair"), verification=item.get("verification"),
        )
        if finding.criterion_id not in contract.criterion_ids or finding.file not in contract.allowed_paths:
            raise ValueError("review finding outside contract scope")
        if not any(result["criterion_id"] == finding.criterion_id and result["status"] == "fail" for result in results):
            raise ValueError("blocking finding must match failed criterion evidence")
        findings.append(finding)
    if verdict == "pass" and (seen != set(contract.criterion_ids) or any(result["status"] != "pass" for result in results) or findings):
        raise ValueError("pass requires evidence for every criterion and no blockers")
    if verdict == "repair":
        if not findings:
            raise ValueError("repair requires a valid blocking finding")
        covered = {finding.criterion_id for finding in findings}
        if any(result["status"] == "fail" and result["criterion_id"] not in covered for result in results):
            raise ValueError("failed criterion is missing a valid blocker")
    reason = payload.get("reason") if verdict == "escalate" else None
    if verdict == "escalate" and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("escalation requires an actionable reason")
    return ReviewResult(contract.ticket_id, contract.contract_hash, expected_candidate_sha, verdict, tuple(results), tuple(findings), reason.strip() if isinstance(reason, str) else None)


def validate_review_identity(result: ReviewResult, expected: Mapping[str, str]) -> bool:
    if type(result) is not ReviewResult or not isinstance(expected, Mapping):
        return False
    return all(expected.get(field) == getattr(result, field) for field in ("ticket_id", "contract_hash", "candidate_sha"))


class ReviewPacketBuilder:
    @staticmethod
    def build(*, candidate: Mapping[str, object], checks: tuple[Mapping[str, object], ...], contract: TicketContract) -> Mapping[str, object]:
        if type(contract) is not TicketContract or not isinstance(candidate, Mapping) or not isinstance(checks, tuple):
            raise ValueError("candidate, checks, and ticket contract are required")
        candidate_sha = candidate.get("sha")
        if not isinstance(candidate_sha, str) or not _SHA.fullmatch(candidate_sha):
            raise ValueError("candidate SHA must be a full lowercase Git SHA")
        if any(not isinstance(check, Mapping) for check in checks):
            raise ValueError("review checks must be immutable-compatible objects")
        frozen_checks = tuple(MappingProxyType(dict(check)) for check in checks)
        return MappingProxyType({
            "ticket_id": contract.ticket_id,
            "contract_hash": contract.contract_hash,
            "candidate_sha": candidate_sha,
            "candidate": MappingProxyType(dict(candidate)),
            "checks": frozen_checks,
        })


def _normalized_text(value: str) -> str:
    value = re.sub(r"\b(?:line|ln)\s*\d+\b", "", value.lower())
    return re.sub(r"\s+", " ", value).strip(" :,-")
