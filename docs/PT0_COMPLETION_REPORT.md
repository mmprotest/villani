# PT0 Completion Report: Restore Product Truth and First-Run Integrity

Status: **COMPLETE**  
Final release certification: **RELEASE GATE PASSED**  
Completed: 2026-07-17 04:34:50Z  
Machine-readable companion: [`PT0_COMPLETION_REPORT.json`](PT0_COMPLETION_REPORT.json)

PT0 is complete. PT1 was not started.

## Outcome

The recorded installed-user sample now reaches one selected, acceptance-eligible result without a
task, repository, language, operating-system, or onboarding bypass. Its authoritative repository
command passes once, no redundant focused probe runs, three requirements are proved, classification
is easy, non-destructive delivery completes, doctor passes, and all public surfaces agree.

Missing or uncertain evidence still fails closed. A passing generic suite cannot prove an unrelated
requirement, malformed semantic output remains ineligible, and behavior failures remain distinct
from validation or probe infrastructure failures.

## User-facing behavior

Before PT0, the onboarding sample produced a non-empty patch that changed implementation and tests,
passed `python -m unittest -q`, and received semantic approval, but Villani rejected it with
`focused_probe_missing`. The terminal displayed `0 repository checks passed; 0 failed` even though
the canonical repository-validation artifact recorded a passing command. The trivial task also
classified as hard.

After PT0, the same generic workflow links the changed behavior, changed test, and executed
authoritative validation through conservative coverage. It suppresses only a provably redundant
probe, selects the proved candidate, and displays one passed and zero failed repository checks in
CLI, web, Markdown reports, static/support artifacts, and Flight Recorder. The task classifies easy
unless repository evidence supplies a real risk signal. Unrelated or uncertain requirements still
request a focused probe or remain unproved.

## Architecture and product decisions

- `villani.validation_coverage.v1` records command identity and safe display, execution role, cwd,
  status, start/end time, discoverable targets, changed tests, requirement links, provenance,
  confidence, inability reasons, and artifact references. Coverage is deterministic and
  conservative.
- A generic passing suite is not universal proof. A changed test contributes only when the evidence
  graph links the requirement, changed behavior, changed test, and authoritative execution.
- Verifier-requested probes are persisted before acceptance, scheduled by the controller, run in
  the preserved candidate environment, and linked to candidate, requirement, evidence, worktree,
  and environment identity.
- Probe behavior mismatch and probe infrastructure failure are separate durable outcomes. Both
  block acceptance; only the former is a candidate behavior failure.
- Known deterministic no-model advisories are projected into non-accepting canonical actions while
  preserving their raw value. Unknown or malformed semantic-verifier actions remain malformed.
- `villani.run_summary.v1` is the sole presentation projection for counts, requirement proof, probe
  results, and accounting status. Unknown is never rendered as zero.
- Classification normalizes duplicate/restated criteria and combines repository breadth,
  subsystem count, risk, dependency uncertainty, validation burden, and scope. Raw signals and
  effective classification are both durable.
- Installation stages the full environment and configuration, verifies mandatory imports and
  executables, and only then activates. Failure or interruption restores the prior working state,
  removes incomplete staged state, and emits one exact repair command.
- Runtime and development dependency profiles are separate. `pip-audit` remains a development/CI
  release scanner and was not added to production runtime dependencies.
- The release gate builds packages, installs only built artifacts into clean environments, uses a
  deterministic local model fixture, drives the installed product and browser, performs
  non-destructive delivery, scans evidence, and retains a timestamped bundle.

## Schema and configuration migrations

- Added normative and packaged `villani.validation_coverage.v1` schemas.
- Added normative and packaged `villani.run_summary.v1` schemas.
- Added optional run-summary and validation-coverage references to `villani.run_manifest.v1`.
- Extended Python and TypeScript protocol models and schema validation together.
- Added a valid migrated protocol fixture with repository validation, coverage, evidence, and
  summary artifacts.
- Legacy bundles synthesize conservative summaries and coverage. Missing legacy information stays
  unknown/not-run and never becomes inferred proof.
- Existing configurations remain readable. Installer/setup activation preserves the previous
  environment and configuration until staged validation succeeds.

