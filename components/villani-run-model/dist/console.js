import { canonicalRunSnapshot } from "./canonical.js";
import { maskSensitive } from "./mask.js";
const object = (value) => value && typeof value === "object" && !Array.isArray(value)
    ? value
    : {};
const text = (value) => typeof value === "string" && value.length > 0 ? value : null;
const number = (value) => typeof value === "number" && Number.isFinite(value) ? value : null;
const eventName = (event) => text(event.name ?? event.title ?? event.type) ?? "unknown_event";
const eventTime = (event) => text(event.occurred_at ?? event.timestamp ?? event.observed_at);
const bodyText = (event, key) => {
    const body = object(event.body);
    const attributes = object(event.attributes);
    return text(body[key] ?? attributes[key] ?? event[key]);
};
const runLink = (runId) => `/console/runs/${encodeURIComponent(runId)}`;
/**
 * Project an authorized connected run into the same replay contract emitted by
 * Flight Recorder's local parsing engine. This function interprets only the
 * stable run model; it never reads files or infers missing telemetry.
 */
export function consoleReplayFromRunDetail(detail, events = [], artifacts = [], synchronizationState = "SYNCHRONIZED") {
    const canonical = canonicalRunSnapshot(detail);
    const base = runLink(detail.id);
    const sortedEvents = [...events].sort((left, right) => (left.sequence ?? Number.MAX_SAFE_INTEGER) -
        (right.sequence ?? Number.MAX_SAFE_INTEGER));
    const replayEvents = sortedEvents.map((event, index) => {
        const id = text(event.event_id ?? event.idempotency_key ?? event.id) ??
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
            duration_ms: number(event.duration_ms ?? object(event.body).duration_ms),
            path: bodyText(event, "path") ?? text(event.path),
            stdout: bodyText(event, "stdout"),
            stderr: bodyText(event, "stderr"),
            deep_link: `${base}/events/${encodeURIComponent(id)}`,
        };
    });
    const attempts = canonical.attempts.map((attempt) => ({
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
    }));
    const filesByKey = new Map();
    for (const attempt of attempts) {
        for (const path of attempt.changed_files) {
            const key = `${attempt.id}\0${path}`;
            filesByKey.set(key, {
                path,
                attempt_id: attempt.id,
                materialized: attempt.selected &&
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
    const logs = [];
    for (const event of replayEvents) {
        for (const stream of ["stdout", "stderr"]) {
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
            accounting_status: canonical.total_cost_usd === null ? "unknown" : "known",
            currency: "USD",
            coding: canonical.coding_cost_usd,
            verification: canonical.verifier_cost_usd,
            total: canonical.total_cost_usd,
        },
        logs,
        canonical,
        warnings: [],
        deep_links: { self: base, history: "/console/history" },
    });
}
