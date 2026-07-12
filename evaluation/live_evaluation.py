#!/usr/bin/env python3
"""Explicit live evaluator with isolated paired tasks and fail-closed claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


POLICIES = (
    "strong-only",
    "cheap-only",
    "cheap-first-escalation",
    "strong-first",
    "adaptive",
)
DEFAULT_BASELINE = "strong-only"
DEFAULT_CANDIDATE = "adaptive"
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 20_260_712


@dataclass(frozen=True)
class LiveObservation:
    policy: str
    task_id: str
    run_id: str
    verified_success: bool
    false_acceptance: bool
    false_rejection: bool
    total_model_cost: float | None
    verifier_cost: float | None
    wall_time_ms: int
    attempts: int
    escalations: int
    revisions: dict[str, Any]
    resolved_repository_revision: str | None = None
    policy_configuration_digest: str | None = None
    locks: dict[str, Any] = field(default_factory=dict)
    exclusion_reason: str | None = None
    repository_contamination_detected: bool = False

    @property
    def combined_cost(self) -> float | None:
        if self.total_model_cost is None or self.verifier_cost is None:
            return None
        return self.total_model_cost + self.verifier_cost


REQUIRED_MANIFEST_FIELDS = {
    "schema_version",
    "evaluation_version",
    "environment_identity",
    "model_provider_identities",
    "verifier_identity",
    "minimum_sample_size",
    "baseline_policy",
    "success_non_inferiority_margin",
    "minimum_required_cost_improvement",
    "maximum_false_acceptance_rate",
    "confidence_level",
    "policies",
    "tasks",
}
REQUIRED_TASK_FIELDS = {
    "task_id",
    "repository_source",
    "exact_revision",
    "instruction",
    "success_criteria",
    "expected_success",
}


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def validate_manifest(document: dict[str, Any]) -> None:
    missing = REQUIRED_MANIFEST_FIELDS - document.keys()
    if missing:
        raise ValueError(f"live evaluation manifest is missing: {', '.join(sorted(missing))}")
    if document.get("schema_version") != "villani.live_evaluation_manifest.v2":
        raise ValueError("unsupported live evaluation manifest version")
    if not isinstance(document.get("evaluation_version"), str):
        raise ValueError("evaluation_version must be a string")
    policies = document.get("policies")
    if not isinstance(policies, dict) or set(policies) != set(POLICIES):
        raise ValueError("manifest must configure exactly the five supported policies")
    for name, value in policies.items():
        if not isinstance(value, dict) or not isinstance(value.get("villani_home"), str):
            raise ValueError(f"policy {name} requires villani_home")
        if not isinstance(value.get("configuration"), dict):
            raise ValueError(f"policy {name} requires an immutable configuration object")
    tasks = document.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("manifest requires at least one task")
    task_ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or not REQUIRED_TASK_FIELDS <= task.keys():
            raise ValueError("every live task requires identity, exact repository lock, criteria, and truth")
        task_id = str(task["task_id"])
        if task_id in task_ids:
            raise ValueError(f"duplicate task_id: {task_id}")
        task_ids.add(task_id)
        revision = str(task["exact_revision"])
        if len(revision) != 40 or any(character not in "0123456789abcdefABCDEF" for character in revision):
            raise ValueError(f"task {task_id} requires a full 40-character Git revision")
    if document["baseline_policy"] not in POLICIES:
        raise ValueError("baseline_policy is not configured")
    for name in (
        "success_non_inferiority_margin",
        "minimum_required_cost_improvement",
        "maximum_false_acceptance_rate",
    ):
        value = document[name]
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise ValueError(f"{name} must be between zero and one")
    confidence = document["confidence_level"]
    if not isinstance(confidence, (int, float)) or not 0.5 < float(confidence) < 1:
        raise ValueError("confidence_level must be between 0.5 and 1")
    if not isinstance(document["minimum_sample_size"], int) or document["minimum_sample_size"] < 1:
        raise ValueError("minimum_sample_size must be positive")


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=check,
    )


@contextmanager
def isolated_checkout(repository_source: str | Path, exact_revision: str) -> Iterator[tuple[Path, str]]:
    source = Path(repository_source).expanduser().resolve()
    resolved = _git(source, "rev-parse", f"{exact_revision}^{{commit}}").stdout.strip()
    if resolved.lower() != exact_revision.lower():
        raise ValueError(f"repository revision mismatch: expected {exact_revision}, resolved {resolved}")
    root = Path(tempfile.mkdtemp(prefix="villani-live-evaluation-"))
    checkout = root / "checkout"
    added = False
    try:
        _git(source, "worktree", "add", "--detach", str(checkout), resolved)
        added = True
        checkout_revision = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        if checkout_revision != resolved:
            raise RuntimeError("isolated checkout resolved to a different revision")
        if _git(checkout, "status", "--porcelain").stdout:
            raise RuntimeError("isolated checkout is dirty before execution")
        yield checkout, resolved
    finally:
        if added:
            _git(source, "worktree", "remove", "--force", str(checkout), check=False)
        shutil.rmtree(root, ignore_errors=True)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def execute_manifest(
    document: dict[str, Any], *, villani_command: str = "villani"
) -> list[LiveObservation]:
    validate_manifest(document)
    rows: list[LiveObservation] = []
    common_locks = {
        "evaluation_version": document["evaluation_version"],
        "environment_identity": document["environment_identity"],
        "model_provider_identities": document["model_provider_identities"],
        "verifier_identity": document["verifier_identity"],
    }
    for task in document["tasks"]:
        for policy in POLICIES:
            policy_document = document["policies"][policy]
            home = Path(policy_document["villani_home"]).expanduser().resolve()
            if not (home / "config.yaml").is_file():
                raise ValueError(f"policy home is not preconfigured: {home}")
            before = {path.name for path in (home / "runs").glob("*")} if (home / "runs").is_dir() else set()
            with isolated_checkout(task["repository_source"], task["exact_revision"]) as (
                repository,
                resolved_revision,
            ):
                environment = os.environ.copy()
                environment["VILLANI_HOME"] = str(home)
                started = time.perf_counter()
                completed = subprocess.run(
                    [
                        villani_command,
                        "run",
                        str(task["instruction"]),
                        "--repo",
                        str(repository),
                        "--success-criteria",
                        str(task["success_criteria"]),
                    ],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                wall_time_ms = int((time.perf_counter() - started) * 1000)
                run_id = next(
                    (
                        line.split(":", 1)[1].strip()
                        for line in completed.stdout.splitlines()
                        if line.startswith("Run ID:")
                    ),
                    None,
                )
                if run_id is None or run_id in before:
                    raise RuntimeError(
                        f"public Villani execution did not create a new run for {policy}/{task['task_id']}"
                    )
                run_directory = home / "runs" / run_id
                manifest = _read_object(run_directory / "manifest.json")
                selected = manifest.get("selected_attempt_id")
                verification: dict[str, Any] = {}
                if isinstance(selected, str):
                    verification = _read_object(run_directory / "verification" / f"{selected}.json")
                materialization = (
                    _read_object(run_directory / "materialization.json")
                    if (run_directory / "materialization.json").is_file()
                    else {}
                )
                accepted = bool(verification.get("acceptance_eligible"))
                expected = bool(task["expected_success"])
                stage_metrics = manifest.get("stage_metrics") or {}
                verifier = stage_metrics.get("verification") or {}
                events = [
                    json.loads(line)
                    for line in (run_directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                rows.append(
                    LiveObservation(
                        policy=policy,
                        task_id=str(task["task_id"]),
                        run_id=run_id,
                        verified_success=(
                            accepted
                            and materialization.get("status") == "succeeded"
                            and manifest.get("final_state") == "COMPLETED"
                        ),
                        false_acceptance=accepted and not expected,
                        false_rejection=(not accepted) and expected,
                        total_model_cost=(
                            float(manifest["total_cost_usd"])
                            if manifest.get("total_cost_usd") is not None
                            else None
                        ),
                        verifier_cost=(
                            float(verifier["cost"]) if verifier.get("cost") is not None else None
                        ),
                        wall_time_ms=wall_time_ms,
                        attempts=len(manifest.get("attempt_ids") or []),
                        escalations=sum(
                            event.get("event_type") == "escalation_selected" for event in events
                        ),
                        revisions={"manifest": manifest.get("schema_version")},
                        resolved_repository_revision=resolved_revision,
                        policy_configuration_digest=_canonical_digest(policy_document["configuration"]),
                        locks=common_locks,
                    )
                )
    return rows


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _paired_intervals(
    candidate: list[LiveObservation],
    baseline: list[LiveObservation],
    confidence: float,
) -> dict[str, list[float] | None]:
    candidate_by_task = {row.task_id: row for row in candidate}
    baseline_by_task = {row.task_id: row for row in baseline}
    task_ids = sorted(candidate_by_task)
    if not task_ids or set(task_ids) != set(baseline_by_task):
        return {"success_rate_difference": None, "cost_improvement_fraction": None}
    random_source = random.Random(BOOTSTRAP_SEED)
    success_differences: list[float] = []
    cost_improvements: list[float] = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sampled = [random_source.choice(task_ids) for _ in task_ids]
        candidate_rows = [candidate_by_task[task_id] for task_id in sampled]
        baseline_rows = [baseline_by_task[task_id] for task_id in sampled]
        success_differences.append(
            sum(row.verified_success for row in candidate_rows) / len(sampled)
            - sum(row.verified_success for row in baseline_rows) / len(sampled)
        )
        candidate_success = sum(row.verified_success for row in candidate_rows)
        baseline_success = sum(row.verified_success for row in baseline_rows)
        candidate_costs = [row.combined_cost for row in candidate_rows]
        baseline_costs = [row.combined_cost for row in baseline_rows]
        if (
            candidate_success
            and baseline_success
            and all(value is not None for value in candidate_costs + baseline_costs)
        ):
            candidate_cps = sum(float(value) for value in candidate_costs) / candidate_success
            baseline_cps = sum(float(value) for value in baseline_costs) / baseline_success
            if baseline_cps > 0:
                cost_improvements.append((baseline_cps - candidate_cps) / baseline_cps)
    alpha = 1 - confidence
    interval = lambda values: (
        [_percentile(values, alpha / 2), _percentile(values, 1 - alpha / 2)]
        if values
        else None
    )
    return {
        "success_rate_difference": interval(success_differences),
        "cost_improvement_fraction": interval(cost_improvements),
    }


def aggregate(
    rows: list[LiveObservation],
    minimum_sample_size: int,
    *,
    baseline_policy: str = DEFAULT_BASELINE,
    candidate_policy: str = DEFAULT_CANDIDATE,
    success_non_inferiority_margin: float = 0.05,
    minimum_required_cost_improvement: float = 0.10,
    maximum_false_acceptance_rate: float = 0.01,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    strategies: dict[str, Any] = {}
    exclusions = [row for row in rows if row.exclusion_reason]
    usable = [row for row in rows if not row.exclusion_reason]
    for policy in POLICIES:
        group = [row for row in usable if row.policy == policy]
        successes = sum(row.verified_success for row in group)
        model_costs = [row.total_model_cost for row in group]
        verifier_costs = [row.verifier_cost for row in group]
        combined_costs = [row.combined_cost for row in group]
        known = all(value is not None for value in combined_costs)
        total_combined = sum(float(value) for value in combined_costs) if known else None
        strategies[policy] = {
            "raw_run_ids": [row.run_id for row in group],
            "paired_task_ids": [row.task_id for row in group],
            "resolved_repository_revisions": [row.resolved_repository_revision for row in group],
            "verified_successes": successes,
            "verified_success_rate": successes / len(group) if group else None,
            "false_acceptances": sum(row.false_acceptance for row in group),
            "false_rejections": sum(row.false_rejection for row in group),
            "attempts": sum(row.attempts for row in group),
            "escalations": sum(row.escalations for row in group),
            "total_model_cost": (
                sum(float(value) for value in model_costs) if all(value is not None for value in model_costs) else None
            ),
            "total_verifier_cost": (
                sum(float(value) for value in verifier_costs)
                if all(value is not None for value in verifier_costs)
                else None
            ),
            "total_combined_cost": total_combined,
            "cost_per_verified_accepted_change": (
                total_combined / successes if total_combined is not None and successes else None
            ),
            "wall_time_ms": sum(row.wall_time_ms for row in group),
            "missing_accounting_count": sum(row.combined_cost is None for row in group),
            "policy_configuration_digests": sorted(
                {row.policy_configuration_digest for row in group if row.policy_configuration_digest}
            ),
            "locks": [row.locks for row in group],
            "escalation_value": None,
        }

    candidate = [row for row in usable if row.policy == candidate_policy]
    baseline = [row for row in usable if row.policy == baseline_policy]
    intervals = _paired_intervals(candidate, baseline, confidence_level)
    candidate_strategy = strategies[candidate_policy]
    baseline_strategy = strategies[baseline_policy]
    success_difference = (
        candidate_strategy["verified_success_rate"] - baseline_strategy["verified_success_rate"]
        if candidate_strategy["verified_success_rate"] is not None
        and baseline_strategy["verified_success_rate"] is not None
        else None
    )
    candidate_cps = candidate_strategy["cost_per_verified_accepted_change"]
    baseline_cps = baseline_strategy["cost_per_verified_accepted_change"]
    cost_difference = (
        candidate_strategy["total_combined_cost"] - baseline_strategy["total_combined_cost"]
        if candidate_strategy["total_combined_cost"] is not None
        and baseline_strategy["total_combined_cost"] is not None
        else None
    )
    cps_difference = (
        candidate_cps - baseline_cps if candidate_cps is not None and baseline_cps is not None else None
    )
    improvement = (
        (baseline_cps - candidate_cps) / baseline_cps
        if candidate_cps is not None and baseline_cps not in (None, 0)
        else None
    )
    false_acceptance_rate = (
        candidate_strategy["false_acceptances"] / len(candidate) if candidate else None
    )

    refusal_reasons: list[dict[str, Any]] = []
    for policy in POLICIES:
        count = len([row for row in usable if row.policy == policy])
        if count < minimum_sample_size:
            refusal_reasons.append({"code": "minimum_sample_size", "policy": policy, "actual": count})
    if {row.task_id for row in candidate} != {row.task_id for row in baseline}:
        refusal_reasons.append({"code": "different_paired_task_sets"})
    if any(row.combined_cost is None for row in candidate + baseline):
        refusal_reasons.append({"code": "missing_accounting"})
    if exclusions:
        refusal_reasons.append({"code": "missing_or_corrupt_runs", "count": len(exclusions)})
    if any(row.repository_contamination_detected for row in rows):
        refusal_reasons.append({"code": "repository_contamination"})
    expected_revision_by_task: dict[str, str] = {}
    revision_consistent = True
    for row in usable:
        revision = row.resolved_repository_revision or str(row.revisions.get("repository_revision") or "")
        previous = expected_revision_by_task.setdefault(row.task_id, revision)
        revision_consistent = revision_consistent and bool(revision) and revision == previous
    if not revision_consistent:
        refusal_reasons.append({"code": "repository_revision_mismatch"})
    lock_digests = {_canonical_digest(row.locks) for row in candidate + baseline}
    if len(lock_digests) != 1 or not lock_digests or lock_digests == {_canonical_digest({})}:
        refusal_reasons.append({"code": "inconsistent_evaluation_locks"})
    if success_difference is None or success_difference < -success_non_inferiority_margin:
        refusal_reasons.append({"code": "success_non_inferiority_point_estimate"})
    success_interval = intervals["success_rate_difference"]
    if success_interval is None or success_interval[0] < -success_non_inferiority_margin:
        refusal_reasons.append({"code": "success_non_inferiority_confidence_interval"})
    if improvement is None or improvement < minimum_required_cost_improvement:
        refusal_reasons.append({"code": "cost_improvement_point_estimate"})
    cost_interval = intervals["cost_improvement_fraction"]
    if cost_interval is None or cost_interval[0] < minimum_required_cost_improvement:
        refusal_reasons.append({"code": "cost_improvement_confidence_interval"})
    if false_acceptance_rate is None or false_acceptance_rate > maximum_false_acceptance_rate:
        refusal_reasons.append({"code": "false_acceptance_guardrail"})

    cheap = {row.task_id: row for row in usable if row.policy == "cheap-only"}
    for policy in ("cheap-first-escalation", "adaptive"):
        group = {row.task_id: row for row in usable if row.policy == policy}
        if set(group) == set(cheap) and all(
            group[task].combined_cost is not None and cheap[task].combined_cost is not None
            for task in group
        ):
            numerator = sum(
                int(group[task].verified_success) - int(cheap[task].verified_success)
                for task in group
            )
            denominator = sum(
                float(group[task].combined_cost) - float(cheap[task].combined_cost)
                for task in group
            )
            strategies[policy]["escalation_value"] = {
                "incremental_verified_acceptances": numerator,
                "incremental_combined_cost": denominator,
                "verified_acceptances_per_incremental_cost": (
                    numerator / denominator if denominator > 0 else None
                ),
            }

    return {
        "schema_version": "villani.live_evaluation.v2",
        "strategies": strategies,
        "comparison": {
            "candidate_policy": candidate_policy,
            "baseline_policy": baseline_policy,
            "success_rate_difference": success_difference,
            "cost_difference": cost_difference,
            "cost_per_success_difference": cps_difference,
            "cost_improvement_fraction": improvement,
            "confidence_intervals": intervals,
        },
        "decision_rule": {
            "minimum_sample_size": minimum_sample_size,
            "success_non_inferiority_margin": success_non_inferiority_margin,
            "minimum_required_cost_improvement": minimum_required_cost_improvement,
            "maximum_false_acceptance_rate": maximum_false_acceptance_rate,
            "confidence_level": confidence_level,
            "method": "paired_task_nonparametric_bootstrap_percentile",
            "bootstrap_samples": BOOTSTRAP_SAMPLES,
            "bootstrap_seed": BOOTSTRAP_SEED,
        },
        "exclusion_count": len(exclusions),
        "exclusion_reasons": sorted({row.exclusion_reason for row in exclusions}),
        "missing_accounting_count": sum(row.combined_cost is None for row in usable),
        "savings_claim_supported": not refusal_reasons,
        "savings_claim_refusal_reasons": refusal_reasons,
        "production_routing_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--villani-command", default="villani")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("live evaluation requires explicit --execute")
    document = _read_object(args.manifest)
    validate_manifest(document)
    result = aggregate(
        execute_manifest(document, villani_command=args.villani_command),
        int(document["minimum_sample_size"]),
        baseline_policy=str(document["baseline_policy"]),
        success_non_inferiority_margin=float(document["success_non_inferiority_margin"]),
        minimum_required_cost_improvement=float(document["minimum_required_cost_improvement"]),
        maximum_false_acceptance_rate=float(document["maximum_false_acceptance_rate"]),
        confidence_level=float(document["confidence_level"]),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
