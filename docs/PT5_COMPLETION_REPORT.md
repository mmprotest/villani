# Villani Product Transformation Milestone PT5 Completion Report

- Milestone: `PT5 — Create the Complete Agent-System and Harness Contract`
- Status: **COMPLETE**
- Machine-readable report: `docs/PT5_COMPLETION_REPORT.json`
- Harness conformance report: `docs/PT5_HARNESS_CONFORMANCE_REPORT.json`
- PT6 started: **No**

## Outcome

The public controller now resolves a versioned, content-addressed complete agent system through
configuration and a registry. It no longer constructs the Villani Code attempt adapter directly.
Villani Code implements the same harness lifecycle and evidence contract intended for future
harnesses, while remaining the only production-enabled harness in PT5. External systems may be
described and diagnosed, but cannot be selected or qualified for production.

All acceptance criteria passed:

1. The controller is not hardwired to Villani Code.
2. Villani Code runs through the common versioned harness contract.
3. Every new run stores its complete agent-system identity.
4. Verifier and selector inputs are harness-neutral.
5. Old bundles and legacy backend configuration remain readable and migrate safely.
6. No external production harness is enabled.
7. PT6 was not started.

## Exact files

### Added

- `components/villani-ops/villani_ops/closed_loop/agent_systems/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/adapters.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/configuration.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/conformance.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/models.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/registry.py`
- `components/villani-ops/villani_ops/schemas/v1/agent-system.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/harness-conformance-report.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/harness-result.schema.json`
- `components/villani-ops/villani_ops/tests/test_pt5_agent_systems.py`
- `components/villani-run-model/src/agentSystem.ts`
- `components/villani-run-model/test/agentSystem.test.ts`
- `components/villani-run-model/dist/agentSystem.d.ts`
- `components/villani-run-model/dist/agentSystem.js`
- `components/villani-web/dist/assets/index-D0m9i5XQ.css`
- `components/villani-web/dist/assets/index-Db_ajSzJ.js`
- `components/villani-agentd/villani_agentd/console_assets/assets/index-D0m9i5XQ.css`
- `components/villani-agentd/villani_agentd/console_assets/assets/index-Db_ajSzJ.js`
- `docs/AGENT_SYSTEMS.md`
- `docs/PT5_HARNESS_CONFORMANCE_REPORT.json`
- `docs/PT5_COMPLETION_REPORT.md`
- `docs/PT5_COMPLETION_REPORT.json`
- `integration/fixtures/protocol/v1/valid_run/agent-systems/asys_80147fac99d0bfffb4605d4a447ad9a0b6d6e947426c95efcf7168cc6ec94dfa.json`
- `integration/fixtures/protocol/v1/valid_run/agent-systems/asys_d605dea1f6503cf9996864423c705228b426ccee3c2e02869084ac9bbbbda575.json`
- `integration/fixtures/protocol/v1/valid_run/agent-systems/index.json`
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_001/harness-result.json`
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_002/harness-result.json`
- `integration/fixtures/protocol/v1/valid_run/harness-conformance.json`
- `schemas/v1/agent-system.schema.json`
- `schemas/v1/harness-conformance-report.schema.json`
- `schemas/v1/harness-result.schema.json`
- `scripts/generate-harness-schemas.py`

### Modified

