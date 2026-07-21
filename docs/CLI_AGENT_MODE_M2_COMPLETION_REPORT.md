# CLI Agent Mode Milestone 2 completion report

Status: `COMPLETE`

Date: 2026-07-21 (Australia/Sydney)

## Scope result

Milestone 2 adds one provider-neutral asynchronous subprocess runtime and a
cross-platform fake-executable conformance fixture. It does not construct a
Codex command, construct a Claude Code command, parse either provider's event
format, implement a role adapter, or connect the runtime to a controller role.
No Codex CLI or Claude Code CLI process was launched. Milestone 3 was not
started.

The existing Milestone 1 configuration, registry, role factories, one
`ClosedLoopController`, verifier boundary, candidate eligibility, materializer,
and API/internal execution paths are unchanged.

## Dependency and runtime paths

The current public construction path remains:

`villani_distribution.cli:app` / `villani run` or
`villani_agentd.console.ConsoleService`
→ `villani_ops.cli.unified.build_controller`
→ `migrate_agent_system_configuration`
→ `_load_backends`
→ `build_agent_system_registry`
→ `RoleSystemRegistry.resolve_profile`
→ `RoleFactoryDependencies`
→ `build_classifier`, `build_attempt_runner`, `build_verifier`, and
`build_selector`
→ the existing typed implementations
→ the same `ClosedLoopController`.

The new lower-level runtime path is intentionally not in that construction
path yet:

future driver-owned command construction
→ immutable `CliInvocation`
→ `CliProcessSupervisor.run`
→ redacted `invocation.json` and bounded binary artifact handles
→ `asyncio.create_subprocess_exec(executable, *arguments, cwd=..., env=...)`
→ concurrent stdout/stderr drain and optional provider-neutral JSONL framing
→ one idempotent process-tree cleanup gate
→ `CliProcessResult`, `output-tail.json`, and `process-result.json`.

The supervisor receives one executable and one argument tuple. It has no role
prompt, provider name, Codex/Claude import, provider command construction, or
provider-specific error classification.

## Process creation and termination strategy

### Common creation and evidence

- Uses `asyncio.create_subprocess_exec`; no shell API or command string is used.
- Supplies each argument separately, the exact working directory, a resolved
  environment mapping, and piped/closed stdin when bytes are present.
- Drains stdout and stderr in independent tasks. Each stream has independent
  total and read-chunk bounds, a bounded in-memory tail, raw-byte disk evidence,
  incremental UTF-8 policy monitoring, and streaming known-secret redaction.
- Optional JSONL mode only frames, bounds, UTF-8-decodes, and validates JSON
  objects. It contains no provider event interpretation.
- Timeout, user/controller cancellation, parent task cancellation, stdin or
  output failure, and parser/limit failure all converge on one cleanup path.

### POSIX

- `start_new_session=True` creates an invocation-owned session/process group.
- Graceful cleanup signals only that group with `SIGTERM`.
- The runtime polls for the group to disappear for the configured grace period.
- Remaining group members receive `SIGKILL`; the parent is awaited/reaped and
  the group is checked again.
- The branch avoids targeting a PID or group not created for the invocation.

### Windows

- Creation combines `CREATE_NEW_PROCESS_GROUP` with `CREATE_SUSPENDED`.
- Before provider code can execute, the process is assigned to a kill-on-close
  Windows Job Object, its owned initial thread is enumerated and resumed, and
  inability to prove either operation fails closed.
- Graceful cleanup sends `CTRL_BREAK_EVENT` to the invocation process-group ID
  and waits the configured grace period while polling Job Object membership.
- After grace expiry, `TerminateJobObject` terminates the complete descendant
  tree. `taskkill /PID ... /T /F` is a shell-free fallback only when Job Object
  attachment failed while the parent remains identifiable.
- The parent is awaited and the Job Object is closed. No elevation is required.
- Five child-spawning cases passed in the full conformance suite, and the same
  five-case test was run three more times (15/15) after suspended-start
  hardening. An unrelated sleeping process remained alive during cleanup.

## Environment and artifact policy

`CliEnvironmentPolicy` supports explicit `inherit` and `minimal` modes,
additions, overrides, removals, and redaction-key names. Windows key comparison
is case-insensitive, including `PATH`; POSIX comparison is exact. Real values
exist only in the in-memory invocation. `invocation.json` records variable
names, provenance, and redaction flags, never values.

Every invocation targets this bounded artifact set:

