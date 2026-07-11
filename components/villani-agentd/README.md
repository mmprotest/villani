# Villani Agent Daemon

`villani-agentd` is Villani's local-only telemetry and artifact spool. This first pass binds to loopback, stores normalized v2 records in SQLite, and performs no cloud upload.

```console
villani-agentd start
villani-agentd status
villani-agentd wrap --adapter generic-process -- python -c "print('hello')"
villani-agentd stop
villani-agentd doctor
```

The selected endpoint is written beneath `~/.villani/agentd`. Authentication material is stored separately with user-only permissions. The installer never starts the daemon automatically.

Observation adapters are available for generic processes, mapped or native v2 JSONL, Villani Code runtime/debug events, Codex `exec --json`, and Claude Code `stream-json`. Provider adapters are enabled only when `doctor` detects the documented machine-readable capability; they never fall back to terminal decoration or implicit private-session discovery. Authenticated OTLP/HTTP JSON traces are accepted at `POST /v1/traces`. See [the adapter contract and normalization notes](../../docs/OBSERVATION_ADAPTERS.md).
