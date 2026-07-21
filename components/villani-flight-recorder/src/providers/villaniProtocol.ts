export type VillaniAccountingStatus =
  "complete" | "partial" | "unknown" | "not_applicable";

export interface VillaniStageUsage {
  stage:
    | "classification"
    | "coding"
    | "verification"
    | "selection"
    | "materialization"
    | "total";
  backend: string | null;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  token_accounting_status: VillaniAccountingStatus;
  model_calls: number | null;
  model_call_accounting_status: VillaniAccountingStatus;
  cost: number | null;
  cost_accounting_status: VillaniAccountingStatus;
  currency: string;
  duration_ms: number | null;
  duration_accounting_status: VillaniAccountingStatus;
  failure_state: "succeeded" | "failed" | "unknown";
}

export type VillaniControllerState =
  | "CREATED"
  | "CLASSIFYING"
  | "CLASSIFIED"
  | "POLICY_SELECTED"
  | "ATTEMPT_RUNNING"
  | "ATTEMPT_COMPLETED"
  | "VERIFYING"
  | "VERIFIED"
  | "REJECTED"
  | "ESCALATING"
  | "SELECTING"
  | "AWAITING_APPROVAL"
  | "MATERIALIZING"
  | "COMPLETED"
  | "EXHAUSTED"
  | "FAILED"
  | "CANCELLED";

