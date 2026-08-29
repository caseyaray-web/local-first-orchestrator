import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.repository_snapshot import RepositoryPlanValidator, snapshot
from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, FeatureContract, Tranche
from local_first_orchestrator.symbols import SymbolIndex, symbols_for
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.readiness import validate_ticket


class MultilanguageEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(("git", *args), cwd=self.repo, check=True, text=True, capture_output=True)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-m", "base")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_mixed_manifest_and_language_symbols(self):
        (self.repo / "backend").mkdir()
        (self.repo / "frontend").mkdir()
        (self.repo / "backend/service.py").write_text("def calculate_total(value):\n    return value\n")
        (self.repo / "frontend/payment.ts").write_text("export function calculatePayment(total: number) { return total; }\n")
        (self.repo / "frontend/Checkout.tsx").write_text("export const Checkout = () => <div />;\n")
        (self.repo / "frontend/payment.test.ts").write_text("test('payment', () => {});\n")
        (self.repo / "README.md").write_text("payment feature documentation\n")
        sha = self.commit()
        feature = FeatureContract("f", "Payment", "calculate payment", (Criterion("A", "payment"),), (), (), (), sha)
        snap = snapshot(self.repo, sha, feature)
        paths = {entry.path for entry in snap.manifest}
        self.assertEqual(paths, {"backend/service.py", "frontend/payment.ts", "frontend/Checkout.tsx", "frontend/payment.test.ts"})
        self.assertEqual(symbols_for("backend/service.py", "def calculate_total(value):\n    return value\n"), ("calculate_total",))
        self.assertIn("calculatePayment", symbols_for("frontend/payment.ts", (self.repo / "frontend/payment.ts").read_text()))
        self.assertIn("Checkout", symbols_for("frontend/Checkout.tsx", (self.repo / "frontend/Checkout.tsx").read_text()))
        self.assertEqual(symbols_for("frontend/widget.jsx", "export function Widget() { return null; }\n"), ("Widget",))

    def test_feature_relevant_typescript_beats_unrelated_python(self):
        for index in range(20):
            (self.repo / f"a{index:02}.py").write_text("def unrelated(): pass\n")
        (self.repo / "z_payment.ts").write_text("export function calculatePaymentTotal() { return 1; }\n")
        sha = self.commit()
        feature = FeatureContract("f", "Payment", "calculate payment total", (Criterion("A", "payment total"),), (), (), (), sha)
        result = snapshot(self.repo, sha, feature, limit=1)
        self.assertEqual(result.entries[0].path, "z_payment.ts")

    def test_snapshot_is_deterministic_and_dirty_checkout_is_ignored(self):
        (self.repo / "payment.ts").write_text("export function calculatePayment() { return 1; }\n")
        (self.repo / "service.py").write_text("def calculate_payment():\n    return 1\n")
        sha = self.commit()
        feature = FeatureContract("f", "Payment", "calculate payment", (Criterion("A", "payment"),), (), (), (), sha)
        first = snapshot(self.repo, sha, feature)
        (self.repo / "payment.ts").write_text("export function changedLocally() { return 99; }\n")
        (self.repo / "service.py").write_text("def dirty(): pass\n")
        second = snapshot(self.repo, sha, feature)
        self.assertEqual(first, second)
        self.assertEqual(first.snapshot_hash, second.snapshot_hash)

    def test_repository_validation_distinguishes_unknown_symbol_and_insufficient_detail(self):
        (self.repo / "payment.ts").write_text("export function calculatePayment() { return 1; }\n")
        (self.repo / "other.ts").write_text("export function unrelated() { return 1; }\n")
        sha = self.commit()
        feature = FeatureContract("f", "Payment", "calculate payment", (Criterion("A", "payment"),), (), (), (), sha)
        snap = snapshot(self.repo, sha, feature, limit=1)
        ticket = MicroTicket("T", "Change calculate payment behavior.", ("A",), "other.ts::unrelated", ("other.ts",), ("No API changes.",), PatchBudget(1, 20), VerificationProfile((("python", "-c", "print('ok')"),)), "low", True, 1, ())
        plan = DecompositionPlan(1, "f", feature.contract_hash, sha, snap.snapshot_hash, (), {"A": ("T",)}, (Tranche("tr", 0, "implement", (), ("A",), (ticket,)),))
        validation = RepositoryPlanValidator().validate(plan, snap)
        self.assertEqual(validation.reasons, ("insufficient_repository_evidence",))
        unknown = MicroTicket(**{**ticket.__dict__, "primary_symbol": "payment.ts::missing", "allowed_files": ("payment.ts",)})
        unknown_plan = DecompositionPlan(**{**plan.__dict__, "tranches": (Tranche("tr", 0, "implement", (), ("A",), (unknown,)),)})
        self.assertEqual(RepositoryPlanValidator().validate(unknown_plan, snap).reasons, ("unknown_symbol",))

    def test_valid_typescript_ticket_and_missing_symbol_reason(self):
        (self.repo / "payment.ts").write_text("export function calculatePayment() { return 1; }\n")
        sha = self.commit()
        ticket = MicroTicket("T", "Change calculate payment behavior.", ("A",), "payment.ts::calculatePayment", ("payment.ts",), ("No API changes.",), PatchBudget(1, 20), VerificationProfile((("python", "-c", "print('ok')"),)), "low", True, 1, ())
        self.assertIs(validate_ticket(ticket), ticket)
        self.assertFalse(SymbolIndex(self.repo).select_for_ticket(ticket).scope_unverified)
        missing = MicroTicket(**{**ticket.__dict__, "primary_symbol": "payment.ts::missing"})
        self.assertTrue(SymbolIndex(self.repo).select_for_ticket(missing).scope_unverified)


if __name__ == "__main__":
    unittest.main()
