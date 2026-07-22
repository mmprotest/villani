# CLI Agent Mode Milestone 8 baseline report

Captured: 2026-07-22T17:00:47+10:00

Repository HEAD: `80cdab397b8d48ee937a2ae81c00d3359cd178c8`

The worktree already contained the uncommitted Milestones 5-7 implementation. This baseline was
run before any Milestone 8 production-code edit. Existing changes were preserved.

## Baseline status

`AVAILABLE_DETERMINISTIC_SUITES_GREEN_WITH_ENVIRONMENT_LIMITATION`

All available Python, TypeScript, package, setup, service, schema, closed-loop, lint, type, format,
and asset-parity checks passed. The mandatory browser/Playwright leg was not executed because the
in-app browser runtime returned an empty browser inventory. This baseline does not treat that gap as
a pass and does not claim release certification.

## Exact results

| Surface | Command | Result |
|---|---|---|
| Villani Ops | `python -m pytest -q` | 1,535 passed, 4 skipped, 122 deselected |
| Villani Code | `python -m pytest -q` | 686 passed, 1 skipped, 27 warnings |
| Villani Agentd | `python -m pytest -q` | 92 passed |
| Public distribution | `python -m pytest -q` | 79 passed |
| Closed-loop integration | `python -m pytest tests/closed_loop -q` | 11 passed, 2 warnings |
| Run Model | `npm test` | 6 files, 17 tests passed |
| Villani Web | `npm test` | 4 files, 29 tests passed |
| Flight Recorder | `npm test` | 21 files, 118 tests passed |
| Villani UI | `npm test` | 4 tests passed |
| Run Model | `npm run typecheck && npm run build` | passed |
| Villani Web | `npm run typecheck`, `npm run build`, `npm run format:check` | passed; 61-module build |
| Flight Recorder | `npm run typecheck`, `npm run build`, `npm run format:check` | passed |
| Villani UI | `npm run build` | passed |
| Packaged Console | `python scripts/sync-console-assets.py --check` | 3 files verified |
| Scoped Python checks | Ruff plus targeted mypy for Ops, Agentd, distribution | passed: 6 + 2 + 3 typed files |
| Worktree whitespace | `git diff --check` | passed; line-ending notices only |
| Browser/Playwright | in-app browser discovery | unavailable: browser inventory `[]` |

## Failure classification

### Introduced by CLI agent mode

None observed in the baseline.

### Exposed by CLI agent mode

None observed in the baseline.

### Unrelated pre-existing

- Villani Code emitted 27 Typer deprecation warnings concerning `is_flag`/`flag_value`.
- Root closed-loop emitted the existing Starlette/httpx deprecation warning.
- Root pytest emitted the existing repository `.pytest_cache` ACL warning.
- Git emitted existing LF-to-CRLF working-copy notices; `git diff --check` still exited zero.

### Environment limitation

- The in-app browser runtime exposed no browser instance, so mandatory Playwright execution and
  screenshots were unavailable. No standalone or unrelated browser backend was substituted.
- The first four Python invocations used nested `--basetemp .m8b/<component>` paths before the
  `.m8b` parent existed. Pytest failed during fixture setup with `FileNotFoundError`; no product
  conclusion was drawn. After creating only the test-temp parent, the identical suites passed with
  the authoritative counts above.
- Vite/esbuild cannot traverse its installed runtime under the managed filesystem sandbox. The Web
  build/tests and distribution clean-install test were rerun with the required bounded permission
  and passed.

## External-provider status

No real Codex or Claude model call was made. Real-provider certification remains opt-in and must
never be inferred from fake-provider or doctor results.

