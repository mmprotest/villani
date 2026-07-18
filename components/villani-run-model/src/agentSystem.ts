export const AGENT_SYSTEM_SCHEMA_VERSION = "villani.agent_system.v1" as const;
export const HARNESS_RESULT_SCHEMA_VERSION = "villani.harness_result.v1" as const;
export const HARNESS_CONFORMANCE_SCHEMA_VERSION =
  "villani.harness_conformance_report.v1" as const;

export const REQUIRED_HARNESS_CONFORMANCE_CHECKS = [
  "manifest",
  "protocol_negotiation",
  "version_capture",
  "worktree_enforcement",
  "path_safety",
  "event_ordering",
  "cancellation",
  "timeout",
  "malformed_output",
  "oversized_output",
  "process_crash",
  "missing_executable",
  "permissions",
  "artifacts",
  "patch_correctness",
  "cleanup",
  "secret_redaction",
  "unknown_cost",
  "cross_platform_paths",
] as const;

export type AgentSystemCapabilityState =
  | "supported"
  | "unsupported"
  | "unknown";
export type AgentSystemCapabilitySource =
  | "declared"
  | "detected"
  | "conformance_tested"
  | "unsupported";

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
  qualification_status:
    | "qualified"
    | "bootstrap"
    | "unqualified"
    | "unsupported"
    | "disabled";
  harness: {
    harness_id: string;
    display_name: string;
    version: string;
    executable_digest: string | null;
    adapter_id: string;
    adapter_version: string;
    protocol: string;
    protocol_version: string;
    transport:
      | "local_subprocess"
      | "acp_stdio"
      | "direct_protocol"
      | "structured_headless_cli";
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
  detection_time: string;
  detection_source: string;
  configuration_digest: string;
  configuration: Record<string, unknown>;
  redaction_status: "redacted" | "no_sensitive_values_detected";
  unknown_fields: string[];
}

export type HarnessAccountingStatus =
  | "complete"
  | "partial"
  | "unknown"
  | "not_applicable";

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
  usage: {
    input_tokens: number | null;
    output_tokens: number | null;
    accounting_status: HarnessAccountingStatus;
  };
  cost: {
    amount: number | null;
    currency: string | null;
    accounting_status: HarnessAccountingStatus;
    source: string | null;
  };
  duration_ms: number | null;
  duration_accounting_status: HarnessAccountingStatus;
  harness_status: "completed" | "failed" | "cancelled";
  infrastructure_failure: {
    code: string;
    category:
      | "cancellation"
      | "timeout"
      | "protocol"
      | "process"
      | "missing_executable"
      | "permission"
      | "environment"
      | "malformed_output"
      | "oversized_output"
      | "cleanup"
      | "unknown";
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
