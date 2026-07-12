# Operations and recovery

Install `villani-control-plane[otel]` and set
`VILLANI_CONTROL_PLANE_OTLP_ENDPOINT` to enable the OpenTelemetry SDK OTLP/HTTP metrics exporter.
Air-gapped mode rejects an OTLP endpoint. The ordinary JSON metrics endpoint remains available for
local collection.

## Health and shutdown

`/liveness` proves the process loop is running. `/readiness` requires database access and the exact
Alembic head. Kubernetes and Compose use those separate checks. SIGTERM stops admission through
the server, signals the outbox worker, waits up to the configured graceful-shutdown bound, then
cancels it; leased outbox/task records are retryable and idempotent.

## Migration rules

Run `alembic upgrade head` as the dedicated migration Job before rolling API pods. Expand first:
add nullable/defaulted columns or new tables/indexes; deploy readers/writers compatible with both
shapes; backfill in bounded batches; only then contract in a separately approved release. Never
rename/drop a live column, add a table rewrite, or combine destructive data cleanup with rollout.
Every revision must render in PostgreSQL offline mode and pass restore tests.

## Backup and restore

PostgreSQL production procedure: take a provider snapshot plus `pg_dump --format=custom`, checksum
both database and object manifests, encrypt to a separate failure domain, and record migration head,
object-store version markers, region, and key IDs. Restore into a new database, run `pg_restore`,
verify checksums and Alembic head, reconcile object manifests, run readiness and tenant-isolation
smokes, then switch traffic. Never restore over the only copy.

Local SQLite restore is automated with `python -m villani_control_plane.backup`; it uses SQLite's
online backup API, a SHA-256 manifest, an empty destination, and `PRAGMA integrity_check`.

## Incidents and rollback

For ingestion degradation: stop rollout, preserve evidence, inspect readiness/outbox/lease metrics,
disable the failing provider/policy canary, and roll back to the last compatible application image.
Do not downgrade the database. For suspected exfiltration: revoke keys/sessions, disable exports,
place legal holds, retain audit-chain heads, and rotate affected encryption keys. Corrections append
new audit/provenance records; history is never rewritten.
