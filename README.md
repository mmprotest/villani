# Villani

Villani is a local-first coding-agent control loop that classifies work, routes isolated attempts, verifies evidence, selects one patch, and leaves a replayable audit bundle.

## Prerequisites

- Python 3.11 or newer.
- Node.js 18 or newer and npm.
- Git.
- A local coding backend or an API credential supplied through an environment variable.

## Install locally

From the repository root, run the same cross-platform installer on Windows, macOS, or Linux:

```console
python scripts/install-local.py
```

The installer prints the exact activation command that makes `villani` and `vfr` discoverable. It is safe to run twice and does not collect telemetry or download a model.

## Quickstart

Create the local configuration and run store:

```console
villani init
```

Add a local OpenAI-compatible backend. This example declares both its bootstrap capability and its local compute cost; replace the URL and model with those exposed by your local server:

```console
villani backend add local-qwen --provider local --base-url http://127.0.0.1:11434/v1 --model qwen2.5-coder --role classification --role coding --capability-score 55 --billing-mode compute_time --compute-cost-per-hour 0.18
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
```

`villani run` exits `0` for an accepted and materialized result, `3` when trustworthy attempts are exhausted without an accepted patch, and `4` when the controller fails and manual inspection is required. Invalid command or configuration input exits `2`.

The public execution path is `villani run`. `villani-ops run --orchestrator ...`, `villani-ops viewer ...`, and `villani-ops cost-run ...` remain compatibility-only interfaces and are not reachable from the public run command.

See the [closed-loop architecture](docs/CLOSED_LOOP.md) and the [canonical protocol and run-bundle contract](docs/CLOSED_LOOP.md#canonical-run-bundle).
