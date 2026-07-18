import type { ArtifactDescriptor, CanonicalRunSnapshot, RunDetail, RunEvent } from "./types.js";
import type { EntitlementState, UpdateState } from "./selfService.js";
export type ConsoleMode = "local" | "connected";
export type ConsoleRecordKind = "run" | "session";
export type ConsoleSynchronizationState = "LOCAL" | "SYNC PENDING" | "SYNCHRONIZED" | "REDACTED" | "SYNC FAILED";
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
/**
 * Project an authorized connected run into the same replay contract emitted by
 * Flight Recorder's local parsing engine. This function interprets only the
 * stable run model; it never reads files or infers missing telemetry.
 */
export declare function consoleReplayFromRunDetail(detail: RunDetail, events?: RunEvent[], artifacts?: ArtifactDescriptor[], synchronizationState?: ConsoleSynchronizationState): ConsoleReplaySnapshot;
