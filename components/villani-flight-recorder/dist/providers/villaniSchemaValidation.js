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
    "villani.validation_coverage.v1": "validation-coverage.schema.json",
    "villani.run_summary.v1": "run-summary.schema.json",
    "villani.agent_system.v1": "agent-system.schema.json",
    "villani.harness_result.v1": "harness-result.schema.json",
    "villani.harness_conformance_report.v1": "harness-conformance-report.schema.json",
    "villani.harness_discovery.v1": "harness-discovery.schema.json",
    "villani.qualification_observation.v1": "qualification-observation.schema.json",
    "villani.qualification_invalidation.v1": "qualification-invalidation.schema.json",
    "villani.qualification_snapshot.v1": "qualification-snapshot.schema.json",
    "villani.gate_c.v1": "gate-c.schema.json",
    "villani.economics_observation.v1": "economics-observation.schema.json",
    "villani.economics_snapshot.v1": "economics-snapshot.schema.json",
    "villani.online_evidence_update.v1": "online-evidence-update.schema.json",
    "villani.route_plan.v1": "route-plan.schema.json",
    "villani.route_policy.v1": "route-policy.schema.json",
    "villani.route_policy_evaluation.v1": "route-policy-evaluation.schema.json",
    "villani.route_policy_publication.v1": "route-policy-publication.schema.json",
    "villani.adaptive_verification_plan.v1": "adaptive-verification-plan.schema.json",
    "villani.binary_verification_decision.v1": "binary-verification-decision.schema.json",
    "villani.review_package.v1": "review-package.schema.json",
    "villani.human_outcome.v1": "human-outcome.schema.json",
    "villani.supervision_metrics.v1": "supervision-metrics.schema.json",
    "villani.gate_d.v1": "gate-d.schema.json",
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
function adaptiveMoneyIssues(value, instancePath) {
    if (!isRecord(value))
        return [];
    const known = value.accounting_status === "complete" || value.accounting_status === "partial";
    const hasMoney = value.amount !== null && value.currency !== null;
    if (known !== hasMoney) {
        return [{
                instancePath,
                keyword: "accounting_status",
                message: known
                    ? "known money requires amount and currency"
                    : "unknown money must remain null",
            }];
    }
    return [];
}
function adaptiveDurationIssues(value, instancePath) {
    if (!isRecord(value))
        return [];
    const known = value.accounting_status === "complete" || value.accounting_status === "partial";
    if (known !== (value.duration_ms !== null)) {
        return [{
                instancePath,
                keyword: "accounting_status",
                message: known
                    ? "known duration requires duration_ms"
                    : "unknown duration must remain null",
            }];
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
    if (version === "villani.agent_system.v1") {
        const digest = document.configuration_digest;
        if (typeof digest === "string" &&
            document.system_id !== `asys_${digest.replace(/^sha256:/, "")}`) {
            errors.push({
                instancePath: "/system_id",
                keyword: "content_addressed_identity",
                message: "system_id must be derived from configuration_digest",
            });
        }
        if (document.production_enabled === true &&
            ["disabled", "unsupported", "unqualified"].includes(String(document.qualification_status))) {
            errors.push({
                instancePath: "/qualification_status",
                keyword: "production_qualification",
                message: "enabled systems cannot be disabled, unsupported, or unqualified",
            });
        }
        if (document.qualification_status === "qualified" &&
            !(Array.isArray(document.qualification_references) &&
                document.qualification_references.some((reference) => isRecord(reference) && reference.kind === "conformance"))) {
            errors.push({
                instancePath: "/qualification_references",
                keyword: "conformance_qualification",
                message: "qualified systems require conformance evidence",
            });
        }
    }
    if (version === "villani.qualification_observation.v1") {
        const requiredTruth = Boolean(document.baseline_valid === true &&
            document.candidate_evidence_complete === true &&
            document.authoritative_verification_complete === true &&
            document.infrastructure_status === "resolved" &&
            (document.human_review_required !== true ||
                document.human_review_status === "complete") &&
            document.corruption_detected === false &&
            document.secret_issue_detected === false);
        if (document.eligible !== requiredTruth) {
            errors.push({
                instancePath: "/eligible",
                keyword: "qualification_eligibility",
                message: "eligible must exactly reflect the PT7 evidence rules",
            });
        }
        const expectedSuccess = document.eligible === true
            ? document.proved_acceptable === true &&
                (document.human_review_required !== true ||
                    document.accepted_as_is === true) &&
                document.false_acceptance === false &&
                document.later_rollback === false &&
                document.reopened_defect === false
            : null;
        if (document.successful !== expectedSuccess) {
            errors.push({
                instancePath: "/successful",
                keyword: "qualification_success",
                message: "success requires proved acceptable and accepted as-is evidence",
            });
        }
        if ((document.eligible === true && document.exclusion_reason !== null) ||
            (document.eligible === false &&
                (typeof document.exclusion_reason !== "string" ||
                    document.exclusion_reason.length === 0))) {
            errors.push({
                instancePath: "/exclusion_reason",
                keyword: "qualification_exclusion",
                message: "exclusions are persisted only for ineligible observations",
            });
        }
        errors.push(...accountingIssues(document, ["cost_amount", "cost_currency"], "cost_accounting_status"), ...accountingIssues(document, ["duration_ms"], "duration_accounting_status"));
    }
    if (version === "villani.gate_c.v1" && Array.isArray(document.checks)) {
        const statuses = new Set(document.checks.filter(isRecord).map((check) => String(check.status)));
        const expected = statuses.has("fail")
            ? "FAIL"
            : statuses.has("insufficient_evidence")
                ? "INSUFFICIENT_EVIDENCE"
                : "PASS";
        if (document.status !== expected) {
            errors.push({
                instancePath: "/status",
                keyword: "gate_status",
                message: `Gate C status must be ${expected}`,
            });
        }
    }
    if (version === "villani.route_plan.v1") {
        const considered = Array.isArray(document.systems_considered)
            ? document.systems_considered.filter(isRecord)
            : [];
        const eligible = new Set(considered
            .filter((item) => item.eligible === true)
            .map((item) => String(item.route_name)));
        if (typeof document.selected_first_system === "string" &&
            !eligible.has(document.selected_first_system)) {
            errors.push({
                instancePath: "/selected_first_system",
                keyword: "route_eligibility",
                message: "selected system must be an eligible consideration",
            });
        }
        if (Array.isArray(document.ordered_fallbacks)) {
            document.ordered_fallbacks.forEach((route, index) => {
                if (typeof route === "string" && !eligible.has(route)) {
                    errors.push({
                        instancePath: `/ordered_fallbacks/${index}`,
                        keyword: "route_eligibility",
                        message: "fallback system must be an eligible consideration",
                    });
                }
            });
        }
        if (document.forced_choice === document.automatic_policy_metrics_eligible) {
            errors.push({
                instancePath: "/automatic_policy_metrics_eligible",
                keyword: "forced_policy_metric_exclusion",
                message: "forced choices are excluded from automatic policy metrics",
            });
        }
        considered.forEach((item, index) => {
            const objective = isRecord(item.objective) ? item.objective : null;
            if (!objective)
                return;
            if (objective.accounting_status === "complete" &&
                (objective.expected_accepted_change_cost === null ||
                    (Array.isArray(objective.unknown_components) &&
                        objective.unknown_components.length > 0))) {
                errors.push({
                    instancePath: `/systems_considered/${index}/objective`,
                    keyword: "accepted_change_accounting",
                    message: "complete objectives require a numeric full total and no unknowns",
                });
            }
            if (objective.accounting_status === "partial" &&
                objective.expected_accepted_change_cost !== null) {
                errors.push({
                    instancePath: `/systems_considered/${index}/objective/expected_accepted_change_cost`,
                    keyword: "accepted_change_accounting",
                    message: "partial objectives cannot claim a full expected total",
                });
            }
        });
    }
    if (version === "villani.economics_observation.v1") {
        const expectedProfileEligibility = Boolean(document.qualification_eligible === true &&
            document.authoritative_verification_complete === true &&
            document.infrastructure_status === "resolved" &&
            document.false_acceptance === false);
        if (document.eligible_for_profile !== expectedProfileEligibility) {
            errors.push({
                instancePath: "/eligible_for_profile",
                keyword: "economics_eligibility",
                message: "economics profile eligibility must match verified evidence",
            });
        }
        if (document.eligible_for_automatic_policy_metrics !==
            (expectedProfileEligibility && document.forced_choice !== true)) {
            errors.push({
                instancePath: "/eligible_for_automatic_policy_metrics",
                keyword: "automatic_policy_metrics",
                message: "forced or excluded outcomes cannot train automatic policy",
            });
        }
        for (const componentName of [
            "execution_cost",
            "verification_cost",
            "human_review_cost",
            "retry_escalation_cost",
        ]) {
            const component = document[componentName];
            if (isRecord(component)) {
                errors.push(...accountingIssues(component, ["amount", "currency"], "accounting_status", `/${componentName}`));
            }
        }
    }
    if (version === "villani.route_policy_evaluation.v1") {
        const expectedSafe = Boolean(Number(document.frozen_case_count) > 0 &&
            document.conservative_reliability_non_decreasing === true &&
            document.false_acceptance_exposure_non_increasing === true &&
            Array.isArray(document.rejection_reasons) &&
            document.rejection_reasons.length === 0);
        if (document.safe_to_publish !== expectedSafe) {
            errors.push({
                instancePath: "/safe_to_publish",
                keyword: "policy_publication_safety",
                message: "safe_to_publish must be derived from fail-closed replay checks",
            });
        }
    }
    if (version === "villani.online_evidence_update.v1") {
        const recorded = document.status === "recorded";
        if (recorded !==
            Boolean(document.qualification_observation_id &&
                document.economics_observation_id &&
                document.profile_updated === true)) {
            errors.push({
                instancePath: "/status",
                keyword: "online_evidence_update",
                message: "only recorded updates may report an updated economics profile",
            });
        }
        if (!recorded &&
            (!Array.isArray(document.reasons) || document.reasons.length === 0)) {
            errors.push({
                instancePath: "/reasons",
                keyword: "online_evidence_update",
                message: "non-recorded updates require an explicit reason",
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
                    if (event.name === "permission_request" &&
                        !("request_id" in payload && "permission" in payload)) {
                        errors.push({
                            instancePath: `/normalized_events/${index}/payload`,
                            keyword: "permission_request",
                            message: "permission requests require request_id and permission",
                        });
                    }
                    if (event.name === "permission_resolution" &&
                        !("request_id" in payload && "resolution" in payload)) {
                        errors.push({
                            instancePath: `/normalized_events/${index}/payload`,
                            keyword: "permission_resolution",
                            message: "permission resolutions require request_id and resolution",
                        });
                    }
                }
            });
            if (new TextEncoder().encode(JSON.stringify(normalizedEvents)).length >
                32 * 1024 * 1024) {
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
                const normalized = typeof changed === "string" ? changed.replaceAll("\\", "/") : "";
                if (!normalized ||
                    normalized.startsWith("/") ||
                    /^[A-Za-z]:/.test(normalized) ||
                    normalized.split("/").includes("..")) {
                    errors.push({
                        instancePath: `/changed_files/${index}`,
                        keyword: "path_safety",
                        message: "changed files must be worktree-relative safe paths",
                    });
                }
            });
        }
        const worktree = typeof document.isolated_worktree === "string"
            ? document.isolated_worktree.replaceAll("\\", "/")
            : "";
        if (!worktree || worktree.split("/").includes("..")) {
            errors.push({
                instancePath: "/isolated_worktree",
                keyword: "worktree_safety",
                message: "isolated worktree cannot contain parent traversal",
            });
        }
        if (typeof document.stdout === "string" &&
            new TextEncoder().encode(document.stdout).length > 8 * 1024 * 1024) {
            errors.push({
                instancePath: "/stdout",
                keyword: "message_bound",
                message: "stdout exceeds the harness message bound",
            });
        }
        if (typeof document.stderr === "string" &&
            new TextEncoder().encode(document.stderr).length > 8 * 1024 * 1024) {
            errors.push({
                instancePath: "/stderr",
                keyword: "message_bound",
                message: "stderr exceeds the harness message bound",
            });
        }
        if (Array.isArray(document.artifacts)) {
            document.artifacts.forEach((artifact, index) => {
                const artifactPath = isRecord(artifact) && typeof artifact.path === "string"
                    ? artifact.path.replaceAll("\\", "/")
                    : "";
                if (!artifactPath ||
                    artifactPath.startsWith("/") ||
                    /^[A-Za-z]:/.test(artifactPath) ||
                    artifactPath.split("/").includes("..")) {
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
            errors.push(...accountingIssues(cost, ["amount"], "accounting_status", "/cost"));
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
            "successful_patch",
            "no_patch",
            "command_recovery",
            "permission_request",
            "rate_limit_retry",
            "unsupported_version",
            "schema_change",
            "missing_final_result",
            "partial_patch_on_crash",
            "known_cost",
            "non_ascii_spaced_paths",
            "large_output",
            "outside_isolation_mutation",
        ]);
        const checkIds = checks
            .filter(isRecord)
            .map((check) => String(check.check_id));
        if (checkIds.length !== requiredChecks.size ||
            new Set(checkIds).size !== requiredChecks.size ||
            checkIds.some((checkId) => !requiredChecks.has(checkId))) {
            errors.push({
                instancePath: "/checks",
                keyword: "required_conformance_checks",
                message: "conformance report must contain every required check once",
            });
        }
        checks.filter(isRecord).forEach((check, index) => {
            if (check.status === "pass" &&
                (!isRecord(check.evidence) || Object.keys(check.evidence).length === 0)) {
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
        if (document.production_qualification_authorized === true &&
            expected !== "passed") {
            errors.push({
                instancePath: "/production_qualification_authorized",
                keyword: "fail_closed_qualification",
                message: "qualification cannot be authorized unless every check passed",
            });
        }
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
    if (version === "villani.binary_verification_decision.v1") {
        const accepting = document.decision === 1;
        const blockers = Array.isArray(document.blockers) ? document.blockers : [];
        const notProved = Array.isArray(document.requirements_not_proved)
            ? document.requirements_not_proved
            : [];
        const nodes = Array.isArray(document.node_results)
            ? document.node_results.filter(isRecord)
            : [];
        const blockingStatuses = new Set([
            "failed",
            "unavailable",
            "infrastructure_error",
            "not_run",
        ]);
        if (accepting &&
            (document.semantic_status !== "passed" ||
                document.infrastructure_status !== "resolved" ||
                blockers.length > 0 ||
                notProved.length > 0 ||
                (document.independent_verifier_required === true &&
                    document.independent_verifier_completed !== true) ||
                nodes.some((node) => blockingStatuses.has(String(node.status))))) {
            errors.push({
                instancePath: "/decision",
                keyword: "binary_verification_authority",
                message: "decision 1 requires complete acceptance-grade semantic evidence",
            });
        }
        if (["unclear", "error", "not_invoked"].includes(String(document.semantic_status)) &&
            document.decision !== 0) {
            errors.push({
                instancePath: "/decision",
                keyword: "binary_normalization",
                message: "unclear, error, and missing semantic results normalize to zero",
            });
        }
        errors.push(...adaptiveMoneyIssues(document.verification_cost, "/verification_cost"));
    }
    if (version === "villani.review_package.v1") {
        if (document.status === "ready_to_apply" &&
            ((Array.isArray(document.requirements_not_proved) && document.requirements_not_proved.length > 0) ||
                document.unresolved_decision !== null ||
                (Array.isArray(document.checks) &&
                    document.checks.filter(isRecord).some((check) => check.status !== "passed")))) {
            errors.push({
                instancePath: "/status",
                keyword: "review_package_authority",
                message: "ready packages require complete passing proof",
            });
        }
        if (document.status === "needs_review" && !document.unresolved_decision) {
            errors.push({
                instancePath: "/unresolved_decision",
                keyword: "review_package_authority",
                message: "needs-review packages require the exact unresolved decision",
            });
        }
        errors.push(...adaptiveMoneyIssues(document.known_cost, "/known_cost"), ...adaptiveDurationIssues(document.known_duration, "/known_duration"));
    }
    if (version === "villani.human_outcome.v1") {
        const known = document.review_time_accounting_status === "complete";
        if (known !== (document.review_minutes !== null)) {
            errors.push({
                instancePath: "/review_minutes",
                keyword: "accounting_status",
                message: known
                    ? "complete review time requires minutes"
                    : "unknown review time must remain null",
            });
        }
        const fullTraceKnown = document.full_trace_accounting_status === "complete";
        if (fullTraceKnown !== (typeof document.full_trace_opened === "boolean")) {
            errors.push({
                instancePath: "/full_trace_opened",
                keyword: "accounting_status",
                message: fullTraceKnown
                    ? "complete full-trace accounting requires a boolean"
                    : "unknown full-trace use must remain null",
            });
        }
        if (document.outcome === "corrected_before_use" && !document.correction_summary) {
            errors.push({
                instancePath: "/correction_summary",
                keyword: "human_outcome",
                message: "corrected outcomes require a correction summary",
            });
        }
        if (["reverted", "reopened_defect"].includes(String(document.outcome)) && !document.linked_reference) {
            errors.push({
                instancePath: "/linked_reference",
                keyword: "human_outcome",
                message: "later adverse outcomes require an explicit reference",
            });
        }
    }
    if (version === "villani.supervision_metrics.v1") {
        const known = ["complete", "partial"].includes(String(document.review_time_accounting_status));
        if (known !== (document.explicit_review_minutes !== null)) {
            errors.push({
                instancePath: "/explicit_review_minutes",
                keyword: "accounting_status",
                message: known
                    ? "known review time requires minutes"
                    : "unknown review time must remain null",
            });
        }
        if (document.full_trace_accounting_status !== "complete" &&
            document.application_without_full_trace_count !== 0) {
            errors.push({
                instancePath: "/application_without_full_trace_count",
                keyword: "accounting_status",
                message: "unknown full-trace use cannot claim an observed count",
            });
        }
        errors.push(...adaptiveMoneyIssues(document.verification_cost, "/verification_cost"), ...adaptiveMoneyIssues(document.review_cost, "/review_cost"), ...adaptiveMoneyIssues(document.total_accepted_change_cost, "/total_accepted_change_cost"));
    }
    if (version === "villani.gate_d.v1") {
        const arms = Array.isArray(document.arms) ? document.arms.filter(isRecord) : [];
        arms.forEach((arm, index) => {
            errors.push(...adaptiveMoneyIssues(arm.total_cost, `/arms/${index}/total_cost`), ...adaptiveDurationIssues(arm.elapsed_duration, `/arms/${index}/elapsed_duration`));
            const known = ["complete", "partial"].includes(String(arm.review_time_accounting_status));
            if (known !== (arm.review_minutes !== null)) {
                errors.push({
                    instancePath: `/arms/${index}/review_minutes`,
                    keyword: "accounting_status",
                    message: "review-time value must match its accounting status",
                });
            }
        });
        const statuses = Array.isArray(document.checks)
            ? document.checks.filter(isRecord).map((check) => String(check.status))
            : [];
        if ((document.status === "PASS" && statuses.some((status) => status !== "pass")) ||
            (document.status === "FAIL" && !statuses.includes("fail")) ||
            (document.status === "INSUFFICIENT_EVIDENCE" && !statuses.includes("insufficient_evidence")) ||
            document.next_milestone_permitted !== (document.status === "PASS")) {
            errors.push({
                instancePath: "/status",
                keyword: "gate_d_authority",
                message: "Gate D status and milestone permission must follow its checks",
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
export function defaultVillaniSchemaValidator() {
    defaultValidator ??= new VillaniSchemaValidator();
    return defaultValidator;
}
export function validateVillaniProtocol(value) {
    return defaultVillaniSchemaValidator().validate(value);
}
export function validateVillaniEventStream(events) {
    return defaultVillaniSchemaValidator().validateEventStream(events);
}
export function readVillaniV2Document(path, validator = defaultVillaniSchemaValidator()) {
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
