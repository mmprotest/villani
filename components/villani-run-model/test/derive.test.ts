import { describe, expect, it } from "vitest";
import {
  artifactMayRender,
  deriveRun,
  deriveRunStatus,
  maskSensitive,
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
});
