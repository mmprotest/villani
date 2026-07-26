# Codex CLI compatibility fix

Date: 2026-07-26

Status: COMPLETE

## Outcome

Villani now supports the installed `codex-cli 0.144.6` with ChatGPT login and the
Windows npm launcher at `%APPDATA%\npm\codex.cmd`.

The final clean-runtime doctors report:

```json
[
  {
    "system_id": "codex-luna-medium",
    "status": "READY",
    "selectable": true,
    "supported_roles": ["coding"],
    "authentication_status": "ready",
    "unattended_safe_execution": true,
    "approval_strategy": "config_override",
    "probe_used_model": false,
    "exact_next_action": "No action required."
  },
  {
    "system_id": "codex-sol-max",
    "status": "READY",
    "selectable": true,
    "supported_roles": ["classification", "verification", "selection"],
    "authentication_status": "ready",
    "unattended_safe_execution": true,
    "approval_strategy": "config_override",
    "probe_used_model": false,
    "exact_next_action": "No action required."
  }
]
```

The deterministic compatibility suite, the affected targeted matrix, the complete
Villani Ops suite, root closed-loop integration, changed-file quality gates, clean
installation, real Codex coding invocation, and independent real Codex verifier
invocation all passed.

## Root cause

The failure had four related causes:

1. Villani treated `noninteractive_approval` as the presence of one spelling,
   `--ask-for-approval`, in one help screen. In Codex CLI 0.144.6 that option is a
   global option, so it appears in `codex --help` and not in
   `codex exec --help`.
2. The old command builder treated all options as `exec` options. That could place
   a global `--ask-for-approval never` after `exec`, even though Codex requires the
   option before the subcommand.
3. Doctor used the narrowly parsed flag field as a hard eligibility requirement
   and produced a generic npm-upgrade action. It could therefore report that all
   safe execution checks passed while still marking the system unsupported.
4. Several CLI role adapters validated an exact singleton role set. A system
   advertising more than one role was rejected even when the requested role was
   explicitly present.

The installed CLI already exposed two safe unattended mechanisms:

- a strict configuration override accepted by
  `codex -c 'approval_policy="never"' --strict-config exec --help`; and
- a global `codex --ask-for-approval never exec --help` option.

No Codex version was special-cased. The selected approval strategy is determined
by bounded behavior probes, not by the version number.

## Responsible implementation locations

These were the implementation paths identified before editing:

| Responsibility | Path |
| --- | --- |
| Codex help parsing | `components/villani-ops/villani_ops/closed_loop/codex_cli/driver.py` |
| Codex capability probing and normalization | `components/villani-ops/villani_ops/closed_loop/codex_cli/driver.py`, `codex_cli/models.py` |
| Codex invocation construction | `components/villani-ops/villani_ops/closed_loop/codex_cli/driver.py` |
| Coding attempt adapter | `components/villani-ops/villani_ops/closed_loop/codex_cli/attempt.py` |
| Role capability requirements and driver factories | `components/villani-ops/villani_ops/closed_loop/agent_systems/registry.py` |
| Agent-system doctor results | `components/villani-ops/villani_ops/closed_loop/agent_systems/management.py`, `cli/unified.py` |
| Repair-action generation | `components/villani-ops/villani_ops/closed_loop/agent_systems/management.py` |
| Classifier role validation | `components/villani-ops/villani_ops/closed_loop/cli_classification/adapter.py` |
| Verifier role validation | `components/villani-ops/villani_ops/closed_loop/cli_verification/adapter.py` |
| Selector role validation | `components/villani-ops/villani_ops/closed_loop/cli_selection/adapter.py` |
| Execution-profile binding validation | `components/villani-ops/villani_ops/closed_loop/agent_systems/role_registry.py`, `agent_systems/registry.py` |
| Claude CLI shared role compatibility | `components/villani-ops/villani_ops/closed_loop/claude_code_cli/driver.py`, `claude_code_cli/attempt.py` |

`role_registry.py` was inspected but did not require modification. Its profile
model already permits the same system ID in more than one binding; the rejecting
logic was in the downstream driver/adapters and factory construction.

## Old and new capability logic

### Old logic

The legacy decision effectively reduced safety support to:

```text
does this parsed help text contain --ask-for-approval?
```

The serialized field was:

```json
{
  "noninteractive_approval": false
}
```

That describes a CLI spelling, not the safety property Villani needs.

