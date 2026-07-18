# Villani Product Transformation PT2 Completion Report

Date: 17 July 2026  
Milestone: PT2 — Build the Magical Single-Task Loop  
Status: **COMPLETE**  
Gate A: **PASS** — see `docs/PT2_GATE_A.json`  
Next milestone: **PT3 was not started**

## Outcome

Villani now has one default task loop in both browser and CLI. A configured user selects a
repository, writes a multiline task, optionally opens Details, and chooses **Run safely**. The run
then presents exactly four event-derived stages — Understanding, Working, Checking, and Ready —
and finishes with exactly one verdict: Ready to apply, Needs review, Could not prove, or Cancelled.

`villani.product_run.v1` is the shared public projection. The web does not reproduce acceptance or
delivery eligibility logic. Unknown cost and duration remain unknown, retries use plain language,
refresh reconnects to the durable run, cancellation preserves evidence, and only a selected change
with acceptance-grade proof can expose a delivery action.

## Acceptance results

| Criterion | Result | Evidence |
| --- | --- | --- |
| Repository + task + Run safely starts a configured run | PASS | New task unit coverage and clean-success Playwright scenario |
| Strongest eligible is the pre-qualification default | PASS | Performance preset/request assertions and full Villani Ops suite |
| Verification is mandatory; no time budget is invented | PASS | Shared CLI, Agentd, and web request defaults |
| Running has four truthful stages | PASS | Product projection tests and four stage screenshots |
| Result has one of four verdicts | PASS | Normative schema, Python/TypeScript models, and verdict E2E cases |
| CLI and web consume the same projection | PASS | `test_cli_json_equals_the_product_projection_consumed_by_web` |
| No unproved change can be delivered | PASS | Product contract, delivery tests, missing-evidence and drift E2E cases |
| Gate A is machine readable and passing | PASS | `docs/PT2_GATE_A.json` |
| PT3 was not started | PASS | No qualification, learned routing, team, SaaS, or later-milestone work added |

No acceptance criterion failed.

## User-facing behavior

Before PT2, New task still exposed the earlier general run controls, while browser and CLI each
interpreted internal controller state. There was no single durable product contract for progress,
result ordering, cancellation, reconnection, and fail-closed delivery.

After PT2:

- New task has no more than repository, the large task field, Details, and the primary action in its
  default surface. Unsent text is saved locally and restored without changing multiline content.
- Details contains optional success criteria, issue/reference text, attachments, and all retained
  advanced controls.
- High-confidence repository and validation discovery proceeds automatically. Dirty repositories
  and unavailable validation get a truthful safe-action explanation; uncertain detection asks one
  plain-language question.
- Performance, strongest eligible, required verification, no default wall-time, and deferred
  delivery are shared CLI/browser defaults.
- Canonical events persist and drive Understanding, Working, Checking, and Ready. No percentage or
  frontend timer invents progress.
- Results render verdict/reason, target modification state, what changed, files, checks,
  requirements, cost, elapsed time, one action, then Evidence.
- Refresh reconnects without duplicate submission. Closing the browser does not cancel server work.
- Cancel requests runner cancellation, prevents future controller work, preserves evidence, cleans
  isolation, and ends in a durable Cancelled state with target modification truth.

## Architecture and product decisions

- `villani.product_run.v1` is the public presentation boundary used by CLI and web. Acceptance and
  available-action decisions remain in the controller-side Python projection.
- A Ready to apply verdict requires selection plus acceptance-grade verification evidence. Missing,
  malformed, accepted-unverified, unselected, stale, or drifted evidence fails closed.
- Retry and escalation sentences are projection data derived from canonical events. They do not
  reveal harness identity, route cost, or competing candidates to semantic verification.
- Repository and validation discovery is cancellable and cached by repository fingerprint.
  Submission is idempotent; status uses event subscription with a bounded fallback rather than
  high-frequency polling.
- Cancellation is a durable controller state. An atomic delivery already in progress is not
  interrupted midway; the completed delivery outcome and target state remain authoritative.
- Cost and duration use value plus accounting status. A missing source never becomes numeric zero,
  and combined check counts remain unknown if any required accounting source is unknown.
- Existing explicit CLI flags, task-file input, success criteria, JSON, and advanced controls remain
  available. Default human output is the four stages and one verdict; Evidence retains technical
  artifacts.

## Schema and configuration migrations

- Added normative and packaged `villani.product_run.v1` schemas, Python models, TypeScript models,
  a canonical fixture, and persisted `product-run.json` artifacts.
