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

`First Villani local-daemon core pass: complete`

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

#### 2026-07-10: Release-audit hardening pass

Status: blocked by the local Python/tooling environment; source changes are complete, but the release gate cannot be declared green until the supported test environment is available.

Changed files:
- `README.md`, `components/villani-ops/README.md`, `docs/CLOSED_LOOP.md`, and `.github/workflows/ci.yml`
- `components/villani-ops/villani_ops/providers.py`, backend/runner/classifier/verifier/controller, isolation, policy/capability, cost, protocol, schema, CLI, and new hardening/recovery tests
- `schemas/v1/{classification,run-manifest,verification}.schema.json` and matching packaged Ops schemas
- `tests/closed_loop/test_cli_e2e.py`
- Villani Flight Recorder provider/protocol/types/rendering/tests and generated `dist/` output

Architectural decisions:
- The public provider vocabulary is `local`, `openai-compatible`, and `openai`; public closed-loop validation maps all three to Villani Code's `--provider openai` mode, with explicit local URLs and the standard OpenAI URL default.
- Stage usage is represented by one backward-compatible `StageUsage` contract and aggregated into classification, coding, verification, selection, materialization, and total metrics. Coding duration remains separate from run wall-clock duration; classifier fallback/retry projection and verifier retry projection reserve configured worst cases under a cost cap.
- Attempt isolation exports tracked Git files only by default, preserves symlinks, enforces file/total bounds, and removes attempt worktrees unless explicit retention is configured.
- Classifier retries and configured alternate backends are persisted; an explicit conservative fallback event is emitted when all calls fail. Recovery is exposed through `villani resume RUN_ID` and `villani resume --latest`.
- Flight Recorder tolerates only structurally truncated final JSONL lines and renders currency, stage metrics, model calls, and wall-clock duration without mutating run bundles.

Verification:
- `python -m compileall -q components\\villani-ops\\villani_ops components\\villani-code\\villani_code tests\\closed_loop`: exit code 0.
- JSON schema validation with `python -m json.tool` over root and packaged schemas: exit code 0.
- `git diff --check`: exit code 0.
- `python -m pytest -q components\\villani-ops\\villani_ops\\tests`: blocked at collection with 78 errors because system Python 3.10's installed pydantic lacks `model_validator`.
- `python -m pytest -q components\\villani-code\\tests`: blocked at collection with 61 errors (pydantic v2 symbols, missing `httpx`, and Python 3.10 `StrEnum`/`tomllib` incompatibilities); 7 tests skipped during collection.
- `python -m pytest -q tests\\closed_loop`: blocked by the same system dependency mismatch. The workspace `.venv\\Scripts\\python.exe` also cannot launch because it targets a missing Windows Store Python 3.12 executable.
- `python -m pytest -q tests\\closed_loop\\test_secret_scan.py` with a workspace temp directory: exit code 0; 1 passed.
- Flight Recorder `cmd.exe /d /c npm test -- --run`: exit code 0; 19 files and 89 tests passed. `npm run typecheck`, `npm run build`, and `npm run format:check`: exit code 0.
- `cmd.exe /d /c npm audit --omit=dev`: blocked by the network request to `https://registry.npmjs.org/-/npm/v1/security/advisories/bulk`; no audit result was obtained.
- Flight Recorder `npm pack --dry-run` with a workspace npm cache: exit code 0; 62 files packaged (the later stage-metrics rendering change also builds and tests successfully).
- `python -m build --version`: unavailable (`No module named build`). The reproducible closest package check, `python -m pip wheel --no-deps --no-build-isolation --ignore-requires-python -w build-smoke components\\villani-code components\\villani-ops` with the existing workspace site-packages on `PYTHONPATH`, exited 0 and built both wheels.

Acceptance criteria:
- PASS: Provider compatibility, early configuration validation, real Villani Code local-stub E2E coverage, stage-separated accounting, currency-safe rendering, bounded isolation, cleanup/retention controls, classifier fallback, runner categories, Wilson-bound empirical qualification, public recovery, and strict JSONL final-line handling are implemented with tests and schemas.
- PASS: CI now has a package-smoke job that builds and installs both Python wheels, runs `villani --help`, exercises the missing-run resume path, and executes the README-shaped local-stub E2E with the installed Villani Code command.
- PASS: Flight Recorder tests, typecheck, build, format check, and package dry-run pass.
- BLOCKED: Full Python component suites, root closed-loop suite, supported package-install smoke tests, and production npm audit require a supported Python 3.11+ environment, complete dependency installation, and network access for the audit endpoint.

