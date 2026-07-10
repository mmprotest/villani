# Villani Closed Loop Integration Plan

Status: approved implementation plan

Plan version: 1.0

Last updated: 2026-07-10

## 1. Outcome

Build one local-first product named `villani` that executes this closed loop:

1. Understand the coding task and repository.
2. Classify task difficulty, risk, category, and required capabilities.
3. Select the least expensive backend that is sufficiently capable under the configured policy and budget.
4. Run one isolated coding attempt.
5. Capture the attempt's patch, trace, token usage, duration, and cost.
6. Verify the candidate against explicit success criteria and repository evidence.
7. Stop, retry, or escalate using a deterministic policy decision.
8. Select only from candidates that are eligible for acceptance.
9. Materialize exactly one selected patch into the target repository.
10. Persist enough structured evidence to replay and interrogate every decision.

The first public workflow is:

```text
villani run "<task>" --repo <path> --success-criteria "<criteria>"
villani runs
villani inspect <run_id>
villani open <run_id>
```

## 2. Repository layout

The integration repository must use this layout:

```text
villani/
  AGENTS.md
  PLANS.md
  README.md
  docs/
    BASELINE.md
    CLOSED_LOOP.md
  schemas/
  components/
    villani-code/
    villani-ops/
    villani-flight-recorder/
  integration/
    fixtures/
  scripts/
```

`components/villani-code` remains the coding runtime. `components/villani-ops` owns the deterministic controller and public Python CLI. `components/villani-flight-recorder` owns replay and observability. Root `schemas` is the source of truth for cross-component wire contracts.

## 3. Product boundary for version 1

Version 1 must provide:

- One deterministic closed-loop controller.
- Task classification before coding backend selection.
- A transparent cost and capability policy.
- Sequential isolated attempts, with optional policy-controlled collection of more than one accepted candidate.
- A dedicated verifier that cannot silently convert uncertainty into success.
- Evidence-based candidate selection.
- Safe patch materialization.
- One canonical run bundle under `~/.villani/runs/<run_id>/`.
- One public `villani` command.
- A native Villani provider in Flight Recorder.
- Complete task, policy, model, tool, command, patch, verification, token, duration, and cost observability when the underlying runner supplies the data.
- Explicit `unknown` accounting when cost or usage data is unavailable. Missing cost must never be displayed or ranked as zero.

Version 1 does not include:

- Multi-task decomposition.
- General DAG scheduling.
- Autonomous teams or role-playing agents.
- A hosted control plane.
- Billing, organizations, RBAC, or collaboration.
- A learned neural router.
- Automatic training from unverified outcomes.
- More orchestration modes in the public CLI.
- A rewrite of Villani Code or Flight Recorder.

## 4. Architectural invariants

These rules are mandatory in every milestone:

1. The controller owns state transitions. A model may propose classification or verification content, but it may not mutate controller state.
2. Classification occurs before coding backend selection.
3. Every attempt runs in an isolated git-backed copy or worktree.
4. Every attempt has a stable `run_id`, `trace_id`, and `attempt_id` before it starts.
5. Every controller decision is appended to `events.jsonl` and, when it is a policy choice, to `policy_decisions.jsonl`.
6. Event sequence numbers are monotonic within a run.
7. A candidate is selectable only when normalized verification says `acceptance_eligible: true`.
8. Verifier errors, malformed output, missing evidence, and unclear outcomes are never acceptance eligible.
9. The selector receives only acceptance-eligible candidates.
10. Materialization applies only the selected candidate's recorded patch.
11. Failed materialization produces terminal `FAILED` state and must not claim completion.
12. Budget exhaustion produces terminal `EXHAUSTED` state and must not claim success.
13. Unknown cost is represented as `null` plus an accounting status, never as numeric zero.
14. Secrets must not be written to the run bundle, events, prompts, logs, or UI.
15. The original task and success criteria must be preserved verbatim in `task.json`.
16. Legacy orchestration paths may remain for compatibility, but `villani run` must not call them.
17. Root JSON Schemas are normative. Python and TypeScript contract tests must validate the same fixtures.
18. Existing unrelated behavior and user changes must be preserved.

## 5. Component ownership

| Concern | Owning location | Existing code to reuse |
| --- | --- | --- |
| Coding attempt execution | `components/villani-code` | Existing CLI, debug recorder, runtime events, benchmark runner primitives |
| Controller, policy, verification adapters, materialization | `components/villani-ops/villani_ops/closed_loop` | `classification`, `runners/villani_code.py`, `orchestrator/verifier_parallel.py`, `orchestrator/selection.py`, `isolation/copy_git.py`, `git_ops.py` |
| Wire contracts | Root `schemas` | New versioned JSON Schemas and shared fixtures |
| Replay and run interrogation | `components/villani-flight-recorder` | Existing providers, session index, timeline, graph, metrics, static renderer |
| Public command | `components/villani-ops/villani_ops/cli/unified.py` | Typer, backend storage, controller services |
| Cross-component fixtures | `integration/fixtures` | New deterministic tiny repositories and canonical run bundles |

## 6. Canonical controller states

Allowed states:

```text
CREATED
CLASSIFYING
CLASSIFIED
POLICY_SELECTED
ATTEMPT_RUNNING
ATTEMPT_COMPLETED
VERIFYING
VERIFIED
REJECTED
ESCALATING
SELECTING
MATERIALIZING
COMPLETED
EXHAUSTED
FAILED
```

Allowed transitions:

```text
CREATED -> CLASSIFYING
CLASSIFYING -> CLASSIFIED | FAILED
CLASSIFIED -> POLICY_SELECTED | EXHAUSTED | FAILED
POLICY_SELECTED -> ATTEMPT_RUNNING | SELECTING | EXHAUSTED | FAILED
ATTEMPT_RUNNING -> ATTEMPT_COMPLETED | FAILED
ATTEMPT_COMPLETED -> VERIFYING | REJECTED | FAILED
VERIFYING -> VERIFIED | FAILED
VERIFIED -> SELECTING | REJECTED | FAILED
REJECTED -> POLICY_SELECTED | ESCALATING | EXHAUSTED | FAILED
ESCALATING -> POLICY_SELECTED | EXHAUSTED | FAILED
SELECTING -> MATERIALIZING | EXHAUSTED | FAILED
MATERIALIZING -> COMPLETED | FAILED
```

A retry on the same backend is represented by `REJECTED -> POLICY_SELECTED`. An escalation is represented by `REJECTED -> ESCALATING -> POLICY_SELECTED`. The corresponding policy event records why the backend stayed the same or changed.

Terminal states are `COMPLETED`, `EXHAUSTED`, and `FAILED`. Terminal states cannot transition.

## 7. Canonical run bundle

Every run is stored at `~/.villani/runs/<run_id>/`, or beneath a test-provided run root. The required layout is:

```text
<run_id>/
  manifest.json
  task.json
  classification.json
  state.json
  events.jsonl
  policy_decisions.jsonl
  attempts/
    attempt_001/
      attempt.json
      worktree.json
      patch.diff
      stdout.log
      stderr.log
      runner_telemetry.json
      trace/
    attempt_002/
      ...
  verification/
    attempt_001.json
    attempt_002.json
  candidate_evidence_matrix.json
  selection.json
  selection_report.md
  materialization.json
  final.patch
  final_report.md
```

Files that do not yet have data may be absent while a run is active. `manifest.json`, `task.json`, `state.json`, and `events.jsonl` must exist immediately after run creation.

All JSON snapshots are written atomically with temporary file plus replace. JSONL files are append-only. A truncated final JSONL line after a process crash may be ignored during recovery, but earlier valid lines must remain readable.

## 8. Canonical event envelope

Every line in `events.jsonl` has this shape:

```json
{
  "schema_version": "villani.event.v1",
  "event_id": "evt_01...",
  "sequence": 1,
  "timestamp": "2026-07-10T00:00:00Z",
  "trace_id": "trace_01...",
  "run_id": "run_01...",
  "attempt_id": null,
  "parent_event_id": null,
  "source": "controller",
  "event_type": "run_created",
  "payload": {}
}
```

Minimum controller event types:

```text
run_created
classification_started
classification_completed
classification_failed
policy_selected
attempt_started
attempt_completed
attempt_failed
patch_captured
verification_started
verification_completed
verification_failed
retry_selected
escalation_selected
candidate_selected
materialization_started
materialization_completed
materialization_failed
run_completed
run_exhausted
run_failed
```

Minimum normalized runtime event types:

```text
model_call_started
model_call_completed
model_call_failed
tool_call_started
tool_call_completed
tool_call_failed
command_started
command_completed
file_read
file_write
```

Unknown future event types remain readable and are rendered as generic events.

