# PT1 Completion Report

**Milestone:** Product Transformation PT1 — Unify the Visual System and Information Architecture  
**Date:** 2026-07-17  
**Status:** **COMPLETE**  
**PT2 started:** **No**

## Outcome

Onboarding and the main Villani product now use one shared light monochrome system from
`components/villani-ui`. New task is the root experience, Activity is the unified chronological
stream, Agents presents complete configured systems in user language, and Settings owns setup,
service metadata, diagnostics, delivery defaults, and the Advanced index. The default navigation
contains only New task, Activity, Agents, and Settings.

PT1 changes presentation, information architecture, and browser-local onboarding state only. It
does not change controller state, task classification, backend routing, retry, verification,
selection, acceptance, patch application, or delivery behavior.

The machine-readable companion to this report is
[`PT1_COMPLETION_REPORT.json`](PT1_COMPLETION_REPORT.json).

## Acceptance criteria

| Criterion | Result | Evidence |
| --- | --- | --- |
| Onboarding and the main product share one visual identity | Passed | Both source and packaged experiences consume the `villani-ui` tokens and primitives; the standalone blue/green onboarding palette contract rejects duplication. |
| Default navigation contains only New task, Activity, Agents, and Settings | Passed | Unit and Playwright navigation contracts pass; Team remains hidden. |
| Healthy infrastructure is silent | Passed | Unit and visual tests cover healthy Settings without a strip and actionable service/setup failures with a compact notice. |
| Advanced capabilities remain reachable | Passed | Models, Policies, Replay, Fleet, Tasks, Costs, Alerts, and Audit are linked from Settings and existing deep routes remain supported. |
| The first screen explains the outcome without control-plane architecture | Passed | `/console` renders the outcome-focused New task composer and moves technical assessment into a disclosure. |
| No acceptance, routing, or delivery behavior changes | Passed | The change is limited to presentation and browser-local setup state; Villani Ops and closed-loop suites pass. |
| No later milestone is started | Passed | PT2 and every later milestone remain untouched. |

## Exact PT1 file inventory

This inventory is scoped to PT1 relative to the already-dirty PT0/user working-tree baseline that
existed when this milestone began. Those unrelated baseline changes were preserved and are not
claimed here.

### Added

