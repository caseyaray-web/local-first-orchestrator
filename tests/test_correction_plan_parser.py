from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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

    def test_valid_plan_parses_and_equivalent_defaults_keep_identity(self):
        raw = self.plan_json()
        parsed = self.parse(raw)
        equivalent = self.plan_json()
        # Different JSON object ordering/whitespace is equivalent input.
        equivalent_parsed = self.parse(equivalent)
        self.assertEqual(parsed.payload(), equivalent_parsed.payload())
        first = self.service().create_plan(parsed)
        replay = self.service().create_plan(equivalent_parsed)
        self.assertEqual(first.correction_plan_id, replay.correction_plan_id)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM supplemental_correction_plans").fetchone()[0], 1)

    def test_invalid_types_fail_closed_without_persisting_a_plan(self):
        cases = [
            (("tickets", 0, "review_required"), "false"),
            (("tickets", 0, "review_required"), 0),
            (("tickets", 0, "review_required"), 1),
            (("tickets", 0, "review_required"), "0"),
            (("tickets", 0, "review_required"), "1"),
            (("tickets", 0, "max_attempts"), True),
            (("tickets", 0, "max_attempts"), "true"),
            (("tickets", 0, "max_attempts"), "20"),
            (("tickets", 0, "max_attempts"), 20.0),
            (("finding_summary",), None),
            (("tickets", 0, "criterion_ids"), {}),
            (("tickets", 0, "patch_budget"), []),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                raw = self.plan_json()
                target = raw
                for key in path[:-1]:
                    target = target[key] if not isinstance(key, int) else target[key]
                target[path[-1]] = value
                with self.assertRaises(ValueError):
                    self.parse(raw)
                self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM supplemental_correction_plans").fetchone()[0], 0)

    def test_unknown_nested_and_top_level_fields_fail_closed(self):
        for path in (("tickets", 0, "verification", "unexpected"), ("unexpected",)):
            with self.subTest(path=path):
                raw = self.plan_json()
                target = raw
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = True
                with self.assertRaises(ValueError):
                    self.parse(raw)
                self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM supplemental_correction_plans").fetchone()[0], 0)

    def test_null_required_nested_fields_fail_closed(self):
        for field in ("objective", "patch_budget", "verification", "review_required", "max_attempts"):
            with self.subTest(field=field):
                raw = self.plan_json()
                raw["tickets"][0][field] = None
                with self.assertRaises(ValueError):
                    self.parse(raw)
                self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM supplemental_correction_plans").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
