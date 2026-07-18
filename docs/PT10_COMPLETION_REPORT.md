# PT10 completion report

Status: **INSUFFICIENT_EVIDENCE**  
Implementation status: complete  
Acceptance status: not proven across all three target operating systems  
Canonical version: 1.0.0  
Assessment date: 2026-07-18

PT10's self-service product implementation is complete, and the Windows x86_64 release candidate passed standalone install, no-YAML setup, a real isolated coding task, proof inspection, Doctor, update and rollback, support-bundle privacy inspection, entitlement safety, performance, dependency/secret/malware scans, and screenshot certification.

The milestone is not reported COMPLETE. The committed release matrix includes Windows, macOS, and Linux, but only Windows produced a current certification artifact in this pass. Workflow presence is not evidence that the macOS and Linux artifacts install and recover correctly, so cross-platform acceptance remains `INSUFFICIENT_EVIDENCE`.

The exact path inventory and complete validation ledger are in [PT10_COMPLETION_REPORT.json](PT10_COMPLETION_REPORT.json). PT11 was not started.

## Acceptance assessment

| Criterion | Result | Evidence |
| --- | --- | --- |
| Normal setup requires no YAML | Met on certified Windows | The installed-product onboarding gate passed and wrote safe local configuration without manual YAML editing. |
| Built artifacts work independently | Windows met; macOS/Linux pending | The Windows artifact ran from an external consumer directory with the checkout, source venv, `PYTHONPATH`, `NODE_PATH`, and sibling `node_modules` excluded. |
| Updates roll back safely | Met on certified Windows | Certification covered preflight, size/SHA/archive verification, migration preview, configuration backup, side-by-side install, atomic launcher switch, startup/Doctor verification, and verified rollback. |
| Doctor gives exact repair actions | Met | Every non-pass check identifies the failure, states that repositories were not modified, supplies one exact repair action, and points to durable evidence. |
| Support preserves privacy | Met | The final archive was created locally, inspected against its exact manifest/digests, contained no default run evidence or raw checkout/home paths, and recorded `uploaded=false`. |
| Entitlements do not compromise safety | Met | Free and expired states retain isolation, acceptance-grade verification, evidence readability, accepted-run verification, manual delivery, and Activity. |
| Default product remains New task, Activity, Agents, Settings | Met | Web tests, production build, asset parity, and screenshots verify the four-item navigation. |
| No later milestone is started | Met | PT11 was not started. |
| Windows, macOS, and Linux have current release certification | Insufficient evidence | Windows x86_64 passed. The committed macOS/Linux hosted jobs were not observed in this pass. |

## Delivered product

Villani now has one canonical 1.0.0 identity across Python packages, Node packages, Helm, compatibility metadata, release metadata, CLI output, and Console Settings. The deterministic release builder produces a platform archive with launch commands, release notes, a strict package manifest, checksums, an update feed, and a CycloneDX SBOM inventory. The package smoke installs only built artifacts and deliberately removes access to the source checkout, source virtual environment, sibling `node_modules`, `PYTHONPATH`, and `NODE_PATH`.

Guided setup detects a repository, Villani Code, Codex, Claude Code, authentication, provider/model identity, protocol conformance, and qualification. It does not execute inferred repository commands and does not fabricate readiness. It selects the strongest safe observed route, local storage, isolation, and non-destructive delivery without requiring YAML editing. Advanced files remain validated.

Updates are explicit and never forced. Stable, beta, and pinned policies are supported. Update checks send only the installed Villani version; remote feeds require HTTPS, while local files and loopback are explicit offline/test exceptions. The updater uses an exclusive cross-process lock, validates size/SHA/strict ZIP contents, previews compatibility, backs up configuration, installs side-by-side, atomically switches a stable launcher, verifies startup and install Doctor, journals crash recovery, and keeps a verified rollback target.

Doctor covers installation, versions, service, storage, config, migrations, repository access, Git, harnesses, authentication, protocol, provider/model, permissions, isolation, validation, qualification, stale runs, dead letters, update state, and disk. It is non-mutating and writes durable evidence.

