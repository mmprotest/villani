import { existsSync, readFileSync } from "node:fs";
import { dirname, join, parse, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  Ajv2020,
  type AnySchemaObject,
  type ErrorObject,
  type ValidateFunction,
} from "ajv/dist/2020.js";

import type {
  VillaniEventEnvelope,
  VillaniProtocolDocument,
} from "./villaniProtocol.js";
import type { VillaniProtocolDocumentV2 } from "./villaniProtocolV2.js";

export const VILLANI_SCHEMA_FILE_BY_VERSION = {
  "villani.task.v1": "task.schema.json",
  "villani.run_manifest.v1": "run-manifest.schema.json",
  "villani.run_state.v1": "run-state.schema.json",
  "villani.event.v1": "event.schema.json",
  "villani.classification.v1": "classification.schema.json",
  "villani.policy_decision.v1": "policy-decision.schema.json",
  "villani.attempt.v1": "attempt.schema.json",
  "villani.verification.v1": "verification.schema.json",
  "villani.selection.v1": "selection.schema.json",
  "villani.materialization.v1": "materialization.schema.json",
  "villani.validation_coverage.v1": "validation-coverage.schema.json",
  "villani.run_summary.v1": "run-summary.schema.json",
  "villani.agent_system.v1": "agent-system.schema.json",
  "villani.harness_result.v1": "harness-result.schema.json",
  "villani.harness_conformance_report.v1":
    "harness-conformance-report.schema.json",
} as const;

export const VILLANI_V2_SCHEMA_FILE_BY_VERSION = {
  "villani.telemetry_envelope.v2": "telemetry-envelope.schema.json",
  "villani.resource.v2": "resource.schema.json",
  "villani.span.v2": "span.schema.json",
  "villani.artifact_descriptor.v2": "artifact-descriptor.schema.json",
  "villani.outcome.v2": "outcome.schema.json",
  "villani.agent_capability.v2": "agent-capability.schema.json",
  "villani.verifier_capability.v2": "verifier-capability.schema.json",
  "villani.policy_publication.v2": "policy-publication.schema.json",
} as const;

const ALL_SCHEMA_FILE_BY_VERSION = {
  ...VILLANI_SCHEMA_FILE_BY_VERSION,
  ...VILLANI_V2_SCHEMA_FILE_BY_VERSION,
} as const;

export type VillaniSchemaVersion = keyof typeof ALL_SCHEMA_FILE_BY_VERSION;

export interface VillaniValidationError {
  instancePath: string;
  keyword: string;
  message: string;
}

export type VillaniValidationResult<
  T = VillaniProtocolDocument | VillaniProtocolDocumentV2,
> =
  | { valid: true; value: T; errors: [] }
  | { valid: false; errors: VillaniValidationError[] };

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function findRootFrom(start: string): string | undefined {
  let current = resolve(start);
  const filesystemRoot = parse(current).root;
  while (true) {
    if (existsSync(join(current, "schemas", "v1", "event.schema.json"))) {
      return current;
    }
    if (current === filesystemRoot) return undefined;
    current = dirname(current);
  }
}

export function resolveVillaniRepositoryRoot(): string {
  const moduleDirectory = dirname(fileURLToPath(import.meta.url));
  for (const candidate of [process.cwd(), moduleDirectory]) {
    const root = findRootFrom(candidate);
    if (root) return root;
  }
  throw new Error("Unable to locate the Villani root schemas directory");
}

function ajvErrors(
  errors: ErrorObject[] | null | undefined,
): VillaniValidationError[] {
  return (errors ?? []).map((error) => ({
    instancePath: error.instancePath,
    keyword: error.keyword,
    message: error.message ?? "schema validation failed",
  }));
}

function accountingIssues(
  document: Record<string, unknown>,
  valueKeys: string[],
  statusKey: string,
  instancePath = "",
): VillaniValidationError[] {
  if (!(statusKey in document) || valueKeys.some((key) => !(key in document))) {
    return [];
  }
  const status = document[statusKey];
  if (status === "complete") {
    const missing = valueKeys.find((key) => document[key] === null);
    if (missing) {
      return [
        {
          instancePath: `${instancePath}/${missing}`,
          keyword: "accounting_status",
          message: `complete ${statusKey} requires non-null accounting data`,
        },
      ];
    }
  }
  if (status === "unknown" || status === "not_applicable") {
    const captured = valueKeys.find((key) => document[key] !== null);
    if (captured) {
      return [
        {
          instancePath: `${instancePath}/${captured}`,
          keyword: "accounting_status",
          message: `${status} ${statusKey} requires null accounting data`,
        },
      ];
    }
  }
  return [];
}

