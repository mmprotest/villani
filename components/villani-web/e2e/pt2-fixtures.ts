import type { Page } from "@playwright/test";
import type { ProductRun, ProductRunAction, ProductVerdict } from "@villani/run-model";

const stageSequence = ["Understanding", "Working", "Checking", "Ready"] as const;
const stageSentences = {
  Understanding: "Understanding the task and choosing a safe route.",
  Working: "The agent system is working in an isolated repository copy.",
  Checking: "Verification is checking the change and its evidence.",
  Ready: "The proved change is ready for your decision.",
} as const;
const stageSequences = { Understanding: 1, Working: 4, Checking: 8, Ready: 12 };

export type Pt2FixtureOptions = {
  products?: ProductRun[];
  deliveryResult?: ProductRun;
  cancelResult?: ProductRun;
  dirtyRepository?: boolean;
  unavailableAgent?: boolean;
  validationUnavailable?: boolean;
};

export type Pt2Fixture = {
  advance: () => void;
  submissions: Array<Record<string, unknown>>;
  approvals: Array<Record<string, unknown>>;
  getCurrent: () => ProductRun;
};

function action(
  id: ProductRunAction["id"],
  label: string,
  method: ProductRunAction["method"],
  href: string,
): ProductRunAction {
  return { id, label, method, href };
}

export function productRun(
  options: {
    stage?: (typeof stageSequence)[number];
    verdict?: ProductVerdict | null;
    reason?: string | null;
    sentence?: string;
    actions?: ProductRunAction[];
    cost?: number | null;
    costStatus?: ProductRun["cost"]["accounting_status"];
    targetModified?: boolean;
    targetStatement?: string;
    changedFiles?: string[];
  } = {},
): ProductRun {
  const stage = options.stage ?? "Ready";
  const verdict = options.verdict === undefined ? "Ready to apply" : options.verdict;
  const ready = verdict === "Ready to apply";
  const changedFiles = options.changedFiles ?? ["src/parser.ts", "test/parser.test.ts"];
  const currentIndex = stageSequence.indexOf(stage);
  const transitions = stageSequence.slice(0, currentIndex + 1).map((item) => ({
    sequence: stageSequences[item],
    timestamp: `2026-07-17T00:00:${String(stageSequences[item]).padStart(2, "0")}Z`,
    stage: item,
    sentence: stageSentences[item],
  }));
  const cost =
    options.cost === undefined ? (verdict === null ? null : 0.17) : options.cost;
  const costStatus = options.costStatus ?? (cost === null ? "unknown" : "complete");
  const targetModified = options.targetModified ?? false;
  return {
    schema_version: "villani.product_run.v1",
    run_identity: { run_id: "run_pt2_fixture", trace_id: "trace_pt2_fixture" },
    task_summary: {
      task: "Fix repeated separators in the parser.\nPreserve multiline text.",
      success_criteria: "The repository checks pass.",
      repository: "C:/work/sample-app",
    },
    current_stage: stage,
    stage_sentence: options.sentence ?? stageSentences[stage],
    stage_transitions: transitions,
    final_verdict: verdict,
    verdict_reason:
      options.reason === undefined
        ? ready
          ? "Verification proved the selected change acceptable."
          : null
        : options.reason,
    change_summary:
      changedFiles.length === 0
        ? "No file changes were recorded."
        : `${changedFiles.length} files changed in the selected candidate.`,
    changed_files: changedFiles,
    checks_summary:
      verdict === null
        ? {
            passed: null,
            failed: null,
            not_run: null,
            unavailable: null,
            accounting_status: "unknown",
          }
        : {
            passed: ready ? 18 : 0,
            failed: 0,
            not_run: ready ? 0 : 1,
            unavailable: 0,
            accounting_status: "complete",
          },
    requirement_summary:
      verdict === null
        ? { proved: null, not_proved: null, accounting_status: "unknown" }
        : {
            proved: ready ? 2 : 0,
            not_proved: ready ? 0 : 1,
            accounting_status: "complete",
          },
    cost: {
      value: cost,
      currency: cost === null ? null : "USD",
      accounting_status: costStatus,
    },
    duration: {
      value_ms: stageSequences[stage] * 1_000,
      accounting_status: verdict === null ? "partial" : "complete",
    },
    agent_system: {
      name: "Villani agent system",
      backend: stage === "Understanding" ? null : "strong-local-agent",
      model: stage === "Understanding" ? null : "qualified-model",
    },
    escalation_summary: {
      attempts: stage === "Understanding" ? 0 : 1,
      retries: options.sentence?.includes("Retrying") ? 1 : 0,
      escalations: options.sentence?.includes("stronger") ? 1 : 0,
      summary: options.sentence?.includes("Retrying")
        ? options.sentence
        : "No retry or escalation was needed.",
    },
    available_actions:
      options.actions ??
      (verdict === null
        ? [
            action(
              "cancel",
              "Cancel",
              "POST",
              "/v1/console/runs/run_pt2_fixture/cancel",
            ),
          ]
        : ready
          ? [
              action(
                "apply_change",
                "Apply change",
                "POST",
                "/v1/console/runs/run_pt2_fixture/approval",
              ),
              action(
                "review_evidence",
                "Review evidence",
                "GET",
                "/console/runs/run_pt2_fixture/replay",
              ),
            ]
          : [
              action("retry", "Start again", "GET", "/console"),
              action(
                "review_evidence",
                "Review evidence",
                "GET",
                "/console/runs/run_pt2_fixture/replay",
              ),
            ]),
    evidence_links: [
      {
        label: "Recorded evidence",
        href: "/console/runs/run_pt2_fixture/replay",
        artifact: "events.jsonl",
      },
    ],
    recovery_action:
      verdict !== null && !ready
        ? {
            label: "Start again",
            instruction:
              "Review the recorded evidence, resolve the stated issue, then start again.",
            href: "/console",
          }
        : null,
    technical_detail_references: ["events.jsonl", "verification/attempt_001.json"],
    target_repository: {
      modified: targetModified,
      accounting_status: "known",
      statement:
        options.targetStatement ??
        (targetModified
          ? "The target repository was modified."
          : "The target repository was not modified."),
    },
    last_event_sequence: stageSequences[stage],
    updated_at: "2026-07-17T00:00:12Z",
  };
}

