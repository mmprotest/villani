"""Deterministic coverage projection for authoritative repository validation.

The repository-validation report proves that a command ran.  This module records
the narrower statement that can safely be made about *what* that command covered.
It deliberately does not treat a passing suite as proof of every task requirement.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import Field, model_validator

from villani_ops.execution_environment.models import (
    CandidatePatchQuality,
    RepositoryValidationReport,
)

from .protocol import StrictProtocolModel
from .verification_evidence import RequirementDefinition, extract_requirements


CoverageConfidence = Literal["high", "medium", "low", "unknown"]
CoverageStatus = Literal[
    "passed",
    "failed",
    "not_run",
    "unavailable",
    "infrastructure_error",
]

_WORD = re.compile(r"[a-z][a-z0-9_-]{2,}")
_TEST_PATH = re.compile(
    r"(?:^|[/_.-])(?:tests?|specs?)(?:[/_.-]|$)", re.IGNORECASE
)
_EXECUTION_SIGNAL = re.compile(r"\b\d+\s+(?:tests?|specs?|checks?)\b", re.IGNORECASE)
_STOP_WORDS = {
    "add",
    "all",
    "and",
    "are",
    "behavior",
    "behaviour",
    "changed",
    "changes",
    "command",
    "criteria",
    "file",
    "files",
    "for",
    "from",
    "function",
    "implementation",
    "must",
    "only",
    "pass",
    "passing",
    "repository",
    "required",
    "should",
    "test",
    "tests",
    "that",
    "the",
    "this",
    "typed",
    "validation",
    "with",
}
_NEGATIVE_MARKERS = ("do not", "must not", "never", "without")


class ValidationCommandCoverage(StrictProtocolModel):
    validation_id: str = Field(min_length=1)
    command_identity: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    argv: list[str] = Field(min_length=1)
    safe_display: str = Field(min_length=1)
    execution_role: str = Field(min_length=1)
    working_directory: str = Field(min_length=1)
    status: CoverageStatus
    exit_status: int | None
    started_at: str
    ended_at: str
    explicitly_named_test_targets: list[str] = Field(default_factory=list)
    changed_test_files_proven: list[str] = Field(default_factory=list)
    changed_test_files_plausibly_included: list[str] = Field(default_factory=list)
    requirement_ids_covered: list[str] = Field(default_factory=list)
    coverage_provenance: list[str] = Field(default_factory=list)
    confidence: CoverageConfidence
    coverage_unestablished_reasons: list[str] = Field(default_factory=list)
    artifact_references: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_coverage_claims(self) -> "ValidationCommandCoverage":
        if len(self.requirement_ids_covered) != len(set(self.requirement_ids_covered)):
            raise ValueError("requirement_ids_covered must be unique")
        if self.status != "passed" and self.requirement_ids_covered:
            raise ValueError("only a passing validation command can cover requirements")
        if self.requirement_ids_covered and self.confidence not in {"high", "medium"}:
            raise ValueError("covered requirements require high or medium confidence")
        return self


class ValidationCoverageReport(StrictProtocolModel):
    schema_version: Literal["villani.validation_coverage.v1"]
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    commands: list[ValidationCommandCoverage]
    requirement_ids: list[str]
    requirements_covered: list[str]
    requirements_not_covered: list[str]
    generated_at: str
    migration: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_requirement_partition(self) -> "ValidationCoverageReport":
        known = set(self.requirement_ids)
        covered = set(self.requirements_covered)
        uncovered = set(self.requirements_not_covered)
        if covered & uncovered or covered | uncovered != known:
            raise ValueError("coverage report must partition every requirement")
        command_covered = {
            requirement_id
            for command in self.commands
            for requirement_id in command.requirement_ids_covered
        }
        if command_covered != covered:
            raise ValueError("report coverage must equal command coverage")
        return self


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/").removeprefix("./")


def _is_test_path(value: str) -> bool:
    return bool(_TEST_PATH.search(_normalize_path(value)))


def _terms(value: str) -> set[str]:
    return {
        term
        for term in _WORD.findall(value.casefold())
        if len(term) >= 4 and term not in _STOP_WORDS
    }


def _git_added_text(worktree: Path, relative: str, *, untracked: bool) -> str:
    target = worktree / relative
    if untracked:
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    completed = subprocess.run(
        ["git", "diff", "--unified=0", "HEAD", "--", relative],
        cwd=worktree,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return "\n".join(
        line[1:]
        for line in completed.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _safe_argv(argv: list[str]) -> list[str]:
    """Mask credential-shaped arguments while retaining command structure."""

    result: list[str] = []
    mask_next = False
    for value in argv:
        lowered = value.casefold()
        if mask_next:
            result.append("[REDACTED]")
            mask_next = False
            continue
        if any(marker in lowered for marker in ("api-key", "api_key", "token", "secret", "password")):
            if "=" in value:
                result.append(value.split("=", 1)[0] + "=[REDACTED]")
            else:
                result.append(value)
                mask_next = True
            continue
        result.append(value)
    return result


def _command_identity(validation_id: str, argv: list[str], cwd: str, role: str) -> str:
    encoded = json.dumps(
        {
            "validation_id": validation_id,
            "argv": argv,
            "working_directory": cwd,
            "execution_role": role,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _explicit_test_targets(
    argv: list[str], worktree: Path, changed_tests: list[str]
) -> list[str]:
    targets: set[str] = set()
    normalized_tests = {_normalize_path(item): item for item in changed_tests}
    for argument in argv[1:]:
        if argument.startswith("-"):
            continue
        candidate = _normalize_path(argument.split("::", 1)[0].split(":", 1)[0])
        if not candidate:
            continue
        for normalized, original in normalized_tests.items():
            if candidate == normalized or normalized.startswith(candidate.rstrip("/") + "/"):
                targets.add(original)
                continue
            if Path(candidate).name == Path(normalized).name:
                targets.add(original)
        try:
            resolved = (worktree / candidate).resolve()
            resolved.relative_to(worktree.resolve())
        except (OSError, ValueError):
            continue
        if resolved.exists() and _is_test_path(candidate):
            targets.add(candidate)
    return sorted(targets)


def _suite_execution_signal(argv: list[str], stdout: str, stderr: str) -> bool:
    command_words = _terms(" ".join(argv))
    command_signal = any(
        marker in term
        for term in command_words
        for marker in ("test", "spec", "check", "verify")
    )
    return command_signal or bool(_EXECUTION_SIGNAL.search(stdout + "\n" + stderr))


def _configured_requirement_ids(
    configuration: Mapping[str, Any], validation_id: str
) -> set[str]:
    configured = configuration.get("repository_validation_commands")
    if not isinstance(configured, list):
        return set()
    for index, item in enumerate(configured, 1):
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("validation_id") or f"repository_validation_{index:03d}")
        if item_id != validation_id:
            continue
        values = item.get("requirement_ids")
        return {str(value) for value in values} if isinstance(values, list) else set()
    return set()


def _command_requirement(definition: RequirementDefinition, validation_id: str) -> bool:
    return (
        definition.source == "repository_validation_command"
        and validation_id.casefold() in definition.description.casefold()
    )


def _validation_outcome_requirement(definition: RequirementDefinition) -> bool:
    text = definition.description.casefold()
    if definition.source == "repository_validation_command":
        return False
    if re.search(r"\b(?:add|create|edit|modify|update|remove)\b", text):
        return False
    subject = any(term in text for term in ("test", "check", "validation", "suite"))
    outcome = any(term in text for term in ("pass", "succeed", "green"))
    exact_behavior = bool(
        re.search(
            r"\b(?:exact|return|raise|include|contain|omit|preserve|support|"
            r"accept|panic|print)\b",
            text,
        )
    )
    return subject and outcome and not exact_behavior


def build_validation_coverage(
    *,
    worktree: Path,
    task_instruction: str,
    success_criteria: str,
    policy_configuration: Mapping[str, Any],
    repository_validation: RepositoryValidationReport,
    candidate_quality: CandidatePatchQuality,
) -> ValidationCoverageReport:
    """Build a conservative requirement-to-validation evidence graph."""

    worktree = Path(worktree).resolve()
    definitions = extract_requirements(
        task_instruction=task_instruction,
        success_criteria=success_criteria,
        policy_configuration=policy_configuration,
    )
    changed = sorted(
        dict.fromkeys(
            [
                *candidate_quality.tracked_files_changed,
                *candidate_quality.untracked_files,
            ]
        )
    )
    changed_tests = [item for item in changed if _is_test_path(item)]
    changed_implementation = [item for item in changed if not _is_test_path(item)]
    untracked = set(candidate_quality.untracked_files)
    added_terms = {
        relative: _terms(
            _git_added_text(worktree, relative, untracked=relative in untracked)
        )
        for relative in changed
    }
    implementation_terms = set().union(
        *(added_terms[path] for path in changed_implementation)
    ) if changed_implementation else set()
    test_terms = set().union(*(added_terms[path] for path in changed_tests)) if changed_tests else set()

    command_rows: list[ValidationCommandCoverage] = []
    all_covered: set[str] = set()
    known_ids = {item.requirement_id for item in definitions}
    for command in repository_validation.commands:
        safe_argv = _safe_argv(list(command.argv))
        explicit_targets = _explicit_test_targets(
            list(command.argv), worktree, changed_tests
        )
        suite_signal = _suite_execution_signal(
            list(command.argv), command.stdout, command.stderr
        )
        proven_tests = explicit_targets if command.status == "passed" else []
        plausible_tests = (
            sorted(set(changed_tests) - set(proven_tests))
            if command.status == "passed" and suite_signal
            else []
        )
        covered: set[str] = set()
        provenance: set[str] = set()
        explicitly_mapped = _configured_requirement_ids(
            policy_configuration, command.validation_id
        ) & known_ids
        if command.status == "passed":
            covered.update(explicitly_mapped)
            if explicitly_mapped:
                provenance.add("configured_requirement_mapping")
            for definition in definitions:
                if _command_requirement(definition, command.validation_id):
                    covered.add(definition.requirement_id)
                    provenance.add("executed_command_identity")
                    continue
                if _validation_outcome_requirement(definition):
                    covered.add(definition.requirement_id)
                    provenance.add("authoritative_validation_outcome")
                    continue
                lowered = definition.description.casefold()
                if any(marker in lowered for marker in _NEGATIVE_MARKERS):
                    continue
                requirement_terms = _terms(definition.description)
                linked = requirement_terms & implementation_terms & test_terms
                if linked and (proven_tests or plausible_tests):
                    covered.add(definition.requirement_id)
                    provenance.add("changed_behavior_test_validation_graph")
        all_covered.update(covered)
        reasons: list[str] = []
        if command.status != "passed":
            reasons.append(f"command_status_{command.status}")
        if not changed_tests:
            reasons.append("no_changed_test_file")
        elif not proven_tests and not plausible_tests:
            reasons.append("changed_test_execution_not_established")
        if command.status == "passed" and not covered:
            reasons.append("no_requirement_link_established")
        confidence: CoverageConfidence
        if covered and (
            explicitly_mapped
            or "executed_command_identity" in provenance
            or proven_tests
        ):
            confidence = "high"
        elif covered:
            confidence = "medium"
        elif command.status == "passed":
            confidence = "low"
        else:
            confidence = "unknown"
        command_rows.append(
            ValidationCommandCoverage(
                validation_id=command.validation_id,
                command_identity=_command_identity(
                    command.validation_id,
                    list(command.argv),
                    command.worktree_path,
                    command.command_role,
                ),
                argv=safe_argv,
                safe_display=subprocess.list2cmdline(safe_argv),
                execution_role=command.command_role,
                working_directory=command.worktree_path,
                status=(
                    command.status
                    if command.status in {"passed", "failed", "infrastructure_error"}
                    else "infrastructure_error"
                ),
                exit_status=command.exit_code,
                started_at=command.started_at,
                ended_at=command.completed_at,
                explicitly_named_test_targets=explicit_targets,
                changed_test_files_proven=proven_tests,
                changed_test_files_plausibly_included=plausible_tests,
                requirement_ids_covered=sorted(covered),
                coverage_provenance=sorted(provenance),
                confidence=confidence,
                coverage_unestablished_reasons=sorted(set(reasons)),
                artifact_references=[
                    "repository-validation.json",
                    "candidate-patch-quality.json",
                    "candidate/patch.diff",
                ],
            )
        )
    requirement_ids = sorted(known_ids)
    return ValidationCoverageReport(
        schema_version="villani.validation_coverage.v1",
        run_id=repository_validation.run_id,
        attempt_id=repository_validation.attempt_id,
        candidate_id=repository_validation.candidate_id,
        commands=command_rows,
        requirement_ids=requirement_ids,
        requirements_covered=sorted(all_covered),
        requirements_not_covered=sorted(known_ids - all_covered),
        generated_at=repository_validation.completed_at or _utc_now(),
    )


def legacy_validation_coverage(
    *,
    repository_validation: RepositoryValidationReport,
    task_instruction: str,
    success_criteria: str,
    policy_configuration: Mapping[str, Any],
) -> ValidationCoverageReport:
    """Conservatively project an old bundle without inventing behavior coverage."""

    definitions = extract_requirements(
        task_instruction=task_instruction,
        success_criteria=success_criteria,
        policy_configuration=policy_configuration,
    )
    known = {item.requirement_id for item in definitions}
    commands: list[ValidationCommandCoverage] = []
    covered: set[str] = set()
    for command in repository_validation.commands:
        command_covered = {
            item.requirement_id
            for item in definitions
            if command.status == "passed"
            and (
                _command_requirement(item, command.validation_id)
                or _validation_outcome_requirement(item)
            )
        }
        command_covered.update(
            _configured_requirement_ids(policy_configuration, command.validation_id)
            & known
            if command.status == "passed"
            else set()
        )
        covered.update(command_covered)
        safe_argv = _safe_argv(list(command.argv))
        commands.append(
            ValidationCommandCoverage(
                validation_id=command.validation_id,
                command_identity=_command_identity(
                    command.validation_id,
                    list(command.argv),
                    command.worktree_path,
                    command.command_role,
                ),
                argv=safe_argv,
                safe_display=subprocess.list2cmdline(safe_argv),
                execution_role=command.command_role,
                working_directory=command.worktree_path,
                status=(
                    command.status
                    if command.status in {"passed", "failed", "infrastructure_error"}
                    else "infrastructure_error"
                ),
                exit_status=command.exit_code,
                started_at=command.started_at,
                ended_at=command.completed_at,
                requirement_ids_covered=sorted(command_covered),
                coverage_provenance=(
                    ["legacy_explicit_command_evidence"] if command_covered else []
                ),
                confidence="high" if command_covered else "unknown",
                coverage_unestablished_reasons=(
                    [] if command_covered else ["legacy_bundle_has_no_coverage_graph"]
                ),
                artifact_references=["repository-validation.json"],
            )
        )
    return ValidationCoverageReport(
        schema_version="villani.validation_coverage.v1",
        run_id=repository_validation.run_id,
        attempt_id=repository_validation.attempt_id,
        candidate_id=repository_validation.candidate_id,
        commands=commands,
        requirement_ids=sorted(known),
        requirements_covered=sorted(covered),
        requirements_not_covered=sorted(known - covered),
        generated_at=repository_validation.completed_at,
        migration={
            "source_schema_version": repository_validation.schema_version,
            "mode": "conservative_read_projection",
            "behavior_coverage_inferred": False,
        },
    )


def load_validation_coverage_report(path: Path) -> ValidationCoverageReport | None:
    path = Path(path)
    if path.is_dir():
        path = path / "validation-coverage.json"
    if not path.is_file():
        return None
    return ValidationCoverageReport.model_validate_json(
        path.read_text(encoding="utf-8")
    )