Assumptions:
- Existing `*_cost_usd` field names remain for wire compatibility; the canonical currency field and all user-facing cost displays carry the configured ISO-style currency, so non-USD local compute is never presented as USD.
- Selection and materialization remain deterministic and make no model calls, so their stage metrics are explicitly `not_applicable`.

Known risks:
- The local environment cannot execute the Python test suites, so runtime coverage of the newly added Python tests is pending a supported environment.
- Temporary wheel-build directories (`build-smoke`, `.pip-cache`, and `components/villani-ops/build`) were created by the successful package check. Cleanup commands were rejected by the command-review usage limit and remain to be removed in a later environment turn.

Known remaining issues:
- Release gate remains blocked by the dependency/runtime and npm audit environment failures above; no source-level failure was observed in the checks that could run.

Next permitted milestone:
- none. This is the final planned milestone; no later milestone was started.

#### 2026-07-11: Release-audit repair pass

Status: complete

Changed files:
- Release automation and hygiene: `.github/workflows/ci.yml`, `.gitignore`, component `.gitignore` files, `pytest.ini`, and removal of tracked `.pip-cache`, `build-smoke`, and `components/villani-ops/build` artifacts.
- Villani Code: CLI task-file boundary, repository-scoped Git evidence, command-environment filtering, Windows executable discovery, and focused tests.
- Villani Ops: accounting and controller policy, bounded legacy non-Git isolation and materialization, deterministic CLI validation, empirical capability reporting, verifier evidence extraction, runner failure classification, command resolution, focused typing fixes, and regression tests.
- Cross-component and documentation: `tests/closed_loop/test_cli_e2e.py`, `docs/CLOSED_LOOP.md`, and Flight Recorder `.prettierrc.json`. Tracked Flight Recorder `dist` remains the intentional package payload and has no semantic diff.

Architectural decisions:
- Non-billed stages with `not_applicable` accounting do not consume or poison a fully known coding-attempt budget; genuinely unknown monetary spend remains `null` with an accounting status.
- The canonical public `villani run` path still requires Git and tracked-files-only isolation. Compatibility-only legacy orchestrators may use a bounded non-Git snapshot that preserves symlinks, rejects oversized files/snapshots, and excludes environment files, virtual environments, dependency trees, caches, build output, Villani state, and known secret files.
- Empirical capability evidence is an independent eligibility source from static capability scoring, and both sources are persisted.
- Direct deterministic validation command evidence satisfies the validation-artifact requirement without inventing a duplicate missing `validations.jsonl` failure; verifier errors and genuinely missing acceptance evidence remain ineligible.
- The hermetic E2E uses an in-test Git repository, stdlib `unittest`, installed `villani` and `villani-code` entry points, a loopback OpenAI-compatible stub, both proxy modes, and actual Flight Recorder rendering.

Verification:
- From `components/villani-code`, `python -m pytest -q`: exit code 0; 671 passed, 1 skipped, 27 warnings in 46.88s.
- From `components/villani-ops`, `python -m pytest -q --basetemp final-temp-full-0711c`: exit code 0; 730 passed, 114 deselected in 98.84s.
- From the repository root, `python -m pytest tests/closed_loop -q --basetemp root-final-temp-0711b`: exit code 0; 6 passed in 15.29s.
- From the repository root with installed entry points on `PATH`, `python -m pytest tests/closed_loop/test_cli_e2e.py -m e2e -q`: exit code 0; 2 passed, 1 deselected in 12.56s. The fresh-wheel environment rerun also exited 0 with 2 passed and 1 deselected in 12.45s.
- From Flight Recorder, `npm ci`: exit code 0; 135 packages added and 136 audited. `npm test`: exit code 0; 19 files and 89 tests passed. `npm run typecheck`, `npm run build`, and `npm run format:check`: exit code 0. `npm audit --omit=dev`: exit code 0; 0 vulnerabilities. `npm pack --dry-run`: exit code 0; 62 files, 67.2 kB packed, 265.3 kB unpacked.
- Isolated `python -m build --wheel` for Villani Code and Villani Ops: exit code 0; `villani_code-0.1.0rc1-py3-none-any.whl` and `villani_ops-0.2.0-py3-none-any.whl` built. Fresh Python 3.12 environment installation, `villani --help`, and `villani-code --help`: exit code 0.
- Villani Code focused `ruff check --select E9,F ...`: exit code 0; all checks passed. Focused `mypy ...`: exit code 0; no issues in 3 source files.
- Villani Ops focused `ruff check --select E9,F ...`: exit code 0; all checks passed. Focused `mypy --follow-imports=skip --ignore-missing-imports ...`: exit code 0; no issues in 28 source files.
- `python scripts/check-secrets.py integration/fixtures`: exit code 0; 1 root, 0 findings. The generated fresh-wheel E2E tree scan also exited 0 with 1 root and 0 findings.

