"""Deterministic closed-loop controller with dependency-injected side effects."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, cast

from villani_ops.core.backend import Backend
from villani_ops.materialize import inspect_patch_application

from .event_writer import EventWriter, failure_payload, redact_data, redact_message
from .event_sink import RunEventSink
from .failure_classification import classify_failure, material_progress
from .classification_adjustments import apply_classification_policy
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
    SelectionRanking,
    Selector,
    Verification,
    VerificationSummary,
    Verifier,
)
from .protocol import (
    AccountingStatus,
    AttemptSnapshot,
    BackendConsideration,
    BudgetSnapshot,
    CandidateRanking,
    ClassificationSnapshot,
    ControllerState,
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
    StageUsage,
    TaskSnapshot,
    VerificationSnapshot,
)
from .run_store import RunStore, RunStoreError, json_safe_copy
from villani_ops.execution_environment import preflight_report
from villani_ops.execution_environment import ExecutionPolicyDenied
from .protocol_v2 import ResourceV2
from .durable_io import (
    append_jsonl_durable,
    read_jsonl_tolerant,
    repair_truncated_final_jsonl,
)
from .delivery_workflow import (
    ApprovalWorkflow,
    DeliveryRecord,
    automatic_authority,
    build_patch_review,
    configured_delivery_mode,
    delivery_configuration,
    materialization_type_for_mode,
    patch_digest,
    successful_delivery_state,
    workflow_enabled,
)
from .schema_validation import (
    parse_protocol_document,
    validate_event_stream,
    validate_protocol_document,
)
from .state_machine import ClosedLoopStateMachine, TERMINAL_STATES
from .policy import (
    BootstrapPolicyConfiguration,
    BootstrapPolicyEngine,
    configured_backends,
)
from .costs import estimate_attempt_cost
from .approvals import ApprovalRecord, ApprovalScope
from .adapters.git_isolation import validate_target_lineage
from villani_ops.isolation.copy_git import remove_tree
from villani_ops.providers import (
    validate_closed_loop_backend,
    validate_runtime_credentials,
)
from .shadow_routing import (
    CapabilityCatalogSnapshot,
    ShadowRouter,
    TaskFeatures,
    capability_catalog_snapshot,
    extract_task_features,
)
from .guarded_routing import GuardedTaskRouter
from .candidate_strategies import (
    acknowledged_diversity_summary,
    CandidateDimensions,
    CandidateObservation,
    CandidatePlan,
    CandidateScheduler,
    ReliabilityAccounting,
    ReliabilityStrategyConfiguration,
    adaptive_stop,
    build_candidate_plans,
    configuration_from_policy,
    immutable_baseline_digest,
)


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
    wall_clock_offset_ms: int = 0
    machine: ClosedLoopStateMachine = field(default_factory=ClosedLoopStateMachine)
    last_event: EventEnvelope | None = None
    previous_state: ControllerState | None = None
    active_attempt_id: str | None = None
    classification: ClassificationSnapshot | None = None
    classification_backend: Backend | None = None
    task_features: TaskFeatures | None = None
    capability_catalog: CapabilityCatalogSnapshot | None = None
    policy_decision_count: int = 0
    attempts: list[AttemptSnapshot] = field(default_factory=list)
    attempt_results: dict[str, AttemptResult] = field(default_factory=dict)
    attempt_contexts: dict[str, AttemptContext] = field(default_factory=dict)
    attempt_start_events: dict[str, str] = field(default_factory=dict)
    attempt_patches: dict[str, str] = field(default_factory=dict)
    verifications: list[VerificationSnapshot] = field(default_factory=list)
    policy_decisions: list[PolicyDecisionSnapshot] = field(default_factory=list)
    allocated_attempt_ids: set[str] = field(default_factory=set)
    eligible_candidate_ids: list[str] = field(default_factory=list)
    selected_attempt_id: str | None = None
    selection: SelectionSnapshot | None = None
    materialization: MaterializationSnapshot | None = None
    delivery: DeliveryRecord | None = None
    committed_events: list[EventEnvelope] = field(default_factory=list)
    loaded_state_terminal: bool = False
    failure: FailureDetail | None = None
    terminal_reason: str | None = None
    reliability_configuration: ReliabilityStrategyConfiguration | None = None
    reliability_baseline_sha256: str | None = None
    candidate_plans: dict[str, CandidatePlan] = field(default_factory=dict)
    reliability_explicit: bool = False


class ClosedLoopController:
    """Run the canonical controller state machine using injected dependencies."""

    def __init__(
        self,
        *,
        classifier: Classifier,
        policy_engine: PolicyEngine | None = None,
        attempt_runner: AttemptRunner,
        verifier: Verifier,
        selector: Selector,
        materializer: Materializer,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        id_factory: Callable[[str], str] | None = None,
        on_event: Callable[[EventEnvelope], None] | None = None,
        failure_injector: Callable[[str], None] | None = None,
        event_sink: RunEventSink | None = None,
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
        self._on_event = on_event
        self._failure_injector = failure_injector
        self._event_sink = event_sink
        manifests: list[Any] = []
        for dependency in (attempt_runner, verifier, selector, materializer):
            manifest = getattr(dependency, "plugin_manifest", None)
            if manifest is not None:
                manifests.append(manifest)
            manifests.extend(getattr(dependency, "additional_plugin_manifests", ()))
        self._plugin_identities = tuple(manifest.identity() for manifest in manifests)

    def _checkpoint(self, boundary: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(boundary)

    def run(self, request: ClosedLoopRunRequest) -> ClosedLoopRunResult:
        """Execute until terminal state or a persisted approval pause."""

        run_id = request.run_id or self._id_factory("run")
        trace_id = self._id_factory("trace")
        task_id = self._id_factory("task")
        created_at = self._now()
        store = RunStore(request.runs_root, run_id)
        runtime: _Runtime | None = None
        try:
            store.create()
            events = EventWriter(
                store, trace_id, self._now, self._on_event, self._event_sink
            )
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
            self._validate_run_configuration(runtime.request.policy_configuration)
            self._checkpoint("after_run_creation")
            if not self._classify(runtime):
                runtime.events.finalize_delivery()
                return self._result(runtime)
            self._drive(runtime)
            runtime.events.finalize_delivery()
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
                runtime.events.finalize_delivery()
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

    def approval_action(
        self,
        run_id: str,
        runs_root: str | Path,
        *,
        action: Literal["approve", "reject", "request_rerun", "choose_candidate"],
        actor: str,
        authenticated: bool,
        authentication_type: str,
        reason: str = "",
        candidate_id: str | None = None,
    ) -> ClosedLoopRunResult:
        """Apply one authenticated, audited action to a persisted approval pause."""

        store = RunStore(runs_root, run_id)
        with store.recovery_lock():
            runtime = self._load_recovery_runtime(store)
            if runtime.machine.state != "AWAITING_APPROVAL":
                raise RunStoreError(
                    "approval action is permitted only while awaiting approval"
                )
            self._resume_awaiting_approval(runtime)
            if runtime.machine.state != "AWAITING_APPROVAL":
                runtime.events.finalize_delivery()
                return self._result(runtime)
            delivery = runtime.delivery
            if delivery is None:
                raise RunStoreError("approval state has no delivery record")
            if delivery.approval.authenticated_required and not authenticated:
                self._append_approval_audit(
                    runtime,
                    action=action,
                    actor=actor,
                    authenticated=False,
                    authentication_type=authentication_type,
                    result="denied",
                    reason="authenticated approval is required in connected mode",
                    candidate_id=candidate_id,
                )
                self._emit_state_event(
                    runtime,
                    "approval_unauthorized",
                    {
                        "action": action,
                        "authentication_type": authentication_type,
                        "approval_state_unchanged": True,
                    },
                    attempt_id=delivery.selected_attempt_id,
                )
                raise PermissionError(
                    "authenticated approval is required in connected mode"
                )
            safe_actor = redact_message(actor or "unknown approver", limit=200)
            safe_reason = redact_message(reason or action.replace("_", " "), limit=500)
            if action == "choose_candidate":
                if not candidate_id:
                    raise ValueError("candidate_id is required")
                self._change_approval_candidate(
                    runtime,
                    candidate_id=candidate_id,
                    actor=safe_actor,
                    authenticated=authenticated,
                    authentication_type=authentication_type,
                    reason=safe_reason,
                )
                runtime.events.finalize_delivery()
                return self._result(runtime)
            if action == "approve":
                self._approve_delivery(
                    runtime,
                    actor=safe_actor,
                    authenticated=authenticated,
                    authentication_type=authentication_type,
                    reason=safe_reason,
                )
            else:
                self._decline_delivery(
                    runtime,
                    action=action,
                    actor=safe_actor,
                    authenticated=authenticated,
                    authentication_type=authentication_type,
                    reason=safe_reason,
                )
            runtime.events.finalize_delivery()
            return self._result(runtime)

    @staticmethod
    def _validate_run_configuration(configuration: Mapping[str, Any]) -> None:
        backends = configured_backends(configuration)
        currencies: set[str] = set()
        for backend in backends.values():
            if backend.enabled and (
                "classification" in backend.roles or "coding" in backend.roles
            ):
                validate_closed_loop_backend(backend)
                validate_runtime_credentials(backend)
                currencies.add(backend.currency)
        if len(currencies) > 1:
            raise ValueError(
                "enabled classification/coding backends must use one currency per run; "
                "currency conversion is not performed"
            )
        verifier = configuration.get("verifier")
        if isinstance(verifier, Mapping) and not bool(verifier.get("no_llm", True)):
            verifier_backend = backends.get(str(verifier.get("backend")))
            if verifier_backend is not None:
                validate_closed_loop_backend(verifier_backend)
                validate_runtime_credentials(verifier_backend)
                if currencies and verifier_backend.currency not in currencies:
                    raise ValueError(
                        "enabled classification/coding/verifier backends must use one currency per run; "
                        "currency conversion is not performed"
                    )

    def _drive(
        self,
        runtime: _Runtime,
        pending: tuple[PolicyDecision, str | None] | None = None,
    ) -> None:
        """Continue a new or recovered run from a committed controller state."""

        next_decision = pending
        while not runtime.machine.terminal:
            decision = next_decision or self._ask_policy(runtime)
            next_decision = None
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
            reliability = runtime.reliability_configuration
            if (
                reliability is not None
                and not runtime.attempts
                and reliability.strategy
                in {"parallel_diverse_candidates", "adaptive_candidates"}
            ):
                self._run_parallel_strategy(runtime, action, attempt_id)
                break
            self._run_attempt(runtime, action, attempt_id)
            if self._apply_reliability_stop(runtime):
                break

    def resume(self, run_id: str, runs_root: str | Path) -> ClosedLoopRunResult:
        """Reconcile and continue one interrupted canonical run idempotently."""

        store = RunStore(runs_root, run_id)
        runtime: _Runtime | None = None
        try:
            with store.recovery_lock():
                runtime = self._load_recovery_runtime(store)
                # A committed terminal state is read-only: no recovery event,
                # dependency invocation, snapshot rewrite, or bundle mutation.
                if runtime.loaded_state_terminal:
                    runtime.events.finalize_delivery()
                    return self._result(runtime)
                if runtime.machine.terminal:
                    self._recovery_event(
                        runtime,
                        "terminal_state_reconciled",
                        {"committed_terminal_state": runtime.machine.state},
                    )
                    runtime.events.finalize_delivery()
                    return self._result(runtime)
                repaired: list[str] = []
                for name in ("events.jsonl", "policy_decisions.jsonl"):
                    path = store.run_directory / name
                    if repair_truncated_final_jsonl(path):
                        repaired.append(name)
                if repaired:
                    runtime = self._load_recovery_runtime(store)
                    self._recovery_event(
                        runtime,
                        "truncated_jsonl_repaired",
                        {"files": repaired},
                    )
                self._reconcile_and_continue(runtime)
                runtime.events.finalize_delivery()
                return self._result(runtime)
        except Exception as error:
            if runtime is not None and not runtime.machine.terminal:
                try:
                    self._recovery_event(
                        runtime,
                        "failed",
                        {
                            "exception_class": error.__class__.__name__,
                            "message": redact_message(str(error)),
                            "manual_inspection_required": True,
                        },
                    )
                    self._fail(
                        runtime,
                        "recovery_failure",
                        redact_message(str(error)),
                        error=error,
                    )
                    runtime.events.finalize_delivery()
                    return self._result(runtime)
                except Exception:
                    pass
            return ClosedLoopRunResult(
                run_id=run_id,
                terminal_state="FAILED",
                selected_attempt_id=(
                    runtime.selected_attempt_id if runtime is not None else None
                ),
                run_directory=store.run_directory,
                actual_known_cost_usd=None,
                accounting_status="unknown",
                failure_or_exhaustion_reason=redact_message(str(error)),
            )

    @staticmethod
    def _read_protocol(path: Path, expected: type[Any]) -> Any:
        document = json.loads(path.read_text(encoding="utf-8"))
        validate_protocol_document(document)
        value = expected.model_validate(document)
        return value

    def _load_recovery_runtime(self, store: RunStore) -> _Runtime:
        run_dir = store.run_directory
        manifest = self._read_protocol(run_dir / "manifest.json", RunManifestSnapshot)
        state = self._read_protocol(run_dir / "state.json", RunStateSnapshot)
        task = self._read_protocol(run_dir / "task.json", TaskSnapshot)
        if not (
            manifest.run_id == state.run_id == task.run_id == store.run_id
            and manifest.trace_id == state.trace_id
            and manifest.task_id == task.task_id
        ):
            raise RunStoreError("canonical recovery identities do not agree")

        event_documents = read_jsonl_tolerant(run_dir / "events.jsonl")
        events = validate_event_stream(event_documents)
        if not events:
            raise RunStoreError("canonical run has no committed events")
        if any(
            event.run_id != store.run_id or event.trace_id != manifest.trace_id
            for event in events
        ):
            raise RunStoreError("event identity does not match the run manifest")
        store.open_existing(last_sequence=events[-1].sequence)

        machine = ClosedLoopStateMachine()
        previous_state: ControllerState | None = None
        state_at_sequence: dict[int, str] = {}
        for event in events:
            from_state = event.payload.get("from_state")
            to_state = event.payload.get("to_state")
            if to_state is not None:
                if from_state != machine.state:
                    raise RunStoreError(
                        f"event {event.event_id} contradicts committed state "
                        f"{machine.state}"
                    )
                previous_state = machine.state
                machine.transition(str(to_state))  # type: ignore[arg-type]
            state_at_sequence[event.sequence] = machine.state
        if state.last_sequence not in state_at_sequence:
            raise RunStoreError("state snapshot references an uncommitted event")
        if state_at_sequence[state.last_sequence] != state.state:
            raise RunStoreError("state snapshot contradicts its committed event")

        configuration = manifest.metadata.get("policy_configuration")
        if not isinstance(configuration, Mapping):
            configuration = {}
        run_created = events[0]
        max_attempts_value = run_created.payload.get("max_attempts", 1)
        budgets = configuration.get("budgets")
        budget_values = budgets if isinstance(budgets, Mapping) else {}
        request = ClosedLoopRunRequest(
            task=task.instruction,
            repository_path=task.repository_path,
            success_criteria=task.success_criteria,
            runs_root=store.runs_root,
            max_attempts=max(int(max_attempts_value), 1),
            max_cost=(
                float(budget_values["max_cost"])
                if budget_values.get("max_cost") is not None
                else None
            ),
            max_wall_time=(
                float(budget_values["max_wall_time"])
                if budget_values.get("max_wall_time") is not None
                else None
            ),
            requires_file_changes=task.requires_file_changes,
            policy_configuration=dict(configuration),
            run_id=manifest.run_id,
            lineage=(
                dict(task.metadata.get("lineage", {}))
                if isinstance(task.metadata.get("lineage"), Mapping)
                else {}
            ),
        )
        runtime = _Runtime(
            request=request,
            run_id=manifest.run_id,
            trace_id=manifest.trace_id,
            task_id=manifest.task_id,
            created_at=manifest.created_at,
            started_monotonic=self._monotonic(),
            wall_clock_offset_ms=max(int(manifest.run_wall_clock_duration_ms or 0), 0),
            store=store,
            events=EventWriter(
                store,
                manifest.trace_id,
                self._now,
                self._on_event,
                self._event_sink,
            ),
            machine=machine,
            last_event=events[-1],
            previous_state=previous_state,
            active_attempt_id=state.active_attempt_id,
            selected_attempt_id=manifest.selected_attempt_id,
            failure=state.failure,
            terminal_reason=str(state.metadata.get("terminal_reason") or "") or None,
            committed_events=list(events),
            loaded_state_terminal=state.terminal,
        )
        runtime.reliability_configuration = configuration_from_policy(
            request.policy_configuration, maximum_attempts=request.max_attempts
        )
        runtime.reliability_explicit = isinstance(
            request.policy_configuration.get("candidate_reliability"), Mapping
        )
        strategy_path = run_dir / "candidate_strategy.json"
        if strategy_path.is_file():
            strategy_document = json.loads(strategy_path.read_text(encoding="utf-8"))
            baseline_value = strategy_document.get("baseline_sha256")
            if isinstance(baseline_value, str):
                runtime.reliability_baseline_sha256 = baseline_value

        classification_path = run_dir / "classification.json"
        if classification_path.is_file():
            classification = self._read_protocol(
                classification_path, ClassificationSnapshot
            )
            if classification.run_id != runtime.run_id:
                raise RunStoreError("classification belongs to another run")
            runtime.classification = classification

        decisions_path = run_dir / "policy_decisions.jsonl"
        if decisions_path.is_file():
            decision_documents = read_jsonl_tolerant(decisions_path)
            for expected_sequence, document in enumerate(decision_documents, 1):
                parsed = parse_protocol_document(document)
                if not isinstance(parsed, PolicyDecisionSnapshot):
                    raise RunStoreError("policy JSONL contains a non-policy document")
                if (
                    parsed.run_id != runtime.run_id
                    or parsed.trace_id != runtime.trace_id
                    or parsed.decision_sequence != expected_sequence
                ):
                    raise RunStoreError(
                        "policy decision identity or sequence is invalid"
                    )
                runtime.policy_decisions.append(parsed)
                if parsed.attempt_id:
                    runtime.allocated_attempt_ids.add(parsed.attempt_id)
        runtime.policy_decision_count = len(runtime.policy_decisions)

        attempt_ids = set(manifest.attempt_ids)
        attempt_ids.update(
            event.attempt_id
            for event in events
            if event.attempt_id and event.event_type == "attempt_started"
        )
        attempt_ids.update(runtime.allocated_attempt_ids)
        attempts_root = run_dir / "attempts"
        if attempts_root.is_dir():
            attempt_ids.update(
                path.name
                for path in attempts_root.iterdir()
                if path.is_dir() and path.name.startswith("attempt_")
            )
        for attempt_id in sorted(attempt_ids):
            runtime.allocated_attempt_ids.add(attempt_id)
            start_event = next(
                (
                    event
                    for event in reversed(events)
                    if event.event_type == "attempt_started"
                    and event.attempt_id == attempt_id
                ),
                None,
            )
            if start_event is not None:
                runtime.attempt_start_events[attempt_id] = start_event.event_id
            attempt_path = attempts_root / attempt_id / "attempt.json"
            if not attempt_path.is_file():
                continue
            attempt = self._read_protocol(attempt_path, AttemptSnapshot)
            if attempt.run_id != runtime.run_id or attempt.attempt_id != attempt_id:
                raise RunStoreError("attempt snapshot identity is invalid")
            runtime.attempts.append(attempt)
            dimensions_value = attempt.metadata.get("candidate_dimensions")
            if isinstance(dimensions_value, Mapping):
                dimensions = CandidateDimensions.model_validate(dimensions_value)
                fingerprint_value = attempt.metadata.get(
                    "effective_configuration_sha256"
                )
                baseline = str(
                    attempt.metadata.get("baseline_sha256")
                    or runtime.reliability_baseline_sha256
                    or ""
                )
                if (
                    baseline
                    and attempt.metadata.get(
                        "runner_acknowledged_candidate_configuration"
                    )
                    and isinstance(fingerprint_value, str)
                ):
                    runtime.candidate_plans[attempt_id] = CandidatePlan(
                        candidate_id=attempt_id,
                        ordinal=attempt.ordinal,
                        dimensions=dimensions,
                        effective_configuration_sha256=fingerprint_value,
                        baseline_sha256=baseline,
                        sandbox_id=str(
                            attempt.metadata.get("sandbox_id")
                            or f"sandbox_{attempt_id}"
                        ),
                        repair_source_attempt_id=(
                            str(attempt.metadata["repair_source_attempt_id"])
                            if attempt.metadata.get("repair_source_attempt_id")
                            else None
                        ),
                    )
            context = self._attempt_context_from_snapshot(runtime, attempt)
            runtime.attempt_contexts[attempt_id] = context
            result = self._attempt_result_from_snapshot(runtime, attempt)
            runtime.attempt_results[attempt_id] = result
            if result.patch is not None:
                runtime.attempt_patches[attempt_id] = result.patch
            for artifact_name in ("worktree.json", "runner_telemetry.json"):
                artifact = attempts_root / attempt_id / artifact_name
                if artifact.is_file():
                    value = json.loads(artifact.read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise RunStoreError(f"{artifact_name} must be a JSON object")
            verification_path = run_dir / "verification" / f"{attempt_id}.json"
            if verification_path.is_file():
                verification = self._read_protocol(
                    verification_path, VerificationSnapshot
                )
                if (
                    verification.run_id != runtime.run_id
                    or verification.attempt_id != attempt_id
                ):
                    raise RunStoreError("verification snapshot identity is invalid")
                runtime.verifications.append(verification)
                if verification.acceptance_eligible:
                    runtime.eligible_candidate_ids.append(attempt_id)

        selection_path = run_dir / "selection.json"
        if selection_path.is_file():
            selection = self._read_protocol(selection_path, SelectionSnapshot)
            if selection.run_id != runtime.run_id:
                raise RunStoreError("selection belongs to another run")
            runtime.selection = selection
            runtime.selected_attempt_id = (
                selection.selected_candidate_ids[0]
                if selection.selected_candidate_ids
                else None
            )
        materialization_path = run_dir / "materialization.json"
        if materialization_path.is_file():
            materialization = self._read_protocol(
                materialization_path, MaterializationSnapshot
            )
            if materialization.run_id != runtime.run_id:
                raise RunStoreError("materialization belongs to another run")
            runtime.materialization = materialization
        delivery_path = run_dir / "delivery.json"
        if delivery_path.is_file():
            delivery = DeliveryRecord.model_validate(
                json.loads(delivery_path.read_text(encoding="utf-8"))
            )
            if (
                delivery.run_id != runtime.run_id
                or delivery.trace_id != runtime.trace_id
            ):
                raise RunStoreError("delivery state belongs to another run")
            runtime.delivery = delivery
        return runtime

    def _attempt_context_from_snapshot(
        self, runtime: _Runtime, attempt: AttemptSnapshot
    ) -> AttemptContext:
        return AttemptContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            task_id=runtime.task_id,
            attempt_id=attempt.attempt_id,
            ordinal=attempt.ordinal,
            task=runtime.request.task,
            repository_path=str(runtime.request.repository_path),
            success_criteria=runtime.request.success_criteria,
            requires_file_changes=runtime.request.requires_file_changes,
            backend_name=attempt.backend_name,
            model=attempt.model,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
            run_directory=runtime.store.run_directory,
            attempt_directory=(
                runtime.store.run_directory / "attempts" / attempt.attempt_id
            ),
            candidate_dimensions=(
                _read_only_mapping(attempt.metadata["candidate_dimensions"])
                if isinstance(attempt.metadata.get("candidate_dimensions"), Mapping)
                else MappingProxyType({})
            ),
            classification=(
                _read_only_mapping(
                    runtime.classification.metadata["effective_classification"]
                )
                if runtime.classification is not None
                and isinstance(
                    runtime.classification.metadata.get("effective_classification"),
                    Mapping,
                )
                else MappingProxyType({})
            ),
            baseline_sha256=(
                str(attempt.metadata["baseline_sha256"])
                if attempt.metadata.get("baseline_sha256")
                else runtime.reliability_baseline_sha256
            ),
            repair_source_attempt_id=(
                str(attempt.metadata["repair_source_attempt_id"])
                if attempt.metadata.get("repair_source_attempt_id")
                else None
            ),
            cancellation_event=threading.Event(),
        )

    def _artifact_text(self, runtime: _Runtime, value: str | None) -> str:
        if not value:
            return ""
        path = (runtime.store.run_directory / value).resolve()
        if not path.is_relative_to(runtime.store.run_directory.resolve()):
            raise RunStoreError("attempt artifact path escapes the run directory")
        return (
            path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        )

    def _attempt_result_from_snapshot(
        self, runtime: _Runtime, attempt: AttemptSnapshot
    ) -> AttemptResult:
        patch = self._artifact_text(runtime, attempt.patch_path)
        telemetry: Mapping[str, Any] = {}
        if attempt.runner_telemetry_path:
            telemetry_path = (
                runtime.store.run_directory / attempt.runner_telemetry_path
            ).resolve()
            if telemetry_path.is_file():
                loaded = json.loads(telemetry_path.read_text(encoding="utf-8"))
                telemetry = loaded if isinstance(loaded, dict) else {}
        return AttemptResult(
            runner_name=attempt.runner_name,
            status=cast(Literal["completed", "failed", "cancelled"], attempt.status),
            worktree_path=attempt.worktree_path,
            patch=patch,
            exit_code=attempt.exit_code,
            model=attempt.model,
            stdout=self._artifact_text(runtime, attempt.stdout_path),
            stderr=self._artifact_text(runtime, attempt.stderr_path),
            runner_telemetry=telemetry,
            duration_ms=attempt.duration_ms,
            duration_accounting_status=attempt.duration_accounting_status,
            input_tokens=attempt.input_tokens,
            output_tokens=attempt.output_tokens,
            token_accounting_status=attempt.token_accounting_status,
            cost_usd=attempt.cost_usd,
            cost_accounting_status=attempt.cost_accounting_status,
            error=(
                DependencyFailure(
                    code=attempt.error.code,
                    message=attempt.error.message,
                    details=attempt.error.details,
                )
                if attempt.error is not None
                else None
            ),
            metadata=attempt.metadata,
        )

    def _recovery_event(
        self, runtime: _Runtime, action: str, evidence: Mapping[str, Any]
    ) -> EventEnvelope:
        event = self._emit_state_event(
            runtime,
            f"recovery_{action}",
            {
                "previous_state": runtime.machine.state,
                "evidence": _mapping_copy(evidence),
            },
            attempt_id=(
                runtime.active_attempt_id
                if action.startswith(("attempt", "verification"))
                else None
            ),
        )
        self._persist_manifest(runtime)
        return event

    def _has_event(
        self,
        runtime: _Runtime,
        event_type: str,
        *,
        attempt_id: str | None = None,
        decision_id: str | None = None,
    ) -> bool:
        return any(
            event.event_type == event_type
            and (attempt_id is None or event.attempt_id == attempt_id)
            and (decision_id is None or event.payload.get("decision_id") == decision_id)
            for event in runtime.committed_events
        )

    def _policy_from_snapshot(self, snapshot: PolicyDecisionSnapshot) -> PolicyDecision:
        return PolicyDecision(
            action=snapshot.action,
            reason=snapshot.reason,
            considered_backends=tuple(
                BackendOption(
                    backend_name=item.backend_name,
                    model=item.model,
                    eligible=item.eligible,
                    capability_score=item.capability_score,
                    estimated_cost_usd=item.estimated_cost_usd,
                    cost_accounting_status=item.cost_accounting_status,
                    rejection_reasons=tuple(item.rejection_reasons),
                )
                for item in snapshot.considered_backends
            ),
            chosen_backend=snapshot.chosen_backend,
            chosen_model=snapshot.chosen_model,
            policy_version=snapshot.policy_version,
            classification_reference=snapshot.classification_id,
            metadata=snapshot.metadata,
        )

    def _latest_planned_policy(
        self, runtime: _Runtime
    ) -> tuple[PolicyDecision, str | None, PolicyDecisionSnapshot] | None:
        if not runtime.policy_decisions:
            return None
        snapshot = runtime.policy_decisions[-1]
        return self._policy_from_snapshot(snapshot), snapshot.attempt_id, snapshot

    def _reconcile_and_continue(self, runtime: _Runtime) -> None:
        while not runtime.machine.terminal:
            state = runtime.machine.state
            if state == "CREATED":
                self._recovery_event(
                    runtime,
                    "classification_restarted",
                    {"reason": "run creation committed without classification start"},
                )
                if not self._classify(runtime):
                    return
                self._drive(runtime)
                return

            if state == "CLASSIFYING":
                if runtime.classification is None:
                    if self._has_event(runtime, "classification_completed"):
                        raise RunStoreError(
                            "classification completion exists without a valid snapshot"
                        )
                    self._recovery_event(
                        runtime,
                        "classification_retried",
                        {"reason": "classification started without a snapshot"},
                    )
                    if not self._classify(runtime, already_started=True):
                        return
                else:
                    self._recovery_event(
                        runtime,
                        "classification_completion_reconciled",
                        {"classification_id": runtime.classification.classification_id},
                    )
                    self._transition(
                        runtime,
                        "CLASSIFIED",
                        "classification_completed",
                        {
                            "classification_id": runtime.classification.classification_id,
                            "recovered": True,
                        },
                    )
                self._drive(runtime)
                return

            if runtime.classification is None:
                raise RunStoreError(
                    "committed classification state has no valid classification snapshot"
                )

            planned = self._latest_planned_policy(runtime)
            if state in {"CLASSIFIED", "REJECTED", "VERIFIED"} and planned:
                decision, attempt_id, snapshot = planned
                if not self._has_event(
                    runtime, "policy_selected", decision_id=snapshot.decision_id
                ):
                    self._recovery_event(
                        runtime,
                        "policy_completion_reconciled",
                        {"decision_id": snapshot.decision_id},
                    )
                    self._record_policy_state(runtime, decision, snapshot)
                    state = runtime.machine.state

            if state in {"CLASSIFIED", "REJECTED", "VERIFIED"}:
                if runtime.selection is not None:
                    self._reuse_selection(runtime)
                    return
                self._drive(runtime)
                return

            if state == "POLICY_SELECTED":
                if planned is None:
                    raise RunStoreError(
                        "POLICY_SELECTED has no committed policy decision"
                    )
                decision, attempt_id, snapshot = planned
                self._recovery_event(
                    runtime,
                    "policy_reused",
                    {
                        "decision_id": snapshot.decision_id,
                        "attempt_id": attempt_id,
                    },
                )
                if decision.action == "select":
                    if runtime.selection is not None:
                        self._reuse_selection(runtime)
                    else:
                        self._select_and_materialize(runtime)
                    return
                if decision.action in {"fail", "exhaust"}:
                    self._drive(runtime, (decision, attempt_id))
                    return
                if attempt_id is None:
                    raise RunStoreError("attempt policy decision has no attempt ID")
                if self._has_event(runtime, "attempt_started", attempt_id=attempt_id):
                    raise RunStoreError(
                        "attempt start event exists but recovered state is POLICY_SELECTED"
                    )
                self._drive(runtime, (decision, attempt_id))
                return

            if state == "ATTEMPT_RUNNING":
                attempt_id = self._active_attempt_from_events(runtime)
                runtime.active_attempt_id = attempt_id
                attempt = next(
                    (
                        item
                        for item in runtime.attempts
                        if item.attempt_id == attempt_id
                    ),
                    None,
                )
                if attempt is None:
                    self._record_interrupted_attempt(runtime, attempt_id)
                    self._drive(runtime)
                    return
                self._recovery_event(
                    runtime,
                    "attempt_completion_reconciled",
                    {
                        "attempt_id": attempt_id,
                        "snapshot_status": attempt.status,
                    },
                )
                self._complete_loaded_attempt(runtime, attempt)
                self._drive(runtime)
                return

            if state == "ATTEMPT_COMPLETED":
                attempt_id = self._active_attempt_from_events(runtime)
                attempt = next(
                    (
                        item
                        for item in runtime.attempts
                        if item.attempt_id == attempt_id
                    ),
                    None,
                )
                if attempt is None:
                    raise RunStoreError(
                        "ATTEMPT_COMPLETED has no valid attempt snapshot"
                    )
                verification = next(
                    (
                        item
                        for item in runtime.verifications
                        if item.attempt_id == attempt_id
                    ),
                    None,
                )
                if verification is not None:
                    self._transition(
                        runtime,
                        "VERIFYING",
                        "verification_started",
                        {"attempt_id": attempt_id, "recovered": True},
                        attempt_id=attempt_id,
                    )
                    self._complete_loaded_verification(runtime, verification)
                else:
                    self._resume_verification(runtime, attempt, already_started=False)
                self._drive(runtime)
                return

            if state == "VERIFYING":
                attempt_id = self._active_attempt_from_events(runtime)
                verification = next(
                    (
                        item
                        for item in runtime.verifications
                        if item.attempt_id == attempt_id
                    ),
                    None,
                )
                if verification is not None:
                    self._recovery_event(
                        runtime,
                        "verification_completion_reconciled",
                        {
                            "attempt_id": attempt_id,
                            "outcome": verification.outcome,
                        },
                    )
                    self._complete_loaded_verification(runtime, verification)
                else:
                    attempt = next(
                        (
                            item
                            for item in runtime.attempts
                            if item.attempt_id == attempt_id
                        ),
                        None,
                    )
                    if attempt is None:
                        raise RunStoreError(
                            "verification start has no valid attempt snapshot"
                        )
                    self._recovery_event(
                        runtime,
                        "verification_retried",
                        {
                            "attempt_id": attempt_id,
                            "coding_attempt_rerun": False,
                        },
                    )
                    self._resume_verification(runtime, attempt, already_started=True)
                self._drive(runtime)
                return

            if state == "AWAITING_APPROVAL":
                self._resume_awaiting_approval(runtime)
                return

            if state == "SELECTING":
                if runtime.selection is not None:
                    self._reuse_selection(runtime)
                else:
                    self._recovery_event(
                        runtime,
                        "selection_retried",
                        {"reason": "selection started without a snapshot"},
                    )
                    self._select_and_materialize(runtime)
                return

            if state == "MATERIALIZING":
                self._resume_materialization(runtime)
                return

            raise RunStoreError(f"unsupported recovery state: {state}")

    def _active_attempt_from_events(self, runtime: _Runtime) -> str:
        for event in reversed(runtime.committed_events):
            if event.attempt_id and event.event_type in {
                "attempt_started",
                "attempt_completed",
                "attempt_failed",
                "verification_started",
                "verification_completed",
                "verification_failed",
            }:
                return event.attempt_id
        if runtime.active_attempt_id:
            return runtime.active_attempt_id
        raise RunStoreError("recovery state has no active attempt identity")

    def _planned_attempt_context(
        self, runtime: _Runtime, attempt_id: str
    ) -> AttemptContext:
        start = next(
            (
                event
                for event in reversed(runtime.committed_events)
                if event.event_type == "attempt_started"
                and event.attempt_id == attempt_id
            ),
            None,
        )
        decision = next(
            (
                value
                for value in reversed(runtime.policy_decisions)
                if value.attempt_id == attempt_id
            ),
            None,
        )
        if start is None or decision is None:
            raise RunStoreError("interrupted attempt lacks start or policy evidence")
        return AttemptContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            task_id=runtime.task_id,
            attempt_id=attempt_id,
            ordinal=int(start.payload.get("ordinal") or len(runtime.attempts) + 1),
            task=runtime.request.task,
            repository_path=str(runtime.request.repository_path),
            success_criteria=runtime.request.success_criteria,
            requires_file_changes=runtime.request.requires_file_changes,
            backend_name=decision.chosen_backend or "interrupted",
            model=decision.chosen_model,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
            run_directory=runtime.store.run_directory,
            attempt_directory=(runtime.store.run_directory / "attempts" / attempt_id),
        )

    def _record_interrupted_attempt(self, runtime: _Runtime, attempt_id: str) -> None:
        context = self._planned_attempt_context(runtime, attempt_id)
        runtime.attempt_contexts[attempt_id] = context
        start = next(
            event
            for event in runtime.committed_events
            if event.event_type == "attempt_started" and event.attempt_id == attempt_id
        )
        self._recovery_event(
            runtime,
            "attempt_interrupted",
            {
                "attempt_id": attempt_id,
                "failure_category": "infrastructure_failure",
            },
        )
        result = AttemptResult(
            runner_name="interrupted",
            status="cancelled",
            worktree_path="interrupted",
            patch=None,
            exit_code=None,
            model=context.model,
            stdout=self._artifact_text(runtime, f"attempts/{attempt_id}/stdout.log"),
            stderr="Interrupted before a complete attempt snapshot was committed.",
            duration_accounting_status="unknown",
            token_accounting_status="unknown",
            cost_accounting_status="unknown",
            error=DependencyFailure(
                code="interrupted_attempt",
                message="Coding attempt was interrupted before completion.",
                details={"failure_category": "infrastructure_failure"},
            ),
            metadata={
                "failure_category": "infrastructure_failure",
                "material_progress": False,
                "recovered_interruption": True,
            },
        )
        snapshot = self._persist_attempt(
            runtime, context, result, start.timestamp, self._now()
        )
        runtime.attempt_results[attempt_id] = result
        self._record_attempt_failure(
            runtime, attempt_id, "infrastructure_failure", False
        )
        self._transition(
            runtime,
            "ATTEMPT_COMPLETED",
            "attempt_failed",
            {
                "status": snapshot.status,
                "exit_code": snapshot.exit_code,
                "interrupted": True,
            },
            attempt_id=attempt_id,
            parent_event_id=start.event_id,
        )
        self._transition(
            runtime,
            "REJECTED",
            "candidate_rejected",
            {
                "outcome": "interrupted",
                "reason": "attempt interrupted as infrastructure failure",
            },
            attempt_id=attempt_id,
            parent_event_id=start.event_id,
        )

    def _complete_loaded_attempt(
        self, runtime: _Runtime, attempt: AttemptSnapshot
    ) -> None:
        event_type = (
            "attempt_completed"
            if attempt.status == "completed" and attempt.exit_code == 0
            else "attempt_failed"
        )
        start_event_id = runtime.attempt_start_events.get(attempt.attempt_id)
        self._transition(
            runtime,
            "ATTEMPT_COMPLETED",
            event_type,
            {
                "status": attempt.status,
                "exit_code": attempt.exit_code,
                "recovered": True,
            },
            attempt_id=attempt.attempt_id,
            parent_event_id=start_event_id,
        )
        self._emit_state_event(
            runtime,
            "patch_captured",
            {
                "patch_bytes": attempt.patch_bytes,
                "patch_sha256": attempt.patch_sha256,
                "recovered": True,
            },
            attempt_id=attempt.attempt_id,
            parent_event_id=start_event_id,
        )
        if (
            runtime.request.requires_file_changes
            and not runtime.attempt_patches.get(attempt.attempt_id, "").strip()
        ):
            normalized = self._empty_patch_verification(runtime, attempt.attempt_id)
            runtime.store.write_protocol(
                f"verification/{attempt.attempt_id}.json", normalized
            )
            runtime.verifications.append(normalized)
            self._transition(
                runtime,
                "REJECTED",
                "verification_completed",
                {
                    "outcome": "rejected",
                    "acceptance_eligible": False,
                    "normalization": "empty_patch",
                    "recovered": True,
                },
                attempt_id=attempt.attempt_id,
            )
            return
        self._resume_verification(runtime, attempt, already_started=False)

    def _resume_verification(
        self,
        runtime: _Runtime,
        attempt: AttemptSnapshot,
        *,
        already_started: bool,
    ) -> None:
        context = runtime.attempt_contexts[attempt.attempt_id]
        result = runtime.attempt_results[attempt.attempt_id]
        initial_retry = 1 if already_started else 0
        policy_values = runtime.request.policy_configuration.get("policy")
        values = (
            policy_values
            if isinstance(policy_values, Mapping)
            else runtime.request.policy_configuration
        )
        retry_limit = (
            BootstrapPolicyConfiguration.model_validate(values).verifier_retry_limit
            if values.get("version") == "bootstrap_v1"
            else 0
        )
        if already_started and retry_limit < 1:
            error = RuntimeError(
                "verification was interrupted and policy permits no verifier retry"
            )
            normalized = self._verifier_error_snapshot(
                runtime, attempt.attempt_id, error
            ).model_copy(
                update={
                    "metadata": {
                        "failure_category": "verification_failure",
                        "verifier_retry_count": 0,
                        "coding_attempt_rerun_for_verification": False,
                        "recovery_retry_disallowed": True,
                    }
                }
            )
            runtime.store.write_protocol(
                f"verification/{attempt.attempt_id}.json", normalized
            )
            runtime.verifications.append(normalized)
            self._complete_loaded_verification(runtime, normalized)
            return
        self._verify_attempt(
            runtime,
            context,
            result,
            runtime.attempt_start_events.get(attempt.attempt_id, ""),
            already_started=already_started,
            initial_retry_count=initial_retry,
        )

    def _complete_loaded_verification(
        self, runtime: _Runtime, verification: VerificationSnapshot
    ) -> None:
        self._transition(
            runtime,
            "VERIFIED",
            (
                "verification_failed"
                if verification.outcome == "error"
                else "verification_completed"
            ),
            {
                "outcome": verification.outcome,
                "acceptance_eligible": verification.acceptance_eligible,
                "recovered": True,
            },
            attempt_id=verification.attempt_id,
        )
        failure_category = (
            str(verification.metadata.get("failure_category") or "") or None
        )
        if failure_category:
            self._record_attempt_failure(
                runtime,
                verification.attempt_id,
                failure_category,
                bool(
                    next(
                        item
                        for item in runtime.attempts
                        if item.attempt_id == verification.attempt_id
                    ).metadata.get("material_progress", False)
                ),
            )
        if verification.acceptance_eligible:
            if verification.attempt_id not in runtime.eligible_candidate_ids:
                runtime.eligible_candidate_ids.append(verification.attempt_id)
            self._persist_state(runtime)
            self._persist_manifest(runtime)
        else:
            self._transition(
                runtime,
                "REJECTED",
                "candidate_rejected",
                {
                    "outcome": verification.outcome,
                    "reason": verification.reason,
                    "recovered": True,
                },
                attempt_id=verification.attempt_id,
            )

    def _selection_interface(self, snapshot: SelectionSnapshot) -> Selection:
        return Selection(
            selected_attempt_id=(
                snapshot.selected_candidate_ids[0]
                if snapshot.selected_candidate_ids
                else None
            ),
            strategy=snapshot.strategy,
            reason=snapshot.reason,
            rankings=tuple(
                SelectionRanking(
                    attempt_id=item.attempt_id,
                    rank=item.rank,
                    reason=item.reason,
                    actual_cost_usd=item.actual_cost_usd,
                    cost_accounting_status=item.cost_accounting_status,
                    evidence=item.evidence,
                )
                for item in snapshot.rankings
            ),
            advisory_comparison=snapshot.advisory_comparison,
            metadata=snapshot.metadata,
        )

    def _reuse_selection(self, runtime: _Runtime) -> None:
        selection = runtime.selection
        if selection is None or not selection.selected_candidate_ids:
            raise RunStoreError("recovery selection has no selected candidate")
        selected_id = selection.selected_candidate_ids[0]
        if selected_id not in runtime.eligible_candidate_ids:
            raise RunStoreError("recovery selection is not acceptance eligible")
        runtime.selected_attempt_id = selected_id
        if runtime.machine.state != "SELECTING":
            self._transition_to_selecting(runtime)
        self._recovery_event(
            runtime,
            "selection_reused",
            {
                "selection_id": selection.selection_id,
                "selected_attempt_id": selected_id,
            },
        )
        if not self._has_event(runtime, "candidate_selected"):
            self._emit_state_event(
                runtime,
                "candidate_selected",
                {
                    "selection_id": selection.selection_id,
                    "attempt_id": selected_id,
                    "recovered": True,
                },
            )
        if workflow_enabled(runtime.request.policy_configuration):
            self._continue_delivery_workflow(
                runtime,
                selection,
                self._selection_interface(selection),
                self._eligible_candidates(runtime),
            )
        else:
            self._materialize_reused_selection(runtime, selection)

    def _materialize_reused_selection(
        self, runtime: _Runtime, selection: SelectionSnapshot
    ) -> None:
        selected_id = selection.selected_candidate_ids[0]
        candidate = next(
            item
            for item in self._eligible_candidates(runtime)
            if item.attempt.attempt_id == selected_id
        )
        if runtime.machine.state != "MATERIALIZING":
            started = self._transition(
                runtime,
                "MATERIALIZING",
                "materialization_started",
                {
                    "selection_id": selection.selection_id,
                    "selected_attempt_id": selected_id,
                    "recovered": True,
                },
            )
        else:
            recovered_started = next(
                (
                    event
                    for event in reversed(runtime.committed_events)
                    if event.event_type == "materialization_started"
                ),
                runtime.last_event,
            )
            if recovered_started is None:
                raise RunStoreError("materialization recovery lacks a start event")
            started = recovered_started
        returned_selection = self._selection_interface(selection)
        context = MaterializationContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            repository_path=str(runtime.request.repository_path),
            selected_candidate=candidate,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
            run_directory=runtime.store.run_directory,
            risk=runtime.classification.risk if runtime.classification else None,
        )
        returned = self._materializer.materialize(returned_selection, context)
        if not isinstance(returned, Materialization):
            raise RunStoreError("materializer returned an invalid recovery result")
        materialization = self._persist_materialization(
            runtime, selection, candidate, returned, started.timestamp
        )
        self._finish_recovered_materialization(runtime, materialization)

    def _resume_materialization(self, runtime: _Runtime) -> None:
        if runtime.selection is None or not runtime.selection.selected_candidate_ids:
            raise RunStoreError(
                "materialization recovery has no valid recorded selection"
            )
        if workflow_enabled(runtime.request.policy_configuration):
            delivery = runtime.delivery
            if delivery is None:
                raise RunStoreError(
                    "delivery workflow recovery has no persisted delivery record"
                )
            selected_id = runtime.selection.selected_candidate_ids[0]
            candidate = next(
                item
                for item in self._eligible_candidates(runtime)
                if item.attempt.attempt_id == selected_id
            )
            if runtime.materialization is not None:
                self._recovery_event(
                    runtime,
                    "materialization_snapshot_reused",
                    {
                        "materialization_id": (
                            runtime.materialization.materialization_id
                        ),
                        "status": runtime.materialization.status,
                        "delivery_id": delivery.delivery_id,
                    },
                )
                self._finish_delivery_materialization(
                    runtime,
                    delivery,
                    runtime.materialization,
                    candidate,
                    recovered=True,
                )
                return
            self._materialize_delivery_workflow(
                runtime,
                runtime.selection,
                self._selection_interface(runtime.selection),
                candidate,
                delivery,
                already_started=True,
            )
            return
        if runtime.materialization is not None:
            self._recovery_event(
                runtime,
                "materialization_snapshot_reused",
                {
                    "materialization_id": runtime.materialization.materialization_id,
                    "status": runtime.materialization.status,
                },
            )
            self._finish_recovered_materialization(runtime, runtime.materialization)
            return
        selected_id = runtime.selection.selected_candidate_ids[0]
        candidate = next(
            item
            for item in self._eligible_candidates(runtime)
            if item.attempt.attempt_id == selected_id
        )
        patch_value = candidate.attempt.patch_path
        if not patch_value:
            raise RunStoreError("selected candidate has no recorded patch path")
        patch_path = (runtime.store.run_directory / patch_value).resolve()
        if not patch_path.is_relative_to(runtime.store.run_directory.resolve()):
            raise RunStoreError("selected patch escapes the canonical run directory")
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        if patch_text != candidate.patch:
            raise RunStoreError(
                "selected patch bytes differ from the recorded candidate"
            )
        patch_hash = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
        if patch_hash != candidate.attempt.patch_sha256:
            raise RunStoreError("selected patch hash is invalid")
        worktree = candidate.attempt.metadata.get("worktree")
        baseline = (
            worktree.get("source_repository") if isinstance(worktree, dict) else None
        )
        if not isinstance(baseline, dict):
            raise RunStoreError("selected candidate lacks repository identity evidence")
        target_repo = Path(runtime.request.repository_path).resolve()
        validate_target_lineage(target_repo, baseline)
        inspection = inspect_patch_application(target_repo, patch_path)
        self._recovery_event(
            runtime,
            "materialization_inspected",
            {
                "selected_attempt_id": selected_id,
                "patch_sha256": patch_hash,
                "apply_status": inspection.get("status"),
                "reverse_check_exit_code": inspection.get("reverse_check_exit_code"),
                "normal_check_exit_code": inspection.get("normal_check_exit_code"),
            },
        )
        if inspection.get("status") == "applied":
            started = next(
                event
                for event in reversed(runtime.committed_events)
                if event.event_type == "materialization_started"
            )
            materialization = self._persist_materialization(
                runtime,
                runtime.selection,
                candidate,
                Materialization(
                    status="succeeded",
                    final_patch=patch_text,
                    final_report=(
                        "# Materialization report\n\n"
                        "Recovery proved the exact selected patch was already applied "
                        "using `git apply --reverse --check`; it was not applied again.\n"
                    ),
                    changed_files=tuple(
                        str(item) for item in inspection.get("changed_files") or []
                    ),
                    metadata={
                        "recovered_already_applied": True,
                        "apply_inspection": inspection,
                    },
                ),
                started.timestamp,
            )
            self._finish_recovered_materialization(runtime, materialization)
            return
        if inspection.get("status") == "not_applied":
            self._materialize_reused_selection(runtime, runtime.selection)
            return
        raise RunStoreError(
            "selected patch application state is unsafe to infer; manual inspection required"
        )

    def _finish_recovered_materialization(
        self, runtime: _Runtime, materialization: MaterializationSnapshot
    ) -> None:
        if materialization.status != "succeeded":
            message = (
                materialization.failure.message
                if materialization.failure is not None
                else "materialization recovery found a failed snapshot"
            )
            self._fail(runtime, "materialization_failure", message)
            return
        if not self._has_event(runtime, "materialization_completed"):
            self._emit_state_event(
                runtime,
                "materialization_completed",
                {
                    "materialization_id": materialization.materialization_id,
                    "recovered": True,
                },
            )
        if runtime.machine.state == "MATERIALIZING":
            self._transition(
                runtime,
                "COMPLETED",
                "run_completed",
                {
                    "selected_attempt_id": runtime.selected_attempt_id,
                    "recovered": True,
                },
            )

    def _initialize_bundle(self, runtime: _Runtime) -> None:
        event = runtime.events.emit(
            "run_created",
            {
                "task_id": runtime.task_id,
                "max_attempts": runtime.request.max_attempts,
                "task_instruction": runtime.request.task,
                "success_criteria": runtime.request.success_criteria,
                "repository_id": str(runtime.request.repository_path),
                "repository": Path(runtime.request.repository_path).name,
                "agent_name": "villani-ops",
                "agent_version": "0.2.0",
                "lineage": _mapping_copy(runtime.request.lineage),
            },
        )
        runtime.last_event = event
        runtime.committed_events.append(event)
        task = TaskSnapshot(
            schema_version="villani.task.v1",
            task_id=runtime.task_id,
            run_id=runtime.run_id,
            created_at=runtime.created_at,
            repository_path=str(runtime.request.repository_path),
            instruction=str(redact_data(runtime.request.task)),
            success_criteria=str(redact_data(runtime.request.success_criteria)),
            constraints=[],
            requires_file_changes=runtime.request.requires_file_changes,
            metadata={"lineage": redact_data(_mapping_copy(runtime.request.lineage))},
        )
        runtime.store.write_protocol("task.json", task)
        validation_discovery = runtime.request.policy_configuration.get(
            "repository_validation_discovery"
        )
        validation_commands = runtime.request.policy_configuration.get(
            "repository_validation_commands"
        )
        if isinstance(validation_discovery, Mapping) or isinstance(
            validation_commands, list
        ):
            runtime.store.write_json(
                "validation_plan.json",
                redact_data(
                    {
                        "schema_version": "villani.repository_validation_plan.v1",
                        "discovery": (
                            _mapping_copy(validation_discovery)
                            if isinstance(validation_discovery, Mapping)
                            else None
                        ),
                        "confirmed_commands": (
                            list(validation_commands)
                            if isinstance(validation_commands, list)
                            else []
                        ),
                        "discovery_is_authoritative": False,
                        "authority_begins": "structured_repository_validation_execution",
                    }
                ),
            )
        runtime.reliability_configuration = configuration_from_policy(
            runtime.request.policy_configuration,
            maximum_attempts=runtime.request.max_attempts,
        )
        runtime.reliability_explicit = isinstance(
            runtime.request.policy_configuration.get("candidate_reliability"), Mapping
        )
        runtime.reliability_baseline_sha256 = immutable_baseline_digest(
            runtime.request.repository_path,
            runtime.request.task,
            runtime.request.success_criteria,
        )
        runtime.store.write_json(
            "candidate_strategy.json",
            {
                **runtime.reliability_configuration.model_dump(mode="json"),
                "baseline_sha256": runtime.reliability_baseline_sha256,
                "task_id": runtime.task_id,
                "run_id": runtime.run_id,
            },
        )
        repository_path = Path(runtime.request.repository_path)
        preflight = (
            preflight_report(repository_path, runtime.request.policy_configuration)
            if repository_path.exists()
            else {
                "schema_version": "villani.execution_preflight.v1",
                "repository": {"path": str(repository_path), "available": False},
                "provider": {"provider": "unavailable", "available": False},
                "execution_environment_fingerprint": None,
                "fingerprint_error": "repository_unavailable",
                "inferred_setup_executed": False,
            }
        )
        runtime.store.write_json("preflight.json", preflight)
        fingerprint = str(preflight["execution_environment_fingerprint"])
        provider_value = preflight.get("provider")
        provider_name = (
            str(provider_value.get("provider"))
            if isinstance(provider_value, Mapping)
            else "unknown"
        )
        runtime.store.write_protocol(
            "resource.json",
            ResourceV2(
                schema_version="villani.resource.v2",
                service_name="villani",
                service_version=None,
                deployment_environment="local",
                host_id=None,
                process_id=None,
                attributes={
                    "villani.execution_environment.provider": provider_name,
                    "villani.execution_environment.fingerprint": fingerprint,
                    "villani.execution_environment.preflight": "preflight.json",
                },
            ),
        )
        self._persist_state(runtime)
        self._persist_manifest(runtime)

    def _classify(self, runtime: _Runtime, *, already_started: bool = False) -> bool:
        if not already_started:
            self._transition(
                runtime,
                "CLASSIFYING",
                "classification_started",
                {"task_id": runtime.task_id},
            )
            self._checkpoint("after_classification_start")
        try:
            runtime.classification_backend = self._resolve_classification_backend(
                runtime.request.policy_configuration
            )
            if runtime.request.max_cost is not None and runtime.classification_backend:
                projected, projected_status = self._projected_classification_cost(
                    runtime
                )
                if projected_status != "complete" or projected is None:
                    self._fail(
                        runtime,
                        "cost_budget_configuration",
                        "cost budget cannot permit classification with unknown projected spend",
                    )
                    return False
                if projected > runtime.request.max_cost:
                    self._fail(
                        runtime,
                        "cost_budget_exhausted",
                        "cost budget is below the projected classifier spend",
                    )
                    return False
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
                classification_backend_name=(
                    runtime.classification_backend.name
                    if runtime.classification_backend is not None
                    else None
                ),
                classification_backend_model=(
                    runtime.classification_backend.model
                    if runtime.classification_backend is not None
                    else None
                ),
            )
            returned = self._classifier.classify(runtime.request.task, context)
            if not isinstance(returned, Classification):
                raise TypeError("classifier returned an invalid Classification")
            classified_at = self._now()
            effective, adjustments, classification_policy_version = (
                apply_classification_policy(
                    returned,
                    runtime.request.policy_configuration,
                    timestamp=classified_at,
                )
            )
            classification = ClassificationSnapshot(
                schema_version="villani.classification.v1",
                classification_id="classification_001",
                run_id=runtime.run_id,
                task_id=runtime.task_id,
                classified_at=classified_at,
                difficulty=effective.difficulty,
                risk=effective.risk,
                category=effective.category,
                required_capabilities=list(effective.required_capabilities),
                estimated_attempts_needed=effective.estimated_attempts_needed,
                needs_tests=effective.needs_tests,
                confidence=effective.confidence,
                reasoning_summary=effective.reasoning_summary,
                signals=_mapping_copy(effective.signals),
                metadata={
                    **_mapping_copy(returned.metadata),
                    "raw_classification": {
                        "difficulty": returned.difficulty,
                        "risk": returned.risk,
                        "category": returned.category,
                        "required_capabilities": list(returned.required_capabilities),
                        "confidence": returned.confidence,
                    },
                    "effective_classification": {
                        "difficulty": effective.difficulty,
                        "risk": effective.risk,
                        "category": effective.category,
                        "required_capabilities": list(effective.required_capabilities),
                        "confidence": effective.confidence,
                    },
                    "classification_adjustments": [
                        item.model_dump(mode="json") for item in adjustments
                    ],
                    "classification_policy_version": classification_policy_version,
                    "classification_backend": (
                        _mapping_copy(returned.metadata).get("classification_backend")
                        or {
                            "name": runtime.classification_backend.name,
                            "model": runtime.classification_backend.model,
                            "role": "classification",
                        }
                        if runtime.classification_backend is not None
                        else None
                    ),
                },
                llm_usage=[
                    StageUsage.model_validate(
                        {key: value for key, value in item.items() if key != "error"}
                    )
                    for item in _mapping_copy(returned.metadata).get(
                        "classifier_attempts", []
                    )
                    if isinstance(item, Mapping)
                ],
            )
            runtime.store.write_protocol("classification.json", classification)
            runtime.classification = classification
            self._checkpoint("after_classification_snapshot")
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

        if classification.metadata.get("classification_fallback"):
            self._emit_state_event(
                runtime,
                "classification_fallback",
                {
                    "reason": classification.metadata.get(
                        "classification_fallback_reason", "classifier failure"
                    ),
                    "failed_calls": len(classification.llm_usage),
                },
            )
        self._transition(
            runtime,
            "CLASSIFIED",
            "classification_completed",
            {
                "classification_id": classification.classification_id,
                "raw_classification": {
                    **classification.metadata["raw_classification"],
                },
                "effective_classification": {
                    **classification.metadata["effective_classification"],
                },
                "confidence": classification.confidence,
                "classification_adjustments": classification.metadata.get(
                    "classification_adjustments", []
                ),
                "policy_version": classification.metadata.get(
                    "classification_policy_version"
                ),
                "category": classification.category,
                "required_capabilities": classification.required_capabilities,
            },
        )
        return True

    def _resolve_classification_backend(
        self, configuration: Mapping[str, Any]
    ) -> Backend | None:
        """Resolve only the classification role before classification completes."""

        backends = configured_backends(configuration)
        if not backends:
            return None
        eligible = [
            backend
            for backend in backends.values()
            if backend.enabled and "classification" in backend.roles
        ]
        if not eligible:
            raise ValueError("no enabled classification-capable backend is configured")
        return min(eligible, key=lambda item: (-item.capability_score, item.name))

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
                    failure_category=str(attempt.metadata.get("failure_category") or "")
                    or None,
                    material_progress=bool(
                        attempt.metadata.get("material_progress", False)
                    ),
                    duration_ms=attempt.duration_ms,
                    rate_limited=(
                        str(attempt.metadata.get("failure_category") or "")
                        == "rate_limit"
                        or bool(attempt.error and "rate" in attempt.error.code.lower())
                    ),
                )
                for attempt in runtime.attempts
            ),
            verifications=tuple(
                VerificationSummary(
                    attempt_id=verification.attempt_id,
                    outcome=verification.outcome,
                    acceptance_eligible=verification.acceptance_eligible,
                    recommended_action=verification.recommended_action,
                    failure_category=str(
                        verification.metadata.get("failure_category") or ""
                    )
                    or None,
                    verifier_retry_count=int(
                        verification.metadata.get("verifier_retry_count") or 0
                    ),
                    disagreement=bool(
                        verification.metadata.get("verifier_disagreement", False)
                    ),
                )
                for verification in runtime.verifications
            ),
            eligible_candidate_ids=tuple(runtime.eligible_candidate_ids),
            budget=budget_before,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
        )
        # This is intentionally a one-way evidence side effect. ShadowRouter does
        # not implement PolicyEngine, and no field from its output is supplied to
        # the production policy engine below.
        self._record_shadow_recommendation(runtime)
        try:
            policy_engine = (
                self._policy_engine
                or BootstrapPolicyEngine.from_configuration(
                    runtime.request.policy_configuration
                )
            )
            returned = policy_engine.decide(context)
            if not isinstance(returned, PolicyDecision):
                raise TypeError("policy engine returned an invalid PolicyDecision")
            routing_values = runtime.request.policy_configuration.get("routing")
            routing_configuration = (
                routing_values if isinstance(routing_values, Mapping) else {}
            )
            assignment = routing_configuration.get("experiment_assignment")
            returned, guarded = GuardedTaskRouter(
                runtime.request.policy_configuration
            ).evaluate(
                run_id=runtime.run_id,
                sequence=runtime.policy_decision_count + 1,
                bootstrap=returned,
                attempts=context.attempts,
                verifications=context.verifications,
                budget=budget_before,
                timestamp=self._now(),
                experiment_assignment=(
                    assignment if isinstance(assignment, Mapping) else None
                ),
            )
            append_jsonl_durable(
                runtime.store.run_directory / "guarded_routing_decisions.jsonl",
                guarded.model_dump(mode="json"),
            )
            self._validate_policy_semantics(runtime, returned)
            attempt_id = (
                self._next_attempt_id(runtime)
                if returned.action in {"attempt", "retry", "escalate"}
                else None
            )
            runtime.policy_decision_count += 1
            snapshot = self._policy_snapshot(
                runtime, returned, attempt_id, budget_before
            )
            runtime.store.append_policy_decision(snapshot)
            runtime.policy_decisions.append(snapshot)
            if attempt_id is not None:
                runtime.allocated_attempt_ids.add(attempt_id)
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
        self._checkpoint("after_policy_decision")
        return returned, attempt_id

    def _record_shadow_recommendation(self, runtime: _Runtime) -> None:
        """Persist advisory routing evidence without participating in execution."""

        assert runtime.classification is not None
        try:
            if runtime.task_features is None:
                path = runtime.store.run_directory / "task_features.json"
                if path.is_file():
                    runtime.task_features = TaskFeatures.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                else:
                    shadow_values = runtime.request.policy_configuration.get(
                        "shadow_routing", {}
                    )
                    shadow_configuration = (
                        shadow_values if isinstance(shadow_values, Mapping) else {}
                    )
                    aggregates = shadow_configuration.get("historical_aggregates")
                    runtime.task_features = extract_task_features(
                        runtime.request.repository_path,
                        run_id=runtime.run_id,
                        task=runtime.request.task,
                        success_criteria=runtime.request.success_criteria,
                        classification=runtime.classification,
                        historical_aggregates=(
                            aggregates if isinstance(aggregates, Mapping) else None
                        ),
                        historical_snapshot_id=(
                            str(shadow_configuration["historical_snapshot_id"])
                            if shadow_configuration.get("historical_snapshot_id")
                            else None
                        ),
                    )
                    runtime.store.write_json(
                        "task_features.json",
                        runtime.task_features.model_dump(mode="json"),
                    )
            if runtime.capability_catalog is None:
                path = runtime.store.run_directory / "capability_catalog_snapshot.json"
                if path.is_file():
                    runtime.capability_catalog = (
                        CapabilityCatalogSnapshot.model_validate_json(
                            path.read_text(encoding="utf-8")
                        )
                    )
                else:
                    runtime.capability_catalog = capability_catalog_snapshot(
                        configured_backends(runtime.request.policy_configuration),
                        generated_at=self._now(),
                    )
                    runtime.store.write_json(
                        "capability_catalog_snapshot.json",
                        runtime.capability_catalog.model_dump(mode="json"),
                    )
            shadow_values = runtime.request.policy_configuration.get(
                "shadow_routing", {}
            )
            shadow_configuration = (
                shadow_values if isinstance(shadow_values, Mapping) else {}
            )
            by_backend = shadow_configuration.get("historical_by_backend")
            recommendation = ShadowRouter().recommend(
                run_id=runtime.run_id,
                decision_sequence=runtime.policy_decision_count + 1,
                features=runtime.task_features,
                catalog=runtime.capability_catalog,
                classification=runtime.classification,
                timestamp=self._now(),
                historical_by_backend=(
                    by_backend if isinstance(by_backend, Mapping) else None
                ),
            )
            append_jsonl_durable(
                runtime.store.run_directory / "shadow_recommendations.jsonl",
                recommendation.model_dump(mode="json"),
            )
            self._emit_state_event(
                runtime,
                "shadow_recommendation_recorded",
                {
                    "recommendation_id": recommendation.recommendation_id,
                    "chosen_strategy": recommendation.chosen_strategy,
                    "advisory_only": True,
                },
            )
        except Exception as error:
            # Observability cannot acquire controller authority by failing closed.
            self._emit_state_event(
                runtime,
                "shadow_recommendation_failed",
                {"message": redact_message(str(error)), "advisory_only": True},
            )

    @staticmethod
    def _next_attempt_id(runtime: _Runtime) -> str:
        ordinals = [
            int(value.removeprefix("attempt_"))
            for value in runtime.allocated_attempt_ids
            if value.startswith("attempt_") and value.removeprefix("attempt_").isdigit()
        ]
        ordinal = max(ordinals, default=0) + 1
        candidate = f"attempt_{ordinal:03d}"
        while candidate in runtime.allocated_attempt_ids:
            ordinal += 1
            candidate = f"attempt_{ordinal:03d}"
        return candidate

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
        if runtime.classification is None:
            raise RunStoreError("policy snapshot requires classification")
        metadata = _mapping_copy(decision.metadata)
        metadata.update(
            {
                "classification_reference": (
                    decision.classification_reference
                    or runtime.classification.classification_id
                ),
                "required_capability_score": decision.required_capability_score,
                "required_capability_rule": decision.required_capability_rule,
                "repeats_prior_backend": decision.repeats_prior_backend,
                "escalates_from_prior_backend": decision.escalates_from_prior_backend,
            }
        )
        projected_budget = (
            decision.budget_projection_after
            or self._budget_after_decision(budget_before, decision)
        )
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
            budget_after=self._budget_snapshot(projected_budget),
            metadata=metadata,
        )

    def _record_policy_state(
        self,
        runtime: _Runtime,
        decision: PolicyDecision,
        snapshot: PolicyDecisionSnapshot,
    ) -> None:
        payload = {
            "decision_id": snapshot.decision_id,
            "policy_version": snapshot.policy_version,
            "action": decision.action,
            "reason": decision.reason,
            "chosen_backend": decision.chosen_backend,
            "chosen_model": next(
                (
                    item.model
                    for item in snapshot.considered_backends
                    if item.backend_name == decision.chosen_backend
                ),
                None,
            ),
            "considered_backends": [
                item.model_dump(mode="json") for item in snapshot.considered_backends
            ],
            "required_capability_score": snapshot.metadata.get(
                "required_capability_score"
            ),
            "attempt_id": snapshot.attempt_id,
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

    def _candidate_plan(
        self,
        runtime: _Runtime,
        decision: PolicyDecision,
        attempt_id: str,
        ordinal: int,
    ) -> tuple[CandidateDimensions, CandidatePlan]:
        configuration = runtime.reliability_configuration or configuration_from_policy(
            runtime.request.policy_configuration,
            maximum_attempts=runtime.request.max_attempts,
        )
        requested = (
            configuration.candidates[ordinal - 1]
            if ordinal <= len(configuration.candidates)
            else CandidateDimensions()
        )
        # Reliability strategy cannot bypass guarded routing. Agent/model dimensions
        # are effective only after the production policy selected that route.
        dimensions = requested.model_copy(
            update={
                "backend_name": decision.chosen_backend,
                "model": decision.chosen_model,
                "seed": (
                    requested.seed
                    if requested.seed is not None
                    else ordinal
                    if configuration.strategy
                    in {"parallel_diverse_candidates", "adaptive_candidates"}
                    else None
                ),
            }
        )
        baseline = runtime.reliability_baseline_sha256 or immutable_baseline_digest(
            runtime.request.repository_path,
            runtime.request.task,
            runtime.request.success_criteria,
        )
        repair_source = (
            runtime.attempts[-1].attempt_id
            if configuration.repair_strategy and runtime.attempts
            else None
        )
        plan = CandidatePlan(
            candidate_id=attempt_id,
            ordinal=ordinal,
            dimensions=dimensions,
            effective_configuration_sha256=dimensions.effective_fingerprint,
            baseline_sha256=baseline,
            sandbox_id=f"sandbox_{attempt_id}",
            expected_success=(
                configuration.expected_success_by_ordinal[ordinal - 1]
                if ordinal <= len(configuration.expected_success_by_ordinal)
                else None
            ),
            estimated_cost_usd=(
                configuration.estimated_cost_usd_by_ordinal[ordinal - 1]
                if ordinal <= len(configuration.estimated_cost_usd_by_ordinal)
                else next(
                    (
                        item.estimated_cost_usd
                        for item in decision.considered_backends
                        if item.backend_name == decision.chosen_backend
                    ),
                    None,
                )
            ),
            repair_source_attempt_id=repair_source,
        )
        return dimensions, plan

    @staticmethod
    def _evidence_grade(verification: VerificationSnapshot) -> str:
        if not verification.acceptance_eligible:
            return "none"
        requirements_proven = bool(verification.requirement_results) and all(
            item.outcome in {"passed", "not_applicable"}
            for item in verification.requirement_results
        )
        if requirements_proven and verification.success_evidence:
            return "strong"
        if verification.success_evidence:
            return "moderate"
        return "weak"

    def _apply_reliability_stop(self, runtime: _Runtime) -> bool:
        configuration = runtime.reliability_configuration
        if (
            configuration is None
            or not runtime.reliability_explicit
            or runtime.machine.terminal
        ):
            return runtime.machine.terminal
        observations = tuple(
            CandidateObservation(
                candidate_id=item.attempt_id,
                acceptance_eligible=item.acceptance_eligible,
                verifier_confidence=item.confidence,
                evidence_grade=self._evidence_grade(item),  # type: ignore[arg-type]
                actual_cost_usd=next(
                    (
                        attempt.cost_usd
                        for attempt in runtime.attempts
                        if attempt.attempt_id == item.attempt_id
                    ),
                    None,
                ),
            )
            for item in runtime.verifications
        )
        if not runtime.candidate_plans:
            return False
        last = runtime.candidate_plans[next(reversed(runtime.candidate_plans))]
        default = last.dimensions
        all_plans = build_candidate_plans(
            configuration,
            baseline_sha256=last.baseline_sha256,
            default_dimensions=default,
        )
        # Preserve actual attempt identities and effective configurations for the
        # completed prefix; future plans remain estimates only.
        merged = list(all_plans)
        for index, plan in enumerate(runtime.candidate_plans.values()):
            if index < len(merged):
                merged[index] = plan
        budget = self._budget_context(runtime)
        decision = adaptive_stop(
            configuration,
            tuple(merged),
            observations,
            remaining_attempt_budget=budget.remaining_attempts,
            remaining_cost_budget_usd=budget.remaining_cost_usd,
        )
        diversity_claimed, distinct = acknowledged_diversity_summary(runtime.attempts)
        accounting = ReliabilityAccounting(
            strategy=configuration.strategy,
            planned_attempts=len(merged),
            started_attempts=len(runtime.attempts),
            completed_attempts=len(runtime.verifications),
            cancelled_attempts=sum(
                item.status == "cancelled" for item in runtime.attempts
            ),
            avoided_attempts=decision.avoided_attempts if decision.stop else 0,
            estimated_avoided_spend_usd=(
                decision.estimated_avoided_spend_usd if decision.stop else None
            ),
            diversity_claimed=diversity_claimed,
            distinct_effective_configurations=distinct,
            maximum_observed_concurrency=1,
            stop_reason=decision.reason if decision.stop else None,
        )
        runtime.store.write_json(
            "reliability_accounting.json", accounting.model_dump(mode="json")
        )
        self._checkpoint("after_reliability_stop_decision")
        if not decision.stop:
            return False
        self._emit_state_event(
            runtime,
            "candidate_strategy_stopped",
            {
                **decision.model_dump(mode="json"),
                "strategy": configuration.strategy,
            },
            attempt_id=runtime.active_attempt_id,
        )
        if runtime.eligible_candidate_ids:
            self._select_and_materialize(runtime)
        else:
            self._exhaust(runtime, decision.reason)
        return True

    def _run_parallel_strategy(
        self, runtime: _Runtime, decision: PolicyDecision, first_attempt_id: str
    ) -> None:
        configuration = runtime.reliability_configuration
        assert configuration is not None
        budget = self._budget_context(runtime)
        maximum = min(
            configuration.maximum_candidates,
            budget.remaining_attempts,
            runtime.request.max_attempts - len(runtime.attempts),
        )
        if maximum <= 0:
            self._exhaust(
                runtime, "attempt budget exhausted before candidate scheduling"
            )
            return
        default_dimensions = CandidateDimensions(
            backend_name=decision.chosen_backend,
            model=decision.chosen_model,
        )
        planned = build_candidate_plans(
            configuration,
            baseline_sha256=(runtime.reliability_baseline_sha256 or "0" * 64),
            default_dimensions=default_dimensions,
        )[:maximum]
        attempt_ids = [first_attempt_id]
        for _ in range(1, len(planned)):
            allocated = self._next_attempt_id(runtime)
            runtime.allocated_attempt_ids.add(allocated)
            attempt_ids.append(allocated)
        plans: list[CandidatePlan] = []
        contexts: dict[str, AttemptContext] = {}
        cancellation_events: dict[str, threading.Event] = {}
        route = decision.metadata.get("guarded_task_route")
        route_values = route if isinstance(route, Mapping) else {}
        for source, attempt_id in zip(planned, attempt_ids, strict=True):
            dimensions = source.dimensions.model_copy(
                update={
                    "backend_name": decision.chosen_backend,
                    "model": decision.chosen_model,
                }
            )
            plan = source.model_copy(
                update={
                    "candidate_id": attempt_id,
                    "dimensions": dimensions,
                    "effective_configuration_sha256": dimensions.effective_fingerprint,
                    "sandbox_id": f"sandbox_{attempt_id}",
                }
            )
            plans.append(plan)
            cancellation = threading.Event()
            cancellation_events[attempt_id] = cancellation
            runtime.candidate_plans[attempt_id] = plan
            contexts[attempt_id] = AttemptContext(
                run_id=runtime.run_id,
                trace_id=runtime.trace_id,
                task_id=runtime.task_id,
                attempt_id=attempt_id,
                ordinal=source.ordinal,
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
                execution_provider=(
                    str(route_values["execution_provider"])
                    if route_values.get("execution_provider")
                    else None
                ),
                guarded_task_route=(
                    _read_only_mapping(route_values)
                    if route_values
                    else MappingProxyType({})
                ),
                candidate_dimensions=_read_only_mapping(
                    dimensions.model_dump(mode="json")
                ),
                baseline_sha256=plan.baseline_sha256,
                repair_source_attempt_id=plan.repair_source_attempt_id,
                cancellation_event=cancellation,
            )
        preverified: dict[str, Verification] = {}

        def generate(
            plan: CandidatePlan, cancellation: threading.Event
        ) -> AttemptResult:
            context = contexts[plan.candidate_id]
            if cancellation.is_set():
                return AttemptResult(
                    runner_name="cancelled_before_start",
                    status="cancelled",
                    worktree_path="unavailable",
                    patch=None,
                    exit_code=None,
                )
            return self._attempt_runner.run(context)

        def verify(plan: CandidatePlan, result: AttemptResult) -> CandidateObservation:
            returned = self._verifier.verify(contexts[plan.candidate_id], result)
            if not isinstance(returned, Verification):
                raise TypeError("verifier returned an invalid Verification")
            preverified[plan.candidate_id] = returned
            requirements_proven = bool(returned.requirement_results) and all(
                item.outcome in {"passed", "not_applicable"}
                for item in returned.requirement_results
            )
            grade = (
                "strong"
                if returned.acceptance_eligible
                and requirements_proven
                and returned.success_evidence
                else "moderate"
                if returned.acceptance_eligible and returned.success_evidence
                else "weak"
                if returned.acceptance_eligible
                else "none"
            )
            return CandidateObservation(
                candidate_id=plan.candidate_id,
                acceptance_eligible=returned.acceptance_eligible,
                verifier_confidence=returned.confidence,
                evidence_grade=grade,  # type: ignore[arg-type]
                actual_cost_usd=result.cost_usd,
            )

        scheduler = CandidateScheduler(
            configuration,
            journal_path=runtime.store.run_directory / "candidate_schedule.jsonl",
            checkpoint=lambda boundary: self._checkpoint(f"candidate_{boundary}"),
        )
        executions, accounting = scheduler.execute(
            tuple(plans),
            generate=generate,
            verify=verify,
            remaining_attempt_budget=maximum,
            remaining_cost_budget_usd=budget.remaining_cost_usd,
        )
        runtime.store.write_json(
            "reliability_accounting.json", accounting.model_dump(mode="json")
        )
        for execution in executions:
            attempt_id = execution.plan.candidate_id
            context = contexts[attempt_id]
            if execution.cancelled:
                self._cleanup_attempt_worktree(runtime, context)
                continue
            if runtime.machine.state == "VERIFIED":
                self._transition(
                    runtime,
                    "REJECTED",
                    "candidate_collection_continued",
                    {"strategy": configuration.strategy},
                    attempt_id=runtime.active_attempt_id,
                )
            if runtime.machine.state == "REJECTED":
                self._transition(
                    runtime,
                    "POLICY_SELECTED",
                    "candidate_batch_continued",
                    {"strategy": configuration.strategy},
                )
            if runtime.machine.terminal:
                break
            result = execution.result
            if not isinstance(result, AttemptResult):
                result = AttemptResult(
                    runner_name="candidate_scheduler",
                    status="failed",
                    worktree_path="unavailable",
                    patch=None,
                    exit_code=None,
                    error=DependencyFailure(
                        code="candidate_execution_failed",
                        message=redact_message(execution.error or "candidate failed"),
                    ),
                )
            self._run_attempt(
                runtime,
                decision,
                attempt_id,
                context_override=context,
                precomputed_result=result,
                precomputed_verification=preverified.get(attempt_id),
            )
        if runtime.machine.terminal:
            return
        self._checkpoint("after_candidate_batch")
        if runtime.eligible_candidate_ids:
            self._select_and_materialize(runtime)
        else:
            self._exhaust(
                runtime, accounting.stop_reason or "candidate strategy exhausted"
            )

    def _run_attempt(
        self,
        runtime: _Runtime,
        decision: PolicyDecision,
        attempt_id: str,
        *,
        context_override: AttemptContext | None = None,
        precomputed_result: AttemptResult | None = None,
        precomputed_verification: Verification | None = None,
    ) -> None:
        ordinal = len(runtime.attempts) + 1
        dimensions, plan = self._candidate_plan(runtime, decision, attempt_id, ordinal)
        context = context_override or AttemptContext(
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
            attempt_directory=(runtime.store.run_directory / "attempts" / attempt_id),
            execution_provider=(
                str(
                    decision.metadata.get("guarded_task_route", {}).get(
                        "execution_provider"
                    )
                )
                if isinstance(decision.metadata.get("guarded_task_route"), Mapping)
                and decision.metadata.get("guarded_task_route", {}).get(
                    "execution_provider"
                )
                else None
            ),
            guarded_task_route=(
                _read_only_mapping(decision.metadata["guarded_task_route"])
                if isinstance(decision.metadata.get("guarded_task_route"), Mapping)
                else MappingProxyType({})
            ),
            candidate_dimensions=_read_only_mapping(dimensions.model_dump(mode="json")),
            classification=(
                _read_only_mapping(
                    runtime.classification.metadata["effective_classification"]
                )
                if runtime.classification is not None
                and isinstance(
                    runtime.classification.metadata.get("effective_classification"),
                    Mapping,
                )
                else MappingProxyType({})
            ),
            baseline_sha256=plan.baseline_sha256,
            repair_source_attempt_id=plan.repair_source_attempt_id,
            cancellation_event=threading.Event(),
        )
        if context_override is not None and attempt_id in runtime.candidate_plans:
            plan = runtime.candidate_plans[attempt_id]
            dimensions = plan.dimensions
        runtime.candidate_plans[attempt_id] = plan
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
                "candidate_dimensions": dimensions.model_dump(mode="json"),
                "effective_configuration_sha256": plan.effective_configuration_sha256,
                "baseline_sha256": plan.baseline_sha256,
                "sandbox_id": plan.sandbox_id,
            },
            attempt_id=attempt_id,
        )
        runtime.attempt_start_events[attempt_id] = started.event_id
        runtime.allocated_attempt_ids.add(attempt_id)
        self._checkpoint("after_attempt_start")
        try:
            returned = (
                precomputed_result
                if precomputed_result is not None
                else self._attempt_runner.run(context)
            )
            self._checkpoint("after_runner_return")
            if not isinstance(returned, AttemptResult):
                raise TypeError("attempt runner returned an invalid AttemptResult")
            snapshot = self._persist_attempt(
                runtime, context, returned, started.timestamp, self._now()
            )
            runtime.attempt_results[attempt_id] = returned
            self._checkpoint("after_attempt_snapshot")
            initial_failure = classify_failure(
                returned,
                requires_file_changes=runtime.request.requires_file_changes,
            )
            if initial_failure in {
                "infrastructure_failure",
                "no_change_failure",
            }:
                self._record_attempt_failure(
                    runtime,
                    attempt_id,
                    initial_failure,
                    material_progress(returned),
                )
        except Exception as error:
            if isinstance(error, ExecutionPolicyDenied):
                policy_event = runtime.events.emit(
                    "execution_policy_denied",
                    error.event,
                    attempt_id=attempt_id,
                    parent_event_id=started.event_id,
                )
                runtime.last_event = policy_event
                runtime.committed_events.append(policy_event)
            snapshot = self._persist_synthetic_failed_attempt(
                runtime, context, started.timestamp, error
            )
            self._cleanup_attempt_worktree(runtime, context)
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
            {
                "status": snapshot.status,
                "exit_code": snapshot.exit_code,
                "backend_name": snapshot.backend_name,
                "model": snapshot.model,
                "duration_ms": snapshot.duration_ms,
                "input_tokens": snapshot.input_tokens,
                "output_tokens": snapshot.output_tokens,
                "total_tokens": (
                    snapshot.input_tokens + snapshot.output_tokens
                    if snapshot.input_tokens is not None
                    and snapshot.output_tokens is not None
                    else None
                ),
                "token_accounting_status": snapshot.token_accounting_status,
                "cost_usd": snapshot.cost_usd,
                "cost_accounting_status": snapshot.cost_accounting_status,
                "file_write_count": snapshot.metadata.get(
                    "total_file_writes",
                    returned.runner_telemetry.get("total_file_writes", 0),
                ),
                "changed_files": snapshot.metadata.get("changed_files", []),
                "failure_category": snapshot.metadata.get("failure_category"),
                "patch_sha256": snapshot.patch_sha256,
                "patch_bytes": snapshot.patch_bytes,
                "candidate_configuration": snapshot.metadata.get(
                    "effective_candidate_configuration"
                ),
                "candidate_configuration_acknowledged": bool(
                    snapshot.metadata.get("runner_acknowledged_candidate_configuration")
                ),
                "effective_configuration_sha256": snapshot.metadata.get(
                    "effective_configuration_sha256"
                ),
            },
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
            recorded_category = str(
                next(
                    item for item in runtime.attempts if item.attempt_id == attempt_id
                ).metadata.get("failure_category")
                or "no_change_failure"
            )
            normalized = normalized.model_copy(
                update={
                    "metadata": {
                        **normalized.metadata,
                        "failure_category": recorded_category,
                    }
                }
            )
            runtime.store.write_protocol(f"verification/{attempt_id}.json", normalized)
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
            self._cleanup_attempt_worktree(runtime, context)
            return

        try:
            self._verify_attempt(
                runtime,
                context,
                returned,
                started.event_id,
                returned_override=precomputed_verification,
            )
        finally:
            # Verification failures, including malformed dependency output,
            # must not leave an attempt export behind by accident.
            self._cleanup_attempt_worktree(runtime, context)

    def _cleanup_attempt_worktree(
        self, runtime: _Runtime, context: AttemptContext
    ) -> None:
        """Remove an attempt-owned export once patch capture and verification end."""

        isolation = runtime.request.policy_configuration.get("isolation")
        settings = isolation if isinstance(isolation, Mapping) else {}
        if bool(settings.get("keep_attempt_worktrees", False)):
            return
        attempt_dir = Path(context.attempt_directory).absolute()
        worktree = attempt_dir / "worktree"
        try:
            # Do not resolve this path: resolving a symlink could turn a cleanup
            # operation into deletion of an external target.
            worktree.absolute().relative_to(attempt_dir)
            if worktree.is_symlink():
                worktree.unlink(missing_ok=True)
            else:
                remove_tree(worktree)
            worktree_info_path = attempt_dir / "worktree.json"
            worktree_info: dict[str, Any] = {}
            if worktree_info_path.is_file():
                loaded = json.loads(worktree_info_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    worktree_info = loaded
            worktree_info.update({"retained": False, "cleanup_status": "removed"})
            runtime.store.write_json(
                f"attempts/{context.attempt_id}/worktree.json", worktree_info
            )
            self._emit_state_event(
                runtime,
                "attempt_worktree_removed",
                {"retained": False},
                attempt_id=context.attempt_id,
            )
        except Exception as error:
            self._emit_state_event(
                runtime,
                "attempt_cleanup_failed",
                {
                    "message": redact_message(str(error)),
                    "manual_cleanup_path": str(worktree),
                },
                attempt_id=context.attempt_id,
            )

    def _record_attempt_failure(
        self,
        runtime: _Runtime,
        attempt_id: str,
        category: str,
        has_material_progress: bool,
    ) -> None:
        for index, attempt in enumerate(runtime.attempts):
            if attempt.attempt_id != attempt_id:
                continue
            metadata = _mapping_copy(attempt.metadata)
            metadata.update(
                {
                    "failure_category": category,
                    "material_progress": has_material_progress,
                }
            )
            updated = attempt.model_copy(update={"metadata": metadata})
            runtime.attempts[index] = updated
            runtime.store.write_protocol(f"attempts/{attempt_id}/attempt.json", updated)
            self._persist_manifest(runtime)
            return
        raise RuntimeError(f"cannot classify unknown attempt {attempt_id}")

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

        attempt_metadata = _mapping_copy(result.metadata)
        candidate_plan = runtime.candidate_plans.get(context.attempt_id)
        attempt_metadata.update(
            {
                "candidate_dimensions": _mapping_copy(context.candidate_dimensions),
                "effective_configuration_sha256": (
                    attempt_metadata.get("effective_configuration_sha256")
                    if attempt_metadata.get(
                        "runner_acknowledged_candidate_configuration"
                    )
                    else None
                ),
                "baseline_sha256": context.baseline_sha256,
                "repair_source_attempt_id": context.repair_source_attempt_id,
                "sandbox_id": (
                    candidate_plan.sandbox_id
                    if candidate_plan is not None
                    else context.attempt_id
                ),
            }
        )
        configured_backend = configured_backends(
            runtime.request.policy_configuration
        ).get(context.backend_name)
        if configured_backend is not None:
            attempt_metadata.setdefault("provider", configured_backend.provider)
            attempt_metadata.setdefault("backend_model", configured_backend.model)
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
            metadata=attempt_metadata,
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
        return self._persist_attempt(runtime, context, result, started_at, self._now())

    def _verify_attempt(
        self,
        runtime: _Runtime,
        context: AttemptContext,
        result: AttemptResult,
        attempt_start_event_id: str,
        *,
        already_started: bool = False,
        initial_retry_count: int = 0,
        returned_override: Verification | None = None,
    ) -> None:
        if not already_started:
            self._transition(
                runtime,
                "VERIFYING",
                "verification_started",
                {"attempt_id": context.attempt_id},
                attempt_id=context.attempt_id,
                parent_event_id=attempt_start_event_id,
            )
            self._checkpoint("after_verification_start")
        policy_values = runtime.request.policy_configuration.get("policy")
        if not isinstance(policy_values, Mapping):
            policy_values = runtime.request.policy_configuration
        retry_limit = (
            BootstrapPolicyConfiguration.model_validate(
                policy_values
            ).verifier_retry_limit
            if policy_values.get("version") == "bootstrap_v1"
            else 0
        )
        retry_count = initial_retry_count
        final_error: Exception | None = None
        failure_category: str | None = None
        verification_usage: list[StageUsage] = []
        normalized: VerificationSnapshot | None = None
        while True:
            verification_started = self._monotonic()
            try:
                returned = (
                    returned_override
                    if returned_override is not None
                    else self._verifier.verify(context, result)
                )
                returned_override = None
                if not isinstance(returned, Verification):
                    raise TypeError("verifier returned an invalid Verification")
                self._emit_repository_validation_events(
                    runtime,
                    context.attempt_id,
                    returned,
                    attempt_start_event_id,
                )
                verification_usage.extend(
                    StageUsage.model_validate(dict(item))
                    for item in returned.llm_usage
                    if isinstance(item, Mapping)
                )
                normalized = self._normalize_verification(
                    runtime, context.attempt_id, returned
                )
                failure_category = classify_failure(
                    result,
                    returned,
                    requires_file_changes=runtime.request.requires_file_changes,
                )
                final_error = None
            except Exception as error:
                final_error = error
                failure_category = "verification_failure"
                verification_usage.append(
                    StageUsage(
                        stage="verification",
                        model_calls=None,
                        model_call_accounting_status="unknown",
                        cost=None,
                        cost_accounting_status="unknown",
                        duration_ms=max(
                            int((self._monotonic() - verification_started) * 1000), 0
                        ),
                        duration_accounting_status="complete",
                        failure_state="failed",
                    )
                )
                normalized = self._verifier_error_snapshot(
                    runtime, context.attempt_id, error
                )

            if failure_category == "verification_failure" and retry_count < retry_limit:
                retry_count += 1
                self._emit_state_event(
                    runtime,
                    "verification_failed",
                    {
                        "operation": "verification",
                        "retry_count": retry_count,
                        "retry_limit": retry_limit,
                        "message": (
                            redact_message(str(final_error))
                            if final_error is not None
                            else normalized.reason
                        ),
                    },
                    attempt_id=context.attempt_id,
                    parent_event_id=attempt_start_event_id,
                )
                self._emit_state_event(
                    runtime,
                    "verification_retry_started",
                    {"retry_count": retry_count, "coding_attempt_rerun": False},
                    attempt_id=context.attempt_id,
                    parent_event_id=attempt_start_event_id,
                )
                continue
            break

        if normalized is None:
            raise RunStoreError("verification loop produced no normalized result")
        metadata = _mapping_copy(normalized.metadata)
        metadata.update(
            {
                "failure_category": failure_category,
                "verifier_retry_count": retry_count,
                "coding_attempt_rerun_for_verification": False,
            }
        )
        normalized = normalized.model_copy(
            update={"metadata": metadata, "llm_usage": verification_usage}
        )
        runtime.store.write_protocol(
            f"verification/{context.attempt_id}.json", normalized
        )
        runtime.verifications.append(normalized)
        self._write_evidence_matrix(runtime)
        self._checkpoint("after_verification_snapshot")
        self._transition(
            runtime,
            "VERIFIED",
            "verification_failed"
            if final_error is not None
            else "verification_completed",
            (
                failure_payload(final_error, operation="verification")
                if final_error is not None
                else {
                    **normalized.model_dump(mode="json"),
                    "verifier_retry_count": retry_count,
                    "verification_mode": normalized.metadata.get(
                        "verification_mode", "unspecified"
                    ),
                    "authority_source": normalized.metadata.get(
                        "authority_source", "unspecified"
                    ),
                    "verifier_cost_usd": sum(
                        item.cost or 0 for item in normalized.llm_usage
                    ),
                }
            ),
            attempt_id=context.attempt_id,
            parent_event_id=attempt_start_event_id,
        )

        if failure_category is not None:
            self._record_attempt_failure(
                runtime,
                context.attempt_id,
                failure_category,
                material_progress(result),
            )

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

    def _emit_repository_validation_events(
        self,
        runtime: _Runtime,
        attempt_id: str,
        verification: Verification,
        parent_event_id: str,
    ) -> None:
        """Persist controller-owned validation commands returned as verifier evidence."""

        records = verification.metadata.get("repository_validation_events")
        if not isinstance(records, list):
            return
        delivered = {
            str(event.payload.get("source_event_id"))
            for event in runtime.committed_events
            if event.payload.get("source_event_id")
        }
        appended = False
        for record in records:
            if not isinstance(record, Mapping):
                continue
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                continue
            source_event_id = str(record.get("source_event_id") or "")
            if source_event_id and source_event_id in delivered:
                continue
            timestamp = self._now()
            raw_timestamp = record.get("timestamp")
            if isinstance(raw_timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(
                        raw_timestamp.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
            event_payload = _mapping_copy(payload)
            if source_event_id:
                event_payload["source_event_id"] = source_event_id
            event_type = str(record.get("event_type") or "command_failed")
            if event_type not in {"command_completed", "command_failed"}:
                event_type = "command_failed"
            event = runtime.store.append_event(
                timestamp=timestamp,
                trace_id=runtime.trace_id,
                attempt_id=attempt_id,
                parent_event_id=parent_event_id,
                source="villani_ops_verifier",
                event_type=event_type,
                payload=event_payload,
            )
            runtime.last_event = event
            runtime.committed_events.append(event)
            appended = True
            if source_event_id:
                delivered.add(source_event_id)
        if appended:
            self._persist_state(runtime)

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
        verification_metadata = _mapping_copy(returned.metadata)
        verification_metadata.setdefault("verifier_version", returned.verifier)
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
                self._requirement_result(item) for item in returned.requirement_results
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
            metadata=verification_metadata,
            llm_usage=[],
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
            metadata={
                "normalized_without_verifier": True,
                "verifier_version": "controller_normalizer_v1",
                "failure_category": "no_change_failure",
                "verifier_retry_count": 0,
            },
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
            metadata={
                "exception_class": error.__class__.__name__,
                "verifier_version": "dependency_error_v1",
            },
        )

    def _append_approval_audit(
        self,
        runtime: _Runtime,
        *,
        action: str,
        actor: str,
        authenticated: bool,
        authentication_type: str,
        result: str,
        reason: str,
        candidate_id: str | None,
    ) -> None:
        append_jsonl_durable(
            runtime.store.run_directory / "approval-audit.jsonl",
            redact_data(
                {
                    "schema_version": "villani.approval_audit.v1",
                    "run_id": runtime.run_id,
                    "action": action,
                    "actor": actor,
                    "authenticated": authenticated,
                    "authentication_type": authentication_type,
                    "result": result,
                    "reason": reason,
                    "candidate_id": candidate_id,
                    "timestamp": self._now().isoformat(),
                }
            ),
        )

    def _approval_record(
        self,
        runtime: _Runtime,
        *,
        decision: Literal["approved", "denied"],
        actor: str,
        reason: str,
    ) -> ApprovalRecord:
        delivery = runtime.delivery
        if delivery is None:
            raise RunStoreError("approval state has no delivery record")
        candidate = next(
            item
            for item in runtime.attempts
            if item.attempt_id == delivery.selected_attempt_id
        )
        configuration = cast(dict[str, Any], runtime.request.policy_configuration)
        raw_policy = configuration.get("approval_policy")
        if isinstance(raw_policy, Mapping):
            policy_version = str(
                raw_policy.get("policy_version") or "villani.delivery.approval.v1"
            )
        else:
            policy_version = "villani.delivery.approval.v1"
            configuration["approval_policy"] = {
                "schema_version": "villani.approval_policy.v1",
                "policy_version": policy_version,
                "rules": [
                    {
                        "rule_id": "explicit_delivery_approval",
                        "materialization_types": [
                            materialization_type_for_mode(delivery.mode)
                        ],
                    }
                ],
            }
        now = self._now()
        expires_at = delivery.approval.deadline or now + timedelta(hours=1)
        record = ApprovalRecord(
            approval_id=self._id_factory("approval"),
            run_id=runtime.run_id,
            attempt_id=delivery.selected_attempt_id,
            approver_identity=actor,
            scope=ApprovalScope(
                repository=str(Path(runtime.request.repository_path).resolve()),
                paths=delivery.review.files_changed,
                tool_actions=(materialization_type_for_mode(delivery.mode),),
                materialization_type=materialization_type_for_mode(delivery.mode),
                maximum_cost_usd=candidate.cost_usd,
            ),
            decision=decision,
            reason=reason,
            issued_at=now,
            expires_at=expires_at,
            policy_version=policy_version,
        )
        records = configuration.get("approval_records")
        values = list(records) if isinstance(records, (list, tuple)) else []
        document = record.model_dump(mode="json")
        values.append(document)
        configuration["approval_records"] = values
        runtime.store.write_json(
            f"approval-records/{record.approval_id}.json", document
        )
        runtime.store.write_json("approval-records.json", values)
        return record

    def _approve_delivery(
        self,
        runtime: _Runtime,
        *,
        actor: str,
        authenticated: bool,
        authentication_type: str,
        reason: str,
    ) -> None:
        delivery = runtime.delivery
        selection = runtime.selection
        if delivery is None or selection is None:
            raise RunStoreError("approval state is incomplete")
        record = self._approval_record(
            runtime, decision="approved", actor=actor, reason=reason
        )
        approval = delivery.approval.model_copy(
            update={
                "status": "approved",
                "actor": actor,
                "authentication_type": authentication_type,
                "decided_at": self._now(),
                "reason": reason,
            }
        )
        authority = _mapping_copy(delivery.authority)
        authority["permitted"] = True
        authority["reasons"] = [
            "acceptance-grade evidence is present",
            "an in-scope explicit approval was persisted",
        ]
        approved = delivery.model_copy(
            update={
                "state": "approved",
                "updated_at": self._now(),
                "approval": approval,
                "authority": authority,
                "metadata": {
                    **delivery.metadata,
                    "approval_record": f"approval-records/{record.approval_id}.json",
                },
            }
        )
        self._persist_delivery(runtime, approved)
        self._append_approval_audit(
            runtime,
            action="approve",
            actor=actor,
            authenticated=authenticated,
            authentication_type=authentication_type,
            result="approved",
            reason=reason,
            candidate_id=approved.selected_attempt_id,
        )
        self._emit_state_event(
            runtime,
            "approval_granted",
            {
                "approval_id": record.approval_id,
                "selected_attempt_id": approved.selected_attempt_id,
                "authentication_type": authentication_type,
                "authenticated": authenticated,
            },
            attempt_id=approved.selected_attempt_id,
        )
        self._materialize_delivery_workflow(
            runtime,
            selection,
            self._selection_interface(selection),
            next(
                item
                for item in self._eligible_candidates(runtime)
                if item.attempt.attempt_id == approved.selected_attempt_id
            ),
            approved,
        )

    def _complete_without_materialization(
        self, runtime: _Runtime, delivery: DeliveryRecord, *, reason: str
    ) -> None:
        self._persist_delivery(runtime, delivery)
        self._emit_state_event(
            runtime,
            "delivery_completed",
            {
                "delivery_id": delivery.delivery_id,
                "delivery_mode": delivery.mode,
                "delivery_state": delivery.state,
                "repository_modified": False,
                "target_worktree_modified": False,
                "patch_preserved": True,
            },
            attempt_id=delivery.selected_attempt_id,
        )
        runtime.terminal_reason = reason
        self._transition(
            runtime,
            "COMPLETED",
            "run_completed",
            {
                **self._terminal_observability_payload(runtime, status="completed"),
                "changed_files": list(delivery.changed_files),
                "materialization_status": "not_materialized",
                "delivery_mode": delivery.mode,
                "delivery_state": delivery.state,
                "patch_preserved": True,
                "terminal_reason": reason,
            },
            attempt_id=delivery.selected_attempt_id,
        )

    def _decline_delivery(
        self,
        runtime: _Runtime,
        *,
        action: Literal["reject", "request_rerun"],
        actor: str,
        authenticated: bool,
        authentication_type: str,
        reason: str,
    ) -> None:
        delivery = runtime.delivery
        if delivery is None:
            raise RunStoreError("approval state has no delivery record")
        record = self._approval_record(
            runtime, decision="denied", actor=actor, reason=reason
        )
        approval_status = "rejected" if action == "reject" else "rerun_requested"
        delivery_state = "rejected" if action == "reject" else "rerun_requested"
        approval = delivery.approval.model_copy(
            update={
                "status": approval_status,
                "actor": actor,
                "authentication_type": authentication_type,
                "decided_at": self._now(),
                "reason": reason,
            }
        )
        completed = delivery.model_copy(
            update={
                "state": delivery_state,
                "updated_at": self._now(),
                "completed_at": self._now(),
                "approval": approval,
                "metadata": {
                    **delivery.metadata,
                    "approval_record": f"approval-records/{record.approval_id}.json",
                },
            }
        )
        self._append_approval_audit(
            runtime,
            action=action,
            actor=actor,
            authenticated=authenticated,
            authentication_type=authentication_type,
            result=approval_status,
            reason=reason,
            candidate_id=delivery.selected_attempt_id,
        )
        self._emit_state_event(
            runtime,
            "approval_rejected" if action == "reject" else "approval_rerun_requested",
            {
                "approval_id": record.approval_id,
                "selected_attempt_id": delivery.selected_attempt_id,
                "authenticated": authenticated,
                "authentication_type": authentication_type,
                "patch_preserved": True,
            },
            attempt_id=delivery.selected_attempt_id,
        )
        self._complete_without_materialization(
            runtime,
            completed,
            reason=(
                "accepted_patch_delivery_rejected"
                if action == "reject"
                else "accepted_patch_rerun_requested"
            ),
        )

    def _change_approval_candidate(
        self,
        runtime: _Runtime,
        *,
        candidate_id: str,
        actor: str,
        authenticated: bool,
        authentication_type: str,
        reason: str,
    ) -> None:
        delivery = runtime.delivery
        selection = runtime.selection
        if delivery is None or selection is None:
            raise RunStoreError("approval state is incomplete")
        if not delivery.approval.allow_candidate_change:
            raise PermissionError(
                "active delivery policy does not permit candidate choice"
            )
        if candidate_id not in runtime.eligible_candidate_ids:
            raise ValueError("candidate is not acceptance eligible")
        if candidate_id == delivery.selected_attempt_id:
            return
        history_root = runtime.store.run_directory / "selection-history"
        existing = (
            list(history_root.glob("selection_*.json")) if history_root.is_dir() else []
        )
        runtime.store.write_protocol(
            f"selection-history/{selection.selection_id}.json", selection
        )
        selection_id = f"selection_{len(existing) + 2:03d}"
        metadata = _mapping_copy(selection.metadata)
        metadata.update(
            {
                "previous_selection_id": selection.selection_id,
                "previous_selected_attempt_id": delivery.selected_attempt_id,
                "selection_changed_by_approval": True,
            }
        )
        updated_selection = selection.model_copy(
            update={
                "selection_id": selection_id,
                "selected_at": self._now(),
                "selected_candidate_ids": [candidate_id],
                "reason": "An explicit approver selected another acceptance-eligible candidate.",
                "metadata": metadata,
            }
        )
        runtime.store.write_protocol("selection.json", updated_selection)
        runtime.selection = updated_selection
        runtime.selected_attempt_id = candidate_id
        candidate = next(
            item
            for item in self._eligible_candidates(runtime)
            if item.attempt.attempt_id == candidate_id
        )
        replacement = self._new_delivery_record(
            runtime, updated_selection, candidate
        ).model_copy(
            update={
                "state": "awaiting_approval",
                "requested_at": delivery.requested_at,
                "approval": delivery.approval,
                "metadata": {
                    **delivery.metadata,
                    "previous_patch_sha256": delivery.patch_sha256,
                },
            }
        )
        self._persist_delivery(runtime, replacement)
        self._label_unselected_accepted_candidates(runtime)
        self._append_approval_audit(
            runtime,
            action="choose_candidate",
            actor=actor,
            authenticated=authenticated,
            authentication_type=authentication_type,
            result="selected",
            reason=reason,
            candidate_id=candidate_id,
        )
        self._emit_state_event(
            runtime,
            "approval_candidate_changed",
            {
                "previous_selected_attempt_id": delivery.selected_attempt_id,
                "selected_attempt_id": candidate_id,
                "selection_id": selection_id,
                "authenticated": authenticated,
                "authentication_type": authentication_type,
            },
            attempt_id=candidate_id,
        )

    def _resume_awaiting_approval(self, runtime: _Runtime) -> None:
        delivery = runtime.delivery
        selection = runtime.selection
        if delivery is None or selection is None:
            raise RunStoreError("AWAITING_APPROVAL has no persisted delivery selection")
        deadline = delivery.approval.deadline
        if deadline is None or self._now() < deadline:
            return
        approval = delivery.approval.model_copy(
            update={
                "status": "timed_out",
                "actor": "villani-timeout-policy",
                "authentication_type": "controller_policy",
                "decided_at": self._now(),
                "reason": f"approval deadline elapsed; policy={delivery.approval.timeout_policy}",
            }
        )
        self._append_approval_audit(
            runtime,
            action="timeout",
            actor="villani-timeout-policy",
            authenticated=True,
            authentication_type="controller_policy",
            result=delivery.approval.timeout_policy,
            reason="persisted approval deadline elapsed",
            candidate_id=delivery.selected_attempt_id,
        )
        self._emit_state_event(
            runtime,
            "approval_timed_out",
            {
                "selected_attempt_id": delivery.selected_attempt_id,
                "timeout_policy": delivery.approval.timeout_policy,
                "patch_preserved": True,
            },
            attempt_id=delivery.selected_attempt_id,
        )
        if delivery.approval.timeout_policy == "suggest":
            configuration = cast(dict[str, Any], runtime.request.policy_configuration)
            raw_delivery = configuration.setdefault("delivery", {})
            if not isinstance(raw_delivery, dict):
                raise RunStoreError("persisted delivery configuration is invalid")
            raw_delivery["mode"] = "suggest"
            raw_delivery["materialization_type"] = "patch_export"
            suggested = delivery.model_copy(
                update={
                    "mode": "suggest",
                    "state": "timed_out",
                    "updated_at": self._now(),
                    "approval": approval,
                    "authority": {
                        "policy_version": "approval_timeout_policy",
                        "required": "no repository mutation",
                        "observed": "accepted patch preserved",
                        "permitted": True,
                        "reasons": ["timeout policy permits suggestion only"],
                    },
                    "metadata": {
                        **delivery.metadata,
                        "requested_mode": "approve",
                        "materialization_type": "patch_export",
                    },
                }
            )
            self._persist_delivery(runtime, suggested)
            self._materialize_delivery_workflow(
                runtime,
                selection,
                self._selection_interface(selection),
                next(
                    item
                    for item in self._eligible_candidates(runtime)
                    if item.attempt.attempt_id == suggested.selected_attempt_id
                ),
                suggested,
            )
            return
        if delivery.approval.timeout_policy == "fail":
            failure = FailureDetail(
                code="approval_timeout",
                message="explicit delivery approval timed out",
                details={"patch_preserved": True},
            )
            self._persist_delivery(
                runtime,
                delivery.model_copy(
                    update={
                        "state": "failed",
                        "updated_at": self._now(),
                        "completed_at": self._now(),
                        "approval": approval,
                        "failure": failure,
                    }
                ),
            )
            self._fail(runtime, failure.code, failure.message)
            return
        timed_out = delivery.model_copy(
            update={
                "state": "timed_out",
                "updated_at": self._now(),
                "completed_at": self._now(),
                "approval": approval,
            }
        )
        self._complete_without_materialization(
            runtime, timed_out, reason="accepted_patch_approval_timed_out"
        )

    def _selected_verification(
        self, runtime: _Runtime, attempt_id: str
    ) -> VerificationSnapshot:
        try:
            return next(
                item for item in runtime.verifications if item.attempt_id == attempt_id
            )
        except StopIteration as error:
            raise RunStoreError(
                "selected candidate has no persisted verification"
            ) from error

    def _persist_delivery(self, runtime: _Runtime, delivery: DeliveryRecord) -> None:
        runtime.store.write_json(
            "delivery.json", redact_data(delivery.model_dump(mode="json"))
        )
        runtime.delivery = delivery

    def _new_delivery_record(
        self,
        runtime: _Runtime,
        selection: SelectionSnapshot,
        candidate: EligibleCandidate,
    ) -> DeliveryRecord:
        patch = candidate.patch
        digest = patch_digest(patch)
        if not patch or digest != candidate.attempt.patch_sha256:
            raise RunStoreError("selected patch no longer matches its recorded digest")
        runtime.store.write_text("delivery/selected.patch", patch)
        verification = self._selected_verification(
            runtime, candidate.attempt.attempt_id
        )
        total_cost, accounting_status = self._actual_cost(runtime)
        currency = self._stage_metrics(runtime)["total"].currency
        review = build_patch_review(
            attempt=candidate.attempt,
            verification=verification,
            selection=selection,
            patch=patch,
            total_cost=total_cost,
            accounting_status=accounting_status,
            currency=currency,
        )
        configuration = delivery_configuration(runtime.request.policy_configuration)
        mode = configured_delivery_mode(runtime.request.policy_configuration)
        now = self._now()
        approval_configuration = configuration.get("approval")
        approval_values = (
            dict(approval_configuration)
            if isinstance(approval_configuration, Mapping)
            else {}
        )
        try:
            timeout_seconds = max(
                int(approval_values.get("timeout_seconds", 24 * 60 * 60)), 0
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "delivery approval timeout_seconds must be an integer"
            ) from error
        timeout_policy = str(approval_values.get("timeout_policy") or "reject")
        if timeout_policy not in {"reject", "suggest", "fail"}:
            raise ValueError("delivery approval timeout_policy is invalid")
        approval_required = mode == "approve"
        approval = ApprovalWorkflow(
            required=approval_required,
            status="pending" if approval_required else "not_required",
            request_id="approval_request_001" if approval_required else None,
            requested_at=now if approval_required else None,
            deadline=(now + timedelta(seconds=timeout_seconds))
            if approval_required
            else None,
            timeout_policy=cast(Literal["reject", "suggest", "fail"], timeout_policy),
            authenticated_required=bool(
                approval_values.get("authenticated_required", False)
            ),
            allow_candidate_change=bool(
                approval_values.get("allow_candidate_change", False)
            ),
        )
        if mode == "suggest":
            authority = {
                "policy_version": str(
                    _mapping_copy(configuration).get("authority_policy_version")
                    or "not_required"
                ),
                "required": "no repository-mutation authority is required",
                "observed": "acceptance-grade selection",
                "permitted": True,
                "reasons": ["suggest mode never mutates the target repository"],
            }
        elif mode == "approve":
            authority = {
                "policy_version": str(
                    approval_values.get("policy_version")
                    or "villani.delivery.approval.v1"
                ),
                "required": "acceptance-grade evidence and explicit approval",
                "observed": str(
                    verification.metadata.get("authority_source")
                    or verification.metadata.get("verification_mode")
                    or "normalized_verifier"
                ),
                "permitted": False,
                "reasons": ["explicit approval is pending"],
            }
        else:
            authority = automatic_authority(
                runtime.request.policy_configuration,
                verification,
                risk=runtime.classification.risk if runtime.classification else None,
            )
        delivery = DeliveryRecord(
            delivery_id="delivery_001",
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            selection_id=selection.selection_id,
            selected_attempt_id=candidate.attempt.attempt_id,
            mode=mode,
            state="selected",
            requested_at=now,
            updated_at=now,
            repository_path=str(Path(runtime.request.repository_path).resolve()),
            patch_artifact="delivery/selected.patch",
            patch_sha256=digest,
            changed_files=review.files_changed,
            review=review,
            authority=_mapping_copy(authority),
            approval=approval,
            metadata={
                "materialization_type": materialization_type_for_mode(mode),
                "commit_requested": bool(configuration.get("commit", False)),
            },
        )
        self._persist_delivery(runtime, delivery)
        return delivery

    def _await_delivery_approval(
        self, runtime: _Runtime, delivery: DeliveryRecord
    ) -> None:
        approval = delivery.approval
        waiting = delivery.model_copy(
            update={
                "state": "awaiting_approval",
                "updated_at": self._now(),
            }
        )
        self._persist_delivery(runtime, waiting)
        self._transition(
            runtime,
            "AWAITING_APPROVAL",
            "approval_requested",
            {
                "delivery_id": waiting.delivery_id,
                "selected_attempt_id": waiting.selected_attempt_id,
                "deadline": (
                    approval.deadline.isoformat() if approval.deadline else None
                ),
                "timeout_policy": approval.timeout_policy,
                "files_changed": list(waiting.review.files_changed),
                "insertions": waiting.review.insertions,
                "deletions": waiting.review.deletions,
                "verifier_authority": waiting.review.verifier_authority,
                "remaining_risks": list(waiting.review.remaining_risks),
                "sensitive_file_warnings": list(waiting.review.sensitive_file_warnings),
                "unrelated_change_warnings": list(
                    waiting.review.unrelated_change_warnings
                ),
            },
            attempt_id=waiting.selected_attempt_id,
        )

    def _continue_delivery_workflow(
        self,
        runtime: _Runtime,
        selection: SelectionSnapshot,
        returned_selection: Selection,
        candidates: tuple[EligibleCandidate, ...],
    ) -> None:
        selected_id = selection.selected_candidate_ids[0]
        candidate = next(
            item for item in candidates if item.attempt.attempt_id == selected_id
        )
        delivery = runtime.delivery
        if delivery is None or delivery.selected_attempt_id != selected_id:
            delivery = self._new_delivery_record(runtime, selection, candidate)
        if delivery.mode == "approve" and delivery.approval.status != "approved":
            if runtime.machine.state != "AWAITING_APPROVAL":
                self._await_delivery_approval(runtime, delivery)
            return
        if delivery.mode not in {"suggest", "approve"} and not bool(
            delivery.authority.get("permitted")
        ):
            reason = (
                "; ".join(str(item) for item in delivery.authority.get("reasons", []))
                or "automatic delivery authority is insufficient"
            )
            failure = FailureDetail(
                code="delivery_authority_insufficient",
                message=reason,
                details={"patch_preserved": True},
            )
            self._persist_delivery(
                runtime,
                delivery.model_copy(
                    update={
                        "state": "failed",
                        "updated_at": self._now(),
                        "completed_at": self._now(),
                        "failure": failure,
                    }
                ),
            )
            self._fail(runtime, failure.code, failure.message)
            return
        self._materialize_delivery_workflow(
            runtime, selection, returned_selection, candidate, delivery
        )

    def _materialize_delivery_workflow(
        self,
        runtime: _Runtime,
        selection: SelectionSnapshot,
        returned_selection: Selection,
        candidate: EligibleCandidate,
        delivery: DeliveryRecord,
        *,
        already_started: bool = False,
    ) -> None:
        if already_started:
            started = next(
                (
                    event
                    for event in reversed(runtime.committed_events)
                    if event.event_type == "materialization_started"
                ),
                None,
            )
            if started is None:
                raise RunStoreError(
                    "delivery recovery has no materialization start event"
                )
        else:
            started = self._transition(
                runtime,
                "MATERIALIZING",
                "materialization_started",
                {
                    "selection_id": selection.selection_id,
                    "selected_attempt_id": candidate.attempt.attempt_id,
                    "delivery_mode": delivery.mode,
                    "delivery_id": delivery.delivery_id,
                },
                attempt_id=candidate.attempt.attempt_id,
            )
            self._checkpoint("after_materialization_start")
        context = MaterializationContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            repository_path=str(runtime.request.repository_path),
            selected_candidate=candidate,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
            run_directory=runtime.store.run_directory,
            risk=runtime.classification.risk if runtime.classification else None,
        )
        try:
            returned = self._materializer.materialize(returned_selection, context)
            self._checkpoint("after_materializer_return")
            if not isinstance(returned, Materialization):
                raise TypeError("materializer returned an invalid Materialization")
            materialization = self._persist_materialization(
                runtime, selection, candidate, returned, started.timestamp
            )
        except Exception as error:
            failure = FailureDetail(
                code="delivery_failed",
                message=redact_message(str(error)),
                details={"patch_preserved": True},
            )
            self._persist_delivery(
                runtime,
                delivery.model_copy(
                    update={
                        "state": "failed",
                        "updated_at": self._now(),
                        "completed_at": self._now(),
                        "failure": failure,
                    }
                ),
            )
            self._emit_failure_event(
                runtime, "materialization_failed", error, "delivery"
            )
            self._fail(runtime, failure.code, failure.message, error=error)
            return
        if materialization.status != "succeeded":
            failure = materialization.failure or FailureDetail(
                code="delivery_failed",
                message="delivery reported failure",
                details={"patch_preserved": True},
            )
            details = _mapping_copy(failure.details)
            details["patch_preserved"] = True
            failure = failure.model_copy(update={"details": details})
            self._persist_delivery(
                runtime,
                delivery.model_copy(
                    update={
                        "state": "failed",
                        "updated_at": self._now(),
                        "completed_at": self._now(),
                        "failure": failure,
                    }
                ),
            )
            self._emit_state_event(
                runtime,
                "materialization_failed",
                {"code": failure.code, "message": failure.message},
                attempt_id=candidate.attempt.attempt_id,
            )
            self._fail(runtime, failure.code, failure.message)
            return
        self._finish_delivery_materialization(
            runtime, delivery, materialization, candidate, recovered=False
        )

    def _finish_delivery_materialization(
        self,
        runtime: _Runtime,
        delivery: DeliveryRecord,
        materialization: MaterializationSnapshot,
        candidate: EligibleCandidate,
        *,
        recovered: bool,
    ) -> None:
        """Commit the user-facing delivery result after a durable snapshot exists."""

        if materialization.status != "succeeded":
            failure = materialization.failure or FailureDetail(
                code="delivery_failed",
                message="delivery reported failure",
                details={"patch_preserved": True},
            )
            details = _mapping_copy(failure.details)
            details["patch_preserved"] = True
            failure = failure.model_copy(update={"details": details})
            self._persist_delivery(
                runtime,
                delivery.model_copy(
                    update={
                        "state": "failed",
                        "updated_at": self._now(),
                        "completed_at": self._now(),
                        "failure": failure,
                    }
                ),
            )
            if not self._has_event(runtime, "materialization_failed"):
                self._emit_state_event(
                    runtime,
                    "materialization_failed",
                    {
                        "code": failure.code,
                        "message": failure.message,
                        "recovered": recovered,
                    },
                    attempt_id=candidate.attempt.attempt_id,
                )
            self._fail(runtime, failure.code, failure.message)
            return

        state = successful_delivery_state(delivery.mode)
        complete_delivery = delivery.model_copy(
            update={
                "state": state,
                "updated_at": self._now(),
                "completed_at": self._now(),
                "repository_modified": delivery.mode != "suggest",
                "target_worktree_modified": delivery.mode in {"apply", "approve"},
                "result": _mapping_copy(materialization.metadata),
                "failure": None,
            }
        )
        self._persist_delivery(runtime, complete_delivery)
        if not self._has_event(runtime, "delivery_completed"):
            self._emit_state_event(
                runtime,
                "delivery_completed",
                {
                    "delivery_id": complete_delivery.delivery_id,
                    "delivery_mode": complete_delivery.mode,
                    "delivery_state": complete_delivery.state,
                    "repository_modified": complete_delivery.repository_modified,
                    "target_worktree_modified": (
                        complete_delivery.target_worktree_modified
                    ),
                    "patch_preserved": True,
                    "recovered": recovered,
                },
                attempt_id=candidate.attempt.attempt_id,
            )
        if not self._has_event(runtime, "materialization_completed"):
            self._emit_state_event(
                runtime,
                "materialization_completed",
                {
                    "materialization_id": materialization.materialization_id,
                    "selected_attempt_id": materialization.selected_attempt_id,
                    "materialization_status": materialization.status,
                    "changed_files": materialization.changed_files,
                    "patch_digest": materialization.patch_sha256,
                    "delivery_state": complete_delivery.state,
                    "recovered": recovered,
                },
                attempt_id=candidate.attempt.attempt_id,
            )
        runtime.terminal_reason = f"accepted_and_{complete_delivery.state}"
        if runtime.machine.state == "MATERIALIZING":
            self._transition(
                runtime,
                "COMPLETED",
                "run_completed",
                {
                    **self._terminal_observability_payload(runtime, status="completed"),
                    "delivery_mode": complete_delivery.mode,
                    "delivery_state": complete_delivery.state,
                    "patch_preserved": True,
                    "recovered": recovered,
                    "terminal_reason": runtime.terminal_reason,
                },
                attempt_id=candidate.attempt.attempt_id,
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
            runtime.selection = selection
            self._label_unselected_accepted_candidates(runtime)
            self._checkpoint("after_selection_snapshot")
            self._emit_state_event(
                runtime,
                "candidate_selected",
                {
                    "selection_id": selection.selection_id,
                    "selected_attempt_id": returned.selected_attempt_id,
                    "eligible_candidate_ids": selection.eligible_candidate_ids,
                    "selection_strategy": selection.strategy,
                    "selection_reason": selection.reason,
                    "rankings": [
                        item.model_dump(mode="json") for item in selection.rankings
                    ],
                },
            )
        except Exception as error:
            self._emit_failure_event(runtime, "selection_failed", error, "selection")
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
        if workflow_enabled(runtime.request.policy_configuration):
            self._continue_delivery_workflow(runtime, selection, returned, candidates)
            return
        materialization_started = self._transition(
            runtime,
            "MATERIALIZING",
            "materialization_started",
            {
                "selection_id": selection.selection_id,
                "selected_attempt_id": runtime.selected_attempt_id,
            },
        )
        self._checkpoint("after_materialization_start")
        materialization_context = MaterializationContext(
            run_id=runtime.run_id,
            trace_id=runtime.trace_id,
            repository_path=str(runtime.request.repository_path),
            selected_candidate=selected_candidate,
            policy_configuration=_read_only_mapping(
                runtime.request.policy_configuration
            ),
            run_directory=runtime.store.run_directory,
            risk=runtime.classification.risk if runtime.classification else None,
        )
        try:
            returned_materialization = self._materializer.materialize(
                returned, materialization_context
            )
            self._checkpoint("after_materializer_return")
            if not isinstance(returned_materialization, Materialization):
                raise TypeError("materializer returned an invalid Materialization")
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
            {
                "materialization_id": materialization.materialization_id,
                "selected_attempt_id": materialization.selected_attempt_id,
                "materialization_status": materialization.status,
                "changed_files": materialization.changed_files,
                "patch_digest": materialization.patch_sha256,
            },
        )
        stage_metrics = self._stage_metrics(runtime)
        total = stage_metrics["total"]
        # Public "coding" cost is the non-verifier product path: task
        # classification plus candidate execution.  This keeps the public
        # invariant total = coding + verifier without hiding classifier spend.
        coding = self._aggregate_stage(
            "coding",
            [stage_metrics["classification"], stage_metrics["coding"]],
            total.currency,
        )
        verification = stage_metrics["verification"]
        selected_attempt = next(
            item
            for item in runtime.attempts
            if item.attempt_id == runtime.selected_attempt_id
        )
        self._transition(
            runtime,
            "COMPLETED",
            "run_completed",
            {
                "status": "completed",
                "selected_attempt_id": runtime.selected_attempt_id,
                "selected_backend": selected_attempt.backend_name,
                "selected_model": selected_attempt.model,
                "attempt_count": len(runtime.attempts),
                "escalation_count": sum(
                    event.event_type == "escalation_selected"
                    for event in runtime.committed_events
                ),
                "input_tokens": total.input_tokens,
                "output_tokens": total.output_tokens,
                "total_tokens": (
                    total.input_tokens + total.output_tokens
                    if total.input_tokens is not None
                    and total.output_tokens is not None
                    else None
                ),
                "token_accounting_status": total.token_accounting_status,
                "coding_cost_usd": coding.cost,
                "verifier_cost_usd": verification.cost,
                "total_cost_usd": total.cost,
                "cost_accounting_status": total.cost_accounting_status,
                "duration_ms": max(
                    runtime.wall_clock_offset_ms
                    + int((self._monotonic() - runtime.started_monotonic) * 1000),
                    0,
                ),
                "changed_files": materialization.changed_files,
                "file_write_count": sum(
                    int(
                        item.metadata.get(
                            "total_file_writes",
                            runtime.attempt_results[
                                item.attempt_id
                            ].runner_telemetry.get("total_file_writes", 0),
                        )
                        or 0
                    )
                    for item in runtime.attempts
                ),
                "verification_status": next(
                    item.outcome
                    for item in runtime.verifications
                    if item.attempt_id == runtime.selected_attempt_id
                ),
                "materialization_status": materialization.status,
                "terminal_reason": runtime.terminal_reason
                or "accepted_and_materialized",
            },
        )

    def _label_unselected_accepted_candidates(self, runtime: _Runtime) -> None:
        """Persist the only clean-success exception for unmaterialized candidates."""

        policy_values = runtime.request.policy_configuration.get("policy")
        values = (
            policy_values
            if isinstance(policy_values, Mapping)
            else runtime.request.policy_configuration
        )
        try:
            required = int(values.get("accepted_candidates_required", 1))
        except (TypeError, ValueError):
            required = 1
        if required <= 1 or runtime.selected_attempt_id is None:
            return
        unselected = {
            verification.attempt_id
            for verification in runtime.verifications
            if verification.acceptance_eligible
            and verification.attempt_id != runtime.selected_attempt_id
        }
        for attempt_id in sorted(unselected):
            for index, attempt in enumerate(runtime.attempts):
                if attempt.attempt_id != attempt_id:
                    continue
                metadata = _mapping_copy(attempt.metadata)
                metadata["capability_outcome_label"] = "accepted_not_selected"
                updated_attempt = attempt.model_copy(update={"metadata": metadata})
                runtime.attempts[index] = updated_attempt
                runtime.store.write_protocol(
                    f"attempts/{attempt_id}/attempt.json", updated_attempt
                )
                break
            for index, verification in enumerate(runtime.verifications):
                if verification.attempt_id != attempt_id:
                    continue
                metadata = _mapping_copy(verification.metadata)
                metadata["capability_outcome_label"] = "accepted_not_selected"
                updated_verification = verification.model_copy(
                    update={"metadata": metadata}
                )
                runtime.verifications[index] = updated_verification
                runtime.store.write_protocol(
                    f"verification/{attempt_id}.json", updated_verification
                )
                break

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
            raise RuntimeError(f"selection cannot start from {runtime.machine.state}")

    def _eligible_candidates(self, runtime: _Runtime) -> tuple[EligibleCandidate, ...]:
        candidates: list[EligibleCandidate] = []
        for attempt_id in runtime.eligible_candidate_ids:
            attempt = next(
                item for item in runtime.attempts if item.attempt_id == attempt_id
            )
            verification = next(
                item for item in runtime.verifications if item.attempt_id == attempt_id
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
        runtime.materialization = snapshot
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
                            evidence.evidence_id for evidence in item.success_evidence
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
        known_cost, actual_cost_status = self._actual_cost(runtime)
        stages = self._stage_metrics(runtime)
        no_spend_bearing_stage = all(
            stages[name].cost_accounting_status == "not_applicable"
            for name in ("classification", "coding", "verification")
        )
        elapsed = max(
            runtime.wall_clock_offset_ms
            + int((self._monotonic() - runtime.started_monotonic) * 1000),
            0,
        )
        if runtime.request.max_cost is None:
            remaining_cost = None
            cost_status = "not_applicable"
        elif (
            runtime.classification is None
            and not runtime.attempts
            and not runtime.verifications
        ):
            remaining_cost = runtime.request.max_cost
            cost_status = "complete"
        else:
            if no_spend_bearing_stage:
                # No spend-bearing stage has run. Keep the observable monetary
                # total unknown/not-applicable, but do not poison a cost cap
                # before a known-price coding attempt can start.
                remaining_cost = runtime.request.max_cost
                cost_status = "complete"
            elif actual_cost_status == "complete" and known_cost is not None:
                remaining_cost = max(runtime.request.max_cost - known_cost, 0.0)
                cost_status = "complete"
            else:
                remaining_cost = None
                cost_status = "unknown"

        if runtime.request.max_wall_time is None:
            remaining_wall_time_ms = None
            duration_status = "not_applicable"
        else:
            remaining_wall_time_ms = max(
                int(runtime.request.max_wall_time * 1000) - elapsed, 0
            )
            duration_status = "complete"
        return BudgetContext(
            remaining_attempts=remaining_attempts,
            remaining_cost_usd=remaining_cost,
            cost_accounting_status=cast(AccountingStatus, cost_status),
            remaining_wall_time_ms=remaining_wall_time_ms,
            duration_accounting_status=cast(AccountingStatus, duration_status),
            actual_attempts_used=len(runtime.attempts),
            actual_cost_consumed_usd=known_cost,
            actual_cost_accounting_status=cast(AccountingStatus, actual_cost_status),
            actual_wall_time_ms=elapsed,
            actual_stage_attempts_used=(
                len(runtime.attempts)
                + (
                    len(runtime.classification.llm_usage)
                    if runtime.classification
                    else 0
                )
                + sum(len(item.llm_usage) for item in runtime.verifications)
            ),
        )

    def _budget_after_decision(
        self, before: BudgetContext, decision: PolicyDecision
    ) -> BudgetContext:
        if decision.action not in {"attempt", "retry", "escalate"}:
            return before
        if decision.metadata.get("retry_scope") == "verification":
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
                    (before.remaining_cost_usd or 0.0) - option.estimated_cost_usd,
                    0.0,
                )
        return BudgetContext(
            remaining_attempts=max(before.remaining_attempts - 1, 0),
            remaining_cost_usd=remaining_cost,
            cost_accounting_status=cost_status,
            remaining_wall_time_ms=before.remaining_wall_time_ms,
            duration_accounting_status=before.duration_accounting_status,
            actual_attempts_used=before.actual_attempts_used,
            actual_cost_consumed_usd=before.actual_cost_consumed_usd,
            actual_cost_accounting_status=before.actual_cost_accounting_status,
            actual_wall_time_ms=before.actual_wall_time_ms,
            actual_stage_attempts_used=before.actual_stage_attempts_used,
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
                return (
                    "cost budget cannot permit an attempt with unknown estimated cost"
                )
            if option.estimated_cost_usd > (budget.remaining_cost_usd or 0.0):
                return "cost budget exhausted before unaffordable attempt"
            verifier_cost, verifier_status = self._projected_verification_cost(runtime)
            if verifier_status != "complete" or verifier_cost is None:
                return "cost budget cannot permit an attempt with unknown projected verification spend"
            if option.estimated_cost_usd + verifier_cost > (
                budget.remaining_cost_usd or 0.0
            ):
                return "cost budget exhausted before coding and verification projected spend"
        return None

    def _projected_verification_cost(
        self, runtime: _Runtime
    ) -> tuple[float | None, str]:
        verifier = runtime.request.policy_configuration.get("verifier")
        settings = verifier if isinstance(verifier, Mapping) else {}
        if bool(settings.get("no_llm", True)):
            return 0.0, "complete"
        backend_name = settings.get("backend")
        backend = configured_backends(runtime.request.policy_configuration).get(
            str(backend_name)
        )
        if backend is None:
            return None, "unknown"
        estimate = estimate_attempt_cost(backend)
        if estimate.total is None:
            return None, estimate.accounting_status
        policy = runtime.request.policy_configuration.get("policy")
        values = (
            policy
            if isinstance(policy, Mapping)
            else runtime.request.policy_configuration
        )
        retry_limit = 0
        if values.get("version") == "bootstrap_v1":
            try:
                retry_limit = max(
                    0,
                    int(
                        BootstrapPolicyConfiguration.model_validate(
                            values
                        ).verifier_retry_limit
                    ),
                )
            except (TypeError, ValueError):
                return None, "unknown"
        # The verifier is allowed to retry without rerunning coding.  Reserve
        # the full configured worst case so a cost cap cannot be exceeded by a
        # transient verifier failure.
        return estimate.total * (retry_limit + 1), estimate.accounting_status

    def _projected_classification_cost(
        self, runtime: _Runtime
    ) -> tuple[float | None, str]:
        backend = runtime.classification_backend
        if backend is None:
            return 0.0, "complete"
        configured = configured_backends(runtime.request.policy_configuration)
        policy = runtime.request.policy_configuration.get("policy")
        values = (
            policy
            if isinstance(policy, Mapping)
            else runtime.request.policy_configuration
        )
        try:
            retry_limit = max(0, int(values.get("classifier_retry_limit", 1)))
        except (TypeError, ValueError):
            return None, "unknown"
        names = [backend.name]
        fallback_names = values.get("classifier_fallback_backends")
        if isinstance(fallback_names, list):
            names.extend(
                str(name)
                for name in fallback_names
                if str(name) not in names
                and str(name) in configured
                and configured[str(name)].enabled
                and "classification" in configured[str(name)].roles
            )
        total = 0.0
        for name in names:
            candidate = configured.get(name, backend if name == backend.name else None)
            if candidate is None:
                return None, "unknown"
            estimate = estimate_attempt_cost(candidate)
            if estimate.total is None or estimate.accounting_status != "complete":
                return None, estimate.accounting_status
            total += estimate.total * (retry_limit + 1)
        return total, "complete"

    def _chosen_backend_option(self, decision: PolicyDecision) -> BackendOption | None:
        return next(
            (
                item
                for item in decision.considered_backends
                if item.backend_name == decision.chosen_backend
            ),
            None,
        )

    def _actual_cost(self, runtime: _Runtime) -> tuple[float | None, AccountingStatus]:
        if (
            runtime.classification is None
            and not runtime.attempts
            and not runtime.verifications
        ):
            return None, "unknown"
        total = self._stage_metrics(runtime).get("total")
        if total is None:
            return None, "unknown"
        if total.cost_accounting_status == "not_applicable":
            return None, "unknown"
        return total.cost, total.cost_accounting_status

    @staticmethod
    def _aggregate_stage(
        stage: str, usages: list[StageUsage], currency: str
    ) -> StageUsage:
        if not usages:
            return StageUsage(
                stage=stage,  # type: ignore[arg-type]
                token_accounting_status="not_applicable",
                model_call_accounting_status="not_applicable",
                cost_accounting_status="not_applicable",
                duration_accounting_status="not_applicable",
                currency=currency,
            )

        def total_for(name: str, status_name: str) -> tuple[int | float | None, str]:
            values = [getattr(item, name) for item in usages]
            statuses = [getattr(item, status_name) for item in usages]
            active = [
                (value, status)
                for value, status in zip(values, statuses)
                if status != "not_applicable"
            ]
            if not active:
                return None, "not_applicable"
            active_values = [value for value, _status in active]
            known = [value for value in active_values if value is not None]
            if all(status == "complete" for _value, status in active) and len(
                known
            ) == len(active_values):
                return sum(known), "complete"
            if known:
                return sum(known), "partial"
            return None, "unknown"

        input_tokens, input_status = total_for(
            "input_tokens", "token_accounting_status"
        )
        output_tokens, output_status = total_for(
            "output_tokens", "token_accounting_status"
        )
        total_tokens = (
            int(input_tokens) + int(output_tokens)
            if input_tokens is not None and output_tokens is not None
            else None
        )
        token_status = (
            "complete"
            if input_status == output_status == "complete"
            else "partial"
            if input_tokens is not None or output_tokens is not None
            else "not_applicable"
            if input_status == output_status == "not_applicable"
            else "unknown"
        )
        model_calls, model_status = total_for(
            "model_calls", "model_call_accounting_status"
        )
        cost, cost_status = total_for("cost", "cost_accounting_status")
        duration, duration_status = total_for(
            "duration_ms", "duration_accounting_status"
        )
        return StageUsage(
            stage=stage,  # type: ignore[arg-type]
            input_tokens=int(input_tokens) if input_tokens is not None else None,
            output_tokens=int(output_tokens) if output_tokens is not None else None,
            total_tokens=total_tokens,
            token_accounting_status=token_status,  # type: ignore[arg-type]
            model_calls=int(model_calls) if model_calls is not None else None,
            model_call_accounting_status=model_status,  # type: ignore[arg-type]
            cost=float(cost) if cost is not None else None,
            cost_accounting_status=cost_status,  # type: ignore[arg-type]
            currency=currency,
            duration_ms=int(duration) if duration is not None else None,
            duration_accounting_status=duration_status,  # type: ignore[arg-type]
            failure_state=(
                "failed"
                if all(item.failure_state == "failed" for item in usages)
                else "succeeded"
                if any(item.failure_state == "succeeded" for item in usages)
                else "unknown"
            ),
        )

    def _stage_metrics(self, runtime: _Runtime) -> dict[str, StageUsage]:
        configured = configured_backends(runtime.request.policy_configuration)
        currency = next(
            (
                item.currency
                for item in configured.values()
                if item.enabled
                and ("classification" in item.roles or "coding" in item.roles)
            ),
            "USD",
        )
        classification = (
            list(runtime.classification.llm_usage) if runtime.classification else []
        )
        verification = [
            usage for snapshot in runtime.verifications for usage in snapshot.llm_usage
        ]
        # Existing bundles may not have a runner model-call counter, so keep
        # their coding usage readable with explicit unknown accounting.
        coding: list[StageUsage] = []
        for attempt in runtime.attempts:
            metrics_value = attempt.metadata.get("runner_metrics")
            metrics = metrics_value if isinstance(metrics_value, Mapping) else {}
            calls_value = metrics.get("model_requests")
            calls = (
                int(calls_value)
                if isinstance(calls_value, int) and calls_value >= 0
                else None
            )
            backend = configured.get(attempt.backend_name)
            coding.append(
                StageUsage(
                    stage="coding",
                    backend=attempt.backend_name,
                    model=attempt.model,
                    input_tokens=attempt.input_tokens,
                    output_tokens=attempt.output_tokens,
                    total_tokens=(
                        attempt.input_tokens + attempt.output_tokens
                        if attempt.input_tokens is not None
                        and attempt.output_tokens is not None
                        else None
                    ),
                    token_accounting_status=attempt.token_accounting_status,
                    model_calls=calls,
                    model_call_accounting_status="complete"
                    if calls is not None
                    else "unknown",
                    cost=attempt.cost_usd,
                    cost_accounting_status=attempt.cost_accounting_status,
                    currency=backend.currency if backend else currency,
                    duration_ms=attempt.duration_ms,
                    duration_accounting_status=attempt.duration_accounting_status,
                    failure_state="succeeded"
                    if attempt.status == "completed"
                    else "failed",
                )
            )
        stages = {
            "classification": self._aggregate_stage(
                "classification", classification, currency
            ),
            "coding": self._aggregate_stage("coding", coding, currency),
            "verification": self._aggregate_stage(
                "verification", verification, currency
            ),
            "selection": self._aggregate_stage("selection", [], currency),
            "materialization": self._aggregate_stage("materialization", [], currency),
        }
        included = [
            stages[name]
            for name in ("classification", "coding", "verification")
            if stages[name].cost_accounting_status != "not_applicable"
            or stages[name].duration_accounting_status != "not_applicable"
        ]
        stages["total"] = self._aggregate_stage("total", included, currency)
        return stages

    def _accounting_total(
        self,
        runtime: _Runtime,
        value_names: tuple[str, ...],
        status_name: str,
    ) -> tuple[list[int | None], str]:
        if not runtime.attempts:
            return [None for _ in value_names], "unknown"
        values_by_name = [
            [getattr(item, name) for item in runtime.attempts] for name in value_names
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
        stage_metrics = self._stage_metrics(runtime)
        total = stage_metrics["total"]
        coding = stage_metrics["coding"]
        has_stage_usage = bool(
            runtime.classification is not None
            or runtime.attempts
            or runtime.verifications
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
            total_cost_usd=total.cost,
            cost_accounting_status=(
                total.cost_accounting_status if has_stage_usage else "unknown"
            ),
            total_input_tokens=total.input_tokens,
            total_output_tokens=total.output_tokens,
            token_accounting_status=(
                total.token_accounting_status if has_stage_usage else "unknown"
            ),
            total_duration_ms=coding.duration_ms,
            duration_accounting_status=(
                coding.duration_accounting_status if runtime.attempts else "unknown"
            ),
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
                "plugins": list(self._plugin_identities),
                "lineage": redact_data(_mapping_copy(runtime.request.lineage)),
                "delivery": (
                    redact_data(runtime.delivery.model_dump(mode="json"))
                    if runtime.delivery is not None
                    else None
                ),
            },
            currency=total.currency,
            stage_metrics=stage_metrics,
            total_model_calls=total.model_calls,
            model_call_accounting_status=(
                total.model_call_accounting_status if has_stage_usage else "unknown"
            ),
            run_wall_clock_duration_ms=max(
                runtime.wall_clock_offset_ms
                + int((self._monotonic() - runtime.started_monotonic) * 1000),
                0,
            ),
            run_wall_clock_duration_accounting_status="complete",
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
            metadata={
                "terminal_reason": runtime.terminal_reason,
                "delivery": (
                    redact_data(runtime.delivery.model_dump(mode="json"))
                    if runtime.delivery is not None
                    else None
                ),
            },
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
        runtime.committed_events.append(event)
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
        runtime.committed_events.append(event)
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

    def _terminal_observability_payload(
        self, runtime: _Runtime, *, status: str
    ) -> dict[str, Any]:
        """Project complete accounting for every terminal state, not only success."""

        stages = self._stage_metrics(runtime)
        total = stages["total"]
        coding = self._aggregate_stage(
            "coding",
            [stages["classification"], stages["coding"]],
            total.currency,
        )
        verification = stages["verification"]
        routed_attempt = next(
            (
                item
                for item in runtime.attempts
                if item.attempt_id == runtime.selected_attempt_id
            ),
            runtime.attempts[-1] if runtime.attempts else None,
        )
        changed_files = (
            list(runtime.materialization.changed_files)
            if runtime.materialization is not None
            else []
        )
        file_write_count = 0
        for attempt in runtime.attempts:
            result = runtime.attempt_results.get(attempt.attempt_id)
            telemetry = result.runner_telemetry if result is not None else {}
            file_write_count += int(
                attempt.metadata.get(
                    "total_file_writes", telemetry.get("total_file_writes", 0)
                )
                or 0
            )
        last_verification = runtime.verifications[-1] if runtime.verifications else None
        failure_category = (
            last_verification.metadata.get("failure_category")
            if last_verification is not None
            else runtime.failure.code
            if runtime.failure is not None
            else None
        )
        return {
            "status": status,
            "selected_attempt_id": runtime.selected_attempt_id,
            "selected_backend": routed_attempt.backend_name if routed_attempt else None,
            "selected_model": routed_attempt.model if routed_attempt else None,
            "attempt_count": len(runtime.attempts),
            "escalation_count": sum(
                event.event_type == "escalation_selected"
                for event in runtime.committed_events
            ),
            "input_tokens": total.input_tokens,
            "output_tokens": total.output_tokens,
            "total_tokens": (
                total.input_tokens + total.output_tokens
                if total.input_tokens is not None and total.output_tokens is not None
                else None
            ),
            "token_accounting_status": total.token_accounting_status,
            "coding_cost_usd": coding.cost,
            "verifier_cost_usd": verification.cost,
            "total_cost_usd": total.cost,
            "cost_accounting_status": total.cost_accounting_status,
            "duration_ms": max(
                runtime.wall_clock_offset_ms
                + int((self._monotonic() - runtime.started_monotonic) * 1000),
                0,
            ),
            "changed_files": changed_files,
            "file_write_count": file_write_count,
            "verification_status": (
                last_verification.outcome if last_verification is not None else None
            ),
            "materialization_status": (
                runtime.materialization.status
                if runtime.materialization is not None
                else "not_materialized"
            ),
            "terminal_reason": runtime.terminal_reason,
            "failure_category": failure_category,
        }

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
                **self._terminal_observability_payload(runtime, status="failed"),
                "code": code,
                "failure_category": code,
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
            {
                **self._terminal_observability_payload(runtime, status="exhausted"),
                "reason": reason,
            },
        )

    def _result(
        self, runtime: _Runtime, forced_state: str | None = None
    ) -> ClosedLoopRunResult:
        state = forced_state or runtime.machine.state
        if state not in {*TERMINAL_STATES, "AWAITING_APPROVAL"}:
            state = "FAILED"
        cost, accounting = self._actual_cost(runtime)
        currency = self._stage_metrics(runtime)["total"].currency
        return ClosedLoopRunResult(
            run_id=runtime.run_id,
            terminal_state=state,  # type: ignore[arg-type]
            selected_attempt_id=runtime.selected_attempt_id,
            run_directory=runtime.store.run_directory,
            actual_known_cost_usd=cost,
            accounting_status=accounting,  # type: ignore[arg-type]
            failure_or_exhaustion_reason=runtime.terminal_reason,
            currency=currency,
        )
