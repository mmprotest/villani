import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  ConsoleBootstrap,
  ConsoleHistoryEntry,
  ConsoleReplaySnapshot,
} from "@villani/run-model";

import ConsoleApp, {
  filterHistory,
  migrateLegacyPath,
  type HistoryFilters,
} from "../src/ConsoleApp";

const bootstrap = (connected = false): ConsoleBootstrap => ({
  schema_version: "villani.console.bootstrap.v1",
  mode: connected ? "connected" : "local",
  data_source: "local-service",
  version: "0.3.0",
  workspace: {
    connected,
    id: connected ? "workspace_1" : null,
    endpoint: connected ? "https://workspace.invalid" : null,
  },
  service: {
    status: "running",
    started_at: null,
    log_path: "service.log",
    last_error: null,
  },
  setup: { configured: true, valid: true, schema_version: 1, issues: [] },
  synchronization: { pending: connected ? 1 : 0, dead_letters: 0 },
  storage: { home: "home", runs: "runs", spool: "spool", writable: true },
  models: [
    {
      id: "local-model",
      provider: "local",
      endpoint: "http://127.0.0.1:1234/v1",
      configured: true,
      detected: true,
      available: true,
      capability: "unrated",
      context_window: 8192,
      pricing_status: "unknown",
    },
  ],
  active_policy: "bootstrap_v1",
});

const entries: ConsoleHistoryEntry[] = [
  {
    id: "run_1",
    logical_id: "run_1",
    kind: "run",
    source: "villani",
    source_label: "Villani",
    provider: "villani",
    repository: "repo",
    task: "Fix parser",
    status: "completed",
    model: "local-model",
    started_at: "2026-07-14T00:00:00Z",
    updated_at: "2026-07-14T00:01:00Z",
    duration_ms: 60_000,
    cost: null,
    currency: null,
    cost_available: false,
    synchronization_state: "SYNC PENDING",
    deep_link: "/console/runs/run_1",
  },
  {
    id: "run_1",
    logical_id: "run_1",
    kind: "run",
    source: "villani",
    source_label: "Villani",
    provider: "villani",
    repository: "repo",
    task: "Duplicate",
    status: "completed",
    model: "local-model",
    started_at: "2026-07-14T00:00:00Z",
    updated_at: "2026-07-14T00:01:00Z",
    duration_ms: 60_000,
    cost: null,
    currency: null,
    cost_available: false,
    synchronization_state: "SYNCHRONIZED",
    deep_link: "/console/runs/run_1",
  },
  {
    id: "claude_1",
    logical_id: "claude_1",
    kind: "session",
    source: "claude",
    source_label: "Claude Code",
    provider: "claude",
    repository: "repo",
    task: "Imported task",
    status: "success",
    model: "claude-model",
    started_at: "2026-07-13T00:00:00Z",
    updated_at: "2026-07-13T00:01:00Z",
    duration_ms: 60_000,
    cost: 0.1,
    currency: "USD",
    cost_available: true,
    synchronization_state: "LOCAL",
    deep_link: "/console/sessions/claude_1",
  },
];

const replay: ConsoleReplaySnapshot = {
  schema_version: "villani.console.replay.v1",
  id: "claude_1",
  logical_id: "claude_1",
  kind: "session",
  source: "claude",
  source_label: "Claude Code",
  provider: "claude",
  synchronization_state: "LOCAL",
  summary: {
    status: "success",
    task: "Imported task",
    repository: "repo",
    model: "claude-model",
    policy: null,
    started_at: "2026-07-13T00:00:00Z",
    completed_at: "2026-07-13T00:01:00Z",
    duration_ms: 60_000,
    total_tokens: null,
    total_cost: null,
    currency: null,
    terminal_reason: null,
  },
  events: [
    {
      id: "event_1",
      sequence: 1,
      timestamp: "2026-07-13T00:00:00Z",
      source: "claude",
      kind: "user_message",
      title: "Task submitted",
      summary: "Imported task",
      status: "recorded",
      attempt_id: null,
      command: null,
      exit_code: null,
      duration_ms: null,
      path: null,
      stdout: null,
      stderr: null,
      deep_link: "/console/sessions/claude_1/events/event_1",
    },
  ],
  attempts: [],
  evidence: { warnings: [] },
  verification: { outcome: "not_applicable" },
  candidate_comparison: [],
  files: [],
  artifacts: [],
  cost: {
    accounting_status: "unknown",
    currency: null,
    coding: null,
    verification: null,
    total: null,
  },
  logs: [],
  canonical: null,
  warnings: [],
  deep_links: { self: "/console/sessions/claude_1", history: "/console/history" },
};