Acceptance criteria:
- PASS: All required component, closed-loop, E2E, Flight Recorder, isolated-wheel, installed-entry-point, focused Ruff/mypy, and secret-scan commands exit zero on local Python 3.12.
- PASS: The real local-stub E2E applies the correct patch and reaches `COMPLETED` in both proxy modes without a live provider, paid model, global pytest dependency in the target repository, or secret.
- PASS: CI runs Python 3.11/3.12 on Linux, executes the real package-smoke E2E without a conditional executable skip, and validates both installed CLI entry points.
- PASS: No test was deleted, xfailed, broadly skipped, or weakened to hide a regression; all pytest markers used by root tests are registered.
- PASS: Generated caches, wheel smoke outputs, component build trees, egg metadata, and `node_modules` are excluded from the deliverable. Flight Recorder `dist` remains because the npm package contract intentionally tracks it.

Assumptions:
- The audit's Linux baseline (Ops 698 passed/23 failed/114 deselected; Code 669 passed/1 failed/1 skipped) is the reproduction source of truth. This workstation has no installed WSL distribution or running Docker daemon, so the repaired Linux execution is enforced by the existing Ubuntu CI matrix rather than duplicated locally.
- Python 3.12 is the local supported interpreter used for the complete release gate; Python 3.11 remains covered by CI.

Known risks:
- Linux Python 3.11/3.12 results depend on the next CI run; the local complete run was Windows Python 3.12. The repaired failures are cross-platform or have explicit platform-safe tests, but no Linux runtime was available in this workspace.
- Sandbox-owned ignored pytest directories may remain physically present on this workstation because their ACLs reject deletion even outside the managed sandbox; they are ignored, untracked, and absent from the deliverable. All tracked generated artifacts were removed.

Known remaining issues:
- none within this release-audit repair pass

Next permitted milestone:
- none. Prompt 01 and all later roadmap work were not started.

#### 2026-07-11: Prompt 01 versioned shared contracts pass

Status: complete

Changed files:
- Normative and packaged contracts: `schemas/v2/*.schema.json`, `components/villani-ops/villani_ops/schemas/v2/*.schema.json`, and the Ops package-data declaration. All eight v1 schemas and fixtures remain semantically unchanged.
- Python models, validation, translation, and tests: `components/villani-ops/villani_ops/closed_loop/protocol_v2.py`, `translate_v2.py`, `schema_validation.py`, `__init__.py`, and `components/villani-ops/villani_ops/tests/closed_loop/test_protocol_v2.py`.
- TypeScript models, strict reader, validation, generated distribution files, and tests: `components/villani-flight-recorder/src/providers/villaniProtocolV2.ts`, `villaniSchemaValidation.ts`, their `dist/providers` outputs, and `test/villaniProtocolV2.test.ts`.
- Shared valid, invalid, byte-digest, and translation-golden fixtures: `integration/fixtures/protocol/v2/**`.
- Compatibility and migration records: `docs/PROTOCOL_V2_COMPATIBILITY.md` and `docs/decisions/ADR-001-v2-transport-contract.md`.
- `PLANS.md` progress section only.

Architectural decisions:
- v1 remains the durable local run-bundle contract; v2 is a separate transport and platform contract for runners, future process/control-plane boundaries, and observability. No v1 schema or fixture was changed.
- Telemetry carries an explicit idempotency key and causal run/trace/span identity. Namespaced SHA-256 deterministically maps legacy trace and event IDs to non-zero W3C-shaped IDs, while preserving original IDs in attributes.
- Translation preserves v1 sequence and known parent links only. Because v1 has one recorded clock, that timestamp is projected to both required clock fields with `villani.clock.status: legacy_single_timestamp`; cost, tokens, outcomes, distinct observation times, tenancy, and missing parents are never inferred.
- Known span kinds are documented, while lower-case future kinds remain readable. Strict top-level documents and open attributes/body maps form the forward-compatibility boundary.
- Artifact descriptors contain metadata and opaque storage references only. Artifact-byte-shaped telemetry body properties are schema-invalid. Outcomes use nullable facts plus explicit accounting and provenance status; unknown cost is never zero.
- Root v2 schemas are normative and Ops packages a semantically identical copy. Python and TypeScript validate the same fixture bytes and reason categories.

