Native release detached approval boundary

The plugin never signs native release approvals and has no private-key option. The operator workflow is two-step:

1. Run prepare-native-release-revalidation with the registered runtime and external board read access. It performs the paused, projection, external snapshot, worktree, branch, base, and execution-evidence checks, then emits one canonical UTF-8 JSON document. If --output-file is used, the file bytes are the exact bytes to sign.
2. Outside the plugin, an operator-controlled Ed25519 key signs those exact bytes. Pass the detached 64-byte signature file and the approval document to revalidate-native-release. The command loads only the public key and fingerprint from the persisted operator registration, checks the runtime binding authority hash, rejects noncanonical or stale documents, verifies the signature, then appends exactly one immutable event and revalidation row.

The private key must remain outside the plugin and outside the ledger. Public-key registration accepts only a base64 Ed25519 public key and its SHA-256 fingerprint. No key generation, private-key path, CLI public-key override, board write, unpark, state transition, attempt, or model dispatch is part of this flow.