```text
agent/
  invocation.json
  stdout.log
  stderr.log
  process-result.json
  raw-events.jsonl
  output-tail.json
```

`invocation.json` records the executable path, unresolved executable-identity
placeholder, redacted arguments, value-free environment metadata, role/workspace
identity, repository-writability fact, timeout, output limits, and start time.
Governed stdin/prompt content is represented only by artifact reference, size,
and SHA-256 digest. Logs preserve bounded redacted bytes; malformed bytes remain
in logs while tail rendering uses the explicit replacement policy, or strict
mode produces `output_decode_failed`. No truncation can produce success.

## Failure taxonomy

- `executable_not_found`
- `executable_not_runnable`
- `spawn_failed`
- `stdin_failed`
- `timeout`
- `cancelled`
- `nonzero_exit`
- `process_tree_cleanup_failed`
- `stdout_limit_exceeded`
- `stderr_limit_exceeded`
- `event_line_limit_exceeded`
- `output_decode_failed`
- `artifact_write_failed`
- `malformed_stream`
- `final_output_missing`
- `unknown_infrastructure_failure`

Cancellation has distinct `cancelled`/`timed_out` infrastructure states and
records its origin, partial output, raw events, cleanup outcome, forced/graceful
termination, and whether the target repository was writable. It is not mapped
to a coding rejection or verifier score in this milestone.

## Versioned schemas

Added normative root schemas, packaged Villani Ops mirrors, Python models,
Run Model TypeScript mirrors, and Flight Recorder validation/type mirrors for:

- `villani.cli_invocation.v1`
- `villani.cli_process_result.v1`
- `villani.cli_output_tail.v1`

The shared valid-run fixture includes all three records. Existing run schemas
remain readable; no existing field was made required. The three new root/package
schema pairs are byte-identical, and the repository's all-schema semantic parity
test passes for all 45 v1 schemas.

## Public command and migration changes

None. No command, login flow, quota surface, agent-system migration, controller
factory, or delivery behavior changed in Milestone 2.

## Files

### Added

- `components/villani-ops/villani_ops/closed_loop/cli_runtime/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/cli_runtime/cancellation.py`
- `components/villani-ops/villani_ops/closed_loop/cli_runtime/environment.py`
- `components/villani-ops/villani_ops/closed_loop/cli_runtime/models.py`
- `components/villani-ops/villani_ops/closed_loop/cli_runtime/process_tree.py`
- `components/villani-ops/villani_ops/closed_loop/cli_runtime/supervisor.py`
- `components/villani-ops/villani_ops/tests/fixtures/cli_runtime/fake_cli.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_cli_runtime.py`
- `schemas/v1/cli-invocation.schema.json`
- `schemas/v1/cli-process-result.schema.json`
- `schemas/v1/cli-output-tail.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/cli-invocation.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/cli-process-result.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/cli-output-tail.schema.json`
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_001/agent/invocation.json`
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_001/agent/process-result.json`
- `integration/fixtures/protocol/v1/valid_run/attempts/attempt_001/agent/output-tail.json`
- `docs/CLI_AGENT_MODE_M2_COMPLETION_REPORT.md`
- `docs/CLI_AGENT_MODE_M2_COMPLETION_REPORT.json`

### Changed

- `components/villani-ops/villani_ops/closed_loop/schema_validation.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py`
- `tests/closed_loop/test_protocol_contract.py`
- `components/villani-run-model/src/agentSystem.ts`
- `components/villani-run-model/dist/agentSystem.js`
- `components/villani-run-model/dist/agentSystem.d.ts`
- `components/villani-run-model/test/agentSystem.test.ts`
- `components/villani-flight-recorder/src/providers/villaniProtocol.ts`
- `components/villani-flight-recorder/src/providers/villaniSchemaValidation.ts`
- `components/villani-flight-recorder/dist/providers/villaniSchemaValidation.js`
- `components/villani-flight-recorder/test/villaniProtocol.test.ts`
- `PLANS.md` (progress section only)

### Removed or migrated

None. The dirty Milestone 1 baseline and unrelated user virtual environments
were preserved.

## Tests added

The 41 passing Windows cases cover the requested 23 categories plus valid and
malformed JSONL, strict/replacement UTF-8, early stdout close, missing final
output, non-runnable executable, spawn failure, stdin pipe failure, mid-stream
artifact failure, secret argument redaction, parent-service cancellation,
output after cancellation, unrelated-process safety, schema validation, and
Windows environment-key behavior. The fake can additionally attempt an
outside-working-directory write for later isolation milestones.

