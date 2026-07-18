"""Metrics, answer-first reports, and the conservative Founder Gate (Gate B)."""

from __future__ import annotations

import hashlib
import html
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

from villani_ops.closed_loop.durable_io import write_json_atomic

from .models import (
    ConfidenceInterval,
    EvaluationReport,
    EvaluationTask,
    EvaluationTrial,
    GateCheck,
    HumanReview,
    MetricValue,
)
from .reviews import latest_reviews, load_reviews
from .workspace import canonical_digest, load_suite, load_task, utc_now, validate_suite


def load_trials(suite_directory: str | Path) -> tuple[EvaluationTrial, ...]:
    root = Path(suite_directory).expanduser().resolve()
    trials: list[EvaluationTrial] = []
    for path in sorted((root / "trials").glob("*/trial.json")):
        trials.append(
            EvaluationTrial.model_validate_json(path.read_text(encoding="utf-8"))
        )
    identities = [item.trial_id for item in trials]
    if len(set(identities)) != len(identities):
        raise ValueError("evaluation trial identities are not unique")
    return tuple(trials)


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _wilson(successes: int, total: int) -> ConfidenceInterval:
    if total == 0:
        return ConfidenceInterval(
            method="wilson_score",
            estimate=None,
            lower=None,
            upper=None,
            sample_count=0,
            status="insufficient_evidence",
        )
    z = 1.959963984540054
    estimate = successes / total
    denominator = 1 + z * z / total
    centre = (estimate + z * z / (2 * total)) / denominator
    spread = (
        z
        * math.sqrt(estimate * (1 - estimate) / total + z * z / (4 * total * total))
        / denominator
    )
    return ConfidenceInterval(
        method="wilson_score",
        estimate=estimate,
        lower=max(0.0, centre - spread),
        upper=min(1.0, centre + spread),
        sample_count=total,
        status="available",
    )


def _median_interval(
    values: Sequence[float], *, seed: int = 20260717
) -> ConfidenceInterval:
    if not values:
        return ConfidenceInterval(
            method="deterministic_nonparametric_bootstrap_median",
            estimate=None,
            lower=None,
            upper=None,
            sample_count=0,
            status="insufficient_evidence",
        )
    estimate = float(median(values))
    if len(values) == 1:
        return ConfidenceInterval(
            method="deterministic_nonparametric_bootstrap_median",
            estimate=estimate,
            lower=None,
            upper=None,
            sample_count=1,
            status="insufficient_evidence",
        )
    rng = random.Random(seed)
    samples = sorted(
        float(median([values[rng.randrange(len(values))] for _ in values]))
        for _ in range(2000)
    )
    return ConfidenceInterval(
        method="deterministic_nonparametric_bootstrap_median",
        estimate=estimate,
        lower=samples[49],
        upper=samples[1949],
        sample_count=len(values),
        status="available",
    )


def _rate(successes: int, total: int) -> MetricValue:
    return MetricValue(
        value=_safe_ratio(successes, total),
        numerator=successes,
        denominator=total,
        unit="ratio",
        accounting_status="complete" if total else "not_defined",
        interval=_wilson(successes, total),
    )


def _median_metric(values: Sequence[float], *, unit: str) -> MetricValue:
    interval = _median_interval(values)
    return MetricValue(
        value=interval.estimate,
        numerator=None,
        denominator=len(values),
        unit=unit,
        accounting_status="complete" if values else "not_defined",
        interval=interval,
    )


def _per_success_metric(
    values: Sequence[float | None], successes: int, *, unit: str
) -> MetricValue:
    if not successes:
        return MetricValue(
            value=None,
            numerator=None,
            denominator=0,
            unit=unit,
            accounting_status="not_defined",
        )
    if any(value is None for value in values):
        known_values = [value for value in values if value is not None]
        return MetricValue(
            value=None,
            numerator=sum(known_values) if known_values else None,
            denominator=successes,
            unit=unit,
            accounting_status="unknown",
        )
    known_values = [float(value) for value in values if value is not None]
    total = sum(known_values)
    return MetricValue(
        value=total / successes,
        numerator=total,
        denominator=successes,
        unit=unit,
        accounting_status="complete",
    )


