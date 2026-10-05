"""Pure, bounded planning request and proposal contracts. No model or runtime effects."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .decomposition import DecompositionPlan, PlanValidator, TranchePlan
from .ticket import TicketContract, contract_payload, parse_contract

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Parser resource ceilings; policy values may be lower but never higher.
HARD_CAPS = {"max_context_tokens": 1_000_000, "max_patch_files": 2048,
             "max_patch_lines": 1_000_000, "max_attempts": 10_000,
             "verification_timeout_seconds": 3600, "verification_output_limit": 16_000_000,
             "max_tranches": 128, "max_tickets": 2048,
             "max_payload_bytes": 4_000_000, "max_json_depth": 64}


class PlannerError(ValueError):
    """Invalid planning request or untrusted proposal."""


@dataclass(frozen=True)
class TrancheSemantics:
    tranche_id: str
    objective: str
    non_goals: tuple[str, ...]

    def __post_init__(self):
        if type(self.tranche_id) is not str or not SAFE_ID.fullmatch(self.tranche_id) or type(self.objective) is not str or not self.objective.strip() or len(self.objective) > 4096 or type(self.non_goals) is not tuple or len(self.non_goals) > 1024 or any(type(x) is not str or not x.strip() or len(x) > 4096 for x in self.non_goals) or not _valid_unicode((self.tranche_id, self.objective, self.non_goals)):
            raise ValueError("invalid tranche semantics")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _path_ok(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    bits = path.split("/")
    return all(bit not in ("", ".", "..") for bit in bits)


def _valid_unicode(value: object) -> bool:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return True
        except UnicodeEncodeError:
            return False
    if isinstance(value, dict):
        return all(_valid_unicode(k) and _valid_unicode(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_valid_unicode(x) for x in value)
    if isinstance(value, tuple):
        return all(_valid_unicode(x) for x in value)
    return True


def _json_depth(value: object, depth: int = 0) -> int:
    if depth > 64:
        return depth
    if isinstance(value, dict):
        return max([depth, *(_json_depth(k, depth + 1) for k in value), *(_json_depth(v, depth + 1) for v in value.values())])
    if isinstance(value, list):
        return max([depth, *(_json_depth(v, depth + 1) for v in value)])
    return depth


@dataclass(frozen=True)
class PlanningRequest:
    board_id: str
    anchor_id: str
    repository_identity: str
    base_sha: str
    snapshot_hash: str
    root_contract_hash: str
    expected_criteria: frozenset[str]
    authorized_paths: frozenset[str]
    max_tranches: int
    max_tickets: int
    max_context_tokens: int
    max_patch_files: int
    max_patch_lines: int
    max_attempts: int
    verification_commands: tuple[tuple[str, ...], ...]
    verification_timeout_seconds: int
    verification_output_limit: int
    max_payload_bytes: int
    max_json_depth: int
    objective: str
    non_goals: tuple[str, ...]
    criterion_statements: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for name in ("board_id", "anchor_id", "repository_identity"):
            if type(getattr(self, name)) is not str or not getattr(self, name).strip():
                raise ValueError(f"{name} must be explicit and non-empty")
        if any(type(x) is not str or not rx.fullmatch(x) for x, rx in ((self.base_sha, SHA40), (self.snapshot_hash, SHA64), (self.root_contract_hash, SHA64))):
            raise ValueError("base and evidence hashes must be lowercase full SHA-1/SHA-256")
        if type(self.expected_criteria) is not frozenset or not self.expected_criteria or any(type(x) is not str or not x.strip() for x in self.expected_criteria):
            raise ValueError("expected criteria must be a non-empty immutable set")
        if type(self.authorized_paths) is not frozenset or not self.authorized_paths or len(self.authorized_paths) > 2048 or any(not _path_ok(x) or len(x)>4096 for x in self.authorized_paths):
            raise ValueError("authorized paths must be a non-empty safe immutable set")
        limits = (self.max_tranches, self.max_tickets, self.max_context_tokens, self.max_patch_files,
                  self.max_patch_lines, self.max_attempts, self.verification_timeout_seconds,
                  self.verification_output_limit, self.max_payload_bytes, self.max_json_depth)
        if any(type(v) is not int or v <= 0 for v in limits):
            raise ValueError("all planning policy limits must be explicit positive finite integers")
        for name, cap in HARD_CAPS.items():
            if getattr(self, name) > cap:
                raise ValueError(f"{name} exceeds parser hard ceiling")
        if type(self.objective) is not str or type(self.non_goals) is not tuple or len(self.non_goals)>1024 or any(type(x) is not str or not x.strip() or len(x)>4096 for x in self.non_goals) or type(self.criterion_statements) is not tuple or len(self.criterion_statements)>16384 or any(type(x) is not tuple or len(x)!=2 or any(type(y) is not str or not y.strip() or len(y)>4096 for y in x) for x in self.criterion_statements):
            raise ValueError("request text fields have invalid types, counts, or lengths")
        # Validate trusted nested commands before traversing them for string constraints.
        if not isinstance(self.verification_commands, tuple) or not self.verification_commands:
            raise ValueError("trusted verification commands are required")
        from .ticket import VerificationProfile
        try:
            VerificationProfile(self.verification_commands, self.verification_timeout_seconds,
                                self.verification_output_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid trusted verification policy") from exc
        strings=(self.board_id,self.anchor_id,self.repository_identity,self.base_sha,self.snapshot_hash,self.root_contract_hash,self.objective,*self.non_goals,*self.expected_criteria,*self.authorized_paths,*(y for pair in self.criterion_statements for y in pair),*(arg for cmd in self.verification_commands for arg in cmd))
        if any(type(x) is not str or len(x)>4096 for x in strings) or not _valid_unicode(strings):
            raise ValueError("request text exceeds finite string limit")
        if not self.objective.strip():
            raise ValueError("request objective/non-goals must be explicit")
        if {x[0] for x in self.criterion_statements} != self.expected_criteria or len(self.criterion_statements) != len(self.expected_criteria):
            raise ValueError("criterion statements must exactly match expected criteria")
        if self.max_tranches > 128 or self.max_tickets > 2048 or self.max_payload_bytes > 4_000_000 or self.max_json_depth > 64:
            raise ValueError("planning policy exceeds parser hard ceilings")

    @property
    def identity(self) -> str:
        return _sha(request_payload(self))


def request_payload(request: PlanningRequest) -> dict:
    return {"board_id": request.board_id, "anchor_id": request.anchor_id,
            "repository_identity": request.repository_identity, "base_sha": request.base_sha,
            "snapshot_hash": request.snapshot_hash, "root_contract_hash": request.root_contract_hash,
            "expected_criteria": sorted(request.expected_criteria), "authorized_paths": sorted(request.authorized_paths),
            "max_tranches": request.max_tranches, "max_tickets": request.max_tickets,
            "max_context_tokens": request.max_context_tokens, "max_patch_files": request.max_patch_files,
            "max_patch_lines": request.max_patch_lines, "max_attempts": request.max_attempts,
            "verification_commands": [list(c) for c in request.verification_commands],
            "verification_timeout_seconds": request.verification_timeout_seconds,
            "verification_output_limit": request.verification_output_limit,
            "max_payload_bytes": request.max_payload_bytes, "max_json_depth": request.max_json_depth,
            "objective": request.objective, "non_goals": list(request.non_goals),
            "criterion_statements": [list(x) for x in request.criterion_statements]}


@dataclass(frozen=True)
class PlanProposal:
    request_identity: str
    plan: DecompositionPlan
    tranche_semantics: tuple[TrancheSemantics, ...]

    def __post_init__(self):
        if type(self.request_identity) is not str or not SHA64.fullmatch(self.request_identity) or type(self.plan) is not DecompositionPlan or type(self.tranche_semantics) is not tuple or len(self.tranche_semantics) != len(self.plan.tranches) or any(type(s) is not TrancheSemantics for s in self.tranche_semantics) or tuple(s.tranche_id for s in self.tranche_semantics) != tuple(t.tranche_id for t in self.plan.tranches):
            raise ValueError("tranche semantics must exactly match tranche order")

    @property
    def proposal_hash(self) -> str:
        return _sha(proposal_payload(self))


def proposal_payload(proposal: PlanProposal) -> dict:
    plan = proposal.plan
    return {"schema_version": 1, "request_identity": proposal.request_identity,
            "execution_order": list(topological_ticket_order(plan)),
            "plan": {"plan_id": plan.plan_id, "schema_version": plan.schema_version,
                     "tranches": [{"tranche_id": t.tranche_id, "ordinal": t.ordinal,
                                   "objective": s.objective, "non_goals": list(s.non_goals),
                                   "criterion_ids": list(t.criterion_ids),
                                   "tickets": [contract_payload(ticket) for ticket in t.tickets]} for t, s in zip(plan.tranches, proposal.tranche_semantics)],
                     "criterion_coverage": {k: list(v) for k, v in sorted(plan.criterion_coverage.items())}}}


def serialize_proposal(proposal: PlanProposal) -> str:
    return _canonical(proposal_payload(proposal))


def _plan_schema(request: PlanningRequest | None = None) -> dict:
    """The shared model-owned plan body for envelope and typed tool views."""
    def arr(item, *, minimum=0, maximum=16384, unique=False):
        return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum, "uniqueItems": unique}
    string = {"type": "string", "minLength": 1, "maxLength": 4096}
    positive = {"type": "integer", "minimum": 1, "maximum": 1000000}
    def bound(field, cap): return {"type":"integer","minimum":1,"maximum":getattr(request,field) if request else HARD_CAPS[cap]}
    ticket = {"type": "object", "required": ["schema_version", "ticket_id", "objective", "criterion_ids", "non_goals", "allowed_paths", "dependencies", "patch_budget", "context_budget_tokens", "verification"], "additionalProperties": False, "properties": {
        "schema_version": {"type":"integer", "const": 1}, "ticket_id": {**string,"pattern":SAFE_ID.pattern}, "objective": string,
        "criterion_ids": arr({**string, **({"enum":sorted(request.expected_criteria)} if request else {})}, minimum=1, maximum=16384, unique=True), "non_goals": arr(string, maximum=1024),
        "allowed_paths": arr({"allOf":[string,{"enum":sorted(request.authorized_paths)}]} if request else string, minimum=1, maximum=2048, unique=True), "dependencies": arr({**string,"pattern":SAFE_ID.pattern}, maximum=2048, unique=True),
        "context_budget_tokens": bound("max_context_tokens","max_context_tokens"),
        "patch_budget": {"type":"object", "required":["max_files","max_changed_lines","max_attempts"], "additionalProperties":False, "properties":{"max_files":bound("max_patch_files","max_patch_files"),"max_changed_lines":bound("max_patch_lines","max_patch_lines"),"max_attempts":bound("max_attempts","max_attempts")}},
        "verification": {"type":"object", "required":["commands","working_directory","timeout_seconds","output_limit"], "additionalProperties":False, "properties":{"commands":{"const":[list(c) for c in request.verification_commands]} if request else arr(arr(string,minimum=1,maximum=32),minimum=1,maximum=32),"working_directory":{"const":"."},"timeout_seconds":{"type":"integer","minimum":1,"maximum":request.verification_timeout_seconds if request else HARD_CAPS["verification_timeout_seconds"]},"output_limit":{"type":"integer","minimum":1,"maximum":request.verification_output_limit if request else HARD_CAPS["verification_output_limit"]}}}}}
    tranche = {"type":"object", "required":["tranche_id","ordinal","objective","non_goals","criterion_ids","tickets"],"additionalProperties":False,"properties":{"tranche_id":{**string,"pattern":SAFE_ID.pattern},"ordinal":{"type":"integer","minimum":0,"maximum":127},"objective":string,"non_goals":arr(string,maximum=1024),"criterion_ids":arr({**string, **({"enum":sorted(request.expected_criteria)} if request else {})},minimum=1,maximum=16384,unique=True),"tickets":arr(ticket,minimum=1,maximum=2048)}}
    if request is not None and request.non_goals:
        # Every generated tranche and ticket must carry every root exclusion.
        # Do not emit an empty allOf: Draft 2020-12 requires at least one item.
        ticket["allOf"] = [{"properties": {"non_goals": {"contains": {"const": non_goal}}}}
                           for non_goal in request.non_goals]
        tranche["allOf"] = [{"properties": {"non_goals": {"contains": {"const": non_goal}}}}
                            for non_goal in request.non_goals]
    plan={"type":"object","required":["plan_id","schema_version","tranches","criterion_coverage"],"additionalProperties":False,"properties":{"plan_id":{**string,"pattern":SAFE_ID.pattern},"schema_version":{"type":"integer","const":1},"tranches":arr(tranche,minimum=1,maximum=128),"criterion_coverage":{"type":"object","maxProperties":16384,"propertyNames":{"type":"string","minLength":1,"maxLength":4096},"additionalProperties":arr({**string,"pattern":SAFE_ID.pattern},minimum=1,maximum=2048,unique=True)}}}
    if request is not None:
        coverage=plan["properties"]["criterion_coverage"]
        coverage["properties"]={key:arr({**string,"pattern":SAFE_ID.pattern},minimum=1,maximum=2048,unique=True) for key in sorted(request.expected_criteria)}
        coverage["required"]=sorted(request.expected_criteria)
        coverage["additionalProperties"]=False
        plan["properties"]["tranches"]["maxItems"]=request.max_tranches
        plan["properties"]["tranches"]["items"]["properties"]["tickets"]["maxItems"]=request.max_tickets
    return plan


def planner_decisions_schema(request: PlanningRequest | None = None) -> dict:
    """JSON Schema for the complete model-owned decisions body only."""
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **_plan_schema(request)}


def planner_schema(request: PlanningRequest | None = None) -> dict:
    """Historical v1 canonical envelope schema; retained for evidence replay."""
    schema={"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["schema_version","request_identity","execution_order","plan"],"additionalProperties":False,"properties":{"schema_version":{"type":"integer","const":1},"request_identity":{"type":"string","pattern":"^[0-9a-f]{64}$"},"execution_order":{"type":"array","items":{"type":"string","pattern":SAFE_ID.pattern},"minItems":0,"maxItems":2048,"uniqueItems":True},"plan":_plan_schema(request)}}
    if request is not None:
        schema["properties"]["request_identity"]={"const":request.identity}
    return schema


def packet(request: PlanningRequest) -> str:
    return _canonical({"contract": "local-first-planning-v1", "request": request_payload(request),
                       "request_identity": request.identity, "schema": planner_decisions_schema(request),
                       "rules": ["Return typed decisions matching schema exactly; do not wrap them in an envelope.",
                                 "Preserve every expected criterion; do not add scope.",
                                 "All paths, commands and finite limits are fixed by the request.",
                                 "The plugin owns envelope version, request identity, and execution order.",
                                 "Tranches are ordered; only the first validated tranche is eligible for later materialization."]})


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlannerError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value, required: set[str], path: str) -> dict:
    if type(value) is not dict or set(value) != required:
        raise PlannerError(f"invalid or unknown fields at {path}")
    return value


def _plain_json_snapshot(value: object, request: PlanningRequest) -> object:
    """Detach one bounded plain JSON value without invoking hostile containers."""
    nodes = 0
    def copy(item: object, depth: int = 0) -> object:
        nonlocal nodes
        nodes += 1
        if nodes > HARD_CAPS["max_tickets"] * 32 or depth > request.max_json_depth:
            raise PlannerError("proposal exceeds nesting or node limit")
        if type(item) is str:
            try:
                if len(item.encode("utf-8")) > 4096:
                    raise PlannerError("proposal string exceeds limit")
            except UnicodeEncodeError as exc:
                raise PlannerError("proposal contains invalid Unicode") from exc
            return item
        if type(item) in (int, bool) or item is None:
            return item
        if type(item) is list:
            return [copy(child, depth + 1) for child in item]
        if type(item) is dict:
            output: dict[str, object] = {}
            for key, child in item.items():
                if type(key) is not str or key in output:
                    raise PlannerError("proposal has non-string or duplicate JSON key")
                output[key] = copy(child, depth + 1)
            return output
        raise PlannerError("proposal decisions must use exact plain JSON types")
    snapshot = copy(value)
    try:
        if len(_canonical(snapshot).encode("utf-8")) > request.max_payload_bytes:
            raise PlannerError("proposal exceeds byte limit")
    except UnicodeEncodeError as exc:
        raise PlannerError("proposal contains invalid Unicode") from exc
    return snapshot


def proposal_from_decisions(decisions: Mapping[str, object], request: PlanningRequest) -> PlanProposal:
    """Bind closed model decisions to trusted request authority and canonical v1 wire form."""
    if type(request) is not PlanningRequest or type(decisions) is not dict:
        raise PlannerError("typed decisions and trusted request are required")
    plan_body = _plain_json_snapshot(decisions, request)
    if type(plan_body) is not dict:
        raise PlannerError("typed decisions must be an object")
    raw_plan = _object(plan_body, {"plan_id", "schema_version", "tranches", "criterion_coverage"}, "decisions")
    plan, semantics = _parse_plan_body(raw_plan, request)
    proposal = PlanProposal(request.identity, plan, semantics)
    canonical = serialize_proposal(proposal)
    if len(canonical.encode("utf-8")) > request.max_payload_bytes:
        raise PlannerError("generated proposal exceeds byte limit")
    # The historical parser remains the final v1 envelope validator.
    return parse_proposal(canonical, request)


def parse_proposal(raw: str, request: PlanningRequest) -> PlanProposal:
    if type(request) is not PlanningRequest:
        raise PlannerError("request must be a PlanningRequest")
    try:
        size = len(raw.encode("utf-8")) if type(raw) is str else request.max_payload_bytes + 1
    except UnicodeEncodeError as exc:
        raise PlannerError("proposal contains invalid Unicode") from exc
    if type(raw) is not str or size > request.max_payload_bytes:
        raise PlannerError("proposal exceeds byte limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=lambda x: (_ for _ in ()).throw(PlannerError("invalid numeric constant")))
    except (ValueError, UnicodeError, RecursionError, PlannerError) as exc:
        raise PlannerError("malformed proposal JSON") from exc
    if _json_depth(value) > request.max_json_depth:
        raise PlannerError("proposal exceeds nesting limit")
    if not _valid_unicode(value):
        raise PlannerError("proposal contains invalid Unicode")
    root = _object(value, {"schema_version", "request_identity", "execution_order", "plan"}, "root")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1 or type(root["request_identity"]) is not str or not SHA64.fullmatch(root["request_identity"]) or root["request_identity"] != request.identity:
        raise PlannerError("proposal request identity mismatch")
    raw_plan = _object(root["plan"], {"plan_id", "schema_version", "tranches", "criterion_coverage"}, "plan")
    plan, semantics = _parse_plan_body(raw_plan, request)
    proposal = PlanProposal(request.identity, plan, semantics)
    if type(root["execution_order"]) is not list or tuple(root["execution_order"]) != topological_ticket_order(plan):
        raise PlannerError("execution order mismatch")
    return proposal


def _parse_plan_body(raw_plan: dict, request: PlanningRequest) -> tuple[DecompositionPlan, tuple[TrancheSemantics, ...]]:
    """Parse the shared model-owned body after the enclosing authority is known."""
    if type(raw_plan["tranches"]) is not list or len(raw_plan["tranches"]) > request.max_tranches:
        raise PlannerError("tranche bound exceeded")
    tranches = []
    count = 0
    for i, raw_tranche in enumerate(raw_plan["tranches"]):
        t = _object(raw_tranche, {"tranche_id", "ordinal", "objective", "non_goals", "criterion_ids", "tickets"}, f"tranche[{i}]")
        if type(t["objective"]) is not str or not t["objective"].strip() or len(t["objective"])>4096 or type(t["non_goals"]) is not list or len(t["non_goals"])>1024 or any(type(x) is not str or not x.strip() or len(x)>4096 for x in t["non_goals"]):
            raise PlannerError("tranche objective and non-goals must be explicit")
        if not set(request.non_goals) <= set(t["non_goals"]):
            raise PlannerError("tranche drops authoritative root non-goals")
        if type(t["criterion_ids"]) is not list or not t["criterion_ids"] or len(t["criterion_ids"])>16384 or any(type(x) is not str or not x.strip() or len(x)>4096 for x in t["criterion_ids"]) or len(set(t["criterion_ids"]))!=len(t["criterion_ids"]):
            raise PlannerError("invalid tranche criteria")
        if type(t["tickets"]) is not list or not t["tickets"]:
            raise PlannerError("empty tranche is forbidden")
        tickets = []
        for j, raw_ticket in enumerate(t["tickets"]):
            count += 1
            if count > request.max_tickets:
                raise PlannerError("ticket bound exceeded")
            ticket = _object(raw_ticket, {"schema_version", "ticket_id", "objective", "criterion_ids", "non_goals", "allowed_paths", "dependencies", "patch_budget", "context_budget_tokens", "verification"}, f"ticket[{i}][{j}]")
            if (type(ticket["objective"]) is not str or not ticket["objective"].strip() or len(ticket["objective"])>4096
                    or any(type(ticket[key]) is not list or len(ticket[key])>cap or any(type(x) is not str or not x.strip() or len(x)>4096 for x in ticket[key]) for key,cap in (("criterion_ids",16384),("non_goals",1024),("allowed_paths",2048),("dependencies",2048)))
                    or not ticket["criterion_ids"] or not ticket["allowed_paths"]
                    or len(set(ticket["criterion_ids"]))!=len(ticket["criterion_ids"]) or len(set(ticket["allowed_paths"]))!=len(ticket["allowed_paths"]) or len(set(ticket["dependencies"]))!=len(ticket["dependencies"])):
                raise PlannerError("ticket text and reference bounds are invalid")
            if not set(request.non_goals) <= set(ticket["non_goals"]):
                raise PlannerError("ticket drops authoritative root non-goals")
            try:
                parsed = parse_contract(ticket)
            except (TypeError, ValueError) as exc:
                raise PlannerError(f"invalid ticket contract at {i}:{j}") from exc
            _safe_id(parsed.ticket_id)
            for dep in parsed.dependencies:
                _safe_id(dep)
            if not set(parsed.allowed_paths) <= request.authorized_paths:
                raise PlannerError("ticket path widens authorized scope")
            if parsed.context_budget_tokens > request.max_context_tokens:
                raise PlannerError("ticket context budget widened")
            budget = parsed.patch_budget
            if (budget.max_files > request.max_patch_files or budget.max_changed_lines > request.max_patch_lines or budget.max_attempts > request.max_attempts):
                raise PlannerError("ticket patch or attempt budget widened")
            if parsed.verification.commands != request.verification_commands or parsed.verification.working_directory != "." or parsed.verification.timeout_seconds > request.verification_timeout_seconds or parsed.verification.output_limit > request.verification_output_limit:
                raise PlannerError("ticket verification policy widened")
            tickets.append(parsed)
        _safe_id(t["tranche_id"])
        if type(t["ordinal"]) is not int or type(t["criterion_ids"]) is not list or type(t["non_goals"]) is not list:
            raise PlannerError("invalid tranche fields")
        try:
            tranches.append(TranchePlan(t["tranche_id"], t["ordinal"], tuple(tickets), tuple(t["criterion_ids"])))
        except (TypeError, ValueError) as exc:
            raise PlannerError("invalid tranche fields") from exc
    if type(raw_plan["criterion_coverage"]) is not dict or any(type(k) is not str or not k.strip() or len(k)>4096 or type(v) is not list or not v or len(v)>2048 or any(type(x) is not str or not x.strip() or len(x)>4096 for x in v) or len(set(v))!=len(v) for k,v in raw_plan["criterion_coverage"].items()):
        raise PlannerError("invalid criterion coverage")
    try:
        coverage = {k: tuple(v) for k, v in raw_plan["criterion_coverage"].items()}
        if type(raw_plan["schema_version"]) is not int or raw_plan["schema_version"] != 1: raise PlannerError("invalid schema version")
        _safe_id(raw_plan["plan_id"])
        plan = DecompositionPlan(raw_plan["plan_id"], raw_plan["schema_version"], tuple(tranches), coverage)
    except (TypeError, ValueError) as exc:
        raise PlannerError("invalid plan structure") from exc
    for tranche in plan.tranches:
        _safe_id(tranche.tranche_id)
        if type(tranche.ordinal) is not int or not isinstance(tranche.tranche_id, str):
            raise PlannerError("invalid tranche identity")
    if [t.ordinal for t in plan.tranches] != list(range(len(plan.tranches))):
        raise PlannerError("tranche ordinals must follow consecutive proposal order")
    expected = frozenset(request.expected_criteria)
    validator = PlanValidator(expected_criteria=expected, max_tranches=request.max_tranches, max_tickets=request.max_tickets)
    errors = validator.validate(plan)
    if errors:
        raise PlannerError("invalid plan: " + ", ".join(errors))
    if set(plan.criterion_coverage) != expected:
        raise PlannerError("criterion coverage must exactly preserve expected criteria")
    if any(not tranche.criterion_ids for tranche in plan.tranches):
        raise PlannerError("empty tranche criterion scope")
    semantics = tuple(TrancheSemantics(t["tranche_id"], t["objective"], tuple(t["non_goals"])) for t in raw_plan["tranches"])
    return plan, semantics


def _safe_id(value: object) -> None:
    if type(value) is not str or not SAFE_ID.fullmatch(value):
        raise PlannerError("unsafe identifier")


def topological_ticket_order(plan: DecompositionPlan) -> tuple[str, ...]:
    if type(plan) is not DecompositionPlan or len(plan.tranches) > HARD_CAPS["max_tranches"] or sum(len(t.tickets) for t in plan.tranches) > HARD_CAPS["max_tickets"]:
        raise PlannerError("plan exceeds topology bounds")
    ids = [ticket.ticket_id for tranche in plan.tranches for ticket in tranche.tickets]
    tranche_ids = [t.tranche_id for t in plan.tranches]
    if type(plan.plan_id) is not str or not SAFE_ID.fullmatch(plan.plan_id) or type(plan.schema_version) is not int or plan.schema_version != 1:
        raise PlannerError("invalid plan identity or version")
    for tid in tranche_ids: _safe_id(tid)
    for tid in ids: _safe_id(tid)
    if len(ids) != len(set(ids)) or len(tranche_ids) != len(set(tranche_ids)):
        raise PlannerError("duplicate ticket or tranche ID")
    tickets = {ticket.ticket_id: ticket for tranche in plan.tranches for ticket in tranche.tickets}
    ordinal = {ticket.ticket_id: tranche.ordinal for tranche in plan.tranches for ticket in tranche.tickets}
    if [t.ordinal for t in plan.tranches] != list(range(len(plan.tranches))):
        raise PlannerError("invalid tranche order")
    pending = set(tickets)
    references=0
    for tranche in plan.tranches:
        for ticket in tranche.tickets:
            references += len(ticket.dependencies)
            if references > 16384: raise PlannerError("dependency reference bound exceeded")
            for dep in ticket.dependencies:
                _safe_id(dep)
                if dep not in tickets or ordinal[dep] > tranche.ordinal:
                    raise PlannerError("missing or future dependency")
    done: set[str] = set()
    output: list[str] = []
    while pending:
        current = min(ordinal[name] for name in pending)
        ready = sorted((name for name in pending if ordinal[name] == current and set(tickets[name].dependencies) <= done))
        if not ready:
            raise PlannerError("ticket dependency cycle or missing dependency")
        for name in ready:
            pending.remove(name); done.add(name); output.append(name)
    return tuple(output)
