import { consoleReplayFromRunDetail, } from "@villani/run-model";
import { scanToIndex } from "../index/sessionIndex.js";
import { readIndex } from "../index/sessionStore.js";
import { adaptersFor } from "../providers/providerAdapter.js";
import { parseVillaniRun } from "../providers/villani.js";
import { redactDeep } from "../redaction/redact.js";
import { defaultVillaniRunsRoot } from "../scanners/findVillaniRuns.js";
import path from "node:path";
const text = (value) => typeof value === "string" && value.length > 0 ? value : null;
const number = (value) => typeof value === "number" && Number.isFinite(value) ? value : null;
const object = (value) => value && typeof value === "object" && !Array.isArray(value)
    ? value
    : {};
const strings = (value) => Array.isArray(value)
    ? [
        ...new Set(value.filter((item) => typeof item === "string")),
    ]
    : [];
const state = (record) => text(record.state ?? record.outcome) ?? "unknown";
export function consoleHistoryFromIndex(index) {
    const canonicalRoots = index.sessions
        .filter((record) => record.provider === "villani")
        .map((record) => path.resolve(record.sourcePath));
    return index.sessions
        .filter((record) => record.provider === "villani" ||
        !canonicalRoots.some((root) => {
            const source = path.resolve(record.sourcePath);
            return source === root || source.startsWith(`${root}${path.sep}`);
        }))
        .map((record) => {
        const isRun = record.provider === "villani";
        return {
            id: record.id,
            logical_id: record.id,
            kind: isRun ? "run" : "session",
            source: String(record.provider),
            source_label: record.providerLabel,
            provider: String(record.provider),
            repository: text(record.repositoryPath) ??
                text(record.projectDisplayName) ??
                text(record.projectName),
            task: text(record.title ?? record.firstPrompt),
            status: state(record),
            model: text(record.selectedModel ?? record.model),
            started_at: text(record.firstEventAt ?? record.createdAt),
            updated_at: text(record.lastEventAt ?? record.updatedAt),
            duration_ms: number(record.durationMs),
            cost: number(record.costUsd),
            currency: text(record.currency),
            cost_available: number(record.costUsd) !== null,
            synchronization_state: "LOCAL",
            deep_link: isRun
                ? `/console/runs/${encodeURIComponent(record.id)}`
                : `/console/sessions/${encodeURIComponent(record.id)}`,
        };
    })
        .sort((left, right) => String(right.updated_at ?? "").localeCompare(String(left.updated_at ?? "")));
}
function flightEvent(event, runId) {
    return {
        id: event.eventId ?? event.id,
        event_id: event.eventId ?? event.id,
        run_id: event.runId ?? runId,
        sequence: event.sequence,
        occurred_at: event.timestamp,
        timestamp: event.timestamp,
        trace_id: event.traceId,
        attempt_id: event.attemptId,
        parent_event_id: event.parentEventId,
        source: String(event.provider),
        kind: event.type,
        name: event.title,
        status: event.exitCode == null
            ? "recorded"
            : event.exitCode === 0
                ? "ok"
                : "failed",
        command: event.command,
        exit_code: event.exitCode,
        path: event.path,
        duration_ms: event.durationMs,
        body: {
            message: event.summary,
            stdout: event.stdout,
            stderr: event.stderr,
            diff: event.diff,
        },
    };
}
function localRunDetail(session) {
    const data = session.villani;
    const manifest = data?.manifest;
    const runState = data?.state;
    const runId = manifest?.run_id ?? session.sessionId ?? "unknown_run";
    const selected = data?.attempts.find((attempt) => attempt.snapshot.attempt_id === manifest?.selected_attempt_id);
    const candidateOutcomes = Object.fromEntries((data?.attempts ?? []).map((attempt) => {
        const verification = data?.verifications.find((item) => item.attempt_id === attempt.snapshot.attempt_id);
        const metadata = object(verification?.metadata);
        return [
            attempt.snapshot.attempt_id,
            {
                status: attempt.snapshot.status,
                backend_name: attempt.snapshot.backend_name,
                model: attempt.snapshot.model,
                candidate_eligibility: verification?.acceptance_eligible ?? null,
                input_tokens: attempt.snapshot.input_tokens,
                output_tokens: attempt.snapshot.output_tokens,
                total_tokens: attempt.snapshot.input_tokens === null ||
                    attempt.snapshot.output_tokens === null
                    ? null
                    : attempt.snapshot.input_tokens + attempt.snapshot.output_tokens,
                cost_usd: attempt.snapshot.cost_usd,
                duration_ms: attempt.snapshot.duration_ms,
                changed_files: attempt.snapshot.attempt_id ===
                    data?.materialization?.selected_attempt_id
                    ? data.materialization.changed_files
                    : strings(object(attempt.runnerTelemetry).changed_files),
                file_write_count: number(object(attempt.runnerTelemetry).file_write_count),
                failure_category: attempt.snapshot.error?.code ?? null,
                verification: verification
                    ? {
                        outcome: verification.outcome,
                        authority_source: text(metadata.authority_source ??
                            metadata.verification_authority) ?? null,
                        verifier: verification.verifier,
                    }
                    : {},
            },
        ];
    }));
    const verification = data?.verifications.find((item) => item.attempt_id === manifest?.selected_attempt_id);
    const verificationMetadata = object(verification?.metadata);
    const classificationMetadata = object(data?.classification?.metadata);
    const rawClassification = data?.classification
        ? {
            difficulty: data.classification.difficulty,
            risk: data.classification.risk,
            category: data.classification.category,
            required_capabilities: data.classification.required_capabilities,
            confidence: data.classification.confidence,
        }
        : null;
    const first = manifest?.created_at ?? session.startedAt ?? new Date(0).toISOString();
    const last = manifest?.completed_at ??
        manifest?.updated_at ??
        session.endedAt ??
        session.startedAt ??
        first;
    const policy = data?.policyDecisions.at(-1);
    return {
        id: runId,
        trace_id: manifest?.trace_id,
        status: manifest?.final_state ?? runState?.state ?? "unknown",
        first_occurred_at: first,
        last_observed_at: last,
        attempts: (data?.attempts ?? []).map((attempt) => ({
            id: attempt.snapshot.attempt_id,
            status: attempt.snapshot.status,
        })),
        outcomes: [],
        artifact_count: 0,
        task_instruction: data?.task?.instruction ?? null,
        success_criteria: data?.task?.success_criteria ?? null,
        repository: data?.task?.repository_path ?? null,
        agent_name: selected?.snapshot.runner_name ?? null,
        raw_classification: rawClassification,
        effective_classification: classificationMetadata.effective_classification &&
            typeof classificationMetadata.effective_classification === "object" &&
            !Array.isArray(classificationMetadata.effective_classification)
            ? object(classificationMetadata.effective_classification)
            : rawClassification,
        classification_confidence: data?.classification?.confidence ?? null,
        classification_adjustments: Array.isArray(classificationMetadata.adjustments)
            ? classificationMetadata.adjustments
            : [],
        policy_version: policy?.policy_version ?? null,
        policy_decisions: data?.policyDecisions,
        selected_attempt_id: manifest?.selected_attempt_id ?? null,
        selected_backend: selected?.snapshot.backend_name ?? null,
        selected_model: selected?.snapshot.model ?? null,
        attempt_count: data?.attempts.length ?? 0,
        escalation_count: (data?.policyDecisions ?? []).filter((decision) => decision.action === "escalate").length,
        input_tokens: manifest?.total_input_tokens ?? null,
        output_tokens: manifest?.total_output_tokens ?? null,
        total_tokens: manifest?.total_input_tokens === null ||
            manifest?.total_output_tokens === null
            ? null
            : manifest?.total_input_tokens !== undefined &&
                manifest?.total_output_tokens !== undefined
                ? manifest.total_input_tokens + manifest.total_output_tokens
                : null,
        coding_cost_usd: manifest?.stage_metrics?.coding?.cost ??
            selected?.snapshot.cost_usd ??
            null,
        verifier_cost_usd: manifest?.stage_metrics?.verification?.cost ?? null,
        total_cost_usd: manifest?.total_cost_usd ?? null,
        duration_ms: manifest?.total_duration_ms ?? null,
        changed_files: data?.materialization?.changed_files ?? [],
        file_write_count: data?.aggregate?.fileWrites ?? 0,
        terminal_reason: runState?.failure?.message ?? manifest?.final_state ?? null,
        candidate_outcomes: candidateOutcomes,
        selection_reason: data?.selection?.reason ?? null,
        selection_rankings: data?.selection?.rankings,
        verification_status: verification?.outcome ?? null,
        verification_authority: text(verificationMetadata.authority_source ??
            verificationMetadata.verification_authority) ?? null,
        materialization_status: data?.materialization?.status ?? null,
        failure_category: runState?.failure?.code ?? null,
        canonical_projection: {},
    };
}
function cleanRedaction(value) {
    const result = redactDeep(value);
    if (result && typeof result === "object")
        delete result.redactionReport;
    return result;
}
function importedReplay(session, record) {
    const base = `/console/sessions/${encodeURIComponent(record.id)}`;
    const events = session.events.map((event, index) => {
        const id = event.eventId ?? event.id ?? `event_${index}`;
        return {
            id,
            sequence: number(event.sequence),
            timestamp: text(event.timestamp),
            source: String(event.provider),
            kind: event.type,
            title: event.title,
            summary: text(event.summary),
            status: event.exitCode == null
                ? "recorded"
                : event.exitCode === 0
                    ? "ok"
                    : "failed",
            attempt_id: text(event.attemptId),
            command: text(event.command),
            exit_code: number(event.exitCode),
            duration_ms: number(event.durationMs),
            path: text(event.path),
            stdout: text(event.stdout),
            stderr: text(event.stderr),
            deep_link: `${base}/events/${encodeURIComponent(id)}`,
        };
    });
    const logs = [];
    for (const event of events) {
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
    const files = [
        ...new Set(record.changedFiles ?? session.events.map((event) => event.path)),
    ]
        .filter((item) => typeof item === "string" && item.length > 0)
        .map((file) => ({
        path: file,
        attempt_id: null,
        materialized: false,
        deep_link: `${base}/files/${encodeURIComponent(file)}`,
    }));
    return cleanRedaction({
        schema_version: "villani.console.replay.v1",
        id: record.id,
        logical_id: record.id,
        kind: "session",
        source: String(record.provider),
        source_label: record.providerLabel,
        provider: String(record.provider),
        synchronization_state: "LOCAL",
        summary: {
            status: state(record),
            task: text(record.title ?? record.firstPrompt),
            repository: text(record.projectDisplayName) ??
                text(record.projectName) ??
                text(record.repositoryPath),
            model: text(record.model),
            policy: null,
            started_at: text(session.startedAt ?? record.firstEventAt),
            completed_at: text(session.endedAt ?? record.lastEventAt),
            duration_ms: number(record.durationMs),
            total_tokens: number(record.tokenCount),
            total_cost: number(record.costUsd),
            currency: text(record.currency),
            terminal_reason: text(record.failureSummary),
        },
        events,
        attempts: [],
        evidence: { warnings: session.warnings },
        verification: {
            outcome: "not_applicable",
            authority: null,
            verifier: null,
        },
        candidate_comparison: [],
        files,
        artifacts: [],
        cost: {
            accounting_status: record.costAccountingStatus ?? "unknown",
            currency: text(record.currency),
            coding: number(record.costUsd),
            verification: null,
            total: number(record.costUsd),
        },
        logs,
        canonical: null,
        warnings: session.warnings,
        deep_links: { self: base, history: "/console/history" },
    });
}
function localReplay(session) {
    const detail = localRunDetail(session);
    const result = consoleReplayFromRunDetail(detail, session.events.map((event) => flightEvent(event, detail.id)), [], "LOCAL");
    const data = session.villani;
    const extraLogs = [];
    for (const attempt of data?.attempts ?? []) {
        for (const [stream, content] of [
            ["stdout", attempt.stdout],
            ["stderr", attempt.stderr],
        ]) {
            if (content)
                extraLogs.push({
                    id: `${attempt.snapshot.attempt_id}:${stream}`,
                    event_id: attempt.snapshot.attempt_id,
                    stream,
                    content,
                    deep_link: `${result.deep_links.self}/attempts/${encodeURIComponent(attempt.snapshot.attempt_id)}`,
                });
        }
    }
    result.logs.push(...extraLogs);
    result.warnings.push(...session.warnings);
    return cleanRedaction(result);
}
export async function consoleIndex(options = {}) {
    let index = options.refresh ? null : await readIndex(options.indexDir);
    if (!index) {
        const result = await scanToIndex({
            all: true,
            roots: options.roots,
            indexDir: options.indexDir,
            rebuild: options.refresh,
        });
        index = result.index;
    }
    return {
        schema_version: "villani.console.history.v1",
        entries: consoleHistoryFromIndex(index),
        warnings: index.warnings,
    };
}
export async function consoleReplay(options) {
    const index = await readIndex(options.indexDir);
    const record = index?.sessions.find((item) => item.id === options.id);
    if (options.kind === "run") {
        if (record && record.provider !== "villani")
            throw new Error(`indexed record ${options.id} is not a Villani run`);
        const root = options.runsRoot ?? defaultVillaniRunsRoot();
        const session = record
            ? (await adaptersFor("villani")[0].parse({
                provider: "villani",
                sourcePath: record.sourcePath,
                sourceKind: "directory",
                confidence: record.confidence,
                reason: "Villani Console replay",
            }))
            : await parseVillaniRun(path.join(path.resolve(root), options.id));
        return localReplay(session);
    }
    if (!record)
        throw new Error(`indexed session ${options.id} was not found`);
    if (record.provider === "villani")
        throw new Error(`indexed record ${options.id} is a Villani run`);
    const adapter = adaptersFor(String(record.provider))[0];
    if (!adapter)
        throw new Error(`unsupported indexed provider ${record.provider}`);
    const session = (await adapter.parse({
        provider: record.provider,
        sourcePath: record.sourcePath,
        sourceKind: record.sourceKind === "directory" ? "directory" : "file",
        confidence: record.confidence,
        reason: "Villani Console replay",
    }));
    return importedReplay(session, record);
}