def _cost_per_success_metric(
    trials: Sequence[EvaluationTrial], successes: int
) -> MetricValue:
    currencies = {
        trial.total_cost.currency
        for trial in trials
        if trial.total_cost.value is not None
    }
    unit = next(iter(currencies)) if len(currencies) == 1 else None
    if not successes:
        return MetricValue(
            value=None,
            numerator=None,
            denominator=0,
            unit=unit,
            accounting_status="not_defined",
        )
    if len(currencies) != 1 or any(
        trial.total_cost.accounting_status != "complete"
        or trial.total_cost.value is None
        for trial in trials
    ):
        known_values = [
            trial.total_cost.value
            for trial in trials
            if trial.total_cost.value is not None
        ]
        return MetricValue(
            value=None,
            numerator=sum(known_values) if known_values else None,
            denominator=successes,
            unit=unit,
            accounting_status="unknown",
        )
    total = sum(
        float(trial.total_cost.value)
        for trial in trials
        if trial.total_cost.value is not None
    )
    return MetricValue(
        value=total / successes,
        numerator=total,
        denominator=successes,
        unit=unit,
        accounting_status="complete",
    )


def _latest_review_map(reviews: Iterable[HumanReview]) -> dict[str, HumanReview]:
    return latest_reviews(reviews)


def _trial_ref(value: str) -> str:
    return "ref_" + hashlib.sha256(value.encode()).hexdigest()[:16]


def _public_reason(value: str) -> str:
    redacted = re.sub(
        r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        value,
    )
    redacted = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", redacted)
    return redacted[:240]


def _arm_trials(trials: Sequence[EvaluationTrial], arm: str) -> list[EvaluationTrial]:
    return [
        trial
        for trial in trials
        if trial.arm == arm and trial.status == "completed" and trial.evidence_eligible
    ]


def _metrics(
    trials: Sequence[EvaluationTrial], reviews: Sequence[HumanReview]
) -> tuple[
    dict[str, MetricValue],
    dict[str, MetricValue],
    dict[str, MetricValue],
    dict[str, MetricValue],
    dict[str, MetricValue],
]:
    review_map = _latest_review_map(reviews)
    reliability: dict[str, MetricValue] = {}
    review_time: dict[str, MetricValue] = {}
    cost: dict[str, MetricValue] = {}
    supervision: dict[str, MetricValue] = {}
    false_acceptance: dict[str, MetricValue] = {}
    for arm in ("direct", "villani"):
        rows = _arm_trials(trials, arm)
        proved = sum(trial.proved_acceptable is True for trial in rows)
        reliability[f"{arm}.proved_acceptable_rate"] = _rate(proved, len(rows))
        reviewed = [
            (trial, review_map[trial.trial_id])
            for trial in rows
            if trial.trial_id in review_map
        ]
        accepted_as_is = sum(
            review.outcome == "accepted_as_is" for _trial, review in reviewed
        )
        accepted_after = sum(
            review.outcome == "accepted_after_correction" for _trial, review in reviewed
        )
        reliability[f"{arm}.human_accepted_as_is_rate"] = _rate(
            accepted_as_is, len(reviewed)
        )
        reliability[f"{arm}.accepted_after_correction_rate"] = _rate(
            accepted_after, len(reviewed)
        )
        false_positive = sum(review.false_acceptance for _trial, review in reviewed)
        false_negative = sum(review.false_rejection for _trial, review in reviewed)
        false_acceptance[f"{arm}.false_acceptance_rate"] = _rate(
            false_positive, len(reviewed)
        )
        false_acceptance[f"{arm}.false_rejection_rate"] = _rate(
            false_negative, len(reviewed)
        )
        review_time[f"{arm}.median_review_minutes"] = _median_metric(
            [review.review_minutes for _trial, review in reviewed], unit="minutes"
        )
        cost[f"{arm}.cost_per_proved_acceptable_change"] = _cost_per_success_metric(
            rows, proved
        )
        cost[f"{arm}.total_cost_per_human_accepted_as_is_change"] = (
            _cost_per_success_metric(rows, accepted_as_is)
        )
        review_time[f"{arm}.elapsed_time_per_accepted_as_is_change_ms"] = (
            _per_success_metric(
                [
                    (
                        float(trial.duration.value_ms)
                        if trial.duration.accounting_status == "complete"
                        and trial.duration.value_ms is not None
                        else None
                    )
                    for trial in rows
                ],
                accepted_as_is,
                unit="milliseconds",
            )
        )
        supervision[f"{arm}.attempts_per_accepted_change"] = _per_success_metric(
            [float(trial.attempts) for trial in rows],
            accepted_as_is,
            unit="attempts_per_change",
        )
        supervision[f"{arm}.escalation_frequency"] = _rate(
            sum(trial.escalations > 0 for trial in rows), len(rows)
        )
        disagreement_rows = [
            trial for trial in rows if trial.verifier_disagreement is not None
        ]
        supervision[f"{arm}.verifier_disagreement"] = _rate(
            sum(trial.verifier_disagreement is True for trial in disagreement_rows),
            len(disagreement_rows),
        )
        all_arm = [
            trial for trial in trials if trial.arm == arm and trial.evidence_eligible
        ]
        supervision[f"{arm}.infrastructure_exclusion_rate"] = _rate(
            sum(trial.status == "excluded" for trial in all_arm), len(all_arm)
        )
        cost[f"{arm}.unknown_cost_rate"] = _rate(
            sum(trial.total_cost.accounting_status != "complete" for trial in rows),
            len(rows),
        )
    return reliability, review_time, cost, supervision, false_acceptance


