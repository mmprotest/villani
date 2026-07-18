export const ADAPTIVE_VERIFICATION_PLAN_SCHEMA_VERSION =
  "villani.adaptive_verification_plan.v1" as const;
export const BINARY_VERIFICATION_DECISION_SCHEMA_VERSION =
  "villani.binary_verification_decision.v1" as const;
export const REVIEW_PACKAGE_SCHEMA_VERSION = "villani.review_package.v1" as const;
export const HUMAN_OUTCOME_SCHEMA_VERSION = "villani.human_outcome.v1" as const;
export const SUPERVISION_METRICS_SCHEMA_VERSION =
  "villani.supervision_metrics.v1" as const;
export const GATE_D_SCHEMA_VERSION = "villani.gate_d.v1" as const;
export const ADAPTIVE_VERIFICATION_POLICY_VERSION = "adaptive_verification_v1" as const;

export type AdaptiveAccountingStatus =
  | "complete"
  | "partial"
  | "unknown"
  | "not_applicable";
export type VerificationRiskTier = "standard" | "elevated" | "critical";
export type VerificationNodeStatus =
  | "passed"
  | "failed"
  | "unavailable"
  | "infrastructure_error"
  | "not_run"
  | "not_applicable";

export interface AdaptiveMoneyAccounting {
  amount: number | null;
  currency: string | null;
  accounting_status: AdaptiveAccountingStatus;
  source: string;
}

export interface AdaptiveDurationAccounting {
  duration_ms: number | null;
  accounting_status: AdaptiveAccountingStatus;
  source: string;
}

export interface AdaptiveVerificationPlan {
  schema_version: typeof ADAPTIVE_VERIFICATION_PLAN_SCHEMA_VERSION;
  plan_id: string;
  run_id: string;
  attempt_id: string;
  policy_version: typeof ADAPTIVE_VERIFICATION_POLICY_VERSION;
  policy_digest: string;
  created_at: string;
  risk_tier: VerificationRiskTier;
  risk_reasons: string[];
  task_digest: string;
  criteria_digest: string;
  candidate_diff_digest: string;
  changed_files: string[];
  requirement_ids: string[];
  qualification_state:
    | "qualified"
    | "provisional"
    | "experimental"
    | "unsupported"
    | "unknown";
  historical_failure_modes: string[];
  nodes: Array<{
    node_id: string;
    kind:
      | "repository_validation"
      | "focused_probe"
      | "changed_test_execution"
      | "static_checks"
      | "diff_integrity"
      | "generated_artifact_exclusion"
      | "requirement_mapping"
      | "semantic_verifier"
      | "independent_second_verifier"
      | "manual_review";
    disposition: "required" | "conditional" | "omitted";
    reason: string;
    depends_on: string[];
    repository_commands: string[][];
    evidence_requirements: string[];
    estimated_model_calls: number | null;
  }>;
  independent_verifier_required: boolean;
  manual_review_if_unresolved: boolean;
  semantic_context_allowlist: string[];
  semantic_context_excluded: string[];
  deterministic_input_digest: string;
}

export interface BinaryVerificationDecision {
  schema_version: typeof BINARY_VERIFICATION_DECISION_SCHEMA_VERSION;
  decision_id: string;
  run_id: string;
  attempt_id: string;
  plan_id: string;
  decided_at: string;
  decision: 0 | 1;
  reason_code: string;
  reason: string;
  requirements_proved: string[];
  requirements_not_proved: string[];
  blockers: string[];
  infrastructure_status: "resolved" | "infrastructure_failure" | "unavailable";
  semantic_status: "passed" | "failed" | "unclear" | "error" | "not_invoked";
  independent_verifier_required: boolean;
  independent_verifier_completed: boolean;
  node_results: Array<{
    node_id: string;
    status: VerificationNodeStatus;
    reason: string;
    commands: string[][];
    evidence_paths: string[];
  }>;
  verifier_provenance: Array<{
    verifier_role: "semantic" | "independent_semantic";
    verifier_identity_digest: string;
    invocation_status:
      | "completed"
      | "not_invoked"
      | "malformed_output"
      | "timeout"
      | "error";
    independent: boolean;
    artifact_path: string | null;
  }>;
  verification_cost: AdaptiveMoneyAccounting;
  normalized_from:
    | "accepted"
    | "rejected"
    | "unclear"
    | "error"
    | "deterministic_failure";
}

