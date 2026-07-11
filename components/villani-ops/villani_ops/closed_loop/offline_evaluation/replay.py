from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .drift import monitor_drift
from .evaluation import evaluate_policy, validate_assignment_provenance
from .models import EvaluationObservation
from .optimizer import SegmentedPolicyOptimizer


def _markdown(report: dict[str, Any]) -> str:
    evaluation = report["evaluation"]
    lines = [
        "# Villani Offline Policy Evaluation",
        "",
        f"Raw observations: {evaluation['raw_count']}",
        f"Observed outcomes: {evaluation['observed_count']}",
        f"Censored outcomes: {evaluation['censored_count']}",
        "",
        "## Estimates",
        "",
        "| Metric | Estimate | 95% CI | N | Status |",
        "|---|---:|---:|---:|---|",
    ]
    for key in (
        "direct_success",
        "direct_cost",
        "direct_latency",
        "inverse_propensity_success",
        "doubly_robust_success",
    ):
        value = evaluation[key]
        interval = (
            f"[{value['lower']}, {value['upper']}]"
            if value["lower"] is not None
            else "unavailable"
        )
        lines.append(
            f"| {key} | {value['estimate']} | {interval} | {value['sample_count']} | {value['status']} |"
        )
    lines.extend(["", "## Refusals", ""])
    lines.extend(f"- {reason}" for reason in evaluation["refusal_reasons"] or ["None"])
    return "\n".join(lines) + "\n"


def replay_file(
    input_path: str | Path,
    *,
    json_output: str | Path,
    markdown_output: str | Path,
    minimum_sample_size: int = 5,
) -> dict[str, Any]:
    document = json.loads(Path(input_path).read_text(encoding="utf-8"))
    records = tuple(
        EvaluationObservation.model_validate(item) for item in document["observations"]
    )
    validate_assignment_provenance(records)
    evaluation = evaluate_policy(records, minimum_sample_size=minimum_sample_size)
    split = max(1, len(records) // 2)
    drift = (
        monitor_drift(records[:split], records[split:]) if len(records) > 1 else None
    )
    optimized = SegmentedPolicyOptimizer(minimum_samples=minimum_sample_size).optimize(
        records
    )
    report = {
        "schema_version": "villani.offline_replay_report.v1",
        "source_fixture": str(input_path),
        "evaluation": evaluation.model_dump(mode="json"),
        "optimized_policy": optimized.model_dump(mode="json"),
        "drift": drift.model_dump(mode="json") if drift else None,
        "controls_live_execution": False,
    }
    Path(json_output).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    Path(markdown_output).write_text(_markdown(report), encoding="utf-8")
    return report
