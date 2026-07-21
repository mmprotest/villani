# CLI Agent Mode Milestone 4 completion report

Status: `COMPLETE`

Date: 2026-07-21 (Australia/Sydney)

## Scope result

Milestone 4 implements Claude Code CLI as a **coding attempt runner only**. A
complete execution profile can bind `coding` to a doctor-ready `cli_agent`
system with `driver: claude_code`; classification, verification, selection,
retry/stopping, eligibility, finalization, and delivery continue through the
existing typed ports and the same `ClosedLoopController`.

Claude Code owns the complete coding loop inside one Villani-created isolated
worktree per candidate. Villani supervises the non-interactive process through
the Milestone 2 provider-neutral runtime, translates structured stream events,
validates the supplementary final report, and derives canonical patch evidence
from Git. The Codex adapter now uses the same extracted Git-evidence pipeline,
so verifier-facing candidate evidence is provider-neutral.

No Claude classifier, verifier, or selector was added. The Codex coding adapter
and existing API/internal coding paths remain enabled. No interactive session,
resume/continue, persistent/shared session, background agent, cloud handoff,
login, setup token, API-key configuration, quota surface, or dangerous global
permission bypass was added. Milestone 5 was not started.

## Exact construction paths

### Public and service entry to the one controller

The public path remains:

`villani_distribution.cli:main` -> `villani_ops.cli.unified:app` -> public
`villani run` handler -> `villani_ops.cli.unified.build_controller`.

The service path remains:

`villani_agentd.console.ConsoleService` ->
`villani_ops.cli.unified.build_controller`.

`build_controller` still performs:

`migrate_agent_system_configuration` -> `_load_backends` ->
`build_agent_system_registry` -> `RoleSystemRegistry.resolve_profile` ->
`require_profile_runnable` -> `RoleFactoryDependencies` ->
`build_classifier` / `build_attempt_runner` / `build_verifier` /
`build_selector` -> one `ClosedLoopController`.

The controller imports no Codex or Claude module and continues to depend on the
role-specific `Classifier`, `AttemptRunner`, `Verifier`, and `Selector` ports.

### Existing API/internal and Codex paths

- Classification binding -> `build_classifier` -> existing
  `_ClassifierAdapter`.
- API/internal coding binding -> `build_attempt_runner` -> existing
  `AgentSystemRegistry.attempt_runner()` ->
  `AgentSystemAttemptRunner` / `VillaniCodeHarnessAdapter` -> existing
  `VillaniCodeAttemptAdapter` and Villani Code runner.
- Codex coding binding -> `build_attempt_runner` -> existing
  `CodexCliAttemptAdapter`; its normal candidate-evidence path now calls the
  shared `cli_coding.prepare_candidate` and
  `cli_coding.collect_candidate_evidence` functions.
- Verification binding -> `build_verifier` -> existing
  `VillaniVerifierAdapter`.
- Selection binding -> `build_selector` -> existing
  `EvidenceSelectorAdapter`.
- Materialization remains `ApprovalGuardedMaterializer` over
  `DeliveryMaterializerAdapter`; it is not an agent-system role.

### New Claude coding path

`build_agent_system_registry` -> parse coding-only
`CliAgentSystemConfig(driver="claude_code")` ->
`ClaudeCodeCliDriver.probe` -> passing `ClaudeProbeResult` ->
`ClaudeCodeCliAttemptAdapter(driver, probe)` -> registry
`cli_attempt_runners[system.id]` -> `RoleFactoryDependencies` ->
`build_attempt_runner` -> `BuiltinAgentRunnerPlugin` -> the same controller.

Per candidate:

`ClaudeCodeCliAttemptAdapter.run` -> shared `prepare_candidate` ->
`GitIsolationAdapter.create` -> external-symlink guard -> write governed
prompt/schema/provider artifacts -> `ClaudeCodeCliDriver.build_invocation` ->
shared `CliProcessSupervisor` -> sanitize and parse Claude stream-JSON ->
strict final-result validation -> shared `collect_candidate_evidence` -> Git
status/path-safety/canonical patch/changed files/digests -> existing
`AttemptResult` and candidate bundle -> unchanged verifier, selector, and
delivery pipeline.

## Version, capability, and Doctor evidence

The driver resolves the configured executable and runs these four bounded,
shell-free probes through the shared supervisor:

