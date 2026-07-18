# Villani Closed Loop

## Product boundary for version 1

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

Empirical routing is an eligibility signal only when
`capabilities.minimum_empirical_samples` and
`capabilities.minimum_empirical_wilson_lower_bound` are met (the latter
defaults to `target_success_probability`). The history is policy-selected and
censored (for example, rejected or infrastructure-failed candidates are not a
random sample), so empirical profiles must not be interpreted as randomized
model-performance estimates.

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

## Canonical controller states

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
AWAITING_APPROVAL
MATERIALIZING
COMPLETED
EXHAUSTED
FAILED
CANCELLED
```

Allowed transitions:

```text
CREATED -> CLASSIFYING | CANCELLED
CLASSIFYING -> CLASSIFIED | FAILED | CANCELLED
CLASSIFIED -> POLICY_SELECTED | EXHAUSTED | FAILED | CANCELLED
POLICY_SELECTED -> ATTEMPT_RUNNING | VERIFYING | SELECTING | EXHAUSTED | FAILED | CANCELLED
ATTEMPT_RUNNING -> ATTEMPT_COMPLETED | FAILED | CANCELLED
ATTEMPT_COMPLETED -> VERIFYING | REJECTED | FAILED | CANCELLED
VERIFYING -> VERIFIED | FAILED | CANCELLED
VERIFIED -> SELECTING | REJECTED | FAILED | CANCELLED
REJECTED -> POLICY_SELECTED | ESCALATING | EXHAUSTED | FAILED | CANCELLED
ESCALATING -> POLICY_SELECTED | EXHAUSTED | FAILED | CANCELLED
SELECTING -> AWAITING_APPROVAL | MATERIALIZING | EXHAUSTED | FAILED | CANCELLED
AWAITING_APPROVAL -> MATERIALIZING | COMPLETED | FAILED | CANCELLED
MATERIALIZING -> COMPLETED | FAILED
```

A retry on the same backend is represented by `REJECTED -> POLICY_SELECTED`. An escalation is represented by `REJECTED -> ESCALATING -> POLICY_SELECTED`. The corresponding policy event records why the backend stayed the same or changed.

Terminal states are `COMPLETED`, `EXHAUSTED`, `FAILED`, and `CANCELLED`. Terminal states cannot
transition. Cancellation requests propagate to the active runner, which terminates its process
tree when required; the controller then preserves recorded evidence, requests isolation cleanup,
and records whether the target repository was modified.

## Canonical run bundle

Every run is stored at `~/.villani/runs/<run_id>/`, or beneath a test-provided run root. The required layout is:

```text
<run_id>/
  manifest.json
  task.json
  classification.json
  state.json
  events.jsonl
  policy_decisions.jsonl
  agent-systems/
    index.json
    <system-id>.json
    migration.json
  attempts/
    attempt_001/
      attempt.json
      harness-result.json
      worktree.json
      patch.diff
      stdout.log
      stderr.log
      runner_telemetry.json
      repository-validation.json
      validation-coverage.json
      trace/
    attempt_002/
      ...
  verification/
    attempt_001.json
    attempt_002.json
  candidate_evidence_matrix.json
  run-summary.json
  product-run.json
  selection.json
  selection_report.md
  delivery.json
  delivery/
    selected.patch
    branch-state.json
    pull-request-body.md
  approval-audit.jsonl
  approval-records/
    approval_....json
  materialization.json
  final.patch
  final_report.md
```

Files that do not yet have data may be absent while a run is active. `manifest.json`, `task.json`, `state.json`, and `events.jsonl` must exist immediately after run creation.

Agent-system identities and harness results use the versioned, harness-neutral contracts described
in [AGENT_SYSTEMS.md](AGENT_SYSTEMS.md). Identity fields are optional additions to the version-1
manifest and attempt schemas so older bundles remain readable. New runs persist the complete
non-secret identity for every configured route and link each attempt to the exact selected system.

`validation-coverage.json` uses `villani.validation_coverage.v1`. It records each authoritative
command's identity, safe display, role, working directory, status, timing, discoverable test targets,
changed tests linked to the command, requirement coverage, provenance, confidence, uncertainty
reasons, and artifact references. Coverage is deterministic and conservative: a generic passing
suite does not prove an unrelated requirement. If coverage remains uncertain, the controller runs
the verifier-requested focused probe in the same isolated candidate environment before making its
final binary decision.

`run-summary.json` uses `villani.run_summary.v1` and is the canonical terminal projection for CLI,
Console, Flight Recorder, static viewers, `final_report.md`, and `selection_report.md`. It separates
passed, failed, not-run, and unavailable repository checks and focused probes, counts proved and
unproved requirements, and represents unknown accounting as `null` plus an explicit status.

`product-run.json` uses `villani.product_run.v1`. It is the shared CLI/Console product projection:
run identity, task summary, one of four persisted event-derived stages (`Understanding`, `Working`,
`Checking`, `Ready`), one of four final verdicts (`Ready to apply`, `Needs review`, `Could not
prove`, `Cancelled`), change/check/requirement summaries, cost and duration accounting, agent and
escalation summaries, available actions, evidence links, recovery guidance, technical references,
and target-repository state. Existing bundles remain readable because this projection is derived
conservatively from their canonical artifacts when absent. Only controller-proved selected work
may receive a delivery action; the browser does not reproduce acceptance logic.

`delivery.json` is the durable user-facing projection of the selected patch, its review evidence,
the authority decision, approval status, and the explicit delivery result. `delivery/selected.patch`
is written before any delivery mutation and is never discarded by rejection, timeout, conflict,
push failure, or recovery. Approval audit records are append-only; authenticated identity is
required when the run was created in connected mode.

All JSON snapshots are written atomically with temporary file plus replace. JSONL files are append-only. A truncated final JSONL line after a process crash may be ignored during recovery, but earlier valid lines must remain readable.

## Canonical event envelope

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
approval_requested
approval_granted
approval_rejected
approval_rerun_requested
approval_candidate_changed
approval_timed_out
approval_unauthorized
materialization_started
materialization_completed
materialization_failed
delivery_completed
run_completed
run_exhausted
run_failed
run_cancelled
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

## Verification and selection contracts

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

Verification records its authority source. Heuristic-only deterministic output can extract
evidence and reject clear failures but cannot authorize materialization. Structured repository
validation is authoritative only when it completes in the isolated candidate after the final
relevant mutation, exits zero, has no later failing validation, and has no missing blocker.

Candidate dimensions cross the runner boundary as typed `RunnerContext` data. Supported prompt
strategies are `direct`, `plan_first`, and `test_first`; rendered prompts and effective
configuration acknowledgements are stored with each attempt. Unsupported dimensions remain
auditable but do not contribute to the effective diversity fingerprint.

Selection is deterministic by default. It ranks only eligible candidates using direct behavioral evidence, repository test evidence, critical requirement coverage, lower risk, and then lower actual cost when costs are known. Stable `attempt_id` is the final tie-breaker. An LLM comparison may be recorded as advisory, but it may not override a stronger deterministic evidence result in version 1.

## Architectural invariants

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

The public `villani run` path requires a Git repository and retains the
tracked-files-only isolation default. Legacy compatibility orchestrators may
accept a non-Git source through a bounded snapshot: symlinks are preserved,
oversized files and snapshots are rejected, and environment files, virtual
environments, dependency trees, caches, build output, Villani state, and known
secret files are excluded. The snapshot is initialized as an isolated Git
baseline before execution; this compatibility path does not relax the public
CLI requirement.