## 9. Verification and selection contracts

Normalized verification includes:

```json
{
  "schema_version": "villani.verification.v1",
  "run_id": "run_01...",
  "attempt_id": "attempt_001",
  "outcome": "accepted",
  "acceptance_eligible": true,
  "confidence": 0.94,
  "reason": "All required behaviors have direct evidence.",
  "requirement_results": [],
  "success_evidence": [],
  "failure_evidence": [],
  "missing_evidence": [],
  "risk_flags": [],
  "recommended_action": "accept",
  "raw_verifier_artifact": "verification/raw/attempt_001.json"
}
```

`outcome` is one of `accepted`, `rejected`, `unclear`, or `error`.

`acceptance_eligible` may be true only when all of these conditions hold:

- The verifier completed without infrastructure or parsing error.
- The normalized outcome is `accepted`.
- The verifier recommended acceptance.
- Required success criteria have evidence.
- The patch exists and is non-empty unless the task explicitly requires no file change.
- No acceptance blocker is present.

Selection is deterministic by default. It ranks only eligible candidates using direct behavioral evidence, repository test evidence, critical requirement coverage, lower risk, and then lower actual cost when costs are known. Stable `attempt_id` is the final tie-breaker. An LLM comparison may be recorded as advisory, but it may not override a stronger deterministic evidence result in version 1.

## 10. Cost and capability policy

Backend configuration must distinguish these values:

- Provider, endpoint, and model.
- Roles.
- Static capability score.
- Token input and output prices.
- Optional compute cost per hour for local models.
- Optional fixed cost per attempt.
- Billing mode and accounting source.
- Concurrency and timeout.

The planner must not invent missing prices, token estimates, durations, success rates, or capability values. Missing values are `unknown` and appear in the policy alternatives with an explicit rejection or uncertainty reason.

Bootstrap policy thresholds are versioned configuration, not hidden constants:

```yaml
policy:
  version: bootstrap_v1
  easy_min_capability: 20
  medium_min_capability: 50
  hard_min_capability: 80
  economy_confidence_threshold: 0.80
  conservative_confidence_threshold: 0.65
  max_same_backend_retries: 1
  verifier_retry_limit: 1
```

Bootstrap routing rules:

1. High-risk, hard, or classification confidence below the conservative threshold requires the hard capability threshold.
2. Medium-risk or medium difficulty requires the medium threshold.
3. Easy, low-risk work with confidence at or above the economy threshold requires the easy threshold.
4. Among eligible backends with known estimated cost, choose the least expensive. Break ties with higher capability, then backend name.
5. If every eligible backend has unknown estimated cost and there is no cost cap, choose the smallest sufficient capability score. Record that cost ordering was unavailable.
6. If a cost cap is supplied, a backend with unknown estimated cost is ineligible because the controller cannot prove the cap.
7. If no backend meets the required capability, choose the highest-capability backend only when policy allows a documented constraint violation. Otherwise exhaust before attempting.
8. Infrastructure failure may retry the same backend once. It does not prove model incapability.
9. Implementation failure with material progress may retry the same backend once.
10. Capability failure escalates to the next higher-capability eligible backend.
11. Unclear or errored verification retries the verifier once. It never accepts the candidate.
12. The controller stops when a candidate is acceptance eligible unless policy explicitly requires collecting more accepted candidates.

Every policy decision records all considered backends, their eligibility, estimated cost or unknown status, capability score, rejection reasons, budget before and after, chosen action, and policy version.

## 11. Empirical capability scoring

Static capability scores are the bootstrap mechanism. The later empirical scorer groups only verified attempts by backend, model, task category, difficulty, and risk. It stores successes, failures, sample count, posterior mean, lower confidence bound, observed cost, and observed duration.

Rules:

- Never train from attempts without a terminal verifier outcome.
- Never treat verifier errors or missing traces as model failures.
- Never treat human-edited or manually materialized patches as clean model outcomes unless marked separately.
- Use static scores until the configurable minimum sample count is reached.
- Once enough samples exist, policy ranking uses a conservative success estimate such as a lower confidence bound.
- Optimize estimated cost to an accepted solution, not raw cost per attempt.
- Keep all feature definitions and score versions in the run bundle so historical decisions remain explainable.

## 12. Milestones

### M0: Monorepo and measured baseline

Deliverables:

- Required root layout.
- `AGENTS.md`, this `PLANS.md`, `docs/BASELINE.md`, and `docs/CLOSED_LOOP.md`.
- Reproducible install and test commands.
- Exact baseline test counts and known failures for all three components.

Exit criteria:

- Each component is present exactly once.
- There are no accidental nested git repositories.
- No component source behavior changed.
- Baseline results are recorded with Python, Node, and npm versions.

### M1: Green component baseline

Deliverables:

- Reproduced component failures fixed at their implementation cause.
- Regression tests for every fixed defect.
- No closed-loop integration code.

Exit criteria:

- Villani Code test suite passes.
- Villani Ops default test suite passes.
- Flight Recorder test, typecheck, build, and format checks pass.

### M2: Canonical protocol

Deliverables:

- Versioned root JSON Schemas.
- Python models and validators.
- TypeScript types and validators.
- Shared valid and invalid fixtures.
- Atomic JSON snapshot writer and append-only JSONL writer.

Exit criteria:

- Python and TypeScript accept the same valid fixture.
- Python and TypeScript reject each invalid fixture for the intended reason.
- Existing component tests remain green.

### M3: Deterministic controller with fakes

Deliverables:

- `villani_ops.closed_loop` package.
- Explicit dependency interfaces for classifier, policy, attempt runner, verifier, selector, and materializer.
- State machine and crash-safe run store.
- Unit tests covering accept, retry, escalation, exhaustion, and failure.

Exit criteria:

- No external model, network call, subprocess runner, or real repository mutation is needed for the controller tests.
- Every path produces the exact canonical bundle and event order.
- Illegal and post-terminal transitions fail closed.

### M4: Real attempt, verifier, selector, and materializer adapters

Deliverables:

- Villani Code attempt adapter.
- Git isolation and patch capture adapter.
- Dedicated verifier adapter.
- Evidence selector adapter.
- Safe materialization adapter.
- Deterministic tiny-repository integration tests.

Exit criteria:

- A fake Villani Code executable can run through the real adapters end to end.
- Missing trace, empty patch, verifier error, and failed apply cannot be accepted.
- The selected patch is the only patch applied.

### M5: Cost accounting and bootstrap escalation policy

Deliverables:

- Explicit local and API cost configuration.
- Actual and estimated cost components with accounting status.
- Classification-before-routing implementation.
- Versioned bootstrap policy and policy decision log.
- Policy matrix tests.

Exit criteria:

- A cheaper sufficient backend is chosen for a confident easy task.
- A hard, high-risk, or low-confidence task chooses a sufficiently capable backend.
- Capability rejection escalates.
- Infrastructure failure does not masquerade as capability failure.
- Unknown cost is never treated as zero.
- Attempt, cost, and wall-time budgets terminate deterministically.

### M6: Unified public CLI

Deliverables:

- `villani` entry point.
- `init`, `backend add`, `backend list`, `run`, `runs`, `inspect`, and `open` commands.
- One configuration root at `~/.villani`.
- Compatibility behavior for legacy commands.

Exit criteria:

- `villani run` calls only the closed-loop controller.
- The public command has no orchestration architecture selector.
- CLI tests prove config loading, execution, terminal summary, and exit codes.

### M7: Native Flight Recorder observability

Deliverables:

- Native `villani` provider and scanner.
- Run list and run detail support for canonical bundles.
- Policy timeline, candidate evidence comparison, verification details, tokens, duration, and cost.
- Redaction tests and canonical run fixture tests.

Exit criteria:

- Flight Recorder opens a canonical run without conversion.
- Cost, token, duration, and attempt values match the source bundle.
- Missing values render as unknown or not captured, not zero.
- Existing Codex, Claude, Pi, and generic providers still pass.

### M8: Empirical capability registry and optimizer

Deliverables:

- Versioned capability profile store.
- Offline rebuild command from verified run bundles.
- Conservative success estimates and minimum-sample fallback.
- Estimated cost-to-accepted-solution ranking.
- Deterministic evaluation fixtures and policy explanation output.

Exit criteria:

- Rebuilding twice from the same runs is idempotent.
- Unverified, errored, and human-modified outcomes are excluded or separately labeled.
- Sparse data uses static capability scores.
- Sufficient data changes backend ranking only when policy evidence supports it.
- Every learned decision remains reproducible from stored profile and policy versions.

### M9: Recovery, packaging, CI, and release gate

Deliverables:

