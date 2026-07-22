# Packaged release verification

Run the cross-platform gate from the repository root:

```console
python release-verification/run_release_gate.py --mode local
python release-verification/run_release_gate.py --mode ci
python release-verification/run_release_gate.py --mode release
```

All modes rebuild five Python distributions and four Node distributions, install the wheels and
packed Node packages into clean consumers without editable installs, validate frontend assets and
component compatibility, migrate PostgreSQL to the manifest Alembic head, and run the packaged
connected product. The deterministic fixture service drives eight temporary-repository scenarios
covering authoritative validation, coding escalation, repeated canonical attempt IDs, redaction
and artifact withholding, heuristic-only rejection, verifier escalation, classification floors,
and acknowledged candidate diversity.

After the clean wheel install, the gate runs non-interactive setup against the deterministic local
model fixture using the installed interpreter. It starts services, completes the disposable sample
with exactly one selected acceptance-eligible attempt, checks canonical validation totals, proves
non-destructive delivery, opens the UI, captures five onboarding screenshots, runs doctor, stops
services, verifies zero unexplained dead letters, and scans the evidence for secrets. This evidence
is preserved beneath `artifacts/latest/installed-user-onboarding/<UTC timestamp>/`.

The connected run starts the PostgreSQL control plane and Agentd, enrolls and synchronizes the
daemon, queries the real API, performs six-source canonical reconciliation, serves Villani Web and
Flight Recorder, and runs Playwright at 1280x800, 1440x900, and 1920x1080. Seventeen screenshots
are produced from synchronized fixture data.

Mode policy is fail-closed:

- `local` requires every deterministic product check. Optional external scanners may be
  `unavailable`, and the report explicitly says this is not official certification.
- `ci` additionally requires pip-audit and every Node production-lock audit to execute and pass.
- `release` additionally requires the repository-secret scanner, external SBOM scanner, and
  release-container vulnerability scanner. Missing, failed, or unavailable required tooling is a
  failed release, never a pass.

Every run replaces `artifacts/latest`; stale reports are not reused. Within that run, installed-user
onboarding evidence has its own timestamped directory. The directory contains the
gate report, package hashes and archives, compatibility/build/test/security manifests, PostgreSQL
migration proof, verifier/diversity/classification/redaction evidence, canonical and API snapshots,
browser results, logs, and screenshots. Any missing scenario, zero synchronized runs, unexpected
dead letter, mismatch, broken asset, browser error, missing screenshot, or required scanner failure
leaves the verdict as `RELEASE GATE FAILED`.

## CLI Agent Mode phase

The packaged gate includes a required `cli_agent_mode` phase after clean wheel installation. It
runs `run_cli_agent_gate.py` against the isolated source and installed artifacts, covering API
regression, the 30-scenario fake Codex/Claude suite, mixed profiles, repeated cancellation/cleanup,
blindness, selector eligibility, secret scanning, and CLI/UI projection. Its report, conformance
matrix, exact commands, bounds, and digest index are stored under
`artifacts/latest/cli-agent-mode/`. Missing deterministic evidence is `FAIL`.

Optional real calls remain separately consented:

```console
python release-verification/run_cli_agent_smoke.py --detect-only
python release-verification/run_cli_agent_smoke.py --consent
```

Skipped or unavailable real providers make CLI certification `PARTIAL`; they never turn missing
deterministic evidence into a pass. The smoke command uses disposable repositories, does not inspect
credentials or subscription quota, and states that external calls may consume provider usage.
Detection-only mode runs bounded executable/version/auth/capability probes and never makes a model
call.
