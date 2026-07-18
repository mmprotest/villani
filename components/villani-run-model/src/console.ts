import { canonicalRunSnapshot } from "./canonical.js";
import { maskSensitive } from "./mask.js";
import type {
  ArtifactDescriptor,
  CanonicalRunSnapshot,
  RunDetail,
  RunEvent,
} from "./types.js";
import type { EntitlementState, UpdateState } from "./selfService.js";

export type ConsoleMode = "local" | "connected";
export type ConsoleRecordKind = "run" | "session";
export type ConsoleSynchronizationState =
  "LOCAL" | "SYNC PENDING" | "SYNCHRONIZED" | "REDACTED" | "SYNC FAILED";

export interface ConsoleHistoryEntry {
  id: string;
  logical_id: string;
  kind: ConsoleRecordKind;
  source: string;
  source_label: string;
  provider: string;
  repository: string | null;
  task: string | null;
  status: string;
  model: string | null;
  started_at: string | null;
  updated_at: string | null;
  duration_ms: number | null;
  cost: number | null;
  currency: string | null;
  cost_available: boolean;
  synchronization_state: ConsoleSynchronizationState;
  deep_link: string;
}

export interface ConsoleModel {
  id: string;
  backend_name: string | null;
  display_name: string;
  model: string;
  provider: string;
  endpoint: string | null;
  configured: boolean;
  detected: boolean;
  availability: string;
  available: boolean | null;
  tool_support: "supported" | "unsupported" | "unknown";
  context_metadata: Record<string, unknown>;
  configured_roles: string[];
  capability: string;
  capability_status: "UNRATED" | "BOOTSTRAP" | "OBSERVED" | "QUALIFIED" | "DISABLED";
  context_window: number | string | null;
  pricing_status: "known" | "unknown";
  currency: string | null;
  observed_task_count: number;
  observed_success_rate: number | null;
  observed_cost_per_accepted_task: number | null;
  bootstrap_default: boolean;
  manual_override: boolean;
  manual_override_label: string | null;
  last_tested_at: string | null;
  last_test_diagnostic: string | null;
  capability_policy_version: string;
}

export interface ConsoleBootstrap {
  schema_version: "villani.console.bootstrap.v1";
  mode: ConsoleMode;
  data_source: "local-service" | "workspace";
  version: string;
  workspace: {
    connected: boolean;
    id: string | null;
    endpoint: string | null;
  };
  service: {
    status: string;
    started_at: string | null;
    log_path: string | null;
    last_error: string | null;
  };
  setup: {
    configured: boolean;
    valid: boolean;
    schema_version: number | null;
    issues: string[];
  };
  synchronization: {
    pending: number;
    dead_letters: number;
  };
  storage: {
    home: string;
    runs: string;
    spool: string;
    writable: boolean;
  };
  models: ConsoleModel[];
  active_policy: string | null;
  active_policy_preset?: string;
  entitlement: EntitlementState;
  update: UpdateState;
}

export interface ConsoleReplayEvent {
  id: string;
  sequence: number | null;
  timestamp: string | null;
  source: string;
  kind: string;
  title: string;
  summary: string | null;
  status: string;
  attempt_id: string | null;
  command: string | null;
  exit_code: number | null;
  duration_ms: number | null;
  path: string | null;
  stdout: string | null;
  stderr: string | null;
  deep_link: string;
}

export interface ConsoleReplayAttempt {
  id: string;
  status: string | null;
  backend: string | null;
  model: string | null;
  eligible: boolean | null;
  selected: boolean;
  verification_outcome: string | null;
  verification_authority: string | null;
  verifier: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cost: number | null;
  currency: string | null;
  duration_ms: number | null;
  changed_files: string[];
  failure_category: string | null;
  deep_link: string;
}

export interface ConsoleReplayFile {
  path: string;
  attempt_id: string | null;
  materialized: boolean;
  deep_link: string;
}

export interface ConsoleReplayLog {
  id: string;
  event_id: string;
  stream: "stdout" | "stderr" | "message";
  content: string;
  deep_link: string;
}

export interface ConsoleReplaySnapshot {
  schema_version: "villani.console.replay.v1";
  id: string;
  logical_id: string;
  kind: ConsoleRecordKind;
  source: string;
  source_label: string;
  provider: string;
  synchronization_state: ConsoleSynchronizationState;
  summary: {
    status: string;
    task: string | null;
    repository: string | null;
    model: string | null;
    policy: string | null;
    started_at: string | null;
    completed_at: string | null;
    duration_ms: number | null;
    total_tokens: number | null;
    total_cost: number | null;
    currency: string | null;
    terminal_reason: string | null;
  };
  events: ConsoleReplayEvent[];
  attempts: ConsoleReplayAttempt[];
  evidence: Record<string, unknown>;
  verification: Record<string, unknown>;
  candidate_comparison: ConsoleReplayAttempt[];
  files: ConsoleReplayFile[];
  artifacts: ArtifactDescriptor[];
  cost: {
    accounting_status: string;
    currency: string | null;
    coding: number | null;
    verification: number | null;
    total: number | null;
  };
  logs: ConsoleReplayLog[];
  canonical: CanonicalRunSnapshot | null;
  warnings: string[];
  deep_links: {
    self: string;
    history: string;
  };
}

const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

const text = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 ? value : null;

const number = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

const eventName = (event: RunEvent): string =>
  text(event.name ?? event.title ?? event.type) ?? "unknown_event";

const eventTime = (event: RunEvent): string | null =>
  text(event.occurred_at ?? event.timestamp ?? event.observed_at);

const bodyText = (event: RunEvent, key: string): string | null => {
  const body = object(event.body);
  const attributes = object(event.attributes);
  return text(body[key] ?? attributes[key] ?? event[key as keyof RunEvent]);
};

