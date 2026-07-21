import { readFileSync } from "node:fs";
import { dirname, join, parse, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  VILLANI_SCHEMA_FILE_BY_VERSION,
  VillaniSchemaValidator,
} from "../src/providers/villaniSchemaValidation.js";

function repositoryRoot(): string {
  let current = dirname(fileURLToPath(import.meta.url));
  const filesystemRoot = parse(current).root;
  while (true) {
    try {
      const schema = readFileSync(
        join(current, "schemas", "v1", "event.schema.json"),
        "utf8",
      );
      if (schema) return current;
    } catch {
      // Keep walking toward the filesystem root.
    }
    if (current === filesystemRoot)
      throw new Error("repository root not found");
    current = dirname(current);
  }
}

const root = repositoryRoot();
const fixtureRoot = join(root, "integration", "fixtures", "protocol", "v1");
const validRun = join(fixtureRoot, "valid_run");
const invalid = join(fixtureRoot, "invalid");
const validator = new VillaniSchemaValidator(root);

function json(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
}

function jsonl(path: string): Record<string, unknown>[] {
  return readFileSync(path, "utf8")
    .trim()
    .split(/\r?\n/)
    .map((line) => JSON.parse(line) as Record<string, unknown>);
}

const snapshotPaths = [
  join(validRun, "task.json"),
  join(validRun, "manifest.json"),
  join(validRun, "state.json"),
  join(validRun, "classification.json"),
  join(validRun, "attempts", "attempt_001", "attempt.json"),
  join(validRun, "attempts", "attempt_002", "attempt.json"),
  join(validRun, "verification", "attempt_001.json"),
  join(validRun, "verification", "attempt_002.json"),
  join(validRun, "selection.json"),
  join(validRun, "materialization.json"),
  join(validRun, "validation-coverage.json"),
  join(validRun, "run-summary.json"),
  join(validRun, "agent-system-config.json"),
  join(validRun, "role-bindings.json"),
  join(validRun, "agent-invocation-identity.json"),
  join(validRun, "attempts", "attempt_001", "agent", "invocation.json"),
  join(validRun, "attempts", "attempt_001", "agent", "process-result.json"),
  join(validRun, "attempts", "attempt_001", "agent", "output-tail.json"),
  join(validRun, "attempts", "attempt_001", "agent", "coder-result.json"),
  join(
    validRun,
    "attempts",
    "attempt_001",
    "agent",
    "claude-coder-result.json",
  ),
  join(
    validRun,
    "agent-systems",
    "asys_d605dea1f6503cf9996864423c705228b426ccee3c2e02869084ac9bbbbda575.json",
  ),
  join(
    validRun,
    "agent-systems",
    "asys_80147fac99d0bfffb4605d4a447ad9a0b6d6e947426c95efcf7168cc6ec94dfa.json",
  ),
  join(validRun, "attempts", "attempt_001", "harness-result.json"),
  join(validRun, "attempts", "attempt_002", "harness-result.json"),
  join(validRun, "harness-conformance.json"),
  join(validRun, "harness-discovery.json"),
  join(validRun, "qualification-observation.json"),
  join(validRun, "qualification-invalidation.json"),
  join(validRun, "qualification-snapshot.json"),
  join(validRun, "gate-c.json"),
  join(validRun, "economics-observation.json"),
  join(validRun, "economics-snapshot.json"),
  join(validRun, "online-evidence-update.json"),
  join(validRun, "route-plan.json"),
  join(validRun, "route-policy.json"),
  join(validRun, "route-policy-evaluation.json"),
  join(validRun, "route-policy-publication.json"),
  join(validRun, "adaptive-verification-plan.json"),
  join(validRun, "binary-verification-decision.json"),
  join(validRun, "review-package.json"),
  join(validRun, "human-outcome.json"),
  join(validRun, "supervision-metrics.json"),
  join(validRun, "gate-d.json"),
];

