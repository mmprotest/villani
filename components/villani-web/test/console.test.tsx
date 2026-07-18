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
  AgentSystemCapabilityAssessment,
  AgentSystemIdentity,
  ConsoleBootstrap,
  ConsoleHistoryEntry,
  ConsoleReplaySnapshot,
  EconomicsProfile,
  ProductRun,
  QualificationAssessment,
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

const unknownCapability: AgentSystemCapabilityAssessment = {
  state: "unknown",
  evidence: [],
  notes: null,
};

const agentSystem: AgentSystemIdentity = {
  schema_version: "villani.agent_system.v1",
  system_id: "asys_44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  route_name: "default",
  production_enabled: true,
  qualification_status: "qualified",
  harness: {
    harness_id: "villani-code",
    display_name: "Villani Code",
    version: "0.1.0rc1",
    adapter_id: "villani-code-attempt",
    adapter_version: "1.0.0",
    protocol: "villani.harness_adapter.v1",
    protocol_version: "1.0.0",
    transport: "structured_headless_cli",
    executable_digest: null,
  },
  model_provider: {
    provider: "local",
    model_id: "local-model",
    model_revision: null,
    endpoint_identity: "http://127.0.0.1:1234/v1",
    serving_engine: null,
    serving_engine_version: null,
    context_metadata: { authoritative: false },
    tool_metadata: { authoritative: false },
  },
  execution: {
    execution_provider: "inherit",
    environment_fingerprint: null,
    permission_profile: "workspace-write",
    network_policy: "restricted",
    sandbox_identity: null,
  },
  route_profile: {
    repository_profile: "generic",
    task_profile: "generic",
    verification_policy: "acceptance-grade",
    tool_protocol: "villani-code-tools-v1",
    prompt_protocol: "villani-code-prompt-v1",
  },
  capabilities: {
    file_editing: {
      state: "supported",
      evidence: [
        {
          source: "conformance_tested",
          reference: "fixture:patch-correctness",
          observed_at: "2026-07-18T00:00:00Z",
          digest: null,
        },
      ],
      notes: null,
    },
    command_execution: unknownCapability,
    streaming: unknownCapability,
    cancellation: unknownCapability,
    usage_reporting: unknownCapability,
    cost_reporting: unknownCapability,
    model_identity: unknownCapability,
    session_identity: unknownCapability,
    resume: unknownCapability,
    fork: unknownCapability,
    permission_requests: unknownCapability,
    custom_model: unknownCapability,
    custom_provider: unknownCapability,
    local_model: unknownCapability,
    mcp: unknownCapability,
    acp: {
      state: "unsupported",
      evidence: [
        {
          source: "unsupported",
          reference: "fixture:transport",
          observed_at: "2026-07-18T00:00:00Z",
          digest: null,
        },
      ],
      notes: null,
    },
    structured_result: unknownCapability,
    complete_trace: unknownCapability,
    isolated_worktree: unknownCapability,
    non_interactive_execution: unknownCapability,
  },
  qualification_references: [
    { kind: "conformance", reference: "fixture", digest: null },
  ],
  billing: {
    mode: "unknown",
    cost_source: null,
    currency: null,
    unknown_fields: ["cost"],
  },
  detection_time: "2026-07-18T00:00:00Z",
  detection_source: "configuration_migration",
  configuration_digest:
    "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  configuration: {},
  redaction_status: "redacted",
  unknown_fields: ["model_revision", "environment_fingerprint"],
};

