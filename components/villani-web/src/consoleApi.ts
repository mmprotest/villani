import {
  consoleReplayFromRunDetail,
  type ArtifactDescriptor,
  type AgentSystemIdentity,
  type QualificationAssessment,
  type EconomicsProfile,
  type HarnessDiscovery,
  type ConsoleBootstrap,
  type ConsoleHistoryEntry,
  type ConsoleReplaySnapshot,
  type RunDetail,
  type RunEvent,
  type ProductRun,
} from "@villani/run-model";
import { ApiError } from "./api";

export interface ConsoleHistoryDocument {
  schema_version: "villani.console.history.v1";
  entries: ConsoleHistoryEntry[];
  warnings: string[];
}

export interface ConsoleHomeDocument {
  schema_version: "villani.console.home.v1";
  service: ConsoleBootstrap["service"];
  models: ConsoleBootstrap["models"];
  recent_runs: ConsoleHistoryEntry[];
  recent_sessions: ConsoleHistoryEntry[];
  accepted_task_rate: number | null;
  recent_recovery_events: Record<string, unknown>[];
  pending_synchronization: number;
  setup_issues: string[];
  warnings: string[];
}

export interface RunRepositoryOption {
  path: string;
  name: string;
  valid: boolean;
  dirty: boolean | null;
  source: string;
  failure?: RunFailure | null;
}

export interface RunChoice {
  id: string;
  label: string;
  description: string;
}

export interface ConsoleRunOptions {
  schema_version: "villani.console.run_options.v1";
  repositories: RunRepositoryOption[];
  default_repository: string | null;
  delivery_modes: RunChoice[];
  approval_modes: RunChoice[];
  policies: RunChoice[];
  policy_presets: PolicyPreset[];
  advanced_policies: RunChoice[];
  routing_modes: string[];
  defaults: {
    delivery_mode: string;
    approval_mode: string;
    policy_preset: string;
    policy_selection: string;
    routing_mode: string;
    max_attempts: number;
    max_cost: number | null;
    max_wall_time: number | null;
  };
  setup_issues: string[];
}

export interface PolicyPreset extends RunChoice {
  active: boolean;
  advanced: boolean;
  policy_version: string;
}

export interface ConsoleModelInventory {
  schema_version: string;
  models: ConsoleBootstrap["models"];
  bootstrap_default: string | null;
  capability_states: string[];
  qualification?: {
    minimum_sample_count: number;
    minimum_conservative_confidence_bound: number;
    policy_version: string;
    repository_context?: {
      repository_id: string;
      repository_head: string | null;
      task_profile: {
        category: string;
        difficulty: string;
        risk: string;
        required_capabilities: string[];
      };
    } | null;
  };
  economics?: {
    policy_version: string;
    objective_version: string;
    default_explanation: string;
    unknown_accounting_note: string;
  };
  setup_issues?: string[];
  detections?: Record<string, unknown>[];
  agent_systems?: ConsoleAgentSystem[];
  agent_harnesses?: HarnessDiscovery[];
  agent_system_migration?: Record<string, unknown>;
}

export type ConsoleAgentSystem = AgentSystemIdentity & {
  repository_qualification?: QualificationAssessment;
  repository_economics?: {
    profile: EconomicsProfile;
    matching_profile_count: number;
    scope_note: string;
  } | null;
};

export interface ConsolePolicies {
  schema_version: string;
  active_preset: string;
  presets: PolicyPreset[];
  setup_issues: string[];
}

export interface PolicyPreview {
  schema_version: "villani.policy_preview.v1";
  raw_classification: Record<string, unknown> & {
    difficulty: string;
    risk: string;
    confidence: number;
  };
  effective_classification: Record<string, unknown> & {
    difficulty: string;
    risk: string;
    confidence: number;
  };
  adjustments: {
    field: string;
    before: string | number;
    after: string | number;
    rule_id: string;
    reason: string;
  }[];
  eligible_models: Record<string, unknown>[];
  excluded_models: (Record<string, unknown> & {
    backend_name?: string;
    reasons?: string[];
  })[];
  selected_coding_route: {
    backend: string | null;
    model: string | null;
    action: string;
    reason: string;
    route_provenance: { basis?: string } | null;
  };
  selected_verifier_route: Record<string, unknown> & {
    selected?: Record<string, unknown> | null;
  };
  estimated_cost: {
    value: number | null;
    status: string;
    currency: string | null;
  };
  uncertainty: string[];
  policy_version: { public: string; preset: string; controller: string };
  coding_attempt_executed: false;
}