Support export is opt-in, local, previewable, exact-manifested, and never uploaded automatically. Its strict allowlist excludes run evidence by default. It redacts secrets, prompts, source, diffs, repository names, usernames, absolute paths, and terminal content; exact run IDs must be selected to add run evidence.

Centralized Free/Pro decisions live in Villani Ops. Free keeps one configured system, isolation, acceptance-grade verification and evidence, manual delivery, and Activity. Pro gates multi-harness qualification, automatic routing/escalation, adaptive verification, persistent repository learning, pull-request delivery, analytics, and advanced export. Local RS256 verification ships only a public key; the development fixture requires `VILLANI_ALLOW_DEVELOPMENT_LICENSE=1`; invalid replacement fails before current state changes; offline grace is bounded to 90 declared days; and licensing receives no source.

The CLI and Console work from a repository root. Activity can repeat a task, successful runs expose copyable proof and deep evidence links, delivery supports manual/apply/branch and entitled pull request, and later corrections/reverts are explicit imports. No editor extension or payment processing was added.

## Architecture and product decisions

- The canonical version is a release contract, not duplicated mutable state. `scripts/check-version-contract.py` verifies all 30 declarations.
- Only a platform with a passing certification artifact is claimed supported by this report. The CI matrix targets three platforms, but PT10 currently certifies Windows only.
- Update delivery is content-addressed and fail closed. A malformed, oversized, digest-mismatched, traversal-bearing, symlink-bearing, incompatible, or unverifiable artifact never changes the active installation.
- On Windows, immutable content-addressed runners live outside `current`; stable `.cmd` launchers are switched atomically. Direct execution from the mutable `current` tree fails with an exact repair action.
- Entitlements never participate in verifier semantics and cannot reduce acceptance requirements. Semantic verification remains blind to harness identity, route, cost, qualification, and competitors.
- Missing cost, duration, capability, validation, model, conformance, and qualification evidence remains unknown/null; setup and Doctor do not synthesize success.
- Support privacy is enforced by both field-level redaction and archive-level allowlisting/manifest verification. Run evidence is opt-in by exact identifier.
- Cleanup is dry-run by default and protects durable runs, configuration, licenses, current/previous installs, and evidence. Logs are bounded at 5 MiB with three backups.
- Existing controller ownership, deterministic state transitions, isolated attempts, acceptance-grade verifier authority, selection, and selected-patch-only materialization remain unchanged.

## Schemas and migrations

Eight additive normative and packaged contracts were added:

- `villani.doctor.v1`
- `villani.entitlement_state.v1`
- `villani.license.v1`
- `villani.package_manifest.v1`
- `villani.support_bundle_manifest.v1`
- `villani.update_feed.v1`
- `villani.update_policy.v1`
- `villani.update_state.v1`

Strict Python models and matching TypeScript models expose the contracts to CLI, Agentd, Console, and release tooling. All eight root/package schema pairs are byte-identical.

Configuration remains version 1. Existing spool state uses the existing versioned agentd migrator, and run protocol majors 1 and 2 remain readable. Update preview performs compatibility inspection without mutation. Apply writes migration state atomically only after configuration backup. Newer, malformed, or unsupported configuration, spool, or run state fails before the active installation changes. Existing run bundles are not rewritten, and absent PT10 data is shown as unavailable rather than fabricated.

## Exact file inventory

### Added

