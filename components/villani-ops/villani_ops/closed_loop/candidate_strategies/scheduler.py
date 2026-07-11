"""Bounded, recoverable execution for candidate plans of one coding task."""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..durable_io import append_jsonl_durable, read_jsonl_tolerant
from .models import (
    CandidateObservation,
    CandidatePlan,
    ReliabilityAccounting,
    ReliabilityStrategyConfiguration,
)
from .planner import adaptive_stop, diversity_summary


@dataclass(frozen=True, slots=True)
class CandidateExecution:
    plan: CandidatePlan
    result: Any | None
    observation: CandidateObservation | None
    cancelled: bool = False
    error: str | None = None


class CandidateScheduler:
    """Execute candidates with independent cancellation and an append-only journal."""

    def __init__(
        self,
        configuration: ReliabilityStrategyConfiguration,
        *,
        journal_path: Path,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.configuration = configuration
        self.journal_path = journal_path
        self.checkpoint = checkpoint
        self._journal_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active = 0
        self._maximum_active = 0

    def _append(
        self,
        event: str,
        plan: CandidatePlan | None = None,
        *,
        candidate_id: str | None = None,
        **payload: Any,
    ) -> None:
        document = {
            "schema_version": "villani.candidate_schedule_event.v1",
            "event": event,
            "candidate_id": plan.candidate_id if plan else candidate_id,
            "ordinal": plan.ordinal if plan else None,
            "payload": payload,
        }
        with self._journal_lock:
            append_jsonl_durable(self.journal_path, document)
        if self.checkpoint is not None:
            self.checkpoint(event)

    def _recovered(self) -> tuple[set[str], set[str]]:
        terminal: set[str] = set()
        started: set[str] = set()
        if not self.journal_path.is_file():
            return terminal, started
        for item in read_jsonl_tolerant(self.journal_path):
            candidate_id = item.get("candidate_id")
            if not isinstance(candidate_id, str):
                continue
            event = item.get("event")
            if event == "candidate_started":
                started.add(candidate_id)
            if event in {
                "candidate_verified",
                "candidate_cancelled",
                "candidate_failed",
            }:
                terminal.add(candidate_id)
        interrupted = started - terminal
        for candidate_id in sorted(interrupted):
            # A process interruption makes ownership of the old sandbox uncertain.
            # Fail closed and never invoke the same candidate identity again.
            self._append(
                "candidate_cancelled",
                None,
                candidate_id=candidate_id,
                reason="interrupted_execution",
            )
            terminal.add(candidate_id)
        return terminal, interrupted

    def execute(
        self,
        plans: tuple[CandidatePlan, ...],
        *,
        generate: Callable[[CandidatePlan, threading.Event], Any],
        verify: Callable[[CandidatePlan, Any], CandidateObservation],
        remaining_attempt_budget: int,
        remaining_cost_budget_usd: float | None = None,
    ) -> tuple[tuple[CandidateExecution, ...], ReliabilityAccounting]:
        terminal, interrupted = self._recovered()
        available = [plan for plan in plans if plan.candidate_id not in terminal]
        observations: list[CandidateObservation] = []
        executions: list[CandidateExecution] = []
        cancellations: dict[str, threading.Event] = {
            plan.candidate_id: threading.Event() for plan in available
        }
        started = completed = cancelled = 0
        stop_reason: str | None = None
        self._append("scheduling_started", None, strategy=self.configuration.strategy)
        pool = ThreadPoolExecutor(max_workers=self.configuration.maximum_parallelism)
        pending: dict[Future[Any], CandidatePlan] = {}

        def invoke(plan: CandidatePlan) -> Any:
            nonlocal started
            with self._active_lock:
                self._active += 1
                self._maximum_active = max(self._maximum_active, self._active)
                started += 1
            self._append("candidate_started", plan, sandbox_id=plan.sandbox_id)
            try:
                return generate(plan, cancellations[plan.candidate_id])
            finally:
                with self._active_lock:
                    self._active -= 1

        next_index = 0
        scheduled = 0
        try:
            while next_index < len(available) or pending:
                while (
                    next_index < len(available)
                    and len(pending) < self.configuration.maximum_parallelism
                    and scheduled < remaining_attempt_budget
                    and (
                        self.configuration.stop_policy != "compare"
                        or len(pending)
                        < max(
                            self.configuration.accepted_candidate_requirement
                            - sum(item.acceptance_eligible for item in observations),
                            0,
                        )
                    )
                ):
                    plan = available[next_index]
                    next_index += 1
                    self._append(
                        "candidate_scheduled",
                        plan,
                        baseline_sha256=plan.baseline_sha256,
                    )
                    pending[pool.submit(invoke, plan)] = plan
                    scheduled += 1
                    if self.configuration.strategy in {
                        "single_attempt",
                        "sequential_escalation",
                    }:
                        break
                if not pending:
                    break
                done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: pending[item].ordinal):
                    plan = pending.pop(future)
                    try:
                        result = future.result()
                        if cancellations[plan.candidate_id].is_set():
                            cancelled += 1
                            self._append(
                                "candidate_cancelled",
                                plan,
                                reason="policy_cancellation",
                            )
                            executions.append(
                                CandidateExecution(plan, result, None, cancelled=True)
                            )
                            continue
                        self._append("candidate_completed", plan)
                        observation = verify(plan, result)
                        observations.append(observation)
                        completed += 1
                        self._append(
                            "candidate_verified",
                            plan,
                            acceptance_eligible=observation.acceptance_eligible,
                            confidence=observation.verifier_confidence,
                            evidence_grade=observation.evidence_grade,
                        )
                        executions.append(CandidateExecution(plan, result, observation))
                    except Exception as error:
                        completed += 1
                        observations.append(
                            CandidateObservation(
                                candidate_id=plan.candidate_id,
                                acceptance_eligible=False,
                            )
                        )
                        self._append(
                            "candidate_failed",
                            plan,
                            error_class=error.__class__.__name__,
                        )
                        executions.append(
                            CandidateExecution(plan, None, None, error=str(error))
                        )
                decision = adaptive_stop(
                    self.configuration,
                    plans,
                    tuple(observations),
                    remaining_attempt_budget=max(remaining_attempt_budget - started, 0),
                    remaining_cost_budget_usd=remaining_cost_budget_usd,
                )
                if decision.stop:
                    stop_reason = decision.reason
                    for future, plan in pending.items():
                        cancellations[plan.candidate_id].set()
                        future.cancel()
                        self._append(
                            "cancellation_requested", plan, reason=decision.reason
                        )
                    for plan in available[next_index:]:
                        self._append("candidate_avoided", plan, reason=decision.reason)
                    break
            for future, plan in list(pending.items()):
                try:
                    result = future.result()
                    cancelled += 1
                    self._append(
                        "candidate_cancelled",
                        plan,
                        reason=stop_reason or "scheduler_stop",
                    )
                    executions.append(
                        CandidateExecution(plan, result, None, cancelled=True)
                    )
                except Exception as error:
                    cancelled += 1
                    self._append(
                        "candidate_cancelled", plan, reason=error.__class__.__name__
                    )
                    executions.append(
                        CandidateExecution(plan, None, None, cancelled=True)
                    )
            self._append(
                "selection_ready",
                None,
                eligible_candidate_ids=[
                    item.candidate_id
                    for item in observations
                    if item.acceptance_eligible
                ],
            )
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        diversity_claimed, distinct = diversity_summary(plans)
        avoided = max(len(plans) - started, 0)
        avoided_costs = [item.estimated_cost_usd for item in plans[started:]]
        estimated_avoided = (
            sum(value for value in avoided_costs if value is not None)
            if avoided_costs and all(value is not None for value in avoided_costs)
            else None
        )
        accounting = ReliabilityAccounting(
            strategy=self.configuration.strategy,
            planned_attempts=len(plans),
            started_attempts=started,
            completed_attempts=completed,
            cancelled_attempts=cancelled + len(interrupted),
            avoided_attempts=avoided,
            estimated_avoided_spend_usd=estimated_avoided,
            diversity_claimed=diversity_claimed,
            distinct_effective_configurations=distinct,
            maximum_observed_concurrency=self._maximum_active,
            stop_reason=stop_reason,
        )
        self._append(
            "scheduling_completed", None, accounting=accounting.model_dump(mode="json")
        )
        return tuple(sorted(executions, key=lambda item: item.plan.ordinal)), accounting
