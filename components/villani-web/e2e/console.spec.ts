import { expect, test, type Page } from "@playwright/test";

const entry = {
  id: "session_1",
  logical_id: "session_1",
  kind: "session",
  source: "claude",
  source_label: "Claude Code",
  provider: "claude",
  repository: "repo",
  task: "Imported task",
  status: "success",
  model: "claude-model",
  started_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:01:00Z",
  duration_ms: 60_000,
  cost: null,
  currency: null,
  cost_available: false,
  synchronization_state: "LOCAL",
  deep_link: "/console/sessions/session_1",
};

const roleBindings = [
  { role: "classification", label: "Understand task", agent_system_id: "codex-cli" },
  { role: "coding", label: "Write code", agent_system_id: "codex-cli" },
  { role: "verification", label: "Verify result", agent_system_id: "claude-cli" },
  { role: "selection", label: "Choose candidate", agent_system_id: "claude-cli" },
];

function agentSystemsFixture() {
  const system = (
    id: string,
    displayName: string,
    driver: string,
    model: string,
    version: string,
  ) => ({
    id,
    system_id: id,
    display_name: displayName,
    kind: "cli_agent",
    driver,
    configured: true,
    status: "READY",
    ready: true,
    configured_executable: driver === "codex" ? "codex" : "claude",
    safe_display_path: `C:/tools/${driver}.exe`,
    resolved_path_digest: `sha256:${(driver === "codex" ? "1" : "2").repeat(64)}`,
    exact_version: version,
    authentication_ready: true,
    authentication_status: "ready",
    supported_roles: ["classification", "coding", "verification", "selection"],
    configured_roles: ["classification", "coding", "verification", "selection"],
    configured_model: model,
    model,
    instruction_policy: "native_project",
    permission_policy: "role-specific",
    conformance_status: "passed",
    last_doctor_time: "2026-07-22T12:00:00Z",
    affected_roles: [],
    what_failed: null,
    repository_modified: false,
    exact_next_action: `villani agents doctor ${id}`,
    evidence_path: `diagnostics/agent-systems/${id}.json`,
    role_results: [],
    role_badges: roleBindings.map(({ role, label }) => ({ id: role, label })),
  });
  return {
    schema_version: "villani.console.agent_systems.v1",
    agent_systems: [
      system("codex-cli", "Codex CLI", "codex", "codex-user-model", "codex 1.2.3"),
      system(
        "claude-cli",
        "Claude Code",
        "claude_code",
        "claude-user-model",
        "Claude Code 2.3.4",
      ),
    ],
    profiles: [
      {
        profile_id: "hybrid",
        profile_type: "hybrid",
        active: true,
        status: "ready",
        runnable: true,
        reasons: [],
        role_bindings: roleBindings,
      },
    ],
    active_profile: "hybrid",
    role_labels: Object.fromEntries(
      roleBindings.map(({ role, label }) => [role, label]),
    ),
    setup_issues: [],
  };
}

