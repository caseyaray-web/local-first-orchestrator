import json
import unittest

from local_first_orchestrator.decomposition_planner import minimal_plan_example, packet
from local_first_orchestrator.readiness import ReadinessError, validate_ticket
from local_first_orchestrator.repository_snapshot import RepositorySnapshot
from local_first_orchestrator.ticket import PATCH_BUDGET_POLICY, MicroTicket, PatchBudget, PatchBudgetPolicy, VerificationProfile
from local_first_orchestrator.decomposition import Criterion, FeatureContract


class PatchBudgetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.feature = FeatureContract("F", "Feature", "Implement the bounded concern", (Criterion("AC", "Do the concern"),), (), (), (), "base")
        self.snapshot = RepositorySnapshot("repo", "base", (), ())

    def ticket(self, *, allowed=("app.py",), new=(), budget=None):
        return MicroTicket("TK", "Implement the bounded concern.", ("AC",), "app.py::concern", allowed, ("Do not change unrelated behavior.",), budget or PatchBudget(max_files=2, max_changed_lines=180), VerificationProfile((("python", "-m", "unittest"),)), "low", True, 2, (), new)

    def test_packet_exposes_shared_policy_and_combined_path_rules(self):
        body = json.loads(packet(self.feature, self.snapshot))
        self.assertEqual(body["patch_budget_policy"], PATCH_BUDGET_POLICY.as_json())
        self.assertEqual(body["patch_budget_policy"]["normal_max_files"], PATCH_BUDGET_POLICY.normal_max_files)
        self.assertEqual(body["patch_budget_policy"]["normal_max_changed_lines"], PATCH_BUDGET_POLICY.normal_max_changed_lines)
        self.assertEqual(body["patch_budget_policy"]["minimum_exception_reason_length"], PATCH_BUDGET_POLICY.minimum_exception_reason_length)
        self.assertEqual(body["patch_budget_policy"]["declared_paths"], "allowed_files + new_test_files")
        self.assertTrue(body["patch_budget_policy"]["declared_paths_must_be_unique"])
        self.assertTrue(any("combined" not in rule and "declared paths are allowed_files plus new_test_files" in rule for rule in body["rules"]))
        self.assertIn("absolute_max_files", body["limits"])
        self.assertEqual(body["limits"]["absolute_max_files"], 3)
        self.assertEqual(body["limits"]["absolute_max_changed_lines"], 200)

    def test_readiness_and_packet_accept_same_fixture_policy(self):
        policy = PatchBudgetPolicy(1, 10, 7)
        ticket = self.ticket(budget=PatchBudget(max_files=1, max_changed_lines=10))
        self.assertIs(validate_ticket.__kwdefaults__["budget_policy"], PATCH_BUDGET_POLICY)
        validate_ticket(ticket, budget_policy=policy)
        body = json.loads(packet(self.feature, self.snapshot, budget_policy=policy))
        self.assertEqual(body["patch_budget_policy"]["normal_max_files"], 1)
        self.assertEqual(body["patch_budget_policy"]["normal_max_changed_lines"], 10)
        self.assertEqual(body["patch_budget_policy"]["minimum_exception_reason_length"], 7)

    def test_normal_two_file_180_line_ticket_passes(self):
        validate_ticket(self.ticket(allowed=("app.py",), new=("test_app.py",)))

    def test_duplicate_allowed_and_new_path_rejects(self):
        with self.assertRaisesRegex(ReadinessError, "declared files exceed"):
            validate_ticket(self.ticket(allowed=("app.py",), new=("app.py",)))

    def test_three_file_ticket_without_exception_rejects(self):
        with self.assertRaisesRegex(ReadinessError, "bounded exception reason"):
            validate_ticket(self.ticket(allowed=("a.py", "b.py", "c.py"), budget=PatchBudget(3, 180)))

    def test_three_file_180_line_ticket_without_exception_rejects(self):
        with self.assertRaisesRegex(ReadinessError, "bounded exception reason"):
            validate_ticket(self.ticket(allowed=("a.py", "b.py", "c.py"), budget=PatchBudget(3, 180)))

    def test_two_file_200_line_ticket_without_exception_rejects(self):
        with self.assertRaisesRegex(ReadinessError, "bounded exception reason"):
            validate_ticket(self.ticket(allowed=("app.py", "test_app.py"), budget=PatchBudget(2, 200)))

    def test_broader_budget_with_bounded_reason_passes(self):
        validate_ticket(self.ticket(allowed=("a.py", "b.py", "c.py"), budget=PatchBudget(3, 200, "Requires one inseparable integration seam.")))

    def test_blank_or_short_exception_reason_rejects(self):
        for reason in (None, "   ", "too short"):
            with self.subTest(reason=reason), self.assertRaisesRegex(ReadinessError, "bounded exception reason"):
                validate_ticket(self.ticket(allowed=("a.py", "b.py", "c.py"), budget=PatchBudget(3, 200, reason)))

    def test_generated_minimal_example_is_parser_and_readiness_valid(self):
        from local_first_orchestrator.decomposition_planner import parse
        proposal = parse(json.dumps(minimal_plan_example(self.feature, self.snapshot)))
        validate_ticket(proposal.tranches[0].microtickets[0])
        self.assertEqual(proposal.tranches[0].microtickets[0].allowed_files, ("app.py",))
        self.assertEqual(proposal.tranches[0].microtickets[0].new_test_files, ("test_app.py",))


if __name__ == "__main__":
    unittest.main()
