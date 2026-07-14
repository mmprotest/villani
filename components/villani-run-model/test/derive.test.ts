import { describe, expect, it } from "vitest";
import {
  artifactMayRender,
  consoleReplayFromRunDetail,
  deriveFlightRecorderRunModel,
  deriveRun,
  deriveRunStatus,
  deriveVillaniWebRunModel,
  maskSensitive,
  type RunDetail,
} from "../src/index.js";

describe("shared run derivation", () => {
  it("lets canonical controller state own status", () => {
    expect(deriveRunStatus([], "COMPLETED").status).toBe("succeeded");
    expect(deriveRunStatus([], "EXHAUSTED").status).toBe("partial");
    expect(deriveRunStatus([], "FAILED").status).toBe("failed");
  });

  it("derives candidates and selected work without inventing unknown cost", () => {
    const result = deriveRun(
      {
        id: "run",
        status: "COMPLETED",
        repository_id: "repo",
        first_occurred_at: "2026-01-01T00:00:00Z",
        last_observed_at: "2026-01-01T00:00:01Z",
        attempts: [{ id: "a", status: "completed" }],
        outcomes: [],
        artifact_count: 0,
      },
      [
        {
          id: "1",
          name: "verification_completed",
          attempt_id: "a",
          payload: { acceptance_eligible: true },
        },
        {
          id: "2",
          name: "candidate_selected",
          payload: { selected_attempt_id: "a" },
        },
      ],
    );
    expect(result.candidates[0]).toMatchObject({
      attemptId: "a",
      eligible: true,
      selected: true,
    });
    expect(result.candidates[0]?.costUsd).toBeNull();
  });

  it("masks sensitive fields and blocks secret artifacts", () => {
    expect(
      maskSensitive({ api_key: "value", safe: { token: "value" } }),
    ).toEqual({ api_key: "••••••••", safe: { token: "••••••••" } });
    expect(artifactMayRender("secret")).toBe(false);
  });

  it("keeps Villani Web and Flight Recorder canonical values identical", () => {
    const detail: RunDetail = {
      id: "run-shared-parity",
      trace_id: "trace-shared-parity",
      status: "COMPLETED",
      first_occurred_at: "2026-07-14T00:00:00Z",
      last_observed_at: "2026-07-14T00:00:01Z",
      attempts: [
        { id: "attempt_001", status: "rejected" },
        { id: "attempt_002", status: "completed" },
      ],
      outcomes: [],
      artifact_count: 2,
      selected_attempt_id: "attempt_002",
      selected_backend: "strong",
      selected_model: "fixture-strong",
      input_tokens: 21,
      output_tokens: 13,
      total_tokens: 34,
      coding_cost_usd: 0.02,
      verifier_cost_usd: null,
      total_cost_usd: null,
      duration_ms: 2_500,
      changed_files: ["src/fixed.ts"],
      withheld_artifact_count: 1,
      withheld_artifact_categories: ["secret"],
      candidate_outcomes: {
        attempt_001: {
          candidate_eligibility: false,
          failure_category: "implementation_failure",
          changed_files: ["src/first.ts"],
        },
        attempt_002: {
          candidate_eligibility: true,
          backend_name: "strong",
          model: "fixture-strong",
          changed_files: ["src/fixed.ts"],
          verification: {
            outcome: "accepted",
            authority_source: "repository_validation",
            verifier: "fixture-verifier",
          },
        },
      },
    };

    const web = deriveVillaniWebRunModel(detail);
    const recorder = deriveFlightRecorderRunModel(detail);

    expect(web).toEqual(recorder);
    expect(web).toMatchObject({
      run_id: "run-shared-parity",
      selected_attempt_id: "attempt_002",
      total_tokens: 34,
      coding_cost_usd: 0.02,
      verifier_cost_usd: null,
      total_cost_usd: null,
      selected_materialized_files: ["src/fixed.ts"],
      withheld_artifact_count: 1,
      withheld_artifact_categories: ["secret"],
    });
  });

  it("projects both connected and local consumers onto stable Console replay links", () => {
    const detail: RunDetail = {
      id: "run/deep link",
      status: "COMPLETED",
      first_occurred_at: "2026-07-14T00:00:00Z",
      last_observed_at: "2026-07-14T00:00:01Z",
      attempts: [{ id: "attempt_001", status: "completed" }],
      outcomes: [],
      artifact_count: 0,
      selected_attempt_id: "attempt_001",
      candidate_outcomes: {
        attempt_001: {
          candidate_eligibility: true,
          changed_files: ["src/a b.ts"],
        },
      },
    };
    const replay = consoleReplayFromRunDetail(
      detail,
      [{ id: "event/1", sequence: 1, name: "run_completed" }],
      [],
      "SYNCHRONIZED",
    );
    expect(replay.canonical?.run_id).toBe(detail.id);
    expect(replay.synchronization_state).toBe("SYNCHRONIZED");
    expect(replay.deep_links.self).toBe("/console/runs/run%2Fdeep%20link");
    expect(replay.events[0]?.deep_link).toContain("/events/event%2F1");
    expect(replay.attempts[0]?.deep_link).toContain("/attempts/attempt_001");
    expect(replay.files[0]?.deep_link).toContain("/files/src%2Fa%20b.ts");
  });
});