const repositoryQualification: QualificationAssessment = {
  schema_version: "villani.qualification_assessment.v1",
  policy_version: "repository_qualification_v1",
  system_id: agentSystem.system_id,
  route_name: agentSystem.route_name,
  repository_id: "repo_fixture",
  repository_head: "a".repeat(40),
  task_profile: {
    category: "*",
    difficulty: "easy",
    risk: "low",
    required_capabilities: [],
  },
  state: "qualified",
  selected_level: "repository_wide",
  selected_cohort: null,
  task_wilson_threshold: 0.6,
  statistics: {
    sample_count: 20,
    successes: 20,
    failures: 0,
    exclusions: { provider_outage: 1 },
    acceptance_rate: 1,
    wilson_lower_bound: 0.8389,
    proved_acceptable_count: 20,
    accepted_as_is_count: 20,
    false_acceptance_count: 0,
    false_rejection_count: 0,
    false_case_ids: [],
    cost_distribution_by_currency: {},
    cost_unknown_count: 20,
    accepted_change_cost_by_currency: {},
    accepted_change_cost_unknown_count: 20,
    duration_distribution: {
      known_count: 20,
      unknown_count: 0,
      minimum: 900,
      median: 1000,
      p90: 1100,
      maximum: 1200,
      unit: "ms",
    },
    review_minutes_distribution: {
      known_count: 20,
      unknown_count: 0,
      minimum: 1,
      median: 2,
      p90: 3,
      maximum: 4,
      unit: "minutes",
    },
    last_evidence_at: "2026-07-18T00:00:00Z",
    software_version_diversity: { harness: ["0.1.0rc1"] },
    drift_flags: [],
  },
  backoff_evidence: [
    {
      level: "repository_wide",
      repository_ids: ["repo_fixture"],
      cohort: null,
      eligible_observation_count: 20,
      selected: true,
      approved_for_qualification: true,
      rejection_reasons: [],
    },
  ],
  automatic_eligible: true,
  provisional_fallback_eligible: false,
  manual_override_required: false,
  unsupported_reasons: [],
  caveat: "Qualified from 20 eligible repository observations.",
  doctor_action: "villani agents doctor default",
  evidence_action: "villani agents evidence default --repo C:/repo",
  evaluated_at: "2026-07-18T00:00:00Z",
};

const repositoryEconomics: EconomicsProfile = {
  key: {
    repository_id: "repo_fixture",
    task_profile: repositoryQualification.task_profile,
    system_id: agentSystem.system_id,
    system_identity_digest: "sha256:" + "5".repeat(64),
    route_name: agentSystem.route_name,
  },
  observation_ids: ["eobs_" + "7".repeat(64)],
  sample_count: 1,
  successes: 1,
  failures: 0,
  exclusions: {},
  cost_distributions: {
    execution_cost: {
      USD: {
        known_count: 1,
        unknown_count: 0,
        minimum: 1.25,
        median: 1.25,
        p90: 1.25,
        maximum: 1.25,
        unit: "USD",
      },
    },
    verification_cost: {},
    human_review_cost: {},
    retry_escalation_cost: {},
  },
  cost_unknown_counts: {
    execution_cost: 0,
    verification_cost: 1,
    human_review_cost: 1,
    retry_escalation_cost: 1,
  },
  duration_distribution: {
    known_count: 1,
    unknown_count: 0,
    minimum: 1000,
    median: 1000,
    p90: 1000,
    maximum: 1000,
    unit: "ms",
  },
  review_minutes_distribution: {
    known_count: 0,
    unknown_count: 1,
    minimum: null,
    median: null,
    p90: null,
    maximum: null,
    unit: "minutes",
  },
  attempt_count_distribution: {
    known_count: 1,
    unknown_count: 0,
    minimum: 1,
    median: 1,
    p90: 1,
    maximum: 1,
    unit: "count",
  },
  escalation_count_distribution: {
    known_count: 1,
    unknown_count: 0,
    minimum: 0,
    median: 0,
    p90: 0,
    maximum: 0,
    unit: "count",
  },
  false_acceptance_count: 0,
  last_evidence_at: "2026-07-18T00:00:00Z",
  source_digest: "sha256:" + "8".repeat(64),
};

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

