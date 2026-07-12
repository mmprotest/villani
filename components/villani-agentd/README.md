# Villani Agent Daemon

`villani-agentd` is Villani's local-first telemetry and artifact spool. It binds to loopback and
stores normalized v2 records in SQLite. Local-only mode is the default: without an explicit
enrollment it starts no uploader and makes no external connection.

```console
villani-agentd start
villani-agentd status
villani-agentd wrap --adapter generic-process -- python -c "print('hello')"
villani-agentd stop
villani-agentd doctor
```

The selected endpoint is written beneath `~/.villani/agentd`. Authentication material is stored separately with user-only permissions. The installer never starts the daemon automatically.

If the daemon is already running, a normal public `villani run` automatically registers its
canonical run, events, permitted artifact metadata, and final outcome in this spool. The daemon
acknowledges events only after its SQLite transaction commits. It uses the public run's existing
run ID and idempotent event/outcome keys; it does not create a telemetry-only run. Stopping agentd
does not fail the coding run, and a later resume or run finalization replays locally committed
events that remain pending.

Runs created while agentd was absent follow a separate durable path. Startup and every sync
iteration scan a bounded, deterministic batch of canonical directories under
`VILLANI_HOME/runs`; operators can trigger it directly with:

```console
villani-agentd backfill --batch-size 100
villani-agentd doctor
```

Backfill validates the canonical protocol, preserves the original run/trace/event/attempt and
sequence identities, imports only approved metadata artifacts, and records progress in SQLite.
The command reports `imported`, `already_imported`, `incomplete`, `malformed`,
`unsupported_protocol`, `sensitive_content_rejected`, and `temporarily_failed`; `doctor` exposes
the persistent `local_run_imports` records. Repair the local bundle or remove prohibited content
at its source and rerun the command. Event IDs, sequence identities, artifact IDs, and outcome
keys—not the tracking row—make retries safe after interruption or tracking loss.

To opt into synchronization, exchange a one-time enrollment token and run a first sync:

```console
villani-agentd enroll --control-plane https://control.example \
  --token ONE_TIME_TOKEN --installation-id workstation-01
villani-agentd sync-once
```

The installation credential is stored through the operating-system keyring when a working
`keyring` backend is installed (`villani-agentd[keyring]`). If the OS backend is unavailable,
the daemon uses `~/.villani/agentd/installation-credential` with the same user-only mode/ACL
enforcement as its local token. The sync configuration never contains the credential. Pending
events remain durable until a server acknowledgment; transient failures use jittered exponential
backoff and `Retry-After`, permanent 4xx rejections become inspectable dead letters, and artifact
bytes remain in the local content-addressed store after remote acknowledgment.

Remote execution is a separate explicit opt-in after enrollment. Enrollment by itself continues
to synchronize telemetry only:

```console
villani-agentd worker-enable --worker-id worker-01 \
  --residency au-sydney --network-class restricted-egress \
  --reachable-runtime python-3.11
villani-agentd worker-once
villani-agentd worker-disable
```

The worker reports execution-provider and agent-adapter probes, platform/architecture, configured
model/runtime reachability, CPU/memory/GPU metadata, concurrency, network class, residency labels,
and version. It pulls tasks over the enrolled outbound connection and never listens for remote
task connections. While a child runs it renews the lease; a cancellation response terminates the
child process tree and reports terminal evidence.

Private checkout tasks carry only an opaque, repository-scoped broker reference with a lifetime
of at most 15 minutes. `checkout_secret_commands` in the local sync configuration maps
that reference to a shell-free command which mints the token. The existing secret broker injects
the token into the Git subprocess environment, then clears it; neither the task nor completion
evidence contains the credential. Remote work stays in `~/.villani/agentd/remote-work`. Local
`villani run` remains available and independent of worker registration.

Observation adapters are available for generic processes, mapped or native v2 JSONL, Villani Code runtime/debug events, Codex `exec --json`, and Claude Code `stream-json`. Provider adapters are enabled only when `doctor` detects the documented machine-readable capability; they never fall back to terminal decoration or implicit private-session discovery. Authenticated OTLP/HTTP JSON traces are accepted at `POST /v1/traces`. See [the adapter contract and normalization notes](../../docs/OBSERVATION_ADAPTERS.md).
