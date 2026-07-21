# Villani CLI Agent Mode Milestone 1 Completion Report

Status: **COMPLETE**  
Date: 2026-07-20  
Milestone: Neutral Agent-System Configuration and Role Binding

## Outcome

Villani still has one deterministic `ClosedLoopController`. Every controller role now resolves through a neutral `RoleSystemRegistry` and a role-specific factory. Existing API/Villani Code behavior remains green. Codex and Claude Code CLI systems can be parsed, validated, listed, and inspected, but are deliberately unavailable for execution and never fall back to API.

Milestone 2 was not started. No Codex CLI or Claude Code CLI process was launched, no login or quota flow was added, and no subprocess driver runtime was implemented.

## Architecture note: exact construction paths

### Construction path before this milestone

Public CLI:

`villani_distribution.cli:app` (imports `villani_ops.cli.unified:app`)  
→ `unified.run_command`  
→ `unified._execute_new_run`  
→ `unified.build_controller`  
→ `_ClassifierAdapter` for classification  
→ `AgentSystemRegistry.attempt_runner()` → `AgentSystemAttemptRunner` → the selected legacy harness adapter, normally `VillaniCodeHarnessAdapter` → `VillaniCodeAttemptAdapter` for coding  
→ `VillaniVerifierAdapter`, optionally wrapped by `VerifierCascade` or `VerificationGraphVerifierAdapter`, for verification  
→ `EvidenceSelectorAdapter` for selection  
→ built-in typed plugin wrappers  
→ the single `ClosedLoopController`.

Service/agentd:

`villani_agentd.console.ConsoleService` lazily imported `villani_ops.cli.unified.build_controller` for submit, cancel, approval, and recovery operations, then used the same path and controller.

### Construction path after this milestone

Public CLI and agentd still converge on the same `unified.build_controller`:

`villani run` or `ConsoleService`  
→ `migrate_agent_system_configuration`  
→ `_load_backends`  
→ `build_agent_system_registry`  
→ `AgentSystemRegistry.role_registry` (`RoleSystemRegistry`)  
→ resolve and validate the selected `RoleBindings` profile  
→ construct the existing concrete classifier, attempt runner, verifier, selector, and materializer dependencies  
→ register those dependencies in `RoleFactoryDependencies`  
→ `build_classifier`, `build_attempt_runner`, `build_verifier`, and `build_selector`  
→ the existing typed plugin wrappers  
→ the same single `ClosedLoopController`.

The migrated default profile resolves as follows:

| Role | Resolved system | Factory result |
|---|---|---|
| classification | `api-<backend-id>` | Existing `_ClassifierAdapter`, bound to that backend reference |
| coding | `villani-code-runner` | Existing `AgentSystemAttemptRunner` → `VillaniCodeHarnessAdapter` → `VillaniCodeAttemptAdapter`/current compatible runner |
| verification | `villani-verifier` | Existing `VillaniVerifierAdapter`, cascade, or verification-graph adapter |
| selection | `evidence-selector` | Existing `EvidenceSelectorAdapter` |

Provider and driver selection is outside controller source. The controller has no Codex or Claude driver import. The only additive controller construction datum is a provider-neutral classification backend reference so the resolved API classifier identity and actual classifier backend agree.

## Architectural decisions

- Preserved the four typed controller ports: `Classifier`, `AttemptRunner`, `Verifier`, and `Selector`.
- Added one lower-level neutral registry used by four role-specific factories; no generic untyped agent callback was introduced.
- Preserved the existing rich, content-addressed `villani.agent_system.v1` coding/harness identity. Because that name was already normative, the new executable-system catalog uses the repository-compatible name `villani.agent_system_config.v1` rather than redefining the existing contract.
- Kept candidate count, retry, escalation, verification eligibility, selection, and delivery as controller/orchestration concerns.
- Generated API migration profiles retain current classifier choice and use existing Villani Code, verifier, and selector implementations. Explicit custom profiles are never silently rerouted.
- Neutral CLI configurations are static in Milestone 1. Their availability is `configured`/unavailable; resolution raises an actionable no-fallback error before a run.
- Agent-system configuration and invocation identities reject secret-valued fields. Environment/reference names may be retained; secret values are never projected into identities.
- New role bindings and per-role invocation identities are persisted in the canonical run bundle and referenced from the additive run-manifest fields. Old fields remain optional.