export interface VillaniFailureDetail {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

export interface VillaniTaskSnapshot {
  schema_version: "villani.task.v1";
  task_id: string;
  run_id: string;
  created_at: string;
  repository_path: string;
  instruction: string;
  success_criteria: string;
  constraints: string[];
  requires_file_changes: boolean;
  metadata: Record<string, unknown>;
}

export interface VillaniRunArtifactPaths {
  task: string;
  classification: string;
  state: string;
  events: string;
  policy_decisions: string;
  selection: string;
  materialization: string;
  /** Added after v1 launch; older bundles omit both fields. */
  validation_coverage?: string | null;
  run_summary?: string;
  product_run?: string;
  agent_systems?: string | null;
  role_bindings?: string | null;
  agent_invocations?: string | null;
  route_plans?: string | null;
  economics_update?: string | null;
  adaptive_verification?: string | null;
  human_outcomes?: string | null;
  supervision_metrics?: string | null;
}

export interface VillaniRunManifestSnapshot {
  schema_version: "villani.run_manifest.v1";
  run_id: string;
  trace_id: string;
  task_id: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  final_state: VillaniControllerState;
  attempt_ids: string[];
  selected_attempt_id: string | null;
  total_cost_usd: number | null;
  cost_accounting_status: VillaniAccountingStatus;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  token_accounting_status: VillaniAccountingStatus;
  total_duration_ms: number | null;
  duration_accounting_status: VillaniAccountingStatus;
  artifact_paths: VillaniRunArtifactPaths;
  metadata: Record<string, unknown>;
  /** Added after v1 launch; absent fields remain valid for older run bundles. */
  currency?: string;
  stage_metrics?: Record<string, VillaniStageUsage>;
  total_model_calls?: number | null;
  model_call_accounting_status?: VillaniAccountingStatus;
  run_wall_clock_duration_ms?: number | null;
  run_wall_clock_duration_accounting_status?: VillaniAccountingStatus;
  agent_system_ids?: string[];
  execution_profile_id?: string | null;
  role_bindings?: Record<string, string>;
  agent_invocation_ids?: Record<string, string>;
}

export type VillaniAgentRole =
  "classification" | "coding" | "verification" | "selection";

export type VillaniAgentSystemConfig =
  | {
      kind: "api";
      id: string;
      enabled: boolean;
      provider: string;
      model: string;
      roles: VillaniAgentRole[];
      existing_backend_reference: string | null;
      timeout_seconds: number;
      max_parallel: number;
      metadata: Record<string, unknown>;
    }
  | {
      kind: "internal_runner";
      id: string;
      enabled: boolean;
      runner: string;
      roles: VillaniAgentRole[];
      timeout_seconds: number;
      max_parallel: number;
      metadata: Record<string, unknown>;
    }
  | {
      kind: "cli_agent";
      id: string;
      enabled: boolean;
      driver: "codex" | "claude_code";
      executable: string;
      model: string;
      roles: VillaniAgentRole[];
      timeout_seconds: number;
      max_parallel: number;
      instruction_policy: "native_project" | "villani_controlled";
      permission_profile: string;
      environment_policy: string;
      provider_options: Record<string, unknown>;
    };

export interface VillaniAgentSystemCatalog {
  schema_version: "villani.agent_system_config.v1";
  systems: VillaniAgentSystemConfig[];
}

export interface VillaniRoleBindings {
  schema_version: "villani.role_bindings.v1";
  profile_id: string;
  bindings: Record<VillaniAgentRole, string>;
}

export interface VillaniAgentInvocationIdentity {
  schema_version: "villani.agent_invocation_identity.v1";
  invocation_id: string;
  profile_id: string;
  role: VillaniAgentRole;
  agent_system_id: string;
  system_kind: "api" | "internal_runner" | "cli_agent";
  implementation_id: string;
  provider: string | null;
  model: string | null;
  driver: "codex" | "claude_code" | null;
  executable: string | null;
  timeout_seconds: number;
  max_parallel: number;
  availability: "ready" | "unavailable";
  unavailable_reason: string | null;
  configuration_digest: string;
  configuration: Record<string, unknown>;
}

export type VillaniCliFailure =
  | "executable_not_found"
  | "executable_not_runnable"
  | "spawn_failed"
  | "stdin_failed"
  | "timeout"
  | "cancelled"
  | "nonzero_exit"
  | "process_tree_cleanup_failed"
  | "stdout_limit_exceeded"
  | "stderr_limit_exceeded"
  | "event_line_limit_exceeded"
  | "output_decode_failed"
  | "artifact_write_failed"
  | "malformed_stream"
  | "final_output_missing"
  | "unknown_infrastructure_failure";

export interface VillaniCliOutputLimits {
  maximum_stdout_bytes: number;
  maximum_stderr_bytes: number;
  maximum_stdout_chunk_bytes: number;
  maximum_stderr_chunk_bytes: number;
  maximum_event_line_bytes: number;
  maximum_tail_bytes: number;
  read_chunk_bytes: number;
}

export interface VillaniCliInvocation {
  schema_version: "villani.cli_invocation.v1";
  executable: string;
  executable_identity: { status: "unresolved"; sha256: null };
  arguments: string[];
  environment: Array<{
    name: string;
    provenance: "inherited" | "addition" | "override" | "explicit";
    redacted: boolean;
  }>;
  role_workspace_identity: Record<string, unknown>;
  target_repository_writable: boolean;
  cwd: string;
  stdin: {
    provided: boolean;
    size_bytes: number;
    artifact_reference: string | null;
    sha256: string | null;
  };
  timeout_seconds: number;
  graceful_shutdown_seconds: number;
  limits: VillaniCliOutputLimits;
  event_stream_format: "none" | "jsonl";
  utf8_policy: "replacement" | "strict";
  final_output_path: string | null;
  require_final_output: boolean;
  started_at: string;
}

export interface VillaniCliStreamResult {
  artifact_path: string;
  total_bytes_observed: number;
  bytes_persisted: number;
  limit_exceeded: boolean;
  largest_read_bytes: number;
  decode_replacements: boolean;
  output_after_cancellation: boolean;
}

export interface VillaniCliProcessResult {
  schema_version: "villani.cli_process_result.v1";
  infrastructure_state: "succeeded" | "failed" | "cancelled" | "timed_out";
  failure: VillaniCliFailure | null;
  failures: Array<{
    code: VillaniCliFailure;
    message: string;
    stream: "stdout" | "stderr" | "events" | "stdin" | "artifact" | null;
    configured_limit_bytes: number | null;
    observed_bytes: number | null;
  }>;
  started_at: string;
  completed_at: string;
  duration_ms: number;
  pid: number | null;
  exit_code: number | null;
  timed_out: boolean;
  cancelled: boolean;
  cancellation_origin:
    | "user"
    | "controller"
    | "timeout"
    | "parent_service_shutdown"
    | "runtime_failure"
    | null;
  termination_reason: string | null;
  graceful_termination_requested: boolean;
  graceful_termination_succeeded: boolean;
  forced_termination: boolean;
  cleanup_status: "succeeded" | "failed" | "not_required";
  cleanup_error: string | null;
  target_repository_writable: boolean;
  stdin_bytes_delivered: number;
  stdout: VillaniCliStreamResult;
  stderr: VillaniCliStreamResult;
  raw_events: VillaniCliStreamResult;
  final_output_path: string | null;
  final_output_present: boolean | null;
  invocation_artifact: string;
  output_tail_artifact: string;
  process_result_artifact: string;
  artifact_set_complete: boolean;
}

export interface VillaniCliOutputTail {
  schema_version: "villani.cli_output_tail.v1";
  stdout: string;
  stderr: string;
  maximum_tail_bytes: number;
  utf8_policy: "replacement" | "strict";
  stdout_decode_replacements: boolean;
  stderr_decode_replacements: boolean;
}

export interface VillaniCodexCoderResult {
  schema_version: "villani.codex_coder_result.v1";
  status: "completed" | "blocked";
  summary: string;
  tests_run: Array<{
    command: string;
    reported_exit_status: number | null;
    reported_result: string;
  }>;
  known_limitations: string[];
  files_the_agent_believes_changed: string[];
}

export interface VillaniRunStateSnapshot {
  schema_version: "villani.run_state.v1";
  run_id: string;
  trace_id: string;
  state: VillaniControllerState;
  previous_state: VillaniControllerState | null;
  terminal: boolean;
  updated_at: string;
  last_event_id: string;
  last_sequence: number;
  active_attempt_id: string | null;
  attempt_count: number;
  accepted_candidate_ids: string[];
  failure: VillaniFailureDetail | null;
  metadata: Record<string, unknown>;
}

export interface VillaniEventEnvelope {
  schema_version: "villani.event.v1";
  event_id: string;
  sequence: number;
  timestamp: string;
  trace_id: string;
  run_id: string;
  attempt_id: string | null;
  parent_event_id: string | null;
  source: string;
  event_type: string;
  payload: Record<string, unknown>;
}

export interface VillaniClassificationSnapshot {
  schema_version: "villani.classification.v1";
  classification_id: string;
  run_id: string;
  task_id: string;
  classified_at: string;
  difficulty: "easy" | "medium" | "hard";
  risk: "low" | "medium" | "high";
  category: string;
  required_capabilities: string[];
  estimated_attempts_needed: number;
  needs_tests: boolean;
  confidence: number;
  reasoning_summary: string;
  signals: Record<string, unknown>;
  metadata: Record<string, unknown>;
  llm_usage?: VillaniStageUsage[];
}

export interface VillaniBackendConsideration {
  backend_name: string;
  model: string | null;
  eligible: boolean;
  capability_score: number | null;
  estimated_cost_usd: number | null;
  cost_accounting_status: VillaniAccountingStatus;
  rejection_reasons: string[];
}

export interface VillaniBudgetSnapshot {
  remaining_attempts: number | null;
  remaining_cost_usd: number | null;
  cost_accounting_status: VillaniAccountingStatus;
  remaining_wall_time_ms: number | null;
  duration_accounting_status: VillaniAccountingStatus;
}

export interface VillaniPolicyDecisionSnapshot {
  schema_version: "villani.policy_decision.v1";
  decision_id: string;
  run_id: string;
  trace_id: string;
  timestamp: string;
  decision_sequence: number;
  classification_id: string;
  policy_version: string;
  action: "attempt" | "retry" | "escalate" | "select" | "exhaust" | "fail";
  reason: string;
  considered_backends: VillaniBackendConsideration[];
  chosen_backend: string | null;
  chosen_model: string | null;
  attempt_id: string | null;
  budget_before: VillaniBudgetSnapshot;
  budget_after: VillaniBudgetSnapshot;
  metadata: Record<string, unknown>;
}

export interface VillaniAttemptSnapshot {
  schema_version: "villani.attempt.v1";
  attempt_id: string;
  run_id: string;
  trace_id: string;
  ordinal: number;
  backend_name: string;
  runner_name: string;
  model: string | null;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  started_at: string | null;
  completed_at: string | null;
  worktree_path: string;
  patch_path: string | null;
  patch_sha256: string | null;
  patch_bytes: number | null;
  stdout_path: string | null;
  stderr_path: string | null;
  runner_telemetry_path: string | null;
  trace_path: string | null;
  exit_code: number | null;
  duration_ms: number | null;
  duration_accounting_status: VillaniAccountingStatus;
  input_tokens: number | null;
  output_tokens: number | null;
  token_accounting_status: VillaniAccountingStatus;
  cost_usd: number | null;
  cost_accounting_status: VillaniAccountingStatus;
  error: VillaniFailureDetail | null;
  metadata: Record<string, unknown>;
  agent_system_id?: string | null;
  agent_system_identity_path?: string | null;
  harness_result_path?: string | null;
}

export type VillaniCapabilityState = "supported" | "unsupported" | "unknown";
export type VillaniCapabilitySource =
  "declared" | "detected" | "conformance_tested" | "unsupported";

export interface VillaniCapabilityAssessment {
  state: VillaniCapabilityState;
  evidence: Array<{
    source: VillaniCapabilitySource;
    reference: string;
    observed_at: string | null;
    digest: string | null;
  }>;
  notes: string | null;
}

export interface VillaniAgentSystemIdentity {
  schema_version: "villani.agent_system.v1";
  system_id: string;
  route_name: string;
  production_enabled: boolean;
  qualification_status:
    | "qualified"
    | "bootstrap"
    | "experimental"
    | "provisional"
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
  capabilities: Record<string, VillaniCapabilityAssessment>;
  qualification_references: Array<Record<string, unknown>>;
  billing: {
    mode: "token" | "compute_time" | "fixed" | "hybrid" | "unknown";
    cost_source: string | null;
    currency: string | null;
    unknown_fields: string[];
  };
  readiness?: VillaniHarnessReadiness | null;
  detection_time: string;
  detection_source: string;
  configuration_digest: string;
  configuration: Record<string, unknown>;
  redaction_status: "redacted" | "no_sensitive_values_detected";
  unknown_fields: string[];
}

export interface VillaniHarnessReadiness {
  installed: boolean;
  command_identity: string;
  exact_version: string | null;
  supported_version_range: string | null;
  version_supported: boolean | null;
  authentication_status: "ready" | "not_ready" | "unknown" | "not_applicable";
  protocol: string;
  conformance_status: "passed" | "failed" | "not_run" | "insufficient_evidence";
  qualification_state: VillaniAgentSystemIdentity["qualification_status"];
  custom_model_capability: VillaniCapabilityState;
  custom_provider_capability: VillaniCapabilityState;
  local_model_capability: VillaniCapabilityState;
  repair_action: string;
  details: Record<string, unknown>;
}

export interface VillaniHarnessDiscovery {
  schema_version: "villani.harness_discovery.v1";
  harness_id: "villani-code" | "codex" | "claude-code";
  display_name: string;
  readiness: VillaniHarnessReadiness;
  detected_at: string;
}

export type VillaniQualificationObservation = QualificationObservation;
export type VillaniQualificationInvalidation = QualificationInvalidation;
export type VillaniQualificationSnapshot = QualificationSnapshot;
export type VillaniGateCReport = GateCReport;
export type VillaniEconomicsObservation = EconomicsObservation;
export type VillaniEconomicsSnapshot = EconomicsSnapshot;
export type VillaniOnlineEvidenceUpdateReport = OnlineEvidenceUpdateReport;
export type VillaniRoutePlan = RoutePlan;
export type VillaniRoutePolicy = RoutePolicy;
export type VillaniRoutePolicyEvaluation = RoutePolicyEvaluation;
export type VillaniRoutePolicyPublication = RoutePolicyPublication;
export type VillaniAdaptiveVerificationPlan = AdaptiveVerificationPlan;
export type VillaniBinaryVerificationDecision = BinaryVerificationDecision;
export type VillaniCompactReviewPackage = CompactReviewPackage;
export type VillaniHumanOutcome = HumanOutcome;
export type VillaniSupervisionMetrics = SupervisionMetrics;
export type VillaniGateDReport = GateDReport;

export interface VillaniHarnessResult {
  schema_version: "villani.harness_result.v1";
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
  execution_identity?: Record<string, unknown> | null;
  usage: Record<string, unknown>;
  cost: Record<string, unknown>;
  duration_ms: number | null;
  duration_accounting_status: VillaniAccountingStatus;
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
      | "transport_overload"
      | "rate_limit"
      | "unknown";
    message: string;
    retryable: boolean | null;
    details: Record<string, unknown>;
  } | null;
  artifacts: Array<Record<string, unknown>>;
  cleanup: Record<string, unknown>;
}

