# Agent-system and harness contract

PT5 models a coding route as a complete agent system rather than a model name. The canonical
identity is `villani.agent_system.v1`; it binds the non-secret harness, adapter, protocol, model,
provider endpoint, serving metadata, execution environment, permissions, repository/task profile,
verification policy, capabilities, qualification references, and billing knowledge into a
content-addressed `asys_<sha256>` ID.

Secrets are removed before the configuration digest is calculated. Endpoint identity excludes
credentials, query strings, and fragments. Unknown revisions, costs, environment fingerprints,
and capability facts remain explicit unknowns; they are never replaced by fabricated values.

## Production boundary

Villani Code is the only production-enabled harness in PT5. ACP-over-stdio, direct versioned
protocols, local subprocess integrations, and structured headless CLI fallback are represented by
the lifecycle contract, but no external harness can be enabled or selected. A configured external
system is inspectable as unsupported and disabled. Technical connectivity alone does not qualify a
system.

Legacy `backends` configuration remains readable and is preserved. On load or an atomic
configuration write, each coding backend receives a generated Villani Code agent-system entry.
Generated entries are removed only when their corresponding legacy backend is removed; explicitly
authored entries are preserved. The migration report records these decisions.

Use the public inventory commands without running a coding task:

```text
villani agents list
villani agents inspect <route-or-system-id>
villani agents doctor [route-or-system-id]
```

Each command also supports `--json`. Doctor exits nonzero for a disabled, unsupported, unqualified,
or unhealthy route.

## Adapter lifecycle

`villani.harness_adapter.v1` defines these operations:

```text
probe -> describe capabilities -> prepare session -> execute task
      -> stream normalized events -> collect result and artifacts
      -> request cancellation when needed -> cleanup
doctor (out-of-band diagnostics)
```

The contract fixes protocol negotiation, session IDs, cancellation and timeout behavior, message
bounds, backpressure policy, permission requests, separated stdout/stderr, event ordering, failure
classification, artifacts, and cleanup. Unknown namespaced raw events are retained alongside the
normalized event stream.

Capabilities are tri-state (`supported`, `unsupported`, or `unknown`) and carry declared,
detected, or conformance evidence. In particular, a generic model selector does not imply custom
model or custom provider support.

## Harness-neutral evidence

Every attempt emits `villani.harness_result.v1`, including:

- exact isolated worktree and baseline digest;
- patch and normalized changed paths;
- separated stdout and stderr;
- ordered normalized events and a raw-trace reference;
- usage, cost, and duration with accounting status;
- harness and infrastructure status;
- artifacts and cleanup result.

The verifier receives task, requirements, patch, changed files, validation evidence, and execution
environment facts. It does not receive harness identity, route, cost, or competing-candidate facts.
Only acceptance-eligible verification reaches selection.

## Run artifacts and compatibility

New bundles contain:

```text
agent-systems/index.json
agent-systems/<system-id>.json
agent-systems/migration.json
attempts/<attempt-id>/harness-result.json
```

The manifest lists agent-system IDs and the attempt snapshot links the selected identity and
harness result. All new fields are optional in the version-1 run schemas, so founder-era bundles
remain readable without rewriting or guessing missing identity evidence.

`villani.harness_conformance_report.v1` records the fail-closed conformance kit. Qualification
requires all mandated checks to pass: manifest/protocol negotiation, exact versions, worktree and
path safety, event ordering, cancellation, timeout, malformed or oversized output, process crash,
missing executable, permissions, artifacts, patch correctness, cleanup, redaction, unknown cost,
and cross-platform paths.
