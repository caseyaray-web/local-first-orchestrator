from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

import pytest

import local_first_orchestrator.accepted_dependency_links as accepted_links
from local_first_orchestrator.accepted_dependency_links import (
    reconstruct_accepted_dependency_link_authority,
    validate_observed_multi_edge_transition,
)


def test_transition_rejects_held_status_runs_event_schema_and_null_presence_gaps():
    source, token, receipts, recording = _trusted_inputs()
    observation = recording["observations"][2]
    kwargs = dict(trusted_accepted_source=source, trusted_accepted_token=token,
                  frozen_create_receipts=receipts, authority=None,
                  prior_applied_edges=(("TK-A", "TK-B"), ("TK-B", "TK-C")), edge=("TK-A", "TK-C"),
                  command_window=(observation["command_started_seconds"], observation["command_ended_seconds"]))
    for damage in ("status", "status_missing", "runs", "historical_event", "historical_nondict", "appended_event", "appended_extra", "appended_missing", "null_add"):
        prior = _raw_by_task(copy.deepcopy(observation["raw_before"]))
        observed = _raw_by_task(copy.deepcopy(observation["raw_after"]))
        if damage == "status":
            for cards in (prior, observed):
                for index, card in enumerate(cards.values()):
                    card["task"]["status"] = ("ready", "running", "done")[index]
        elif damage == "status_missing":
            for cards in (prior, observed): del next(iter(cards.values()))["task"]["status"]
        elif damage == "runs":
            for cards in (prior, observed):
                for card in cards.values(): card["runs"] = [{"id": "unexpected"}]
        elif damage == "historical_event":
            for cards in (prior, observed): cards["t_0813876a"]["events"][0]["future"] = None
        elif damage == "historical_nondict":
            for cards in (prior, observed): cards["t_0813876a"]["events"].append("not-an-event")
        elif damage == "appended_event":
            observed["t_10409aec"]["events"][-1]["created_at"] = 1.5
        elif damage == "appended_extra":
            observed["t_10409aec"]["events"][-1]["future"] = None
        elif damage == "appended_missing":
            del observed["t_10409aec"]["events"][-1]["run_id"]
        else:
            observed["t_0813876a"]["future"] = None
        with pytest.raises(ValueError):
            validate_observed_multi_edge_transition(prior_cards=prior, observed_cards=observed, **kwargs)


def test_transition_retains_unchanged_unknown_raw_fields_but_rejects_absent_vs_null():
    source, token, receipts, recording = _trusted_inputs()
    observation = recording["observations"][2]
    prior = _raw_by_task(copy.deepcopy(observation["raw_before"]))
    observed = _raw_by_task(copy.deepcopy(observation["raw_after"]))
    for cards in (prior, observed):
        cards["t_0813876a"]["future"] = {"opaque": [None]}
        cards["t_0813876a"]["task"]["future_task"] = {"opaque": True}
    kwargs = dict(trusted_accepted_source=source, trusted_accepted_token=token,
                  frozen_create_receipts=receipts, authority=None,
                  prior_applied_edges=(("TK-A", "TK-B"), ("TK-B", "TK-C")), edge=("TK-A", "TK-C"),
                  command_window=(observation["command_started_seconds"], observation["command_ended_seconds"]))
    assert validate_observed_multi_edge_transition(prior_cards=prior, observed_cards=observed, **kwargs)["status"] == "applied"
    observed["t_0813876a"].pop("future")
    with pytest.raises(ValueError):
        validate_observed_multi_edge_transition(prior_cards=prior, observed_cards=observed, **kwargs)

_RECORDING = Path(__file__).with_name("fixtures") / "m4_native_second_edge_recording.json"
_RECORDING_SHA256 = "d9a0587f6bf7a36195a8089b9f04088dd199ab1d1c2844ba017944381f0579e7"


def _trusted_inputs():
    raw = _RECORDING.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _RECORDING_SHA256
    recording = json.loads(raw)
    initial = recording["initial_evidence"]
    # These inputs are explicitly named trusted evidence at this pure boundary.
    # The raw cards remain untrusted wire receipts and are revalidated below.
    return (
        copy.deepcopy(initial["accepted_source_evidence"]),
        copy.deepcopy(initial["accepted_token"]),
        copy.deepcopy(initial["raw_cards"]),
        recording,
    )


def _raw_by_task(raw_by_label):
    return {value["task"]["id"]: value for value in raw_by_label.values()}