def _paired_deltas(
    trials: Sequence[EvaluationTrial], review_map: dict[str, HumanReview]
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int], dict[str, EvaluationTrial]] = defaultdict(dict)
    for trial in trials:
        if trial.status == "completed" and trial.evidence_eligible:
            groups[(trial.task_id, trial.repetition)][trial.arm] = trial
    rows: list[dict[str, Any]] = []
    for (task_id, repetition), arms in sorted(groups.items()):
        if set(arms) != {"direct", "villani"}:
            continue
        direct, villani = arms["direct"], arms["villani"]
        direct_review = review_map.get(direct.trial_id)
        villani_review = review_map.get(villani.trial_id)
        rows.append(
            {
                "task_reference": _trial_ref(task_id),
                "repetition": repetition,
                "proved_acceptable_delta": int(villani.proved_acceptable is True)
                - int(direct.proved_acceptable is True),
                "accepted_as_is_delta": (
                    int(villani_review.outcome == "accepted_as_is")
                    - int(direct_review.outcome == "accepted_as_is")
                    if direct_review and villani_review
                    else None
                ),
                "cost_delta": (
                    villani.total_cost.value - direct.total_cost.value
                    if villani.total_cost.value is not None
                    and direct.total_cost.value is not None
                    and villani.total_cost.currency == direct.total_cost.currency
                    else None
                ),
                "duration_delta_ms": (
                    (villani.duration.value_ms or 0) - (direct.duration.value_ms or 0)
                    if villani.duration.value_ms is not None
                    and direct.duration.value_ms is not None
                    else None
                ),
                "review_minutes_delta": (
                    villani_review.review_minutes - direct_review.review_minutes
                    if direct_review and villani_review
                    else None
                ),
            }
        )
    return rows


def _confusion(
    trials: Sequence[EvaluationTrial], review_map: dict[str, HumanReview]
) -> tuple[dict[str, int | None], dict[str, float | None], list[dict[str, Any]]]:
    tp = fp = tn = fn = 0
    wrong: list[dict[str, Any]] = []
    for trial in trials:
        review = review_map.get(trial.trial_id)
        if (
            not trial.evidence_eligible
            or trial.status != "completed"
            or trial.proved_acceptable is None
            or review is None
        ):
            continue
        predicted = trial.proved_acceptable
        actual = review.outcome == "accepted_as_is"
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1
        if predicted != actual:
            wrong.append(
                {
                    "trial_reference": _trial_ref(trial.trial_id),
                    "arm": trial.arm,
                    "verification": predicted,
                    "human_accepted_as_is": actual,
                    "review_id": review.review_id,
                }
            )
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    specificity = _safe_ratio(tn, tn + fp)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return (
        {
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
        },
        {
            "precision": precision,
            "recall": recall,
            "specificity": specificity,
            "f1": f1,
        },
        wrong,
    )


def _gate_check(
    check_id: str,
    status: str,
    actual: Any,
    required: Any,
    reason: str,
) -> GateCheck:
    return GateCheck(
        check_id=check_id,
        status=status,
        actual=actual,
        required=required,
        reason=reason,
    )