export interface CompactReviewPackage {
  schema_version: typeof REVIEW_PACKAGE_SCHEMA_VERSION;
  package_id: string;
  run_id: string;
  attempt_id: string;
  decision_id: string;
  created_at: string;
  status: "ready_to_apply" | "needs_review";
  task: string;
  change_summary: string;
  changed_files: string[];
  requirements_proved: string[];
  requirements_not_proved: string[];
  checks: Array<{
    label: string;
    status: Exclude<VerificationNodeStatus, "not_applicable">;
    evidence_path: string | null;
  }>;
  risk_tier: VerificationRiskTier;
  risk_flags: string[];
  known_cost: AdaptiveMoneyAccounting;
  known_duration: AdaptiveDurationAccounting;
  why_villani_trusts_it: string;
  unresolved_decision: string | null;
  full_evidence_href: string;
}

export type HumanOutcomeKind =
  | "accepted_as_is"
  | "corrected_before_use"
  | "reverted"
  | "reopened_defect"
  | "false_acceptance"
  | "false_rejection";

export interface HumanOutcome {
  schema_version: typeof HUMAN_OUTCOME_SCHEMA_VERSION;
  outcome_id: string;
  run_id: string;
  attempt_id: string | null;
  recorded_at: string;
  outcome: HumanOutcomeKind;
  review_minutes: number | null;
  review_time_accounting_status: "complete" | "unknown" | "not_applicable";
  full_trace_opened: boolean | null;
  full_trace_accounting_status: "complete" | "unknown" | "not_applicable";
  correction_summary: string | null;
  linked_reference: string | null;
  imported_from: "explicit_cli" | "explicit_local_file";
  actor: string;
  notes: string | null;
}

export interface SupervisionMetrics {
  schema_version: typeof SUPERVISION_METRICS_SCHEMA_VERSION;
  metrics_id: string;
  run_id: string;
  policy_version: typeof ADAPTIVE_VERIFICATION_POLICY_VERSION;
  calculated_at: string;
  eligible_outcome_count: number;
  evidence_expansion_count: number;
  explicit_review_minutes: number | null;
  review_time_accounting_status: AdaptiveAccountingStatus;
  application_without_full_trace_count: number;
  full_trace_accounting_status: "complete" | "unknown" | "not_applicable";
  correction_count: number;
  false_acceptance_count: number;
  false_rejection_count: number;
  verification_cost: AdaptiveMoneyAccounting;
  review_cost: AdaptiveMoneyAccounting;
  total_accepted_change_cost: AdaptiveMoneyAccounting;
  source_outcome_ids: string[];
}

export type GateDStrategy =
  | "strongest_only"
  | "accepted_change_optimizer"
  | "optimizer_plus_adaptive";

export interface GateDArm {
  strategy: GateDStrategy;
  case_ids: string[];
  eligible_cases: number;
  accepted_as_is: number;
  false_acceptances: number;
  total_cost: AdaptiveMoneyAccounting;
  elapsed_duration: AdaptiveDurationAccounting;
  review_minutes: number | null;
  review_time_accounting_status: "complete" | "partial" | "unknown";
  explainable_routes: boolean;
  safe_fallback: boolean;
}

export interface GateDReport {
  schema_version: typeof GATE_D_SCHEMA_VERSION;
  gate_id: string;
  policy_version: typeof ADAPTIVE_VERIFICATION_POLICY_VERSION;
  generated_at: string;
  status: "PASS" | "FAIL" | "INSUFFICIENT_EVIDENCE";
  arms: GateDArm[];
  checks: Array<{
    check:
      | "matched_founder_cases"
      | "accepted_as_is_no_regression"
      | "zero_false_acceptance"
      | "lower_cost_or_time"
      | "lower_review_burden"
      | "explainability"
      | "safe_fallback";
    status: "pass" | "fail" | "insufficient_evidence";
    reason: string;
  }>;
  warnings: string[];
  evidence_references: string[];
  next_milestone_permitted: boolean;
}
