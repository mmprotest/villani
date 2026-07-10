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
} as const;

export type VillaniSchemaVersion = keyof typeof VILLANI_SCHEMA_FILE_BY_VERSION;

export interface VillaniValidationError {
  instancePath: string;
  keyword: string;
  message: string;
}

export type VillaniValidationResult<T = VillaniProtocolDocument> =
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

  return errors;
}

export class VillaniSchemaValidator {
  private readonly validators = new Map<
    VillaniSchemaVersion,
    ValidateFunction
  >();

  constructor(repositoryRoot = resolveVillaniRepositoryRoot()) {
    const schemaRoot = join(repositoryRoot, "schemas", "v1");
    const ajv = new Ajv2020({ allErrors: true, strict: false });
    ajv.addFormat("date-time", {
      type: "string",
      validate: (value: string) =>
        /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value) &&
        Number.isFinite(Date.parse(value)),
    });

    for (const [version, filename] of Object.entries(
      VILLANI_SCHEMA_FILE_BY_VERSION,
    ) as [VillaniSchemaVersion, string][]) {
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
      !(schemaVersion in VILLANI_SCHEMA_FILE_BY_VERSION)
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
          value: value as unknown as VillaniProtocolDocument,
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

export function validateVillaniProtocol(
  value: unknown,
): VillaniValidationResult {
  defaultValidator ??= new VillaniSchemaValidator();
  return defaultValidator.validate(value);
}

export function validateVillaniEventStream(
  events: unknown[],
): VillaniValidationResult<VillaniEventEnvelope[]> {
  defaultValidator ??= new VillaniSchemaValidator();
  return defaultValidator.validateEventStream(events);
}
