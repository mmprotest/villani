"""Validation of canonical documents against root schemas and semantic rules."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ValidationError
from referencing import Registry, Resource

from .durable_io import read_jsonl_tolerant
from .protocol import PROTOCOL_MODEL_BY_VERSION, EventEnvelope, ProtocolDocument
from .protocol_v2 import PROTOCOL_V2_MODEL_BY_VERSION, ProtocolDocumentV2
from .verification_evidence import VerificationEvidenceMatrix
from .validation_coverage import ValidationCoverageReport
from .run_summary import RunSummary
from .product_run import ProductRun
from .invocation_evidence import RoleInvocationEvidence, RoleInvocationIndex
from .agent_systems.models import (
    AgentSystemIdentity,
    HarnessConformanceReport,
    HarnessDiscovery,
    HarnessResult,
)
from .agent_systems.role_models import (
    AgentInvocationIdentity,
    AgentSystemCatalog,
    RoleBindings,
)
from .cli_runtime.models import (
    CliInvocationRecord,
    CliOutputTail,
    CliProcessResult,
)
from .codex_cli.models import CodexCoderResult
from .claude_code_cli.models import ClaudeCoderResult
from .qualification.models import (
    GateCReport,
    QualificationInvalidation,
    QualificationObservation,
    QualificationSnapshot,
)
from .economics.models import (
    EconomicsObservation,
    EconomicsSnapshot,
    OnlineEvidenceUpdateReport,
    RoutePlan,
    RoutePolicy,
    RoutePolicyEvaluation,
    RoutePolicyPublication,
)
from .adaptive_verification.models import (
    AdaptiveVerificationPlan,
    BinaryVerificationDecision,
    CompactReviewPackage,
    GateDReport,
    HumanOutcome,
    SupervisionMetrics,
)
from villani_ops.evaluation_lab.models import (
    EvaluationReport,
    EvaluationSuite,
    EvaluationTask,
    EvaluationTrial,
    HumanReview,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
ROOT_SCHEMA_ROOT = REPOSITORY_ROOT / "schemas" / "v1"
PACKAGED_SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas" / "v1"
ROOT_SCHEMA_ROOT_V2 = REPOSITORY_ROOT / "schemas" / "v2"
PACKAGED_SCHEMA_ROOT_V2 = Path(__file__).resolve().parents[1] / "schemas" / "v2"
# The repository copy is normative during development; built wheels carry a
# semantically identical package-data copy so protocol validation remains local.
SCHEMA_ROOT = (
    ROOT_SCHEMA_ROOT
    if (ROOT_SCHEMA_ROOT / "event.schema.json").is_file()
    else PACKAGED_SCHEMA_ROOT
)
SCHEMA_ROOT_V2 = (
    ROOT_SCHEMA_ROOT_V2
    if (ROOT_SCHEMA_ROOT_V2 / "telemetry-envelope.schema.json").is_file()
    else PACKAGED_SCHEMA_ROOT_V2
)

# This is the sole schema-version-to-path registry used by the Python protocol.
SCHEMA_VERSION_TO_PATH: dict[str, Path] = {
    "villani.task.v1": SCHEMA_ROOT / "task.schema.json",
    "villani.run_manifest.v1": SCHEMA_ROOT / "run-manifest.schema.json",
    "villani.run_state.v1": SCHEMA_ROOT / "run-state.schema.json",
    "villani.event.v1": SCHEMA_ROOT / "event.schema.json",
    "villani.classification.v1": SCHEMA_ROOT / "classification.schema.json",
    "villani.policy_decision.v1": SCHEMA_ROOT / "policy-decision.schema.json",
    "villani.attempt.v1": SCHEMA_ROOT / "attempt.schema.json",
    "villani.verification.v1": SCHEMA_ROOT / "verification.schema.json",
    "villani.selection.v1": SCHEMA_ROOT / "selection.schema.json",
    "villani.materialization.v1": SCHEMA_ROOT / "materialization.schema.json",
    "villani.validation_coverage.v1": SCHEMA_ROOT / "validation-coverage.schema.json",
    "villani.run_summary.v1": SCHEMA_ROOT / "run-summary.schema.json",
    "villani.product_run.v1": SCHEMA_ROOT / "product-run.schema.json",
    "villani.evaluation_suite.v1": SCHEMA_ROOT / "evaluation-suite.schema.json",
    "villani.evaluation_task.v1": SCHEMA_ROOT / "evaluation-task.schema.json",
    "villani.evaluation_trial.v1": SCHEMA_ROOT / "evaluation-trial.schema.json",
    "villani.human_review.v1": SCHEMA_ROOT / "human-review.schema.json",
    "villani.evaluation_report.v1": SCHEMA_ROOT / "evaluation-report.schema.json",
    "villani.agent_system.v1": SCHEMA_ROOT / "agent-system.schema.json",
    "villani.agent_system_config.v1": SCHEMA_ROOT / "agent-system-config.schema.json",
    "villani.role_bindings.v1": SCHEMA_ROOT / "role-bindings.schema.json",
    "villani.agent_invocation_identity.v1": SCHEMA_ROOT
    / "agent-invocation-identity.schema.json",
    "villani.role_invocation_evidence.v1": SCHEMA_ROOT
    / "role-invocation-evidence.schema.json",
    "villani.role_invocation_index.v1": SCHEMA_ROOT
    / "role-invocation-index.schema.json",
    "villani.cli_invocation.v1": SCHEMA_ROOT / "cli-invocation.schema.json",
    "villani.cli_process_result.v1": SCHEMA_ROOT / "cli-process-result.schema.json",
    "villani.cli_output_tail.v1": SCHEMA_ROOT / "cli-output-tail.schema.json",
    "villani.codex_coder_result.v1": SCHEMA_ROOT / "codex-coder-result.schema.json",
    "villani.claude_coder_result.v1": SCHEMA_ROOT / "claude-coder-result.schema.json",
    "villani.harness_result.v1": SCHEMA_ROOT / "harness-result.schema.json",
    "villani.harness_conformance_report.v1": SCHEMA_ROOT
    / "harness-conformance-report.schema.json",
    "villani.harness_discovery.v1": SCHEMA_ROOT / "harness-discovery.schema.json",
    "villani.qualification_observation.v1": SCHEMA_ROOT
    / "qualification-observation.schema.json",
    "villani.qualification_invalidation.v1": SCHEMA_ROOT
    / "qualification-invalidation.schema.json",
    "villani.qualification_snapshot.v1": SCHEMA_ROOT
    / "qualification-snapshot.schema.json",
    "villani.gate_c.v1": SCHEMA_ROOT / "gate-c.schema.json",
    "villani.economics_observation.v1": SCHEMA_ROOT
    / "economics-observation.schema.json",
    "villani.economics_snapshot.v1": SCHEMA_ROOT / "economics-snapshot.schema.json",
    "villani.online_evidence_update.v1": SCHEMA_ROOT
    / "online-evidence-update.schema.json",
    "villani.route_plan.v1": SCHEMA_ROOT / "route-plan.schema.json",
    "villani.route_policy.v1": SCHEMA_ROOT / "route-policy.schema.json",
    "villani.route_policy_evaluation.v1": SCHEMA_ROOT
    / "route-policy-evaluation.schema.json",
    "villani.route_policy_publication.v1": SCHEMA_ROOT
    / "route-policy-publication.schema.json",
    "villani.adaptive_verification_plan.v1": SCHEMA_ROOT
    / "adaptive-verification-plan.schema.json",
    "villani.binary_verification_decision.v1": SCHEMA_ROOT
    / "binary-verification-decision.schema.json",
    "villani.review_package.v1": SCHEMA_ROOT / "review-package.schema.json",
    "villani.human_outcome.v1": SCHEMA_ROOT / "human-outcome.schema.json",
    "villani.supervision_metrics.v1": SCHEMA_ROOT / "supervision-metrics.schema.json",
    "villani.gate_d.v1": SCHEMA_ROOT / "gate-d.schema.json",
}
SCHEMA_V2_VERSION_TO_PATH: dict[str, Path] = {
    "villani.telemetry_envelope.v2": SCHEMA_ROOT_V2 / "telemetry-envelope.schema.json",
    "villani.resource.v2": SCHEMA_ROOT_V2 / "resource.schema.json",
    "villani.span.v2": SCHEMA_ROOT_V2 / "span.schema.json",
    "villani.artifact_descriptor.v2": SCHEMA_ROOT_V2
    / "artifact-descriptor.schema.json",
    "villani.outcome.v2": SCHEMA_ROOT_V2 / "outcome.schema.json",
    "villani.agent_capability.v2": SCHEMA_ROOT_V2 / "agent-capability.schema.json",
    "villani.verifier_capability.v2": SCHEMA_ROOT_V2
    / "verifier-capability.schema.json",
    "villani.policy_publication.v2": SCHEMA_ROOT_V2 / "policy-publication.schema.json",
    "villani.verification_evidence.v2": SCHEMA_ROOT_V2
    / "verification-evidence.schema.json",
}

ALL_SCHEMA_VERSION_TO_PATH = {**SCHEMA_VERSION_TO_PATH, **SCHEMA_V2_VERSION_TO_PATH}
ALL_PROTOCOL_MODELS: dict[str, type[BaseModel]] = {
    **PROTOCOL_MODEL_BY_VERSION,
    **PROTOCOL_V2_MODEL_BY_VERSION,
    "villani.verification_evidence.v2": VerificationEvidenceMatrix,
    "villani.validation_coverage.v1": ValidationCoverageReport,
    "villani.run_summary.v1": RunSummary,
    "villani.product_run.v1": ProductRun,
    "villani.evaluation_suite.v1": EvaluationSuite,
    "villani.evaluation_task.v1": EvaluationTask,
    "villani.evaluation_trial.v1": EvaluationTrial,
    "villani.human_review.v1": HumanReview,
    "villani.evaluation_report.v1": EvaluationReport,
    "villani.agent_system.v1": AgentSystemIdentity,
    "villani.agent_system_config.v1": AgentSystemCatalog,
    "villani.role_bindings.v1": RoleBindings,
    "villani.agent_invocation_identity.v1": AgentInvocationIdentity,
    "villani.role_invocation_evidence.v1": RoleInvocationEvidence,
    "villani.role_invocation_index.v1": RoleInvocationIndex,
    "villani.cli_invocation.v1": CliInvocationRecord,
    "villani.cli_process_result.v1": CliProcessResult,
    "villani.cli_output_tail.v1": CliOutputTail,
    "villani.codex_coder_result.v1": CodexCoderResult,
    "villani.claude_coder_result.v1": ClaudeCoderResult,
    "villani.harness_result.v1": HarnessResult,
    "villani.harness_conformance_report.v1": HarnessConformanceReport,
    "villani.harness_discovery.v1": HarnessDiscovery,
    "villani.qualification_observation.v1": QualificationObservation,
    "villani.qualification_invalidation.v1": QualificationInvalidation,
    "villani.qualification_snapshot.v1": QualificationSnapshot,
    "villani.gate_c.v1": GateCReport,
    "villani.economics_observation.v1": EconomicsObservation,
    "villani.economics_snapshot.v1": EconomicsSnapshot,
    "villani.online_evidence_update.v1": OnlineEvidenceUpdateReport,
    "villani.route_plan.v1": RoutePlan,
    "villani.route_policy.v1": RoutePolicy,
    "villani.route_policy_evaluation.v1": RoutePolicyEvaluation,
    "villani.route_policy_publication.v1": RoutePolicyPublication,
    "villani.adaptive_verification_plan.v1": AdaptiveVerificationPlan,
    "villani.binary_verification_decision.v1": BinaryVerificationDecision,
    "villani.review_package.v1": CompactReviewPackage,
    "villani.human_outcome.v1": HumanOutcome,
    "villani.supervision_metrics.v1": SupervisionMetrics,
    "villani.gate_d.v1": GateDReport,
}


def _pointer(parts: Iterable[object]) -> str:
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "" if not encoded else "/" + "/".join(encoded)


@dataclass(frozen=True, slots=True)
class ProtocolValidationIssue:
    instance_path: str
    keyword: str
    message: str


class ProtocolValidationError(ValueError):
    def __init__(self, issues: Sequence[ProtocolValidationIssue]) -> None:
        self.issues = tuple(issues)
        detail = "; ".join(
            f"{issue.instance_path or '/'} [{issue.keyword}] {issue.message}"
            for issue in self.issues
        )
        super().__init__(detail or "protocol validation failed")


@lru_cache(maxsize=None)
def _schema_registry() -> Registry:
    resources: list[tuple[str, Resource[Any]]] = []
    for path in set(ALL_SCHEMA_VERSION_TO_PATH.values()):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schema_id = schema.get("$id") if isinstance(schema, Mapping) else None
        if isinstance(schema_id, str) and schema_id:
            resources.append((schema_id, Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def _validator(schema_version: str) -> Draft202012Validator:
    schema_path = ALL_SCHEMA_VERSION_TO_PATH[schema_version]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
        registry=_schema_registry(),
    )


def _accounting_issues(
    document: Mapping[str, Any],
    value_keys: tuple[str, ...],
    status_key: str,
    path: tuple[object, ...] = (),
) -> list[ProtocolValidationIssue]:
    if status_key not in document or any(key not in document for key in value_keys):
        return []
    status = document[status_key]
    values = [document[key] for key in value_keys]
    if status == "complete" and any(value is None for value in values):
        return [
            ProtocolValidationIssue(
                _pointer(
                    (*path, next(key for key in value_keys if document[key] is None))
                ),
                "accounting_status",
                f"complete {status_key} requires non-null accounting data",
            )
        ]
    if status in {"unknown", "not_applicable"} and any(
        value is not None for value in values
    ):
        return [
            ProtocolValidationIssue(
                _pointer(
                    (
                        *path,
                        next(key for key in value_keys if document[key] is not None),
                    )
                ),
                "accounting_status",
                f"{status} {status_key} requires null accounting data",
            )
        ]
    return []


def _validate_accounting(document: Mapping[str, Any]) -> list[ProtocolValidationIssue]:
    version = document.get("schema_version")
    issues: list[ProtocolValidationIssue] = []
    if version == "villani.run_manifest.v1":
        issues.extend(
            _accounting_issues(document, ("total_cost_usd",), "cost_accounting_status")
        )
        issues.extend(
            _accounting_issues(
                document,
                ("total_input_tokens", "total_output_tokens"),
                "token_accounting_status",
            )
        )
        issues.extend(
            _accounting_issues(
                document, ("total_duration_ms",), "duration_accounting_status"
            )
        )
    elif version == "villani.attempt.v1":
        issues.extend(
            _accounting_issues(document, ("cost_usd",), "cost_accounting_status")
        )
        issues.extend(
            _accounting_issues(
                document,
                ("input_tokens", "output_tokens"),
                "token_accounting_status",
            )
        )
        issues.extend(
            _accounting_issues(document, ("duration_ms",), "duration_accounting_status")
        )
    elif version == "villani.policy_decision.v1":
        considered = document.get("considered_backends")
        if isinstance(considered, list):
            for index, backend in enumerate(considered):
                if isinstance(backend, Mapping):
                    issues.extend(
                        _accounting_issues(
                            backend,
                            ("estimated_cost_usd",),
                            "cost_accounting_status",
                            ("considered_backends", index),
                        )
                    )
        for budget_name in ("budget_before", "budget_after"):
            budget = document.get(budget_name)
            if isinstance(budget, Mapping):
                issues.extend(
                    _accounting_issues(
                        budget,
                        ("remaining_cost_usd",),
                        "cost_accounting_status",
                        (budget_name,),
                    )
                )
                issues.extend(
                    _accounting_issues(
                        budget,
                        ("remaining_wall_time_ms",),
                        "duration_accounting_status",
                        (budget_name,),
                    )
                )
    elif version == "villani.selection.v1":
        rankings = document.get("rankings")
        if isinstance(rankings, list):
            for index, ranking in enumerate(rankings):
                if isinstance(ranking, Mapping):
                    issues.extend(
                        _accounting_issues(
                            ranking,
                            ("actual_cost_usd",),
                            "cost_accounting_status",
                            ("rankings", index),
                        )
                    )
    elif version == "villani.qualification_observation.v1":
        issues.extend(
            _accounting_issues(
                document,
                ("cost_amount", "cost_currency"),
                "cost_accounting_status",
            )
        )
        issues.extend(
            _accounting_issues(
                document,
                ("duration_ms",),
                "duration_accounting_status",
            )
        )
    elif version == "villani.economics_observation.v1":
        for component_name in (
            "execution_cost",
            "verification_cost",
            "human_review_cost",
            "retry_escalation_cost",
        ):
            component = document.get(component_name)
            if isinstance(component, Mapping):
                issues.extend(
                    _accounting_issues(
                        component,
                        ("amount", "currency"),
                        "accounting_status",
                        (component_name,),
                    )
                )
        duration = document.get("duration")
        if isinstance(duration, Mapping):
            issues.extend(
                _accounting_issues(
                    duration,
                    ("duration_ms",),
                    "accounting_status",
                    ("duration",),
                )
            )
    return issues


def _semantic_issues(document: Mapping[str, Any]) -> list[ProtocolValidationIssue]:
    issues: list[ProtocolValidationIssue] = []
    version = document.get("schema_version")

    if version in {
        "villani.evaluation_suite.v1",
        "villani.evaluation_task.v1",
        "villani.evaluation_trial.v1",
        "villani.human_review.v1",
        "villani.evaluation_report.v1",
        "villani.qualification_observation.v1",
        "villani.qualification_invalidation.v1",
        "villani.qualification_snapshot.v1",
        "villani.gate_c.v1",
        "villani.economics_observation.v1",
        "villani.economics_snapshot.v1",
        "villani.online_evidence_update.v1",
        "villani.route_plan.v1",
        "villani.route_policy.v1",
        "villani.route_policy_evaluation.v1",
        "villani.route_policy_publication.v1",
        "villani.adaptive_verification_plan.v1",
        "villani.binary_verification_decision.v1",
        "villani.review_package.v1",
        "villani.human_outcome.v1",
        "villani.supervision_metrics.v1",
        "villani.gate_d.v1",
    }:
        try:
            ALL_PROTOCOL_MODELS[str(version)].model_validate(document)
        except ValidationError as error:
            issues.extend(
                ProtocolValidationIssue(
                    _pointer(item["loc"]),
                    "semantic_contract",
                    str(item["msg"]),
                )
                for item in error.errors(include_url=False)
            )

    if (
        version == "villani.verification.v1"
        and document.get("acceptance_eligible") is True
    ):
        if (
            document.get("outcome") != "accepted"
            or document.get("recommended_action") != "accept"
        ):
            issues.append(
                ProtocolValidationIssue(
                    "/acceptance_eligible",
                    "acceptance_eligibility",
                    "true requires outcome=accepted and recommended_action=accept",
                )
            )

    if version == "villani.selection.v1":
        eligible = document.get("eligible_candidate_ids")
        selected = document.get("selected_candidate_ids")
        if isinstance(eligible, list) and isinstance(selected, list):
            eligible_ids = {item for item in eligible if isinstance(item, str)}
            for index, candidate_id in enumerate(selected):
                if isinstance(candidate_id, str) and candidate_id not in eligible_ids:
                    issues.append(
                        ProtocolValidationIssue(
                            f"/selected_candidate_ids/{index}",
                            "selection_eligibility",
                            f"{candidate_id!r} is not in eligible_candidate_ids",
                        )
                    )

    if (
        version == "villani.run_state.v1"
        and document.get("state") == "COMPLETED"
        and document.get("terminal") is not True
    ):
        issues.append(
            ProtocolValidationIssue(
                "/terminal",
                "terminal_state",
                "a completed state must be terminal",
            )
        )

    issues.extend(_validate_accounting(document))
    if version == "villani.outcome.v2":
        issues.extend(_accounting_issues(document, ("cost",), "cost_accounting_status"))
        issues.extend(
            _accounting_issues(document, ("latency_ms",), "latency_accounting_status")
        )
        if document.get("cost") is None and document.get("currency") is not None:
            issues.append(
                ProtocolValidationIssue(
                    "/currency",
                    "accounting_status",
                    "currency must be null when cost is null",
                )
            )
        if document.get("cost") is not None and document.get("currency") is None:
            issues.append(
                ProtocolValidationIssue(
                    "/currency",
                    "accounting_status",
                    "currency is required when cost is known",
                )
            )
    return issues


def collect_protocol_validation_issues(value: Any) -> list[ProtocolValidationIssue]:
    if not isinstance(value, Mapping):
        return [
            ProtocolValidationIssue("", "type", "protocol document must be an object")
        ]

    schema_version = value.get("schema_version")
    if not isinstance(schema_version, str):
        return [
            ProtocolValidationIssue(
                "/schema_version", "required", "schema_version must be present"
            )
        ]
    if schema_version not in ALL_SCHEMA_VERSION_TO_PATH:
        return [
            ProtocolValidationIssue(
                "/schema_version",
                "schema_version",
                f"unsupported schema_version {schema_version!r}",
            )
        ]

    issues = [
        ProtocolValidationIssue(
            _pointer(error.absolute_path),
            error.validator or "schema",
            error.message,
        )
        for error in sorted(
            _validator(schema_version).iter_errors(value),
            key=lambda error: (_pointer(error.absolute_path), str(error.validator)),
        )
    ]
    issues.extend(_semantic_issues(value))
    return issues


def validate_protocol_document(value: Any) -> None:
    issues = collect_protocol_validation_issues(value)
    if issues:
        raise ProtocolValidationError(issues)


def parse_protocol_document(
    value: Any,
) -> (
    ProtocolDocument
    | ProtocolDocumentV2
    | VerificationEvidenceMatrix
    | ValidationCoverageReport
    | RunSummary
    | ProductRun
    | EvaluationSuite
    | EvaluationTask
    | EvaluationTrial
    | HumanReview
    | EvaluationReport
    | CliInvocationRecord
    | CliProcessResult
    | CliOutputTail
    | QualificationObservation
    | QualificationInvalidation
    | QualificationSnapshot
    | GateCReport
    | EconomicsObservation
    | EconomicsSnapshot
    | OnlineEvidenceUpdateReport
    | RoutePlan
    | RoutePolicy
    | RoutePolicyEvaluation
    | RoutePolicyPublication
    | AdaptiveVerificationPlan
    | BinaryVerificationDecision
    | CompactReviewPackage
    | HumanOutcome
    | SupervisionMetrics
    | GateDReport
):
    validate_protocol_document(value)
    schema_version = value["schema_version"]
    return cast(
        ProtocolDocument
        | ProtocolDocumentV2
        | VerificationEvidenceMatrix
        | ValidationCoverageReport
        | RunSummary
        | ProductRun
        | EvaluationSuite
        | EvaluationTask
        | EvaluationTrial
        | HumanReview
        | EvaluationReport
        | CliInvocationRecord
        | CliProcessResult
        | CliOutputTail
        | QualificationObservation
        | QualificationInvalidation
        | QualificationSnapshot
        | GateCReport
        | EconomicsObservation
        | EconomicsSnapshot
        | OnlineEvidenceUpdateReport
        | RoutePlan
        | RoutePolicy
        | RoutePolicyEvaluation
        | RoutePolicyPublication
        | AdaptiveVerificationPlan
        | BinaryVerificationDecision
        | CompactReviewPackage
        | HumanOutcome
        | SupervisionMetrics
        | GateDReport,
        ALL_PROTOCOL_MODELS[schema_version].model_validate(value),
    )


def validate_event_stream(events: Iterable[Mapping[str, Any]]) -> list[EventEnvelope]:
    parsed: list[EventEnvelope] = []
    previous_sequence: int | None = None
    issues: list[ProtocolValidationIssue] = []

    for index, event in enumerate(events):
        event_issues = collect_protocol_validation_issues(event)
        issues.extend(
            ProtocolValidationIssue(
                f"/{index}{issue.instance_path}", issue.keyword, issue.message
            )
            for issue in event_issues
        )
        sequence = event.get("sequence") if isinstance(event, Mapping) else None
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            if previous_sequence is not None and sequence <= previous_sequence:
                issues.append(
                    ProtocolValidationIssue(
                        f"/{index}/sequence",
                        "event_sequence",
                        "event sequences must strictly increase",
                    )
                )
            previous_sequence = sequence
        if not event_issues:
            parsed.append(EventEnvelope.model_validate(event))

    if issues:
        raise ProtocolValidationError(issues)
    return parsed


def validate_jsonl_event_stream(
    path: str | Path,
) -> list[EventEnvelope]:
    return validate_event_stream(read_jsonl_tolerant(path))
