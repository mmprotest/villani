"""Deterministic closed-loop controller with dependency-injected side effects."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .event_writer import EventWriter, failure_payload, redact_data, redact_message
from .interfaces import (
    AttemptContext,
    AttemptResult,
    AttemptRunner,
    AttemptSummary,
    BackendOption,
    BudgetContext,
    Classification,
    ClassificationContext,
    Classifier,
    ClosedLoopRunRequest,
    ClosedLoopRunResult,
    DependencyFailure,
    EligibleCandidate,
    EvidenceItem,
    Materialization,
    MaterializationContext,
    Materializer,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    Requirement,
    Selection,
    SelectionContext,
    Selector,
    Verification,
    VerificationSummary,
    Verifier,
)
from .protocol import (
    AttemptSnapshot,
    BackendConsideration,
    BudgetSnapshot,
    CandidateRanking,
    ClassificationSnapshot,
    EventEnvelope,
    Evidence,
    FailureDetail,
    MaterializationSnapshot,
    PolicyDecisionSnapshot,
    RequirementResult,
    RunArtifactPaths,
    RunManifestSnapshot,
    RunStateSnapshot,
    SelectionSnapshot,
    TaskSnapshot,
    VerificationSnapshot,
)
from .run_store import RunStore, RunStoreError, json_safe_copy
from .state_machine import ClosedLoopStateMachine, TERMINAL_STATES


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _mapping_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    copied = json_safe_copy(dict(value))
    if not isinstance(copied, dict):  # pragma: no cover - defensive invariant
        raise TypeError("metadata must be a JSON object")
    return copied


def _read_only_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(_mapping_copy(value))


def _failure_detail(
    failure: DependencyFailure | None,
    *,
    fallback_code: str,
    fallback_message: str,
) -> FailureDetail:
    if failure is None:
        return FailureDetail(
            code=fallback_code,
            message=redact_message(fallback_message),
            details={},
        )
    return FailureDetail(
        code=failure.code,
        message=redact_message(failure.message),
        details=_mapping_copy(failure.details),
    )


@dataclass(slots=True)
class _Runtime:
    request: ClosedLoopRunRequest
    run_id: str
    trace_id: str
    task_id: str
    created_at: datetime
    started_monotonic: float
    store: RunStore
    events: EventWriter
    machine: ClosedLoopStateMachine = field(default_factory=ClosedLoopStateMachine)
    last_event: EventEnvelope | None = None
    previous_state: str | None = None
    active_attempt_id: str | None = None
    classification: ClassificationSnapshot | None = None
    policy_decision_count: int = 0
    attempts: list[AttemptSnapshot] = field(default_factory=list)
    attempt_results: dict[str, AttemptResult] = field(default_factory=dict)
    attempt_contexts: dict[str, AttemptContext] = field(default_factory=dict)
    attempt_start_events: dict[str, str] = field(default_factory=dict)
    attempt_patches: dict[str, str] = field(default_factory=dict)
    verifications: list[VerificationSnapshot] = field(default_factory=list)
    eligible_candidate_ids: list[str] = field(default_factory=list)
    selected_attempt_id: str | None = None
    failure: FailureDetail | None = None
    terminal_reason: str | None = None


class ClosedLoopController:
    """Run the canonical controller state machine using injected dependencies."""

    def __init__(
        self,
        *,
        classifier: Classifier,
        policy_engine: PolicyEngine,
        attempt_runner: AttemptRunner,
        verifier: Verifier,
        selector: Selector,
        materializer: Materializer,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._classifier = classifier
        self._policy_engine = policy_engine
        self._attempt_runner = attempt_runner
        self._verifier = verifier
        self._selector = selector
        self._materializer = materializer
        self._now = now or _utc_now
        self._monotonic = monotonic or time.monotonic
        self._id_factory = id_factory or _default_id

    def run(self, request: ClosedLoopRunRequest) -> ClosedLoopRunResult:
        """Execute one run to a canonical terminal state and return its summary."""

        run_id = self._id_factory("run")
        trace_id = self._id_factory("trace")
        task_id = self._id_factory("task")
        created_at = self._now()
        store = RunStore(request.runs_root, run_id)
        runtime: _Runtime | None = None
        try:
            store.create()
            events = EventWriter(store, trace_id, self._now)
            runtime = _Runtime(
                request=request,
                run_id=run_id,
                trace_id=trace_id,
                task_id=task_id,
                created_at=created_at,
                started_monotonic=self._monotonic(),
                store=store,
                events=events,
            )
            self._initialize_bundle(runtime)
            if not self._classify(runtime):
                return self._result(runtime)

            while not runtime.machine.terminal:
                decision = self._ask_policy(runtime)
                if decision is None:
                    break
                action, attempt_id = decision

                if action.action == "fail":
                    self._fail(runtime, "policy_failed", action.reason)
                    break
                if action.action == "exhaust":
                    if runtime.eligible_candidate_ids:
                        self._select_and_materialize(runtime)
                    else:
                        self._exhaust(runtime, action.reason)
                    break
                if action.action == "select":
                    self._select_and_materialize(runtime)
                    break

                assert attempt_id is not None
                budget_reason = self._attempt_budget_block(runtime, action)
                if budget_reason is not None:
                    if runtime.eligible_candidate_ids:
                        self._select_and_materialize(runtime)
                    else:
                        self._exhaust(runtime, budget_reason)
                    break
                self._run_attempt(runtime, action, attempt_id)

            return self._result(runtime)
        except Exception as error:
            if runtime is not None and not runtime.machine.terminal:
                try:
                    self._emit_failure_event(
                        runtime, "controller_failed", error, "controller"
                    )
                    self._fail(
                        runtime,
                        "controller_failure",
                        redact_message(str(error)),
                        error=error,
                    )
                except Exception:
                    # An unrecoverable store failure may make terminal persistence
                    # impossible; no traceback or unredacted message escapes here.
                    runtime.terminal_reason = redact_message(str(error))
            if runtime is not None:
                return self._result(runtime, forced_state="FAILED")
            return ClosedLoopRunResult(
                run_id=run_id,
                terminal_state="FAILED",
                selected_attempt_id=None,
                run_directory=store.run_directory,
                actual_known_cost_usd=None,
                accounting_status="unknown",
                failure_or_exhaustion_reason=redact_message(str(error)),
            )

    def _initialize_bundle(self, runtime: _Runtime) -> None:
        event = runtime.events.emit(
            "run_created",
            {
                "task_id": runtime.task_id,
                "max_attempts": runtime.request.max_attempts,
            },
        )
        runtime.last_event = event
        task = TaskSnapshot(
            schema_version="villani.task.v1",
            task_id=runtime.task_id,
            run_id=runtime.run_id,
            created_at=runtime.created_at,
            repository_path=str(runtime.request.repository_path),
            instruction=runtime.request.task,
            success_criteria=runtime.request.success_criteria,
            constraints=[],
            requires_file_changes=runtime.request.requires_file_changes,
            metadata={},
        )
        runtime.store.write_protocol("task.json", task)
        self._persist_state(runtime)
        self._persist_manifest(runtime)

    def _classify(self, runtime: _Runtime) -> bool:
        self._transition(
            runtime,
            "CLASSIFYING",
            "classification_started",
            {"task_id": runtime.task_id},
        )
        context = ClassificationContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            task_id=runtime.task_id,
            repository_path=str(runtime.request.repository_path),
            success_criteria=runtime.request.success_criteria,
            requires_file_changes=runtime.request.requires_file_changes,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
        )
        try:
            returned = self._classifier.classify(runtime.request.task, context)
            if not isinstance(returned, Classification):
                raise TypeError("classifier returned an invalid Classification")
            classification = ClassificationSnapshot(
                schema_version="villani.classification.v1",
                classification_id="classification_001",
                run_id=runtime.run_id,
                task_id=runtime.task_id,
                classified_at=self._now(),
                difficulty=returned.difficulty,
                risk=returned.risk,
                category=returned.category,
                required_capabilities=list(returned.required_capabilities),
                estimated_attempts_needed=returned.estimated_attempts_needed,
                needs_tests=returned.needs_tests,
                confidence=returned.confidence,
                reasoning_summary=returned.reasoning_summary,
                signals=_mapping_copy(returned.signals),
                metadata=_mapping_copy(returned.metadata),
            )
            runtime.store.write_protocol("classification.json", classification)
            runtime.classification = classification
        except Exception as error:
            self._emit_failure_event(
                runtime, "classification_failed", error, "classification"
            )
            self._fail(
                runtime,
                "classification_failure",
                redact_message(str(error)),
                error=error,
            )
            return False

        self._transition(
            runtime,
            "CLASSIFIED",
            "classification_completed",
            {"classification_id": classification.classification_id},
        )
        return True

    def _ask_policy(
        self, runtime: _Runtime
    ) -> tuple[PolicyDecision, str | None] | None:
        assert runtime.classification is not None
        self._emit_state_event(
            runtime,
            "policy_decision_started",
            {"decision_sequence": runtime.policy_decision_count + 1},
        )
        budget_before = self._budget_context(runtime)
        context = PolicyContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            state=runtime.machine.state,
            classification=runtime.classification.model_copy(deep=True),
            attempts=tuple(
                AttemptSummary(
                    attempt_id=attempt.attempt_id,
                    backend_name=attempt.backend_name,
                    exit_code=attempt.exit_code,
                    status=attempt.status,
                    cost_usd=attempt.cost_usd,
                    cost_accounting_status=attempt.cost_accounting_status,
                )
                for attempt in runtime.attempts
            ),
            verifications=tuple(
                VerificationSummary(
                    attempt_id=verification.attempt_id,
                    outcome=verification.outcome,
                    acceptance_eligible=verification.acceptance_eligible,
                    recommended_action=verification.recommended_action,
                )
                for verification in runtime.verifications
            ),
            eligible_candidate_ids=tuple(runtime.eligible_candidate_ids),
            budget=budget_before,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
        )
        try:
            returned = self._policy_engine.decide(context)
            if not isinstance(returned, PolicyDecision):
                raise TypeError("policy engine returned an invalid PolicyDecision")
            self._validate_policy_semantics(runtime, returned)
            attempt_id = (
                f"attempt_{len(runtime.attempts) + 1:03d}"
                if returned.action in {"attempt", "retry", "escalate"}
                else None
            )
            runtime.policy_decision_count += 1
            snapshot = self._policy_snapshot(
                runtime, returned, attempt_id, budget_before
            )
            runtime.store.append_policy_decision(snapshot)
        except Exception as error:
            self._emit_failure_event(
                runtime, "policy_selection_failed", error, "policy_selection"
            )
            self._fail(
                runtime,
                "illegal_policy_output",
                redact_message(str(error)),
                error=error,
            )
            return None

        self._record_policy_state(runtime, returned, snapshot)
        return returned, attempt_id

    def _validate_policy_semantics(
        self, runtime: _Runtime, decision: PolicyDecision
    ) -> None:
        current = runtime.machine.state
        if current == "CLASSIFIED" and decision.action in {"retry", "escalate"}:
            raise ValueError(
                f"policy action {decision.action} requires a previous attempt"
            )
        if current not in {"CLASSIFIED", "REJECTED", "VERIFIED"}:
            raise ValueError(f"policy cannot decide from state {current}")
        if decision.action in {"attempt", "retry", "escalate"}:
            if not decision.chosen_backend:
                raise ValueError("attempt policy action requires chosen_backend")
            matching = [
                item
                for item in decision.considered_backends
                if item.backend_name == decision.chosen_backend
            ]
            if not matching or not any(item.eligible for item in matching):
                raise ValueError("chosen backend must be an eligible consideration")
        if decision.action == "select" and not runtime.eligible_candidate_ids:
            raise ValueError("policy cannot select without an eligible candidate")

    def _policy_snapshot(
        self,
        runtime: _Runtime,
        decision: PolicyDecision,
        attempt_id: str | None,
        budget_before: BudgetContext,
    ) -> PolicyDecisionSnapshot:
        return PolicyDecisionSnapshot(
            schema_version="villani.policy_decision.v1",
            decision_id=f"decision_{runtime.policy_decision_count:03d}",
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            timestamp=self._now(),
            decision_sequence=runtime.policy_decision_count,
            classification_id=runtime.classification.classification_id,
            policy_version=decision.policy_version,
            action=decision.action,
            reason=decision.reason,
            considered_backends=[
                BackendConsideration(
                    backend_name=item.backend_name,
                    model=item.model,
                    eligible=item.eligible,
                    capability_score=item.capability_score,
                    estimated_cost_usd=item.estimated_cost_usd,
                    cost_accounting_status=item.cost_accounting_status,
                    rejection_reasons=list(item.rejection_reasons),
                )
                for item in decision.considered_backends
            ],
            chosen_backend=decision.chosen_backend,
            chosen_model=decision.chosen_model,
            attempt_id=attempt_id,
            budget_before=self._budget_snapshot(budget_before),
            budget_after=self._budget_snapshot(
                self._budget_after_decision(budget_before, decision)
            ),
            metadata=_mapping_copy(decision.metadata),
        )

    def _record_policy_state(
        self,
        runtime: _Runtime,
        decision: PolicyDecision,
        snapshot: PolicyDecisionSnapshot,
    ) -> None:
        payload = {
            "decision_id": snapshot.decision_id,
            "action": decision.action,
            "reason": decision.reason,
            "chosen_backend": decision.chosen_backend,
        }
        current = runtime.machine.state
        if decision.action in {"exhaust", "fail"}:
            self._emit_state_event(runtime, "policy_selected", payload)
            return
        if current == "VERIFIED":
            if decision.action == "select":
                self._emit_state_event(runtime, "policy_selected", payload)
                return
            self._transition(
                runtime,
                "REJECTED",
                "candidate_collection_continued",
                payload,
                attempt_id=runtime.active_attempt_id,
            )
            current = "REJECTED"
        if current == "REJECTED" and decision.action == "escalate":
            self._transition(
                runtime,
                "ESCALATING",
                "escalation_selected",
                payload,
                attempt_id=runtime.active_attempt_id,
            )
            self._transition(runtime, "POLICY_SELECTED", "policy_selected", payload)
            return
        if current == "REJECTED" and decision.action in {"retry", "attempt"}:
            self._emit_state_event(
                runtime,
                "retry_selected",
                payload,
                attempt_id=runtime.active_attempt_id,
            )
        self._transition(runtime, "POLICY_SELECTED", "policy_selected", payload)

    def _run_attempt(
        self, runtime: _Runtime, decision: PolicyDecision, attempt_id: str
    ) -> None:
        ordinal = len(runtime.attempts) + 1
        context = AttemptContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            task_id=runtime.task_id,
            attempt_id=attempt_id,
            ordinal=ordinal,
            task=runtime.request.task,
            repository_path=str(runtime.request.repository_path),
            success_criteria=runtime.request.success_criteria,
            requires_file_changes=runtime.request.requires_file_changes,
            backend_name=decision.chosen_backend or "",
            model=decision.chosen_model,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
            run_directory=runtime.store.run_directory,
            attempt_directory=(
                runtime.store.run_directory / "attempts" / attempt_id
            ),
        )
        runtime.active_attempt_id = attempt_id
        runtime.attempt_contexts[attempt_id] = context
        started = self._transition(
            runtime,
            "ATTEMPT_RUNNING",
            "attempt_started",
            {
                "ordinal": ordinal,
                "backend_name": context.backend_name,
                "model": context.model,
            },
            attempt_id=attempt_id,
        )
        runtime.attempt_start_events[attempt_id] = started.event_id
        try:
            returned = self._attempt_runner.run(context)
            if not isinstance(returned, AttemptResult):
                raise TypeError("attempt runner returned an invalid AttemptResult")
            snapshot = self._persist_attempt(
                runtime, context, returned, started.timestamp, self._now()
            )
            runtime.attempt_results[attempt_id] = returned
        except Exception as error:
            snapshot = self._persist_synthetic_failed_attempt(
                runtime, context, started.timestamp, error
            )
            self._transition(
                runtime,
                "ATTEMPT_COMPLETED",
                "attempt_failed",
                failure_payload(error, operation="attempt_runner"),
                attempt_id=attempt_id,
                parent_event_id=started.event_id,
            )
            self._fail(
                runtime,
                "attempt_dependency_failure",
                redact_message(str(error)),
                error=error,
            )
            return

        completion_type = (
            "attempt_completed"
            if snapshot.status == "completed" and snapshot.exit_code == 0
            else "attempt_failed"
        )
        self._transition(
            runtime,
            "ATTEMPT_COMPLETED",
            completion_type,
            {"status": snapshot.status, "exit_code": snapshot.exit_code},
            attempt_id=attempt_id,
            parent_event_id=started.event_id,
        )
        self._emit_runtime_events(runtime, returned, started.event_id)
        self._emit_state_event(
            runtime,
            "patch_captured",
            {
                "patch_bytes": snapshot.patch_bytes,
                "patch_sha256": snapshot.patch_sha256,
            },
            attempt_id=attempt_id,
            parent_event_id=started.event_id,
        )

        patch = runtime.attempt_patches.get(attempt_id, "")
        if runtime.request.requires_file_changes and not patch.strip():
            normalized = self._empty_patch_verification(runtime, attempt_id)
            runtime.store.write_protocol(
                f"verification/{attempt_id}.json", normalized
            )
            runtime.verifications.append(normalized)
            self._write_evidence_matrix(runtime)
            self._transition(
                runtime,
                "REJECTED",
                "verification_completed",
                {
                    "outcome": "rejected",
                    "acceptance_eligible": False,
                    "normalization": "empty_patch",
                },
                attempt_id=attempt_id,
                parent_event_id=started.event_id,
            )
            return

        self._verify_attempt(runtime, context, returned, started.event_id)

    def _persist_attempt(
        self,
        runtime: _Runtime,
        context: AttemptContext,
        result: AttemptResult,
        started_at: datetime,
        completed_at: datetime,
    ) -> AttemptSnapshot:
        base = f"attempts/{context.attempt_id}"
        worktree = {"path": result.worktree_path, "isolated": True}
        returned_worktree = result.metadata.get("worktree")
        if isinstance(returned_worktree, Mapping):
            worktree.update(_mapping_copy(returned_worktree))
        runtime.store.write_json(f"{base}/worktree.json", worktree)
        runtime.store.write_text(f"{base}/stdout.log", result.stdout)
        runtime.store.write_text(f"{base}/stderr.log", result.stderr)
        runtime.store.write_json(
            f"{base}/runner_telemetry.json", _mapping_copy(result.runner_telemetry)
        )
        runtime.store.write_json(
            f"{base}/trace/runtime.json", _mapping_copy(result.trace)
        )

        patch_path: str | None = None
        patch_sha256: str | None = None
        patch_bytes: int | None = None
        if result.patch is not None:
            patch_path = f"{base}/patch.diff"
            encoded = result.patch.encode("utf-8")
            patch_sha256 = hashlib.sha256(encoded).hexdigest()
            patch_bytes = len(encoded)
            runtime.store.write_text(patch_path, result.patch)
            runtime.attempt_patches[context.attempt_id] = result.patch

        snapshot = AttemptSnapshot(
            schema_version="villani.attempt.v1",
            attempt_id=context.attempt_id,
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            ordinal=context.ordinal,
            backend_name=context.backend_name,
            runner_name=result.runner_name,
            model=result.model if result.model is not None else context.model,
            status=result.status,
            started_at=started_at,
            completed_at=completed_at,
            worktree_path=result.worktree_path,
            patch_path=patch_path,
            patch_sha256=patch_sha256,
            patch_bytes=patch_bytes,
            stdout_path=f"{base}/stdout.log",
            stderr_path=f"{base}/stderr.log",
            runner_telemetry_path=(
                result.telemetry_path or f"{base}/runner_telemetry.json"
            ),
            trace_path=result.trace_path or f"{base}/trace/runtime.json",
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            duration_accounting_status=result.duration_accounting_status,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            token_accounting_status=result.token_accounting_status,
            cost_usd=result.cost_usd,
            cost_accounting_status=result.cost_accounting_status,
            error=(
                _failure_detail(
                    result.error,
                    fallback_code="runner_failure",
                    fallback_message="runner failed",
                )
                if result.error is not None
                else None
            ),
            metadata=_mapping_copy(result.metadata),
        )
        runtime.store.write_protocol(f"{base}/attempt.json", snapshot)
        runtime.attempts.append(snapshot)
        self._persist_manifest(runtime)
        return snapshot

    def _emit_runtime_events(
        self,
        runtime: _Runtime,
        result: AttemptResult,
        parent_event_id: str,
    ) -> None:
        for translated in result.runtime_events:
            payload = _mapping_copy(translated.payload)
            if translated.source_event_id:
                payload.setdefault("source_event_id", translated.source_event_id)
            event = runtime.store.append_event(
                timestamp=translated.timestamp,
                trace_id=runtime.trace_id,
                attempt_id=runtime.active_attempt_id,
                parent_event_id=parent_event_id,
                source="villani_code",
                event_type=translated.event_type,
                payload=payload,
            )
            runtime.last_event = event
        if result.runtime_events:
            self._persist_state(runtime)

    def _persist_synthetic_failed_attempt(
        self,
        runtime: _Runtime,
        context: AttemptContext,
        started_at: datetime,
        error: Exception,
    ) -> AttemptSnapshot:
        result = AttemptResult(
            runner_name="unavailable",
            status="failed",
            worktree_path="unavailable",
            patch=None,
            exit_code=None,
            error=DependencyFailure(
                code="runner_exception",
                message=redact_message(str(error)),
                details={"exception_class": error.__class__.__name__},
            ),
        )
        return self._persist_attempt(
            runtime, context, result, started_at, self._now()
        )

    def _verify_attempt(
        self,
        runtime: _Runtime,
        context: AttemptContext,
        result: AttemptResult,
        attempt_start_event_id: str,
    ) -> None:
        self._transition(
            runtime,
            "VERIFYING",
            "verification_started",
            {"attempt_id": context.attempt_id},
            attempt_id=context.attempt_id,
            parent_event_id=attempt_start_event_id,
        )
        try:
            returned = self._verifier.verify(context, result)
        except Exception as error:
            normalized = self._verifier_error_snapshot(
                runtime, context.attempt_id, error
            )
            runtime.store.write_protocol(
                f"verification/{context.attempt_id}.json", normalized
            )
            runtime.verifications.append(normalized)
            self._write_evidence_matrix(runtime)
            self._transition(
                runtime,
                "VERIFIED",
                "verification_failed",
                failure_payload(error, operation="verification"),
                attempt_id=context.attempt_id,
                parent_event_id=attempt_start_event_id,
            )
        else:
            try:
                if not isinstance(returned, Verification):
                    raise TypeError("verifier returned an invalid Verification")
                normalized = self._normalize_verification(
                    runtime, context.attempt_id, returned
                )
                runtime.store.write_protocol(
                    f"verification/{context.attempt_id}.json", normalized
                )
                runtime.verifications.append(normalized)
                self._write_evidence_matrix(runtime)
                self._transition(
                    runtime,
                    "VERIFIED",
                    "verification_completed",
                    {
                        "outcome": normalized.outcome,
                        "acceptance_eligible": normalized.acceptance_eligible,
                    },
                    attempt_id=context.attempt_id,
                    parent_event_id=attempt_start_event_id,
                )
            except Exception as error:
                self._emit_failure_event(
                    runtime,
                    "verification_failed",
                    error,
                    "verification_output",
                    attempt_id=context.attempt_id,
                    parent_event_id=attempt_start_event_id,
                )
                self._fail(
                    runtime,
                    "illegal_verifier_output",
                    redact_message(str(error)),
                    error=error,
                )
                return

        if runtime.machine.terminal:
            return
        if normalized.acceptance_eligible:
            runtime.eligible_candidate_ids.append(context.attempt_id)
            self._persist_state(runtime)
            self._persist_manifest(runtime)
        else:
            self._transition(
                runtime,
                "REJECTED",
                "candidate_rejected",
                {
                    "outcome": normalized.outcome,
                    "reason": normalized.reason,
                },
                attempt_id=context.attempt_id,
                parent_event_id=attempt_start_event_id,
            )

    def _normalize_verification(
        self, runtime: _Runtime, attempt_id: str, returned: Verification
    ) -> VerificationSnapshot:
        requirements_ok = bool(returned.requirement_results) and all(
            item.outcome in {"passed", "not_applicable"}
            for item in returned.requirement_results
        )
        blockers_absent = not any(
            "blocker" in flag.lower() for flag in returned.risk_flags
        )
        eligible = bool(
            returned.acceptance_eligible
            and returned.outcome == "accepted"
            and returned.recommended_action == "accept"
            and requirements_ok
            and returned.success_evidence
            and not returned.missing_evidence
            and blockers_absent
            and (
                not runtime.request.requires_file_changes
                or bool(runtime.attempt_patches.get(attempt_id, "").strip())
            )
        )
        return VerificationSnapshot(
            schema_version="villani.verification.v1",
            run_id=runtime.run_id,
            attempt_id=attempt_id,
            verified_at=self._now(),
            verifier=returned.verifier,
            outcome=returned.outcome,
            acceptance_eligible=eligible,
            confidence=returned.confidence,
            reason=returned.reason,
            requirement_results=[
                self._requirement_result(item)
                for item in returned.requirement_results
            ],
            success_evidence=[
                self._evidence(item) for item in returned.success_evidence
            ],
            failure_evidence=[
                self._evidence(item) for item in returned.failure_evidence
            ],
            missing_evidence=[
                self._evidence(item) for item in returned.missing_evidence
            ],
            risk_flags=list(returned.risk_flags),
            recommended_action=returned.recommended_action,
            raw_verifier_artifact=returned.raw_verifier_artifact,
            metadata=_mapping_copy(returned.metadata),
        )

    def _requirement_result(self, item: Requirement) -> RequirementResult:
        return RequirementResult(
            requirement_id=item.requirement_id,
            description=item.description,
            outcome=item.outcome,
            evidence_ids=list(item.evidence_ids),
        )

    def _evidence(self, item: EvidenceItem) -> Evidence:
        return Evidence(
            evidence_id=item.evidence_id,
            kind=item.kind,
            summary=item.summary,
            artifact_path=item.artifact_path,
            **_mapping_copy(item.details),
        )

    def _empty_patch_verification(
        self, runtime: _Runtime, attempt_id: str
    ) -> VerificationSnapshot:
        return VerificationSnapshot(
            schema_version="villani.verification.v1",
            run_id=runtime.run_id,
            attempt_id=attempt_id,
            verified_at=self._now(),
            verifier="controller_normalizer",
            outcome="rejected",
            acceptance_eligible=False,
            confidence=1.0,
            reason="Candidate has no non-empty patch for a file-changing task.",
            requirement_results=[
                RequirementResult(
                    requirement_id="file_change",
                    description="A non-empty candidate patch is required.",
                    outcome="failed",
                    evidence_ids=["empty_patch"],
                )
            ],
            success_evidence=[],
            failure_evidence=[
                Evidence(
                    evidence_id="empty_patch",
                    kind="patch",
                    summary="The runner returned no non-empty patch.",
                    artifact_path=None,
                )
            ],
            missing_evidence=[],
            risk_flags=["acceptance_blocker:empty_patch"],
            recommended_action="reject",
            raw_verifier_artifact=None,
            metadata={"normalized_without_verifier": True},
        )

    def _verifier_error_snapshot(
        self, runtime: _Runtime, attempt_id: str, error: Exception
    ) -> VerificationSnapshot:
        return VerificationSnapshot(
            schema_version="villani.verification.v1",
            run_id=runtime.run_id,
            attempt_id=attempt_id,
            verified_at=self._now(),
            verifier="dependency_error",
            outcome="error",
            acceptance_eligible=False,
            confidence=None,
            reason="Verifier failed; the candidate is not acceptance eligible.",
            requirement_results=[],
            success_evidence=[],
            failure_evidence=[],
            missing_evidence=[
                Evidence(
                    evidence_id="verifier_error",
                    kind="verifier_error",
                    summary=redact_message(str(error)),
                    artifact_path=None,
                )
            ],
            risk_flags=["acceptance_blocker:verifier_error"],
            recommended_action="retry_verifier",
            raw_verifier_artifact=None,
            metadata={"exception_class": error.__class__.__name__},
        )

    def _select_and_materialize(self, runtime: _Runtime) -> None:
        if not runtime.eligible_candidate_ids:
            self._transition_to_selecting(runtime)
            self._exhaust(runtime, "no acceptance-eligible candidate exists")
            return
        self._transition_to_selecting(runtime)
        candidates = self._eligible_candidates(runtime)
        context = SelectionContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            task=runtime.request.task,
            repository_path=str(runtime.request.repository_path),
            success_criteria=runtime.request.success_criteria,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
            run_directory=runtime.store.run_directory,
        )
        self._emit_state_event(
            runtime,
            "selection_dependency_started",
            {"eligible_candidate_ids": list(runtime.eligible_candidate_ids)},
        )
        try:
            returned = self._selector.select(candidates, context)
            if not isinstance(returned, Selection):
                raise TypeError("selector returned an invalid Selection")
            if returned.selected_attempt_id not in runtime.eligible_candidate_ids:
                raise ValueError(
                    "selector selected a candidate that was not acceptance eligible"
                )
            selection = self._selection_snapshot(runtime, returned)
            runtime.store.write_protocol("selection.json", selection)
            runtime.store.write_text("selection_report.md", returned.report)
            runtime.selected_attempt_id = returned.selected_attempt_id
            self._emit_state_event(
                runtime,
                "candidate_selected",
                {
                    "selection_id": selection.selection_id,
                    "attempt_id": returned.selected_attempt_id,
                },
            )
        except Exception as error:
            self._emit_failure_event(
                runtime, "selection_failed", error, "selection"
            )
            self._fail(
                runtime,
                "selector_violation",
                redact_message(str(error)),
                error=error,
            )
            return

        selected_candidate = next(
            candidate
            for candidate in candidates
            if candidate.attempt.attempt_id == runtime.selected_attempt_id
        )
        materialization_started = self._transition(
            runtime,
            "MATERIALIZING",
            "materialization_started",
            {
                "selection_id": selection.selection_id,
                "selected_attempt_id": runtime.selected_attempt_id,
            },
        )
        materialization_context = MaterializationContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            repository_path=str(runtime.request.repository_path),
            selected_candidate=selected_candidate,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
            run_directory=runtime.store.run_directory,
        )
        try:
            returned_materialization = self._materializer.materialize(
                returned, materialization_context
            )
            if not isinstance(returned_materialization, Materialization):
                raise TypeError(
                    "materializer returned an invalid Materialization"
                )
            materialization = self._persist_materialization(
                runtime,
                selection,
                selected_candidate,
                returned_materialization,
                materialization_started.timestamp,
            )
        except Exception as error:
            self._emit_failure_event(
                runtime,
                "materialization_failed",
                error,
                "materialization",
            )
            self._fail(
                runtime,
                "materialization_failure",
                redact_message(str(error)),
                error=error,
            )
            return

        if materialization.status != "succeeded":
            message = (
                materialization.failure.message
                if materialization.failure is not None
                else "materialization reported failure"
            )
            self._emit_state_event(
                runtime,
                "materialization_failed",
                {"message": redact_message(message)},
            )
            self._fail(runtime, "materialization_failure", message)
            return

        self._emit_state_event(
            runtime,
            "materialization_completed",
            {"materialization_id": materialization.materialization_id},
        )
        self._transition(
            runtime,
            "COMPLETED",
            "run_completed",
            {"selected_attempt_id": runtime.selected_attempt_id},
        )

    def _transition_to_selecting(self, runtime: _Runtime) -> None:
        if runtime.machine.state == "POLICY_SELECTED":
            self._transition(
                runtime,
                "SELECTING",
                "selection_started",
                {"eligible_candidate_ids": list(runtime.eligible_candidate_ids)},
            )
        elif runtime.machine.state == "VERIFIED":
            self._transition(
                runtime,
                "SELECTING",
                "selection_started",
                {"eligible_candidate_ids": list(runtime.eligible_candidate_ids)},
                attempt_id=runtime.active_attempt_id,
            )
        else:
            raise RuntimeError(
                f"selection cannot start from {runtime.machine.state}"
            )

    def _eligible_candidates(
        self, runtime: _Runtime
    ) -> tuple[EligibleCandidate, ...]:
        candidates: list[EligibleCandidate] = []
        for attempt_id in runtime.eligible_candidate_ids:
            attempt = next(
                item for item in runtime.attempts if item.attempt_id == attempt_id
            )
            verification = next(
                item
                for item in runtime.verifications
                if item.attempt_id == attempt_id
            )
            candidates.append(
                EligibleCandidate(
                    attempt=attempt.model_copy(deep=True),
                    verification=verification.model_copy(deep=True),
                    patch=runtime.attempt_patches.get(attempt_id, ""),
                )
            )
        return tuple(candidates)

    def _selection_snapshot(
        self, runtime: _Runtime, returned: Selection
    ) -> SelectionSnapshot:
        return SelectionSnapshot(
            schema_version="villani.selection.v1",
            selection_id="selection_001",
            run_id=runtime.run_id,
            selected_at=self._now(),
            strategy=returned.strategy,
            eligible_candidate_ids=list(runtime.eligible_candidate_ids),
            selected_candidate_ids=(
                [returned.selected_attempt_id]
                if returned.selected_attempt_id is not None
                else []
            ),
            rankings=[
                CandidateRanking(
                    attempt_id=item.attempt_id,
                    rank=item.rank,
                    reason=item.reason,
                    actual_cost_usd=item.actual_cost_usd,
                    cost_accounting_status=item.cost_accounting_status,
                    evidence=_mapping_copy(item.evidence),
                )
                for item in returned.rankings
            ],
            reason=returned.reason,
            advisory_comparison=(
                _mapping_copy(returned.advisory_comparison)
                if returned.advisory_comparison is not None
                else None
            ),
            metadata=_mapping_copy(returned.metadata),
        )

    def _persist_materialization(
        self,
        runtime: _Runtime,
        selection: SelectionSnapshot,
        candidate: EligibleCandidate,
        returned: Materialization,
        started_at: datetime,
    ) -> MaterializationSnapshot:
        failure: FailureDetail | None = None
        materialized_patch_path: str | None = None
        patch_sha256: str | None = None
        if returned.status == "succeeded":
            if returned.final_patch is None:
                raise ValueError("successful materialization returned no final patch")
            if returned.final_patch != candidate.patch:
                raise ValueError(
                    "materializer output differs from the selected recorded patch"
                )
            runtime.store.write_text("final.patch", returned.final_patch)
            runtime.store.write_text("final_report.md", returned.final_report)
            materialized_patch_path = "final.patch"
            patch_sha256 = hashlib.sha256(
                returned.final_patch.encode("utf-8")
            ).hexdigest()
        else:
            failure = _failure_detail(
                returned.failure,
                fallback_code="materialization_failed",
                fallback_message="materialization reported failure",
            )

        source_patch_path = (
            candidate.attempt.patch_path
            or f"attempts/{candidate.attempt.attempt_id}/patch.diff"
        )
        snapshot = MaterializationSnapshot(
            schema_version="villani.materialization.v1",
            materialization_id="materialization_001",
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            selection_id=selection.selection_id,
            selected_attempt_id=candidate.attempt.attempt_id,
            started_at=started_at,
            completed_at=self._now(),
            status=returned.status,
            source_patch_path=source_patch_path,
            target_repository_path=str(runtime.request.repository_path),
            materialized_patch_path=materialized_patch_path,
            patch_sha256=patch_sha256,
            changed_files=list(returned.changed_files),
            failure=failure,
            metadata=_mapping_copy(returned.metadata),
        )
        runtime.store.write_protocol("materialization.json", snapshot)
        return snapshot

    def _write_evidence_matrix(self, runtime: _Runtime) -> None:
        runtime.store.write_json(
            "candidate_evidence_matrix.json",
            {
                "run_id": runtime.run_id,
                "candidates": [
                    {
                        "attempt_id": item.attempt_id,
                        "outcome": item.outcome,
                        "acceptance_eligible": item.acceptance_eligible,
                        "success_evidence_ids": [
                            evidence.evidence_id
                            for evidence in item.success_evidence
                        ],
                        "risk_flags": list(item.risk_flags),
                    }
                    for item in runtime.verifications
                ],
            },
        )

    def _budget_context(self, runtime: _Runtime) -> BudgetContext:
        remaining_attempts = max(
            runtime.request.max_attempts - len(runtime.attempts), 0
        )
        if runtime.request.max_cost is None:
            remaining_cost = None
            cost_status = "not_applicable"
        elif not runtime.attempts:
            remaining_cost = runtime.request.max_cost
            cost_status = "complete"
        else:
            known_cost, accounting = self._actual_cost(runtime)
            if accounting == "complete" and known_cost is not None:
                remaining_cost = max(runtime.request.max_cost - known_cost, 0.0)
                cost_status = "complete"
            else:
                remaining_cost = None
                cost_status = "unknown"

        if runtime.request.max_wall_time is None:
            remaining_wall_time_ms = None
            duration_status = "not_applicable"
        else:
            elapsed = max(
                int((self._monotonic() - runtime.started_monotonic) * 1000), 0
            )
            remaining_wall_time_ms = max(
                int(runtime.request.max_wall_time * 1000) - elapsed, 0
            )
            duration_status = "complete"
        return BudgetContext(
            remaining_attempts=remaining_attempts,
            remaining_cost_usd=remaining_cost,
            cost_accounting_status=cost_status,
            remaining_wall_time_ms=remaining_wall_time_ms,
            duration_accounting_status=duration_status,
        )

    def _budget_after_decision(
        self, before: BudgetContext, decision: PolicyDecision
    ) -> BudgetContext:
        if decision.action not in {"attempt", "retry", "escalate"}:
            return before
        remaining_cost = before.remaining_cost_usd
        cost_status = before.cost_accounting_status
        option = self._chosen_backend_option(decision)
        if before.cost_accounting_status == "complete":
            if option is None or option.estimated_cost_usd is None:
                remaining_cost = None
                cost_status = "unknown"
            else:
                remaining_cost = max(
                    (before.remaining_cost_usd or 0.0)
                    - option.estimated_cost_usd,
                    0.0,
                )
        return BudgetContext(
            remaining_attempts=max(before.remaining_attempts - 1, 0),
            remaining_cost_usd=remaining_cost,
            cost_accounting_status=cost_status,
            remaining_wall_time_ms=before.remaining_wall_time_ms,
            duration_accounting_status=before.duration_accounting_status,
        )

    def _budget_snapshot(self, budget: BudgetContext) -> BudgetSnapshot:
        return BudgetSnapshot(
            remaining_attempts=budget.remaining_attempts,
            remaining_cost_usd=budget.remaining_cost_usd,
            cost_accounting_status=budget.cost_accounting_status,
            remaining_wall_time_ms=budget.remaining_wall_time_ms,
            duration_accounting_status=budget.duration_accounting_status,
        )

    def _attempt_budget_block(
        self, runtime: _Runtime, decision: PolicyDecision
    ) -> str | None:
        budget = self._budget_context(runtime)
        if budget.remaining_attempts <= 0:
            return "attempt budget exhausted"
        if (
            budget.remaining_wall_time_ms is not None
            and budget.remaining_wall_time_ms <= 0
        ):
            return "wall-time budget exhausted"
        if runtime.request.max_cost is not None:
            if budget.cost_accounting_status != "complete":
                return "cost budget cannot permit another attempt with unknown spend"
            option = self._chosen_backend_option(decision)
            if (
                option is None
                or option.cost_accounting_status != "complete"
                or option.estimated_cost_usd is None
            ):
                return "cost budget cannot permit an attempt with unknown estimated cost"
            if option.estimated_cost_usd > (budget.remaining_cost_usd or 0.0):
                return "cost budget exhausted before unaffordable attempt"
        return None

    def _chosen_backend_option(
        self, decision: PolicyDecision
    ) -> BackendOption | None:
        return next(
            (
                item
                for item in decision.considered_backends
                if item.backend_name == decision.chosen_backend
            ),
            None,
        )

    def _actual_cost(self, runtime: _Runtime) -> tuple[float | None, str]:
        if not runtime.attempts:
            return None, "unknown"
        known = [
            item.cost_usd for item in runtime.attempts if item.cost_usd is not None
        ]
        if len(known) == len(runtime.attempts) and all(
            item.cost_accounting_status == "complete" for item in runtime.attempts
        ):
            return float(sum(known)), "complete"
        if known:
            return float(sum(known)), "partial"
        return None, "unknown"

    def _accounting_total(
        self,
        runtime: _Runtime,
        value_names: tuple[str, ...],
        status_name: str,
    ) -> tuple[list[int | None], str]:
        if not runtime.attempts:
            return [None for _ in value_names], "unknown"
        values_by_name = [
            [getattr(item, name) for item in runtime.attempts]
            for name in value_names
        ]
        all_known = all(
            all(value is not None for value in values) for values in values_by_name
        )
        statuses_complete = all(
            getattr(item, status_name) == "complete" for item in runtime.attempts
        )
        totals = [
            sum(value for value in values if value is not None)
            if any(value is not None for value in values)
            else None
            for values in values_by_name
        ]
        if all_known and statuses_complete:
            return totals, "complete"
        if any(total is not None for total in totals):
            return totals, "partial"
        return totals, "unknown"

    def _persist_manifest(self, runtime: _Runtime) -> None:
        cost, cost_status = self._actual_cost(runtime)
        tokens, token_status = self._accounting_total(
            runtime,
            ("input_tokens", "output_tokens"),
            "token_accounting_status",
        )
        durations, duration_status = self._accounting_total(
            runtime, ("duration_ms",), "duration_accounting_status"
        )
        terminal = runtime.machine.state in TERMINAL_STATES
        manifest = RunManifestSnapshot(
            schema_version="villani.run_manifest.v1",
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            task_id=runtime.task_id,
            created_at=runtime.created_at,
            updated_at=self._now(),
            completed_at=self._now() if terminal else None,
            final_state=runtime.machine.state,
            attempt_ids=[item.attempt_id for item in runtime.attempts],
            selected_attempt_id=runtime.selected_attempt_id,
            total_cost_usd=cost,
            cost_accounting_status=cost_status,
            total_input_tokens=tokens[0],
            total_output_tokens=tokens[1],
            token_accounting_status=token_status,
            total_duration_ms=durations[0],
            duration_accounting_status=duration_status,
            artifact_paths=RunArtifactPaths(
                task="task.json",
                classification="classification.json",
                state="state.json",
                events="events.jsonl",
                policy_decisions="policy_decisions.jsonl",
                selection="selection.json",
                materialization="materialization.json",
            ),
            metadata={
                "policy_configuration": redact_data(
                    _mapping_copy(runtime.request.policy_configuration)
                ),
                "terminal_reason": runtime.terminal_reason,
            },
        )
        runtime.store.write_protocol("manifest.json", manifest)

    def _persist_state(self, runtime: _Runtime) -> None:
        if runtime.last_event is None:
            raise RunStoreError("cannot persist state before the first event")
        state = RunStateSnapshot(
            schema_version="villani.run_state.v1",
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            state=runtime.machine.state,
            previous_state=runtime.previous_state,
            terminal=runtime.machine.terminal,
            updated_at=self._now(),
            last_event_id=runtime.last_event.event_id,
            last_sequence=runtime.last_event.sequence,
            active_attempt_id=(
                None if runtime.machine.terminal else runtime.active_attempt_id
            ),
            attempt_count=len(runtime.attempts),
            accepted_candidate_ids=list(runtime.eligible_candidate_ids),
            failure=runtime.failure,
            metadata={"terminal_reason": runtime.terminal_reason},
        )
        runtime.store.write_protocol("state.json", state)

    def _transition(
        self,
        runtime: _Runtime,
        target: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        attempt_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        runtime.machine.require_transition(target)  # type: ignore[arg-type]
        previous = runtime.machine.state
        complete_payload = {
            "from_state": previous,
            "to_state": target,
            **_mapping_copy(payload),
        }
        event = runtime.events.emit(
            event_type,
            complete_payload,
            attempt_id=attempt_id,
            parent_event_id=parent_event_id,
        )
        runtime.machine.transition(target)  # type: ignore[arg-type]
        runtime.previous_state = previous
        runtime.last_event = event
        if runtime.machine.terminal:
            runtime.active_attempt_id = None
        self._persist_state(runtime)
        self._persist_manifest(runtime)
        return event

    def _emit_state_event(
        self,
        runtime: _Runtime,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        attempt_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        event = runtime.events.emit(
            event_type,
            _mapping_copy(payload),
            attempt_id=attempt_id,
            parent_event_id=parent_event_id,
        )
        runtime.last_event = event
        self._persist_state(runtime)
        return event

    def _emit_failure_event(
        self,
        runtime: _Runtime,
        event_type: str,
        error: Exception,
        operation: str,
        *,
        attempt_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> None:
        self._emit_state_event(
            runtime,
            event_type,
            failure_payload(error, operation=operation),
            attempt_id=attempt_id,
            parent_event_id=parent_event_id,
        )

    def _fail(
        self,
        runtime: _Runtime,
        code: str,
        reason: str,
        *,
        error: Exception | None = None,
    ) -> None:
        if runtime.machine.terminal:
            return
        runtime.terminal_reason = redact_message(reason)
        runtime.failure = FailureDetail(
            code=code,
            message=runtime.terminal_reason,
            details=(
                {"exception_class": error.__class__.__name__}
                if error is not None
                else {}
            ),
        )
        self._transition(
            runtime,
            "FAILED",
            "run_failed",
            {
                "code": code,
                "message": runtime.terminal_reason,
                "exception_class": (
                    error.__class__.__name__ if error is not None else None
                ),
            },
        )

    def _exhaust(self, runtime: _Runtime, reason: str) -> None:
        runtime.terminal_reason = reason
        self._transition(
            runtime,
            "EXHAUSTED",
            "run_exhausted",
            {"reason": reason},
        )

    def _result(
        self, runtime: _Runtime, forced_state: str | None = None
    ) -> ClosedLoopRunResult:
        state = forced_state or runtime.machine.state
        if state not in TERMINAL_STATES:
            state = "FAILED"
        cost, accounting = self._actual_cost(runtime)
        return ClosedLoopRunResult(
            run_id=runtime.run_id,
            terminal_state=state,  # type: ignore[arg-type]
            selected_attempt_id=runtime.selected_attempt_id,
            run_directory=runtime.store.run_directory,
            actual_known_cost_usd=cost,
            accounting_status=accounting,  # type: ignore[arg-type]
            failure_or_exhaustion_reason=runtime.terminal_reason,
        )
