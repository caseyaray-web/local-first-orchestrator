#!/usr/bin/env bash
# Parent-only, bounded, real-worker/provider M7 rehearsal.  Do not run from a
# delegated child.  All effects are confined to the newly-created RUN_ROOT.
set -Eeuo pipefail
umask 077

# Pins are mandatory and the source tree must be the exact clean candidate.
# The runner archives that commit into RUN_ROOT before any candidate helper is
# inspected or imported; it never falls back to the live checkout.
PM_BOOTSTRAP_PYTHON=${M7_PM_BOOTSTRAP_PYTHON:-$(command -v python3 || true)}
: "${M7_TYPED_CANDIDATE_SOURCE:?missing fresh candidate source}"
: "${M7_TYPED_SOURCE_COMMIT:?missing exact candidate commit}"
: "${M7_TYPED_WHEEL:?missing fresh candidate wheel}"
: "${M7_TYPED_WHEEL_SHA256:?missing fresh wheel SHA-256}"
SOURCE=$M7_TYPED_CANDIDATE_SOURCE
SOURCE_COMMIT=$M7_TYPED_SOURCE_COMMIT
WHEEL=$M7_TYPED_WHEEL
WHEEL_SHA=$M7_TYPED_WHEEL_SHA256
HERMES_LAUNCHER=${M7_TYPED_HERMES_CLI:-}
HERMES_SOURCE=${M7_TYPED_HERMES_SOURCE:-/home/ocadmin/.hermes/hermes-agent}
RUN_ROOT=${M7_TYPED_REHEARSAL_ROOT:-"$SOURCE/.rehearsal/m7-typed-$(date -u +%Y%m%dT%H%M%SZ)"}
MAX_WALL_SECONDS=${M7_MAX_WALL_SECONDS:-2700}
MAX_TASK_SECONDS=${M7_MAX_TASK_SECONDS:-1200}
MAX_WORKER_RUNS=${M7_MAX_WORKER_RUNS:-7}
CLEANUP_SECONDS=${M7_CLEANUP_SECONDS:-120}
REQUEST_ID=${M7_TYPED_REQUEST_ID:-m7-typed-request-1}
BOARD=${M7_TYPED_BOARD:-}
CANDIDATE_SOURCE="$RUN_ROOT/frozen-source"
[[ $SOURCE_COMMIT =~ ^[0-9a-f]{40}$ && $WHEEL_SHA =~ ^[0-9a-f]{64}$ ]] || { echo 'malformed candidate pins' >&2; exit 64; }
[[ -d $SOURCE/.git && -f $WHEEL ]] || { echo 'missing candidate prerequisite' >&2; exit 66; }
[[ $(git -C "$SOURCE" rev-parse HEAD) == "$SOURCE_COMMIT" && -z $(git -C "$SOURCE" status --porcelain) ]] || { echo 'candidate commit must be current and clean' >&2; exit 65; }
[[ $(sha256sum "$WHEEL" | awk '{print $1}') == "$WHEEL_SHA" ]] || { echo 'candidate wheel SHA-256 mismatch' >&2; exit 65; }
[[ ! -e $RUN_ROOT ]] || { echo "refusing existing run root: $RUN_ROOT" >&2; exit 73; }
mkdir -p "$CANDIDATE_SOURCE"; chmod 700 "$RUN_ROOT" "$CANDIDATE_SOURCE"
git -C "$SOURCE" archive --format=tar "$SOURCE_COMMIT" | tar -x -C "$CANDIDATE_SOURCE"
[[ -f $CANDIDATE_SOURCE/scripts/m7_typed_planner_runtime.py ]] || { echo 'frozen helper absent from pinned candidate source' >&2; exit 66; }
FROZEN_HELPER_SHA=$(sha256sum "$CANDIDATE_SOURCE/scripts/m7_typed_planner_runtime.py" | awk '{print $1}')
printf '{"candidate_source_commit":"%s","candidate_wheel_sha256":"%s","frozen_helper":"scripts/m7_typed_planner_runtime.py","frozen_helper_sha256":"%s"}\n' "$SOURCE_COMMIT" "$WHEEL_SHA" "$FROZEN_HELPER_SHA" >"$RUN_ROOT/frozen-provenance.json"

# The pinned M7 artifact predates the revised authority-gated handoff.  A
# future pinned artifact may be accompanied by an explicit contract receipt,
# but this runner never changes these pins or infers a finalizer command from
# source names.  The receipt must name the exact public finalizer supplied by
# the implementation owner and the exact pending/finalized status values.
TWO_PHASE_CONTRACT=${M7_TWO_PHASE_CONTRACT:-}

# Receipt provenance is validated by the single capability report below; do not
# retain a second report implementation that can silently override it.

