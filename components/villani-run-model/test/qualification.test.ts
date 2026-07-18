import { describe, expect, it } from "vitest";

import {
  GATE_C_SCHEMA_VERSION,
  QUALIFICATION_INVALIDATION_SCHEMA_VERSION,
  QUALIFICATION_OBSERVATION_SCHEMA_VERSION,
  QUALIFICATION_POLICY_VERSION,
  QUALIFICATION_SNAPSHOT_SCHEMA_VERSION,
  type QualificationAssessment,
  type QualificationStatistics,
} from "../src/qualification.js";

describe("repository qualification public models", () => {
  it("exports stable policy and wire-contract versions", () => {
    expect(QUALIFICATION_POLICY_VERSION).toBe("repository_qualification_v1");
    expect(QUALIFICATION_OBSERVATION_SCHEMA_VERSION).toBe(
      "villani.qualification_observation.v1",
    );
    expect(QUALIFICATION_INVALIDATION_SCHEMA_VERSION).toBe(
      "villani.qualification_invalidation.v1",
    );
    expect(QUALIFICATION_SNAPSHOT_SCHEMA_VERSION).toBe(
      "villani.qualification_snapshot.v1",
    );
    expect(GATE_C_SCHEMA_VERSION).toBe("villani.gate_c.v1");
  });

  it("represents zero evidence and unknown cost without invented numbers", () => {
    const statistics: Pick<
      QualificationStatistics,
      | "sample_count"
      | "acceptance_rate"
      | "wilson_lower_bound"
      | "cost_distribution_by_currency"
      | "cost_unknown_count"
      | "accepted_change_cost_by_currency"
      | "accepted_change_cost_unknown_count"
    > = {
      sample_count: 0,
      acceptance_rate: null,
      wilson_lower_bound: null,
      cost_distribution_by_currency: {},
      cost_unknown_count: 0,
      accepted_change_cost_by_currency: {},
      accepted_change_cost_unknown_count: 0,
    };
    expect(statistics.acceptance_rate).toBeNull();
    expect(statistics.cost_distribution_by_currency).toEqual({});
  });

  it("makes automatic and provisional eligibility explicit", () => {
    const eligibility: Pick<
      QualificationAssessment,
      | "state"
      | "automatic_eligible"
      | "provisional_fallback_eligible"
      | "manual_override_required"
    > = {
      state: "experimental",
      automatic_eligible: false,
      provisional_fallback_eligible: false,
      manual_override_required: true,
    };
    expect(eligibility).toEqual({
      state: "experimental",
      automatic_eligible: false,
      provisional_fallback_eligible: false,
      manual_override_required: true,
    });
  });
});