def founder_gate(
    *,
    suite_directory: Path,
    tasks: Sequence[EvaluationTask],
    trials: Sequence[EvaluationTrial],
    reviews: Sequence[HumanReview],
    reliability: dict[str, MetricValue],
    review_time: dict[str, MetricValue],
    cost: dict[str, MetricValue],
    disclosures_complete: bool,
) -> tuple[str, list[GateCheck]]:
    suite = load_suite(suite_directory)
    review_map = _latest_review_map(reviews)
    groups: dict[str, set[str]] = defaultdict(set)
    for trial in trials:
        if trial.status == "completed" and trial.evidence_eligible:
            groups[trial.task_id].add(trial.arm)
    paired_task_ids = {
        task_id for task_id, arms in groups.items() if arms == {"direct", "villani"}
    }
    paired_count = len(paired_task_ids)
    checks: list[GateCheck] = [
        _gate_check(
            "minimum_paired_tasks",
            "pass" if paired_count >= 30 else "insufficient_evidence",
            paired_count,
            30,
            "Gate B requires at least 30 distinct paired real tasks.",
        )
    ]
    repositories = {
        task.source_snapshot.repository_identity
        for task in tasks
        if task.task_id in paired_task_ids and task.evidence_eligible
    }
    checks.append(
        _gate_check(
            "minimum_repositories",
            "pass" if len(repositories) >= 2 else "insufficient_evidence",
            len(repositories),
            2,
            "Gate B requires evidence from at least two repositories.",
        )
    )
    validation = validate_suite(suite_directory)
    checks.append(
        _gate_check(
            "valid_immutable_baselines",
            "pass" if validation["valid"] else "fail",
            validation["valid"],
            True,
            "Every baseline must remain content-addressed and restorable.",
        )
    )
    paired_trials = [
        trial
        for trial in trials
        if trial.task_id in paired_task_ids
        and trial.status == "completed"
        and trial.evidence_eligible
    ]
    complete_reviews = all(
        trial.trial_id in review_map for trial in paired_trials
    ) and bool(paired_trials)
    known_false_acceptances = sum(
        review_map[trial.trial_id].false_acceptance
        for trial in paired_trials
        if trial.trial_id in review_map
    )
    false_status = (
        "fail"
        if known_false_acceptances
        else "pass"
        if complete_reviews
        else "insufficient_evidence"
    )
    checks.append(
        _gate_check(
            "zero_known_false_acceptance",
            false_status,
            known_false_acceptances,
            0,
            "All paired trials must be reviewed and no known false acceptance may remain.",
        )
    )
    direct_accept = reliability["direct.human_accepted_as_is_rate"]
    villani_accept = reliability["villani.human_accepted_as_is_rate"]
    if (
        direct_accept.value is None
        or villani_accept.value is None
        or not complete_reviews
    ):
        accepted_status = "insufficient_evidence"
    else:
        accepted_status = (
            "pass" if villani_accept.value >= direct_accept.value else "fail"
        )
    checks.append(
        _gate_check(
            "accepted_as_is_non_inferiority",
            accepted_status,
            {
                "direct": direct_accept.value,
                "villani": villani_accept.value,
            },
            "villani >= direct",
            "Villani's human accepted-as-is rate may not be lower.",
        )
    )
    direct_review = review_time["direct.median_review_minutes"].value
    villani_review = review_time["villani.median_review_minutes"].value
    review_reduction = (
        (direct_review - villani_review) / direct_review
        if direct_review not in {None, 0} and villani_review is not None
        else None
    )
    direct_cost = cost["direct.total_cost_per_human_accepted_as_is_change"].value
    villani_cost = cost["villani.total_cost_per_human_accepted_as_is_change"].value
    cost_units_match = (
        cost["direct.total_cost_per_human_accepted_as_is_change"].unit
        == cost["villani.total_cost_per_human_accepted_as_is_change"].unit
        and cost["direct.total_cost_per_human_accepted_as_is_change"].unit is not None
    )
    cost_reduction = (
        (direct_cost - villani_cost) / direct_cost
        if cost_units_match
        and direct_cost not in {None, 0}
        and villani_cost is not None
        else None
    )
    if review_reduction is None and cost_reduction is None:
        improvement_status = "insufficient_evidence"
    else:
        improvement_status = (
            "pass"
            if (review_reduction is not None and review_reduction >= 0.30)
            or (cost_reduction is not None and cost_reduction >= 0.25)
            else "fail"
        )
    checks.append(
        _gate_check(
            "review_or_cost_improvement",
            improvement_status,
            {
                "median_review_time_reduction": review_reduction,
                "total_cost_per_accepted_change_reduction": cost_reduction,
            },
            {"review_time": 0.30, "cost": 0.25, "operator": "either"},
            "Require 30% lower median review time or 25% lower total cost per accepted change.",
        )
    )
    automatic = sum(trial.configuration_mode == "automatic" for trial in paired_trials)
    automatic_rate = _safe_ratio(automatic, len(paired_trials))
    checks.append(
        _gate_check(
            "automatic_configuration",
            (
                "insufficient_evidence"
                if automatic_rate is None
                else "pass"
                if automatic_rate >= 0.80
                else "fail"
            ),
            automatic_rate,
            0.80,
            "At least 80% of eligible paired trials must use automatic configuration.",
        )
    )
    checks.append(
        _gate_check(
            "complete_unknown_and_exclusion_disclosure",
            "pass" if disclosures_complete and suite.disclosure_complete else "fail",
            bool(disclosures_complete and suite.disclosure_complete),
            True,
            "Unknown accounting and exclusions must be explicit and the frozen suite must attest disclosure.",
        )
    )
    real_evidence = suite.evidence_kind == "real_founder_work" and all(
        task.evidence_kind == "real_founder_work"
        for task in tasks
        if task.task_id in paired_task_ids
    )
    checks.append(
        _gate_check(
            "real_founder_evidence_only",
            "pass" if real_evidence and paired_task_ids else "insufficient_evidence",
            real_evidence and bool(paired_task_ids),
            True,
            "Synthetic fixtures are always excluded from Founder Gate evidence.",
        )
    )
    statuses = {item.status for item in checks}
    status = (
        "FAIL"
        if "fail" in statuses
        else "INSUFFICIENT_EVIDENCE"
        if "insufficient_evidence" in statuses
        else "PASS"
    )
    return status, checks