### New semantic logic

The normalized capability is:

```json
{
  "unattended_safe_execution": true,
  "approval_strategy": "config_override",
  "approval_policy": "never",
  "probe_used_model": false
}
```

It means Villani has behaviorally proved that it can launch one bounded Codex
process with:

- approval policy explicitly set to `never`;
- an explicit role-specific sandbox policy;
- no approval or sandbox bypass;
- Villani-owned timeout, process-tree cancellation, output capture, and cleanup.

`safe_editing`, role support, system selectability, and final doctor status all
consume this same normalized capability. The previous
`safe_editing=PASS`/approval-capability-fail contradiction is therefore no longer
representable by the doctor projection.

The legacy `noninteractive_approval` field remains present for compatibility.
When an old v1 probe record contains it, Pydantic load normalization derives
`unattended_safe_execution`, `approval_policy="never"`, and the historical global
flag strategy in memory. It does not rewrite the old evidence or change its
schema version.

## Approval strategies

Villani probes and selects strategies in this order:

1. **Strict config override**

   ```text
   codex -c approval_policy="never" --strict-config exec --help
   ```

   This is an argv list. Success requires exit code zero from strict
   configuration parsing. stdout and stderr are captured, bounded, redacted, and
   represented by limited excerpts plus hashes. Timeout, an unknown key, or any
   nonzero result fails the strategy closed. `exec --help` does not invoke a
   model and `probe_used_model` is recorded as `false`.

2. **Validated global flag**

   ```text
   codex --ask-for-approval never exec --help
   ```

   The global help must advertise the option and the bounded placement probe
   must exit zero. Generated invocations put the flag before `exec`.

3. **Unsupported**

   If neither probe succeeds, `unattended_safe_execution=false`; the affected
   roles are not selectable.

The probes also normalize ANSI control sequences and CRLF/CR line endings before
parsing. Version output is retained as evidence, but approval support is never
inferred from the version.

## Invocation construction

Global options and `exec` options are now built separately. Approval, reasoning
effort, web-search policy, scoped permission profiles, and `--strict-config`
precede `exec`. Ephemeral mode, JSONL, model, workspace, output schema, final
message, sandbox and stdin marker are `exec` options.

Windows `.cmd` launchers use the repository's safe argv launcher:

```text
C:\WINDOWS\system32\cmd.exe /d /c call <resolved codex.cmd> <individual argv elements>
```

Villani does not use `shell=True` or a shell command string. Unix and macOS
executables continue to be invoked directly. Paths containing spaces remain one
argv element.

No production invocation contains `--full-auto`,
`--dangerously-bypass-approvals-and-sandbox`, or `--yolo`.

### Exact coding argv

This is the exact redacted argv persisted by the successful real coding smoke:

```json
[
  "C:\\WINDOWS\\system32\\cmd.exe",
  "/d",
  "/c",
  "call",
  "C:\\Users\\Simon\\AppData\\Roaming\\npm\\codex.cmd",
  "-c",
  "approval_policy=\"never\"",
  "-c",
  "model_reasoning_effort=\"medium\"",
  "-c",
  "web_search=\"disabled\"",
  "--strict-config",
  "exec",
  "--ephemeral",
  "--json",
  "--model",
  "gpt-5.6-luna",
  "--sandbox",
  "workspace-write",
  "--cd",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\run\\attempts\\attempt_001\\worktree",
  "--output-schema",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\run\\attempts\\attempt_001\\agent\\coder-result.schema.json",
  "--output-last-message",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\run\\attempts\\attempt_001\\agent\\final-output.json",
  "-"
]
```

### Exact classification argv

This exact argv was built, without launching a process, from the final configured
multi-role Sol system:

```json
[
  "C:\\WINDOWS\\system32\\cmd.exe",
  "/d",
  "/c",
  "call",
  "C:\\Users\\Simon\\AppData\\Roaming\\npm\\codex.cmd",
  "-c",
  "approval_policy=\"never\"",
  "-c",
  "model_reasoning_effort=\"max\"",
  "-c",
  "web_search=\"disabled\"",
  "-c",
  "default_permissions=\"villani_verifier_read_only\"",
  "-c",
  "permissions.villani_verifier_read_only.filesystem={\":minimal\"=\"read\",\":workspace_roots\"={\".\"=\"read\"}}",
  "-c",
  "permissions.villani_verifier_read_only.network.enabled=false",
  "-c",
  "allow_login_shell=false",
  "--strict-config",
  "exec",
  "--ephemeral",
  "--json",
  "--model",
  "gpt-5.6-sol",
  "--skip-git-repo-check",
  "--cd",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\role-argv-evidence\\classification",
  "--output-schema",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\role-argv-evidence\\classification\\input\\classification-result.schema.json",
  "--output-last-message",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\role-argv-evidence\\classification\\output\\classification-result.json",
  "--ignore-user-config",
  "--ignore-rules",
  "-"
]
```

