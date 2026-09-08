import unittest

from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, FeatureContract, PlanValidator, Tranche
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


CRITERIA = (
    "export-completeness", "secret-exclusion", "household-isolation", "restore-dry-run",
    "restore-apply-safeguards", "restore-parity", "audit-intent", "verification-discipline",
)


def ticket(ticket_id, criteria):
    return MicroTicket(ticket_id, "Implement the bounded planner concern.", tuple(criteria), "app.py::calculate", ("app.py", "test_app.py"), ("Do not change unrelated behavior.",), PatchBudget(), VerificationProfile((("python", "-m", "unittest"),)), "low", True, 2, ())


def plan(coverage=None, tranche_criteria=CRITERIA, ticket_sets=None):
    ticket_sets = ticket_sets or (("TK-1", ("export-completeness", "secret-exclusion")), ("TK-2", ("household-isolation", "restore-dry-run")), ("TK-3", ("restore-apply-safeguards", "restore-parity", "audit-intent")), ("TK-4", ("verification-discipline",)))
    tickets = tuple(ticket(ticket_id, criteria) for ticket_id, criteria in ticket_sets)
    coverage = coverage or {criterion: [ticket_id for ticket_id, criteria in ticket_sets if criterion in criteria] for criterion in CRITERIA}
    feature = FeatureContract("F", "Feature", "Implement the bounded concern", tuple(Criterion(c, c) for c in CRITERIA), (), (), (), "base")
    return feature, DecompositionPlan(1, "F", feature.contract_hash, "base", "snapshot", ("decision",), {k: tuple(v) for k, v in coverage.items()}, (Tranche("T-0", 0, "Implement the bounded concern", (), tuple(tranche_criteria), tickets),))


class CriterionCoverageTests(unittest.TestCase):
    def assert_reasons(self, plan_value, *reasons):
        feature, proposal = plan_value
        result = PlanValidator().validate(feature, proposal)
        self.assertFalse(result.passed)
        for reason in reasons:
            self.assertIn(reason, result.reasons)

    def test_terra_shaped_eight_criteria_four_tickets_passes_coverage_and_readiness(self):
        feature, proposal = plan()
        result = PlanValidator().validate(feature, proposal)
        self.assertTrue(result.passed, result.reasons)

    def test_missing_criterion_key_is_uncovered(self):
        coverage = {c: ["TK-1"] for c in CRITERIA[:-1]}
        self.assert_reasons(plan(coverage), "criterion_uncovered")

    def test_unknown_criterion_key_rejects(self):
        feature, proposal = plan()
        coverage = dict(proposal.criterion_coverage, unknown=["TK-1"])
        self.assert_reasons(plan(coverage), "unknown_criterion")

    def test_empty_ticket_list_rejects(self):
        feature, proposal = plan()
        coverage = dict(proposal.criterion_coverage, **{"export-completeness": []})
        self.assert_reasons(plan(coverage), "invalid_criterion_coverage")

    def test_nonexistent_mapped_ticket_rejects(self):
        feature, proposal = plan()
        coverage = dict(proposal.criterion_coverage, **{"export-completeness": ["MISSING"]})
        self.assert_reasons(plan(coverage), "invalid_criterion_ticket_reference")

    def test_ticket_that_does_not_claim_criterion_rejects(self):
        feature, proposal = plan()
        coverage = dict(proposal.criterion_coverage, **{"export-completeness": ["TK-2"]})
        self.assert_reasons(plan(coverage), "criterion_ticket_mismatch")

    def test_tranche_that_does_not_claim_criterion_rejects(self):
        self.assert_reasons(plan(tranche_criteria=tuple(c for c in CRITERIA if c != "export-completeness")), "criterion_tranche_mismatch")

    def test_one_ticket_may_cover_multiple_criteria(self):
        feature, proposal = plan(ticket_sets=(("TK-1", CRITERIA),))
        self.assertTrue(PlanValidator().validate(feature, proposal).passed)

    def test_one_criterion_may_map_to_multiple_tickets(self):
        feature, proposal = plan(coverage={c: (["TK-1", "TK-2"] if c == "export-completeness" else ["TK-1"]) for c in CRITERIA}, ticket_sets=(("TK-1", CRITERIA), ("TK-2", ("export-completeness",))))
        self.assertTrue(PlanValidator().validate(feature, proposal).passed)


if __name__ == "__main__":
    unittest.main()