- Added the optional `product_run` run-manifest artifact reference. Old manifests remain valid.
- Added `CANCELLED` to the durable run-state enum and Flight Recorder TypeScript protocol type.
- Existing bundles without a product artifact are projected conservatively from their canonical
  evidence. They do not gain retrospective proof.
- No durable configuration migration was required. Performance, verification, absent wall-time,
  and deferred delivery are request defaults; explicit existing settings still override eligible
  advanced behavior.

## Exact PT2 file inventory

### Added

```text
schemas/v1/product-run.schema.json
components/villani-ops/villani_ops/schemas/v1/product-run.schema.json
components/villani-ops/villani_ops/closed_loop/product_run.py
components/villani-ops/villani_ops/tests/closed_loop/test_product_run.py
components/villani-run-model/src/product.ts
components/villani-run-model/dist/product.js
components/villani-run-model/dist/product.d.ts
components/villani-web/src/SingleTaskPage.tsx
components/villani-web/e2e/pt2-fixtures.ts
components/villani-web/e2e/pt2-loop.spec.ts
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/browser-refresh-reconnected.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/delivery-apply-selected-patch.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/delivery-create-branch.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/dirty-target-safe-action.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/stage-checking.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/stage-ready-verdict-ready-to-apply.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/stage-understanding.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/stage-working.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/unavailable-agent.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/unknown-cost.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/validation-failure-retrying.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/verdict-cancelled.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/verdict-could-not-prove-ordered.png
components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts/verdict-needs-review-target-drift.png
components/villani-web/dist/assets/index-rDw1plUb.js
components/villani-agentd/villani_agentd/console_assets/assets/index-rDw1plUb.js
integration/fixtures/protocol/v1/valid_run/product-run.json
docs/PT2_GATE_A.json
docs/PT2_COMPLETION_REPORT.json
docs/PT2_COMPLETION_REPORT.md
```

### Changed

```text
PLANS.md
docs/CLOSED_LOOP.md
schemas/v1/run-manifest.schema.json
schemas/v1/run-state.schema.json
components/villani-ops/README.md
components/villani-ops/villani_ops/cli/unified.py
components/villani-ops/villani_ops/closed_loop/controller.py
components/villani-ops/villani_ops/closed_loop/interfaces.py
components/villani-ops/villani_ops/closed_loop/policy_presets.py
components/villani-ops/villani_ops/closed_loop/presentation.py
components/villani-ops/villani_ops/closed_loop/protocol.py
components/villani-ops/villani_ops/closed_loop/schema_validation.py
components/villani-ops/villani_ops/closed_loop/state_machine.py
components/villani-ops/villani_ops/schemas/v1/run-manifest.schema.json
components/villani-ops/villani_ops/schemas/v1/run-state.schema.json
components/villani-ops/villani_ops/tests/closed_loop/test_controller.py
components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py
components/villani-ops/villani_ops/tests/test_milestone3_presentation.py
components/villani-ops/villani_ops/tests/test_resume_cli.py
components/villani-ops/villani_ops/tests/test_unified_cli.py
components/villani-agentd/README.md
components/villani-agentd/tests/test_console.py
components/villani-agentd/villani_agentd/console.py
components/villani-agentd/villani_agentd/server.py
components/villani-agentd/villani_agentd/console_assets/console-assets.json
components/villani-agentd/villani_agentd/console_assets/index.html
components/villani-run-model/src/index.ts
components/villani-run-model/dist/index.js
components/villani-run-model/dist/index.d.ts
components/villani-ui/react.js
components/villani-web/README.md
components/villani-web/src/ConsoleApp.tsx
components/villani-web/src/consoleApi.ts
components/villani-web/test/console.test.tsx
components/villani-web/e2e/pt1-fixtures.ts
components/villani-web/e2e/pt1-visual.spec.ts
components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/keyboard-focus.png
components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/new-task-empty.png
components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/new-task-mobile-320.png
components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/new-task-populated.png
components/villani-web/e2e/__screenshots__/pt1-visual.spec.ts/service-failure-actionable.png
components/villani-web/dist/index.html
components/villani-web/test-results/.last-run.json
components/villani-flight-recorder/src/providers/villaniProtocol.ts
integration/fixtures/protocol/v1/valid_run/manifest.json
tests/closed_loop/test_cli_e2e.py
```

### Deleted or migrated