Verification:
- Shared Python v1/v2 contracts, invalid reason categories, cross-language byte manifest, schema duplication, and translation goldens: `python -m pytest -q components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py components/villani-ops/villani_ops/tests/closed_loop/test_protocol_v2.py tests/closed_loop/test_protocol_contract.py --basetemp .test-temp-v2-final-targeted`: exit code 0; 35 passed in 0.57s.
- Shared TypeScript v1/v2 targeted contracts: `npm test -- --run test/villaniProtocol.test.ts test/villaniProtocolV2.test.ts`: exit code 0; 2 files and 24 tests passed.
- Villani Ops full suite: `python -m pytest -q`: exit code 0; 746 passed and 114 deselected in 88.66s.
- Villani Code full suite, with `GIT_CEILING_DIRECTORIES` bounded at the component root so the dirty parent contract pass is not attributed to non-Git temp tasks: `python -m pytest -q --basetemp final-temp-v2-code-final`: exit code 0; 671 passed, 1 skipped, and 28 warnings in 39.25s.
- Root closed-loop suite after final contract changes: `python -m pytest tests/closed_loop -q --basetemp final-temp-v2-root-last`: exit code 0; 6 passed in 15.25s.
- Flight Recorder full suite: `npm test`: exit code 0; 20 files and 102 tests passed. `npm run typecheck`, `npm run build`, and `npm run format:check`: exit code 0.
- v2 schema formatting: repository Prettier `--check` over root and packaged `schemas/v2`: exit code 0; all matched files use Prettier code style.
- Python focused Ruff check over v2 models, translation, validators, exports, and tests: exit code 0; all checks passed.
- Root and packaged v2 semantic duplicate assertion: included in the 35-test shared Python contract run and the 746-test full Ops run; passed.
- `git diff --check`: exit code 0; no whitespace errors before the progress update and repeated after it.

Acceptance criteria:
- PASS: Existing v1 Python and TypeScript readers and fixtures remain green and v1 semantics are unchanged.
- PASS: Python and TypeScript accept all eight valid v2 fixture documents and reject all nine invalid documents for identical reason categories.
- PASS: Repeated translation of both the one-attempt failed run and two-attempt completed run is normalized-byte stable and matches checked-in SHA-256 goldens.
- PASS: Every telemetry event requires an idempotency key, run identity, W3C-shaped trace identity, and span identity; missing causal identity is schema-invalid.
- PASS: Artifact bytes are excluded, unknown accounting is explicit, future span kinds remain readable, and root/package schemas are semantically identical.
- PASS: No server, network uploader, database, daemon, or UI was introduced.

Assumptions:
- Copying v1's sole recorded timestamp into both required v2 time fields is a lossless single-clock projection, not a claim that a distinct observer clock was measured; the clock-status attribute makes that limitation explicit.
- Stable legacy parent-event relationships are safe to project to parent-span IDs; null legacy parents remain null.

Known risks:
- Hash-based legacy ID mapping is deterministic and W3C-shaped but is not intended to reconstruct the original identifier without the preserved legacy attribute.
- The initial unbounded Code run observed the known dirty-parent test artifact; the bounded final full run passed. The sandboxed Ops run could not terminate its Windows child-process test, so the final exact Ops gate ran outside that process restriction and passed.

Known remaining issues:
- none within Prompt 01

Next permitted milestone:
- Prompt 02 only after the user starts a new task. Prompt 02 was not started.

#### 2026-07-11: First Villani local-daemon core pass

Status: complete

Changed files:
- New Python 3.11+ package and entry point: `components/villani-agentd/pyproject.toml`, `README.md`, `.gitignore`, `villani_agentd/*.py`, and `tests/test_agentd_core.py`.
- Installation and release automation: `scripts/install-local.py`, root `README.md`, and `.github/workflows/ci.yml`.
- `PLANS.md` progress section only.

