"""Evidence-bounded PT4 failure analysis and founder-proof certification.

This module is deliberately outside the controller and policy packages.  It
can summarize explicit founder-evaluation evidence, but it cannot change a
coding route, retry, verification outcome, candidate selection, or delivery.
When the evidence population is insufficient, every production-change and
certificate decision fails closed.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from statistics import mean
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from villani_ops.closed_loop.durable_io import write_json_atomic

from .models import (
    AccountingAmount,
    EvaluationReport,
    EvaluationTask,
    EvaluationTrial,
    HumanReview,
)
from .reporting import build_report, load_trials
from .reviews import latest_reviews, load_reviews
from .workspace import canonical_digest, load_suite, load_task, utc_now, validate_suite


FailureTaxonomy = Literal[
    "task_misunderstanding",
    "irrelevant_navigation",
    "context_waste",
    "patch_quality",
    "validation_failure",
    "missing_validation",
    "verifier_false_reject",
    "verifier_false_accept",
    "runner_infrastructure",
    "model_capability",
    "environment_mismatch",
    "retry_without_progress",
    "premature_escalation",
    "late_escalation",
    "selection_error",
    "delivery_conflict",
    "unknown_accounting",
    "user_flow_friction",
    "evidence_backed_other",
]

FAILURE_TAXONOMY: tuple[str, ...] = (
    "task_misunderstanding",
    "irrelevant_navigation",
    "context_waste",
    "patch_quality",
    "validation_failure",
    "missing_validation",
    "verifier_false_reject",
    "verifier_false_accept",
    "runner_infrastructure",
    "model_capability",
    "environment_mismatch",
    "retry_without_progress",
    "premature_escalation",
    "late_escalation",
    "selection_error",
    "delivery_conflict",
    "unknown_accounting",
    "user_flow_friction",
    "evidence_backed_other",
)

PT4_MINIMUM_PAIRED_REAL_TASKS = 20
REPEATED_CLUSTER_MINIMUM_TASKS = 2
PRIORITIZATION_FORMULA = (
    "frequency x recoverable accepted-change loss x average cost or "
    "supervision burden x diagnostic confidence"
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _safe_artifact_reference(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact references must be safe relative paths")
    return path.as_posix()


def _opaque_reference(kind: str, value: str) -> str:
    return f"{kind}_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class FailureObservation(_StrictModel):
    """Explicit human/evaluator evidence for one generic failure mechanism."""

    observation_id: str = Field(min_length=1)
    taxonomy: FailureTaxonomy
    mechanism: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    task_reference: str = Field(min_length=1)
    repository_reference: str = Field(min_length=1)
    task_classes: list[str] = Field(default_factory=list)
    agent_system: str = Field(min_length=1)
    cost_impact: AccountingAmount
    review_minutes_impact: float | None = Field(default=None, ge=0)
    recoverable_accepted_change_loss: float = Field(ge=0, le=1)
    diagnostic_confidence: float = Field(ge=0, le=1)
    artifact_references: list[str] = Field(min_length=1)
    generic_fix_exists: bool
    evidence_kind: Literal["real_founder_work", "synthetic_fixture"]

    @model_validator(mode="after")
    def safe_references(self) -> "FailureObservation":
        self.artifact_references = [
            _safe_artifact_reference(value) for value in self.artifact_references
        ]
        return self


class VerifierEvidenceObservation(_StrictModel):
    """Human-labelled verifier facts without route, harness, or cost inputs."""

    case_id: str = Field(min_length=1)
    trial_reference: str = Field(min_length=1)
    verifier_proved_acceptable: bool | None
    human_accepted_as_is: bool | None
    infrastructure_exclusion: bool = False
    requirement_errors: list[str] = Field(default_factory=list)
    evidence_types: list[str] = Field(default_factory=list)
    semantic_result: bool | None = None
    deterministic_result: bool | None = None
    artifact_references: list[str] = Field(default_factory=list)
    evidence_kind: Literal["real_founder_work", "synthetic_fixture"]

    @model_validator(mode="after")
    def safe_references(self) -> "VerifierEvidenceObservation":
        self.artifact_references = [
            _safe_artifact_reference(value) for value in self.artifact_references
        ]
        return self


class FrozenTaskOutcome(_StrictModel):
    """One before/after result keyed to the exact immutable task and baseline."""

    task_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    arm: Literal["direct", "villani"]
    repetition: int = Field(ge=1)
    proved_acceptable: bool | None
    human_outcome: (
        Literal["accepted_as_is", "accepted_after_correction", "rejected"] | None
    ) = None
    total_cost: AccountingAmount
    review_minutes: float | None = Field(default=None, ge=0)
    tool_versions: dict[str, str] = Field(default_factory=dict)

    @property
    def identity(self) -> tuple[str, str, str, int]:
        return (
            self.task_digest,
            self.baseline_digest,
            self.arm,
            self.repetition,
        )


def cluster_failures(
    observations: Iterable[FailureObservation],
) -> list[dict[str, Any]]:
    """Group only real, explicitly labelled observations by generic mechanism."""

    grouped: dict[tuple[str, str], list[FailureObservation]] = defaultdict(list)
    for observation in observations:
        if observation.evidence_kind != "real_founder_work":
            continue
        grouped[(observation.taxonomy, observation.mechanism)].append(observation)

    clusters: list[dict[str, Any]] = []
    for (taxonomy, mechanism), rows in sorted(grouped.items()):
        task_references = sorted({item.task_reference for item in rows})
        repository_references = sorted({item.repository_reference for item in rows})
        cost_by_currency: dict[str, float] = defaultdict(float)
        known_costs: list[float] = []
        cost_units: set[str] = set()
        for item in rows:
            if item.cost_impact.value is not None and item.cost_impact.currency:
                cost_by_currency[item.cost_impact.currency] += item.cost_impact.value
                known_costs.append(item.cost_impact.value)
                cost_units.add(item.cost_impact.currency)
        cost_complete = (
            len(known_costs) == len(rows)
            and len(cost_units) == 1
            and all(item.cost_impact.accounting_status == "complete" for item in rows)
        )
        review_values = [
            item.review_minutes_impact
            for item in rows
            if item.review_minutes_impact is not None
        ]
        review_complete = len(review_values) == len(rows)
        confidence = float(mean(item.diagnostic_confidence for item in rows))
        recoverable_loss = float(
            mean(item.recoverable_accepted_change_loss for item in rows)
        )
        clusters.append(
            {
                "taxonomy": taxonomy,
                "mechanism": mechanism,
                "count": len(rows),
                "distinct_task_count": len(task_references),
                "repeated": len(task_references) >= REPEATED_CLUSTER_MINIMUM_TASKS,
                "repositories": [
                    _opaque_reference("repository", value)
                    for value in repository_references
                ],
                "task_classes": sorted(
                    {value for item in rows for value in item.task_classes}
                ),
                "agent_systems": sorted({item.agent_system for item in rows}),
                "cost_impact": {
                    "totals_by_currency": dict(sorted(cost_by_currency.items())),
                    "known_count": len(known_costs),
                    "unknown_count": len(rows) - len(known_costs),
                    "average": float(mean(known_costs)) if cost_complete else None,
                    "unit": next(iter(cost_units)) if cost_complete else None,
                    "accounting_status": "complete" if cost_complete else "unknown",
                },
                "review_impact": {
                    "total_minutes": (
                        float(sum(review_values)) if review_values else None
                    ),
                    "known_count": len(review_values),
                    "unknown_count": len(rows) - len(review_values),
                    "average_minutes": (
                        float(mean(review_values)) if review_complete else None
                    ),
                    "accounting_status": ("complete" if review_complete else "unknown"),
                },
                "acceptance_impact": {
                    "recoverable_accepted_change_loss_average": recoverable_loss,
                    "recoverable_accepted_change_loss_total": float(
                        sum(item.recoverable_accepted_change_loss for item in rows)
                    ),
                },
                "diagnostic_confidence": confidence,
                "linked_artifacts": sorted(
                    {value for item in rows for value in item.artifact_references}
                ),
                "generic_fix_exists": all(item.generic_fix_exists for item in rows),
                "generic_fix_evidence_consistent": len(
                    {item.generic_fix_exists for item in rows}
                )
                == 1,
            }
        )
    return clusters


def prioritize_failure_clusters(
    clusters: Sequence[dict[str, Any]],
    *,
    burden_basis: str = "review_minutes",
) -> dict[str, Any]:
    """Rank comparable repeated clusters with the exact PT4 formula."""

    ranked: list[dict[str, Any]] = []
    unranked: list[dict[str, str]] = []
    for cluster in clusters:
        identity = f"{cluster['taxonomy']}:{cluster['mechanism']}"
        if not cluster["repeated"]:
            unranked.append({"cluster": identity, "reason": "not_repeated"})
            continue
        if not cluster["generic_fix_exists"]:
            unranked.append({"cluster": identity, "reason": "no_generic_fix"})
            continue
        if burden_basis == "review_minutes":
            burden = cluster["review_impact"]["average_minutes"]
            burden_unit = "review_minutes"
        elif burden_basis.startswith("cost:"):
            requested_currency = burden_basis.split(":", 1)[1].upper()
            cost = cluster["cost_impact"]
            burden = (
                cost["average"]
                if cost["accounting_status"] == "complete"
                and cost["unit"] == requested_currency
                else None
            )
            burden_unit = requested_currency
        else:
            raise ValueError("burden basis must be review_minutes or cost:<currency>")
        if burden is None:
            unranked.append(
                {"cluster": identity, "reason": "unknown_comparable_burden"}
            )
            continue
        frequency = int(cluster["count"])
        recoverable_loss = float(
            cluster["acceptance_impact"]["recoverable_accepted_change_loss_average"]
        )
        confidence = float(cluster["diagnostic_confidence"])
        score = frequency * recoverable_loss * float(burden) * confidence
        ranked.append(
            {
                "rank": 0,
                "taxonomy": cluster["taxonomy"],
                "mechanism": cluster["mechanism"],
                "score": score,
                "frequency": frequency,
                "recoverable_accepted_change_loss": recoverable_loss,
                "average_burden": float(burden),
                "burden_unit": burden_unit,
                "diagnostic_confidence": confidence,
                "formula": PRIORITIZATION_FORMULA,
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["taxonomy"], item["mechanism"]))
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    return {
        "formula": PRIORITIZATION_FORMULA,
        "burden_basis": burden_basis,
        "status": "available" if ranked else "insufficient_evidence",
        "ranked": ranked,
        "unranked": unranked,
    }


def build_verifier_diagnostics(
    observations: Iterable[VerifierEvidenceObservation],
) -> dict[str, Any]:
    """Calculate labelled verifier diagnostics without probability fabrication."""

    rows = [item for item in observations if item.evidence_kind == "real_founder_work"]
    tp = fp = tn = fn = 0
    false_positive_cases: list[dict[str, Any]] = []
    false_negative_cases: list[dict[str, Any]] = []
    infrastructure: list[dict[str, Any]] = []
    requirement_errors: Counter[str] = Counter()
    evidence: dict[str, Counter[str]] = defaultdict(Counter)
    disagreement_cases: list[dict[str, Any]] = []
    labelled = 0
    for row in rows:
        reference = _opaque_reference("trial", row.trial_reference)
        if row.infrastructure_exclusion:
            infrastructure.append(
                {
                    "trial_reference": reference,
                    "artifacts": row.artifact_references,
                }
            )
            # Infrastructure failures are disclosed, but they are not verifier
            # classifications and must not distort the human-labelled matrix.
            continue
        if (
            row.verifier_proved_acceptable is not None
            and row.human_accepted_as_is is not None
        ):
            labelled += 1
            predicted = row.verifier_proved_acceptable
            actual = row.human_accepted_as_is
            if predicted and actual:
                tp += 1
            elif predicted and not actual:
                fp += 1
                false_positive_cases.append(
                    {
                        "trial_reference": reference,
                        "artifacts": row.artifact_references,
                    }
                )
            elif not predicted and actual:
                fn += 1
                false_negative_cases.append(
                    {
                        "trial_reference": reference,
                        "artifacts": row.artifact_references,
                    }
                )
            else:
                tn += 1
            for evidence_type in row.evidence_types:
                evidence[evidence_type]["cases"] += 1
                evidence[evidence_type]["verifier_accepts"] += int(predicted)
                evidence[evidence_type]["human_accepts"] += int(actual)
                evidence[evidence_type]["wrong"] += int(predicted != actual)
        requirement_errors.update(row.requirement_errors)
        if (
            row.semantic_result is not None
            and row.deterministic_result is not None
            and row.semantic_result != row.deterministic_result
        ):
            disagreement_cases.append(
                {
                    "trial_reference": reference,
                    "semantic_result": row.semantic_result,
                    "deterministic_result": row.deterministic_result,
                    "artifacts": row.artifact_references,
                }
            )

    def ratio(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    specificity = ratio(tn, tn + fp)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "human_labelled_cases": labelled,
        "confusion_matrix": {
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
        },
        "precision": precision,
        "recall": recall,
        "specificity": specificity,
        "f1": f1,
        "false_positive_cases": false_positive_cases,
        "false_negative_cases": false_negative_cases,
        "infrastructure_exclusions": {
            "count": len(infrastructure),
            "cases": infrastructure,
        },
        "requirement_level_errors": [
            {"requirement_error": name, "count": count}
            for name, count in sorted(requirement_errors.items())
        ],
        "evidence_type_correlations": [
            {
                "evidence_type": name,
                "cases": counts["cases"],
                "verifier_accepts": counts["verifier_accepts"],
                "human_accepts": counts["human_accepts"],
                "wrong": counts["wrong"],
            }
            for name, counts in sorted(evidence.items())
        ],
        "semantic_deterministic_disagreement": {
            "count": len(disagreement_cases),
            "cases": disagreement_cases,
        },
        "calibration": {
            "status": "not_defined",
            "reason": "Binary verification records no success probability.",
            "probability_fabricated": False,
        },
    }


def compare_frozen_outcomes(
    before: Sequence[FrozenTaskOutcome],
    after: Sequence[FrozenTaskOutcome],
) -> list[dict[str, Any]]:
    """Compare exact task/baseline identities and reject any population drift."""

    before_by_id = {item.identity: item for item in before}
    after_by_id = {item.identity: item for item in after}
    if len(before_by_id) != len(before) or len(after_by_id) != len(after):
        raise ValueError("before/after outcome identities must be unique")
    if set(before_by_id) != set(after_by_id):
        missing_after = sorted(set(before_by_id) - set(after_by_id))
        missing_before = sorted(set(after_by_id) - set(before_by_id))
        raise ValueError(
            "before/after frozen task identities differ: "
            f"missing_after={len(missing_after)}, missing_before={len(missing_before)}"
        )
    comparisons: list[dict[str, Any]] = []
    for identity in sorted(before_by_id):
        earlier = before_by_id[identity]
        later = after_by_id[identity]
        cost_delta = (
            later.total_cost.value - earlier.total_cost.value
            if earlier.total_cost.value is not None
            and later.total_cost.value is not None
            and earlier.total_cost.currency == later.total_cost.currency
            else None
        )
        review_delta = (
            later.review_minutes - earlier.review_minutes
            if earlier.review_minutes is not None and later.review_minutes is not None
            else None
        )
        comparisons.append(
            {
                "task_reference": _opaque_reference("task", identity[0]),
                "baseline_digest": identity[1],
                "arm": identity[2],
                "repetition": identity[3],
                "before_proved_acceptable": earlier.proved_acceptable,
                "after_proved_acceptable": later.proved_acceptable,
                "before_human_outcome": earlier.human_outcome,
                "after_human_outcome": later.human_outcome,
                "cost_delta": cost_delta,
                "cost_unit": (
                    earlier.total_cost.currency if cost_delta is not None else None
                ),
                "review_minutes_delta": review_delta,
                "tool_version_changes": {
                    key: {
                        "before": earlier.tool_versions.get(key),
                        "after": later.tool_versions.get(key),
                    }
                    for key in sorted(
                        set(earlier.tool_versions) | set(later.tool_versions)
                    )
                    if earlier.tool_versions.get(key) != later.tool_versions.get(key)
                },
            }
        )
    return comparisons


def _paired_real_population(
    suite_kind: str,
    tasks: Sequence[EvaluationTask],
    trials: Sequence[EvaluationTrial],
) -> tuple[set[str], list[EvaluationTrial]]:
    if suite_kind != "real_founder_work":
        return set(), []
    real_tasks = {
        task.task_id: task
        for task in tasks
        if task.evidence_kind == "real_founder_work" and task.evidence_eligible
    }
    arms: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
    for trial in trials:
        task = real_tasks.get(trial.task_id)
        if (
            task is not None
            and trial.evidence_eligible
            and trial.status == "completed"
            and trial.baseline_digest == task.immutable_baseline_digest
            and trial.task_digest == task.content_digest
        ):
            identity = (
                trial.task_id,
                trial.repetition,
                trial.task_digest,
                trial.baseline_digest,
            )
            arms[identity].add(trial.arm)
    paired_identities = {
        identity for identity, values in arms.items() if values == {"direct", "villani"}
    }
    paired = {identity[0] for identity in paired_identities}
    paired_trials = [
        trial
        for trial in trials
        if (
            trial.task_id,
            trial.repetition,
            trial.task_digest,
            trial.baseline_digest,
        )
        in paired_identities
        and trial.evidence_eligible
        and trial.status == "completed"
    ]
    return paired, paired_trials


def assess_hardening_sufficiency(
    *,
    suite_directory: Path,
    tasks: Sequence[EvaluationTask],
    trials: Sequence[EvaluationTrial],
    reviews: Sequence[HumanReview],
) -> dict[str, Any]:
    suite = load_suite(suite_directory)
    paired, paired_trials = _paired_real_population(suite.evidence_kind, tasks, trials)
    validation = validate_suite(suite_directory)
    baseline_integrity: bool | None = validation["valid"] if paired else None
    review_map = latest_reviews(reviews)
    labels_complete = bool(paired_trials) and all(
        trial.trial_id in review_map for trial in paired_trials
    )
    missing: list[str] = []
    if len(paired) < PT4_MINIMUM_PAIRED_REAL_TASKS:
        missing.append(
            f"paired_real_tasks:{len(paired)}/{PT4_MINIMUM_PAIRED_REAL_TASKS}"
        )
    if baseline_integrity is not True:
        missing.append("real_baseline_integrity:unresolved")
    if not labels_complete:
        missing.append("human_labels:materially_incomplete")
    return {
        "status": (
            "SUFFICIENT_FOR_EVIDENCE_BACKED_HARDENING"
            if not missing
            else "INSUFFICIENT_EVIDENCE"
        ),
        "paired_real_tasks": len(paired),
        "required_paired_real_tasks": PT4_MINIMUM_PAIRED_REAL_TASKS,
        "paired_real_trials": len(paired_trials),
        "baseline_integrity": baseline_integrity,
        "human_labels_complete": labels_complete,
        "latest_human_reviews": len(review_map),
        "missing_evidence": missing,
    }


def _derived_verifier_observations(
    suite_kind: str,
    trials: Sequence[EvaluationTrial],
    reviews: Sequence[HumanReview],
) -> list[VerifierEvidenceObservation]:
    review_map = latest_reviews(reviews)
    evidence_kind = (
        "real_founder_work"
        if suite_kind == "real_founder_work"
        else "synthetic_fixture"
    )
    return [
        VerifierEvidenceObservation(
            case_id=f"case_{hashlib.sha256(trial.trial_id.encode()).hexdigest()[:16]}",
            trial_reference=trial.trial_id,
            verifier_proved_acceptable=trial.proved_acceptable,
            human_accepted_as_is=(
                review_map[trial.trial_id].outcome == "accepted_as_is"
                if trial.trial_id in review_map
                else None
            ),
            infrastructure_exclusion=(
                trial.status == "excluded"
                or trial.verification_status == "infrastructure_failure"
            ),
            artifact_references=[
                reference
                for reference in trial.artifact_references
                if not Path(reference).is_absolute()
            ],
            evidence_kind=evidence_kind,
        )
        for trial in trials
    ]


def build_founder_proof_certificate(
    *,
    report: EvaluationReport,
    tasks: Sequence[EvaluationTask],
    trials: Sequence[EvaluationTrial],
) -> dict[str, Any]:
    """Create a redacted content-addressed certificate only for Gate B PASS."""

    if report.founder_gate_status != "PASS":
        raise ValueError("founder-proof certificate requires Gate B PASS")
    if int(report.confusion_matrix.get("false_positive") or 0) != 0:
        raise ValueError("founder-proof certificate forbids known false acceptance")
    checks = {item.check_id: item for item in report.founder_gate_checks}
    repository_identities = sorted(
        {
            task.source_snapshot.repository_identity
            for task in tasks
            if task.evidence_kind == "real_founder_work" and task.evidence_eligible
        }
    )
    agent_identities = sorted(
        {
            json.dumps(
                trial.agent_system.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            for trial in trials
            if trial.evidence_eligible and trial.status == "completed"
        }
    )
    improvement = checks["review_or_cost_improvement"].actual
    automatic = checks["automatic_configuration"].actual
    certificate: dict[str, Any] = {
        "document_kind": "villani_founder_proof_certificate",
        "suite_digest": report.suite_digest,
        "report_digest": canonical_digest(report.model_dump(mode="json")),
        "task_count": checks["minimum_paired_tasks"].actual,
        "repositories": [
            _opaque_reference("repository", value) for value in repository_identities
        ],
        "repositories_redacted": True,
        "agent_identities": [json.loads(value) for value in agent_identities],
        "accepted_as_is_rates": {
            "direct": report.reliability["direct.human_accepted_as_is_rate"].value,
            "villani": report.reliability["villani.human_accepted_as_is_rate"].value,
        },
        "false_cases": {
            "false_acceptance": report.confusion_matrix.get("false_positive"),
            "false_rejection": report.confusion_matrix.get("false_negative"),
        },
        "review_time_delta": improvement.get("median_review_time_reduction"),
        "accepted_change_cost_delta": improvement.get(
            "total_cost_per_accepted_change_reduction"
        ),
        "automatic_configuration_rate": automatic,
        "exclusions": {
            "count": len(report.exclusions),
            "reason_digests": sorted(
                {
                    hashlib.sha256(
                        str(item.get("reason", "")).encode("utf-8")
                    ).hexdigest()
                    for item in report.exclusions
                }
            ),
        },
    }
    certificate["certificate_digest"] = canonical_digest(certificate)
    return certificate


def build_hardening_analysis(
    suite_directory: str | Path,
    *,
    failure_observations: Sequence[FailureObservation] = (),
    verifier_observations: Sequence[VerifierEvidenceObservation] = (),
    before_outcomes: Sequence[FrozenTaskOutcome] = (),
    after_outcomes: Sequence[FrozenTaskOutcome] = (),
    burden_basis: str = "review_minutes",
    correctness_regressions: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build the fail-closed PT4 analysis without mutating product behavior."""

    root = Path(suite_directory).expanduser().resolve()
    suite = load_suite(root)
    tasks = [load_task(root, item.task_id) for item in suite.task_versions]
    trials = list(load_trials(root))
    reviews = list(load_reviews(root))
    evaluation_report = build_report(root)
    sufficiency = assess_hardening_sufficiency(
        suite_directory=root,
        tasks=tasks,
        trials=trials,
        reviews=reviews,
    )
    clusters = cluster_failures(failure_observations)
    prioritization = prioritize_failure_clusters(clusters, burden_basis=burden_basis)
    if sufficiency["status"] == "INSUFFICIENT_EVIDENCE":
        prioritization = {
            **prioritization,
            "status": "insufficient_evidence",
            "ranked": [],
            "reason": "PT4 requires at least 20 paired real tasks, valid baselines, and materially complete labels.",
        }
    verifier_rows = list(verifier_observations) or _derived_verifier_observations(
        suite.evidence_kind, trials, reviews
    )
    verifier = build_verifier_diagnostics(verifier_rows)
    if before_outcomes or after_outcomes:
        before_after = {
            "status": "available",
            "comparisons": compare_frozen_outcomes(before_outcomes, after_outcomes),
        }
    else:
        before_after = {
            "status": "insufficient_evidence",
            "comparisons": [],
            "reason": "No exact frozen real-task before/after population exists.",
        }
    certificate = (
        build_founder_proof_certificate(
            report=evaluation_report,
            tasks=tasks,
            trials=trials,
        )
        if evaluation_report.founder_gate_status == "PASS"
        else None
    )
    taxonomy_counts: Counter[str] = Counter(
        observation.taxonomy
        for observation in failure_observations
        if observation.evidence_kind == "real_founder_work"
    )
    generated_at = utc_now()
    analysis: dict[str, Any] = {
        "document_kind": "pt4_founder_hardening_analysis",
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "suite_digest": evaluation_report.suite_digest,
        "status": evaluation_report.founder_gate_status,
        "gate_b": {
            "status": evaluation_report.founder_gate_status,
            "checks": [
                item.model_dump(mode="json")
                for item in evaluation_report.founder_gate_checks
            ],
        },
        "sufficiency": sufficiency,
        "failure_taxonomy": [
            {"taxonomy": name, "count": taxonomy_counts[name]}
            for name in FAILURE_TAXONOMY
        ],
        "failure_clusters": clusters,
        "prioritization": prioritization,
        "verifier_diagnostics": verifier,
        "before_after": before_after,
        "correctness_regressions": list(correctness_regressions),
        "production_changes_authorized": (
            sufficiency["status"] == "SUFFICIENT_FOR_EVIDENCE_BACKED_HARDENING"
            and prioritization["status"] == "available"
        ),
        "speculative_performance_changes": [],
        "verifier_behavior_changed": False,
        "no_false_acceptance_introduced": "not_applicable_no_verifier_change",
        "founder_proof_certificate": certificate,
        "founder_proof_certificate_issued": certificate is not None,
        "pt5_authorized": evaluation_report.founder_gate_status == "PASS",
        "pt5_started": False,
    }
    analysis["analysis_digest"] = canonical_digest(analysis)
    return analysis


