# CLI Agent Mode Milestone 3 completion report

Status: `COMPLETE`

Date: 2026-07-21 (Australia/Sydney)

## Scope result

Milestone 3 implements Codex CLI as a **coding attempt runner only**. A complete
execution profile can bind `coding` to a doctor-ready `cli_agent` system with
`driver: codex` while classification, verification, selection, stopping,
retry, finalization, and delivery continue through the existing typed ports and
the same `ClosedLoopController`.

Each candidate gets its own Villani-created Git worktree, Codex process, prompt,
JSONL stream, final-report artifact, provider identity, and repository evidence.
Codex owns the coding loop inside that worktree. Villani derives patch truth,
changed paths, baseline/candidate digests, and path-safety facts from Git after
the process exits. The source repository is not changed until the unchanged
controller later authorizes and performs delivery.

No Codex classifier, verifier, or selector was added. No Claude Code code was
added. No interactive TUI, terminal scraping, session resume, shared session,
login flow, API-key configuration, quota surface, provider update, or bypass
flag was added. Milestone 4 was not started.

## Exact construction paths

### Public entry to the one controller

The public and service paths remain:

`villani_distribution.cli:main` -> `villani_ops.cli.unified:app` -> public
`villani run` handling -> `villani_ops.cli.unified.build_controller`

and:

`villani_agentd.console.ConsoleService` ->
`villani_ops.cli.unified.build_controller`.

`build_controller` performs:

`migrate_agent_system_configuration` -> `_load_backends` ->
`build_agent_system_registry` -> `RoleSystemRegistry.resolve_profile` ->
`require_profile_runnable` -> `RoleFactoryDependencies` ->
`build_classifier` / `build_attempt_runner` / `build_verifier` /
`build_selector` -> one `ClosedLoopController`.

The controller imports neither the Codex driver nor any Claude driver. It still
depends on the role-specific `Classifier`, `AttemptRunner`, `Verifier`, and
`Selector` protocols.

### Existing API/internal path

- Classification binding -> `build_classifier` -> existing `_ClassifierAdapter`.
- Coding binding `villani-code-runner` -> `build_attempt_runner` ->
  `AgentSystemRegistry.attempt_runner()` -> existing
  `AgentSystemAttemptRunner` / `VillaniCodeHarnessAdapter` -> existing
  `VillaniCodeAttemptAdapter` and internal Villani Code runner.
- Verification binding -> `build_verifier` -> existing
  `VillaniVerifierAdapter`.
- Selection binding -> `build_selector` -> existing
  `EvidenceSelectorAdapter`.
- Materialization remains the existing `ApprovalGuardedMaterializer` over
  `DeliveryMaterializerAdapter` and is not an agent-system role.

The current API-mode installed CLI fixture completed classification, coding,
verification, selection, and delivery without changed orchestration behavior.

### New Codex coding path

`build_agent_system_registry` -> parse coding-only `CliAgentSystemConfig` ->
`CodexCliDriver.probe` -> passing `CodexProbeResult` ->
`CodexCliAttemptAdapter(driver, probe)` -> registry
`cli_attempt_runners[system.id]` -> `RoleFactoryDependencies` ->
`build_attempt_runner` -> `BuiltinAgentRunnerPlugin` -> the same controller.

For each attempt:

`CodexCliAttemptAdapter.run` -> `GitIsolationAdapter.create` -> reject external
symlinks -> write prompt/schema/provider artifacts ->
`CodexCliDriver.build_invocation` -> Milestone 2
`CliProcessSupervisor` -> parse Codex JSONL and strict final report -> inspect
Git status -> path-safety check -> Git-derived canonical patch/changed files and
digests -> existing `AttemptResult` and candidate-bundle shape -> existing
verifier, selector, and delivery pipeline.

## Probe and supported-version evidence

The driver resolves the configured executable and runs three shell-free,
bounded probes through the shared supervisor:

1. `codex --version`
2. `codex exec --help`
3. `codex login status`

