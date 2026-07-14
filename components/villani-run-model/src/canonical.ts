import type {
  CanonicalAttemptSnapshot,
  CanonicalRunSnapshot,
  RunDetail,
} from "./types.js";

const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

const text = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 ? value : null;

const finiteNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

const boolean = (value: unknown): boolean | null =>
  typeof value === "boolean" ? value : null;

const records = (value: unknown): Record<string, unknown>[] =>
  Array.isArray(value) ? value.map(object) : [];

const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? [...new Set(value.filter((item): item is string => typeof item === "string"))].sort()
    : [];

const normalizedStatus = (value: unknown): string | null => {
  const result = text(value);
  return result ? result.toLowerCase() : null;
};

function attemptSnapshot(
  attemptId: string,
  candidate: Record<string, unknown>,
  attemptStatus: unknown,
  selectedAttemptId: string | null,
): CanonicalAttemptSnapshot {
  const verification = object(candidate.verification);
  return {
    attempt_id: attemptId,
    status: normalizedStatus(candidate.status ?? attemptStatus),
    backend: text(candidate.backend_name),
    model: text(candidate.model),
    eligible: boolean(candidate.candidate_eligibility),
    selected: attemptId === selectedAttemptId,
    verification_outcome: normalizedStatus(verification.outcome),
    verification_authority: text(
      verification.authority_source ?? object(verification.metadata).authority_source,
    ),
    verifier_identity: text(verification.verifier),
    input_tokens: finiteNumber(candidate.input_tokens),
    output_tokens: finiteNumber(candidate.output_tokens),
    total_tokens: finiteNumber(candidate.total_tokens),
    cost_usd: finiteNumber(candidate.cost_usd),
    duration_ms: finiteNumber(candidate.duration_ms),
    changed_files: strings(candidate.changed_files),
    file_write_count: finiteNumber(candidate.file_write_count),
    failure_category: text(candidate.failure_category),
  };
}

/**
 * Produce the lossless, presentation-neutral run truth consumed by both browser
 * surfaces. Missing values remain null; the UI must never invent telemetry.
 */
export function canonicalRunSnapshot(detail: RunDetail): CanonicalRunSnapshot {
  const projection = object(detail.canonical_projection);
  const selectedAttemptId = text(
    detail.selected_attempt_id ?? projection.selected_attempt_id,
  );
  const candidateOutcomes = object(
    detail.candidate_outcomes ?? projection.candidate_outcomes,
  );
  const statuses = new Map(detail.attempts.map((attempt) => [attempt.id, attempt.status]));
  const attemptIds = [...new Set([...statuses.keys(), ...Object.keys(candidateOutcomes)])].sort();
  const attempts = attemptIds.map((attemptId) =>
    attemptSnapshot(
      attemptId,
      object(candidateOutcomes[attemptId]),
      statuses.get(attemptId),
      selectedAttemptId,
    ),
  );
  const selected = attempts.find((attempt) => attempt.selected);
  const candidateEligibility = Object.fromEntries(
    attempts.map((attempt) => [attempt.attempt_id, attempt.eligible]),
  );
  const attemptChangedFiles = Object.fromEntries(
    attempts.map((attempt) => [attempt.attempt_id, attempt.changed_files]),
  );

  return {
    run_id: detail.id,
    trace_id: text(detail.trace_id),
    status: normalizedStatus(detail.status ?? projection.status),
    task: text(detail.task_instruction ?? projection.task_instruction),
    success_criteria: text(detail.success_criteria ?? projection.success_criteria),
    repository: text(detail.repository ?? projection.repository),
    agent_name: text(detail.agent_name ?? projection.agent_name),
    agent_version: text(detail.agent_version ?? projection.agent_version),
    raw_classification:
      detail.raw_classification ??
      (projection.raw_classification ? object(projection.raw_classification) : null),
    effective_classification:
      detail.effective_classification ??
      (projection.effective_classification
        ? object(projection.effective_classification)
        : null),
    classification_confidence: finiteNumber(
      detail.classification_confidence ?? projection.classification_confidence,
    ),
    classification_adjustments: records(
      detail.classification_adjustments ?? projection.classification_adjustments,
    ),
    policy_version: text(detail.policy_version ?? projection.policy_version),
    selected_backend: text(detail.selected_backend ?? projection.selected_backend),
    selected_model: text(detail.selected_model ?? projection.selected_model),
    selected_attempt_id: selectedAttemptId,
    attempts,
    escalation_count: finiteNumber(
      detail.escalation_count ?? projection.escalation_count,
    ),
    input_tokens: finiteNumber(detail.input_tokens ?? projection.input_tokens),
    output_tokens: finiteNumber(detail.output_tokens ?? projection.output_tokens),
    total_tokens: finiteNumber(detail.total_tokens ?? projection.total_tokens),
    coding_cost_usd: finiteNumber(
      detail.coding_cost_usd ?? projection.coding_cost_usd,
    ),
    verifier_cost_usd: finiteNumber(
      detail.verifier_cost_usd ?? projection.verifier_cost_usd,
    ),
    total_cost_usd: finiteNumber(detail.total_cost_usd ?? projection.total_cost_usd),
    duration_ms: finiteNumber(detail.duration_ms ?? projection.duration_ms),
    verification_outcome: normalizedStatus(
      detail.verification_status ?? projection.verification_status,
    ),
    verification_authority: text(
      detail.verification_authority ?? projection.verification_authority,
    ),
    verifier_identity: selected?.verifier_identity ?? null,
    candidate_eligibility: candidateEligibility,
    candidate_rankings: records(
      detail.selection_rankings ?? projection.selection_rankings,
    ),
    selection_reason: text(detail.selection_reason ?? projection.selection_reason),
    file_write_count: finiteNumber(
      detail.file_write_count ?? projection.file_write_count,
    ),
    attempt_changed_files: attemptChangedFiles,
    selected_materialized_files: strings(
      detail.changed_files ?? projection.changed_files,
    ),
    materialization_status: normalizedStatus(
      detail.materialization_status ?? projection.materialization_status,
    ),
    failure_category: text(detail.failure_category ?? projection.failure_category),
    terminal_reason: text(detail.terminal_reason ?? projection.terminal_reason),
    redaction_status:
      detail.redaction_status ??
      (projection.redaction_status ? object(projection.redaction_status) : null),
    redacted_field_count: finiteNumber(
      detail.redacted_field_count ?? projection.redacted_field_count,
    ),
    withheld_artifact_count: finiteNumber(
      detail.withheld_artifact_count ?? projection.withheld_artifact_count,
    ),
    withheld_artifact_categories:
      detail.withheld_artifact_categories !== undefined ||
      Array.isArray(projection.withheld_artifact_categories)
        ? strings(
            detail.withheld_artifact_categories ??
              projection.withheld_artifact_categories,
          )
        : null,
  };
}

/**
 * Named consumer adapters keep the two application boundaries explicit while
 * guaranteeing that both surfaces use the same presentation-neutral model.
 */
export function deriveVillaniWebRunModel(
  detail: RunDetail,
): CanonicalRunSnapshot {
  return canonicalRunSnapshot(detail);
}

export function deriveFlightRecorderRunModel(
  detail: RunDetail,
): CanonicalRunSnapshot {
  return canonicalRunSnapshot(detail);
}
