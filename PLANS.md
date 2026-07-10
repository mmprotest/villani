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

- [ ] `villani run` is the only documented primary execution path.
- [ ] Classification is persisted before the first coding backend decision.
- [ ] Every attempt is isolated.
- [ ] Every attempt has patch, logs, telemetry, and verification references.
- [ ] No verifier error can become acceptance.
- [ ] Selection considers only acceptance-eligible candidates.
- [ ] Only the selected patch is materialized.
- [ ] Unknown cost is never numeric zero.
- [ ] Local compute cost can be configured and displayed.
- [ ] Policy alternatives and rejection reasons are visible.
- [ ] Run replay works directly from `~/.villani/runs`.
- [ ] Tokens, costs, duration, model calls, tool calls, commands, and file mutations are visible when captured.
- [ ] Secrets are redacted.
- [ ] Interruption recovery is idempotent.
- [ ] All tests and CI checks pass.
- [ ] The quickstart works from a clean machine with documented prerequisites.

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

`M2: complete`

### Milestone status

- [x] M0: Monorepo and measured baseline
- [x] M1: Green component baseline
- [x] M2: Canonical protocol
- [ ] M3: Deterministic controller with fakes
- [ ] M4: Real attempt, verifier, selector, and materializer adapters
- [ ] M5: Cost accounting and bootstrap escalation policy
- [ ] M6: Unified public CLI
- [ ] M7: Native Flight Recorder observability
- [ ] M8: Empirical capability registry and optimizer
- [ ] M9: Recovery, packaging, CI, and release gate

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