Readiness requires exact version output, active authentication, `exec`, JSONL,
model, working-directory, `workspace-write` sandbox, output-schema,
output-last-message, ephemeral-session, and explicit non-interactive approval
support. `villani_controlled` additionally requires both supported suppression
flags. Coding-only role declaration and `workspace_write` permission are also
required. Probe artifacts contain authentication readiness/method only, never a
credential value or billing claim.

The deterministic conformance executable reported the exact tested version:

`codex-cli 9.9.9-fixture`

It advertised every required flag and reported `Logged in using ChatGPT`; tests
proved the exact version, capability map, resolved executable, executable
SHA-256, authentication method, and `billing_identity: not_reported` in the
provider record. This is fixture conformance evidence, not a claim about a real
Codex release. `Get-Command codex` returned `CODEX_NOT_ON_PATH` on this host.
The real smoke therefore remained disabled and skipped with the exact reason:

`set VILLANI_ENABLE_REAL_CODEX_TESTS=1 to enable the paid/external real Codex smoke test`

No real Codex coding invocation or model call was made.

## Exact constructed command

For a native-project production system, the safely rendered argument vector is:

```text
<resolved-codex-executable>
exec
--ephemeral
--json
--model <configured-model>
--sandbox workspace-write
--cd <absolute-candidate-worktree>
--output-schema <absolute-agent-dir>/coder-result.schema.json
--output-last-message <absolute-agent-dir>/final-output.json
--ask-for-approval never
-
```

`villani_controlled` inserts `--ignore-user-config --ignore-rules` before the
final `-`, but only when the probe proves both flags. `native_project` is the
coding default and does not suppress repository instructions. The prompt is
passed as stdin bytes and stdin is closed. Each value is one argv element;
there is no shell command string or `shell=True`.

The production scan found no `--yolo`,
`--dangerously-bypass-approvals-and-sandbox`, `danger-full-access`, deprecated
`--full-auto`, or resume behavior.

## Prompt and final-result contracts

`villani.codex_coding_prompt.v1` contains the task and success criteria
verbatim, attempt ID, exact worktree, resolved instruction policy, worktree-only
mutation rule, validation instruction, no commit/push/PR/external-repository
rule, no unchecked success claim, and final structured-summary instruction. It
contains no other candidate, future verifier result, hidden evaluator,
routing score, model ranking, expected patch, or benchmark hint.

The prompt is stored once as `prompt.txt`; `prompt.digest` stores its SHA-256.
`invocation.json` stores only that governed reference, digest, and byte count,
not prompt text.

`villani.codex_coder_result.v1` is strict and supplementary:

- `status: completed | blocked`
- `summary`
- reported test command, nullable reported exit status, and reported result
- known limitations
- files the agent believes changed

It does not authorize acceptance and does not override Git-derived patch truth,
changed files, or verifier evidence.

## Git-derived evidence and artifacts

Each attempt records:

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
    codex-events.jsonl
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

The existing canonical attempt directory and candidate bundle are also written.
Repository evidence captures tracked, untracked, renamed, and deleted paths;
source/worktree HEAD and tree identity; baseline and candidate digests; unsafe,
external-symlink, and Villani-owned paths; patch presence; cleanup status; and
the exact isolated workspace. Git porcelain is parsed as NUL-delimited bytes,
and Git output uses explicit UTF-8/surrogate handling so spaces and non-ASCII
paths remain exact on Windows.

Partial Git changes remain recorded for malformed streams, crashes, timeout,
cancellation, and path violations. Villani-owned paths are marked ineligible
and excluded from the materializable patch. The existing verifier receives the
same candidate bundle, patch-quality, repository-validation, and trace boundary.
Codex-reported tests remain non-authoritative; the adapter does not fabricate a
repository-validation pass.

## Exact normalized event mappings

