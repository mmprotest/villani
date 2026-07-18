import type { QualificationDistribution, QualificationState, QualificationTaskProfile } from "./qualification.js";
export declare const ACCEPTED_CHANGE_POLICY_VERSION: "accepted_change_economics_v1";
export declare const ECONOMICS_CONFIGURATION_SCHEMA_VERSION: "villani.accepted_change_economics_configuration.v1";
export declare const ROUTE_POLICY_SCHEMA_VERSION: "villani.route_policy.v1";
export declare const ROUTE_PLAN_SCHEMA_VERSION: "villani.route_plan.v1";
export declare const ECONOMICS_OBSERVATION_SCHEMA_VERSION: "villani.economics_observation.v1";
export declare const ECONOMICS_SNAPSHOT_SCHEMA_VERSION: "villani.economics_snapshot.v1";
export declare const ROUTE_POLICY_EVALUATION_SCHEMA_VERSION: "villani.route_policy_evaluation.v1";
export declare const ROUTE_POLICY_PUBLICATION_SCHEMA_VERSION: "villani.route_policy_publication.v1";
export declare const ONLINE_EVIDENCE_UPDATE_SCHEMA_VERSION: "villani.online_evidence_update.v1";
export type EconomicsAccountingStatus = "complete" | "partial" | "unknown" | "not_applicable";
export type RouteStrategy = "accepted_change_optimizer" | "strongest_only" | "cheapest_qualified" | "forced";
export interface MoneyEstimate {
    amount: number | null;
    currency: string | null;
    accounting_status: EconomicsAccountingStatus;
    source: string;
    sample_count: number;
}
export interface DurationEstimate {
    duration_ms: number | null;
    accounting_status: EconomicsAccountingStatus;
    source: string;
    sample_count: number;
}
export interface RouteConstraints {
    local_only: boolean;
    prefer_local: boolean;
    allowed_providers: string[];
    preferred_provider: string | null;
    excluded_systems: string[];
    forced_system: string | null;
    strongest_only: boolean;
    maximum_known_cost_usd: number | null;
    allowed_permission_profiles: string[];
    allow_experimental_forced: boolean;
}
export interface RoutePolicy {
    schema_version: typeof ROUTE_POLICY_SCHEMA_VERSION;
    policy_version: string;
    strategy: RouteStrategy;
    objective_version: "total_accepted_change_v1";
    conservative_cost_statistic: "p90" | "median";
    conservative_duration_statistic: "p90" | "median";
    currency: string;
    human_review_cost_per_minute: number | null;
    latency_penalty_per_second: number | null;
    allow_provisional_fallback: boolean;
    require_complete_objective_for_comparison: boolean;
    constraints: RouteConstraints;
}
export interface AcceptedChangeObjective {
    objective_version: "total_accepted_change_v1";
    execution_cost: MoneyEstimate;
    verification_cost: MoneyEstimate;
    human_review_cost: MoneyEstimate;
    retry_escalation_cost: MoneyEstimate;
    latency_penalty: MoneyEstimate;
    conservative_acceptance_probability: number | null;
    probability_source: string;
    known_numerator_cost: number | null;
    currency: string | null;
    accounting_status: EconomicsAccountingStatus;
    unknown_components: string[];
    expected_accepted_change_cost: number | null;
    partial_expected_known_cost: number | null;
    expected_duration: DurationEstimate;
}
export interface RouteConsideration {
    backend_name: string;
    route_name: string;
    system_id: string | null;
    harness: string;
    model: string;
    provider: string;
    local: boolean;
    permission_profile: string;
    availability: string;
    qualification_state: QualificationState;
    qualification_level: string | null;
    qualification_sample_count: number;
    conservative_acceptance_probability: number | null;
    task_probability_threshold: number;
    capability_score: number;
    eligible: boolean;
    rejection_reasons: string[];
    unknowns: string[];
    objective: AcceptedChangeObjective;
}
export interface RouteSequenceEconomics {
    systems: string[];
    conservative_success_probability: number | null;
    expected_cost_before_acceptance: number | null;
    expected_accepted_change_cost: number | null;
    currency: string | null;
    expected_duration_ms: number | null;
    accounting_status: EconomicsAccountingStatus;
    unknowns: string[];
}
export interface RoutePlan {
    schema_version: typeof ROUTE_PLAN_SCHEMA_VERSION;
    plan_id: string;
    run_id: string;
    repository_id: string;
    repository_head: string | null;
    task_profile: QualificationTaskProfile;
    policy_version: string;
    policy_digest: string;
    evidence_cutoff: string | null;
    input_digest: string;
    systems_considered: RouteConsideration[];
    selected_first_system: string | null;
    ordered_fallbacks: string[];
    sequence_economics: RouteSequenceEconomics;
    reserves: Record<string, unknown>;
    constraints: RouteConstraints;
    selection_mode: "accepted_change_optimizer" | "sparse_strongest_evidence" | "strongest_only" | "cheapest_qualified" | "provisional_fallback" | "forced" | "no_safe_route" | "sequential_retry" | "sequential_escalation";
    forced_choice: boolean;
    automatic_policy_metrics_eligible: boolean;
    unknowns: string[];
    explanation: string;
}
export interface EconomicsObservation {
    schema_version: typeof ECONOMICS_OBSERVATION_SCHEMA_VERSION;
    observation_id: string;
    recorded_at: string;
    observed_at: string;
    source_run_id: string;
    source_route_plan_id: string;
    qualification_observation_id: string;
    repository_id: string;
    task_profile: QualificationTaskProfile;
    system_id: string;
    system_identity_digest: string;
    route_name: string;
    policy_version: string;
    forced_choice: boolean;
    qualification_eligible: boolean;
    authoritative_verification_complete: boolean;
    infrastructure_status: "resolved" | "excluded" | "unresolved";
    proved_acceptable: boolean | null;
    accepted_as_is: boolean | null;
    false_acceptance: boolean;
    eligible_for_profile: boolean;
    eligible_for_automatic_policy_metrics: boolean;
    exclusion_reason: string | null;
    execution_cost: MoneyEstimate;
    verification_cost: MoneyEstimate;
    human_review_cost: MoneyEstimate;
    retry_escalation_cost: MoneyEstimate;
    duration: DurationEstimate;
    review_minutes: number | null;
    attempt_count: number;
    escalation_count: number;
}
export interface EconomicsProfile {
    key: {
        repository_id: string;
        task_profile: QualificationTaskProfile;
        system_id: string;
        system_identity_digest: string;
        route_name: string;
    };
    observation_ids: string[];
    sample_count: number;
    successes: number;
    failures: number;
    exclusions: Record<string, number>;
    cost_distributions: Record<string, Record<string, QualificationDistribution>>;
    cost_unknown_counts: Record<string, number>;
    duration_distribution: QualificationDistribution;
    review_minutes_distribution: QualificationDistribution;
    attempt_count_distribution: QualificationDistribution;
    escalation_count_distribution: QualificationDistribution;
    false_acceptance_count: number;
    last_evidence_at: string | null;
    source_digest: string;
}
export interface EconomicsSnapshot {
    schema_version: typeof ECONOMICS_SNAPSHOT_SCHEMA_VERSION;
    generated_at: string;
    source_digest: string;
    snapshot_digest: string;
    observation_count: number;
    profiles: EconomicsProfile[];
    exclusions: Record<string, number>;
}
export interface StrategyMetrics {
    strategy: RouteStrategy;
    case_count: number;
    accepted_as_is: number;
    proved_acceptable: number;
    false_acceptance: number;
    failures: number;
    total_cost: MoneyEstimate;
    elapsed_duration: DurationEstimate;
    review_minutes: number | null;
    escalation_count: number | null;
    regret: MoneyEstimate;
    unknown_input_rate: number;
    unmatched_outcome_count: number;
}
export interface RoutePolicyEvaluation {
    schema_version: typeof ROUTE_POLICY_EVALUATION_SCHEMA_VERSION;
    evaluation_id: string;
    generated_at: string;
    active_policy_version: string;
    active_policy_digest: string;
    proposed_policy_version: string;
    proposed_policy_digest: string;
    point_in_time_replay: true;
    frozen_case_count: number;
    comparisons: Array<{
        case_id: string;
        active_choice: string | null;
        proposed_choice: string | null;
        active_probability: number | null;
        proposed_probability: number | null;
        reliability_non_decreasing: boolean | null;
        active_false_acceptance_exposure: boolean | null;
        proposed_false_acceptance_exposure: boolean | null;
        evidence_cutoff: string;
    }>;
    strategy_metrics: StrategyMetrics[];
    conservative_reliability_non_decreasing: boolean;
    false_acceptance_exposure_non_increasing: boolean;
    safe_to_publish: boolean;
    rejection_reasons: string[];
    source_digest: string;
}
export interface RoutePolicyPublication {
    schema_version: typeof ROUTE_POLICY_PUBLICATION_SCHEMA_VERSION;
    publication_id: string;
    published_at: string;
    policy: RoutePolicy;
    policy_digest: string;
    evaluation_id: string;
    evaluation_digest: string;
    prior_policy_version: string | null;
    state: "active" | "rolled_back";
    deterministic: true;
    authored_by_llm: false;
}
export interface OnlineEvidenceUpdateReport {
    schema_version: typeof ONLINE_EVIDENCE_UPDATE_SCHEMA_VERSION;
    run_id: string;
    recorded_at: string;
    status: "skipped" | "recorded" | "excluded" | "failed";
    qualification_observation_id: string | null;
    economics_observation_id: string | null;
    profile_updated: boolean;
    automatic_policy_metrics_eligible: boolean;
    reasons: string[];
}