- `components/villani-agentd/villani_agentd/console_assets/assets/index-D0m9i5XQ.css`
- `components/villani-agentd/villani_agentd/console_assets/assets/index-DbblgCLO.js`
- `components/villani-agentd/villani_agentd/console_assets/console-assets.json`
- `components/villani-agentd/villani_agentd/console_assets/index.html`
- `components/villani-ops/villani_ops/schemas/v1/doctor.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/entitlement-state.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/license.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/package-manifest.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/support-bundle-manifest.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/update-feed.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/update-policy.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/update-state.schema.json`
- `components/villani-ops/villani_ops/self_service/__init__.py`
- `components/villani-ops/villani_ops/self_service/contracts.py`
- `components/villani-ops/villani_ops/self_service/entitlements.py`
- `components/villani-ops/villani_ops/self_service/fixtures/development-pro.json`
- `components/villani-ops/villani_ops/self_service/state.py`
- `components/villani-ops/villani_ops/tests/test_pt10_self_service.py`
- `components/villani-run-model/dist/selfService.d.ts`
- `components/villani-run-model/dist/selfService.js`
- `components/villani-run-model/src/selfService.ts`
- `components/villani-web/dist/assets/index-DbblgCLO.js`
- `components/villani/tests/test_pt10_performance.py`
- `components/villani/tests/test_pt10_self_service.py`
- `components/villani/villani_distribution/maintenance.py`
- `components/villani/villani_distribution/support_bundle.py`
- `components/villani/villani_distribution/update_system.py`
- `docs/PT10_COMPLETION_REPORT.json`
- `docs/PT10_COMPLETION_REPORT.md`
- `docs/SELF_SERVICE.md`
- `integration/fixtures/licenses/development-pro.json`
- `integration/fixtures/licenses/development-tampered.json`
- `release/RELEASE_NOTES.md`
- `release/VERSION`
- `release/performance-targets.json`
- `release/release-metadata.json`
- `schemas/v1/doctor.schema.json`
- `schemas/v1/entitlement-state.schema.json`
- `schemas/v1/license.schema.json`
- `schemas/v1/package-manifest.schema.json`
- `schemas/v1/support-bundle-manifest.schema.json`
- `schemas/v1/update-feed.schema.json`
- `schemas/v1/update-policy.schema.json`
- `schemas/v1/update-state.schema.json`
- `scripts/check-version-contract.py`
- `scripts/generate-pt10-schemas.py`
- `scripts/pt10-performance-gate.py`
- `scripts/scan-release-artifact.py`

### Changed

- `.github/workflows/ci.yml`
- `PLANS.md` (progress section only)
- `README.md`
- `components/villani-agentd/pyproject.toml`
- `components/villani-agentd/tests/test_agentd_core.py`
- `components/villani-agentd/tests/test_console.py`
- `components/villani-agentd/villani_agentd/__init__.py`
- `components/villani-agentd/villani_agentd/cli.py`
- `components/villani-agentd/villani_agentd/config.py`
- `components/villani-agentd/villani_agentd/console.py`
- `components/villani-agentd/villani_agentd/server.py`
- `components/villani-agentd/villani_agentd/structured_log.py`
- `components/villani-agentd/villani_agentd/wrapper.py`
- `components/villani-code/pyproject.toml`
- `components/villani-code/villani_code/__init__.py`
- `components/villani-control-plane/pyproject.toml`
- `components/villani-control-plane/villani_control_plane/__init__.py`
- `components/villani-control-plane/villani_control_plane/main.py`
- `components/villani-flight-recorder/dist/cli.js`
- `components/villani-flight-recorder/dist/providers/villaniSchemaValidation.js`
- `components/villani-flight-recorder/package-lock.json`
- `components/villani-flight-recorder/package.json`
- `components/villani-flight-recorder/src/cli.ts`
- `components/villani-ops/pyproject.toml`
- `components/villani-ops/villani_ops/__init__.py`
- `components/villani-ops/villani_ops/cli/unified.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/adapters.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/configuration.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/discovery.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/policy.py`
- `components/villani-ops/villani_ops/closed_loop/presentation.py`
- `components/villani-ops/villani_ops/evaluation_lab/runner.py`
- `components/villani-ops/villani_ops/tests/test_unified_cli.py`
- `components/villani-run-model/dist/console.d.ts`
- `components/villani-run-model/dist/index.d.ts`
- `components/villani-run-model/dist/index.js`
- `components/villani-run-model/package-lock.json`
- `components/villani-run-model/package.json`
- `components/villani-run-model/src/console.ts`
- `components/villani-run-model/src/index.ts`
- `components/villani-ui/package-lock.json`
- `components/villani-ui/package.json`
- `components/villani-web/dist/index.html`
- `components/villani-web/package-lock.json`
- `components/villani-web/package.json`
- `components/villani-web/src/ConsoleApp.tsx`
- `components/villani-web/src/ProductPages.tsx`
- `components/villani-web/src/SingleTaskPage.tsx`
- `components/villani-web/src/consoleApi.ts`
- `components/villani-web/src/consoleContext.tsx`
- `components/villani-web/test/console.test.tsx`
- `components/villani/README.md`
- `components/villani/pyproject.toml`
- `components/villani/tests/test_onboarding_integration.py`
- `components/villani/villani_distribution/__init__.py`
- `components/villani/villani_distribution/cli.py`
- `components/villani/villani_distribution/diagnostics.py`
- `components/villani/villani_distribution/migrations.py`
- `components/villani/villani_distribution/onboarding.py`
- `deploy/helm/villani-control-plane/Chart.yaml`
- `deploy/helm/villani-control-plane/values.yaml`
- `docs/DISTRIBUTION.md`
- `docs/SUPPLY_CHAIN.md`
- `onboarding-verification/capture_screenshots.mjs`
- `onboarding-verification/run_onboarding_gate.py`
- `release/component-compatibility.json`
- `scripts/build-release.py`
- `scripts/ci-package-smoke.py`
- `scripts/sync-console-assets.py`

