# Villani v2 protocol compatibility

Villani v1 and v2 have different, complementary boundaries. `schemas/v1` remains the canonical local run-bundle contract. `schemas/v2` is the transport and platform contract shared by local runners, a future daemon, a future hosted control plane, and observability consumers. This release adds contracts and local readers only; it does not add a server, uploader, database, daemon, network path, or UI.

## Identity and causality

Every telemetry envelope has a stable `event_id` and a separate `idempotency_key`. Consumers deduplicate on the idempotency key within a protocol version and must treat a repeated byte-equivalent record as the same observation. `run_id` preserves the canonical Villani run identity. `trace_id` and `span_id` use the W3C shapes: 32 lowercase hexadecimal characters for a non-zero trace ID and 16 for a non-zero span ID. `parent_span_id` is nullable.

Legacy Villani trace and event identifiers are mapped with namespaced SHA-256. The first 32 hexadecimal characters form the v2 trace ID and the first 16 form a span ID. The original v1 IDs remain in `attributes` under `villani.legacy.*`. A v1 `parent_event_id` is mapped only when it exists; translation never creates a missing parent relationship.

## Ordering and clock differences

`sequence` is monotonic only inside `sequence_scope`. The canonical v1 projection uses `run:<run_id>` and preserves the v1 sequence exactly. Global ordering must not be inferred across scopes.

`occurred_at` is the source event time. `observed_at` is the receiving boundary's observation time and may differ because clocks are independent. Canonical v1 has only one timestamp, so deterministic translation copies that recorded timestamp into both fields and sets `villani.clock.status` to `legacy_single_timestamp`. This records the absence of a distinct observation clock and does not manufacture one. Consumers should use sequence for within-run ordering and treat clock comparisons across resources as approximate.

## Span kinds and forward compatibility

Known span kinds are `controller_stage`, `agent_run`, `model_call`, `tool_call`, `command`, `file_operation`, `verifier`, `policy_decision`, `selection`, `materialization`, `queue`, and `external_service`. The field is an extensible lower-case protocol name, not a closed enum. Readers must preserve unknown future kinds and render or store them generically. The same forward-compatible rule applies to telemetry `name` and `status` values and to open `attributes` and `body` maps.

Strict top-level fields protect contract boundaries: unknown top-level fields are rejected. A future incompatible shape requires a new schema version. Optional knowledge is represented by nullable fields or explicit status, never by guessed values.

## Unknown values and outcomes

Coding outcomes separate facts from their evidence quality. Verification, acceptance, materialization, merge, revert, CI, developer disposition, and defect association can be null when unobserved. Unknown cost is `null` with `cost_accounting_status: unknown`; unknown latency follows the same rule. Known cost requires a three-letter currency. `provenance_status` says whether outcome data was recorded, derived, or unknown, and `provenance` identifies the available source without inventing missing evidence.

The v1 translator emits telemetry only from recorded canonical events. It does not infer token counts, cost, coding outcomes, additional timestamps, or missing parent relationships from terminal names.

## Artifacts and privacy

Telemetry contains artifact descriptors, references, or logical metadata, never artifact bytes. An artifact descriptor records SHA-256 digest, byte size, media type, logical role, sensitivity, retention class, encryption status, storage reference, provenance status, and attributes. `storage_reference` is an opaque locator, not authorization to fetch or expose content.

Sensitivity values are `public`, `internal`, `confidential`, `restricted`, and `secret`. Producers must redact secrets before serialization and apply retention and encryption policy outside this protocol. Organization, workspace, project, and repository identifiers are nullable so local-first runs do not need hosted tenancy. Their presence is routing metadata, not proof of authorization.

## Fixture and schema compatibility

The shared fixtures under `integration/fixtures/protocol/v2` are read as the same bytes by Python and TypeScript. Both validators accept every valid fixture and reject every invalid fixture by the recorded reason category. Root schemas are normative; packaged Ops schemas must remain semantically identical. Re-translating an unchanged v1 event stream produces byte-stable normalized JSONL.