function response(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function mockConsole(connected = false) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/v1/console/bootstrap")) return response(bootstrap(connected));
      if (url.includes("/v1/console/history"))
        return response({
          schema_version: "villani.console.history.v1",
          entries,
          warnings: [],
        });
      if (url.includes("/v1/console/sessions/claude_1")) return response(replay);
      if (url.includes("/v1/console/policies"))
        return response({
          schema_version: "villani.console.policies.v1",
          active_policy: "bootstrap_v1",
          presets: [],
        });
      if (url.includes("/v1/console/workspace/"))
        return response({
          connected,
          workspace_id: "workspace_1",
          surface: "tasks",
          items: [],
          message: "Connected",
        });
      throw new Error(`Unhandled request: ${url}`);
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  history.replaceState(null, "", "/");
});

describe("Console routing and migration", () => {
  it("migrates supported Web and Flight Recorder links", () => {
    expect(migrateLegacyPath("/runs/run_1")).toBe("/console/runs/run_1");
    expect(migrateLegacyPath("/flight/runs/run_1")).toBe("/console/runs/run_1/replay");
    expect(migrateLegacyPath("/flight/runs/run_1/events/event_1")).toBe(
      "/console/runs/run_1/events/event_1",
    );
    expect(migrateLegacyPath("/flight")).toBe("/console/replay");
    expect(migrateLegacyPath("/flight/sessions/session_1/events/e1")).toBe(
      "/console/sessions/session_1/events/e1",
    );
    expect(migrateLegacyPath("/fleet")).toBe("/console/fleet");
    expect(migrateLegacyPath("/history")).toBe("/console/history");
  });

  it("keeps Team navigation hidden locally and reveals it after enrolment", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console/models");
    const view = render(<ConsoleApp />);
    await screen.findByRole("heading", { name: "Models" });
    expect(screen.queryByTestId("team-navigation")).not.toBeInTheDocument();
    view.unmount();
    mockConsole(true);
    render(<ConsoleApp />);
    await screen.findByRole("heading", { name: "Models" });
    expect(screen.getByTestId("team-navigation")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Fleet" })).toHaveAttribute(
      "href",
      "/console/fleet",
    );
  });
});

describe("merged History", () => {
  it("shows local runs and imported providers once with public synchronization states", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console/history");
    render(<ConsoleApp />);
    const table = await screen.findByTestId("merged-history");
    expect(
      within(table).getAllByRole("link", { name: /Duplicate|Fix parser/ }),
    ).toHaveLength(1);
    expect(
      within(table).getByRole("link", { name: "Imported task" }),
    ).toBeInTheDocument();
    expect(within(table).getByText("Claude Code")).toBeInTheDocument();
    expect(within(table).getByText("LOCAL")).toBeInTheDocument();
    expect(within(table).getByText("SYNCHRONIZED")).toBeInTheDocument();
  });

  it("filters providers, sync states, cost availability, and task text", () => {
    const empty: HistoryFilters = {
      repository: "",
      source: "",
      status: "",
      model: "",
      date: "",
      synchronization: "",
      cost: "",
      task: "",
    };
    expect(filterHistory(entries, { ...empty, source: "claude" })).toEqual([
      entries[2],
    ]);
    expect(
      filterHistory(entries, { ...empty, synchronization: "SYNC PENDING" }),
    ).toEqual([entries[0]]);
    expect(filterHistory(entries, { ...empty, cost: "known" })).toEqual([entries[2]]);
    expect(filterHistory(entries, { ...empty, task: "parser" })).toEqual([entries[0]]);
  });
});

describe("embedded replay", () => {
  it("renders every required panel and resolves an event deep link", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console/sessions/claude_1/events/event_1");
    render(<ConsoleApp />);
    await screen.findByTestId("console-replay");
    for (const name of [
      "SUMMARY",
      "TIMELINE",
      "EVENT STREAM",
      "ATTEMPTS",
      "EVIDENCE",
      "VERIFICATION",
      "CANDIDATE COMPARISON",
      "FILES",
      "COST",
      "LOGS",
    ])
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    expect(screen.getByTestId("deep-link-target")).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Primary navigation" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute(
      "href",
      "#main-content",
    );
  });
});