function productRun(
  options: { awaiting?: boolean; applied?: boolean } = {},
): ProductRun {
  const action = options.awaiting
    ? [
        {
          id: "apply_change" as const,
          label: "Apply change",
          method: "POST" as const,
          href: "/v1/console/runs/run_new/approval",
        },
      ]
    : [];
  return {
    schema_version: "villani.product_run.v1",
    run_identity: { run_id: "run_new", trace_id: "trace_new" },
    task_summary: {
      task: "Fix repeated separators in the parser.",
      success_criteria: "The repository test passes.",
      repository: "C:/repo",
    },
    current_stage: "Ready",
    stage_sentence: "Verification proved the selected change acceptable.",
    stage_transitions: [
      {
        sequence: 1,
        timestamp: "2026-07-14T00:00:00Z",
        stage: "Understanding",
        sentence: "Understanding the task and choosing a safe route.",
      },
      {
        sequence: 12,
        timestamp: "2026-07-14T00:01:00Z",
        stage: "Ready",
        sentence: "The proved change is ready for your decision.",
      },
    ],
    final_verdict: "Ready to apply",
    verdict_reason: "Verification proved the selected change acceptable.",
    change_summary: "2 files changed in the selected candidate.",
    changed_files: ["src/parser.ts", "test/parser.test.ts"],
    checks_summary: {
      passed: 18,
      failed: 0,
      not_run: 0,
      unavailable: 0,
      accounting_status: "complete",
    },
    requirement_summary: {
      proved: 2,
      not_proved: 0,
      accounting_status: "complete",
    },
    cost: { value: 0.17, currency: "USD", accounting_status: "complete" },
    duration: { value_ms: 60_000, accounting_status: "complete" },
    agent_system: {
      name: "Villani agent system",
      backend: "default",
      model: "local-model",
    },
    escalation_summary: {
      attempts: 1,
      retries: 0,
      escalations: 0,
      summary: "No retry or escalation was needed.",
    },
    available_actions: [
      ...action,
      {
        id: "review_evidence",
        label: "Review evidence",
        method: "GET",
        href: "/console/runs/run_new/replay",
      },
    ],
    evidence_links: [
      {
        label: "Recorded evidence",
        href: "/console/runs/run_new/replay",
        artifact: "events.jsonl",
      },
    ],
    recovery_action: null,
    technical_detail_references: ["events.jsonl", "verification/attempt_001.json"],
    target_repository: {
      modified: options.applied === true,
      accounting_status: "known",
      statement: options.applied
        ? "The target repository was modified."
        : "The target repository was not modified.",
    },
    proof_package: {
      status: "ready_to_apply",
      risk_tier: "standard",
      why_villani_trusts_it:
        "Repository checks and semantic verification proved every requirement.",
      unresolved_decision: null,
      artifact: "verification/attempt_001-review-package.json",
    },
    last_event_sequence: 12,
    updated_at: "2026-07-14T00:01:00Z",
  };
}