Architectural decisions:
- `villani-agentd` depends on the packaged `villani-ops` v2 protocol models and validators. It does not copy schemas or protocol definitions.
- The local API uses the Python standard-library threaded HTTP server and client. Normal operation binds and connects only to loopback; both server lifecycle and client reject non-loopback use unless the server receives the explicit insecure-development flag. No uploader or other network client exists.
- Endpoint discovery is written atomically to `~/.villani/agentd/endpoint.json`. A separately generated URL-safe bearer token is never passed on a process command line; POSIX mode is `0600`, and Windows inheritance is removed with a user-only full-control ACL so lifecycle cleanup remains possible.
- SQLite owns committed local state with WAL, full synchronous commits, busy timeouts, one transaction per event batch, unique `event_id`, and unique `(run_id, sequence_scope, sequence)`. Same-content replay is idempotent; identity or sequence reuse with different content is a conflict.
- Events remain normalized `TelemetryEnvelopeV2` bytes in deterministic JSON with `upload_state='offline'`, zero retries, and no upload destination. Run registration and finalization are local metadata only.
- Artifact content is SHA-256 and size verified, atomically materialized into a content-addressed `sha256/<prefix>/<digest>` tree, and represented in SQLite by validated descriptors and storage references.
- Generic wrapping uses `subprocess.Popen` with an argument vector and `shell=False`, dedicated process groups/sessions, concurrent pipe draining, explicit stdout/stderr and body truncation metadata, and child exit-code propagation. Ctrl-C cancellation terminates the process tree; Windows group behavior is covered through mocks and real Windows execution.
- Limits cover stdout, stderr, event body, individual artifact, per-run artifacts, and serialized event spool size. Structured logs are JSON lines and redact authorization, token, secret, and artifact-content fields.
- Start/status/stop/doctor are lifecycle-only commands. The root installer installs the daemon entry point but does not create an endpoint, token, process, or spool automatically.

Verification:
- From `components/villani-agentd`, `python -m pytest -q --basetemp .test-temp/final2`: exit code 0; 21 passed in 5.92s.
- Agentd Ruff formatting check: exit code 0; 12 files already formatted. Ruff lint: exit code 0; all checks passed. Mypy with skipped external imports: exit code 0; no issues in 11 source files.
- Agentd secret scan, `python scripts/check-secrets.py components/villani-agentd`: exit code 0; 1 root and 0 findings.
- Villani Ops full suite: `python -m pytest -q`: exit code 0; 746 passed and 114 deselected in 90.27s.
- Villani Code full suite with Git discovery bounded at the component root: `python -m pytest -q --basetemp final-temp-agentd-code`: exit code 0; 671 passed, 1 skipped, and 28 warnings in 40.82s.
- Root closed-loop suite: `python -m pytest tests/closed_loop -q --basetemp final-temp-agentd-root`: exit code 0; 6 passed in 15.95s.
- Flight Recorder: `npm test`: exit code 0; 20 files and 102 tests passed. `npm run typecheck`, `npm run build`, and `npm run format:check`: exit code 0.
- Final non-isolated wheel builds: exit code 0; `villani_agentd-0.1.0-py3-none-any.whl` (19,213 bytes) and dependency wheel `villani_ops-0.2.0-py3-none-any.whl` (644,975 bytes).
- Fresh Python 3.12 wheel installation and installed-entry-point smoke: exit code 0. Installed `start`, `status`, `doctor`, generic wrap, and `stop` succeeded; the shell-free wrapped Python process returned 13 and `villani-agentd` returned the same exit code.
- Root installer repeatability: `python scripts/install-local.py --venv .venv`: exit code 0. `villani-agentd --help` succeeded and `INSTALLER_AGENTD_NOT_STARTED=1` confirmed no endpoint or daemon was created.
- CI YAML parse: exit code 0; jobs are `villani-code`, `villani-ops`, `villani-agentd`, `flight-recorder`, `cross-component`, and `package-smoke`. Agentd has Python 3.11/3.12 tests plus Ruff/mypy, and package smoke builds and installs the wheel before lifecycle checks.
- `git diff --check`: exit code 0 before the progress update and repeated after it.

Acceptance criteria:
- PASS: Replaying an identical event batch stores one copy and reports duplicates without consuming spool capacity.
- PASS: Reusing a sequence or event identity with different normalized content is rejected and the transaction rolls back.
- PASS: A real daemon termination and restart retains two committed offline events.
- PASS: Generic wrapping never requests a shell, bounds both streams, propagates cancellation, and preserves real and mocked nonzero child exit codes.
- PASS: Health is public; status, run, batch, artifact, and finalize endpoints all reject missing authentication.
- PASS: Default endpoint and every runtime client destination are loopback. No cloud synchronization occurs.
- PASS: Artifact digest/size mismatch and file/per-run limits are rejected. Concurrent writers, WAL restart, structured-log redaction, and Windows process flags are covered.
- PASS: Existing Ops, Code, root closed-loop, and Flight Recorder suites remain green.
- PASS: No Codex/Claude adapter, cloud synchronization, remote worker, or web UI was introduced.

Assumptions:
- `villani-ops` remains the packaged owner of the shared Python v2 protocol implementation for this pass; a later dedicated protocol distribution may replace that dependency without changing wire bytes.
- The spool limit is enforced against normalized serialized event payload bytes; SQLite page/WAL bookkeeping is implementation overhead rather than accepted telemetry capacity.

