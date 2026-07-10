"""Normalized closed-loop adapter for the shared Villani verifier pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from villani_ops.verifier.service import execute_verifier

from ..event_writer import redact_data
from ..interfaces import (
    AttemptContext,
    AttemptResult,
    EvidenceItem,
    Requirement,
    Verification,
)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("summary") or value.get("reason") or value)
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
        outcome = "passed" if status in {"satisfied", "passed"} else "failed"
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


class VillaniVerifierAdapter:
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
    ) -> None:
        self._raw_verifier = raw_verifier
        self._invocation = invocation
        self._no_llm = no_llm
        self._backend = backend
        self._timeout_seconds = timeout_seconds
        self._max_tool_calls = max_tool_calls
        self._base_url = base_url
        self._model = model

    def verify(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
    ) -> Verification:
        run_dir = Path(attempt_context.run_directory).resolve()
        raw_dir = run_dir / "verification" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{attempt_context.attempt_id}.json"
        trace_dir = raw_dir / f"{attempt_context.attempt_id}_trace"
        trace_value = attempt_result.metadata.get("debug_trace_path")
        trace_path = (
            (run_dir / str(trace_value)).resolve() if trace_value else None
        )
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
        )
        raw = redact_data(execution.result)
        if not isinstance(raw, dict):
            raw = {}
        requirements = _requirements(raw)
        success = _evidence(
            _list(raw.get("successEvidence")),
            "success",
            raw_path.relative_to(run_dir).as_posix(),
        )
        failure = _evidence(
            _list(raw.get("failureEvidence")),
            "failure",
            raw_path.relative_to(run_dir).as_posix(),
        )
        missing = _evidence(
            _list(raw.get("missingEvidence")),
            "missing",
            raw_path.relative_to(run_dir).as_posix(),
        )
        risks = _risk_texts(raw)
        result_value = raw.get("result")
        verdict = str(raw.get("verdict") or "").lower()
        raw_action = str(raw.get("recommendedAction") or "").lower()
        raw_shape_valid = bool(
            result_value in {0, 1, None}
            and verdict in {"success", "failure", "error", "unclear"}
            and raw_action
        )
        critical_proven = raw.get("criticalRequirementCoverageProven") is True
        if not critical_proven:
            critical_proven = bool(
                requirements
                and all(item.outcome == "passed" for item in requirements)
                and success
                and not missing
            )

        blockers: list[str] = []
        if not raw_shape_valid:
            blockers.append("malformed_verifier_output")
        if execution.invocation_status != "completed":
            blockers.append(execution.invocation_status)
        if execution.debug_dir is None:
            blockers.append("missing_compatible_trace")
        if attempt_result.exit_code != 0:
            blockers.append("runner_nonzero_exit")
        if result_value != 1:
            blockers.append("verifier_result_not_one")
        if verdict != "success":
            blockers.append("verifier_verdict_not_success")
        if raw_action != "accept":
            blockers.append("verifier_recommendation_not_accept")
        if not critical_proven:
            blockers.append("missing_critical_evidence")
        if missing:
            blockers.append("missing_evidence")
        if _has_acceptance_blocker(risks):
            blockers.append("acceptance_blocker")
        if attempt_context.requires_file_changes and not (
            attempt_result.patch and attempt_result.patch.strip()
        ):
            blockers.append("empty_patch")

        eligible = not blockers
        infrastructure_failure = (
            attempt_result.metadata.get("failure_classification")
            == "infrastructure_failure"
        )
        if (
            execution.invocation_status != "completed"
            or verdict == "error"
            or not raw_shape_valid
            or infrastructure_failure
        ):
            outcome = "error"
        elif verdict in {"unclear", "unknown"}:
            outcome = "unclear"
        elif eligible:
            outcome = "accepted"
        else:
            outcome = "rejected"

        if raw_action == "accept":
            recommended = "accept"
        elif raw_action in {"retry_higher_model", "escalate"}:
            recommended = "escalate"
        elif outcome == "error":
            recommended = "retry_verifier"
        else:
            recommended = "reject"
        reason = str(raw.get("reason") or execution.resolution_reason)
        if blockers:
            reason = reason.rstrip(".") + ". Acceptance blockers: " + ", ".join(blockers) + "."
        return Verification(
            verifier="villani_ops_verifier_pipeline",
            outcome=outcome,  # type: ignore[arg-type]
            acceptance_eligible=eligible,
            confidence=(
                float(raw["confidence"])
                if isinstance(raw.get("confidence"), (int, float))
                else None
            ),
            reason=reason,
            recommended_action=recommended,  # type: ignore[arg-type]
            requirement_results=requirements,
            success_evidence=success,
            failure_evidence=failure,
            missing_evidence=missing,
            risk_flags=risks + tuple(f"acceptance_blocker:{item}" for item in blockers),
            raw_verifier_artifact=raw_path.relative_to(run_dir).as_posix(),
            metadata={
                "invocation_status": (
                    "malformed_output"
                    if not raw_shape_valid
                    else execution.invocation_status
                ),
                "resolution_status": execution.resolution_status,
                "resolution_reason": execution.resolution_reason,
                "subprocess_exit_code": execution.subprocess_exit_code,
                "critical_requirement_coverage_proven": critical_proven,
                "raw_result": result_value,
                "raw_verdict": verdict,
                "raw_recommended_action": raw_action,
            },
        )
