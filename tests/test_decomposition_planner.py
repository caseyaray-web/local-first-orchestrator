import json, subprocess, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner, PlannerError, packet
from local_first_orchestrator.repository_snapshot import RepositorySnapshot, ManifestEntry, Evidence
from tests.test_decomposition import Plans

class Planner(unittest.TestCase):
    def raw_plan(self):
        p = Plans(); f = p.feature(); s = RepositorySnapshot('r', 'a' * 40, (ManifestEntry('a.py', 'source'),), (Evidence('a.py', 'h', 'feature', ('f',), 1),)); plan = p.plan(repo_base_sha=s.base_sha, repo_snapshot_hash=s.snapshot_hash)
        raw = {'plan_version': plan.plan_version, 'feature_id': plan.feature_id, 'feature_contract_hash': plan.feature_contract_hash, 'repo_base_sha': plan.repo_base_sha, 'repo_snapshot_hash': plan.repo_snapshot_hash, 'architecture_decisions': list(plan.architecture_decisions), 'criterion_coverage': plan.criterion_coverage, 'tranches': [{'id': t.id, 'ordinal': t.ordinal, 'objective': t.objective, 'capabilities': list(t.capabilities), 'criterion_ids': list(t.criterion_ids), 'microtickets': [x.contract() | {'id': x.ticket_id} for x in t.microtickets]} for t in plan.tranches]}
        return f, s, json.dumps(raw)

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

if __name__ == '__main__': unittest.main()