## Files changed

Added:

```text
components/villani-agentd/villani_agentd/console_assets/assets/index-BwtYjqYI.js
components/villani-ops/villani_ops/closed_loop/run_summary.py
components/villani-ops/villani_ops/closed_loop/validation_coverage.py
components/villani-ops/villani_ops/schemas/v1/run-summary.schema.json
components/villani-ops/villani_ops/schemas/v1/validation-coverage.schema.json
components/villani-ops/villani_ops/tests/closed_loop/test_run_summary.py
components/villani-ops/villani_ops/tests/closed_loop/test_validation_coverage.py
components/villani-ops/villani_ops/tests/fixtures/classification_calibration.json
components/villani-web/dist/assets/index-BwtYjqYI.js
docs/PT0_COMPLETION_REPORT.json
docs/PT0_COMPLETION_REPORT.md
integration/fixtures/protocol/v1/valid_run/attempts/attempt_002/repository-validation.json
integration/fixtures/protocol/v1/valid_run/run-summary.json
integration/fixtures/protocol/v1/valid_run/validation-coverage.json
integration/fixtures/protocol/v1/valid_run/verification/attempt_002-evidence.json
schemas/v1/run-summary.schema.json
schemas/v1/validation-coverage.schema.json
```

Changed:

```text
PLANS.md
README.md
components/villani-agentd/villani_agentd/console.py
components/villani-agentd/villani_agentd/console_assets/console-assets.json
components/villani-agentd/villani_agentd/console_assets/index.html
components/villani-flight-recorder/dist/providers/villani.js
components/villani-flight-recorder/dist/providers/villaniSchemaValidation.js
components/villani-flight-recorder/dist/render/components/villaniRunDetails.js
components/villani-flight-recorder/src/providers/villani.ts
components/villani-flight-recorder/src/providers/villaniProtocol.ts
components/villani-flight-recorder/src/providers/villaniSchemaValidation.ts
components/villani-flight-recorder/src/render/components/villaniRunDetails.ts
components/villani-flight-recorder/test/villaniProtocol.test.ts
components/villani-flight-recorder/test/villaniProvider.test.ts
components/villani-ops/villani_ops/agentic/git_artifacts.py
components/villani-ops/villani_ops/classification/classifier.py
components/villani-ops/villani_ops/cli/unified.py
components/villani-ops/villani_ops/closed_loop/adapters/git_isolation.py
components/villani-ops/villani_ops/closed_loop/adapters/villani_code_attempt.py
components/villani-ops/villani_ops/closed_loop/adapters/villani_verifier.py
components/villani-ops/villani_ops/closed_loop/controller.py
components/villani-ops/villani_ops/closed_loop/plugins/builtins.py
components/villani-ops/villani_ops/closed_loop/presentation.py
components/villani-ops/villani_ops/closed_loop/protocol.py
components/villani-ops/villani_ops/closed_loop/schema_validation.py
components/villani-ops/villani_ops/isolation/copy_git.py
components/villani-ops/villani_ops/schemas/v1/run-manifest.schema.json
components/villani-ops/villani_ops/tests/closed_loop/test_adapters.py
components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py
components/villani-ops/villani_ops/tests/test_candidate_patch_quality.py
components/villani-ops/villani_ops/tests/test_classification_normalization.py
components/villani-ops/villani_ops/tests/test_closed_loop_plugins.py
components/villani-ops/villani_ops/tests/test_focused_probes.py
components/villani-ops/villani_ops/tests/test_milestone3_presentation.py
components/villani-web/.prettierrc.json
components/villani-web/dist/index.html
components/villani-web/src/ConsoleApp.tsx
components/villani-web/src/consoleApi.ts
components/villani-web/test/console.test.tsx
components/villani/README.md
components/villani/tests/test_install_local.py
components/villani/tests/test_onboarding_integration.py
docs/CLOSED_LOOP.md
docs/DISTRIBUTION.md
integration/fixtures/protocol/v1/valid_run/manifest.json
onboarding-verification/run_onboarding_gate.py
release-verification/README.md
release-verification/connected_product.py
release-verification/fixtures/model_service.py
release-verification/run_release_gate.py
schemas/v1/run-manifest.schema.json
scripts/install-local.py
tests/final_foundation/test_release_gate_contract.py
```

