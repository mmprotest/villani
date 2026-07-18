# Villani Flight Recorder engine

Flight Recorder is Villani's internal parsing, session-discovery, indexing, and
replay-data engine. It is not a separate mainstream application. Developers and
users should open the single product interface with:

```console
villani open
```

Villani Console combines canonical Villani runs and imported coding-agent
sessions in History, and renders both through the same Replay routes and shared
`@villani/ui` shell.

## Engine responsibilities

The package continues to own:

- Claude Code, Codex, Pi, generic JSONL, and canonical Villani parsing
- local session discovery and incremental indexing
- redaction and replay normalization
- strict parsing of agent-system identities, harness-neutral results, and conformance reports
- Git replay evidence collection
- the structured Console history and replay adapter

It does not run a second web server, theme, application shell, or mainstream
navigation system. Villani Web does not import this package's raw source.
Canonical derivation shared by the applications comes from
`@villani/run-model`.

## Compatibility CLI

The `vfr` command remains available for advanced diagnostics and scripts.

```console
vfr scan --all
vfr sessions
vfr tasks
```

`browse`, `open`, and the normal `launch` flow are compatibility aliases for
the running Villani Console. They require Villani Service and resolve to
Console History. `launch --provider villani --run-id <id>` resolves directly to
that run's Console Replay route. An optional `--out` on History aliases writes a
small redirect document, not a second application.

```console
vfr browse --open
vfr launch --all
vfr launch --provider villani --run-id run_123
```

If Villani Service is stopped, start it with `villani service start`.

## Structured Console adapter

Agentd invokes the presentation-neutral adapter with bounded subprocesses:

```console
vfr console-data --kind history
vfr console-data --kind run --id run_123
vfr console-data --kind session --id session_123
```

The adapter emits versioned JSON and never exposes source transcript paths to
the browser. Agentd merges its result with authoritative local synchronization
state before serving `/v1/console/*`.

## Offline evidence compatibility

`vfr replay` and `vfr git-replay` remain advanced compatibility commands for
writing a self-contained evidence artifact when an offline file is explicitly
requested. These files are exports; they do not provide a session browser or a
second product navigation system.

For backward compatibility, explicitly combining `vfr launch --run-id` with
`--out` also writes this offline evidence export. Without `--out`, the same
command opens the run in Console Replay.

```console
vfr replay --session ./codex-rollout.jsonl --provider codex --out ./replay.html
vfr git-replay --repo ./repo --from HEAD~1 --to HEAD --out ./git-replay.html
```

Replay output is redacted by default. Do not share exported evidence without
reviewing prompts, paths, commands, diffs, and model metadata.

## Development

```console
npm ci
npm test
npm run typecheck
npm run build
npm run format:check
```

The package supports Node.js 20 or newer. Its tarball contains `dist`, including
the structured Console adapter and all parsing/indexing engines required by the
installed Villani Service.
