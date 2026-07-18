# PT6 completion report: production Codex and Claude Code integrations

Status: **INSUFFICIENT_EVIDENCE**  
Implementation: **complete**  
Acceptance: **not proven**  
Assessment date: **2026-07-18**  
Gate C claimed: **no**  
PT7 started: **no**

PT6 now has production-shaped Codex app-server and Claude Code stream-json adapters, a reusable
ACP client, shared isolated execution/evidence, exact discovery identity, fail-closed version and
permission handling, cancellation, authoritative cost handling, CLI/UI readiness, public schemas,
and cross-platform fake-executable conformance.

The milestone cannot truthfully be marked `COMPLETE` on this host. Codex 0.144.5 is installed but
not authenticated, Claude Code 2.1.138 is authenticated but cannot provide the required strict
sandbox on native Windows, and the repository contains no qualifying frozen real founder suite.
No paid real-harness task was authorized or executed. The exact qualification boundary is recorded
in [PT6_QUALIFICATION_EVIDENCE.json](PT6_QUALIFICATION_EVIDENCE.json).

## Outcome against acceptance criteria

| Criterion | Result | Evidence |
| --- | --- | --- |
| Codex and Claude Code complete isolated tasks through real structured integrations | **INSUFFICIENT_EVIDENCE** | Real tests were correctly gated; credentials/isolation/suite evidence are missing. |
| Exact identities and comparable evidence are recorded | Pass in conformance | Shared result/identity schemas, discovery fixture, fake Codex and Claude executions. |
| Cancellation and cleanup work | Pass in conformance | Protocol cancellation plus bounded process-tree cleanup tests for both harnesses and ACP. |
| Target repository cannot be mutated directly | Pass in conformance | Target canary remains unchanged; candidate patch exists only in the attempt worktree. |
| Unsupported versions fail actionably | Pass | Narrow supported ranges and unsupported/schema-change tests. |
| No arbitrary model-harness claim | Pass | Custom model use requires exact configuration conformance; custom providers are unsupported. |
| Gate C is not claimed | Pass | External harnesses remain provisional. |
| PT7 was not started | Pass | No PT7 surface was implemented. |

## Architecture and product decisions

