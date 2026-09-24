# C12R1-TK-3 protected launcher installation (future manual procedure)

This is a **code-only staging and installation procedure**. Do not run it from this checkout until independent review approves the exact commit and a human has supplied the real absolute ledger/config/key/exchange paths. Neither script invokes project Python; the root installer only copies bytes and generates checksums.

## Properties and handoff boundary

The root launcher is installed at the fixed path `/usr/local/sbin/local-first-orchestrator-c12r1-tk-3-human-recovery`. It accepts **no arguments** and has no runtime source-path or environment override. Before it executes `python -I -c ...`, it checks:

- each component of the snapshot path is root-owned, a directory, and not group/world-writable (normal `0755` system ancestors and `0555` snapshot directories are accepted);
- every listed Python source is `root:root`, regular, and mode `0444`;
- `/etc/local-first-orchestrator/c12r1-tk-3-human-recovery.json` is `root:root`, regular, and mode `0600`;
- `source.sha256`, its fixed SHA-256 embedded in the launcher, the runtime manifest's fixed SHA-256 embedded in the launcher, and the resolved system Python's fixed SHA-256 all match;
- the resolved system Python is `root:root`, regular, and has the installed exact mode.

The protected signing-key path is a separate manifest field; it is never staged below the source snapshot. The helper module remains unchanged in this correction. Integration must retain its existing `_guard()` source validation, but the launcher establishes the stronger prerequisite: no user-writable Python import occurs as root before the snapshot has passed owner/mode/hash checks.

## Future commands (do not run as part of this change)

From the reviewed checkout, stage bytes as the unprivileged operator. Replace every `/ABSOLUTE/...` value with the approved real path; the destination is fixed and is deliberately repeated in the staging manifest:

```sh
chmod 0755 scripts/c12r1-tk-3-stage-install.sh scripts/c12r1-tk-3-root-install.sh
scripts/c12r1-tk-3-stage-install.sh \
  /var/tmp/c12r1-tk-3-stage \
  /ABSOLUTE/ledger.db \
  /ABSOLUTE/operator.json \
  /usr/bin/python3 \
  /root/.local-first-orchestrator/c12r1-tk-3-ed25519.key \
  /ABSOLUTE/c12r1-tk-3-exchange \
  /usr/local/lib/local-first-orchestrator/c12r1-tk-3
```

Review staged bytes and the manifest before any root action:

```sh
find /var/tmp/c12r1-tk-3-stage -type l -print
sha256sum /var/tmp/c12r1-tk-3-stage/c12r1-tk-3-human-recovery.json
sha256sum /var/tmp/c12r1-tk-3-stage/root-launcher.sh.in
```

Only after independent approval, run the root installer exactly once with the fixed staging directory:

```sh
sudo /bin/sh scripts/c12r1-tk-3-root-install.sh /var/tmp/c12r1-tk-3-stage
```

Then independently verify the installed values before invoking the launcher. The digest output is the exact deployment evidence and must be retained with the approval record:

```sh
stat -Lc '%n %u:%g %a %F' \
  /usr/local/lib/local-first-orchestrator/c12r1-tk-3 \
  /usr/local/lib/local-first-orchestrator/c12r1-tk-3/source \
  /usr/local/lib/local-first-orchestrator/c12r1-tk-3/source.sha256 \
  /etc/local-first-orchestrator/c12r1-tk-3-human-recovery.json \
  /usr/local/sbin/local-first-orchestrator-c12r1-tk-3-human-recovery
sha256sum \
  /usr/local/lib/local-first-orchestrator/c12r1-tk-3/source.sha256 \
  /etc/local-first-orchestrator/c12r1-tk-3-human-recovery.json \
  "$(readlink -f /usr/bin/python3)"
( cd /usr/local/lib/local-first-orchestrator/c12r1-tk-3/source && sha256sum -c ../source.sha256 )
find /usr/local/lib/local-first-orchestrator/c12r1-tk-3/source -type l -print
find /usr/local/lib/local-first-orchestrator/c12r1-tk-3/source -type f ! -perm 0444 -print
find /usr/local/lib/local-first-orchestrator/c12r1-tk-3/source -type d ! -perm 0555 -print
```

Expected results: `stat` shows UID/GID `0:0`; source directories `555`, source files and `source.sha256` `444`, runtime manifest `600`, and launcher `555`; `sha256sum -c` prints only `OK`; all three `find` commands print nothing. Do not invoke the launcher if any command differs. The installer intentionally does not create the private-key or exchange directories; create and verify those under a separately approved key-custody procedure.
