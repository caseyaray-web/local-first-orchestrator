#!/usr/bin/env bash
# Isolated regression fixture for m7-real-worker-provider-rehearsal.sh.
# It extracts and executes production helper definitions against a fake CLI;
# no board, profile, plugin, provider, or native dispatcher is contacted.
set -Eeuo pipefail
umask 077
TARGET=${1:?target rehearsal script required}
FIXTURE_ROOT=$(mktemp -d "${TMPDIR:-/home/ocadmin/.hermes/cache/scratch}/m7-owned-cleanup.XXXXXX")
export FIXTURE_ROOT
cleanup_fixture_pids() {
  local pid
  for pid in "${OWN_PID:-}" "${SIBLING_PID:-}"; do
    [[ -n $pid ]] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup_fixture_pids EXIT
mkdir -p "$FIXTURE_ROOT/logs"

# Dispatcher output contains a live sibling PID in arbitrary nested fields.
# Production ownership must ignore it and use only exact show task/run/events.
CLI="$FIXTURE_ROOT/fake-cli"
cat >"$CLI" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ " $* " == *" dispatch "* ]]; then
  printf '{"dispatcher":{"pid":%s,"nested":{"worker_pid":%s,"process_pid":%s}}}\n' "$SIBLING_PID" "$SIBLING_PID" "$SIBLING_PID"
  exit 0
fi
if [[ " $* " == *" show "* ]]; then
  task=${@: -2:1}
  [[ $task == child ]] || { printf '{"task":{"id":"%s","status":"blocked"},"runs":[],"events":[]}\n' "$task"; exit 0; }
  if [[ -e $FIXTURE_ROOT/blocked ]]; then
    printf '{"task":{"id":"child","status":"blocked"},"runs":[{"id":17,"status":"cancelled","worker_pid":%s}],"events":[]}\n' "$OWN_PID"
    exit 0
  fi
  # Native-shaped process receipt: metadata is explicitly null and the spawned
  # event deliberately carries no session_id. That must still establish process
  # containment, while reviewer authority remains outside this helper.
  start=$OWN_FINGERPRINT
  events='[{"kind":"spawned","run_id":17,"payload":{"pid":'$OWN_PID',"started_at":"'$start'"}}]'
  if [[ -e $FIXTURE_ROOT/missing-event ]]; then
    events='[]'
  elif [[ -e $FIXTURE_ROOT/ambiguous-event ]]; then
    events='[{"kind":"spawned","run_id":17,"payload":{"pid":'$OWN_PID',"started_at":"'$start'"}},{"kind":"worker_registered","run_id":17,"payload":{"pid":'$OWN_PID',"started_at":"'$start'"}}]'
  elif [[ -e $FIXTURE_ROOT/pid-reuse ]]; then
    events='[{"kind":"spawned","run_id":17,"payload":{"pid":'$OWN_PID',"started_at":"reused-epoch|'$OWN_START'"}}]'
  fi
  printf '{"task":{"id":"child","status":"running"},"runs":[{"id":17,"status":"running","worker_pid":%s,"metadata":null}],"events":%s}\n' "$OWN_PID" "$events"
  exit 0
fi
if [[ " $* " == *" list "* ]]; then
  printf '{"tasks":[{"id":"child","status":"blocked","runs":[{"id":17,"status":"cancelled"}]}]}\n'
  exit 0
fi
if [[ " $* " == *" block "* ]]; then
  touch "$FIXTURE_ROOT/blocked"
  printf '{"status":"blocked"}\n'
  exit 0
fi
case "${1:-}" in
  json) printf '{"status":"blocked"}'; printf 'warning-on-stderr\n' >&2 ;;
  empty) : ;;
  nonjson) printf 'not-json' ;;
  failure) printf '{"error":"cancel failed"}'; exit 3 ;;
  partial) printf '{"outcome":"partial"}'; exit 4 ;;
  timeout) sleep 2 ;;
esac
SH
chmod 700 "$CLI"

