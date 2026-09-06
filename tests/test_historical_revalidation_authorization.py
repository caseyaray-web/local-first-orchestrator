from __future__ import annotations

import json
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig, ticket_from_ledger
from local_first_orchestrator.evidence_hash import canonical_sha256
from local_first_orchestrator.hermes_board import ExternalTicket
from local_first_orchestrator.historical_revalidation import attestation_hash, attestation_hash_from_row, attestation_identity, authorization_hash, authorization_hash_from_row, authorization_identity
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.validation import DeterministicValidator


F1_FILE = "scripts/test-meal-planner-c0910-ui.mjs"


def contract() -> dict[str, object]:
    return {
        "objective": "Update the UI fixture.", "criterion_ids": ["AC-1"],
        "primary_symbol": f"{F1_FILE}::run", "allowed_files": [F1_FILE],
        "forbidden_changes": ["no unrelated changes"], "patch_budget": {"max_files": 1, "max_changed_lines": 30},
        "verification": {"commands": [["true"]]}, "risk": "low",
        "review_required": True, "max_attempts": 2,
    }


class Board:
    is_fake = False
    def set_state(self, ticket_id: str, state: object, *, idempotency_key: str) -> None: pass


class F1Model:
    provider = "fixture-provider"
    model = "fixture-model"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
        self.calls.append(purpose)
        assert workdir is not None
        if purpose == "implementation":
            (workdir / F1_FILE).write_text("function run() { return true; }\nfunction incidental() { return true; }\n", encoding="utf-8")
        artifact = artifact_dir / f"{purpose}.json"
        artifact.write_text("{}", encoding="utf-8")
        return type("Result", (), {"payload": {}, "artifact_path": artifact})()


class HistoricalAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(); self.root = Path(self.temp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        self.git("init", "-q", "-b", "main"); self.git("config", "user.email", "t@example.invalid"); self.git("config", "user.name", "Test")
        (self.repo / "scripts").mkdir(); (self.repo / F1_FILE).write_text("function run() { return false; }\n", encoding="utf-8")
        self.git("add", "."); self.git("commit", "-qm", "base"); self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate()
        self.config = RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,))
        card = ExternalTicket("C09.10-T0-F1-fixture", "F1 fixture", "<!-- local-first-orchestrator -->\n```local-first-contract\n" + json.dumps(contract()) + "\n```", "scheduled", str(self.repo))
        self.model = F1Model()
        self.controller = LocalFirstController(self.ledger, Board(), self.config, local_model=self.model)
        self.ticket = self.controller.import_card(card)
        self.ledger.pause("operator", reason="authorization fixture")

    def tearDown(self) -> None:
        self.ledger.close(); self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True)

    def create_obsolete_failure(self) -> None:
        import local_first_orchestrator.validation as validation_module
        with mock.patch.object(validation_module, "contract_target_scope", lambda ticket: "symbol"), mock.patch.object(validation_module, "enforce_symbol_scope", lambda *args: ((f"symbol scope exceeded in test file: {F1_FILE}",), False)):
            self.assertIsNone(self.controller.execute_implementation(self.ticket, repository=self.repo))
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"], CanonicalState.NEEDS_TRIAGE.value)
        self.assertEqual(self.model.calls, ["implementation"])

    def authorize(self, *, operator: str = "operator", reason: str = "recognized F1 obsolete rule") -> dict[str, object]:
        return self.controller.authorize_historical_revalidation(self.ticket, 1, repository=self.repo, operator_id=operator, reason=reason)

    def attest(self) -> dict[str, object]:
        return self.controller.attest_historical_revalidation_implementation(self.ticket, 1, repository=self.repo, operator_id="attestor")

    def raw_identity(self, *, attempt: int = 99, invocation: str = "raw-invocation") -> dict[str, object]:
        return authorization_identity(ticket_id=self.ticket, attempt_number=attempt, base_sha=self.base, repository_identity=str(self.repo.resolve()), target_file=F1_FILE, failure_classification="obsolete_file_scope_symbol_validation", failure_evidence_identity="e" * 64, implementation_invocation_id=invocation, operator_id="raw-operator", reason="raw fixture")

    def raw_insert(self, identity: dict[str, object], digest: str | None) -> None:
        values = ("raw-authorization", *[identity[field] for field in ("ticket_id", "attempt_number", "base_sha", "repository_identity", "target_file", "failure_classification", "failure_evidence_identity", "implementation_invocation_id", "operator_id", "reason")], digest, 1)
        self.ledger.connection.execute("INSERT INTO historical_revalidation_authorizations(authorization_id,ticket_id,attempt_number,base_sha,repository_identity,target_file,failure_classification,failure_evidence_identity,implementation_invocation_id,operator_id,reason,authorization_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values)

    def test_controller_authorization_stores_valid_canonical_hash(self) -> None:
        self.create_obsolete_failure(); authorization = self.authorize()
        self.assertEqual(authorization["authorization_hash"], authorization_hash_from_row(authorization))

    def test_direct_sql_correct_hash_succeeds(self) -> None:
        identity = self.raw_identity(); self.raw_insert(identity, authorization_hash(identity))
        self.assertIsNotNone(self.ledger.historical_revalidation_authorization(self.ticket, 99))

    def test_direct_sql_arbitrary_changed_field_or_null_hash_is_rejected(self) -> None:
        identity = self.raw_identity()
        for digest, changed in (("a" * 64, identity), (authorization_hash(identity), {**identity, "target_file": "other.mjs"}), (None, identity)):
            with self.subTest(digest=digest, changed=changed):
                with self.assertRaises((sqlite3.IntegrityError, sqlite3.OperationalError)):
                    self.raw_insert(changed, digest)

    def test_consumer_rejects_corrupt_persisted_hash_fixture(self) -> None:
        self.create_obsolete_failure(); authorization = self.authorize()
        corrupt = dict(authorization); corrupt["authorization_hash"] = "0" * 64
        with mock.patch.object(self.ledger, "historical_revalidation_authorization", return_value=corrupt):
            with self.assertRaisesRegex(PermissionError, "authorization hash mismatch"):
                self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)

    def test_valid_authorized_attempt_receives_integrity_attestation_and_replay_is_idempotent(self) -> None:
        self.create_obsolete_failure(); self.authorize(); first = self.attest(); replay = self.attest()
        self.assertEqual(first["attestation_hash"], attestation_hash_from_row(first))
        self.assertEqual(first["attestation_id"], replay["attestation_id"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM historical_revalidation_attestations").fetchone()[0], 1)

    def test_attestation_requires_authorization_and_preserved_worktree_identity(self) -> None:
        self.create_obsolete_failure()
        with self.assertRaisesRegex(PermissionError, "authorization"):
            self.attest()
        self.authorize()
        attempt = self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=? AND attempt_number=1", (self.ticket,)).fetchone(); assert attempt is not None
        Path(attempt["worktree_path"]).joinpath(F1_FILE).write_text("function run() { return false; }\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "does not match durable historical diff"):
            self.attest()

    def test_attestation_provenance_mismatch_and_conflict_fail_closed(self) -> None:
        self.create_obsolete_failure(); self.authorize()
        self.ledger.connection.execute("UPDATE model_stage_artifacts SET diff_hash=? WHERE ticket_id=? AND attempt_number=1 AND stage='implementation'", ("0" * 64, self.ticket))
        with self.assertRaisesRegex(RuntimeError, "does not match durable historical diff"):
            self.attest()

    def test_attestation_base_or_invocation_provenance_mismatch_fails_closed(self) -> None:
        self.create_obsolete_failure(); self.authorize()
        self.ledger.connection.execute("UPDATE model_stage_artifacts SET base_sha=? WHERE ticket_id=? AND attempt_number=1 AND stage='implementation'", ("f" * 40, self.ticket))
        with self.assertRaisesRegex(ValueError, "base provenance"):
            self.attest()
        self.ledger.connection.execute("UPDATE model_stage_artifacts SET base_sha=? WHERE ticket_id=? AND attempt_number=1 AND stage='implementation'", (self.base, self.ticket))
        self.ledger.connection.execute("UPDATE model_invocations SET invocation_id=invocation_id || '-changed' WHERE ticket_id=? AND attempt_number=1 AND stage='implementation'", (self.ticket,))
        with self.assertRaisesRegex(PermissionError, "invocation mismatch"):
            self.attest()

    def test_direct_sql_attestation_hash_forgery_is_rejected(self) -> None:
        self.create_obsolete_failure(); authorization = self.authorize()
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=1", (self.ticket,)).fetchone(); assert attempt is not None
        impl = self.ledger.model_stage(self.ticket, 1, "implementation"); assert impl is not None
        invocation = self.ledger.invocation_for_stage(self.ticket, 1, "implementation"); assert invocation is not None
        identity = attestation_identity(ticket_id=self.ticket, attempt_number=1, base_sha=str(impl["base_sha"]), repository_identity=str(self.repo.resolve()), implementation_invocation_id=str(invocation["invocation_id"]), implementation_artifact=str(impl["response_artifact"]), implementation_diff_hash=str(impl["diff_hash"]), worktree_path=str(Path(str(attempt["worktree_path"])).resolve()), worktree_diff_hash=str(impl["diff_hash"]), authorization_hash=str(authorization["authorization_hash"]), operator_id="attestor")
        values = ("raw-attestation", *[identity[field] for field in ("ticket_id", "attempt_number", "base_sha", "repository_identity", "implementation_invocation_id", "implementation_artifact", "implementation_diff_hash", "worktree_path", "worktree_diff_hash", "authorization_hash", "operator_id")], "f" * 64, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("INSERT INTO historical_revalidation_attestations(attestation_id,ticket_id,attempt_number,base_sha,repository_identity,implementation_invocation_id,implementation_artifact,implementation_diff_hash,worktree_path,worktree_diff_hash,authorization_hash,operator_id,attestation_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)

    def test_consumer_with_valid_attestation_reaches_only_next_explicit_gate(self) -> None:
        self.create_obsolete_failure(); self.authorize(); self.attest()
        with self.assertRaisesRegex(RuntimeError, "historical candidate freeze gate required"):
            self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
        self.assertIsNone(self.ledger.review_candidate(self.ticket)); self.assertEqual(self.model.calls, ["implementation"])

    def test_consumer_rejects_post_attestation_mutation_then_accepts_restored_state(self) -> None:
        self.create_obsolete_failure(); self.authorize(); self.attest()
        attempt = self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=? AND attempt_number=1", (self.ticket,)).fetchone(); assert attempt is not None
        path = Path(attempt["worktree_path"]).joinpath(F1_FILE); original = path.read_text(encoding="utf-8")
        path.write_text(original + "\n// changed after attestation\n", encoding="utf-8")
        with self.assertRaisesRegex(PermissionError, "live worktree diff mismatch"):
            self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
        path.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "historical candidate freeze gate required"):
            self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)

    def test_consumer_rejects_deleted_worktree(self) -> None:
        self.create_obsolete_failure(); self.authorize(); self.attest()
        attempt = self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=? AND attempt_number=1", (self.ticket,)).fetchone(); assert attempt is not None
        path = Path(attempt["worktree_path"]); replacement = path.with_name(path.name + "-missing"); path.rename(replacement)
        try:
            with self.assertRaisesRegex(PermissionError, "worktree is unavailable"):
                self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
        finally:
            replacement.rename(path)

    def test_consumer_rejects_detached_live_attempt_branch(self) -> None:
        self.create_obsolete_failure(); self.authorize(); self.attest()
        attempt = self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=? AND attempt_number=1", (self.ticket,)).fetchone(); assert attempt is not None
        path = Path(attempt["worktree_path"])
        subprocess.run(("git", "checkout", "--detach"), cwd=path, check=True, capture_output=True, text=True)
        try:
            with self.assertRaisesRegex(PermissionError, "live worktree identity"):
                self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
        finally:
            subprocess.run(("git", "symbolic-ref", "HEAD", f"refs/heads/local-first/{self.ticket}/attempt-1"), cwd=path, check=True, capture_output=True, text=True)

    def test_consumer_rejects_corrupt_attestation_hash_fixture(self) -> None:
        self.create_obsolete_failure(); self.authorize(); attestation = self.attest(); corrupt = dict(attestation); corrupt["attestation_hash"] = "0" * 64
        with mock.patch.object(self.ledger, "historical_revalidation_attestation", return_value=corrupt):
            with self.assertRaisesRegex(PermissionError, "attestation hash mismatch"):
                self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)

    def test_current_validation_replay_does_not_rerun_validator(self) -> None:
        self.create_obsolete_failure(); self.authorize(); self.attest()
        original_validate = DeterministicValidator.validate
        calls: list[object] = []
        def count_then_validate(validator: DeterministicValidator, worktree: Path, ticket: Any, *, base_sha: str) -> Any:
            calls.append(ticket)
            return original_validate(validator, worktree, ticket, base_sha=base_sha)
        with mock.patch.object(DeterministicValidator, "validate", new=count_then_validate):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "historical candidate freeze gate required"):
                    self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
            self.assertEqual(len(calls), 1)

    def test_passing_historical_validation_freezes_one_canonical_candidate(self) -> None:
        self.create_obsolete_failure(); self.authorize(); self.attest()
        with self.assertRaisesRegex(RuntimeError, "candidate freeze gate"):
            self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
        result = self.ledger.historical_revalidation_validation_result(self.ticket, 1); assert result is not None
        candidate = self.controller.freeze_historical_candidate(self.ticket, 1, repository=self.repo)
        replay = self.controller.freeze_historical_candidate(self.ticket, 1, repository=self.repo)
        self.assertEqual(candidate["candidate_fingerprint"], result["implementation_diff_hash"])
        self.assertEqual(candidate["candidate_fingerprint"], replay["candidate_fingerprint"])
        self.assertEqual(candidate["historical_provenance_json"], replay["historical_provenance_json"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM review_candidates WHERE ticket_id=?", (self.ticket,)).fetchone()[0], 1)
        self.assertEqual(self.model.calls, ["implementation"])

    def test_incomplete_claim_fails_closed_before_validator(self) -> None:
        self.create_obsolete_failure(); authorization = self.authorize(); attestation = self.attest()
        impl = self.ledger.model_stage(self.ticket, 1, "implementation"); assert impl is not None
        self.ledger.claim_historical_revalidation_validation(ticket_id=self.ticket, attempt_number=1, authorization_hash_value=str(authorization["authorization_hash"]), attestation_hash_value=str(attestation["attestation_hash"]), base_sha=str(impl["base_sha"]), implementation_diff_hash=str(impl["diff_hash"]), validation_profile_hash=canonical_sha256(ticket_from_ledger(self.ledger.get_ticket(self.ticket)).contract()))
        with mock.patch.object(DeterministicValidator, "validate") as validate:
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
            validate.assert_not_called()

    def test_post_validation_implementation_mutation_is_rejected(self) -> None:
        self.create_obsolete_failure(); self.authorize(); self.attest()
        original_validate = DeterministicValidator.validate
        attempt = self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=? AND attempt_number=1", (self.ticket,)).fetchone(); assert attempt is not None
        path = Path(attempt["worktree_path"]).joinpath(F1_FILE)
        original = path.read_text(encoding="utf-8")
        def mutate_then_validate(validator: DeterministicValidator, worktree: Path, ticket: Any, *, base_sha: str) -> Any:
            path.write_text(original + "\n// mutated by validation\n", encoding="utf-8")
            return original_validate(validator, worktree, ticket, base_sha=base_sha)
        with mock.patch.object(DeterministicValidator, "validate", new=mutate_then_validate):
            with self.assertRaisesRegex(RuntimeError, "changed during validation"):
                self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
        path.write_text(original, encoding="utf-8")
        self.assertIsNone(self.ledger.historical_revalidation_validation_result(self.ticket, 1))

    def test_exact_f1_attempt_can_receive_bound_authorization(self) -> None:
        self.create_obsolete_failure()
        authorization = self.authorize()
        self.assertEqual(authorization["ticket_id"], self.ticket)
        self.assertEqual(authorization["attempt_number"], 1)
        self.assertEqual(authorization["base_sha"], self.base)
        self.assertEqual(authorization["target_file"], F1_FILE)
        self.assertEqual(authorization["failure_classification"], "obsolete_file_scope_symbol_validation")
        self.assertTrue(authorization["implementation_invocation_id"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM historical_revalidation_authorizations").fetchone()[0], 1)

    def test_identical_replay_is_idempotent_and_conflicts_fail_closed(self) -> None:
        self.create_obsolete_failure(); first = self.authorize(); replay = self.authorize()
        self.assertEqual(replay["authorization_id"], first["authorization_id"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM historical_revalidation_authorizations").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.authorize(reason="different reason")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.ledger.create_historical_revalidation_authorization(
                ticket_id=self.ticket, attempt_number=1, base_sha="f" * 40,
                repository_identity=str(self.repo.resolve()), target_file=F1_FILE,
                failure_classification=first["failure_classification"], failure_evidence_identity=first["failure_evidence_identity"],
                implementation_invocation_id=first["implementation_invocation_id"], operator_id="operator", reason="recognized F1 obsolete rule")

    def test_authorization_requires_latest_attempt_and_rejects_other_identity(self) -> None:
        self.create_obsolete_failure(); first = self.authorize()
        self.ledger.ensure_attempt(self.ticket, 2)
        with self.assertRaisesRegex(ValueError, "latest"):
            self.controller.authorize_historical_revalidation(self.ticket, 1, repository=self.repo)
        with self.assertRaisesRegex(ValueError, "completed implementation"):
            self.controller.authorize_historical_revalidation(self.ticket, 2, repository=self.repo)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.ledger.create_historical_revalidation_authorization(
                ticket_id=self.ticket, attempt_number=1, base_sha=first["base_sha"],
                repository_identity=str(self.repo.resolve()), target_file=F1_FILE,
                failure_classification=first["failure_classification"], failure_evidence_identity=first["failure_evidence_identity"],
                implementation_invocation_id="different-invocation", operator_id="operator", reason="recognized F1 obsolete rule")

    def test_authorization_alone_cannot_bypass_integrity_gate_or_invoke_model(self) -> None:
        self.create_obsolete_failure(); self.authorize()
        with self.assertRaisesRegex(RuntimeError, "implementation-integrity attestation"):
            self.controller.revalidate_historical_implementation(self.ticket, 1, repository=self.repo)
        self.assertEqual(self.model.calls, ["implementation"])
        self.assertIsNone(self.ledger.review_candidate(self.ticket))
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"], CanonicalState.NEEDS_TRIAGE.value)

    def test_other_ticket_cannot_reuse_authorization(self) -> None:
        self.create_obsolete_failure(); authorization = self.authorize()
        other = self.ledger.create_ticket(title="other", state=CanonicalState.NEEDS_TRIAGE, contract=contract())
        self.ledger.bind_runtime(other, str(self.repo.resolve()), self.base)
        self.assertIsNone(self.ledger.historical_revalidation_authorization(other, 1))
        with self.assertRaisesRegex(ValueError, "completed implementation"):
            self.controller.revalidate_historical_implementation(other, 1, repository=self.repo)
        self.assertNotEqual(other, authorization["ticket_id"])


if __name__ == "__main__": unittest.main()
