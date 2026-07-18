import { describe, expect, it } from "vitest";

import {
  ADAPTIVE_VERIFICATION_PLAN_SCHEMA_VERSION,
  BINARY_VERIFICATION_DECISION_SCHEMA_VERSION,
  GATE_D_SCHEMA_VERSION,
  HUMAN_OUTCOME_SCHEMA_VERSION,
  REVIEW_PACKAGE_SCHEMA_VERSION,
  SUPERVISION_METRICS_SCHEMA_VERSION,
  type BinaryVerificationDecision,
  type HumanOutcome,
} from "../src/adaptiveVerification.js";

describe("adaptive verification protocol types", () => {
  it("exports every additive PT9 schema identity", () => {
    expect([
      ADAPTIVE_VERIFICATION_PLAN_SCHEMA_VERSION,
      BINARY_VERIFICATION_DECISION_SCHEMA_VERSION,
      REVIEW_PACKAGE_SCHEMA_VERSION,
      HUMAN_OUTCOME_SCHEMA_VERSION,
      SUPERVISION_METRICS_SCHEMA_VERSION,
      GATE_D_SCHEMA_VERSION,
    ]).toEqual([
      "villani.adaptive_verification_plan.v1",
      "villani.binary_verification_decision.v1",
      "villani.review_package.v1",
      "villani.human_outcome.v1",
      "villani.supervision_metrics.v1",
      "villani.gate_d.v1",
    ]);
  });

  it("keeps binary authority and unknown review time explicit", () => {
    const binary: Pick<
      BinaryVerificationDecision,
      "decision" | "semantic_status" | "verification_cost"
    > = {
      decision: 0,
      semantic_status: "unclear",
      verification_cost: {
        amount: null,
        currency: null,
        accounting_status: "unknown",
        source: "verifier_cost_unavailable",
      },
    };
    const outcome: Pick<
      HumanOutcome,
      "outcome" | "review_minutes" | "review_time_accounting_status"
    > = {
      outcome: "accepted_as_is",
      review_minutes: null,
      review_time_accounting_status: "unknown",
    };
    expect(binary.decision).toBe(0);
    expect(binary.verification_cost.amount).toBeNull();
    expect(outcome.review_minutes).toBeNull();
  });
});
