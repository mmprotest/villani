export type ProductRunStage = "Understanding" | "Working" | "Checking" | "Ready";
export type ProductRunVerdict = "Ready to apply" | "Needs review" | "Could not prove" | "Cancelled";
export interface ProductRunStageTransition {
    sequence: number;
    timestamp: string;
    stage: ProductRunStage;
    sentence: string;
}
export interface ProductRunCounts {
    passed: number | null;
    failed: number | null;
    not_run: number | null;
    unavailable: number | null;
    accounting_status: "complete" | "unknown";
}
export interface ProductRunRequirementCounts {
    proved: number | null;
    not_proved: number | null;
    accounting_status: "complete" | "unknown";
}
export interface ProductRunAction {
    id: "apply_change" | "create_branch" | "open_pull_request" | "cancel" | "retry" | "review_evidence";
    label: string;
    method: "GET" | "POST";
    href: string;
}
export interface ProductRoleExecution {
    role: "classification" | "coding" | "verification" | "selection";
    label: "Understand task" | "Write code" | "Verify result" | "Choose candidate";
    agent_system_id: string;
    system_name: string;
    driver: string;
    model: string | null;
    invocation_count: number;
    status: "recorded" | "succeeded" | "infrastructure_failure" | "not_invoked";
    evidence_artifact: string;
    infrastructure_failure?: {
        stage: "classification" | "coding" | "verification" | "selection";
        role: "classification" | "coding" | "verification" | "selection";
        agent_system_id: string;
        safe_error_summary: string;
        target_repository_modified: boolean;
        partial_patch_preserved: boolean;
        automatic_fallback_performed: boolean;
        exact_repair_action: string;
        evidence_path: string;
    } | null;
}
export interface ProductRun {
    schema_version: "villani.product_run.v1";
    run_identity: {
        run_id: string;
        trace_id: string | null;
    };
    task_summary: {
        task: string;
        success_criteria: string | null;
        repository: string | null;
    };
    current_stage: ProductRunStage;
    stage_sentence: string;
    stage_transitions: ProductRunStageTransition[];
    final_verdict: ProductRunVerdict | null;
    verdict_reason: string | null;
    change_summary: string;
    changed_files: string[];
    checks_summary: ProductRunCounts;
    requirement_summary: ProductRunRequirementCounts;
    cost: {
        value: number | null;
        currency: string | null;
        accounting_status: "complete" | "partial" | "unknown" | "not_applicable";
    };
    duration: {
        value_ms: number | null;
        accounting_status: "complete" | "partial" | "unknown" | "not_applicable";
    };
    agent_system: {
        name: string;
        backend: string | null;
        model: string | null;
    };
    role_executions?: ProductRoleExecution[];
    escalation_summary: {
        attempts: number;
        retries: number;
        escalations: number;
        summary: string;
    };
    available_actions: ProductRunAction[];
    evidence_links: {
        label: string;
        href: string;
        artifact: string;
    }[];
    recovery_action: {
        label: string;
        instruction: string;
        href: string | null;
    } | null;
    technical_detail_references: string[];
    target_repository: {
        modified: boolean | null;
        accounting_status: "known" | "unknown";
        statement: string;
    };
    proof_package?: {
        status: "ready_to_apply" | "needs_review";
        risk_tier: "standard" | "elevated" | "critical";
        why_villani_trusts_it: string;
        unresolved_decision: string | null;
        artifact: string;
    } | null;
    last_event_sequence: number;
    updated_at: string;
}
export declare const PRODUCT_RUN_STAGES: readonly ProductRunStage[];
