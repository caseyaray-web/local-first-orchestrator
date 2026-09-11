import json, subprocess, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner, PlannerError, packet, planner_schema, minimal_plan_example, PLANNER_SCHEMA
from local_first_orchestrator.repository_snapshot import RepositorySnapshot, ManifestEntry, Evidence
from tests.test_decomposition import Plans

class Planner(unittest.TestCase):
    def raw_plan(self):
        p = Plans(); f = p.feature(); s = RepositorySnapshot('r', 'a' * 40, (ManifestEntry('a.py', 'source'),), (Evidence('a.py', 'h', 'feature', ('f',), 1),)); plan = p.plan(repo_base_sha=s.base_sha, repo_snapshot_hash=s.snapshot_hash)
        raw = {'plan_version': plan.plan_version, 'feature_id': plan.feature_id, 'feature_contract_hash': plan.feature_contract_hash, 'repo_base_sha': plan.repo_base_sha, 'repo_snapshot_hash': plan.repo_snapshot_hash, 'architecture_decisions': list(plan.architecture_decisions), 'criterion_coverage': plan.criterion_coverage, 'tranches': [{'id': t.id, 'ordinal': t.ordinal, 'objective': t.objective, 'capabilities': list(t.capabilities), 'criterion_ids': list(t.criterion_ids), 'microtickets': [x.contract() | {'id': x.ticket_id} for x in t.microtickets]} for t in plan.tranches]}
        return f, s, json.dumps(raw)

    def test_packet_contains_canonical_schema_paths_and_context_mapping(self):
        f, s, _ = self.raw_plan(); value = json.loads(packet(f, s)); contract = value['output_contract']
        self.assertEqual(contract['schema'], planner_schema()); self.assertEqual(contract['schema'], PLANNER_SCHEMA)
        self.assertEqual(contract['context_to_output']['feature.contract_hash'], 'feature_contract_hash'); self.assertEqual(contract['context_to_output']['repository.base_sha'], 'repo_base_sha')
        self.assertEqual(contract['allowed_paths'], [{'path': 'app.py', 'disposition': 'modify'}, {'path': 'test_app.py', 'disposition': 'create'}])

    def test_generated_minimal_example_passes_production_parser(self):
        f, s, _ = self.raw_plan(); example = minimal_plan_example(f, s); parsed = __import__('local_first_orchestrator.decomposition_planner', fromlist=['parse']).parse(json.dumps(example))
        self.assertEqual(parsed.feature_id, f.id); self.assertEqual(parsed.tranches[0].microtickets[0].ticket_id, 'TK-1')
    def test_tool_free_surface_cwd_and_provenance(self):
        f, s, raw = self.raw_plan(); calls = []
        def run(argv, **kw): calls.append((argv, kw)); return subprocess.CompletedProcess(argv, 0, raw, '')
        with TemporaryDirectory() as d:
            root = Path(d); repo = root / 'repo'; repo.mkdir(); (repo / 'a.py').write_text('x\n')
            subprocess.run(('git', 'init', '-q'), cwd=repo, check=True); subprocess.run(('git', 'add', '.'), cwd=repo, check=True); subprocess.run(('git', '-c', 'user.name=T', '-c', 'user.email=t@t', 'commit', '-qm', 'base'), cwd=repo, check=True)
            got = LocalDecompositionPlanner(run, cost_class='local', provider='p', model='m', profile='cfg').propose(f, s, artifact_dir=root / 'artifacts', repository=repo)
            self.assertEqual(got.feature_id, 'F'); argv, kwargs = calls[0]
            self.assertIn('--toolsets', argv); self.assertIn('safe', argv); self.assertNotIn('--yolo', argv); self.assertNotEqual(Path(kwargs['cwd']).resolve(), repo.resolve())
            provenance = json.loads((root / 'artifacts' / 'planner-provenance.json').read_text()); self.assertEqual(provenance['tool_mode'], 'safe-no-mutation-tools'); self.assertEqual(provenance['model'], 'm'); self.assertTrue((root / 'artifacts' / 'protected-before.json').exists()); self.assertTrue((root / 'artifacts' / 'protected-after.json').exists())

    def test_successor_request_is_scoped_to_coarse_tranche_and_rejects_completed_criteria(self):
        f, s, raw = self.raw_plan(); coarse = Plans().plan().tranches[1]; value = json.loads(raw)
        value['criterion_coverage'] = {'B': ['two']}
        value['tranches'] = [value['tranches'][1]]
        value['tranches'][0]['criterion_ids'] = ['B']
        calls = []
        def run(argv, **kw):
            calls.append((argv, kw)); return subprocess.CompletedProcess(argv, 0, json.dumps(value), '')
        with TemporaryDirectory() as d:
            got = LocalDecompositionPlanner(run, cost_class='standard', provider='p', model='m').propose_next(
                f, s, coarse_tranche=coarse, completion_evidence={'final_integration_sha': s.base_sha}, artifact_dir=Path(d)
            )
            self.assertEqual(got.tranches[0].criterion_ids, ('B',))
            request = json.loads((Path(d) / 'planner-request.json').read_text())
            self.assertEqual(request['successor_scope']['allowed_criterion_ids'], ['B'])
            self.assertEqual(request['successor_scope']['completed_criterion_ids'], ['A'])
            self.assertTrue(request['planner_contract_hash'])
        expanded = json.loads(json.dumps(value))
        expanded['criterion_coverage'] = {'A': ['one'], 'B': ['two']}
        expanded['tranches'][0]['criterion_ids'] = ['A', 'B']
        with TemporaryDirectory() as d:
            with self.assertRaisesRegex(PlannerError, 'criterion scope expanded'):
                LocalDecompositionPlanner(lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(expanded), ''), cost_class='standard').propose_next(
                    f, s, coarse_tranche=coarse, completion_evidence={}, artifact_dir=Path(d)
                )

    def test_successor_completed_response_replays_without_second_model_call(self):
        f, s, raw = self.raw_plan(); coarse = Plans().plan().tranches[1]; value = json.loads(raw)
        value['criterion_coverage'] = {'B': ['two']}
        value['tranches'] = [value['tranches'][1]]
        value['tranches'][0]['criterion_ids'] = ['B']
        calls = []
        def run(argv, **kw):
            calls.append(tuple(argv)); return subprocess.CompletedProcess(argv, 0, json.dumps(value), '')
        with TemporaryDirectory() as d:
            artifacts = Path(d)
            first = LocalDecompositionPlanner(run, cost_class='standard', provider='p', model='m').propose_next(
                f, s, coarse_tranche=coarse, completion_evidence={'final_integration_sha': s.base_sha}, artifact_dir=artifacts
            )
            second = LocalDecompositionPlanner(run, cost_class='standard', provider='p', model='m').propose_next(
                f, s, coarse_tranche=coarse, completion_evidence={'final_integration_sha': s.base_sha}, artifact_dir=artifacts
            )
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            journal = json.loads((artifacts / 'planner-result.json').read_text())
            self.assertEqual((journal['status'], journal['returncode']), ('completed', 0))

    def test_successor_prior_request_without_result_fails_closed_without_model_retry(self):
        f, s, _ = self.raw_plan(); coarse = Plans().plan().tranches[1]
        with TemporaryDirectory() as d:
            artifacts = Path(d)
            planner = LocalDecompositionPlanner(lambda *a, **k: (_ for _ in ()).throw(AssertionError('runner must not be called')), cost_class='standard', provider='p', model='m')
            base = json.loads(packet(f, s, allowed_paths=planner.allowed_paths))
            base['successor_scope'] = {
                'coarse_tranche_id': coarse.id,
                'coarse_tranche_objective': coarse.objective,
                'coarse_tranche_capabilities': list(coarse.capabilities),
                'allowed_criterion_ids': list(coarse.criterion_ids),
                'completed_criterion_ids': ['A'],
                'predecessor_completion': {},
                'required_repo_base_sha': s.base_sha,
            }
            base['rules'] = [
                *base['rules'],
                'successor planning only: return exactly one tranche and only the criteria listed in successor_scope.allowed_criterion_ids',
                'do not include, rematerialize, cover, or create tickets for successor_scope.completed_criterion_ids',
                'criterion_coverage keys must equal successor_scope.allowed_criterion_ids exactly',
                'the single output tranche criterion_ids must equal successor_scope.allowed_criterion_ids exactly',
                'the single output tranche must implement only successor_scope.coarse_tranche_objective and capabilities',
                'repo_base_sha must equal successor_scope.required_repo_base_sha exactly',
                'scope_change_proposals must be empty for successor materialization; never broaden the coarse tranche',
            ]
            payload = json.dumps(base, sort_keys=True, separators=(',', ':'))
            artifacts.mkdir(exist_ok=True)
            scratch = artifacts / 'planner-scratch'; scratch.mkdir()
            provenance = {'role': planner.role, 'routing_source': planner.routing_source, 'planner_contract_hash': planner.planner_contract_hash, 'provider': planner.provider, 'model': planner.model, 'profile': planner.profile, 'cost_class': planner.cost_class, 'mechanism': 'hermes-chat', 'tool_mode': 'safe-no-mutation-tools', 'toolsets': ['safe'], 'cwd': str(scratch), 'successor_scope': True}
            (artifacts / 'planner-request.json').write_text(payload)
            (artifacts / 'planner-provenance.json').write_text(json.dumps(provenance, sort_keys=True, separators=(',', ':')))
            with self.assertRaisesRegex(PlannerError, 'prior invocation outcome unknown'):
                planner.propose_next(f, s, coarse_tranche=coarse, completion_evidence={}, artifact_dir=artifacts)

    def test_mutation_tripwire_fails_closed_and_retains_evidence(self):
        f, s, raw = self.raw_plan()
        with TemporaryDirectory() as d:
            root = Path(d); repo = root / 'repo'; repo.mkdir(); target = repo / 'a.py'; target.write_text('x\n')
            subprocess.run(('git', 'init', '-q'), cwd=repo, check=True); subprocess.run(('git', 'add', '.'), cwd=repo, check=True); subprocess.run(('git', '-c', 'user.name=T', '-c', 'user.email=t@t', 'commit', '-qm', 'base'), cwd=repo, check=True)
            def run(argv, **kw): target.write_text('mutated\n'); return subprocess.CompletedProcess(argv, 0, raw, '')
            with self.assertRaisesRegex(PlannerError, 'protected repository mutated'):
                LocalDecompositionPlanner(run, cost_class='local').propose(f, s, artifact_dir=root / 'artifacts', repository=repo)
            self.assertEqual(target.read_text(), 'mutated\n'); self.assertTrue((root / 'artifacts' / 'planner-response.json').exists()); self.assertTrue((root / 'artifacts' / 'protected-after.json').exists())

    def test_incident_implementation_response_is_rejected(self):
        f, s, _ = self.raw_plan(); incident = '  ┊ review diff\n' + '{"status":"completed","feature":"C11","changed_files":["src/lib/export/json.js"]}'
        def run(argv, **kw): return subprocess.CompletedProcess(argv, 0, incident, '')
        with TemporaryDirectory() as d:
            with self.assertRaisesRegex(PlannerError, 'malformed planner JSON'):
                LocalDecompositionPlanner(run, cost_class='local').propose(f, s, artifact_dir=Path(d))

    def test_fake_process_parses_packet_and_persists_artifacts(self):
        f, s, raw = self.raw_plan(); calls = []
        def run(argv, **kw): calls.append((argv, kw)); return subprocess.CompletedProcess(argv, 0, raw, '')
        with TemporaryDirectory() as d:
            got = LocalDecompositionPlanner(run, cost_class='local').propose(f, s, artifact_dir=Path(d)); self.assertEqual(got.feature_id, 'F'); self.assertIn('planning only', packet(f, s)); self.assertTrue((Path(d) / 'planner-response.json').exists())

    def test_unknown_fields_are_rejected_recursively_with_locations(self):
        f, s, raw = self.raw_plan(); base = json.loads(raw)
        cases = [('root', base | {'status': 'completed'}, 'unknown planner fields at root: status'), ('tranche', (lambda x: (x['tranches'][0].update({'implementation': 'completed'}), x)[1])(json.loads(raw)), 'root.tranches[0]'), ('ticket', (lambda x: (x['tranches'][0]['microtickets'][0].update({'changed_files': []}), x)[1])(json.loads(raw)), 'root.tranches[0].microtickets[0]')]
        for name, value, location in cases:
            with self.subTest(name=name):
                with self.assertRaises(PlannerError) as ctx: LocalDecompositionPlanner(lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(value), ''), cost_class='local').propose(f, s, artifact_dir=Path(TemporaryDirectory().name))
                self.assertIn(location, str(ctx.exception))

    def test_implementation_shaped_valid_json_rejects_all_unknown_root_fields(self):
        f, s, raw = self.raw_plan(); value = json.loads(raw); value.update({'status': 'completed', 'changed_files': ['src/lib/export/json.js'], 'implementation': 'done', 'verification': {'tests': 'passed'}, 'patch': 'diff', 'commands_run': []})
        with TemporaryDirectory() as d:
            with self.assertRaisesRegex(PlannerError, 'unknown planner fields at root: changed_files, commands_run, implementation, patch, status, verification'):
                LocalDecompositionPlanner(lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(value), ''), cost_class='local').propose(f, s, artifact_dir=Path(d))
            self.assertTrue((Path(d) / 'planner-request.json').exists()); self.assertTrue((Path(d) / 'planner-response.json').exists())

    def test_legacy_id_only_ticket_document_remains_valid(self):
        f, s, raw = self.raw_plan(); value = json.loads(raw); ticket = value['tranches'][0]['microtickets'][0]; legacy_id = ticket.pop('ticket_id'); ticket['id'] = legacy_id
        with TemporaryDirectory() as d:
            got = LocalDecompositionPlanner(lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(value), ''), cost_class='local').propose(f, s, artifact_dir=Path(d))
            self.assertEqual(got.tranches[0].microtickets[0].ticket_id, legacy_id)

    def test_schema_rejection_still_records_protected_postcheck(self):
        f, s, raw = self.raw_plan(); value = json.loads(raw); value['tranches'][0]['implementation'] = 'completed'
        with TemporaryDirectory() as d:
            root = Path(d); repo = root / 'repo'; repo.mkdir(); (repo / 'a.py').write_text('x\\n')
            subprocess.run(('git', 'init', '-q'), cwd=repo, check=True); subprocess.run(('git', 'add', '.'), cwd=repo, check=True); subprocess.run(('git', '-c', 'user.name=T', '-c', 'user.email=t@t', 'commit', '-qm', 'base'), cwd=repo, check=True)
            with self.assertRaises(PlannerError): LocalDecompositionPlanner(lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(value), ''), cost_class='local').propose(f, s, artifact_dir=root / 'artifacts', repository=repo)
            self.assertTrue((root / 'artifacts' / 'protected-before.json').exists()); self.assertTrue((root / 'artifacts' / 'protected-after.json').exists()); self.assertTrue((root / 'artifacts' / 'planner-provenance.json').exists())

    def test_strict_types_reject_bool_as_integer_and_object_as_string(self):
        f, s, raw = self.raw_plan(); value = json.loads(raw); value['plan_version'] = True
        with self.assertRaisesRegex(PlannerError, 'expected integer'): LocalDecompositionPlanner(lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(value), ''), cost_class='local').propose(f, s, artifact_dir=Path(TemporaryDirectory().name))
        value = json.loads(raw); value['architecture_decisions'] = {'bad': 'shape'}
        with self.assertRaisesRegex(PlannerError, 'expected string array'): LocalDecompositionPlanner(lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(value), ''), cost_class='local').propose(f, s, artifact_dir=Path(TemporaryDirectory().name))

if __name__ == '__main__': unittest.main()
