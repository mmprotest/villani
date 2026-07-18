import fs from "node:fs/promises";
import path from "node:path";
import { isTestCommand } from "../normalize/events.js";
import { defaultVillaniSchemaValidator, } from "./villaniSchemaValidation.js";
function record(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value)
        ? value
        : undefined;
}
function number(value) {
    return typeof value === "number" && Number.isFinite(value)
        ? value
        : undefined;
}
function validationMessage(file, errors) {
    const detail = errors
        .map((error) => `${error.instancePath || "/"}: ${error.message}`)
        .join("; ");
    return `${path.basename(file)} is not a valid canonical snapshot: ${detail}`;
}
async function readJson(file) {
    try {
        return JSON.parse(await fs.readFile(file, "utf8"));
    }
    catch (error) {
        throw new Error(`${path.basename(file)} could not be read: ${error instanceof Error ? error.message : String(error)}`);
    }
}
async function readSnapshot(file, validator) {
    const value = await readJson(file);
    const result = validator.validate(value);
    if (!result.valid)
        throw new Error(validationMessage(file, result.errors));
    return result.value;
}
async function exists(file) {
    return fs
        .stat(file)
        .then((stat) => stat.isFile())
        .catch(() => false);
}
function isStructurallyTruncatedJson(line) {
    const text = line.trim();
    if (!text)
        return false;
    const stack = [];
    let inString = false;
    let escaped = false;
    let invalidNesting = false;
    for (const character of text) {
        if (inString) {
            if (escaped)
                escaped = false;
            else if (character === "\\")
                escaped = true;
            else if (character === '"')
                inString = false;
            continue;
        }
        if (character === '"') {
            inString = true;
        }
        else if (character === "{" || character === "[") {
            stack.push(character === "{" ? "}" : "]");
        }
        else if (character === "}" || character === "]") {
            if (stack.pop() !== character)
                invalidNesting = true;
        }
    }
    return !invalidNesting && (inString || stack.length > 0);
}
export async function readVillaniJsonl(file) {
    const text = await fs.readFile(file, "utf8");
    const lines = text.split(/\r?\n/);
    const finalLineIsTruncated = !text.endsWith("\n") && !text.endsWith("\r");
    let lastNonEmpty = -1;
    for (let index = lines.length - 1; index >= 0; index--) {
        if (lines[index].trim().length > 0) {
            lastNonEmpty = index;
            break;
        }
    }
    const values = [];
    const warnings = [];
    for (let index = 0; index < lines.length; index++) {
        const line = lines[index];
        if (!line.trim())
            continue;
        try {
            values.push(JSON.parse(line));
        }
        catch (error) {
            // A missing terminal newline is normal. Tolerate only a physically
            // incomplete JSON structure; `{"x":}` and other complete malformed
            // objects must remain visible corruption, not a recoverable crash tail.
            const structurallyOpen = isStructurallyTruncatedJson(line);
            if (index === lastNonEmpty && finalLineIsTruncated && structurallyOpen) {
                warnings.push(`${path.basename(file)} ignored a truncated final JSONL line ${index + 1}`);
                continue;
            }
            throw new Error(`${path.basename(file)} contains malformed JSONL at line ${index + 1}: ${error instanceof Error ? error.message : String(error)}`);
        }
    }
    return { values, warnings };
}
function safeArtifactPath(runDirectory, relative) {
    const root = path.resolve(runDirectory);
    const candidate = path.resolve(root, relative);
    if (candidate !== root && !candidate.startsWith(`${root}${path.sep}`)) {
        throw new Error(`artifact path escapes the canonical run directory: ${relative}`);
    }
    return candidate;
}
async function readArtifactText(runDirectory, relative) {
    if (!relative)
        return undefined;
    const file = safeArtifactPath(runDirectory, relative);
    return fs.readFile(file, "utf8").catch(() => undefined);
}
async function readArtifactJson(runDirectory, relative) {
    const text = await readArtifactText(runDirectory, relative);
    if (text === undefined)
        return undefined;
    try {
        return record(JSON.parse(text));
    }
    catch {
        return undefined;
    }
}
async function readTrace(runDirectory, relative) {
    if (!relative)
        return [];
    const file = safeArtifactPath(runDirectory, relative);
    if (!(await exists(file)))
        return [];
    if (file.toLowerCase().endsWith(".jsonl")) {
        return (await readVillaniJsonl(file)).values;
    }
    const parsed = await readJson(file).catch(() => undefined);
    if (parsed === undefined)
        return [];
    if (Array.isArray(parsed))
        return parsed;
    const asRecord = record(parsed);
    if (Array.isArray(asRecord?.events))
        return asRecord.events;
    return [parsed];
}
function considerationForAttempt(attempt, decisions) {
    const decision = decisions.find((item) => item.attempt_id === attempt.attempt_id);
    return decision?.considered_backends.find((item) => item.backend_name === attempt.backend_name);
}
async function loadAttempt(runDirectory, attemptId, decisions, validator) {
    const snapshot = await readSnapshot(path.join(runDirectory, "attempts", attemptId, "attempt.json"), validator);
    const runnerTelemetry = await readArtifactJson(runDirectory, snapshot.runner_telemetry_path);
    const backend = record(runnerTelemetry?.backend);
    const consideration = considerationForAttempt(snapshot, decisions);
    const metadata = record(snapshot.metadata);
    const costComponents = record(metadata?.cost_breakdown) ?? record(runnerTelemetry?.cost_breakdown);
    const harnessResult = snapshot.harness_result_path
        ? await optionalSnapshot(runDirectory, snapshot.harness_result_path, validator)
        : undefined;
    return {
        snapshot,
        provider: typeof backend?.provider === "string" ? backend.provider : undefined,
        capabilityScore: consideration?.capability_score ?? undefined,
        stdout: await readArtifactText(runDirectory, snapshot.stdout_path),
        stderr: await readArtifactText(runDirectory, snapshot.stderr_path),
        patch: await readArtifactText(runDirectory, snapshot.patch_path),
        runnerTelemetry,
        traceEvents: await readTrace(runDirectory, snapshot.trace_path),
        costComponents,
        harnessResult,
        artifactPaths: {
            attempt: `attempts/${attemptId}/attempt.json`,
            stdout: snapshot.stdout_path,
            stderr: snapshot.stderr_path,
            patch: snapshot.patch_path,
            telemetry: snapshot.runner_telemetry_path,
            trace: snapshot.trace_path,
            harness_result: snapshot.harness_result_path ?? null,
            agent_system_identity: snapshot.agent_system_identity_path ?? null,
        },
    };
}
function capturedCounter(attempts, names) {
    let found = false;
    let total = 0;
    for (const attempt of attempts) {
        const telemetry = attempt.runnerTelemetry;
        const value = names
            .map((name) => number(telemetry?.[name]))
            .find((candidate) => candidate !== undefined);
        if (value !== undefined) {
            found = true;
            total += value;
        }
    }
    return found ? total : undefined;
}
function capturedArrayCount(attempts, names) {
    let found = false;
    let total = 0;
    for (const attempt of attempts) {
        const value = names
            .map((name) => attempt.runnerTelemetry?.[name])
            .find(Array.isArray);
        if (Array.isArray(value)) {
            found = true;
            total += value.length;
        }
    }
    return found ? total : undefined;
}
function lifecycleCount(events, prefix) {
    const terminal = events.filter((event) => event.event_type === `${prefix}_completed` ||
        event.event_type === `${prefix}_failed`).length;
    const started = events.filter((event) => event.event_type === `${prefix}_started`).length;
    return terminal || started || undefined;
}
function positiveEventCount(events, eventType) {
    const count = events.filter((event) => event.event_type === eventType).length;
    return count || undefined;
}
function aggregate(manifest, attempts, events) {
    return {
        costUsd: manifest.total_cost_usd,
        currency: manifest.currency ?? "USD",
        stageMetrics: manifest.stage_metrics,
        totalModelCalls: manifest.total_model_calls,
        runWallClockDurationMs: manifest.run_wall_clock_duration_ms,
        costAccountingStatus: manifest.cost_accounting_status,
        inputTokens: manifest.total_input_tokens,
        outputTokens: manifest.total_output_tokens,
        tokenAccountingStatus: manifest.token_accounting_status,
        durationMs: manifest.total_duration_ms,
        durationAccountingStatus: manifest.duration_accounting_status,
        modelCalls: capturedCounter(attempts, ["model_calls", "model_requests"]) ??
            lifecycleCount(events, "model_call") ??
            null,
        toolCalls: capturedCounter(attempts, ["total_tool_calls", "tool_calls"]) ??
            lifecycleCount(events, "tool_call") ??
            null,
        commands: capturedCounter(attempts, ["commands", "commands_executed"]) ??
            lifecycleCount(events, "command") ??
            null,
        fileReads: capturedCounter(attempts, ["total_file_reads", "file_reads"]) ??
            capturedArrayCount(attempts, ["files_read"]) ??
            positiveEventCount(events, "file_read") ??
            null,
        fileWrites: capturedCounter(attempts, ["total_file_writes", "file_writes"]) ??
            capturedArrayCount(attempts, ["files_written"]) ??
            positiveEventCount(events, "file_write") ??
            null,
    };
}
function eventType(event) {
    const type = event.event_type;
    if (type === "run_created")
        return "session_start";
    if (type === "run_completed" || type === "run_exhausted")
        return "session_end";
    if (type === "run_failed" || type.endsWith("_failed"))
        return "error";
    if (type === "patch_captured")
        return "diff";
    if (type === "file_read")
        return "file_read";
    if (type === "file_write")
        return "file_write";
    if (type.startsWith("command_")) {
        const command = event.payload.command;
        return typeof command === "string" && isTestCommand(command)
            ? "test_run"
            : "bash_command";
    }
    if (type.startsWith("tool_call_"))
        return type.endsWith("_started") ? "tool_call" : "tool_result";
    if (type.startsWith("model_call_"))
        return "assistant_message";
    if (type === "policy_selected" ||
        type === "retry_selected" ||
        type === "escalation_selected" ||
        type === "candidate_selected")
        return "approval";
    if (type.endsWith("_started") ||
        type.endsWith("_completed") ||
        type === "attempt_completed")
        return type.endsWith("_started") ? "tool_call" : "tool_result";
    return "unknown";
}
const TITLES = {
    run_created: "Run created",
    classification_started: "Classification started",
    classification_completed: "Classification completed",
    policy_selected: "Policy decision recorded",
    attempt_started: "Coding attempt started",
    attempt_completed: "Coding attempt completed",
    attempt_failed: "Coding attempt failed",
    patch_captured: "Patch captured",
    verification_started: "Verification started",
    verification_completed: "Verification completed",
    verification_failed: "Verification failed",
    retry_selected: "Retry selected",
    escalation_selected: "Escalation selected",
    candidate_selected: "Candidate selected",
    materialization_started: "Materialization started",
    materialization_completed: "Materialization completed",
    materialization_failed: "Materialization failed",
    run_completed: "Run completed",
    run_exhausted: "Run exhausted",
    run_failed: "Run failed",
    model_call_started: "Model call started",
    model_call_completed: "Model call completed",
    model_call_failed: "Model call failed",
    tool_call_started: "Tool call started",
    tool_call_completed: "Tool call completed",
    tool_call_failed: "Tool call failed",
    command_started: "Command started",
    command_completed: "Command completed",
    file_read: "File read",
    file_write: "File written",
};
function eventSummary(event, data) {
    if (event.event_type === "classification_completed" && data.classification) {
        return `${data.classification.difficulty} difficulty, ${data.classification.risk} risk, confidence ${data.classification.confidence}`;
    }
    if (event.event_type === "policy_selected") {
        const id = event.payload.decision_id;
        const decision = data.policyDecisions.find((item) => item.decision_id === id);
        return decision
            ? `${decision.action}: ${decision.reason}`
            : "Policy decision persisted";
    }
    if (event.event_type.startsWith("attempt_") && event.attempt_id) {
        const attempt = data.attempts.find((item) => item.snapshot.attempt_id === event.attempt_id);
        if (attempt)
            return `${attempt.snapshot.backend_name} / ${attempt.snapshot.model ?? "model not captured"}`;
    }
    if (event.event_type.startsWith("verification_") && event.attempt_id) {
        const verification = data.verifications.find((item) => item.attempt_id === event.attempt_id);
        if (verification)
            return `${verification.outcome}: ${verification.reason}`;
    }
    if (event.event_type === "candidate_selected")
        return data.selection?.reason;
    if (event.event_type.startsWith("materialization_"))
        return data.materialization
            ? `${data.materialization.status}: ${data.materialization.selected_attempt_id}`
            : undefined;
    if (event.event_type === "run_completed")
        return "Terminal state COMPLETED";
    if (event.event_type === "run_exhausted")
        return "Terminal state EXHAUSTED";
    if (event.event_type === "run_failed")
        return "Terminal state FAILED";
    return Object.keys(event.payload).length
        ? JSON.stringify(event.payload)
        : undefined;
}
function normalizeEvent(event, data) {
    const attempt = event.attempt_id
        ? data.attempts.find((item) => item.snapshot.attempt_id === event.attempt_id)
        : undefined;
    const command = typeof event.payload.command === "string"
        ? event.payload.command
        : undefined;
    const eventPath = [event.payload.path, event.payload.patch_path].find((value) => typeof value === "string");
    return {
        id: event.event_id,
        eventId: event.event_id,
        provider: "villani",
        sessionId: event.run_id,
        runId: event.run_id,
        traceId: event.trace_id,
        attemptId: event.attempt_id,
        parentEventId: event.parent_event_id,
        sequence: event.sequence,
        timestamp: event.timestamp,
        cwd: data.task?.repository_path,
        type: eventType(event),
        title: TITLES[event.event_type] ?? `Unknown Villani event: ${event.event_type}`,
        summary: eventSummary(event, data),
        path: eventPath,
        command,
        exitCode: number(event.payload.exit_code),
        durationMs: event.event_type === "attempt_completed"
            ? (attempt?.snapshot.duration_ms ?? undefined)
            : undefined,
        stdout: attempt?.stdout,
        stderr: attempt?.stderr,
        diff: event.event_type === "patch_captured" ? attempt?.patch : undefined,
        raw: event,
    };
}
async function optionalSnapshot(runDirectory, relative, validator) {
    const file = safeArtifactPath(runDirectory, relative);
    return (await exists(file)) ? readSnapshot(file, validator) : undefined;
}
export async function parseVillaniRun(runPath, validator = defaultVillaniSchemaValidator()) {
    const runDirectory = path.resolve(runPath);
    const manifest = await readSnapshot(path.join(runDirectory, "manifest.json"), validator);
    const state = await readSnapshot(path.join(runDirectory, "state.json"), validator);
    const task = await readSnapshot(path.join(runDirectory, manifest.artifact_paths.task), validator);
    if (manifest.run_id !== state.run_id || manifest.run_id !== task.run_id) {
        throw new Error(`canonical run identity mismatch for ${path.basename(runDirectory)}`);
    }
    const warnings = [];
    const eventFile = path.join(runDirectory, manifest.artifact_paths.events);
    const eventJsonl = await readVillaniJsonl(eventFile);
    warnings.push(...eventJsonl.warnings);
    const eventResult = validator.validateEventStream(eventJsonl.values);
    if (!eventResult.valid)
        throw new Error(validationMessage(eventFile, eventResult.errors));
    const policyDecisions = [];
    const policyFile = path.join(runDirectory, manifest.artifact_paths.policy_decisions);
    if (await exists(policyFile)) {
        const policyJsonl = await readVillaniJsonl(policyFile);
        warnings.push(...policyJsonl.warnings);
        for (const value of policyJsonl.values) {
            const result = validator.validate(value);
            if (!result.valid)
                throw new Error(validationMessage(policyFile, result.errors));
            if (result.value.schema_version !== "villani.policy_decision.v1")
                throw new Error(`${path.basename(policyFile)} contains a non-policy document`);
            policyDecisions.push(result.value);
        }
    }
    policyDecisions.sort((left, right) => left.decision_sequence - right.decision_sequence);
    const classification = await optionalSnapshot(runDirectory, manifest.artifact_paths.classification, validator);
    const attempts = [];
    for (const attemptId of manifest.attempt_ids) {
        attempts.push(await loadAttempt(runDirectory, attemptId, policyDecisions, validator));
    }
    attempts.sort((left, right) => left.snapshot.ordinal - right.snapshot.ordinal);
    for (const attempt of attempts) {
        attempt.canonicalEvents = eventResult.value.filter((event) => event.attempt_id === attempt.snapshot.attempt_id);
    }
    const verifications = [];
    for (const attempt of attempts) {
        const relative = `verification/${attempt.snapshot.attempt_id}.json`;
        const verification = await optionalSnapshot(runDirectory, relative, validator);
        if (verification)
            verifications.push(verification);
    }
    const selection = await optionalSnapshot(runDirectory, manifest.artifact_paths.selection, validator);
    const materialization = await optionalSnapshot(runDirectory, manifest.artifact_paths.materialization, validator);
    const evidenceFile = path.join(runDirectory, "candidate_evidence_matrix.json");
    const candidateEvidenceMatrix = (await exists(evidenceFile))
        ? await readJson(evidenceFile)
        : undefined;
    const runSummary = await optionalSnapshot(runDirectory, "run-summary.json", validator);
    const agentSystems = [];
    for (const systemId of manifest.agent_system_ids ?? []) {
        const identity = await optionalSnapshot(runDirectory, `agent-systems/${systemId}.json`, validator);
        if (identity)
            agentSystems.push(identity);
    }
    const data = {
        runDirectory,
        manifest,
        state,
        task,
        classification,
        policyDecisions,
        attempts,
        verifications,
        candidateEvidenceMatrix,
        selection,
        materialization,
        runSummary,
        agentSystems,
        aggregate: aggregate(manifest, attempts, eventResult.value),
        artifactPaths: {
            manifest: "manifest.json",
            state: manifest.artifact_paths.state,
            task: manifest.artifact_paths.task,
            classification: manifest.artifact_paths.classification,
            events: manifest.artifact_paths.events,
            policy_decisions: manifest.artifact_paths.policy_decisions,
            selection: manifest.artifact_paths.selection,
            materialization: manifest.artifact_paths.materialization,
            evidence_matrix: "candidate_evidence_matrix.json",
            ...(manifest.artifact_paths.validation_coverage
                ? {
                    validation_coverage: manifest.artifact_paths.validation_coverage,
                }
                : {}),
            run_summary: manifest.artifact_paths.run_summary ?? "run-summary.json",
            ...(manifest.artifact_paths.agent_systems
                ? { agent_systems: manifest.artifact_paths.agent_systems }
                : {}),
        },
    };
    const events = eventResult.value
        .slice()
        .sort((left, right) => left.sequence - right.sequence)
        .map((event) => normalizeEvent(event, data));
    const selected = attempts.find((attempt) => attempt.snapshot.attempt_id === manifest.selected_attempt_id);
    const input = manifest.total_input_tokens;
    const output = manifest.total_output_tokens;
    return {
        provider: "villani",
        sessionPath: runDirectory,
        path: runDirectory,
        sessionId: manifest.run_id,
        cwd: task.repository_path,
        model: selected?.snapshot.model ?? undefined,
        startedAt: manifest.created_at,
        endedAt: manifest.completed_at ?? undefined,
        events,
        warnings,
        tokenUsage: input !== null || output !== null
            ? {
                inputTokens: input ?? undefined,
                outputTokens: output ?? undefined,
                totalTokens: input !== null && output !== null ? input + output : undefined,
                source: "canonical_manifest",
            }
            : undefined,
        villani: data,
    };
}
export function corruptVillaniRun(runPath, error) {
    const runDirectory = path.resolve(runPath);
    const reason = error instanceof Error ? error.message : String(error);
    const runId = path.basename(runDirectory);
    return {
        provider: "villani",
        sessionPath: runDirectory,
        path: runDirectory,
        sessionId: runId,
        events: [
            {
                id: `corrupt:${runId}`,
                provider: "villani",
                sessionId: runId,
                runId,
                type: "error",
                title: "Corrupt Villani run",
                summary: reason,
                raw: { run_directory: runDirectory, error: reason },
            },
        ],
        warnings: [`Corrupt Villani run ${runId}: ${reason}`],
        villani: {
            runDirectory,
            policyDecisions: [],
            attempts: [],
            verifications: [],
            artifactPaths: {},
            corruptReason: reason,
        },
    };
}