```text
components/villani-web/dist/assets/index-DJq66jpi.js
  -> components/villani-web/dist/assets/index-rDw1plUb.js
components/villani-agentd/villani_agentd/console_assets/assets/index-DJq66jpi.js
  -> components/villani-agentd/villani_agentd/console_assets/assets/index-rDw1plUb.js
```

The shared PT1 CSS asset `index-D0m9i5XQ.css` was retained unchanged.

## Tests added or extended

- Product projection: five tests for legacy derivation, missing selection proof, canonical retry
  language, forbidden unproved delivery actions, and combined repository/probe accounting.
- Controller/protocol: durable cancellation, event persistence, manifest path, and state enum.
- CLI: shared defaults, optional success criteria, task-file and advanced compatibility, four-stage
  output, JSON equality with web, Evidence inspection, and cancellation.
- Agentd: repository-fingerprint cache, cancellable discovery, uncertain/unavailable validation,
  alternative evidence, exact multiline/idempotent submission, background failure recovery, event
  subscription, HTTP cancellation, evidence preservation, and target modification state.
- Web unit: draft restore, control budget, safe defaults, one-click submission, reconnect, result
  ordering, delivery gating, unknown accounting, and cancellation.
- Browser E2E: clean success, selected apply, branch, validation retry success, missing evidence,
  cancellation, refresh, dirty target, unknown cost, unavailable agent, and target drift/conflict.
- Projection equality: CLI `--json` is compared directly with the projection consumed by web.

## Validation commands and exact results

Commands below are shown from their stated working directory. Intermediate red/green checks and
environment failures are included; none is hidden as a final pass.

### Product projection and CLI development checks

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\closed_loop\test_product_run.py villani_ops\tests\closed_loop\test_protocol.py villani_ops\tests\test_milestone3_presentation.py villani_ops\tests\test_unified_cli.py --basetemp .pt2-test-product-04
```

Result: **FAIL** — 118 passed, 1 failed. The failure exposed an explicit failure reason lost during
public projection; it was fixed.

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\closed_loop\test_product_run.py villani_ops\tests\closed_loop\test_protocol.py villani_ops\tests\test_milestone3_presentation.py villani_ops\tests\test_unified_cli.py --basetemp .pt2-test-product-05
```

Result: **PASS** — 119 passed.

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\closed_loop\test_product_run.py villani_ops\tests\test_run_summary.py villani_ops\tests\test_unified_cli.py --basetemp .pt2-test-final-focused-02
```

Result: command **FAIL** before collection — `test_run_summary.py` is under `tests/closed_loop`; no
tests ran.

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\closed_loop\test_product_run.py villani_ops\tests\closed_loop\test_run_summary.py villani_ops\tests\test_unified_cli.py --basetemp .pt2-test-final-focused-03
```

Result: **PASS** — 50 passed in 2.89 seconds.

### Villani Web and Playwright

```powershell
# cwd: components/villani-web
npm test -- test/console.test.tsx --reporter=dot
```

Sandbox result: environment **FAIL** before test collection because Vite/esbuild child-process
execution was denied. The same approved command passed 19 tests. The final full command was:

```powershell
# cwd: components/villani-web, approved process execution
npm test
```

Result: **PASS** — 4 files, 24 tests passed.

```powershell
# cwd: components/villani-web
npm run typecheck
```

Result: **PASS**.

```powershell
# cwd: components/villani-web
npm run build
```

Sandbox result: environment **FAIL** after type/model checks because Vite config child-process
execution was denied. Approved-process result: **PASS** — 55 modules; `index.html` 0.57 kB, CSS
35.18 kB, JavaScript 295.73 kB.

```powershell
# cwd: components/villani-web
npm run e2e -- e2e/pt1-visual.spec.ts e2e/pt2-loop.spec.ts --update-snapshots
npm run e2e -- e2e/pt1-visual.spec.ts e2e/pt2-loop.spec.ts
```

Results: **PASS** — 23 passed with baselines updated, then 23 passed without update.

```powershell
# cwd: components/villani-web
npm run e2e -- e2e/pt2-loop.spec.ts --update-snapshots --grep "missing evidence"
```

Result: **PASS** — 1 passed. This revealed the stale screenshot filename; the final ordered baseline
replaced it.

```powershell
# cwd: components/villani-web
npm run e2e -- e2e/pt2-loop.spec.ts --update-snapshots
npm run e2e -- e2e/pt2-loop.spec.ts
npm run e2e
```