export interface VillaniHarnessConformanceReport {
  schema_version: "villani.harness_conformance_report.v1";
  report_id: string;
  system_id: string;
  harness_id: string;
  harness_version: string;
  protocol_version: string;
  generated_at: string;
  status: "passed" | "failed" | "insufficient_evidence";
  checks: Array<{
    check_id: string;
    status: "pass" | "fail" | "not_run";
    evidence: Record<string, unknown>;
    reason: string;
  }>;
  production_qualification_authorized: boolean;
}

export interface VillaniRequirementResult {
  requirement_id: string;
  description: string;
  outcome: "passed" | "failed" | "missing" | "not_applicable";
  evidence_ids: string[];
}

export interface VillaniEvidence {
  evidence_id: string;
  kind: string;
  summary: string;
  artifact_path: string | null;
  [key: string]: unknown;
}

export interface VillaniVerificationSnapshot {
  schema_version: "villani.verification.v1";
  run_id: string;
  attempt_id: string;
  verified_at: string;
  verifier: string;
  outcome: "accepted" | "rejected" | "unclear" | "error";
  acceptance_eligible: boolean;
  confidence: number | null;
  reason: string;
  requirement_results: VillaniRequirementResult[];
  success_evidence: VillaniEvidence[];
  failure_evidence: VillaniEvidence[];
  missing_evidence: VillaniEvidence[];
  risk_flags: string[];
  recommended_action:
    "accept" | "reject" | "retry_verifier" | "escalate" | "fail";
  raw_verifier_artifact: string | null;
  metadata: Record<string, unknown>;
  llm_usage?: VillaniStageUsage[];
}

