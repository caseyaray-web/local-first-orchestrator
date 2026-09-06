from __future__ import annotations

import inspect
import unittest

from local_first_orchestrator.historical_revalidation import (
    OBSOLETE_FILE_SCOPE_SYMBOL_VALIDATION,
    classify_obsolete_validation_failure,
)
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


F1_FILE = "scripts/test-meal-planner-c0910-ui.mjs"


def f1_ticket(*, allowed_files: tuple[str, ...] = (F1_FILE,), new_test_files: tuple[str, ...] = (), primary: str | None = None) -> MicroTicket:
    return MicroTicket(
        "F1", "fixture", ("AC-1",), primary or f"{F1_FILE}::run", allowed_files,
        (), PatchBudget(1, 20), VerificationProfile((("true",),)), "low", True, 1, (), new_test_files,
    )


def historical(*, compact: object = f"symbol scope exceeded in test file: {F1_FILE}", passed: object = False, attempt: object = 3) -> dict[str, object]:
    return {"attempt_number": attempt, "passed": passed, "compact_evidence": compact}


class HistoricalRevalidationClassifierTests(unittest.TestCase):
    def test_exact_f1_obsolete_failure_is_classified(self) -> None:
        self.assertEqual(
            classify_obsolete_validation_failure(f1_ticket(), historical()),
            OBSOLETE_FILE_SCOPE_SYMBOL_VALIDATION,
        )

    def test_current_contract_is_file_scoped(self) -> None:
        from local_first_orchestrator.symbols import contract_target_scope
        self.assertEqual(contract_target_scope(f1_ticket()), "file")

    def test_other_file_is_rejected(self) -> None:
        self.assertIsNone(classify_obsolete_validation_failure(f1_ticket(), historical(compact="symbol scope exceeded in test file: scripts/other.mjs")))

    def test_generic_passed_false_is_rejected(self) -> None:
        self.assertIsNone(classify_obsolete_validation_failure(f1_ticket(), historical(compact="validation failed")))

    def test_test_command_failure_is_rejected(self) -> None:
        self.assertIsNone(classify_obsolete_validation_failure(f1_ticket(), historical(compact="verification command failed: npm test")))

    def test_budget_failure_is_rejected(self) -> None:
        self.assertIsNone(classify_obsolete_validation_failure(f1_ticket(), historical(compact="changed line budget exceeded")))

    def test_true_symbol_scoped_contract_is_rejected(self) -> None:
        ticket = f1_ticket(primary=f"{F1_FILE}::run", allowed_files=(F1_FILE, "scripts/helper.mjs"))
        self.assertIsNone(classify_obsolete_validation_failure(ticket, historical()))

    def test_new_test_file_contract_is_rejected(self) -> None:
        self.assertIsNone(classify_obsolete_validation_failure(f1_ticket(new_test_files=("scripts/test-extra.mjs",)), historical()))

    def test_malformed_or_ambiguous_evidence_is_rejected(self) -> None:
        ticket = f1_ticket()
        for evidence in (None, {}, {"passed": False}, historical(passed=True), historical(attempt=True), historical(compact=True)):
            with self.subTest(evidence=evidence):
                self.assertIsNone(classify_obsolete_validation_failure(ticket, evidence))

    def test_operator_cannot_supply_a_classification_argument(self) -> None:
        from local_first_orchestrator.controller import LocalFirstController
        parameters = inspect.signature(LocalFirstController.revalidate_historical_implementation).parameters
        self.assertNotIn("classification", parameters)


if __name__ == "__main__":
    unittest.main()
