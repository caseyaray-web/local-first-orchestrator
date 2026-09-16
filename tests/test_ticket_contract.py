from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import ticket_from_ledger
from local_first_orchestrator.evidence_hash import canonical_sha256
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


class TicketContractTests(unittest.TestCase):
    def ticket(self, *, timeout_seconds: int = 17, output_limit: int = 321) -> MicroTicket:
        return MicroTicket(
            "T-contract",
            "Return the bounded fixture value.",
            ("AC-1",),
            "app.py::value",
            ("app.py",),
            ("No unrelated changes.",),
            PatchBudget(1, 20),
            VerificationProfile((("python", "-c", "print(1)"),), timeout_seconds=timeout_seconds, output_limit=output_limit),
            "low",
            True,
            1,
            (),
        )

    def test_contract_persists_verification_execution_limits_and_binds_identity(self) -> None:
        ticket = self.ticket()
        verification = ticket.contract()["verification"]
        self.assertEqual(verification["timeout_seconds"], 17)
        self.assertEqual(verification["output_limit"], 321)
        other = self.ticket(timeout_seconds=18)
        self.assertNotEqual(canonical_sha256(ticket.contract()), canonical_sha256(other.contract()))

        with TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp) / "ledger.db")
            ledger.migrate()
            ticket_id = ledger.create_ticket(title="contract", contract=ticket.contract())
            restored = ticket_from_ledger(ledger.get_ticket(ticket_id))
            self.assertEqual(restored.verification.timeout_seconds, 17)
            self.assertEqual(restored.verification.output_limit, 321)
            ledger.close()

    def test_legacy_contract_without_limits_keeps_backward_compatible_defaults(self) -> None:
        legacy = self.ticket().contract()
        verification = dict(legacy["verification"])
        verification.pop("timeout_seconds")
        verification.pop("output_limit")
        legacy["verification"] = verification
        with TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp) / "ledger.db")
            ledger.migrate()
            ticket_id = ledger.create_ticket(title="legacy", contract=legacy)
            restored = ticket_from_ledger(ledger.get_ticket(ticket_id))
            self.assertEqual(restored.verification.timeout_seconds, 60)
            self.assertEqual(restored.verification.output_limit, 20_000)
            ledger.close()


if __name__ == "__main__":
    unittest.main()
