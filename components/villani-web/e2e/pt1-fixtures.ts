import type { Page } from "@playwright/test";

export type Pt1FixtureOptions = {
  setupValid?: boolean;
  serviceStatus?: string;
  serviceError?: string | null;
  runOptionsError?: boolean;
  modelTestAvailable?: boolean;
  activity?: Record<string, unknown>[];
};

export const localModel = {
  id: "local-model",
  backend_name: "default",
  display_name: "Local coding agent",
  model: "local-model",
  provider: "Local provider",
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
  last_tested_at: "2026-07-17T00:00:00Z",
  last_test_diagnostic: "Connection verified without model-token use.",
  capability_policy_version: "villani-model-lifecycle-v1",
};

export const mixedActivity = [
  {
    id: "run_accepted",
    logical_id: "run_accepted",
    kind: "run",
    source: "villani",
    source_label: "Villani",
    provider: "villani",
    repository: "villani",
    task: "Unify the product navigation",
    status: "accepted",
    model: "local-model",
    started_at: "2026-07-17T00:00:00Z",
    updated_at: "2026-07-17T00:02:05Z",
    duration_ms: 125_000,
    cost: null,
    currency: null,
    cost_available: false,
    synchronization_state: "LOCAL",
    deep_link: "/console/runs/run_accepted",
  },
  {
    id: "session_imported",
    logical_id: "session_imported",
    kind: "session",
    source: "claude",
    source_label: "Claude Code",
    provider: "claude",
    repository: "sample-app",
    task: "Investigate a flaky parser test",
    status: "success",
    model: "claude-model",
    started_at: "2026-07-16T03:30:00Z",
    updated_at: "2026-07-16T03:30:42Z",
    duration_ms: 42_000,
    cost: 0.08,
    currency: "USD",
    cost_available: true,
    synchronization_state: "IMPORTED",
    deep_link: "/console/sessions/session_imported",
  },
  {
    id: "run_unproved",
    logical_id: "run_unproved",
    kind: "run",
    source: "villani",
    source_label: "Villani",
    provider: "villani",
    repository: "docs-site",
    task: "Update the release notes",
    status: "exhausted",
    model: "local-model",
    started_at: "2026-07-15T11:00:00Z",
    updated_at: "2026-07-15T11:01:14Z",
    duration_ms: 74_000,
    cost: null,
    currency: null,
    cost_available: false,
    synchronization_state: "LOCAL",
    deep_link: "/console/runs/run_unproved",
  },
];

export async function mockPt1Console(page: Page, options: Pt1FixtureOptions = {}) {
  const setupValid = options.setupValid ?? true;
  await page.route("**/v1/console/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/console/bootstrap")
      return route.fulfill({
        json: {
          schema_version: "villani.console.bootstrap.v1",
          mode: "local",
          data_source: "local-service",
          version: "0.3.0",
          workspace: { connected: false, id: null, endpoint: null },
          service: {
            status: options.serviceStatus ?? "running",
            started_at: "2026-07-17T00:00:00Z",
            log_path: "C:/villani/service.log",
            last_error: options.serviceError ?? null,
          },
          setup: {
            configured: setupValid,
            valid: setupValid,
            schema_version: 1,
            issues: setupValid ? [] : ["Choose a repository and verify an agent."],
          },
          synchronization: { pending: 0, dead_letters: 0 },
          storage: {
            home: "C:/villani",
            runs: "C:/villani/runs",
            spool: "C:/villani/spool",
            writable: true,
          },
          models: [localModel],
          active_policy: "balanced",
        },
      });
    if (path === "/v1/console/run-options") {
      if (options.runOptionsError)
        return route.fulfill({ status: 503, json: { message: "Service unavailable" } });
      return route.fulfill({
        json: {
          schema_version: "villani.console.run_options.v1",
          repositories: [
            {
              path: "C:/work/villani",
              name: "villani",
              valid: true,
              dirty: false,
              source: "setup",
            },
            {
              path: "C:/work/sample-app",
              name: "sample-app",
              valid: true,
              dirty: false,
              source: "recent",
            },
          ],
          default_repository: "C:/work/villani",
          delivery_modes: [
            {
              id: "approve",
              label: "Apply with approval",
              description: "Wait for a delivery decision after proof.",
            },
            {
              id: "suggest",
              label: "Suggest",
              description: "Record a patch without applying it.",
            },
          ],
          approval_modes: [],
          policies: [{ id: "balanced", label: "Balanced", description: "Balanced" }],
          policy_presets: [
            {
              id: "performance",
              label: "Performance",
              description: "Use the strongest eligible agent system.",
              active: true,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
            {
              id: "balanced",
              label: "Balanced",
              description: "Balanced local verification",
              active: true,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
          ],
          advanced_policies: [
            { id: "configured", label: "Configured", description: "Configured" },
          ],
          routing_modes: ["observe"],
          defaults: {
            delivery_mode: "approve",
            approval_mode: "automatic",
            policy_preset: "performance",
            policy_selection: "configured",
            routing_mode: "observe",
            max_attempts: 3,
            max_cost: null,
            max_wall_time: null,
            verification_required: true,
            mode: "performance",
          },
          setup_issues: setupValid ? [] : ["Finish setup before starting a task."],
        },
      });
    }
    if (path === "/v1/console/validation:discover")
      return route.fulfill({
        json: {
          schema_version: "villani.console.validation_discovery.v1",
          repository: {
            path: "C:/work/villani",
            name: "villani",
            valid: true,
            dirty: false,
            source: "setup",
          },
          suggestions: [
            {
              suggestion_id: "pytest",
              argv: ["python", "-m", "pytest", "-q"],
              display_command: "python -m pytest -q",
              confidence: 1,
              confidence_label: "high",
              requires_confirmation: false,
              reason: "Detected repository tests",
              source: "repository",
              advisory_only: true,
              authoritative: false,
            },
          ],
          selected_suggestion_id: "pytest",
          authority: "advisory",
          failure: null,
        },
      });
    if (path === "/v1/console/models" || path === "/v1/console/models:detect")
      return route.fulfill({
        json: {
          schema_version: "villani.console.models.v1",
          models: [localModel],
          bootstrap_default: "default",
          capability_states: [
            "UNRATED",
            "BOOTSTRAP",
            "OBSERVED",
            "QUALIFIED",
            "DISABLED",
          ],
        },
      });
    if (path === "/v1/console/models:test")
      return route.fulfill({
        json: {
          schema_version: "villani.console.model_test.v1",
          results: [
            {
              backend_name: "default",
              availability:
                options.modelTestAvailable === false ? "unavailable" : "available",
              diagnostic:
                options.modelTestAvailable === false
                  ? "The local agent endpoint did not respond."
                  : "Connection verified without model-token use.",
              tested_at: "2026-07-17T00:00:00Z",
              model_tokens_used: 0,
            },
          ],
          model_tokens_used: 0,
        },
      });
    if (path === "/v1/console/history")
      return route.fulfill({
        json: {
          schema_version: "villani.console.history.v1",
          entries: options.activity ?? [],
          warnings: [],
        },
      });
    if (path === "/v1/console/settings")
      return route.fulfill({
        json: {
          schema_version: "villani.console.settings.v1",
          privacy: { secrets_exposed: false, local_first: true },
        },
      });
    return route.fulfill({
      status: 404,
      json: { message: "Fixture endpoint missing" },
    });
  });
}
