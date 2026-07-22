"""Strict, provider-neutral output contracts for CLI classifier and selector roles."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)


CLI_CLASSIFIER_RESULT_SCHEMA_VERSION = "villani.cli_classifier_result.v1"
CLI_SELECTOR_RESULT_SCHEMA_VERSION = "villani.cli_selector_result.v1"


class CliRoleFailure(str, Enum):
    EXECUTABLE_MISSING = "executable_missing"
    AUTH_MISSING = "auth_missing"
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
    INPUT_MANIFEST_VIOLATION = "input_manifest_violation"
    TARGET_MUTATION = "target_mutation"
    CANDIDATE_MUTATION = "candidate_mutation"
    CLEANUP_FAILURE = "cleanup_failure"


class _StrictRoleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _safe_relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    return bool(normalized) and not path.is_absolute() and ".." not in path.parts


class CliClassifierResult(_StrictRoleResult):
    difficulty: Literal["easy", "medium", "hard"]
    risk: Literal["low", "medium", "high"]
    category: str = Field(min_length=1, max_length=80)
    required_capabilities: list[str] = Field(max_length=32)
    uncertainty: Literal["low", "medium", "high"]
    confidence: StrictFloat = Field(ge=0, le=1)
    estimated_attempts_needed: StrictInt = Field(ge=1, le=5)
    needs_tests: StrictBool
    likely_files: list[str] = Field(max_length=32)
    reasoning_summary: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_lists(self) -> "CliClassifierResult":
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("required_capabilities must contain unique values")
        if any(not item.strip() for item in self.required_capabilities):
            raise ValueError("required_capabilities must not contain empty values")
        if len(self.likely_files) != len(set(self.likely_files)):
            raise ValueError("likely_files must contain unique values")
        if any(not _safe_relative_path(item) for item in self.likely_files):
            raise ValueError("likely_files must contain safe repository-relative paths")
        return self


class CliSelectorResult(_StrictRoleResult):
    selected_candidate_id: str = Field(min_length=1, max_length=128)
    ranking: list[str] = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_ranking(self) -> "CliSelectorResult":
        if len(self.ranking) != len(set(self.ranking)):
            raise ValueError("ranking candidate IDs must be unique")
        if self.ranking[0] != self.selected_candidate_id:
            raise ValueError("selected_candidate_id must be first in ranking")
        return self


class DuplicateJsonFieldError(ValueError):
    """Raised before Pydantic can erase duplicate JSON object keys."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonFieldError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _parse_unique_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("role result must be one JSON object")
    return value


def normalize_cli_classifier_result(
    raw: str, *, repository_inventory: set[str]
) -> CliClassifierResult:
    result = CliClassifierResult.model_validate(_parse_unique_object(raw), strict=True)
    unknown = sorted(set(result.likely_files) - repository_inventory)
    if unknown:
        raise ValueError(
            "classifier returned likely_files outside the supplied inventory: "
            + ", ".join(unknown)
        )
    return result


def normalize_cli_selector_result(
    raw: str, *, supplied_candidate_ids: set[str]
) -> CliSelectorResult:
    result = CliSelectorResult.model_validate(_parse_unique_object(raw), strict=True)
    ranked = set(result.ranking)
    unknown = sorted(ranked - supplied_candidate_ids)
    missing = sorted(supplied_candidate_ids - ranked)
    if unknown:
        raise ValueError(
            "selector returned unknown candidate IDs: " + ", ".join(unknown)
        )
    if missing:
        raise ValueError(
            "selector omitted supplied candidate IDs: " + ", ".join(missing)
        )
    if result.selected_candidate_id not in supplied_candidate_ids:
        raise ValueError("selected_candidate_id was not supplied to the selector")
    return result


__all__ = [
    "CLI_CLASSIFIER_RESULT_SCHEMA_VERSION",
    "CLI_SELECTOR_RESULT_SCHEMA_VERSION",
    "CliClassifierResult",
    "CliRoleFailure",
    "CliSelectorResult",
    "DuplicateJsonFieldError",
    "normalize_cli_classifier_result",
    "normalize_cli_selector_result",
]