- `PLANS.md`
- `README.md`
- `components/villani-ops/README.md`
- `components/villani-ops/villani_ops/agentic/git_artifacts.py`
- `components/villani-ops/villani_ops/cli/unified.py`
- `components/villani-ops/villani_ops/closed_loop/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/model_management.py`
- `components/villani-ops/villani_ops/closed_loop/plugins/builtins.py`
- `components/villani-ops/villani_ops/closed_loop/protocol.py`
- `components/villani-ops/villani_ops/closed_loop/schema_validation.py`
- `components/villani-ops/villani_ops/schemas/v1/attempt.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/run-manifest.schema.json`
- `components/villani-ops/villani_ops/tests/closed_loop/test_adapters.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py`
- `components/villani-ops/villani_ops/tests/test_agentic_git_artifacts.py`
- `components/villani-code/villani_code/context_projection.py`
- `components/villani-code/villani_code/state_tooling.py`
- `components/villani-agentd/README.md`
- `components/villani-agentd/tests/test_console.py`
- `components/villani-agentd/villani_agentd/console.py`
- `components/villani-agentd/villani_agentd/console_assets/console-assets.json`
- `components/villani-agentd/villani_agentd/console_assets/index.html`
- `components/villani-flight-recorder/README.md`
- `components/villani-flight-recorder/src/providers/villani.ts`
- `components/villani-flight-recorder/src/providers/villaniProtocol.ts`
- `components/villani-flight-recorder/src/providers/villaniSchemaValidation.ts`
- `components/villani-flight-recorder/dist/providers/villani.js`
- `components/villani-flight-recorder/dist/providers/villaniSchemaValidation.js`
- `components/villani-flight-recorder/test/villaniProtocol.test.ts`
- `components/villani-flight-recorder/test/villaniProvider.test.ts`
- `components/villani-run-model/src/index.ts`
- `components/villani-run-model/src/types.ts`
- `components/villani-run-model/dist/index.d.ts`
- `components/villani-run-model/dist/index.js`
- `components/villani-run-model/dist/types.d.ts`
- `components/villani-run-model/dist/types.js`
- `components/villani-web/README.md`
- `components/villani-web/src/ProductPages.tsx`
- `components/villani-web/src/consoleApi.ts`
- `components/villani-web/test/console.test.tsx`
- `components/villani-web/dist/index.html`
- `components/villani/README.md`
- `components/villani/tests/test_onboarding.py`
- `components/villani/villani_distribution/onboarding.py`
- `docs/CLOSED_LOOP.md`
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_001/attempt.json`
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_002/attempt.json`
- `integration/fixtures/protocol/v1/valid_run/manifest.json`
- `onboarding-verification/run_onboarding_gate.py`
- `schemas/v1/attempt.schema.json`
- `schemas/v1/run-manifest.schema.json`
- `tests/closed_loop/test_protocol_contract.py`

### Deleted

- `components/villani-web/dist/assets/index-BbBDMbii.js`
- `components/villani-web/dist/assets/index-meSgV0bo.css`
- `components/villani-agentd/villani_agentd/console_assets/assets/index-BbBDMbii.js`
- `components/villani-agentd/villani_agentd/console_assets/assets/index-meSgV0bo.css`

The four deleted files are superseded generated Web and packaged-console asset hashes. No user data
was deleted.

### Migrated

- Legacy `backends` configuration is preserved and projected into
  `villani.agent_system_configuration.v1` during atomic configuration writes.
- Version-1 manifest and attempt contracts gain optional agent-system identity and harness-result
  links; bundles that predate PT5 remain valid and readable.
- The canonical cross-language fixture now contains two complete identities, their index, two
  harness-neutral results, and a conformance report.

## Architecture and product decisions

- A route is represented as a content-addressed complete system identity built from non-secret
  harness, model, provider, serving, protocol, execution, repository/task-profile, and verification
  configuration.
- The public CLI builds an `AgentSystemRegistry`; the controller requests a selected runner plugin
  from that registry. A source regression test prevents public construction of
  `VillaniCodeAttemptAdapter`.
- Villani Code is the only system allowed to be both production-enabled and qualified in PT5.
  External IDs are retained for inspection but forced disabled and unsupported.
- Capability values are `supported`, `unsupported`, or `unknown`, each with declared, detected,
  conformance-tested, or unsupported provenance and evidence. A generic model option does not imply
  custom-model support.
- The contract covers probe, capability description, preparation, execution, normalized streaming,
  cancellation, result and artifact collection, cleanup, and doctor operations across local
  subprocess, ACP stdio, versioned direct protocol, and structured headless CLI transports.
- Bounds, backpressure, stdout/stderr separation, permission decisions, failure classification,
  session identity, file changes, commands, usage, cost, artifacts, timeouts, and cancellation are
  explicit contract data.
- Normalized events retain emission order. Regressing timestamps are clamped and the original value
  is recorded. Unknown namespaced events remain available, and unsafe reasoning is not exposed.
- Each harness result contains isolated worktree identity, baseline digest, patch, changed files,
  stdout, stderr, normalized events, raw trace, usage, cost state, duration state, harness status,
  infrastructure failure, artifacts, and cleanup evidence.
- Semantic verification cannot observe harness, route, cost, or competing candidates. Selection
  receives only generic acceptance-eligible candidates.
