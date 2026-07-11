import { existsSync, readFileSync } from "node:fs";
import { dirname, join, parse, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Ajv2020, } from "ajv/dist/2020.js";
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
};
export const VILLANI_V2_SCHEMA_FILE_BY_VERSION = {
    "villani.telemetry_envelope.v2": "telemetry-envelope.schema.json",
    "villani.resource.v2": "resource.schema.json",
    "villani.span.v2": "span.schema.json",
    "villani.artifact_descriptor.v2": "artifact-descriptor.schema.json",
    "villani.outcome.v2": "outcome.schema.json",
    "villani.agent_capability.v2": "agent-capability.schema.json",
    "villani.verifier_capability.v2": "verifier-capability.schema.json",
    "villani.policy_publication.v2": "policy-publication.schema.json",
};
const ALL_SCHEMA_FILE_BY_VERSION = {
    ...VILLANI_SCHEMA_FILE_BY_VERSION,
    ...VILLANI_V2_SCHEMA_FILE_BY_VERSION,
};
function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}
function findRootFrom(start) {
    let current = resolve(start);
    const filesystemRoot = parse(current).root;
    while (true) {
        if (existsSync(join(current, "schemas", "v1", "event.schema.json"))) {
            return current;
        }
        if (current === filesystemRoot)
            return undefined;
        current = dirname(current);
    }
}
export function resolveVillaniRepositoryRoot() {
    const moduleDirectory = dirname(fileURLToPath(import.meta.url));
    for (const candidate of [process.cwd(), moduleDirectory]) {
        const root = findRootFrom(candidate);
        if (root)
            return root;
    }
    throw new Error("Unable to locate the Villani root schemas directory");
}
function ajvErrors(errors) {
    return (errors ?? []).map((error) => ({
        instancePath: error.instancePath,
        keyword: error.keyword,
        message: error.message ?? "schema validation failed",
    }));
}
function accountingIssues(document, valueKeys, statusKey, instancePath = "") {
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
function semanticErrors(document) {
    const errors = [];
    const version = document.schema_version;
    if (version === "villani.verification.v1" &&
        document.acceptance_eligible === true &&
        (document.outcome !== "accepted" ||
            document.recommended_action !== "accept")) {
        errors.push({
            instancePath: "/acceptance_eligible",
            keyword: "acceptance_eligibility",
            message: "true requires outcome=accepted and recommended_action=accept",
        });
    }
    if (version === "villani.selection.v1" &&
        Array.isArray(document.eligible_candidate_ids) &&
        Array.isArray(document.selected_candidate_ids)) {
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
    if (version === "villani.run_state.v1" &&
        document.state === "COMPLETED" &&
        document.terminal !== true) {
        errors.push({
            instancePath: "/terminal",
            keyword: "terminal_state",
            message: "a completed state must be terminal",
        });
    }
    if (version === "villani.run_manifest.v1") {
        errors.push(...accountingIssues(document, ["total_cost_usd"], "cost_accounting_status"), ...accountingIssues(document, ["total_input_tokens", "total_output_tokens"], "token_accounting_status"), ...accountingIssues(document, ["total_duration_ms"], "duration_accounting_status"));
    }
    else if (version === "villani.attempt.v1") {
        errors.push(...accountingIssues(document, ["cost_usd"], "cost_accounting_status"), ...accountingIssues(document, ["input_tokens", "output_tokens"], "token_accounting_status"), ...accountingIssues(document, ["duration_ms"], "duration_accounting_status"));
    }
    else if (version === "villani.policy_decision.v1") {
        if (Array.isArray(document.considered_backends)) {
            document.considered_backends.forEach((backend, index) => {
                if (isRecord(backend)) {
                    errors.push(...accountingIssues(backend, ["estimated_cost_usd"], "cost_accounting_status", `/considered_backends/${index}`));
                }
            });
        }
        for (const budgetName of ["budget_before", "budget_after"]) {
            const budget = document[budgetName];
            if (isRecord(budget)) {
                errors.push(...accountingIssues(budget, ["remaining_cost_usd"], "cost_accounting_status", `/${budgetName}`), ...accountingIssues(budget, ["remaining_wall_time_ms"], "duration_accounting_status", `/${budgetName}`));
            }
        }
    }
    else if (version === "villani.selection.v1" &&
        Array.isArray(document.rankings)) {
        document.rankings.forEach((ranking, index) => {
            if (isRecord(ranking)) {
                errors.push(...accountingIssues(ranking, ["actual_cost_usd"], "cost_accounting_status", `/rankings/${index}`));
            }
        });
    }
    if (version === "villani.outcome.v2") {
        errors.push(...accountingIssues(document, ["cost"], "cost_accounting_status"), ...accountingIssues(document, ["latency_ms"], "latency_accounting_status"));
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
    validators = new Map();
    constructor(repositoryRoot = resolveVillaniRepositoryRoot()) {
        const ajv = new Ajv2020({ allErrors: true, strict: false });
        ajv.addFormat("date-time", {
            type: "string",
            validate: (value) => /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value) &&
                Number.isFinite(Date.parse(value)),
        });
        for (const [version, filename] of Object.entries(ALL_SCHEMA_FILE_BY_VERSION)) {
            const schemaRoot = join(repositoryRoot, "schemas", version.endsWith(".v2") ? "v2" : "v1");
            const schema = JSON.parse(readFileSync(join(schemaRoot, filename), "utf8"));
            this.validators.set(version, ajv.compile(schema));
        }
    }
    validate(value) {
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
        if (typeof schemaVersion !== "string" ||
            !(schemaVersion in ALL_SCHEMA_FILE_BY_VERSION)) {
            return {
                valid: false,
                errors: [
                    {
                        instancePath: "/schema_version",
                        keyword: "schema_version",
                        message: typeof schemaVersion === "string"
                            ? `unsupported schema_version ${JSON.stringify(schemaVersion)}`
                            : "schema_version must be present",
                    },
                ],
            };
        }
        const validator = this.validators.get(schemaVersion);
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
                value: value,
                errors: [],
            };
    }
    validateEventStream(events) {
        const errors = [];
        const parsed = [];
        let previousSequence;
        events.forEach((event, index) => {
            const result = this.validate(event);
            if (!result.valid) {
                errors.push(...result.errors.map((error) => ({
                    ...error,
                    instancePath: `/${index}${error.instancePath}`,
                })));
            }
            else if (result.value.schema_version !== "villani.event.v1") {
                errors.push({
                    instancePath: `/${index}/schema_version`,
                    keyword: "schema_version",
                    message: "event stream entries must use villani.event.v1",
                });
            }
            else {
                parsed.push(result.value);
            }
            if (isRecord(event) && Number.isInteger(event.sequence)) {
                const sequence = event.sequence;
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
let defaultValidator;
export function validateVillaniProtocol(value) {
    defaultValidator ??= new VillaniSchemaValidator();
    return defaultValidator.validate(value);
}
export function validateVillaniEventStream(events) {
    defaultValidator ??= new VillaniSchemaValidator();
    return defaultValidator.validateEventStream(events);
}
export function readVillaniV2Document(path, validator = new VillaniSchemaValidator()) {
    const value = JSON.parse(readFileSync(path, "utf8"));
    const result = validator.validate(value);
    if (!result.valid) {
        const detail = result.errors
            .map((error) => `${error.instancePath || "/"} [${error.keyword}] ${error.message}`)
            .join("; ");
        throw new Error(`Invalid Villani v2 document: ${detail}`);
    }
    if (!(result.value.schema_version in VILLANI_V2_SCHEMA_FILE_BY_VERSION)) {
        throw new Error("Expected a Villani v2 protocol document");
    }
    return result.value;
}
