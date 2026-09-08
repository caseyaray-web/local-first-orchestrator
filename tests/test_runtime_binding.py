import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger


class RuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.db")
        self.ledger.migrate()
        self.ticket = self.ledger.create_ticket(title="T1")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_exact_replay_preserves_row_and_created_at(self):
        first = self.ledger.bind_runtime(self.ticket, "/repo/a", "A", "A", 1)
        created_at = first["created_at"]
        second = self.ledger.bind_runtime(self.ticket, "/repo/a", "A", "A", 1)
        self.assertEqual(second, first)
        self.assertEqual(self.ledger.connection.execute("select count(*) from runtime_bindings").fetchone()[0], 1)
        self.assertEqual(self.ledger.runtime_binding(self.ticket)["created_at"], created_at)

    def test_each_immutable_conflict_fails_and_preserves_original(self):
        first = self.ledger.bind_runtime(self.ticket, "/repo/a", "A", "A", 1)
        for values in (("/repo/b", "A", "A", 1), ("/repo/a", "B", "A", 1), ("/repo/a", "A", "B", 1), ("/repo/a", "A", "A", 0)):
            with self.assertRaisesRegex(ValueError, "conflicting runtime binding"):
                self.ledger.bind_runtime(self.ticket, *values)
            self.assertEqual(self.ledger.runtime_binding(self.ticket), first)
        self.assertEqual(self.ledger.connection.execute("select count(*) from runtime_bindings").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from attempts").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("select count(*) from model_invocations").fetchone()[0], 0)

    def test_binding_does_not_create_execution_artifacts(self):
        self.ledger.bind_runtime(self.ticket, "/repo/a", "A")
        self.assertEqual(self.ledger.connection.execute("select count(*) from attempts").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("select count(*) from model_invocations").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