### Exact verification argv

This is the exact redacted argv persisted by the successful independent real
verifier smoke:

```json
[
  "C:\\WINDOWS\\system32\\cmd.exe",
  "/d",
  "/c",
  "call",
  "C:\\Users\\Simon\\AppData\\Roaming\\npm\\codex.cmd",
  "-c",
  "approval_policy=\"never\"",
  "-c",
  "model_reasoning_effort=\"max\"",
  "-c",
  "web_search=\"disabled\"",
  "-c",
  "default_permissions=\"villani_verifier_read_only\"",
  "-c",
  "permissions.villani_verifier_read_only.filesystem={\":minimal\"=\"read\",\":workspace_roots\"={\".\"=\"read\"}}",
  "-c",
  "permissions.villani_verifier_read_only.network.enabled=false",
  "-c",
  "allow_login_shell=false",
  "--strict-config",
  "exec",
  "--ephemeral",
  "--json",
  "--model",
  "gpt-5.6-sol",
  "--skip-git-repo-check",
  "--cd",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\run\\verification\\vfy_93b66ffcf35747fa9c192ffc2a6e426c",
  "--output-schema",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\run\\verification\\vfy_93b66ffcf35747fa9c192ffc2a6e426c\\input\\verifier-result.schema.json",
  "--output-last-message",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\run\\verification\\vfy_93b66ffcf35747fa9c192ffc2a6e426c\\output\\verifier-result.json",
  "--ignore-user-config",
  "--ignore-rules",
  "-"
]
```

### Exact selection argv

This exact argv was built, without launching a process, from the final configured
multi-role Sol system:

```json
[
  "C:\\WINDOWS\\system32\\cmd.exe",
  "/d",
  "/c",
  "call",
  "C:\\Users\\Simon\\AppData\\Roaming\\npm\\codex.cmd",
  "-c",
  "approval_policy=\"never\"",
  "-c",
  "model_reasoning_effort=\"max\"",
  "-c",
  "web_search=\"disabled\"",
  "-c",
  "default_permissions=\"villani_verifier_read_only\"",
  "-c",
  "permissions.villani_verifier_read_only.filesystem={\":minimal\"=\"read\",\":workspace_roots\"={\".\"=\"read\"}}",
  "-c",
  "permissions.villani_verifier_read_only.network.enabled=false",
  "-c",
  "allow_login_shell=false",
  "--strict-config",
  "exec",
  "--ephemeral",
  "--json",
  "--model",
  "gpt-5.6-sol",
  "--skip-git-repo-check",
  "--cd",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\role-argv-evidence\\selection",
  "--output-schema",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\role-argv-evidence\\selection\\input\\selection-result.schema.json",
  "--output-last-message",
  "C:\\VillaniCodexCompatibilitySmoke\\real-smoke-pass\\role-argv-evidence\\selection\\output\\selection-result.json",
  "--ignore-user-config",
  "--ignore-rules",
  "-"
]
```

The two model names above are values from the explicitly requested temporary
smoke configuration. They are not generic production defaults.

## Role-specific sandbox matrix

| Role | Sandbox/effective policy | Repository mutation | Process/session |
| --- | --- | --- | --- |
| classification | scoped read-only permission profile; network and login shell disabled | none | fresh ephemeral process and invocation |
| coding | `--sandbox workspace-write`; network disabled | candidate worktree only | fresh ephemeral process and invocation |
| verification | scoped read-only permission profile; immutable evidence workspace; network and login shell disabled | none | fresh ephemeral process and invocation |
| selection | scoped read-only permission profile; network and login shell disabled | none | fresh ephemeral process and invocation |

Codex custom permission profiles and the legacy `--sandbox` option do not compose:
Codex ignores the custom profile when the legacy sandbox flag is supplied. For
read-only roles, the validated custom permission profile is therefore the
effective sandbox and no legacy `--sandbox` argument is emitted. Coding continues
to use explicit `workspace-write`.

