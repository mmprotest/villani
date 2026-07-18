export declare const QUALIFICATION_POLICY_VERSION: "repository_qualification_v1";
export declare const QUALIFICATION_CONFIGURATION_SCHEMA_VERSION: "villani.repository_qualification_configuration.v1";
export declare const QUALIFICATION_OBSERVATION_SCHEMA_VERSION: "villani.qualification_observation.v1";
export declare const QUALIFICATION_INVALIDATION_SCHEMA_VERSION: "villani.qualification_invalidation.v1";
export declare const QUALIFICATION_SNAPSHOT_SCHEMA_VERSION: "villani.qualification_snapshot.v1";
export declare const GATE_C_SCHEMA_VERSION: "villani.gate_c.v1";
export type QualificationState = "qualified" | "provisional" | "experimental" | "unsupported";
export type QualificationBackoffLevel = "exact_repository_task" | "repository_category" | "repository_wide" | "compatible_repository_cohort";
export interface QualificationTaskProfile {
    category: string;
    difficulty: string;
    risk: string;
    required_capabilities: string[];
}
export interface QualificationSystemIdentity {
    system_id: string;
    route_name: string;
    harness_id: string;
    harness_version: string;
    adapter_id: string;
    adapter_version: string;
    protocol: string;
    protocol_version: string;
    provider: string;
    model_id: string;
    model_revision: string | null;
    serving_engine: string | null;
    serving_engine_version: string | null;
    execution_provider: string;
    execution_environment_fingerprint: string;
    verification_policy_version: string;
    software_versions: Record<string, string>;
    identity_digest: string;
}
export interface QualificationArtifactReference {
    kind: string;
    path: string;
    digest: string;
}
export interface QualificationObservation {
    schema_version: typeof QUALIFICATION_OBSERVATION_SCHEMA_VERSION;
    observation_id: string;
    recorded_at: string;
    observed_at: string;
    source_kind: "evaluation_trial" | "imported_qualification_evidence" | "canonical_run";
    source_suite_id: string | null;
    source_suite_digest: string | null;
    source_task_id: string;
    source_task_digest: string;
    source_trial_id: string;
    source_review_id: string | null;
    repository_id: string;
    repository_commit: string;
    repository_baseline_digest: string;
    task_profile: QualificationTaskProfile;
    profile_source: "authoritative_run_classification" | "explicit_evaluation_profile";
    system: QualificationSystemIdentity;
    baseline_valid: boolean;
    candidate_evidence_complete: boolean;
    authoritative_verification_complete: boolean;
    infrastructure_status: "resolved" | "excluded" | "unresolved";
    human_review_required: boolean;
    human_review_status: "complete" | "missing" | "not_applicable";
    corruption_detected: boolean;
    secret_issue_detected: boolean;
    target_repository_modified: false;
    proved_acceptable: boolean | null;
    accepted_as_is: boolean | null;
    successful: boolean | null;
    false_acceptance: boolean;
    false_rejection: boolean;
    later_rollback: boolean;
    reopened_defect: boolean;
    cost_amount: number | null;
    cost_currency: string | null;
    cost_accounting_status: "complete" | "partial" | "unknown" | "not_applicable";
    duration_ms: number | null;
    duration_accounting_status: "complete" | "partial" | "unknown" | "not_applicable";
    review_minutes: number | null;
    eligible: boolean;
    exclusion_reason: string | null;
    artifacts: QualificationArtifactReference[];
}
export interface QualificationInvalidation {
    schema_version: typeof QUALIFICATION_INVALIDATION_SCHEMA_VERSION;
    invalidation_id: string;
    recorded_at: string;
    system_id: string;
    route_name: string;
    repository_id: string | null;
    reason: "explicit_disable" | "capability_loss" | "conformance_failure" | "environment_prohibited" | "version_incompatible" | "model_identity_change" | "provider_identity_change" | "execution_environment_change" | "verification_policy_change" | "repository_lineage_divergence" | "recent_reliability_breach" | "false_acceptance" | "operator_invalidation";
    severity: "warning" | "severe" | "unsupported";
    evidence_reference: string;
    evidence_digest: string | null;
    detail: string;
}
export interface QualificationDistribution {
    known_count: number;
    unknown_count: number;
    minimum: number | null;
    median: number | null;
    p90: number | null;
    maximum: number | null;
    unit: string;
}
export interface QualificationDriftFlag {
    code: string;
    severity: "warning" | "severe" | "unsupported";
    detail: string;
    evidence_ids: string[];
}
export interface QualificationStatistics {
    sample_count: number;
    successes: number;
    failures: number;
    exclusions: Record<string, number>;
    acceptance_rate: number | null;
    wilson_lower_bound: number | null;
    proved_acceptable_count: number;
    accepted_as_is_count: number;
    false_acceptance_count: number;
    false_rejection_count: number;
    false_case_ids: string[];
    cost_distribution_by_currency: Record<string, QualificationDistribution>;
    cost_unknown_count: number;
    accepted_change_cost_by_currency: Record<string, QualificationDistribution>;
    accepted_change_cost_unknown_count: number;
    duration_distribution: QualificationDistribution;
    review_minutes_distribution: QualificationDistribution;
    last_evidence_at: string | null;
    software_version_diversity: Record<string, string[]>;
    drift_flags: QualificationDriftFlag[];
}
export interface QualificationBackoffEvidence {
    level: QualificationBackoffLevel;
    repository_ids: string[];
    cohort: string | null;
    eligible_observation_count: number;
    selected: boolean;
    approved_for_qualification: boolean;
    rejection_reasons: string[];
}
export interface QualificationAssessment {
    schema_version: "villani.qualification_assessment.v1";
    policy_version: string;
    system_id: string;
    route_name: string;
    repository_id: string;
    repository_head: string | null;
    task_profile: QualificationTaskProfile;
    state: QualificationState;
    selected_level: QualificationBackoffLevel | null;
    selected_cohort: string | null;
    task_wilson_threshold: number;
    statistics: QualificationStatistics;
    backoff_evidence: QualificationBackoffEvidence[];
    automatic_eligible: boolean;
    provisional_fallback_eligible: boolean;
    manual_override_required: boolean;
    unsupported_reasons: string[];
    caveat: string;
    doctor_action: string;
    evidence_action: string;
    evaluated_at: string;
}
export interface QualificationPolicy {
    policy_version: typeof QUALIFICATION_POLICY_VERSION;
    minimum_qualified_observations: number;
    provisional_maximum_observations: number;
    wilson_z: number;
    task_wilson_thresholds: Record<string, number>;
    maximum_evidence_age_days: number;
    recent_reliability_window: number;
    approved_backoff_levels: QualificationBackoffLevel[];
    compatible_repository_cohorts: Record<string, string[]>;
    approved_repository_cohorts: string[];
}
export interface QualificationSnapshot {
    schema_version: typeof QUALIFICATION_SNAPSHOT_SCHEMA_VERSION;
    generated_at: string;
    policy: QualificationPolicy;
    source_digest: string;
    snapshot_digest: string;
    observation_count: number;
    invalidation_count: number;
    superseded_observation_count: number;
    profiles: Array<{
        key: {
            repository_id: string;
            task_profile: QualificationTaskProfile;
            system_identity_digest: string;
            execution_environment_fingerprint: string;
            verification_policy_version: string;
        };
        observation_ids: string[];
        statistics: QualificationStatistics;
        source_digest: string;
    }>;
    exclusions: Record<string, number>;
    migrations: Array<{
        migration_id: string;
        source: string;
        source_digest: string | null;
        status: "not_present" | "excluded" | "complete";
        exclusion_reason: string | null;
        qualification_created: false;
    }>;
}
export interface QualificationScorecard {
    system_name: string;
    harness: string;
    model: string;
    provider: string;
    assessment: QualificationAssessment;
    accepted_as_is: number;
    proved_acceptable: number;
    false_cases: number;
    known_cost: boolean;
    known_duration: boolean;
    known_review_time: boolean;
    failures: number;
}
export interface GateCReport {
    schema_version: typeof GATE_C_SCHEMA_VERSION;
    gate: "C";
    generated_at: string;
    repository_id: string;
    repository_head: string | null;
    task_profile: QualificationTaskProfile;
    policy_version: string;
    status: "PASS" | "FAIL" | "INSUFFICIENT_EVIDENCE";
    checks: Array<{
        check_id: string;
        system_id: string | null;
        status: "pass" | "fail" | "insufficient_evidence";
        actual: unknown;
        required: unknown;
        reason: string;
    }>;
    scorecards: QualificationScorecard[];
    unmatched_sample_warning: string | null;
    evidence_snapshot_digest: string | null;
}