Known risks:
- The standard-library HTTP surface is intentionally local and minimal. The insecure-development bind override is not suitable for production or untrusted networks.
- Linux process-group and token-mode behavior is exercised by the Python 3.11/3.12 CI matrix; the complete local run was Windows Python 3.12 with Linux-specific branches represented by portable code and mocks.

Known remaining issues:
- none within this local-daemon core pass

Next permitted milestone:
- A later daemon/adapters/cloud pass only after the user explicitly starts it. Codex/Claude adapters, cloud synchronization, remote workers, and web UI were not started.

#### 2026-07-11: Local observation adapters and normalization pass

Status: complete

Changed files:
- Adapter contract, implementations, normalization, OTLP ingestion, trace propagation, diagnostics, wrapping, process callbacks, and limits under `components/villani-agentd/villani_agentd/`.
- Synthetic fixtures and contract coverage in `components/villani-agentd/tests/fixtures/adapters/`, `test_adapters.py`, and the extended daemon-core tests.
- Adapter documentation in `components/villani-agentd/README.md` and `docs/OBSERVATION_ADAPTERS.md`.
- `PLANS.md` progress section only.

Architectural decisions:
- `AgentAdapter` is a typed observation contract covering identity/version, capability detection, argument-vector construction, incremental parsing, final outcome parsing, process-tree cancellation, and sensitive-field policy. Adapters share the packaged v2 models from Villani Ops; no protocol definition is copied.
- `generic-process` retains bounded shell-free lifecycle capture. `generic-jsonl` accepts validated v2 envelopes or explicit dotted-field mappings. Villani Code consumes its native runtime/debug JSONL shape. Codex requires `codex exec --json`; Claude Code requires `--output-format stream-json`. Provider CLI feature detection uses only executable version/help output and reports exact version plus named missing capability. There is no decorated-terminal fallback or private-session-directory discovery.
- Incremental JSONL parsing buffers partial lines, emits deterministic redacted parse-error records for malformed middle/truncated final records, ignores byte-equivalent native duplicates, and assigns deterministic revisions to changed records that reuse a native ID. Native IDs, provider names, event types, and revision numbers remain queryable attributes.
- Model, tool, command, file, error, and terminal records normalize to schema-valid v2 causal spans. Parent IDs are correlated when present, token revisions remain numeric, and secret-shaped values plus sensitive fields are redacted before spooling.
- Wrapped children receive W3C `traceparent` and Villani run identity without a shell. A valid inherited context is preserved as the causal parent; an invalid context is replaced. Existing process-group cancellation and child exit propagation remain unchanged.
- Authenticated OTLP/HTTP JSON traces are accepted at `/v1/traces` and `/v1/otlp/v1/traces`. GenAI semantic attributes are projected into normalized fields, all unknown attribute keys remain queryable (subject to value redaction), and malformed or oversized payloads are rejected atomically under a configurable limit.
- This pass adds observation only. It adds no backend routing, cloud synchronization, remote execution, database beyond the existing local spool, or UI.

Verification:
- Final agentd suite: `python -m pytest -q --basetemp .test-temp/adapters-final4`: exit code 0; 35 passed in 6.49s (one non-failing pytest cache warning caused by host ACLs).
- Agentd Ruff formatting and lint: exit code 0; all 19 files formatted and all checks passed. Mypy with skipped external imports: exit code 0; no issues in 17 source files.
- Agentd secret scan: `python scripts/check-secrets.py components/villani-agentd`: exit code 0; 1 root and 0 findings.
- Villani Ops full suite: `python -m pytest -q`: exit code 0; 746 passed and 114 deselected in 99.10s.
- Villani Code full suite with workspace-local temp storage: exit code 0; 671 passed, 1 skipped, and 28 warnings in 44.04s.
- Root closed-loop suite with workspace-local temp storage: exit code 0; 6 passed in 17.90s.
- Flight Recorder `npm.cmd test`: exit code 0; 20 files and 102 tests passed in 5.01s. `npm.cmd run typecheck`, `npm.cmd run build`, and `npm.cmd run format:check`: exit code 0; TypeScript compiled and all Prettier files matched.
- Agentd wheel build without isolation: exit code 0; `villani_agentd-0.1.0-py3-none-any.whl` built and inspected successfully, including the adapter contract/implementations and OTLP module.
- Workspace-local `villani-agentd doctor`: exit code 0; all five adapters were listed with exact capabilities, detected versions, and named missing capabilities without requiring provider authentication.
- `git diff --check`: exit code 0 (line-ending notices only).