def build_report(suite_directory: str | Path) -> EvaluationReport:
    root = Path(suite_directory).expanduser().resolve()
    suite = load_suite(root)
    if suite.content_digest is None:
        raise ValueError("reporting requires a frozen suite")
    tasks = [load_task(root, item.task_id) for item in suite.task_versions]
    trials = list(load_trials(root))
    reviews = list(load_reviews(root))
    review_map = _latest_review_map(reviews)
    reliability, review_time, cost, supervision, false_acceptance = _metrics(
        trials, reviews
    )
    paired = _paired_deltas(trials, review_map)
    confusion, classification_metrics, wrong = _confusion(trials, review_map)
    unknowns = [
        {
            "trial_reference": _trial_ref(trial.trial_id),
            "field": "total_cost",
            "accounting_status": trial.total_cost.accounting_status,
        }
        for trial in trials
        if trial.total_cost.accounting_status != "complete"
    ]
    exclusions = [
        {
            "trial_reference": _trial_ref(trial.trial_id),
            "arm": trial.arm,
            "reason": _public_reason(
                trial.exclusion_reason or "missing exclusion reason"
            ),
        }
        for trial in trials
        if trial.status == "excluded"
    ]
    disclosures_complete = all(
        item["reason"] != "missing exclusion reason" for item in exclusions
    )
    class_counts: Counter[tuple[str, str]] = Counter()
    for task in tasks:
        for category in task.category_labels or ["uncategorized"]:
            for risk in task.risk_labels or ["unlabelled"]:
                class_counts[(category, risk)] += 1
    task_classes = [
        {"category": category, "risk": risk, "task_count": count}
        for (category, risk), count in sorted(class_counts.items())
    ]
    failure_counter = Counter(
        trial.exclusion_reason or "verification_could_not_prove"
        for trial in trials
        if trial.status == "excluded" or trial.proved_acceptable is False
    )
    failure_modes = [
        {"failure_mode": _public_reason(reason), "count": count}
        for reason, count in failure_counter.most_common()
    ]
    missing_evidence = [
        {"kind": "unknown_cost", "count": len(unknowns)},
        {
            "kind": "missing_human_review",
            "count": sum(
                trial.status == "completed"
                and trial.evidence_eligible
                and trial.trial_id not in review_map
                for trial in trials
            ),
        },
        {"kind": "infrastructure_exclusion", "count": len(exclusions)},
    ]
    cost_decomposition: list[dict[str, Any]] = []
    for arm in ("direct", "villani"):
        rows = _arm_trials(trials, arm)
        for component in ("execution_cost", "verification_cost", "local_compute_cost"):
            amounts = [getattr(trial, component) for trial in rows]
            known_by_currency: dict[str, float] = defaultdict(float)
            for amount in amounts:
                if amount.value is not None and amount.currency is not None:
                    known_by_currency[amount.currency] += amount.value
            cost_decomposition.append(
                {
                    "arm": arm,
                    "component": component,
                    "known_totals_by_currency": dict(sorted(known_by_currency.items())),
                    "known_count": sum(item.value is not None for item in amounts),
                    "unknown_count": sum(
                        item.accounting_status in {"unknown", "partial"}
                        for item in amounts
                    ),
                }
            )
    route_counts: Counter[tuple[str, str, str, str]] = Counter()
    for trial in trials:
        if trial.status == "completed":
            route_counts[
                (
                    trial.arm,
                    trial.agent_system.harness,
                    trial.agent_system.provider or "unknown",
                    trial.agent_system.model or "unknown",
                )
            ] += 1
    route_decomposition = [
        {
            "arm": arm,
            "harness": harness,
            "provider": provider,
            "model": model,
            "trial_count": count,
        }
        for (arm, harness, provider, model), count in sorted(route_counts.items())
    ]
    gate_status, gate_checks = founder_gate(
        suite_directory=root,
        tasks=tasks,
        trials=trials,
        reviews=reviews,
        reliability=reliability,
        review_time=review_time,
        cost=cost,
        disclosures_complete=disclosures_complete,
    )
    generated_at = utc_now()
    report_id = (
        "report_"
        + canonical_digest(
            {
                "suite": suite.content_digest,
                "trials": [trial.trial_id for trial in trials],
                "reviews": [review.review_id for review in reviews],
                "generated_at": generated_at.isoformat(),
            }
        )[:24]
    )
    return EvaluationReport(
        report_id=report_id,
        suite_id=suite.suite_id,
        suite_digest=suite.content_digest,
        generated_at=generated_at,
        evidence_kind=suite.evidence_kind,
        confidentiality=suite.confidentiality,
        raw_counts={
            "tasks": len(tasks),
            "trials": len(trials),
            "completed_trials": sum(trial.status == "completed" for trial in trials),
            "excluded_trials": len(exclusions),
            "eligible_trials": sum(trial.evidence_eligible for trial in trials),
            "human_review_records": len(reviews),
            "latest_human_reviews": len(review_map),
            "paired_task_repetitions": len(paired),
            "synthetic_trials_excluded_from_gate": sum(
                not trial.evidence_eligible for trial in trials
            ),
        },
        reliability=reliability,
        review_time=review_time,
        cost=cost,
        supervision=supervision,
        false_acceptance=false_acceptance,
        paired_task_deltas=paired,
        task_classes=task_classes,
        failure_modes=failure_modes,
        missing_evidence=missing_evidence,
        confusion_matrix=confusion,
        classification_metrics=classification_metrics,
        calibration={
            "status": "not_defined",
            "reason": "The acceptance process emits a binary proved/not-proved result and records no success probability.",
            "probability_fabricated": False,
        },
        verifier_wrong_cases=wrong,
        cost_decomposition=cost_decomposition,
        route_decomposition=route_decomposition,
        trial_bundle_links=[f"trials/{trial.trial_id}/trial.json" for trial in trials],
        unknowns=unknowns,
        exclusions=exclusions,
        disclosures_complete=disclosures_complete,
        founder_gate_status=gate_status,
        founder_gate_checks=gate_checks,
    )


