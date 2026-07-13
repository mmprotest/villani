import type {
  AttemptSummary,
  DerivedRun,
  RunDetail,
  RunEvent,
  RunStatusSummary,
  StageMetric,
} from "./types.js";

const obj = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const text = (...values: unknown[]) =>
  values.find((v): v is string => typeof v === "string" && v.length > 0);
const number = (...values: unknown[]) =>
  values.find((v): v is number => typeof v === "number" && Number.isFinite(v));
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const strings = (value: unknown): string[] =>
  list(value).filter((v): v is string => typeof v === "string");
const data = (event: RunEvent) => ({
  ...obj(event.raw),
  ...obj(event.payload),
  ...obj(event.attributes),
  ...obj(event.body),
});
const eventName = (event: RunEvent) =>
  text(event.name, obj(event.raw).event_type, event.type, event.title) ??
  "unknown";
const failed = (event: RunEvent) =>
  /fail|error|cancel/.test(
    `${event.status ?? ""} ${eventName(event)}`.toLowerCase(),
  ) || (event.exit_code ?? 0) !== 0;
const isTest = (event: RunEvent) =>
  /test|pytest|vitest|jest|npm test/.test(
    `${event.kind ?? ""} ${eventName(event)} ${event.command ?? ""}`.toLowerCase(),
  );
const isCommand = (event: RunEvent) =>
  /command|tool|shell|bash/.test(
    `${event.kind ?? ""} ${event.type ?? ""} ${eventName(event)}`.toLowerCase(),
  ) || Boolean(event.command);
const isFile = (event: RunEvent) =>
  /file|patch|diff|materializ/.test(
    `${event.kind ?? ""} ${event.type ?? ""} ${eventName(event)}`.toLowerCase(),
  );

export function deriveRunStatus(
  events: RunEvent[],
  controllerState?: string,
): RunStatusSummary {
  const terminal = controllerState?.toUpperCase();
  const commands = events.filter(isCommand);
  const tests = events.filter(isTest);
  const lifecycleKey = (event: RunEvent) => {
    const value = data(event);
    return text(
      value.tool_use_id,
      value.tool_call_id,
      value.call_id,
      value.command_id,
      event.command,
      event.event_id,
      event.id,
    )!;
  };
  const commandKeys = new Set(commands.map(lifecycleKey));
  const testKeys = new Set(tests.map(lifecycleKey));
  const failedCommandKeys = new Set(commands.filter(failed).map(lifecycleKey));
  const failedTestKeys = new Set(tests.filter(failed).map(lifecycleKey));
  const edits = events.filter(isFile).length;
  const base = {
    failedCommands: failedCommandKeys.size,
    failedTests: failedTestKeys.size,
    totalCommands: commandKeys.size,
    totalTests: testKeys.size,
    fileEdits: edits,
    hasFinalAnswer: events.some((e) =>
      /run_(completed|failed|exhausted)|session_end/.test(eventName(e)),
    ),
  };
  if (terminal === "COMPLETED" || terminal === "SUCCEEDED")
    return {
      ...base,
      status: "succeeded",
      label: "Completed",
      tone: "success",
      reason: `Controller state ${terminal}`,
    };
  if (terminal === "FAILED" || terminal === "CANCELLED")
    return {
      ...base,
      status: "failed",
      label: terminal === "CANCELLED" ? "Cancelled" : "Failed",
      tone: "error",
      reason: `Controller state ${terminal}`,
    };
  if (terminal === "EXHAUSTED")
    return {
      ...base,
      status: "partial",
      label: "Exhausted",
      tone: "warning",
      reason: "Controller state EXHAUSTED",
    };
  if (base.failedCommands || base.failedTests)
    return {
      ...base,
      status: "failed",
      label: "Failed",
      tone: "error",
      reason: base.failedTests
        ? `${base.failedTests} failed test${base.failedTests === 1 ? "" : "s"}`
        : `${base.failedCommands} failed command${base.failedCommands === 1 ? "" : "s"}`,
    };
  const running = events.some((e) =>
    /running|started|queued/.test(
      `${e.status ?? ""} ${eventName(e)}`.toLowerCase(),
    ),
  );
  if (running)
    return {
      ...base,
      status: "partial",
      label: "Running",
      tone: "info",
      reason: "Run is still active",
    };
  if (!events.length)
    return {
      ...base,
      status: "unknown",
      label: "Unknown",
      tone: "muted",
      reason: "No events captured",
    };
  return {
    ...base,
    status: "unknown",
    label: "Unknown",
    tone: "muted",
    reason: "No terminal controller state captured",
  };
}