function mockConsole(
  connected = false,
  awaitingApproval = false,
  options: {
    environment?: ConsoleBootstrap;
    activity?: ConsoleHistoryEntry[];
    dirtyRepository?: boolean;
    runOptionsError?: boolean;
    statusProduct?: ProductRun;
    eventProduct?: ProductRun;
    cancelProduct?: ProductRun;
    agentQualification?: QualificationAssessment | null;
  } = {},
) {
  const environment = options.environment ?? bootstrap(connected);
  let approvalResolved = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/v1/console/bootstrap")) return response(environment);
      if (url.includes("/v1/console/run-options")) {
        if (options.runOptionsError)
          return new Response(JSON.stringify({ detail: "service unavailable" }), {
            status: 503,
            headers: { "Content-Type": "application/json" },
          });
        return response({
          schema_version: "villani.console.run_options.v1",
          repositories: [
            {
              path: "C:/repo",
              name: "repo",
              valid: true,
              dirty: options.dirtyRepository ?? false,
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
              id: "performance",
              label: "Performance",
              description: "Use the strongest eligible agent system.",
              active: true,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
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
            policy_preset: "performance",
            policy_selection: "configured",
            routing_mode: "observe",
            max_attempts: 3,
            max_cost: null,
            max_wall_time: null,
          },
          setup_issues: [],
        });
      }
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
        return response(productRun({ applied: true }));
      }
      if (url.includes("/v1/console/runs/run_new/cancel"))
        return response(options.cancelProduct ?? productRun());
      if (url.includes("/v1/console/runs/run_new/events")) {
        if (options.eventProduct) return response(options.eventProduct);
        if (options.statusProduct)
          return new Promise<Response>(() => {
            // A pending long-lived request models the service event subscription.
          });
      }
      if (url.includes("/v1/console/runs/run_new/status") && options.statusProduct)
        return response(options.statusProduct);
      if (
        url.includes("/v1/console/runs/run_new/status") &&
        awaitingApproval &&
        !approvalResolved
      )
        return response(productRun({ awaiting: true }));
      if (
        url.includes("/v1/console/runs/run_new/status") ||
        url.includes("/v1/console/runs/run_new/events")
      )
        return response(productRun());
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
            checks_not_run: 0,
            checks_unavailable: 0,
            checks_accounting_status: "complete",
            focused_probes_passed: 0,
            focused_probes_failed: 0,
            focused_probes_not_run: 0,
            focused_probes_unavailable: 0,
            focused_probes_accounting_status: "complete",
            requirements_proved: 2,
            requirements_not_proved: 0,
            requirements_verified: 2,
            requirements_accounting_status: "complete",
            authority: "executed_repository_validation",
          },
          canonical_summary: {
            schema_version: "villani.run_summary.v1",
            run_id: "run_new",
            attempt_id: "attempt_001",
            checks: {
              passed: 1,
              failed: 0,
              not_run: 0,
              unavailable: 0,
              accounting_status: "complete",
            },
            focused_probes: {
              passed: 0,
              failed: 0,
              not_run: 0,
              unavailable: 0,
              accounting_status: "complete",
            },
            requirements: {
              proved: 2,
              not_proved: 0,
              accounting_status: "complete",
            },
            accounting: {
              known: true,
              accounting_status: "complete",
              total_cost: 0.17,
              currency: "USD",
            },
            acceptance: {
              decision: true,
              reason_code: "accepted",
              reason: "Acceptance-grade evidence is complete.",
            },
            source_artifacts: ["run-summary.json"],
            generated_at: "2026-07-14T00:01:00Z",
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
          entries: options.activity ?? entries,
          warnings: [],
        });
      if (url.includes("/v1/console/sessions/claude_1")) return response(replay);
      if (url.includes("/v1/console/models:test"))
        return response({
          schema_version: "villani.console.model_test.v1",
          results: [
            {
              backend_name: "default",
              availability: "available",
              diagnostic: "Connection verified.",
              tested_at: "2026-07-17T00:00:00Z",
              model_tokens_used: 0,
            },
          ],
          model_tokens_used: 0,
        });
      if (
        url.includes("/v1/console/models:detect") ||
        url.includes("/v1/console/models:add") ||
        url.includes("/v1/console/models:remove") ||
        url.includes("/v1/console/models:default")
      )
        return response({
          schema_version: "villani.console.models.v1",
          models: environment.models,
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
          models: environment.models,
          agent_systems: [
            {
              ...agentSystem,
              ...(options.agentQualification === null
                ? {}
                : {
                    repository_qualification:
                      options.agentQualification ?? repositoryQualification,
                    repository_economics: {
                      profile: repositoryEconomics,
                      matching_profile_count: 1,
                      scope_note:
                        "Exact repository economics only; no language or framework pooling.",
                    },
                  }),
            },
          ],
          economics: {
            policy_version: "accepted_change_economics_v1",
            objective_version: "total_accepted_change_v1",
            default_explanation:
              "Villani chose the route most likely to produce a proven change at the lowest total cost.",
            unknown_accounting_note:
              "Unknown route inputs remain Unknown and are not treated as zero.",
          },
          bootstrap_default: "default",
          capability_states: [
            "UNRATED",
            "BOOTSTRAP",
            "OBSERVED",
            "QUALIFIED",
            "DISABLED",
          ],
        });
      if (url.includes("/v1/console/settings"))
        return response({
          schema_version: "villani.console.settings.v1",
          setup: environment.setup,
          service: environment.service,
          storage: environment.storage,
          privacy: { secrets_exposed: false, local_first: true },
          synchronization: environment.synchronization,
          workspace: environment.workspace,
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
              id: "performance",
              label: "Performance",
              description: "Use the strongest eligible agent system.",
              active: false,
              advanced: false,
              policy_version: "villani-public-policy-v1",
            },
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
  localStorage.clear();
  sessionStorage.clear();
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
    expect(migrateLegacyPath("/history")).toBe("/console/activity");
    expect(migrateLegacyPath("/console/history")).toBe("/console/activity");
    expect(migrateLegacyPath("/console/run")).toBe("/console");
    expect(migrateLegacyPath("/console/home")).toBe("/console");
  });

  it("renders only the four PT1 navigation destinations and keeps advanced links in Settings", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console/models");
    const view = render(<ConsoleApp />);
    await screen.findByRole("heading", { name: "Models" });
    const navigation = screen.getByTestId("primary-navigation");
    expect(within(navigation).getAllByRole("link")).toHaveLength(4);
    for (const name of ["New task", "Activity", "Agents", "Settings"])
      expect(within(navigation).getByRole("link", { name })).toBeInTheDocument();
    expect(screen.queryByTestId("team-navigation")).not.toBeInTheDocument();
    expect(
      within(navigation).queryByRole("link", { name: "Fleet" }),
    ).not.toBeInTheDocument();
    view.unmount();
    mockConsole(true);
    history.replaceState(null, "", "/console/settings");
    render(<ConsoleApp />);
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.queryByTestId("team-navigation")).not.toBeInTheDocument();
    expect(
      within(screen.getByTestId("advanced-navigation")).getByRole("link", {
        name: /Fleet/,
      }),
    ).toHaveAttribute("href", "/console/fleet");
  });
});

