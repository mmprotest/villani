# CLI Agent Mode Milestone 8 certification

Captured: 2026-07-22T18:23:46+10:00  
Repository HEAD: `80cdab397b8d48ee937a2ae81c00d3359cd178c8`

## Final status

**`FAIL — NOT FULLY RELEASE-CERTIFIED IN THIS ENVIRONMENT`**

All available deterministic CLI Agent Mode evidence passed, including all 30 fake-executable
scenarios, API regression, mixed/same-provider profiles, repeated cancellation, isolation,
verifier blindness, selector eligibility, failure projection, security scanning, cleanup, and the
full affected Python/TypeScript suites. The dedicated CLI Agent Mode gate therefore reports
`PARTIAL`: required deterministic evidence is complete, while optional real-provider smoke was not
consented to.

The overall release status is nevertheless `FAIL`, not `PASS` or `PARTIAL`, because the mandatory
in-app-browser inventory was empty (`[]`). Playwright and the complete packaged release gate could
not be run through the required browser channel. `PARTIAL` is reserved for the case where all
mandatory deterministic release evidence exists and only optional real providers are unavailable;
that condition is not met for the overall release here.

## Durable evidence

- [Machine-readable role/provider matrix](release-verification/cli-agent-mode-m8-evidence/cli-agent-mode-conformance-matrix.json)
- [CLI Agent Mode gate report](release-verification/cli-agent-mode-m8-evidence/cli-agent-mode-release-report.json)
- [Release evidence digest index](release-verification/cli-agent-mode-m8-evidence/release-evidence-index.json)
- [Gate command/JUnit report](release-verification/cli-agent-mode-m8-evidence/exact-test-command-report.json)
- [Measured resource bounds](release-verification/cli-agent-mode-m8-evidence/resource-bounds.json)
- [Security scan](release-verification/cli-agent-mode-m8-evidence/secret-scan.json)
- [All milestone validation commands](docs/CLI_AGENT_MODE_M8_TEST_COMMANDS.json)
- [Pre-edit baseline and failure classification](docs/CLI_AGENT_MODE_M8_BASELINE_REPORT.md)

The evidence index covers 22 artifacts plus the index itself. The retained bundle is 157,876 bytes.
Ephemeral repositories and phase worktrees are not retained in the release bundle; their measured
maximum size and cleanup proof are retained instead.

The broader `.m8b` pytest scratch tree was moved intact and recoverably out of the workspace to
`C:\tmp\villani-m8-test-temp-20260722`. No governed release evidence was removed.

## Release conformance matrix

The matrix contains the required 13 rows and 14 columns. Status meanings are embedded in the JSON.
It deliberately separates fake deterministic conformance from observed local readiness.

| System/role family | Deterministic contract | Local readiness | Real smoke | Production enabled |
| --- | --- | --- | --- | --- |
| API classification/coding/verification/selection | PASS | API auth not exercised | N/A | PASS (existing path) |
| Codex classification/coding/verification/selection | PASS | executable absent | NOT_RUN | FAIL |
| Claude classification/coding/verification/selection | PASS | executable/auth/version observed; capability Doctor incomplete | NOT_RUN | FAIL |
| Deterministic selection | PASS | N/A | N/A | PASS |

A launch, version string, or fake pass alone never sets `production_enabled` for a CLI system.
Codex and Claude remain disabled in this machine-readable certification until their real smoke
evidence passes.

## Fake executable end-to-end certification

The production argument builders and stream parsers were exercised through CLI-shaped fake Codex
and Claude executables. All 30 numbered scenarios passed. The manifest maps every scenario to live
pytest node IDs and rejects missing or renumbered coverage.

Covered outcomes include semantic pass/reject, no patch, one/two eligible candidates, classifier
fallback, coder/verifier/selector timeout, auth/rate-limit/permission/version failures, malformed
and missing output, partial patch on crash, coding/verification cancellation, restart recovery,
dirty/drifted targets, non-ASCII and spaced paths, bounded large streams, outside-write and child
escape attempts, known/unknown accounting, raw unknown events, parallel candidates, and five
sequential non-resuming role processes.

Official gate result: **30/30 scenarios; 138 tests; 0 failures; 0 errors; 0 skipped.**

## Isolation and delivery certification

Machine tests prove:

- every coding candidate has a distinct worktree, process, session, and invocation identity;
- classification, each coding candidate, verification, and selection never resume another role;
- each verification gets a separate role workspace and process, including same-provider pairs;
- verifier and selector writable roots exclude target and candidate repositories;
- classifier cannot write the target;
- attempted verifier edits and outside-worktree writes are blocked and leave the target unchanged;
- child processes are terminated on timeout/cancellation and do not survive the role process;
- symlink/path traversal and forbidden Villani-state paths fail closed;
- dirty target state, target drift, branch drift, and stale recorded patches invalidate delivery;
- only the selected recorded patch identity reaches materialization;
- cancellation preserves a safely captured partial patch and cleans or quarantines worktrees;
- cleanup validates its exact hashed governed root, handles read-only Git objects, and fails the
  release phase if cleanup remains incomplete.

The final gate used a durable evidence path containing spaces while phase execution used short
hashed governed paths. This fixes the Windows long-path spawn defect exposed during certification
without weakening the explicit spaced/non-ASCII repository tests.

## Verifier blindness certification

Static contract checks and a dynamic canary prove that verifier input/workspace/prompt excludes:

- provider, model, and CLI driver;
- candidate order/rank, cost, token count, and runtime duration;
- competing candidates and selector output;
- coder transcript and coder session identity.

The canary values were written elsewhere in the run bundle and injected into rejected metadata;
none appeared in any verifier input artifact or prompt. The verifier still receives only the
verbatim task/criteria, clean original representation, one candidate patch, changed-file manifest,
authoritative validation evidence, and contract-permitted debug facts.

