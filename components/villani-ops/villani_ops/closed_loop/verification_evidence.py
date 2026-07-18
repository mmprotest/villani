"""Typed verification evidence and the deterministic acceptance decision."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from villani_ops.execution_environment.models import CandidateCommandResult


EvidenceType: TypeAlias = Literal[
    "repository_validation",
    "focused_probe",
    "static_patch_evidence",
    "source_inspection",
    "debug_trace",
    "semantic_reasoning",
]
DeterministicStatus: TypeAlias = Literal[
    "passed",
    "failed",
    "missing",
    "not_applicable",
    "infrastructure_error",
]
SemanticStatus: TypeAlias = Literal[
    "passed",
    "failed",
    "unclear",
    "not_assessed",
]
RequirementFinalStatus: TypeAlias = Literal[
    "passed",
    "failed",
    "missing",
    "infrastructure_error",
]
FinalReasonCode: TypeAlias = Literal[
    "accepted",
    "candidate_ineligible",
    "empty_patch",
    "repository_validation_failed",
    "repository_validation_unavailable",
    "repository_validation_infrastructure_error",
    "focused_probe_failed",
    "focused_probe_missing",
    "critical_requirement_missing",
    "critical_requirement_failed",
    "semantic_verifier_rejected",
    "semantic_verifier_unclear",
    "verifier_tool_failure",
    "verifier_malformed_output",
    "evidence_contradiction",
]
RequirementSource: TypeAlias = Literal[
    "task_instruction",
    "success_criteria",
    "repository_validation_command",
    "semantic_review",
]


class StrictVerificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequirementEvidence(StrictVerificationModel):
    requirement_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    critical: bool
    evidence_type: EvidenceType
    evidence_ids: list[str]
    deterministic_status: DeterministicStatus
    semantic_status: SemanticStatus
    contradiction: bool
    final_status: RequirementFinalStatus
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_references(self) -> "RequirementEvidence":
        if self.critical and not self.evidence_ids:
            raise ValueError(
                "critical requirements require at least one evidence reference"
            )
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("requirement evidence references must be unique")
        return self


class VerificationEvidenceMatrix(StrictVerificationModel):
    schema_version: Literal["villani.verification_evidence.v2"]
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    requirements: list[RequirementEvidence]
    repository_validation_status: str = Field(min_length=1)
    candidate_eligibility_status: str = Field(min_length=1)
    semantic_verifier_status: str = Field(min_length=1)
    critical_requirements_proven: bool
    contradictions_present: bool
    infrastructure_failure_present: bool
    final_result: Literal[0, 1]
    final_reason_code: FinalReasonCode
    final_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_summary(self) -> "VerificationEvidenceMatrix":
        critical_proven = all(
            item.final_status == "passed" for item in self.requirements if item.critical
        )
        if self.critical_requirements_proven != critical_proven:
            raise ValueError("critical_requirements_proven contradicts requirements")
        if self.contradictions_present != any(
            item.contradiction for item in self.requirements
        ):
            raise ValueError("contradictions_present contradicts requirements")
        expected_infrastructure = (
            self.repository_validation_status == "infrastructure_error"
            or any(
                item.final_status == "infrastructure_error"
                for item in self.requirements
            )
        )
        if self.infrastructure_failure_present != expected_infrastructure:
            raise ValueError(
                "infrastructure_failure_present contradicts requirement evidence"
            )
        if self.final_result == 1 and self.final_reason_code != "accepted":
            raise ValueError("result 1 requires final_reason_code=accepted")
        if self.final_result == 0 and self.final_reason_code == "accepted":
            raise ValueError("result 0 cannot use final_reason_code=accepted")
        return self


class RequirementDefinition(StrictVerificationModel):
    requirement_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    critical: bool = True
    observable: bool = False
    source: RequirementSource


class FocusedProbeTemporaryFile(StrictVerificationModel):
    path: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    content: str | None = Field(default=None, exclude=True, repr=False)
    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_bytes: int = Field(ge=0, le=262_144)

    @model_validator(mode="before")
    @classmethod
    def derive_content_identity(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        output = dict(value)
        content = output.get("content")
        if isinstance(content, str):
            encoded = content.encode("utf-8")
            output.setdefault(
                "content_sha256", f"sha256:{hashlib.sha256(encoded).hexdigest()}"
            )
            output.setdefault("content_bytes", len(encoded))
        return output

    @model_validator(mode="after")
    def validate_file(self) -> "FocusedProbeTemporaryFile":
        posix = PurePosixPath(self.path.replace("\\", "/"))
        windows = PureWindowsPath(self.path)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or ".." in posix.parts
            or self.path.endswith(("/", "\\"))
        ):
            raise ValueError("focused probe temporary file must be a safe relative file")
        if self.content is not None:
            encoded = self.content.encode("utf-8")
            if len(encoded) != self.content_bytes:
                raise ValueError("temporary file byte count does not match content")
            digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
            if digest != self.content_sha256:
                raise ValueError("temporary file digest does not match content")
        return self


class FocusedProbeTemporaryFileEvidence(StrictVerificationModel):
    path: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_bytes: int = Field(ge=0, le=262_144)
    created: bool
    removed: bool


class FocusedProbeRequest(StrictVerificationModel):
    probe_id: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=3600)
    expected_exit_code: int
    expected_stdout: str | None = None
    expected_stdout_contains: list[str] = Field(default_factory=list)
    expected_stderr_contains: list[str] = Field(default_factory=list)
    temporary_files: list[FocusedProbeTemporaryFile] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_probe(self) -> "FocusedProbeRequest":
        if any(not value for value in self.argv):
            raise ValueError("focused probe argv must contain non-empty strings")
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("focused probe requirement_ids must be unique")
        if len(self.expected_stdout_contains) != len(
            set(self.expected_stdout_contains)
        ):
            raise ValueError("expected_stdout_contains must be unique")
        if len(self.expected_stderr_contains) != len(
            set(self.expected_stderr_contains)
        ):
            raise ValueError("expected_stderr_contains must be unique")
        paths = [item.path.replace("\\", "/") for item in self.temporary_files]
        if len(paths) != len(set(paths)):
            raise ValueError("focused probe temporary file paths must be unique")
        return self


class FocusedProbeResult(StrictVerificationModel):
    probe_id: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    request: FocusedProbeRequest
    command_result: CandidateCommandResult
    status: Literal["passed", "failed", "infrastructure_error"]
    evidence_id: str = Field(min_length=1)
    effective_timeout_seconds: int = Field(ge=1)
    reason: str = Field(min_length=1)
    temporary_files: list[FocusedProbeTemporaryFileEvidence] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_result(self) -> "FocusedProbeResult":
        if self.probe_id != self.request.probe_id:
            raise ValueError("focused probe result identity does not match request")
        if self.requirement_ids != self.request.requirement_ids:
            raise ValueError(
                "focused probe requirement identity does not match request"
            )
        if self.command_result.command_role != "verifier_probe":
            raise ValueError("focused probe must use command_role=verifier_probe")
        if self.status == "passed" and self.command_result.status not in {
            "passed",
            "failed",
        }:
            raise ValueError("passed focused probe requires reliable command execution")
        if self.status == "failed" and self.command_result.status not in {
            "passed",
            "failed",
        }:
            raise ValueError("failed focused probe requires reliable command execution")
        if self.status == "infrastructure_error" and self.command_result.status in {
            "passed",
            "failed",
        }:
            raise ValueError(
                "focused probe infrastructure error requires command infrastructure failure"
            )
        return self


class FocusedProbeReport(StrictVerificationModel):
    schema_version: Literal["villani.focused_probe.v1"]
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    execution_environment_fingerprint: str = Field(min_length=1)
    execution_provider: str = Field(min_length=1)
    worktree_path: str = Field(min_length=1)
    baseline_sha256: str = Field(min_length=1)
    requests: list[FocusedProbeRequest]
    results: list[FocusedProbeResult]
    status: Literal["passed", "failed", "unavailable", "infrastructure_error"]
    completed_at: str = Field(min_length=1)
    retry_count: int = Field(default=0, ge=0)
    failure_code: str | None = None

    @model_validator(mode="after")
    def validate_report(self) -> "FocusedProbeReport":
        request_ids = [item.probe_id for item in self.requests]
        result_ids = [item.probe_id for item in self.results]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("focused probe request IDs must be unique")
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("focused probe result IDs must be unique")
        if set(result_ids) - set(request_ids):
            raise ValueError("focused probe results must reference recorded requests")
        if self.status == "passed" and (
            not self.results or any(item.status != "passed" for item in self.results)
        ):
            raise ValueError("passed focused probe report requires all probes to pass")
        if self.status == "failed" and not any(
            item.status == "failed" for item in self.results
        ):
            raise ValueError("failed focused probe report requires a failed probe")
        if self.status == "infrastructure_error" and not any(
            item.status == "infrastructure_error" for item in self.results
        ):
            raise ValueError(
                "focused probe infrastructure report requires infrastructure evidence"
            )
        if self.status == "unavailable" and self.results:
            raise ValueError("unavailable focused probe report cannot contain results")
        return self


class CandidateEligibility(StrictVerificationModel):
    status: Literal[
        "eligible",
        "empty_patch",
        "patch_capture_failure",
        "ineligible",
    ]
    runner_completed_sufficiently: bool
    reason: str = Field(min_length=1)


class RepositoryValidationDecisionInput(StrictVerificationModel):
    status: Literal[
        "passed",
        "failed",
        "unavailable",
        "infrastructure_error",
    ]
    authoritative: bool
    required: bool
    failure_code: str | None = None


class SemanticReviewDecisionInput(StrictVerificationModel):
    raw_result: Literal[0, 1] | None
    verdict: Literal["success", "failure", "unclear", "error"]
    recommended_action: str
    schema_valid: bool
    critical_failure_reported: bool = False


class FinalVerificationDecision(StrictVerificationModel):
    result: Literal[0, 1]
    reason_code: FinalReasonCode
    reason: str = Field(min_length=1)
    recommended_action: Literal[
        "accept",
        "reject",
        "retry_verifier",
        "escalate",
    ]
    retry_scope: Literal["repository_validation", "verification"] | None = None


_OBSERVABLE_PATTERNS = (
    r"\bexact(?:ly)?\b",
    r"\bexact (?:text|output|value|error|message)\b",
    r"\bmust return\b",
    r"\bmust raise\b",
    r"\bmust include\b",
    r"\bmust contain\b",
    r"\bmust omit\b",
    r"\bmust preserve\b",
    r"\bmust support\b",
    r"\bmust accept\b",
    r"\bmust work\b",
    r"\bmust not panic\b",
    r"\bmust not print\b",
    r"\breturned value\b",
    r"\berror text\b",
    r"\bexception type\b",
    r"\bapi behavior\b",
    r"\btests?\b",
    r"\b(?:run|execute)\b.*\b(?:validation|check|command|suite)\b",
    r"\bvalidation commands?\b",
)
_NONCRITICAL_MARKERS = (
    "optional",
    "nice to have",
    "if convenient",
    "if possible",
    "noncritical",
    "non-critical",
)
_REQUIREMENT_MARKERS = (
    "must",
    "should",
    "required",
    "requirement",
    "do not",
    "don't",
    "never",
    "ensure",
    "implement",
    "create",
    "add",
    "edit",
    "modify",
    "update",
    "remove",
    "preserve",
    "support",
    "accept",
    "return",
    "raise",
    "include",
    "omit",
    "pass",
    "run",
    "test",
    "validate",
    "work",
    "change",
    "fix",
)


def normalize_requirement_text(value: str) -> str:
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(" .;")


def stable_requirement_id(value: str) -> str:
    normalized = normalize_requirement_text(value).casefold()
    return "req-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def requirement_is_observable(value: str) -> bool:
    normalized = normalize_requirement_text(value).casefold()
    return any(re.search(pattern, normalized) for pattern in _OBSERVABLE_PATTERNS)


def _segments(value: str) -> list[str]:
    output: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        pieces = re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9`])", stripped)
        output.extend(
            normalized
            for piece in pieces
            if (normalized := normalize_requirement_text(piece))
        )
    return output


def _looks_like_requirement(value: str) -> bool:
    lowered = value.casefold()
    return len(value) >= 4 and (
        any(
            re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", lowered)
            for marker in _REQUIREMENT_MARKERS
        )
        or value.startswith(("`", "'", '"'))
    )


def extract_requirements(
    *,
    task_instruction: str,
    success_criteria: str,
    policy_configuration: Mapping[str, Any],
) -> list[RequirementDefinition]:
    """Extract explicit requirements without task-name or benchmark inference."""

    definitions: list[RequirementDefinition] = []
    seen: set[str] = set()

    def add(description: str, source: RequirementSource) -> None:
        normalized = normalize_requirement_text(description)
        if not normalized or normalized.casefold() in seen:
            return
        seen.add(normalized.casefold())
        lowered = normalized.casefold()
        definitions.append(
            RequirementDefinition(
                requirement_id=stable_requirement_id(normalized),
                description=normalized,
                critical=not any(marker in lowered for marker in _NONCRITICAL_MARKERS),
                observable=requirement_is_observable(normalized),
                source=source,
            )
        )

    for source, value in (
        ("task_instruction", task_instruction),
        ("success_criteria", success_criteria),
    ):
        candidates = _segments(value)
        selected = [item for item in candidates if _looks_like_requirement(item)]
        if not selected and normalize_requirement_text(value):
            selected = [normalize_requirement_text(value)]
        for candidate in selected:
            add(candidate, source)  # type: ignore[arg-type]

    configured = policy_configuration.get("repository_validation_commands")
    if isinstance(configured, list):
        for item in configured:
            if not isinstance(item, Mapping):
                continue
            argv = item.get("argv")
            if not (
                isinstance(argv, list)
                and argv
                and all(isinstance(value, str) and value for value in argv)
            ):
                continue
            validation_id = str(item.get("validation_id") or "configured")
            add(
                "Required repository validation command "
                f"{validation_id}: {json.dumps(argv, ensure_ascii=False)} must pass",
                "repository_validation_command",
            )
    return definitions


def candidate_eligibility(
    *,
    patch: str | None,
    requires_file_changes: bool,
    attempt_status: str,
    failure_classification: object,
    candidate_quality: Mapping[str, Any] | None = None,
) -> CandidateEligibility:
    """Normalize the current and future candidate-quality interface."""

    failure = str(failure_classification or "")
    if failure == "patch_capture_failure":
        return CandidateEligibility(
            status="patch_capture_failure",
            runner_completed_sufficiently=False,
            reason="The candidate patch could not be captured reliably.",
        )
    if attempt_status == "cancelled" or failure == "infrastructure_failure":
        return CandidateEligibility(
            status="ineligible",
            runner_completed_sufficiently=False,
            reason="The runner did not complete sufficiently to produce an eligible candidate.",
        )
    if candidate_quality is not None:
        declared = str(
            candidate_quality.get("status")
            or candidate_quality.get("eligibility_status")
            or ""
        )
        if declared == "warning":
            return CandidateEligibility(
                status="eligible",
                runner_completed_sufficiently=True,
                reason=str(
                    candidate_quality.get("reason")
                    or "Candidate patch quality passed with warnings: "
                    + ", ".join(
                        str(value)
                        for value in candidate_quality.get("reason_codes", [])
                    )
                ),
            )
        if declared in {
            "eligible",
            "empty_patch",
            "patch_capture_failure",
            "ineligible",
        }:
            return CandidateEligibility(
                status=declared,  # type: ignore[arg-type]
                runner_completed_sufficiently=bool(
                    candidate_quality.get(
                        "runner_completed_sufficiently", declared == "eligible"
                    )
                ),
                reason=str(candidate_quality.get("reason") or declared),
            )
    if requires_file_changes and not (patch and patch.strip()):
        return CandidateEligibility(
            status="empty_patch",
            runner_completed_sufficiently=attempt_status == "completed",
            reason="The task requires file changes but the candidate patch is empty.",
        )
    return CandidateEligibility(
        status="eligible",
        runner_completed_sufficiently=True,
        reason="The candidate patch is available for verification.",
    )


def parse_focused_probe_requests(
    value: object,
    *,
    requirement_ids: set[str],
) -> tuple[list[FocusedProbeRequest], list[str]]:
    """Validate model proposals without converting rejected probes into failures."""

    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["focusedProbeRequests must be a list"]
    requests: list[FocusedProbeRequest] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, 1):
        try:
            request = FocusedProbeRequest.model_validate(raw)
        except ValueError as error:
            rejected.append(f"focused probe {index} rejected: {error}")
            continue
        unknown = set(request.requirement_ids) - requirement_ids
        if unknown:
            rejected.append(
                f"focused probe {request.probe_id} references unknown requirements: "
                + ", ".join(sorted(unknown))
            )
            continue
        if request.probe_id in seen:
            rejected.append(
                f"focused probe {request.probe_id} has a duplicate probe_id"
            )
            continue
        seen.add(request.probe_id)
        requests.append(request)
    return requests, rejected


def compute_final_verification_decision(
    candidate_eligibility: CandidateEligibility,
    repository_validation: RepositoryValidationDecisionInput,
    requirement_matrix: Sequence[RequirementEvidence],
    semantic_review: SemanticReviewDecisionInput,
    verifier_invocation_status: str,
) -> FinalVerificationDecision:
    """Compute the mandatory binary result without invoking an LLM."""

    if verifier_invocation_status in {
        "timeout",
        "subprocess_failure",
        "missing_trace",
        "missing_compatible_trace",
        "error",
        "tool_failure",
    }:
        return FinalVerificationDecision(
            result=0,
            reason_code="verifier_tool_failure",
            reason=(
                "The verifier did not complete reliably "
                f"({verifier_invocation_status})."
            ),
            recommended_action="retry_verifier",
            retry_scope="verification",
        )
    if (
        verifier_invocation_status == "malformed_output"
        or not semantic_review.schema_valid
    ):
        return FinalVerificationDecision(
            result=0,
            reason_code="verifier_malformed_output",
            reason="The semantic verifier returned malformed data.",
            recommended_action="retry_verifier",
            retry_scope="verification",
        )
    if candidate_eligibility.status == "empty_patch":
        return FinalVerificationDecision(
            result=0,
            reason_code="empty_patch",
            reason=candidate_eligibility.reason,
            recommended_action="reject",
        )
    if (
        candidate_eligibility.status != "eligible"
        or not candidate_eligibility.runner_completed_sufficiently
    ):
        return FinalVerificationDecision(
            result=0,
            reason_code="candidate_ineligible",
            reason=candidate_eligibility.reason,
            recommended_action="reject",
        )
    if repository_validation.status == "infrastructure_error":
        return FinalVerificationDecision(
            result=0,
            reason_code="repository_validation_infrastructure_error",
            reason=(
                "Repository validation could not execute reliably"
                + (
                    f": {repository_validation.failure_code}"
                    if repository_validation.failure_code
                    else "."
                )
            ),
            recommended_action="retry_verifier",
            retry_scope="repository_validation",
        )
    if repository_validation.status == "failed":
        return FinalVerificationDecision(
            result=0,
            reason_code="repository_validation_failed",
            reason="Authoritative repository validation failed on the candidate.",
            recommended_action="reject",
        )
    if repository_validation.required and (
        repository_validation.status != "passed"
        or not repository_validation.authoritative
    ):
        return FinalVerificationDecision(
            result=0,
            reason_code="repository_validation_unavailable",
            reason="Required authoritative repository validation is unavailable.",
            recommended_action="retry_verifier",
            retry_scope="verification",
        )
    if any(item.final_status == "infrastructure_error" for item in requirement_matrix):
        return FinalVerificationDecision(
            result=0,
            reason_code="verifier_tool_failure",
            reason="Requirement evidence contains an unresolved infrastructure failure.",
            recommended_action="retry_verifier",
            retry_scope="verification",
        )
    critical_failed = [
        item
        for item in requirement_matrix
        if item.critical and item.deterministic_status == "failed"
    ]
    if critical_failed:
        focused = next(
            (item for item in critical_failed if item.evidence_type == "focused_probe"),
            None,
        )
        return FinalVerificationDecision(
            result=0,
            reason_code=(
                "focused_probe_failed"
                if focused is not None
                else "critical_requirement_failed"
            ),
            reason=(focused or critical_failed[0]).reason,
            recommended_action="reject",
        )
    if any(item.contradiction for item in requirement_matrix):
        return FinalVerificationDecision(
            result=0,
            reason_code="evidence_contradiction",
            reason="Executable and semantic evidence contradict each other.",
            recommended_action="retry_verifier",
            retry_scope="verification",
        )
    critical_missing = [
        item
        for item in requirement_matrix
        if item.critical and item.final_status == "missing"
    ]
    if critical_missing:
        focused = next(
            (
                item
                for item in critical_missing
                if item.evidence_type == "focused_probe"
            ),
            None,
        )
        return FinalVerificationDecision(
            result=0,
            reason_code=(
                "focused_probe_missing"
                if focused is not None
                else "critical_requirement_missing"
            ),
            reason=(focused or critical_missing[0]).reason,
            recommended_action="retry_verifier",
            retry_scope="verification",
        )
    if semantic_review.verdict == "error":
        return FinalVerificationDecision(
            result=0,
            reason_code="verifier_tool_failure",
            reason="The semantic verifier reported an infrastructure error.",
            recommended_action="retry_verifier",
            retry_scope="verification",
        )
    if semantic_review.verdict == "unclear":
        return FinalVerificationDecision(
            result=0,
            reason_code="semantic_verifier_unclear",
            reason="The semantic verifier could not reach a clear evidence assessment.",
            recommended_action="retry_verifier",
            retry_scope="verification",
        )
    if (
        semantic_review.raw_result != 1
        or semantic_review.verdict != "success"
        or semantic_review.critical_failure_reported
    ):
        return FinalVerificationDecision(
            result=0,
            reason_code="semantic_verifier_rejected",
            reason="The semantic verifier reported a candidate failure.",
            recommended_action="reject",
        )
    return FinalVerificationDecision(
        result=1,
        reason_code="accepted",
        reason="Every deterministic acceptance gate passed.",
        recommended_action="accept",
    )


def evidence_matrix(
    *,
    run_id: str,
    attempt_id: str,
    requirements: Sequence[RequirementEvidence],
    repository_validation_status: str,
    candidate_eligibility_status: str,
    semantic_verifier_status: str,
    decision: FinalVerificationDecision,
) -> VerificationEvidenceMatrix:
    items = list(requirements)
    infrastructure = repository_validation_status == "infrastructure_error" or any(
        item.final_status == "infrastructure_error" for item in items
    )
    return VerificationEvidenceMatrix(
        schema_version="villani.verification_evidence.v2",
        run_id=run_id,
        attempt_id=attempt_id,
        requirements=items,
        repository_validation_status=repository_validation_status,
        candidate_eligibility_status=candidate_eligibility_status,
        semantic_verifier_status=semantic_verifier_status,
        critical_requirements_proven=all(
            item.final_status == "passed" for item in items if item.critical
        ),
        contradictions_present=any(item.contradiction for item in items),
        infrastructure_failure_present=infrastructure,
        final_result=decision.result,
        final_reason_code=decision.reason_code,
        final_reason=decision.reason,
    )
