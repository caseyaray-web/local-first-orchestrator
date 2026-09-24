from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.ledger import _stable_scheduler_failure_fingerprint, canonical_sha256
from local_first_orchestrator.cli import main
from local_first_orchestrator.native_release_approval import canonical_stale_routing_recovery_bytes
from local_first_orchestrator.operator_config import ModelRegistration, OperatorConfig, operator_authority_hash_from_fingerprint, save_operator_config
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next
from local_first_orchestrator.states import CanonicalState


class StaleRoutingRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.db'
        self.ledger = Ledger(self.path)
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)
        self.ticket = self.ledger.create_ticket(title='stale routing', state=CanonicalState.LOCAL_REVIEW)
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.fingerprint = hashlib.sha256(self.public).hexdigest()
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        subprocess.run(('git', 'init', '-q'), cwd=self.repo, check=True)
        self.config = Path(self.temp.name) / 'operator.json'
        model = ModelRegistration('test-profile', 'test-provider', 'test-model')
        save_operator_config(OperatorConfig(self.path, self.repo, (self.repo,), model, model, Path(self.temp.name) / 'worktrees', Path(self.temp.name) / 'artifacts', 300, 300, operator_signing_public_key=base64.b64encode(self.public).decode(), operator_signing_key_fingerprint=self.fingerprint), self.config)
        db = self.ledger.connection
        db.execute("UPDATE tickets SET lease_owner=NULL,lease_expires_at=NULL,verification_json=? WHERE id=?", (json.dumps({'commands': []}), self.ticket))
        db.execute("INSERT INTO attempts(ticket_id,attempt_number,base_sha,created_at) VALUES (?,?,?,?)", (self.ticket, 1, 'base', 1))
        db.execute("INSERT INTO runtime_bindings(ticket_id,repository_path,starting_sha,ownership_verified,operator_signer_fingerprint,operator_authority_hash,created_at) VALUES (?,?,?,?,?,?,?)", (self.ticket, str(self.repo), 'base', 1, self.fingerprint, operator_authority_hash_from_fingerprint(self.fingerprint), 1))
        implementation_artifact = Path(self.temp.name) / 'implementation.json'
        implementation_artifact.write_text('{"payload":"implementation"}')
        db.execute("INSERT INTO model_stage_artifacts(ticket_id,attempt_number,stage,purpose,adapter,request_hash,response_artifact,worktree_path,base_sha,diff_hash,completed_at) VALUES (?,1,'implementation','implementation','model','request',?,?,?,?,80)", (self.ticket, str(implementation_artifact), str(self.repo), 'base', 'diff'))
        ticket_row = db.execute('SELECT * FROM tickets WHERE id=?', (self.ticket,)).fetchone()
        validation_identity = {'ticket_id': self.ticket, 'attempt_number': 1, 'implementation_artifact': str(implementation_artifact), 'implementation_artifact_sha256': hashlib.sha256(implementation_artifact.read_bytes()).hexdigest(), 'worktree_path': str(self.repo), 'base_sha': 'base', 'implementation_diff_hash': 'diff', 'validation_policy_hash': self.ledger._validation_policy_hash(ticket_row)}
        validation_artifact = Path(self.temp.name) / 'validation.json'
        validation_artifact.write_text('{"passed":false}')
        validation_sha = hashlib.sha256(validation_artifact.read_bytes()).hexdigest()
        validation = {'candidate_identity': validation_identity, 'passed': False, 'compact_evidence': 'old failure', 'validation_artifact': str(validation_artifact), 'validation_artifact_sha256': validation_sha}
        db.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,attempt_count,side_effect_started_at,side_effect_completed_at,finalized_at,candidate_identity_json,result_json,created_at,updated_at) VALUES ('validation-claim',?,'validation:1','completed',1,90,95,96,?,?,90,96)", (self.ticket,json.dumps(validation_identity),json.dumps(validation)))
        db.execute("INSERT INTO runtime_stages(ticket_id,stage,detail,attempt_number,artifact_path,artifact_sha256,created_at) VALUES (?,'validation-1',?,1,?,?,95)", (self.ticket,json.dumps(validation),str(validation_artifact),validation_sha))
        identity = {'ticket_id': self.ticket, 'attempt_number': 1, 'implementation_diff_hash': 'diff'}
        db.execute("INSERT INTO review_candidates(ticket_id,attempt_number,candidate_fingerprint,validation_evidence,runtime_identity_json,status,created_at,updated_at) VALUES (?,?,?,?,?,'review_pending',?,?)", (self.ticket, 1, 'diff', 'old failure', json.dumps(identity), 1, 1))
        artifact = Path(self.temp.name) / 'review.json'
        artifact.write_text(json.dumps({'provider': 'model', 'model': 'test', 'payload': {'verdict': 'pass', 'criterion_results': [], 'findings': [], 'suggestions': []}}))
        db.execute("INSERT INTO model_stage_artifacts(ticket_id,attempt_number,stage,purpose,adapter,request_hash,response_artifact,worktree_path,base_sha,diff_hash,completed_at) VALUES (?,?, 'review','review','model','request',?,?,?, ?,?)", (self.ticket, 1, str(artifact), str(Path(self.temp.name)), 'base', 'diff', 120))
        review = {'ticket_id': self.ticket, 'attempt_number': 1, 'review_artifact': str(artifact), 'candidate_identity': identity, 'review_verdict': 'pass', 'findings': [], 'criterion_results': [], 'suggestions': [], 'review_raw': {'verdict': 'pass', 'criterion_results': [], 'findings': [], 'suggestions': []}}
        db.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,attempt_count,side_effect_started_at,side_effect_completed_at,finalized_at,candidate_identity_json,result_json,created_at,updated_at) VALUES ('review-claim',?,'review:1','completed',1,115,120,121,?,?,115,121)", (self.ticket,json.dumps(identity),json.dumps(review)))
        fingerprint = _stable_scheduler_failure_fingerprint(self.ticket, 'validation', 'old failure')
        routing = {'ticket_id': self.ticket, 'attempt_number': 1, 'source': 'validation', 'action': 'triage', 'failure_fingerprint': fingerprint, 'failure_evidence': 'old failure'}
        db.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,attempt_count,side_effect_started_at,side_effect_completed_at,finalized_at,result_json,created_at,updated_at) VALUES ('route-claim',?,'repair_routing:1','completed',1,99,100,101,?,99,101)", (self.ticket,json.dumps(routing)))
        db.execute("INSERT INTO runtime_stages(ticket_id,stage,detail,attempt_number,created_at) VALUES (?,'repair-routing-1',?,1,100)", (self.ticket,json.dumps(routing)))
        feedback = {'ticket_id': self.ticket, 'attempt_number': 1, 'failure_fingerprint': fingerprint, 'failure_evidence': 'old failure', 'feedback': 'obsolete'}
        db.execute("INSERT INTO runtime_stages(ticket_id,stage,detail,attempt_number,created_at) VALUES (?,'triage-feedback-1',?,1,99)", (self.ticket,json.dumps(feedback)))
        self.ledger.pause('operator', reason='recovery')

    def document(self, request_id: str = 'unique-request') -> dict:
        return {'domain': 'stale-review-routing-recovery', 'version': 1, 'operation': 'supersede-stale-routing', 'request_id': request_id, 'nonce': 'external-unique-nonce', 'operator_id': 'operator', 'reason': 'review supersedes obsolete validation triage', 'authority': self.ledger.stale_routing_recovery_projection(self.ticket, 1, operator_config_path=self.config)}

    def execute(self, document: dict | None = None) -> dict:
        document = document or self.document()
        with patch.object(self.ledger, '_now', return_value=1000):
            return self.ledger.supersede_stale_routing_after_review(self.ticket, attempt_number=1, operator_id=document['operator_id'], reason=document['reason'], approval_document=document, detached_signature=self.key.sign(canonical_stale_routing_recovery_bytes(document)), operator_config_path=self.config)

    def snapshot(self) -> list:
        names = [row[0] for row in self.ledger.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return [(name, [tuple(row) for row in self.ledger.connection.execute(f'SELECT * FROM "{name}" ORDER BY 1')]) for name in names]

    def test_archived_failed_validation_then_later_pass_can_supersede_old_routing(self) -> None:
        old_claim = self.ledger.connection.execute("SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage='validation:1'", (self.ticket,)).fetchone()
        old_stage = self.ledger.connection.execute("SELECT * FROM runtime_stages WHERE ticket_id=? AND stage='validation-1'", (self.ticket,)).fetchone()
        archive_detail = {"original_stage": "validation-1", "terminal_generation": 1, "reason": "replayed validation", "record": json.loads(old_stage['detail']), "claim_status": "completed", "claim_result": {**json.loads(old_claim['result_json']), "ticket_id": self.ticket, "replayed": False}}
        self.ledger.connection.execute("INSERT INTO runtime_stages(ticket_id,stage,detail,attempt_number,artifact_path,artifact_sha256,created_at) VALUES (?,?,?,?,?,?,?)", (self.ticket, 'validation-controller-defect-archive-1-1', json.dumps(archive_detail), 1, old_stage['artifact_path'], old_stage['artifact_sha256'], 105))
        new_artifact = Path(self.temp.name) / 'replay-validation.json'
        new_artifact.write_text('{"passed":true}')
        digest = hashlib.sha256(new_artifact.read_bytes()).hexdigest()
        new_validation = {"candidate_identity": json.loads(old_claim['candidate_identity_json']), "passed": True, "compact_evidence": "new validation passed", "validation_artifact": str(new_artifact), "validation_artifact_sha256": digest}
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET result_json=?,side_effect_completed_at=110,finalized_at=111 WHERE claim_id='validation-claim'", (json.dumps(new_validation),))
        self.ledger.connection.execute("UPDATE runtime_stages SET detail=?,artifact_path=?,artifact_sha256=?,created_at=110 WHERE ticket_id=? AND stage='validation-1'", (json.dumps(new_validation), str(new_artifact), digest, self.ticket))
        self.ledger.connection.execute("UPDATE review_candidates SET validation_evidence=? WHERE ticket_id=?", ('new validation passed', self.ticket))
        self.ledger.connection.commit()
        doc = self.document('archived-failure-new-pass')
        self.assertEqual(doc['authority']['routing_claim_id'], 'route-claim')
        archive = self.ledger.connection.execute("SELECT * FROM runtime_stages WHERE ticket_id=? AND stage=?", (self.ticket, 'validation-controller-defect-archive-1-1')).fetchone()
        altered = json.loads(archive['detail']); altered['record']['compact_evidence'] = 'different failure'
        self.ledger.connection.execute("UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage=?", (json.dumps(altered), self.ticket, archive['stage']))
        with self.assertRaises(ValueError):
            self.ledger.stale_routing_recovery_projection(self.ticket, 1, operator_config_path=self.config)
        self.ledger.connection.execute("UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage=?", (archive['detail'], self.ticket, archive['stage']))
        archived_artifact = Path(archive['artifact_path']); original_bytes = archived_artifact.read_bytes()
        archived_artifact.write_bytes(b'tampered archive artifact')
        with self.assertRaises(ValueError):
            self.ledger.stale_routing_recovery_projection(self.ticket, 1, operator_config_path=self.config)
        archived_artifact.write_bytes(original_bytes)
        result = self.execute(doc)
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(preview_next(self.ledger, now=200).next_stage, 'paused')

    def test_signed_supersession_preserves_prior_evidence_and_replay(self) -> None:
        doc = self.document()
        self.assertEqual(doc['authority']['routing_claim_id'], 'route-claim')
        before = preview_next(self.ledger, now=200)
        self.assertEqual(before.next_stage, 'paused')
        result = self.execute(doc)
        self.assertEqual(result['status'], 'superseded')
        self.assertIsNone(self.ledger.runtime_stage(self.ticket, 'repair-routing-1'))
        self.assertIsNone(self.ledger.runtime_stage(self.ticket, 'triage-feedback-1'))
        archive = self.ledger.connection.execute('SELECT * FROM stale_routing_recovery_archives').fetchone()
        self.assertIn('old failure', archive['routing_claim_json'])
        self.assertIn('obsolete', archive['feedback_stage_json'])
        self.assertEqual(self.ledger.connection.execute("SELECT status FROM scheduler_stage_claims WHERE claim_id='route-claim'").fetchone()[0], 'claimed')
        state = self.snapshot()
        self.assertEqual(self.execute(doc)['status'], 'already_superseded')
        self.assertEqual(state, self.snapshot())
        with self.assertRaises((ValueError, PermissionError)):
            self.execute({**doc, 'request_id': 'different'})
        self.assertEqual(state, self.snapshot())
        self.assertEqual(preview_next(self.ledger, now=200).next_stage, 'paused')
        self.ledger.resume('operator', reason='fixture only')
        self.assertEqual(preview_next(self.ledger, now=1001).next_stage, 'repair_routing')
        claim = self.ledger.claim_next_scheduler_repair_routing('router', lease_seconds=60, now=1001, ticket_id=self.ticket)
        self.assertEqual(claim['claim_id'], 'route-claim')
        self.assertEqual(self.ledger.plan_scheduler_repair_routing_effect('route-claim', 'router', now=1001)['action'], 'pass')

    def test_rejections_leave_every_row_unchanged(self) -> None:
        doc = self.document()
        changes = (
            "UPDATE scheduler_stage_claims SET lease_owner='active',lease_expires_at=999 WHERE claim_id='route-claim'",
            "UPDATE scheduler_stage_claims SET result_json='{broken' WHERE claim_id='route-claim'",
            "UPDATE scheduler_stage_claims SET result_json='{}' WHERE claim_id='review-claim'",
            "UPDATE scheduler_stage_claims SET result_json='[]' WHERE claim_id='review-claim'",
            "UPDATE review_candidates SET candidate_fingerprint='other' WHERE ticket_id=?",
            "UPDATE runtime_bindings SET repository_path='/wrong/repository' WHERE ticket_id=?",
            "UPDATE runtime_stages SET detail='{}' WHERE stage='triage-feedback-1'",
            "UPDATE tickets SET lease_owner='active',lease_expires_at=999 WHERE id=?",
            "INSERT INTO attempts(ticket_id,attempt_number,base_sha,created_at) VALUES (?,2,'base',1)",
            "INSERT INTO review_results(ticket_id,attempt_number,verdict,payload_json,created_at) VALUES (?,1,'pass','{}',1)",
        )
        for command in changes:
            with self.subTest(command=command):
                baseline = sqlite3.connect(':memory:')
                self.ledger.connection.backup(baseline)
                self.ledger.connection.execute(command, (self.ticket,) if '?' in command else ())
                before = self.snapshot()
                with self.assertRaises((ValueError, PermissionError)):
                    self.execute(doc)
                self.assertEqual(before, self.snapshot())
                baseline.backup(self.ledger.connection)
                baseline.close()

    def test_wrong_signature_authority_and_transaction_rollback(self) -> None:
        doc = self.document()
        before = self.snapshot()
        with self.assertRaises((ValueError, PermissionError)):
            self.ledger.supersede_stale_routing_after_review(self.ticket, attempt_number=1, operator_id='operator', reason=doc['reason'], approval_document=doc, detached_signature=b'bad', operator_config_path=self.config)
        self.assertEqual(before, self.snapshot())
        altered = {**doc, 'authority': {**doc['authority'], 'ledger_identity': '/another/database'}}
        with self.assertRaises((ValueError, PermissionError)):
            self.execute(altered)
        self.assertEqual(before, self.snapshot())
        self.ledger.connection.execute("CREATE TRIGGER reject_reopen BEFORE UPDATE ON scheduler_stage_claims WHEN NEW.claim_id='route-claim' BEGIN SELECT RAISE(ABORT, 'forced rollback'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_tampered_review_artifact_rejected_before_audit_write(self) -> None:
        doc = self.document()
        artifact = Path(self.temp.name) / 'review.json'
        artifact.write_text(artifact.read_text() + '\n')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_tampered_feedback_artifact_rejected_without_mutation(self) -> None:
        feedback = Path(self.temp.name) / 'feedback.json'
        feedback.write_text('{"payload":"old"}')
        digest = hashlib.sha256(feedback.read_bytes()).hexdigest()
        self.ledger.connection.execute("UPDATE runtime_stages SET artifact_path=?,artifact_sha256=? WHERE stage='triage-feedback-1'", (str(feedback), digest))
        doc = self.document()
        feedback.write_text(feedback.read_text() + '\n')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_active_tick_rejects_prepare_execution_and_replay_without_mutation(self) -> None:
        doc = self.document()
        tick = "INSERT INTO scheduler_tick_lease(id,lease_owner,lease_token,lease_expires_at,updated_at) VALUES (1,'other','token',9999999999,1)"
        self.ledger.connection.execute(tick)
        before = self.snapshot()
        with self.assertRaises(PermissionError):
            self.ledger.stale_routing_recovery_projection(self.ticket, 1, operator_config_path=self.config)
        with self.assertRaises(PermissionError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())
        self.ledger.connection.execute('DELETE FROM scheduler_tick_lease')
        self.execute(doc)
        self.ledger.connection.execute(tick)
        before = self.snapshot()
        with self.assertRaises(PermissionError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_public_api_rejects_caller_clock_and_active_tick(self) -> None:
        doc = self.document()
        self.ledger.connection.execute("INSERT INTO scheduler_tick_lease(id,lease_owner,lease_token,lease_expires_at,updated_at) VALUES (1,'other','token',2000,1)")
        before = self.snapshot()
        with self.assertRaises(TypeError):
            self.ledger.supersede_stale_routing_after_review(self.ticket, attempt_number=1, operator_id='operator', reason=doc['reason'], approval_document=doc, detached_signature=self.key.sign(canonical_stale_routing_recovery_bytes(doc)), operator_config_path=self.config, now=9999999999)
        with self.assertRaises(PermissionError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_legacy_unsigned_methods_cannot_change_authority(self) -> None:
        before = self.snapshot()
        for method in ('reconcile_stale_repair_routing_after_review', 'reconcile_stale_triage_feedback_after_review'):
            with self.subTest(method=method), self.assertRaises((AttributeError, PermissionError)):
                getattr(self.ledger, method)(self.ticket, attempt_number=1, operator_id='ordinary', reason='unsigned', now=1000)
            self.assertEqual(before, self.snapshot())

    def test_config_changed_after_last_body_check_before_commit_rolls_back(self) -> None:
        doc = self.document()
        before = self.snapshot()
        def mutate(point):
            if point == 'after_validation_recovery_commit_guard_before_sqlite_commit':
                self.config.write_bytes(self.config.read_bytes() + b' ')
        with patch.object(self.ledger, '_inject_failure', side_effect=mutate):
            with self.assertRaises(PermissionError):
                self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_config_inode_replaced_after_last_check_before_commit_rolls_back(self) -> None:
        doc = self.document()
        before = self.snapshot()
        def replace(point):
            if point == 'after_validation_recovery_commit_guard_before_sqlite_commit':
                replacement = self.config.with_suffix('.replacement')
                replacement.write_bytes(self.config.read_bytes())
                replacement.replace(self.config)
        with patch.object(self.ledger, '_inject_failure', side_effect=replace):
            with self.assertRaises(PermissionError):
                self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_config_replacement_and_key_drift_reject_without_mutation(self) -> None:
        doc = self.document()
        before = self.snapshot()
        replacement = self.config.read_bytes()
        self.config.write_bytes(replacement + b' ')
        with self.assertRaises((PermissionError, ValueError)):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())
        other = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        altered = json.loads(replacement)
        altered['operator_signing_public_key'] = base64.b64encode(other).decode()
        altered['operator_signing_key_fingerprint'] = hashlib.sha256(other).hexdigest()
        self.config.write_text(json.dumps(altered))
        with self.assertRaises((PermissionError, ValueError)):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_config_replaced_during_decision_rolls_back(self) -> None:
        doc = self.document()
        before = self.snapshot()
        original = self.ledger._stale_routing_recovery_evidence
        def replace_config(*args):
            result = original(*args)
            changed = self.config.with_suffix('.replacement')
            changed.write_bytes(self.config.read_bytes())
            changed.replace(self.config)
            return result
        with patch.object(self.ledger, '_stale_routing_recovery_evidence', side_effect=replace_config):
            with self.assertRaises((ValueError, PermissionError)):
                self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def populate_triage(self) -> None:
        db = self.ledger.connection
        ticket = db.execute('SELECT * FROM tickets WHERE id=?', (self.ticket,)).fetchone()
        identity = self.ledger._triage_claim_identity(ticket, attempt_number=1, failure_evidence='old failure', triage_execution_policy_hash='policy')
        artifact = Path(self.temp.name) / 'triage.json'
        artifact.write_text(json.dumps({'payload': {'suggestions': ['obsolete']}}))
        result = {'ticket_id': self.ticket, 'attempt_number': 1, 'candidate_identity': identity, 'triage_artifact': str(artifact), 'proposal_hash': canonical_sha256({'suggestions': ['obsolete']})}
        db.execute("INSERT INTO model_stage_artifacts(ticket_id,attempt_number,stage,purpose,adapter,request_hash,response_artifact,worktree_path,base_sha,diff_hash,completed_at) VALUES (?,1,'triage','triage','model','request',?,?,?,?,110)", (self.ticket, str(artifact), str(self.repo), 'base', canonical_sha256(identity)))
        db.execute("INSERT INTO runtime_stages(ticket_id,stage,detail,attempt_number,created_at) VALUES (?,'triage-applied-1',?,1,110)", (self.ticket, json.dumps(result),))
        db.execute("INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,attempt_count,side_effect_started_at,side_effect_completed_at,finalized_at,candidate_identity_json,result_json,created_at,updated_at) VALUES ('triage-claim',?,'triage:1','completed',1,105,110,111,?,?,105,111)", (self.ticket, json.dumps(identity), json.dumps(result)))

    def test_production_shaped_triage_superseded_and_replayed(self) -> None:
        self.populate_triage()
        doc = self.document()
        self.assertEqual(self.execute(doc)['status'], 'superseded')
        archive = self.ledger.connection.execute('SELECT * FROM stale_routing_recovery_archives').fetchone()
        self.assertIn('failure_evidence_hash', archive['triage_claim_json'])
        self.assertIsNotNone(archive['triage_model_json'])
        self.assertIsNone(self.ledger.runtime_stage(self.ticket, 'triage-applied-1'))
        row = self.ledger.connection.execute("SELECT status,last_error FROM scheduler_stage_claims WHERE claim_id='triage-claim'").fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertIn('superseded', row['last_error'])
        before = self.snapshot()
        self.assertEqual(self.execute(doc)['status'], 'already_superseded')
        self.assertEqual(before, self.snapshot())

    def test_malformed_triage_identity_rejected_without_mutation(self) -> None:
        self.populate_triage()
        doc = self.document()
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET candidate_identity_json=? WHERE claim_id='triage-claim'", (json.dumps({'ticket_id': self.ticket, 'attempt_number': 1}),))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_triage_model_and_result_mismatch_reject_without_mutation(self) -> None:
        self.populate_triage()
        doc = self.document()
        for sql, params in (
            ("UPDATE model_stage_artifacts SET diff_hash='other' WHERE stage='triage'", ()),
            ("UPDATE scheduler_stage_claims SET result_json=? WHERE claim_id='triage-claim'", (json.dumps({'ticket_id': self.ticket, 'attempt_number': 1}),)),
            ("UPDATE runtime_stages SET detail='{\"ticket_id\":1,\"ticket_id\":2}' WHERE stage='triage-applied-1'", ()),
        ):
            with self.subTest(sql=sql):
                baseline = sqlite3.connect(':memory:')
                self.ledger.connection.backup(baseline)
                self.ledger.connection.execute(sql, params)
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.execute(doc)
                self.assertEqual(before, self.snapshot())
                baseline.backup(self.ledger.connection)
                baseline.close()

    def test_incomplete_triage_model_status_rejects_without_mutation(self) -> None:
        self.populate_triage()
        doc = self.document()
        for status in ('failed', 'pending', ''):
            with self.subTest(status=status):
                self.ledger.connection.execute("UPDATE model_stage_artifacts SET status=? WHERE stage='triage'", (status,))
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.ledger.stale_routing_recovery_projection(self.ticket, 1, operator_config_path=self.config)
                with self.assertRaises(ValueError):
                    self.execute(doc)
                self.assertEqual(before, self.snapshot())
        self.ledger.connection.execute("UPDATE model_stage_artifacts SET status='completed',purpose='wrong' WHERE stage='triage'")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.ledger.stale_routing_recovery_projection(self.ticket, 1, operator_config_path=self.config)
        with self.assertRaises(ValueError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_mutually_consistent_noncanonical_validation_identity_rejected(self) -> None:
        doc = self.document()
        db = self.ledger.connection
        original = json.loads(db.execute("SELECT candidate_identity_json FROM scheduler_stage_claims WHERE claim_id='validation-claim'").fetchone()[0])
        for field, value in (('worktree_path', '/wrong'), ('base_sha', 'other'), ('validation_policy_hash', 'bad'), ('implementation_artifact_sha256', '0' * 64)):
            with self.subTest(field=field):
                identity = {**original, field: value}
                db.execute("UPDATE scheduler_stage_claims SET candidate_identity_json=? WHERE claim_id='validation-claim'", (json.dumps(identity),))
                for table, column, predicate in (('scheduler_stage_claims', 'result_json', "claim_id='validation-claim'"), ('runtime_stages', 'detail', "stage='validation-1'")):
                    result = json.loads(db.execute(f'SELECT {column} FROM {table} WHERE {predicate}').fetchone()[0])
                    result['candidate_identity'] = identity
                    db.execute(f'UPDATE {table} SET {column}=? WHERE {predicate}', (json.dumps(result),))
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.ledger.stale_routing_recovery_projection(self.ticket, 1, operator_config_path=self.config)
                with self.assertRaises(ValueError):
                    self.execute(doc)
                self.assertEqual(before, self.snapshot())
                db.execute("UPDATE scheduler_stage_claims SET candidate_identity_json=? WHERE claim_id='validation-claim'", (json.dumps(original),))
                for table, column, predicate in (('scheduler_stage_claims', 'result_json', "claim_id='validation-claim'"), ('runtime_stages', 'detail', "stage='validation-1'")):
                    result = json.loads(db.execute(f'SELECT {column} FROM {table} WHERE {predicate}').fetchone()[0])
                    result['candidate_identity'] = original
                    db.execute(f'UPDATE {table} SET {column}=? WHERE {predicate}', (json.dumps(result),))

    def test_duplicate_archive_json_replay_rejected_without_mutation(self) -> None:
        doc = self.document()
        self.execute(doc)
        db = self.ledger.connection
        db.execute('DROP TRIGGER stale_routing_recovery_archives_immutable_update')
        db.execute("UPDATE stale_routing_recovery_archives SET routing_claim_json=substr(routing_claim_json,1,length(routing_claim_json)-1)||',\"claim_id\":\"route-claim\"}'")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_validation_lineage_missing_cross_attempt_and_mismatch_reject(self) -> None:
        doc = self.document()
        variants = (
            ("DELETE FROM scheduler_stage_claims WHERE claim_id='validation-claim'", ()),
            ("DELETE FROM runtime_stages WHERE stage='validation-1'", ()),
            ("UPDATE runtime_stages SET detail='{broken' WHERE stage='validation-1'", ()),
            ("UPDATE runtime_stages SET detail=? WHERE stage='validation-1'", (json.dumps({'candidate_identity': {'ticket_id': self.ticket, 'attempt_number': 2, 'implementation_diff_hash': 'diff'}, 'passed': False, 'compact_evidence': 'old failure'}),)),
            ("UPDATE scheduler_stage_claims SET result_json=? WHERE claim_id='validation-claim'", (json.dumps({'candidate_identity': {'ticket_id': self.ticket, 'attempt_number': 1, 'implementation_diff_hash': 'diff'}, 'passed': False, 'compact_evidence': 'different'}),)),
        )
        for sql, params in variants:
            with self.subTest(sql=sql):
                baseline = sqlite3.connect(':memory:')
                self.ledger.connection.backup(baseline)
                self.ledger.connection.execute(sql, params)
                before = self.snapshot()
                with self.assertRaises((ValueError, PermissionError)):
                    self.execute(doc)
                self.assertEqual(before, self.snapshot())
                baseline.backup(self.ledger.connection)
                baseline.close()

    def test_validation_artifact_byte_drift_rejects_without_mutation(self) -> None:
        doc = self.document()
        (Path(self.temp.name) / 'validation.json').write_text('{"passed":false}\n')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.execute(doc)
        self.assertEqual(before, self.snapshot())

    def test_duplicate_authority_json_rejected_without_mutation(self) -> None:
        doc = self.document()
        for table, column, predicate in (('scheduler_stage_claims', 'result_json', "claim_id='review-claim'"), ('scheduler_stage_claims', 'result_json', "claim_id='route-claim'"), ('runtime_stages', 'detail', "stage='triage-feedback-1'"), ('runtime_stages', 'detail', "stage='validation-1'")):
            with self.subTest(table=table, predicate=predicate):
                baseline = sqlite3.connect(':memory:')
                self.ledger.connection.backup(baseline)
                self.ledger.connection.execute(f"UPDATE {table} SET {column}=substr({column},1,length({column})-1)||',\"ticket_id\":\"' || ? || '\"}}' WHERE {predicate}", (self.ticket,))
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.execute(doc)
                self.assertEqual(before, self.snapshot())
                baseline.backup(self.ledger.connection)
                baseline.close()

    def test_scheduler_applies_reopened_review_once_and_advances_without_board_effect(self) -> None:
        self.execute()
        self.ledger.resume('operator', reason='fixture only')
        self.assertEqual(preview_next(self.ledger, now=1001).next_stage, 'repair_routing')
        class InstrumentedBoard:
            timeout_seconds = 1
            def __init__(self):
                self.writes = []
            def __getattr__(self, name):
                if name in ('set_state', 'deliver_comment', 'create_microticket', 'link_dependency', 'park_native_dependency_child'):
                    def record(*args, **kwargs):
                        self.writes.append((name, args, kwargs))
                        raise AssertionError(f'unexpected board write: {name}')
                    return record
                raise AttributeError(name)
        board = InstrumentedBoard()
        scheduler = ProcessNextScheduler(self.ledger, board, worker_id='fixture-router', target_ticket_id=self.ticket, lease_seconds=60, clock=lambda: 1001)
        result = scheduler.process_next()
        self.assertEqual((result.status, result.stage, result.claim_id), ('completed', 'repair_routing', 'route-claim'))
        review = self.ledger.connection.execute('SELECT verdict FROM review_results WHERE ticket_id=?', (self.ticket,)).fetchall()
        self.assertEqual([row['verdict'] for row in review], ['pass'])
        self.assertEqual(preview_next(self.ledger, now=1002).next_stage, 'acceptance')
        self.assertNotEqual(scheduler.process_next().stage, 'repair_routing')
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_stage_claims WHERE ticket_id=? AND stage='repair_routing:1' AND status='completed'", (self.ticket,)).fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute('SELECT COUNT(*) FROM review_results WHERE ticket_id=?', (self.ticket,)).fetchone()[0], 1)
        self.assertEqual(board.writes, [])
        self.assertEqual(self.ledger.connection.execute('SELECT COUNT(*) FROM board_projection_outbox').fetchone()[0], 0)

    def test_registered_cli_prepare_execute_and_replay(self) -> None:
        root = Path(self.temp.name)
        common = ['--database', str(self.path), '--operator-config-path', str(self.config)]
        flags = ['--task-id', self.ticket, '--attempt-number', '1', '--operator-id', 'operator', '--reason', 'review supersedes obsolete validation triage', '--request-id', 'cli-request']
        approval = root / 'approval.json'
        sig = root / 'signature.bin'
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(common + ['prepare-stale-routing-recovery'] + flags + ['--output-file', str(approval)]), 0)
        self.assertEqual(json.loads(output.getvalue())['request_id'], 'cli-request')
        data = approval.read_bytes()
        doc = json.loads(data)
        self.assertEqual(data, canonical_stale_routing_recovery_bytes(doc))
        sig.write_bytes(self.key.sign(data))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            main(common + ['recover-stale-routing'] + flags[:-1] + ['wrong', '--approval-file', str(approval), '--signature-file', str(sig)])
        self.assertEqual(before, self.snapshot())
        command = common + ['recover-stale-routing'] + flags + ['--approval-file', str(approval), '--signature-file', str(sig)]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(command), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'superseded')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(command), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'already_superseded')