Acceptance criteria:
- PASS: Every synthetic adapter fixture normalizes to schema-valid v2 events and replay is byte-stable.
- PASS: Native IDs and raw provider names remain queryable; duplicate native IDs and token revisions do not create identity collisions.
- PASS: Missing or incapable Codex/Claude CLIs are isolated to their own doctor entries with exact detected version and missing capability. Generic and Villani adapters remain independently usable.
- PASS: Partial lines, malformed middle records, truncated final records, tool nesting, interruption, shell-free execution, and secret-shaped output are covered without terminal scraping or implicit provider-session reads.
- PASS: Authenticated OTLP ingestion maps GenAI attributes, preserves unknown attributes, rejects malformed/oversized requests deterministically, and replays idempotently.
- PASS: Existing daemon persistence, uniqueness, limits, authentication, loopback, cancellation, Windows process mocks, and child exit behavior remain green.
- PASS: Existing Ops, Code, root closed-loop, and Flight Recorder suites remain green.

Assumptions:
- Provider CLI help/version output is the authoritative local feature-discovery surface. Actual provider authentication is intentionally neither required nor tested.
- User-configured best-effort session-file observation remains a possible future explicit feature; this pass performs no provider directory discovery and adds no such configuration surface.

Known risks:
- Provider vendors may revise documented JSON event variants. Unknown record fields remain in redacted bodies and open v2 kinds remain readable, but new correlation shapes may require a future adapter-version update.
- OTLP integer values arrive as JSON strings by specification and are normalized to Python integers; very large values remain bounded later by v2/SQLite validation.

Known remaining issues:
- None within the observation-adapter and normalization pass. Initial Windows runs that used the protected global pytest temp root failed at fixture setup only; the required suites passed with workspace-local `--basetemp` paths.

Next permitted milestone:
- A later cloud-sync, routing, remote-worker, or UI pass only after the user explicitly starts it. None was started here.

#### 2026-07-11: Local distribution and lifecycle management pass

Status: complete

Changed files:
- New end-user Python distribution under `components/villani/` with platform wheel metadata, four console entry points, native Flight Recorder launcher, user-service management, upgrade checks, frozen entry point, signing placeholder, and distribution tests.
- Release tooling: `scripts/build-vfr-standalone.py`, `scripts/build-release.py`, and `scripts/ci-package-smoke.py`.
- Daemon lifecycle and spool migration support in `components/villani-agentd/villani_agentd/cli.py`, `lifecycle.py`, and `spool.py`, with expanded daemon tests.
- Public Flight Recorder install guidance in `components/villani-ops/villani_ops/cli/unified.py`.
- Root development installer, distribution CI matrix, root README, `docs/DISTRIBUTION.md`, and `docs/release-signing/README.md`.
- `PLANS.md` progress section only.

Architectural decisions:
- The supported user artifact is one platform-specific Python distribution named `villani` at version `0.3.0rc1`. It pins and depends on the independently installable internal Python distributions and owns the installed `villani`, `villani-code`, `villani-agentd`, and `vfr` entry points. `pipx install villani` is the intended publication path; this pass publishes nothing.
- Flight Recorder remains TypeScript-owned. Release builds compile its existing `dist/cli.js` and npm dependencies with pinned Bun 1.2.20 into a native per-platform executable embedded in the `villani` wheel and release ZIP. Node.js, npm, and Bun are build-time dependencies only. The monorepo installer retains an explicit Node-based development launcher.
- Platform wheels are deliberately non-pure and tagged for the CI host platform. PyInstaller creates a shared frozen Python runtime that is exposed under the three Python command names; the native Flight Recorder is the fourth executable in the self-contained archive.
- User services never require administrator privileges by default: systemd user unit on Linux, launchd user agent on macOS, and per-user Task Scheduler task on Windows. `villani-agentd service-run` is the foreground service target. CI redirects definitions and dry-runs platform commands as a documented VM approximation.
- `villani uninstall-service` removes only the service definition. Local configuration, runs, artifacts, and spool remain unless both `--delete-data` and `--confirm-delete-data` are supplied, and unsafe deletion roots are refused.
- Upgrade checks preserve legacy configuration and run bytes, validate supported config/protocol majors, migrate a known SQLite spool from `user_version=0` to 1, and refuse newer or structurally unknown spools. Direct daemon initialization performs the same spool-version/layout guard.
- Release archives use fixed member ordering, timestamp, permissions, and compression. CI verifies deterministic archive assembly and generated `SHA256SUMS`. Signing records are explicit unsigned-release placeholders; no credential or fabricated signature exists.
- Windows, macOS, and Linux release jobs build and smoke their own artifacts. A platform is not documented as supported merely because build code exists; support requires that platform's CI artifact to pass.

