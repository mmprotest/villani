# Villani Control Plane

This component is the single-region Villani v2 persistence and controlled remote-dispatch
boundary. It does not open inbound worker connections and has no enterprise scheduler. Its
enterprise identity/RBAC foundation is documented in
[`docs/IDENTITY_AUTHORIZATION.md`](docs/IDENTITY_AUTHORIZATION.md).
Repository-level deployment, operations, SLO, supply-chain, evaluation, and limitation documents
cover the final foundation without making a general-availability claim.

## Development

From this directory:

```console
docker compose up --build
```

The Compose-only development bearer token is
`local-development-token-change-me-123456`. Replace it outside disposable local development.
Only its scrypt verifier and a SHA-256 lookup digest are stored; plaintext tokens are never
persisted.

Optional local browser authentication is enabled with
`VILLANI_CONTROL_PLANE_DEV_USER_EMAIL` and `VILLANI_CONTROL_PLANE_DEV_USER_PASSWORD`. OIDC uses
the provider interface; the bundled OIDC, SAML, and SCIM providers are development fakes and are
not claimed as production federation integrations.

The API accepts strict v2 telemetry in this shape:

```json
{"batch_id":"batch_001","events":[{"schema_version":"villani.telemetry_envelope.v2"}]}
```

Artifact descriptor requests wrap the exact descriptor as
`{"run_id":"run_001","descriptor":{...}}`. The response is either `already_present` or a
short-lived upload instruction. Filesystem development uploads use the dedicated
`/v1/artifact-uploads/{id}` byte path; production S3-compatible storage uses a presigned PUT.
The daemon then calls the completion endpoint, where size and SHA-256 are verified before the
descriptor becomes downloadable. Artifact bytes never pass through event ingestion.

The filesystem store is the development default. Set
`VILLANI_CONTROL_PLANE_OBJECT_STORE_BACKEND=s3`, bucket, optional endpoint URL, and region for an
S3-compatible production store. `secret` sensitivity and `legal_hold` retention are prohibited by
default and rejected before an upload instruction is issued; allowed sets are configurable.

One-time enrollment tokens produce scoped, rotatable installation credentials. Committed event
and artifact updates are published from leased transactional-outbox rows to tenant/run-authorized
SSE subscriptions at `/v1/runs/{run_id}/stream`. Slow subscribers are disconnected when their
bounded queue fills. Outcome requests are the exact v2 outcome document.
Telemetry is ordered for pagination by server-visible `observed_at` plus a database identity,
while retaining both source `occurred_at` and receiver `observed_at` unchanged.

Canonical attempt IDs are run-local. The database identity is
`(organization_id, run_id, attempt_id)`; APIs always return the original attempt ID and never a
concatenated surrogate. Run detail exposes the durable canonical projection assembled from
structured lifecycle events.

Governance policies resolve project over workspace over organization. They control data-class
retention, metadata-only capture, exclusions, redaction/DLP, residency, legal holds, deletion,
and governed exports. Quotas use the same precedence for runs, events, artifact bytes, model
cost, concurrency, workers, exports, and queries. Exported audit/run commitment documents can be
checked with `python -m villani_control_plane.tamper <document.json>`.

## Controlled remote dispatch

Control-plane API tokens submit immutable tasks at `POST /v1/tasks` and request cancellation at
`POST /v1/tasks/{task_id}/cancel`. Enrolled installation credentials may only heartbeat a worker,
pull a compatible task, renew its lease, and complete that owned lease. Workers always initiate
the connection:

- `PUT /v1/workers/{worker_id}/heartbeat`
- `POST /v1/workers/{worker_id}/tasks/claim`
- `POST /v1/tasks/{task_id}/leases/{lease_id}/renew`
- `POST /v1/tasks/{task_id}/leases/{lease_id}/complete`

Claiming uses PostgreSQL row locks with `SKIP LOCKED`, plus a partial unique index allowing only
one active lease per task. Platform, architecture, provider, adapter, model/runtime reachability,
CPU, memory, GPU, concurrency, network-class, and data-residency constraints are checked before a
lease is created. Lease expiry schedules a bounded retry or dead-letters the task. Completion is
idempotent and the immutable server-issued finalization key permits materialization/finalization
only once. Dispatch transitions are stored as v2 events, spans, and transactional-outbox rows.

Repository URLs cannot contain credentials. Tasks contain only an opaque checkout-secret broker
reference, repository scope, and maximum lifetime; credential values are minted and injected by
the worker's secret broker and are never stored by the control plane.

## Tests

Narrow service and authorization tests use SQLite. PostgreSQL behavior is tested only when
`VILLANI_TEST_POSTGRES_URL` points to an isolated PostgreSQL database:

```console
python -m pytest -q
python -m pytest -q -m postgres
python -m pytest -q -m load --run-load-smoke
```

The load smoke records measured throughput and relation size in its test output. It deliberately
does not assert a production SLO.

The release CI job provisions PostgreSQL 16, executes the full suite with `--run-load-smoke`,
fails explicitly if a PostgreSQL test is skipped, upgrades the live database to Alembic head,
renders offline SQL, and performs a representative `pg_dump`/`pg_restore` comparison. Its JUnit
report and migration SQL are uploaded as the `control-plane-postgres-evidence` artifact. SQLite
unit results are useful development evidence but are not presented as PostgreSQL evidence.
