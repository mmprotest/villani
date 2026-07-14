import { describe, expect, it } from "vitest";
import { deriveFlightRecorderRunModel, type RunDetail } from "@villani/run-model";
import { deriveVillaniWebRunModel } from "../src/connectedRunModel";

describe("Flight Recorder golden parity", () => {
  it("derives identical canonical values through both shared consumer adapters", () => {
    const detail: RunDetail = {
      id: "run_parity",
      trace_id: "trace_parity",
      status: "COMPLETED",
      first_occurred_at: "2026-07-14T00:00:00Z",
      last_observed_at: "2026-07-14T00:00:01Z",
      attempts: [{ id: "attempt_001", status: "completed" }],
      outcomes: [],
      artifact_count: 0,
      selected_attempt_id: "attempt_001",
      selected_backend: "local",
      selected_model: "fixture-model",
      input_tokens: 10,
      output_tokens: 5,
      total_tokens: 15,
      total_cost_usd: null,
      duration_ms: 1_000,
      changed_files: ["example.txt"],
      candidate_outcomes: {
        attempt_001: {
          status: "accepted",
          backend_name: "local",
          model: "fixture-model",
          candidate_eligibility: true,
          changed_files: ["example.txt"],
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
    expect(web.selected_attempt_id).toBe("attempt_001");
    expect(web.total_tokens).toBe(15);
    expect(web.total_cost_usd).toBeNull();
    expect(web.selected_materialized_files).toEqual(["example.txt"]);
  });
});
