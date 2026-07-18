from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from villani_ops.closed_loop.interfaces import (
    AttemptContext,
    AttemptResult,
    EvidenceItem,
    Requirement,
    RuntimeEvent,
    Verification,
)
from villani_ops.closed_loop.verifier_routing import (
    VerifierCascade,
    VerifierPolicyEntry,
    VerifierRoute,
    VerifierRoutingContext,
    VerifierRoutingPolicy,
    required_capability,
    select_routes,
)
from villani_ops.verifier.service import _subprocess_invocation_status


class StaticVerifier:
    def __init__(self, result: Verification) -> None:
        self.result = result
        self.calls = 0

    def verify(self, _context: AttemptContext, _result: AttemptResult) -> Verification:
        self.calls += 1
        return self.result


def context(tmp_path: Path, *, risk: str = "low") -> AttemptContext:
    return AttemptContext(
        run_id="run_001",
        trace_id="trace_001",
        task_id="task_001",
        attempt_id="attempt_001",
        ordinal=1,
        task="Fix the behavior",
        repository_path=str(tmp_path),
        success_criteria="Tests pass",
        requires_file_changes=True,
        backend_name="coding",
        model="fixture-standard",
        policy_configuration={},
        run_directory=tmp_path / "run",
        attempt_directory=tmp_path / "run/attempts/attempt_001",
        classification={"risk": risk, "difficulty": "easy"},
    )


def attempt(tmp_path: Path, *, validation_exit: int | None = None) -> AttemptResult:
    events = ()
    metadata = {"changed_files": ["calculator.py"]}
    if validation_exit is not None:
        events = (
            RuntimeEvent(
                event_type=(
                    "repository_validation_completed"
                    if validation_exit == 0
                    else "repository_validation_failed"
                ),
                timestamp=datetime.now(timezone.utc),
                payload={
                    "command_role": "repository_validation",
                    "exit_code": validation_exit,
                },
            ),
        )
        metadata.update(
            {
                "repository_validation_status": (
                    "passed" if validation_exit == 0 else "failed"
                ),
                "repository_validation_authoritative": True,
            }
        )
    return AttemptResult(
        runner_name="fixture",
        status="completed",
        worktree_path=str(tmp_path),
        patch="+fixed\n",
        exit_code=0,
        runtime_events=events,
        metadata=metadata,
    )


def verification(
    outcome: str,
    *,
    status: str = "completed",
    cost: float = 0.0,
) -> Verification:
    accepted = outcome == "accepted"
    return Verification(
        verifier="fixture",
        outcome=outcome,  # type: ignore[arg-type]
        acceptance_eligible=accepted,
        confidence=0.95 if accepted else 0.2,
        reason=outcome,
        recommended_action="accept" if accepted else "retry_verifier",
        requirement_results=(
            Requirement(
                requirement_id="test",
                description="Tests pass",
                outcome="passed" if accepted else "missing",
                evidence_ids=("evidence",) if accepted else (),
            ),
        ),
        success_evidence=(
            EvidenceItem(
                evidence_id="evidence",
                kind="test",
                summary="Repository validation passed",
            ),
        )
        if accepted
        else (),
        metadata={"invocation_status": status},
        llm_usage=(
            {
                "stage": "verification",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cost": cost,
            },
        ),
    )


def test_policy_uses_risk_patch_and_sensitive_file_floors() -> None:
    policy = VerifierRoutingPolicy()
    minimum, reasons = required_capability(
        policy,
        VerifierRoutingContext(
            risk="low",
            difficulty="easy",
            patch_lines=700,
            sensitive_file_change=True,
        ),
    )
    assert minimum == 80
    assert {"large_patch", "sensitive_file_change"} <= set(reasons)


def test_nested_verifier_failure_status_survives_cli_boundary() -> None:
    assert (
        _subprocess_invocation_status(
            {"reason": "malformed verifier output: invalid JSON"}, 1
        )
        == "malformed_output"
    )
    assert (
        _subprocess_invocation_status({"reason": "verifier timeout after 5s"}, 1)
        == "timeout"
    )
    assert _subprocess_invocation_status({"reason": "ok"}, 0) == "completed"


def test_cheapest_eligible_verifier_is_selected_before_stronger_fallback() -> None:
    low = StaticVerifier(verification("unclear"))
    high = StaticVerifier(verification("accepted"))
    routes = (
        VerifierRoute(
            VerifierPolicyEntry(
                backend="high", capability_score=90, price_per_call_usd=0.05
            ),
            high,
        ),
        VerifierRoute(
            VerifierPolicyEntry(
                backend="low", capability_score=30, price_per_call_usd=0.01
            ),
            low,
        ),
    )
    selected, minimum, _ = select_routes(
        VerifierRoutingPolicy(),
        VerifierRoutingContext(risk="low", difficulty="easy"),
        routes,
    )
    assert minimum == 20
    assert [item.entry.backend for item in selected] == ["low", "high"]


def test_malformed_output_escalates_and_billing_is_counted_once(tmp_path: Path) -> None:
    low = StaticVerifier(verification("error", status="malformed_output", cost=0.01))
    high = StaticVerifier(verification("accepted", cost=0.04))
    cascade = VerifierCascade(
        (
            VerifierRoute(
                VerifierPolicyEntry(
                    backend="low", capability_score=30, price_per_call_usd=0.01
                ),
                low,
            ),
            VerifierRoute(
                VerifierPolicyEntry(
                    backend="high", capability_score=90, price_per_call_usd=0.04
                ),
                high,
            ),
        )
    )
    result = cascade.verify(context(tmp_path), attempt(tmp_path, validation_exit=0))
    assert result.acceptance_eligible is True
    assert low.calls == high.calls == 1
    assert [item["backend"] for item in result.metadata["verifier_calls"]] == [
        "low",
        "high",
    ]
    assert (
        result.metadata["verifier_calls"][0]["escalation_reason"] == "malformed_output"
    )
    assert sum(float(item["cost"]) for item in result.llm_usage) == 0.05