export interface PolicySimulation {
  schema_version: "villani.policy_simulation.v1";
  preset: string;
  tasks_evaluated: number;
  tasks_affected: number;
  route_changes: Record<string, unknown>[];
  estimated_cost_differences: {
    status: string;
    simulated_minus_recorded_total: number | null;
    known_task_count: number;
    unknown_task_count: number;
  };
  outcome_evidence_limitations: string[];
  unsupported_counterfactual_claims: string[];
  causal_savings_supported: false;
  live_policy_changed: false;
}

export interface ValidationSuggestion {
  suggestion_id: string;
  argv: string[];
  display_command: string;
  confidence: number;
  confidence_label: string;
  requires_confirmation: boolean;
  reason: string;
  source: string;
  advisory_only: true;
  authoritative: false;
}

export interface ConsoleValidationDiscovery {
  schema_version: string;
  repository: RunRepositoryOption;
  repository_fingerprint?: string;
  suggestions: ValidationSuggestion[];
  selected_suggestion_id: string | null;
  authority: string;
  failure: RunFailure | null;
}

export interface RunFailure {
  code: string;
  what_failed: string;
  what_villani_tried: string;
  missing_evidence: string;
  patch_preserved: boolean;
  patch_status: string;
  next_action: string;
}

export interface ConsoleRunSubmission {
  schema_version: "villani.console.run_submission.v1";
  status: string;
  run_id: string | null;
  run_url?: string;
  replay_url?: string;
  validation_commands?: string[];
  deduplicated?: boolean;
  failure: RunFailure | null;
}

export interface RunProgressLine {
  tone: string;
  symbol: string;
  message: string;
}

export interface RunPatchReview {
  files_changed: string[];
  insertions: number;
  deletions: number;
  validation_evidence: {
    evidence_id?: string;
    kind?: string;
    summary?: string;
    artifact_path?: string | null;
  }[];
  verifier_authority: string;
  candidate_comparison: Record<string, unknown>[];
  remaining_risks: string[];
  cost: {
    value: number | null;
    accounting_status: string;
    currency: string;
  };
  unrelated_change_warnings: string[];
  sensitive_file_warnings: string[];
}

export interface RunDelivery {
  mode: string;
  state: string;
  label: string;
  repository_modified: boolean;
  target_worktree_modified: boolean;
  patch_artifact?: string | null;
  patch_sha256?: string | null;
  authority: {
    policy_version?: string;
    required?: string;
    observed?: string;
    permitted?: boolean;
    reasons?: string[];
  };
  approval: {
    status?: string;
    deadline?: string | null;
    timeout_policy?: string;
    authenticated_required?: boolean;
    allow_candidate_change?: boolean;
    actor?: string | null;
    reason?: string | null;
  };
  review: RunPatchReview;
  result: Record<string, unknown>;
  failure?: Record<string, unknown> | null;
  eligible_candidate_ids: string[];
}

export interface RunPresentation {
  schema_version: "villani.run_presentation.v1";
  run_id: string;
  outcome: "RUNNING" | "AWAITING APPROVAL" | "ACCEPTED" | "EXHAUSTED" | "FAILED";
  execution_status?: string;
  summary: string;
  changed?: {
    files: string[];
    file_count: number;
    zero_file_change: boolean;
    delivery_status?: string;
  };
  confidence?: {
    value: number | null;
    label: string;
    acceptance_eligible: boolean;
    authority: string;
  };
  validation: {
    commands: { command: string; passed?: boolean; authority: string }[];
    checks_passed: number | null;
    checks_failed: number | null;
    checks_not_run?: number | null;
    checks_unavailable?: number | null;
    checks_accounting_status?: "complete" | "unknown";
    focused_probes_passed?: number | null;
    focused_probes_failed?: number | null;
    focused_probes_not_run?: number | null;
    focused_probes_unavailable?: number | null;
    focused_probes_accounting_status?: "complete" | "unknown";
    requirements_proved?: number | null;
    requirements_not_proved?: number | null;
    requirements_verified: number | null;
    requirements_accounting_status?: "complete" | "unknown";
    authority: string;
  };
  canonical_summary?: {
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
  };
  remaining_risks?: string[];
  cost?: {
    currency: string;
    coding: number | null;
    coding_status?: string;
    verification: number | null;
    verification_status?: string;
    total: number | null;
    accounting_status: string;
  };
  recovery?: string[];
  next_actions?: { label: string; action: string }[];
  delivery?: RunDelivery;
  failure?: RunFailure | null;
  synchronization_state?: string;
  synchronization_failure?: RunFailure | null;
  lineage: {
    relationship?: string;
    parent_run_id?: string;
    root_run_id?: string;
  };
  progress: RunProgressLine[];
  attempts?: { attempt_id: string; ordinal: number; backend: string }[];
  selected_attempt_id?: string | null;
}

