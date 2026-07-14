# Villani

Villani is a local-first coding-agent control loop that classifies work, runs isolated attempts, verifies evidence, selects one patch, and keeps every completed run inspectable.

## Prerequisites

- Python 3.11 or newer
- Git
- Either a supported local model server or a cloud-model credential in an environment variable

Node.js is not required by an installed Villani release.

## Install

Install the supported public package with pipx:

```console
pipx install villani
```

While releases are distributed as CI artifacts rather than through a package index, install the downloaded platform wheel instead:

```console
pipx install ./villani-*.whl
```

## First run

Start the guided setup from the Git repository you want to use:

```console
cd your-project
villani setup
```

Setup detects the repository, supported loopback model servers and their loaded models, cloud credentials, existing coding-session history, and Villani Service. It recommends a model, runs a connection check and a small non-destructive capability probe, writes a validated configuration atomically, and offers to start the service, open Villani Console, and complete a disposable sample task.

You are not asked for a capability score, pricing guess, token estimate, role list, execution environment, or parallelism setting. The selected model starts as `unrated` under the explicit bootstrap policy. Unknown pricing remains unknown until reliable provider metadata is available.

Cloud secrets stay outside the configuration. For example, set OpenAI credentials before setup:

```console
export OPENAI_API_KEY="your-provider-key"
villani setup
```

PowerShell uses `$env:OPENAI_API_KEY = "your-provider-key"`. Setup saves only the environment-variable name and never prints the value.

Check the resulting installation and open the one local console:

```console
villani doctor
villani open
```

Machine-readable diagnostics are available with:

```console
villani doctor --json
```

Every failed doctor check includes a concrete recovery command or action.

## Villani Service

The public lifecycle commands are safe to repeat and prevent duplicate processes:

```console
villani service status
villani service start
villani service stop
villani service restart
```

Use `villani service start --automatic` to install user-level automatic startup. Status reports the log path and last detected error. A bounded stop recovers stale process state after an unclean exit.

`villani open` verifies that the service and console are responding before opening the URL; it never silently opens a dead address.

## Run a coding task

```console
villani run "Fix calculator addition" --repo ./calculator --success-criteria "The test suite passes"
```

Inspect or resume recorded runs with:

```console
villani runs
villani inspect RUN_ID
villani resume RUN_ID
villani resume --latest
```

`villani run` exits `0` for an accepted and materialized result, `3` when trustworthy attempts are exhausted without an accepted patch, and `4` when the controller fails and manual inspection is required. Invalid command or configuration input exits `2`.

## Monorepo development

From the repository root, install the development checkout on Windows, macOS, or Linux:

```console
python scripts/install-local.py
```

The development installer requires Node.js 20 and npm to rebuild bundled observability assets. It does not start the service, collect telemetry, or download a model. Internal component and protocol details are documented in [distribution details](docs/DISTRIBUTION.md), [closed-loop architecture](docs/CLOSED_LOOP.md), and the [canonical run-bundle contract](docs/CLOSED_LOOP.md#canonical-run-bundle).

## Verification

The recorded onboarding gate executes setup against a deterministic OpenAI-compatible fixture, starts the real local service, completes and materializes a sample task, runs doctor, validates the console, captures screenshots, and proves shutdown:

```console
python onboarding-verification/run_onboarding_gate.py
```

Its report and screenshots are written beneath `onboarding-verification/artifacts/latest/`.

The complete packaged connected-product gate remains:

```console
python release-verification/run_release_gate.py --mode local
```

It builds release packages in isolation, applies PostgreSQL migrations, executes all connected scenarios, reconciles canonical data across packaged consumers, and runs browser tests.
