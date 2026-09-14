from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.admission import FeatureAdmissionSpec, FileDisposition
from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, PlanValidator, Tranche, create_and_activate_validated_plan
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.ledger import Ledger, _hash_recheck_payload


class _Board:
    is_fake = False


class FeatureAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "existing.js").write_text("export const existing = true;\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self._seed_predecessor_authority()
        self.config = RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,))
        self.controller = LocalFirstController(self.ledger, _Board(), self.config)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()

    def _seed_predecessor_authority(self):
        now = 1
        c = self.ledger.connection
        c.execute("INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('C09.10','prior','planned',?,?)", (now, now))
        c.execute("INSERT INTO tranches(id,feature_id,ordinal,status,base_sha) VALUES ('C09.10-T0','C09.10',0,'active',?)", (self.base,))
        completion = {"tranche_id": "C09.10-T0", "root_planning_sha": self.base, "final_integration_sha": self.base, "accepted_ticket_ids_json": "[]", "accepted_commit_shas_json": "[]"}
        self.ledger.record_tranche_completion(completion)
        h1 = self.ledger.tranche_completion("C09.10-T0")["evidence_hash"]
        recheck_payload={"tranche_id":"C09.10-T0","generation":1,"previous_generation":0,"previous_evidence_hash":h1,"correction_plan_ids":[],"accepted_ticket_ids":[],"accepted_commit_shas":[],"current_integration_sha":self.base,"repository_identity":str(self.repo),"repo_base_sha":self.base,"repo_snapshot_hash":"snapshot","unresolved_correction_count":0,"status":"recheck_passed"}
        c.execute("INSERT INTO tranche_completion_rechecks(tranche_id,generation,previous_generation,previous_evidence_hash,correction_plan_ids_json,accepted_ticket_ids_json,accepted_commit_shas_json,current_integration_sha,repository_identity,repo_base_sha,repo_snapshot_hash,unresolved_correction_count,status,evidence_hash,idempotency_key,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("C09.10-T0",1,0,h1,"[]","[]","[]",self.base,str(self.repo),self.base,"snapshot",0,"recheck_passed",_hash_recheck_payload(recheck_payload),"fixture",now))
        self.ledger.connection.execute("UPDATE controller_state SET paused=1 WHERE id=1")

    def spec(self, **changes):
        data = dict(
            feature_id="C11", feature_title="Meal Planner C11: export and restore", tranche_id="C11-T0", tranche_title="C11 initial tranche",
            objective="Add complete JSON export and restore rehearsal", source_revision=self.base,
            acceptance_criteria=(Criterion("export", "Complete canonical and history export without secrets."), Criterion("restore", "Dry-run and safe restore rehearsal preserve parity.")),
            non_goals=("No unrelated application export expansion.",), invariants=("Household scope is mandatory.",), constraints=("RED GREEN REFACTOR verification is required.",),
            files=(FileDisposition("existing.js", "modify"), FileDisposition("new-test.mjs", "create")), predecessor_tranche_id="C09.10-T0",
        )
        data.update(changes)
        return FeatureAdmissionSpec(**data)

    def test_admission_api_persists_feature_tranche_contract_atomically(self):
        result = self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        self.assertEqual(result.feature_id, "C11")
        self.assertEqual(result.tranche_id, "C11-T0")
        self.assertEqual(self.ledger.connection.execute("select count(*) from features where id='C11'").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from tranches where id='C11-T0'").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from feature_contracts where feature_id='C11'").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from decomposition_plans where feature_id='C11'").fetchone()[0], 0)

    def test_valid_c11_specification_parses_without_field_error(self):
        parsed = FeatureAdmissionSpec.from_json(self.spec().canonical_payload)
        self.assertEqual(parsed, self.spec())

    def test_parser_rejects_unknown_field(self):
        raw = self.spec().canonical_payload | {"unexpected": True}
        with self.assertRaisesRegex(ValueError, "unknown or missing fields"):
            FeatureAdmissionSpec.from_json(raw)

    def test_parser_rejects_missing_required_field(self):
        raw = self.spec().canonical_payload
        raw.pop("objective")
        with self.assertRaisesRegex(ValueError, "unknown or missing fields"):
            FeatureAdmissionSpec.from_json(raw)

    def test_parser_rejects_unknown_and_missing_fields(self):
        raw = self.spec().canonical_payload
        raw.pop("objective")
        raw["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unknown or missing fields"):
            FeatureAdmissionSpec.from_json(raw)

    def test_parser_preserves_all_admission_fields(self):
        parsed = FeatureAdmissionSpec.from_json(self.spec().canonical_payload)
        self.assertEqual(parsed.canonical_payload, self.spec().canonical_payload)

    def test_admission_contract_activates_through_normal_decomposition_lifecycle(self):
        result = self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        row = self.ledger.connection.execute("select * from feature_contracts where feature_id='C11'").fetchone()
        self.assertEqual(row["contract_hash"], self.spec().contract.contract_hash)
        ticket = MicroTicket("c11-export", "Verify the admitted export contract.", ("export", "restore"), "existing.js::existing", ("existing.js",), ("No unrelated files.",), PatchBudget(1, 20), VerificationProfile((("python", "-c", "print(1)"),)), "low", True, 1, ())
        manifest = row["repo_snapshot_manifest_json"]
        plan = DecompositionPlan(1, "C11", self.spec().contract.contract_hash, result.repo_base_sha, result.repo_snapshot_hash, ("admitted",), {"export": ("c11-export",), "restore": ("c11-export",)}, (Tranche("C11-T0", 0, self.spec().objective, ("export",), ("export", "restore"), (ticket,)),), repository_identity=str(self.repo), repo_snapshot_manifest_json=manifest)
        validation = PlanValidator().validate(self.spec().contract, plan)
        self.assertTrue(validation.passed, validation.reasons)
        repository_validation = type("R", (), {"passed": True, "repository_identity": str(self.repo), "base_sha": result.repo_base_sha, "snapshot_hash": result.repo_snapshot_hash, "manifest_json": manifest})()
        create_and_activate_validated_plan(self.ledger, self.spec().contract, plan, validation, repository_validation)
        self.assertEqual(self.ledger.connection.execute("select count(*) from feature_contracts where feature_id='C11'").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from tranches where id='C11-T0'").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from tickets where feature_id='C11'").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select contract_hash from feature_contracts where feature_id='C11'").fetchone()[0], self.spec().contract.contract_hash)

    def test_recheck_predecessor_uses_durable_h2_provenance(self):
        with patch("local_first_orchestrator.controller.CorrectionService.completion_authority", return_value={"authorized": True, "kind": "recheck", "completion": self.ledger.tranche_completion("C09.10-T0"), "final_integration_sha": self.ledger.tranche_completion_rechecks("C09.10-T0")[-1]["current_integration_sha"]}):
            result = self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        h2 = self.ledger.tranche_completion_rechecks("C09.10-T0")[-1]
        row = self.ledger.connection.execute("select predecessor_authority_kind,predecessor_generation,predecessor_evidence_hash,predecessor_final_integration_sha from feature_contracts where feature_id='C11'").fetchone()
        self.assertEqual(row["predecessor_authority_kind"], "recheck")
        self.assertEqual(int(row["predecessor_generation"]), int(h2["generation"]))
        self.assertEqual(row["predecessor_evidence_hash"], h2["evidence_hash"])
        self.assertEqual(row["predecessor_final_integration_sha"], h2["current_integration_sha"])
        self.assertEqual(result.repo_base_sha, h2["current_integration_sha"])

    def test_provenance_and_predecessor_are_persisted(self):
        result = self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        contract = self.ledger.connection.execute("select * from feature_contracts where feature_id='C11'").fetchone()
        tranche = self.ledger.connection.execute("select * from tranches where id='C11-T0'").fetchone()
        self.assertEqual(contract["repo_base_sha"], self.base)
        self.assertEqual(contract["predecessor_tranche_id"], "C09.10-T0")
        self.assertEqual(contract["predecessor_authority_kind"], "h1")
        self.assertEqual(contract["repo_snapshot_hash"], result.repo_snapshot_hash)
        self.assertEqual(json.loads(tranche["criterion_ids_json"]), ["export", "restore"])

    def test_immutable_contract_cannot_be_updated_or_deleted(self):
        self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        with self.assertRaises(Exception): self.ledger.connection.execute("update feature_contracts set contract_json='{}' where feature_id='C11'")
        with self.assertRaises(Exception): self.ledger.connection.execute("delete from feature_contracts where feature_id='C11'")

    def test_conflicting_tranche_identity_fails_closed(self):
        self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        with self.assertRaises(ValueError): self.controller.admit_feature_contract(self.spec(tranche_id="C11-T1"), repository=self.repo)

    def test_no_attempts_or_model_activity_are_created(self):
        self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        self.assertEqual(self.ledger.connection.execute("select count(*) from attempts").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("select count(*) from model_invocations").fetchone()[0], 0)

    def test_identical_replay_is_idempotent_and_conflict_fails(self):
        first = self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        second = self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        self.assertEqual(first.contract_hash, second.contract_hash)
        with self.assertRaises(ValueError):
            self.controller.admit_feature_contract(self.spec(objective="different objective"), repository=self.repo)
        self.assertEqual(self.ledger.connection.execute("select count(*) from features where id='C11'").fetchone()[0], 1)

    def test_paths_are_strictly_validated(self):
        with self.assertRaises(ValueError): self.controller.admit_feature_contract(self.spec(files=(FileDisposition("../escape.js", "modify"),)), repository=self.repo)
        with self.assertRaises(ValueError): self.controller.admit_feature_contract(self.spec(files=(FileDisposition("/absolute.js", "create"),)), repository=self.repo)
        with self.assertRaises(ValueError): self.controller.admit_feature_contract(self.spec(files=(FileDisposition("missing.js", "modify"),)), repository=self.repo)
        with self.assertRaises(ValueError): self.controller.admit_feature_contract(self.spec(files=(FileDisposition("existing.js", "create"),)), repository=self.repo)

    def test_modify_directory_tree_is_rejected(self):
        (self.repo / "tree").mkdir()
        (self.repo / "tree" / "nested.js").write_text("export const nested = true;\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "tree")
        with self.assertRaises(ValueError):
            self.controller.admit_feature_contract(self.spec(files=(FileDisposition("tree", "modify"),)), repository=self.repo)

    def test_h1_only_predecessor_admits_without_recheck_row(self):
        root = Path(TemporaryDirectory().name)
        root.mkdir()
        repo = root / "repo"; repo.mkdir()
        def git(*args): return subprocess.run(("git", *args), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
        git("init", "-q"); git("config", "user.email", "test@example.invalid"); git("config", "user.name", "Test")
        (repo / "existing.js").write_text("export const existing = true;\n", encoding="utf-8")
        git("add", "."); git("commit", "-qm", "base"); base = git("rev-parse", "HEAD")
        ledger = Ledger(root / "ledger.db"); ledger.migrate(); c = ledger.connection
        c.execute("INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('C09.10','prior','planned',1,1)")
        c.execute("INSERT INTO tranches(id,feature_id,ordinal,status,base_sha) VALUES ('C09.10-T0','C09.10',0,'active',?)", (base,))
        ledger.record_tranche_completion({"tranche_id":"C09.10-T0","root_planning_sha":base,"final_integration_sha":base,"accepted_ticket_ids_json":"[]","accepted_commit_shas_json":"[]"})
        c.execute("UPDATE controller_state SET paused=1 WHERE id=1")
        controller = LocalFirstController(ledger, _Board(), RuntimeConfig(repo, root / "worktrees", root / "artifacts", (repo,)))
        result = controller.admit_feature_contract(FeatureAdmissionSpec("C11", "C11", "C11-T0", "initial", "objective", base, (Criterion("export", "export"),), (), (), (), (FileDisposition("existing.js", "modify"),), "C09.10-T0"), repository=repo)
        self.assertEqual(result.predecessor_authority_kind, "h1")
        row = c.execute("SELECT predecessor_authority_kind,predecessor_generation,predecessor_evidence_hash FROM feature_contracts WHERE feature_id='C11'").fetchone()
        self.assertEqual(row["predecessor_authority_kind"], "h1"); self.assertEqual(row["predecessor_generation"], "0"); self.assertEqual(row["predecessor_evidence_hash"], ledger.tranche_completion("C09.10-T0")["evidence_hash"])
        self.assertEqual(c.execute("SELECT count(*) FROM tranche_completion_rechecks WHERE tranche_id='C09.10-T0'").fetchone()[0], 0)
        ledger.close(); root_obj = root

    def test_incomplete_predecessor_rejects_without_rows(self):
        with self.assertRaises(PermissionError): self.controller.admit_feature_contract(self.spec(predecessor_tranche_id="C09.11-T0"), repository=self.repo)
        self.assertEqual(self.ledger.connection.execute("select count(*) from features where id='C11'").fetchone()[0], 0)

    def test_transaction_failure_leaves_no_partial_rows(self):
        original = self.ledger._append_event
        def fail(*args, **kwargs): raise RuntimeError("injected")
        self.ledger._append_event = fail
        with self.assertRaises(RuntimeError): self.controller.admit_feature_contract(self.spec(), repository=self.repo)
        self.ledger._append_event = original
        self.assertEqual(self.ledger.connection.execute("select count(*) from features where id='C11'").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("select count(*) from tranches where id='C11-T0'").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("select count(*) from feature_contracts where feature_id='C11'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
