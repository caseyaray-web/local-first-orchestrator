from __future__ import annotations
import json, hashlib, subprocess
from pathlib import Path
from .decomposition import FeatureContract, DecompositionPlan, Tranche
from .ticket import MicroTicket, PatchBudget, VerificationProfile
from .repository_snapshot import RepositorySnapshot

class PlannerError(RuntimeError):
    pass

def packet(feature: FeatureContract, snapshot: RepositorySnapshot, *, max_active: int = 4, max_files: int = 3, max_lines: int = 200, prior_decisions: tuple[str, ...] = ()) -> str:
    return json.dumps({'feature': {**feature.__dict__, 'contract_hash': feature.contract_hash}, 'repository': {'id': snapshot.repository_id, 'base_sha': snapshot.base_sha, 'snapshot_hash': snapshot.snapshot_hash, 'manifest': [x.__dict__ for x in snapshot.manifest], 'evidence': [x.__dict__ for x in snapshot.entries], 'omitted_count': snapshot.omitted_count}, 'limits': {'active': max_active, 'max_files': max_files, 'max_lines': max_lines}, 'prior_decisions': prior_decisions, 'rules': ['planning only: return one DecompositionPlan JSON object', 'never edit, execute, inspect, or report repository changes', 'do not output prose, diffs, patches, implementation reports, or test results', 'do not invent criteria or broaden scope', 'use supplied repository-relative path::symbol only', 'materialize active tranche only', 'output JSON only']}, sort_keys=True, separators=(',', ':'), default=lambda x: x.__dict__ if hasattr(x, '__dict__') else list(x))

def _ticket(x: dict) -> MicroTicket:
    v = x['verification']
    return MicroTicket(x.get('id', x.get('ticket_id')), x['objective'], tuple(x['criterion_ids']), x['primary_symbol'], tuple(x['allowed_files']), tuple(x['forbidden_changes']), PatchBudget(**x['patch_budget']), VerificationProfile(tuple(tuple(a) for a in v['commands']), v.get('working_directory', '.')), x['risk'], x['review_required'], x['max_attempts'], tuple(x.get('dependencies', ())), tuple(x.get('new_test_files', ())))

def parse(raw: str) -> DecompositionPlan:
    try:
        x = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlannerError('malformed planner JSON') from exc
    try:
        return DecompositionPlan(x['plan_version'], x['feature_id'], x['feature_contract_hash'], x['repo_base_sha'], x['repo_snapshot_hash'], tuple(x['architecture_decisions']), {k: tuple(v) for k, v in x['criterion_coverage'].items()}, tuple(Tranche(t['id'], t['ordinal'], t['objective'], tuple(t['capabilities']), tuple(t['criterion_ids']), tuple(_ticket(y) for y in t.get('microtickets', ()))) for t in x['tranches']), tuple(x.get('scope_change_proposals', ())), tuple(x.get('unresolved_questions', ())), x.get('repository_identity', ''), x.get('repo_snapshot_manifest_json', ''))
    except (KeyError, TypeError, ValueError) as exc:
        raise PlannerError('invalid planner schema') from exc

def _protected_fingerprint(repository: Path) -> dict:
    repo = Path(repository).resolve()
    def run(*args: str) -> str:
        return subprocess.run(('git', *args), cwd=repo, text=True, capture_output=True, check=True).stdout
    status = run('status', '--porcelain=v1')
    paths = []
    for line in status.splitlines():
        raw = line[3:] if len(line) >= 4 else ''
        if ' -> ' in raw:
            raw = raw.split(' -> ', 1)[1]
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        if raw:
            paths.append(raw)
    hashes = {}
    for path in sorted(set(paths)):
        result = subprocess.run(('git', 'hash-object', '--', path), cwd=repo, text=True, capture_output=True, check=False)
        hashes[path] = result.stdout.strip() if result.returncode == 0 else None
    return {'head': run('rev-parse', 'HEAD').strip(), 'status': status, 'path_hashes': hashes}

