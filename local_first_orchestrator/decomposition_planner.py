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

def _strict_object(value: object, *, path: str, required: set[str], optional: set[str] = set()) -> dict:
    if type(value) is not dict:
        raise PlannerError(f"planner schema at {path}: expected object")
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise PlannerError(f"unknown planner fields at {path}: {', '.join(unknown)}")
    missing = sorted(required - set(value))
    if missing:
        raise PlannerError(f"missing planner fields at {path}: {', '.join(missing)}")
    return value

def _string(value: object, path: str) -> str:
    if type(value) is not str: raise PlannerError(f"planner schema at {path}: expected string")
    return value

def _integer(value: object, path: str) -> int:
    if type(value) is not int: raise PlannerError(f"planner schema at {path}: expected integer")
    return value

def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool: raise PlannerError(f"planner schema at {path}: expected boolean")
    return value

def _strings(value: object, path: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(x) is not str for x in value): raise PlannerError(f"planner schema at {path}: expected string array")
    return tuple(value)

def _commands(value: object, path: str) -> tuple[tuple[str, ...], ...]:
    if type(value) is not list: raise PlannerError(f"planner schema at {path}: expected command array")
    result=[]
    for i, command in enumerate(value): result.append(_strings(command, f"{path}[{i}]"))
    return tuple(result)

def _ticket(x: dict, path: str) -> MicroTicket:
    data = _strict_object(x, path=path, required={'objective','criterion_ids','primary_symbol','allowed_files','forbidden_changes','patch_budget','verification','risk','review_required','max_attempts','dependencies'}, optional={'id','ticket_id','new_test_files'})
    ticket_id = data.get('ticket_id', data.get('id'))
    if ticket_id is None: raise PlannerError(f"missing planner fields at {path}: ticket_id or id")
    if 'ticket_id' in data and 'id' in data and data['ticket_id'] != data['id']: raise PlannerError(f"planner schema at {path}: id conflicts with ticket_id")
    budget = _strict_object(data['patch_budget'], path=f"{path}.patch_budget", required={'max_files','max_changed_lines'}, optional={'exception_reason'})
    patch_budget = PatchBudget(_integer(budget['max_files'], f"{path}.patch_budget.max_files"), _integer(budget['max_changed_lines'], f"{path}.patch_budget.max_changed_lines"), None if budget.get('exception_reason') is None else _string(budget['exception_reason'], f"{path}.patch_budget.exception_reason"))
    verification = _strict_object(data['verification'], path=f"{path}.verification", required={'commands'}, optional={'working_directory','timeout_seconds','output_limit'})
    profile = VerificationProfile(_commands(verification['commands'], f"{path}.verification.commands"), _string(verification.get('working_directory','.'), f"{path}.verification.working_directory"), _integer(verification.get('timeout_seconds',60), f"{path}.verification.timeout_seconds"), _integer(verification.get('output_limit',20000), f"{path}.verification.output_limit"))
    return MicroTicket(_string(ticket_id, f"{path}.ticket_id"), _string(data['objective'], f"{path}.objective"), _strings(data['criterion_ids'], f"{path}.criterion_ids"), _string(data['primary_symbol'], f"{path}.primary_symbol"), _strings(data['allowed_files'], f"{path}.allowed_files"), _strings(data['forbidden_changes'], f"{path}.forbidden_changes"), patch_budget, profile, _string(data['risk'], f"{path}.risk"), _boolean(data['review_required'], f"{path}.review_required"), _integer(data['max_attempts'], f"{path}.max_attempts"), _strings(data['dependencies'], f"{path}.dependencies"), _strings(data.get('new_test_files',[]), f"{path}.new_test_files"))

def parse(raw: str) -> DecompositionPlan:
    try: x=json.loads(raw)
    except json.JSONDecodeError as exc: raise PlannerError('malformed planner JSON') from exc
    try:
        root=_strict_object(x,path='root',required={'plan_version','feature_id','feature_contract_hash','repo_base_sha','repo_snapshot_hash','architecture_decisions','criterion_coverage','tranches'},optional={'scope_change_proposals','unresolved_questions','repository_identity','repo_snapshot_manifest_json'})
        coverage=root['criterion_coverage']
        if type(coverage) is not dict or any(type(k) is not str for k in coverage) : raise PlannerError('planner schema at root.criterion_coverage: expected string mapping')
        coverage={k:_strings(v,f"root.criterion_coverage.{k}") for k,v in coverage.items()}
        tranches=[]
        for i, raw_tranche in enumerate(root['tranches']):
            path=f'root.tranches[{i}]'; t=_strict_object(raw_tranche,path=path,required={'id','ordinal','objective','capabilities','criterion_ids','microtickets'})
            if type(t['microtickets']) is not list: raise PlannerError(f"planner schema at {path}.microtickets: expected array")
            tranches.append(Tranche(_string(t['id'],f'{path}.id'),_integer(t['ordinal'],f'{path}.ordinal'),_string(t['objective'],f'{path}.objective'),_strings(t['capabilities'],f'{path}.capabilities'),_strings(t['criterion_ids'],f'{path}.criterion_ids'),tuple(_ticket(v,f'{path}.microtickets[{j}]') for j,v in enumerate(t['microtickets']))))
        return DecompositionPlan(_integer(root['plan_version'],'root.plan_version'),_string(root['feature_id'],'root.feature_id'),_string(root['feature_contract_hash'],'root.feature_contract_hash'),_string(root['repo_base_sha'],'root.repo_base_sha'),_string(root['repo_snapshot_hash'],'root.repo_snapshot_hash'),_strings(root['architecture_decisions'],'root.architecture_decisions'),coverage,tuple(tranches),_strings(root.get('scope_change_proposals',[]),'root.scope_change_proposals'),_strings(root.get('unresolved_questions',[]),'root.unresolved_questions'),_string(root.get('repository_identity',''),'root.repository_identity'),_string(root.get('repo_snapshot_manifest_json',''),'root.repo_snapshot_manifest_json'))
    except PlannerError: raise
    except (KeyError,TypeError,ValueError) as exc: raise PlannerError('invalid planner schema') from exc

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
