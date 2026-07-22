"""Strict provider-neutral contracts for CLI semantic verification."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Any, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    ValidationError,
    model_validator,
)


CLI_VERIFIER_RESULT_SCHEMA_VERSION = "villani.cli_verifier_result.v1"
CLI_VERIFIER_NORMALIZED_RESULT_SCHEMA_VERSION = (
    "villani.cli_verifier_normalized_result.v1"
)


class CliVerifierFailure(str, Enum):
    SEMANTIC_REJECTION = "semantic_rejection"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EXECUTABLE_MISSING = "verifier_executable_missing"
    AUTH_MISSING = "verifier_auth_missing"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    PERMISSION_FAILURE = "permission_failure"
    MALFORMED_OUTPUT = "malformed_output"
    SCHEMA_FAILURE = "schema_failure"
    MISSING_FINAL_RESULT = "missing_final_result"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    PROCESS_CRASH = "process_crash"
    ARTIFACT_PREPARATION_FAILURE = "artifact_preparation_failure"
    BASELINE_COPY_FAILURE = "baseline_copy_failure"
    INPUT_MANIFEST_VIOLATION = "input_manifest_violation"
    INDEPENDENCE_VIOLATION = "independence_violation"
    CLEANUP_FAILURE = "cleanup_failure"


class StrictVerifierModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CliVerifierBlockingIssue(StrictVerifierModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    summary: str = Field(min_length=1, max_length=1000)
    evidence_reference: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_evidence_reference(self) -> "CliVerifierBlockingIssue":
        value = self.evidence_reference.replace("\\", "/")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] != "input"
        ):
            raise ValueError(
                "evidence_reference must be a safe path beneath supplied input/"
            )
        return self


class CliVerifierResult(StrictVerifierModel):
    """The exact model-produced result. Its schema is versioned externally."""

    decision: Annotated[int, Strict(), Field(ge=0, le=1)]
    reason: str = Field(min_length=1, max_length=1000)
    requirements_proved: list[str]
    requirements_not_proved: list[str]
    blocking_issues: list[CliVerifierBlockingIssue]

    @model_validator(mode="after")
    def validate_sets(self) -> "CliVerifierResult":
        for name in ("requirements_proved", "requirements_not_proved"):
            values = getattr(self, name)
            if values != list(dict.fromkeys(values)):
                raise ValueError(f"{name} must not contain duplicate requirement ids")
            if any(not value for value in values):
                raise ValueError(f"{name} must not contain an empty requirement id")
        overlap = set(self.requirements_proved).intersection(
            self.requirements_not_proved
        )
        if overlap:
            raise ValueError("a requirement cannot be both proved and not proved")
        codes = [item.code for item in self.blocking_issues]
        if codes != list(dict.fromkeys(codes)):
            raise ValueError("blocking_issues must not contain duplicate codes")
        if self.decision == 1 and (
            self.requirements_not_proved or self.blocking_issues
        ):
            raise ValueError(
                "decision 1 cannot retain unproved requirements or blocking issues"
            )
        if self.decision == 0 and not (
            self.requirements_not_proved or self.blocking_issues
        ):
            raise ValueError(
                "decision 0 requires an unproved requirement or blocking issue"
            )
        return self


class DuplicateJsonFieldError(ValueError):
    """Raised before model validation when JSON repeats an object member."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonFieldError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def normalize_cli_verifier_result(
    raw_text: str,
    *,
    requirement_ids: set[str],
) -> CliVerifierResult:
    """Parse once, reject duplicates, and require an exact requirement partition."""

    try:
        value = json.loads(raw_text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, DuplicateJsonFieldError) as error:
        raise ValueError(f"malformed verifier JSON: {error}") from error
    if not isinstance(value, Mapping):
        raise ValueError("verifier result must be a JSON object")
    try:
        result = CliVerifierResult.model_validate(value)
    except ValidationError as error:
        raise ValueError(f"verifier result schema failure: {error}") from error
    supplied = set(result.requirements_proved).union(result.requirements_not_proved)
    unknown = sorted(supplied - requirement_ids)
    missing = sorted(requirement_ids - supplied)
    if unknown:
        raise ValueError(
            "verifier result contains unknown requirement id(s): " + ", ".join(unknown)
        )
    if missing:
        raise ValueError(
            "verifier result omits requirement id(s): " + ", ".join(missing)
        )
    if result.decision == 1 and set(result.requirements_proved) != requirement_ids:
        raise ValueError("decision 1 must prove every supplied requirement")
    return result


__all__ = [
    "CLI_VERIFIER_NORMALIZED_RESULT_SCHEMA_VERSION",
    "CLI_VERIFIER_RESULT_SCHEMA_VERSION",
    "CliVerifierBlockingIssue",
    "CliVerifierFailure",
    "CliVerifierResult",
    "DuplicateJsonFieldError",
    "normalize_cli_verifier_result",
]
