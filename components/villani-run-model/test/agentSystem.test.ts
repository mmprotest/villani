import { describe, expect, it } from "vitest";

import {
  AGENT_SYSTEM_SCHEMA_VERSION,
  HARNESS_CONFORMANCE_SCHEMA_VERSION,
  HARNESS_DISCOVERY_SCHEMA_VERSION,
  HARNESS_RESULT_SCHEMA_VERSION,
  REQUIRED_HARNESS_CONFORMANCE_CHECKS,
  type HarnessResult,
} from "../src/agentSystem.js";

describe("agent-system public models", () => {
  it("exports stable versioned contracts and the complete conformance set", () => {
    expect(AGENT_SYSTEM_SCHEMA_VERSION).toBe("villani.agent_system.v1");
    expect(HARNESS_RESULT_SCHEMA_VERSION).toBe("villani.harness_result.v1");
    expect(HARNESS_CONFORMANCE_SCHEMA_VERSION).toBe(
      "villani.harness_conformance_report.v1",
    );
    expect(HARNESS_DISCOVERY_SCHEMA_VERSION).toBe(
      "villani.harness_discovery.v1",
    );
    expect(REQUIRED_HARNESS_CONFORMANCE_CHECKS).toHaveLength(32);
    expect(new Set(REQUIRED_HARNESS_CONFORMANCE_CHECKS).size).toBe(32);
  });

  it("represents unknown cost as null plus accounting status", () => {
    const cost: HarnessResult["cost"] = {
      amount: null,
      currency: null,
      accounting_status: "unknown",
      source: null,
      per_model: {},
    };
    expect(cost).toEqual({
      amount: null,
      currency: null,
      accounting_status: "unknown",
      source: null,
      per_model: {},
    });
  });
});