def _format(value: float | None, *, percent: bool = False) -> str:
    if value is None:
        return "unknown"
    return f"{value * 100:.1f}%" if percent else f"{value:.3f}"


def _format_metric(metric: MetricValue, *, percent: bool = False) -> str:
    rendered = _format(metric.value, percent=percent)
    if metric.value is not None and metric.unit not in {None, "ratio"}:
        return f"{rendered} {metric.unit}"
    return rendered


def _metric_rows(metrics: dict[str, MetricValue]) -> list[str]:
    rows: list[str] = []
    for name, metric in sorted(metrics.items()):
        interval = metric.interval
        if interval is not None and interval.status == "available":
            interval_text = (
                f"{_format(interval.lower)} to {_format(interval.upper)} "
                f"({interval.method})"
            )
        elif interval is not None:
            interval_text = "insufficient evidence"
        else:
            interval_text = "not defined"
        if metric.numerator is not None and metric.denominator is not None:
            count_text = f"{metric.numerator}/{metric.denominator}"
        elif metric.denominator is not None:
            count_text = str(metric.denominator)
        else:
            count_text = "-"
        rows.append(
            f"| {name} | {_format_metric(metric)} | {count_text} | "
            f"{interval_text} | {metric.accounting_status} |"
        )
    return rows