1. `claude --version`
2. `claude --help`
3. `claude auth status`
4. `claude doctor`

The recorded repository assumption is enforced exactly: Claude Code
`>=2.1.138,<2.2.0`. Readiness additionally requires print mode, stream-JSON,
JSON Schema output, no-session-persistence, model selection, `acceptEdits`,
explicit tool and allowed-tool flags, verbose structured streaming, browser
suppression, stdin prompt delivery, coding-only role declaration, and
`workspace_write`. `villani_controlled` also requires bare mode, controlled
settings sources, strict empty MCP configuration, and slash-command
suppression. Unsupported capabilities fail closed and no API fallback occurs.

The deterministic conformance executable reported:

`2.1.138 (Claude Code fixture)`

It exposed every required capability, reported authenticated Claude account
readiness and healthy Doctor status, and enabled the complete fake-executable
vertical slice without paid credentials.

A separate bounded local probe (no model call) resolved:

- executable: `C:\Users\Simon\.local\bin\claude.exe`
- exact output: `2.1.138 (Claude Code)`
- parsed version: `2.1.138`
- authentication: ready, method `authenticated_unspecified`
- required native-project capabilities: present, except optional
  `--max-turns`
- `claude doctor`: unhealthy in this environment
- final readiness: false with
  `mcp_plugin_hook_startup_failure`

This is the intended fail-closed behavior: the local system is configured but
cannot be selected until Doctor is healthy. Probe artifacts record readiness
and a non-secret authentication method only. They do not inspect credential
files, perform login, create tokens, or claim subscription/API billing.

## Exact safe command construction

For the passing native-project conformance system, the safely rendered argv is:

```text
<resolved-claude-executable>
<optional-launcher-arguments>
-p
--model
<configured-model>
--output-format
stream-json
--verbose
--no-session-persistence
--permission-mode
acceptEdits
--tools
Bash,Read,Edit,Write,Glob,Grep
--allowedTools
Bash,Read,Edit,Write,Glob,Grep
--no-chrome
--json-schema
<inline-coder-result-schema>
--max-turns
<configured-positive-limit>
```

`--max-turns` is included only when the installed help advertises it; the
shared process timeout always remains bounded. The probe accepts the installed
spelling `--allowedTools` or `--allowed-tools` and records which spelling was
resolved. The complete prompt is delivered through stdin and stdin is closed;
it is never placed in argv. Every option/value is a separate argument and no
shell string is constructed.

`villani_controlled` appends only after capability proof:

```text
--bare
--settings <absolute-controlled-settings.json>
--setting-sources=
--strict-mcp-config
--mcp-config <absolute-empty-mcp.json>
--disable-slash-commands
```

The controlled settings disable hooks, plugins, auto-memory, user/project
instructions, slash commands, browser integration, and ambient MCP servers;
the exact disabled list is persisted. `native_project` is the coding default,
permits ordinary user/project instruction discovery, and does not silently
ignore repository `AGENTS.md` or native Claude behavior.

Production scans found no `shell=True`, `create_subprocess_shell`, `--resume`,
`--continue`, dangerous permission-skip flag, or full-access mode. One process
and one non-persistent session are used per candidate.

## Prompt and structured-result contracts

`villani.claude_code_coding_prompt.v1` contains the task and success criteria
verbatim, attempt ID, exact worktree, resolved instruction policy,
worktree-only mutation scope, relevant-validation instruction, no
commit/push/PR/cloud-session/external-repository rule, no unchecked success
claim, and the strict final-summary requirement. It includes no competing
candidate, future verifier result, hidden evaluator, route score, model
prestige, expected patch, or benchmark hint.

The prompt is stored as `prompt.txt` plus `prompt.digest`.
`invocation.json` stores only its governed reference, SHA-256, and byte count,
not prompt text. A test delivered more than 100,000 prompt bytes through stdin
from a worktree whose path contained spaces and non-ASCII characters.

The normative, strict supplementary result contract is
`villani.claude_coder_result.v1`:

- `status: completed | blocked`
- `summary`
- reported test command, nullable reported exit status, and reported result
- known limitations
- files the agent believes changed

This report never overrides Git patch truth, canonical changed files,
repository validation, verifier evidence, or acceptance.

## Git evidence, isolation, and artifacts

