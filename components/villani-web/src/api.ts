import type {
  ArtifactDescriptor,
  RunDetail,
  RunEvent,
  RunSpan,
} from "@villani/run-model";

export interface EventPage {
  events: RunEvent[];
  next_cursor: string | null;
  cursor?: string | null;
}
export interface CursorPage<T> {
  values: T[];
  nextCursor: string | null;
}
export interface LiveCallbacks {
  onEvent: (event: RunEvent) => void;
  onState: (state: "connecting" | "live" | "reconnecting" | "offline") => void;
  onCatchUp: (events: RunEvent[], cursor?: string | null) => void;
}
export interface InterrogationAnswer {
  answer: string;
  interpreted_query: string;
  query_plan: Record<string, unknown>;
  metric_definitions: Record<string, string>;
  filters: Record<string, unknown>[];
  authorization: { permission_version: string; tenant_predicates_injected: boolean };
  estimate: {
    scan_rows: number;
    result_limit: number;
    estimated_cells: number;
    cost_units?: number;
  };
  data_freshness: string | null;
  row_count: number;
  uncertainty: Record<string, number | null>;
  rows: Record<string, unknown>[];
  supporting_runs: { run_id: string; url: string; last_observed_at: string }[];
  conversation: { id: string; version: number; stored_context: string };
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export class RunClient {
  constructor(
    private baseUrl = "",
    private token = "",
  ) {}
  private headers(): Record<string, string> {
    return this.token ? { Authorization: `Bearer ${this.token}` } : {};
  }
  private async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      headers: this.headers(),
      signal,
    });
    if (!response.ok)
      throw new ApiError(
        response.status,
        response.status === 404
          ? "Run not found or you are not authorized to view it."
          : `Request failed (${response.status})`,
      );
    return response.json() as Promise<T>;
  }
  private async post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { ...this.headers(), "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok)
      throw new ApiError(response.status, `Request failed (${response.status})`);
    return response.json() as Promise<T>;
  }
  fleetSearch(
    filters: Record<string, unknown>,
    cursor: string | null,
    limit = 100,
    signal?: AbortSignal,
  ) {
    return this.post<{ runs: Record<string, unknown>[]; next_cursor: string | null }>(
      "/v1/fleet/runs/search",
      { filters, cursor, limit },
      signal,
    );
  }
  fleetMetrics(
    filters: Record<string, unknown>,
    groupBy: string | null,
    signal?: AbortSignal,
  ) {
    return this.post<{
      definition_version: string;
      metrics: Record<string, any>;
      comparisons: Record<string, any>;
    }>("/v1/fleet/metrics", { filters, group_by: groupBy }, signal);
  }
  metricDefinitions(signal?: AbortSignal) {
    return this.get<{
      version: string;
      metrics: Record<string, Record<string, string>>;
    }>("/v1/fleet/metrics/definitions", signal);
  }
  semanticCatalog(signal?: AbortSignal) {
    return this.get<{ version: string; metrics: Record<string, string> }>(
      "/v1/interrogation/catalog",
      signal,
    );
  }
  interrogate(question: string, conversationId?: string, signal?: AbortSignal) {
    return this.post<InterrogationAnswer>(
      "/v1/interrogation/query",
      { question, conversation_id: conversationId ?? null },
      signal,
    );
  }
  savedViews(signal?: AbortSignal) {
    return this.get<{ views: Record<string, any>[] }>("/v1/fleet/saved-views", signal);
  }
  createSavedView(body: Record<string, unknown>, signal?: AbortSignal) {
    return this.post<Record<string, unknown>>("/v1/fleet/saved-views", body, signal);
  }
  alertRules(signal?: AbortSignal) {
    return this.get<{ rules: Record<string, any>[]; events: Record<string, any>[] }>(
      "/v1/fleet/alerts",
      signal,
    );
  }
  createAlertRule(body: Record<string, unknown>, signal?: AbortSignal) {
    return this.post<Record<string, unknown>>("/v1/fleet/alerts", body, signal);
  }
  reviewQueue(signal?: AbortSignal) {
    return this.get<{ items: Record<string, any>[] }>("/v1/fleet/review-queue", signal);
  }
  failureClusters(signal?: AbortSignal) {
    return this.get<{ clusters: Record<string, any>[] }>(
      "/v1/fleet/failure-clusters",
      signal,
    );
  }
  async fleetExport(filters: Record<string, unknown>, format: "csv" | "json") {
    const response = await fetch(`${this.baseUrl}/v1/fleet/export`, {
      method: "POST",
      headers: { ...this.headers(), "Content-Type": "application/json" },
      body: JSON.stringify({ filters, format }),
    });
    if (!response.ok)
      throw new ApiError(response.status, `Export failed (${response.status})`);
    return response.blob();
  }
  detail(runId: string, signal?: AbortSignal) {
    return this.get<RunDetail>(`/v1/runs/${encodeURIComponent(runId)}`, signal);
  }
  events(runId: string, cursor?: string | null, signal?: AbortSignal) {
    return this.get<EventPage>(
      `/v1/runs/${encodeURIComponent(runId)}/events?limit=500${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      signal,
    );
  }
  async spans(
    runId: string,
    cursor?: string | null,
    signal?: AbortSignal,
  ): Promise<CursorPage<RunSpan>> {
    const page = await this.get<{ spans: RunSpan[]; next_cursor: string | null }>(
      `/v1/runs/${encodeURIComponent(runId)}/spans?limit=250${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      signal,
    );
    return { values: page.spans, nextCursor: page.next_cursor };
  }
  async artifacts(
    runId: string,
    cursor?: string | null,
    signal?: AbortSignal,
  ): Promise<CursorPage<ArtifactDescriptor>> {
    const page = await this.get<{
      artifacts: ArtifactDescriptor[];
      next_cursor: string | null;
    }>(
      `/v1/runs/${encodeURIComponent(runId)}/artifacts?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      signal,
    );
    return { values: page.artifacts, nextCursor: page.next_cursor };
  }
  async artifactContent(artifactId: string, signal?: AbortSignal) {
    const response = await fetch(
      `${this.baseUrl}/v1/artifacts/${encodeURIComponent(artifactId)}/content`,
      { headers: this.headers(), signal, redirect: "follow" },
    );
    if (!response.ok)
      throw new ApiError(
        response.status,
        response.status === 404
          ? "Artifact unavailable or not authorized."
          : `Artifact request failed (${response.status})`,
      );
    return response.text();
  }
  async catchUp(
    runId: string,
    cursor: string | null,
    callbacks: LiveCallbacks,
    signal: AbortSignal,
  ): Promise<string | null> {
    let current = cursor;
    while (!signal.aborted) {
      const page = await this.events(runId, current, signal);
      callbacks.onCatchUp(page.events, page.cursor);
      current = page.cursor ?? current;
      if (!page.next_cursor) return current;
      current = page.next_cursor;
    }
    return current;
  }
  async subscribe(
    runId: string,
    cursor: string | null,
    callbacks: LiveCallbacks,
    signal: AbortSignal,
  ) {
    let delay = 500;
    let current = cursor;
    while (!signal.aborted) {
      callbacks.onState(delay === 500 ? "connecting" : "reconnecting");
      try {
        current = await this.catchUp(runId, current, callbacks, signal);
        const response = await fetch(
          `${this.baseUrl}/v1/runs/${encodeURIComponent(runId)}/stream`,
          { headers: { ...this.headers(), Accept: "text/event-stream" }, signal },
        );
        if (!response.ok || !response.body)
          throw new ApiError(response.status, "Live stream unavailable");
        callbacks.onState("live");
        delay = 500;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!signal.aborted) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          const frames = buffer.split(/\r?\n\r?\n/);
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            const raw = frame
              .split(/\r?\n/)
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).trim())
              .join("\n");
            if (!raw) continue;
            const message = JSON.parse(raw) as { payload?: { event?: RunEvent } };
            if (message.payload?.event) callbacks.onEvent(message.payload.event);
          }
        }
      } catch (error) {
        if (signal.aborted) break;
        callbacks.onState("reconnecting");
        await new Promise((resolve) => setTimeout(resolve, delay));
        delay = Math.min(delay * 2, 10_000);
      }
    }
    callbacks.onState("offline");
  }
}
