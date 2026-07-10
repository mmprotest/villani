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
  | "MATERIALIZING"
  | "COMPLETED"
  | "EXHAUSTED"
  | "FAILED";

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
  | VillaniMaterializationSnapshot;