- Codex uses the stable app-server JSON-RPC JSONL stdio lifecycle documented by OpenAI, not the
  experimental WebSocket transport. Every attempt regenerates the installed CLI schema, validates
  client/server messages, initializes with Villani identity, starts an ephemeral worktree-rooted
  thread, sends the verbatim task and criteria, declines permission expansion, interrupts the turn,
  and applies bounded process-tree termination. See the
  [official Codex app-server documentation](https://developers.openai.com/codex/app-server/).
- Claude Code uses the supported non-interactive `stream-json` surface with strict MCP/config
  isolation, an explicit bounded built-in tool list, no unsandboxed fallback, controlled-file input
  for large UTF-8 prompts, redacted output, and default-disabled session persistence. Resume is
  allowed only for the same attempt ID when explicitly enabled. See the
  [official Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference),
  [permissions documentation](https://code.claude.com/docs/en/permissions), and
  [sandboxing documentation](https://code.claude.com/docs/en/sandboxing).
- Both integrations reuse the existing Villani isolated-worktree lifecycle. The recorded patch is
  collected from Git, never from final model prose. The target repository is not the process cwd.
- Coding failure and harness infrastructure failure are distinct. Transport overload/rate-limit
  failures are retryable infrastructure evidence, not task rejection.
- Vendor-reported Claude total and per-model cost is authoritative when present; unavailable cost
  remains `null` with unknown accounting. No price, duration, capability, or success estimate is
  fabricated.
- The ACP v1 client implements newline-delimited JSON-RPC stdio, initialization/capabilities,
  sessions, updates, bounded messages/backpressure, worktree-scoped file and terminal requests,
  permission denial, cancellation, supervision, and redacted raw preservation. Connectivity alone
  does not enable ACP or establish model/trace parity. See the
  [ACP transport contract](https://agentclientprotocol.com/protocol/v1/transports).
- Codex and Claude Code remain `provisional`; conformance execution alone does not qualify them or
  claim Gate C.

## Schema and configuration migration

- Added normative and packaged `villani.harness_discovery.v1` JSON Schemas.
- Additively extended `villani.agent_system.v1` and `villani.harness_result.v1` with optional
  readiness, exact execution identity, per-model usage/cost, provisional/experimental qualification,
  and structured infrastructure-failure fields.
- Updated matching Python, TypeScript, Flight Recorder, fixtures, CLI, Agents UI, and schema
  generation/validation surfaces.
- Existing v1 run bundles and legacy configuration remain readable. No destructive migration is
  required. The identity digest changes only when new model-conformance configuration is present.
- The Web production bundle was rebuilt; its old content-hashed asset was replaced by the new hash.

## User-facing behavior

Before PT6, Villani Code was the only operational harness and Agents could not present actionable
Codex/Claude structured-protocol readiness.

After PT6, `villani agents list` and the Agents page detect Villani Code, Codex, and Claude Code and
show installed state, exact version, authentication readiness, protocol, provider/model capability,
conformance, qualification state, and one repair action. Compatible configured external harnesses
use the common candidate schema and isolated lifecycle. Unsupported, unauthenticated, or
insufficiently isolated configurations fail closed.

Observed local discovery:

| Harness | Version | Auth | Protocol | Qualification/readiness |
| --- | --- | --- | --- | --- |
| Villani Code | 0.1.0rc1 | not applicable | `villani.harness_adapter.v1` | bootstrap, ready |
| Codex | 0.144.5 | not ready | `codex-app-server-jsonrpc-stdio` | provisional; run `codex login` |
| Claude Code | 2.1.138 | ready | `claude-code-stream-json` | provisional; use WSL2/container isolation |

Supported ranges are deliberately narrow: Codex `>=0.144.0,<0.145.0`; Claude Code
`>=2.1.138,<2.2.0`. A later version fails closed unless its exact protocol/configuration passes
conformance.

## Tests added

`components/villani-ops/villani_ops/tests/test_pt6_structured_harnesses.py` collects 31 tests:
29 deterministic parser/protocol/fake-executable tests and 2 opt-in real-harness smoke tests.

Coverage includes successful patch, no patch, command recovery, permission request, cancellation,
rate limit/retry, malformed output, unsupported version, installed schema change, missing final
result, partial patch on crash, known/unknown cost, non-ASCII and spaced paths, large output, large
controlled input, isolation canary, secret redaction, exact discovery, same-attempt-only Claude
resume, official-format Codex/Claude fixtures, and ACP lifecycle/path safety.

## Validation commands and exact results

| Command | Result |
| --- | --- |
| `& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\test_pt6_structured_harnesses.py villani_ops\tests\test_pt5_agent_systems.py villani_ops\tests\closed_loop\test_protocol.py -m 'not e2e' --basetemp ..\..\.pt6-test-temp\focused-post-types` from `components/villani-ops` | 59 passed, 2 deselected in 9.03s |
| `& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp ..\..\.pt6-test-temp\ops-final` from `components/villani-ops` | 1,231 passed, 2 skipped, 116 deselected in 307.13s |
| `& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp ..\..\.pt6-test-temp\code-full` from `components/villani-code` | 686 passed, 1 skipped, 27 warnings in 124.88s |
| `& .venv\Scripts\python.exe -m pytest tests\closed_loop -q --basetemp .pt6-test-temp\root-closed-loop` from the repository root | 11 passed, 2 warnings in 60.25s |
| `npm test` (Flight Recorder) | 21 files, 112 tests passed in 8.24s |
| `npm run typecheck` (Flight Recorder) | passed |
| `npm run build` (Flight Recorder) | passed |
| `npm run format:check` (Flight Recorder) | passed after formatting the three PT6-edited files |
| `npm test -- --run` (Run Model) | 3 files, 9 tests passed in 728ms |
| `npm run typecheck` and `npm run build` (Run Model) | both passed |
| `npm test` (Web) | 4 files, 24 tests passed in 2.45s |
| `npm run build` (Web) | typecheck passed; 57 modules built |
| `npm run format:check` (Web) | passed after formatting the two PT6-edited files |
| `& ..\..\.venv\Scripts\python.exe -m mypy --follow-imports=skip villani_ops\runners\structured_protocol.py villani_ops\runners\codex_app_server.py villani_ops\runners\claude_code.py villani_ops\closed_loop\agent_systems\acp.py villani_ops\closed_loop\agent_systems\discovery.py villani_ops\closed_loop\agent_systems\adapters.py villani_ops\closed_loop\agent_systems\configuration.py villani_ops\closed_loop\agent_systems\registry.py` | success; no issues found in 8 files |
| `& ..\..\.venv\Scripts\python.exe -m ruff check villani_ops\runners villani_ops\closed_loop\agent_systems villani_ops\closed_loop\adapters\villani_code_attempt.py villani_ops\closed_loop\costs.py villani_ops\closed_loop\schema_validation.py villani_ops\cli\unified.py villani_ops\tests\test_pt6_structured_harnesses.py` | all checks passed |
| `& .venv\Scripts\python.exe -m ruff check scripts\generate-harness-schemas.py` | all checks passed |
| `& 'C:\Users\Simon\AppData\Local\Programs\Python\Python311\python.exe' -m compileall -q components\villani-ops\villani_ops\runners\structured_protocol.py components\villani-ops\villani_ops\runners\codex_app_server.py components\villani-ops\villani_ops\runners\claude_code.py components\villani-ops\villani_ops\closed_loop\agent_systems\acp.py components\villani-ops\villani_ops\closed_loop\agent_systems\discovery.py components\villani-ops\villani_ops\closed_loop\agent_systems\adapters.py components\villani-ops\villani_ops\closed_loop\agent_systems\configuration.py components\villani-ops\villani_ops\closed_loop\agent_systems\registry.py` | passed under Python 3.11 |
| PowerShell `Get-FileHash -Algorithm SHA256` comparison for `agent-system`, `harness-result`, and `harness-discovery` normative/packaged schemas | 3 of 3 pairs byte-identical |
| `& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\test_pt6_structured_harnesses.py -m e2e -rs --basetemp ..\..\.pt6-test-temp\real-gate` | 2 skipped, 29 deselected in 0.63s; `VILLANI_REAL_HARNESS_TESTS` was not set to `1` |
| `$env:VILLANI_HOME=(Resolve-Path .pt6-test-temp\cli-home).Path; & .venv\Scripts\villani.exe agents list --json` | detected exact versions/readiness for all three harnesses |
| `git diff --check` | passed; line-ending warnings only |

Development-time validation failures were corrected and rerun:

- Villani Code initially reached 100% but pytest failed while cleaning an inaccessible default
  Windows temp symlink. The workspace-local `--basetemp` rerun passed 686 tests.
- Web Vite test/build initially hit the managed filesystem sandbox. Approved outside-sandbox reruns
  passed. The first Web typecheck also exposed stale Run Model declarations; rebuilding that package
  fixed the public package boundary.
- Initial Flight/Web format checks identified only edited files; Prettier was applied to those exact
  files and both final checks passed.
- Focused mypy initially found nullable/platform typing issues. Guards and annotations were fixed;
  the final run reports zero issues.
- The Python launcher did not resolve `py -3.11` despite listing it; the exact discovered Python 3.11
  executable compiled the production modules successfully.

The complete machine-readable command/result list is in
[PT6_COMPLETION_REPORT.json](PT6_COMPLETION_REPORT.json).

## End-to-end and qualification artifacts

- [PT6_QUALIFICATION_EVIDENCE.json](PT6_QUALIFICATION_EVIDENCE.json)
- `components/villani-ops/villani_ops/tests/fixtures/pt6/codex-events.jsonl`
- `components/villani-ops/villani_ops/tests/fixtures/pt6/claude-events.jsonl`
- `integration/fixtures/protocol/v1/valid_run/harness-discovery.json`
- `schemas/v1/harness-discovery.schema.json`

No screenshot was captured because the in-app browser had no attached target. The browser skill's
supported surface reported zero available tabs, and no unrelated browser mechanism was substituted.
Web unit tests, typecheck, format check, and the production build provide the available UI evidence.

## Known failures, skips, assumptions, and risks

- Real Codex smoke: not run because authentication is not ready and paid execution was not
  authorized.
- Real Claude Code smoke: not run because the opt-in gate was disabled and strict sandboxing is
  unavailable on native Windows.
- Frozen founder qualification: not run for any arm because there are zero qualifying frozen real
  founder suites/tasks. Synthetic fixtures count as zero. Gate C is not claimed.
- The two full Ops skips are exact host capabilities: this Windows Python does not support
  Unix-domain sockets or FIFO creation. Villani Code retains its existing opt-in external smoke skip.
- The fake executable and subprocess code are cross-platform, but this pass executed on Windows
  only; no second-OS execution evidence is claimed.
- Raw protocol traces and patches can contain repository data. Redaction is bounded and artifacts
  are not encrypted; inspect them before sharing.
- Process/worktree isolation is not a kernel sandbox. Claude Code therefore fails closed on this
  host; hostile tasks require WSL2/container isolation.
- Codex permission expansion is declined and Claude unsandboxed fallback is disabled. Vendor
  configuration, credentials, plugins, MCP settings, and session directories are preserved; Claude
  session persistence is disabled by default.
- Temporary prompt, schema, CLI-home, and test directories were removed. The target mutation canary
  remained unchanged. The only deleted tracked file is the replaced generated Web asset hash.
- Supported vendor ranges require maintenance as official schemas evolve. Unsupported versions fail
  with a repair action rather than running optimistically.

## Exact files

Added:

```text
components/villani-ops/villani_ops/closed_loop/agent_systems/acp.py
components/villani-ops/villani_ops/closed_loop/agent_systems/discovery.py
components/villani-ops/villani_ops/runners/codex_app_server.py
components/villani-ops/villani_ops/runners/structured_protocol.py
components/villani-ops/villani_ops/schemas/v1/harness-discovery.schema.json
components/villani-ops/villani_ops/tests/fixtures/pt6/claude-events.jsonl
components/villani-ops/villani_ops/tests/fixtures/pt6/codex-events.jsonl
components/villani-ops/villani_ops/tests/fixtures/pt6/fake_structured_harness.py
components/villani-ops/villani_ops/tests/test_pt6_structured_harnesses.py
components/villani-web/dist/assets/index-GtA_ObUO.js
docs/PT6_COMPLETION_REPORT.json
docs/PT6_COMPLETION_REPORT.md
docs/PT6_QUALIFICATION_EVIDENCE.json
integration/fixtures/protocol/v1/valid_run/harness-discovery.json
schemas/v1/harness-discovery.schema.json
```

Changed:

```text
PLANS.md
components/villani-flight-recorder/dist/providers/villaniSchemaValidation.js
components/villani-flight-recorder/src/providers/villaniProtocol.ts
components/villani-flight-recorder/src/providers/villaniSchemaValidation.ts
components/villani-flight-recorder/test/villaniProtocol.test.ts
components/villani-ops/README.md
components/villani-ops/villani_ops/cli/unified.py
components/villani-ops/villani_ops/closed_loop/__init__.py
components/villani-ops/villani_ops/closed_loop/adapters/villani_code_attempt.py
components/villani-ops/villani_ops/closed_loop/agent_systems/__init__.py
components/villani-ops/villani_ops/closed_loop/agent_systems/adapters.py
components/villani-ops/villani_ops/closed_loop/agent_systems/configuration.py
components/villani-ops/villani_ops/closed_loop/agent_systems/conformance.py
components/villani-ops/villani_ops/closed_loop/agent_systems/models.py
components/villani-ops/villani_ops/closed_loop/agent_systems/registry.py
components/villani-ops/villani_ops/closed_loop/costs.py
components/villani-ops/villani_ops/closed_loop/model_management.py
components/villani-ops/villani_ops/closed_loop/schema_validation.py
components/villani-ops/villani_ops/runners/__init__.py
components/villani-ops/villani_ops/runners/base.py
components/villani-ops/villani_ops/runners/claude_code.py
components/villani-ops/villani_ops/schemas/v1/agent-system.schema.json
components/villani-ops/villani_ops/schemas/v1/harness-result.schema.json
components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py
components/villani-run-model/dist/agentSystem.d.ts
components/villani-run-model/dist/agentSystem.js
components/villani-run-model/src/agentSystem.ts
components/villani-run-model/test/agentSystem.test.ts
components/villani-web/dist/index.html
components/villani-web/src/ProductPages.tsx
components/villani-web/src/consoleApi.ts
docs/AGENT_SYSTEMS.md
integration/fixtures/protocol/v1/valid_run/harness-conformance.json
schemas/v1/agent-system.schema.json
schemas/v1/harness-result.schema.json
scripts/generate-harness-schemas.py
```

Deleted/replaced generated file:

```text
components/villani-web/dist/assets/index-Db_ajSzJ.js
```

The unrelated untracked `components/villani-agentd/villani_agentd/console_assets/` directory was
preserved and excluded from PT6.

## Milestone boundary

PT6 implementation stops here. PT7 was explicitly **not started**.
