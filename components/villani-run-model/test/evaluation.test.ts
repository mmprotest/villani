import { describe, expect, it } from "vitest";

import {
  EVALUATION_SCHEMA_VERSIONS,
  type EvaluationReportV1,
  type EvaluationTaskV1,
} from "../src/index.js";

describe("Founder Thesis Lab contracts", () => {
  it("exports all five exact v1 schema identities", () => {
    expect(EVALUATION_SCHEMA_VERSIONS).toEqual([
      "villani.evaluation_suite.v1",
      "villani.evaluation_task.v1",
      "villani.evaluation_trial.v1",
      "villani.human_review.v1",
      "villani.evaluation_report.v1",
    ]);
  });

  it("keeps evaluator-only task material and truthful metric units explicit", () => {
    const task = {
      schema_version: "villani.evaluation_task.v1",
      evaluator_only: {
        hidden_check_references: ["evaluator-only/task/hidden/check.py"],
        future_context_references: [],
        runner_expected_patch_present: false,
      },
    } as Pick<EvaluationTaskV1, "schema_version" | "evaluator_only">;
    const metric: EvaluationReportV1["cost"][string] = {
      value: null,
      numerator: null,
      denominator: 1,
      unit: "USD",
      accounting_status: "unknown",
      interval: null,
    };

    expect(task.evaluator_only.runner_expected_patch_present).toBe(false);
    expect(metric.value).toBeNull();
    expect(metric.unit).toBe("USD");
  });
});