### Deleted or migrated

- Deleted/replaced: `components/villani-web/dist/assets/index-DvFaI5_1.js`
- Added the eight v1 contracts without replacing an existing contract.
- Preserved config v1 and run protocol v1/v2 readability; added preview, backup, and atomic migration-state recording.
- Replaced the previous Web production bundle with `index-DbblgCLO.js` and synchronized the exact HTML/CSS/JavaScript bundle into Agentd.

## Tests added and expanded

- `components/villani-ops/villani_ops/tests/test_pt10_self_service.py`: strict contracts, centralized entitlements, development-license gating, offline grace and expiry safety, one-system Free projection, Pro route/delivery checks, and state persistence.
- `components/villani/tests/test_pt10_self_service.py`: update channels/pinning, HTTPS/local feeds, privacy-preserving checks, artifact/traversal rejection, migration preview, backup/switch/rollback/recovery/locking, support privacy and manifest integrity, maintenance safety, and CLI workflows.
- `components/villani/tests/test_pt10_performance.py`: shell/version and startup/Doctor targets plus repository non-mutation.
- Agentd tests: exact version/state APIs, bounded logs, packaged asset integrity, normal-user ACL behavior, and service/Console behavior.
- Web tests: Settings update/entitlement/support/Doctor, Activity repeat, Pro gating, proof copy/deep links, feedback import, and default navigation.
- Onboarding integration: installed runtime identity, real task and proof, all delivery modes, privacy scan, service cleanup, and five distinct screenshots.

## Validation results

The machine report contains every command, working directory, preliminary failure, fix, and final result. Final required and release-facing results are:

