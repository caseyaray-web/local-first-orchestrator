from __future__ import annotations

import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.git_adapter import GitWorktreeAdapter, IntegrationHeadConflictError
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.paid_model import PaidPurpose
from local_first_orchestrator.scheduler import ProcessNextScheduler
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.usage_governor import UsageGovernor


class Board:
    timeout_seconds = 1
    def set_state(self, *args, **kwargs): return None
    def find_comment_marker(self, *args, **kwargs): return "not_found"
    def deliver_comment(self, *args, **kwargs): return None
    def create_microticket(self, *args, **kwargs): return "external"


class ImplementationReached(RuntimeError):
    pass


class SchedulerConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "ledger.db"
        self.ledger = Ledger(self.database)
        self.ledger.migrate()

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    @staticmethod
    def contract() -> dict[str, object]:
        return {
            "objective": "prove scheduler contention safety",
            "criterion_ids": ["AC-1"],
            "primary_symbol": "app.py::value",
            "allowed_files": ["app.py"],
            "forbidden_changes": ["no unrelated changes"],
            "patch_budget": {"max_files": 1, "max_changed_lines": 10},
            "verification": {"commands": [["python", "-c", "pass"]]},
            "risk": "low",
            "review_required": True,
            "max_attempts": 2,
            "dependencies": [],
        }

    def ticket(self, title: str, *, state: CanonicalState = CanonicalState.READY_LOCAL) -> str:
        ticket = self.ledger.create_ticket(title=title, state=state, external_id=f"external-{title}", contract=self.contract())
        self.ledger.bind_runtime(ticket, str(self.root), "a" * 40)
        return ticket

    @staticmethod
    def run_threads(*functions):
        barrier = threading.Barrier(len(functions))
        results: list[object] = [None] * len(functions)
        errors: list[BaseException | None] = [None] * len(functions)

        def invoke(index: int, fn) -> None:
            try:
                barrier.wait(timeout=5)
                results[index] = fn()
            except BaseException as exc:  # test captures thread failures explicitly
                errors[index] = exc

        threads = [threading.Thread(target=invoke, args=(index, fn)) for index, fn in enumerate(functions)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        if any(thread.is_alive() for thread in threads):
            raise AssertionError("concurrency test thread did not terminate")
        return results, errors

    def test_overlapping_global_ticks_choose_oldest_ticket_and_reach_model_launch_once(self) -> None:
        self.ticket("oldest")
        self.ticket("newer")
        expected = self.ledger.connection.execute(
            "SELECT id FROM tickets WHERE state=? ORDER BY created_at,id LIMIT 1", (CanonicalState.READY_LOCAL.value,)
        ).fetchone()[0]
        first = Ledger(self.database)
        second = Ledger(self.database)
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def runner(ticket_id: str) -> dict[str, object]:
            calls.append(ticket_id)
            first.ensure_attempt(ticket_id, 1)
            first.start_model_invocation(
                invocation_id="proof-invocation",
                ticket_id=ticket_id,
                attempt_number=1,
                stage="implementation",
                provider="fixture",
                model="fixture",
                packet_hash="packet",
                worktree_path=str(self.root),
                timeout_seconds=1,
            )
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            raise ImplementationReached(ticket_id)

        scheduler_a = ProcessNextScheduler(first, Board(), worker_id="worker-a", lease_seconds=30, clock=lambda:100, implementation_runner=runner)
        scheduler_b = ProcessNextScheduler(second, Board(), worker_id="worker-b", lease_seconds=30, clock=lambda:100, implementation_runner=lambda ticket_id: (_ for _ in ()).throw(AssertionError(f"second worker executed {ticket_id}")))
        first_error: list[BaseException] = []

        def run_first() -> None:
            try:
                scheduler_a.process_next()
            except BaseException as exc:
                first_error.append(exc)

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        competing = scheduler_b.process_next()
        self.assertEqual(competing.status, "busy")
        release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(first_error), 1)
        self.assertIsInstance(first_error[0], ImplementationReached)
        self.assertEqual(calls, [expected])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_stage_claims").fetchone()[0], 1)
        first.close(); second.close()

    def test_expired_tick_takeover_has_one_winner(self) -> None:
        self.assertEqual(self.ledger.claim_scheduler_tick("dead", "old-token", lease_seconds=1, now=100), "claimed")
        first = Ledger(self.database); second = Ledger(self.database)
        results, errors = self.run_threads(
            lambda: first.claim_scheduler_tick("worker-a", "token-a", lease_seconds=30, now=102),
            lambda: second.claim_scheduler_tick("worker-b", "token-b", lease_seconds=30, now=102),
        )
        self.assertEqual(errors, [None, None])
        self.assertEqual(sorted(results), ["busy", "claimed"])
        active = self.ledger.connection.execute("SELECT lease_owner,lease_token FROM scheduler_tick_lease WHERE id=1").fetchone()
        self.assertIn((active["lease_owner"], active["lease_token"]), {("worker-a", "token-a"), ("worker-b", "token-b")})
        first.close(); second.close()

    def test_duplicate_stage_claims_and_same_ticket_workers_have_one_owner(self) -> None:
        ticket = self.ticket("claim-once")
        first = Ledger(self.database); second = Ledger(self.database)
        results, errors = self.run_threads(
            lambda: first.claim_next_scheduler_implementation("worker-a", lease_seconds=30, now=100),
            lambda: second.claim_next_scheduler_implementation("worker-b", lease_seconds=30, now=100),
        )
        self.assertEqual(errors, [None, None])
        claimed = [row for row in results if row is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["ticket_id"], ticket)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_stage_claims WHERE ticket_id=?", (ticket,)).fetchone()[0], 1)
        owner = self.ledger.connection.execute("SELECT lease_owner FROM scheduler_stage_claims WHERE ticket_id=?", (ticket,)).fetchone()[0]
        self.assertIn(owner, {"worker-a", "worker-b"})
        first.close(); second.close()

    def test_expired_stage_recovery_beats_competing_validation_for_same_ticket(self) -> None:
        ticket = self.ticket("same-ticket-stage-race")
        claim = self.ledger.claim_next_scheduler_implementation("dead", lease_seconds=1, now=100)
        self.assertIsNotNone(claim)
        self.ledger.ensure_attempt(ticket, 1)
        artifact = self.root / "implementation.json"
        artifact.write_text("{}", encoding="utf-8")
        self.assertTrue(self.ledger.record_model_stage(ticket, 1, "implementation", purpose="implementation", adapter="fixture", request_hash="request", response_artifact=str(artifact), worktree_path=str(self.root), base_sha="a" * 40, diff_hash="diff"))

        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="recovery",
            lease_seconds=30,
            clock=lambda:102,
            implementation_runner=lambda ticket_id: (_ for _ in ()).throw(ImplementationReached(ticket_id)),
            validation_runner=lambda ticket_id: (_ for _ in ()).throw(AssertionError(f"validation bypassed implementation recovery: {ticket_id}")),
        )
        with self.assertRaisesRegex(ImplementationReached, ticket):
            scheduler.process_next()
        stages = [row[0] for row in self.ledger.connection.execute("SELECT stage FROM scheduler_stage_claims WHERE ticket_id=? ORDER BY created_at,claim_id", (ticket,)).fetchall()]
        self.assertEqual(stages, ["implementation"])

    def test_state_outbox_retry_overlap_claims_row_once(self) -> None:
        ticket = self.ticket("projection", state=CanonicalState.DRAFT)
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        self.assertIsNotNone(self.ledger.plan_projection(ticket, evidence="state projection contention"))
        first = Ledger(self.database); second = Ledger(self.database)
        results, errors = self.run_threads(
            lambda: first.claim_next_state_projection("worker-a", lease_seconds=30, now=100),
            lambda: second.claim_next_state_projection("worker-b", lease_seconds=30, now=100),
        )
        self.assertEqual(errors, [None, None])
        claimed = [row for row in results if row is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["ticket_id"], ticket)
        self.assertIn(claimed[0]["lease_owner"], {"worker-a", "worker-b"})
        first.close(); second.close()

    def test_paid_reservation_same_request_is_cross_connection_idempotent(self) -> None:
        UsageGovernor(self.ledger).configure("F", architecture=1, checkpoint=0, escalation=0)

        def reserve(worker: str) -> str:
            ledger = Ledger(self.database)
            try:
                return UsageGovernor(ledger).authorize("F", PaidPurpose.ARCHITECTURE, "same-request").reservation_id
            finally:
                ledger.close()

        results, errors = self.run_threads(lambda: reserve("a"), lambda: reserve("b"), lambda: reserve("c"), lambda: reserve("d"))
        self.assertEqual(errors, [None, None, None, None])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM paid_reservations WHERE request_key='same-request'").fetchone()[0], 1)

    def test_git_integration_head_compare_and_swap_has_one_concurrent_winner(self) -> None:
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        target = repo / "app.py"
        target.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

        target.write_text("first\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-qam", "first"], cwd=repo, check=True)
        first_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "reset", "--hard", "-q", base], cwd=repo, check=True)
        target.write_text("second\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-qam", "second"], cwd=repo, check=True)
        second_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "reset", "--hard", "-q", base], cwd=repo, check=True)

        adapter_a = GitWorktreeAdapter(repo, self.root / "wa")
        adapter_b = GitWorktreeAdapter(repo, self.root / "wb")
        self.assertEqual(adapter_a.resolve_execution_base("T", base), base)
        results, errors = self.run_threads(
            lambda: adapter_a.advance_integration_head("T", base, first_sha),
            lambda: adapter_b.advance_integration_head("T", base, second_sha),
        )
        successes = [value for value in results if value is not None]
        conflicts = [error for error in errors if isinstance(error, IntegrationHeadConflictError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        final = subprocess.run(["git", "rev-parse", "refs/local-first/tranches/T/integration-head"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
        self.assertIn(final, {first_sha, second_sha})


if __name__ == "__main__":
    unittest.main()