def test_timeout_escalates_and_final_timeout_fails_closed(tmp_path: Path) -> None:
    timed_out = StaticVerifier(verification("error", status="timeout", cost=0.01))
    stronger = StaticVerifier(verification("accepted", cost=0.04))
    cascade = VerifierCascade(
        (
            VerifierRoute(
                VerifierPolicyEntry(backend="low", capability_score=30), timed_out
            ),
            VerifierRoute(
                VerifierPolicyEntry(backend="high", capability_score=90), stronger
            ),
        )
    )
    result = cascade.verify(context(tmp_path), attempt(tmp_path, validation_exit=0))
    assert result.acceptance_eligible is True
    assert result.metadata["verifier_calls"][0]["timeout"] is True
    assert result.metadata["verifier_calls"][0]["escalation_reason"] == "timeout"
    assert sum(float(item["cost"]) for item in result.llm_usage) == 0.05

    only_timeout = VerifierCascade(
        (
            VerifierRoute(
                VerifierPolicyEntry(backend="only", capability_score=90), timed_out
            ),
        )
    ).verify(context(tmp_path), attempt(tmp_path, validation_exit=0))
    assert only_timeout.acceptance_eligible is False
    assert only_timeout.metadata["verifier_route_complete"] is False


def test_unresolved_disagreement_fails_closed(tmp_path: Path) -> None:
    rejected = StaticVerifier(verification("rejected", cost=0.01))
    accepted = StaticVerifier(verification("accepted", cost=0.02))
    result = VerifierCascade(
        (
            VerifierRoute(
                VerifierPolicyEntry(backend="low", capability_score=30), rejected
            ),
            VerifierRoute(
                VerifierPolicyEntry(backend="high", capability_score=90), accepted
            ),
        )
    ).verify(context(tmp_path), attempt(tmp_path, validation_exit=0))
    assert result.outcome == "unclear"
    assert result.acceptance_eligible is False
    assert result.metadata["verifier_disagreement"] is True
    assert result.metadata["verifier_disagreement_resolution"] == "unresolved"


def test_disagreement_invokes_stronger_resolver(tmp_path: Path) -> None:
    routes = tuple(
        VerifierRoute(
            VerifierPolicyEntry(
                backend=name,
                capability_score=capability,
                price_per_call_usd=cost,
            ),
            StaticVerifier(verification(outcome, cost=cost)),
        )
        for name, capability, cost, outcome in (
            ("low", 30, 0.01, "rejected"),
            ("medium", 60, 0.02, "accepted"),
            ("high", 90, 0.04, "accepted"),
        )
    )
    result = VerifierCascade(routes).verify(
        context(tmp_path), attempt(tmp_path, validation_exit=0)
    )
    assert result.acceptance_eligible is True
    assert [item["backend"] for item in result.metadata["verifier_calls"]] == [
        "low",
        "medium",
        "high",
    ]
    assert (
        result.metadata["verifier_calls"][1]["escalation_reason"]
        == "verifier_disagreement"
    )
    assert result.metadata["verifier_disagreement_resolution"] == "stronger_verifier"


def test_unavailable_verifier_is_never_selected() -> None:
    unavailable = StaticVerifier(verification("accepted"))
    available = StaticVerifier(verification("accepted"))
    selected, _, _ = select_routes(
        VerifierRoutingPolicy(),
        VerifierRoutingContext(risk="low", difficulty="easy"),
        (
            VerifierRoute(
                VerifierPolicyEntry(
                    backend="unavailable",
                    capability_score=100,
                    price_per_call_usd=0,
                    available=False,
                ),
                unavailable,
            ),
            VerifierRoute(
                VerifierPolicyEntry(
                    backend="available", capability_score=30, price_per_call_usd=1
                ),
                available,
            ),
        ),
    )
    assert [item.entry.backend for item in selected] == ["available"]


def test_failed_repository_validation_blocks_semantic_acceptance(
    tmp_path: Path,
) -> None:
    backend = StaticVerifier(verification("accepted", cost=1.0))
    cascade = VerifierCascade(
        (
            VerifierRoute(
                VerifierPolicyEntry(backend="high", capability_score=100), backend
            ),
        )
    )
    result = cascade.verify(
        context(tmp_path, risk="high"),
        attempt(tmp_path, validation_exit=1),
    )
    assert result.acceptance_eligible is False
    assert result.outcome == "rejected"
    assert backend.calls == 0
    assert result.metadata["redundant_semantic_call_avoided"] is True
    assert result.metadata["semantic_verifier_invoked"] is False
    assert "verifier_disagreement" not in result.metadata


def test_no_eligible_authority_fails_closed(tmp_path: Path) -> None:
    advisory = StaticVerifier(verification("accepted"))
    cascade = VerifierCascade(
        (
            VerifierRoute(
                VerifierPolicyEntry(
                    backend="advisory",
                    capability_score=100,
                    authority="advisory",
                ),
                advisory,
            ),
        )
    )
    result = cascade.verify(
        context(tmp_path, risk="high"), attempt(tmp_path, validation_exit=0)
    )
    assert result.acceptance_eligible is False
    assert "No available verifier" in result.reason
    assert advisory.calls == 0
