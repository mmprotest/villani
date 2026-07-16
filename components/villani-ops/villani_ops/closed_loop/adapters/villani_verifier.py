"""Normalized closed-loop adapter for the shared Villani verifier pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import ValidationError

from villani_ops.verifier.service import execute_verifier
from villani_ops.closed_loop.costs import actual_attempt_cost
from villani_ops.closed_loop.durable_io import (
    read_jsonl_tolerant,
    write_json_atomic,
)
from villani_ops.core.backend import Backend
from villani_ops.providers import validate_runtime_credentials

from ..event_writer import redact_data
from ..focused_probes import (
    focused_probe_identity_valid,
    load_focused_probe_report,
)
from ..interfaces import (
    AttemptContext,
    AttemptResult,
    EvidenceItem,
    Requirement,
    Verification,
)
from ..plugins.builtins import VERIFIER_MANIFEST
from ..repository_validation import load_repository_validation_report
from ..verification_evidence import (
    FinalVerificationDecision,
    FocusedProbeReport,
    RequirementDefinition,
    RequirementEvidence,
    RepositoryValidationDecisionInput,
    SemanticReviewDecisionInput,
    candidate_eligibility,
    compute_final_verification_decision,
    evidence_matrix,
    extract_requirements,
    normalize_requirement_text,
    parse_focused_probe_requests,
)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("text") or value.get("summary") or value.get("reason") or value
        )
    return str(value)


def _evidence(items: list[Any], prefix: str, artifact: str) -> tuple[EvidenceItem, ...]:
    output: list[EvidenceItem] = []
    for index, item in enumerate(items, 1):
        evidence_id = (
            str(item.get("id") or item.get("evidence_id") or f"{prefix}_{index:03d}")
            if isinstance(item, dict)
            else f"{prefix}_{index:03d}"
        )
        kind = (
            str(item.get("kind") or item.get("category") or prefix)
            if isinstance(item, dict)
            else prefix
        )
        output.append(
            EvidenceItem(
                evidence_id=evidence_id,
                kind=kind,
                summary=_text(item)[:1000],
                artifact_path=artifact,
            )
        )
    return tuple(output)


def _requirements(raw: Mapping[str, Any]) -> tuple[Requirement, ...]:
    output: list[Requirement] = []
    for index, item in enumerate(_list(raw.get("requirementResults")), 1):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").lower()
        outcome: Literal["passed", "failed"] = (
            "passed" if status in {"satisfied", "passed"} else "failed"
        )
        evidence = item.get("evidence")
        evidence_ids_list: list[str] = []
        for evidence_index, value in enumerate(_list(evidence), 1):
            raw_id = value.get("id") if isinstance(value, dict) else value
            evidence_id = (
                str(raw_id)
                if raw_id
                else f"requirement_{index:03d}_evidence_{evidence_index:03d}"
            )
            if evidence_id not in evidence_ids_list:
                evidence_ids_list.append(evidence_id)
        evidence_ids = tuple(evidence_ids_list)
        output.append(
            Requirement(
                requirement_id=str(item.get("id") or f"requirement_{index:03d}"),
                description=str(
                    item.get("requirement")
                    or item.get("description")
                    or "Verifier requirement"
                ),
                outcome=outcome,
                evidence_ids=evidence_ids,
            )
        )
    return tuple(output)


def _risk_texts(raw: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(_text(item) for item in _list(raw.get("riskFlags")))


def _has_acceptance_blocker(risks: tuple[str, ...]) -> bool:
    blockers = (
        "acceptance_blocker",
        "blocking failure",
        "constraint violation",
        "critical requirement evidence refs missing",
        "critical requirement coverage unproven",
    )
    return any(any(blocker in risk.lower() for blocker in blockers) for risk in risks)


def _candidate_quality(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _repository_validation_required(configuration: Mapping[str, Any]) -> bool:
    commands = configuration.get("repository_validation_commands")
    return isinstance(commands, list) and bool(commands)


def _semantic_status(
    value: object,
) -> Literal["passed", "failed", "unclear", "not_assessed"]:
    normalized = str(value or "").casefold()
    if normalized in {"satisfied", "passed", "success"}:
        return "passed"
    if normalized in {"unsatisfied", "failed", "failure"}:
        return "failed"
    if normalized in {"unclear", "unknown"}:
        return "unclear"
    return "not_assessed"


def _raw_requirement_description(value: Mapping[str, Any]) -> str:
    return str(
        value.get("requirement")
        or value.get("description")
        or value.get("reason")
        or ""
    ).strip()


def _semantic_requirements(
    definitions: list[RequirementDefinition],
    raw: Mapping[str, Any],
) -> tuple[
    list[RequirementDefinition],
    dict[str, tuple[Literal["passed", "failed", "unclear", "not_assessed"], list[str]]],
]:
    """Map semantic assessments onto stable extracted requirement identities."""

    by_id = {item.requirement_id: item for item in definitions}
    by_text = {
        normalize_requirement_text(item.description).casefold(): item
        for item in definitions
    }
    assessments: dict[
        str,
        tuple[Literal["passed", "failed", "unclear", "not_assessed"], list[str]],
    ] = {}
    raw_items = [
        item
        for item in _list(raw.get("requirementResults"))
        if isinstance(item, Mapping)
    ]
    for item in raw_items:
        raw_id = str(item.get("requirement_id") or item.get("id") or "")
        description = _raw_requirement_description(item)
        definition = by_id.get(raw_id)
        if definition is None and description:
            definition = by_text.get(normalize_requirement_text(description).casefold())
        if definition is None and len(definitions) == 1 and len(raw_items) == 1:
            definition = definitions[0]
        if definition is None:
            # Semantic review assesses controller-extracted requirements. It
            # cannot turn prompt wrappers or invented obligations into gates.
            continue
        references: list[str] = []
        for evidence in _list(item.get("evidence_ids", item.get("evidence"))):
            value = evidence.get("id") if isinstance(evidence, Mapping) else evidence
            if value and str(value) not in references:
                references.append(str(value))
        assessments[definition.requirement_id] = (
            _semantic_status(item.get("semantic_status", item.get("status"))),
            references,
        )
    return definitions, assessments


def _meaningful_terms(value: str) -> set[str]:
    quoted = {
        match.group(1).casefold()
        for match in re.finditer(r"[`'\"]([^`'\"]{2,})[`'\"]", value)
    }
    tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9_.:/-]{4,}", value)
        if token.casefold()
        not in {
            "must",
            "should",
            "required",
            "requirement",
            "candidate",
            "repository",
            "validation",
            "command",
            "passes",
            "passed",
            "works",
            "work",
        }
    }
    return quoted | tokens


def _explicit_command_terms(value: str) -> set[str]:
    quoted = {
        match.group(1).casefold()
        for match in re.finditer(r"[`'\"]([^`'\"]{2,})[`'\"]", value)
    }
    paths = {
        token.casefold()
        for token in re.findall(
            r"[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+|"
            r"\b[A-Za-z0-9_-]+\.[A-Za-z0-9][A-Za-z0-9._-]{0,31}\b",
            value,
        )
    }
    return quoted | paths


def _is_validation_outcome_requirement(
    definition: RequirementDefinition,
) -> bool:
    if definition.source == "repository_validation_command":
        return False
    value = normalize_requirement_text(definition.description).casefold()
    if re.search(r"\b(?:add|create|edit|modify|update|remove)\b", value):
        return False
    has_validation_subject = bool(
        re.search(r"\b(?:tests?|suite|validation|checks?|commands?)\b", value)
    )
    has_validation_action = bool(
        re.search(
            r"\b(?:pass|passes|passed|succeed|succeeds|run|runs|execute)\b",
            value,
        )
    )
    exact_behavior = bool(
        re.search(
            r"\b(?:exact|return|raise|include|contain|omit|preserve|support|"
            r"accept|panic|print)\b",
            value,
        )
    )
    return has_validation_subject and has_validation_action and not exact_behavior


def _configured_command_coverage(
    definition: RequirementDefinition,
    configuration: Mapping[str, Any],
) -> set[str]:
    configured = configuration.get("repository_validation_commands")
    if not isinstance(configured, list):
        return set()
    covered: set[str] = set()
    terms = _meaningful_terms(definition.description)
    explicit_terms = _explicit_command_terms(definition.description)
    for index, item in enumerate(configured, 1):
        if not isinstance(item, Mapping):
            continue
        validation_id = str(
            item.get("validation_id") or f"repository_validation_{index:03d}"
        )
        mapped = item.get("requirement_ids")
        if isinstance(mapped, list) and definition.requirement_id in {
            str(value) for value in mapped
        }:
            covered.add(validation_id)
            continue
        argv = item.get("argv")
        command_text = (
            " ".join(str(value) for value in argv).casefold()
            if isinstance(argv, list)
            else ""
        )
        if definition.source == "repository_validation_command":
            covered.add(validation_id)
        elif _is_validation_outcome_requirement(definition):
            covered.add(validation_id)
        elif definition.observable and any(
            term in command_text for term in explicit_terms
        ):
            covered.add(validation_id)
        elif (
            not definition.observable
            and terms
            and any(term in command_text for term in terms)
        ):
            covered.add(validation_id)
    return covered


def _allowed_worktree_paths(attempt_result: AttemptResult) -> list[str]:
    raw = attempt_result.metadata.get("candidate_execution_worktree_paths")
    if isinstance(raw, list):
        values = [str(item) for item in raw if item]
    else:
        values = []
    focused = attempt_result.metadata.get("focused_probe_worktree_path")
    if focused:
        values.append(str(focused))
    if attempt_result.worktree_path:
        values.append(attempt_result.worktree_path)
    return list(dict.fromkeys(values))


def _outcome_for_decision(
    decision: FinalVerificationDecision,
) -> Literal["accepted", "rejected", "unclear", "error"]:
    if decision.result == 1:
        return "accepted"
    if decision.reason_code in {
        "repository_validation_infrastructure_error",
        "verifier_tool_failure",
        "verifier_malformed_output",
    }:
        return "error"
    if decision.reason_code in {
        "focused_probe_missing",
        "critical_requirement_missing",
        "semantic_verifier_unclear",
        "evidence_contradiction",
        "repository_validation_unavailable",
    }:
        return "unclear"
    return "rejected"


@dataclass(frozen=True, slots=True)
class _RepositoryValidationAuthority:
    passed: bool = False
    failed: bool = False
    infrastructure_error: bool = False
    unavailable: bool = False
    source: str = "none"
    status: str = "unavailable"
    failure_code: str | None = "repository_validation_unavailable"
    retry_count: int = 0
    report_path: str | None = None


def _resolved_path(value: object) -> str | None:
    try:
        return str(Path(str(value)).resolve())
    except (OSError, TypeError, ValueError):
        return None


def _legacy_repository_validation_authority(
    attempt_context: AttemptContext,
    attempt_result: AttemptResult,
) -> _RepositoryValidationAuthority:
    """Read only exact legacy structured validation events."""

    mutations = [
        event.timestamp
        for event in attempt_result.runtime_events
        if event.event_type == "file_write"
    ]
    final_mutation = max(mutations) if mutations else None
    expected_worktree = str(Path(attempt_result.worktree_path).resolve())
    expected_baseline = attempt_context.baseline_sha256
    validations = []
    accepted_types = {
        "repository_validation_completed",
        "repository_validation_failed",
        "repository_validation_infrastructure_error",
        # Old bundles used these generic names, but only an exact structured
        # repository-validation payload is accepted here.
        "command_completed",
        "command_failed",
    }
    for event in attempt_result.runtime_events:
        payload = event.payload
        if event.event_type not in accepted_types:
            continue
        if payload.get("command_role") != "repository_validation":
            continue
        required = {
            "run_id",
            "attempt_id",
            "worktree_path",
            "baseline_sha256",
            "candidate_state",
            "exit_code",
        }
        if not required.issubset(payload):
            continue
        if payload.get("run_id") != attempt_context.run_id:
            continue
        if payload.get("attempt_id") != attempt_context.attempt_id:
            continue
        event_worktree = _resolved_path(payload["worktree_path"])
        if event_worktree is None:
            continue
        if event_worktree != expected_worktree:
            continue
        if (
            expected_baseline is None
            or payload.get("baseline_sha256") != expected_baseline
        ):
            continue
        if payload.get("candidate_state") != "post_mutation":
            continue
        if final_mutation is not None and event.timestamp < final_mutation:
            continue
        validations.append(event)
    if not validations:
        return _RepositoryValidationAuthority(source="none")
    if any(
        event.event_type == "repository_validation_infrastructure_error"
        or str(event.payload.get("failure_code") or "")
        in {
            "repository_validation_timeout",
            "repository_validation_executable_missing",
            "repository_validation_environment_mismatch",
            "repository_validation_provider_failure",
            "repository_validation_policy_denied",
            "repository_validation_malformed_result",
        }
        for event in validations
    ):
        return _RepositoryValidationAuthority(
            infrastructure_error=True,
            source="legacy_runtime_events",
            status="infrastructure_error",
            failure_code=next(
                (
                    str(event.payload["failure_code"])
                    for event in validations
                    if event.payload.get("failure_code")
                ),
                "repository_validation_provider_failure",
            ),
        )
    failed = any(
        event.event_type in {"repository_validation_failed", "command_failed"}
        or event.payload.get("exit_code") not in {None, 0}
        for event in validations
    )
    return _RepositoryValidationAuthority(
        passed=not failed,
        failed=failed,
        source="legacy_runtime_events",
        status="failed" if failed else "passed",
        failure_code=(
            "repository_validation_test_failure"
            if failed
            else "repository_validation_passed"
        ),
    )


def _repository_validation_details(
    attempt_context: AttemptContext,
    attempt_result: AttemptResult,
) -> _RepositoryValidationAuthority:
    report_path = Path(attempt_context.attempt_directory) / "repository-validation.json"
    if report_path.is_file():
        try:
            report = load_repository_validation_report(
                Path(attempt_context.attempt_directory)
            )
        except (OSError, ValueError, ValidationError):
            return _RepositoryValidationAuthority(
                infrastructure_error=True,
                source="repository_validation_v2",
                status="infrastructure_error",
                failure_code="repository_validation_malformed_result",
                report_path=report_path.relative_to(
                    Path(attempt_context.run_directory)
                ).as_posix(),
            )
        if report is None:  # pragma: no cover - guarded by is_file
            return _RepositoryValidationAuthority(source="none")
        expected_fingerprint = str(
            attempt_result.metadata.get("execution_environment_fingerprint") or ""
        )
        expected_provider = str(
            attempt_result.metadata.get("execution_provider")
            or attempt_context.execution_provider
            or ""
        )
        expected_worktrees = {
            value
            for item in _allowed_worktree_paths(attempt_result)
            if (value := _resolved_path(item)) is not None
        }
        identity_valid = bool(
            report.run_id == attempt_context.run_id
            and report.attempt_id == attempt_context.attempt_id
            and report.candidate_id == attempt_context.attempt_id
            and expected_fingerprint
            and expected_worktrees
            and report.execution_environment_fingerprint == expected_fingerprint
            and (
                not expected_provider or report.execution_provider == expected_provider
            )
            and all(
                command.execution_environment_fingerprint == expected_fingerprint
                and command.execution_provider == report.execution_provider
                and _resolved_path(command.worktree_path) in expected_worktrees
                and command.candidate_state == "post_mutation"
                and (
                    attempt_context.baseline_sha256 is None
                    or command.baseline_sha256 == attempt_context.baseline_sha256
                )
                for command in report.commands
            )
        )
        relative = report_path.relative_to(
            Path(attempt_context.run_directory)
        ).as_posix()
        if not identity_valid:
            return _RepositoryValidationAuthority(
                infrastructure_error=True,
                source="repository_validation_v2",
                status="infrastructure_error",
                failure_code="repository_validation_environment_mismatch",
                retry_count=report.retry_count,
                report_path=relative,
            )
        return _RepositoryValidationAuthority(
            passed=report.status == "passed" and report.authoritative,
            failed=report.status == "failed" and report.authoritative,
            infrastructure_error=report.status == "infrastructure_error",
            unavailable=report.status == "unavailable",
            source="repository_validation_v2",
            status=report.status,
            failure_code=report.failure_code,
            retry_count=report.retry_count,
            report_path=relative,
        )
    return _legacy_repository_validation_authority(attempt_context, attempt_result)


def _repository_validation_authority(
    attempt_context: AttemptContext,
    attempt_result: AttemptResult,
) -> tuple[bool, bool]:
    authority = _repository_validation_details(attempt_context, attempt_result)
    return authority.passed, authority.failed


class VillaniVerifierAdapter:
    plugin_manifest = VERIFIER_MANIFEST

    def __init__(
        self,
        *,
        raw_verifier: Callable[..., Any] | None = None,
        invocation: str = "in_process",
        no_llm: bool = True,
        backend: str | None = None,
        timeout_seconds: int = 180,
        max_tool_calls: int = 12,
        base_url: str | None = None,
        model: str | None = None,
        backend_config: Backend | None = None,
    ) -> None:
        self._raw_verifier = raw_verifier
        self._invocation = invocation
        self._no_llm = no_llm
        self._backend = backend
        self._timeout_seconds = timeout_seconds
        self._max_tool_calls = max_tool_calls
        self._base_url = base_url
        self._model = model
        self._backend_config = backend_config

    def _llm_usage(
        self, trace_dir: Path, duration_ms: int, failure_state: str
    ) -> tuple[dict[str, Any], ...]:
        backend = self._backend_config
        records: list[dict[str, Any]] = []
        source = trace_dir / "llm_raw_responses.jsonl"
        if source.is_file():
            try:
                records = read_jsonl_tolerant(source)
            except Exception:
                records = []
        if not records:
            attempted_cost = (
                actual_attempt_cost(
                    backend,
                    input_tokens=None,
                    output_tokens=None,
                    duration_seconds=duration_ms / 1000,
                    started=not self._no_llm,
                )
                if backend is not None and not self._no_llm
                else None
            )
            return (
                {
                    "stage": "verification",
                    "backend": backend.name if backend else self._backend,
                    "model": backend.model if backend else self._model,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "token_accounting_status": "not_applicable"
                    if self._no_llm
                    else "unknown",
                    "model_calls": 0 if self._no_llm else None,
                    "model_call_accounting_status": "complete"
                    if self._no_llm
                    else "unknown",
                    "cost": attempted_cost.total if attempted_cost else None,
                    "cost_accounting_status": (
                        "not_applicable"
                        if self._no_llm
                        else attempted_cost.accounting_status
                        if attempted_cost
                        else "unknown"
                    ),
                    "currency": backend.currency if backend else "USD",
                    "duration_ms": duration_ms,
                    "duration_accounting_status": "complete",
                    "failure_state": failure_state,
                },
            )
        usages: list[dict[str, Any]] = []
        for record in records:
            raw_usage = record.get("usage")
            usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            has_usage = bool(usage)
            input_tokens = (
                int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                if has_usage
                else None
            )
            output_tokens = (
                int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                if has_usage
                else None
            )
            call_duration = int(record.get("durationMs") or 0)
            cost = (
                actual_attempt_cost(
                    backend,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_seconds=call_duration / 1000,
                    started=True,
                )
                if backend is not None
                else None
            )
            status = str(record.get("status") or "unknown")
            usages.append(
                {
                    "stage": "verification",
                    "backend": backend.name if backend else self._backend,
                    "model": str(
                        record.get("model")
                        or (backend.model if backend else self._model)
                        or ""
                    )
                    or None,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": (
                        input_tokens + output_tokens
                        if input_tokens is not None and output_tokens is not None
                        else None
                    ),
                    "token_accounting_status": "complete" if has_usage else "unknown",
                    "model_calls": 1,
                    "model_call_accounting_status": "complete",
                    "cost": cost.total if cost else None,
                    "cost_accounting_status": cost.accounting_status
                    if cost
                    else "unknown",
                    "currency": backend.currency if backend else "USD",
                    "duration_ms": call_duration,
                    "duration_accounting_status": "complete",
                    "failure_state": "succeeded" if status == "ok" else "failed",
                }
            )
        return tuple(usages)

    def _verification_context(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
        definitions: Sequence[RequirementDefinition],
        repository_authority: _RepositoryValidationAuthority,
    ) -> dict[str, Any]:
        return dict(
            redact_data(
                {
                    "schema_version": "villani.verifier_context.v2",
                    "run_id": attempt_context.run_id,
                    "attempt_id": attempt_context.attempt_id,
                    "task_prompt": attempt_context.task,
                    "success_criteria": attempt_context.success_criteria,
                    "requirements": [
                        item.model_dump(mode="json") for item in definitions
                    ],
                    "candidate_patch": (attempt_result.patch or "")[:60_000],
                    "changed_files": attempt_result.metadata.get("changed_files", []),
                    "repository_validation": {
                        "status": repository_authority.status,
                        "authoritative": repository_authority.passed
                        or repository_authority.failed,
                        "failure_code": repository_authority.failure_code,
                        "report_path": repository_authority.report_path,
                    },
                    "execution_environment_fingerprint": attempt_result.metadata.get(
                        "execution_environment_fingerprint"
                    ),
                }
            )
        )

    def _build_verification(
        self,
        *,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
        repository_authority: _RepositoryValidationAuthority,
        raw: Mapping[str, Any],
        raw_path: Path,
        invocation_status: str,
        resolution_status: str,
        resolution_reason: str,
        subprocess_exit_code: int | None,
        llm_usage: tuple[Mapping[str, Any], ...],
        semantic_verifier_invoked: bool,
    ) -> Verification:
        run_dir = Path(attempt_context.run_directory).resolve()
        raw_artifact = raw_path.relative_to(run_dir).as_posix()
        definitions = extract_requirements(
            task_instruction=attempt_context.task,
            success_criteria=attempt_context.success_criteria,
            policy_configuration=attempt_context.policy_configuration,
        )
        definitions, semantic_assessments = _semantic_requirements(definitions, raw)
        definition_ids = {item.requirement_id for item in definitions}
        probe_requests, rejected_probes = parse_focused_probe_requests(
            raw.get("focusedProbeRequests"),
            requirement_ids=definition_ids,
        )

        registry: dict[str, EvidenceItem] = {}
        buckets: dict[str, Literal["success", "failure", "missing"]] = {}

        def register(
            item: EvidenceItem,
            bucket: Literal["success", "failure", "missing"],
        ) -> None:
            registry[item.evidence_id] = item
            current = buckets.get(item.evidence_id)
            rank = {"success": 0, "missing": 1, "failure": 2}
            if current is None or rank[bucket] > rank[current]:
                buckets[item.evidence_id] = bucket

        for prefix, key, bucket in (
            ("semantic_success", "successEvidence", "success"),
            ("semantic_failure", "failureEvidence", "failure"),
            ("semantic_missing", "missingEvidence", "missing"),
        ):
            for index, item in enumerate(_list(raw.get(key)), 1):
                evidence_id = (
                    str(item.get("id") or item.get("evidence_id"))
                    if isinstance(item, Mapping)
                    and (item.get("id") or item.get("evidence_id"))
                    else f"{prefix}_{index:03d}"
                )
                register(
                    EvidenceItem(
                        evidence_id=evidence_id,
                        kind=(
                            str(
                                item.get("kind")
                                or item.get("evidence_type")
                                or "semantic_reasoning"
                            )
                            if isinstance(item, Mapping)
                            else "semantic_reasoning"
                        ),
                        summary=_text(item)[:1000],
                        artifact_path=raw_artifact,
                        details={"provenance": "semantic_output"},
                    ),
                    bucket,  # type: ignore[arg-type]
                )

        for index, item in enumerate(_list(raw.get("toolsUsed")), 1):
            if not isinstance(item, Mapping):
                continue
            tool = str(item.get("tool") or "")
            evidence_id = str(item.get("evidence_id") or f"semantic_tool_{index:03d}")
            kind = (
                "source_inspection"
                if tool
                in {
                    "read_repo_file",
                    "search_repo",
                    "read_diff",
                    "search_diff",
                }
                else "debug_trace"
            )
            register(
                EvidenceItem(
                    evidence_id=evidence_id,
                    kind=kind,
                    summary=str(item.get("reason") or f"Verifier used {tool}."),
                    artifact_path=raw_artifact,
                    details={
                        "tool": tool,
                        "status": item.get("status", "ok"),
                        "provenance": "verifier_tool",
                    },
                ),
                "success" if str(item.get("status") or "ok") == "ok" else "missing",
            )

        repository_report = None
        if repository_authority.report_path:
            try:
                repository_report = load_repository_validation_report(
                    Path(attempt_context.attempt_directory)
                )
            except (OSError, ValueError, ValidationError):
                repository_report = None
        repository_evidence_by_validation_id: dict[str, str] = {}
        if repository_report is not None:
            for command in repository_report.commands:
                evidence_id = f"repository_validation:{command.validation_id}"
                repository_evidence_by_validation_id[command.validation_id] = (
                    evidence_id
                )
                register(
                    EvidenceItem(
                        evidence_id=evidence_id,
                        kind="repository_validation",
                        summary=(
                            f"Repository validation {command.validation_id} "
                            f"{command.status}."
                        ),
                        artifact_path=repository_authority.report_path,
                        details={
                            "validation_id": command.validation_id,
                            "argv": list(command.argv),
                            "status": command.status,
                            "exit_code": command.exit_code,
                            "failure_code": command.failure_code,
                            "execution_environment_fingerprint": (
                                command.execution_environment_fingerprint
                            ),
                            "worktree_path": command.worktree_path,
                            "candidate_state": command.candidate_state,
                        },
                    ),
                    (
                        "success"
                        if command.status == "passed"
                        else "failure"
                        if command.status == "failed"
                        else "missing"
                    ),
                )
        repository_aggregate_id = "repository_validation:aggregate"
        if repository_authority.source != "none":
            register(
                EvidenceItem(
                    evidence_id=repository_aggregate_id,
                    kind="repository_validation",
                    summary=(
                        "Authoritative repository validation "
                        f"{repository_authority.status}."
                    ),
                    artifact_path=repository_authority.report_path,
                    details={
                        "status": repository_authority.status,
                        "failure_code": repository_authority.failure_code,
                        "authority_source": repository_authority.source,
                    },
                ),
                (
                    "success"
                    if repository_authority.passed
                    else "failure"
                    if repository_authority.failed
                    else "missing"
                ),
            )

        for request in probe_requests:
            register(
                EvidenceItem(
                    evidence_id=f"focused_probe_request:{request.probe_id}",
                    kind="focused_probe_request",
                    summary=request.reason,
                    artifact_path=(
                        f"verification/{attempt_context.attempt_id}"
                        "-focused-probe-requests.json"
                    ),
                    details={
                        "probe_id": request.probe_id,
                        "requirement_ids": list(request.requirement_ids),
                        "argv": list(request.argv),
                    },
                ),
                "missing",
            )

        focused_report: FocusedProbeReport | None = None
        focused_path_value = attempt_result.metadata.get("focused_probe_report_path")
        focused_path = (
            (run_dir / str(focused_path_value)).resolve()
            if focused_path_value
            else run_dir
            / "verification"
            / f"{attempt_context.attempt_id}-focused-probes.json"
        )
        focused_report_error: str | None = None
        if focused_path.is_file():
            try:
                focused_report = load_focused_probe_report(focused_path)
            except (OSError, ValueError, ValidationError) as error:
                focused_report_error = f"malformed focused probe report: {error}"
        expected_fingerprint = str(
            attempt_result.metadata.get("execution_environment_fingerprint") or ""
        )
        expected_provider = str(
            attempt_result.metadata.get("execution_provider")
            or attempt_context.execution_provider
            or ""
        )
        baseline_sha256 = (
            attempt_context.baseline_sha256
            or str(attempt_result.metadata.get("baseline_sha256") or "")
            or "unknown"
        )
        if focused_report is not None and not focused_probe_identity_valid(
            focused_report,
            run_id=attempt_context.run_id,
            attempt_id=attempt_context.attempt_id,
            baseline_sha256=baseline_sha256,
            execution_environment_fingerprint=expected_fingerprint,
            execution_provider=expected_provider,
            allowed_worktree_paths=_allowed_worktree_paths(attempt_result),
        ):
            focused_report_error = (
                "focused probe identity or execution environment does not "
                "match the candidate"
            )
            focused_report = None

        focused_by_requirement: dict[str, list[Any]] = {}
        if focused_report is not None:
            relative_focused = focused_path.relative_to(run_dir).as_posix()
            for result in focused_report.results:
                command = result.command_result
                register(
                    EvidenceItem(
                        evidence_id=result.evidence_id,
                        kind="focused_probe",
                        summary=result.reason,
                        artifact_path=relative_focused,
                        details={
                            "probe_id": result.probe_id,
                            "requirement_ids": list(result.requirement_ids),
                            "status": result.status,
                            "argv": list(command.argv),
                            "exit_code": command.exit_code,
                            "stdout": command.stdout,
                            "stderr": command.stderr,
                            "failure_code": command.failure_code,
                            "execution_environment_fingerprint": (
                                command.execution_environment_fingerprint
                            ),
                            "worktree_path": command.worktree_path,
                            "candidate_state": command.candidate_state,
                        },
                    ),
                    (
                        "success"
                        if result.status == "passed"
                        else "failure"
                        if result.status == "failed"
                        else "missing"
                    ),
                )
                for requirement_id in result.requirement_ids:
                    focused_by_requirement.setdefault(requirement_id, []).append(result)
                request_evidence_id = f"focused_probe_request:{result.probe_id}"
                if request_evidence_id in registry:
                    buckets[request_evidence_id] = (
                        "success"
                        if result.status == "passed"
                        else "failure"
                        if result.status == "failed"
                        else "missing"
                    )

        patch_text = (attempt_result.patch or "").casefold()
        raw_changed_files = attempt_result.metadata.get("changed_files")
        changed_files = (
            {str(item).casefold() for item in raw_changed_files}
            if isinstance(raw_changed_files, list)
            else set()
        )
        requirement_matrix: list[RequirementEvidence] = []
        for definition in definitions:
            semantic_status, semantic_refs = semantic_assessments.get(
                definition.requirement_id,
                ("not_assessed", []),
            )
            for reference in semantic_refs:
                if reference not in registry:
                    register(
                        EvidenceItem(
                            evidence_id=reference,
                            kind="semantic_reasoning",
                            summary=reference,
                            artifact_path=raw_artifact,
                        ),
                        "success"
                        if semantic_status == "passed"
                        else "failure"
                        if semantic_status == "failed"
                        else "missing",
                    )

            evidence_type: Literal[
                "repository_validation",
                "focused_probe",
                "static_patch_evidence",
                "source_inspection",
                "debug_trace",
                "semantic_reasoning",
            ] = "semantic_reasoning"
            deterministic_status: Literal[
                "passed",
                "failed",
                "missing",
                "not_applicable",
                "infrastructure_error",
            ] = "missing"
            evidence_ids: list[str] = []
            reason = "No deterministic evidence proves this requirement."

            focused = focused_by_requirement.get(definition.requirement_id, [])
            requested = [
                item
                for item in probe_requests
                if definition.requirement_id in item.requirement_ids
            ]
            if focused:
                evidence_type = "focused_probe"
                evidence_ids = [item.evidence_id for item in focused]
                if any(item.status == "infrastructure_error" for item in focused):
                    deterministic_status = "infrastructure_error"
                    reason = (
                        "A focused probe could not execute reliably in the "
                        "candidate environment."
                    )
                elif any(item.status == "failed" for item in focused):
                    deterministic_status = "failed"
                    reason = next(
                        item.reason for item in focused if item.status == "failed"
                    )
                else:
                    deterministic_status = "passed"
                    reason = "Focused executable evidence proved the requirement."
            elif focused_report_error and requested:
                evidence_type = "focused_probe"
                evidence_id = (
                    f"focused_probe_infrastructure:{definition.requirement_id}"
                )
                register(
                    EvidenceItem(
                        evidence_id=evidence_id,
                        kind="focused_probe",
                        summary=focused_report_error,
                        artifact_path=(
                            focused_path.relative_to(run_dir).as_posix()
                            if focused_path.is_file()
                            else None
                        ),
                    ),
                    "missing",
                )
                evidence_ids = [evidence_id]
                deterministic_status = "infrastructure_error"
                reason = focused_report_error
            else:
                coverage = _configured_command_coverage(
                    definition, attempt_context.policy_configuration
                )
                repository_ids = [
                    repository_evidence_by_validation_id[item]
                    for item in sorted(coverage)
                    if item in repository_evidence_by_validation_id
                ]
                negative_constraint = any(
                    marker in definition.description.casefold()
                    for marker in ("do not", "must not", "never", "without")
                )
                if (
                    not repository_ids
                    and repository_authority.source != "none"
                    and not definition.observable
                    and not negative_constraint
                ):
                    repository_ids = [repository_aggregate_id]
                if repository_ids:
                    evidence_type = "repository_validation"
                    evidence_ids = repository_ids
                    if repository_authority.infrastructure_error:
                        deterministic_status = "infrastructure_error"
                        reason = "Repository validation could not execute reliably."
                    elif repository_authority.failed:
                        deterministic_status = "failed"
                        reason = "Repository validation contradicted the requirement."
                    elif repository_authority.passed:
                        deterministic_status = "passed"
                        reason = (
                            "Authoritative repository validation proved this "
                            "requirement."
                        )
                if deterministic_status == "missing":
                    source_ids = [
                        reference
                        for reference in semantic_refs
                        if reference in registry
                        and registry[reference].kind
                        in {"source_inspection", "static_patch_evidence"}
                        and registry[reference].details.get("provenance")
                        == "verifier_tool"
                        and registry[reference].details.get("status", "ok") == "ok"
                    ]
                    if source_ids and not definition.observable:
                        evidence_type = "source_inspection"
                        evidence_ids = source_ids
                        deterministic_status = "passed"
                        reason = (
                            "Structured source or patch inspection proved this "
                            "non-executable requirement."
                        )
                if deterministic_status == "missing" and not definition.observable:
                    terms = _meaningful_terms(definition.description)
                    matched = sorted(
                        term
                        for term in terms
                        if term in patch_text
                        or any(term in path for path in changed_files)
                    )
                    if matched:
                        evidence_id = f"static_patch:{definition.requirement_id}"
                        register(
                            EvidenceItem(
                                evidence_id=evidence_id,
                                kind="static_patch_evidence",
                                summary=(
                                    "The candidate patch directly contains "
                                    + ", ".join(matched[:5])
                                    + "."
                                ),
                                artifact_path=attempt_result.metadata.get(
                                    "candidate_patch_path"
                                ),
                            ),
                            "success",
                        )
                        evidence_type = "static_patch_evidence"
                        evidence_ids = [evidence_id]
                        deterministic_status = "passed"
                        reason = "Direct patch inspection proved the requirement."
                if deterministic_status == "missing" and definition.observable:
                    evidence_type = "focused_probe"
                    evidence_ids = [
                        f"focused_probe_request:{item.probe_id}" for item in requested
                    ]
                    if not evidence_ids:
                        evidence_id = (
                            f"focused_probe_missing:{definition.requirement_id}"
                        )
                        register(
                            EvidenceItem(
                                evidence_id=evidence_id,
                                kind="focused_probe",
                                summary=(
                                    "This directly testable critical requirement "
                                    "has no authoritative executable evidence."
                                ),
                                artifact_path=None,
                            ),
                            "missing",
                        )
                        evidence_ids = [evidence_id]
                    reason = (
                        "Directly observable behavior requires an authoritative "
                        "focused probe or explicitly covering repository validation."
                    )

            if not evidence_ids:
                evidence_id = f"semantic_missing:{definition.requirement_id}"
                register(
                    EvidenceItem(
                        evidence_id=evidence_id,
                        kind="semantic_reasoning",
                        summary=reason,
                        artifact_path=raw_artifact,
                    ),
                    "missing",
                )
                evidence_ids = [evidence_id]

            contradiction = bool(
                deterministic_status == "passed"
                and semantic_status == "failed"
                or deterministic_status == "failed"
                and semantic_status == "passed"
            )
            if deterministic_status == "infrastructure_error":
                final_status = "infrastructure_error"
            elif deterministic_status == "failed" or semantic_status == "failed":
                final_status = "failed"
            elif deterministic_status == "passed":
                final_status = "passed"
            elif not definition.critical and semantic_status == "passed":
                final_status = "passed"
            else:
                final_status = "missing"
            requirement_matrix.append(
                RequirementEvidence(
                    requirement_id=definition.requirement_id,
                    description=definition.description,
                    critical=definition.critical,
                    evidence_type=evidence_type,
                    evidence_ids=evidence_ids,
                    deterministic_status=deterministic_status,
                    semantic_status=semantic_status,
                    contradiction=contradiction,
                    final_status=final_status,  # type: ignore[arg-type]
                    reason=reason,
                )
            )

        result_value = raw.get("result")
        verdict = str(raw.get("verdict") or "").casefold()
        raw_action = str(raw.get("recommendedAction") or "").casefold()
        raw_result: Literal[0, 1] | None = None
        if type(result_value) is int and result_value in {0, 1}:
            raw_result = 0 if result_value == 0 else 1
        raw_result_valid = raw_result is not None
        semantic_verdict: Literal["success", "failure", "unclear", "error"]
        if verdict == "success":
            semantic_verdict = "success"
        elif verdict == "failure":
            semantic_verdict = "failure"
        elif verdict == "unclear":
            semantic_verdict = "unclear"
        else:
            semantic_verdict = "error"
        raw_shape_valid = bool(
            raw_result_valid
            and verdict in {"success", "failure", "unclear", "error"}
            and raw_action in {"accept", "reject", "retry_verifier", "escalate"}
            and isinstance(raw.get("requirementResults", []), list)
            and isinstance(raw.get("focusedProbeRequests", []), list)
        )
        normalized_invocation = (
            invocation_status
            if invocation_status != "completed"
            else "malformed_output"
            if not raw_shape_valid
            else "completed"
        )
        eligibility = candidate_eligibility(
            patch=attempt_result.patch,
            requires_file_changes=attempt_context.requires_file_changes,
            attempt_status=attempt_result.status,
            failure_classification=attempt_result.metadata.get(
                "failure_classification"
            ),
            candidate_quality=_candidate_quality(
                attempt_result.metadata.get("candidate_quality_report")
            ),
        )
        repository_input = RepositoryValidationDecisionInput(
            status=repository_authority.status,  # type: ignore[arg-type]
            authoritative=repository_authority.passed or repository_authority.failed,
            required=_repository_validation_required(
                attempt_context.policy_configuration
            ),
            failure_code=repository_authority.failure_code,
        )
        semantic_input = SemanticReviewDecisionInput(
            raw_result=raw_result,
            verdict=semantic_verdict,
            recommended_action=raw_action,
            schema_valid=raw_shape_valid,
            critical_failure_reported=any(
                item.critical and item.semantic_status == "failed"
                for item in requirement_matrix
            ),
        )
        decision = compute_final_verification_decision(
            eligibility,
            repository_input,
            requirement_matrix,
            semantic_input,
            normalized_invocation,
        )
        matrix = evidence_matrix(
            run_id=attempt_context.run_id,
            attempt_id=attempt_context.attempt_id,
            requirements=requirement_matrix,
            repository_validation_status=repository_authority.status,
            candidate_eligibility_status=eligibility.status,
            semantic_verifier_status=(
                verdict if semantic_verifier_invoked else "not_invoked"
            ),
            decision=decision,
        )
        matrix_path = (
            run_dir / "verification" / f"{attempt_context.attempt_id}-evidence.json"
        )
        write_json_atomic(matrix_path, redact_data(matrix))
        matrix_relative = matrix_path.relative_to(run_dir).as_posix()

        for requirement in requirement_matrix:
            final_bucket: Literal["success", "failure", "missing"] = (
                "success"
                if requirement.final_status == "passed"
                else "failure"
                if requirement.final_status == "failed"
                else "missing"
            )
            for evidence_id in requirement.evidence_ids:
                if evidence_id not in registry:
                    register(
                        EvidenceItem(
                            evidence_id=evidence_id,
                            kind=requirement.evidence_type,
                            summary=requirement.reason,
                            artifact_path=matrix_relative,
                        ),
                        final_bucket,
                    )
                elif final_bucket != "success":
                    register(registry[evidence_id], final_bucket)

        success = tuple(
            item
            for evidence_id, item in registry.items()
            if buckets.get(evidence_id) == "success"
        )
        failure = tuple(
            item
            for evidence_id, item in registry.items()
            if buckets.get(evidence_id) == "failure"
        )
        missing = tuple(
            item
            for evidence_id, item in registry.items()
            if buckets.get(evidence_id) == "missing"
        )
        requirements = tuple(
            Requirement(
                requirement_id=item.requirement_id,
                description=item.description,
                outcome=(
                    "missing"
                    if item.final_status == "infrastructure_error"
                    else item.final_status
                ),
                evidence_ids=tuple(item.evidence_ids),
            )
            for item in requirement_matrix
        )
        pending_requests = (
            probe_requests
            if focused_report is None
            or focused_report.status in {"unavailable", "infrastructure_error"}
            else []
        )
        disagreement = bool(raw_result is not None and raw_result != decision.result)
        raw_risks = _risk_texts(raw)
        risk_flags = (
            raw_risks
            + tuple(f"focused_probe_rejected:{item}" for item in rejected_probes)
            + (
                ()
                if decision.result == 1
                else (f"acceptance_blocker:{decision.reason_code}",)
            )
        )
        return Verification(
            verifier="villani_ops_verifier_pipeline",
            outcome=_outcome_for_decision(decision),
            acceptance_eligible=decision.result == 1,
            confidence=(
                float(raw["confidence"])
                if isinstance(raw.get("confidence"), (int, float))
                else None
            ),
            reason=decision.reason,
            recommended_action=decision.recommended_action,
            requirement_results=requirements,
            success_evidence=success,
            failure_evidence=failure,
            missing_evidence=missing,
            risk_flags=risk_flags,
            raw_verifier_artifact=raw_artifact,
            metadata={
                "verifier_version": "villani_ops_verifier_pipeline_v2",
                "invocation_status": normalized_invocation,
                "resolution_status": resolution_status,
                "resolution_reason": resolution_reason,
                "subprocess_exit_code": subprocess_exit_code,
                "verification_mode": "deterministic_evidence_matrix_v2",
                "authority_source": "deterministic_evidence_matrix_v2",
                "repository_validation_authority_source": (repository_authority.source),
                "repository_validation_path": repository_authority.report_path,
                "repository_validation_status": repository_authority.status,
                "repository_validation_failure_code": (
                    repository_authority.failure_code
                ),
                "repository_validation_retry_count": (repository_authority.retry_count),
                "candidate_eligibility_status": eligibility.status,
                "candidate_patch_quality_path": attempt_result.metadata.get(
                    "candidate_patch_quality_path"
                ),
                "candidate_quality_report": attempt_result.metadata.get(
                    "candidate_quality_report"
                ),
                "semantic_verifier_invoked": semantic_verifier_invoked,
                "semantic_verifier_status": verdict,
                "raw_llm_result": result_value,
                "raw_result": result_value,
                "raw_verdict": verdict,
                "raw_recommended_action": raw_action,
                "computed_final_result": decision.result,
                "computed_final_reason_code": decision.reason_code,
                "verifier_disagreement": disagreement,
                "critical_requirement_coverage_proven": (
                    matrix.critical_requirements_proven
                ),
                "verification_evidence_path": matrix_relative,
                "focused_probe_requests": [
                    item.model_dump(mode="json") for item in pending_requests
                ],
                "focused_probe_requests_pending": bool(pending_requests),
                "focused_probe_rejections": rejected_probes,
                "focused_probe_report_path": (
                    focused_path.relative_to(run_dir).as_posix()
                    if focused_path.is_file()
                    else None
                ),
                "retry_scope": decision.retry_scope,
            },
            llm_usage=llm_usage,
        )

    def verify(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
    ) -> Verification:
        repository_authority = _repository_validation_details(
            attempt_context, attempt_result
        )
        run_dir = Path(attempt_context.run_directory).resolve()
        raw_dir = run_dir / "verification" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{attempt_context.attempt_id}.json"
        trace_dir = raw_dir / f"{attempt_context.attempt_id}_trace"
        suffix = 2
        while trace_dir.exists():
            trace_dir = raw_dir / f"{attempt_context.attempt_id}_trace_{suffix}"
            suffix += 1

        if repository_authority.infrastructure_error:
            raw = {
                "result": 0,
                "verdict": "error",
                "recommendedAction": "retry_verifier",
                "reason": (
                    "Repository validation could not execute reliably against "
                    "the preserved candidate."
                ),
                "requirementResults": [],
                "successEvidence": [],
                "failureEvidence": [],
                "missingEvidence": [],
                "riskFlags": [],
                "criticalRequirementCoverageProven": False,
                "focusedProbeRequests": [],
            }
            write_json_atomic(raw_path, redact_data(raw))
            return self._build_verification(
                attempt_context=attempt_context,
                attempt_result=attempt_result,
                repository_authority=repository_authority,
                raw=raw,
                raw_path=raw_path,
                invocation_status="completed",
                resolution_status="not_invoked",
                resolution_reason=(
                    "Semantic verification was deferred until repository "
                    "validation infrastructure is available."
                ),
                subprocess_exit_code=None,
                llm_usage=(
                    {
                        "stage": "verification",
                        "backend": (
                            self._backend_config.name
                            if self._backend_config
                            else self._backend
                        ),
                        "model": (
                            self._backend_config.model
                            if self._backend_config
                            else self._model
                        ),
                        "input_tokens": None,
                        "output_tokens": None,
                        "total_tokens": None,
                        "token_accounting_status": "not_applicable",
                        "model_calls": 0,
                        "model_call_accounting_status": "complete",
                        "cost": None,
                        "cost_accounting_status": "not_applicable",
                        "currency": (
                            self._backend_config.currency
                            if self._backend_config
                            else "USD"
                        ),
                        "duration_ms": 0,
                        "duration_accounting_status": "complete",
                        "failure_state": "failed",
                    },
                ),
                semantic_verifier_invoked=False,
            )

        definitions = extract_requirements(
            task_instruction=attempt_context.task,
            success_criteria=attempt_context.success_criteria,
            policy_configuration=attempt_context.policy_configuration,
        )
        context_payload = self._verification_context(
            attempt_context,
            attempt_result,
            definitions,
            repository_authority,
        )
        context_path = (
            raw_dir / f"{attempt_context.attempt_id}-verification-context.json"
        )
        write_json_atomic(context_path, context_payload)
        trace_value = attempt_result.metadata.get("debug_trace_path")
        trace_path = (run_dir / str(trace_value)).resolve() if trace_value else None
        if not self._no_llm and self._backend_config is not None:
            validate_runtime_credentials(self._backend_config)
        started = time.monotonic()
        execution = execute_verifier(
            debug_root=trace_path,
            resolved_trace_dir=trace_path,
            repo_dir=Path(attempt_result.worktree_path),
            workspace=run_dir,
            out=raw_path,
            trace_dir=trace_dir,
            backend=self._backend,
            timeout_seconds=self._timeout_seconds,
            max_tool_calls=self._max_tool_calls,
            verifier=self._raw_verifier,
            invocation=self._invocation,  # type: ignore[arg-type]
            no_llm=self._no_llm,
            base_url=self._base_url,
            model=self._model,
            api_key=(
                self._backend_config.resolved_api_key()
                if self._backend_config
                else None
            ),
            verification_context=context_payload,
            verification_context_path=context_path,
        )
        duration_ms = max(int((time.monotonic() - started) * 1000), 0)
        raw_value = redact_data(execution.result)
        raw = raw_value if isinstance(raw_value, dict) else {}
        return self._build_verification(
            attempt_context=attempt_context,
            attempt_result=attempt_result,
            repository_authority=repository_authority,
            raw=raw,
            raw_path=raw_path,
            invocation_status=execution.invocation_status,
            resolution_status=execution.resolution_status,
            resolution_reason=execution.resolution_reason,
            subprocess_exit_code=execution.subprocess_exit_code,
            llm_usage=self._llm_usage(
                trace_dir,
                duration_ms,
                (
                    "failed"
                    if execution.invocation_status != "completed"
                    else "succeeded"
                ),
            ),
            semantic_verifier_invoked=True,
        )

    def finalize_with_focused_probes(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
        initial_verification: Verification,
    ) -> Verification:
        """Recompute authority from persisted probe results without another LLM call."""

        raw_artifact = initial_verification.raw_verifier_artifact
        if not raw_artifact:
            return initial_verification
        run_dir = Path(attempt_context.run_directory).resolve()
        raw_path = (run_dir / raw_artifact).resolve()
        try:
            raw_value = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_value = {}
        raw = raw_value if isinstance(raw_value, dict) else {}
        repository_authority = _repository_validation_details(
            attempt_context, attempt_result
        )
        return self._build_verification(
            attempt_context=attempt_context,
            attempt_result=attempt_result,
            repository_authority=repository_authority,
            raw=raw,
            raw_path=raw_path,
            invocation_status=str(
                initial_verification.metadata.get("invocation_status") or "completed"
            ),
            resolution_status=str(
                initial_verification.metadata.get("resolution_status") or "resolved"
            ),
            resolution_reason=str(
                initial_verification.metadata.get("resolution_reason")
                or "recomputed after focused probes"
            ),
            subprocess_exit_code=(
                int(initial_verification.metadata["subprocess_exit_code"])
                if isinstance(
                    initial_verification.metadata.get("subprocess_exit_code"),
                    int,
                )
                else None
            ),
            llm_usage=initial_verification.llm_usage,
            semantic_verifier_invoked=bool(
                initial_verification.metadata.get("semantic_verifier_invoked", True)
            ),
        )