export interface VillaniCandidateRanking {
  attempt_id: string;
  rank: number;
  reason: string;
  actual_cost_usd: number | null;
  cost_accounting_status: VillaniAccountingStatus;
  evidence: Record<string, unknown>;
}

export interface VillaniSelectionSnapshot {
  schema_version: "villani.selection.v1";
  selection_id: string;
  run_id: string;
  selected_at: string;
  strategy: string;
  eligible_candidate_ids: string[];
  selected_candidate_ids: string[];
  rankings: VillaniCandidateRanking[];
  reason: string;
  advisory_comparison: Record<string, unknown> | null;
  metadata: Record<string, unknown>;
}

export interface VillaniMaterializationSnapshot {
  schema_version: "villani.materialization.v1";
  materialization_id: string;
  run_id: string;
  trace_id: string;
  selection_id: string;
  selected_attempt_id: string;
  started_at: string;
  completed_at: string | null;
  status: "pending" | "running" | "succeeded" | "failed";
  source_patch_path: string;
  target_repository_path: string;
  materialized_patch_path: string | null;
  patch_sha256: string | null;
  changed_files: string[];
  failure: VillaniFailureDetail | null;
  metadata: Record<string, unknown>;
}

export interface VillaniValidationCommandCoverage {
  validation_id: string;
  command_identity: string;
  argv: string[];
  safe_display: string;
  execution_role: string;
  working_directory: string;
  status:
    "passed" | "failed" | "not_run" | "unavailable" | "infrastructure_error";
  exit_status: number | null;
  started_at: string;
  ended_at: string;
  explicitly_named_test_targets: string[];
  changed_test_files_proven: string[];
  changed_test_files_plausibly_included: string[];
  requirement_ids_covered: string[];
  coverage_provenance: string[];
  confidence: "high" | "medium" | "low" | "unknown";
  coverage_unestablished_reasons: string[];
  artifact_references: string[];
}