Results: **PASS** — 11 passed with final PT2 baselines, 11 passed locked, and the full suite passed
37 tests in 11.1 seconds.

```powershell
# cwd: components/villani-web
npm run format:check
```

Initial result: **FAIL** because a generated `.pt2-vitest.json` scratch file and README needed
formatting. The scratch file was removed and README formatted. Final result: **PASS** — all matched
files use Prettier style.

### Shared UI, run model, and Flight Recorder

```powershell
# cwd: components/villani-ui
npm run build
npm test
```

Results: **PASS** — build passed; 4 tests passed.

```powershell
# cwd: components/villani-run-model
npm run build
```

Result: environment **FAIL** — this package checkout has no local `tsc` executable.

```powershell
# cwd: components/villani-run-model
& ..\villani-web\node_modules\.bin\tsc.cmd -p tsconfig.json
& ..\villani-web\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.json
& ..\villani-web\node_modules\.bin\vitest.cmd run
```

Results: **PASS** — emit and no-emit compilation succeeded; 1 file, 5 tests passed using the pinned
repository binaries.

```powershell
# cwd: components/villani-flight-recorder
npm test
npm run typecheck
npm run build
npm run format:check
```

Results: **PASS** — build completed; 21 test files and 111 tests passed; typecheck and build passed.
The first format check identified `src/providers/villaniProtocol.ts`; after formatting that file,
the final check passed.

### Python component and integration gates

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp C:\tmp\villani-pt2-vops-final-20260717
```

Sandbox result: environment **FAIL** during setup because pytest base-temp access was denied.
Approved-process first result: **FAIL** — 1,161 passed, 2 skipped, 114 deselected, 2 failed, 2
warnings in 243.42 seconds. Both failures were legacy raw-output expectations and were updated to
the PT2 projection. Final approved-process result: **PASS** — 1,164 passed, 2 skipped, 114
deselected, one pytest-cache warning in 242.07 seconds.

```powershell
# cwd: components/villani-agentd
& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp .pt2-test-agentd-posttype-02
```

Result: **PASS** — 86 passed in 17.50 seconds.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m pytest tests\closed_loop -q --basetemp C:\tmp\villani-pt2-closed-loop-final-20260717
```

Result: **FAIL** — 8 passed, 3 failed because legacy E2E scenarios assumed default apply and
weak-first routing. Those scenarios now request their legacy intent explicitly.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m pytest -q tests\closed_loop\test_cli_e2e.py --basetemp C:\tmp\villani-pt2-closed-loop-targeted-20260717
& .venv\Scripts\python.exe -m pytest tests\closed_loop -q --basetemp C:\tmp\villani-pt2-closed-loop-final2-20260717
```

Results: **PASS** — targeted 3 passed with one Starlette warning in 33.81 seconds; final closed-loop
suite 11 passed with one Starlette warning in 42.34 seconds.

```powershell
# cwd: components/villani-code
& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp C:\tmp\villani-pt2-vcode-final-20260717
```

Result: **PASS** — 686 passed, 1 skipped, 28 warnings in 72.98 seconds.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m pytest tests\final_foundation -q --basetemp C:\tmp\villani-pt2-final-foundation-20260717
```

Result: **PASS** — 38 passed in 6.89 seconds.

### Packaged console parity

```powershell
# cwd: repository root
& .venv\Scripts\python.exe scripts\sync-console-assets.py
& .venv\Scripts\python.exe scripts\sync-console-assets.py --check
& .venv\Scripts\python.exe -m pytest -q tests\final_foundation\test_frontend_assets.py --basetemp components\villani-agentd\.pt2-parity-final
```

Results: **PASS** — synchronized 3 files, verified 3 files, and 2 parity tests passed in 0.55
seconds. A final standalone `--check` again reported `Console assets verified: 3 files`.

