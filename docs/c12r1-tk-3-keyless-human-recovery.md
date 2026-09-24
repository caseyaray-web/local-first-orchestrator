# C12R1-TK-3 keyless human-recovery helper (code-only)

`local_first_orchestrator.keyless_human_recovery` is a candidate payload for a **future** root-owned installation. It has not completed independent review and this repository does not install or execute it with sudo. This one-time path assumes the local account's ledger/config writers are trusted; it does not provide a root-anchored scheduler gate against deliberate raw-SQL/config forgery.

The installed entry point has no ticket, database, repository, board, dispatch, or resume arguments. It reads only a root-owned `0600`-or-stricter manifest at `/etc/local-first-orchestrator/c12r1-tk-3-human-recovery.json`, with exactly:

- `ledger_path`, `config_path`, `source_root`, `python_executable`, `key_path`, `exchange_parent`

Before any effect it requires effective root, a real stdin/stdout terminal, sudo provenance, an `ocadmin` target, and a non-symlink root-owned/non-group-or-world-writable source snapshot, interpreter, and manifest. The installed snapshot must provide `local_first_orchestrator` to `/usr/bin/python3 -I`; user-writable checkout code must never be imported by root.

The helper generates/reuses only a raw Ed25519 private key at `key_path` mode `0600`, never prints it, and runs the existing CLI paths after dropping to the sudo invoker UID/GID. It prepares and hashes a C12R1-TK-3-only enrollment document, requires an exact `APPROVE <hash>` terminal confirmation, signs it, and invokes `enroll-operator-signer`. It then separately prepares/hashes C12R1-TK-3 attempt 2 stale-routing recovery, requires a second exact confirmation, signs it, and invokes `recover-stale-routing`.

Prepared public documents/signatures remain in a per-operation exchange directory to permit inspection/retry after a crash. The existing signed APIs own all idempotency and SQLite checkpoints. Any document or config hash drift stops before confirmation/write. The helper contains no board, dispatch, resume, OPS, shell, or sudo call.

This is not live-readiness evidence. A production installation still requires an independent review of the exact snapshot, ownership/mode verification, a separately approved deployment procedure, and human confirmation of paused/lease/runtime state immediately before use.