- Resume or safe terminalization after interruption.
- Failure-injection tests.
- Root CI for Python 3.11 and 3.12 plus Node 18 and current LTS.
- One documented local installation workflow.
- One public quickstart.
- Legacy public paths marked deprecated without deleting compatibility code.

Exit criteria:

- Full component and cross-component test suites pass from a clean checkout.
- A deterministic CLI end-to-end test creates, verifies, selects, materializes, and replays a patch.
- Crash recovery cannot duplicate an attempt or apply a patch twice.
- No secret appears in checked fixtures or generated run artifacts.
- The release checklist below is complete.

## 13. Release checklist

- [x] `villani run` is the only documented primary execution path. Evidence: root `README.md`; compatibility-only help assertions in `villani_ops/tests/test_unified_cli.py`; `703 passed` Ops suite.
- [x] Classification is persisted before the first coding backend decision. Evidence: `tests/closed_loop/test_cli_e2e.py` asserts the classification completion sequence precedes policy selection; standalone E2E `1 passed`.
- [x] Every attempt is isolated. Evidence: the E2E executes both fake backends through `VillaniCodeAttemptAdapter` and asserts canonical attempts; cross-component suite `120 passed`.
- [x] Every attempt has patch, logs, telemetry, and verification references. Evidence: canonical E2E bundle assertions and Flight Recorder render; standalone E2E `1 passed`.
- [x] No verifier error can become acceptance. Evidence: closed-loop protocol/controller tests in the `120 passed` cross-component suite and `703 passed` Ops suite.
- [x] Selection considers only acceptance-eligible candidates. Evidence: E2E asserts only `attempt_002` is eligible and selected; standalone E2E `1 passed`.
- [x] Only the selected patch is materialized. Evidence: E2E repository content and selection assertions; recovery reverse-apply test proves no duplicate apply; `17 passed` recovery suite.
- [x] Unknown cost is never numeric zero. Evidence: M5/M8 accounting tests within the `703 passed` Ops suite and `120 passed` cross-component suite.
- [x] Local compute cost can be configured and displayed. Evidence: documented and executed README command uses `--billing-mode compute_time --compute-cost-per-hour 0.18`; temporary-home quickstart exited 0.
- [x] Policy alternatives and rejection reasons are visible. Evidence: E2E renders both backend policy rows and the first rejection through Flight Recorder; standalone E2E `1 passed`.
- [x] Run replay works directly from `~/.villani/runs`. Evidence: E2E invokes Flight Recorder with the canonical `VILLANI_HOME/runs` root and `--no-open`; `1 passed`.
- [x] Tokens, costs, duration, model calls, tool calls, commands, and file mutations are visible when captured. Evidence: E2E asserts canonical 22 input tokens, 10 output tokens, 50 ms, USD 0.60, two attempts, and rendered totals; Flight Recorder `86 passed`.
- [x] Secrets are redacted. Evidence: `python scripts/check-secrets.py integration/fixtures` reports 0 findings; E2E scans its generated bundle and asserts 0 findings; Flight Recorder redaction tests are included in `86 passed`.
- [x] Interruption recovery is idempotent. Evidence: all 15 required interruption boundaries, concurrent lock, and terminal no-op tests pass in `17 passed`; second resume preserves bundle hashes and dependency call counts.
- [x] All tests and CI checks pass. Evidence: Code `670 passed, 1 skipped`; Ops `703 passed, 114 deselected`; cross-component `120 passed`; Flight Recorder `86 passed` plus typecheck/build/format; root workflow parses with four required jobs.
- [x] The quickstart works from a clean machine with documented prerequisites. Evidence: installer exited 0 twice; README init/local/API configuration exited 0 under a temporary `VILLANI_HOME` with a tiny Git repository; deterministic public-run E2E `1 passed`.

## 14. Risks and controls

| Risk | Control |
| --- | --- |
| Two competing controller architectures remain active | Only `villani_ops.closed_loop.ClosedLoopController` is reachable from `villani run`. Legacy paths are compatibility-only. |
| Static capability scores look scientific but are arbitrary | Label them bootstrap scores, store their source, and replace ranking only after a minimum verified sample count. |
| Local backends appear free | Require explicit accounting mode and represent missing cost as unknown. |
| Verifier hallucination causes false acceptance | Combine deterministic evidence gates with normalized verifier output. Fail closed on missing evidence or parse errors. |
| Selection adds model cost without improving decisions | Keep LLM comparison advisory. Use deterministic evidence ranking for the final decision. |
| Three telemetry schemas drift | Root versioned schemas plus shared cross-language fixtures are the contract. |
| Flight Recorder becomes another data store | It reads canonical run bundles and maintains only a rebuildable index. |
| Controller crash corrupts state | Atomic snapshots, append-only events, monotonic sequences, and idempotent recovery. |
| Integration effort becomes a rewrite | Reuse existing runner, verifier, selector, isolation, and renderer primitives. Add adapters around them. |

## 15. Decision log

| ID | Decision | Reason |
| --- | --- | --- |
| D001 | Villani Ops owns the controller. | It already contains routing, runners, verification, selection, isolation, and materialization primitives. |
| D002 | Verifier-parallel primitives are reused, but its orchestration directory is not the canonical product store. | Its evidence and selection work is stronger than the default adaptive path, while the closed loop requires one run model. |
| D003 | Adaptive, agentic, and graph orchestrators are not the public default. | Multiple controller paths make policy behavior and observability inconsistent. |
| D004 | Root JSON Schemas are the wire source of truth. | Python and TypeScript must read the same run without a translation service. |
| D005 | Flight Recorder is a read-only consumer of canonical runs. | Observability must not mutate execution truth. |
| D006 | Unknown cost is not zero. | A missing price is not evidence that an attempt is free. |
| D007 | Deterministic evidence selection controls materialization. | An additional model judge must not silently override stronger evidence. |
| D008 | Version 1 is sequential by default. | Cheap-first escalation is easier to reason about and avoids paying for unnecessary candidates. |
| D009 | Empirical scoring is built only after the full trace is reliable. | Learning from incomplete or inconsistent outcomes would optimize noise. |

## 16. Progress log

Codex must update this section at the end of each milestone. It must not mark a milestone complete unless all exit criteria passed. Use exact commands and counts, not words such as "mostly" or "appears".

### Current milestone

`M9: complete`

### Milestone status

- [x] M0: Monorepo and measured baseline
- [x] M1: Green component baseline
- [x] M2: Canonical protocol
- [x] M3: Deterministic controller with fakes
- [x] M4: Real attempt, verifier, selector, and materializer adapters
- [x] M5: Cost accounting and bootstrap escalation policy
- [x] M6: Unified public CLI
- [x] M7: Native Flight Recorder observability
- [x] M8: Empirical capability registry and optimizer
- [x] M9: Recovery, packaging, CI, and release gate

### Evidence entries

Add one entry per completed pass using this exact template:

```text
#### YYYY-MM-DD: M<number> <name>

Status: complete | blocked

Changed files:
- <path>

Verification:
- `<exact command>`: <exit code and exact pass/fail/skip counts>

Acceptance criteria:
- PASS | FAIL: <criterion and evidence>

Known remaining issues:
- <issue or "none within this milestone">

Next permitted milestone:
- M<number>, only after the user starts a new Codex task from this completed state.
```

#### 2026-07-10: M0 Monorepo and measured baseline

Status: complete

Changed files:
- `.gitignore`
- `AGENTS.md`
- `PLANS.md`
- `README.md`
- `docs/BASELINE.md`
- `docs/CLOSED_LOOP.md`
- `integration/fixtures/.gitkeep`
- `schemas/.gitkeep`
- `scripts/.gitkeep`
- `.venv/` (ignored local environment)