# The revised candidate's public contract is inspected structurally from its
# frozen executable modules. This is deliberately stronger than a token search:
# registrations, handlers, parser routing, the worker-owned native call, and
# finalizer body must all exist in the packaged candidate.
two_phase_capability_report() {
  "$PM_BOOTSTRAP_PYTHON" - "$CANDIDATE_SOURCE" "$WHEEL_SHA" "$SOURCE_COMMIT" "$TWO_PHASE_CONTRACT" <<'PY'
import ast, json, pathlib, sys
root = pathlib.Path(sys.argv[1]); wheel_sha, commit, receipt_path = sys.argv[2:]
paths = {p.name: p for p in root.joinpath('local_first_orchestrator').glob('*.py')}
def tree(name): return ast.parse(paths[name].read_text(encoding='utf8'), filename=str(paths[name]))
def calls_name(t, name):
    return [n for n in ast.walk(t) if isinstance(n, ast.Call) and ((isinstance(n.func, ast.Name) and n.func.id == name) or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]
tools, cli, coord = tree('plugin_tools.py'), tree('cli.py'), tree('coordinator.py')
schemas = set(); registered = set(); parser_commands = set()
for node in ast.walk(tools):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_schema' and node.args and isinstance(node.args[0], ast.Constant): schemas.add(node.args[0].value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'register_tool':
        for kw in node.keywords:
            if kw.arg == 'name' and isinstance(kw.value, ast.Name): registered.add(kw.value.id)
for node in ast.walk(cli):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_parser' and node.args and isinstance(node.args[0], ast.Constant): parser_commands.add(node.args[0].value)
functions = {n.name: n for n in ast.walk(coord) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
hooks = tree('plugin_hooks.py')
request = functions.get('request_local_review_from_worker'); reserve = functions.get('request_local_review'); final = functions.get('finalize_local_review')
reasons=[]
required_schemas={'local_first_request_local_review','local_first_status','local_first_submit_review'}
if not required_schemas <= schemas: reasons.append('required public tool schemas missing')
if request is None or reserve is None or not calls_name(request, 'request_local_review') or not calls_name(reserve, '_validated_provisional_candidate_observation'): reasons.append('provisional worker request path is not executable')
# Native request-review is intentionally worker-owned; verify the public hook
# directs that exact registered worker transition without pretending the plugin
# itself invokes a native worker tool.
if 'kanban_request_review' not in ast.unparse(hooks): reasons.append('worker-native kanban_request_review contract is absent')
if final is None: reasons.append('public finalize_local_review method is absent')
else:
    body=ast.unparse(final)
    for value in ('review_requested','local_first_review','_worker_session','finalize_request_review'):
        if value not in body: reasons.append('finalizer lacks exact terminal evidence field: '+value)
if 'finalize-local-review' not in parser_commands: reasons.append('public CLI finalizer parser is absent')
# This satisfies the current handler registration topology: the loop registers
# SCHEMAS[name], so schema membership is the registration source of truth.
if not required_schemas <= schemas: reasons.append('registered public contract incomplete')
receipt = None
if receipt_path:
    try:
        receipt = json.loads(pathlib.Path(receipt_path).read_text(encoding='utf8'))
        if not isinstance(receipt, dict): raise ValueError('receipt root is not an object')
        if receipt.get('candidate_wheel_sha256') != wheel_sha or receipt.get('candidate_source_commit') != commit:
            raise ValueError('receipt does not bind this pinned wheel/source commit')
    except Exception as error:
        reasons.append(f'invalid two-phase contract receipt: {error}')
print(json.dumps({'version':2,'candidate_wheel_sha256':wheel_sha,'candidate_source_commit':commit,'receipt_path':receipt_path or None,'receipt':receipt,'public_tools':sorted(schemas),'public_cli_commands':sorted(parser_commands),'worker_owned_native_request_review':('kanban_request_review' in ast.unparse(hooks)),'ready':not reasons,'reasons':reasons},sort_keys=True))
PY
}

# Import public schemas only from the candidate wheel installed in an isolated
# venv. The frozen archive remains provenance data and is never put on
# PYTHONPATH for this dynamic-schema extraction.
planner_tool_schemas_from_installed_wheel() {
  local interpreter=$1
  "$interpreter" -I - "$CANDIDATE_SOURCE" <<'PY'
import importlib, json, pathlib, sys
frozen = pathlib.Path(sys.argv[1]).resolve()
tools = importlib.import_module('local_first_orchestrator.plugin_tools')
module_path = pathlib.Path(tools.__file__).resolve()
if frozen == module_path or frozen in module_path.parents:
    raise SystemExit('plugin_tools was imported from frozen source, not the installed wheel')
schemas = getattr(tools, 'SCHEMAS', None)
selected = ('local_first_register_planning_request', 'local_first_submit_plan', 'local_first_status')
if not isinstance(schemas, dict):
    raise SystemExit('installed plugin_tools exposes no SCHEMAS mapping')
out = {name: schemas[name] for name in selected}
for name, schema in out.items():
    if not isinstance(schema, dict) or schema.get('name') != name:
        raise SystemExit(f'installed schema {name} is malformed')
print(json.dumps({'source': 'installed-wheel', 'module_path': str(module_path), 'schemas': out}, sort_keys=True, separators=(',', ':')))
PY
}

if [[ ${1:-} == --schema-extraction-self-test ]]; then
  SELF_TEST_VENV="$RUN_ROOT/schema-extraction-venv"
  "$PM_BOOTSTRAP_PYTHON" -m venv "$SELF_TEST_VENV"
  "$SELF_TEST_VENV/bin/pip" install --no-deps "$WHEEL" >"$RUN_ROOT/schema-extraction-wheel-install.log"
  extracted=$(planner_tool_schemas_from_installed_wheel "$SELF_TEST_VENV/bin/python")
  "$SELF_TEST_VENV/bin/python" - "$extracted" "$RUN_ROOT/schema-extraction-self-test.json" <<'PY'
import json, pathlib, sys
payload = json.loads(sys.argv[1]); decisions = payload['schemas']['local_first_submit_plan']['parameters']['properties']['decisions']
if not isinstance(decisions, dict) or decisions.get('type') != 'object':
    raise SystemExit('installed typed decisions schema did not resolve to an object')
pathlib.Path(sys.argv[2]).write_text(json.dumps({'source': payload['source'], 'module_path': payload['module_path'], 'decisions_schema': decisions}, sort_keys=True), encoding='utf-8')
PY
  printf 'schema-extraction-self-test: isolated installed wheel resolved dynamic typed decisions without provider, board, or core effects\n'
  exit 0
fi

# This isolated fixture executes the production capture/cleanup/dispatcher
# helper definitions against only a disposable fake CLI. It never opens a real
# board, profile, plugin, or provider.
if [[ ${1:-} == --fixture-test ]]; then
  exec "$(dirname "$0")/m7-real-worker-provider-rehearsal-fixture.sh" "$0"
fi

if [[ ${1:-} == --capability-report ]]; then
  two_phase_capability_report
  exit 0
fi

# Retained historical fixture receipt only; the production-helper fixture above
# is the supported regression entrypoint.
if [[ ${1:-} == --legacy-fixture-test ]]; then
  FIXTURE_ROOT=$(mktemp -d "${TMPDIR:-/home/ocadmin/.hermes/cache/scratch}/m7-harness-fixture.XXXXXX")
  FIXTURE_PYTHON="$CANDIDATE_SOURCE/../verification-venv/bin/python"
  [[ -x $FIXTURE_PYTHON ]] || { echo 'fixture verification interpreter unavailable' >&2; exit 66; }
  # Exercise the cleanup capture contract against a fake CLI: a stderr warning
  # must not corrupt JSON, while empty/non-JSON/failed/timeout calls preserve
  # their exact provenance and never become successful observations.
  "$FIXTURE_PYTHON" - "$FIXTURE_ROOT/fake-cli" <<'PY'
import pathlib, sys
pathlib.Path(sys.argv[1]).write_text('''#!/usr/bin/env bash
case "$1" in
  json) printf '{"status":"blocked"}'; printf 'warning-on-stderr\\n' >&2 ;;
  empty) : ;;
  nonjson) printf 'not-json' ;;
  failure) printf '{"error":"cancel failed"}'; exit 3 ;;
  partial) printf '{"outcome":"partial"}'; exit 4 ;;
  timeout) sleep 2 ;;
esac
''', encoding='utf-8')
pathlib.Path(sys.argv[1]).chmod(0o700)
PY
  fixture_capture() {
    local label=$1; shift; local out="$FIXTURE_ROOT/$label.stdout" err="$FIXTURE_ROOT/$label.stderr" rc
    set +e; timeout --foreground 1 "$FIXTURE_ROOT/fake-cli" "$@" >"$out" 2>"$err"; rc=$?; set -e
    "$FIXTURE_PYTHON" - "$FIXTURE_ROOT/$label.capture.json" "$label" "$rc" "$out" "$err" <<'PY'
import hashlib, json, pathlib, sys
p,label,rc,out,err=sys.argv[1:]
bout,b_err=pathlib.Path(out).read_bytes(),pathlib.Path(err).read_bytes()
try: json.loads(bout.decode()); valid=True; parse_error=None
except (UnicodeDecodeError,json.JSONDecodeError) as exc: valid=False; parse_error=f'{type(exc).__name__}: {exc}'
outcome='success' if int(rc)==0 and valid else ('partial_cancellation' if int(rc)==4 and valid else 'error')
json.dump({'label':label,'exit_code':int(rc),'stdout_json_valid':valid,'outcome':outcome,'stdout_sha256':hashlib.sha256(bout).hexdigest(),'stderr_sha256':hashlib.sha256(b_err).hexdigest(),'stdout_path':out,'stderr_path':err,'stdout_parse_error':parse_error},open(p,'w'),sort_keys=True)
PY
  }
  fixture_capture readback-one json
  fixture_capture readback-empty empty || true
  fixture_capture readback-nonjson nonjson || true
  fixture_capture cancel-failure failure || true
  fixture_capture cancel-partial partial || true
  fixture_capture readback-timeout timeout || true
  "$FIXTURE_PYTHON" - "$FIXTURE_ROOT" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
records={p.stem.replace('.capture',''):json.loads(p.read_text()) for p in root.glob('*.capture.json')}
assert records['readback-one']['outcome']=='success' and records['readback-one']['stderr_sha256']
for name in ('readback-empty','readback-nonjson','cancel-failure','readback-timeout'):
    assert records[name]['outcome']=='error', (name,records[name])
assert records['cancel-partial']['outcome']=='partial_cancellation' and records['cancel-partial']['exit_code']==4
assert records['readback-empty']['stdout_parse_error'].startswith('JSONDecodeError')
assert records['readback-nonjson']['stdout_parse_error'].startswith('JSONDecodeError')
assert records['cancel-failure']['exit_code']==3 and records['cancel-failure']['stdout_json_valid']
assert records['readback-timeout']['exit_code']==124
PY
  # Preserve the fixture receipt for inspection; never erase a run root.
  trap 'kill "${FIXTURE_PID:-}" 2>/dev/null || true' EXIT
  mkdir -p "$FIXTURE_ROOT"/{tmp,repo,cache}
  export TMPDIR="$FIXTURE_ROOT/tmp" PYTHONDONTWRITEBYTECODE=1
  printf 'def test_clean():\n    assert 2 + 2 == 4\n' >"$FIXTURE_ROOT/repo/test_clean.py"
  git -C "$FIXTURE_ROOT/repo" init -q
  git -C "$FIXTURE_ROOT/repo" config user.name 'm7-fixture'
  git -C "$FIXTURE_ROOT/repo" config user.email 'm7-fixture@invalid'
  git -C "$FIXTURE_ROOT/repo" add test_clean.py
  git -C "$FIXTURE_ROOT/repo" commit -qm fixture
  [[ $(git -C "$FIXTURE_ROOT/repo" config --local user.name) == m7-fixture ]]
  [[ $(git -C "$FIXTURE_ROOT/repo" config --local user.email) == m7-fixture@invalid ]]
  "$FIXTURE_PYTHON" -m pytest -p no:cacheprovider -q "$FIXTURE_ROOT/repo/test_clean.py"
  [[ ! -e $FIXTURE_ROOT/repo/.pytest_cache && ! -e $FIXTURE_ROOT/repo/__pycache__ ]]
  sleep 60 & FIXTURE_PID=$!
  FIXTURE_START=$(awk '{print $22}' "/proc/$FIXTURE_PID/stat")
  "$FIXTURE_PYTHON" - "$FIXTURE_ROOT/board.json" "$FIXTURE_PID" "$FIXTURE_START" <<'PY'
import json, sys
path, pid, start = sys.argv[1:]
json.dump({"tasks":[{"id":"anchor","status":"blocked","runs":[]},
                         {"id":"child","status":"running","runs":[{"status":"running","pid":int(pid),"pid_start":start}]}]},
          open(path, "w"), sort_keys=True)
PY
  # Simulated native cancel result is deliberately partial.  The fallback
  # reads exact scoped rows, validates PID + Linux start identity, terminates
  # only that worker, then records supported scoped blocking before readback.
  "$FIXTURE_PYTHON" - "$FIXTURE_ROOT/board.json" "$FIXTURE_PID" "$FIXTURE_START" <<'PY'
import json, os, signal, sys, time
path, pid, start = sys.argv[1:]
data = json.load(open(path))
rows = [row for row in data["tasks"] if row["id"] in {"anchor", "child"}]
assert len(rows) == 2 and any(row["status"] == "running" for row in rows)
assert open(f"/proc/{pid}/stat").read().split()[21] == start
os.kill(int(pid), signal.SIGTERM)
def exited_or_zombie():
    try: return open(f"/proc/{pid}/stat").read().split()[2] == "Z"
    except FileNotFoundError: return True
for _ in range(40):
    if exited_or_zombie(): break
    time.sleep(.025)
else: raise SystemExit("fixture worker did not exit")
for row in rows:
    if row["status"] in {"ready", "running"}:
        row["status"] = "blocked"
        for run in row["runs"]:
            if run["status"] in {"ready", "running"}: run["status"] = "cancelled"
json.dump(data, open(path, "w"), sort_keys=True)
final = json.load(open(path))
assert not any(row["status"] in {"ready", "running"} for row in final["tasks"])
assert all(run["status"] not in {"ready", "running"} for row in final["tasks"] for run in row["runs"])
PY
  wait "$FIXTURE_PID" 2>/dev/null || true
  [[ ! -e /proc/$FIXTURE_PID ]]
  FIXTURE_PID=
  printf 'fixture-test: cache-free preparation and partial-cancel scoped containment passed\n'
  exit 0
fi

if [[ ${1:-} == --self-test ]]; then
  [[ $MAX_WALL_SECONDS =~ ^[1-9][0-9]*$ && $MAX_TASK_SECONDS =~ ^[1-9][0-9]*$ && $MAX_WORKER_RUNS =~ ^[1-9][0-9]*$ && $MAX_WORKER_RUNS -eq 7 && $MAX_WALL_SECONDS -eq 2700 && $MAX_TASK_SECONDS -eq 1200 ]] || exit 64
  [[ -f $WHEEL && -f $CANDIDATE_SOURCE/local_first_orchestrator/plugin_tools.py && -f $CANDIDATE_SOURCE/local_first_orchestrator/cli.py ]] || exit 66
  [[ $(sha256sum "$WHEEL" | awk '{print $1}') == "$WHEEL_SHA" ]] || exit 65
  bash -n "$0"
  # Harness-only regression contracts: inspect the production text and frozen
  # public status serializer without loading a plugin, opening a board, or
  # contacting a provider. These prevent a future prompt/wait edit from
  # accepting dirty work or treating provisional/unknown authority as review.
  "$PM_BOOTSTRAP_PYTHON" - "$0" "$CANDIDATE_SOURCE" <<'PY'
import ast, pathlib, sys
script = pathlib.Path(sys.argv[1]).read_text(encoding='utf8')
source = pathlib.Path(sys.argv[2])
required = (
    'run the configured pytest check, then verify git diff --check and git status --porcelain',
    'Stage only -- src/normalize.py tests/test_normalize.py, create one Git commit',
    'reviewer=worker-write-local and metadata.local_first_review exactly equal to that pending review_marker',
    'If the public request is not outcome=proposed or its pending marker is absent, stop and report blocked; do not call native kanban_request_review.',
    'begin_post_provisional_window', 'wait_for_native_handoff',
    'unbound native terminal review: exact marker is absent; refusing impossible finalization',
    'wait_for_finalized_handoff', 'RUN_CLI_LIMIT_SECONDS=$limit lf_cli finalize-local-review',
)
missing = [text for text in required if text not in script]
assert not missing, missing
obsolete_selector = 's.get("hand' + 'offs"'
assert obsolete_selector not in script, 'obsolete non-public status selector remains'
assert 'PYTHONDONTWRITEBYTECODE=1' in script and '-p no:cacheprovider' in script
coordinator = (source / 'local_first_orchestrator/coordinator.py').read_text(encoding='utf8')
tree = ast.parse(coordinator)
status = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'status')
rendered = ast.unparse(status)
assert 'review_handoffs' in rendered and 'awaiting_native_request_review_finalization' in rendered
assert 'state' in rendered and 'pending' in rendered and 'finalized' in rendered
print('harness-contract-proof: committed-clean worker prompts, exact public pending status, unbound-native rejection, and shared 120-second post-provisional window match')
PY
  # Resolve the public schemas from an isolated installation of the pinned
  # wheel. Dynamic decision schemas are executable API values, not AST literals.
  SELF_TEST_VENV="$RUN_ROOT/self-test-venv"
  "$PM_BOOTSTRAP_PYTHON" -m venv "$SELF_TEST_VENV"
  "$SELF_TEST_VENV/bin/pip" install --no-deps "$WHEEL" >"$RUN_ROOT/self-test-wheel-install.log"
  installed_schemas=$(planner_tool_schemas_from_installed_wheel "$SELF_TEST_VENV/bin/python")
  "$SELF_TEST_VENV/bin/python" - "$installed_schemas" "$CANDIDATE_SOURCE" <<'PY'
import json, pathlib, sys
payload = json.loads(sys.argv[1]); schemas = payload['schemas']
expected = {
    "local_first_status": ({"board_id", "anchor_task_id"}, {"board_id", "anchor_task_id"}),
    "local_first_submit_plan": ({"board_id", "anchor_task_id", "decisions", "request_id"}, {"board_id", "anchor_task_id", "decisions"}),
    "local_first_register_planning_request": ({"board_id", "anchor_task_id", "request_id"}, {"board_id", "anchor_task_id"}),
}
for name, (expected_properties, expected_required) in expected.items():
    parameters = schemas[name]['parameters']
    actual_properties = set(parameters['properties'])
    actual_required = set(parameters['required'])
    if actual_properties != expected_properties or actual_required != expected_required:
        raise SystemExit(f'installed tool schema mismatch for {name}: {actual_properties!r}/{actual_required!r}')
if schemas['local_first_submit_plan']['parameters']['properties']['decisions'].get('type') != 'object':
    raise SystemExit('installed typed decisions schema is not an object')
source = (pathlib.Path(sys.argv[2]) / 'local_first_orchestrator/cli.py').read_text(encoding='utf8')
for command in ("prepare-paid-correction", "release-paid-correction", "integrate-piece", "prepare-paid-review", "release-paid-review", "accept-tranche"):
    if f'add_parser("{command}")' not in source:
        raise SystemExit(f'frozen public CLI command missing: {command}')
print('contract-proof: installed plugin schemas and frozen correction/re-review CLI commands match')
PY
  capability=$(two_phase_capability_report)
  "$PM_BOOTSTRAP_PYTHON" - "$capability" "$WHEEL_SHA" <<'PY'
import json, sys
report = json.loads(sys.argv[1]); expected_wheel_sha = sys.argv[2]
assert report["ready"] is True, report
assert report["candidate_wheel_sha256"] == expected_wheel_sha, report
assert report["worker_owned_native_request_review"] is True, report
print("capability-proof: frozen revised candidate exposes the required public two-phase contract")
PY
  printf 'self-test: syntax, frozen wheel hash, bounded limits, public-contract proof, and revised capability gate passed\n'
  exit 0
fi
# Verify the frozen executable contract before any disposable state is created.
capability=$(two_phase_capability_report)
printf '%s\n' "$capability" >&2
"$PM_BOOTSTRAP_PYTHON" - "$capability" <<'PY'
import json, sys
report=json.loads(sys.argv[1])
if not report.get('ready'): raise SystemExit('revised two-phase capability gate failed: '+repr(report.get('reasons')))
PY
# The contract-only probe below never dispatches or mutates; keep the child
# guard for every actual rehearsal path.
if [[ ${1:-} != --native-fingerprint-contract-test ]]; then
  [[ -z ${HERMES_DELEGATED_CHILD_CONTEXT:-} ]] || { echo 'refusing delegated-child execution' >&2; exit 64; }
fi
[[ -n $BOARD && -x $HERMES_LAUNCHER && -f ${M7_TYPED_PROVIDER_CONFIG_SOURCE:-} && -f ${M7_TYPED_OPERATOR_REQUEST:-} ]] || { echo 'missing board, Hermes launcher, provider config, or operator request prerequisite' >&2; exit 66; }
APPROVED_PROVIDER_CONFIG=/home/ocadmin/.hermes/profiles/worker-code-local/config.yaml
[[ $(readlink -f "$M7_TYPED_PROVIDER_CONFIG_SOURCE") == "$APPROVED_PROVIDER_CONFIG" ]] || { echo 'provider config source is not the approved worker-code-local config' >&2; exit 64; }
PROVIDER_CONFIG_SHA=$(sha256sum "$M7_TYPED_PROVIDER_CONFIG_SOURCE" | awk '{print $1}')
[[ $MAX_WALL_SECONDS =~ ^[1-9][0-9]*$ && $MAX_TASK_SECONDS =~ ^[1-9][0-9]*$ && $MAX_WORKER_RUNS =~ ^[1-9][0-9]*$ ]] || { echo 'invalid bounded limits' >&2; exit 64; }
[[ $CLEANUP_SECONDS =~ ^[1-9][0-9]*$ && $CLEANUP_SECONDS -le 120 ]] || { echo 'cleanup budget must be 1..120 seconds' >&2; exit 64; }
VERIFY_PYTHON=${M7_TYPED_VERIFY_PYTHON:-$(command -v python3 || true)}
[[ -n $PM_BOOTSTRAP_PYTHON && -x $PM_BOOTSTRAP_PYTHON && -x $HERMES_LAUNCHER && -d $HERMES_SOURCE/pm && -d $CANDIDATE_SOURCE && -f $WHEEL && -x $VERIFY_PYTHON ]] || { echo 'missing PM bootstrap/frozen source/wheel/verification prerequisite' >&2; exit 66; }
[[ $(git -C "$SOURCE" rev-parse "$SOURCE_COMMIT^{commit}") == "$SOURCE_COMMIT" ]] || { echo 'verified source commit unavailable' >&2; exit 65; }
[[ $(sha256sum "$WHEEL" | awk '{print $1}') == "$WHEEL_SHA" ]] || { echo 'candidate wheel SHA-256 mismatch' >&2; exit 65; }

# Do not let a parent profile/home redirect the selector to an unrelated
# install. This only clears inherited process routing; it removes no files.
for k in $(env | cut -d= -f1 | grep -E '^HERMES_(KANBAN|HOME|PROFILE|SESSION|TENANT|DELEGATED_CHILD|M0_CLI|TERMINAL_CWD)' || true); do unset "$k"; done
unset LOCAL_FIRST_ORCHESTRATOR_CONFIG
PM_SELECTOR_HOME="$HOME"

resolve_pm_python() {
  # Pin resolution to the source install's normal dependency home even after
  # the runner switches HERMES_HOME to its disposable profile clone.
  env -u HERMES_HOME HOME="$PM_SELECTOR_HOME" "$PM_BOOTSTRAP_PYTHON" -I - "$HERMES_SOURCE" <<'PY'
import sys
from pathlib import Path
source = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(source))
from pm.environments import project_python
print(project_python(source))
PY
}

