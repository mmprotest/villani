import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join, parse } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { readVillaniV2Json } from "../src/providers/villaniProtocolV2.js";
import {
  readVillaniV2Document,
  VILLANI_V2_SCHEMA_FILE_BY_VERSION,
  VillaniSchemaValidator,
} from "../src/providers/villaniSchemaValidation.js";

function repositoryRoot(): string {
  let current = dirname(fileURLToPath(import.meta.url));
  const root = parse(current).root;
  while (current !== root) {
    try {
      if (
        readFileSync(
          join(current, "schemas", "v2", "telemetry-envelope.schema.json"),
        )
      )
        return current;
    } catch {
      // Continue upward.
    }
    current = dirname(current);
  }
  throw new Error("repository root not found");
}

const root = repositoryRoot();
const fixtures = join(root, "integration", "fixtures", "protocol", "v2");
const validator = new VillaniSchemaValidator(root);
const validFiles = [
  "agent-capability.json",
  "artifact-descriptor.json",
  "outcome.json",
  "policy-publication.json",
  "resource.json",
  "span.json",
  "telemetry-envelope.json",
  "verifier-capability.json",
];

describe("Villani v2 shared contracts", () => {
  it("accepts all eight strict contract fixtures", () => {
    const versions = new Set<string>();
    for (const filename of validFiles) {
      const result = validator.validate(
        readVillaniV2Json(join(fixtures, "valid", filename)),
      );
      expect(result, filename).toMatchObject({ valid: true, errors: [] });
      if (result.valid) versions.add(result.value.schema_version);
    }
    expect(versions).toEqual(
      new Set(Object.keys(VILLANI_V2_SCHEMA_FILE_BY_VERSION)),
    );
  });

  it("strictly reads valid v2 bytes and rejects invalid bytes", () => {
    expect(
      readVillaniV2Document(
        join(fixtures, "valid", "telemetry-envelope.json"),
        validator,
      ).schema_version,
    ).toBe("villani.telemetry_envelope.v2");
    expect(() =>
      readVillaniV2Document(
        join(fixtures, "invalid", "telemetry_missing_idempotency.json"),
        validator,
      ),
    ).toThrow(/\[required\]/);
  });

  it.each([
    ["agent_capability_duplicate_feature.json", "uniqueItems"],
    ["artifact_embeds_bytes.json", "additionalProperties"],
    ["outcome_unknown_cost_has_value.json", "accounting_status"],
    ["policy_bad_digest.json", "pattern"],
    ["resource_unknown_property.json", "additionalProperties"],
    ["span_bad_kind.json", "pattern"],
    ["telemetry_embeds_artifact_bytes.json", "not"],
    ["telemetry_missing_idempotency.json", "required"],
    ["verifier_capability_missing_evidence.json", "required"],
  ])("rejects %s for %s", (filename, category) => {
    const result = validator.validate(
      readVillaniV2Json(join(fixtures, "invalid", filename)),
    );
    expect(result.valid).toBe(false);
    if (result.valid) throw new Error("expected invalid fixture");
    expect(result.errors.map((error) => error.keyword)).toContain(category);
  });

  it("checks exactly the same fixture bytes as Python", () => {
    const manifest = JSON.parse(
      readFileSync(join(fixtures, "fixture-digests.json"), "utf8"),
    ) as Record<string, string>;
    const actual = Object.fromEntries(
      Object.keys(manifest).map((relative) => [
        relative,
        createHash("sha256")
          .update(readFileSync(join(fixtures, relative)))
          .digest("hex"),
      ]),
    );
    expect(actual).toEqual(manifest);
  });

  it("keeps unknown future span kinds readable", () => {
    const span = readVillaniV2Json(
      join(fixtures, "valid", "span.json"),
    ) as Record<string, unknown>;
    span.kind = "future_span_kind";
    expect(validator.validate(span)).toMatchObject({ valid: true });
  });
});