def resolve_hermes_identity(executable='hermes') -> dict[str,str]:
    result = subprocess.run((executable, 'config'), text=True, capture_output=True, check=False)
    text = result.stdout or ''
    import re
    match = re.search(r"Model:\s+\{'default': '([^']+)', 'provider': '([^']+)'", text)
    if result.returncode or not match:
        return {'provider': 'unresolved', 'model': 'unresolved', 'profile': 'default'}
    return {'provider': match.group(2), 'model': match.group(1), 'profile': 'default'}

class LocalDecompositionPlanner:
    def __init__(self, runner=subprocess.run, executable='hermes', cost_class: str = 'unknown', provider='unresolved', model='unresolved', profile='unresolved'):
        if cost_class not in {'local', 'paid', 'unknown'}:
            raise ValueError('invalid planner cost class')
        self.runner, self.executable, self.cost_class = runner, executable, cost_class
        self.provider, self.model, self.profile = provider, model, profile

    @property
    def is_paid(self):
        return self.cost_class == 'paid'

    def propose(self, feature, snapshot, *, artifact_dir: Path, prior_decisions=(), repository: Path | None = None):
        if self.cost_class == 'unknown':
            raise PlannerError('unknown planner cost class')
        payload = packet(feature, snapshot, prior_decisions=prior_decisions)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        scratch = artifact_dir / 'planner-scratch'
        scratch.mkdir(exist_ok=True)
        provenance = {'provider': self.provider, 'model': self.model, 'profile': self.profile, 'cost_class': self.cost_class, 'mechanism': 'hermes-chat', 'tool_mode': 'safe-no-mutation-tools', 'toolsets': ['safe'], 'cwd': str(scratch)}
        (artifact_dir / 'planner-request.json').write_text(payload, encoding='utf-8')
        (artifact_dir / 'planner-provenance.json').write_text(json.dumps(provenance, sort_keys=True, separators=(',', ':')), encoding='utf-8')
        before = _protected_fingerprint(repository) if repository is not None else None
        if before is not None:
            (artifact_dir / 'protected-before.json').write_text(json.dumps(before, sort_keys=True, separators=(',', ':')), encoding='utf-8')
        failure = None
        try:
            argv = [self.executable, 'chat', '--toolsets', 'safe']
            if self.provider != 'unresolved': argv.extend(('--provider', self.provider))
            if self.model != 'unresolved': argv.extend(('--model', self.model))
            argv.extend(('--query', payload, '--quiet'))
            result = self.runner(tuple(argv), text=True, capture_output=True, timeout=300, check=False, cwd=str(scratch))
            raw = result.stdout or ''
            (artifact_dir / 'planner-response.json').write_text(raw, encoding='utf-8')
            if result.returncode:
                failure = PlannerError('planner failure')
            elif not raw.strip():
                failure = PlannerError('empty planner response')
            else:
                plan = parse(raw)
                if before is not None:
                    after = _protected_fingerprint(repository)
                    (artifact_dir / 'protected-after.json').write_text(json.dumps(after, sort_keys=True, separators=(',', ':')), encoding='utf-8')
                    if before != after:
                        failure = PlannerError('protected repository mutated: ' + json.dumps({'before': before, 'after': after}, sort_keys=True, separators=(',', ':')))
                if failure is None:
                    return plan
        except subprocess.TimeoutExpired as exc:
            failure = PlannerError('planner timeout')
        except PlannerError as exc:
            failure = exc
        finally:
            if before is not None and not (artifact_dir / 'protected-after.json').exists():
                try:
                    (artifact_dir / 'protected-after.json').write_text(json.dumps(_protected_fingerprint(repository), sort_keys=True, separators=(',', ':')), encoding='utf-8')
                except Exception:
                    pass
        raise failure or PlannerError('planner failed')