describe("PT1 shell, setup, and accessibility", () => {
  it("makes New task the healthy silent root", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console");
    render(<ConsoleApp />);

    await screen.findByRole("heading", {
      name: "What would you like Villani to change?",
    });
    expect(screen.queryByTestId("actionable-system-notice")).not.toBeInTheDocument();
    expect(screen.queryByText(/SERVICE \/ RUNNING/)).not.toBeInTheDocument();
  });

  it("shows one actionable notice when setup or the service needs attention", async () => {
    const incomplete: ConsoleBootstrap = {
      ...bootstrap(false),
      setup: {
        configured: true,
        valid: false,
        schema_version: 1,
        issues: ["Agent verification is incomplete."],
      },
    };
    mockConsole(false, false, { environment: incomplete });
    history.replaceState(null, "", "/console");
    const first = render(<ConsoleApp />);
    expect(await screen.findByTestId("actionable-system-notice")).toHaveTextContent(
      "Finish setup",
    );
    expect(screen.getByRole("link", { name: "Continue setup" })).toHaveAttribute(
      "href",
      "/console/onboarding",
    );
    first.unmount();

    const stopped: ConsoleBootstrap = {
      ...bootstrap(false),
      service: {
        ...bootstrap(false).service,
        status: "stopped",
        last_error: "The local service stopped unexpectedly.",
      },
    };
    mockConsole(false, false, { environment: stopped });
    render(<ConsoleApp />);
    expect(await screen.findByTestId("actionable-system-notice")).toHaveTextContent(
      "Villani service is unavailable",
    );
    expect(screen.getByRole("link", { name: "View recovery" })).toHaveAttribute(
      "href",
      "/console/settings#service",
    );
  });

  it("resumes onboarding at the saved stage and opens New task with the repository", async () => {
    const incomplete: ConsoleBootstrap = {
      ...bootstrap(false),
      setup: {
        configured: true,
        valid: false,
        schema_version: 1,
        issues: ["Verification is incomplete."],
      },
    };
    localStorage.setItem(
      "villani.onboarding.v1",
      JSON.stringify({ stage: 1, repository: "C:/repo", backend: "default" }),
    );
    mockConsole(false, false, { environment: incomplete });
    history.replaceState(null, "", "/console/onboarding");
    const first = render(<ConsoleApp />);
    await screen.findByRole("heading", {
      name: "Which agent system should Villani use?",
    });
    expect(screen.getByLabelText("Agent system")).toHaveValue("default");
    fireEvent.click(screen.getByRole("button", { name: "Use this agent" }));
    await screen.findByRole("heading", { name: "Verify the agent connection" });
    first.unmount();

    render(<ConsoleApp />);
    await screen.findByRole("heading", { name: "Verify the agent connection" });
    fireEvent.click(screen.getByRole("button", { name: "Verify connection" }));
    await screen.findByTestId("setup-complete");
    expect(screen.getByRole("link", { name: "Open New task" })).toHaveAttribute(
      "href",
      "/console?repository=C%3A%2Frepo",
    );
  });

  it("associates repository errors with the labelled form control", async () => {
    mockConsole(false, false, { dirtyRepository: true });
    history.replaceState(null, "", "/console");
    render(<ConsoleApp />);
    const repository = await screen.findByLabelText(/^Repository/);
    const error = screen.getByText(
      "Commit or stash existing changes before starting a task.",
    );
    expect(repository).toHaveAttribute("aria-invalid", "true");
    expect(repository.getAttribute("aria-describedby")).toContain(error.id);
    expect(screen.getByLabelText(/^Task/)).toHaveAttribute("required");
  });

  it("links empty Activity to New task and presents configured Agents as systems", async () => {
    mockConsole(false, false, { activity: [] });
    history.replaceState(null, "", "/console/activity");
    const activity = render(<ConsoleApp />);
    await screen.findByRole("heading", { name: "No activity yet" });
    expect(screen.getByRole("link", { name: "Open New task" })).toHaveAttribute(
      "href",
      "/console",
    );
    activity.unmount();

    mockConsole(false);
    history.replaceState(null, "", "/console/agents");
    render(<ConsoleApp />);
    await screen.findByRole("heading", { name: "Agents" });
    expect(
      await screen.findByRole("heading", { name: "Villani Code · local-model" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Eligible for automatic selection")).toBeInTheDocument();
    expect(screen.getByText("Complete system identity")).toBeInTheDocument();
    expect(screen.getByText("View evidence")).toBeInTheDocument();
    expect(screen.getByText("QUALIFIED")).toBeInTheDocument();
    expect(screen.getByText("qualified")).toBeInTheDocument();
    expect(screen.getByText("100.0%")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Villani chose the route most likely to produce a proven change at the lowest total cost.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("USD 1.2500")).toBeInTheDocument();
    expect(screen.getByText("1 eligible / 1 task profile(s)")).toBeInTheDocument();
    expect(screen.getAllByText("0.839")).toHaveLength(2);
    expect(screen.getAllByText("Unknown").length).toBeGreaterThan(0);
  });

  it("does not invent an Agents qualification when repository context is absent", async () => {
    mockConsole(false, false, { agentQualification: null });
    history.replaceState(null, "", "/console/agents");
    render(<ConsoleApp />);
    await screen.findByRole("heading", { name: "Agents" });
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Repository context is unavailable; no qualification is implied.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("SELECTABLE")).not.toBeInTheDocument();
    expect(screen.getAllByText("Unknown").length).toBeGreaterThan(0);
  });
});

describe("Activity", () => {
  it("shows Villani tasks and imported sessions once with outcome-first columns", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console/activity");
    render(<ConsoleApp />);
    const table = await screen.findByTestId("merged-history");
    expect(
      within(table).getAllByRole("link", { name: /Duplicate|Fix parser/ }),
    ).toHaveLength(1);
    expect(
      within(table).getByRole("link", { name: "Imported task" }),
    ).toBeInTheDocument();
    expect(within(table).getByText("IMPORTED")).toBeInTheDocument();
    expect(within(table).getByText("claude-model")).toBeInTheDocument();
    expect(within(table).getAllByText("1.0 min")).toHaveLength(2);
    expect(within(table).getByText("USD 0.1000")).toBeInTheDocument();
    expect(within(table).getByText("Unknown (unknown)")).toBeInTheDocument();
    for (const heading of [
      "Task",
      "Result",
      "Repository",
      "Elapsed time",
      "Known cost",
      "Agent system",
      "Next action",
    ])
      expect(
        within(table).getByRole("columnheader", { name: heading }),
      ).toBeInTheDocument();
    expect(screen.getByText("Advanced filters").closest("details")).not.toHaveAttribute(
      "open",
    );
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
      "Performance",
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
  it("restores an unsent multiline draft exactly", async () => {
    const task = "Keep this first line.\n\n  Preserve the indented line.\n";
    localStorage.setItem(
      "villani.new-task.draft.v1",
      JSON.stringify({
        repository: "C:/repo",
        task,
        successCriteria: "",
        referenceText: "ISSUE-42\nsecond line",
        manualValidation: "",
      }),
    );
    mockConsole(false);
    history.replaceState(null, "", "/console");
    render(<ConsoleApp />);

    expect(await screen.findByLabelText(/^Task/)).toHaveValue(task);
    fireEvent.click(screen.getByText("Details (optional)"));
    expect(screen.getByLabelText("Issue or reference text")).toHaveValue(
      "ISSUE-42\nsecond line",
    );
    expect(screen.getByRole("button", { name: "Run safely" })).toBeInTheDocument();
  });

  it("reconnects to a server-side run after refresh without resubmitting", async () => {
    const running: ProductRun = {
      ...productRun(),
      current_stage: "Checking",
      stage_sentence: "Verification needs another check.",
      stage_transitions: [
        {
          sequence: 1,
          timestamp: "2026-07-14T00:00:00Z",
          stage: "Understanding",
          sentence: "Understanding the task and choosing a safe route.",
        },
        {
          sequence: 3,
          timestamp: "2026-07-14T00:00:01Z",
          stage: "Working",
          sentence: "Working on the change in an isolated copy.",
        },
        {
          sequence: 8,
          timestamp: "2026-07-14T00:00:04Z",
          stage: "Checking",
          sentence: "Verification needs another check.",
        },
      ],
      final_verdict: null,
      verdict_reason: null,
      change_summary: "No file changes were recorded.",
      duration: { value_ms: 4_230, accounting_status: "partial" },
      cost: { value: null, currency: null, accounting_status: "unknown" },
      available_actions: [
        {
          id: "cancel",
          label: "Cancel",
          method: "POST",
          href: "/v1/console/runs/run_new/cancel",
        },
      ],
      last_event_sequence: 8,
    };
    const cancelled: ProductRun = {
      ...running,
      current_stage: "Ready",
      stage_sentence: "The task was cancelled safely.",
      final_verdict: "Cancelled",
      verdict_reason:
        "Cancellation stopped future work and preserved recorded evidence.",
      available_actions: [
        {
          id: "retry",
          label: "Start again",
          method: "GET",
          href: "/console",
        },
      ],
      target_repository: {
        modified: false,
        accounting_status: "known",
        statement: "The target repository was not modified.",
      },
    };
    mockConsole(false, false, {
      statusProduct: running,
      cancelProduct: cancelled,
    });
    history.replaceState(null, "", "/console?run=run_new");
    render(<ConsoleApp />);

    const progress = await screen.findByLabelText("Task progress");
    for (const stage of ["Understanding", "Working", "Checking", "Ready"])
      expect(within(progress).getByText(stage)).toBeInTheDocument();
    expect(screen.getByText("Verification needs another check.")).toBeInTheDocument();
    expect(screen.getByText("Unknown (unknown)")).toBeInTheDocument();
    expect(
      (fetch as ReturnType<typeof vi.fn>).mock.calls.filter(
        ([input, init]) =>
          String(input).endsWith("/v1/console/runs") && init?.method === "POST",
      ),
    ).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    const result = await screen.findByTestId("run-presentation");
    expect(within(result).getByText("Cancelled")).toBeInTheDocument();
    expect(
      within(result).getByText("The target repository was not modified."),
    ).toBeInTheDocument();
  });

  it("submits one click once with safe PT2 defaults and no invented wall time", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console");
    render(<ConsoleApp />);
    fireEvent.change(await screen.findByLabelText(/^Task/), {
      target: { value: "Keep line one.\n\nKeep line three." },
    });
    const button = screen.getByRole("button", { name: "Run safely" });
    fireEvent.click(button);
    fireEvent.click(button);
    await screen.findByTestId("run-presentation");

    const submissions = (fetch as ReturnType<typeof vi.fn>).mock.calls.filter(
      ([input, init]) =>
        String(input).endsWith("/v1/console/runs") && init?.method === "POST",
    );
    expect(submissions).toHaveLength(1);
    const request = JSON.parse(String(submissions[0]?.[1]?.body));
    expect(request).toMatchObject({
      task: "Keep line one.\n\nKeep line three.",
      policy_preset: "performance",
      delivery_mode: "approve",
      verification_required: true,
    });
    expect(request.submission_id).toEqual(expect.any(String));
    expect(request).not.toHaveProperty("max_wall_time");
  });

  it("never offers delivery for work that could not be proved", async () => {
    const failed: ProductRun = {
      ...productRun(),
      stage_sentence: "The change could not be proved acceptable.",
      final_verdict: "Could not prove",
      verdict_reason: "Verification evidence was missing.",
      change_summary: "A candidate was preserved for review.",
      available_actions: [
        {
          id: "retry",
          label: "Start again",
          method: "GET",
          href: "/console",
        },
        {
          id: "review_evidence",
          label: "Review evidence",
          method: "GET",
          href: "/console/runs/run_new/replay",
        },
      ],
      recovery_action: {
        label: "Start again",
        instruction: "Review the missing evidence, then run the task again.",
        href: "/console",
      },
    };
    mockConsole(false, false, { statusProduct: failed });
    history.replaceState(null, "", "/console?run=run_new");
    render(<ConsoleApp />);

    const result = await screen.findByTestId("run-presentation");
    expect(within(result).getByText("Could not prove")).toBeInTheDocument();
    for (const action of ["Apply change", "Create branch", "Open pull request"])
      expect(within(result).queryByRole("button", { name: action })).toBeNull();
    expect(
      within(result).getByRole("button", { name: "Start again" }),
    ).toBeInTheDocument();
  });

  it("submits the complete run form and answers the outcome questions", async () => {
    mockConsole(false);
    history.replaceState(null, "", "/console/run");
    render(<ConsoleApp />);

    await screen.findByRole("heading", {
      name: "What would you like Villani to change?",
    });
    expect(
      await screen.findByText("npm test", { selector: "code" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/^Task/), {
      target: { value: "Fix repeated separators in the parser." },
    });
    fireEvent.change(screen.getByLabelText("Success criteria (optional)"), {
      target: { value: "The repository test passes." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview task assessment" }));
    fireEvent.click(await screen.findByText("Advanced task assessment"));
    const preview = await screen.findByRole("region", { name: "Task assessment" });
    expect(within(preview).getByText("default / local-model")).toBeInTheDocument();
    expect(within(preview).getByText("Available")).toBeInTheDocument();
    expect(within(preview).getByText("Unknown (unknown)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run safely" }));

    const result = await screen.findByTestId("run-presentation");
    expect(within(result).getByText("Ready to apply")).toBeInTheDocument();
    expect(within(result).getByText("src/parser.ts")).toBeInTheDocument();
    for (const heading of [
      "WHAT CHANGED",
      "FILES CHANGED",
      "CHECKS AND TESTS",
      "REQUIREMENT COVERAGE",
      "WHY VILLANI TRUSTS IT",
      "KNOWN COST",
      "ELAPSED TIME",
    ])
      expect(
        within(result).getByRole("heading", { name: heading }),
      ).toBeInTheDocument();
    expect(within(result).getByText("USD 0.1700")).toBeInTheDocument();
    expect(within(result).getByText("1.0 min")).toBeInTheDocument();
    expect(within(result).getByText("Proved")).toBeInTheDocument();
    expect(
      within(result).getByText(
        "Repository checks and semantic verification proved every requirement.",
      ),
    ).toBeInTheDocument();
    expect(
      within(result).getByRole("link", { name: "View full evidence" }),
    ).toHaveAttribute("href", "/console/runs/run_new/replay");
    const submissionCall = (fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      ([input, init]) =>
        String(input).endsWith("/v1/console/runs") && init?.method === "POST",
    );
    expect(JSON.parse(String(submissionCall?.[1]?.body))).toMatchObject({
      policy_preset: "performance",
      delivery_mode: "approve",
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

    await screen.findByRole("heading", {
      name: "What would you like Villani to change?",
    });
    fireEvent.change(screen.getByLabelText(/^Task/), {
      target: { value: "Fix repeated separators in the parser." },
    });
    fireEvent.change(screen.getByLabelText("Delivery mode"), {
      target: { value: "approve" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run safely" }));

    const review = await screen.findByTestId("run-presentation");
    expect(within(review).getByText("Ready to apply")).toBeInTheDocument();
    expect(
      within(review).getByRole("heading", { name: "CHECKS AND TESTS" }),
    ).toBeInTheDocument();
    expect(within(review).getByText("18")).toBeInTheDocument();

    fireEvent.click(within(review).getByRole("button", { name: "Apply change" }));

    await waitFor(() =>
      expect(
        within(review).getByText("The target repository was modified."),
      ).toBeInTheDocument(),
    );
    const approvalCall = (fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      ([input, init]) =>
        String(input).includes("/v1/console/runs/run_new/approval") &&
        init?.method === "POST",
    );
    expect(JSON.parse(String(approvalCall?.[1]?.body))).toMatchObject({
      action: "approve",
      reason: "Apply change selected from the product result.",
    });
  });

  it("explains how to recover when Villani Service is offline", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("connection refused")));
    history.replaceState(null, "", "/console/run");

    render(<ConsoleApp />);

    expect(
      await screen.findByText("Villani Service is unavailable"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No run was started. The target repository was not modified."),
    ).toBeInTheDocument();
    expect(screen.getByText("villani service start")).toBeInTheDocument();
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