function semanticErrors(
  document: Record<string, unknown>,
): VillaniValidationError[] {
  const errors: VillaniValidationError[] = [];
  const version = document.schema_version;

  if (
    version === "villani.verification.v1" &&
    document.acceptance_eligible === true &&
    (document.outcome !== "accepted" ||
      document.recommended_action !== "accept")
  ) {
    errors.push({
      instancePath: "/acceptance_eligible",
      keyword: "acceptance_eligibility",
      message: "true requires outcome=accepted and recommended_action=accept",
    });
  }

  if (
    version === "villani.selection.v1" &&
    Array.isArray(document.eligible_candidate_ids) &&
    Array.isArray(document.selected_candidate_ids)
  ) {
    const eligible = new Set(document.eligible_candidate_ids);
    document.selected_candidate_ids.forEach((candidateId, index) => {
      if (typeof candidateId === "string" && !eligible.has(candidateId)) {
        errors.push({
          instancePath: `/selected_candidate_ids/${index}`,
          keyword: "selection_eligibility",
          message: `${JSON.stringify(candidateId)} is not in eligible_candidate_ids`,
        });
      }
    });
  }

  if (
    version === "villani.run_state.v1" &&
    document.state === "COMPLETED" &&
    document.terminal !== true
  ) {
    errors.push({
      instancePath: "/terminal",
      keyword: "terminal_state",
      message: "a completed state must be terminal",
    });
  }

  if (version === "villani.run_manifest.v1") {
    errors.push(
      ...accountingIssues(
        document,
        ["total_cost_usd"],
        "cost_accounting_status",
      ),
      ...accountingIssues(
        document,
        ["total_input_tokens", "total_output_tokens"],
        "token_accounting_status",
      ),
      ...accountingIssues(
        document,
        ["total_duration_ms"],
        "duration_accounting_status",
      ),
    );
  } else if (version === "villani.attempt.v1") {
    errors.push(
      ...accountingIssues(document, ["cost_usd"], "cost_accounting_status"),
      ...accountingIssues(
        document,
        ["input_tokens", "output_tokens"],
        "token_accounting_status",
      ),
      ...accountingIssues(
        document,
        ["duration_ms"],
        "duration_accounting_status",
      ),
    );
  } else if (version === "villani.policy_decision.v1") {
    if (Array.isArray(document.considered_backends)) {
      document.considered_backends.forEach((backend, index) => {
        if (isRecord(backend)) {
          errors.push(
            ...accountingIssues(
              backend,
              ["estimated_cost_usd"],
              "cost_accounting_status",
              `/considered_backends/${index}`,
            ),
          );
        }
      });
    }
    for (const budgetName of ["budget_before", "budget_after"] as const) {
      const budget = document[budgetName];
      if (isRecord(budget)) {
        errors.push(
          ...accountingIssues(
            budget,
            ["remaining_cost_usd"],
            "cost_accounting_status",
            `/${budgetName}`,
          ),
          ...accountingIssues(
            budget,
            ["remaining_wall_time_ms"],
            "duration_accounting_status",
            `/${budgetName}`,
          ),
        );
      }
    }
  } else if (
    version === "villani.selection.v1" &&
    Array.isArray(document.rankings)
  ) {
    document.rankings.forEach((ranking, index) => {
      if (isRecord(ranking)) {
        errors.push(
          ...accountingIssues(
            ranking,
            ["actual_cost_usd"],
            "cost_accounting_status",
            `/rankings/${index}`,
          ),
        );
      }
    });
  }

  if (version === "villani.agent_system.v1") {
    const digest = document.configuration_digest;
    if (
      typeof digest === "string" &&
      document.system_id !== `asys_${digest.replace(/^sha256:/, "")}`
    ) {
      errors.push({
        instancePath: "/system_id",
        keyword: "content_addressed_identity",
        message: "system_id must be derived from configuration_digest",
      });
    }
    if (
      document.production_enabled === true &&
      !["qualified", "bootstrap"].includes(
        String(document.qualification_status),
      )
    ) {
      errors.push({
        instancePath: "/qualification_status",
        keyword: "production_qualification",
        message: "enabled systems must be qualified or bootstrap",
      });
    }
    if (
      document.qualification_status === "qualified" &&
      !(
        Array.isArray(document.qualification_references) &&
        document.qualification_references.some(
          (reference) =>
            isRecord(reference) && reference.kind === "conformance",
        )
      )
    ) {
      errors.push({
        instancePath: "/qualification_references",
        keyword: "conformance_qualification",
        message: "qualified systems require conformance evidence",
      });
    }
  }

  if (version === "villani.harness_result.v1") {
    const normalizedEvents = document.normalized_events;
    if (Array.isArray(normalizedEvents)) {
      let previousTimestamp = Number.NEGATIVE_INFINITY;
      normalizedEvents.forEach((event, index) => {
        if (isRecord(event) && event.sequence !== index + 1) {
          errors.push({
            instancePath: `/normalized_events/${index}/sequence`,
            keyword: "event_sequence",
            message: "normalized events must be contiguous and ordered",
          });
        }
        if (isRecord(event)) {
          const timestamp = Date.parse(String(event.timestamp));
          if (timestamp < previousTimestamp) {
            errors.push({
              instancePath: `/normalized_events/${index}/timestamp`,
              keyword: "event_ordering",
              message: "normalized event timestamps must be ordered",
            });
          }
          previousTimestamp = timestamp;
          const payload = isRecord(event.payload) ? event.payload : {};
          if (
            event.name === "permission_request" &&
            !("request_id" in payload && "permission" in payload)
          ) {
            errors.push({
              instancePath: `/normalized_events/${index}/payload`,
              keyword: "permission_request",
              message: "permission requests require request_id and permission",
            });
          }
          if (
            event.name === "permission_resolution" &&
            !("request_id" in payload && "resolution" in payload)
          ) {
            errors.push({
              instancePath: `/normalized_events/${index}/payload`,
              keyword: "permission_resolution",
              message:
                "permission resolutions require request_id and resolution",
            });
          }
        }
      });
      if (
        new TextEncoder().encode(JSON.stringify(normalizedEvents)).length >
        32 * 1024 * 1024
      ) {
        errors.push({
          instancePath: "/normalized_events",
          keyword: "backpressure_bound",
          message: "normalized events exceed the bounded event buffer",
        });
      }
    }
    const changedFiles = document.changed_files;
    if (Array.isArray(changedFiles)) {
      changedFiles.forEach((changed, index) => {
        const normalized =
          typeof changed === "string" ? changed.replaceAll("\\", "/") : "";
        if (
          !normalized ||
          normalized.startsWith("/") ||
          /^[A-Za-z]:/.test(normalized) ||
          normalized.split("/").includes("..")
        ) {
          errors.push({
            instancePath: `/changed_files/${index}`,
            keyword: "path_safety",
            message: "changed files must be worktree-relative safe paths",
          });
        }
      });
    }
    const worktree =
      typeof document.isolated_worktree === "string"
        ? document.isolated_worktree.replaceAll("\\", "/")
        : "";
    if (!worktree || worktree.split("/").includes("..")) {
      errors.push({
        instancePath: "/isolated_worktree",
        keyword: "worktree_safety",
        message: "isolated worktree cannot contain parent traversal",
      });
    }
    if (
      typeof document.stdout === "string" &&
      new TextEncoder().encode(document.stdout).length > 8 * 1024 * 1024
    ) {
      errors.push({
        instancePath: "/stdout",
        keyword: "message_bound",
        message: "stdout exceeds the harness message bound",
      });
    }
    if (
      typeof document.stderr === "string" &&
      new TextEncoder().encode(document.stderr).length > 8 * 1024 * 1024
    ) {
      errors.push({
        instancePath: "/stderr",
        keyword: "message_bound",
        message: "stderr exceeds the harness message bound",
      });
    }
    if (Array.isArray(document.artifacts)) {
      document.artifacts.forEach((artifact, index) => {
        const artifactPath =
          isRecord(artifact) && typeof artifact.path === "string"
            ? artifact.path.replaceAll("\\", "/")
            : "";
        if (
          !artifactPath ||
          artifactPath.startsWith("/") ||
          /^[A-Za-z]:/.test(artifactPath) ||
          artifactPath.split("/").includes("..")
        ) {
          errors.push({
            instancePath: `/artifacts/${index}/path`,
            keyword: "artifact_path_safety",
            message: "artifact paths must be run-relative and safe",
          });
        }
      });
    }
    const cost = document.cost;
    if (isRecord(cost)) {
      errors.push(
        ...accountingIssues(cost, ["amount"], "accounting_status", "/cost"),
      );
      if (cost.amount === null && cost.currency !== null) {
        errors.push({
          instancePath: "/cost/currency",
          keyword: "accounting_status",
          message: "currency must be null when cost is unknown",
        });
      }
    }
  }

  if (version === "villani.harness_conformance_report.v1") {
    const checks = Array.isArray(document.checks) ? document.checks : [];
    const requiredChecks = new Set([
      "manifest",
      "protocol_negotiation",
      "version_capture",
      "worktree_enforcement",
      "path_safety",
      "event_ordering",
      "cancellation",
      "timeout",
      "malformed_output",
      "oversized_output",
      "process_crash",
      "missing_executable",
      "permissions",
      "artifacts",
      "patch_correctness",
      "cleanup",
      "secret_redaction",
      "unknown_cost",
      "cross_platform_paths",
    ]);
    const checkIds = checks
      .filter(isRecord)
      .map((check) => String(check.check_id));
    if (
      checkIds.length !== requiredChecks.size ||
      new Set(checkIds).size !== requiredChecks.size ||
      checkIds.some((checkId) => !requiredChecks.has(checkId))
    ) {
      errors.push({
        instancePath: "/checks",
        keyword: "required_conformance_checks",
        message: "conformance report must contain every required check once",
      });
    }
    checks.filter(isRecord).forEach((check, index) => {
      if (
        check.status === "pass" &&
        (!isRecord(check.evidence) || Object.keys(check.evidence).length === 0)
      ) {
        errors.push({
          instancePath: `/checks/${index}/evidence`,
          keyword: "conformance_evidence",
          message: "passing conformance checks require evidence",
        });
      }
    });
    const statuses = checks
      .filter(isRecord)
      .map((check) => String(check.status));
    const expected = statuses.includes("fail")
      ? "failed"
      : statuses.includes("not_run")
        ? "insufficient_evidence"
        : "passed";
    if (document.status !== expected) {
      errors.push({
        instancePath: "/status",
        keyword: "conformance_status",
        message: `status must be ${expected}`,
      });
    }
    if (
      document.production_qualification_authorized !==
      (expected === "passed")
    ) {
      errors.push({
        instancePath: "/production_qualification_authorized",
        keyword: "fail_closed_qualification",
        message: "qualification is authorized only when every check passed",
      });
    }
  }

  if (version === "villani.outcome.v2") {
    errors.push(
      ...accountingIssues(document, ["cost"], "cost_accounting_status"),
      ...accountingIssues(
        document,
        ["latency_ms"],
        "latency_accounting_status",
      ),
    );
    if (document.cost === null && document.currency !== null) {
      errors.push({
        instancePath: "/currency",
        keyword: "accounting_status",
        message: "currency must be null when cost is null",
      });
    }
    if (document.cost !== null && document.currency === null) {
      errors.push({
        instancePath: "/currency",
        keyword: "accounting_status",
        message: "currency is required when cost is known",
      });
    }
  }

  return errors;
}