def _valid_transition(*, source, token, receipts, recording, authority=None, edge=("TK-A", "TK-C")):
    observation = recording["observations"][2]
    return validate_observed_multi_edge_transition(
        trusted_accepted_source=source,
        trusted_accepted_token=token,
        frozen_create_receipts=receipts,
        authority=authority,
        prior_cards=_raw_by_task(observation["raw_before"]),
        observed_cards=_raw_by_task(observation["raw_after"]),
        prior_applied_edges=(("TK-A", "TK-B"), ("TK-B", "TK-C")),
        edge=edge,
        command_window=(observation["command_started_seconds"], observation["command_ended_seconds"]),
    )


def test_reconstructs_authority_only_from_explicit_trusted_source_and_actual_receipts():
    source, token, receipts, recording = _trusted_inputs()
    authority = reconstruct_accepted_dependency_link_authority(
        trusted_accepted_source=source,
        trusted_accepted_token=token,
        frozen_create_receipts=receipts,
    )
    result = _valid_transition(source=source, token=token, receipts=receipts, recording=recording, authority=authority)
    assert result["status"] == "applied"
    assert result["edge"] == ("TK-A", "TK-C")
    assert result["edge_table_hash"].startswith("sha256:")
    assert result["edge_hash"].startswith("sha256:")


@pytest.mark.parametrize("legacy_ticket_ids", [("TK-A", "TK-B", "TK-C"), ("TK-A",)])
def test_reconstructs_and_validates_historical_v1_create_receipts(legacy_ticket_ids):
    from local_first_orchestrator.planning_coordinator import (
        ActiveTrancheRoute, accepted_active_tranche_create_payload,
        first_active_tranche_materialization,
    )
    from local_first_orchestrator.planning_coordinator import reconstruct_evidence

    source, token, receipts, recording = _trusted_inputs()
    request, _proposal = reconstruct_evidence(source)
    materialization = first_active_tranche_materialization(
        source, ActiveTrancheRoute(token["route"]["implementation_profile"], token["route"]["workspace"]))
    targets = {target.ticket_id: target for target in materialization.targets}
    for raw in receipts.values():
        current_body, footer = raw["task"]["body"].split("\n\n<!-- local-first-create:", 1)
        current_payload = json.loads(current_body)
        ticket_id = current_payload["ticket"]["ticket_id"]
        if ticket_id not in legacy_ticket_ids:
            continue
        legacy = accepted_active_tranche_create_payload(
            token, source, targets[ticket_id], body_kind="accepted_active_tranche_piece_v1")
        raw["task"]["title"] = legacy.title
        raw["task"]["body"] = legacy.body + "\n\n<!-- local-first-create:" + footer

    authority = reconstruct_accepted_dependency_link_authority(
        trusted_accepted_source=source,
        trusted_accepted_token=token,
        frozen_create_receipts=receipts,
    )
    result = _valid_transition(source=source, token=token, receipts=receipts,
                               recording=recording, authority=authority)
    assert result["status"] == "applied"
    assert result["edge"] == ("TK-A", "TK-C")


