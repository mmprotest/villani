# Adaptive verification and supervision evidence

PT9 adds a deterministic verification layer that spends only the effort required to prove a
candidate while preserving fail-closed delivery. The policy is `adaptive_verification_v1`.
Semantic verification is mandatory and remains blind to agent-system identity, model/provider,
route, qualification, cost, execution-environment identity, and competing candidates.

## Verification flow

For each isolated candidate, Villani records
`verification/<attempt-id>-plan.json` before semantic verification. The plan is derived from the
task and criteria, explicit risk, diff size and changed files, repository-discovered validation,
qualification uncertainty, configured sensitive paths, and recorded historical verification
failure modes. Its nodes cover diff integrity, generated-artifact exclusion, requirement mapping,
repository validation, explicitly configured changed-test/static checks, focused probes, semantic
verification, an independent verifier when required, and exact manual review when proof is
impossible.

Risk has three tiers:

- `standard` for bounded, low-risk, well-qualified work;
- `elevated` for medium/unknown risk, larger scope, provisional evidence, or disagreement history;
- `critical` for explicit high risk, security or destructive implications, data migrations,
  configured sensitive paths, severe historical failures, or very large scope.

Sensitive paths and generated-artifact paths are repository policy, not built-in framework or
language guesses. Repository commands are argv arrays discovered by the existing repository
validation path or configured in `repository_validation_commands`. A command may opt into
`changed_test_execution` or `static_check` through its generic `verification_kind`.

Conclusive deterministic failures stop before a paid semantic call. All non-rejected candidates
still require semantic verification. Critical plans require a distinct independent semantic
verifier. Unclear, malformed, missing, unavailable, or error results normalize to binary decision
`0`; only decision `1`, resolved infrastructure, all required nodes, and all required independent
verification can authorize acceptance. Verification cost is recorded separately and unknown cost
remains null.

Focused probes describe the exact behavior still missing from the evidence. Controlled temporary
files are created only inside the candidate worktree, are content-digested in persisted evidence,
and are removed in a `finally` path. Probe output distinguishes behavior failure from
infrastructure failure; only infrastructure failure is retryable. Probe-only artifacts are never
part of the candidate patch.

## Compact proof package

Each finalized attempt records `verification/<attempt-id>-review-package.json`. A Ready-to-apply
package contains the task, concise change summary, files, proved and unproved requirements, checks,
risk flags, known cost and time, the reason Villani trusts the result, and a full-evidence link. A
Needs-review package names one exact unresolved decision and cannot authorize delivery. The product
run exposes this package additively as `proof_package`; old product runs without the field remain
readable.

## Configuration migration

Existing configuration remains valid. `villani init` adds the following optional block; when it is
absent, the same conservative defaults are loaded in memory:

```json
{
  "adaptive_verification": {
    "schema_version": "villani.adaptive_verification_configuration.v1",
    "policy_version": "adaptive_verification_v1",
    "standard_patch_line_limit": 200,
    "elevated_patch_line_limit": 600,
    "standard_changed_file_limit": 6,
    "elevated_changed_file_limit": 18,
    "sensitive_paths": [],
    "generated_artifact_paths": [],
    "require_independent_verifier_for_critical": true,
    "require_manual_review_when_proof_impossible": true,
    "minimum_independent_verifier_capability": 80,
    "historical_disagreement_window": 20
  }
}
```

Semantic verification cannot be disabled. Keep glob patterns generic and repository-owned.

## Commands and explicit outcomes

```text
villani verification plan RUN_ID [--attempt ATTEMPT_ID] [--json]
villani verification feedback-import RUN_ID --outcome OUTCOME [options]
villani verification feedback-import RUN_ID --file OUTCOME.json
villani verification feedback RUN_ID [--json]
villani verification metrics RUN_ID [--json]
villani verification gate-d --input ARMS.json [--output REPORT.json] [--json]
```

Supported outcomes are `accepted_as_is`, `corrected_before_use`, `reverted`,
`reopened_defect`, `false_acceptance`, and `false_rejection`. Corrected outcomes require a concise
correction summary. Reverts and reopened defects require an explicit commit, issue, or other local
reference. Review minutes and whether the full trace was opened are optional; omitted values remain
unknown and are never treated as zero. The append-only `human-outcomes.jsonl` and derived
`supervision-metrics.json` stay in the run bundle. There is no passive monitoring.

False acceptance, revert, or reopened defect creates a severe append-only qualification
invalidation for the exact selected agent-system identity, then rebuilds the qualification
snapshot. This prevents the affected profile from remaining automatically eligible.

## Durable contracts

The normative root schemas and packaged copies are:

- `villani.adaptive_verification_plan.v1`
- `villani.binary_verification_decision.v1`
- `villani.review_package.v1`
- `villani.human_outcome.v1`
- `villani.supervision_metrics.v1`
- `villani.gate_d.v1`

Run manifests add optional `adaptive_verification`, `human_outcomes`, and `supervision_metrics`
paths. Because every addition is optional, existing run bundles remain readable. New documents are
strict and schema-validated across Python, TypeScript, Flight Recorder, fixtures, and integration
tests.

## Gate D

Gate D compares matched frozen founder cases for `strongest_only`,
`accepted_change_optimizer`, and `optimizer_plus_adaptive`. It requires no accepted-as-is
regression, zero false acceptance, lower fully accounted cost or time, lower explicit review
minutes, explainability, and safe fallback. Samples must have identical case IDs across arms.
Unknown or unmatched evidence is not ranked.

The command exits `0` for `PASS`, `1` for `FAIL`, and `2` for
`INSUFFICIENT_EVIDENCE`. Empty evidence can never pass or permit the next milestone.
