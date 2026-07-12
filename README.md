# Villani

Villani is a local-first coding-agent control loop that classifies work, routes isolated attempts, verifies evidence, selects one patch, and leaves a replayable audit bundle.

## Prerequisites

- Python 3.11 or newer.
- Git.
- A local coding backend or an API credential supplied through an environment variable.

Node.js, npm, and Bun are release-build dependencies only. They are not required by an installed platform package or self-contained release archive.

## Install

The supported user path is one platform package:

```console
pipx install villani
```

It installs `villani`, `villani-code`, `villani-agentd`, and `vfr`. This repository currently builds local release candidates and does not publish them; install a CI-produced platform wheel with `pipx install ./villani-*.whl` while the release is unpublished.

## Monorepo development

From the repository root, run the same cross-platform installer on Windows, macOS, or Linux:

```console
python scripts/install-local.py
```

The development installer requires Node.js 18 and npm to rebuild Flight Recorder. It prints the activation command for all four executables, is safe to run twice, installs but does not start the local daemon, and does not collect telemetry or download a model. See [distribution and user-service details](docs/DISTRIBUTION.md).

The canonical closed-loop provider names are `local`, `openai-compatible`, and
`openai`. The first two require an explicit `--base-url`; `openai` uses
`https://api.openai.com/v1` when no URL is supplied. All three are translated to
Villani Code's supported `--provider openai` protocol mode.

## Quickstart

Create the local configuration and run store:

```console
villani init
```

Add a local OpenAI-compatible backend. This example declares both its bootstrap capability and its local compute cost; replace the URL and model with those exposed by your local server:

```console
villani backend add local-qwen --provider local --base-url http://127.0.0.1:11434/v1 --model qwen2.5-coder --role classification --role coding --capability-score 55 --billing-mode compute_time --compute-cost-per-hour 0.18
```

For another OpenAI-compatible local server, use the same vocabulary and keep
the API key in the environment:

```console
export LOCAL_API_KEY="dummy"
villani backend add local-stub --provider openai-compatible --base-url http://127.0.0.1:8000/v1 --model deterministic --role classification --role coding --capability-score 55 --billing-mode compute_time --compute-cost-per-hour 0.18 --api-key-env LOCAL_API_KEY
```

Or add an API backend without putting a literal key in configuration or shell history:

```console
export OPENAI_API_KEY="your-key-from-the-provider"
villani backend add api-coder --provider openai --model gpt-5-codex --role classification --role coding --capability-score 85 --billing-mode token --input-cost-per-million 1.25 --output-cost-per-million 10.00 --api-key-env OPENAI_API_KEY
```

On PowerShell, set the variable with `$env:OPENAI_API_KEY = "your-key-from-the-provider"` instead. Run one canonical closed loop against an existing Git repository:

```console
villani run "Fix calculator addition" --repo ./calculator --success-criteria "The test suite passes"
```

Inspect and replay local runs:

```console
villani runs
villani inspect RUN_ID
villani open RUN_ID
villani resume RUN_ID
villani resume --latest
```

When `villani-agentd` is installed and already running, `villani run` automatically registers the
same canonical run and sends each event to the daemon after the event is durably appended to the
local run bundle. It does not start the daemon. If the daemon is absent, stopped, or temporarily
unavailable, execution remains local-first and records the telemetry condition in
`telemetry_diagnostics.jsonl` without changing the coding result. Enrollment and upload remain
explicit opt-ins.

The run ID printed by `villani run` is the identity used by the local run directory, daemon spool,
control-plane run and outcome records, web run detail, and Flight Recorder replay. To verify a
synchronized run, use `villani-agentd status`, run `villani-agentd sync-once` when enrolled, and
look up that exact run ID in the control-plane/web run detail. Pending or degraded delivery remains
inspectable in the local telemetry diagnostics and daemon status.

`villani run` exits `0` for an accepted and materialized result, `3` when trustworthy attempts are exhausted without an accepted patch, and `4` when the controller fails and manual inspection is required. Invalid command or configuration input exits `2`.

The public execution path is `villani run`. `villani-ops run --orchestrator ...`, `villani-ops viewer ...`, and `villani-ops cost-run ...` remain compatibility-only interfaces and are not reachable from the public run command.

See the [closed-loop architecture](docs/CLOSED_LOOP.md) and the [canonical protocol and run-bundle contract](docs/CLOSED_LOOP.md#canonical-run-bundle).
