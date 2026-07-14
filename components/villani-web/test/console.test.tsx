import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
      backend_name: "default",
      display_name: "Local model",
      model: "local-model",
      provider: "local",
      endpoint: "http://127.0.0.1:1234/v1",
      configured: true,
      detected: true,
      availability: "available",
      available: true,
      tool_support: "unknown",
      context_metadata: { context_window: 8192 },
      configured_roles: ["coding", "classification"],
      capability: "BOOTSTRAP",
      capability_status: "BOOTSTRAP",
      context_window: 8192,
      pricing_status: "unknown",
      currency: "USD",
      observed_task_count: 0,
      observed_success_rate: null,
      observed_cost_per_accepted_task: null,
      bootstrap_default: true,
      manual_override: false,
      manual_override_label: null,
      last_tested_at: null,
      last_test_diagnostic: null,
      capability_policy_version: "villani-model-lifecycle-v1",
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

function mockConsole(connected = false, awaitingApproval = false) {
  let approvalResolved = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/v1/console/bootstrap")) return response(bootstrap(connected));
      if (url.includes("/v1/console/run-options"))
        return response({
          schema_version: "villani.console.run_options.v1",
          repositories: [
            {
              path: "C:/repo",
              name: "repo",
              valid: true,
              dirty: false,
              source: "setup",
            },
          ],
          default_repository: "C:/repo",
          delivery_modes: [
            { id: "suggest", label: "Suggest", description: "Preserve patch" },
            { id: "approve", label: "Apply with approval", description: "Review" },
            { id: "apply", label: "Apply automatically", description: "Apply" },
            { id: "branch", label: "Create local branch", description: "Branch" },
            {
              id: "pull-request",
              label: "Create pull request",
              description: "Pull request",
            },
          ],
          approval_modes: [
            {
              id: "automatic",
              label: "Automatic after acceptance",
              description: "Automatic",
            },
            { id: "review", label: "Review before apply", description: "Review" },
          ],
          policy_presets: [
            {
              id: "reliable",
              label: "Reliable",
              description: "Prefer stronger validation and escalation evidence.",
              active: false,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
            {
              id: "balanced",
              label: "Balanced",
              description: "Balance cost and reliability.",
              active: true,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
            {
              id: "local-first",
              label: "Local first",
              description: "Prefer local models.",
              active: false,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
            {
              id: "cheapest-acceptable",
              label: "Cheapest acceptable",
              description: "Lowest known cost that meets requirements.",
              active: false,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
            {
              id: "custom",
              label: "Custom",
              description: "Advanced controls.",
              active: false,
              advanced: true,
              policy_version: "villani-public-policy-v1",
            },
          ],
          policies: [
            {
              id: "balanced",
              label: "Balanced",
              description: "Balance cost and reliability.",
              active: true,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
          ],
          advanced_policies: [
            {
              id: "configured",
              label: "Configured policy",
              description: "Configured",
            },
          ],
          routing_modes: ["observe", "recommend", "enforce"],
          defaults: {
            delivery_mode: "suggest",
            approval_mode: "automatic",
            policy_preset: "balanced",
            policy_selection: "configured",
            routing_mode: "observe",
            max_attempts: 3,
            max_cost: null,
            max_wall_time: null,
          },
          setup_issues: [],
        });
      if (url.includes("/v1/console/validation:discover"))
        return response({
          schema_version: "villani.console.validation_discovery.v1",
          repository: {
            path: "C:/repo",
            name: "repo",
            valid: true,
            dirty: false,
            source: "setup",
          },
          suggestions: [
            {
              suggestion_id: "npm_test",
              argv: ["npm", "test"],
              display_command: "npm test",
              confidence: 0.9,
              confidence_label: "high",
              requires_confirmation: false,
              reason: "package.json test script",
              source: "package_script",
              advisory_only: true,
              authoritative: false,
            },
          ],
          selected_suggestion_id: "npm_test",
          authority: "none_until_confirmed_command_execution",
          failure: null,
        });
      if (url.includes("/v1/console/policy:preview"))
        return response({
          schema_version: "villani.policy_preview.v1",
          raw_classification: { difficulty: "easy", risk: "low", confidence: 0.72 },
          effective_classification: {
            difficulty: "medium",
            risk: "low",
            confidence: 0.72,
          },
          adjustments: [
            {
              field: "difficulty",
              before: "easy",
              after: "medium",
              rule_id: "difficulty_floor.v1",
              reason: "Configured floor.",
            },
          ],
          eligible_models: [{ backend_name: "default" }],
          excluded_models: [
            { backend_name: "offline", reasons: ["backend is unavailable"] },
          ],
          selected_coding_route: {
            backend: "default",
            model: "local-model",
            action: "attempt",
            reason: "Balanced route.",
            route_provenance: { basis: "bootstrap_default" },
          },
          selected_verifier_route: {
            selected: { route: "deterministic-verifier", authority: "acceptance" },
          },
          estimated_cost: { value: null, status: "unknown", currency: "USD" },
          uncertainty: ["Selected-route cost is unknown."],
          policy_version: {
            public: "villani-public-policy-v1",
            preset: "balanced",
            controller: "bootstrap_v1",
          },
          coding_attempt_executed: false,
        });
      if (
        url.includes("/v1/console/runs/run_new/approval") &&
        init?.method === "POST"
      ) {
        approvalResolved = true;
        return response({
          schema_version: "villani.run_presentation.v1",
          run_id: "run_new",
          outcome: "ACCEPTED",
          execution_status: "COMPLETED",
          summary: "The accepted patch was applied to the target working tree.",
          changed: {
            files: ["src/parser.ts"],
            file_count: 1,
            zero_file_change: false,
            delivery_status: "applied",
          },
          confidence: {
            value: 0.98,
            label: "acceptance-grade",
            acceptance_eligible: true,
            authority: "repository_validation",
          },
          validation: {
            commands: [],
            checks_passed: 1,
            checks_failed: 0,
            requirements_verified: 1,
            authority: "executed_repository_validation",
          },
          remaining_risks: [],
          cost: {
            currency: "USD",
            coding: 0.1,
            verification: 0.02,
            total: 0.12,
            accounting_status: "complete",
          },
          recovery: ["No retry or escalation was needed"],
          next_actions: [],
          delivery: {
            mode: "approve",
            state: "applied",
            label: "Applied",
            repository_modified: true,
            target_worktree_modified: true,
            authority: { policy_version: "approval-v1", permitted: true, reasons: [] },
            approval: { status: "approved", deadline: null },
            review: {
              files_changed: ["src/parser.ts"],
              insertions: 8,
              deletions: 2,
              validation_evidence: [{ summary: "18 repository checks passed" }],
              verifier_authority: "repository_validation",
              candidate_comparison: [{ attempt_id: "attempt_001", rank: 1 }],
              remaining_risks: [],
              cost: { value: 0.12, accounting_status: "complete", currency: "USD" },
              unrelated_change_warnings: [],
              sensitive_file_warnings: [],
            },
            result: {},
            failure: null,
            eligible_candidate_ids: ["attempt_001"],
          },
          failure: null,
          lineage: {},
          progress: [],
          attempts: [],
          selected_attempt_id: "attempt_001",
        });
      }
      if (
        url.includes("/v1/console/runs/run_new/status") &&
        awaitingApproval &&
        !approvalResolved
      )
        return response({
          schema_version: "villani.run_presentation.v1",
          run_id: "run_new",
          outcome: "AWAITING APPROVAL",
          execution_status: "AWAITING_APPROVAL",
          summary:
            "An acceptance-eligible patch is waiting for explicit delivery approval.",
          changed: {
            files: ["src/parser.ts"],
            file_count: 1,
            zero_file_change: false,
            delivery_status: "awaiting_approval",
          },
          confidence: {
            value: 0.98,
            label: "acceptance-grade",
            acceptance_eligible: true,
            authority: "repository_validation",
          },
          validation: {
            commands: [{ command: "npm test", authority: "repository_validation" }],
            checks_passed: 18,
            checks_failed: 0,
            requirements_verified: 3,
            authority: "executed_repository_validation",
          },
          remaining_risks: ["Review the parser edge case."],
          cost: {
            currency: "USD",
            coding: 0.1,
            verification: 0.02,
            total: 0.12,
            accounting_status: "complete",
          },
          recovery: ["Selected attempt 1"],
          next_actions: [],
          delivery: {
            mode: "approve",
            state: "awaiting_approval",
            label: "Awaiting Approval",
            repository_modified: false,
            target_worktree_modified: false,
            patch_artifact: "delivery/selected.patch",
            patch_sha256: "b".repeat(64),
            authority: {
              policy_version: "approval-v1",
              permitted: false,
              reasons: ["Explicit approval is pending."],
            },
            approval: {
              status: "pending",
              deadline: "2026-07-15T00:00:00Z",
              timeout_policy: "reject",
              allow_candidate_change: true,
            },
            review: {
              files_changed: ["src/parser.ts"],
              insertions: 8,
              deletions: 2,
              validation_evidence: [{ summary: "18 repository checks passed" }],
              verifier_authority: "repository_validation",
              candidate_comparison: [
                { attempt_id: "attempt_001", rank: 1 },
                { attempt_id: "attempt_002", rank: 2 },
              ],
              remaining_risks: ["Review the parser edge case."],
              cost: { value: 0.12, accounting_status: "complete", currency: "USD" },
              unrelated_change_warnings: ["One scope warning was recorded."],
              sensitive_file_warnings: [],
            },
            result: {},
            failure: null,
            eligible_candidate_ids: ["attempt_001", "attempt_002"],
          },
          failure: null,
          lineage: {},
          progress: [{ tone: "active", symbol: "●", message: "Waiting for approval" }],
          attempts: [],
          selected_attempt_id: "attempt_001",
        });
      if (url.includes("/v1/console/runs/run_new/status"))
        return response({
          schema_version: "villani.run_presentation.v1",
          run_id: "run_new",
          outcome: "ACCEPTED",
          execution_status: "COMPLETED",
          summary: "The parser now handles repeated separators.",
          changed: {
            files: ["src/parser.ts", "test/parser.test.ts"],
            file_count: 2,
            zero_file_change: false,
            delivery_status: "succeeded",
          },
          confidence: {
            value: 0.98,
            label: "acceptance-grade",
            acceptance_eligible: true,
            authority: "structured repository-validation evidence",
          },
          validation: {
            commands: [
              { command: "npm test", passed: true, authority: "repository_validation" },
            ],
            checks_passed: 1,
            checks_failed: 0,
            requirements_verified: 2,
            authority: "executed_repository_validation",
          },
          remaining_risks: ["No remaining risk was recorded by the verifier."],
          cost: {
            currency: "USD",
            coding: 0.14,
            verification: 0.03,
            total: 0.17,
            accounting_status: "complete",
          },
          recovery: ["No retry or escalation was needed"],
          next_actions: [{ label: "Review changes", action: "git diff --stat" }],
          delivery: {
            mode: "suggest",
            state: "suggested",
            label: "Suggested",
            repository_modified: false,
            target_worktree_modified: false,
            patch_artifact: "delivery/selected.patch",
            patch_sha256: "a".repeat(64),
            authority: {
              policy_version: "not_required",
              permitted: true,
              reasons: ["Suggest mode never mutates the repository."],
            },
            approval: { status: "not_required", deadline: null },
            review: {
              files_changed: ["src/parser.ts", "test/parser.test.ts"],
              insertions: 18,
              deletions: 2,
              validation_evidence: [{ summary: "Repository checks passed." }],
              verifier_authority: "repository_validation",
              candidate_comparison: [{ attempt_id: "attempt_001", rank: 1 }],
              remaining_risks: [],
              cost: {
                value: 0.17,
                accounting_status: "complete",
                currency: "USD",
              },
              unrelated_change_warnings: [],
              sensitive_file_warnings: [],
            },
            result: {},
            failure: null,
            eligible_candidate_ids: ["attempt_001"],
          },
          failure: null,
          lineage: {},
          progress: [
            {
              tone: "success",
              symbol: "✓",
              message: "Run accepted and delivery completed",
            },
          ],
          attempts: [],
        });
      if (url.endsWith("/v1/console/runs") && init?.method === "POST")
        return response({
          schema_version: "villani.console.run_submission.v1",
          status: "QUEUED",
          run_id: "run_new",
          run_url: "/console/run?run=run_new",
          replay_url: "/console/runs/run_new",
          validation_commands: ["npm test"],
          failure: null,
        });
      if (url.includes("/v1/console/history"))
        return response({
          schema_version: "villani.console.history.v1",
          entries,
          warnings: [],
        });
      if (url.includes("/v1/console/sessions/claude_1")) return response(replay);
      if (
        url.includes("/v1/console/models:detect") ||
        url.includes("/v1/console/models:test") ||
        url.includes("/v1/console/models:add") ||
        url.includes("/v1/console/models:remove") ||
        url.includes("/v1/console/models:default")
      )
        return response({
          schema_version: "villani.console.models.v1",
          models: bootstrap(connected).models,
          bootstrap_default: "default",
          capability_states: [
            "UNRATED",
            "BOOTSTRAP",
            "OBSERVED",
            "QUALIFIED",
            "DISABLED",
          ],
        });
      if (url.includes("/v1/console/models"))
        return response({
          schema_version: "villani.console.models.v1",
          models: bootstrap(connected).models,
          bootstrap_default: "default",
          capability_states: [
            "UNRATED",
            "BOOTSTRAP",
            "OBSERVED",
            "QUALIFIED",
            "DISABLED",
          ],
        });
      if (url.includes("/v1/console/policies:simulate"))
        return response({
          schema_version: "villani.policy_simulation.v1",
          preset: "local-first",
          tasks_evaluated: 4,
          tasks_affected: 2,
          route_changes: [{ run_id: "run_1" }, { run_id: "run_2" }],
          estimated_cost_differences: {
            status: "partial",
            simulated_minus_recorded_total: -0.5,
            known_task_count: 2,
            unknown_task_count: 2,
          },
          outcome_evidence_limitations: [
            "Recorded outcomes apply only to routes that actually executed.",
          ],
          unsupported_counterfactual_claims: ["causal cost savings"],
          causal_savings_supported: false,
          live_policy_changed: false,
        });
      if (url.includes("/v1/console/policies:select"))
        return response({
          schema_version: "villani.console.policies.v1",
          active_preset: "reliable",
          presets: [],
          setup_issues: [],
        });
      if (url.includes("/v1/console/policies"))
        return response({
          schema_version: "villani.console.policies.v1",
          active_preset: "balanced",
          presets: [
            {
              id: "reliable",
              label: "Reliable",
              description: "Prefer stronger validation and escalation.",
              active: false,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
            {
              id: "balanced",
              label: "Balanced",
              description: "Balance cost and reliability.",
              active: true,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
            {
              id: "local-first",
              label: "Local first",
              description: "Prefer local models.",
              active: false,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
            {
              id: "cheapest-acceptable",
              label: "Cheapest acceptable",
              description: "Choose lowest known cost.",
              active: false,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
            {
              id: "custom",
              label: "Custom",
              description: "Advanced controls.",
              active: false,
              advanced: true,
              policy_version: "villani-public-policy-v1",
            },
          ],
          setup_issues: [],
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

describe("model and policy management", () => {
  it("detects and tests models while showing unknown lifecycle facts", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console/models");
    render(<ConsoleApp />);

    await screen.findByRole("heading", { name: "Models" });
    expect(await screen.findByText("Local model")).toBeInTheDocument();
    expect(screen.getByText("BOOTSTRAP")).toBeInTheDocument();
    expect(screen.getAllByText("unknown").length).toBeGreaterThan(0);
    expect(
      screen.getByText("Manual capability score (Advanced override)"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Detect models" }));
    await waitFor(() =>
      expect(
        (fetch as ReturnType<typeof vi.fn>).mock.calls.some(([input]) =>
          String(input).includes("/v1/console/models:detect"),
        ),
      ).toBe(true),
    );
    expect(
      screen.getByText(/inspect model-list endpoints and use zero model tokens/),
    ).toBeInTheDocument();
  });

  it("selects public presets and reports historical simulation limits", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console/policies");
    render(<ConsoleApp />);

    await screen.findByRole("heading", { name: "Policies" });
    for (const label of [
      "Reliable",
      "Balanced",
      "Local first",
      "Cheapest acceptable",
      "Custom",
    ])
      expect(screen.getByRole("heading", { name: label })).toBeInTheDocument();
    expect(screen.getByText("Exposes Advanced controls.")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Evaluate preset"), {
      target: { value: "local-first" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Evaluate recorded runs" }));
    expect(
      await screen.findByText(
        "Recorded outcomes apply only to routes that actually executed.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/cannot establish causal savings/)).toBeInTheDocument();
    expect(screen.getByText(/causal cost savings/)).toBeInTheDocument();
  });
});

describe("Console run workflow", () => {
  it("submits the complete run form and answers the outcome questions", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console/run");
    render(<ConsoleApp />);

    await screen.findByRole("heading", { name: "Run" });
    expect(
      await screen.findByText("npm test", { selector: "code" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Task instruction"), {
      target: { value: "Fix repeated separators in the parser." },
    });
    fireEvent.change(screen.getByLabelText("Success criteria (optional)"), {
      target: { value: "The repository test passes." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview routing" }));
    const preview = await screen.findByRole("region", { name: "Policy preview" });
    expect(within(preview).getByText(/easy difficulty, low risk/)).toBeInTheDocument();
    expect(within(preview).getByText("bootstrap_default")).toBeInTheDocument();
    expect(within(preview).getByText("deterministic-verifier")).toBeInTheDocument();
    expect(within(preview).getByText("Unknown (unknown)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run task" }));

    const result = await screen.findByTestId("run-presentation");
    expect(
      within(result).getByRole("heading", { name: "ACCEPTED" }),
    ).toBeInTheDocument();
    expect(within(result).getByText("src/parser.ts")).toBeInTheDocument();
    for (const heading of [
      "DELIVERY",
      "CONFIDENCE AND AUTHORITY",
      "VALIDATION",
      "REMAINING RISKS",
      "COST",
      "VILLANI RECOVERY",
      "NEXT",
    ])
      expect(
        within(result).getByRole("heading", { name: heading }),
      ).toBeInTheDocument();
    expect(within(result).getByText("USD 0.1700")).toBeInTheDocument();
    const submissionCall = (fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      ([input, init]) =>
        String(input).endsWith("/v1/console/runs") && init?.method === "POST",
    );
    expect(JSON.parse(String(submissionCall?.[1]?.body))).toMatchObject({
      policy_preset: "balanced",
      task: "Fix repeated separators in the parser.",
    });
    await waitFor(() =>
      expect(
        String((fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0]),
      ).toContain("/v1/console/runs/run_new/status"),
    );
  });

  it("shows the persisted patch review and records an approval action", async () => {
    mockConsole(false, true);
    history.replaceState(null, "", "/console/run");
    render(<ConsoleApp />);

    await screen.findByRole("heading", { name: "Run" });
    fireEvent.change(screen.getByLabelText("Task instruction"), {
      target: { value: "Fix repeated separators in the parser." },
    });
    fireEvent.change(screen.getByLabelText("Delivery mode"), {
      target: { value: "approve" },
    });
    expect(screen.getByText("Explicit approval after selection")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run task" }));

    const review = await screen.findByTestId("run-presentation");
    expect(
      within(review).getByRole("heading", { name: "AWAITING APPROVAL" }),
    ).toBeInTheDocument();
    expect(
      within(review).getByRole("heading", { name: "PATCH REVIEW" }),
    ).toBeInTheDocument();
    expect(within(review).getByText("18 repository checks passed")).toBeInTheDocument();
    expect(within(review).getByText(/attempt_001 · rank 1/)).toBeInTheDocument();
    expect(within(review).getByText(/attempt_002 · rank 2/)).toBeInTheDocument();
    expect(within(review).getAllByText("repository_validation").length).toBeGreaterThan(
      0,
    );
    expect(
      within(review).getByText("One scope warning was recorded."),
    ).toBeInTheDocument();
    expect(
      within(review).getByRole("button", { name: "Reject delivery" }),
    ).toBeInTheDocument();
    expect(
      within(review).getByRole("button", { name: "Request rerun" }),
    ).toBeInTheDocument();

    fireEvent.change(within(review).getByLabelText("Decision reason (optional)"), {
      target: { value: "Reviewed evidence and patch." },
    });
    fireEvent.click(within(review).getByRole("button", { name: "Approve and apply" }));

    await waitFor(() =>
      expect(
        within(review).getByRole("heading", { name: "ACCEPTED" }),
      ).toBeInTheDocument(),
    );
    expect(within(review).getAllByText("Applied").length).toBeGreaterThan(0);
    const approvalCall = (fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      ([input, init]) =>
        String(input).includes("/v1/console/runs/run_new/approval") &&
        init?.method === "POST",
    );
    expect(JSON.parse(String(approvalCall?.[1]?.body))).toMatchObject({
      action: "approve",
      reason: "Reviewed evidence and patch.",
    });
  });

  it("explains how to recover when Villani Service is offline", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("connection refused")));
    history.replaceState(null, "", "/console/run");

    render(<ConsoleApp />);

    expect(
      await screen.findByText(
        "Console attempted the authenticated local service boundary.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No live local service connection is available."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No run was started, so no patch was created."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Run `villani service start`, then retry."),
    ).toBeInTheDocument();
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
