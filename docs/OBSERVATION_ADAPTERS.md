# Local observation adapters

`villani-agentd` observes local processes; it does not select an agent or execute work remotely. All adapter output is normalized to `villani.telemetry_envelope.v2` before it enters the SQLite spool.

The adapter contract declares a name and contract version, feature detection, capabilities, argument-vector construction, an incremental parser, final outcome parsing, process-tree cancellation, and a sensitive-field policy. `generic-process` records bounded process lifecycle and streams. `generic-jsonl` accepts complete v2 envelopes or a configured dotted-field mapping. `villani-code` consumes its documented debug/runtime JSONL records. `codex` requires the documented `codex exec --json` capability. `claude-code` requires the documented `--output-format stream-json` capability. Decorated terminal output is never a fallback protocol.

`villani-agentd doctor` reports the exact version output, capabilities, and missing capability for every adapter. An absent provider CLI does not affect generic adapters. No adapter searches a provider session directory. A user-supplied file could be passed to a parser by a future explicit configuration surface, but would have to be labelled `best_effort`; this pass adds no implicit file discovery.

Native event identifiers and provider names are preserved as `villani.native.*` attributes. Exact duplicate native records are ignored; revised records with the same native ID receive a deterministic revision and distinct event identity. Partial input is buffered. Malformed middle records and truncated final records become deterministic redacted parse-error events so later valid records remain observable.

Wrapped children receive W3C `traceparent` plus Villani run identity in their environment. A valid inherited `traceparent` is preserved and becomes the causal parent. Invalid inherited values are replaced.

Authenticated OTLP/HTTP JSON traces are accepted at `POST /v1/traces` (also `/v1/otlp/v1/traces`). The payload limit is configurable. GenAI semantic attributes are projected into the body while every OTLP attribute remains queryable in `attributes`. Malformed or oversized requests are rejected as a whole; no partial batch is committed.