export interface VillaniValidationCoverage {
  schema_version: "villani.validation_coverage.v1";
  run_id: string;
  attempt_id: string;
  candidate_id: string;
  commands: VillaniValidationCommandCoverage[];
  requirement_ids: string[];
  requirements_covered: string[];
  requirements_not_covered: string[];
  generated_at: string;
  migration: Record<string, unknown> | null;
}

export interface VillaniRunSummary {
  schema_version: "villani.run_summary.v1";
  run_id: string;
  attempt_id: string | null;
  checks: {
    passed: number | null;
    failed: number | null;
    not_run: number | null;
    unavailable: number | null;
    accounting_status: "complete" | "unknown";
  };
  focused_probes: {
    passed: number | null;
    failed: number | null;
    not_run: number | null;
    unavailable: number | null;
    accounting_status: "complete" | "unknown";
  };
  requirements: {
    proved: number | null;
    not_proved: number | null;
    accounting_status: "complete" | "unknown";
  };
  accounting: {
    known: boolean;
    accounting_status: string;
    total_cost: number | null;
    currency: string | null;
  };
  acceptance: { decision: boolean; reason_code: string; reason: string };
  source_artifacts: string[];
  generated_at: string;
  migration: Record<string, unknown> | null;
}

export type VillaniProtocolDocument =
  | VillaniTaskSnapshot
  | VillaniRunManifestSnapshot
  | VillaniRunStateSnapshot
  | VillaniEventEnvelope
  | VillaniClassificationSnapshot
  | VillaniPolicyDecisionSnapshot
  | VillaniAttemptSnapshot
  | VillaniVerificationSnapshot
  | VillaniSelectionSnapshot
  | VillaniMaterializationSnapshot
  | VillaniValidationCoverage
  | VillaniRunSummary
  | VillaniAgentSystemIdentity
  | VillaniAgentSystemCatalog
  | VillaniRoleBindings
  | VillaniAgentInvocationIdentity
  | VillaniCliInvocation
  | VillaniCliProcessResult
  | VillaniCliOutputTail
  | VillaniCodexCoderResult
  | VillaniHarnessResult
  | VillaniHarnessConformanceReport
  | VillaniHarnessDiscovery
  | VillaniQualificationObservation
  | VillaniQualificationInvalidation
  | VillaniQualificationSnapshot
  | VillaniGateCReport
  | VillaniEconomicsObservation
  | VillaniEconomicsSnapshot
  | VillaniOnlineEvidenceUpdateReport
  | VillaniRoutePlan
  | VillaniRoutePolicy
  | VillaniRoutePolicyEvaluation
  | VillaniRoutePolicyPublication
  | VillaniAdaptiveVerificationPlan
  | VillaniBinaryVerificationDecision
  | VillaniCompactReviewPackage
  | VillaniHumanOutcome
  | VillaniSupervisionMetrics
  | VillaniGateDReport;
import type {
  AdaptiveVerificationPlan,
  BinaryVerificationDecision,
  CompactReviewPackage,
  EconomicsObservation,
  EconomicsSnapshot,
  GateCReport,
  GateDReport,
  HumanOutcome,
  QualificationInvalidation,
  QualificationObservation,
  QualificationSnapshot,
  OnlineEvidenceUpdateReport,
  RoutePlan,
  RoutePolicy,
  RoutePolicyEvaluation,
  RoutePolicyPublication,
  SupervisionMetrics,
} from "@villani/run-model";