## Versioned schemas and migrations

Added normative and packaged mirrors for:

- `villani.agent_system_config.v1`: discriminated `api`, `internal_runner`, and `cli_agent` system catalog.
- `villani.role_bindings.v1`: complete mapping for classification, coding, verification, and selection.
- `villani.agent_invocation_identity.v1`: secret-free resolved system/implementation identity per role.

The v1 run-manifest schema now optionally records:

- `execution_profile_id`
- `role_bindings`
- `agent_invocation_ids`
- artifact paths for `role-bindings.json` and the invocation index

Migration behavior:

- Legacy backend entries and legacy coding harness routes remain readable and preserved.
- `classification`, `coding`, legacy `review`, and `selection` map to canonical `classification`, `coding`, `verification`, and `selection` roles.
- Migration generates neutral API systems plus the current internal coding, verification, and selection systems.
- The generated `api` profile is a versioned binding document with a migration marker, allowing safe refresh when a backend changes or is removed.
- Generated IDs are deterministic and collision-safe; migration is idempotent.
- Stale migration-owned systems are removed, while explicit user profiles are not silently redirected.
- Malformed legacy backend values produce a field-level path such as `backends.<id>.<field>`.
- Backend secret values are omitted from neutral systems and invocation identities.

## Public command changes

- `villani agents list`: retains legacy rich coding identities and adds neutral configured systems with static readiness.
- `villani agents inspect <system-id>`: inspects a neutral system without invoking it.
- `villani profiles list`: lists profile validity/readiness.
- `villani profiles inspect <profile-id>`: shows resolved role bindings and unavailable reasons.
- `villani run --execution-profile <profile-id>`: selects a profile; an unavailable CLI profile fails closed and explicitly refuses fallback.

No login, authentication setup, external CLI execution, or quota command was added.

## Files

### Added