- `components/villani-web/e2e/pt1-fixtures.ts`
- `components/villani-web/e2e/pt1-visual.spec.ts`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/activity-mixed.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/advanced-navigation.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/keyboard-focus.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/new-task-empty.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/new-task-mobile-320.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/new-task-populated.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/onboarding-complete.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/onboarding-detected-agent.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/onboarding-setup-error.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/onboarding-start.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/service-failure-actionable.png`
- `components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/settings-healthy.png`
- `components/villani-web/src/OnboardingPage.tsx`
- `components/villani-web/src/ProductPages.tsx`
- `components/villani-web/dist/assets/index-D0m9i5XQ.css`
- `components/villani-web/dist/assets/index-DJq66jpi.js`
- `components/villani-agentd/villani_agentd/console_assets/assets/index-D0m9i5XQ.css`
- `components/villani-agentd/villani_agentd/console_assets/assets/index-DJq66jpi.js`
- `docs/PT1_COMPLETION_REPORT.md`
- `docs/PT1_COMPLETION_REPORT.json`

### Changed

- `PLANS.md`
- `components/villani-ui/README.md`
- `components/villani-ui/index.js`
- `components/villani-ui/react.d.ts`
- `components/villani-ui/react.js`
- `components/villani-ui/test/theme.test.js`
- `components/villani-ui/theme-source.js`
- `components/villani-ui/theme.css`
- `components/villani-web/README.md`
- `components/villani-web/e2e/console.spec.ts`
- `components/villani-web/e2e/release-connected.mjs`
- `components/villani-web/playwright.config.ts`
- `components/villani-web/src/App.tsx`
- `components/villani-web/src/ConsoleApp.tsx`
- `components/villani-web/src/ProductShell.tsx`
- `components/villani-web/src/styles.css`
- `components/villani-web/test/console.test.tsx`
- `components/villani-web/test/staticExport.test.ts`
- `components/villani-web/tsconfig.app.json`
- `components/villani-web/dist/index.html`
- `components/villani-agentd/tests/test_console.py`
- `components/villani-agentd/villani_agentd/console_assets/console-assets.json`
- `components/villani-agentd/villani_agentd/console_assets/index.html`
- `components/villani-flight-recorder/src/render/sessionBrowserTheme.ts`
- `components/villani-flight-recorder/src/render/theme.ts`
- `components/villani-flight-recorder/dist/render/theme.js`
- `components/villani-flight-recorder/test/helpers/villaniFixture.ts`
- `components/villani-flight-recorder/test/sessionBrowser.test.ts`
- `onboarding-verification/run_onboarding_gate.py`
- `scripts/sync-console-assets.py`
- `tests/final_foundation/test_frontend_assets.py`
- `tests/final_foundation/test_ui_theme_contract.py`

### Deleted

None by PT1.

### Generated asset migrations

- Web distribution: `index-BwtYjqYI.js` and `index-meSgV0bo.css` →
  `index-DJq66jpi.js` and `index-D0m9i5XQ.css`.
- Packaged Agentd Console: `index-BwtYjqYI.js` and `index-meSgV0bo.css` →
  `index-DJq66jpi.js` and `index-D0m9i5XQ.css`.

The older tracked `index-BbBDMbii.js` deletion visible in the aggregate dirty worktree predates
PT1 and belongs to the preserved PT0 baseline.

## Architecture and product decisions

- `components/villani-ui` is the sole token and shared-primitive source for Villani Web, packaged
  Console, onboarding transcript output, and shared Flight Recorder presentation values.
- The shell exposes exactly four default destinations. Team remains hidden until a later
  enrolment milestone, while Advanced retains Models, Policies, Replay, Fleet, Tasks, Costs,
  Alerts, and Audit.
- `/console` is New task. `/`, `/run`, `/console/run`, and `/console/home` redirect to it.
  `/console/activity` is Activity, and `/history` plus `/console/history` redirect to it. Existing
  run, session, replay, fleet, model, policy, task, cost, alert, and audit deep links remain valid.
- Healthy infrastructure produces no permanent status strip. Setup, service, storage,
  synchronization, migration, credential, and page-recovery conditions alone produce an
  actionable notice.
- Public screens translate controller terminology into user language while technical stored
  artifacts and Evidence disclosures preserve exact internal terms.
- Onboarding is a resumable four-stage experience in the normal shell. Safe choices are
  preselected; endpoint, pricing, capability, and service details stay under Advanced; Ready
  opens New task with the repository selected.
- Activity combines Villani tasks and imported coding sessions without inventing cost or elapsed
  time. Unknown remains unknown.
- Packaged Console synchronization is exact-match idempotent and remains fail closed for manifest
  or digest disagreement.
- Flight Recorder validates its curated public protocol set while run-specific parsing continues
  to read the internal PT0 evidence artifacts it already supports.

## Schema and configuration migrations

There are no durable schema migrations and no product configuration migrations in PT1.

Browser-local presentation state uses `villani.onboarding.v1` with only `stage`, `repository`, and
`backend`. If local storage is unavailable, setup still works but cannot resume across reloads.
No secrets, credentials, cost claims, or qualification evidence are stored there.

Legacy route migration is redirect-only:

| Legacy route | Destination |
| --- | --- |
| `/` | `/console` |
| `/run` | `/console` |
| `/console/run` | `/console` |
| `/console/home` | `/console` |
| `/history` | `/console/activity` |
| `/console/history` | `/console/activity` |

## Tests added or extended

- Shared UI light-token, focus, reduced-motion, computed WCAG 2.2 AA contrast,
  component-export, and no-standalone-onboarding-palette contracts.
- Web route migration, exact default navigation, Advanced reachability, healthy-status silence,
  actionable notices, onboarding resumption and repository handoff, associated form errors,
  Activity empty/mixed states, public language, and human-readable Agents contracts.
- Playwright route, bookmarked deep-link, keyboard-focus, form-accessibility, responsive-overflow,
  and 12-state deterministic visual-regression coverage.
- Agentd source-distribution/package byte parity and idempotent asset synchronization.
- Flight Recorder shared semantic token use and supported-protocol fixture filtering.

## Validation ledger

The ledger records every distinct validation invocation and outcome from the PT1 pass, including
intermediate failures. Identical final re-runs with the same command and result are consolidated;
any changed phase or outcome is listed separately. All paths are relative to the repository root
unless the working directory says otherwise.

| # | Working directory | Exact command | Exit | Exact result |
| ---: | --- | --- | ---: | --- |
| 1 | `components/villani-ui` | `npm run build` | 0 | `theme.css` generated; `index.js` and `react.js` syntax checks passed. |
| 2 | `components/villani-ui` | `npm test` | 1 | Initial contract update: 3 passed, 1 failed because the test still expected the removed standalone dark palette. |
| 3 | `components/villani-ui` | `npm test` | 0 | Final: 4 passed, 0 failed. |
| 4 | `components/villani-web` | `npm test` | 1 | Pre-contract update: 5 legacy navigation/language assertions failed while PT1 routes were being rewritten. |
| 5 | `components/villani-web` | `npm test -- --reporter=json --outputFile C:\tmp\villani-pt1-vitest.json` | 0 | 4 files passed; 20 tests passed. |
| 6 | `components/villani-web` | `npm run typecheck` | 1 | `playwright.config.ts` rejected the unsupported `reducedMotion` option. |
| 7 | `components/villani-web` | `npm run typecheck` | 0 | Final application and Node TypeScript projects passed. |
| 8 | `components/villani-web` | `npm run e2e -- e2e/console.spec.ts --reporter=line` | 1 | Sandbox policy prevented the Vite/esbuild subprocess from reading `vite.config.ts`. |
| 9 | `components/villani-web` | `npm run e2e -- e2e/console.spec.ts --reporter=line` | 0 | Approved local subprocess: 4 passed. |
| 10 | `components/villani-web` | `npm run e2e -- e2e/pt1-visual.spec.ts --update-snapshots --reporter=line` | 1 | First capture: 8 passed, 4 locator failures; eight baselines written. |
| 11 | `components/villani-web` | `npm run e2e -- e2e/pt1-visual.spec.ts --update-snapshots --reporter=line` | 1 | Second capture: 11 passed, 1 imported-session badge locator failure. |
| 12 | `components/villani-web` | `npm run e2e -- e2e/pt1-visual.spec.ts --update-snapshots --reporter=line` | 0 | Baseline generation complete: 12 passed. |
| 13 | `components/villani-web` | `npm run e2e -- --reporter=line` | 0 | Full suite: 26 passed, including routes, deep links, accessibility, responsive layout, and visual regression. |
| 14 | `components/villani-web` | `npm run e2e -- e2e/pt1-visual.spec.ts --reporter=line` | 0 | Final locked baselines: 12 passed. |
| 15 | `components/villani-web` | `npm run format:check` | 1 | Before formatting: 14 modified source/test files required Prettier. |
| 16 | `components/villani-web` | `npm run format:check` | 1 | After documentation update: `README.md` and `vite.config.ts` required Prettier. |
| 17 | `components/villani-web` | `npm run format:check` | 0 | Final: all matched files use Prettier code style. |
| 18 | `components/villani-web` | `npm run build` | 1 | Typecheck/model build passed; sandbox policy denied Vite/esbuild config loading. |
| 19 | `components/villani-web` | `npm run build` | 0 | Approved production build: 53 modules; HTML 0.57 kB, CSS 35.18 kB, JavaScript 300.39 kB. |
| 20 | `components/villani-web` | `npm test` | 1 | Vitest startup was blocked by the same Vite/esbuild sandbox restriction. |
| 21 | `components/villani-web` | `npm test` | 0 | Approved local subprocess: 4 files passed; 20 tests passed. |
| 22 | repository root | `python scripts\sync-console-assets.py --check` | 0 | Console assets verified: 3 files. |
| 23 | repository root | `python -m pytest -q components\villani-agentd\tests\test_console.py::test_packaged_console_references_existing_assets components\villani-agentd\tests\test_console.py::test_packaged_console_matches_source_distribution` | 0 | 2 passed; source distribution and packaged bytes match. |
| 24 | `components/villani-flight-recorder` | `npm test` | 1 | Initial: 110 passed, 1 failed because two PT0 internal evidence artifacts were incorrectly included in the curated public-protocol fixture set. |
| 25 | `components/villani-flight-recorder` | `npx vitest run test/villaniProvider.test.ts -t "copies and parses the immutable canonical fixture concurrently"` | 1 | 1 failed, 16 skipped; reproduced the unsupported internal-schema fixture issue. |
| 26 | `components/villani-flight-recorder` | `npm run format:check` | 1 | Before formatting: 4 modified files required Prettier. |
| 27 | `components/villani-flight-recorder` | `npm test` | 0 | Final: 21 files passed; 111 tests passed. |
| 28 | `components/villani-flight-recorder` | `npm run typecheck` | 0 | `tsc --noEmit` passed. |
| 29 | `components/villani-flight-recorder` | `npm run build` | 0 | TypeScript build passed. |
| 30 | `components/villani-flight-recorder` | `npm run format:check` | 0 | Final: all matched files use Prettier code style. |
| 31 | `components/villani-code` | `python -m pytest -q` | 1 | All 686 runnable test bodies passed and 1 skipped; pytest session cleanup failed on AppData `pytest-current` permission. |
| 32 | `components/villani-code` | `python -m pytest -q --basetemp C:\tmp\villani-pt1-vcode-20260717` | 1 | 212 passed, 1 skipped, 474 setup errors because sandbox policy denied `C:\tmp` creation. |
| 33 | `components/villani-code` | `python -m pytest -q --basetemp .pytest-tmp-pt1` | 1 | 612 passed, 1 skipped, 74 failed because Git-sensitive fixtures inside the source repository changed test semantics. |
| 34 | `components/villani-code` | `python -m pytest -q --basetemp C:\tmp\villani-pt1-vcode-escalated-20260717` | 0 | Final isolated run: 686 passed, 1 skipped, 28 warnings. |
| 35 | `components/villani-ops` | `python -m pytest -q --basetemp C:\tmp\villani-pt1-vops-escalated-20260717` | 0 | 1,139 passed, 2 skipped, 114 deselected, 1 warning. |
| 36 | `components/villani-agentd` | `python -m pytest -q --basetemp C:\tmp\villani-pt1-agentd-escalated-20260717` | 1 | 74 passed, 5 permission-only failures because the elevated context could not read OneDrive-hosted packaged assets. |
| 37 | `components/villani-agentd` | `python -m pytest -q --basetemp .pytest-tmp-pt1-agentd` | 0 | Final workspace-permitted run: 79 passed. |
| 38 | `components/villani` | `python -m pytest -q --basetemp .pytest-tmp-pt1-distribution` | 1 | 65 passed, 1 failed; the clean installer reached npm build and the sandbox denied Vite/esbuild. |
| 39 | `components/villani` | `python -m pytest -q tests\test_install_local.py::test_install_local_bootstraps_a_real_clean_environment --basetemp .pytest-tmp-pt1-distribution-install` | 1 | Elevated retry: frontend build and Python installation passed; OneDrive denied packaged-directory rename. |
| 40 | `components/villani` | `python -m pytest -q tests\test_install_local.py::test_install_local_bootstraps_a_real_clean_environment --basetemp .pytest-tmp-pt1-distribution-install` | 1 | Idempotency retry: elevated-context visibility again prevented exact-match inventory and directory rename. |
| 41 | repository root | `python -m pytest tests\closed_loop -q --basetemp C:\tmp\villani-pt1-closed-loop-escalated-20260717` | 0 | 11 passed, 1 warning. |
| 42 | repository root | `python -m pytest -q tests\final_foundation\test_ui_theme_contract.py` | 0 | 2 passed, 1 pytest-cache warning. |
| 43 | repository root | `python -m pytest tests\final_foundation -q --basetemp C:\tmp\villani-pt1-final-foundation-escalated-20260717` | 0 | Before the idempotency test: 37 passed. |
| 44 | repository root | `python -m pytest -q tests\final_foundation\test_frontend_assets.py --basetemp C:\tmp\villani-pt1-frontend-assets-escalated-20260717` | 0 | 2 passed. |
| 45 | repository root | `python -m pytest tests\final_foundation -q --basetemp C:\tmp\villani-pt1-final-foundation-escalated-20260717` | 0 | Final: 38 passed. |
| 46 | repository root | `python -m ruff check onboarding-verification\run_onboarding_gate.py scripts\sync-console-assets.py components\villani-agentd\tests\test_console.py tests\final_foundation\test_frontend_assets.py` | 0 | All checks passed. |
| 47 | repository root | `git diff --check` | 0 | No whitespace errors; Git emitted only CRLF conversion warnings. |

## End-to-end artifacts and screenshots

Production Web artifacts:

- `components/villani-web/dist/index.html`
- `components/villani-web/dist/assets/index-D0m9i5XQ.css`
- `components/villani-web/dist/assets/index-DJq66jpi.js`

Packaged Console artifacts:

- `components/villani-agentd/villani_agentd/console_assets/index.html`
- `components/villani-agentd/villani_agentd/console_assets/console-assets.json`
- `components/villani-agentd/villani_agentd/console_assets/assets/index-D0m9i5XQ.css`
- `components/villani-agentd/villani_agentd/console_assets/assets/index-DJq66jpi.js`

The deterministic screenshots are in
`components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/`:

- `onboarding-start.png`
- `onboarding-detected-agent.png`
- `onboarding-setup-error.png`
- `onboarding-complete.png`
- `new-task-empty.png`
- `new-task-populated.png`
- `activity-mixed.png`
- `settings-healthy.png`
- `service-failure-actionable.png`
- `advanced-navigation.png`
- `new-task-mobile-320.png`
- `keyboard-focus.png`

All 12 locked comparisons pass. The screenshots were visually inspected for hierarchy, palette,
focus visibility, error treatment, and 320px overflow.

## Known failures, skips, and limitations

### Environment-blocked clean-install test

The distribution suite has one unresolved environment-only failure: 65 tests pass, but the clean
installer cannot complete under either available execution context. In the sandbox, npm cannot
launch Vite/esbuild. When the entire test is elevated, frontend build and Python installation pass,
but that process cannot inventory/rename the OneDrive-hosted packaged Console directory.

This does not leave a PT1 acceptance criterion without evidence: the production build passes, the
three packaged digests verify, Agentd passes 79 tests, source/package byte parity passes 2 tests,
and the remaining 65 distribution/onboarding tests pass. The limitation is reported rather than
masked.

### Skipped and deselected tests

- Villani Code: 1 opt-in external Claude Code smoke test skipped because
  `RUN_CLAUDE_CODE_SMOKE` was not set.
- Villani Ops: 2 host-capability tests skipped on Windows (Unix-domain socket and FIFO).
- Villani Ops: 114 slow/integration/e2e cases deselected by the component's default `addopts`;
  the required root closed-loop e2e suite ran separately and passed 11 tests.
- The in-app browser connector was unavailable. Repository Playwright/Chromium provided the
  deterministic browser and screenshot evidence instead.

## Security, privacy, data-loss, and compatibility risks

- Screenshot fixtures and reports contain no API keys, credentials, real user data, or real run
  content.
- Onboarding stores only stage, selected repository, and backend identifier in local browser
  storage. Storage failure disables resumption only; it does not block setup.
- Unknown cost and duration stay unknown; PT1 does not fabricate accounting, capability,
  validation, or qualification data.
- Asset synchronization remains fail closed on inventory, manifest, or digest disagreement and
  avoids replacement only when the package is already byte-identical.
- Existing run bundles remain readable. Legacy navigation and bookmarked run/session/replay links
  remain reachable through redirects or preserved routes.
- No controller state machine, semantic-verification input, acceptance eligibility, routing,
  retry, selection, recorded patch, or delivery behavior changed.
- Visual baselines are tied to the repository's pinned Playwright Chromium, `en-AU`,
  `Australia/Sydney`, and deterministic fixture data.

## User-facing behavior before and after

| Before | After |
| --- | --- |
| Home and Run were separate primary concepts in broad control-plane navigation. | New task is the root, with only New task, Activity, Agents, and Settings in default navigation. |
| Onboarding used standalone rounded blue/green SaaS styling and separate typography. | Onboarding uses the shared light monochrome shell, tokens, typography, controls, focus, and motion behavior. |
| Healthy service/local state permanently occupied shell space. | Healthy infrastructure is silent; only conditions requiring action create a compact notice. |
| History and connected screens exposed infrastructure-oriented terms. | Activity combines Villani tasks and imported sessions and primary screens use clear outcome-oriented language. |
| Advanced control-plane destinations dominated the shell. | Advanced destinations remain available through Settings and bookmarked deep links. |
| Setup presented service details alongside the main decision. | Each stage presents one decision and one primary action; service, endpoint, pricing, and capability detail is under Advanced. |

## Assumptions

- Existing PT0 and unrelated user changes were the preserved working-tree baseline.
- Existing Agentd run options and model inventory remain the authoritative configured-agent inputs;
  PT1 adds no backend setup contract.
- The repository's pinned Playwright Chromium is the visual-regression reference environment.

## Milestone boundary

PT1 is complete. **PT2 was not started**, and no preparatory PT2 work was performed.
