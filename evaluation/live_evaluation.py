#!/usr/bin/env python3
"""Operator-invoked evaluation that executes the real public Villani command."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


POLICIES = (
    "strong-only",
    "cheap-only",
    "cheap-first-escalation",
    "strong-first",
    "adaptive",
)


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


def _wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if total == 0:
        return None
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((rate * (1 - rate) + z * z / (4 * total)) / total)
    return [max(0.0, center - margin), min(1.0, center + margin)]


def validate_manifest(document: dict[str, Any]) -> None:
    if document.get("schema_version") != "villani.live_evaluation_manifest.v1":
        raise ValueError("unsupported live evaluation manifest version")
    policies = document.get("policies")
    if not isinstance(policies, dict) or set(policies) != set(POLICIES):
        raise ValueError("manifest must configure exactly the five supported policies")
    for name, value in policies.items():
        if not isinstance(value, dict) or not isinstance(value.get("villani_home"), str):
            raise ValueError(f"policy {name} requires villani_home")
    tasks = document.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("manifest requires at least one task")
    for task in tasks:
        required = {"task_id", "repository", "instruction", "success_criteria", "expected_success"}
        if not isinstance(task, dict) or not required <= task.keys():
            raise ValueError("every live task requires identity, repository, task, criteria, and truth")


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
    for policy in POLICIES:
        home = Path(document["policies"][policy]["villani_home"]).expanduser().resolve()
        if not (home / "config.yaml").is_file():
            raise ValueError(f"policy home is not preconfigured: {home}")
        for task in document["tasks"]:
            repository = Path(task["repository"]).expanduser().resolve()
            before = {path.name for path in (home / "runs").glob("*")} if (home / "runs").is_dir() else set()
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
                verification = _read_object(
                    run_directory / "verification" / f"{selected}.json"
                )
            materialization = (
                _read_object(run_directory / "materialization.json")
                if (run_directory / "materialization.json").is_file()
                else {}
            )
            accepted = bool(verification.get("acceptance_eligible"))
            materialized = materialization.get("status") == "succeeded"
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
                    verified_success=accepted and materialized and manifest.get("final_state") == "COMPLETED",
                    false_acceptance=accepted and not expected,
                    false_rejection=(not accepted) and expected,
                    total_model_cost=(
                        float(manifest["total_cost_usd"])
                        if manifest.get("total_cost_usd") is not None
                        else None
                    ),
                    verifier_cost=(
                        float(verifier["cost"])
                        if verifier.get("cost") is not None
                        else None
                    ),
                    wall_time_ms=wall_time_ms,
                    attempts=len(manifest.get("attempt_ids") or []),
                    escalations=sum(event.get("event_type") == "escalation_selected" for event in events),
                    revisions={
                        "manifest_version": document.get("manifest_version"),
                        "repository_revision": task.get("repository_revision"),
                        "environment_revision": document.get("environment_revision"),
                        "model_provider_prompt_verifier": manifest.get("metadata", {}),
                    },
                )
            )
    return rows


def aggregate(rows: list[LiveObservation], minimum_sample_size: int) -> dict[str, Any]:
    strategies: dict[str, Any] = {}
    for policy in POLICIES:
        group = [row for row in rows if row.policy == policy]
        successes = sum(row.verified_success for row in group)
        costs = [row.total_model_cost for row in group]
        verifier_costs = [row.verifier_cost for row in group]
        known_cost = all(value is not None for value in costs)
        strategies[policy] = {
            "raw_run_ids": [row.run_id for row in group],
            "verified_success": successes,
            "verified_success_rate": successes / len(group) if group else None,
            "verified_success_95pct_wilson": _wilson(successes, len(group)),
            "false_acceptance": sum(row.false_acceptance for row in group),
            "false_rejection": sum(row.false_rejection for row in group),
            "total_model_cost": sum(value for value in costs if value is not None) if known_cost else None,
            "model_cost_accounting_status": "complete" if known_cost else "unknown",
            "verifier_cost": (
                sum(value for value in verifier_costs if value is not None)
                if all(value is not None for value in verifier_costs)
                else None
            ),
            "cost_per_verified_accepted_change": (
                sum(value for value in costs if value is not None) / successes
                if known_cost and successes
                else None
            ),
            "wall_time_ms": sum(row.wall_time_ms for row in group),
            "attempts": sum(row.attempts for row in group),
            "escalations": sum(row.escalations for row in group),
            "escalation_value": successes / max(1, sum(row.escalations for row in group)),
            "revisions": [row.revisions for row in group],
        }
    sufficient = all(
        len([row for row in rows if row.policy == policy]) >= minimum_sample_size
        for policy in POLICIES
    )
    return {
        "schema_version": "villani.live_evaluation.v1",
        "strategies": strategies,
        "minimum_sample_size": minimum_sample_size,
        "savings_claim_supported": sufficient,
        "savings_claim_refusal_reason": None if sufficient else "minimum sample size not met",
        "production_routing_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--villani-command", default="villani")
    parser.add_argument("--minimum-sample-size", type=int, default=30)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("live evaluation requires explicit --execute")
    document = _read_object(args.manifest)
    result = aggregate(
        execute_manifest(document, villani_command=args.villani_command),
        args.minimum_sample_size,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