Claude and Codex now call the same provider-neutral Git evidence implementation.
For every candidate it records NUL-delimited Git status, tracked/untracked/
renamed/deleted files, source and worktree HEAD/tree identities, baseline and
candidate digests, external symlinks, unsafe/forbidden paths, target identity,
canonical patch presence, process cleanup, and the exact isolated workspace.
Partial patches survive malformed streams, crashes, timeout, cancellation, and
path violations. The candidate patch is not applied to the source repository
during coding.

Canonical per-attempt layout:

```text
candidates/<attempt-id>/
  agent/
    provider.json
    invocation.json
    prompt.txt
    prompt.digest
    coder-result.schema.json
    stdout.log
    stderr.log
    claude-events.jsonl
    normalized-events.jsonl
    final-output.json
    normalized-result.json
    process-result.json
    output-tail.json
    verifier-trace/
  repository/
    baseline.json
    status.json
    changed-files.json
    candidate.patch
    cleanup.json
```

Tests proved distinct worktrees, PIDs, and session IDs for parallel candidates;
spaces/non-ASCII paths; unchanged target HEAD/tree/status/content; exact
tracked/untracked/rename/delete evidence; and equal provider-neutral candidate
keys between Codex and Claude fixtures. Cancellation and timeout descendant
cleanup passed five consecutive runs each on Windows. The existing verifier
completed and delivered the selected Claude fixture patch through the unchanged
controller.

Provider streams, stderr, output tails, final reports, normalized records, and
metadata are redacted using registered secrets. Hidden `thinking`,
`redacted_thinking`, signature, and reasoning fields are removed from durable
Claude artifacts. The secret/hidden-reasoning test scans every attempt file.

## Exact normalized event mappings

| Claude stream object | Villani normalized event |
|---|---|
| `system` / `init` | `session_started` |
| assistant/user usage | `usage_update` |
| assistant visible text | `agent_message` |
| `tool_use` Bash | `command_started` |
| Bash `tool_result` | `command_completed` or `command_failed` |
| `tool_use` Read | `file_read` |
| Edit/Write/NotebookEdit start | `file_write_started` |
| Edit/Write/NotebookEdit result | `file_write` |
| Agent/Task start/result | `subagent_started` / `subagent_completed` |
| other tool start/result | `tool_call_started` / `tool_call_completed` |
| `rate_limit_event` or `retry` | `retry` |
| `warning` | `warning` |
| `error` or `provider_error` | `provider_error` |
| `cancelled` / `cancellation` | `cancellation` |
| successful `result` | optional `usage_update`, then `turn_completed` |
| failed `result` | optional `usage_update`, then `provider_error` |
| unknown object | `claude_code.raw_event` with namespace `claude_code.raw` |

Only documented structured objects are parsed. Human terminal lines are not
scraped, and hidden chain-of-thought is not requested, derived, or persisted.

## Exact failure mappings

Probe/readiness failures:

- missing executable -> `claude_not_installed`
- inactive `claude auth status` -> `claude_not_authenticated`
- out-of-range/malformed version or missing safe capability ->
  `unsupported_claude_version`
- missing stream-JSON/JSON-Schema capability ->
  `missing_required_structured_output_capability`
- unhealthy `claude doctor` -> `mcp_plugin_hook_startup_failure`
- unsupported permission profile -> `permission_denied`

Invocation/result failures:

- runtime missing/non-runnable executable -> `claude_not_installed`
- shared-runtime timeout -> `process_timeout`
- user/controller/service cancellation -> `process_cancellation`
- process-tree cleanup failure -> `cleanup_failure`
- malformed/decode/oversized structured stream -> `invalid_json`
- provider authentication markers -> `provider_authentication_failure`
- provider rate-limit/overload markers ->
  `provider_rate_limit_or_overload`
- unavailable/not-found model markers -> `model_unavailable`
- permission markers -> `permission_denied`
- denied/disallowed tool markers -> `tool_denied`
- MCP/plugin/hook startup markers -> `mcp_plugin_hook_startup_failure`
- nonzero/failed process -> `process_crash`
- successful process without a result object -> `missing_final_result`
- missing/invalid strict structured payload -> `json_schema_failure`
- successful coding result without a Git patch ->
  `coding_completed_with_no_patch` (coding outcome, not infrastructure)
- unsafe/forbidden path or changed source identity -> `path_violation`