function attemptsFrom(detail: RunDetail, events: RunEvent[]): AttemptSummary[] {
  const map = new Map(detail.attempts.map((a) => [a.id, a]));
  for (const event of events)
    if (event.attempt_id && !map.has(event.attempt_id))
      map.set(event.attempt_id, {
        id: event.attempt_id,
        status: event.status ?? "unknown",
      });
  return [...map.values()];
}

function metrics(events: RunEvent[], selected?: string): StageMetric[] {
  return events
    .filter((event) => {
      const values = data(event);
      return (
        event.kind === "model" ||
        /model|attempt|verif|policy|materializ/.test(eventName(event)) ||
        number(
          values.cost_usd,
          values.cost,
          values.input_tokens,
          values.output_tokens,
        ) !== undefined
      );
    })
    .map((event, index) => {
      const values = data(event);
      const started = text(event.occurred_at, event.timestamp);
      const ended = text(values.ended_at);
      const duration =
        number(event.duration_ms, values.duration_ms) ??
        (started && ended
          ? Math.max(0, new Date(ended).getTime() - new Date(started).getTime())
          : null);
      return {
        key: event.event_id ?? event.id ?? String(index),
        stage: text(values.stage, event.kind, eventName(event)) ?? "unknown",
        attemptId: event.attempt_id ?? undefined,
        model: text(values.model, values.model_name),
        retry: Boolean(values.retry) || /retry/.test(eventName(event)),
        selected: event.attempt_id === selected,
        costUsd: number(values.cost_usd, values.cost) ?? null,
        inputTokens: number(values.input_tokens) ?? null,
        outputTokens: number(values.output_tokens) ?? null,
        durationMs: duration,
      };
    });
}

