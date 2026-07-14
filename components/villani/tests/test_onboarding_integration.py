from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_recorded_guided_setup_reaches_completed_sample_and_stops_service(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    artifacts = tmp_path / "recorded-onboarding"
    env = dict(os.environ)
    env["VILLANI_ONBOARDING_ALLOW_EXTERNAL_ARTIFACTS"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "onboarding-verification" / "run_onboarding_gate.py"),
            "--artifacts",
            str(artifacts),
            "--python",
            sys.executable,
            "--skip-screenshots",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    report = json.loads((artifacts / "onboarding-report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "ONBOARDING GATE PASSED"
    assert report["configured_model"] == "fixture-onboarding"
    assert report["capability_status"] == "unrated"
    assert report["doctor"]["healthy"] is True
    assert report["sample_final_state"] == "COMPLETED"
    assert report["sample_selected_attempt"] == "attempt_001"
    assert report["sample_validation_exit_code"] == 0
    assert set(report["delivery_modes"]) == {
        "suggest",
        "approve",
        "reject",
        "apply",
        "branch",
        "pull_request",
    }
    assert all(result["status"] == "passed" for result in report["delivery_modes"].values())
    assert report["doctor"]["ok"] is True
    assert report["doctor"]["inferred_commands_executed"] is False
    assert all(item["model_tokens_spent"] == 0 for item in report["doctor"]["backend_connectivity"])
    assert report["service_stopped"] is True
    assert report["screenshots"] == []
    assert sorted(command["exit_code"] for command in report["commands"]).count(4) == 1
    assert all(command["exit_code"] in {0, 4} for command in report["commands"])