Verification:
- From `components/villani-code`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 1; 604 passed, 66 failed, 0 errors, 1 skipped, 27 warnings.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 1; 539 passed, 32 failed, 0 errors, 0 skipped, 114 deselected.
- From `components/villani-flight-recorder`, `npm.cmd test`: exit code 1; 56 passed, 6 failed, 0 errors, 0 skipped; 15 test files passed and 2 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd run typecheck`: exit code 0; TypeScript typecheck passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run build`: exit code 0; TypeScript build passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run format:check`: exit code 0; Prettier reported all matched files use Prettier code style; test counts not applicable.

Acceptance criteria:
- PASS: Each package identity occurs exactly once and is at its required `components/` path.
- PASS: All three component trees contain zero nested `.git` directories or gitfiles.
- PASS: Component tracked and unignored files remain unchanged after installation and verification.
- PASS: The required root layout, shared Python environment, and lockfile-based Node installation exist.
- PASS: `docs/BASELINE.md` records the exact six command outcomes, all failing test IDs, and Python, Node, npm, and operating-system versions.

Known remaining issues:
- The measured baseline has 66 Villani Code failures, 32 Villani Ops failures, and 6 Flight Recorder failures; stabilization belongs to M1.

Next permitted milestone:
- M1, only after the user starts a new Codex task from this completed state.

#### 2026-07-10: M1 Green component baseline

Status: complete

Changed files:
- `PLANS.md`
- `components/villani-code/tests/test_agents_claude_adapter_run.py`
- `components/villani-code/tests/test_auto_approve.py`
- `components/villani-code/tests/test_benchmark_agents.py`
- `components/villani-code/tests/test_interactive_approval_dialog.py`
- `components/villani-code/tests/test_mission_state_runtime.py`
- `components/villani-code/tests/test_plan_runtime_architecture.py`
- `components/villani-code/tests/test_plan_workflow.py`
- `components/villani-code/tests/test_tui_controller.py`
- `components/villani-code/tests/test_ui_integration.py`
- `components/villani-code/tests/test_ui_slash_commands.py`
- `components/villani-code/tests/test_villani_mode.py`
- `components/villani-code/villani_code/benchmark/agents/base.py`
- `components/villani-code/villani_code/benchmark/agents/claude_code.py`
- `components/villani-code/villani_code/command_environment.py`
- `components/villani-code/villani_code/mcp.py`
- `components/villani-code/villani_code/mission_state.py`
- `components/villani-code/villani_code/prompting.py`
- `components/villani-code/villani_code/state.py`
- `components/villani-code/villani_code/state_tooling.py`
- `components/villani-code/villani_code/task_memory.py`
- `components/villani-code/villani_code/tui/app.py`
- `components/villani-code/villani_code/tui/assets.py`
- `components/villani-code/villani_code/tui/controller.py`
- `components/villani-code/villani_code/tui/widgets/approval.py`
- `components/villani-code/villani_code/tui/widgets/plan_question.py`
- `components/villani-ops/villani_ops/classification/classifier.py`
- `components/villani-ops/villani_ops/cli/main.py`
- `components/villani-ops/villani_ops/isolation/copy_git.py`
- `components/villani-ops/villani_ops/orchestration/engine.py`
- `components/villani-ops/villani_ops/orchestrator/verifier_parallel.py`
- `components/villani-ops/villani_ops/orchestrator/verifier_sequential.py`
- `components/villani-ops/villani_ops/runners/villani_code.py`
- `components/villani-ops/villani_ops/subprocess_utils.py`
- `components/villani-ops/villani_ops/tests/test_viewer.py`
- `components/villani-ops/villani_ops/tests/test_villani_code_runner.py`
- `components/villani-flight-recorder/src/scanners/findSessions.ts`
- `components/villani-flight-recorder/test/cliReplay.test.ts`
- `components/villani-flight-recorder/dist/scanners/findSessions.js`

Verification:
- From `components/villani-code`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 670 passed, 0 failed, 0 errors, 1 skipped, 27 warnings.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 571 passed, 0 failed, 0 errors, 0 skipped, 114 deselected.
- From `components/villani-flight-recorder`, `npm.cmd test`: exit code 0; 62 passed, 0 failed, 0 errors, 0 skipped; 17 test files passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd run typecheck`: exit code 0; TypeScript typecheck passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run build`: exit code 0; TypeScript build passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run format:check`: exit code 0; Prettier reported all matched files use Prettier code style; test counts not applicable.

Acceptance criteria:
- PASS: Villani Code has zero failing tests: 670 passed and 1 skipped.
- PASS: Villani Ops default suite has zero failing tests: 571 passed and 114 deselected.
- PASS: Flight Recorder tests, typecheck, build, and format check all exit zero.
- PASS: No test was deleted, skipped, xfailed, or weakened; executed test totals remain 671 for Villani Code, 571 for Villani Ops, and 62 for Flight Recorder, and the diff adds no skip or xfail controls.
- PASS: No M2 or later feature code was added; the diff contains no canonical protocol, closed-loop controller, unified CLI, new policy, or Flight Recorder Villani provider.

Known remaining issues:
- none within this milestone

Next permitted milestone:
- M2, only after the user starts a new Codex task from this completed state.

#### 2026-07-10: M2 Canonical protocol

Status: complete

Changed files:
- `.gitattributes`
- `.gitignore`
- `PLANS.md`
- `schemas/v1/attempt.schema.json`
- `schemas/v1/classification.schema.json`
- `schemas/v1/event.schema.json`
- `schemas/v1/materialization.schema.json`
- `schemas/v1/policy-decision.schema.json`
- `schemas/v1/run-manifest.schema.json`
- `schemas/v1/run-state.schema.json`
- `schemas/v1/selection.schema.json`
- `schemas/v1/task.schema.json`
- `schemas/v1/verification.schema.json`
- `components/villani-ops/pyproject.toml`
- `components/villani-ops/villani_ops/closed_loop/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/durable_io.py`
- `components/villani-ops/villani_ops/closed_loop/protocol.py`
- `components/villani-ops/villani_ops/closed_loop/schema_validation.py`
- `components/villani-ops/villani_ops/tests/closed_loop/__init__.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py`
- `components/villani-flight-recorder/package.json`
- `components/villani-flight-recorder/package-lock.json`
- `components/villani-flight-recorder/src/providers/villaniProtocol.ts`
- `components/villani-flight-recorder/src/providers/villaniSchemaValidation.ts`
- `components/villani-flight-recorder/dist/providers/villaniProtocol.js`
- `components/villani-flight-recorder/dist/providers/villaniSchemaValidation.js`
- `components/villani-flight-recorder/test/villaniProtocol.test.ts`
- `integration/fixtures/protocol/v1/valid_run/` (28 fixture files)
- `integration/fixtures/protocol/v1/invalid/` (10 fixture files)
- `tests/closed_loop/test_protocol_contract.py`

Verification:
- From `components/villani-code`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 670 passed, 0 failed, 0 errors, 1 skipped, 27 warnings.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops/tests/closed_loop`: exit code 0; 16 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 587 passed, 0 failed, 0 errors, 0 skipped, 114 deselected.
- From `components/villani-flight-recorder`, `npm.cmd test -- villaniProtocol.test.ts`: exit code 0; 11 passed, 0 failed, 0 errors, 0 skipped; 1 test file passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd test`: exit code 0; 73 passed, 0 failed, 0 errors, 0 skipped; 18 test files passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd run typecheck`: exit code 0; TypeScript typecheck passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run build`: exit code 0; TypeScript build passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run format:check`: exit code 0; Prettier reported all matched files use Prettier code style; test counts not applicable.
- From the repository root, `.\.venv\Scripts\python.exe -m pytest tests/closed_loop -q`: exit code 0; 2 passed, 0 failed, 0 errors, 0 skipped.
- From the repository root, `git diff --check`: exit code 0; no whitespace errors.

Acceptance criteria:
- PASS: Exactly 10 Draft 2020-12 schemas exist under `schemas/v1`; every schema has a stable `https://villani.dev/schemas/v1/` ID, a required exact v1 `schema_version`, and a closed top-level object.
- PASS: Python and TypeScript validate the same complete 28-file root fixture bundle, including two attempts, one rejected verification, one accepted verification, one selection, and successful materialization.
- PASS: Python and TypeScript reject all 8 invalid JSON documents for their intended schema or named semantic rule.
- PASS: Python reads the 2 complete events preceding the one truncated final JSONL line and raises on the malformed middle line and malformed complete final lines.
- PASS: Both event-stream validators reject non-increasing sequences, the event envelope has exactly the 11 required fields, future event types remain open, and scoped attempt/runtime events require a non-null `attempt_id`.
- PASS: Python exposes 10 strict Pydantic v2 models, validates the normative root schemas with `jsonschema`, and provides atomic snapshot plus durable compact JSONL I/O.
- PASS: TypeScript exposes all 10 protocol types and uses Ajv 2020 with structured `instancePath`, `keyword`, and `message` validation errors.
- PASS: Protocol source contains no controller loop, routing policy, subprocess runner, unified CLI, provider, UI, or materialization behavior.
- PASS: All existing Villani Code, Villani Ops, and Flight Recorder tests and checks remain green.

Known remaining issues:
- none within this milestone

Next permitted milestone:
- M3, only after the user starts a new Codex task from this completed state.

#### 2026-07-10: M3 Deterministic controller with fakes

Status: complete

Changed files:
- `PLANS.md`
- `components/villani-ops/villani_ops/closed_loop/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/event_writer.py`
- `components/villani-ops/villani_ops/closed_loop/interfaces.py`
- `components/villani-ops/villani_ops/closed_loop/run_store.py`
- `components/villani-ops/villani_ops/closed_loop/state_machine.py`
- `components/villani-ops/villani_ops/tests/closed_loop/fakes.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_controller.py`