- Unknown cost and duration remain `null` with an accounting status. No capability, qualification,
  validation, or success evidence is fabricated.
- Git baseline creation now fails closed on every failed Git operation and creates an explicit empty
  commit for empty repositories, preventing accidental escape to a containing repository.

## Schema and configuration migrations

- `villani.agent_system.v1`: a standalone canonical identity with validated content ID and redaction
  state.
- `villani.harness_result.v1`: a complete, harness-neutral attempt result linked optionally from a
  version-1 attempt.
- `villani.harness_conformance_report.v1`: a machine-readable qualification report whose missing or
  failed checks cannot authorize production.
- `villani.agent_system_configuration.v1`: a deterministic projection of legacy backend entries;
  the original backend data is retained, writes are atomic, and backups are preserved.
- Root schemas remain normative. The packaged Villani Ops copies are generated and byte-identical.
  TypeScript and Python models, fixtures, CLI, UI, Agentd, Flight Recorder, docs, and tests were
  updated with the public contract.

## Tests added and extended

- Thirteen focused PT5 Python cases cover migration, identity stability, redaction, external
  disablement, lifecycle behavior, events, controller persistence, cancellation, bounds, path
  safety, failure classes, missing executables, malformed output, conformance authorization, CLI
  behavior, controller decoupling, and verifier/selector blindness.
- A Git-baseline regression proves failed initialization cannot fall through to an outer repository.
- Two TypeScript run-model cases cover complete identities and harness/conformance records.
- Python, TypeScript, Web, Agentd, Flight Recorder, distribution, onboarding, fixture, and
  cross-language protocol tests were updated to exercise the new public data.

## Validation commands and exact results

| Command | Exact result |
| --- | --- |
| `cd components/villani-code; python -m pytest -q` | `686 passed, 1 skipped, 27 warnings in 146.76s` |
| `cd components/villani-ops; python -m pytest -q` with `TEMP/TMP=C:\tmp`, `PYTEST_ADDOPTS=--basetemp C:\tmp\pt5-ops-final-20260718b -p no:cacheprovider` | `1202 passed, 2 skipped, 114 deselected in 289.26s` |
| `python -m pytest tests/closed_loop -q` with a short basetemp and cache disabled | `11 passed, 1 warning in 44.93s` |
| `cd components/villani-agentd; python -m pytest -q` | `86 passed in 18.78s` |
| `cd components/villani-flight-recorder; npm test` | `21 files, 112 tests passed`; build included |
| `cd components/villani-flight-recorder; npm run typecheck` | Passed |
| `cd components/villani-flight-recorder; npm run build` | Passed |
| `cd components/villani-flight-recorder; npm run format:check` | Passed |
| `cd components/villani-web; npm test` | `4 files, 24 tests passed` |
| `cd components/villani-web; npm run typecheck` | Passed |
| `cd components/villani-web; npm run build` | `57 modules transformed`; production assets built |
| `cd components/villani-web; npm run format:check` | Passed |
| `python scripts/sync-console-assets.py --check` | `Console assets verified: 3 files` |
| `cd components/villani-run-model; ..\villani-web\node_modules\.bin\tsc.cmd --noEmit; ..\villani-web\node_modules\.bin\tsc.cmd; node ..\villani-web\node_modules\vitest\vitest.mjs run` | Typecheck passed; build passed; `3 files, 9 tests passed in 411ms` |
| `cd components/villani; python -m pytest -q` in the normal sandbox with a short basetemp | `65 passed, 1 sandbox-only clean-install failure in 128.79s` |
| `cd C:\tmp\villani-pt5-validation-copy-20260718b\components\villani; python -m pytest -q tests\test_install_local.py::test_install_local_bootstraps_a_real_clean_environment` | `1 passed in 78.01s`; the disposable copy was removed |
| `cd components/villani; python -m pytest -q tests\test_onboarding_integration.py` in the normal sandbox with a short basetemp | `1 passed in 106.59s` |
| `python scripts/generate-harness-schemas.py` followed by SHA-256 comparison | `3 of 3 schema pairs byte-identical` |
| `cd components/villani-ops; python -m pytest -q villani_ops/tests/test_pt5_agent_systems.py villani_ops/tests/closed_loop/test_protocol.py` | `30 passed in 1.46s` |
| `python -m ruff check <PT5 Python production, test, migration, gate, and schema-generator surface>` | `All checks passed` |
| Credential-pattern scan over 21 PT5 identity, evidence, fixture, UI-model, and documentation files | `0 matches` |
| `git diff --check` | Passed |

