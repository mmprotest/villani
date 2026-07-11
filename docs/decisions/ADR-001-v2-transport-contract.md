# ADR-001: Separate the local bundle from the transport contract

- Status: accepted
- Date: 2026-07-11

## Context

Villani's v1 schemas describe a durable, inspectable local run bundle. Future runner integration, process boundaries, hosted control, and observability need stable causal telemetry, capability publication, artifact metadata, and coding outcomes without turning the local bundle into a network API or mutating existing v1 semantics.

## Decision

`schemas/v1` remains the local run-bundle contract. It is preserved byte-for-byte and semantically unchanged.

`schemas/v2` is the transport and platform contract. It defines telemetry envelopes, resources, spans, artifact descriptors, coding outcomes, agent and verifier capabilities, and policy publications. Root v2 schemas are normative and Villani Ops packages an identical copy for local validation outside the monorepo.

The v1-to-v2 path is a deterministic projection, not an upgrade in place. W3C-shaped trace and span identifiers are namespaced hashes of recorded legacy identities, and the originals remain in attributes. Missing cost, tokens, outcomes, clocks, tenancy, and parent relationships are not inferred.

Unknown future span kinds remain readable. Strict versioned top-level documents and open attribute/body maps provide the forward-compatibility boundary.

## Consequences

- Existing v1 producers, bundles, and consumers remain valid.
- New integrations can share platform-shaped records without requiring hosted infrastructure.
- Local replay can compare translations byte-for-byte and deduplicate by idempotency key.
- Artifact content remains out of telemetry; transport authorization, storage, encryption, and retention enforcement remain future concerns.
- A daemon, server, uploader, database, or UI is explicitly outside this decision and this implementation pass.
