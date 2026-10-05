# Typed planner future rehearsal

`scripts/m7-typed-planner-rehearsal.sh --prepare` is non-effectful readiness
work. It requires a clean, exact source commit and the SHA-256 of a newly built
wheel, then creates a fresh disposable preparation receipt. Its config and
provider-route report are read-only hashed readiness inputs, never execution
configuration. It does not install a plugin, create a board, dispatch a worker,
or contact a provider.

`scripts/m7-typed-planner-rehearsal.sh --execute-authorized` is the separately
source-controlled future path. It refuses delegated-child context and requires
this exact parent acknowledgement:

```bash
M7_TYPED_EXECUTE_ACK=I_ACKNOWLEDGE_DISPOSABLE_PROVIDER_REHEARSAL
```

It is intentionally not part of preparation or package tests. An authorized
operator must additionally supply the fresh candidate source/commit/wheel/hash,
pinned Hermes CLI, the allowlisted `worker-code-local` provider config source,
operator-owned request file, disposable board and anchor. The runner rejects an
existing run root, a dirty source candidate, pin drift, non-default
45-minute/20-minute/7-run limits, and cleanup windows over 120 seconds.

Before a native operation it `git archive`s the declared clean commit into the
new run root and records the hash of the source-controlled planner helper. The
wheel is installed only into that run root's disposable venv; runtime helper
imports use the frozen archive and never fall back to the live checkout.
The runner uses the operator bootstrap command to persist the request, obtains the
request-bound `packet(request)` from the pinned wheel, validates its closed
`decisions` schema with the source-controlled runtime helper, and places that
packet in the planner card instructions. The planner is instructed to make one
`local_first_submit_plan(decisions=...)` submission; the parent never submits a
plan or fabricates worker provenance.

After dispatch, the runner polls exact native task/run JSON together with public
status. An owned terminal planner run without persisted plan evidence triggers
bounded cleanup/readback and fails; it is never retried. The provider-free
fixture mode exercises those same production schema-delivery and terminal-wait
helpers against saved native-shaped JSON and records zero provider calls.

## Provider-free verification dependencies

The dynamic-schema regression builds a scratch wheel using `python3 -m pip`.
Supply `pip` explicitly in the ephemeral test environment; `uv run` does not
include it automatically:

```bash
HERMES_M0_CLI='' HERMES_TEST_CLI=/home/ocadmin/.local/bin/hermes \
uv run --no-project --with pytest --with jsonschema --with pip \
pytest -q -p no:cacheprovider tests/
```

The installed Hermes CLI is used only for the temporary staged-plugin Doctor
proof. This command does not authorize a native board or provider rehearsal.

No run receipt means no claim that a worker or provider rehearsal occurred.
