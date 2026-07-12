# Measured service objectives

These are measurement definitions, not production SLA claims. Numeric production objectives are
deliberately unset until a representative deployment records the required window.

| Signal | Current measured basis | Measurement required before setting an objective |
| --- | --- | --- |
| Ingest availability | Not measured over a production-length window | Successful non-invalid requests divided by all valid ingest requests over a declared rolling window |
| Ingest latency | The 2026-07-11 local PostgreSQL smoke persisted 100,000 events in 319.061 seconds (313.4 events/second); it did not measure request percentiles | p50/p95/p99 request duration for the declared batch size, concurrency, hardware, database, and window |
| Event durability | Deterministic backup/restore and crash/lease scenarios passed; no long-window loss-rate measurement exists | Acknowledged event IDs reconciled after crash and automated restore, retaining numerator and denominator |
| UI freshness | Live progression/reconnect E2E scenarios passed functionally; no representative latency distribution exists | p50/p95/p99 from database commit to authenticated browser receipt under the declared load profile |

The repository's deterministic tests prove correctness but do not simulate a production window.
`components/villani-control-plane/tests/load/RESULTS.md` is the measured local load artifact.
Unknown deployment measurements remain unknown and no availability, latency, durability, or
freshness objective is inferred from functional-test wall time.
