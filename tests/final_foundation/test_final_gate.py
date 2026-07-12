from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_all_ten_deterministic_scenarios(tmp_path: Path):
    script = load(ROOT / "scripts/run-final-scenarios.py", "final_scenarios")
    result = script.run()
    assert result["passed"]
    assert len(result["scenarios"]) == 10
    assert all(item["evidence_sha256"] for item in result["scenarios"])


def test_benchmark_has_locked_versions_raw_refs_intervals_and_no_unsupported_savings_claim():
    benchmark = load(ROOT / "evaluation/final_gate.py", "final_evaluation")
    report = benchmark.report()
    assert set(report["strategies"]) == set(benchmark.STRATEGIES)
    assert report["lock"] == benchmark.LOCK
    assert not report["uncertainty"]["savings_claim_supported"]
    for result in report["strategies"].values():
        assert len(result["raw_run_references"]) == 20
        assert len(result["verified_success_95pct_wilson"]) == 2


def test_deployment_assets_migration_restore_and_supply_chain_gate(tmp_path: Path):
    chart = ROOT / "deploy/helm/villani-control-plane"
    assert (chart / "Chart.yaml").is_file()
    deployment = (chart / "templates/deployment.yaml").read_text(encoding="utf-8")
    assert "maxUnavailable: 0" in deployment
    assert "/readiness" in deployment and "/liveness" in deployment
    assert "kind: Job" in (chart / "templates/migration-job.yaml").read_text(
        encoding="utf-8"
    )
    output = tmp_path / "supply"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/supply-chain-gate.py"),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    report = json.loads(
        (output / "supply-chain-report.json").read_text(encoding="utf-8")
    )
    assert report["passed"]
    assert report["artifact_signature"]["test_key_only"]
    assert (output / "sbom.cdx.json").is_file()