def _markdown(report: EvaluationReport) -> str:
    reliability = report.reliability
    review = report.review_time
    cost = report.cost
    lines = [
        "# Villani Founder Thesis Lab",
        "",
        f"Gate B: **{report.founder_gate_status}**",
        "",
        f"Confidentiality: **{report.confidentiality}**",
        "",
        "This report compares paired, immutable-baseline trials. It makes no automatic significance claim; raw counts and uncertainty are shown below.",
        "",
        "## Answer first",
        "",
        "| Measure | Direct | Villani |",
        "|---|---:|---:|",
        f"| Proved acceptable | {_format(reliability['direct.proved_acceptable_rate'].value, percent=True)} | {_format(reliability['villani.proved_acceptable_rate'].value, percent=True)} |",
        f"| Human accepted as-is | {_format(reliability['direct.human_accepted_as_is_rate'].value, percent=True)} | {_format(reliability['villani.human_accepted_as_is_rate'].value, percent=True)} |",
        f"| Median review minutes | {_format_metric(review['direct.median_review_minutes'])} | {_format_metric(review['villani.median_review_minutes'])} |",
        f"| Total cost / accepted as-is | {_format_metric(cost['direct.total_cost_per_human_accepted_as_is_change'])} | {_format_metric(cost['villani.total_cost_per_human_accepted_as_is_change'])} |",
        "",
        "## Raw counts",
        "",
    ]
    lines.extend(
        f"- {key}: {value}" for key, value in sorted(report.raw_counts.items())
    )
    for title, metrics in (
        ("Reliability", report.reliability),
        ("Review time", report.review_time),
        ("Cost", report.cost),
        ("Supervision burden", report.supervision),
        ("False acceptance and rejection", report.false_acceptance),
    ):
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Measure | Value | Raw count | 95% interval | Accounting |",
                "|---|---:|---:|---|---|",
                *_metric_rows(metrics),
            ]
        )
    lines.extend(
        [
            "",
            "## Founder Gate",
            "",
            "| Check | Status | Actual | Required |",
            "|---|---|---|---|",
        ]
    )
    for check in report.founder_gate_checks:
        lines.append(
            f"| {check.check_id} | {check.status} | `{json.dumps(check.actual, sort_keys=True)}` | `{json.dumps(check.required, sort_keys=True)}` |"
        )
    lines.extend(
        [
            "",
            "## False acceptance and confusion matrix",
            "",
            f"- confusion matrix: `{json.dumps(report.confusion_matrix, sort_keys=True)}`",
            f"- precision/recall/specificity/F1: `{json.dumps(report.classification_metrics, sort_keys=True)}`",
            f"- calibration: {report.calibration['status']} - {report.calibration['reason']}",
            f"- verifier wrong cases: `{json.dumps(report.verifier_wrong_cases, sort_keys=True)}`",
            "",
            "## Task classes and failure modes",
            "",
            f"- task classes: `{json.dumps(report.task_classes, sort_keys=True)}`",
            f"- failure modes: `{json.dumps(report.failure_modes, sort_keys=True)}`",
            "",
            "## Missing evidence, unknowns, and exclusions",
            "",
        ]
    )
    lines.extend(
        f"- {item['kind']}: {item['count']}" for item in report.missing_evidence
    )
    lines.extend(
        [
            f"- unknowns: `{json.dumps(report.unknowns, sort_keys=True)}`",
            f"- exclusions: `{json.dumps(report.exclusions, sort_keys=True)}`",
        ]
    )
    lines.extend(
        [
            "",
            "## Cost decomposition",
            "",
            "```json",
            json.dumps(report.cost_decomposition, indent=2, sort_keys=True),
            "```",
            "",
            "## Route decomposition",
            "",
            "```json",
            json.dumps(report.route_decomposition, indent=2, sort_keys=True),
            "```",
            "",
            "## Trial bundles",
            "",
        ]
    )
    lines.extend(f"- [{path}]({path})" for path in report.trial_bundle_links)
    return "\n".join(lines) + "\n"


