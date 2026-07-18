import { describe, expect, it } from "vitest";

import {
  ACCEPTED_CHANGE_POLICY_VERSION,
  ECONOMICS_OBSERVATION_SCHEMA_VERSION,
  ONLINE_EVIDENCE_UPDATE_SCHEMA_VERSION,
  ROUTE_PLAN_SCHEMA_VERSION,
  ROUTE_POLICY_SCHEMA_VERSION,
  type AcceptedChangeObjective,
  type RoutePlan,
} from "../src/economics.js";

describe("accepted-change economics public models", () => {
  it("exports stable policy and wire versions", () => {
    expect(ACCEPTED_CHANGE_POLICY_VERSION).toBe(
      "accepted_change_economics_v1",
    );
    expect(ROUTE_POLICY_SCHEMA_VERSION).toBe("villani.route_policy.v1");
    expect(ROUTE_PLAN_SCHEMA_VERSION).toBe("villani.route_plan.v1");
    expect(ECONOMICS_OBSERVATION_SCHEMA_VERSION).toBe(
      "villani.economics_observation.v1",
    );
    expect(ONLINE_EVIDENCE_UPDATE_SCHEMA_VERSION).toBe(
      "villani.online_evidence_update.v1",
    );
  });

  it("keeps partial objectives distinct from full totals", () => {
    const objective: Pick<
      AcceptedChangeObjective,
      | "accounting_status"
      | "known_numerator_cost"
      | "expected_accepted_change_cost"
      | "partial_expected_known_cost"
      | "unknown_components"
    > = {
      accounting_status: "partial",
      known_numerator_cost: 2,
      expected_accepted_change_cost: null,
      partial_expected_known_cost: 4,
      unknown_components: ["human_review_cost"],
    };
    expect(objective.expected_accepted_change_cost).toBeNull();
    expect(objective.partial_expected_known_cost).toBe(4);
  });

  it("makes forced policy-metric exclusion explicit", () => {
    const choice: Pick<
      RoutePlan,
      "selection_mode" | "forced_choice" | "automatic_policy_metrics_eligible"
    > = {
      selection_mode: "forced",
      forced_choice: true,
      automatic_policy_metrics_eligible: false,
    };
    expect(choice.automatic_policy_metrics_eligible).toBe(false);
  });
});
