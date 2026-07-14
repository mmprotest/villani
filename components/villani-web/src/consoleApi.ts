import {
  consoleReplayFromRunDetail,
  type ArtifactDescriptor,
  type ConsoleBootstrap,
  type ConsoleHistoryEntry,
  type ConsoleReplaySnapshot,
  type RunDetail,
  type RunEvent,
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
    return this.get<{ schema_version: string; models: ConsoleBootstrap["models"] }>(
      "/v1/console/models",
      signal,
    );
  }

  policies(signal?: AbortSignal) {
    return this.get<{
      schema_version: string;
      active_policy: string | null;
      presets: { id: string; label: string; active: boolean }[];
    }>("/v1/console/policies", signal);
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
