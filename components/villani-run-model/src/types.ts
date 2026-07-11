export type RunTone = "success" | "warning" | "error" | "info" | "muted";
export type RunStatus =
  "succeeded" | "failed" | "partial" | "unknown" | "not_applicable";

export interface RunStatusSummary {
  status: RunStatus;
  label: string;
  tone: RunTone;
  reason: string;
  failedCommands: number;
  failedTests: number;
  totalCommands: number;
  totalTests: number;
  fileEdits: number;
  hasFinalAnswer?: boolean;
}

export interface RunEvent {
  id: string;
  run_id?: string;
  event_id?: string;
  idempotency_key?: string;
  sequence?: number;
  occurred_at?: string;
  observed_at?: string;
  timestamp?: string;
  trace_id?: string;
  span_id?: string;
  parent_span_id?: string | null;
  parent_event_id?: string | null;
  attempt_id?: string | null;
  source?: string;
  kind?: string;
  name?: string;
  status?: string;
  type?: string;
  title?: string;
  command?: string;
  exit_code?: number;
  path?: string;
  duration_ms?: number;
  attributes?: Record<string, unknown>;
  body?: Record<string, unknown>;
  payload?: Record<string, unknown>;
  raw?: unknown;
}

export interface RunSummary {
  id: string;
  workspace_id?: string;
  project_id?: string;
  repository_id?: string;
  trace_id?: string;
  status: string;
  first_occurred_at: string;
  first_observed_at?: string;
  last_observed_at: string;
}

export interface AttemptSummary {
  id: string;
  status: string;
}
export interface ArtifactDescriptor {
  artifact_id: string;
  logical_role: string;
  media_type: string;
  size_bytes: number;
  sensitivity: "public" | "internal" | "confidential" | "restricted" | "secret";
  status?: string;
  digest?: { algorithm: string; value: string };
  attributes?: Record<string, unknown>;
}

export interface RunDetail extends RunSummary {
  attempts: AttemptSummary[];
  outcomes: Record<string, unknown>[];
  artifact_count: number;
  spans?: RunSpan[];
  artifacts?: ArtifactDescriptor[];
}

export interface RunSpan {
  span_id: string;
  parent_span_id: string | null;
  trace_id?: string;
  run_id?: string;
  attempt_id: string | null;
  kind: string;
  name: string;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  attributes: Record<string, unknown>;
}

export interface CandidateView {
  attemptId: string;
  status: string;
  eligible: boolean;
  selected: boolean;
  requirementResults: unknown[];
  evidenceGrades: string[];
  risks: string[];
  patchDigest?: string;
  explanation?: string;
  costUsd?: number | null;
  inputTokens?: number;
  outputTokens?: number;
}

export interface StageMetric {
  key: string;
  stage: string;
  attemptId?: string;
  model?: string;
  retry: boolean;
  selected: boolean;
  costUsd: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  durationMs: number | null;
}

export interface DerivedRun {
  status: RunStatusSummary;
  task: string;
  repository: string;
  policy: string;
  agent: string;
  model: string;
  selectedCandidate?: string;
  terminalReason?: string;
  candidates: CandidateView[];
  metrics: StageMetric[];
  changedFiles: string[];
  patchEvolution: {
    id: string;
    attemptId?: string;
    digest?: string;
    files: string[];
  }[];
  policyDecisions: Record<string, unknown>[];
  failure?: {
    rootCause: string;
    evidence: string[];
    nextSafeAction: string;
    resumeUrl?: string;
    cancelUrl?: string;
  };
}