# Load the production cleanup/capture and dispatcher functions, not lookalikes.
source <(python3 - "$TARGET" <<'PY'
import pathlib, sys
s = pathlib.Path(sys.argv[1]).read_text(encoding='utf8')
a = s.index('scope_task() {')
b = s.index('# No archive-and-wipe:', a)
c = s.index('dispatch_one() {')
# Keep the extraction bound to the production function, not to an incidental
# adjacency of REQUEST_FILE.  The request preamble has explanatory comments in
# current source; older candidates placed REQUEST_FILE immediately after it.
end_markers = ('\n}\n\nREQUEST_FILE=', '\n}\n\n# The request is operator-owned')
d = min(s.index(marker, c) for marker in end_markers if marker in s[c:]) + 2
print(s[a:b])
print(s[c:d])
PY
)
trap - EXIT INT TERM
cleanup_fixture_pids() {
  local pid
  for pid in "${OWN_PID:-}" "${SIBLING_PID:-}"; do
    [[ -n $pid ]] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup_fixture_pids EXIT

PYTHON=$(command -v python3)
# The fixture replaces only the runtime reader with a deterministic read-only
# native-contract double.  The production helper is separately exercised via
# --native-fingerprint-contract-test; this fixture must not invoke a provider,
# dispatcher child, or any epoch mutation API.
pid_start() { [[ $1 =~ ^[1-9][0-9]*$ && -r /proc/$1/stat ]] && awk '$3 != "Z" {print $22}' "/proc/$1/stat" 2>/dev/null; }
pid_live() { [[ -n $(pid_start "$1" || true) ]] && printf 'live\n'; }
native_process_fingerprint() {
  local start
  start=$(pid_start "$1" || true)
  [[ -n $start && -n ${FIXTURE_EPOCH:-} ]] || return 1
  printf '%s|%s\n' "$FIXTURE_EPOCH" "$start"
}
BOARD=fixture-board
RUN_ROOT=$FIXTURE_ROOT
MAX_WALL_SECONDS=60
START_EPOCH=$(date +%s)
CLEANUP_SECONDS=2
CLEANUP_DEADLINE=$(( $(date +%s) + CLEANUP_SECONDS ))
CLI_CALL_COUNT=0
WORKER_RUN_COUNT=0
MAX_WORKER_RUNS=7
CLEANUP_INTERRUPTED=
declare -A OWNED_PID_START=()
declare -A UNRESOLVED_SPAWN_OWNERSHIP=()
SCOPED_TASK_IDS=(child)
note() { printf '%s\n' "$*" >&2; }
# Production dispatch calls die before returning an ordinary rejection. The real
# runner exits there; this fixture converts that terminal reporting hook to a
# success so it can assert the production helper's following `return 1` and
# exercise partial cleanup without starting the real runner.
die() { printf 'fixture die: %s\n' "$*" >&2; return 0; }
board_cli() { "$CLI" kanban --board "$BOARD" "$@"; }

# Production capture functions preserve separate stderr and partial exit 4.
capture_cleanup_json capture-json json
for name in empty nonjson failure partial; do
  CLEANUP_DEADLINE=$(( $(date +%s) + CLEANUP_SECONDS ))
  capture_cleanup_json "capture-$name" "$name" || rc=$?
  case $name in partial) [[ ${rc:-0} == 4 ]] ;; *) [[ ${rc:-0} != 0 ]] ;; esac
  unset rc
done
CLEANUP_DEADLINE=$(( $(date +%s) + 1 ))
capture_cleanup_json capture-timeout timeout || rc=$?
[[ ${rc:-0} == 124 ]]

sleep 60 & OWN_PID=$!
sleep 60 & SIBLING_PID=$!
OWN_START=$(awk '{print $22}' "/proc/$OWN_PID/stat")
FIXTURE_EPOCH=fixture-epoch-a
OWN_FINGERPRINT="$FIXTURE_EPOCH|$OWN_START"
export OWN_PID SIBLING_PID OWN_START OWN_FINGERPRINT FIXTURE_EPOCH