Infrastructure failure remains distinct from semantic rejection and cannot
silently become a coding rejection or verifier zero.

## Public CLI and configuration

Added Claude to the existing Agent Mode coding-only flow:

```text
villani agents add claude-coder --driver claude_code --executable claude --model <model> --roles coding
villani agents doctor claude-coder
villani profiles set-role cli coding claude-coder
```

`agents add` only writes neutral, secret-free configuration. `agents doctor`
runs the four bounded readiness probes but performs no login/model request.
`profiles set-role` changes only coding in the selected complete profile.
Explicit unavailable profiles fail without API fallback. Attempts to declare a
Claude non-coding role fail. No API key or quota option exists.

## Files

The workspace already contained the uncommitted Milestone 1-3 foundation. The
exact Milestone 4 delta follows; those earlier changes and unrelated user
virtual environments were preserved.

### Added

- `components/villani-ops/villani_ops/closed_loop/claude_code_cli/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/claude_code_cli/attempt.py`
- `components/villani-ops/villani_ops/closed_loop/claude_code_cli/driver.py`
- `components/villani-ops/villani_ops/closed_loop/claude_code_cli/events.py`
- `components/villani-ops/villani_ops/closed_loop/claude_code_cli/models.py`
- `components/villani-ops/villani_ops/closed_loop/claude_code_cli/prompt.py`
- `components/villani-ops/villani_ops/closed_loop/cli_coding/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/cli_coding/evidence.py`
- `components/villani-ops/villani_ops/tests/fixtures/claude_code_cli/events-success.jsonl`
- `components/villani-ops/villani_ops/tests/fixtures/claude_code_cli/events-unknown.jsonl`
- `components/villani-ops/villani_ops/tests/fixtures/claude_code_cli/fake_claude.py`
- `components/villani-ops/villani_ops/tests/test_agent_mode_m4.py`
- `components/villani-ops/villani_ops/tests/test_claude_code_cli_coding.py`
- `schemas/v1/claude-coder-result.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/claude-coder-result.schema.json`
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_001/agent/claude-coder-result.json`
- `docs/CLI_AGENT_MODE_M4_COMPLETION_REPORT.md`
- `docs/CLI_AGENT_MODE_M4_COMPLETION_REPORT.json`

### Changed for Milestone 4

- `components/villani-ops/villani_ops/cli/unified.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/factories.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/registry.py`
- `components/villani-ops/villani_ops/closed_loop/codex_cli/attempt.py`
- `components/villani-ops/villani_ops/closed_loop/schema_validation.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py`
- `components/villani-run-model/src/agentSystem.ts`
- `components/villani-run-model/dist/agentSystem.js`
- `components/villani-run-model/dist/agentSystem.d.ts`
- `components/villani-run-model/test/agentSystem.test.ts`
- `components/villani-flight-recorder/src/providers/villaniSchemaValidation.ts`
- `components/villani-flight-recorder/dist/providers/villaniSchemaValidation.js`
- `components/villani-flight-recorder/test/villaniProtocol.test.ts`
- `PLANS.md` (progress section only)

### Removed or data-migrated

None. Milestone 4 reuses the Milestone 1 neutral `cli_agent` discriminator and
idempotent legacy configuration migration. Existing API configurations and old
run bundles remain readable. Codex candidate evidence construction was
internally extracted into a shared module without changing its wire shape.

## Tests added

The fake Claude fixture and M4 regression tests cover official structured
fixtures, unknown events, malformed stream location, exact probe/version/auth/
Doctor/capabilities, missing executable/authentication/capability, native and
controlled policies, safe stdin argv, successful/no patch,
tracked/untracked/rename/delete Git truth, command/edit events, missing/invalid
final result, model/permission/tool/MCP-hook/provider-auth/rate failures,
nonzero/timeout/cancellation partial patches, descendant cleanup, large prompt,
spaces/non-ASCII paths, no session persistence/resume, parallel isolation,
target safety, exact identity, secret and hidden-reasoning absence, public
configure/Doctor/profile commands, Codex/Claude candidate-contract equality,
existing verifier/delivery end-to-end behavior, and the opt-in real smoke gate.

## Command evidence

Authoritative successful results:

| Validation | Exact result |
|---|---|
| Required pre-implementation regression | failed as intended: `AgentSystemIntegrationUnavailable`; passed after registry/factory implementation |
| Final focused M4 (`test_agent_mode_m4.py` + Claude file, non-integration) | `33 passed, 1 deselected in 51.04s` |
| M3/M4 Codex/Claude cross-driver suite | `65 passed, 2 deselected in 94.86s` |
| Complete Villani Ops suite | `1417 passed, 4 skipped, 118 deselected in 451.84s` |
| Timeout + cancellation descendant cleanup stress | five runs, each `2 passed`; 10/10 cases |
| Python packaged protocol/schema suite | `23 passed in 1.19s` |
| Root protocol contract | `2 passed` with one cache warning |
| Root closed-loop suite | `11 passed, 2 warnings in 54.40s` |
| Marked public API-mode E2E | `3 passed, 1 deselected, 1 warning in 42.15s` |
| Villani Code | `686 passed, 1 skipped, 27 warnings in 224.58s` |
| Distribution | 76 in-sandbox tests passed; sandboxed Vite clean-install failed on denied traversal; exact approved rerun passed, so all 77 cases passed |
| Agentd | `87 passed in 20.67s` |
| Run Model | 6 files/17 tests passed; typecheck/build and changed-source Prettier passed |
| Flight Recorder | 21 files/118 tests passed; typecheck/build/format passed |
| Targeted mypy | three groups, respectively 4, 3, and 3 production modules: all `Success: no issues found` |
| Changed Python Ruff | all files formatted; `All checks passed!` |
| Python 3.11 compile | exit 0 for all changed production modules |
| Claude production no-shell/resume/dangerous scan | 0 matches |
| Controller provider-import scan | 0 matches |
| Claude coder schema parity | root/package SHA-256 both `9D929F0F864146E84C58487C950A8EA0C73AC1FF4B7EC202D761FB28AB66910A` |
| Real Claude smoke without opt-in | `1 skipped in 0.78s`: `set VILLANI_ENABLE_REAL_CLAUDE_TESTS=1 to enable the paid/external real Claude Code smoke test` |
| Local non-model Claude probe | exact `2.1.138`; auth ready; Doctor unhealthy; readiness false as required |
| `git diff --check` | exit 0 |

Development/environment failures were retained rather than hidden:

- The first new test failed before implementation because the role factory
  allowed only the Codex CLI coding seam.
- Two early focused failures were test fixture schema-path typos; the strict
  schema and production implementation were unchanged, and the corrected suite
  passes.
- The sandboxed distribution clean-install case failed because esbuild could
  not traverse the repository. Its exact approved out-of-sandbox rerun passed.
- Run Model has no native `format:check` script. Direct Prettier passes its
  changed source/test files; generated `dist` files retain `tsc` formatting.
- An initial real-smoke command was deselected by repository marker defaults;
  the explicit `-m integration` rerun produced the required skip reason.
- `rg` is unavailable on this host; the static scans used PowerShell
  `Select-String` and found no prohibited production pattern.

## Real smoke status and remaining risks

The real test is committed behind `VILLANI_ENABLE_REAL_CLAUDE_TESTS=1`, requires
the executable, successful auth, passing full probe/Doctor, a disposable Git
repository, a bounded 120-second timeout, and at most eight turns when the
installed CLI supports that flag. It was not enabled. No paid/external Claude
coding request was made.

Remaining compatibility risks:

- Local real model/stream/result behavior is not proved because the installed
  CLI's Doctor is unhealthy and the paid smoke was not authorized. Fake
  official-format conformance is complete.
- Windows x86_64 path, isolation, cancellation, forced descendant cleanup, and
  target safety are locally proved. POSIX shared-runtime process-group behavior
  remains covered by committed tests but awaits Linux/macOS CI in this pass.
- Future Claude help or stream-result format changes fail Doctor/parsing closed;
  unknown valid structured objects remain preserved for audit.
- Provider failure classification uses conservative documented markers.
  Unknown failures remain infrastructure failures, not semantic rejection.
- Broad repository mypy remains noisy outside the targeted Milestone 4 modules.
  No production dependency was added.

## Boundary confirmation

Only Claude Code **coding** was implemented. Claude classification,
verification, and selection were not implemented. Codex coding and API/internal
coding remain compatible. Candidate eligibility and delivery were not changed.
Milestone 5 was not started.