### Static analysis and artifact integrity

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m mypy --python-version 3.11 --ignore-missing-imports --no-implicit-optional components\villani-ops\villani_ops\closed_loop\product_run.py components\villani-ops\villani_ops\closed_loop\interfaces.py components\villani-ops\villani_ops\closed_loop\protocol.py components\villani-ops\villani_ops\closed_loop\state_machine.py components\villani-ops\villani_ops\closed_loop\policy_presets.py components\villani-ops\villani_ops\cli\unified.py components\villani-agentd\villani_agentd\console.py components\villani-agentd\villani_agentd\server.py
```

Diagnostic result: **FAIL** — following the existing package graph found 202 errors in 39 files,
mostly existing transitive debt. Direct PT2 errors in product projection and Agentd were fixed.

This intermediate focused command named the obsolete `state.py` and failed before checking:

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m mypy --python-version 3.11 --ignore-missing-imports --no-implicit-optional --follow-imports=skip components\villani-ops\villani_ops\closed_loop\product_run.py components\villani-ops\villani_ops\closed_loop\interfaces.py components\villani-ops\villani_ops\closed_loop\protocol.py components\villani-ops\villani_ops\closed_loop\state.py components\villani-ops\villani_ops\closed_loop\policy_presets.py
```

Result: command **FAIL** — `state.py` does not exist. The live module is `state_machine.py`. The
corrected focused commands were:

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m mypy --python-version 3.11 --ignore-missing-imports --no-implicit-optional --follow-imports=skip components\villani-ops\villani_ops\closed_loop\product_run.py components\villani-ops\villani_ops\closed_loop\interfaces.py components\villani-ops\villani_ops\closed_loop\protocol.py components\villani-ops\villani_ops\closed_loop\state_machine.py components\villani-ops\villani_ops\closed_loop\policy_presets.py
& .venv\Scripts\python.exe -m mypy --python-version 3.11 --ignore-missing-imports --no-implicit-optional --follow-imports=skip components\villani-agentd\villani_agentd\console.py components\villani-agentd\villani_agentd\server.py
& .venv\Scripts\python.exe -m mypy --python-version 3.11 --ignore-missing-imports --no-implicit-optional --follow-imports=skip components\villani-ops\villani_ops\cli\unified.py components\villani-ops\villani_ops\closed_loop\controller.py components\villani-ops\villani_ops\closed_loop\presentation.py
```

Results: **PASS** — no issues in 5, 2, and 3 source files respectively.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m mypy --python-version 3.11 --ignore-missing-imports --no-implicit-optional --follow-imports=skip components\villani-ops\villani_ops\closed_loop\product_run.py components\villani-ops\villani_ops\closed_loop\controller.py components\villani-ops\villani_ops\closed_loop\interfaces.py components\villani-ops\villani_ops\closed_loop\policy_presets.py components\villani-ops\villani_ops\closed_loop\presentation.py components\villani-ops\villani_ops\closed_loop\protocol.py components\villani-ops\villani_ops\closed_loop\schema_validation.py components\villani-ops\villani_ops\closed_loop\state_machine.py components\villani-ops\villani_ops\cli\unified.py components\villani-agentd\villani_agentd\console.py components\villani-agentd\villani_agentd\server.py
```

Result: **PASS** — no issues in all 11 PT2 production files.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m ruff check components\villani-ops\villani_ops\closed_loop\product_run.py components\villani-ops\villani_ops\closed_loop\controller.py components\villani-ops\villani_ops\closed_loop\interfaces.py components\villani-ops\villani_ops\closed_loop\policy_presets.py components\villani-ops\villani_ops\closed_loop\presentation.py components\villani-ops\villani_ops\closed_loop\protocol.py components\villani-ops\villani_ops\closed_loop\schema_validation.py components\villani-ops\villani_ops\closed_loop\state_machine.py components\villani-ops\villani_ops\cli\unified.py components\villani-agentd\villani_agentd\console.py components\villani-agentd\villani_agentd\server.py components\villani-ops\villani_ops\tests\closed_loop\test_product_run.py components\villani-ops\villani_ops\tests\closed_loop\test_protocol.py components\villani-ops\villani_ops\tests\closed_loop\test_controller.py components\villani-ops\villani_ops\tests\test_milestone3_presentation.py components\villani-ops\villani_ops\tests\test_resume_cli.py components\villani-ops\villani_ops\tests\test_unified_cli.py components\villani-agentd\tests\test_console.py tests\closed_loop\test_cli_e2e.py
```

Result: **PASS** — all checks passed. An earlier Ruff pass found two unused caught exception names in
Agentd; both were removed before the final command.

```powershell
# cwd: repository root
git diff --check
```

Result: **PASS** — no whitespace errors. Git emitted only existing LF-to-CRLF working-tree notices.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -c "import json, pathlib; paths=[pathlib.Path('schemas/v1/product-run.schema.json'),pathlib.Path('components/villani-ops/villani_ops/schemas/v1/product-run.schema.json'),pathlib.Path('integration/fixtures/protocol/v1/valid_run/product-run.json')]; [json.loads(p.read_text(encoding='utf-8')) for p in paths]; print(f'parsed {len(paths)} PT2 JSON artifacts')"
```

