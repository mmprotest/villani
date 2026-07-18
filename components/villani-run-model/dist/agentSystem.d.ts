export declare const AGENT_SYSTEM_SCHEMA_VERSION: "villani.agent_system.v1";
export declare const HARNESS_RESULT_SCHEMA_VERSION: "villani.harness_result.v1";
export declare const HARNESS_CONFORMANCE_SCHEMA_VERSION: "villani.harness_conformance_report.v1";
export declare const HARNESS_DISCOVERY_SCHEMA_VERSION: "villani.harness_discovery.v1";
export declare const REQUIRED_HARNESS_CONFORMANCE_CHECKS: readonly ["manifest", "protocol_negotiation", "version_capture", "worktree_enforcement", "path_safety", "event_ordering", "cancellation", "timeout", "malformed_output", "oversized_output", "process_crash", "missing_executable", "permissions", "artifacts", "patch_correctness", "cleanup", "secret_redaction", "unknown_cost", "cross_platform_paths", "successful_patch", "no_patch", "command_recovery", "permission_request", "rate_limit_retry", "unsupported_version", "schema_change", "missing_final_result", "partial_patch_on_crash", "known_cost", "non_ascii_spaced_paths", "large_output", "outside_isolation_mutation"];
export interface HarnessReadiness {
    installed: boolean;
    command_identity: string;
    exact_version: string | null;
    supported_version_range: string | null;
    version_supported: boolean | null;
    authentication_status: "ready" | "not_ready" | "unknown" | "not_applicable";
    protocol: string;
    conformance_status: "passed" | "failed" | "not_run" | "insufficient_evidence";
    qualification_state: "qualified" | "bootstrap" | "experimental" | "provisional" | "unqualified" | "unsupported" | "disabled";
    custom_model_capability: AgentSystemCapabilityState;
    custom_provider_capability: AgentSystemCapabilityState;
    local_model_capability: AgentSystemCapabilityState;
    repair_action: string;
    details: Record<string, unknown>;
}
export interface HarnessDiscovery {
    schema_version: typeof HARNESS_DISCOVERY_SCHEMA_VERSION;
    harness_id: "villani-code" | "codex" | "claude-code";
    display_name: string;
    readiness: HarnessReadiness;
    detected_at: string;
}
export type AgentSystemCapabilityState = "supported" | "unsupported" | "unknown";
export type AgentSystemCapabilitySource = "declared" | "detected" | "conformance_tested" | "unsupported";
export interface AgentSystemCapabilityEvidence {
    source: AgentSystemCapabilitySource;
    reference: string;
    observed_at: string | null;
    digest: string | null;
}
export interface AgentSystemCapabilityAssessment {
    state: AgentSystemCapabilityState;
    evidence: AgentSystemCapabilityEvidence[];
    notes: string | null;
}
export interface AgentSystemIdentity {
    schema_version: typeof AGENT_SYSTEM_SCHEMA_VERSION;
    system_id: string;
    route_name: string;
    production_enabled: boolean;
    qualification_status: "qualified" | "bootstrap" | "experimental" | "provisional" | "unqualified" | "unsupported" | "disabled";
    harness: {
        harness_id: string;
        display_name: string;
        version: string;
        executable_digest: string | null;
        adapter_id: string;
        adapter_version: string;
        protocol: string;
        protocol_version: string;
        transport: "local_subprocess" | "acp_stdio" | "direct_protocol" | "structured_headless_cli";
    };
    model_provider: {
        provider: string;
        model_id: string;
        model_revision: string | null;
        endpoint_identity: string | null;
        serving_engine: string | null;
        serving_engine_version: string | null;
        context_metadata: Record<string, unknown>;
        tool_metadata: Record<string, unknown>;
    };
    execution: {
        execution_provider: string;
        environment_fingerprint: string | null;
        permission_profile: string;
        network_policy: "none" | "restricted" | "allowed" | "unknown";
        sandbox_identity: string | null;
    };
    route_profile: {
        repository_profile: string;
        task_profile: string;
        verification_policy: string;
        tool_protocol: string;
        prompt_protocol: string;
    };
    capabilities: Record<string, AgentSystemCapabilityAssessment>;
    qualification_references: Array<{
        kind: "declared" | "detected" | "conformance" | "operator";
        reference: string;
        digest: string | null;
    }>;
    billing: {
        mode: "token" | "compute_time" | "fixed" | "hybrid" | "unknown";
        cost_source: string | null;
        currency: string | null;
        unknown_fields: string[];
    };
    readiness?: HarnessReadiness | null;
    detection_time: string;
    detection_source: string;
    configuration_digest: string;
    configuration: Record<string, unknown>;
    redaction_status: "redacted" | "no_sensitive_values_detected";
    unknown_fields: string[];
}
export type HarnessAccountingStatus = "complete" | "partial" | "unknown" | "not_applicable";
export interface HarnessResult {
    schema_version: typeof HARNESS_RESULT_SCHEMA_VERSION;
    system_id: string;
    session_id: string;
    run_id: string;
    attempt_id: string;
    isolated_worktree: string;
    baseline_digest: string;
    patch: string | null;
    changed_files: string[];
    stdout: string;
    stderr: string;
    normalized_events: Array<{
        sequence: number;
        timestamp: string;
        name: string;
        payload: Record<string, unknown>;
        raw_namespace: string | null;
        raw_name: string | null;
    }>;
    raw_trace: Record<string, unknown>;
    execution_identity?: {
        harness_id: string;
        harness_version: string;
        protocol: string;
        protocol_version: string;
        protocol_schema_digest: string | null;
        session_id: string | null;
        thread_id: string | null;
        turn_id: string | null;
        model_id: string | null;
        provider: string | null;
        reasoning_effort: string | null;
        system_metadata: Record<string, unknown>;
    } | null;
    usage: {
        input_tokens: number | null;
        output_tokens: number | null;
        accounting_status: HarnessAccountingStatus;
        per_model: Record<string, Record<string, unknown>>;
    };
    cost: {
        amount: number | null;
        currency: string | null;
        accounting_status: HarnessAccountingStatus;
        source: string | null;
        per_model: Record<string, number>;
    };
    duration_ms: number | null;
    duration_accounting_status: HarnessAccountingStatus;
    harness_status: "completed" | "failed" | "cancelled";
    infrastructure_failure: {
        code: string;
        category: "cancellation" | "timeout" | "protocol" | "process" | "missing_executable" | "permission" | "environment" | "malformed_output" | "oversized_output" | "cleanup" | "transport_overload" | "rate_limit" | "unknown";
        message: string;
        retryable: boolean | null;
        details: Record<string, unknown>;
    } | null;
    artifacts: Array<{
        kind: string;
        path: string;
        digest: string | null;
        size_bytes: number | null;
    }>;
    cleanup: {
        status: "succeeded" | "failed" | "not_required" | "unknown";
        completed_at: string;
        details: Record<string, unknown>;
    };
}
export interface HarnessConformanceReport {
    schema_version: typeof HARNESS_CONFORMANCE_SCHEMA_VERSION;
    report_id: string;
    system_id: string;
    harness_id: string;
    harness_version: string;
    protocol_version: string;
    generated_at: string;
    status: "passed" | "failed" | "insufficient_evidence";
    checks: Array<{
        check_id: (typeof REQUIRED_HARNESS_CONFORMANCE_CHECKS)[number];
        status: "pass" | "fail" | "not_run";
        evidence: Record<string, unknown>;
        reason: string;
    }>;
    production_qualification_authorized: boolean;
}
