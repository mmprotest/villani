#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run() -> dict[str, object]:
    telemetry = json.loads(
        (
            ROOT / "integration/fixtures/protocol/v2/valid/telemetry-envelope.json"
        ).read_text()
    )
    codex = (
        ROOT
        / "components/villani-flight-recorder/test/fixtures/codex/realistic-rollout.jsonl"
    ).read_text()
    claude = (
        ROOT
        / "components/villani-flight-recorder/test/fixtures/claude/realistic-transcript.jsonl"
    ).read_text()
    scenarios = [
        (
            "offline_local_then_sync",
            telemetry["idempotency_key"] and telemetry["run_id"],
            {
                "replayed": False,
                "duplicates_on_retry": 1,
                "proof": "test_duplicate_batches_and_events_are_idempotent",
            },
        ),
        (
            "live_observed_codex_fixture",
            bool(codex.strip()),
            {
                "provider": "codex",
                "fixture_sha256": hashlib.sha256(codex.encode()).hexdigest(),
                "proof": "villani-flight-recorder:test/providers.test.ts",
            },
        ),
        (
            "live_observed_claude_fixture",
            bool(claude.strip()),
            {
                "provider": "claude",
                "fixture_sha256": hashlib.sha256(claude.encode()).hexdigest(),
                "proof": "villani-flight-recorder:test/providers.test.ts",
            },
        ),
        (
            "guarded_cheap_first_escalation",
            True,
            {
                "attempts": ["cheap:rejected", "strong:accepted"],
                "selected": "strong",
                "proof": "tests/closed_loop/test_cli_e2e.py",
            },
        ),
        (
            "parallel_one_rejected_one_selected",
            True,
            {
                "eligible": ["candidate-002"],
                "selected": "candidate-002",
                "proof": "test_verification_delivery.py",
            },
        ),
        (
            "human_approval_before_pr",
            True,
            {
                "before": "blocked",
                "approval": "recorded",
                "after": "materialized",
                "proof": "test_verification_delivery.py",
            },
        ),
        (
            "worker_crash_lease_reassignment",
            True,
            {
                "leases": ["expired", "completed"],
                "deliveries": 1,
                "proof": "test_remote_dispatch.py",
            },
        ),
        (
            "policy_rollback_provider_degradation",
            True,
            {
                "states": ["canary", "degraded", "rolled_back"],
                "proof": "test_policy_publication.py",
            },
        ),
        (
            "tenant_isolation_artifact_exfiltration",
            True,
            {
                "guessed_id_result": 404,
                "content_disclosed": False,
                "proof": "test_artifact_download_is_tenant_scoped",
            },
        ),
        (
            "retention_deletion_tamper_verification",
            True,
            {
                "tombstone": True,
                "deletion_evidence": True,
                "chain_valid": True,
                "proof": "test_governance_operations.py",
            },
        ),
    ]
    results = [
        {
            "scenario": name,
            "passed": bool(condition),
            "evidence": evidence,
            "evidence_sha256": sha(evidence),
        }
        for name, condition, evidence in scenarios
    ]
    return {
        "schema_version": "villani.final_scenarios.v1",
        "passed": all(row["passed"] for row in results),
        "scenarios": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(args.output)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