- `docs/CLI_AGENT_MODE_M1_COMPLETION_REPORT.md`
- `docs/CLI_AGENT_MODE_M1_COMPLETION_REPORT.json`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/factories.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/role_models.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/role_registry.py`
- `components/villani-ops/villani_ops/schemas/v1/agent-system-config.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/role-bindings.schema.json`
- `components/villani-ops/villani_ops/schemas/v1/agent-invocation-identity.schema.json`
- `components/villani-ops/villani_ops/tests/test_agent_mode_m1.py`
- `schemas/v1/agent-system-config.schema.json`
- `schemas/v1/role-bindings.schema.json`
- `schemas/v1/agent-invocation-identity.schema.json`
- `integration/fixtures/protocol/v1/valid_run/agent-system-config.json`
- `integration/fixtures/protocol/v1/valid_run/role-bindings.json`
- `integration/fixtures/protocol/v1/valid_run/agent-invocation-identity.json`

### Changed

- `PLANS.md` (progress section only)
- `components/villani-agentd/tests/test_console.py`
- `components/villani-flight-recorder/dist/providers/villani.js`
- `components/villani-flight-recorder/dist/providers/villaniSchemaValidation.js`
- `components/villani-flight-recorder/src/providers/villani.ts`
- `components/villani-flight-recorder/src/providers/villaniProtocol.ts`
- `components/villani-flight-recorder/src/providers/villaniSchemaValidation.ts`
- `components/villani-flight-recorder/test/villaniProtocol.test.ts`
- `components/villani-ops/villani_ops/cli/unified.py`
- `components/villani-ops/villani_ops/closed_loop/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/configuration.py`
- `components/villani-ops/villani_ops/closed_loop/agent_systems/registry.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/protocol.py`
- `components/villani-ops/villani_ops/closed_loop/schema_validation.py`
- `components/villani-ops/villani_ops/schemas/v1/run-manifest.schema.json`
- `components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py`
- `components/villani-ops/villani_ops/tests/test_pt5_agent_systems.py`
- `components/villani-run-model/dist/agentSystem.d.ts`
- `components/villani-run-model/dist/agentSystem.js`
- `components/villani-run-model/dist/types.d.ts`
- `components/villani-run-model/src/agentSystem.ts`
- `components/villani-run-model/src/types.ts`
- `components/villani-run-model/test/agentSystem.test.ts`
- `schemas/v1/run-manifest.schema.json`
- `tests/closed_loop/test_protocol_contract.py`

### Removed or statically migrated

- Removed: none.
- Statically migrated files: none. Configuration migration occurs in memory and when existing configuration-management paths persist an updated configuration.
- Pre-existing untracked `.venv-founder-evidence-matrix-v1/` and `.venv-wedge-experiment-v7/` were not modified.

## Tests added

`test_agent_mode_m1.py` contains 26 tests covering discriminated parsing, invalid kinds and IDs, duplicate IDs, unknown/disabled/role-incompatible bindings, API/CLI/hybrid profiles, CLI unavailability/no fallback, current-config migration, stale generated-system cleanup, refresh and idempotence, exact malformed field paths, secret-free identity serialization, old-bundle readability, new identity schemas, all four factories, controller composition and provider-import isolation, bound API classification, and read-only CLI inspection.

Existing PT5 bundle tests now prove role-binding and invocation-identity persistence. Python and TypeScript protocol tests validate the three new fixtures and old optional-field behavior. Agentd tests verify persisted legacy routes plus neutral systems and cleanup. The existing API end-to-end fixture was not weakened.

## Command ledger and exact results

### Regression-first evidence

| Command | Exact result |
|---|---|
| `python -m pytest -q villani_ops/tests/test_agent_mode_m1.py` before implementation | Exit 1; 1 failed in 1.10s. The old path raised `ValueError: agent system 'codex-classifier' references unknown backend 'codex-classifier'`, proving it could not represent a CLI agent system. |

### Authoritative final validation

| Command | Exact result |
|---|---|
| `python -m pytest -q` in `components/villani-ops` with repository-local `TEMP/TMP` and `--basetemp` | Exit 0; **1,311 passed, 2 skipped, 116 deselected in 370.66s**. |
| `python -m pytest -q` in `components/villani-agentd` | Exit 0; **87 passed in 27.06s**. |
| `python -m pytest -q` in `components/villani` outside the filesystem sandbox for the clean-install Vite build | Exit 0; **77 passed in 233.10s**. |
| `python -m pytest tests/closed_loop -q` at repository root | Exit 0; **11 passed, 2 warnings in 63.22s**. This includes `test_public_cli_two_backend_end_to_end_and_flight_recorder`. |
| `python -m pytest -q tests/closed_loop/test_protocol_contract.py` | Exit 0; **2 passed, 1 cache warning in 0.97s**. |
| `python -m pytest -q components/villani-ops/villani_ops/tests/test_agent_mode_m1.py` | Exit 0; **26 passed in 0.86s**. |
| `npm run build` in `components/villani-run-model` | Exit 0; TypeScript build passed. |
| `npm test` in `components/villani-run-model` | Exit 0; **6 files, 17 tests passed**. |
| `npm run typecheck` in `components/villani-run-model` | Exit 0. |
| `npm test` in `components/villani-flight-recorder` | Exit 0; build passed and **21 files, 118 tests passed**. |
| `npm run typecheck` in `components/villani-flight-recorder` | Exit 0. |
| `npm run format:check` in `components/villani-flight-recorder` | Exit 0; all matched files use Prettier style. |
| Flight Recorder Prettier over the three changed Run Model TypeScript files | Exit 0; all matched files use Prettier style. |
| Ruff `check` over all 16 changed Python files | Exit 0; all checks passed. |
| Ruff `format --check` over all 16 changed Python files | Exit 0; 16 files already formatted. |
| `mypy --follow-imports=skip` over `role_models.py`, `role_registry.py`, `factories.py`, and `configuration.py` | Exit 0; no issues in 4 source files. |
| `mypy --follow-imports=skip --ignore-missing-imports` over `controller.py` and `unified.py` | Exit 0; no issues in 2 source files. |
| `python -m compileall -q` over the changed Ops agent-system/controller/CLI modules | Exit 0. |
| `git diff --check` after report/progress creation | Exit 0; no whitespace errors. |

### Intermediate failures retained as evidence

- Initial full Ops run: 1 failed, 1,307 passed, 2 skipped, 116 deselected. It exposed a disabled legacy Codex route being shadowed by a generated route. The migration now preserves an explicit legacy route; its focused test passed, and subsequent full runs passed.
- Initial agentd full run: 1 failed, 86 passed. Its old assertion expected only two legacy route keys after persistence. The compatibility assertion now verifies preservation plus the new neutral systems/profile and stale-system cleanup; focused and full reruns passed.
- Initial distribution full run: 1 failed, 76 passed. Vite/esbuild received a filesystem-sandbox access denial. The exact test passed outside the sandbox (1 passed in 92.95s), followed by the final full 77-test pass.
- One early combined focused command used the wrong `test_protocol.py` path and collected no tests; the corrected path passed 59 tests, later expanded to 61, and all final tests are included in the 1,311-test authoritative run.
- A first CLI/PT5 combined run without repository-local `TEMP/TMP` produced 7 passes and 52 Windows temp permission errors. All suites were rerun with isolated repository-local temp roots.
- Broad repository/import-graph mypy reported 270 pre-existing errors in 59 files. Four newly introduced findings were corrected. Scoped production-module mypy is green. A strict two-file controller/CLI check then reported only the missing external `yaml` stub; the same check with `--ignore-missing-imports` passed.

Read-only repository inspection used `Get-Content`, `Select-String`, `git grep`, `git status`, and `git diff`. Those commands did not mutate product state. Recursive inspection encountered access-denied warnings for sandboxed pytest-cache/temp paths; targeted reads succeeded.

## Remaining failures, assumptions, and compatibility risks

Remaining product/test failures: **none**.

Assumptions and known risks:

- The two skipped Ops tests and 116 deselected tests are existing opt-in/marker behavior; no paid or external CLI integration was exercised.
- Codex/Claude legacy PT6 runner modules remain in the repository for compatibility, but neutral `cli_agent` entries do not probe or launch them in this milestone.
- CLI readiness is intentionally static and unavailable until the corresponding future drivers and doctor checks exist.
- Explicit custom profiles are preserved and fail actionably if a referenced system disappears; only migration-owned profiles are refreshed automatically.
- The broad repository mypy baseline remains noisy outside the scoped changed modules. No new scoped type errors remain.
- Root pytest emitted an existing Starlette/httpx deprecation warning and pytest-cache permission warnings; neither affected test results.
- No subprocess runtime, provider login, quota probing, or Milestone 2 implementation was started.

## Acceptance confirmation

- One controller remains: confirmed.
- Every role resolves through the neutral registry and its role-specific factory: confirmed.
- Current API behavior remains green: confirmed by the API-mode root end-to-end fixture and all full component suites.
- CLI agent systems are configurable/inspectable but not executable: confirmed.
- New and migrated identities contain no secret values: confirmed by model validation and regression tests.
- Old configurations and run bundles remain readable: confirmed.
- No external CLI was launched: confirmed.
- Milestone 2 was not started: confirmed.