Read-role evidence workspaces are intentionally not Git repositories.
`--skip-git-repo-check` is emitted only after `exec` and only after the capability
is present in `exec --help`; it does not weaken filesystem confinement.

## Multi-role system behavior

A CLI agent system may advertise any nonempty subset of classification, coding,
verification, and selection. Every adapter now checks membership:

```python
AgentRole.VERIFICATION in system.roles
```

instead of requiring an exact singleton set.

When a multi-role system is requested for a role, `system.for_role(role)` creates
a role-scoped driver view. It carries only that role's instruction, permission,
and environment policy. Invocation construction then creates:

- a new prompt and stdin payload;
- a role-specific output schema;
- a role-specific workspace and artifact directory;
- a new opaque role invocation ID;
- a new bounded subprocess;
- no resume/session identifier and no prior conversation.

The same configured system ID can appear in multiple execution-profile bindings,
but each binding receives a distinct invocation identity. A non-advertised role
fails before invocation construction.

Verifier input construction remains blind. The real smoke's independence record
confirmed no coder transcript reference, a blind input manifest, distinct
process/session/invocation identities, a nonwritable candidate and target, and no
mutation before or after verification.

## Doctor and repair behavior

### Before

The old report could contain:

```text
authentication: PASS
process_spawn: PASS
structured_output: PASS
cancellation: PASS
path_with_spaces: PASS
artifact_write: PASS
environment_redaction: PASS
safe_editing: PASS
codex_exec: PASS
codex_jsonl: PASS
codex_sandbox: PASS
noninteractive_approval: FAIL
system: UNSUPPORTED
exact_next_action: npm install -g @openai/codex@latest
```

### After

Doctor now reports the selected semantic strategy and uses it for both the
individual safety checks and final role support. Both real configured systems
are `READY`, authentication is `ready`, `probe_used_model=false`, and
`exact_next_action` is the explicit no-action value `No action required.`.

Repair actions are selected from the actual failing check:

- executable resolution failure: install Codex or configure its executable;
- authentication failure: run `codex login`;
- unattended strategy failure: use a CLI supporting strict
  `approval_policy="never"` or validated global `--ask-for-approval never`;
- structured-output failure: use a CLI with `exec --json` and
  `--output-schema`;
- sandbox failure: use a CLI supporting the required read-only and
  workspace-write confinement;
- rejected model: choose a model available to the authenticated account.

A failed help parser alone no longer creates an upgrade recommendation.

## Strict output-schema compatibility

The real smoke found that Codex's strict response-format validator requires an
explicit type beside `const`/`enum` and rejects `uniqueItems` in this schema
subset. The generic coder, classifier, verifier, and selector schemas were
corrected in both the normative root and packaged copies:

- `schema_version` constants and enum values have explicit string types;
- unsupported `uniqueItems` keywords were removed;
- object properties remain closed and required.

A deterministic schema-subset and root/package parity regression now prevents
this from recurring.

## Migration and compatibility

- Existing API-agent execution was not changed.
- Claude Code retains its invocation path; only shared multi-role membership and
  role-scoped driver behavior changed.
- Existing role-only Codex and Claude systems continue to work.
- Existing profiles with separate system IDs continue to work.
- Profiles may now bind one system ID to several advertised roles.
- The Codex probe schema remains `villani.codex_probe.v1`.
- Legacy `noninteractive_approval` evidence is normalized on read and is not
  rewritten.
- Diagnostic fields are additive and have backward-compatible model defaults.
- Root schema contracts remain normative and are mirrored in package data.
- No credentials, environment values, or credential-file contents are persisted.

## Files changed

### Production

- `components/villani-ops/villani_ops/cli/unified.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/management.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/registry.py`
- `components/villani-ops/villani_ops/closed_loop/claude_code_cli/attempt.py`
- `components/villani-ops/villani_ops/closed_loop/claude_code_cli/driver.py`
- `components/villani-ops/villani_ops/closed_loop/cli_classification/adapter.py`
- `components/villani-ops/villani_ops/closed_loop/cli_selection/adapter.py`
- `components/villani-ops/villani_ops/closed_loop/cli_verification/adapter.py`
- `components/villani-ops/villani_ops/closed_loop/codex_cli/attempt.py`
- `components/villani-ops/villani_ops/closed_loop/codex_cli/driver.py`
- `components/villani-ops/villani_ops/closed_loop/codex_cli/models.py`

