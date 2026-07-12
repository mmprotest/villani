#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = (
    "strong-only",
    "cheap-only",
    "cheap-first-escalation",
    "strong-first",
    "adaptive",
)
LOCK = {
    "schema_version": "villani.evaluation_lock.v1",
    "agent": "deterministic-fixture-agent@1",
    "cheap_model": "fixture-cheap@1",
    "strong_model": "fixture-strong@1",
    "prompt": "closed-loop-system@v1",
    "verifier": "verification-graph@v1",
    "environment": "python-3.11-protocol-v2-fixture",
}


@dataclass(frozen=True)
class Observation:
    strategy: str
    index: int
    accepted: bool
    false_acceptance: bool
    false_rejection: bool
    cost: float
    wall_ms: int
    attempts: int
    escalated: bool
    verifier_cost: float

    @property
    def run_reference(self) -> str:
        return f"fixture://final-evaluation/{self.strategy}/run-{self.index:03d}"


def observations() -> list[Observation]:
    specifications = {
        "strong-only": (16, 0, 1, 2.0, 900, 1, 0, 0.20),
        "cheap-only": (10, 1, 3, 0.4, 500, 1, 0, 0.12),
        "cheap-first-escalation": (16, 0, 1, 1.25, 1100, 1.6, 12, 0.25),
        "strong-first": (16, 0, 1, 2.0, 920, 1, 0, 0.20),
        "adaptive": (15, 0, 2, 1.1, 850, 1.4, 8, 0.22),
    }
    rows: list[Observation] = []
    for strategy, values in specifications.items():
        successes, fa, fr, cost, wall, attempts, escalations, verifier = values
        for index in range(1, 21):
            rows.append(
                Observation(
                    strategy,
                    index,
                    index <= successes,
                    index <= fa,
                    successes < index <= successes + fr,
                    cost,
                    wall,
                    math.ceil(attempts) if index <= escalations else 1,
                    index <= escalations,
                    verifier,
                )
            )
    return rows


def wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if not total:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0, center - margin), 4), round(min(1, center + margin), 4)]


def report() -> dict[str, object]:
    rows = observations()
    strategies: dict[str, object] = {}
    for strategy in STRATEGIES:
        group = [row for row in rows if row.strategy == strategy]
        accepted = [row for row in group if row.accepted]
        cost = sum(row.cost for row in group)
        strategies[strategy] = {
            "runs": len(group),
            "verified_success": len(accepted),
            "verified_success_rate": len(accepted) / len(group),
            "verified_success_95pct_wilson": wilson(len(accepted), len(group)),
            "false_acceptance": sum(row.false_acceptance for row in group),
            "false_rejection": sum(row.false_rejection for row in group),
            "cost_per_accepted_change": round(cost / len(accepted), 4)
            if accepted
            else None,
            "wall_time_ms": sum(row.wall_ms for row in group),
            "attempts": sum(row.attempts for row in group),
            "escalations": sum(row.escalated for row in group),
            "escalation_value": (
                {"additional_acceptances": 6, "incremental_cost": 17.0}
                if strategy == "cheap-first-escalation"
                else None
            ),
            "verifier_cost": round(sum(row.verifier_cost for row in group), 4),
            "raw_run_references": [row.run_reference for row in group],
        }
    return {
        "schema_version": "villani.final_evaluation.v1",
        "measurement": "deterministic_protocol_fixtures",
        "lock": LOCK,
        "strategies": strategies,
        "uncertainty": {
            "method": "95% Wilson interval for verified success",
            "sample_size_per_strategy": 20,
            "savings_claim_supported": False,
            "reason": "fixture sample size is below the locked minimum of 30 per strategy",
        },
        "production_routing_changed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic final Villani evaluation"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--live", action="store_true", help="opt in to configured live providers"
    )
    args = parser.parse_args(argv)
    if args.live:
        raise SystemExit(
            "live evaluation requires separately configured providers and is not an ordinary CI gate"
        )
    result = report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
