from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from villani_ops.closed_loop.interfaces import (
    AttemptResult,
    BackendOption,
    Classification,
    EvidenceItem,
    Materialization,
    PolicyDecision,
    Requirement,
    Selection,
    SelectionRanking,
    Verification,
)


PATCH_ONE = """diff --git a/example.txt b/example.txt
--- a/example.txt
+++ b/example.txt
@@ -1 +1 @@
-old
+first
"""

PATCH_TWO = PATCH_ONE.replace("+first", "+second")


class FakeClassifier:
    def __init__(self, result: Classification | None = None) -> None:
        self.result = result or Classification(
            difficulty="easy",
            risk="low",
            category="test",
            required_capabilities=("python",),
            confidence=0.99,
            reasoning_summary="Deterministic fake classification.",
        )
        self.calls: list[tuple[str, Any]] = []

    def classify(self, task: str, context: Any) -> Classification:
        self.calls.append((task, context))
        return self.result


class FakePolicyEngine:
    def __init__(self, decisions: list[PolicyDecision]) -> None:
        self.decisions = deque(decisions)
        self.calls: list[Any] = []

    def decide(self, context: Any) -> PolicyDecision:
        self.calls.append(context)
        if not self.decisions:
            raise AssertionError("fake policy decision queue is empty")
        return self.decisions.popleft()


class MutatingFakePolicyEngine(FakePolicyEngine):
    def decide(self, context: Any) -> PolicyDecision:
        object.__setattr__(context, "state", "FAILED")
        return super().decide(context)


class FakeAttemptRunner:
    def __init__(self, results: list[AttemptResult]) -> None:
        self.results = deque(results)
        self.calls: list[Any] = []

    def run(self, context: Any) -> AttemptResult:
        self.calls.append(context)
        if not self.results:
            raise AssertionError("fake attempt result queue is empty")
        return self.results.popleft()


class FakeVerifier:
    def __init__(self, results: list[Verification | Exception]) -> None:
        self.results = deque(results)
        self.calls: list[tuple[Any, AttemptResult]] = []

    def verify(self, context: Any, result: AttemptResult) -> Verification:
        self.calls.append((context, result))
        if not self.results:
            raise AssertionError("fake verification queue is empty")
        returned = self.results.popleft()
        if isinstance(returned, Exception):
            raise returned
        return returned


class FakeSelector:
    def __init__(self, selected_attempt_id: str | None = None) -> None:
        self.selected_attempt_id = selected_attempt_id
        self.calls: list[tuple[tuple[Any, ...], Any]] = []

    def select(self, candidates: tuple[Any, ...], context: Any) -> Selection:
        self.calls.append((candidates, context))
        selected = self.selected_attempt_id or candidates[0].attempt.attempt_id
        return Selection(
            selected_attempt_id=selected,
            strategy="fake_deterministic",
            reason="Selected by deterministic fake.",
            rankings=(
                SelectionRanking(
                    attempt_id=selected,
                    rank=1,
                    reason="Only selected fake candidate.",
                    actual_cost_usd=None,
                    cost_accounting_status="unknown",
                ),
            ),
            report="# Selection\n\nDeterministic fake selection.\n",
        )


class FakeMaterializer:
    def __init__(self, result: Materialization | None = None) -> None:
        self.result = result
        self.calls: list[tuple[Selection, Any]] = []

    def materialize(self, selection: Selection, context: Any) -> Materialization:
        self.calls.append((selection, context))
        if self.result is not None:
            return self.result
        return Materialization(
            status="succeeded",
            final_patch=context.selected_candidate.patch,
            final_report="# Final report\n\nMaterialized deterministic fake.\n",
            changed_files=("example.txt",),
        )


class FixedNow:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 10, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=1)
        return value


class FakeMonotonic:
    def __init__(self, values: list[float] | None = None) -> None:
        self.values = deque(values or [0.0])
        self.last = self.values[-1]

    def __call__(self) -> float:
        if self.values:
            self.last = self.values.popleft()
        return self.last


class StableIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        self.counts[prefix] = self.counts.get(prefix, 0) + 1
        return f"{prefix}_test_{self.counts[prefix]:03d}"


def backend(
    name: str,
    *,
    estimated_cost: float | None = 1.0,
    capability: float = 50.0,
) -> BackendOption:
    return BackendOption(
        backend_name=name,
        model=f"{name}-model",
        eligible=True,
        capability_score=capability,
        estimated_cost_usd=estimated_cost,
        cost_accounting_status=(
            "complete" if estimated_cost is not None else "unknown"
        ),
    )


def policy(
    action: str,
    *,
    backend_option: BackendOption | None = None,
    reason: str | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        action=action,  # type: ignore[arg-type]
        reason=reason or f"Fake policy chose {action}.",
        considered_backends=(backend_option,) if backend_option else (),
        chosen_backend=(backend_option.backend_name if backend_option else None),
        chosen_model=(backend_option.model if backend_option else None),
        policy_version="fake_v1",
    )


def attempt(
    *,
    patch: str | None = PATCH_ONE,
    exit_code: int = 0,
    status: str = "completed",
    cost: float | None = 1.0,
) -> AttemptResult:
    return AttemptResult(
        runner_name="fake_runner",
        status=status,  # type: ignore[arg-type]
        worktree_path="fake-worktree",
        patch=patch,
        exit_code=exit_code,
        stdout="fake stdout\n",
        stderr="",
        runner_telemetry={"provider": "fake"},
        trace={"events": []},
        duration_ms=10,
        duration_accounting_status="complete",
        input_tokens=10,
        output_tokens=5,
        token_accounting_status="complete",
        cost_usd=cost,
        cost_accounting_status="complete" if cost is not None else "unknown",
    )


def accepted_verification() -> Verification:
    return Verification(
        verifier="fake_verifier",
        outcome="accepted",
        acceptance_eligible=True,
        confidence=0.99,
        reason="All fake criteria have direct evidence.",
        recommended_action="accept",
        requirement_results=(
            Requirement(
                requirement_id="criterion_1",
                description="The requested behavior works.",
                outcome="passed",
                evidence_ids=("evidence_1",),
            ),
        ),
        success_evidence=(
            EvidenceItem(
                evidence_id="evidence_1",
                kind="test",
                summary="Deterministic fake evidence passed.",
                artifact_path="attempts/attempt_001/runner_telemetry.json",
            ),
        ),
    )


def rejected_verification(*, capability: bool = False) -> Verification:
    return Verification(
        verifier="fake_verifier",
        outcome="rejected",
        acceptance_eligible=False,
        confidence=0.95,
        reason=(
            "Backend lacks the required capability."
            if capability
            else "The fake criterion failed."
        ),
        recommended_action="escalate" if capability else "reject",
        requirement_results=(
            Requirement(
                requirement_id="criterion_1",
                description="The requested behavior works.",
                outcome="failed",
                evidence_ids=("failure_1",),
            ),
        ),
        failure_evidence=(
            EvidenceItem(
                evidence_id="failure_1",
                kind="test",
                summary="Deterministic fake evidence failed.",
            ),
        ),
    )


def verifier_error_marked_eligible() -> Verification:
    return Verification(
        verifier="fake_verifier",
        outcome="error",
        acceptance_eligible=True,
        confidence=None,
        reason="Fake verifier infrastructure failed.",
        recommended_action="retry_verifier",
        requirement_results=(
            Requirement(
                requirement_id="criterion_1",
                description="The requested behavior works.",
                outcome="passed",
                evidence_ids=("evidence_1",),
            ),
        ),
        success_evidence=(
            EvidenceItem(
                evidence_id="evidence_1",
                kind="test",
                summary="Untrusted evidence from an errored verifier.",
            ),
        ),
    )
