from __future__ import annotations

import json
import unittest
from copy import deepcopy

from local_first_orchestrator.cli import _correction_plan_from_file
from tests.test_supplemental_corrections import SupplementalCorrectionTests


class CorrectionPlanParserTests(SupplementalCorrectionTests):
    def plan_json(self) -> dict[str, object]:
        raw = self.spec().payload()
        raw.pop("repository_identity")
        raw["predecessors"] = [{"ticket_id": p["ticket_id"], "accepted_commit": p["accepted_commit_sha"]} for p in raw["predecessors"]]
        return raw

    def parse(self, raw: dict[str, object]):
        path = self.root / "correction.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return _correction_plan_from_file(str(path), str(self.repo))

    def assert_rejected_without_persistence(self, raw: dict[str, object]) -> None:
        with self.assertRaises(ValueError):
            self.parse(raw)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM supplemental_correction_plans").fetchone()[0], 0)

    @staticmethod
    def set_path(raw: dict[str, object], path: tuple[object, ...], value: object) -> None:
        target = raw
        for key in path[:-1]:
            target = target[key]  # type: ignore[index]
        target[path[-1]] = value  # type: ignore[index]

    def test_valid_plan_parses_and_omitted_defaults_have_same_identity_as_explicit_defaults(self):
        full = self.plan_json()
        omitted = deepcopy(full)
        ticket = omitted["tickets"][0]
        for key in ("risk", "review_required", "max_attempts", "dependencies", "relevant_symbols", "acceptance_criteria", "non_goals", "red_evidence"):
            ticket.pop(key)
        ticket["verification"] = {}
        ticket["patch_budget"] = {"exception_reason": None}
        explicit = deepcopy(omitted)
        explicit_ticket = explicit["tickets"][0]
        explicit_ticket.update({"risk": "low", "review_required": True, "max_attempts": 1, "dependencies": [], "relevant_symbols": [], "acceptance_criteria": [], "non_goals": [], "red_evidence": ""})
        explicit_ticket["verification"] = {"commands": [], "working_directory": ".", "timeout_seconds": 60, "output_limit": 20_000}
        explicit_ticket["patch_budget"] = {"max_files": 2, "max_changed_lines": 180, "exception_reason": None}
        parsed = self.parse(omitted)
        equivalent = self.parse(explicit)
        self.assertEqual(parsed.payload(), equivalent.payload())
        from local_first_orchestrator.corrections import _sha
        self.assertEqual(_sha(parsed.payload()), _sha(equivalent.payload()))
        full_parsed = self.parse(full)
        first = self.service().create_plan(full_parsed)
        replay = self.service().create_plan(self.parse(deepcopy(full)))
        self.assertEqual(first.correction_plan_id, replay.correction_plan_id)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM supplemental_correction_plans").fetchone()[0], 1)

    def test_every_required_string_field_rejects_non_strings(self):
        paths = [("feature_id",), ("tranche_id",), ("source_kind",), ("source_reference",), ("finding_fingerprint",), ("finding_summary",), ("base_sha",), ("snapshot_hash",), ("predecessors", 0, "ticket_id"), ("predecessors", 0, "accepted_commit"), ("tickets", 0, "objective"), ("tickets", 0, "primary_symbol"), ("tickets", 0, "risk"), ("tickets", 0, "red_evidence"), ("tickets", 0, "verification", "working_directory")]
        for path in paths:
            with self.subTest(path=path):
                raw = self.plan_json(); self.set_path(raw, path, 7); self.assert_rejected_without_persistence(raw)

    def test_each_nested_closed_structure_rejects_wrong_list_or_object_types(self):
        cases = [(("predecessors",), {}), (("predecessors", 0), []), (("tickets",), {}), (("tickets", 0), []), (("tickets", 0, "patch_budget"), []), (("tickets", 0, "verification"), []), (("tickets", 0, "verification", "commands"), {}), (("tickets", 0, "verification", "commands", 0), {}), (("tickets", 0, "criterion_ids"), {}), (("tickets", 0, "allowed_existing_files"), {}), (("tickets", 0, "new_test_files"), {}), (("tickets", 0, "forbidden_changes"), {}), (("tickets", 0, "dependencies"), {}), (("tickets", 0, "relevant_symbols"), {}), (("tickets", 0, "acceptance_criteria"), {}), (("tickets", 0, "non_goals"), {})]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                raw = self.plan_json(); self.set_path(raw, path, value); self.assert_rejected_without_persistence(raw)

    def test_boolean_and_integer_fields_reject_all_coercible_values(self):
        for path, values in [(("review_required",), ("false", "0", "1", 0, 1)), (("tickets", 0, "review_required"), ("false", "0", "1", 0, 1)), (("tickets", 0, "max_attempts"), (True, "true", "20", 20.0))]:
            for value in values:
                with self.subTest(path=path, value=value):
                    raw = self.plan_json(); self.set_path(raw, path, value); self.assert_rejected_without_persistence(raw)

    def test_null_required_fields_and_unknown_fields_fail_closed(self):
        required = [("feature_id",), ("tranche_id",), ("source_kind",), ("source_reference",), ("finding_fingerprint",), ("finding_summary",), ("predecessors",), ("tickets",), ("base_sha",), ("snapshot_hash",), ("predecessors", 0, "ticket_id"), ("predecessors", 0, "accepted_commit"), ("tickets", 0, "objective"), ("tickets", 0, "criterion_ids"), ("tickets", 0, "primary_symbol"), ("tickets", 0, "allowed_existing_files"), ("tickets", 0, "new_test_files"), ("tickets", 0, "forbidden_changes"), ("tickets", 0, "patch_budget"), ("tickets", 0, "verification"), ("tickets", 0, "risk"), ("tickets", 0, "review_required"), ("tickets", 0, "max_attempts"), ("tickets", 0, "dependencies"), ("tickets", 0, "relevant_symbols"), ("tickets", 0, "acceptance_criteria"), ("tickets", 0, "non_goals"), ("tickets", 0, "red_evidence")]
        for path in required:
            with self.subTest(path=path):
                raw = self.plan_json(); self.set_path(raw, path, None); self.assert_rejected_without_persistence(raw)
        unknown = [((), "top_unknown"), (("predecessors", 0), "predecessor_unknown"), (("tickets", 0), "ticket_unknown"), (("tickets", 0, "patch_budget"), "budget_unknown"), (("tickets", 0, "verification"), "verification_unknown")]
        for prefix, key in unknown:
            with self.subTest(key=key):
                raw = self.plan_json(); self.set_path(raw, prefix + (key,), True); self.assert_rejected_without_persistence(raw)


if __name__ == "__main__":
    unittest.main()