| Codex JSONL input | Villani normalized event |
|---|---|
| `thread.started` | `session_started` |
| `turn.started` | `turn_started` |
| `turn.completed` with usage | `usage_update`, then `turn_completed` |
| `turn.failed` | `turn_failed` |
| top-level `error` | `provider_error` |
| top-level `warning` | `warning` |
| completed `agent_message` item | `agent_message` |
| completed exposed `reasoning` item | `reasoning_summary` with `source_visibility: codex_jsonl` |
| `plan`, `plan_update`, or `todo_list` item | `plan_update` |
| started `command_execution` | `command_started` |
| updated command with output | `command_output` |
| completed successful command | `command_completed` |
| failed/nonzero command | `command_failed` |
| `file_change` / `file_changes` | `file_write` |
| started MCP/tool/web-search item | `tool_call_started` |
| completed MCP/tool/web-search item | `tool_call_completed` |
| unknown object | `codex.raw_event` with namespace `codex.raw` and the redacted raw object |

Only a reasoning summary already exposed by Codex JSONL is stored. Hidden
chain-of-thought is neither requested nor inferred.

## Exact failure mappings

Probe failures:

- missing executable -> `codex_not_installed`
- failed/missing exact version -> `unsupported_codex_version`
- missing required safe flag/capability or non-coding role ->
  `unsupported_required_flag`
- inactive `codex login status` -> `codex_not_authenticated`
- unsupported permission profile -> `permission_sandbox_failure`

Invocation/result failures:

- runtime missing/non-runnable executable -> `codex_not_installed`
- shared-runtime timeout -> `process_timeout`
- user/controller/service cancellation -> `process_cancellation`
- process-tree cleanup failure -> `cleanup_failure`
- malformed/decode/oversized JSONL -> `malformed_jsonl`
- provider authentication markers -> `provider_authentication_failure`
- provider rate-limit/overload markers -> `provider_rate_limit_or_overload`
- unavailable/not-found model markers -> `model_unavailable`
- sandbox/permission/read-only markers -> `permission_sandbox_failure`
- nonzero or otherwise failed process -> `process_crash`
- successful process without final output -> `missing_final_structured_output`
- invalid/oversized final JSON -> `structured_output_schema_failure`
- successful coding result without a Git patch ->
  `coding_completed_with_no_patch` (coding outcome, not infrastructure)
- forbidden/unsafe path or changed source identity -> `path_violation`

Infrastructure failures remain distinct from semantic rejection and never
become a verifier zero by themselves.

## Public CLI changes

Added current-convention commands:

```text
villani agents add <system-id> --driver codex --executable <path> --model <model> --roles coding
villani agents doctor [<system-id>] [--json]
villani profiles set-role <profile-id> coding <system-id>
```

`agents add` is configuration-only and accepts Codex coding systems only in
this milestone. `agents doctor` performs the three bounded probe commands but
does not log in or invoke a model. `profiles set-role` copies an existing
complete profile when necessary and changes only the requested role. Existing
`agents list/inspect` and `profiles list/inspect` now report a doctor-ready
Codex coding system/profile as runnable. Explicit unavailable profiles still
fail without API fallback.

## Files

The workspace already contained the uncommitted Milestone 1 and 2 foundation.
The exact Milestone 3 delta is below; those earlier files and unrelated user
virtual environments were preserved.

### Added