| Surface | Exact command | Exact result |
| --- | --- | --- |
| Villani Code | `$env:TEMP=(Resolve-Path ..\..\.test-temp).Path; $env:TMP=$env:TEMP; & ..\..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp ..\..\.test-temp\code-full-pt10` | 686 passed, 1 skipped, 27 warnings in 135.68s |
| Villani Ops | `$env:TEMP='C:\tmp'; $env:TMP='C:\tmp'; & ..\..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp C:\tmp\villani-pt10-ops-full-clean` | 1285 passed, 2 skipped, 116 deselected in 319.78s |
| Agentd | `$env:TEMP='C:\tmp'; $env:TMP='C:\tmp'; & ..\..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp C:\tmp\villani-pt10-agentd-final` | 87 passed in 20.49s |
| Distribution | `$env:TEMP='C:\tmp'; $env:TMP='C:\tmp'; & ..\..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp C:\tmp\villani-pt10-distribution-final` | 77 passed in 204.82s |
| Control Plane | `$env:TEMP='C:\tmp'; $env:TMP='C:\tmp'; & ..\..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp C:\tmp\villani-pt10-control-plane` | 80 passed, 9 skipped, 43 warnings in 13.62s |
| Closed-loop integration | `$env:TEMP='C:\tmp'; $env:TMP='C:\tmp'; & .venv\Scripts\python.exe -m pytest tests/closed_loop -q -p no:cacheprovider --basetemp C:\tmp\villani-pt10-closed-loop` | 11 passed, 1 warning in 48.97s |
| Run Model | `npm test`; `npm run typecheck`; `npm run build` | 6 files/17 tests passed; typecheck/build passed |
| UI | `npm test`; `npm run build` | 4 tests passed; build passed |
| Flight Recorder | `npm test`; `npm run typecheck`; `npm run build`; `npm run format:check` | 21 files/118 tests passed; typecheck/build/format passed |
| Web | `npm test`; `npm run typecheck`; `npm run build`; `npm run format:check` | 4 files/25 tests passed; typecheck/build/format passed; 61 modules built |
| Version | `& .venv\Scripts\python.exe scripts/check-version-contract.py` | Version 1.0.0; 30 declarations verified |
| Schemas | `& .venv\Scripts\python.exe scripts/generate-pt10-schemas.py --check` | 8 schema pairs verified |
| Console assets | `& .venv\Scripts\python.exe scripts/sync-console-assets.py --check` | 3 manifest files verified |
| Source secrets | `& .venv\Scripts\python.exe scripts/check-secrets.py integration\fixtures docs release` | 3 roots; 0 findings |
| Production Node dependencies | `npm audit --omit=dev` in Flight Recorder, Web, Run Model, and UI | 0 vulnerabilities in each project |
| Final Windows certification | `& .venv\Scripts\python.exe scripts/ci-package-smoke.py --work-root .release-smoke\pt10-final --release-root .release-smoke\pt10-final\release` | Passed in 201.7s; no known Python dependency vulnerabilities, 0 artifact secret findings, Defender exit 0/no threats |
| Final onboarding/screenshots | `$env:VILLANI_ONBOARDING_ALLOW_EXTERNAL_ARTIFACTS='1'; & .venv\Scripts\python.exe onboarding-verification/run_onboarding_gate.py --artifacts .release-smoke\pt10-onboarding-screenshots-final --python .release-smoke\pt10-current-3\venv\Scripts\python.exe` | ONBOARDING GATE PASSED in 119.7s; 1 accepted attempt, 1 repository check, 3 requirements proved, all delivery modes, 1080 files/0 secret findings, service stopped, 5 distinct screenshots |

Scoped Ruff passed, Python compilation passed, distribution mypy passed in 11 source files, and Agentd mypy passed in 25 source files. Broader existing baselines remain: Villani Code mypy has 132 pre-existing errors in 10 unrelated files, Ops mypy has 446 in 94 files, Control Plane mypy has 17 in 8 files, and an overly broad Ruff traversal including benchmark fixtures found 36 pre-existing findings. These are not treated as PT10 regressions; the exact changed-file checks pass.

Final reporting checks also passed: `node --check onboarding-verification/capture_screenshots.mjs` exited 0, the machine JSON parsed with its required PT10 status fields, its added/changed/deleted inventory matched all 119 paths in `git status`, and `git diff --check` exited 0 with only informational LF-to-CRLF warnings. The first inventory command attempted to suppress the user's inaccessible global ignore with `core.excludesFile=NUL`; this Git build rejected `NUL`, so the audit was rerun without that override and passed. No repository state changed during either audit.

## End-to-end artifacts and screenshots

The authoritative release evidence is under `.release-smoke/pt10-final/release`:

- `villani-1.0.0-windows-x86_64.zip` — 238,721,711 bytes, SHA256 `e3276fd3b3526b10945f4a981eca69b696c8a385494813e3daa064fe946b2365`
- `pt10-platform-certification.json` — Windows x86_64 certification `passed=true`
- `release-artifact-scan.json` — 8 manifested files, 318,593,997 expanded bytes, 287 SBOM components, 0 secret findings, Microsoft Defender exit 0/no threats
- `dependency-audit.json` — no known vulnerabilities
- `performance-report.json` — shell/version max 2075.297 ms within 5000 ms; Doctor max 2272.314 ms within 10000 ms; repositories not modified
- `update-feed.json` and `SHA256SUMS`