export class VillaniSchemaValidator {
  private readonly validators = new Map<
    VillaniSchemaVersion,
    ValidateFunction
  >();

  constructor(repositoryRoot = resolveVillaniRepositoryRoot()) {
    const ajv = new Ajv2020({ allErrors: true, strict: false });
    ajv.addFormat("date-time", {
      type: "string",
      validate: (value: string) =>
        /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value) &&
        Number.isFinite(Date.parse(value)),
    });

    for (const [version, filename] of Object.entries(
      ALL_SCHEMA_FILE_BY_VERSION,
    ) as [VillaniSchemaVersion, string][]) {
      const schemaRoot = join(
        repositoryRoot,
        "schemas",
        version.endsWith(".v2") ? "v2" : "v1",
      );
      const schema = JSON.parse(
        readFileSync(join(schemaRoot, filename), "utf8"),
      ) as AnySchemaObject;
      this.validators.set(version, ajv.compile(schema));
    }
  }

  validate(value: unknown): VillaniValidationResult {
    if (!isRecord(value)) {
      return {
        valid: false,
        errors: [
          {
            instancePath: "",
            keyword: "type",
            message: "protocol document must be an object",
          },
        ],
      };
    }

    const schemaVersion = value.schema_version;
    if (
      typeof schemaVersion !== "string" ||
      !(schemaVersion in ALL_SCHEMA_FILE_BY_VERSION)
    ) {
      return {
        valid: false,
        errors: [
          {
            instancePath: "/schema_version",
            keyword: "schema_version",
            message:
              typeof schemaVersion === "string"
                ? `unsupported schema_version ${JSON.stringify(schemaVersion)}`
                : "schema_version must be present",
          },
        ],
      };
    }

    const validator = this.validators.get(
      schemaVersion as VillaniSchemaVersion,
    );
    if (!validator) {
      throw new Error(`Validator was not loaded for ${schemaVersion}`);
    }
    const schemaValid = validator(value);
    const errors = [
      ...(schemaValid ? [] : ajvErrors(validator.errors)),
      ...semanticErrors(value),
    ];
    return errors.length
      ? { valid: false, errors }
      : {
          valid: true,
          value: value as unknown as
            VillaniProtocolDocument | VillaniProtocolDocumentV2,
          errors: [],
        };
  }

  validateEventStream(
    events: unknown[],
  ): VillaniValidationResult<VillaniEventEnvelope[]> {
    const errors: VillaniValidationError[] = [];
    const parsed: VillaniEventEnvelope[] = [];
    let previousSequence: number | undefined;

    events.forEach((event, index) => {
      const result = this.validate(event);
      if (!result.valid) {
        errors.push(
          ...result.errors.map((error) => ({
            ...error,
            instancePath: `/${index}${error.instancePath}`,
          })),
        );
      } else if (result.value.schema_version !== "villani.event.v1") {
        errors.push({
          instancePath: `/${index}/schema_version`,
          keyword: "schema_version",
          message: "event stream entries must use villani.event.v1",
        });
      } else {
        parsed.push(result.value);
      }

      if (isRecord(event) && Number.isInteger(event.sequence)) {
        const sequence = event.sequence as number;
        if (previousSequence !== undefined && sequence <= previousSequence) {
          errors.push({
            instancePath: `/${index}/sequence`,
            keyword: "event_sequence",
            message: "event sequences must strictly increase",
          });
        }
        previousSequence = sequence;
      }
    });

    return errors.length
      ? { valid: false, errors }
      : { valid: true, value: parsed, errors: [] };
  }
}

