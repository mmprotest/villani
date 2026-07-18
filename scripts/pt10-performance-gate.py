#!/usr/bin/env python3
"""Measure cold installed-shell and doctor startup against the PT10 contract."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "release" / "performance-targets.json"


def _measure(
    command: list[str], *, environment: dict[str, str], cwd: Path, attempts: int = 3
) -> tuple[list[float], subprocess.CompletedProcess[str]]:
    durations: list[float] = []
    completed: subprocess.CompletedProcess[str] | None = None
    for _ in range(attempts):
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
            shell=False,
        )
        durations.append((time.perf_counter() - started) * 1000)
        if completed.returncode != 0:
            break
    assert completed is not None
    return durations, completed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    executable = args.command.expanduser().resolve()
    home = args.home.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not executable.is_file():
        raise SystemExit(f"installed command is missing: {executable}")
    home.mkdir(parents=True, exist_ok=True)
    consumer = home.parent / "performance-consumer"
    consumer.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["VILLANI_HOME"] = str(home)
    environment.pop("PYTHONPATH", None)
    targets = {
        item["id"]: item
        for item in json.loads(CONTRACT.read_text(encoding="utf-8"))["targets"]
    }
    version_times, version = _measure(
        [str(executable), "--version"], environment=environment, cwd=consumer
    )
    doctor_times, doctor = _measure(
        [str(executable), "doctor", "--installation-only", "--json"],
        environment=environment,
        cwd=consumer,
    )
    doctor_json: dict[str, object] | None = None
    if doctor.returncode == 0:
        try:
            value = json.loads(doctor.stdout)
            doctor_json = value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            doctor_json = None
    results = {
        "shell_version_render_p95": {
            "samples_ms": [round(value, 3) for value in version_times],
            "observed_ms": round(max(version_times), 3),
            "maximum_ms": targets["shell_version_render_p95"]["maximum"],
            "exit_code": version.returncode,
            "passed": version.returncode == 0
            and max(version_times)
            <= targets["shell_version_render_p95"]["maximum"],
        },
        "installation_doctor_startup_p95": {
            "samples_ms": [round(value, 3) for value in doctor_times],
            "observed_ms": round(max(doctor_times), 3),
            "maximum_ms": targets["installation_doctor_startup_p95"]["maximum"],
            "exit_code": doctor.returncode,
            "valid_json": doctor_json is not None,
            "repositories_modified": (
                doctor_json.get("repositories_modified") if doctor_json else None
            ),
            "passed": doctor.returncode == 0
            and doctor_json is not None
            and doctor_json.get("repositories_modified") is False
            and max(doctor_times)
            <= targets["installation_doctor_startup_p95"]["maximum"],
        },
    }
    report = {
        "schema_version": "villani.performance_report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "command": executable.name,
        "source_checkout_cwd_used": False,
        "results": results,
        "passed": all(item["passed"] for item in results.values()),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(output)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