async function mockConsole(page: Page, connected = false) {
  const agentSystems = agentSystemsFixture();
  await page.route("**/v1/console/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/console/bootstrap")
      return route.fulfill({
        json: {
          schema_version: "villani.console.bootstrap.v1",
          mode: connected ? "connected" : "local",
          data_source: "local-service",
          version: "0.3.0",
          workspace: {
            connected,
            id: connected ? "workspace_1" : null,
            endpoint: null,
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
              last_tested_at: "2026-07-17T00:00:00Z",
              last_test_diagnostic: "Connection verified.",
              capability_policy_version: "villani-model-lifecycle-v1",
            },
          ],
          active_policy: "bootstrap_v1",
        },
      });
    if (path === "/v1/console/run-options")
      return route.fulfill({
        json: {
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
            { id: "suggest", label: "Suggest", description: "No change applied" },
          ],
          approval_modes: [],
          policies: [{ id: "balanced", label: "Balanced", description: "Balanced" }],
          policy_presets: [
            {
              id: "balanced",
              label: "Balanced",
              description: "Balanced",
              active: true,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
          ],
          advanced_policies: [
            { id: "configured", label: "Configured", description: "Configured" },
          ],
          execution_profiles: [
            {
              id: "hybrid",
              label: "Hybrid",
              profile_type: "hybrid",
              active: true,
              bindings: Object.fromEntries(
                roleBindings.map(({ role, agent_system_id }) => [
                  role,
                  agent_system_id,
                ]),
              ),
            },
          ],
          routing_modes: ["observe"],
          defaults: {
            delivery_mode: "suggest",
            approval_mode: "automatic",
            policy_preset: "balanced",
            policy_selection: "configured",
            routing_mode: "observe",
            execution_profile: "hybrid",
            max_attempts: 3,
            max_cost: null,
            max_wall_time: null,
          },
          setup_issues: [],
        },
      });
    if (
      path === "/v1/console/agent-systems" ||
      path === "/v1/console/agent-systems:detect" ||
      path === "/v1/console/agent-systems:doctor" ||
      path === "/v1/console/profiles:activate" ||
      path === "/v1/console/profiles:set-role"
    )
      return route.fulfill({ json: agentSystems });
    if (path === "/v1/console/validation:discover")
      return route.fulfill({
        json: {
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
              suggestion_id: "pytest",
              argv: ["python", "-m", "pytest", "-q"],
              display_command: "python -m pytest -q",
              confidence: 1,
              confidence_label: "high",
              requires_confirmation: false,
              reason: "Detected tests",
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
    if (path === "/v1/console/models")
      return route.fulfill({
        json: {
          schema_version: "villani.console.models.v1",
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
              context_metadata: {},
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
              last_test_diagnostic: "Connection verified.",
              capability_policy_version: "villani-model-lifecycle-v1",
            },
          ],
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
    if (path === "/v1/console/settings")
      return route.fulfill({
        json: {
          schema_version: "villani.console.settings.v1",
          privacy: { secrets_exposed: false, local_first: true },
          active_execution_profile: "hybrid",
          execution_profiles: agentSystems.profiles,
          role_bindings: roleBindings,
          instruction_policy: "Role-specific and Villani-controlled",
          advanced_process_timeouts: [],
        },
      });
    if (path === "/v1/console/home")
      return route.fulfill({
        json: {
          schema_version: "villani.console.home.v1",
          service: { status: "running", last_error: null },
          models: [],
          recent_runs: [],
          recent_sessions: [entry],
          accepted_task_rate: null,
          recent_recovery_events: [],
          pending_synchronization: 0,
          setup_issues: [],
          warnings: [],
        },
      });
    if (path === "/v1/console/history")
      return route.fulfill({
        json: {
          schema_version: "villani.console.history.v1",
          entries: [entry],
          warnings: [],
        },
      });
    if (path === "/v1/console/policies")
      return route.fulfill({
        json: {
          schema_version: "villani.console.policies.v1",
          active_preset: "balanced",
          presets: [
            {
              id: "balanced",
              label: "Balanced",
              description: "Balanced local verification",
              active: true,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
          ],
          setup_issues: [],
        },
      });
    if (path === "/v1/console/sessions/session_1")
      return route.fulfill({
        json: {
          schema_version: "villani.console.replay.v1",
          id: "session_1",
          logical_id: "session_1",
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
            started_at: entry.started_at,
            completed_at: entry.updated_at,
            duration_ms: entry.duration_ms,
            total_tokens: null,
            total_cost: null,
            currency: null,
            terminal_reason: null,
          },
          events: [
            {
              id: "event_1",
              sequence: 1,
              timestamp: entry.started_at,
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
              deep_link: "/console/sessions/session_1/events/event_1",
            },
          ],
          attempts: [],
          evidence: {},
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
          deep_links: { self: entry.deep_link, history: "/console/history" },
        },
      });
    if (path.startsWith("/v1/console/workspace/"))
      return route.fulfill({
        json: {
          connected,
          workspace_id: "workspace_1",
          surface: path.split("/").at(-1),
          items: [],
          message: "Connected workspace data.",
        },
      });
    return route.fulfill({ status: 404, json: { error: "not_found" } });
  });
}

test("every local main route uses one Console shell", async ({ page }) => {
  await mockConsole(page);
  const routes: [string, string][] = [
    ["/console", "What would you like Villani to change?"],
    ["/console/activity", "Activity"],
    ["/console/agents", "Agents"],
    ["/console/settings", "Settings"],
    ["/console/replay", "Replay"],
    ["/console/models", "Models"],
    ["/console/policies", "Policies"],
    ["/console/onboarding", "Set up Villani"],
  ];
  for (const [route, heading] of routes) {
    await page.goto(route);
    await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
    await expect(page.getByTestId("shared-app-shell")).toHaveCount(1);
    await expect(page.getByTestId("team-navigation")).toHaveCount(0);
    await expect(page.getByTestId("actionable-system-notice")).toHaveCount(0);
  }
  await page.goto("/console");
  const navigation = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(navigation.getByRole("link")).toHaveCount(4);
  for (const name of ["New task", "Activity", "Agents", "Settings"])
    await expect(navigation.getByRole("link", { name })).toBeVisible();
});

test("M7 Agents shows role-ready CLI systems and actionable doctor evidence", async ({
  page,
}) => {
  await mockConsole(page);
  await page.goto("/console/agents");
  await expect(page.getByRole("heading", { name: "Agents", level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Codex CLI" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Claude Code" })).toBeVisible();
  await expect(page.getByText("codex-user-model")).toBeVisible();
  await expect(page.getByText("claude-user-model")).toBeVisible();
  await expect(page.getByText("Understand task").first()).toBeVisible();
  await expect(page.getByText("Write code").first()).toBeVisible();
  await expect(page.getByText("Verify result").first()).toBeVisible();
  await expect(page.getByText("Choose candidate").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Run doctor" })).toHaveCount(2);
  await expect(page.getByText(/quota/i)).toHaveCount(0);
});

test("M7 Settings supports keyboard-accessible profile role binding", async ({
  page,
}) => {
  await mockConsole(page);
  await page.goto("/console/settings");
  await expect(page.getByRole("heading", { name: "Execution profile" })).toBeVisible();
  const verification = page.getByLabel("Verify result agent system");
  await verification.focus();
  await expect(verification).toBeFocused();
  const request = page.waitForRequest(
    (value) =>
      value.url().endsWith("/v1/console/profiles:set-role") &&
      value.method() === "POST",
  );
  await verification.selectOption("codex-cli");
  expect((await request).postDataJSON()).toEqual({
    profile_id: "hybrid",
    role: "verification",
    agent_system_id: "codex-cli",
  });
});

test("M7 one-run profile override remains inside optional task details", async ({
  page,
}) => {
  await mockConsole(page);
  await page.goto("/console");
  const profile = page.getByLabel("Execution profile (Advanced)");
  await expect(profile).toBeHidden();
  await page.getByText("Details (optional)").click();
  await expect(profile).toBeVisible();
  await expect(profile).toHaveValue("hybrid");
});

test("advanced deep routes remain reachable without entering default navigation", async ({
  page,
}) => {
  await mockConsole(page, true);
  for (const name of ["Fleet", "Tasks", "Costs", "Alerts", "Audit"]) {
    await page.goto(`/console/${name.toLowerCase()}`);
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
    await expect(page.getByTestId("team-navigation")).toHaveCount(0);
    await expect(
      page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", {
        name,
      }),
    ).toHaveCount(0);
  }
  await page.goto("/console/settings#advanced");
  for (const name of ["Fleet", "Tasks", "Costs", "Alerts", "Audit"])
    await expect(page.getByRole("link", { name })).toBeVisible();
});

