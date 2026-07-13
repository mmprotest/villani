"""Normalized closed-loop adapter for the shared Villani verifier pipeline."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable, Literal, Mapping

from villani_ops.verifier.service import execute_verifier
from villani_ops.closed_loop.costs import actual_attempt_cost
from villani_ops.closed_loop.durable_io import read_jsonl_tolerant
from villani_ops.core.backend import Backend

from ..event_writer import redact_data
from ..interfaces import (
    AttemptContext,
    AttemptResult,
    EvidenceItem,
    Requirement,
    Verification,
)
from ..plugins.builtins import VERIFIER_MANIFEST


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


def _repository_validation_authority(
    attempt_result: AttemptResult,
) -> tuple[bool, bool]:
    """Return (authoritative pass, active failure) from structured runtime events."""

    mutations = [
        event.timestamp for event in attempt_result.runtime_events if event.event_type == "file_write"
    ]
    final_mutation = max(mutations) if mutations else None
    validations = [
        event
        for event in attempt_result.runtime_events
        if event.event_type in {"command_completed", "command_failed"}
        and event.payload.get("command_role") == "validation"
        and (final_mutation is None or event.timestamp >= final_mutation)
    ]
    if not validations:
        return False, False
    active_failure = any(
        event.event_type == "command_failed" or event.payload.get("exit_code") not in {None, 0}
        for event in validations
    )
    return not active_failure, active_failure


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

    def verify(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
    ) -> Verification:
        run_dir = Path(attempt_context.run_directory).resolve()
        raw_dir = run_dir / "verification" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{attempt_context.attempt_id}.json"
        # Each verifier retry gets its own explicit trace directory.  The
        # trace writer intentionally rejects reusing an existing directory,
        # and separate paths preserve usage for every call rather than
        # overwriting the first retry's records.
        trace_dir = raw_dir / f"{attempt_context.attempt_id}_trace"
        suffix = 2
        while trace_dir.exists():
            trace_dir = raw_dir / f"{attempt_context.attempt_id}_trace_{suffix}"
            suffix += 1
        trace_value = attempt_result.metadata.get("debug_trace_path")
        trace_path = (run_dir / str(trace_value)).resolve() if trace_value else None
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
        )
        duration_ms = max(int((time.monotonic() - started) * 1000), 0)
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
        repository_pass, repository_failure = _repository_validation_authority(attempt_result)
        if repository_failure:
            blockers.append("repository_validation_failed")
        authority_source = (
            "authoritative_repository_validation"
            if repository_pass
            else "heuristic_only"
            if self._no_llm
            else "llm_authoritative"
        )
        if self._no_llm and not repository_pass:
            blockers.append("non_authoritative_heuristic")

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

        if raw_action == "accept" and eligible:
            recommended = "accept"
        elif raw_action in {"retry_higher_model", "escalate"}:
            recommended = "escalate"
        elif outcome == "error":
            recommended = "retry_verifier"
        else:
            recommended = "reject"
        reason = str(raw.get("reason") or execution.resolution_reason)
        if blockers:
            reason = (
                reason.rstrip(".")
                + ". Acceptance blockers: "
                + ", ".join(blockers)
                + "."
            )
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
                "verifier_version": "villani_ops_verifier_pipeline_v1",
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
                "verification_mode": "authoritative_repository_validation"
                if repository_pass
                else "deterministic_heuristic"
                if self._no_llm
                else "llm_verifier",
                "authority_source": authority_source,
            },
            llm_usage=self._llm_usage(
                trace_dir,
                duration_ms,
                "failed" if execution.invocation_status != "completed" else "succeeded",
            ),
        )