Deleted/replaced generated assets:

```text
components/villani-agentd/villani_agentd/console_assets/assets/index-BbBDMbii.js
components/villani-web/dist/assets/index-BbBDMbii.js
```

## Tests added or extended

- Exact installed-user onboarding contradiction regression, retained with successful selection and
  delivery eligibility after the fix.
- Validation-coverage schema, changed-test linkage, unrelated-requirement conservatism, and legacy
  migration tests.
- Focused-probe scheduling, plugin forwarding, candidate/requirement/evidence linkage, redundant
  suppression, and behavior-versus-infrastructure tests.
- One-fixture reconciliation contract across CLI, web, Markdown reports, static/support artifacts,
  and Flight Recorder.
- Easy, medium, and hard classifier calibration fixtures with raw/effective persistence.
- Interrupted publication, missing dependency, entry-point rollback, dependency-profile separation,
  and exact repair-command installer tests.
- Clean built-artifact installed-user gate, deterministic local model, service lifecycle, sample
  execution, exact counts, non-destructive delivery, doctor, browser screenshots, dead-letter
  assertion, and evidence secret scan.

## Validation results

Final required suites:

| Command | Working directory | Exact result |
|---|---|---|
| `python -m pytest -q` | `components/villani` | exit 0; 66 passed in 184.10s |
| `python -m pytest -q` | `components/villani-ops` | exit 0; 1,139 passed, 2 skipped, 114 deselected in 244.88s |
| `python -m pytest -q` | `components/villani-code` | exit 0; 686 passed, 1 skipped, 28 warnings in 77.78s |
| `python -m pytest -q` | `components/villani-agentd` | exit 0; 78 passed, 1 warning in 16.02s |
| `python -m pytest tests/closed_loop -q` | root | exit 0; 11 passed, 2 warnings in 44.48s |
| `python -m pytest tests/final_foundation -q` | root | exit 0; 37 passed, 1 warning in 8.27s |
| `npm test` | `components/villani-web` | exit 0; 15 tests passed |
| `npm run typecheck` | `components/villani-web` | exit 0; passed |
| `npm run build` | `components/villani-web` | exit 0; passed |
| `npm run format:check` | `components/villani-web` | exit 0; passed |
| `npx playwright test` | `components/villani-web` | exit 0; 14 passed in 4.8s |
| `npm test` | `components/villani-flight-recorder` | exit 0; 111 tests passed |
| `npm run typecheck` | `components/villani-flight-recorder` | exit 0; passed |
| `npm run build` | `components/villani-flight-recorder` | exit 0; passed |
| `npm run format:check` | `components/villani-flight-recorder` | exit 0; passed |
| `python -m ruff check <all changed Python files>` | root | exit 0; all checks passed |
| targeted Ops mypy, 5 PT0 modules | root | exit 0; no issues in 5 source files |
| targeted installer/release mypy, 5 modules | root | exit 0; no issues in 5 source files |
| `git diff --check` | root | exit 0; no whitespace errors |
| `python release-verification/run_release_gate.py --mode release` | root | exit 0; RELEASE GATE PASSED; 161 commands; 415.0s |

Focused evidence:

- Exact post-fix onboarding regression: exit 0; 1 passed in 98.82s.
- Real clean local installer regression: exit 0; 1 passed in 77.85s.
- Deterministic-advisory behavior-failure plus malformed-semantic fail-closed regression: exit 0;
  2 passed in 5.69s.
- Source-tree connected product gate: exit 0; 8/8 scenarios passed, seven completed and one
  intentionally exhausted, zero dead letters, 17 screenshots, 137.6s.

The pre-fix onboarding test was intentionally run first and failed with the required contradiction:
non-empty implementation-and-test patch, passing authoritative `unittest`, semantic pass,
`focused_probe_missing`, displayed 0/0 checks, and hard classification. The same fixture passes
after PT0.

The first full release attempt reached passing product/onboarding/browser and 8/8 connected results
but failed the release-only supply-chain phase because the local development virtualenv lacked the
CI-required `pip-audit` tool. Installing `pip-audit==2.10.1` in that development environment fixed
the prerequisite without changing runtime dependencies. Two subsequent complete release gates
passed; the retained final run is the type-clean source certification.

An additional, non-required `ruff format --check` reported that 31 existing/changed files would be
reformatted. The required ruff lint check passed. No broad formatter rewrite was applied after the
release evidence was established.

## Clean installed-user evidence

Retained evidence:
`release-verification/artifacts/latest/installed-user-onboarding/20260717T043033Z`

- Official release certification: true.
- Built-artifact installation: passed.
- Mandatory imports and executables: passed.
- Effective classification: easy.
- Acceptance-eligible attempts: 1.
- Selected attempt: `attempt_001`.
- Repository checks: 1 passed, 0 failed, 0 not run, 0 unavailable; accounting complete.
- Focused probes: 0 passed, 0 failed, 0 not run, 0 unavailable; accounting complete. The
  authoritative coverage made a duplicate probe unnecessary.
- Requirements: 3 proved, 0 not proved; accounting complete.
- Doctor: passed.
- Delivery: configured non-destructive `suggest`/`branch` behavior passed.
- Service stopped: true.
- Dead letters: 0.
- Evidence secret scan: passed.
- Required vulnerability, repository-secret, package-secret, container, SBOM, and license checks:
  passed.

Installed-user screenshots:

```text
01-setup-flow.png
02-doctor.png
03-villani-console.png
04-sample-run.png
05-sample-replay.png
```

The connected gate also retained 17 screenshots covering the overview, run list, easy result,
escalation, candidate comparison, verification evidence, classification adjustment, withheld
artifact behavior, fail-closed exhaustion, Flight Recorder, replay, events, evidence, file activity,
and two viewport sizes.

## Known failures, skips, assumptions, and risks

Remaining acceptance failures: none.

Skips:

- Villani Ops skipped two host-capability tests for unavailable Unix-style special-file/socket
  behavior on Windows.
- Villani Code skipped the opt-in Claude Code smoke test because `RUN_CLAUDE_CODE_SMOKE` was not
  enabled.

Warnings were limited to the existing Starlette/httpx deprecation, Windows pytest-cache permission
warnings, and component warning baselines; they did not alter evidence or outcomes.

Validation-environment cleanup note: the automatic approval reviewer reached its approval-usage
limit and rejected cleanup of five ignored pytest scratch directories plus the Docker Desktop
shutdown. No product, installation, configuration, run-bundle, or evidence state is affected. The
remaining scratch paths are `.test-temp`, `.test-temp-closed-loop-final`,
`.test-temp-foundation-final`, `components/villani-ops/.test-temp`, and
`components/villani-ops/.test-temp-final`. Docker Desktop can be returned to its prior stopped state
with `& 'C:\Program Files\Docker\Docker\DockerCli.exe' -Shutdown` when approvals are available.
Git also reports metadata-only modifications for
`components/villani-ops/villani_ops/closed_loop/delivery/materializers.py` and
`components/villani-web/test-results/.last-run.json`; each worktree hash equals its index hash and
`git diff` is empty. The read-only `.git` sandbox prevented refreshing those index-stat entries, so
they are correctly excluded from the 72-file content-change inventory.

Security/privacy: credential values are redacted or withheld; repository, package, and onboarding
evidence secret scans passed. Run artifacts may still contain repository paths and task text by
design.

Data loss: installer and configuration activation are staged and recoverable. Delivery is
non-destructive in the gate. Unproved candidates are never automatically materialized.

Compatibility: legacy bundles and configurations remain readable. Conservative migration can show
unknown/not-run for old evidence rather than retroactively asserting proof.

Residual risk: deterministic coverage intentionally prefers false negatives over false positives.
Process-level providers are not kernel sandboxes; configured container/devcontainer providers remain
the stronger option for hostile candidate code.

Assumptions:

- Additive v1 schema contracts remain in the repository's existing normative `schemas/v1` family.
- Changed-test coverage requires a deterministic evidence link; filename or generic suite success
  alone is insufficient.
- Unknown accounting remains `null` plus an explicit status and never becomes numeric zero.

## Scope confirmation

Only PT0 was implemented. PT1 was not started.