## Command evidence

Authoritative successful commands:

| Command | Exact result |
|---|---|
| Villani Ops `python -m pytest -q` | `1352 passed, 4 skipped, 116 deselected in 309.11s` |
| New runtime test file | `41 passed, 2 skipped in 8.29s` |
| Child-tree test, three repeated final-code invocations | `5 passed in 1.86s`; `5 passed in 1.89s`; `5 passed in 1.89s` (15/15) |
| Existing heartbeat test repeated after transient host failure | `1 passed in 1.14s`; `1 passed in 1.11s`; `1 passed in 1.15s` (3/3) |
| Existing cancellation/cleanup selection | `7 passed in 1.99s` |
| agentd full suite | `87 passed in 22.62s` |
| Root closed-loop/API E2E | `11 passed, 2 warnings in 49.52s` |
| Villani Ops protocol/schema | `23 passed in 1.23s` |
| Root protocol contract | `2 passed, 1 cache warning in 0.93s` |
| Run Model `npm test` | `6 files passed, 17 tests passed` |
| Run Model typecheck/build | both exit 0 |
| Flight Recorder `npm test` | `21 files passed, 118 tests passed` |
| Flight Recorder typecheck/build/format | all exit 0; Prettier matched all files |
| Scoped Ruff check/format | all checks passed; 10 files already formatted |
| Scoped mypy | `Success: no issues found in 7 source files` |
| Python 3.11 compileall | `PYTHON_3_11_COMPILE=PASS` |
| Static runtime scan | `SAFE_EXEC_CALLS=2`, `FORBIDDEN_RUNTIME_MATCHES=0`, `CONTROLLER_PROVIDER_IMPORT_MATCHES=0` |
| New schema byte-pair check | all 3 `True` |
| `git diff --check` | exit 0 |

The required red regression initially exited 1 during collection with
`ModuleNotFoundError: No module named 'villani_ops.closed_loop.cli_runtime'`.
After implementation, the boundary test passed. Development-only reruns also
found and fixed Windows console encoding in the fake, grace-window assertions,
streaming redaction across read boundaries, incomplete artifact-set status,
and a fixture final-output path. Initial Ruff, mypy, and Prettier checks found
only new-code formatting/type issues and all final checks are green.

A post-audit full Ops pass first recorded `1351 passed, 4 skipped, 116
deselected, 1 failed in 297.68s`: the existing
`test_villani_code_runner_timeout_kills_child_process_group` stopped seeing a
OneDrive-hosted heartbeat advance before its production timeout began. The
unchanged test immediately passed three isolated repetitions (3/3), and the
next complete suite passed 1,352/1,352 selected tests in 309.11 seconds. The
new-runtime focused suite remained green. The post-audit Ruff format check also
correctly requested formatting for `supervisor.py`; Ruff formatted that file,
and the final lint, format, and mypy checks passed.

Two test attempts before the authoritative runs failed to allocate sandboxed
temporary directories under the user temp and `C:\tmp`; all tests were rerun
with the workspace-owned `.m2-test-temp` base. A strict byte hash over every
historical schema pair also reported ten pre-existing line-ending differences;
the official semantic parity test is green and all three Milestone 2 pairs are
byte-identical. An initial Python 3.11 launcher attempt could not access the
installed runtime, and a second could not write the workspace bytecode cache;
the approved exact interpreter with `C:\tmp` bytecode storage passed.

## Remaining compatibility risks and assumptions

- Windows x86_64 is locally proved. POSIX implementation and path/permission
  cases are present but the two opposite-platform assertions skip on Windows;
  actual Linux/macOS process-group behavior remains for CI to prove.
- The hosted Windows test process did not expose an interactive console in
  which a fake signal handler could prove receipt of `CTRL_BREAK_EVENT`.
  The runtime did prove that it requests the signal, waits the grace window,
  avoids force when the process exits in that window, and force-cleans an
  ignored signal plus descendants through the Job Object.
- Artifact redaction guarantees cover registered secret values and values named
  by invocation redaction keys. Arbitrary repository output not identified as
  secret remains governed run evidence, consistent with existing local-first
  artifact policy.
- Artifact-write failure can make the full artifact set physically impossible;
  the returned result then reports `artifact_write_failed` and
  `artifact_set_complete=false` rather than claiming complete evidence.

## Milestone boundary confirmation

Milestone 3 was not started. No Codex driver, Claude Code driver, provider role
adapter, provider command builder, provider authentication flow, or live
provider invocation was added or executed.