@pytest.mark.parametrize("mutation", [
    "request_identity", "root_objective", "non_goals", "criterion_statement", "criterion_coverage",
    "allowed_paths", "commands", "budgets", "plan_lineage", "contract_hash",
])
def test_reconstruction_rejects_trusted_source_semantic_mutations_even_with_recomputed_lookalike_hashes(mutation):
    source, token, receipts, _ = _trusted_inputs()
    plan = source["proposal"]["plan"]
    request = source["request"]
    if mutation == "request_identity":
        source["request_identity"] = "0" * 64
    elif mutation == "root_objective":
        request["objective"] = "forged root objective"
    elif mutation == "non_goals":
        request["non_goals"] = ["forged non-goal"]
    elif mutation == "criterion_statement":
        request["criterion_statements"][0][1] = "forged statement"
    elif mutation == "criterion_coverage":
        plan["criterion_coverage"]["AC-1"] = ["TK-C"]
    elif mutation == "allowed_paths":
        plan["tranches"][0]["tickets"][0]["allowed_paths"] = ["forged.py"]
    elif mutation == "commands":
        plan["tranches"][0]["tickets"][0]["verification"]["commands"] = [["forged"]]
    elif mutation == "budgets":
        plan["tranches"][0]["tickets"][0]["patch_budget"]["max_files"] = 99
    elif mutation == "plan_lineage":
        plan["plan_id"] = "forged-plan"
    else:
        token["plan_contract_hash"] = "0" * 64
    # An attacker can rehash their own request/proposal envelope. It still cannot
    # replace the independently supplied accepted token/receipt semantics.
    source["proposal_hash"] = hashlib.sha256(json.dumps(source["proposal"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    with pytest.raises(ValueError):
        reconstruct_accepted_dependency_link_authority(
            trusted_accepted_source=source,
            trusted_accepted_token=token,
            frozen_create_receipts=receipts,
        )


def test_transition_reconstructs_again_and_rejects_mappingproxy_or_constructor_bypass_forgery():
    source, token, receipts, recording = _trusted_inputs()
    genuine = reconstruct_accepted_dependency_link_authority(
        trusted_accepted_source=source, trusted_accepted_token=token, frozen_create_receipts=receipts
    )
    forged_proxy = MappingProxyType({
        "ticket_to_task": {"TK-A": "FORGED-A", "TK-B": "FORGED-B"},
        "declared_edges": (("TK-A", "TK-B"),), "authority_hash": "sha256:" + "0" * 64,
    })
    forged_constructor = object.__new__(type(genuine))
    # Neither a mapping proxy nor an object.__new__ brand bypass participates in
    # the decision: the same trusted source is reconstructed at consumption.
    for forged in (forged_proxy, forged_constructor):
        assert _valid_transition(source=source, token=token, receipts=receipts, recording=recording,
                                 authority=forged)["edge"] == ("TK-A", "TK-C")
        with pytest.raises(ValueError):
            _valid_transition(source=source, token=token, receipts=receipts, recording=recording,
                              authority=forged, edge=("FORGED-A", "FORGED-B"))


def test_forged_wire_body_tokens_and_rehashed_request_lineage_cannot_replace_trusted_source():
    source, token, receipts, _ = _trusted_inputs()
    body, footer = receipts["A"]["task"]["body"].split("\n\n<!-- local-first-create:", 1)
    forged = json.loads(body)
    forged_token = forged["accepted_token"]
    forged_token["request_identity"] = "0" * 64
    forged_token["acceptance_identity"] = "sha256:" + hashlib.sha256(
        json.dumps({key: value for key, value in forged_token.items() if key != "acceptance_identity"},
                   sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    # This is untrusted wire evidence: changing every lookalike token/hash in it
    # cannot substitute for the separately supplied trusted source/token inputs.
    receipts["A"]["task"]["body"] = json.dumps(forged, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n\n<!-- local-first-create:" + footer
    with pytest.raises(ValueError):
        reconstruct_accepted_dependency_link_authority(
            trusted_accepted_source=source, trusted_accepted_token=token, frozen_create_receipts=receipts
        )


@pytest.mark.parametrize("mutation", ["receipt", "edge_order", "duplicate", "cycle", "future"])
def test_full_canonical_edge_table_rejects_tamper_reorder_duplicates_cycles_and_future_edges(mutation):
    source, token, receipts, _ = _trusted_inputs()
    if mutation == "receipt":
        receipts["A"]["task"]["body"] = receipts["A"]["task"]["body"].replace("First criterion", "forged")
    elif mutation == "edge_order":
        source["proposal"]["plan"]["tranches"][0]["tickets"][2]["dependencies"] = ["TK-A", "TK-B"]
    elif mutation == "duplicate":
        source["proposal"]["plan"]["tranches"][0]["tickets"][2]["dependencies"] = ["TK-B", "TK-A", "TK-A"]
    elif mutation == "cycle":
        source["proposal"]["plan"]["tranches"][0]["tickets"][0]["dependencies"] = ["TK-C"]
    else:
        source["proposal"]["plan"]["tranches"][0]["tickets"][2]["dependencies"] = ["TK-FUTURE"]
    with pytest.raises(ValueError):
        reconstruct_accepted_dependency_link_authority(
            trusted_accepted_source=source, trusted_accepted_token=token, frozen_create_receipts=receipts
        )


def test_transition_rejects_a_fresh_apply_when_prior_history_already_records_that_edge():
    source, token, receipts, recording = _trusted_inputs()
    observation = recording["observations"][2]
    prior = _raw_by_task(copy.deepcopy(observation["raw_before"]))
    observed = _raw_by_task(copy.deepcopy(observation["raw_after"]))
    matching = copy.deepcopy(observed["t_10409aec"]["events"][-1])
    matching["created_at"] -= 1
    prior["t_10409aec"]["events"].append(matching)
    observed["t_10409aec"]["events"].insert(-1, copy.deepcopy(matching))
    with pytest.raises(ValueError, match="historical linked event"):
        validate_observed_multi_edge_transition(
            trusted_accepted_source=source,
            trusted_accepted_token=token,
            frozen_create_receipts=receipts,
            authority=None,
            prior_cards=prior,
            observed_cards=observed,
            prior_applied_edges=(("TK-A", "TK-B"), ("TK-B", "TK-C")),
            edge=("TK-A", "TK-C"),
            command_window=(observation["command_started_seconds"], observation["command_ended_seconds"]),
        )


class _ExplodingStr(str):
    calls = 0

    def __eq__(self, other):
        type(self).calls += 1
        return super().__eq__(other)

    def __hash__(self):
        type(self).calls += 1
        return super().__hash__()

    def __str__(self):
        type(self).calls += 1
        return super().__str__()


class _ExplodingList(list):
    calls = 0

    def __iter__(self):
        type(self).calls += 1
        return super().__iter__()

    def __len__(self):
        type(self).calls += 1
        return super().__len__()


class _ExplodingTuple(tuple):
    calls = 0

    def __iter__(self):
        type(self).calls += 1
        return super().__iter__()

    def __len__(self):
        type(self).calls += 1
        return super().__len__()


@pytest.mark.parametrize("edge, hostile", [
    ((_ExplodingStr("TK-A"), "TK-C"), _ExplodingStr),
    (_ExplodingList(["TK-A", "TK-C"]), _ExplodingList),
    (_ExplodingTuple(("TK-A", "TK-C")), _ExplodingTuple),
    (42, None),
])
def test_transition_rejects_hostile_edge_input_before_any_hook_can_run(edge, hostile):
    source, token, receipts, recording = _trusted_inputs()
    _ExplodingStr.calls = _ExplodingList.calls = _ExplodingTuple.calls = 0
    with pytest.raises(ValueError):
        _valid_transition(source=source, token=token, receipts=receipts, recording=recording, edge=edge)
    assert _ExplodingStr.calls == _ExplodingList.calls == _ExplodingTuple.calls == 0


@pytest.mark.parametrize("node", ["TK-A\x00", "x" * 241, "\ud800"])
def test_transition_rejects_noncanonical_ticket_identifier_syntax(node):
    source, token, receipts, recording = _trusted_inputs()
    with pytest.raises(ValueError):
        _valid_transition(source=source, token=token, receipts=receipts, recording=recording, edge=(node, "TK-C"))


def test_transition_rejects_hostile_prior_edge_node_before_equality_or_hashing():
    source, token, receipts, recording = _trusted_inputs()
    observation = recording["observations"][2]
    _ExplodingStr.calls = 0
    with pytest.raises(ValueError):
        validate_observed_multi_edge_transition(
            trusted_accepted_source=source,
            trusted_accepted_token=token,
            frozen_create_receipts=receipts,
            authority=None,
            prior_cards=_raw_by_task(observation["raw_before"]),
            observed_cards=_raw_by_task(observation["raw_after"]),
            prior_applied_edges=([_ExplodingStr("TK-A"), "TK-B"], ["TK-B", "TK-C"]),
            edge=("TK-A", "TK-C"),
            command_window=(observation["command_started_seconds"], observation["command_ended_seconds"]),
        )
    assert _ExplodingStr.calls == 0


def test_transition_rejects_prior_topology_without_matching_prior_link_event():
    source, token, receipts, recording = _trusted_inputs()
    observation = recording["observations"][2]
    prior = _raw_by_task(copy.deepcopy(observation["raw_before"]))
    observed = _raw_by_task(copy.deepcopy(observation["raw_after"]))
    for cards in (prior, observed):
        cards["t_cadc903f"]["events"][-1]["kind"] = "forged"
    with pytest.raises(ValueError, match="prior linked-event history"):
        validate_observed_multi_edge_transition(
            trusted_accepted_source=source,
            trusted_accepted_token=token,
            frozen_create_receipts=receipts,
            authority=None,
            prior_cards=prior,
            observed_cards=observed,
            prior_applied_edges=(("TK-A", "TK-B"), ("TK-B", "TK-C")),
            edge=("TK-A", "TK-C"),
            command_window=(observation["command_started_seconds"], observation["command_ended_seconds"]),
        )


def test_recorded_duplicate_link_cannot_be_represented_as_a_fresh_transition_by_omitting_prior_edge():
    source, token, receipts, recording = _trusted_inputs()
    observation = recording["observations"][3]
    with pytest.raises(ValueError, match="historical linked event"):
        validate_observed_multi_edge_transition(
            trusted_accepted_source=source,
            trusted_accepted_token=token,
            frozen_create_receipts=receipts,
            authority=None,
            prior_cards=_raw_by_task(observation["raw_before"]),
            observed_cards=_raw_by_task(observation["raw_after"]),
            prior_applied_edges=(("TK-A", "TK-B"), ("TK-B", "TK-C")),
            edge=("TK-A", "TK-C"),
            command_window=(observation["command_started_seconds"], observation["command_ended_seconds"]),
        )


def test_reconstruction_rejects_oversized_receipt_events_before_semantic_hashing(monkeypatch):
    source, token, receipts, _ = _trusted_inputs()
    receipts["A"]["events"].extend({} for _ in range(20_000))
    digest_calls = 0

    def no_digest(value):
        nonlocal digest_calls
        digest_calls += 1
        raise AssertionError("semantic hashing must not begin for oversized wire input")

    monkeypatch.setattr(accepted_links, "_digest", no_digest)
    with pytest.raises(ValueError, match="item limit"):
        reconstruct_accepted_dependency_link_authority(
            trusted_accepted_source=source,
            trusted_accepted_token=token,
            frozen_create_receipts=receipts,
        )
    assert digest_calls == 0


class _ExplodingDict(dict):
    calls = 0

    def items(self):
        type(self).calls += 1
        return super().items()

    def __len__(self):
        type(self).calls += 1
        return super().__len__()


def test_snapshot_rejects_container_subclasses_before_their_hooks_run():
    _ExplodingDict.calls = _ExplodingList.calls = 0
    for value in (_ExplodingDict({"x": 1}), _ExplodingList([1]), MappingProxyType({"x": 1})):
        with pytest.raises(ValueError):
            accepted_links._snapshot(value)
    assert _ExplodingDict.calls == _ExplodingList.calls == 0


@pytest.mark.parametrize("case, message", [
    ("mapping", "mapping exceeds"),
    ("array", "array exceeds"),
    ("characters", "character limit"),
    ("utf8", "UTF-8 byte limit"),
    ("aggregate", "UTF-8 byte limit"),
])
def test_snapshot_rejects_direct_container_and_string_bounds(case, message):
    values = {
        "mapping": {str(index): None for index in range(16_385)},
        "array": [None] * 16_385,
        "characters": ["x" * 4_000_001],
        "utf8": ["🚀" * 1_000_001],
        "aggregate": ["x" * 2_000_000, "x" * 2_000_001],
    }
    with pytest.raises(ValueError, match=message):
        accepted_links._snapshot(values[case])


def test_transition_rejects_unknown_raw_multibyte_field_over_aggregate_budget():
    source, token, receipts, recording = _trusted_inputs()
    observation = recording["observations"][2]
    prior = _raw_by_task(copy.deepcopy(observation["raw_before"]))
    observed = _raw_by_task(copy.deepcopy(observation["raw_after"]))
    prior["t_0813876a"]["future"] = "🚀" * 1_000_001
    observed["t_0813876a"]["future"] = "🚀" * 1_000_001
    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        validate_observed_multi_edge_transition(
            trusted_accepted_source=source, trusted_accepted_token=token,
            frozen_create_receipts=receipts, authority=None,
            prior_cards=prior, observed_cards=observed,
            prior_applied_edges=(("TK-A", "TK-B"), ("TK-B", "TK-C")), edge=("TK-A", "TK-C"),
            command_window=(observation["command_started_seconds"], observation["command_ended_seconds"]),
        )


def test_snapshot_rejects_excess_nodes_depth_and_cycles_without_recursing_forever():
    too_many_nodes = [[None] * 16 for _ in range(16_384)]
    too_deep = None
    for _ in range(33):
        too_deep = [too_deep]
    cycle = []
    cycle.append(cycle)
    for value, message in ((too_many_nodes, "node limit"), (too_deep, "nesting limit"), (cycle, "cycle")):
        with pytest.raises(ValueError, match=message):
            accepted_links._snapshot(value)