The authoritative onboarding evidence is `.release-smoke/pt10-onboarding-screenshots-final/onboarding-report.json`. It records `ONBOARDING GATE PASSED`, final state `COMPLETED`, selected `attempt_001`, one acceptance-eligible candidate, one passing repository check, three proved requirements, all delivery modes including entitled pull request, 1,080 scanned files with zero secret findings, and a stopped service.

Five screenshots were captured and visually inspected:

- `01-setup-flow.png` — `26593C6C6B053E6E2266823B78EA605468F8037FB7469EF43E186D62EC2E335F`
- `02-doctor.png` — `7F41F7BE956165B858924DA0F4184D8BA6F005898B16E6BEB7C587C2C379A3E1`
- `03-villani-console.png` — `FFA97A9D55F1D33DD97BCA7C5CF05031D516F50CD0B1A5C0764B924572F2FBB4`
- `04-sample-run.png` — `CB0DDE4BEB674354D97363D1724A0D3057298501CC4281E27E3949A97A518CAC`
- `05-sample-replay.png` — `44643D4B84A7F9241D84EF267DA8FA6E3B9231C88F97D7DE8BF8529A5C87956F`

The first screenshot pass exposed identical run/replay captures despite a mechanically passing gate. The selector and gate were corrected; the final hashes are distinct, the run screenshot is a concise summary, and the replay screenshot contains full replay evidence.

## Preliminary failures, skips, and remaining risks

- The first full Ops run was `2 failed, 1283 passed, 2 skipped, 116 deselected in 350.22s`. One legacy pull-request test did not install the newly required explicit Pro fixture and was corrected. One unchanged process-heartbeat test hit OneDrive atomic-replace interference; it passed in `C:\tmp` in 1.09s, and the authoritative full Ops suite passed there.
- The first normal-user Agentd run was `6 failed, 81 passed in 21.68s`. Temporary staging preserved a private Windows ACL across rename. Staging now uses an inheriting sibling directory; the six focused tests and full 87-test suite pass.
- Package-smoke attempt one failed after 173.6s on a Windows launcher path containing spaces. Attempt two failed after 199.1s because the seeded pip 25.0.1 had six advisories. The stable `.cmd` invocation and isolated pip upgrade/audit binding were fixed. Attempt three passed, and the final privacy-hardened attempt passed in 201.7s.
- Real authenticated Codex and Claude Code qualification was not exercised. The end-to-end task used Villani Code, while fake external-conformance coverage verifies the integration contract. Setup and Doctor report external readiness as observed, including warnings, without fabricating qualification.
- Two Ops tests skip where Windows lacks the required host facility. Opt-in paid/external harness tests were not enabled.
- The archive has SHA256 and strict content-manifest verification but no publisher signature or signed update-feed envelope. This is a supply-chain hardening risk.
- Support and evidence archives are local and redacted but not encrypted at rest. Explicitly selected run evidence still requires human preview before sharing.
- Worktree/process isolation is not a kernel sandbox. Hostile repositories require a stronger configured execution provider.
- Only Windows x86_64 is certified by current evidence. Atomic update/rollback and standalone behavior on macOS/Linux remain unproved until hosted jobs pass.
- Unknown provider cost remains null with accounting status. No capability, model, conformance, validation, qualification, or success evidence is fabricated.

## User-visible behavior

Before PT10, installation depended on a checkout and component knowledge, setup could require configuration editing, and there was no unified safe update/rollback, comprehensive Doctor, privacy-reviewed support export, centralized entitlement surface, or complete daily workflow in Settings and Activity.

After PT10, a developer can install a standalone artifact, run guided setup without YAML, detect and assess coding systems, complete an isolated accepted task, inspect/copy/deep-link proof, repeat from Activity, deliver manually or to a branch and entitled pull request, see exact version and entitlement/update state, explicitly update or roll back, run exact-action Doctor diagnostics, and preview/create a local redacted support archive. Offline installation is documented.

## Scope boundary

PT11 was not started.
