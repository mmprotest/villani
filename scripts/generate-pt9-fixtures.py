"""Generate deterministic PT9 protocol fixtures and the no-evidence Gate D artifact."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components" / "villani-ops"))

from villani_ops.closed_loop.adaptive_verification.gate import evaluate_gate_d  # noqa: E402
from villani_ops.closed_loop.adaptive_verification.planning import (  # noqa: E402
    SEMANTIC_CONTEXT_ALLOWLIST,
    SEMANTIC_CONTEXT_EXCLUDED,
)
from villani_ops.closed_loop.adaptive_verification.models import (  # noqa: E402
    AdaptiveVerificationPlan,
    BinaryVerificationDecision,
    CompactReviewPackage,
    DurationAccounting,
    GateDArm,
    HumanOutcome,
    MoneyAccounting,
    RestrictedVerifierProvenance,
    ReviewCheck,
    SupervisionMetrics,
    VerificationNodeResult,
    VerificationPlanNode,
    canonical_digest,
)


STAMP = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
VALID = ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _money(amount: float, source: str) -> MoneyAccounting:
    return MoneyAccounting(
        amount=amount,
        currency="USD",
        accounting_status="complete",
        source=source,
    )


def _duration(milliseconds: int, source: str) -> DurationAccounting:
    return DurationAccounting(
        duration_ms=milliseconds,
        accounting_status="complete",
        source=source,
    )


def _arm(
    strategy: str,
    *,
    cost: float,
    duration: int,
    review: float,
) -> GateDArm:
    return GateDArm(
        strategy=strategy,  # type: ignore[arg-type]
        case_ids=["founder_case_001"],
        eligible_cases=1,
        accepted_as_is=1,
        false_acceptances=0,
        total_cost=_money(cost, "frozen_founder_fixture"),
        elapsed_duration=_duration(duration, "frozen_founder_fixture"),
        review_minutes=review,
        review_time_accounting_status="complete",
        explainable_routes=True,
        safe_fallback=True,
    )


def main() -> None:
    digest = "sha256:" + "1" * 64
    plan = AdaptiveVerificationPlan(
        plan_id="avp_" + "a" * 64,
        run_id="run_protocol_fixture",
        attempt_id="attempt_002",
        policy_digest=digest,
        created_at=STAMP,
        risk_tier="standard",
        risk_reasons=["ordinary_bounded_change"],
        task_digest="sha256:" + "2" * 64,
        criteria_digest="sha256:" + "3" * 64,
        candidate_diff_digest="sha256:" + "4" * 64,
        changed_files=["src/example.py"],
        requirement_ids=["req_001"],
        qualification_state="qualified",
        historical_failure_modes=[],
        nodes=[
            VerificationPlanNode(
                node_id="node_diff_integrity",
                kind="diff_integrity",
                disposition="required",
                reason="The recorded patch must remain isolated and internally consistent.",
                evidence_requirements=["candidate patch digest"],
            ),
            VerificationPlanNode(
                node_id="node_requirement_mapping",
                kind="requirement_mapping",
                disposition="required",
                reason="Every requirement needs acceptance-grade evidence.",
                depends_on=["node_diff_integrity"],
                evidence_requirements=["requirement evidence matrix"],
            ),
            VerificationPlanNode(
                node_id="node_semantic_verifier",
                kind="semantic_verifier",
                disposition="required",
                reason="Semantic verification is mandatory before acceptance.",
                depends_on=["node_requirement_mapping"],
                evidence_requirements=["binary semantic verdict"],
                estimated_model_calls=1,
            ),
        ],
        independent_verifier_required=False,
        manual_review_if_unresolved=True,
        semantic_context_allowlist=SEMANTIC_CONTEXT_ALLOWLIST,
        semantic_context_excluded=SEMANTIC_CONTEXT_EXCLUDED,
        deterministic_input_digest="sha256:" + "5" * 64,
    )
    decision = BinaryVerificationDecision(
        decision_id="avd_" + "b" * 64,
        run_id=plan.run_id,
        attempt_id=plan.attempt_id,
        plan_id=plan.plan_id,
        decided_at=STAMP,
        decision=1,
        reason_code="proved_acceptable",
        reason="All required deterministic and semantic evidence passed.",
        requirements_proved=["req_001"],
        requirements_not_proved=[],
        blockers=[],
        infrastructure_status="resolved",
        semantic_status="passed",
        independent_verifier_required=False,
        independent_verifier_completed=False,
        node_results=[
            VerificationNodeResult(
                node_id=node.node_id,
                status="passed",
                reason="The required proof passed.",
                evidence_paths=["verification/attempt_002-evidence.json"],
            )
            for node in plan.nodes
        ],
        verifier_provenance=[
            RestrictedVerifierProvenance(
                verifier_role="semantic",
                verifier_identity_digest="sha256:" + "6" * 64,
                invocation_status="completed",
                independent=False,
                artifact_path="verification/attempt_002-raw.json",
            )
        ],
        verification_cost=_money(0.07, "authoritative_verifier_usage"),
        normalized_from="accepted",
    )
    review = CompactReviewPackage(
        package_id="rvp_" + "c" * 64,
        run_id=plan.run_id,
        attempt_id=plan.attempt_id,
        decision_id=decision.decision_id,
        created_at=STAMP,
        status="ready_to_apply",
        task="Apply the bounded fixture change.",
        change_summary="1 file changed in the preserved candidate.",
        changed_files=["src/example.py"],
        requirements_proved=["req_001"],
        requirements_not_proved=[],
        checks=[
            ReviewCheck(
                label="requirement mapping",
                status="passed",
                evidence_path="verification/attempt_002-evidence.json",
            ),
            ReviewCheck(
                label="semantic verifier",
                status="passed",
                evidence_path="verification/attempt_002-raw.json",
            ),
        ],
        risk_tier="standard",
        risk_flags=[],
        known_cost=_money(0.42, "candidate_attempt_telemetry"),
        known_duration=_duration(1800, "candidate_attempt_telemetry"),
        why_villani_trusts_it=(
            "Deterministic integrity checks, requirement mapping, repository evidence, "
            "and semantic verification passed."
        ),
        unresolved_decision=None,
        full_evidence_href="/console/runs/run_protocol_fixture/replay",
    )
    outcome = HumanOutcome(
        outcome_id="hout_" + "d" * 64,
        run_id=plan.run_id,
        attempt_id=plan.attempt_id,
        recorded_at=STAMP,
        outcome="accepted_as_is",
        review_minutes=2.0,
        review_time_accounting_status="complete",
        full_trace_opened=False,
        full_trace_accounting_status="complete",
        correction_summary=None,
        linked_reference=None,
        imported_from="explicit_cli",
        actor="local_user",
        notes="Fixture outcome entered explicitly.",
    )
    metrics = SupervisionMetrics(
        metrics_id="smet_" + "e" * 64,
        run_id=plan.run_id,
        calculated_at=STAMP,
        eligible_outcome_count=1,
        evidence_expansion_count=0,
        explicit_review_minutes=2.0,
        review_time_accounting_status="complete",
        application_without_full_trace_count=1,
        full_trace_accounting_status="complete",
        correction_count=0,
        false_acceptance_count=0,
        false_rejection_count=0,
        verification_cost=_money(0.07, "binary_verification_decisions"),
        review_cost=_money(0.10, "explicit_review_minutes_times_configured_rate"),
        total_accepted_change_cost=_money(0.59, "accepted_change_cost_components"),
        source_outcome_ids=[outcome.outcome_id],
    )
    gate = evaluate_gate_d(
        arms=[
            _arm("strongest_only", cost=1.2, duration=4000, review=8.0),
            _arm("accepted_change_optimizer", cost=0.9, duration=3200, review=6.0),
            _arm("optimizer_plus_adaptive", cost=0.7, duration=2400, review=3.0),
        ],
        generated_at=STAMP,
        evidence_references=["frozen-founder-fixture.json"],
    )

    for filename, value in (
        ("adaptive-verification-plan.json", plan),
        ("binary-verification-decision.json", decision),
        ("review-package.json", review),
        ("human-outcome.json", outcome),
        ("supervision-metrics.json", metrics),
        ("gate-d.json", gate),
    ):
        _write(VALID / filename, value)
    _write(VALID / "verification" / "attempt_002-plan.json", plan)
    _write(VALID / "verification" / "attempt_002-decision.json", decision)
    _write(VALID / "verification" / "attempt_002-review-package.json", review)

    invalid_root = VALID.parent / "invalid"
    invalid_binary = decision.model_dump(mode="json")
    invalid_binary["semantic_status"] = "unclear"
    invalid_binary["normalized_from"] = "unclear"
    _write(invalid_root / "binary_unclear_marked_accepted.json", invalid_binary)
    invalid_review_time = outcome.model_dump(mode="json")
    invalid_review_time["review_time_accounting_status"] = "unknown"
    _write(
        invalid_root / "human_outcome_unknown_review_time_as_number.json",
        invalid_review_time,
    )
    invalid_trace_use = outcome.model_dump(mode="json")
    invalid_trace_use["full_trace_accounting_status"] = "unknown"
    _write(
        invalid_root / "human_outcome_unknown_full_trace_as_boolean.json",
        invalid_trace_use,
    )
    invalid_trace_metrics = metrics.model_dump(mode="json")
    invalid_trace_metrics["full_trace_accounting_status"] = "unknown"
    _write(
        invalid_root / "supervision_unknown_trace_claims_application.json",
        invalid_trace_metrics,
    )

    empty_arms = [
        GateDArm(
            strategy=strategy,  # type: ignore[arg-type]
            case_ids=[],
            eligible_cases=0,
            accepted_as_is=0,
            false_acceptances=0,
            total_cost=MoneyAccounting(
                amount=None,
                currency=None,
                accounting_status="unknown",
                source="founder_evidence_not_run",
            ),
            elapsed_duration=DurationAccounting(
                duration_ms=None,
                accounting_status="unknown",
                source="founder_evidence_not_run",
            ),
            review_minutes=None,
            review_time_accounting_status="unknown",
            explainable_routes=True,
            safe_fallback=True,
        )
        for strategy in (
            "strongest_only",
            "accepted_change_optimizer",
            "optimizer_plus_adaptive",
        )
    ]
    frozen_input = {
        "policy_version": "adaptive_verification_v1",
        "generated_at": STAMP.isoformat().replace("+00:00", "Z"),
        "arms": [item.model_dump(mode="json") for item in empty_arms],
        "evidence_references": [],
        "input_digest": canonical_digest(
            [item.model_dump(mode="json") for item in empty_arms]
        ),
    }
    _write(ROOT / "docs" / "PT9_FROZEN_GATE_D_INPUT.json", frozen_input)
    _write(
        ROOT / "docs" / "PT9_GATE_D.json",
        evaluate_gate_d(arms=empty_arms, generated_at=STAMP),
    )


if __name__ == "__main__":
    main()