### Normative and packaged schemas

- `schemas/v1/agent-system-diagnostic.schema.json`
- `schemas/v1/codex-coder-result.schema.json`
- `schemas/v1/cli-classifier-result.schema.json`
- `schemas/v1/cli-verifier-result.schema.json`
- `schemas/v1/cli-selector-result.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/agent-system-diagnostic.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/codex-coder-result.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/cli-classifier-result.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/cli-verifier-result.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/cli-selector-result.schema.json`

### Fixtures and tests

- `components/villani-ops/villani_ops/tests/fixtures/codex_cli/fake_codex.py`
- `components/villani-ops/villani_ops/tests/fixtures/codex_cli/fake_codex_0144.py`
- `components/villani-ops/villani_ops/tests/fixtures/cli_roles/fake_codex_roles.py`
- `components/villani-ops/villani_ops/tests/fixtures/cli_verifier/fake_codex_verifier.py`
- `components/villani-ops/villani_ops/tests/test_codex_cli_compatibility.py`
- `components/villani-ops/villani_ops/tests/test_codex_cli_coding.py`
- `components/villani-ops/villani_ops/tests/test_cli_classification_selection.py`
- `components/villani-ops/villani_ops/tests/test_cli_agent_mode_m7.py`

### Completion records

- `docs/completion/codex-cli-compatibility-fix.md`
- `PLANS.md` progress section only

No other component was edited.

## Tests added

`fake_codex_0144.py` deterministically reproduces the installed CLI's scope:
global help advertises `--ask-for-approval`, `exec --help` omits it, strict config
accepts `approval_policy="never"`, and version output is
`codex-cli 0.144.6`. Fixture switches cover strict-config rejection, global-flag
rejection, timeout, missing capabilities, authentication failure, ANSI/CRLF help,
Windows launcher behavior, and a marker that would reveal any model invocation.

`test_codex_cli_compatibility.py` adds 25 tests. Together with the existing
agent-system, adapter, process-supervisor, verifier-blindness, profile,
migration, evidence, API, Claude, and root integration suites, they cover all 55
numbered deterministic cases in the request:

- semantic capability and fail-closed probe cases;
- global/config strategy ordering and preference;
- global-versus-`exec` option placement;
- all four role sandbox policies and required output flags;
- stdin, paths with spaces, Windows `.cmd`, direct Unix-style launch, redaction;
- exact 0.144.6 doctor readiness and repair actions;
- partial support and safe-editing/role-status consistency;
- one system bound to several roles with fresh processes and schemas;
- role-only and separate-system profile compatibility;
- verifier blindness, candidate isolation, malformed-output and cancellation
  fail-closed behavior;
- old probe/config/run evidence readability and schema parity;
- static absence of experiment/model/version special cases.

Every subprocess-facing fixture has an internal timeout or uses the bounded
Villani supervisor.

## Validation commands and exact results

### Baseline discovery

- `git status --short`: exit 0; clean before editing.
- `codex --version`: exit 0; `codex-cli 0.144.6`.
- `codex login status`: exit 0; authenticated through ChatGPT.
- `codex --help`: exit 0; global `--ask-for-approval` present.
- `codex exec --help`: exit 0; global approval flag absent; required structured
  output, sandbox, model, workspace, ephemeral and JSON options present.
- `codex -c 'approval_policy="never"' --strict-config exec --help`: exit 0; no
  model invoked.
- The two requested repository searches completed before editing and identified
  the paths listed above.

### Deterministic tests

- `python -m pytest components/villani-ops/villani_ops/tests/test_codex_cli_compatibility.py -q`:
  exit 0; 25 passed in 7.03 seconds.
- Focused Codex/doctor/profile grouping during implementation: exit 0;
  122 passed, 3 deselected.
- Focused verifier/Claude compatibility grouping during implementation: exit 0;
  83 passed, 3 deselected.
- Focused coding/classification/verification/selection rerun after the
  read-workspace fix: exit 0; 136 passed, 5 deselected in 109.77 seconds.
- Focused audit, blindness, fail-closed, migration and evidence grouping:
  exit 0; 123 passed, 2 skipped in 19.71 seconds.