- `components/villani-ops/villani_ops/closed_loop/codex_cli/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/codex_cli/attempt.py`
- `components/villani-ops/villani_ops/closed_loop/codex_cli/driver.py`
- `components/villani-ops/villani_ops/closed_loop/codex_cli/events.py`
- `components/villani-ops/villani_ops/closed_loop/codex_cli/models.py`
- `components/villani-ops/villani_ops/closed_loop/codex_cli/prompt.py`
- `components/villani-ops/villani_ops/tests/fixtures/codex_cli/events-success.jsonl`
- `components/villani-ops/villani_ops/tests/fixtures/codex_cli/events-unknown.jsonl`
- `components/villani-ops/villani_ops/tests/fixtures/codex_cli/fake_codex.py`
- `components/villani-ops/villani_ops/tests/test_agent_mode_m3.py`
- `components/villani-ops/villani_ops/tests/test_codex_cli_coding.py`
- `schemas/v1/codex-coder-result.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/codex-coder-result.schema.json`
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_001/agent/coder-result.json`
- `docs/CLI_AGENT_MODE_M3_COMPLETION_REPORT.md`
- `docs/CLI_AGENT_MODE_M3_COMPLETION_REPORT.json`

### Changed for Milestone 3

- `components/villani-ops/villani_ops/agentic/git_artifacts.py`
- `components/villani-ops/villani_ops/cli/unified.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/git_isolation.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/factories.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/registry.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/role_registry.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/protocol.py`
- `components/villani-ops/villani_ops/closed_loop/schema_validation.py`
- `components/villani-ops/villani_ops/isolation/copy_git.py`
- `components/villani-ops/villani_ops/schemas/v1/attempt.schema.json`
- `components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py`
- `schemas/v1/attempt.schema.json`
- `components/villani-ops/villani_ops/tests/closed_loop/test_cli_runtime.py`
- `components/villani-run-model/src/agentSystem.ts`
- `components/villani-run-model/dist/agentSystem.js`
- `components/villani-run-model/dist/agentSystem.d.ts`
- `components/villani-run-model/test/agentSystem.test.ts`
- `components/villani-flight-recorder/src/providers/villaniProtocol.ts`
- `components/villani-flight-recorder/src/providers/villaniSchemaValidation.ts`
- `components/villani-flight-recorder/dist/providers/villaniSchemaValidation.js`
- `components/villani-flight-recorder/test/villaniProtocol.test.ts`
- `tests/closed_loop/test_protocol_contract.py`
- `PLANS.md` (progress section only)

### Removed or migrated

None. The existing configuration migration remains idempotent and the new
coding system uses the Milestone 1 neutral contracts. The only compatible wire
relaxation permits configured neutral agent-system IDs, while retaining legacy
content-addressed `asys_...` IDs. Old run bundles remain readable.

## Tests added

The M3 tests cover official-format parsing, command and usage events, unknown
event preservation, malformed JSONL, exact probe/version/auth/capabilities,
missing executable, missing authentication, unsupported capabilities, safe
argv construction, native and controlled instruction policy, successful patch,
no patch, untracked/renamed/deleted paths, provider auth/rate limit, model and
permission failures, missing/schema-invalid final output, crash/timeout partial
patches, path violation, controller cancellation and descendant cleanup,
spaces/non-ASCII paths, target safety, parallel candidate isolation/session
identity, exact provider identity, secret absence, API coder compatibility,
controller import boundaries, public configure/doctor/profile commands, the
existing verifier/selector/materializer end-to-end path, and the opt-in real
smoke prerequisite gate.

## Command evidence

Authoritative successful commands:

| Command | Exact result |
|---|---|
| M3 tests (`test_codex_cli_coding.py` + red-regression file) | `32 passed, 1 deselected in 43.26s` |
| Post-format M1/M3/protocol regression | `81 passed, 1 deselected in 49.65s` |
| M1/M2/M3, unified CLI, protocol and closed-loop compatibility selection | `429 passed, 2 skipped, 1 deselected in 177.24s` |
| Final complete Villani Ops suite | `1384 passed, 4 skipped, 117 deselected in 467.96s` |
| Shared runtime complete file | `41 passed, 2 skipped in 11.63s` |
| M3 cancellation/child cleanup repeated after atomic fixture fix | five independent passes (5/5) |
| Output-after-cancellation synchronized stress | ten independent passes (10/10) |
| Graceful shutdown stress | ten independent passes (10/10) |
| Existing Villani Code process-tree test stress | five independent passes (5/5) |
| Distribution full suite, unrestricted after sandbox denial | `77 passed in 211.84s` |
| Agentd full suite | `87 passed in 20.37s` |
| Root closed-loop non-E2E | `8 passed, 3 deselected, 2 warnings in 12.45s` |
| Installed public CLI/API E2E | `3 passed, 1 deselected, 1 warning in 41.04s` |
| Run Model tests | 6 files and 17 tests passed |
| Run Model typecheck/build/targeted Prettier | all exit 0 |
| Flight Recorder tests | 21 files and 118 tests passed |
| Flight Recorder typecheck/build/format | all exit 0 |
| Full Villani Ops Ruff lint | `All checks passed!` |
| Changed-Python Ruff format | all changed files formatted; final check passes |
| Agentd Ruff lint/format | all checks passed |
| Targeted mypy with imported implementation bodies skipped | `Success: no issues found in 8 source files` with `--check-untyped-defs` |
| Python 3.11 compile | `PYTHON_3_11_COMPILE=PASS` |
| Production no-shell scan | no `shell=True` or `create_subprocess_shell` references |
| Forbidden Codex-flag scan | no matches |
| Root/package coder-schema SHA-256 | both `E5B4F1C4C194BF5DC9920E965F04E69BC20C0150DA6E280EAFF5E4F3E5D46914`; byte-identical |
| Packaged Python validation of fixture coder result | `CODEX_CODER_SCHEMA_VALIDATION=PASS` |
| Real Codex smoke without opt-in | `1 skipped in 0.79s` with the exact paid/external opt-in reason |
| `Get-Command codex` | `CODEX_NOT_ON_PATH` |
| `git diff --check` | exit 0 |

Development failures were retained as evidence rather than hidden:

- The required initial regression failed before implementation because the
  role factory dependency set had no CLI coding-attempt seam; it passed after
  the registry/factory boundary was implemented.
- A first broad compatibility command used `C:\tmp` as pytest base temp and was
  invalidated by 246 setup errors (`Access is denied`); the same selection then
  passed 429 tests from the writable workspace.
- Repository-wide mypy followed pre-existing imports and reported 239 errors in
  49 unrelated/baseline files. The targeted eight M3 modules pass with
  `--follow-imports=skip --check-untyped-defs`.
- The first full Ops pass recorded `1382 passed, 4 skipped, 117 deselected, 2
  failed`; one new output-cancellation fixture raced process startup and one
  existing Villani Code heartbeat was transient. Synchronization was scoped to
  the relevant fixture; the existing heartbeat passed 5/5. A second pass found
  one over-broad graceful-test synchronization issue (`1383 passed, 1 failed`),
  which was reverted. The third complete pass is the authoritative 1,384-pass
  result above.
- The sandboxed distribution pass recorded `76 passed, 1 failed` because
  esbuild was denied repository reads. The narrowly approved unrestricted
  rerun passed all 77 tests.
- `py -3.11` could not select the launcher-advertised runtime; invoking the
  exact Python 3.11 executable performed the successful compile check.

## Remaining compatibility risks

- No real Codex executable or authenticated account was available locally, so
  real-version behavior and paid model execution remain unproved. The opt-in
  smoke is committed but was correctly skipped. The exact locally supported
  evidence is the fake official-format conformance version only.
- Windows x86_64 process, path, non-ASCII, cancellation, forced descendant
  cleanup, and target-safety behavior is locally proved. POSIX process-group and
  path behavior remains for Linux/macOS CI; the shared-runtime POSIX tests are
  committed but skipped on this host.
- Capability detection deliberately follows installed `exec --help` behavior.
  A future Codex output-format change will fail doctor or parsing closed; unknown
  valid JSON objects remain preserved for audit.
- Provider stderr classification is conservative string matching. Unknown
  provider failures remain infrastructure failures rather than semantic task
  rejection.
- The broad repository mypy baseline remains noisy outside the targeted M3
  boundary. This milestone added no production dependency.

## Boundary confirmation

Only the Codex coding role was implemented. Classification, verification, and
selection remain existing implementations; candidate eligibility and delivery
rules are unchanged. Claude Code was not started. Milestone 4 was not started.