let defaultValidator: VillaniSchemaValidator | undefined;

export function defaultVillaniSchemaValidator(): VillaniSchemaValidator {
  defaultValidator ??= new VillaniSchemaValidator();
  return defaultValidator;
}

export function validateVillaniProtocol(
  value: unknown,
): VillaniValidationResult {
  return defaultVillaniSchemaValidator().validate(value);
}

export function validateVillaniEventStream(
  events: unknown[],
): VillaniValidationResult<VillaniEventEnvelope[]> {
  return defaultVillaniSchemaValidator().validateEventStream(events);
}

export function readVillaniV2Document(
  path: string,
  validator = defaultVillaniSchemaValidator(),
): VillaniProtocolDocumentV2 {
  const value = JSON.parse(readFileSync(path, "utf8")) as unknown;
  const result = validator.validate(value);
  if (!result.valid) {
    const detail = result.errors
      .map(
        (error) =>
          `${error.instancePath || "/"} [${error.keyword}] ${error.message}`,
      )
      .join("; ");
    throw new Error(`Invalid Villani v2 document: ${detail}`);
  }
  if (!(result.value.schema_version in VILLANI_V2_SCHEMA_FILE_BY_VERSION)) {
    throw new Error("Expected a Villani v2 protocol document");
  }
  return result.value as VillaniProtocolDocumentV2;
}
