#!/bin/sh
# Non-root staging only: it never executes project Python.
set -eu
[ "$#" -eq 7 ] || { printf '%s\n' 'usage: stage DIR LEDGER CONFIG PYTHON KEY EXCHANGE DESTINATION' >&2; exit 64; }
STAGE=$1; LEDGER=$2; CONFIG=$3; PYTHON_BIN=$4; KEY=$5; EXCHANGE=$6; DESTINATION=$7
case "$STAGE:$LEDGER:$CONFIG:$PYTHON_BIN:$KEY:$EXCHANGE:$DESTINATION" in *'\n'*|*'"'*|*'\\'*) printf '%s\n' 'paths must not contain newline, quote, or backslash' >&2; exit 64 ;; esac
for path in "$LEDGER" "$CONFIG" "$PYTHON_BIN" "$KEY" "$EXCHANGE" "$DESTINATION"; do
    case "$path" in /*) ;; *) printf '%s\n' 'all installed paths must be absolute' >&2; exit 64 ;; esac
done
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
[ -d "$ROOT/local_first_orchestrator" ] || { printf '%s\n' 'package source missing' >&2; exit 1; }
rm -rf -- "$STAGE"
mkdir -p -- "$STAGE/snapshot"
tar -C "$ROOT" --exclude='__pycache__' --exclude='*.pyc' -cf - local_first_orchestrator | tar -C "$STAGE/snapshot" -xf -
printf '{"ledger_path":"%s","config_path":"%s","source_root":"%s/source","python_executable":"%s","key_path":"%s","exchange_parent":"%s"}\n' "$LEDGER" "$CONFIG" "$DESTINATION" "$PYTHON_BIN" "$KEY" "$EXCHANGE" > "$STAGE/c12r1-tk-3-human-recovery.json"
cp "$ROOT/scripts/c12r1-tk-3-root-launcher.sh.in" "$STAGE/root-launcher.sh.in"
printf '%s\n' 'staged; inspect this directory, then run the documented root installer command.'
