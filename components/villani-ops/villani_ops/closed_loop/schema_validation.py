"""Validation of canonical documents against root schemas and semantic rules."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel

from .durable_io import read_jsonl_tolerant
from .protocol import PROTOCOL_MODEL_BY_VERSION, EventEnvelope, ProtocolDocument
from .protocol_v2 import PROTOCOL_V2_MODEL_BY_VERSION, ProtocolDocumentV2
from .verification_evidence import VerificationEvidenceMatrix


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
def _validator(schema_version: str) -> Draft202012Validator:
    schema_path = ALL_SCHEMA_VERSION_TO_PATH[schema_version]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


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
    return issues


def _semantic_issues(document: Mapping[str, Any]) -> list[ProtocolValidationIssue]:
    issues: list[ProtocolValidationIssue] = []
    version = document.get("schema_version")

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
) -> ProtocolDocument | ProtocolDocumentV2 | VerificationEvidenceMatrix:
    validate_protocol_document(value)
    schema_version = value["schema_version"]
    return cast(
        ProtocolDocument | ProtocolDocumentV2 | VerificationEvidenceMatrix,
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