export function deriveRun(detail: RunDetail, events: RunEvent[]): DerivedRun {
  const values = events.map(data);
  const selected = text(
    detail.selected_attempt_id,
    ...values.map((v) => v.selected_attempt_id),
    ...values.map((v) => v.selected_candidate),
    ...detail.outcomes.map((v) => v.selected_attempt_id),
  );
  const attempts = attemptsFrom(detail, events);
  const candidates = attempts.map((attempt) => {
    const related = events
      .filter((event) => event.attempt_id === attempt.id)
      .map(data);
    const outcome =
      detail.outcomes.find((value) => value.attempt_id === attempt.id) ?? {};
    const verification =
      related.find((value) => "acceptance_eligible" in value) ?? outcome;
    return {
      attemptId: attempt.id,
      status:
        text(verification.outcome, outcome.status, attempt.status) ?? "unknown",
      eligible:
        verification.acceptance_eligible === true || outcome.accepted === true,
      selected: attempt.id === selected,
      requirementResults: list(verification.requirement_results),
      evidenceGrades: strings(verification.evidence_grades).concat(
        strings(verification.success_evidence).map((item) => item),
      ),
      risks: strings(verification.risk_flags).concat(
        strings(verification.risks),
      ),
      patchDigest: text(verification.patch_digest, outcome.patch_digest),
      explanation: text(
        verification.selection_explanation,
        verification.reason,
        outcome.reason,
      ),
      costUsd: number(outcome.cost, verification.cost_usd) ?? null,
      inputTokens: number(verification.input_tokens),
      outputTokens: number(verification.output_tokens),
    };
  });
  const changed = new Set<string>();
  strings(detail.changed_files).forEach((file) => changed.add(file));
  const evolution: DerivedRun["patchEvolution"] = [];
  for (const event of events) {
    const value = data(event);
    const files = strings(value.changed_files).concat(strings(value.files));
    const one = text(event.path, value.path, value.file);
    if (one) files.push(one);
    files.forEach((file) => changed.add(file));
    if (
      /patch|diff/.test(`${event.kind ?? ""} ${eventName(event)}`.toLowerCase())
    )
      evolution.push({
        id: event.event_id ?? event.id,
        attemptId: event.attempt_id ?? undefined,
        digest: text(value.patch_digest, value.digest),
        files: [...new Set(files)],
      });
  }
  const failureEvent = [...events].reverse().find(failed);
  const failureData = failureEvent ? data(failureEvent) : undefined;
  const terminalReason = text(
    ...[...values].reverse().map((v) => v.terminal_reason),
    ...[...values].reverse().map((v) => v.reason),
  );
  return {
    status: deriveRunStatus(events, detail.status),
    task:
      text(detail.task_instruction, ...values.map((v) => v.task_instruction), ...values.map((v) => v.task), ...values.map((v) => v.instruction)) ??
      "Task not captured",
    repository:
      text(
        detail.repository,
        ...values.map((v) => v.repository),
        ...values.map((v) => v.repository_path),
        detail.repository_id,
      ) ?? "Repository not captured",
    policy:
      text(
        detail.policy_version,
        ...values.map((v) => v.policy_version),
        ...values.map((v) => v.policy),
      ) ?? "Policy not captured",
    agent:
      text(detail.agent_name, ...values.map((v) => v.agent), ...values.map((v) => v.agent_name)) ??
      "Agent not captured",
    model:
      text(detail.selected_model, ...values.map((v) => v.model), ...values.map((v) => v.model_name)) ??
      "Model not captured",
    selectedCandidate: selected,
    terminalReason,
    candidates,
    metrics: metrics(events, selected),
    changedFiles: [...changed].sort(),
    patchEvolution: evolution,
    policyDecisions: (detail.policy_decisions?.length ? detail.policy_decisions : events
      .filter((e) =>
        /policy|retry|escalat|budget|experiment/.test(
          `${e.kind ?? ""} ${eventName(e)}`.toLowerCase(),
        ),
      )
      .map(data)),
    aggregate: {
      inputTokens: number(detail.input_tokens) ?? null,
      outputTokens: number(detail.output_tokens) ?? null,
      totalTokens: number(detail.total_tokens) ?? null,
      codingCostUsd: number(detail.coding_cost_usd) ?? null,
      verifierCostUsd: number(detail.verifier_cost_usd) ?? null,
      totalCostUsd: number(detail.total_cost_usd) ?? null,
      durationMs: number(detail.duration_ms) ?? null,
      fileWriteCount: number(detail.file_write_count) ?? 0,
      attemptCount: number(detail.attempt_count) ?? attempts.length,
      escalationCount: number(detail.escalation_count) ?? 0,
    },
    redaction: detail.redaction_status ?? undefined,
    failure: failureEvent
      ? {
          rootCause:
            text(
              failureData?.root_cause,
              failureData?.classification,
              failureEvent.name,
              failureEvent.title,
            ) ?? "Unclassified failure",
          evidence: strings(failureData?.evidence).concat(
            text(failureData?.message) ? [text(failureData?.message)!] : [],
          ),
          nextSafeAction:
            text(
              failureData?.next_safe_action,
              failureData?.recommended_action,
            ) ?? "Review the evidence before retrying.",
          resumeUrl: text(failureData?.resume_url),
          cancelUrl: text(failureData?.cancel_url),
        }
      : undefined,
  };
}