test("Replay is embedded, deep-linked, accessible, and keyboard reachable", async ({
  page,
}) => {
  await mockConsole(page);
  await page.goto("/console/sessions/session_1/events/event_1");
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
    await expect(page.getByRole("heading", { name })).toBeVisible();
  await expect(page.getByTestId("deep-link-target")).toBeVisible();
  await page.locator("body").press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "New task" })).toBeFocused();
  const ids = await page
    .locator("[id]")
    .evaluateAll((nodes) => nodes.map((node) => node.id));
  expect(new Set(ids).size).toBe(ids.length);
  await expect(
    page.getByRole("navigation", { name: "Primary navigation" }),
  ).toBeVisible();
  const unnamedControls = await page
    .locator("a,button,input,select,textarea")
    .evaluateAll((nodes) =>
      nodes
        .filter((node) => {
          const element = node as HTMLElement;
          const labels =
            "labels" in element ? (element as HTMLInputElement).labels : null;
          return !(
            element.getAttribute("aria-label") ||
            element.getAttribute("aria-labelledby") ||
            element.textContent?.trim() ||
            labels?.length
          );
        })
        .map((node) => node.outerHTML),
    );
  expect(unnamedControls).toEqual([]);
});

test("legacy Web and replay routes migrate without a dead link", async ({ page }) => {
  await mockConsole(page);
  await page.goto("/console/run");
  await expect(page).toHaveURL(/\/console$/);
  await expect(
    page.getByRole("heading", {
      name: "What would you like Villani to change?",
      level: 1,
    }),
  ).toBeVisible();
  await page.goto("/console/history");
  await expect(page).toHaveURL(/\/console\/activity$/);
  await expect(page.getByRole("heading", { name: "Activity", level: 1 })).toBeVisible();
  await page.goto("/flight/sessions/session_1/events/event_1");
  await expect(page).toHaveURL(/\/console\/sessions\/session_1\/events\/event_1$/);
  await expect(page.getByTestId("console-replay")).toBeVisible();
});
