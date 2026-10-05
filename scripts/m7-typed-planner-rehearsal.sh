#!/usr/bin/env bash
# Future typed-planner rehearsal entrypoint.  --prepare is deliberately inert;
# --execute-authorized is a parent-only, explicitly acknowledged future path.
set -Eeuo pipefail
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MODE=${1:-}

usage() {
  cat <<'EOF'
usage: scripts/m7-typed-planner-rehearsal.sh --prepare | --fixture-test [run-root] | --execute-authorized

--prepare validates fresh source/wheel pins and writes a disposable readiness
receipt only.  It stops before authorization, commit/push, build/install,
cutover, worker dispatch, or provider use.

--execute-authorized delegates only to the source-controlled bounded runner.
It requires M7_TYPED_EXECUTE_ACK=I_ACKNOWLEDGE_DISPOSABLE_PROVIDER_REHEARSAL
from the parent process and refuses delegated children.  It is never invoked by
this script's preparation or fixture modes.
EOF
}
require_env() { [[ -n ${!1:-} ]] || { printf 'missing required %s\n' "$1" >&2; exit 64; }; }
prepare() {
  for name in M7_TYPED_CANDIDATE_SOURCE M7_TYPED_SOURCE_COMMIT M7_TYPED_WHEEL M7_TYPED_WHEEL_SHA256 M7_TYPED_HERMES_CLI M7_TYPED_CONFIG M7_TYPED_PROVIDER_ROUTE_REPORT; do require_env "$name"; done
  local source=$M7_TYPED_CANDIDATE_SOURCE wheel=$M7_TYPED_WHEEL
  [[ -d $source/.git && -f $wheel && -x $M7_TYPED_HERMES_CLI && -f $M7_TYPED_CONFIG && -f $M7_TYPED_PROVIDER_ROUTE_REPORT ]] || { echo 'candidate/provider prerequisite unavailable' >&2; exit 66; }
  [[ $M7_TYPED_SOURCE_COMMIT =~ ^[0-9a-f]{40}$ && $M7_TYPED_WHEEL_SHA256 =~ ^[0-9a-f]{64}$ ]] || { echo 'source commit or wheel SHA-256 is malformed' >&2; exit 64; }
  [[ $(git -C "$source" rev-parse HEAD) == "$M7_TYPED_SOURCE_COMMIT" ]] || { echo 'candidate source commit mismatch' >&2; exit 65; }
  [[ -z $(git -C "$source" status --porcelain) ]] || { echo 'candidate source must be clean and source-controlled' >&2; exit 65; }
  [[ $(sha256sum "$wheel" | cut -d' ' -f1) == "$M7_TYPED_WHEEL_SHA256" ]] || { echo 'candidate wheel SHA-256 mismatch' >&2; exit 65; }
  local run_root=${M7_TYPED_REHEARSAL_ROOT:-"$ROOT/.rehearsal/m7-typed-$(date -u +%Y%m%dT%H%M%SZ)"}
  [[ ! -e $run_root ]] || { echo 'refusing existing rehearsal root' >&2; exit 73; }
  mkdir -m 700 -p -- "$run_root"
  python3 - "$run_root/preparation.json" "$source" "$M7_TYPED_SOURCE_COMMIT" "$wheel" "$M7_TYPED_WHEEL_SHA256" "$M7_TYPED_PROVIDER_ROUTE_REPORT" <<'PY'
import hashlib, json, pathlib, sys
out, source, commit, wheel, wheel_sha, report_path = map(pathlib.Path, sys.argv[1:])
report = json.loads(report_path.read_text(encoding='utf-8'))
if type(report) is not dict: raise SystemExit('provider route report must be an object')
config = pathlib.Path(__import__('os').environ['M7_TYPED_CONFIG'])
out.write_text(json.dumps({
  'version': 2, 'candidate_source': str(source), 'candidate_source_commit': str(commit),
  'candidate_wheel': str(wheel), 'candidate_wheel_sha256': str(wheel_sha),
  'preparation_config_not_execution_config': {'path': str(config), 'sha256': hashlib.sha256(config.read_bytes()).hexdigest()},
  'provider_route_report': report, 'submission_budget': 1, 'max_wall_seconds': 2700,
  'max_task_seconds': 1200, 'max_worker_runs': 7, 'cleanup_seconds': 120,
  'stops': ['before authorization', 'before commit/push', 'before build/install/cutover/live provider run'],
  'future_execution': 'requires parent acknowledgement; delivery is public request-bound packet; one typed decisions submission only',
}, sort_keys=True) + '\n', encoding='utf-8')
PY
  printf 'prepared %s\nSTOP before authorization; no provider or worker was run.\n' "$run_root/preparation.json"
}
case "$MODE" in
  --prepare) prepare ;;
  --fixture-test)
    run_root=${2:-"$ROOT/.rehearsal/fixture-$(date -u +%Y%m%dT%H%M%SZ)"}
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'from pathlib import Path; import sys; from scripts.m7_typed_planner_runtime import provider_free_fixture; provider_free_fixture(Path(sys.argv[1]))' "$run_root"
    printf 'fixture-test: production schema-delivery and terminal wait helpers exercised without a provider\n'
    ;;
  --execute-authorized)
    [[ ${M7_TYPED_EXECUTE_ACK:-} == I_ACKNOWLEDGE_DISPOSABLE_PROVIDER_REHEARSAL ]] || { echo 'explicit parent authorization acknowledgement is required' >&2; exit 64; }
    [[ -z ${HERMES_DELEGATED_CHILD_CONTEXT:-} ]] || { echo 'refusing delegated-child execution' >&2; exit 64; }
    exec "$ROOT/scripts/m7-typed-planner-authorized-runner.sh"
    ;;
  *) usage; exit 64 ;;
esac