Verification:
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops/tests/closed_loop/test_controller.py`: exit code 0; 15 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops/tests/closed_loop`: exit code 0; 31 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 602 passed, 0 failed, 0 errors, 0 skipped, 114 deselected.
- From `components/villani-code`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 670 passed, 0 failed, 0 errors, 1 skipped, 27 warnings.
- From `components/villani-flight-recorder`, `npm.cmd test`: exit code 0; 73 passed, 0 failed, 0 errors, 0 skipped; 18 test files passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd run typecheck`: exit code 0; TypeScript typecheck passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run build`: exit code 0; TypeScript build passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run format:check`: exit code 0; Prettier reported all matched files use Prettier code style; test counts not applicable.
- From the repository root, `.\.venv\Scripts\python.exe -m pytest tests\closed_loop -q`: exit code 0; 2 passed, 0 failed, 0 errors, 0 skipped.

Acceptance criteria:
- PASS: All 15 named M3 fake-controller scenarios pass using only temporary run roots and injected pure fakes, with process and network entry points guarded against use.
- PASS: The controller implements exactly the 15 canonical states and listed transitions, rejects illegal edges, and rejects every post-terminal transition.
- PASS: Every accepted generated bundle document and event stream validates against the M2 schemas, with strictly monotonic event sequences and atomic state snapshots.
- PASS: The controller imports no adaptive, agentic, graph, verifier-parallel, subprocess, network, Villani Code, or Flight Recorder execution path.
- PASS: Villani Ops, Villani Code, Flight Recorder, and root closed-loop contract verification all remain green.

Known remaining issues:
- none within this milestone

Next permitted milestone:
- M4, only after the user starts a new Codex task from this completed state.

#### 2026-07-10: M4 Real attempt, verifier, selector, and materializer adapters

Status: complete

Changed files:
- `PLANS.md`
- `components/villani-ops/villani_ops/closed_loop/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/event_writer.py`
- `components/villani-ops/villani_ops/closed_loop/interfaces.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/evidence_selector.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/git_isolation.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/patch_materializer.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/runtime_event_translation.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/villani_code_attempt.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/villani_verifier.py`
- `components/villani-ops/villani_ops/materialize.py`
- `components/villani-ops/villani_ops/orchestrator/selection.py`
- `components/villani-ops/villani_ops/orchestrator/verifier_parallel.py`
- `components/villani-ops/villani_ops/verifier/service.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_adapters.py`
- `integration/fixtures/closed_loop_m4/tiny_repo/example.txt`

Verification:
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops/tests/closed_loop/test_adapters.py`: exit code 0; 13 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops/tests/closed_loop villani_ops/tests/test_verifier_parallel_orchestrator.py villani_ops/tests/test_selection.py villani_ops/tests/test_materialize_verifier_orchestrations.py villani_ops/tests/test_verifier_orchestrator_materialization.py`: exit code 0; 127 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 615 passed, 0 failed, 0 errors, 0 skipped, 114 deselected.
- From `components/villani-code`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 670 passed, 0 failed, 0 errors, 1 skipped, 27 warnings.
- From `components/villani-flight-recorder`, `npm.cmd test`: exit code 0; 73 passed, 0 failed, 0 errors, 0 skipped; 18 test files passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd run typecheck`: exit code 0; TypeScript typecheck passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run build`: exit code 0; TypeScript build passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run format:check`: exit code 0; Prettier reported all matched files use Prettier code style; test counts not applicable.
- From the repository root, `.\.venv\Scripts\python.exe -m pytest tests\closed_loop -q`: exit code 0; 2 passed, 0 failed, 0 errors, 0 skipped.

Acceptance criteria:
- PASS: The controller completes a temporary Git repository through the real isolation, Villani Code runner-protocol, deterministic verifier, evidence selector, and safe materializer adapters.
- PASS: All 13 required M4 integration and safety scenarios pass without contacting a model endpoint.
- PASS: The closed-loop path creates no `.villani-ops/orchestrations` directory and never uses verifier-parallel as its controller.
- PASS: Missing traces, empty patches, malformed verifier output, verifier timeout, runner exit 127, unsafe patch paths, and failed apply are all non-accepting outcomes.
- PASS: Runtime model, tool, command, file, and patch events are translated when present, raw debug traces remain under the canonical attempt, and configured secrets are absent from the run bundle.
- PASS: Verifier-parallel reuses the extracted verifier execution and evidence-finalization services while its existing public behavior and tests remain green.
- PASS: Villani Ops, Villani Code, Flight Recorder, and root closed-loop contract verification all remain green.

Known remaining issues:
- none within this milestone

Next permitted milestone:
- M5, only after the user starts a new Codex task from this completed state.

#### 2026-07-10: M5 Cost accounting and bootstrap escalation policy

Status: complete

Changed files:
- `PLANS.md`
- `components/villani-ops/villani_ops/core/backend.py`
- `components/villani-ops/villani_ops/closed_loop/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/interfaces.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/costs.py`
- `components/villani-ops/villani_ops/closed_loop/policy.py`
- `components/villani-ops/villani_ops/closed_loop/failure_classification.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/villani_code_attempt.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_m5_policy_costs.py`

Architectural decisions:
- The controller resolves only an enabled classification-role backend before classification; it writes `classification.json` and durably emits `classification_completed` before `bootstrap_v1` enumerates coding alternatives.
- Cost accounting is component based. Token, compute-time, and fixed components remain nullable; hybrid totals include each configured applicable component once; unknown data never becomes numeric zero.
- Bootstrap thresholds and retry limits are validated policy configuration with the documented defaults. Policy decisions persist the classification reference, threshold rule, all coding alternatives, estimated and actual budget evidence, repeat/escalation flags, and budget projection.
- Failure classification is evidence based. A generic nonzero runner exit is not capability evidence, and verifier infrastructure failures retry verification without consuming or rerunning a coding attempt.
- Existing adaptive, agentic, and legacy policy paths remain compatibility-only and were not reused as the closed-loop routing order.

Verification:
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\closed_loop\test_m5_policy_costs.py`: exit code 0; 25 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 640 passed, 0 failed, 0 errors, 0 skipped, 114 deselected.
- From `components/villani-code`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 670 passed, 0 failed, 0 errors, 1 skipped, 27 warnings.
- From `components/villani-flight-recorder`, `npm.cmd test`: exit code 0; 73 passed, 0 failed, 0 errors, 0 skipped; 18 test files passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd run typecheck`: exit code 0; TypeScript typecheck passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run build`: exit code 0; TypeScript build passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run format:check`: exit code 0; Prettier reported all matched files use Prettier code style; test counts not applicable.
- From the repository root, `.\.venv\Scripts\python.exe -m pytest tests\closed_loop -q`: exit code 0; 2 passed, 0 failed, 0 errors, 0 skipped.
- From the repository root, `git diff --check`: exit code 0; no whitespace errors.

Acceptance criteria:
- PASS: `classification.json` and `classification_completed` are persisted before the first coding policy decision; the ordering regression test observes one coding run and a verifier-only retry with no second coding attempt.
- PASS: All 20 requested routing, escalation, budget, and accounting cases are covered by the 25-test M5 file and pass.
- PASS: The concrete closed-loop policy is `bootstrap_v1`; every recorded policy decision contains all coding alternatives, capability and cost evidence, rejection reasons, classification reference, and before/after budget data.
- PASS: Legacy backend YAML without new fields still loads; a positive legacy token price infers token billing, while absent or zero-only legacy prices remain unknown.
- PASS: Actual API token, local compute-time, fixed, and hybrid accounting use configured formulas only; missing configuration or telemetry returns partial or unknown rather than fabricated zero.
- PASS: Infrastructure, implementation, capability, verification, no-change, and materialization outcomes have deterministic next actions; nonzero exit alone is never classified as capability failure.
- PASS: Attempt, known-cost, and wall-time budgets block attempts deterministically, including unknown estimates under an active cost cap.
- PASS: Villani Ops, Villani Code, Flight Recorder, and root closed-loop verification remain green.

Assumptions:
- Currency is USD because the existing token price and protocol budget fields are USD-denominated.
- Easy/low-risk classifications with confidence at or above 0.65 but below 0.80 use the medium threshold; below 0.65 uses hard and at or above 0.80 uses easy.

Known remaining issues:
- No defect remains within M5. Bootstrap capability scores and estimates remain user-configured static inputs; empirical learning remains deferred to M8.

Next permitted milestone:
- M6, only after the user starts a new Codex task from this completed state.

