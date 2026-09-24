#!/bin/sh
# One-time root installer. It never invokes Python or imports staged code.
set -eu
PATH=/usr/bin:/bin
export PATH
[ "$#" -eq 1 ] || { printf '%s\n' 'usage: root-install STAGING_DIRECTORY' >&2; exit 64; }
[ "$(id -u)" = 0 ] || { printf '%s\n' 'root installer requires effective root' >&2; exit 1; }
STAGE=$1
DESTINATION=/usr/local/lib/local-first-orchestrator/c12r1-tk-3
LAUNCHER=/usr/local/sbin/local-first-orchestrator-c12r1-tk-3-human-recovery
RUNTIME_MANIFEST=/etc/local-first-orchestrator/c12r1-tk-3-human-recovery.json
SOURCE_ROOT=$DESTINATION/source
SOURCE_MANIFEST=$DESTINATION/source.sha256
TEMPLATE=$STAGE/root-launcher.sh.in
[ -d "$STAGE/snapshot/local_first_orchestrator" ] && [ -f "$STAGE/c12r1-tk-3-human-recovery.json" ] && [ -f "$TEMPLATE" ] || { printf '%s\n' 'incomplete staging directory' >&2; exit 1; }
find "$STAGE/snapshot" -type l -print -quit | grep -q . && { printf '%s\n' 'staging contains symlink' >&2; exit 1; }
rm -rf -- "$DESTINATION"
install -d -o root -g root -m 0555 "$DESTINATION" "$SOURCE_ROOT" /etc/local-first-orchestrator
( cd "$STAGE/snapshot" && tar -cf - . ) | ( cd "$SOURCE_ROOT" && tar -xf - )
chown -R root:root "$SOURCE_ROOT"
find "$SOURCE_ROOT" -type d -exec chmod 0555 {} \;
find "$SOURCE_ROOT" -type f -exec chmod 0444 {} \;
find "$SOURCE_ROOT" -type l -print -quit | grep -q . && { printf '%s\n' 'snapshot contains symlink' >&2; rm -rf -- "$DESTINATION"; exit 1; }
( cd "$SOURCE_ROOT" && find local_first_orchestrator -type f -name '*.py' -print | LC_ALL=C sort | while IFS= read -r file; do sha256sum "$file"; done ) > "$SOURCE_MANIFEST"
[ -s "$SOURCE_MANIFEST" ] || { printf '%s\n' 'snapshot has no Python source' >&2; rm -rf -- "$DESTINATION"; exit 1; }
chown root:root "$SOURCE_MANIFEST"; chmod 0444 "$SOURCE_MANIFEST"
PYTHON_BIN=$(readlink -f /usr/bin/python3)
[ -f "$PYTHON_BIN" ] || { printf '%s\n' 'resolved system Python is not regular' >&2; exit 1; }
PYTHON_MODE=$(stat -Lc '%a' -- "$PYTHON_BIN")
case "$PYTHON_MODE" in [0-7][0-7][0-7]) ;; *) printf '%s\n' 'system Python mode must be three octal digits' >&2; exit 1 ;; esac
case "${PYTHON_MODE#?}" in *[2367]*) printf '%s\n' 'system Python must not be group/world writable' >&2; exit 1 ;; esac
SOURCE_MANIFEST_SHA256=$(sha256sum "$SOURCE_MANIFEST" | cut -d ' ' -f1)
RUNTIME_MANIFEST_SHA256=$(sha256sum "$STAGE/c12r1-tk-3-human-recovery.json" | cut -d ' ' -f1)
PYTHON_SHA256=$(sha256sum "$PYTHON_BIN" | cut -d ' ' -f1)
for value in "$DESTINATION" "$SOURCE_MANIFEST" "$SOURCE_MANIFEST_SHA256" "$RUNTIME_MANIFEST" "$RUNTIME_MANIFEST_SHA256" "$PYTHON_BIN" "$PYTHON_SHA256" "$PYTHON_MODE"; do case "$value" in *'|'*|*'&'*|*'\n'*) printf '%s\n' 'unsafe rendered value' >&2; exit 1 ;; esac; done
tmp=$DESTINATION/launcher.tmp
sed -e "s|@SOURCE_ROOT@|$SOURCE_ROOT|g" -e "s|@SOURCE_MANIFEST@|$SOURCE_MANIFEST|g" -e "s|@SOURCE_MANIFEST_SHA256@|$SOURCE_MANIFEST_SHA256|g" -e "s|@RUNTIME_MANIFEST@|$RUNTIME_MANIFEST|g" -e "s|@RUNTIME_MANIFEST_SHA256@|$RUNTIME_MANIFEST_SHA256|g" -e "s|@PYTHON_BIN@|$PYTHON_BIN|g" -e "s|@PYTHON_SHA256@|$PYTHON_SHA256|g" -e "s|@PYTHON_MODE@|$PYTHON_MODE|g" -e 's|@ID_BIN@|/usr/bin/id|g' -e 's|@STAT_BIN@|/usr/bin/stat|g' -e 's|@SHA256SUM_BIN@|/usr/bin/sha256sum|g' -e 's|@GREP_BIN@|/usr/bin/grep|g' "$TEMPLATE" > "$tmp"
chown root:root "$tmp"; chmod 0555 "$tmp"
install -o root -g root -m 0600 "$STAGE/c12r1-tk-3-human-recovery.json" "$RUNTIME_MANIFEST"
install -o root -g root -m 0555 "$tmp" "$LAUNCHER"
rm -f "$tmp"
printf '%s\n' 'installed; run the documented independent verification before invoking the launcher.'
