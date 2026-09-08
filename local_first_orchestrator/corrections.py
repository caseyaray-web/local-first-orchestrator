"""Append-only, controller-owned post-acceptance correction plans.

Identity conventions (controller-owned; operators never invent IDs):

* ``finding_key`` = SHA-256 of the canonical (sorted-key) plan payload. A
  replayed or re-proposed finding with identical provenance derives the same
  key and therefore the same persisted plan: idempotent, no second ticket,
  no incremented ordinal. Two genuinely distinct findings have distinct keys.
* ``correction_plan_id`` = ``"correction-" + finding_key[:20]`` — stable per
  finding, stored on every derived row for provenance.
* Per-tranche correction ordinal: first persisted plan for a tranche is
  ``F1``, next ``F2``, ... The ordinal is assigned inside the ledger's
  IMMEDIATE write transaction (SQLite serializes concurrent writers across
  threads and processes) and durably guarded by a unique index on
  ``(tranche_id, ordinal)``, so concurrent requests cannot collide.
* Microticket IDs: single-ticket plan -> ``<tranche>-F<ordinal>``; multi-
  ticket plans -> ``<tranche>-F<ordinal>-<position>`` (1-based list order).
  Sibling dependencies are declared locally as ``candidate-N`` and remapped
  to the real correction ticket IDs at materialization, so readiness
  evaluates durable real IDs. The full correction provenance is stored on
  the ticket rows, so board contents are never an identity source.

Accepted predecessor semantics: a correction is runnable only when each
recorded predecessor has accepted evidence AND that exact commit is an
ancestor of the current target-tranche integration head (re-read at every
admission; planning-time heads are observations, not bases). Board "done"
alone is insufficient.

Correction lifecycle (never rewrites original completion facts):
planned -> materialized -> executed/accepted. ``lifecycle_status`` reports
unresolved correction work per tranche so a previously-complete tranche can
show open higher-level-review corrections without altering the historical
"all original children accepted" evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .decomposition import FeatureContract, Tranche, generated_card_payload
from .ledger import Ledger
from .ticket import MicroTicket, PatchBudget, VerificationProfile


_SOURCE_KINDS = frozenset({"tranche_review", "feature_review", "operator_review", "security_review"})


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class AcceptedPredecessor:
    ticket_id: str
    accepted_commit_sha: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", self.accepted_commit_sha):
            raise ValueError("accepted predecessor commit must be a SHA-1")
        if not self.ticket_id:
            raise ValueError("accepted predecessor ticket ID is required")


@dataclass(frozen=True)
class CorrectionTicketSpec:
    objective: str
    criterion_ids: tuple[str, ...]
    primary_symbol: str
    allowed_existing_files: tuple[str, ...]
    new_test_files: tuple[str, ...]
    forbidden_changes: tuple[str, ...]
    patch_budget: PatchBudget
    verification: VerificationProfile
    risk: str
    review_required: bool
    max_attempts: int
    dependencies: tuple[str, ...]
    relevant_symbols: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    non_goals: tuple[str, ...]
    red_evidence: str

    def payload(self) -> dict[str, object]:
        return {
            "objective": self.objective, "criterion_ids": list(self.criterion_ids),
            "primary_symbol": self.primary_symbol,
            "allowed_existing_files": list(self.allowed_existing_files),
            "new_test_files": list(self.new_test_files),
            "forbidden_changes": list(self.forbidden_changes),
            "patch_budget": self.patch_budget.__dict__,
            "verification": {"commands": [list(x) for x in self.verification.commands], "working_directory": self.verification.working_directory, "timeout_seconds": self.verification.timeout_seconds, "output_limit": self.verification.output_limit},
            "risk": self.risk, "review_required": self.review_required, "max_attempts": self.max_attempts,
            "dependencies": list(self.dependencies), "relevant_symbols": list(self.relevant_symbols),
            "acceptance_criteria": list(self.acceptance_criteria), "non_goals": list(self.non_goals), "red_evidence": self.red_evidence,
        }

    def ticket(self, ticket_id: str) -> MicroTicket:
        return MicroTicket(ticket_id, self.objective, self.criterion_ids, self.primary_symbol,
                           self.allowed_existing_files, self.forbidden_changes, self.patch_budget,
                           self.verification, self.risk, self.review_required, self.max_attempts,
                           self.dependencies, self.new_test_files)


@dataclass(frozen=True)
class SupplementalCorrectionPlan:
    feature_id: str
    tranche_id: str
    source_kind: str
    source_reference: str
    finding_fingerprint: str
    finding_summary: str
    predecessors: tuple[AcceptedPredecessor, ...]
    tickets: tuple[CorrectionTicketSpec, ...]
    repository_identity: str
    base_sha: str
    snapshot_hash: str

    def payload(self) -> dict[str, object]:
        return {
            "feature_id": self.feature_id, "tranche_id": self.tranche_id,
            "source_kind": self.source_kind, "source_reference": self.source_reference,
            "finding_fingerprint": self.finding_fingerprint, "finding_summary": self.finding_summary,
            "predecessors": [p.__dict__ for p in self.predecessors],
            "tickets": [t.payload() for t in self.tickets],
            "repository_identity": self.repository_identity, "base_sha": self.base_sha,
            "snapshot_hash": self.snapshot_hash,
        }


@dataclass(frozen=True)
class PersistedCorrectionPlan:
    correction_plan_id: str
    finding_key: str
    observed_integration_head: str


@dataclass(frozen=True)
class MaterializedCorrection:
    correction_plan_id: str
    ticket_id: str


class CorrectionService:
    """Creates and materializes corrections without mutating original plans."""

    def __init__(self, ledger: Ledger, repository: Path) -> None:
        self.ledger = ledger
        self.repository = Path(repository).resolve()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", *args), cwd=self.repository, text=True, capture_output=True, check=check)

    def _head(self, tranche_id: str) -> str:
        ref = f"refs/local-first/tranches/{tranche_id}/integration-head"
        result = self._git("show-ref", "--verify", "--hash", ref, check=False)
        if result.returncode:
            raise ValueError("target tranche integration head is unavailable")
        return self._git("rev-parse", "--verify", result.stdout.strip() + "^{commit}").stdout.strip()

    def _ancestor(self, ancestor: str, head: str) -> bool:
        return self._git("merge-base", "--is-ancestor", ancestor, head, check=False).returncode == 0

    def _feature_scope(self, feature_id: str, tranche_id: str) -> set[str]:
        rows = self.ledger.connection.execute(
            "SELECT allowed_files_json,create_files_json,new_test_files_json FROM tickets WHERE feature_id=? AND tranche_id=?", (feature_id, tranche_id)
        ).fetchall()
        scope: set[str] = set()
        for row in rows:
            scope.update(json.loads(row["allowed_files_json"]))
            scope.update(json.loads(row["create_files_json"]))
            scope.update(json.loads(row["new_test_files_json"]))
        if not scope:
            raise ValueError("authoritative parent scope is unavailable")
        return scope

    def _validate(self, plan: SupplementalCorrectionPlan, *, head: str | None = None) -> str:
        if plan.source_kind not in _SOURCE_KINDS:
            raise ValueError("unsupported correction source kind")
        if not all((plan.feature_id, plan.tranche_id, plan.source_reference, plan.finding_fingerprint, plan.finding_summary, plan.repository_identity, plan.base_sha, plan.snapshot_hash)):
            raise ValueError("correction provenance is incomplete")
        if plan.repository_identity != str(self.repository):
            raise ValueError("correction repository identity mismatch")
        tranche = self.ledger.connection.execute("SELECT feature_id,base_sha FROM tranches WHERE id=?", (plan.tranche_id,)).fetchone()
        if tranche is None or tranche["feature_id"] != plan.feature_id:
            raise ValueError("correction target feature/tranche mismatch")
        if not plan.predecessors or not plan.tickets:
            raise ValueError("correction requires predecessors and tickets")
        if len({p.ticket_id for p in plan.predecessors}) != len(plan.predecessors):
            raise ValueError("duplicate accepted predecessor")
        current = head or self._head(plan.tranche_id)
        parent_scope = self._feature_scope(plan.feature_id, plan.tranche_id)
        ids = [f"candidate-{i}" for i in range(1, len(plan.tickets) + 1)]
        for predecessor in plan.predecessors:
            evidence = self.ledger.connection.execute("SELECT accepted_commit_sha FROM accepted_evidence WHERE ticket_id=?", (predecessor.ticket_id,)).fetchone()
            if evidence is None or evidence["accepted_commit_sha"] != predecessor.accepted_commit_sha:
                raise ValueError("accepted predecessor evidence mismatch")
            if not self._ancestor(predecessor.accepted_commit_sha, current):
                raise ValueError("accepted predecessor is not in target integration lineage")
        for i, ticket in enumerate(plan.tickets):
            if not ticket.objective or not ticket.acceptance_criteria or not ticket.red_evidence:
                raise ValueError("correction ticket contract is incomplete")
            if not set(ticket.allowed_existing_files) | set(ticket.new_test_files) <= parent_scope:
                raise ValueError("correction scope exceeds authoritative parent scope")
            if not set(ticket.dependencies) <= set(ids):
                raise ValueError("correction sibling dependency is invalid")
            if ids[i] in ticket.dependencies:
                raise ValueError("correction ticket cannot depend on itself")
            if ticket.patch_budget.max_files < 1 or ticket.patch_budget.max_changed_lines < 1:
                raise ValueError("invalid correction patch budget")
        graph = {ids[i]: set(ticket.dependencies) for i, ticket in enumerate(plan.tickets)}
        visiting: set[str] = set(); visited: set[str] = set()
        def cycle(node: str) -> bool:
            if node in visiting: return True
            if node in visited: return False
            visiting.add(node)
            bad = any(cycle(dep) for dep in graph[node])
            visiting.remove(node); visited.add(node)
            return bad
        if any(cycle(node) for node in graph):
            raise ValueError("correction dependency cycle")
        return current

    def create_plan(self, plan: SupplementalCorrectionPlan) -> PersistedCorrectionPlan:
        head = self._validate(plan)
        payload = plan.payload()
        finding_key = _sha(payload)
        plan_id = "correction-" + finding_key[:20]
        with self.ledger._transaction() as conn:
            existing = conn.execute("SELECT correction_plan_id,observed_integration_head FROM supplemental_correction_plans WHERE finding_key=?", (finding_key,)).fetchone()
            if existing:
                return PersistedCorrectionPlan(existing["correction_plan_id"], finding_key, existing["observed_integration_head"])
            ordinal = int(conn.execute("SELECT COUNT(*) FROM supplemental_correction_plans WHERE tranche_id=?", (plan.tranche_id,)).fetchone()[0]) + 1
            conn.execute("INSERT INTO supplemental_correction_plans(correction_plan_id,finding_key,feature_id,tranche_id,source_kind,source_reference,finding_fingerprint,finding_summary,observed_integration_head,repository_identity,base_sha,snapshot_hash,plan_json,status,ordinal,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (plan_id, finding_key, plan.feature_id, plan.tranche_id, plan.source_kind, plan.source_reference, plan.finding_fingerprint, plan.finding_summary, head, plan.repository_identity, plan.base_sha, plan.snapshot_hash, _canonical(payload), "planned", ordinal, self.ledger._now()))
            for predecessor in plan.predecessors:
                conn.execute("INSERT INTO supplemental_correction_predecessors(correction_plan_id,ticket_id,accepted_commit_sha) VALUES (?,?,?)", (plan_id, predecessor.ticket_id, predecessor.accepted_commit_sha))
            self.ledger._append_event(conn, entity_type="tranche", entity_id=plan.tranche_id, event_type="supplemental_correction_planned", actor_id="controller", payload={"correction_plan_id": plan_id, "finding_key": finding_key, "source_kind": plan.source_kind})
        return PersistedCorrectionPlan(plan_id, finding_key, head)

    def _load(self, correction_plan_id: str) -> tuple[SupplementalCorrectionPlan, str]:
        row = self.ledger.connection.execute("SELECT * FROM supplemental_correction_plans WHERE correction_plan_id=?", (correction_plan_id,)).fetchone()
        if row is None:
            raise KeyError("correction plan")
        raw = json.loads(row["plan_json"])
        specs = tuple(CorrectionTicketSpec(
            objective=x["objective"], criterion_ids=tuple(x["criterion_ids"]), primary_symbol=x["primary_symbol"],
            allowed_existing_files=tuple(x["allowed_existing_files"]), new_test_files=tuple(x["new_test_files"]), forbidden_changes=tuple(x["forbidden_changes"]),
            patch_budget=PatchBudget(**x["patch_budget"]), verification=VerificationProfile(tuple(tuple(c) for c in x["verification"]["commands"]), x["verification"].get("working_directory", "."), int(x["verification"].get("timeout_seconds", 60)), int(x["verification"].get("output_limit", 20000))),
            risk=x["risk"], review_required=bool(x["review_required"]), max_attempts=int(x["max_attempts"]), dependencies=tuple(x["dependencies"]), relevant_symbols=tuple(x["relevant_symbols"]), acceptance_criteria=tuple(x["acceptance_criteria"]), non_goals=tuple(x["non_goals"]), red_evidence=x["red_evidence"]
        ) for x in raw["tickets"])
        plan = SupplementalCorrectionPlan(raw["feature_id"], raw["tranche_id"], raw["source_kind"], raw["source_reference"], raw["finding_fingerprint"], raw["finding_summary"], tuple(AcceptedPredecessor(**p) for p in raw["predecessors"]), specs, raw["repository_identity"], raw["base_sha"], raw["snapshot_hash"])
        return plan, str(row["finding_key"])

    def materialize(self, correction_plan_id: str) -> MaterializedCorrection:
        plan, finding_key = self._load(correction_plan_id)
        head = self._validate(plan)
        with self.ledger._transaction() as conn:
            existing = conn.execute("SELECT ticket_id FROM supplemental_correction_tickets WHERE correction_plan_id=? ORDER BY ordinal LIMIT 1", (correction_plan_id,)).fetchone()
            if existing:
                return MaterializedCorrection(correction_plan_id, existing["ticket_id"])
            ordinal_row = conn.execute("SELECT ordinal FROM supplemental_correction_plans WHERE correction_plan_id=?", (correction_plan_id,)).fetchone()
            if ordinal_row is None:
                raise KeyError("correction plan")
            ordinal = int(ordinal_row["ordinal"])
            ticket_ids = tuple(f"{plan.tranche_id}-F{ordinal}-{index}" if len(plan.tickets) > 1 else f"{plan.tranche_id}-F{ordinal}" for index in range(1, len(plan.tickets) + 1))
            now = self.ledger._now()
            sibling_map = {f"candidate-{i}": ticket_ids[i - 1] for i in range(1, len(ticket_ids) + 1)}
            for index, (ticket_id, spec) in enumerate(zip(ticket_ids, plan.tickets), start=1):
                if conn.execute("SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone():
                    raise RuntimeError("correction ticket identity collision")
                microticket = replace(spec, dependencies=tuple(sibling_map[d] for d in spec.dependencies)).ticket(ticket_id); contract = microticket.contract()
                conn.execute("INSERT INTO tickets(id,feature_id,tranche_id,title,objective,criterion_ids_json,primary_symbol,allowed_files_json,create_files_json,new_test_files_json,forbidden_changes_json,patch_budget_json,verification_json,risk,review_required,max_attempts,dependencies_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ticket_id, plan.feature_id, plan.tranche_id, ticket_id, contract["objective"], _canonical(contract["criterion_ids"]), contract["primary_symbol"], _canonical(contract["allowed_files"]), _canonical(contract.get("create_files", [])), _canonical(contract.get("new_test_files", [])), _canonical(contract["forbidden_changes"]), _canonical(contract["patch_budget"]), _canonical(contract["verification"]), contract["risk"], int(microticket.review_required), microticket.max_attempts, _canonical(contract["dependencies"]), "draft", now, now))
                conn.execute("INSERT INTO supplemental_correction_tickets(correction_plan_id,ticket_id,ordinal,admission_head) VALUES (?,?,?,NULL)", (correction_plan_id, ticket_id, index))
                for predecessor in plan.predecessors:
                    conn.execute("INSERT INTO correction_ticket_predecessors(correction_ticket_id,ticket_id,accepted_commit_sha) VALUES (?,?,?)", (ticket_id, predecessor.ticket_id, predecessor.accepted_commit_sha))
                feature = FeatureContract(plan.feature_id, plan.feature_id, "supplemental correction", (), (), (), (), plan.base_sha)
                tranche = Tranche(plan.tranche_id, 0, "supplemental correction", (), tuple(microticket.criterion_ids))
                projection = generated_card_payload(feature, tranche, microticket, repository_identity=plan.repository_identity, repo_base_sha=plan.base_sha, repo_snapshot_hash=plan.snapshot_hash)
                event_id = self.ledger._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="generated_microticket_created", actor_id="controller", to_state="draft", payload={"correction_plan_id": correction_plan_id, "finding_key": finding_key})
                self.ledger._enqueue_generated_create_projection_in_transaction(conn, ticket_id=ticket_id, event_id=event_id, payload=projection, idempotency_key=projection["projection_key"])
            conn.execute("UPDATE supplemental_correction_plans SET status='materialized',materialized_at=? WHERE correction_plan_id=?", (now, correction_plan_id))
            self.ledger._append_event(conn, entity_type="tranche", entity_id=plan.tranche_id, event_type="supplemental_correction_materialized", actor_id="controller", payload={"correction_plan_id": correction_plan_id, "integration_head": head, "ticket_ids": list(ticket_ids)})
        return MaterializedCorrection(correction_plan_id, ticket_ids[0])

    def activate(self, correction_ticket_id: str) -> str:
        """Bind a delivered correction to its current authoritative lineage."""
        rows = self.ledger.connection.execute("""
            SELECT e.id,b.acknowledged_at,b.external_task_id,b.terminal_error
            FROM supplemental_correction_tickets t
            JOIN events e ON e.entity_type='ticket' AND e.entity_id=t.ticket_id AND e.event_type='generated_microticket_created'
            LEFT JOIN board_projection_outbox b ON b.ticket_id=t.ticket_id AND b.event_id=e.id AND b.operation='create_microticket'
            WHERE t.ticket_id=?
        """, (correction_ticket_id,)).fetchall()
        if len(rows) != 1:
            raise ValueError("correction generated provenance is missing or ambiguous")
        row = rows[0]
        if row["acknowledged_at"] is None or not isinstance(row["external_task_id"], str) or not row["external_task_id"] or row["terminal_error"] is not None:
            raise ValueError("correction board materialization is incomplete")
        head = self.admission_base(correction_ticket_id)
        expected = (str(self.repository), head)
        with self.ledger._transaction() as conn:
            existing = conn.execute("SELECT repository_path,starting_sha FROM runtime_bindings WHERE ticket_id=?", (correction_ticket_id,)).fetchone()
            if existing is None:
                conn.execute("INSERT INTO runtime_bindings(ticket_id,repository_path,starting_sha,canonical_sha,ownership_verified,created_at) VALUES (?,?,?,?,1,?)", (correction_ticket_id, str(self.repository), head, self._git("rev-parse", "HEAD").stdout.strip(), self.ledger._now()))
            elif (existing["repository_path"], existing["starting_sha"]) != expected:
                raise ValueError("correction runtime binding conflicts with current authoritative lineage")
        result = self.ledger.admit_ticket_if_ready(correction_ticket_id)
        if result.status != "ready":
            raise ValueError("correction admission prerequisites are not satisfied")
        return head

    def admission_base(self, correction_ticket_id: str) -> str:
        row = self.ledger.connection.execute("""SELECT p.correction_plan_id,p.tranche_id,p.plan_json FROM supplemental_correction_tickets t JOIN supplemental_correction_plans p ON p.correction_plan_id=t.correction_plan_id WHERE t.ticket_id=?""", (correction_ticket_id,)).fetchone()
        if row is None:
            raise KeyError("correction ticket")
        plan, _ = self._load(row["correction_plan_id"])
        head = self._validate(plan)
        with self.ledger._transaction() as conn:
            conn.execute("""UPDATE supplemental_correction_tickets SET admission_head=? WHERE ticket_id=?""", (head, correction_ticket_id))
        return head

    def lifecycle_status(self, tranche_id: str) -> dict[str, Any]:
        """Explicit review/correction lifecycle read model for one tranche.

        Reports unresolved higher-level-review correction work without ever
        rewriting the historical "all original children accepted" facts:
        completion evidence and correction rows are separate, append-only.
        Returns a JSON-able view (nested dicts/lists), hence ``Any`` leaves.
        """
        plans = self.ledger.connection.execute(
            "SELECT p.correction_plan_id,p.source_kind,p.source_reference,p.finding_fingerprint,"
            "p.status,p.ordinal FROM supplemental_correction_plans p WHERE p.tranche_id=? ORDER BY p.ordinal", (tranche_id,)
        ).fetchall()
        tickets = self.ledger.connection.execute(
            """SELECT sct.correction_plan_id,sct.ticket_id,t.state FROM supplemental_correction_tickets sct
               JOIN supplemental_correction_plans p ON p.correction_plan_id=sct.correction_plan_id
               JOIN tickets t ON t.id=sct.ticket_id WHERE p.tranche_id=? ORDER BY sct.ordinal""", (tranche_id,)
        ).fetchall()
        ticket_states = {row["ticket_id"]: row["state"] for row in tickets}

        def plan_view(row) -> dict[str, object]:
            rows_t = [dict(r) for r in self.ledger.connection.execute(
                "SELECT sct.ticket_id FROM supplemental_correction_tickets sct WHERE sct.correction_plan_id=?", (row["correction_plan_id"],)).fetchall()]
            states = [ticket_states.get(r["ticket_id"]) for r in rows_t]
            if row["status"] == "planned":
                state = "planned"
            elif all(s == "done" for s in states) and any(s == "done" for s in states):
                state = "accepted"
            elif any(s == "done" for s in states):
                state = "partially_accepted"
            else:
                state = "materialized_open" if rows_t else "planned"
            return {"correction_plan_id": row["correction_plan_id"], "source_kind": row["source_kind"],
                    "source_reference": row["source_reference"], "finding_fingerprint": row["finding_fingerprint"],
                    "ordinal": row["ordinal"], "status": state,
                    "ticket_ids": [r["ticket_id"] for r in rows_t], "ticket_states": {r["ticket_id"]: s for r, s in zip(rows_t, states)}}

        views = [plan_view(row) for row in plans]
        open_count = sum(1 for view in views if view["status"] != "accepted")
        latest = self.ledger.latest_tranche_completion(tranche_id)
        rechecks = self.ledger.tranche_completion_rechecks(tranche_id)
        current_plan_ids = [view["correction_plan_id"] for view in views]
        current_ticket_ids = [ticket_id for view in views for ticket_id in view["ticket_ids"]]
        durable_current_recheck = False
        if rechecks and not open_count:
            candidate = rechecks[-1]
            durable_current_recheck = (
                json.loads(candidate["correction_plan_ids_json"]) == current_plan_ids
                and json.loads(candidate["accepted_ticket_ids_json"]) == current_ticket_ids
                and candidate["status"] == "recheck_passed"
            )
        if open_count:
            review_status = "open_corrections"
        elif not plans:
            review_status = "no_corrections"
        elif durable_current_recheck:
            review_status = "recheck_passed"
        else:
            review_status = "ready_for_recheck"
        return {"tranche_id": tranche_id,
                "correction_plans": len(plans),
                "unresolved_corrections": open_count,
                "review_status": review_status,
                "latest_completion": latest,
                "completion_rechecks": rechecks,
                "plans": views}

    def completion_authority(self, tranche_id: str) -> dict[str, Any]:
        """Return the durable completion authority used by successor gating."""
        h1 = self.ledger.tranche_completion(tranche_id)
        if h1 is None:
            return {"authorized": False, "kind": None, "completion": None,
                    "final_integration_sha": None, "status": "missing_h1"}
        schema = self.ledger.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tranche_completion_rechecks'"
        ).fetchone()
        if schema is None:
            return {"authorized": False, "kind": None, "completion": h1,
                    "final_integration_sha": None, "status": "completion_schema_missing"}
        status = self.lifecycle_status(tranche_id)
        latest = status["latest_completion"]
        if status["review_status"] == "no_corrections" and h1 is not None:
            return {"authorized": True, "kind": "h1", "completion": h1,
                    "final_integration_sha": h1["final_integration_sha"]}
        if status["review_status"] == "recheck_passed" and latest is not None:
            return {"authorized": True, "kind": "recheck", "completion": h1,
                    "final_integration_sha": latest["current_integration_sha"]}
        return {"authorized": False, "kind": None, "completion": h1,
                "final_integration_sha": None, "status": status["review_status"]}