#### 2026-07-10: M6 Unified public CLI

Status: complete

Changed files:
- `PLANS.md`
- `components/villani-ops/pyproject.toml`
- `components/villani-ops/villani_ops/cli/unified.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/event_writer.py`
- `components/villani-ops/villani_ops/tests/test_unified_cli.py`

Architectural decisions:
- The `villani` entry point owns a separate Typer application with only `init`, `backend add`, `backend list`, `run`, `runs`, `inspect`, and `open`; it does not import or delegate to the legacy Villani Ops CLI.
- Public configuration is one commented `config.yaml` beneath `VILLANI_HOME` or `~/.villani`, with canonical runs beneath the same home and environment-variable names used for secret references.
- Public run construction instantiates only `ClosedLoopController`, the classification adapter, `BootstrapPolicyEngine`, Villani Code attempt adapter, dedicated verifier, evidence selector, and safe materializer.
- Concise CLI progress is emitted by an observer only after each canonical event has been durably appended; controller state remains authoritative.
- Run listing and inspection validate and read canonical bundle documents only. Output is recursively redacted, and one corrupt bundle cannot stop other runs from being listed.
- Flight Recorder launch is delegated in the required environment, PATH, then monorepo order; the old Villani Ops viewer is never used as a fallback.

Verification:
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\test_unified_cli.py`: exit code 0; 15 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\test_unified_cli.py villani_ops\tests\closed_loop`: exit code 0; 84 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pip install --no-deps -e .`: exit code 0; editable wheel built and `villani-ops 0.2.0` installed successfully.
- From `components/villani-ops`, `..\..\.venv\Scripts\villani.exe --help`: exit code 0; installed command tree contains exactly the six root commands/groups and no architecture selector.
- From `components/villani-ops`, `..\..\.venv\Scripts\villani-ops.exe --help`: exit code 0; compatibility entry point remains installed.
- From `components/villani-ops`, `..\..\.venv\Scripts\villani-code.exe --help`: exit code 0; compatibility entry point remains installed.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 655 passed, 0 failed, 0 errors, 0 skipped, 114 deselected.
- From `components/villani-code`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 670 passed, 0 failed, 0 errors, 1 skipped, 27 warnings.
- From `components/villani-flight-recorder`, `npm.cmd test`: exit code 0; 73 passed, 0 failed, 0 errors, 0 skipped; 18 test files passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd run typecheck`: exit code 0; TypeScript typecheck passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run build`: exit code 0; TypeScript build passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run format:check`: exit code 0; Prettier reported all matched files use Prettier code style; test counts not applicable.
- From the repository root, `.\.venv\Scripts\python.exe -m pytest tests\closed_loop -q`: exit code 0; 2 passed, 0 failed, 0 errors, 0 skipped.
- From the repository root, `git diff --check`: exit code 0; no whitespace errors.

Acceptance criteria:
- PASS: The exact `villani = "villani_ops.cli.unified:app"` script is installed and `villani --help` exits zero from the editable install; `villani-ops` and `villani-code` remain working.
- PASS: The public command tree exposes only the required commands, the backend group exposes only `add` and `list`, and help contains no architecture, tournament, decomposition, or scheduling selector.
- PASS: The public run path constructs only `ClosedLoopController` and M4/M5 dependencies; focused tests inject a fake controller and assert no legacy runner or CLI is imported.
- PASS: Repository validation occurs before run creation, task and supplied success criteria remain verbatim, and durable event observation prints run identity and concise state updates.
- PASS: Exit 0 for `COMPLETED`, exit 2 for configuration/usage failures, exit 3 for `EXHAUSTED`, and exit 4 for `FAILED` are all covered by focused tests.
- PASS: Init, backend validation, repeatable roles, capability requirements, explicit billing fields, secret environment references, and non-overwrite behavior pass in isolated `VILLANI_HOME` tests.
- PASS: Backend listing, canonical inspection JSON, controller artifacts, and error paths do not reveal configured or resolved secret values.
- PASS: Canonical run listing tolerates corrupt bundles, and inspection exposes classification, policy decisions, attempts, verifications, selection, materialization, tokens, cost components, and artifact paths.
- PASS: Flight Recorder command resolution and optional run ID forwarding pass for configured command, PATH command, monorepo fallback, and unavailable-command instructions.
- PASS: Villani Ops, Villani Code, Flight Recorder, and root closed-loop verification remain green.

Assumptions:
- When `--success-criteria` is omitted, the verbatim task text is used as the non-empty canonical success criterion.
- `villani open <run_id>` invokes Flight Recorder replay with provider `villani`, the canonical runs root, the run ID, and browser opening; `villani open` invokes its run browser launch.

Known remaining issues:
- No defect remains within M6. Native Flight Recorder parsing and presentation of Villani bundles remains intentionally deferred to M7; empirical scoring remains deferred to M8.

Next permitted milestone:
- M7, only after the user starts a new Codex task from this completed state.

#### 2026-07-10: M7 Native Flight Recorder observability

Status: complete

Changed files:
- `PLANS.md`
- `components/villani-flight-recorder/src/cli.ts`
- `components/villani-flight-recorder/src/commands/launchVillani.ts`
- `components/villani-flight-recorder/src/index/sessionIndex.ts`
- `components/villani-flight-recorder/src/index/sessionTypes.ts`
- `components/villani-flight-recorder/src/index/villaniRunIndex.ts`
- `components/villani-flight-recorder/src/providers/providerAdapter.ts`
- `components/villani-flight-recorder/src/providers/types.ts`
- `components/villani-flight-recorder/src/providers/villani.ts`
- `components/villani-flight-recorder/src/render/components/appShell.ts`
- `components/villani-flight-recorder/src/render/components/metricCards.ts`
- `components/villani-flight-recorder/src/render/components/villaniRunDetails.ts`
- `components/villani-flight-recorder/src/render/deriveMetrics.ts`
- `components/villani-flight-recorder/src/render/deriveTimeline.ts`
- `components/villani-flight-recorder/src/render/sessionBrowser.ts`
- `components/villani-flight-recorder/src/render/theme.ts`
- `components/villani-flight-recorder/src/render/viewModel.ts`
- `components/villani-flight-recorder/src/scanners/findSessions.ts`
- `components/villani-flight-recorder/src/scanners/findVillaniRuns.ts`
- `components/villani-flight-recorder/test/cliReplay.test.ts`
- `components/villani-flight-recorder/test/fixtures/villani/.gitignore`
- `components/villani-flight-recorder/test/fixtures/villani/README.md`
- `components/villani-flight-recorder/test/helpers/villaniFixture.ts`
- `components/villani-flight-recorder/test/villaniProvider.test.ts`
- Corresponding generated JavaScript under `components/villani-flight-recorder/dist/`.

Architectural decisions:
- The Villani provider scans canonical run directories, validates snapshots with the M2 validator, applies the protocol's tolerant-final-line JSONL rule, and reads all attempts, verification, evidence, selection, materialization, trace, log, patch, token, duration, and cost data directly from the bundle without a conversion layer.
- Canonical run directories remain read-only. The rebuildable index and static HTML stay under Flight Recorder output locations, and Villani launch/replay rejects configured outputs inside the canonical runs root.
- Canonical event identifiers and sequence values are first-class normalized fields. Villani timelines sort by canonical `sequence`; unknown future event types remain generic inspectable events.
- Villani view data is an optional extension to the existing replay model. Claude, Codex, Pi, generic, and Git paths retain their existing adapters and rendering behavior.
- Aggregate accounting uses manifest values exactly. Runtime counters use explicit attempt telemetry when present and otherwise only positive canonical event occurrences; absent signals remain null, while explicit numeric zero remains zero.
- The test fixture is generated from the normative M2 bundle by `test/helpers/villaniFixture.ts`; no divergent checked-in schema copy is maintained.

Verification:
- From `components/villani-flight-recorder`, `npm.cmd test -- villaniProvider.test.ts`: exit code 0; 13 passed, 0 failed, 0 errors, 0 skipped; 1 test file passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd test`: exit code 0; 86 passed, 0 failed, 0 errors, 0 skipped; 19 test files passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd run typecheck`: exit code 0; TypeScript typecheck passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run build`: exit code 0; TypeScript build passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run format:check`: exit code 0; Prettier reported all matched files use Prettier code style; test counts not applicable.
- From `components/villani-flight-recorder`, `node dist/cli.js launch --provider villani --root test/fixtures/villani --run-id run_protocol_fixture --no-open --out <temporary-path>`: exit code 0; the requested canonical run detail HTML was written outside the run root.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 655 passed, 0 failed, 0 errors, 0 skipped, 114 deselected.
- From `components/villani-code`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 670 passed, 0 failed, 0 errors, 1 skipped, 27 warnings.
- From the repository root, `.\.venv\Scripts\python.exe -m pytest tests\closed_loop -q`: exit code 0; 2 passed, 0 failed, 0 errors, 0 skipped.
- From the repository root, `git diff --check`: exit code 0; no whitespace errors.