- Final explicit affected-surface command:

  ```powershell
  python -m pytest `
    components/villani-ops/villani_ops/tests/test_agent_mode_m1.py `
    components/villani-ops/villani_ops/tests/test_agent_mode_m3.py `
    components/villani-ops/villani_ops/tests/test_agent_mode_m4.py `
    components/villani-ops/villani_ops/tests/test_pt5_agent_systems.py `
    components/villani-ops/villani_ops/tests/test_cli_agent_mode_m7.py `
    components/villani-ops/villani_ops/tests/test_codex_cli_coding.py `
    components/villani-ops/villani_ops/tests/test_codex_cli_compatibility.py `
    components/villani-ops/villani_ops/tests/test_cli_classification_selection.py `
    components/villani-ops/villani_ops/tests/test_cli_verification.py `
    -q --basetemp .final-targeted-temp/pytest
  ```

  Exit 0; 215 passed, 5 opt-in tests deselected in 120.54 seconds.

- `python -m pytest components/villani-ops -q --basetemp .final-pytest-temp/pytest`:
  exit 0; 1,568 passed, 4 skipped, 122 deselected in 418.49 seconds.
- `python -m pytest tests/closed_loop -q --basetemp .final-integration-temp/pytest`:
  final exit 0; 11 passed, 1 existing Starlette/httpx deprecation warning in
  43.37 seconds.

The first root-integration invocation used a nonexistent nested basetemp parent
and ended at setup with 6 passed and 5 `FileNotFoundError` errors. After creating
the explicit parent, the identical suite passed 11/11. A schema-focused
intermediate run similarly reused an inaccessible temp root (16 passed, 9 setup
errors); its clean-root rerun passed 25/25. Neither was a product failure.

### Formatter, lint, type, compile, dependency and diff checks

- Component-wide `python -m ruff format --check components/villani-ops/villani_ops`:
  exit 1; identified 74 pre-existing files outside this change that Ruff would
  reformat. Those unrelated files were preserved.
- `python -m ruff format --check -- <19 changed Python files>`: exit 0;
  19 files already formatted.
- `python -m ruff check -- <19 changed Python files>`: exit 0; all checks passed.
- Broad `python -m mypy components/villani-ops/villani_ops`: existing baseline
  exit 1; 679 errors in 111 files, 451 files checked.
- `python -m mypy --follow-imports=skip --ignore-missing-imports <11 changed production files>`:
  exit 0; no issues in 11 source files.
- Initial `python -m compileall components/villani-ops`: exit 1 while printing a
  generated test path containing `雪` to the CP1252 console; no syntax diagnostic.
- `$env:PYTHONUTF8='1'; python -m compileall components/villani-ops`: exit 0.
- `C:\Users\Simon\AppData\Local\Programs\Python\Python311\python.exe -m compileall -q <11 changed production files>`:
  exit 0; confirms Python 3.11 syntax compatibility.
- `python -m pip check`: exit 0; no broken requirements.
- `git diff --check`: exit 0; no whitespace errors; Git emitted existing
  LF-to-CRLF working-tree warnings.
- Production special-case scan: exit 0; no calibration/compatibility smoke path,
  requested smoke model, or Codex 0.144.6 special case in production.
- Production role-set scan: exit 0; no exact singleton-role set validation.
- Production safety-flag scan: exit 0; no bypass, yolo, or full-auto flag.

All workspace-local test directories created in this pass were removed after
validation.

## Clean installation and real Codex smoke

The requested official installer command completed successfully in 75.6 seconds:

```powershell
python "C:\Users\Simon\OneDrive\Documents\Python Scripts\villani\scripts\install-local.py" `
  --venv "C:\VillaniCodexCompatibilitySmoke\villani-runtime"
```

Build, installation, import, executable and `pip check` stages passed. The
installer's intentional editable mapping points `villani_ops` at this repository,
so the final doctors and passing smoke used the final source state.

The two systems and `codex-compatibility` profile were created through public
configuration interfaces. Final bounded doctor commands:

```powershell
$env:VILLANI_HOME = "C:\VillaniCodexCompatibilitySmoke\home"
& "C:\VillaniCodexCompatibilitySmoke\villani-runtime\Scripts\villani.exe" `
  agents doctor codex-luna-medium --json
& "C:\VillaniCodexCompatibilitySmoke\villani-runtime\Scripts\villani.exe" `
  agents doctor codex-sol-max --json