## Selector eligibility certification

Rejected and infrastructure-failed candidates are excluded before packet construction. Opaque IDs
are unique and privately mapped. Unknown/missing/duplicate IDs fail normalization. One eligible
candidate skips semantic selection. A selector cannot change verification or acceptance state, and
malformed/timeout output uses the explicit existing deterministic fallback rather than first-item
selection.

## Failure presentation and fail-closed behavior

Every persisted CLI infrastructure failure now has a strict projection containing:

- stage and role;
- agent-system ID;
- safe error summary;
- target repository modified: yes/no;
- partial patch preserved: yes/no;
- automatic fallback performed: yes/no;
- one exact repair action;
- evidence path.

The public result schema and Console show this separately from semantic rejection, with full facts
under Evidence. Schema/process/timeout/cancellation/artifact failures cannot yield acceptance.

## Performance and resource bounds

The final gate measured and enforced:

| Bound | Value / observation |
| --- | --- |
| process and Doctor probe | 8 seconds |
| default configurable role timeout | 180 seconds |
| controller wall-time default | none |
| event line | 1 MiB |
| stdout / stderr | 16 MiB each |
| in-memory tail | 16 KiB per stream |
| default / maximum candidate concurrency | 1 / 32 |
| graceful shutdown default | 3 seconds |
| baseline total / file | 500 MiB / 50 MiB |
| cancellation repetitions | 3 |
| maximum cancellation test duration | 6.703 seconds (30-second gate bound) |
| maximum ephemeral phase | 5,355 files / 9,934,611 bytes |
| maximum cleanup latency | 0.875 seconds |
| all ephemeral phase roots removed | yes |

Logs retain at most 2,000,000 characters per phase; runtime streams/events have their own stricter
byte bounds. No arbitrary small coding wall-time budget was added.

## Security and privacy checks

The artifact scan inspected 19 pre-index release artifacts and found zero occurrences of API-key
assignments, OpenAI/GitHub/bearer tokens, private keys, credential-file paths, or raw/escaped user
home paths. Invocation environment records contain names and redacted evidence, never secret values.
Static checks require argv-based `shell=False` invocation, bounded logs/events, explicit real-smoke
consent, and disposable repositories. Dynamic tests cover secret redaction, verifier canaries,
prompt boundaries, outside writes, path traversal, symlinks, and process escape.

This is an internal conformance/security check, **not an external security audit**.

## Real-provider smoke status and version limits

No external model call was made.

- Codex: executable not found on PATH. Exact action: install the provider-owned `codex` CLI, then
  rerun detection/Doctor and the explicitly consented smoke.
- Claude Code: `2.1.138` detected; provider-owned auth probe passed; required capability/Doctor
  readiness did not complete because the bounded `claude doctor` probe timed out at 8 seconds.
  Production remains disabled.
- Supported policy: Codex read-only roles require scoped permission profiles and version `>=0.138.0`
  plus all probed flags. Claude Code requires `>=2.1.138,<2.2.0` plus every role capability.

The opt-in command is:

```text
python release-verification/run_cli_agent_smoke.py --consent
```

It requires explicit consent (or the exact documented consent environment value), states that
provider usage may be consumed, uses disposable repositories, preserves evidence, and skips an
unavailable provider with an exact reason. Detection-only mode makes no model call.

## Validation summary

| Surface | Final result |
| --- | --- |
| Villani Ops | 1,542 passed; 4 skipped; 122 deselected |
| Villani Code | 686 passed; 1 skipped; 27 existing warnings |
| Agentd/service | 92 passed |
| Distribution/setup/Doctor | 79 passed |
| Closed-loop integration | 11 passed; 2 existing warnings |
| Focused CLI role integration | 219 passed; 6 deselected |
| Milestone 8 hardening | 8 passed |
| Run Model | 6 files / 17 tests; typecheck/build passed |
| Villani Web | 4 files / 29 tests; typecheck/build/format passed |
| Flight Recorder | 21 files / 118 tests; typecheck/build/format passed |
| Villani UI | 4 tests; build passed |
| Packaged Console | 3 assets verified |
| Ruff / scoped mypy / Python compile | passed |
| `git diff --check` | passed; line-ending notices only |
| In-app browser / Playwright | NOT_RUN — browser inventory `[]` |
| Full packaged release gate | NOT_RUN — mandatory browser channel unavailable |

The unscoped repository mypy graph still has 283 unrelated pre-existing errors across 54 files;
the changed-file check with imports skipped passed. Existing Typer, Starlette/httpx, pytest-cache,
and LF/CRLF warnings were not hidden.

## Files and architectural changes

- Added the 30-scenario manifest, 13-row matrix template, deterministic gate, explicit real smoke,
  named packaged-gate phase, durable gate evidence, baseline report, and exact command report.
- Added strict CLI infrastructure-failure evidence and public projection in Ops, root/package product
  schemas, Run Model types, Console presentation/tests, and rebuilt packaged Console assets.
- Added Milestone 8 isolation/blindness/eligibility/process/security tests and removed hardcoded model
  defaults from real coding smoke fixtures.
- Added public API/CLI/hybrid, authentication/accounting, isolation, troubleshooting,
  supported-version, and opt-in smoke documentation.

The deterministic controller, role ports, acceptance semantics, selector eligibility, and exact
recorded-patch materialization remain authoritative. No new role or provider was added.

## Explicit exclusions

Milestone 8 did **not** implement subscription quota management, usage-reset tracking, billing
controls, automatic account switching, team features, new providers, new roles, routing
optimization, benchmark-specific logic, or any later product expansion. Authentication remains
provider-owned, and CLI mode is not represented as free inference or quota bypass.