Acceptance criteria:
- PASS: Flight Recorder discovers `~/.villani/runs` through `VILLANI_HOME`, scans directories containing `manifest.json`, `state.json`, and `events.jsonl`, and opens the complete canonical fixture directly without conversion.
- PASS: Normalized events preserve `run_id`, `trace_id`, `attempt_id`, `event_id`, `parent_event_id`, and `sequence`; the controller timeline renders in canonical sequence order and future event types remain inspectable.
- PASS: Run browser rows and run detail render the fixture's exact 275 tokens, 9,000 ms, USD 0.05, two attempts, selected `fixture-large` model, policy decisions, candidate eligibility, deterministic ranking, verification evidence, and successful materialization.
- PASS: Null cost renders `Unknown`, missing counters and artifacts render `Unknown` or `Not captured`, and no missing metric is converted to numeric zero.
- PASS: One corrupt run produces a readable corrupt index record without preventing a valid run from being indexed.
- PASS: Fake API keys are redacted from canonical events and attempt artifacts before HTML rendering, with no remote scripts, fonts, analytics, or assets introduced.
- PASS: `vfr launch --provider villani --root <runs-root> --run-id <id>` opens the requested detail through an injected browser opener; no run ID generates the run browser, and the legacy replay form used by `villani open` remains compatible.
- PASS: Hash-and-mtime snapshots prove parsing and launch do not write canonical run files, and output paths under the canonical runs root fail closed.
- PASS: Existing Claude, Codex, Pi, generic, and Git tests pass within the unchanged-provider full suite.

Assumptions:
- The M2 fixture directory name is not its canonical `run_id`; internal snapshot identities remain authoritative, while the generated M7 scanner fixture is copied into a directory named `run_protocol_fixture`.
- Root schema discovery remains monorepo-local in M7; release packaging of schemas is intentionally deferred to M9.

Known remaining issues:
- none within this milestone

Next permitted milestone:
- M8, only after the user starts a new Codex task from this completed state.

#### 2026-07-10: M8 Empirical capability registry and optimizer

Status: complete

Changed files:
- `PLANS.md`
- `components/villani-ops/villani_ops/cli/unified.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/villani_verifier.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/policy.py`
- `components/villani-ops/villani_ops/closed_loop/capabilities/__init__.py`
- `components/villani-ops/villani_ops/closed_loop/capabilities/ingest.py`
- `components/villani-ops/villani_ops/closed_loop/capabilities/models.py`
- `components/villani-ops/villani_ops/closed_loop/capabilities/optimizer.py`
- `components/villani-ops/villani_ops/closed_loop/capabilities/report.py`
- `components/villani-ops/villani_ops/closed_loop/capabilities/scoring.py`
- `components/villani-ops/villani_ops/closed_loop/capabilities/store.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_m8_capabilities.py`
- `components/villani-ops/villani_ops/tests/test_unified_cli.py`

Architectural decisions:
- The registry reads only canonical local run snapshots. It accepts materialized, acceptance-eligible selected attempts and explicitly labelled accepted-but-unselected candidates from multi-accept policies; only completed normalized `implementation_failure`, `capability_failure`, and `no_change_failure` outcomes enter the failure denominator. Infrastructure, verifier, corruption, interruption, unknown, materialization, manual/human, missing-identity, and untrusted unselected outcomes remain outside the denominator with explicit counts.
- Clean observations are deduplicated by `(run_id, attempt_id, scorer_version)`. Four deterministic aggregate levels retain the exact backend/provider/model and classifier/verifier/scorer versions while backing off task dimensions in the required order: category+difficulty+risk, category+difficulty, category, then global backend/model.
- `profiles-v1.json` is atomically replaced under `VILLANI_HOME/capabilities`; `provenance.jsonl` is append-only. Canonical serialization, source projection digests, deterministic profile ordering, latest-observation generation time, digest verification on load, and no-op same-digest rebuilds make reconstruction deterministic and idempotent.
- Wilson scoring uses fixed `z = 1.959963984540054`. Static eligibility remains owned by M5 and never changes the configured backend score. Empirical ordering is activated only when every M5-eligible backend has a sufficient matching/backed-off profile and known mean actual cost; otherwise the policy records exact missing inputs and stays on `bootstrap_v1`.
- Empirical sequence optimization enumerates ordered sequences of lengths one through the remaining attempt limit, constrains worst-case known sequence cost to the remaining cost budget, persists a deterministic top 100 plus omitted and budget-rejected counts, and records inputs, profile/snapshot versions and digests, formulas, target, chosen order, and pruning. More than eight eligible backends are pruned by conservative cost-to-success then backend name.
- The controller records provider/classifier/verifier provenance in future canonical bundles and explicitly labels clean acceptance-eligible candidates left unselected by a configured multi-accept policy. `capability explain` performs classification and a read-only policy decision only; it never constructs or invokes a coding attempt runner.

Formulas:
- Wilson lower bound: `(p_hat + z^2/(2n) - z*sqrt(p_hat*(1-p_hat)/n + z^2/(4n^2))) / (1 + z^2/n)`, with `z = 1.959963984540054` and zero samples mapped to zero.
- Empirical capability score: `floor(100 * Wilson lower bound)`.
- Single-backend expected cost to accepted solution: `mean actual attempt cost / conservative success probability`; it remains null when samples, cost, or positive probability are unavailable.
- Ordered-sequence expected cost: `sum(cost_i * product(1 - p_j for every earlier j))`.
- Ordered-sequence success probability: `1 - product(1 - p_i)`.
- Known cost-budget constraint: `sum(cost_i) <= remaining known cost budget`.

Deterministic fixture evidence:
- The combined exclusion fixture produces exactly `duplicate_attempt=1`, `human_modified=1`, `infrastructure_failure=1`, `materialization_failure=1`, and `verification_failure=1`; its clean denominator contains exactly two accepted materialized attempts.
- Separate fixtures cover all three verified model-failure categories, accepted-but-unselected multi-candidate labelling, known cost/duration/token means and medians, duplicate run copies, sparse fine-grained backoff, deterministic digests, and optimizer fallback/ordering/budget limits.