def _html_report(report: EvaluationReport) -> str:
    def escaped(value: Any) -> str:
        return html.escape(str(value))

    answer_rows = "".join(
        f"<tr><th>{escaped(label)}</th><td>{escaped(direct)}</td>"
        f"<td>{escaped(villani)}</td></tr>"
        for label, direct, villani in (
            (
                "Proved acceptable",
                _format(
                    report.reliability["direct.proved_acceptable_rate"].value,
                    percent=True,
                ),
                _format(
                    report.reliability["villani.proved_acceptable_rate"].value,
                    percent=True,
                ),
            ),
            (
                "Human accepted as-is",
                _format(
                    report.reliability["direct.human_accepted_as_is_rate"].value,
                    percent=True,
                ),
                _format(
                    report.reliability["villani.human_accepted_as_is_rate"].value,
                    percent=True,
                ),
            ),
            (
                "Median review minutes",
                _format_metric(report.review_time["direct.median_review_minutes"]),
                _format_metric(report.review_time["villani.median_review_minutes"]),
            ),
            (
                "Total cost / accepted as-is",
                _format_metric(
                    report.cost["direct.total_cost_per_human_accepted_as_is_change"]
                ),
                _format_metric(
                    report.cost["villani.total_cost_per_human_accepted_as_is_change"]
                ),
            ),
        )
    )
    gate_rows = "".join(
        "<tr>"
        f"<th>{escaped(check.check_id)}</th><td>{escaped(check.status)}</td>"
        f"<td><code>{escaped(json.dumps(check.actual, sort_keys=True))}</code></td>"
        f"<td><code>{escaped(json.dumps(check.required, sort_keys=True))}</code></td>"
        "</tr>"
        for check in report.founder_gate_checks
    )
    trial_links = "".join(
        f'<li><a href="{html.escape(path, quote=True)}">{escaped(path)}</a></li>'
        for path in report.trial_bundle_links
    )
    detail = html.escape(
        json.dumps(
            {
                "raw_counts": report.raw_counts,
                "reliability": {
                    key: value.model_dump(mode="json")
                    for key, value in report.reliability.items()
                },
                "review_time": {
                    key: value.model_dump(mode="json")
                    for key, value in report.review_time.items()
                },
                "cost": {
                    key: value.model_dump(mode="json")
                    for key, value in report.cost.items()
                },
                "supervision": {
                    key: value.model_dump(mode="json")
                    for key, value in report.supervision.items()
                },
                "false_acceptance": {
                    key: value.model_dump(mode="json")
                    for key, value in report.false_acceptance.items()
                },
                "task_classes": report.task_classes,
                "failure_modes": report.failure_modes,
                "missing_evidence": report.missing_evidence,
                "confusion_matrix": report.confusion_matrix,
                "classification_metrics": report.classification_metrics,
                "calibration": report.calibration,
                "verifier_wrong_cases": report.verifier_wrong_cases,
                "cost_decomposition": report.cost_decomposition,
                "route_decomposition": report.route_decomposition,
                "unknowns": report.unknowns,
                "exclusions": report.exclusions,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Villani Founder Thesis Lab</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;max-width:72rem;margin:auto;padding:2rem;color:#171717;background:#fff}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}th,td{{border:1px solid #ccc;padding:.55rem;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;background:#f5f5f5;border:1px solid #ddd;padding:1rem;overflow:auto}}a{{color:#222}}.gate{{font-size:1.2rem}}</style>
</head><body><h1>Villani Founder Thesis Lab</h1><p class="gate">Gate B: <strong>{escaped(report.founder_gate_status)}</strong></p><p>Confidentiality: <strong>{escaped(report.confidentiality)}</strong></p>
<p>This answer-first report makes no automatic significance claim. Raw counts and uncertainty remain visible.</p>
<h2>Answer first</h2><table><thead><tr><th>Measure</th><th>Direct</th><th>Villani</th></tr></thead><tbody>{answer_rows}</tbody></table>
<h2>Founder Gate</h2><table><thead><tr><th>Check</th><th>Status</th><th>Actual</th><th>Required</th></tr></thead><tbody>{gate_rows}</tbody></table>
<h2>Reliability, review time, cost, false acceptance, task classes, and failure modes</h2><pre>{detail}</pre>
<h2>Trial bundles</h2><ul>{trial_links}</ul></body></html>
"""


def write_reports(
    suite_directory: str | Path,
    *,
    json_output: str | Path | None = None,
    markdown_output: str | Path | None = None,
    html_output: str | Path | None = None,
) -> tuple[EvaluationReport, Path, Path, Path]:
    root = Path(suite_directory).expanduser().resolve()
    report = build_report(root)
    json_path = (
        Path(json_output).resolve() if json_output else root / "evaluation-report.json"
    )
    markdown_path = (
        Path(markdown_output).resolve()
        if markdown_output
        else root / "evaluation-report.md"
    )
    html_path = (
        Path(html_output).resolve() if html_output else root / "evaluation-report.html"
    )
    write_json_atomic(json_path, report.model_dump(mode="json"))
    markdown = _markdown(report)
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(_html_report(report), encoding="utf-8")
    return report, json_path, markdown_path, html_path