describe("canonical Villani protocol", () => {
  it("accepts the complete shared bundle and every v1 schema version", () => {
    const versions = new Set<string>();
    for (const path of snapshotPaths) {
      const document = json(path);
      const result = validator.validate(document);
      expect(result, resolve(path)).toMatchObject({ valid: true, errors: [] });
      versions.add(String(document.schema_version));
    }

    const events = jsonl(join(validRun, "events.jsonl"));
    expect(validator.validateEventStream(events)).toMatchObject({
      valid: true,
    });
    versions.add(String(events[0].schema_version));

    for (const decision of jsonl(join(validRun, "policy_decisions.jsonl"))) {
      expect(validator.validate(decision)).toMatchObject({ valid: true });
      versions.add(String(decision.schema_version));
    }

    expect(versions).toEqual(
      new Set(Object.keys(VILLANI_SCHEMA_FILE_BY_VERSION)),
    );
  });

  it.each([
    ["event_missing_run_id.json", "required"],
    ["event_sequence_zero.json", "minimum"],
    ["attempt_event_without_attempt_id.json", "type"],
    ["verification_error_marked_eligible.json", "acceptance_eligibility"],
    ["selection_contains_ineligible_candidate.json", "selection_eligibility"],
    ["manifest_cost_missing_but_status_complete.json", "accounting_status"],
    ["state_terminal_false_for_completed.json", "terminal_state"],
    ["unknown_top_level_property.json", "additionalProperties"],
    ["qualification_unknown_cost_as_zero.json", "accounting_status"],
    ["economics_unknown_cost_as_zero.json", "accounting_status"],
    ["binary_unclear_marked_accepted.json", "binary_verification_authority"],
    ["human_outcome_unknown_review_time_as_number.json", "accounting_status"],
    ["human_outcome_unknown_full_trace_as_boolean.json", "accounting_status"],
    ["supervision_unknown_trace_claims_application.json", "accounting_status"],
  ])("rejects %s for %s", (filename, expectedKeyword) => {
    const result = validator.validate(json(join(invalid, filename)));
    expect(result.valid).toBe(false);
    if (result.valid) throw new Error("expected fixture to be invalid");
    expect(result.errors).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          instancePath: expect.any(String),
          keyword: expectedKeyword,
          message: expect.any(String),
        }),
      ]),
    );
  });

  it("requires strictly increasing event sequences", () => {
    const events = jsonl(join(validRun, "events.jsonl")).slice(0, 2);
    events[1].sequence = 1;
    const result = validator.validateEventStream(events);
    expect(result.valid).toBe(false);
    if (result.valid) throw new Error("expected stream to be invalid");
    expect(result.errors).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ keyword: "event_sequence" }),
      ]),
    );
  });

  it("keeps future event types open while enforcing scoped categories", () => {
    const futureEvent = jsonl(join(validRun, "events.jsonl"))[0];
    futureEvent.event_type = "future_generic_signal";
    expect(validator.validate(futureEvent)).toMatchObject({ valid: true });
    expect(
      validator.validate(
        json(join(invalid, "attempt_event_without_attempt_id.json")),
      ),
    ).toMatchObject({ valid: false });
  });

  it("fails closed on tampered PT5 identity, evidence, and qualification", () => {
    const identity = structuredClone(
      json(
        join(
          validRun,
          "agent-systems",
          "asys_d605dea1f6503cf9996864423c705228b426ccee3c2e02869084ac9bbbbda575.json",
        ),
      ),
    );
    identity.qualification_status = "qualified";
    identity.qualification_references = [];
    expect(validator.validate(identity)).toMatchObject({ valid: false });

    const harness = structuredClone(
      json(join(validRun, "attempts", "attempt_001", "harness-result.json")),
    );
    harness.cost = {
      amount: 0,
      currency: "USD",
      accounting_status: "unknown",
      source: null,
    };
    harness.artifacts[0].path = "../outside.patch";
    harness.normalized_events[1].sequence = 9;
    const harnessValidation = validator.validate(harness);
    expect(harnessValidation.valid).toBe(false);
    if (harnessValidation.valid) throw new Error("expected evidence to fail");
    expect(harnessValidation.errors.map((error) => error.keyword)).toEqual(
      expect.arrayContaining([
        "accounting_status",
        "artifact_path_safety",
        "event_sequence",
      ]),
    );

    const conformance = structuredClone(
      json(join(validRun, "harness-conformance.json")),
    );
    conformance.checks.pop();
    expect(validator.validate(conformance)).toMatchObject({ valid: false });
  });
});
