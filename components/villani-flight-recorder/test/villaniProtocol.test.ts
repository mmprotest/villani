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
];

describe("canonical Villani protocol", () => {
  it("accepts the complete shared bundle and all ten schema versions", () => {
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
});