def _hardening_markdown(analysis: dict[str, Any]) -> str:
    sufficiency = analysis["sufficiency"]
    lines = [
        "# PT4 founder hardening analysis",
        "",
        f"Status: **{analysis['status']}**",
        "",
        f"Gate B: **{analysis['gate_b']['status']}**",
        "",
        "## Evidence boundary",
        "",
        f"- paired real tasks: {sufficiency['paired_real_tasks']} / {sufficiency['required_paired_real_tasks']}",
        f"- baseline integrity: {sufficiency['baseline_integrity']}",
        f"- human labels complete: {sufficiency['human_labels_complete']}",
        f"- production changes authorized: {analysis['production_changes_authorized']}",
        "",
        "## Failure taxonomy",
        "",
        "| Taxonomy | Count |",
        "|---|---:|",
    ]
    lines.extend(
        f"| {item['taxonomy']} | {item['count']} |"
        for item in analysis["failure_taxonomy"]
    )
    lines.extend(
        [
            "",
            "## Prioritization",
            "",
            f"Formula: `{analysis['prioritization']['formula']}`",
            "",
            f"Status: **{analysis['prioritization']['status']}**",
            "",
            "```json",
            json.dumps(analysis["prioritization"]["ranked"], indent=2, sort_keys=True),
            "```",
            "",
            "## Verifier diagnostics",
            "",
            "```json",
            json.dumps(analysis["verifier_diagnostics"], indent=2, sort_keys=True),
            "```",
            "",
            "## Before and after",
            "",
            f"Status: **{analysis['before_after']['status']}**",
            "",
            "## Certificate",
            "",
            (
                f"Issued: `{analysis['founder_proof_certificate']['certificate_digest']}`"
                if analysis["founder_proof_certificate_issued"]
                else "Not issued. Gate B did not pass."
            ),
            "",
            "PT5 was not started.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_hardening_analysis(
    suite_directory: str | Path,
    *,
    json_output: str | Path,
    markdown_output: str | Path,
    **kwargs: Any,
) -> tuple[dict[str, Any], Path, Path]:
    analysis = build_hardening_analysis(suite_directory, **kwargs)
    json_path = Path(json_output).expanduser().resolve()
    markdown_path = Path(markdown_output).expanduser().resolve()
    write_json_atomic(json_path, analysis)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_hardening_markdown(analysis), encoding="utf-8")
    return analysis, json_path, markdown_path


def scan_production_for_evidence_identifiers(
    production_roots: Iterable[str | Path],
    identifiers: Iterable[str],
) -> list[str]:
    """Return exact identifier leaks in production rules; never fuzzy-match."""

    needles = sorted({value for value in identifiers if len(value) >= 8})
    violations: list[str] = []
    for root_value in production_roots:
        root = Path(root_value).expanduser().resolve()
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            if not path.is_file() or any(
                part in {"tests", "test", "fixtures", "__pycache__"}
                for part in path.parts
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for identifier in needles:
                if identifier in text:
                    violations.append(
                        f"{path.as_posix()}:{_opaque_reference('identifier', identifier)}"
                    )
    return sorted(violations)