resolve_store_python() {
  # The published source launcher starts this store Python, not the selected
  # venv interpreter. This facts-only lookup never provisions a generation.
  env -u HERMES_HOME HOME="$PM_SELECTOR_HOME" "$PM_BOOTSTRAP_PYTHON" -I - "$HERMES_SOURCE" <<'PY'
import sys
from pathlib import Path
source = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(source))
from hermes_cli._launchers import resolve_store_python
value = resolve_store_python(source)
if value is None:
    raise SystemExit('published source launcher has no store Python')
print(value)
PY
}

# ``project_python`` is the PM pre-import selector for the committed
# application environment.  It reads the per-install facts record without
# syncing, repairing, or choosing a new generation; do not derive a generation
# from a path or reuse the legacy venv interpreter.
MANAGED_PYTHON=$(resolve_pm_python)
[[ -x $MANAGED_PYTHON && -f $MANAGED_PYTHON ]] || { echo 'PM selected Python is unavailable' >&2; exit 66; }
STORE_PYTHON=$(resolve_store_python)
[[ -x $STORE_PYTHON && -f $STORE_PYTHON ]] || { echo 'published store Python is unavailable' >&2; exit 66; }

# Read the native dispatcher contract from the exact bootstrap-selected runtime.
# This is intentionally read-only: it calls current_instantiation_epoch(), never
# an epoch-advance/reset helper, and does not start a worker or touch a board.
native_process_fingerprint() {
  local pid=$1
  [[ $pid =~ ^[1-9][0-9]*$ ]] || return 1
  env -u HERMES_HOME HOME="$PM_SELECTOR_HOME" "$STORE_PYTHON" -I - "$HERMES_SOURCE" "$PM_SELECTOR_HOME/.hermes" "$pid" <<'PY'
import os, sys
source, hermes_home, pid = sys.argv[1:]
os.environ.pop("PYTHONHOME", None)
os.environ.pop("PYTHONPATH", None)
os.environ.pop("VIRTUAL_ENV", None)
sys.path.insert(0, source)
# Match the source launcher's bootstrap/import lane before reading the native
# helpers.  The disposable HERMES_HOME is deliberately not used for selection.
os.environ["HERMES_HOME"] = hermes_home
import hermes_bootstrap  # noqa: F401
from gateway.drain_control import current_instantiation_epoch
from gateway.status import get_process_start_time
start = get_process_start_time(int(pid))
if start is None:
    raise SystemExit(1)
epoch = current_instantiation_epoch()
if not isinstance(epoch, str) or not epoch:
    raise SystemExit(1)
print(f"{epoch}|{start}")
PY
}

if [[ ${1:-} == --native-fingerprint-contract-test ]]; then
  # The selected native runtime must produce the exact composite contract for
  # this live shell; this test has no board/profile/provider/child effects.
  first=$(native_process_fingerprint "$$")
  second=$(native_process_fingerprint "$$")
  [[ $first == "$second" && $first =~ ^.+\|[0-9]+$ ]] || {
    echo 'native fingerprint contract is unavailable or unstable' >&2; exit 66;
  }
  printf 'native-fingerprint-contract-test: selected runtime produced stable full epoch|start fingerprint\n'
  exit 0
fi

# RUN_ROOT and frozen-source were created only after pin/clean checks above.
mkdir -p "$RUN_ROOT"/{logs,state,repo,plugin,evidence,outer-home,hermes-home,kanban-home,profiles,tmp,venv}
chmod 700 "$RUN_ROOT" "$RUN_ROOT"/{state,repo,plugin,evidence,outer-home,hermes-home,kanban-home,profiles,tmp,venv}
export HOME="$RUN_ROOT/outer-home" HERMES_HOME="$RUN_ROOT/hermes-home" HERMES_KANBAN_HOME="$RUN_ROOT/kanban-home" HERMES_M0_CLI=
export TMPDIR="$RUN_ROOT/tmp" PYTHONDONTWRITEBYTECODE=1
export GIT_TERMINAL_PROMPT=0 HERMES_DISABLE_LAZY_INSTALLS=1
# The candidate artifact is installed only in this disposable venv. Its helper
# source remains the frozen git archive recorded above, never the live checkout.
python3 -m venv "$RUN_ROOT/venv"
"$RUN_ROOT/venv/bin/pip" install --no-deps "$WHEEL" >"$RUN_ROOT/logs/pinned-wheel-install.log"
VERIFY_PYTHON="$RUN_ROOT/venv/bin/python"
export PYTHON="$MANAGED_PYTHON"
# The selected venv interpreter deliberately has no app/test helper contract:
# bare imports have no YAML/pytest. Start the same store Python and import
# hermes_bootstrap before hermes_cli. Bootstrap must see the source install's
# normal dependency home; only after activation may the wrapper switch to the
# disposable profile home.
# The configuration validates ``hermes_executable`` as a regular executable.
CLI="$RUN_ROOT/state/hermes-managed"
cat >"$CLI" <<EOF
#!/usr/bin/env bash
exec "$STORE_PYTHON" -I -c 'import os,runpy,sys; os.environ.pop("PYTHONHOME",None); os.environ.pop("PYTHONPATH",None); os.environ.pop("VIRTUAL_ENV",None); sys.path.insert(0,"$HERMES_SOURCE"); os.environ["HERMES_HOME"]="$PM_SELECTOR_HOME/.hermes"; import hermes_bootstrap; os.environ["HERMES_HOME"]="$HERMES_HOME"; runpy.run_module("hermes_cli.main",run_name="__main__",alter_sys=True)' "\$@"
EOF
chmod 700 "$CLI"
START_EPOCH=$(date +%s)

# Capture the exact selected runtime and source receipt before creating the
# disposable repository or making any native-board call. The supported source
# launcher imports from this workspace after its bootstrap activates deps.
RUNTIME_WORKSPACE="$HERMES_SOURCE"
[[ -f $RUNTIME_WORKSPACE/hermes_cli/main.py ]] || { echo 'managed runtime workspace is incomplete' >&2; exit 66; }
RUNTIME_SOURCE_COMMIT=$(git -C "$HERMES_SOURCE" rev-parse HEAD)
RUNTIME_MAIN_SHA=$(sha256sum "$RUNTIME_WORKSPACE/hermes_cli/main.py" | awk '{print $1}')
"$MANAGED_PYTHON" - "$RUN_ROOT/evidence/runtime-selection.json" "$MANAGED_PYTHON" "$RUNTIME_WORKSPACE" "$RUNTIME_SOURCE_COMMIT" "$RUNTIME_MAIN_SHA" <<'PY'
import json, pathlib, sys
out, python, workspace, commit, main_sha = sys.argv[1:]
pathlib.Path(out).write_text(json.dumps({"pm_selector":"pm.environments.project_python","python":python,"workspace":workspace,"source_commit":commit,"workspace_main_sha256":main_sha},sort_keys=True),encoding="utf8")
PY

note() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$RUN_ROOT/logs/runner.log" >&2; }
die() { note "STOPPED: $*"; exit 1; }
CLEANUP_ARMED=0; REHEARSAL_SUCCESS=0; ANCHOR=; SCOPED_TASK_IDS=(); WORKER_RUN_COUNT=0
# Cleanup receives its own finite window.  It never reuses an exhausted main
# wall budget, and every cleanup CLI call records independent stdout/stderr.
CLEANUP_DEADLINE=0
CLEANUP_INTERRUPTED=
CLI_CALL_COUNT=0
declare -A OWNED_PID_START=()
# A task can have been spawned before the dispatcher response/readback supplies
# a trustworthy native run/session/PID binding.  Keep that uncertainty durable:
# a terminal card is not evidence that an unbound process ceased to exist.
declare -A UNRESOLVED_SPAWN_OWNERSHIP=()
scope_task() { local task=$1 known; [[ -n $task ]] || return 0; for known in "${SCOPED_TASK_IDS[@]}"; do [[ $known == "$task" ]] && return 0; done; SCOPED_TASK_IDS+=("$task"); }
spawn_attempt_diagnostic() {
  local task_id=$1 label=$2 phase=$3 detail=$4
  "$PYTHON" - "$RUN_ROOT/logs/spawn-attempt-${task_id}.json" "$task_id" "$label" "$phase" "$detail" <<'PY'
import json, pathlib, sys, time
path, task_id, label, phase, detail = map(str, sys.argv[1:])
record = {"task_id": task_id, "label": label, "phase": phase,
          "detail": detail, "ownership": "unresolved" if phase != "resolved" else "resolved",
          "recorded_at_epoch": time.time()}
pathlib.Path(path).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
PY
}
mark_spawn_ownership_pending() {
  local task_id=$1 label=$2 detail=$3
  UNRESOLVED_SPAWN_OWNERSHIP["$task_id"]=$detail
  spawn_attempt_diagnostic "$task_id" "$label" pending "$detail"
}
resolve_spawn_ownership() {
  local task_id=$1 label=$2 detail=$3
  [[ -n ${UNRESOLVED_SPAWN_OWNERSHIP[$task_id]+present} ]] || return 1
  unset 'UNRESOLVED_SPAWN_OWNERSHIP[$task_id]'
  spawn_attempt_diagnostic "$task_id" "$label" resolved "$detail"
}
record_unresolved_cleanup_partial() {
  local task_id detail
  for task_id in "${!UNRESOLVED_SPAWN_OWNERSHIP[@]}"; do
    detail=${UNRESOLVED_SPAWN_OWNERSHIP[$task_id]}
    spawn_attempt_diagnostic "$task_id" cleanup partial "unresolved ownership remains after cleanup: $detail"
  done
}
pid_live() { [[ $1 =~ ^[1-9][0-9]*$ && -r /proc/$1/stat ]] && awk '$3 != "Z" {print "live"}' "/proc/$1/stat" 2>/dev/null; }
terminate_owned_pid() {
  local pid=$1 expected=${OWNED_PID_START[$1]:-} observed
  observed=$(native_process_fingerprint "$pid" || true)
  # Its exact start identity was previously registered. An absent/zombie PID
  # independently proves that this exact native process is no longer live;
  # do not convert that benign race into a false partial cleanup.
  [[ -n $expected && -z $observed ]] && return 0
  [[ -n $expected && $observed == "$expected" ]] || { note "refusing PID $pid: missing or changed start identity"; return 1; }
  kill -TERM "$pid" 2>/dev/null || return 1
  for _ in {1..20}; do [[ -z $(pid_live "$pid" || true) ]] && return 0; sleep .1; done
  observed=$(native_process_fingerprint "$pid" || true)
  [[ $observed == "$expected" ]] || return 0
  kill -KILL "$pid" 2>/dev/null || return 1
  for _ in {1..10}; do [[ -z $(pid_live "$pid" || true) ]] && return 0; sleep .1; done
  return 1
}
cleanup_remaining() {
  local seconds=$(( CLEANUP_DEADLINE - $(date +%s) ))
  (( seconds > 0 )) || return 1
  printf '%s' "$seconds"
}
run_cleanup_cli() {
  # Do not call run_cli here: its main-wall budget may already be exhausted.
  # This remains the same disposable CLI/home/config routing as normal calls.
  local seconds
  seconds=$(cleanup_remaining) || return 124
  timeout --foreground "$seconds" "$CLI" "$@"
}
record_cleanup_capture() {
  local label=$1 rc=$2 stdout=$3 stderr=$4
  "$PYTHON" - "$RUN_ROOT/logs/$label.capture.json" "$label" "$rc" "$stdout" "$stderr" "${CLEANUP_INTERRUPTED:-}" <<'PY'
import hashlib, json, pathlib, sys
out, label, rc, stdout_path, stderr_path, interrupted = sys.argv[1:]
stdout = pathlib.Path(stdout_path).read_bytes()
stderr = pathlib.Path(stderr_path).read_bytes()
record = {"label": label, "exit_code": int(rc), "stdout_path": stdout_path,
          "stderr_path": stderr_path, "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
          "stderr_sha256": hashlib.sha256(stderr).hexdigest(), "interrupted": interrupted or None}
try:
    json.loads(stdout.decode("utf-8"))
    record["stdout_json_valid"] = True
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    record["stdout_json_valid"] = False
    record["stdout_parse_error"] = f"{type(exc).__name__}: {exc}"
if record["exit_code"] == 0 and record["stdout_json_valid"]:
    record["outcome"] = "success"
elif record["exit_code"] == 4 and record["stdout_json_valid"]:
    record["outcome"] = "partial_cancellation"
else:
    record["outcome"] = "error"
pathlib.Path(out).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
PY
}
capture_cleanup_cli() {
  local label=$1; shift
  local stdout="$RUN_ROOT/logs/$label.stdout" stderr="$RUN_ROOT/logs/$label.stderr" rc
  set +e
  run_cleanup_cli "$@" >"$stdout" 2>"$stderr"
  rc=$?
  set -e
  record_cleanup_capture "$label" "$rc" "$stdout" "$stderr"
  return "$rc"
}
capture_cleanup_json() {
  # A JSON payload is usable only when the command succeeded and stdout is a
  # complete JSON document.  stderr is never merged into stdout: the prior
  # `2>&1` routing caused a valid show response to look like empty/malformed
  # JSON when the CLI emitted a warning on stderr.
  local label=$1; shift
  local stdout="$RUN_ROOT/logs/$label.json" stderr="$RUN_ROOT/logs/$label.stderr" rc
  set +e
  run_cleanup_cli "$@" >"$stdout" 2>"$stderr"
  rc=$?
  set -e
  record_cleanup_capture "$label" "$rc" "$stdout" "$stderr"
  [[ $rc -eq 0 ]] || return "$rc"
  "$PYTHON" -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$stdout" >/dev/null 2>/dev/null
}
capture_scoped_readback() {
  local task rc=0
  for task in "${SCOPED_TASK_IDS[@]}"; do
    capture_cleanup_json "cleanup-show-$task" kanban --board "$BOARD" show "$task" --json || rc=$?
  done
  return "$rc"
}
active_scoped_tasks() {
  "$PYTHON" - "$RUN_ROOT/logs" "${SCOPED_TASK_IDS[@]}" <<'PY'
import json, pathlib, sys
logs, task_ids = pathlib.Path(sys.argv[1]), sys.argv[2:]
active = {"ready", "running"}
def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from walk(child)
    elif isinstance(value, list):
        for child in value: yield from walk(child)
for task_id in task_ids:
    data = json.loads((logs / f"cleanup-show-{task_id}.json").read_text())
    rows = [row for row in walk(data) if row.get("id") == task_id]
    if len(rows) != 1: raise SystemExit(f"exact scoped task readback missing/ambiguous: {task_id}")
    row = rows[0]
    runs = row.get("runs", [])
    if row.get("status") in active or any(isinstance(run, dict) and run.get("status") in active for run in runs): print(task_id)
PY
}
verify_parked_scope() {
  "$PYTHON" - "$RUN_ROOT/logs" "${SCOPED_TASK_IDS[@]}" <<'PY'
import json, pathlib, sys
logs, task_ids = pathlib.Path(sys.argv[1]), sys.argv[2:]
if not task_ids: raise SystemExit("no scoped tasks to verify")
active = {"ready", "running"}; seen = set()
def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from walk(child)
    elif isinstance(value, list):
        for child in value: yield from walk(child)
for task_id in task_ids:
    path = logs / f"cleanup-show-{task_id}.json"
    data = json.loads(path.read_text())
    rows = [row for row in walk(data) if row.get("id") == task_id]
    if len(rows) != 1: raise SystemExit(f"exact scoped task readback missing/ambiguous: {task_id}")
    row = rows[0]; seen.add(task_id)
    if row.get("status") in active: raise SystemExit(f"scoped task still active: {task_id}")
    runs = row.get("runs", [])
    if not isinstance(runs, list) or any(not isinstance(run, dict) or not isinstance(run.get("status"), str) or run["status"] in active for run in runs):
        raise SystemExit(f"scoped task has nonterminal run: {task_id}")
if seen != set(task_ids): raise SystemExit("scoped task readback coverage mismatch")
PY
  local pid
  for pid in "${!OWNED_PID_START[@]}"; do [[ -z $(pid_live "$pid" || true) ]] || return 1; done
  # Do not turn a blocked/terminal card into a process-termination claim when
  # dispatch never supplied an exact native ownership receipt.
  (( ${#UNRESOLVED_SPAWN_OWNERSHIP[@]} == 0 )) || return 1
}
verify_whole_board_parked() {
  capture_cleanup_json cleanup-whole-board kanban --board "$BOARD" list --json || return $?
  "$PYTHON" - "$RUN_ROOT/logs/cleanup-whole-board.json" <<'PY'
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text())
active = {"ready", "running"}
def walk(value):
    if isinstance(value, dict):
        if isinstance(value.get("status"), str): yield value
        for child in value.values(): yield from walk(child)
    elif isinstance(value, list):
        for child in value: yield from walk(child)
rows = list(walk(data))
if any(row["status"] in active for row in rows): raise SystemExit("whole-board readback still has ready/running task or run")
PY
}
fallback_park_scope() {
  local task pid failed=0 active_tasks
  note "partial cancellation: inspecting exact scoped tasks and runs before fallback"
  capture_scoped_readback || failed=1
  for pid in "${!OWNED_PID_START[@]}"; do terminate_owned_pid "$pid" || failed=1; done
  capture_scoped_readback || failed=1
  active_tasks=$(active_scoped_tasks) || failed=1
  for task in $active_tasks; do
    capture_cleanup_cli "cleanup-block-$task" kanban --board "$BOARD" block "$task" 'M7 rehearsal cleanup: cancellation did not fully contain this disposable scope' || failed=1
  done
  capture_scoped_readback || failed=1
  verify_parked_scope || failed=1
  verify_whole_board_parked || failed=1
  if (( ${#UNRESOLVED_SPAWN_OWNERSHIP[@]} )); then
    record_unresolved_cleanup_partial
    note "cleanup remains partial: unresolved post-spawn ownership prevents verified containment"
    failed=1
  fi
  (( failed == 0 )) || { note "cleanup fallback verification failed"; return 1; }
  note "cleanup fallback verified: scoped tasks blocked, runs terminal, owned PIDs absent"
}
cleanup() {
  local rc=${1:-$?} cancel_rc
  trap - EXIT INT TERM
  if (( CLEANUP_ARMED )) && (( ! REHEARSAL_SUCCESS )); then
    set +e
    CLEANUP_DEADLINE=$(( $(date +%s) + CLEANUP_SECONDS ))
    note "attempting supported cancellation for unfinished disposable scope${CLEANUP_INTERRUPTED:+ after $CLEANUP_INTERRUPTED}"
    # Exit code 4 is a documented partial-cancellation contract only when its
    # stdout is complete JSON; the capture retains it without treating it as
    # successful containment.
    capture_cleanup_json cancel-on-failure -p worker-architect-sol local-first-orchestrator --config "$CONFIG" --board "$BOARD" --anchor-task-id "$ANCHOR" cancel
    cancel_rc=$?
    capture_scoped_readback || true
    # A success exit is not proof of containment. Always read exact persisted
    # tasks/runs; any active lane or failed/partial cancel enters the supported
    # scoped-block fallback.
    if (( cancel_rc != 0 )) || ! verify_parked_scope || ! verify_whole_board_parked; then fallback_park_scope || rc=1; fi
  fi
  exit "$rc"
}
handle_interrupt() { CLEANUP_INTERRUPTED=$1; cleanup "$2"; }
trap 'cleanup $?' EXIT
trap 'handle_interrupt INT 130' INT
trap 'handle_interrupt TERM 143' TERM
remaining() { local n=$(( MAX_WALL_SECONDS - ($(date +%s) - START_EPOCH) )); (( n > 0 )) || die "global wall cap exhausted"; printf '%s' "$n"; }
run_cli() {
  # Every native CLI operation, including a dispatcher pass, is bounded by the
  # remaining global budget rather than only the polling loop.  Keep raw stdout
  # and stderr separate for every invocation even when a caller later chooses
  # a presentation redirect; the capture is the authoritative diagnostic
  # receipt and no malformed/empty output is silently treated as JSON success.
  local seconds=$(( MAX_WALL_SECONDS - ($(date +%s) - START_EPOCH) )) label stdout stderr rc
  [[ $(resolve_pm_python) == "$MANAGED_PYTHON" ]] || die 'PM runtime selection drifted after identity capture'
  [[ $(git -C "$HERMES_SOURCE" rev-parse HEAD) == "$RUNTIME_SOURCE_COMMIT" ]] || die 'source commit receipt drifted after identity capture'
  [[ $(sha256sum "$RUNTIME_WORKSPACE/hermes_cli/main.py" | awk '{print $1}') == "$RUNTIME_MAIN_SHA" ]] || die 'managed runtime workspace drifted after identity capture'
  (( seconds > 0 )) || seconds=5  # leave cleanup a narrow cancellation window
  if [[ -n ${RUN_CLI_LIMIT_SECONDS:-} ]]; then
    [[ $RUN_CLI_LIMIT_SECONDS =~ ^[1-9][0-9]*$ ]] || die 'invalid per-command timeout limit'
    (( seconds <= RUN_CLI_LIMIT_SECONDS )) || seconds=$RUN_CLI_LIMIT_SECONDS
  fi
  label=$(printf 'cli-%04d' "$((++CLI_CALL_COUNT))")
  stdout="$RUN_ROOT/logs/$label.stdout"; stderr="$RUN_ROOT/logs/$label.stderr"
  if timeout --foreground "$seconds" "$CLI" "$@" >"$stdout" 2>"$stderr"; then rc=0; else rc=$?; fi
  record_cleanup_capture "$label" "$rc" "$stdout" "$stderr"
  cat "$stdout"
  cat "$stderr" >&2
  return "$rc"
}
board_cli() { run_cli kanban --board "$BOARD" "$@"; }
lf_cli() { run_cli -p worker-architect-sol local-first-orchestrator --config "$CONFIG" --board "$BOARD" --anchor-task-id "$ANCHOR" "$@"; }
json_field() { "$PYTHON" -c 'import json,sys
v=json.load(sys.stdin)
for k in sys.argv[1:]: v=v[int(k)] if isinstance(v,list) else v[k]
print(v)' "$@"; }

# No archive-and-wipe: construct just the deliberately tiny target repository.
REPO="$RUN_ROOT/repo"
mkdir -p "$REPO/src" "$REPO/tests"
printf '%s\n' 'def normalize(value: str) -> str:' '    """Collapse whitespace and lowercase text."""' '    raise NotImplementedError' > "$REPO/src/normalize.py"
printf '%s\n' 'from src.normalize import normalize' '' 'def test_normalize_whitespace_and_case():' '    assert normalize("  Alpha   BETA ") == "alpha beta"' > "$REPO/tests/test_normalize.py"
printf '%s\n' '[tool.pytest.ini_options]' 'pythonpath = ["."]' > "$REPO/pyproject.toml"
git -C "$REPO" init -q
git -C "$REPO" config user.name 'm7-rehearsal'
git -C "$REPO" config user.email 'm7-rehearsal@invalid'
git -C "$REPO" add src tests pyproject.toml
git -C "$REPO" commit -qm 'chore: rehearsal baseline'
BASE_SHA=$(git -C "$REPO" rev-parse HEAD)

# Extract only the packaged plugin payload, checking every member path.
PLUGIN="$RUN_ROOT/plugin/local-first-orchestrator"
"$PYTHON" - "$WHEEL" "$PLUGIN" <<'PY'
import pathlib, shutil, sys, zipfile
wheel, out=map(pathlib.Path,sys.argv[1:]); prefix='local_first_orchestrator-0.1.0.data/data/local-first-orchestrator/'
with zipfile.ZipFile(wheel) as z:
    names=[n for n in z.namelist() if n.startswith(prefix)]
    if not names or prefix+'plugin.yaml' not in names: raise SystemExit('wheel lacks expected plugin payload')
    out.mkdir()
    for name in names:
        rel=pathlib.PurePosixPath(name[len(prefix):])
        if not rel.parts or rel.is_absolute() or '..' in rel.parts: raise SystemExit('unsafe wheel member')
        dest=out.joinpath(*rel.parts); dest.parent.mkdir(parents=True,exist_ok=True)
        if not name.endswith('/'):
            with z.open(name) as src, dest.open('wb') as dst: shutil.copyfileobj(src,dst)
PY

profiles=(worker-architect-sol worker-code-local worker-write-local worker-review-terra)
for p in "${profiles[@]}"; do
  src="/home/ocadmin/.hermes/profiles/$p"; dst="$HERMES_HOME/profiles/$p"
  [[ -d $src ]] || die "missing required profile $p"
  mkdir -p "$dst/plugins"; chmod 700 "$dst" "$dst/plugins"
  # Preserve configured provider/model/auth but never print copied credentials.
  for f in config.yaml .env auth.json; do [[ -f $src/$f ]] && install -m 600 "$src/$f" "$dst/$f"; done
  # The provider route is allowlisted: worker-code-local's copied configuration
  # must be exactly the operator-approved source, never an arbitrary execution
  # config supplied to this runner.
  if [[ $p == worker-code-local ]]; then install -m 600 "$M7_TYPED_PROVIDER_CONFIG_SOURCE" "$dst/config.yaml"; fi
  [[ -f $dst/config.yaml ]] || die "profile $p has no config.yaml"
  if [[ $p == worker-code-local ]]; then
    [[ $(sha256sum "$dst/config.yaml" | awk '{print $1}') == "$PROVIDER_CONFIG_SHA" ]] || die 'approved provider route drifted while cloning'
  fi
  # A profile terminal cwd would defeat dispatcher-owned dir:<workspace> routing.
  run_cli --version >/dev/null 2>&1
  "$PYTHON" - "$dst/config.yaml" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
lines = p.read_text(encoding='utf8').splitlines(keepends=True)
out, in_terminal = [], False
for line in lines:
    top = re.match(r'^[^\s#][^:]*:', line)
    if top:
        in_terminal = line.split(':', 1)[0] == 'terminal'
    # The supported profile schema makes terminal.cwd a scalar setting.  Do
    # not parse YAML through the bare PM-selected interpreter: it intentionally
    # lacks optional YAML helpers.  Preserve every other copied byte exactly.
    if in_terminal and re.match(r'^  cwd\s*:', line):
        continue
    out.append(line)
p.write_text(''.join(out), encoding='utf8')
PY
  cp -a "$PLUGIN" "$dst/plugins/local-first-orchestrator"
  printf '\nLOCAL_FIRST_ORCHESTRATOR_CONFIG=%q\n' "$RUN_ROOT/state/local-first.json" >> "$dst/.env"
  chmod 600 "$dst/.env"
done
[[ -f /home/ocadmin/.hermes/auth.json ]] && install -m 600 /home/ocadmin/.hermes/auth.json "$HERMES_HOME/auth.json"

# Native CLI/plugin readiness only. It uses the copied disposable profiles.
for p in "${profiles[@]}"; do
  run_cli -p "$p" plugins doctor "$HERMES_HOME/profiles/$p/plugins/local-first-orchestrator" --ci >"$RUN_ROOT/logs/$p.plugin-doctor.log" 2>&1 || die "plugin doctor failed for $p"
  run_cli -p "$p" plugins enable local-first-orchestrator --no-allow-tool-override >"$RUN_ROOT/logs/$p.plugin-enable.log" 2>&1 || die "plugin enable failed for $p"
  run_cli -p "$p" config check >"$RUN_ROOT/logs/$p.config-check.log" 2>&1 || die "provider/config readiness failed for $p"
  run_cli -p "$p" plugins list --plain --no-bundled >"$RUN_ROOT/logs/$p.plugins-list.log" 2>&1
  grep -q 'local-first-orchestrator' "$RUN_ROOT/logs/$p.plugins-list.log" || die "candidate plugin absent after enable for $p"
done

write_configured_check_runner() { # absolute, argument-closed, cache-free worker check
  local target=$1
  [[ $target == /* && $VERIFY_PYTHON == /* && $REPO == /* ]] || die 'configured check runner requires absolute trusted paths'
  printf '#!/usr/bin/env bash\nset -Eeuo pipefail\n(( $# == 0 )) || { printf "%%s\\n" "configured check accepts no worker-supplied pytest arguments" >&2; exit 64; }\nexport PYTHONDONTWRITEBYTECODE=1\ncd -- %q\nexec %q -m pytest -p no:cacheprovider -q\n' "$REPO" "$VERIFY_PYTHON" >"$target"
  chmod 700 "$target"
}

# Disposable native board and explicit operator bootstrap request.
run_cli kanban boards create "$BOARD" >"$RUN_ROOT/logs/kanban-board-create.log" 2>&1
run_cli kanban --board "$BOARD" init >"$RUN_ROOT/logs/kanban-init.log" 2>&1
ANCHOR=$(board_cli create 'M7 rehearsal anchor' --body 'One tiny normalize function; no unrelated edits.' --assignee worker-architect-sol --workspace "dir:$REPO" --idempotency-key m7-anchor --max-runtime 20m --json | json_field id)
[[ -n $ANCHOR ]] || die 'anchor ID missing'
scope_task "$ANCHOR"
# Prove the intended split explicitly: the PM-selected venv is suitable for
# ABI-bound app deps, while the sealed candidate verification venv owns pytest.
# Neither probe installs or changes a host environment.
"$MANAGED_PYTHON" -c 'import openai,pydantic_core' || die 'managed Python ABI dependency check failed'
if "$MANAGED_PYTHON" -c 'import yaml,pytest' 2>/dev/null; then die 'managed Python unexpectedly carries helper dependencies'; fi
"$VERIFY_PYTHON" -c 'import pytest,jsonschema; assert "verification-venv" in __import__("sys").executable' || die 'candidate verification interpreter is incomplete'
CHECK="$RUN_ROOT/state/run-pytest"
write_configured_check_runner "$CHECK"
CONFIG="$RUN_ROOT/state/local-first.json"
"$PYTHON" - "$CONFIG" "$RUN_ROOT/state" "$CLI" "$HERMES_HOME" "$HERMES_KANBAN_HOME" "$REPO" "$BOARD" "$ANCHOR" "$CHECK" <<'PY'
import json,pathlib,sys
c,state,cli,home,kh,repo,board,anchor,check=sys.argv[1:]
value={"version":1,"state_root":state,"hermes_executable":cli,"hermes_home":home,"kanban_home":kh,
"trusted_roots":{"repository":repo,"workspace":repo},"roles":{"implementation_profile":"worker-code-local","local_review_profile":"worker-write-local","planning_profile":"worker-architect-sol","paid_review_profile":"worker-review-terra"},
"budgets":{"implementation_attempts":2,"review_corrections":1,"infrastructure_retries":1,"workflow_repairs":5,"paid_capacity":3},"poll_interval_seconds":5,"scope":{"board_id":board,"anchor_task_id":anchor},"check_commands":[{"check_id":"pytest","argv":[check]}]}
pathlib.Path(c).write_text(json.dumps(value,sort_keys=True),encoding='utf8')
PY
chmod 600 "$CONFIG"; export LOCAL_FIRST_ORCHESTRATOR_CONFIG="$CONFIG"

# Public operator lifecycle helpers; every mutating command is immediately read back.
status() { local f="$RUN_ROOT/logs/status-$(date +%s%N).json"; lf_cli status >"$f"; cat "$f"; }
readonly_sql() { # exact persisted evidence lookup; URI mode=ro, never writes.
  "$PYTHON" - "$RUN_ROOT/state/evidence.sqlite3" "$BOARD" "$ANCHOR" "$1" <<'PY'
import json,sqlite3,sys,urllib.parse
path,board,anchor,kind=sys.argv[1:]
c=sqlite3.connect('file:'+urllib.parse.quote(path)+'?mode=ro',uri=True)
if kind=='plan':
 r=c.execute('select plan_id from plan_proposals where board_id=? and anchor_task_id=? order by plan_id',(board,anchor)).fetchall(); print(json.dumps([x[0] for x in r]))
elif kind=='review':
 r=c.execute('select evidence_json from review_evidence where board_id=? and anchor_task_id=? order by review_id',(board,anchor)).fetchall(); print(json.dumps([json.loads(x[0]) for x in r]))
elif kind=='members':
 r=c.execute('select task_id,role,generation from managed_members where board_id=? and anchor_task_id=? order by task_id',(board,anchor)).fetchall(); print(json.dumps(r))
elif kind=='ticket':
 r=c.execute('select evidence_json from plan_proposals where board_id=? and anchor_task_id=?',(board,anchor)).fetchall()
 if len(r)!=1: raise SystemExit('expected exactly one persisted plan proposal')
 e=json.loads(r[0][0]); tickets=e['proposal']['plan']['tranches'][0]['tickets']
 if len(tickets)!=1: raise SystemExit('rehearsal requires exactly one first-tranche ticket')
 print(tickets[0]['ticket_id'])
else: raise SystemExit('unknown readonly selector')
PY
}
readback() { board_cli show "$1" --json >"$RUN_ROOT/logs/show-$1-$(date +%s%N).json"; }
wait_for() { # label, shell predicate evaluated with STATUS_JSON and SQL_JSON exported; optional per-wait seconds
  # Bash expands every assignment in one `local` declaration before assigning
  # any of them. Keep values that feed later arithmetic on separate lines.
  local label=$1 predicate=$2
  local limit=${3:-$MAX_TASK_SECONDS}
  local end=$(( $(date +%s) + limit ))
  while (( $(date +%s) < end )); do
    (( $(date +%s) - START_EPOCH < MAX_WALL_SECONDS )) || die "global cap while waiting for $label"
    if ! STATUS_JSON=$(status); then die "status observation failed while waiting for $label"; fi
    if ! SQL_JSON=$(readonly_sql plan); then die "read-only evidence observation failed while waiting for $label"; fi
    export STATUS_JSON SQL_JSON
    if [[ $(bash -c "$predicate") == True ]]; then note "observed $label"; return 0; fi
    sleep 5
  done
  die "task cap waiting for $label"
}
wait_for_provisional_request() { # exact pending proposal, with fail-fast held/terminal detection
  local operation_key=$1 task_id=$2 reviewer=$3
  local end=$(( $(date +%s) + MAX_TASK_SECONDS ))
  while (( $(date +%s) < end )); do
    (( $(date +%s) - START_EPOCH < MAX_WALL_SECONDS )) || die "global cap while waiting for provisional local review $operation_key"
    if ! STATUS_JSON=$(status); then die "status observation failed while waiting for provisional local review $operation_key"; fi
    export STATUS_JSON operation_key task_id reviewer
    local observation_rc
    set +e
    "$PYTHON" - <<'PY'
import json, os, sys
status = json.loads(os.environ['STATUS_JSON'])['status']
key, task_id, reviewer = (os.environ[name] for name in ('operation_key', 'task_id', 'reviewer'))
handoffs = [item for item in status.get('review_handoffs', []) if item.get('operation_key') == key]
if len(handoffs) == 1 and handoffs[0].get('state') == 'pending':
    marker = handoffs[0].get('review_marker')
    if isinstance(marker, dict) and marker.get('operation_key') == key and marker.get('reviewer_profile') == reviewer:
        # A native worker may already have completed its own transition.  The
        # durable exact pending proposal is authoritative here; do not mistake
        # that completed state for an implementation failure.
        raise SystemExit(0)
    print('pending provisional handoff marker/reviewer is malformed', file=sys.stderr)
    raise SystemExit(2)
snapshot = status.get('native_tasks', {}).get(task_id, {})
task = snapshot.get('native_task', {}) if isinstance(snapshot, dict) else {}
runs = snapshot.get('runs', []) if isinstance(snapshot, dict) else []
if not isinstance(task, dict) or not isinstance(runs, list):
    print('native status is malformed while awaiting provisional handoff', file=sys.stderr)
    raise SystemExit(2)
terminal = {'blocked', 'cancelled', 'canceled', 'failed', 'stopped', 'done', 'completed'}
task_state = task.get('status')
terminal_runs = [run for run in runs if isinstance(run, dict) and run.get('status') in terminal]
if task_state in {'blocked', 'cancelled', 'canceled', 'failed', 'stopped'}:
    print('implementation worker reached terminal blocked state without a pending provisional handoff; held STOP', file=sys.stderr)
    raise SystemExit(2)
if task_state in {'done', 'completed'} or terminal_runs:
    print('terminal native worker state without an exact pending provisional handoff; refusing unbound review/transition', file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(1)
PY
    observation_rc=$?
    set -e
    if (( observation_rc == 0 )); then note "observed exact pending provisional local review for $operation_key"; return 0; fi
    if (( observation_rc != 1 )); then
      die "provisional local-review authority failure for $operation_key"
      return 1
    fi
    sleep 5
  done
  die "task cap waiting for provisional local review $operation_key"
}

wait_for_native_handoff() { # exact pending operation, task, configured reviewer; fixed post-provisional budget
  local operation_key=$1 task_id=$2 reviewer=$3
  local end=${POST_PROVISIONAL_DEADLINE:?post-provisional window was not started}
  while (( $(date +%s) < end )); do
    (( $(date +%s) - START_EPOCH < MAX_WALL_SECONDS )) || die "global cap while waiting for native handoff $operation_key"
    if ! STATUS_JSON=$(status); then die "status observation failed while waiting for native handoff $operation_key"; fi
    export STATUS_JSON operation_key task_id reviewer
    local observation_rc
    set +e
    "$PYTHON" - <<'PY'
import json, os, sys
status = json.loads(os.environ['STATUS_JSON'])['status']
key, task_id, reviewer = (os.environ[name] for name in ('operation_key', 'task_id', 'reviewer'))
def authority_failure(detail):
    print(detail, file=sys.stderr)
    raise SystemExit(2)
handoffs = [item for item in status.get('review_handoffs', [])
            if item.get('operation_key') == key]
if len(handoffs) != 1 or handoffs[0].get('state') != 'pending':
    authority_failure('exact pending provisional handoff is absent or ambiguous')
marker = handoffs[0].get('review_marker')
if not isinstance(marker, dict) or marker.get('operation_key') != key or marker.get('reviewer_profile') != reviewer:
    authority_failure('pending handoff marker/reviewer is malformed')
snapshot = status.get('native_tasks', {}).get(task_id, {})
task = snapshot.get('native_task', {})
runs = snapshot.get('runs', [])
events = snapshot.get('events', [])
if not isinstance(task, dict) or not isinstance(runs, list) or not isinstance(events, list):
    authority_failure('native status is malformed')
matching = [run for run in runs if isinstance(run, dict)
            and isinstance(run.get('metadata'), dict)
            and run['metadata'].get('local_first_review') == marker]
terminal_requested = [run for run in runs if isinstance(run, dict)
                      and run.get('status') in {'completed', 'done'}
                      and run.get('outcome') == 'review_requested']
if terminal_requested and not matching:
    authority_failure('unbound native terminal review: exact marker is absent; refusing impossible finalization')
if len(matching) == 1:
    run = matching[0]
    run_id = run.get('id')
    exact_events = [event for event in events if isinstance(event, dict)
                    and event.get('kind') == 'review_requested'
                    and str(event.get('run_id')) == str(run_id)
                    and isinstance(event.get('payload'), dict)
                    and event['payload'].get('reviewer') == reviewer]
    if (run.get('status') in {'completed', 'done'} and run.get('outcome') == 'review_requested'
            and isinstance(run.get('metadata'), dict)
            and isinstance(run['metadata'].get('worker_session_id'), str)
            and run['metadata']['worker_session_id']
            and len(exact_events) == 1 and task.get('status') in {'review', 'done'}
            and task.get('assignee') == reviewer):
        raise SystemExit(0)
if task.get('status') in {'review', 'done'} and terminal_requested:
    authority_failure('unbound native terminal review: terminal transition lacks exact configured reviewer/session receipt')
raise SystemExit(1)
PY
    observation_rc=$?
    set -e
    if (( observation_rc == 0 )); then
      note "observed exact worker-owned native handoff for $operation_key"
      return 0
    fi
    # The Python predicate uses 1 for ordinary pending and a distinct failure
    # message on authority loss. Never wait out an unfinalizable native review.
    if (( observation_rc != 1 )); then die "native handoff authority failure for $operation_key"; fi
    sleep 5
  done
  die "post-provisional 120-second cap waiting for native handoff $operation_key"
}
begin_post_provisional_window() {
  POST_PROVISIONAL_DEADLINE=$(( $(date +%s) + 120 ))
}
post_provisional_remaining() {
  local seconds=$(( POST_PROVISIONAL_DEADLINE - $(date +%s) ))
  (( seconds > 0 )) || die 'post-provisional 120-second handoff/finalization budget exhausted'
  printf '%s' "$seconds"
}
finalize_local_review_bounded() { # operation key, result path; never replays the native request
  local operation_key=$1 result_path=$2 limit
  limit=$(post_provisional_remaining)
  RUN_CLI_LIMIT_SECONDS=$limit lf_cli finalize-local-review --operation-key "$operation_key" >"$result_path" || die "public local-review finalization failed for $operation_key"
  "$PYTHON" - "$result_path" "$operation_key" <<'PY'
import json, pathlib, sys
value=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert value.get('outcome') == 'finalized' and value.get('operation_key') == sys.argv[2], value
PY
}
wait_for_finalized_handoff() { # exact operation uses the remainder of the one post-provisional window
  local operation_key=$1
  while (( $(date +%s) < POST_PROVISIONAL_DEADLINE )); do
    if ! STATUS_JSON=$(status); then die "status observation failed while waiting for finalized handoff $operation_key"; fi
    export STATUS_JSON operation_key
    if [[ $("$PYTHON" - <<'PY'
import json, os
s=json.loads(os.environ['STATUS_JSON'])['status']
h=[x for x in s.get('review_handoffs',[]) if x.get('operation_key') == os.environ['operation_key']]
print(len(h)==1 and h[0].get('state')=='finalized')
PY
) == True ]]; then return 0; fi
    sleep 5
  done
  die "post-provisional 120-second cap waiting for finalized handoff $operation_key"
}
dispatch_one() {
  local label=$1 task_id=$2 f readback dispatch_rc pid start registered=0
  f="$RUN_ROOT/logs/dispatch-$label.json"
  readback="$RUN_ROOT/logs/dispatch-show-$task_id.json"
  (( ++WORKER_RUN_COUNT <= MAX_WORKER_RUNS )) || die 'worker-run cap exceeded'
  [[ -n $task_id ]] || die 'dispatch ownership requires an exact scoped task ID'
  scope_task "$task_id"
  # Registration must precede every response/readback parse. A successful
  # dispatch may have already created a worker even when its response is lost,
  # malformed, or lacks a bindable native PID/start receipt. Process ownership
  # is intentionally separate from review-session authority.
  mark_spawn_ownership_pending "$task_id" "$label" 'dispatch submitted; exact native ownership receipt pending'
  set +e
  board_cli dispatch --max 1 --failure-limit 1 --json >"$f"
  dispatch_rc=$?
  set -e
  if (( dispatch_rc != 0 )); then
    spawn_attempt_diagnostic "$task_id" "$label" unknown "dispatch exit $dispatch_rc; no trusted ownership receipt"
    die "dispatch response unavailable for $task_id; ownership remains unresolved"
    return 1
  fi
  # Dispatcher JSON is a scheduling receipt, not process ownership authority:
  # it may recursively embed unrelated process IDs. Read the exact task back
  # from the native board and bind one active run to one exact spawned receipt.
  # Process containment deliberately does not require worker_session_id: review
  # authority remains a separate, worker-owned task/run/session gate.
  board_cli show "$task_id" --json >"$readback"
  local receipt
  if ! receipt=$("$PYTHON" - "$readback" "$task_id" <<'PY'
import json, sys
path, task_id = sys.argv[1:]
data = json.load(open(path, encoding='utf8'))
task = data.get('task')
runs = data.get('runs')
events = data.get('events')
if not isinstance(task, dict) or task.get('id') != task_id or task.get('status') != 'running':
    raise SystemExit('exact scoped task is not an active native task')
if not isinstance(runs, list) or not isinstance(events, list):
    raise SystemExit('native show receipt lacks runs/events')
active = [r for r in runs if isinstance(r, dict) and r.get('status') == 'running']
if len(active) != 1 or not isinstance(active[0].get('id'), int):
    raise SystemExit('exact scoped task lacks one active native claim run')
run = active[0]
# Process ownership is restricted to the exact task's sole active native run,
# its source-of-record worker_pid, and one event explicitly bound to that run.
# Never recursively search dispatcher/task metadata for PID-looking fields.
# Session receipts are intentionally not a prerequisite here: they establish
# review authority, not whether this harness may stop an exact spawned process.
receipts = []
for event in events:
    if not isinstance(event, dict) or event.get('run_id') != run['id']:
        continue
    if event.get('kind') not in {'spawned', 'worker_registered'}:
        continue
    payload = event.get('payload')
    if not isinstance(payload, dict):
        continue
    pid, start = payload.get('pid'), payload.get('started_at')
    if (isinstance(pid, int) and pid > 0 and isinstance(start, str) and start
            and start != 'unverified'):
        receipts.append((pid, start))
if len(receipts) != 1:
    raise SystemExit('exact active run has no unique PID/start spawned receipt')
pid, start = receipts[0]
if run.get('worker_pid') != pid:
    raise SystemExit('active run worker_pid does not match exact spawned receipt')
print(f'{pid}\t{start}')
PY
)
  then
    spawn_attempt_diagnostic "$task_id" "$label" unknown 'exact task/run/PID/start receipt unavailable after dispatch'
    die "native active run/PID/start ownership receipt unavailable for $task_id; refusing full rehearsal"
    return 1
  fi
  while IFS=$'\t' read -r pid start; do
    [[ -n $pid ]] || continue
    # Only the native composite wire format is supported here.  An absent epoch
    # is an unsupported capability, not permission to fall back to raw start.
    [[ $pid =~ ^[1-9][0-9]*$ && $start =~ ^[^\|[:space:]]+\|[0-9]+$ && $start != unverified ]] || continue
    # Require the board's spawn fingerprint and the live /proc identity to
    # agree before registering anything that cleanup could signal.
    # This must equal the complete native ``<instantiation epoch>|<start>``
    # fingerprint.  Do not accept a suffix/start-only match: that would admit
    # a post-reboot/container-recreate PID incarnation.
    [[ $(native_process_fingerprint "$pid" || true) == "$start" ]] || continue
    OWNED_PID_START["$pid"]=$start
    registered=1
  done <<< "$receipt"
  # A pending record may be cleared only after exact task/run/PID/start receipt
  # validation and a live /proc identity registered it as signalable ownership.
  # This containment receipt is not reviewer identity or review authorization.
  # No native review-handoff capability is assumed by this harness.
  (( registered == 1 )) || { spawn_attempt_diagnostic "$task_id" "$label" unknown 'receipt parsed but no live owned PID registered'; return 1; }
  resolve_spawn_ownership "$task_id" "$label" 'exact active run/PID/start receipt registered'
}

# The request is operator-owned; the runner never synthesizes authority.
REQUEST_FILE=$M7_TYPED_OPERATOR_REQUEST
[[ -f $REQUEST_FILE ]] || die 'operator request file unavailable'
lf_cli initialize-store >"$RUN_ROOT/logs/initialize-store.json"
lf_cli enroll >"$RUN_ROOT/logs/enroll.json" || die 'enrollment failed'
CLEANUP_ARMED=1
readback "$ANCHOR"
lf_cli bootstrap-planning --request-file "$REQUEST_FILE" --request-id "$REQUEST_ID" >"$RUN_ROOT/logs/bootstrap-planning.json" || die 'operator bootstrap failed'
lf_cli prepare-planner --request-id "$REQUEST_ID" >"$RUN_ROOT/logs/prepare-planner.json" || [[ $? == 3 ]] || die 'prepare planner failed'
PLANNER=$(json_field task_id <"$RUN_ROOT/logs/prepare-planner.json")
scope_task "$PLANNER"
lf_cli release-planner --request-id "$REQUEST_ID" >"$RUN_ROOT/logs/release-planner.json" || die 'planner release failed'
# Build and verify the packet with the pinned wheel, then validate it with the
# frozen helper from the exact archived source commit.
PLANNER_STATUS=$(lf_cli status); printf '%s' "$PLANNER_STATUS" >"$RUN_ROOT/logs/status-before-planner-dispatch.json"
"$VERIFY_PYTHON" - "$PLANNER_STATUS" "$RUN_ROOT/state/planner-packet.json" <<'PY'
import json, sys
from local_first_orchestrator.decomposition_planner import packet
from local_first_orchestrator.planning_coordinator import request_from_payload
status=json.loads(sys.argv[1])['status']
request=request_from_payload(status['planning_request']['request'])
open(sys.argv[2], 'w', encoding='utf-8').write(packet(request))
PY
PYTHONPATH="$CANDIDATE_SOURCE" "$VERIFY_PYTHON" - "$RUN_ROOT/state/planner-packet.json" "$PLANNER_STATUS" >"$RUN_ROOT/logs/schema-delivery-receipt.json" <<'PY'
import json, sys
from local_first_orchestrator.planning_coordinator import request_from_payload
from scripts.m7_typed_planner_runtime import schema_delivery_receipt
status=json.loads(sys.argv[2])['status']; request=request_from_payload(status['planning_request']['request'])
print(json.dumps(schema_delivery_receipt(open(sys.argv[1], encoding='utf-8').read(), request), sort_keys=True))
PY
readback "$PLANNER"
PLANNER_TOOL_SCHEMAS=$(planner_tool_schemas_from_installed_wheel "$VERIFY_PYTHON")
[[ -n $PLANNER_TOOL_SCHEMAS ]] || die 'installed planner tool schemas unavailable'
board_cli comment "$PLANNER" --author operator "You are the actual planner worker. The enrolled scope is exactly board_id=$BOARD and anchor_task_id=$ANCHOR; include those exact fields in every Local First tool call and do not discover or substitute another root. First call local_first_status with only this scope, then local_first_register_planning_request with exactly this scope plus request_id=$REQUEST_ID. The delivered pinned request-bound packet is $RUN_ROOT/state/planner-packet.json; its receipt is $RUN_ROOT/logs/schema-delivery-receipt.json. Submit exactly one typed decisions object through local_first_submit_plan; never send proposal_json, request_identity, execution_order, direct SQLite, a second submission, or fabricated task/run/session evidence." >"$RUN_ROOT/logs/planner-instructions.json"

# Workers own all registration/submission/review tool calls.  No parent invokes
# worker tools, injects provenance, or writes native task/runs directly.
WORKER_RUN_COUNT=0
dispatch_one planner "$PLANNER"
# The dispatch readback is the only source for the owned run ID. Never select
# runs[0], infer from timestamps, or treat an unknown status as permission.
PLANNER_RUN=$("$PYTHON" - "$RUN_ROOT/logs/dispatch-show-$PLANNER.json" "$PLANNER" <<'PY'
import json, sys
show=json.load(open(sys.argv[1], encoding='utf-8')); task_id=sys.argv[2]
if show.get('task',{}).get('id') != task_id: raise SystemExit('dispatch readback task mismatch')
runs=show.get('runs'); owned=[r for r in runs if isinstance(r,dict) and r.get('status')=='running' and r.get('id') is not None] if isinstance(runs,list) else []
if len(owned)!=1: raise SystemExit('dispatch readback has no unique exact owned run')
print(owned[0]['id'])
PY
)
DEADLINE=$(( $(date +%s) + MAX_TASK_SECONDS ))
while (( $(date +%s) < DEADLINE )); do
  TASK_JSON=$(board_cli show "$PLANNER" --json)
  STATUS_JSON=$(status)
  outcome=$(PYTHONPATH="$CANDIDATE_SOURCE" "$VERIFY_PYTHON" - "$TASK_JSON" "$STATUS_JSON" "$RUN_ROOT/state/planner-packet.json" "$PLANNER" "$PLANNER_RUN" worker-architect-sol "$REQUEST_ID" <<'PY'
import json, sys
from scripts.m7_typed_planner_runtime import planning_wait_outcome
show,status,packet_text,task_id,run_id,profile,request_id=sys.argv[1:]
show=json.loads(show); public=json.loads(status)['status']; identity=json.loads(open(packet_text, encoding='utf-8').read())['request_identity']
plans=public.get('plans'); exact=[]
if isinstance(plans,list):
    for plan in plans:
        if not isinstance(plan,dict): continue
        proposal=plan.get('proposal', plan)
        if isinstance(proposal,dict) and proposal.get('request_id') == request_id and proposal.get('request_identity') == identity:
            exact.append(proposal)
persisted=exact[0] if len(exact)==1 else None
task=show.get('task')
if isinstance(task,dict): task={**task, 'runs': show.get('runs')}
print(json.dumps(planning_wait_outcome(task, task_id=task_id, run_id=run_id, profile=profile, persisted_proposal=persisted, request_id=request_id, request_identity=identity), sort_keys=True))
PY
)
  printf '%s\n' "$outcome" >>"$RUN_ROOT/logs/planning-wait.jsonl"
  [[ $outcome != *stop_cleanup_readback* ]] || die 'owned planner run reached terminal/unknown state without an exact persisted request-bound proposal'
  [[ $outcome == *proposal_persisted* ]] && break
  sleep 5
done
[[ $(date +%s) -lt $DEADLINE ]] || die 'planner task limit exhausted'
PLAN_ID=$(readonly_sql plan | "$PYTHON" -c 'import json,sys; x=json.load(sys.stdin); assert len(x)==1,x; print(x[0])')
lf_cli accept-plan --plan-id "$PLAN_ID" --request-id "$REQUEST_ID" >"$RUN_ROOT/logs/accept-plan.json" || die 'plan acceptance failed'

# First active piece is the only permitted tiny implementation unit.
TICKET_ID=$(readonly_sql ticket)
lf_cli prepare-piece --plan-id "$PLAN_ID" --ticket-id "$TICKET_ID" --request-id "$REQUEST_ID" >"$RUN_ROOT/logs/prepare-piece.json" || [[ $? == 3 ]] || die 'prepare piece failed'
IMPL=$(json_field task_id <"$RUN_ROOT/logs/prepare-piece.json")
scope_task "$IMPL"
lf_cli release-piece --plan-id "$PLAN_ID" --ticket-id "$TICKET_ID" >"$RUN_ROOT/logs/release-piece.json" || die 'piece release failed'
readback "$IMPL"
LOCAL_OPERATION_KEY="m7-local-review-$IMPL"
board_cli comment "$IMPL" --author operator "You are the implementation worker. Work only in the assigned repository $REPO and only on src/normalize.py and tests/test_normalize.py. Implement the ticket, run the configured pytest check, then verify git diff --check and git status --porcelain. Stage only -- src/normalize.py tests/test_normalize.py, create one Git commit, and verify HEAD changed from $BASE_SHA and git status --porcelain is empty. Only after those checks and the clean committed HEAD may you call the registered public tool local_first_request_local_review with exactly board_id=$BOARD, anchor_task_id=$ANCHOR, operation_key=$LOCAL_OPERATION_KEY and a concise summary. Read local_first_status and retain the one exact pending review_marker returned for $LOCAL_OPERATION_KEY. From this owned implementation run only, call native kanban_request_review using the actual exposed native schema with reviewer=worker-write-local and metadata.local_first_review exactly equal to that pending review_marker. Do not omit the marker, pass a null reviewer, infer metadata fields, submit review evidence, invoke the operator finalizer, or fabricate terminal metadata. If the public request is not outcome=proposed or its pending marker is absent, stop and report blocked; do not call native kanban_request_review."
dispatch_one implementation "$IMPL"
export IMPL LOCAL_OPERATION_KEY
wait_for_provisional_request "$LOCAL_OPERATION_KEY" "$IMPL" worker-write-local
# The implementation budget ends only when the durable public proposal with its
# exact pending marker exists.  The separate 120-second window begins now.
begin_post_provisional_window
wait_for_native_handoff "$LOCAL_OPERATION_KEY" "$IMPL" worker-write-local
# The parent executes the only public finalizer exactly once after the worker's
# terminal native transition. It never replays kanban_request_review.
finalize_local_review_bounded "$LOCAL_OPERATION_KEY" "$RUN_ROOT/logs/finalize-local-review.json"
wait_for_finalized_handoff "$LOCAL_OPERATION_KEY"
board_cli comment "$IMPL" --author operator "You are the local reviewer. The parent has finalized the exact native request-review transition. Use local_first_status to read only the finalized immutable handoff, then submit through local_first_submit_review from your owned reviewer task/run/session. Do not rerun implementation observation, native handoff, or the finalizer; if status is not finalized, remain held."
dispatch_one local-review "$IMPL"
wait_for 'worker-owned local review evidence after finalized handoff' '"$PYTHON" - <<"PY"
import json,os
s=json.loads(os.environ["STATUS_JSON"])["status"]
print(bool(s.get("reviews")))
PY'
LOCAL_REVIEW=$(readonly_sql review | "$PYTHON" -c 'import json,sys; rows=json.load(sys.stdin); xs=[x["review_id"] for x in rows if x.get("reviewer_role") == "local"]; assert len(xs)==1,xs; print(xs[0])')
lf_cli integrate-piece --plan-id "$PLAN_ID" --ticket-id "$TICKET_ID" --review-id "$LOCAL_REVIEW" >"$RUN_ROOT/logs/integrate-piece.json" || die 'git integration failed'
[[ -z $(git -C "$REPO" status --porcelain) ]] || die 'integration repository is dirty'
[[ $(git -C "$REPO" rev-parse HEAD) != "$BASE_SHA" ]] || die 'implementation did not produce an integrated commit'

lf_cli prepare-paid-review --plan-id "$PLAN_ID" >"$RUN_ROOT/logs/prepare-paid.json" || [[ $? == 3 ]] || die 'prepare paid review failed'
PAID_TASK=$(readonly_sql members | "$PYTHON" -c 'import json,sys; rows=json.load(sys.stdin); xs=[x[0] for x in rows if x[1]=="paid_review"]; assert len(xs)==1,xs; print(xs[0])')
scope_task "$PAID_TASK"
lf_cli release-paid-review --plan-id "$PLAN_ID" >"$RUN_ROOT/logs/release-paid.json" || die 'paid review release failed'
readback "$PAID_TASK"
board_cli comment "$PAID_TASK" --author operator "You are the paid reviewer. Use only the registered public review tools. First call local_first_status with only {\"board_id\":\"$BOARD\",\"anchor_task_id\":\"$ANCHOR\"}. Resolve the persisted integrated candidate and exact accepted paid-review request from that public status, including the exact candidate, checks, checks_identity, and criterion_ids. Independently inspect only that clean Git candidate and run only the configured observed check. Call local_first_submit_paid_review with exactly {\"board_id\":\"$BOARD\",\"anchor_task_id\":\"$ANCHOR\",\"plan_id\":\"$PLAN_ID\",\"review\":<complete structured paid review>}; do not pass candidate, checks, task, run, session, or profile as top-level selectors. The review.native_review must describe only this owned paid task/run/session/profile; its candidate/check evidence must exactly copy the persisted request. If public status lacks complete exact authority, report this worker run blocked; do not invent a candidate payload, rerun an implementation-only observer, use operator CLI/direct SQLite, or weaken authority. If the evidence is complete and the candidate passes, submit approved; do not fabricate findings merely to force a correction path."
dispatch_one paid-review "$PAID_TASK"
wait_for 'worker-owned paid review evidence' '"$PYTHON" - <<"PY"
import json,os
s=json.loads(os.environ["STATUS_JSON"])["status"]
print(any(x.get("reviewer_role")=="paid" for x in s.get("reviews",[])))
PY'
PAID_REVIEW=$(readonly_sql review | "$PYTHON" -c 'import json,sys; rows=json.load(sys.stdin); xs=[x for x in rows if x.get("reviewer_role")=="paid"]; assert len(xs)==1,xs; print(xs[0]["review_id"])')
VERDICT=$(readonly_sql review | "$PYTHON" -c 'import json,sys; rows=json.load(sys.stdin); print(next(x["verdict"] for x in rows if x.get("review_id")=="'"$PAID_REVIEW"'"))')

# A correction is contingent on the real paid verdict. Never manufacture a
# negative result merely to exercise the branch.
if [[ $VERDICT == changes_requested ]]; then
  # The paid result is already durable. Public commands reconstruct its exact
  # review/finding identity and create/release one bounded correction card.
  lf_cli prepare-paid-correction --plan-id "$PLAN_ID" --review-id "$PAID_REVIEW" >"$RUN_ROOT/logs/prepare-correction.json" || die 'prepare correction failed'
  CORRECTION=$(json_field correction_task_id <"$RUN_ROOT/logs/prepare-correction.json")
  scope_task "$CORRECTION"
  CORRECTION_TICKET=$(json_field operation_key <"$RUN_ROOT/logs/prepare-correction.json")
  [[ -n $CORRECTION && -n $CORRECTION_TICKET ]] || die 'correction command omitted persisted task or operation identity'
  lf_cli release-paid-correction --plan-id "$PLAN_ID" --review-id "$PAID_REVIEW" >"$RUN_ROOT/logs/release-correction.json" || die 'release correction failed'
  [[ $(json_field task_id <"$RUN_ROOT/logs/release-correction.json") == "$CORRECTION" ]] || die 'correction release task identity drifted'
  readback "$CORRECTION"
  CORRECTION_LOCAL_OPERATION_KEY="m7-local-review-$CORRECTION"
  board_cli comment "$CORRECTION" --author operator "You are the correction implementation worker. Work only in assigned repository $REPO and only on src/normalize.py and tests/test_normalize.py. Address the persisted findings, run the configured pytest check, then verify git diff --check and git status --porcelain. Stage only -- src/normalize.py tests/test_normalize.py, create one Git commit, and verify git status --porcelain is empty. Only after this clean committed HEAD may you call local_first_request_local_review with exactly board_id=$BOARD, anchor_task_id=$ANCHOR, operation_key=$CORRECTION_LOCAL_OPERATION_KEY and a concise summary. Read local_first_status and retain the one exact pending review_marker for that operation. From this owned correction implementation run only, call native kanban_request_review using the actual exposed native schema with reviewer=worker-write-local and metadata.local_first_review exactly equal to that marker. If request outcome is not proposed or the marker is unavailable, stop blocked; never make an unmarked native handoff, pass a null reviewer, invent metadata, submit review evidence, or call the operator finalizer."
  dispatch_one correction "$CORRECTION"
  export CORRECTION CORRECTION_LOCAL_OPERATION_KEY
  wait_for_provisional_request "$CORRECTION_LOCAL_OPERATION_KEY" "$CORRECTION" worker-write-local
  begin_post_provisional_window
  wait_for_native_handoff "$CORRECTION_LOCAL_OPERATION_KEY" "$CORRECTION" worker-write-local
  finalize_local_review_bounded "$CORRECTION_LOCAL_OPERATION_KEY" "$RUN_ROOT/logs/finalize-correction-local-review.json"
  wait_for_finalized_handoff "$CORRECTION_LOCAL_OPERATION_KEY"
  board_cli comment "$CORRECTION" --author operator "You are the correction local reviewer. Read only the finalized immutable handoff from local_first_status, then submit through local_first_submit_review from your owned reviewer task/run/session. If it is not finalized, remain held. Do not rerun implementation observation, request a native handoff, or call the finalizer."
  dispatch_one correction-local-review "$CORRECTION"
  wait_for 'fresh correction local review evidence' '"$PYTHON" - <<"PY"
import json,os
s=json.loads(os.environ["STATUS_JSON"])["status"]
reviews=s.get("reviews",[])
print(sum(x.get("reviewer_role")=="local" for x in reviews) >= 2)
PY'
  CORRECTION_LOCAL_REVIEW=$(readonly_sql review | "$PYTHON" -c 'import json,sys; rows=json.load(sys.stdin); xs=[x["review_id"] for x in rows if x.get("reviewer_role")=="local"]; assert len(xs)==2,xs; print(xs[-1])')
  lf_cli integrate-piece --plan-id "$PLAN_ID" --ticket-id "$CORRECTION_TICKET" --review-id "$CORRECTION_LOCAL_REVIEW" >"$RUN_ROOT/logs/integrate-correction.json" || die 'correction integration failed'
  [[ -z $(git -C "$REPO" status --porcelain) ]] || die 'correction integration repository is dirty'

  # A correction changes the integrated head. Create/release a distinct paid
  # review from that persisted head; never reuse the changes-requested review.
  lf_cli prepare-paid-review --plan-id "$PLAN_ID" >"$RUN_ROOT/logs/prepare-fresh-paid.json" || die 'prepare fresh paid review failed'
  PAID_TASK_FRESH=$(json_field review_task_id <"$RUN_ROOT/logs/prepare-fresh-paid.json")
  scope_task "$PAID_TASK_FRESH"
  lf_cli release-paid-review --plan-id "$PLAN_ID" >"$RUN_ROOT/logs/release-fresh-paid.json" || die 'release fresh paid review failed'
  [[ $(json_field task_id <"$RUN_ROOT/logs/release-fresh-paid.json") == "$PAID_TASK_FRESH" ]] || die 'fresh paid release task identity drifted'
  readback "$PAID_TASK_FRESH"
  board_cli comment "$PAID_TASK_FRESH" --author operator "You are the fresh paid reviewer after a correction. First call local_first_status with only {\"board_id\":\"$BOARD\",\"anchor_task_id\":\"$ANCHOR\"}, resolve the exact current paid-review request for plan $PLAN_ID, and inspect only its persisted candidate/check evidence. Submit only through local_first_submit_paid_review with exactly {\"board_id\":\"$BOARD\",\"anchor_task_id\":\"$ANCHOR\",\"plan_id\":\"$PLAN_ID\",\"review\":<complete structured paid review>}. review.native_review must be this owned paid task/run/session/profile; do not pass candidate/check/task/run/session/profile as top-level selectors, call implementation-only tools, use operator CLI/direct SQLite, or invent authority. If authoritative status is incomplete, report blocked."
  dispatch_one fresh-paid-review "$PAID_TASK_FRESH"
  wait_for 'fresh paid review evidence' '"$PYTHON" - <<"PY"
import json,os
s=json.loads(os.environ["STATUS_JSON"])["status"]
print(sum(x.get("reviewer_role")=="paid" for x in s.get("reviews",[])) >= 2)
PY'
  PAID_REVIEW_FRESH=$(readonly_sql review | "$PYTHON" -c 'import json,sys; rows=json.load(sys.stdin); xs=[x for x in rows if x.get("reviewer_role")=="paid"]; assert len(xs)==2,xs; print(xs[-1]["review_id"])')
  FRESH_VERDICT=$(readonly_sql review | "$PYTHON" -c 'import json,sys; rows=json.load(sys.stdin); print([x["verdict"] for x in rows if x.get("reviewer_role")=="paid"][-1])')
  [[ $FRESH_VERDICT == approved ]] || die "fresh paid review did not approve corrected head: $FRESH_VERDICT"
  lf_cli accept-tranche --plan-id "$PLAN_ID" --review-id "$PAID_REVIEW_FRESH" --authorize-successor >"$RUN_ROOT/logs/accept-tranche.json" || die 'tranche acceptance after fresh paid review failed'
elif [[ $VERDICT == approved ]]; then
  lf_cli accept-tranche --plan-id "$PLAN_ID" --review-id "$PAID_REVIEW" --authorize-successor >"$RUN_ROOT/logs/accept-tranche.json" || die 'tranche acceptance failed'
else
  die "paid review returned unsupported verdict: $VERDICT"
fi

readback "$ANCHOR"; board_cli show "$IMPL" --json >"$RUN_ROOT/logs/final-implementation.json"; board_cli show "$PAID_TASK" --json >"$RUN_ROOT/logs/final-paid-review.json"
readonly_sql review >"$RUN_ROOT/evidence/reviews.json"
printf '%s\n' "run_root=$RUN_ROOT" "plan_id=$PLAN_ID" "local_review_id=$LOCAL_REVIEW" "paid_review_id=$PAID_REVIEW" "paid_verdict=$VERDICT" >"$RUN_ROOT/RESULT.txt"
REHEARSAL_SUCCESS=1
note "SUCCESS: actual provider-backed lifecycle completed; evidence $RUN_ROOT"
