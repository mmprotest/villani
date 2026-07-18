"""Run-bundle persistence helpers for additive PT9 artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from ..durable_io import write_json_atomic
from .models import (
    AdaptiveVerificationPlan,
    BinaryVerificationDecision,
    CompactReviewPackage,
    GateDReport,
    SupervisionMetrics,
)


def plan_path(run_directory: str | Path, attempt_id: str) -> Path:
    return Path(run_directory) / "verification" / f"{attempt_id}-plan.json"


def decision_path(run_directory: str | Path, attempt_id: str) -> Path:
    return Path(run_directory) / "verification" / f"{attempt_id}-decision.json"


def review_package_path(run_directory: str | Path, attempt_id: str) -> Path:
    return Path(run_directory) / "verification" / f"{attempt_id}-review-package.json"


def persist_plan(run_directory: str | Path, plan: AdaptiveVerificationPlan) -> Path:
    destination = plan_path(run_directory, plan.attempt_id)
    write_json_atomic(destination, plan)
    return destination


def persist_decision(
    run_directory: str | Path, decision: BinaryVerificationDecision
) -> Path:
    destination = decision_path(run_directory, decision.attempt_id)
    write_json_atomic(destination, decision)
    return destination


def persist_review_package(
    run_directory: str | Path, package: CompactReviewPackage
) -> Path:
    destination = review_package_path(run_directory, package.attempt_id)
    write_json_atomic(destination, package)
    return destination


def load_plan(path: str | Path) -> AdaptiveVerificationPlan:
    return AdaptiveVerificationPlan.model_validate(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def load_decision(path: str | Path) -> BinaryVerificationDecision:
    return BinaryVerificationDecision.model_validate(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def load_review_package(path: str | Path) -> CompactReviewPackage:
    return CompactReviewPackage.model_validate(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def load_supervision_metrics(path: str | Path) -> SupervisionMetrics:
    return SupervisionMetrics.model_validate(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def load_gate_d(path: str | Path) -> GateDReport:
    return GateDReport.model_validate(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )
