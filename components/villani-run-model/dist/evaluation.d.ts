/** Shared Founder Thesis Lab v1 wire contracts. */
export type EvaluationArm = "direct" | "villani";
export type EvaluationAccountingStatus = "complete" | "partial" | "unknown" | "not_applicable";
export interface EvaluationAmount {
    value: number | null;
    currency: string | null;
    accounting_status: EvaluationAccountingStatus;
    source: string;
}
export interface EvaluationDuration {
    value_ms: number | null;
    accounting_status: EvaluationAccountingStatus;
    source: string;
}
export interface EvaluationTaskReference {
    task_id: string;
    task_digest: string;
}
export interface EvaluationSuiteV1 {
    schema_version: "villani.evaluation_suite.v1";
    suite_id: string;
    title: string;
    suite_version: number;
    status: "draft" | "frozen";
    created_at: string;
    frozen_at: string | null;
    randomization_seed: string;
    task_versions: EvaluationTaskReference[];
    local_compute: {
        measured_power_watts: number | null;
        electricity_price_per_kwh: number | null;
        currency: string | null;
    };
    evidence_kind: "real_founder_work" | "synthetic_fixture";
    confidentiality: "public" | "internal" | "confidential";
    disclosure_complete: boolean;
    content_digest: string | null;
}
export interface EvaluationValidationCommand {
    validation_id: string;
    argv: string[];
    timeout_seconds: number;
    authoritative: boolean;
    visibility: "runner_visible" | "evaluator_only";
}
export interface EvaluationTaskV1 {
    schema_version: "villani.evaluation_task.v1";
    task_id: string;
    suite_id: string;
    task_version: number;
    immutable_baseline_digest: string;
    source_snapshot: {
        repository_identity: string;
        source_kind: "git_commit";
        resolved_commit: string;
        baseline_digest: string;
        archive_digest: string;
        archive_path: string;
        included_paths: string[];
        excluded_paths: string[];
        file_count: number;
        restore_verified: boolean;
    };
    verbatim_task: string;
    success_criteria: string[];
    authoritative_validation: EvaluationValidationCommand[];
    allowed_setup: {
        setup_id: string;
        argv: string[];
        timeout_seconds: number;
    }[];
    file_change_requirement: {
        behavior: "required" | "optional" | "forbidden";
        allowed_path_prefixes: string[];
        forbidden_path_prefixes: string[];
    };
    provenance: {
        captured_at: string;
        captured_by: string;
        source_reference: string;
        later_context_present: boolean;
    };
    risk_labels: string[];
    category_labels: string[];
    secret_exclusions: string[];
    evaluator_only: {
        hidden_check_references: string[];
        future_context_references: string[];
        runner_expected_patch_present: false;
    };
    confidentiality: "public" | "internal" | "confidential";
    evidence_kind: "real_founder_work" | "synthetic_fixture";
    evidence_eligible: boolean;
    frozen: boolean;
    content_digest: string | null;
}
export interface EvaluationAgentSystemIdentity {
    product: string;
    product_version: string;
    harness: string;
    harness_version: string;
    agent: string;
    agent_version: string;
    model: string | null;
    provider: string | null;
    serving_engine: string | null;
    serving_engine_version: string | null;
    execution_provider: string;
    environment_fingerprint: string;
}
export interface EvaluationTrialV1 {
    schema_version: "villani.evaluation_trial.v1";
    trial_id: string;
    suite_id: string;
    suite_digest: string;
    task_id: string;
    task_digest: string;
    arm: EvaluationArm;
    repetition: number;
    randomized_order: number;
    order_digest: string;
    status: "planned" | "running" | "completed" | "excluded" | "interrupted";
    started_at: string | null;
    completed_at: string | null;
    agent_system: EvaluationAgentSystemIdentity;
    run_id: string | null;
    baseline_digest: string;
    baseline_restore_digest: string;
    execution_cost: EvaluationAmount;
    verification_cost: EvaluationAmount;
    local_compute_cost: EvaluationAmount;
    total_cost: EvaluationAmount;
    duration: EvaluationDuration;
    proved_acceptable: boolean | null;
    verification_status: "complete" | "infrastructure_failure" | "not_run";
    human_outcome: "accepted_as_is" | "accepted_after_correction" | "rejected" | null;
    correction_required: boolean | null;
    review_minutes: number | null;
    false_acceptance: boolean | null;
    false_rejection: boolean | null;
    exclusion_reason: string | null;
    target_repository_modified: false;
    attempts: number;
    escalations: number;
    verifier_disagreement: boolean | null;
    configuration_mode: "automatic" | "manual";
    artifact_references: string[];
    evidence_eligible: boolean;
}
export interface HumanReviewV1 {
    schema_version: "villani.human_review.v1";
    review_id: string;
    trial_id: string;
    created_at: string;
    reviewer_id: string;
    blinded: boolean;
    arm_revealed_during_review: boolean;
    outcome: "accepted_as_is" | "accepted_after_correction" | "rejected";
    correction_required: boolean;
    review_minutes: number;
    correction_summary: string | null;
    severity: "none" | "low" | "medium" | "high" | "critical";
    false_acceptance: boolean;
    false_rejection: boolean;
    later_rollback: boolean | null;
    reopened_defect: boolean | null;
    amends_review_id: string | null;
    artifact_references: string[];
}
export interface EvaluationMetricValue {
    value: number | null;
    numerator: number | null;
    denominator: number | null;
    unit: string | null;
    accounting_status: "complete" | "partial" | "unknown" | "not_defined";
    interval: {
        method: string;
        estimate: number | null;
        lower: number | null;
        upper: number | null;
        confidence: number;
        sample_count: number;
        status: "available" | "insufficient_evidence" | "not_defined";
    } | null;
}
export interface EvaluationReportV1 {
    schema_version: "villani.evaluation_report.v1";
    report_id: string;
    suite_id: string;
    suite_digest: string;
    generated_at: string;
    evidence_kind: "real_founder_work" | "synthetic_fixture";
    confidentiality: "public" | "internal" | "confidential";
    raw_counts: Record<string, number>;
    reliability: Record<string, EvaluationMetricValue>;
    review_time: Record<string, EvaluationMetricValue>;
    cost: Record<string, EvaluationMetricValue>;
    supervision: Record<string, EvaluationMetricValue>;
    false_acceptance: Record<string, EvaluationMetricValue>;
    paired_task_deltas: Record<string, unknown>[];
    task_classes: Record<string, unknown>[];
    failure_modes: Record<string, unknown>[];
    missing_evidence: Record<string, unknown>[];
    confusion_matrix: Record<string, number | null>;
    classification_metrics: Record<string, number | null>;
    calibration: Record<string, unknown>;
    verifier_wrong_cases: Record<string, unknown>[];
    cost_decomposition: Record<string, unknown>[];
    route_decomposition: Record<string, unknown>[];
    trial_bundle_links: string[];
    unknowns: Record<string, unknown>[];
    exclusions: Record<string, unknown>[];
    disclosures_complete: boolean;
    small_sample_significance_claimed: false;
    founder_gate_status: "PASS" | "FAIL" | "INSUFFICIENT_EVIDENCE";
    founder_gate_checks: {
        check_id: string;
        status: "pass" | "fail" | "insufficient_evidence";
        actual: unknown;
        required: unknown;
        reason: string;
    }[];
}
export type EvaluationProtocolDocument = EvaluationSuiteV1 | EvaluationTaskV1 | EvaluationTrialV1 | HumanReviewV1 | EvaluationReportV1;
export declare const EVALUATION_SCHEMA_VERSIONS: readonly ["villani.evaluation_suite.v1", "villani.evaluation_task.v1", "villani.evaluation_trial.v1", "villani.human_review.v1", "villani.evaluation_report.v1"];
