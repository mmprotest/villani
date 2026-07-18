from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path


def test_recorded_guided_setup_reaches_completed_sample_and_stops_service(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    artifacts = tmp_path / "recorded-onboarding"
    env = dict(os.environ)
    env["VILLANI_ONBOARDING_ALLOW_EXTERNAL_ARTIFACTS"] = "1"
    scripts = Path(os.path.abspath(sysconfig.get_path("scripts")))
    scripts_key = os.path.normcase(str(scripts))
    env["PATH"] = os.pathsep.join(
        item
        for item in env.get("PATH", "").split(os.pathsep)
        if item and os.path.normcase(os.path.abspath(item)) != scripts_key
    )
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
    assert report["selected_interpreter"] == str(Path(sys.executable).absolute())
    assert report["scripts_directory"] == str(scripts)
    assert report["caller_path_contained_scripts_directory"] is False
    assert set(report["entry_points"]) == {
        "villani",
        "villani-code",
        "villani-agentd",
        "vfr",
    }
    assert report["configured_model"] == "fixture-onboarding"
    assert report["capability_status"] == "unrated"
    assert report["doctor"]["healthy"] is True
    assert report["sample_final_state"] == "COMPLETED"
    assert report["sample_selected_attempt"] == "attempt_001"
    assert report["sample_validation_exit_code"] == 0
    assert report["sample_evidence"]["patch_bytes"] > 0
    assert any(
        "test" in Path(path).name.casefold()
        for path in report["sample_evidence"]["changed_files"]
    )
    assert any(
        "test" not in Path(path).name.casefold()
        for path in report["sample_evidence"]["changed_files"]
    )
    assert report["sample_evidence"]["repository_checks"] == {
        "passed": 1,
        "failed": 0,
        "not_run": 0,
        "unavailable": 0,
        "accounting_status": "complete",
    }
    assert report["sample_evidence"]["focused_probes"]["passed"] == 0
    assert report["sample_evidence"]["requirements"]["not_proved"] == 0
    assert report["sample_evidence"]["acceptance"]["decision"] is True
    assert report["sample_evidence"]["classification"]["raw"]["difficulty"] == "easy"
    assert (
        report["sample_evidence"]["classification"]["effective"]["difficulty"]
        == "easy"
    )
    assert report["sample_evidence"]["classification"]["signals"]["behavior_count"] == 1
    assert report["sample_evidence"]["coverage_schema_version"] == (
        "villani.validation_coverage.v1"
    )
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
    assert report["dead_letters"] == 0
    assert report["secret_scan"]["status"] == "passed"
    assert report["secret_scan"]["matches"] == []
    assert report["screenshots"] == []
    assert sorted(command["exit_code"] for command in report["commands"]).count(4) == 1
    assert all(command["exit_code"] in {0, 4} for command in report["commands"])