const workspaceFallback = (): ConsoleBootstrap => ({
  schema_version: "villani.console.bootstrap.v1",
  mode: "connected",
  data_source: "workspace",
  version: "unknown",
  workspace: { connected: true, id: null, endpoint: location.origin },
  service: { status: "connected", started_at: null, log_path: null, last_error: null },
  setup: { configured: true, valid: true, schema_version: null, issues: [] },
  synchronization: { pending: 0, dead_letters: 0 },
  storage: { home: "", runs: "", spool: "", writable: false },
  models: [],
  active_policy: null,
});

export class ConsoleClient {
  constructor(
    private baseUrl = "",
    private token = "",
  ) {}

  private headers(): Record<string, string> {
    return this.token ? { Authorization: `Bearer ${this.token}` } : {};
  }

  private async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      headers: { ...this.headers(), Accept: "application/json" },
      credentials: "same-origin",
      signal,
    });
    if (!response.ok)
      throw new ApiError(response.status, `Request failed (${response.status})`);
    const contentType = response.headers?.get?.("content-type") ?? "application/json";
    if (!contentType.includes("json"))
      throw new ApiError(
        response.status,
        "Console endpoint returned a non-JSON response",
      );
    return response.json() as Promise<T>;
  }

  private async post<T>(
    path: string,
    value: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: {
        ...this.headers(),
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      credentials: "same-origin",
      body: JSON.stringify(value),
      signal,
    });
    const contentType = response.headers?.get?.("content-type") ?? "";
    const payload = contentType.includes("json")
      ? ((await response.json()) as Record<string, unknown>)
      : null;
    if (!response.ok)
      throw new ApiError(
        response.status,
        typeof payload?.message === "string"
          ? payload.message
          : `Request failed (${response.status})`,
      );
    return payload as T;
  }

  async bootstrap(signal?: AbortSignal): Promise<ConsoleBootstrap> {
    try {
      return await this.get<ConsoleBootstrap>("/v1/console/bootstrap", signal);
    } catch (error) {
      if (signal?.aborted) throw error;
      // Older connected deployments do not expose the Console bootstrap yet.
      // Their existing authenticated run APIs remain a supported migration path.
      return workspaceFallback();
    }
  }

  home(signal?: AbortSignal) {
    return this.get<ConsoleHomeDocument>("/v1/console/home", signal);
  }

  history(refresh = false, signal?: AbortSignal) {
    return this.get<ConsoleHistoryDocument>(
      `/v1/console/history${refresh ? "?refresh=1" : ""}`,
      signal,
    );
  }

  models(signal?: AbortSignal) {
    return this.get<ConsoleModelInventory>("/v1/console/models", signal);
  }

  detectModels(signal?: AbortSignal) {
    return this.post<ConsoleModelInventory>("/v1/console/models:detect", {}, signal);
  }

  testModels(backendName?: string, signal?: AbortSignal) {
    return this.post<{
      schema_version: string;
      results: {
        backend_name: string;
        availability: string;
        diagnostic: string;
        tested_at: string;
        model_tokens_used: 0;
      }[];
      model_tokens_used: 0;
    }>(
      "/v1/console/models:test",
      backendName ? { backend_name: backendName } : {},
      signal,
    );
  }

  addModel(value: Record<string, unknown>, signal?: AbortSignal) {
    return this.post<ConsoleModelInventory>("/v1/console/models:add", value, signal);
  }

  removeModel(backendName: string, signal?: AbortSignal) {
    return this.post<ConsoleModelInventory>(
      "/v1/console/models:remove",
      { backend_name: backendName },
      signal,
    );
  }

  setDefaultModel(backendName: string, signal?: AbortSignal) {
    return this.post<ConsoleModelInventory>(
      "/v1/console/models:default",
      { backend_name: backendName },
      signal,
    );
  }

  policies(signal?: AbortSignal) {
    return this.get<ConsolePolicies>("/v1/console/policies", signal);
  }

  selectPolicy(preset: string, signal?: AbortSignal) {
    return this.post<ConsolePolicies>(
      "/v1/console/policies:select",
      { preset },
      signal,
    );
  }

  previewPolicy(value: Record<string, unknown>, signal?: AbortSignal) {
    return this.post<PolicyPreview>("/v1/console/policy:preview", value, signal);
  }

  simulatePolicy(preset: string, signal?: AbortSignal) {
    return this.post<PolicySimulation>(
      "/v1/console/policies:simulate",
      { preset },
      signal,
    );
  }

  runOptions(signal?: AbortSignal) {
    return this.get<ConsoleRunOptions>("/v1/console/run-options", signal);
  }

  discoverValidation(repository: string, signal?: AbortSignal) {
    return this.post<ConsoleValidationDiscovery>(
      "/v1/console/validation:discover",
      { repository },
      signal,
    );
  }

  startRun(value: Record<string, unknown>, signal?: AbortSignal) {
    return this.post<ConsoleRunSubmission>("/v1/console/runs", value, signal);
  }

  runStatus(runId: string, signal?: AbortSignal) {
    return this.get<ProductRun>(
      `/v1/console/runs/${encodeURIComponent(runId)}/status`,
      signal,
    );
  }

  /** @deprecated Retained only while legacy source snapshots remain readable. */
  runStatusLegacy(runId: string, signal?: AbortSignal) {
    return this.get<RunPresentation>(
      `/v1/console/runs/${encodeURIComponent(runId)}/status`,
      signal,
    );
  }

  runEvents(runId: string, afterSequence: number, signal?: AbortSignal) {
    return this.get<ProductRun>(
      `/v1/console/runs/${encodeURIComponent(runId)}/events?after=${afterSequence}&wait=20`,
      signal,
    );
  }

  cancelRun(runId: string, signal?: AbortSignal) {
    return this.post<ProductRun>(
      `/v1/console/runs/${encodeURIComponent(runId)}/cancel`,
      {},
      signal,
    );
  }

  approvalAction(
    runId: string,
    value: {
      action: "approve" | "reject" | "request_rerun" | "choose_candidate";
      reason?: string;
      candidate_id?: string;
    },
    signal?: AbortSignal,
  ) {
    return this.post<ProductRun>(
      `/v1/console/runs/${encodeURIComponent(runId)}/approval`,
      value,
      signal,
    );
  }

  /** @deprecated Retained only while legacy source snapshots remain readable. */
  approvalActionLegacy(
    runId: string,
    value: {
      action: "approve" | "reject" | "request_rerun" | "choose_candidate";
      reason?: string;
      candidate_id?: string;
    },
    signal?: AbortSignal,
  ) {
    return this.post<RunPresentation>(
      `/v1/console/runs/${encodeURIComponent(runId)}/approval`,
      value,
      signal,
    );
  }

  settings(signal?: AbortSignal) {
    return this.get<Record<string, unknown>>("/v1/console/settings", signal);
  }

  workspace(surface: string, signal?: AbortSignal) {
    return this.get<{
      connected: boolean;
      workspace_id: string | null;
      surface: string | null;
      items: Record<string, unknown>[];
      message: string;
    }>(`/v1/console/workspace/${encodeURIComponent(surface)}`, signal);
  }

  private async connectedReplay(runId: string, signal?: AbortSignal) {
    const detail = await this.get<RunDetail>(
      `/v1/runs/${encodeURIComponent(runId)}`,
      signal,
    );
    const events: RunEvent[] = [];
    let cursor: string | null = null;
    do {
      const suffix: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
      const page: { events: RunEvent[]; next_cursor: string | null } = await this.get<{
        events: RunEvent[];
        next_cursor: string | null;
      }>(`/v1/runs/${encodeURIComponent(runId)}/events?limit=500${suffix}`, signal);
      events.push(...page.events);
      cursor = page.next_cursor;
    } while (cursor && !signal?.aborted);
    const artifacts: ArtifactDescriptor[] = [];
    cursor = null;
    do {
      const suffix: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
      const page: { artifacts: ArtifactDescriptor[]; next_cursor: string | null } =
        await this.get<{
          artifacts: ArtifactDescriptor[];
          next_cursor: string | null;
        }>(
          `/v1/runs/${encodeURIComponent(runId)}/artifacts?limit=100${suffix}`,
          signal,
        );
      artifacts.push(...page.artifacts);
      cursor = page.next_cursor;
    } while (cursor && !signal?.aborted);
    return consoleReplayFromRunDetail(detail, events, artifacts, "SYNCHRONIZED");
  }

  async replay(
    id: string,
    kind: "run" | "session",
    dataSource: ConsoleBootstrap["data_source"],
    signal?: AbortSignal,
  ): Promise<ConsoleReplaySnapshot> {
    if (kind === "run" && dataSource === "workspace")
      return this.connectedReplay(id, signal);
    try {
      return await this.get<ConsoleReplaySnapshot>(
        `/v1/console/${kind === "run" ? "runs" : "sessions"}/${encodeURIComponent(id)}`,
        signal,
      );
    } catch (error) {
      throw error;
    }
  }
}
