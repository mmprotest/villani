import { describe, expect, it } from "vitest";

import {
  AGENT_INVOCATION_IDENTITY_SCHEMA_VERSION,
  AGENT_SYSTEM_CONFIG_SCHEMA_VERSION,
  AGENT_SYSTEM_SCHEMA_VERSION,
  CLI_INVOCATION_SCHEMA_VERSION,
  CLI_OUTPUT_TAIL_SCHEMA_VERSION,
  CLI_PROCESS_RESULT_SCHEMA_VERSION,
  CLAUDE_CODER_RESULT_SCHEMA_VERSION,
  CODEX_CODER_RESULT_SCHEMA_VERSION,
  HARNESS_CONFORMANCE_SCHEMA_VERSION,
  HARNESS_DISCOVERY_SCHEMA_VERSION,
  HARNESS_RESULT_SCHEMA_VERSION,
  REQUIRED_HARNESS_CONFORMANCE_CHECKS,
  ROLE_BINDINGS_SCHEMA_VERSION,
  type HarnessResult,
} from "../src/agentSystem.js";

describe("agent-system public models", () => {
  it("exports stable versioned contracts and the complete conformance set", () => {
    expect(AGENT_SYSTEM_SCHEMA_VERSION).toBe("villani.agent_system.v1");
    expect(AGENT_SYSTEM_CONFIG_SCHEMA_VERSION).toBe(
      "villani.agent_system_config.v1",
    );
    expect(ROLE_BINDINGS_SCHEMA_VERSION).toBe("villani.role_bindings.v1");
    expect(AGENT_INVOCATION_IDENTITY_SCHEMA_VERSION).toBe(
      "villani.agent_invocation_identity.v1",
    );
    expect(CLI_INVOCATION_SCHEMA_VERSION).toBe("villani.cli_invocation.v1");
    expect(CLI_PROCESS_RESULT_SCHEMA_VERSION).toBe(
      "villani.cli_process_result.v1",
    );
    expect(CLI_OUTPUT_TAIL_SCHEMA_VERSION).toBe("villani.cli_output_tail.v1");
    expect(CODEX_CODER_RESULT_SCHEMA_VERSION).toBe(
      "villani.codex_coder_result.v1",
    );
    expect(CLAUDE_CODER_RESULT_SCHEMA_VERSION).toBe(
      "villani.claude_coder_result.v1",
    );
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