# Positive control: an exact task/run/worker_pid/spawned PID/start receipt with
# null metadata and no event session is process-owned. Nested sibling PIDs are
# never registered or killed. The /proc start identity is rechecked at stop.
dispatch_one null-metadata-no-session child
[[ ${#OWNED_PID_START[@]} == 1 && -n ${OWNED_PID_START[$OWN_PID]:-} ]]
[[ -z ${OWNED_PID_START[$SIBLING_PID]:-} ]]
[[ ${OWNED_PID_START[$OWN_PID]} == "$OWN_FINGERPRINT" ]]
terminate_owned_pid "$OWN_PID"
for _ in {1..30}; do [[ -z $(pid_live "$OWN_PID" || true) ]] && break; sleep .05; done
[[ -z $(pid_live "$OWN_PID" || true) ]]
[[ -n $(pid_live "$SIBLING_PID") ]]

# Exact bound absence is enough for the production fallback's positive control.
CLEANUP_DEADLINE=$(( $(date +%s) + CLEANUP_SECONDS ))
rm -f "$FIXTURE_ROOT/blocked"
fallback_park_scope
[[ -e $FIXTURE_ROOT/blocked ]]
[[ ${#UNRESOLVED_SPAWN_OWNERSHIP[@]} == 0 ]]

# Missing, ambiguous, stale-start/PID-reuse-shaped, and same-PID/start but
# changed-epoch receipts are unowned. The last case proves comparison is the
# full native fingerprint, never the legacy start-time suffix.
# The card may be parked, but cleanup remains partial and must not signal it.
assert_unowned_partial() {
  local mode=$1 dispatch_rc cleanup_rc
  OWNED_PID_START=()
  UNRESOLVED_SPAWN_OWNERSHIP=()
  sleep 60 & OWN_PID=$!
  OWN_START=$(awk '{print $22}' "/proc/$OWN_PID/stat")
  FIXTURE_EPOCH=fixture-epoch-a
  OWN_FINGERPRINT="$FIXTURE_EPOCH|$OWN_START"
  export OWN_PID OWN_START OWN_FINGERPRINT FIXTURE_EPOCH
  rm -f "$FIXTURE_ROOT/missing-event" "$FIXTURE_ROOT/ambiguous-event" "$FIXTURE_ROOT/pid-reuse" "$FIXTURE_ROOT/epoch-change" "$FIXTURE_ROOT/blocked"
  touch "$FIXTURE_ROOT/$mode"
  [[ $mode != epoch-change ]] || { FIXTURE_EPOCH=fixture-epoch-b; export FIXTURE_EPOCH; }
  if dispatch_one "$mode" child 2>"$FIXTURE_ROOT/$mode.stderr"; then dispatch_rc=0; else dispatch_rc=$?; fi
  [[ $dispatch_rc -ne 0 && ${#OWNED_PID_START[@]} == 0 ]]
  # Structural absence/ambiguity fails at receipt parsing; a stale start token
  # reaches the live /proc comparison and must fail there. Neither is owned.
  if [[ $mode == pid-reuse || $mode == epoch-change ]]; then
    # These live-identity mismatches report through the durable spawn receipt,
    # not stderr. Ordinary execution must not depend on bash tracing output.
    python3 - "$FIXTURE_ROOT/logs/spawn-attempt-child.json" <<'PY'
import json, pathlib, sys
r=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert r['phase'] == 'unknown' and r['ownership'] == 'unresolved', r
assert r['detail'] == 'receipt parsed but no live owned PID registered', r
PY
  else
    [[ $(<"$FIXTURE_ROOT/$mode.stderr") == *'native active run/PID/start ownership receipt unavailable'* ]]
  fi
  CLEANUP_DEADLINE=$(( $(date +%s) + CLEANUP_SECONDS ))
  if fallback_park_scope 2>"$FIXTURE_ROOT/$mode-cleanup.stderr"; then cleanup_rc=0; else cleanup_rc=$?; fi
  [[ $cleanup_rc -ne 0 ]]
  [[ -n $(pid_live "$OWN_PID") ]]
  [[ -e $FIXTURE_ROOT/blocked ]]
  python3 - "$FIXTURE_ROOT/logs/spawn-attempt-child.json" <<'PY'
import json, pathlib, sys
r=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert r['phase'] == 'partial' and r['ownership'] == 'unresolved', r
assert 'unresolved ownership remains after cleanup' in r['detail'], r
PY
  [[ $(<"$FIXTURE_ROOT/$mode-cleanup.stderr") == *'cleanup remains partial'* ]]
  kill "$OWN_PID" 2>/dev/null || true
  wait "$OWN_PID" 2>/dev/null || true
  OWN_PID=
  rm -f "$FIXTURE_ROOT/$mode"
}
assert_unowned_partial missing-event
assert_unowned_partial ambiguous-event
assert_unowned_partial pid-reuse
assert_unowned_partial epoch-change

# Extract the production polling helper itself.  Clock, sleep, status, and SQL
# are controlled doubles: this test cannot open a native board or provider.
source <(python3 - "$TARGET" <<'PY'
import pathlib, sys
s = pathlib.Path(sys.argv[1]).read_text(encoding='utf8')
a = s.index('wait_for() {')
b = s.index('\n}\ndispatch_one()', a) + 2
print(s[a:b])
PY
)
WAIT_NOW=100
WAIT_STATUS_CALLS=0
WAIT_SQL_CALLS=0
WAIT_SLEEP_CALLS=0
WAIT_DIE=
date() { [[ ${1:-} == +%s ]] || return 2; printf '%s\n' "$WAIT_NOW"; }
sleep() { [[ ${1:-} == 5 ]] || return 2; WAIT_SLEEP_CALLS=$((WAIT_SLEEP_CALLS + 1)); WAIT_NOW=$((WAIT_NOW + 5)); }
status() { printf '.\n' >>"$FIXTURE_ROOT/wait-status.calls"; printf '{"fixture":"status"}\n'; }
readonly_sql() { [[ ${1:-} == plan ]] || return 2; printf '.\n' >>"$FIXTURE_ROOT/wait-sql.calls"; printf '[]\n'; }
note() { :; }
die() { WAIT_DIE=$1; return 77; }
START_EPOCH=0
MAX_WALL_SECONDS=1000
MAX_TASK_SECONDS=10
# Default limit: succeeds immediately, proving initialization no longer trips
# nounset before the predicate can observe mocked values.
wait_for default-limit 'printf True'
[[ $(wc -l <"$FIXTURE_ROOT/wait-status.calls") == 1 && $(wc -l <"$FIXTURE_ROOT/wait-sql.calls") == 1 && $WAIT_SLEEP_CALLS == 0 && -z $WAIT_DIE ]]
# Custom limit: separate successful call proves the optional per-wait override.
: >"$FIXTURE_ROOT/wait-status.calls"; : >"$FIXTURE_ROOT/wait-sql.calls"
WAIT_NOW=200; WAIT_SLEEP_CALLS=0; WAIT_DIE=
wait_for custom-limit 'printf True' 1
[[ $(wc -l <"$FIXTURE_ROOT/wait-status.calls") == 1 && $(wc -l <"$FIXTURE_ROOT/wait-sql.calls") == 1 && $WAIT_SLEEP_CALLS == 0 && -z $WAIT_DIE ]]
# Timeout: false predicate polls at controlled 200 and 205, then exits at 210;
# no unbounded wait and no native command are possible in this fixture.
: >"$FIXTURE_ROOT/wait-status.calls"; : >"$FIXTURE_ROOT/wait-sql.calls"
WAIT_NOW=200; WAIT_SLEEP_CALLS=0; WAIT_DIE=
if wait_for timeout-limit 'printf False' 6; then
  printf '%s\n' 'wait_for timeout unexpectedly succeeded' >&2; exit 1
else
  wait_rc=$?
fi
[[ $wait_rc == 77 && $WAIT_DIE == 'task cap waiting for timeout-limit' ]]
[[ $(wc -l <"$FIXTURE_ROOT/wait-status.calls") == 2 && $(wc -l <"$FIXTURE_ROOT/wait-sql.calls") == 2 && $WAIT_SLEEP_CALLS == 2 && $WAIT_NOW == 210 ]]
unset -f date sleep status readonly_sql note die
printf '%s\n' 'fixture-test: production wait_for default/custom/immediate/controlled-timeout assertions passed'

# Exercise the production provisional wait with only status doubles.  A held
# implementation without a durable pending proposal must stop after its first
# observation; conversely, a valid exact pending marker remains authoritative
# even when the worker's own native run/task is already completed.
source <(python3 - "$TARGET" <<'PY'
import pathlib, sys
s = pathlib.Path(sys.argv[1]).read_text(encoding='utf8')
a = s.index('wait_for_provisional_request() {')
b = s.index('\n}\n\nwait_for_native_handoff()', a) + 2
print(s[a:b])
PY
)
PROVISIONAL_NOW=300
PROVISIONAL_SLEEP_CALLS=0
PROVISIONAL_DIE=
date() { [[ ${1:-} == +%s ]] || return 2; printf '%s\n' "$PROVISIONAL_NOW"; }
sleep() { [[ ${1:-} == 5 ]] || return 2; PROVISIONAL_SLEEP_CALLS=$((PROVISIONAL_SLEEP_CALLS + 1)); PROVISIONAL_NOW=$((PROVISIONAL_NOW + 5)); }
status() { printf '.\n' >>"$FIXTURE_ROOT/provisional-status.calls"; printf '%s\n' "$PROVISIONAL_STATUS"; }
note() { :; }
die() { PROVISIONAL_DIE=$1; return 77; }
PYTHON=$(command -v python3)
START_EPOCH=0
MAX_WALL_SECONDS=1000
MAX_TASK_SECONDS=1200
PROVISIONAL_STATUS='{"status":{"review_handoffs":[{"operation_key":"op","state":"pending","review_marker":{"operation_key":"op","reviewer_profile":"worker-write-local"}}],"native_tasks":{"task":{"native_task":{"status":"completed"},"runs":[{"status":"completed"}]}}}}'
: >"$FIXTURE_ROOT/provisional-status.calls"
wait_for_provisional_request op task worker-write-local
[[ $(wc -l <"$FIXTURE_ROOT/provisional-status.calls") == 1 && $PROVISIONAL_SLEEP_CALLS == 0 && -z $PROVISIONAL_DIE ]]
PROVISIONAL_SLEEP_CALLS=0; PROVISIONAL_DIE=
PROVISIONAL_STATUS='{"status":{"review_handoffs":[],"native_tasks":{"task":{"native_task":{"status":"blocked"},"runs":[{"status":"blocked"}]}}}}'
: >"$FIXTURE_ROOT/provisional-status.calls"
if wait_for_provisional_request op task worker-write-local; then
  printf '%s\n' 'blocked worker unexpectedly waited for a provisional request' >&2; exit 1
else
  provisional_rc=$?
fi
[[ $provisional_rc == 1 && $(wc -l <"$FIXTURE_ROOT/provisional-status.calls") == 1 && $PROVISIONAL_SLEEP_CALLS == 0 ]]
[[ $PROVISIONAL_DIE == 'provisional local-review authority failure for op' ]]
PROVISIONAL_SLEEP_CALLS=0; PROVISIONAL_DIE=
PROVISIONAL_STATUS='{"status":{"review_handoffs":[],"native_tasks":{"task":{"native_task":{"status":"completed"},"runs":[{"status":"completed","outcome":"review_requested"}]}}}}'
: >"$FIXTURE_ROOT/provisional-status.calls"
if wait_for_provisional_request op task worker-write-local; then
  printf '%s\n' 'unbound terminal review unexpectedly waited for a provisional request' >&2; exit 1
else
  provisional_rc=$?
fi
[[ $provisional_rc == 1 && $(wc -l <"$FIXTURE_ROOT/provisional-status.calls") == 1 && $PROVISIONAL_SLEEP_CALLS == 0 ]]
[[ $PROVISIONAL_DIE == 'provisional local-review authority failure for op' ]]
unset -f date sleep status note die
printf '%s\n' 'fixture-test: pending-authoritative completed transition and fail-fast blocked/unbound provisional detection passed'

# Extract the production configured-check writer and execute the generated
# absolute runner against a real temporary module.  This must prove the runner
# itself, rather than this fixture's shell environment, suppresses both Python
# bytecode and pytest's cache provider; workers receive only this configured
# no-argument command and cannot append arbitrary pytest selectors/options.
source <(python3 - "$TARGET" <<'PY'
import pathlib, sys
s = pathlib.Path(sys.argv[1]).read_text(encoding='utf8')
a = s.index('write_configured_check_runner() {')
b = s.index('\n}\n', a) + 2
print(s[a:b])
PY
)
CHECK_REPO="$FIXTURE_ROOT/configured-check-repo"
CHECK_CACHE="$FIXTURE_ROOT/configured-check-cache"
mkdir -p "$CHECK_REPO"
printf 'VALUE = 23\n' >"$CHECK_REPO/fixture_module.py"
printf 'import fixture_module\n\ndef test_fixture_module():\n    assert fixture_module.VALUE == 23\n' >"$CHECK_REPO/test_fixture_module.py"
git -C "$CHECK_REPO" init -q
git -C "$CHECK_REPO" config user.name 'm7-production-fixture'
git -C "$CHECK_REPO" config user.email 'm7-production-fixture@invalid'
git -C "$CHECK_REPO" add fixture_module.py test_fixture_module.py
git -C "$CHECK_REPO" commit -qm 'fixture: configured check baseline'
[[ $(git -C "$CHECK_REPO" config --local user.name) == m7-production-fixture ]]
[[ $(git -C "$CHECK_REPO" config --local user.email) == m7-production-fixture@invalid ]]
CHECK="$FIXTURE_ROOT/run-configured-pytest"
VERIFY_PYTHON=${M7_FIXTURE_VERIFY_PYTHON:-$(python3 - "$TARGET" <<'PY'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding='utf8')
match = re.search(r'^CANDIDATE_SOURCE=(.+)$', text, re.M)
if not match:
    raise SystemExit('target does not declare CANDIDATE_SOURCE')
print(pathlib.Path(match.group(1)).parent / 'verification-venv' / 'bin' / 'python')
PY
)}
[[ -x $VERIFY_PYTHON ]] || { printf '%s\n' 'fixture verification interpreter unavailable' >&2; exit 66; }
REPO="$CHECK_REPO"
ORIGINAL_TMPDIR=${TMPDIR-__unset__}
write_configured_check_runner "$CHECK"
env -u PYTHONDONTWRITEBYTECODE TMPDIR="$FIXTURE_ROOT/fixture-tmp" "$CHECK"
[[ ${TMPDIR-__unset__} == "$ORIGINAL_TMPDIR" ]]
[[ ! -e $CHECK_REPO/__pycache__ && ! -e $CHECK_REPO/.pytest_cache ]]
[[ ! -e $CHECK_CACHE ]]
[[ -z $(git -C "$CHECK_REPO" status --porcelain) ]]
if "$CHECK" ignored-selector >/dev/null 2>&1; then
  printf '%s\n' 'configured check runner accepted an arbitrary worker pytest argument' >&2
  exit 1
fi
[[ -z $(git -C "$CHECK_REPO" status --porcelain) ]]
printf '%s\n' 'fixture-test: generated configured check runner is cache-free, argument-closed, and leaves its trusted fixture repository clean'

python3 - "$FIXTURE_ROOT/logs" <<'PY'
import json, pathlib, sys
logs=pathlib.Path(sys.argv[1])
expected={
 'capture-json': ('success',0,True),
 'capture-empty': ('error',0,False),
 'capture-nonjson': ('error',0,False),
 'capture-failure': ('error',3,True),
 'capture-partial': ('partial_cancellation',4,True),
 'capture-timeout': ('error',124,False),
}
for label,(outcome,rc,valid) in expected.items():
 r=json.loads((logs/f'{label}.capture.json').read_text())
 assert (r['outcome'],r['exit_code'],r['stdout_json_valid']) == (outcome,rc,valid), r
 assert r['stdout_sha256'] and r['stderr_sha256']
print('fixture-test: native full epoch|start ownership, nested sibling preservation, exact termination, missing/ambiguous/PID-reuse/changed-epoch rejection, unresolved partial cleanup, and production capture receipts passed')
PY
printf '%s\n' 'fixture-test: production helper fixture assertions passed'