The distribution result is complete across two compatible execution contexts: 65 tests passed in
the repository sandbox, and the only remaining clean-install case passed unchanged after copying
the working tree outside the OneDrive/esbuild permission boundary. Thus all 66 distribution cases
have passing evidence, but they did not execute in one process.

## Non-authoritative failures encountered and resolved

- The first Villani Code full run had 74 failures from mocked `stdout=None` and Windows decoding.
  UTF-8 replacement decoding and `None`-safe stream handling fixed them; the final full suite passed.
- The first root closed-loop run had three failures from timestamp-regressed legacy runtime events.
  Emission order is now preserved with explicit timestamp adjustment evidence; the final run passed.
- An intermediate Villani Ops run had one empty-repository baseline failure. An explicit
  `git commit --allow-empty` fixed it; the final full suite passed.
- Two elevated onboarding attempts failed with Windows `WinError 5` while reading OneDrive package
  data, although API health was 200. The complete normal-sandbox onboarding gate passed.
- The repository-sandbox clean-install distribution case hit an npm/esbuild filesystem boundary;
  an elevated in-place retry then hit a OneDrive asset rename denial. The identical test and working
  tree passed from `C:\tmp`.
- Standalone run-model npm scripts lacked package-local binaries. The pinned TypeScript and Vitest
  binaries already installed for Villani Web passed typecheck, build, and all nine tests.

## End-to-end artifacts and screenshots

- `integration/fixtures/protocol/v1/valid_run/agent-systems/index.json`: validated complete-system
  index.
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_001/harness-result.json`: validated
  harness-neutral result.
- `integration/fixtures/protocol/v1/valid_run/harness-conformance.json`: validated cross-language
  conformance fixture.
- `docs/PT5_HARNESS_CONFORMANCE_REPORT.json`: validated `passed` report containing 19 checks;
  production qualification is true for Villani Code only.

No screenshot was captured. The required in-app browser workflow reported no attached target, so
repository Playwright was not substituted. UI behavior instead has passing evidence from 24 Web
tests, typecheck, format check, the production build, and packaged-console asset parity.

## Known failures and skipped tests

- Known unresolved PT5 product or acceptance failures: **none**.
- Villani Ops skipped two POSIX-only special-file/socket host-capability tests on Windows.
- Villani Code skipped one opt-in external Claude Code smoke test.
- The distribution suite required the split evidence described above because OneDrive and esbuild
  could not share one compatible filesystem permission context in the repository sandbox.

## Security, privacy, data-loss, and compatibility risks

- Secret removal is bounded and pattern-based. Operators must inspect bundles before sharing.
- Process execution is not a kernel sandbox; hostile repositories require an existing hardened
  execution provider.
- Endpoint credentials are excluded, although non-secret host and port identity is intentionally
  observable.
- Raw traces, patches, task text, and repository source can be sensitive. PT5 remains local-first
  but does not encrypt these artifacts.
- Only a selected, recorded, acceptance-eligible patch may be materialized. Accepted-unverified and
  best-effort candidates remain fail closed.
- Configuration migration is atomic and retains legacy backends; a failed write leaves the previous
  configuration intact.
- Old bundles omit only optional PT5 links and remain readable with unknown values. Executable
  digest, cost, duration, and capability stay unknown when no authoritative evidence exists.
- External harness definitions are intentionally non-operational until a later explicitly
  authorized milestone proves them.

## User-facing behavior before and after

Before PT5, the public controller directly constructed the Villani Code attempt adapter, and the
model/backend inventory described only part of a route.

After PT5, the controller resolves a complete configured agent system through a registry. Users can
run `villani agents list`, `villani agents inspect`, and `villani agents doctor`, and the Agents UI
shows harness, model, provider, environment, qualification, and capability identity. Existing
commands remain valid, legacy configuration migrates safely, and Villani Code remains the sole
production route.

## Milestone boundary

PT5 is complete. **PT6 was not started.**