function publicFailure(code: string, whatFailed: string, nextAction: string) {
  return {
    code,
    what_failed: whatFailed,
    what_villani_tried:
      "Villani checked the local configuration before creating a run.",
    attempts_recorded: 0,
    missing_evidence: "No usable agent response was recorded.",
    patch_preserved: false,
    patch_status: "No run was started. The target repository was not modified.",
    next_action: nextAction,
  };
}

export async function mockPt2Console(
  page: Page,
  options: Pt2FixtureOptions = {},
): Promise<Pt2Fixture> {
  const products = options.products ?? [productRun()];
  let productIndex = 0;
  let permits = 0;
  const waiters: Array<() => void> = [];
  const submissions: Array<Record<string, unknown>> = [];
  const approvals: Array<Record<string, unknown>> = [];
  const advance = () => {
    const waiter = waiters.shift();
    if (waiter) waiter();
    else permits += 1;
  };
  const waitForAdvance = async () => {
    if (permits > 0) {
      permits -= 1;
      return;
    }
    await new Promise<void>((resolve) => waiters.push(resolve));
  };

  await page.route("**/v1/console/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/v1/console/bootstrap")
      return route.fulfill({
        json: {
          schema_version: "villani.console.bootstrap.v1",
          mode: "local",
          data_source: "local-service",
          version: "0.3.0",
          workspace: { connected: false, id: null, endpoint: null },
          service: {
            status: "running",
            started_at: null,
            log_path: "service.log",
            last_error: null,
          },
          setup: { configured: true, valid: true, schema_version: 1, issues: [] },
          synchronization: { pending: 0, dead_letters: 0 },
          storage: { home: "home", runs: "runs", spool: "spool", writable: true },
          models: [
            {
              id: "strong-local-agent",
              backend_name: "strong-local-agent",
              display_name: "Strong local agent",
              model: "qualified-model",
              provider: "local",
              endpoint: "http://127.0.0.1:1234/v1",
              configured: true,
              detected: true,
              availability: options.unavailableAgent ? "unavailable" : "available",
              available: !options.unavailableAgent,
              tool_support: "unknown",
              context_metadata: {},
              configured_roles: ["classification", "coding"],
              capability: "QUALIFIED",
              capability_status: "QUALIFIED",
              context_window: null,
              pricing_status: "unknown",
              currency: "USD",
              observed_task_count: 12,
              observed_success_rate: null,
              observed_cost_per_accepted_task: null,
              bootstrap_default: true,
              manual_override: false,
              manual_override_label: null,
              last_tested_at: "2026-07-17T00:00:00Z",
              last_test_diagnostic: null,
              capability_policy_version: "villani-model-lifecycle-v1",
            },
          ],
          active_policy: "performance",
        },
      });
    if (path === "/v1/console/run-options")
      return route.fulfill({
        json: {
          schema_version: "villani.console.run_options.v1",
          repositories: [
            {
              path: "C:/work/sample-app",
              name: "sample-app",
              valid: true,
              dirty: options.dirtyRepository ?? false,
              source: "setup",
            },
          ],
          default_repository: "C:/work/sample-app",
          delivery_modes: [
            {
              id: "approve",
              label: "Apply with approval",
              description: "Review first",
            },
            {
              id: "branch",
              label: "Create local branch",
              description: "Separate branch",
            },
            {
              id: "pull-request",
              label: "Create pull request",
              description: "Open review",
            },
            { id: "suggest", label: "Suggest", description: "Preserve patch" },
          ],
          approval_modes: [],
          policies: [],
          policy_presets: [
            {
              id: "performance",
              label: "Performance",
              description: "Use the strongest eligible agent system.",
              active: true,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
          ],
          advanced_policies: [
            { id: "configured", label: "Configured policy", description: "Configured" },
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
          setup_issues: [],
        },
      });
    if (path === "/v1/console/validation:discover")
      return route.fulfill({
        json: {
          schema_version: "villani.console.validation_discovery.v1",
          repository: {
            path: "C:/work/sample-app",
            name: "sample-app",
            valid: true,
            dirty: options.dirtyRepository ?? false,
            source: "setup",
          },
          repository_fingerprint: "pt2-deterministic-fingerprint",
          suggestions: options.validationUnavailable
            ? []
            : [
                {
                  suggestion_id: "npm-test",
                  argv: ["npm", "test"],
                  display_command: "npm test",
                  confidence: 0.98,
                  confidence_label: "high",
                  requires_confirmation: false,
                  reason: "Detected package test script.",
                  source: "package_script",
                  advisory_only: true,
                  authoritative: false,
                },
              ],
          selected_suggestion_id: options.validationUnavailable ? null : "npm-test",
          authority: "none_until_confirmed_command_execution",
          failure: options.validationUnavailable
            ? publicFailure(
                "validation_unavailable",
                "Repository validation is unavailable.",
                "Add a check under Details, or continue knowing alternative evidence is required.",
              )
            : null,
        },
      });
    if (path === "/v1/console/policy:preview")
      return route.fulfill({
        json: {
          schema_version: "villani.policy_preview.v1",
          raw_classification: { difficulty: "medium", risk: "low", confidence: 0.9 },
          effective_classification: {
            difficulty: "medium",
            risk: "low",
            confidence: 0.9,
          },
          adjustments: [],
          eligible_models: [{ backend_name: "strong-local-agent" }],
          excluded_models: [],
          selected_coding_route: {
            backend: "strong-local-agent",
            model: "qualified-model",
            action: "attempt",
            reason: "Strongest eligible route.",
            route_provenance: { basis: "qualification" },
          },
          selected_verifier_route: {
            selected: { route: "verification", authority: "acceptance" },
          },
          estimated_cost: { value: null, status: "unknown", currency: "USD" },
          uncertainty: ["Cost is unknown."],
          policy_version: { public: "v1", preset: "performance", controller: "v1" },
          coding_attempt_executed: false,
        },
      });
    if (path === "/v1/console/runs" && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      submissions.push(body);
      if (options.unavailableAgent)
        return route.fulfill({
          json: {
            schema_version: "villani.console.run_submission.v1",
            status: "FAILED",
            run_id: null,
            failure: publicFailure(
              "no_usable_agent",
              "No usable agent system is available.",
              "Open Settings > Agents, connect one usable agent system, then try again.",
            ),
          },
        });
      return route.fulfill({
        status: 202,
        json: {
          schema_version: "villani.console.run_submission.v1",
          status: "QUEUED",
          run_id: "run_pt2_fixture",
          run_url: "/console?run=run_pt2_fixture",
          replay_url: "/console/runs/run_pt2_fixture",
          validation_commands: options.validationUnavailable ? [] : ["npm test"],
          deduplicated: false,
          failure: null,
        },
      });
    }
    if (path.endsWith("/status"))
      return route.fulfill({ json: products[productIndex] });
    if (path.endsWith("/events")) {
      await waitForAdvance();
      productIndex = Math.min(productIndex + 1, products.length - 1);
      return route.fulfill({ json: products[productIndex] });
    }
    if (path.endsWith("/cancel") && request.method() === "POST")
      return route.fulfill({
        json:
          options.cancelResult ??
          productRun({
            verdict: "Cancelled",
            reason:
              "The task was cancelled safely and recorded evidence was preserved.",
            sentence: "The task was cancelled safely.",
            actions: [action("retry", "Start again", "GET", "/console")],
          }),
      });
    if (path.endsWith("/approval") && request.method() === "POST") {
      approvals.push(request.postDataJSON() as Record<string, unknown>);
      return route.fulfill({ json: options.deliveryResult ?? productRun() });
    }
    return route.fulfill({
      status: 404,
      json: { message: `No PT2 fixture for ${path}` },
    });
  });

  return {
    advance,
    submissions,
    approvals,
    getCurrent: () => products[productIndex],
  };
}

export const pt2Action = action;
