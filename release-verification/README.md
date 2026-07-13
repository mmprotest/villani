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

Every run replaces `artifacts/latest`; stale reports are not reused. The directory contains the
gate report, package hashes and archives, compatibility/build/test/security manifests, PostgreSQL
migration proof, verifier/diversity/classification/redaction evidence, canonical and API snapshots,
browser results, logs, and screenshots. Any missing scenario, zero synchronized runs, unexpected
dead letter, mismatch, broken asset, browser error, missing screenshot, or required scanner failure
leaves the verdict as `RELEASE GATE FAILED`.