const runLink = (runId: string): string =>
  `/console/runs/${encodeURIComponent(runId)}`;

/**
 * Project an authorized connected run into the same replay contract emitted by
 * Flight Recorder's local parsing engine. This function interprets only the
 * stable run model; it never reads files or infers missing telemetry.
 */
export function consoleReplayFromRunDetail(
  detail: RunDetail,
  events: RunEvent[] = [],
  artifacts: ArtifactDescriptor[] = [],
  synchronizationState: ConsoleSynchronizationState = "SYNCHRONIZED",
): ConsoleReplaySnapshot {
  const canonical = canonicalRunSnapshot(detail);
  const base = runLink(detail.id);
  const sortedEvents = [...events].sort(
    (left, right) =>
      (left.sequence ?? Number.MAX_SAFE_INTEGER) -
      (right.sequence ?? Number.MAX_SAFE_INTEGER),
  );
  const replayEvents: ConsoleReplayEvent[] = sortedEvents.map(
    (event, index) => {
      const id =
        text(event.event_id ?? event.idempotency_key ?? event.id) ??
        `event_${index}`;
      return {
        id,
        sequence: number(event.sequence),
        timestamp: eventTime(event),
        source: text(event.source) ?? "unknown",
        kind: text(event.kind ?? event.type) ?? "unknown",
        title: eventName(event),
        summary: bodyText(event, "message") ?? text(event.status),
        status: text(event.status) ?? "recorded",
        attempt_id: text(event.attempt_id),
        command: bodyText(event, "command") ?? text(event.command),
        exit_code: number(event.exit_code ?? object(event.body).exit_code),
        duration_ms: number(
          event.duration_ms ?? object(event.body).duration_ms,
        ),
        path: bodyText(event, "path") ?? text(event.path),
        stdout: bodyText(event, "stdout"),
        stderr: bodyText(event, "stderr"),
        deep_link: `${base}/events/${encodeURIComponent(id)}`,
      };
    },
  );
  const attempts: ConsoleReplayAttempt[] = canonical.attempts.map(
    (attempt) => ({
      id: attempt.attempt_id,
      status: attempt.status,
      backend: attempt.backend,
      model: attempt.model,
      eligible: attempt.eligible,
      selected: attempt.selected,
      verification_outcome: attempt.verification_outcome,
      verification_authority: attempt.verification_authority,
      verifier: attempt.verifier_identity,
      input_tokens: attempt.input_tokens,
      output_tokens: attempt.output_tokens,
      total_tokens: attempt.total_tokens,
      cost: attempt.cost_usd,
      currency: "USD",
      duration_ms: attempt.duration_ms,
      changed_files: attempt.changed_files,
      failure_category: attempt.failure_category,
      deep_link: `${base}/attempts/${encodeURIComponent(attempt.attempt_id)}`,
    }),
  );
  const filesByKey = new Map<string, ConsoleReplayFile>();
  for (const attempt of attempts) {
    for (const path of attempt.changed_files) {
      const key = `${attempt.id}\0${path}`;
      filesByKey.set(key, {
        path,
        attempt_id: attempt.id,
        materialized:
          attempt.selected &&
          canonical.selected_materialized_files.includes(path),
        deep_link: `${base}/files/${encodeURIComponent(path)}`,
      });
    }
  }
  for (const path of canonical.selected_materialized_files) {
    const key = `${canonical.selected_attempt_id ?? "run"}\0${path}`;
    if (!filesByKey.has(key))
      filesByKey.set(key, {
        path,
        attempt_id: canonical.selected_attempt_id,
        materialized: true,
        deep_link: `${base}/files/${encodeURIComponent(path)}`,
      });
  }
  const logs: ConsoleReplayLog[] = [];
  for (const event of replayEvents) {
    for (const stream of ["stdout", "stderr"] as const) {
      const content = event[stream];
      if (content)
        logs.push({
          id: `${event.id}:${stream}`,
          event_id: event.id,
          stream,
          content,
          deep_link: event.deep_link,
        });
    }
  }
  return maskSensitive({
    schema_version: "villani.console.replay.v1",
    id: detail.id,
    logical_id: detail.id,
    kind: "run",
    source: "villani",
    source_label: "Villani",
    provider: "villani",
    synchronization_state: synchronizationState,
    summary: {
      status: canonical.status ?? "unknown",
      task: canonical.task,
      repository: canonical.repository,
      model: canonical.selected_model,
      policy: canonical.policy_version,
      started_at: text(detail.first_occurred_at),
      completed_at: text(detail.last_observed_at),
      duration_ms: canonical.duration_ms,
      total_tokens: canonical.total_tokens,
      total_cost: canonical.total_cost_usd,
      currency: "USD",
      terminal_reason: canonical.terminal_reason,
    },
    events: replayEvents,
    attempts,
    evidence: {
      verification_outcome: canonical.verification_outcome,
      verification_authority: canonical.verification_authority,
      verifier: canonical.verifier_identity,
      selection_reason: canonical.selection_reason,
      materialization_status: canonical.materialization_status,
    },
    verification: {
      outcome: canonical.verification_outcome,
      authority: canonical.verification_authority,
      verifier: canonical.verifier_identity,
      failure_category: canonical.failure_category,
    },
    candidate_comparison: attempts,
    files: [...filesByKey.values()],
    artifacts,
    cost: {
      accounting_status:
        canonical.total_cost_usd === null ? "unknown" : "known",
      currency: "USD",
      coding: canonical.coding_cost_usd,
      verification: canonical.verifier_cost_usd,
      total: canonical.total_cost_usd,
    },
    logs,
    canonical,
    warnings: [],
    deep_links: { self: base, history: "/console/history" },
  }) as ConsoleReplaySnapshot;
}