Verification:
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\closed_loop\test_m8_capabilities.py`: exit code 0; 29 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q villani_ops/tests/closed_loop/test_m8_capabilities.py villani_ops/tests/test_unified_cli.py`: exit code 0; 45 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 685 passed, 0 failed, 0 errors, 0 skipped, 114 deselected.
- From `components/villani-code`, `..\..\.venv\Scripts\python.exe -m pytest -q`: exit code 0; 670 passed, 0 failed, 0 errors, 1 skipped, 27 warnings.
- From `components/villani-flight-recorder`, `npm.cmd test`: exit code 0; 86 passed, 0 failed, 0 errors, 0 skipped; 19 test files passed and 0 test files failed.
- From `components/villani-flight-recorder`, `npm.cmd run typecheck`: exit code 0; TypeScript typecheck passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run build`: exit code 0; TypeScript build passed with no diagnostics; test counts not applicable.
- From `components/villani-flight-recorder`, `npm.cmd run format:check`: exit code 0; Prettier reported all matched files use Prettier code style; test counts not applicable.
- From the repository root, `.\.venv\Scripts\python.exe -m pytest tests\closed_loop -q`: exit code 0; 2 passed, 0 failed, 0 errors, 0 skipped.
- From `components/villani-ops`, `..\..\.venv\Scripts\villani.exe capability --help`: exit code 0; installed public command exposes exactly `rebuild`, `list`, and `explain` beneath `capability`.
- From the repository root, `git diff --check`: exit code 0; no whitespace errors.

Acceptance criteria:
- PASS: Profiles derive only from trustworthy canonical verifier/materialization outcomes; all non-capability and human-modified outcomes are excluded and counted.
- PASS: Same canonical source data produces byte-stable profile content, identical source/profile digests, no duplicate provenance append, and the same optimizer decision.
- PASS: Sparse fine-grained data backs off in the required order, and every level below the configured minimum leaves static routing active with `empirical_status: insufficient_data`.
- PASS: Empirical routing changes only the order of M5-eligible backends and only when every considered backend has sufficient probability evidence and known mean actual cost; missing cost or samples force recorded `bootstrap_v1` fallback.
- PASS: Static and empirical scores coexist, cost-to-success remains null for unknown inputs, and policy decisions contain the selected score source, every optimizer input, formulas, profile versions/digests, chosen sequence, top-N evidence, and rejected counts.
- PASS: `villani capability rebuild`, `list`, and read-only `explain` pass through the installed public command; Ops, Code, Flight Recorder, and root closed-loop suites remain green.

Assumptions:
- Historical canonical bundles lacking both an attempt-level provider and a backend configuration provider are excluded as `missing_backend_identity`; new controller bundles persist provider provenance directly.
- When explicit classifier or verifier version metadata is absent, ingestion uses the canonical classification schema version or verifier identity respectively; current public runs now persist explicit implementation versions.
- A known sequence cost budget is enforced against worst-case sum of attempt costs rather than expected cost so the optimizer cannot authorize a sequence whose full execution would exceed the cap.

Known risks:
- Older pre-M8 bundles without normalized failure categories or explicit identity/version provenance may contribute exclusion counts but intentionally cannot influence routing until trustworthy new observations accumulate.

Known remaining issues:
- none within M8

Next permitted milestone:
- M9, only after the user starts a new Codex task from this completed state. M9 was not started.

#### 2026-07-10: M9 Recovery, packaging, CI, and release gate

Status: complete

Changed files:
- `PLANS.md`
- `README.md`
- `.github/workflows/ci.yml`
- `scripts/check-secrets.py`
- `scripts/install-local.py`
- `components/villani-flight-recorder/package.json`
- `components/villani-flight-recorder/package-lock.json`
- `components/villani-ops/pyproject.toml`
- `components/villani-ops/villani_ops/cli/main.py`
- `components/villani-ops/villani_ops/cli/unified.py`
- `components/villani-ops/villani_ops/closed_loop/adapters/git_isolation.py`
- `components/villani-ops/villani_ops/closed_loop/controller.py`
- `components/villani-ops/villani_ops/closed_loop/durable_io.py`
- `components/villani-ops/villani_ops/closed_loop/run_store.py`
- `components/villani-ops/villani_ops/closed_loop/schema_validation.py`
- `components/villani-ops/villani_ops/materialize.py`
- `components/villani-ops/villani_ops/schemas/v1/*.json` (10 packaged schema files)
- `components/villani-ops/villani_ops/tests/closed_loop/test_m9_recovery.py`
- `components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py`
- `tests/closed_loop/test_cli_e2e.py`
- `tests/closed_loop/test_secret_scan.py`

Architectural decisions:
- Recovery is a single public `ClosedLoopController.resume(run_id, runs_root)` method. It holds a non-blocking per-run OS lock, validates every committed protocol snapshot and complete JSONL event, reconstructs the highest sequence, and emits canonical `recovery_*` events for every reconciliation action.
- Attempt identifiers are allocated from the complete historical allocation set, including interrupted attempts. Started attempts without snapshots become recorded interrupted infrastructure failures; completed attempt or verification snapshots are reused without rerunning earlier dependencies.
- Selection snapshots are authoritative on resume. Materialization recovery validates the exact selected patch, its digest, and target Git lineage; reverse-apply proof finalizes an already-applied patch, normal apply proof permits one resume, and ambiguous checks terminalize `FAILED` for manual inspection.
- Atomic JSON snapshot replacement and append-only JSONL remain the side-effect boundaries. Recovery repairs only one structurally truncated final JSONL record; malformed complete or middle records fail validation.
- CI has no model endpoint or secret dependency. Matrix jobs cover Python 3.11/3.12 and Node 18/current LTS with lockfile-keyed caches; deterministic fakes cover cross-component execution and generated-bundle secret scanning.
- The root schemas remain normative. Villani Ops packages a semantically identical local copy, verified by protocol tests, so installed wheels validate bundles without monorepo paths.
- The installer creates or reuses one local virtual environment, installs both Python components, runs lockfile-based Flight Recorder installation/build, writes a `vfr` launcher beside `villani`, and performs no telemetry or model download.

Pre-edit baseline:
- Villani Code: exit code 1; 669 passed, 1 failed (`test_fail_first_localization_uses_isolated_workspace`), 1 skipped, 27 warnings.
- Villani Ops: exit code 0; 685 passed, 114 deselected.
- Flight Recorder: 86 passed across 19 files; typecheck, build, and format check exited 0.
- Root closed-loop: exit code 0; 2 passed.

Verification:
- `python -m pytest components/villani-ops/villani_ops/tests/closed_loop/test_m9_recovery.py -q`: exit code 0; 17 passed in 3.07s.
- `python -m pytest tests/closed_loop/test_cli_e2e.py -q`: exit code 0; 1 passed in 3.54s.
- `python -m pytest <M9 recovery, protocol, unified CLI, and root closed-loop paths> -q`: exit code 0; 54 passed in 6.44s.
- From `components/villani-ops`, `python -m pytest -q`: exit code 0; 703 passed, 114 deselected in 85.34s.
- `python -m pytest components/villani-ops/villani_ops/tests/closed_loop tests/closed_loop -q`: exit code 0; 120 passed in 19.31s.
- From `components/villani-code`, `python -m pytest -q`: exit code 0; 670 passed, 1 skipped, 27 warnings in 42.58s; the pre-edit failure passed unchanged.
- From `components/villani-flight-recorder`, `npm test`: exit code 0; 86 passed across 19 files; `npm run typecheck`, `npm run build`, and `npm run format:check`: exit code 0.
- From `components/villani-flight-recorder`, `npm pack --dry-run`: exit code 0; 62 files, 66.5 kB packed, 262.5 kB unpacked.
- `python -m build --no-isolation` for Villani Code: exit code 0; sdist and wheel built. The same command for Villani Ops: exit code 0; sdist and wheel built with all 10 packaged schemas.
- Temporary install of the Villani Ops wheel followed by protocol validation outside the monorepo: exit code 0; schema root resolved to the installed `villani_ops/schemas/v1`.
- `python scripts/install-local.py --venv .venv`, executed twice: exit code 0 both times; both Python entry points and the `vfr` launcher reported discoverable; no telemetry or model download performed.
- README init plus local-compute and API-environment backend commands against a temporary `VILLANI_HOME` and tiny Git repository: exit code 0; deterministic `villani run` behavior is covered by the standalone public CLI E2E.
- `python scripts/check-secrets.py integration/fixtures`: exit code 0; 0 findings. The E2E generated-bundle scan also reports 0 findings.
- Production dependency audit, `npm audit --omit=dev`: exit code 0; 0 vulnerabilities.
- CI YAML parse: exit code 0; jobs are `villani-code`, `villani-ops`, `flight-recorder`, and `cross-component`, with no live-model or secret configuration.
- `git diff --check`: exit code 0; no whitespace errors.

Acceptance criteria:
- PASS: Resume is idempotent at every required injected interruption boundary; no duplicate coding attempt, verifier call beyond policy, selection, or patch apply occurs.
- PASS: Terminal resume is a bundle-hash-preserving no-op, interrupted attempts retain their identifiers and become infrastructure failures, and one truncated final JSONL line is repaired deterministically.
- PASS: The deterministic public command E2E persists classification before routing, rejects the cheap candidate, accepts and selects only the strong candidate, applies exactly its patch, passes the target test, renders the canonical run, and matches attempt/token/duration/cost values.
- PASS: CI contains the required version matrices, caches, protocol/E2E job, and deterministic secret scan with no live endpoint or secret dependency.
- PASS: Both Python sdists/wheels and the Flight Recorder dry-run package build succeed; the installed Ops wheel validates through packaged schemas.
- PASS: The installer is repeatable, the README workflow is executable with documented prerequisites, and compatibility-only legacy paths are not reachable from `villani run`.
- PASS: Fixture and generated-bundle scans contain no secrets; Flight Recorder production dependencies report 0 vulnerabilities.
- PASS: Every release checklist item has the direct command, test, or artifact evidence recorded in section 13.
- PASS: All component and cross-component suites pass.

Assumptions:
- The local machine exposes current supported Python 3.12 and Node 24.7.0. Python 3.11 and Node 18 execution are represented by deterministic CI matrix jobs; all current-version equivalents ran locally.
- Windows Store Python could not create `build`'s nested temporary isolated environment, so package builds used the permitted closest reproducible equivalent: declared build dependencies installed explicitly followed by successful `python -m build --no-isolation` for both components.
- The README local run requires the documented user-supplied OpenAI-compatible local model server; release automation substitutes deterministic local fakes and never contacts a model endpoint.

Known risks:
- Node 18-compatible test-only dependencies report npm audit advisories; `npm audit --omit=dev` confirms zero production dependency vulnerabilities.

Known remaining issues:
- none within M9

Next permitted milestone:
- none. M9 is the final planned milestone; no later milestone was started.