Result: **PASS** — parsed 3 PT2 JSON artifacts.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m json.tool docs\PT2_GATE_A.json
& .venv\Scripts\python.exe -m json.tool docs\PT2_COMPLETION_REPORT.json
```

Result: **PASS** — both machine-readable reports parsed as JSON.

```powershell
# cwd: repository root
$r = Get-Content docs\PT2_COMPLETION_REPORT.json -Raw | ConvertFrom-Json; $g = Get-Content docs\PT2_GATE_A.json -Raw | ConvertFrom-Json; if ($r.status -ne 'COMPLETE' -or $r.pt3_started -ne $false) { throw 'Completion status mismatch' }; if ($g.status -ne 'PASS' -or $g.pt3_started -ne $false -or $g.blocking_failures.Count -ne 0 -or $g.acceptance_failures.Count -ne 0) { throw 'Gate status mismatch' }; $missing = @(); foreach ($kind in @('added','changed')) { foreach ($path in $r.files.$kind) { if (-not (Test-Path -LiteralPath $path)) { $missing += $path } } }; $present = @(); foreach ($path in $r.files.deleted) { if (Test-Path -LiteralPath $path) { $present += $path } }; if ($missing.Count -ne 0) { throw ('Missing report paths: ' + ($missing -join ', ')) }; if ($present.Count -ne 0) { throw ('Deleted report paths still present: ' + ($present -join ', ')) }; 'completion report consistent: {0} added, {1} changed, {2} deleted; Gate A {3}' -f $r.files.added.Count,$r.files.changed.Count,$r.files.deleted.Count,$g.status
```

Result: **PASS** — `completion report consistent: 30 added, 46 changed, 2 deleted; Gate A
PASS`. Two earlier Python one-line versions failed because PowerShell stripped nested quoting; both
JSON parsers had already passed, and the shell-native assertion above is the authoritative check.

## End-to-end artifacts and screenshots

The PT2 suite produced 14 deterministic PNGs under
`components/villani-web/e2e/__screenshots__/pt2-loop.spec.ts`. They cover all four stages, all four
verdicts, apply, branch, retry, refresh, dirty target, unknown cost, unavailable agent, and target
drift. Five PT1 New task/focus/service baselines were intentionally refreshed for the new form.

Every PT2 screenshot was inspected individually. Original-resolution review confirmed result
section order, verdict dominance, target-state placement, and a 320px New task layout without page
overflow. The in-app Browser connector reported no attached browser (`agent.browsers.list()` was
empty), so live connector inspection was skipped; the repository Playwright/Chromium suite remained
fully exercised and passed 37 tests.

## Known failures, skips, and limitations

- No final test, Gate A, or acceptance failure remains.
- Villani Ops skipped 2 host-capability tests and deselected 114 opt-in/configuration tests.
- Villani Code skipped the single opt-in external Claude Code smoke test.
- The sandbox denied Vite/Vitest child-process execution; the exact approved commands passed.
- Standalone run-model `npm run build` cannot find a local `tsc`; the pinned repository compiler and
  test runner passed emit, no-emit, and all 5 tests.
- Broad followed-import mypy retains existing package debt. The complete 11-file PT2 production
  surface is clean under the focused gate, and all component suites pass.
- Existing unrelated PT0/PT1/user working-tree changes were preserved.

## Security, privacy, data-loss, and compatibility risks

- Delivery is fail closed for unproved, stale, unselected, drifted, or accepted-unverified work.
- Cancellation is cooperative before delivery. Interrupting an atomic target write midway would be
  less safe, so an already-started delivery completes and reports its truthful outcome.
- Browser-local drafts can contain user task/reference text. They never leave the browser until
  submission, but clearing site data removes resumability.
- Idempotency prevents duplicate runs after a repeated click or refresh. Closing the browser has no
  cancellation side effect.
- Repository discovery reads local metadata and caches by fingerprint; repository changes invalidate
  the cache.
- Cost, duration, validation, capability, and proof are never synthesized. Missing data remains null
  with an explicit accounting or recovery state.
- Existing run bundles stay readable through optional manifest fields and conservative projection.
- No secret, API key, harness identity, competing candidate, route, or cost input was added to
  semantic verification evidence.

## Milestone boundary

PT2 is complete. PT3 was not started.
