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

You are not asked for a capability score, pricing guess, token estimate, role list, execution environment, or parallelism setting. The selected model starts as `BOOTSTRAP`; other new models start as `UNRATED`. Unknown pricing and capability remain unknown until reliable evidence is available.

Manage models without editing YAML:

```console
villani models
villani models detect
villani models test
villani models add local-qwen --model qwen --provider local --endpoint http://127.0.0.1:1234/v1 --default
villani models remove local-qwen
```

Detection is advisory and uses model-list metadata without an inference call. Testing reports availability and uses zero model tokens. Observed outcomes populate local profiles; `QUALIFIED` requires the configured sample minimum and conservative confidence bound. Manual capability scores remain available only as clearly labelled Advanced overrides.

Choose and preview a public routing preset:

```console
villani policy list
villani policy use "Local first"
villani policy explain "Fix calculator addition"
villani policy simulate --preset balanced
```

The public presets are Reliable, Balanced, Local first, Cheapest acceptable, and Custom. A preview shows raw and effective classification, adjustments, eligible and excluded models, coding and verifier routes, cost status, uncertainty, and policy version without starting a coding attempt. Historical simulation is read-only and does not claim causal savings or counterfactual success.

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
cd calculator
villani run "Fix calculator addition"
```

Inside a Git repository, Villani selects the current repository, configured policy, available
backends, and discovered validation command. Use `--success-criteria`,
`--validation-command`, budget, delivery, approval, or policy options only when you want to
override those defaults. Use `--preset` to choose a different public preset for one run; internal
routing modes remain under Advanced controls.

Delivery is explicit for every accepted run:

```console
villani run "Fix calculator addition" --delivery suggest
villani run "Fix calculator addition" --delivery approve
villani run "Fix calculator addition" --delivery apply
villani run "Fix calculator addition" --delivery branch
villani run "Fix calculator addition" --delivery pull-request
```

`suggest` preserves the selected patch and evidence without touching the repository. `approve`
persists an approval pause that survives process restart. `apply` fails closed unless the active
authority policy permits automatic mutation. `branch` uses a separate local worktree and never
switches the original branch; committing that branch is opt-in. `pull-request` branches, commits,
pushes, and submits through the configured Git-host adapter with a redacted Villani evidence body.
Fresh `villani init` configuration keeps the established bare-command experience by selecting
`apply` only for low-risk runs with acceptance-grade evidence. Removing or tightening that explicit
authority policy makes automatic delivery fail closed; `--delivery` always overrides the default.

Review a paused run with `villani inspect RUN_ID`, then record an audited decision:

```console
villani approve RUN_ID
villani reject RUN_ID
villani request-rerun RUN_ID
villani choose-candidate RUN_ID ATTEMPT_ID
```

Candidate changes are accepted only when policy allows them and the candidate already has
acceptance-grade evidence. Connected Console approvals require an authenticated session. Every
delivery failure keeps the selected patch in the run bundle.

Inspect, resume, or rerun recorded work with:

```console
villani runs
villani inspect RUN_ID
villani resume RUN_ID
villani resume --latest
villani rerun RUN_ID
```

Resume continues the same persisted controller identity only when recovery is safe. Rerun creates
a new run identity, retains parent/root lineage, and starts fresh cost accounting; policy and budget
options may be changed on the new run.

`villani run` exits `0` when the accepted run reached its configured delivery boundary, including a
preserved suggestion or an approval pause; it exits `3` when trustworthy attempts are exhausted
without an accepted patch, and `4` when the controller or delivery fails and manual inspection is
required. Invalid command or configuration input exits `2`.

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