```

Both exited 0 and produced the READY results at the top of this report. Doctor
modified no repository, started no login, changed no provider configuration, and
invoked no model.

The real coding smoke used the normal Codex coding adapter against a disposable
Git repository. It:

- launched PID 22680 as invocation `attempt_001`;
- used `workspace-write`, no network, no resume, and config-override approval;
- exited 0 with cleanup `succeeded`;
- produced nonempty JSONL, schema-constrained final output, and a nonempty Git
  patch adding the trivial local compatibility marker.

The separate real verifier:

- launched PID 24320 as invocation
  `vfy_93b66ffcf35747fa9c192ffc2a6e426c`;
- used a distinct session, invocation ID, artifact directory and prompt;
- used scoped read-only enforcement, no network, no login shell, no resume, and
  no candidate/target write access;
- exited 0 with cleanup `succeeded`;
- produced JSONL and parsed normalized structured output;
- left the source, target and candidate hashes unchanged.

Neither PID remained alive after the smoke. The verifier returned `unclear` and
`acceptance_eligible=false` because the deliberately minimal disposable task had
no authoritative validation command. That is a semantic fail-closed result, not
an infrastructure failure, and proves that the smoke did not loosen acceptance
eligibility.

### Real smoke evidence

- Summary:
  `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\real-smoke-summary.json`
- Coding invocation:
  `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\run\attempts\attempt_001\agent\invocation.json`
- Coding events:
  `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\run\attempts\attempt_001\agent\codex-events.jsonl`
- Coding final output:
  `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\run\attempts\attempt_001\agent\final-output.json`
- Nonempty candidate patch:
  `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\run\attempts\attempt_001\repository\candidate.patch`
- Verifier invocation:
  `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\run\verification\vfy_93b66ffcf35747fa9c192ffc2a6e426c\agent\invocation.json`
- Verifier events:
  `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\run\verification\vfy_93b66ffcf35747fa9c192ffc2a6e426c\agent\raw-events.jsonl`
- Verifier normalized result:
  `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\run\verification\vfy_93b66ffcf35747fa9c192ffc2a6e426c\output\normalized-result.json`
- Final Luna doctor:
  `C:\VillaniCodexCompatibilitySmoke\doctor-luna-medium.json`
- Final Sol doctor:
  `C:\VillaniCodexCompatibilitySmoke\doctor-sol-max.json`

Two real-smoke discoveries were fixed generically before the passing run:

- the first Codex coding call rejected an output schema lacking explicit types
  beside strict `const`/`enum` constraints;
- the first verifier call rejected its deliberately non-Git evidence workspace
  until Villani detected and emitted the supported `--skip-git-repo-check`
  `exec` option.

Their retained diagnostic evidence is:

- `C:\VillaniCodexCompatibilitySmoke\real-smoke\real-smoke-summary.json`
- `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\verifier-trust-precondition-failure.json`
- `C:\VillaniCodexCompatibilitySmoke\real-smoke-pass\reconstruction-preflight-failure.json`

## Known limitations and remaining risks

- The authenticated real smoke proves Windows/npm `.cmd` behavior. POSIX direct
  executable behavior is deterministic-test coverage only in this pass.
- The actual machine selected the preferred config-override strategy. The global
  approval fallback is behaviorally covered by the fake CLI but was not selected
  by the real CLI because the preferred strategy succeeded.
- The broad legacy Villani Ops tree is not globally Ruff-formatted and has the
  disclosed pre-existing mypy baseline. Every changed Python file passes Ruff,
  and every changed production module passes scoped mypy.
- The real verifier correctly remained non-accepting for a smoke task without an
  authoritative validation command. Its process, isolation, immutability and
  structured-output contracts passed.

There are no remaining failures in the affected test or quality-gate scope.

## Scope confirmation

- Only Villani product source, tests, schemas, this completion record, and the
  `PLANS.md` progress section were changed.
- `C:\VillaniCodexCalibrationSmoke` was not modified.
- No experiment ZIP or experiment script was modified.
- No task ID, benchmark name, repository name, requested smoke model, or Codex
  0.144.6 special case was added to generic production logic.
- No approval/sandbox bypass, yolo, full-auto behavior, disabled sandbox, shell
  command string, persistent Codex session, verifier conversation reuse, or
  weakened acceptance rule was added.
- No later milestone or 35-task experiment was started.