Verification:
- Distribution tests: `python -m pytest -q --basetemp .test-temp/distribution-final`: exit code 0; 9 passed in 0.60s.
- Distribution Ruff and mypy: exit code 0; all checks passed and no issues in 6 source files. Root packaging-script Ruff: exit code 0.
- Agentd final suite: exit code 0; 36 passed in 6.37s. Agentd Ruff: all checks passed. Earlier final mypy: no issues in 17 source files.
- Villani Ops final full suite: exit code 0; 746 passed and 114 deselected in 92.64s.
- Villani Code full suite: exit code 0; 671 passed, 1 skipped, and 28 warnings in 44.69s.
- Root closed-loop suite: exit code 0; 6 passed in 17.95s.
- Flight Recorder: `npm.cmd test`: exit code 0; 20 files and 102 tests passed in 4.90s. Typecheck, build, and format check: exit code 0.
- Root development installer repeatability after adding the umbrella distribution and `--no-build-isolation`: exit code 0; all four development commands installed, daemon not started.
- Fresh isolated Windows wheel install from four locally built wheels: exit code 0; `villani==0.3.0rc1` and pinned internal distributions installed with third-party dependencies. All four commands passed; `vfr --help` passed with Node removed from `PATH`.
- Isolated wheel user-service smoke: exit code 0; install, status, and uninstall passed through the redirected Windows per-user Task Scheduler strategy; preserved run data remained.
- Final self-contained Windows RC: `villani-0.3.0rc1-windows-amd64.zip`, 240,084,891 bytes. Extracted `villani`, `villani-code`, `villani-agentd`, and `vfr` all passed with Node absent from `PATH`; service lifecycle approximation and data preservation passed.
- Final archive checksum verification: exit code 0; SHA-256 `eb3c7bd68f366b1bb1d1f33ab8d2592317af78401b71bb5dfa45458c9e94d52c` matched `SHA256SUMS`.
- Upgrade fixture: legacy config and run manifest remained byte-identical, the existing event row survived, and SQLite migrated from version 0 to 1. Newer versions and unknown legacy table layouts were rejected.
- CI YAML parse: exit code 0; `distribution-smoke` is a Windows/macOS/Linux matrix that builds native vfr, platform wheels, fresh installs, service approximations, PyInstaller archives, checksums, and uploaded RC artifacts.
- Secret scan across `components/villani`, docs, and scripts: exit code 0; 3 roots and 0 findings. `git diff --check`: exit code 0 with line-ending notices only.

Acceptance criteria:
- PASS locally on Windows: one platform wheel provides all four commands from a fresh isolated install without Node.js at runtime.
- PASS locally on Windows: the self-contained release ZIP provides all four commands, verifies its checksum, and contains an explicit unsigned signing placeholder.
- PASS by implementation and test: systemd user, launchd user-agent, and Windows per-user Task Scheduler definitions require no administrator path; cross-platform definition generation is covered by synthetic platform tests.
- PASS: service uninstall preserves run data by default and destructive removal requires two explicit flags.
- PASS: previous-package fixtures preserve config, canonical runs, and SQLite events while applying the supported version migration.
- PASS: internal Python package installs and the root monorepo development installer remain available.
- PASS: existing Ops, Code, daemon, root closed-loop, and Flight Recorder suites remain green.
- PENDING EXTERNAL CI EVIDENCE: macOS and Linux support is not claimed until the new matrix jobs complete and upload their platform smoke artifacts. This Windows workspace cannot produce those platform binaries.

Assumptions:
- Internal distributions will be published at their pinned versions before a future public `villani` wheel is published; local RC installation uses the colocated wheel directory.
- Bun's compiled executable is the Flight Recorder runtime boundary. Node-compatible APIs used by the current Flight Recorder are covered by each platform's `vfr --help` and existing Flight Recorder suite before artifact upload.

Known risks:
- The Windows self-contained ZIP is large because it contains three command-named copies of the shared one-file Python runtime plus the native Flight Recorder. Size optimization is deferred; functionality and isolation were prioritized in this packaging pass.
- PyInstaller and Bun output bytes can change with toolchain versions. Bun is pinned; CI must pin PyInstaller before public release provenance is considered reproducible across time.
- Hosted CI execution is required before macOS or Linux can be promoted from configured build targets to supported release platforms.

Known remaining issues:
- No local functional failures. macOS/Linux artifact status is awaiting external CI execution and is deliberately not represented as completed support.

Next permitted milestone:
- A later publication or hosted-services pass only after the user explicitly starts it. No package was published, and no hosted service, account, or cloud feature was added.
