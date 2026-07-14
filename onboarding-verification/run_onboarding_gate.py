#!/usr/bin/env python3
"""Recorded clean-user setup, service, doctor, console, and sample-task gate."""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS = ROOT / "onboarding-verification" / "artifacts" / "latest"
MODEL_FIXTURE = ROOT / "release-verification" / "fixtures" / "model_service.py"
SCREENSHOT_SCRIPT = ROOT / "onboarding-verification" / "capture_screenshots.mjs"


class GateFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandRecord:
    command: list[str]
    cwd: str
    exit_code: int
    elapsed_seconds: float
    stdout_path: str
    stderr_path: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_artifacts(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    allowed = (ROOT / "onboarding-verification").resolve()
    if resolved != allowed and allowed not in resolved.parents:
        # Pytest and CI may explicitly choose an external temporary directory.
        if "pytest-" not in str(resolved).lower() and not os.environ.get(
            "VILLANI_ONBOARDING_ALLOW_EXTERNAL_ARTIFACTS"
        ):
            raise GateFailure(f"refusing artifact path outside onboarding-verification: {resolved}")
    return resolved


def _villani_prefix(python: Path) -> list[str]:
    # ``-m`` guarantees the command is loaded from the exact interpreter under
    # test. Release packaging separately validates the generated console shim.
    return [str(python), "-m", "villani_distribution.frozen_entry"]


def _run(
    records: list[CommandRecord],
    artifacts: Path,
    label: str,
    command: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path = ROOT,
    timeout: float = 300,
    require_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            shell=False,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        stdout = str(error.stdout or "")
        stderr = str(error.stderr or "") + f"\nTimed out after {timeout} seconds.\n"
        completed = subprocess.CompletedProcess(list(command), 124, stdout, stderr)
    stdout_path = artifacts / f"{label}.stdout.log"
    stderr_path = artifacts / f"{label}.stderr.log"
    stdout_path.write_text(completed.stdout or "", encoding="utf-8")
    stderr_path.write_text(completed.stderr or "", encoding="utf-8")
    records.append(
        CommandRecord(
            list(command),
            str(cwd.resolve()),
            int(completed.returncode),
            round(time.monotonic() - started, 3),
            str(stdout_path),
            str(stderr_path),
        )
    )
    if require_success and completed.returncode != 0:
        raise GateFailure(
            f"{label} failed with exit code {completed.returncode}; see {stdout_path} and {stderr_path}"
        )
    return completed


def _wait_for_endpoint(process: subprocess.Popen[bytes], path: Path) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise GateFailure(f"fixture model service exited with {process.returncode}")
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            endpoint = value.get("base_url") if isinstance(value, dict) else None
            if isinstance(endpoint, str) and endpoint:
                return endpoint
        time.sleep(0.05)
    raise GateFailure("fixture model service did not publish its endpoint")


def _transcript_html(setup: str, doctor: str, open_output: str) -> str:
    def panel(identifier: str, title: str, command: str, body: str) -> str:
        return (
            f'<section id="{identifier}" class="panel"><header><i></i><b>{html.escape(title)}</b>'
            f'<span>{html.escape(command)}</span></header><pre>{html.escape(body)}</pre></section>'
        )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Villani guided setup recording</title><style>
*{{box-sizing:border-box}}body{{margin:0;padding:42px;background:#090d19;color:#e7ecfb;font:15px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}}
main{{width:1120px;margin:auto}}h1{{font:700 34px/1.2 Inter,system-ui,sans-serif;margin:0 0 8px}}.lede{{font:16px Inter,system-ui,sans-serif;color:#9caccf;margin:0 0 30px}}
.panel{{margin:0 0 28px;border:1px solid #2b3858;border-radius:14px;overflow:hidden;background:#11182a;box-shadow:0 22px 55px #0007}}
header{{height:52px;padding:0 18px;display:flex;align-items:center;gap:12px;background:#182238;border-bottom:1px solid #2b3858;font-family:Inter,system-ui,sans-serif}}
header i{{width:11px;height:11px;border-radius:50%;background:#45dfa7;box-shadow:0 0 14px #45dfa7}}header b{{font-size:15px}}header span{{margin-left:auto;color:#8fa0c2;font:13px ui-monospace,monospace}}
pre{{white-space:pre-wrap;word-break:break-word;margin:0;padding:22px;color:#dce6ff}}.stamp{{color:#56e0ae;font:700 14px Inter,system-ui,sans-serif;margin-bottom:12px}}
</style></head><body><main><div class="stamp">RECORDED INTEGRATION · {html.escape(utc_now())}</div><h1>Villani first-run setup</h1>
<p class="lede">Detected model → atomic configuration → service → diagnostic → sample task.</p>
{panel('setup', 'Guided setup and sample task', 'villani setup', setup)}
{panel('doctor', 'Environment diagnostics', 'villani doctor', doctor)}
{panel('open', 'Console handoff', 'villani open', open_output)}
</main></body></html>"""


def run_gate(
    *, artifacts: Path, python: Path, skip_screenshots: bool = False
) -> dict[str, Any]:
    artifacts = _safe_artifacts(artifacts)
    if artifacts.exists():
        shutil.rmtree(artifacts)
    artifacts.mkdir(parents=True)
    work = artifacts / "work"
    home = work / "home"
    temporary = work / "tmp"
    temporary.mkdir(parents=True)
    endpoint_file = work / "model-endpoint.json"
    model_requests = artifacts / "model-requests.jsonl"
    model_process_log = (artifacts / "model-service.log").open("wb")
    records: list[CommandRecord] = []
    report: dict[str, Any] = {
        "schema_version": "villani.onboarding_gate.v1",
        "started_at": utc_now(),
        "verdict": "ONBOARDING GATE FAILED",
        "python": str(python.resolve()),
        "villani_home": str(home.resolve()),
        "temporary_directory": str(temporary.resolve()),
        "commands": [],
        "screenshots": [],
    }
    env = dict(os.environ)
    env.update(
        {
            "VILLANI_HOME": str(home.resolve()),
            "TEMP": str(temporary.resolve()),
            "TMP": str(temporary.resolve()),
            "PYTHONUTF8": "1",
        }
    )
    for secret_name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "VILLANI_MODEL_API_KEY_ENV",
    ):
        env.pop(secret_name, None)
    fixture = subprocess.Popen(
        [
            str(python),
            str(MODEL_FIXTURE),
            "--log",
            str(model_requests),
            "--endpoint-file",
            str(endpoint_file),
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=model_process_log,
        stderr=subprocess.STDOUT,
        shell=False,
    )
    prefix = _villani_prefix(python)
    try:
        endpoint = _wait_for_endpoint(fixture, endpoint_file)
        setup = _run(
            records,
            artifacts,
            "01-setup",
            [
                *prefix,
                "setup",
                "--endpoint",
                endpoint,
                "--model",
                "fixture-onboarding",
                "--yes",
                "--start",
                "--no-automatic",
                "--no-open",
                "--sample",
            ],
            env=env,
        )
        if "Sample task completed successfully." not in setup.stdout:
            raise GateFailure("setup did not record a successful sample task")
        setup_record_path = home / "setup-record.json"
        setup_record = json.loads(setup_record_path.read_text(encoding="utf-8"))
        if setup_record.get("sample_exit_code") != 0:
            raise GateFailure("setup record does not prove sample task success")
        sample_path = Path(str(setup_record["sample"]["path"])).resolve()
        sample_validation = _run(
            records,
            artifacts,
            "02-sample-validation",
            [str(python), "-m", "unittest", "-q"],
            env=env,
            cwd=sample_path,
        )
        sample_diff = _run(
            records,
            artifacts,
            "03-sample-diff",
            ["git", "diff", "--check"],
            env=env,
            cwd=sample_path,
        )
        if "def subtract" not in (sample_path / "calculator.py").read_text(encoding="utf-8"):
            raise GateFailure("materialized sample patch does not contain subtract")
        doctor_json = _run(
            records,
            artifacts,
            "04-doctor-json",
            [*prefix, "doctor", "--json"],
            env=env,
        )
        doctor_document = json.loads(doctor_json.stdout)
        if not doctor_document.get("healthy") or doctor_document.get("summary", {}).get("failed") != 0:
            raise GateFailure("doctor did not report a healthy configured installation")
        doctor_human = _run(
            records,
            artifacts,
            "05-doctor-human",
            [*prefix, "doctor"],
            env=env,
        )
        opened = _run(
            records,
            artifacts,
            "06-open",
            [*prefix, "open", "--print-only"],
            env=env,
        )
        service = _run(
            records,
            artifacts,
            "07-service-status",
            [*prefix, "service", "status", "--json"],
            env=env,
        )
        service_document = json.loads(service.stdout)
        if not service_document.get("running") or not service_document.get("console_url"):
            raise GateFailure("service did not report a running console")
        console_url = str(service_document["console_url"])
        if console_url not in opened.stdout:
            raise GateFailure("villani open did not return the running console URL")
        transcript = artifacts / "setup-flow.html"
        transcript.write_text(
            _transcript_html(setup.stdout, doctor_human.stdout, opened.stdout),
            encoding="utf-8",
        )
        screenshots: list[str] = []
        if not skip_screenshots:
            node = shutil.which("node")
            if not node:
                raise GateFailure("Node.js is required to capture onboarding screenshots")
            _run(
                records,
                artifacts,
                "08-screenshots",
                [
                    node,
                    str(SCREENSHOT_SCRIPT),
                    "--transcript",
                    str(transcript),
                    "--console-url",
                    console_url,
                    "--output",
                    str(artifacts),
                ],
                env=env,
            )
            expected = (
                artifacts / "screenshots" / "01-setup-flow.png",
                artifacts / "screenshots" / "02-doctor.png",
                artifacts / "screenshots" / "03-villani-console.png",
            )
            for path in expected:
                if not path.is_file() or path.stat().st_size < 1_000:
                    raise GateFailure(f"required screenshot is missing or empty: {path}")
                screenshots.append(str(path))
        run_roots = sorted((home / "runs").glob("run_*"))
        if len(run_roots) != 1:
            raise GateFailure(f"expected one recorded sample run, found {len(run_roots)}")
        manifest = json.loads((run_roots[0] / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("final_state") != "COMPLETED" or not manifest.get("selected_attempt_id"):
            raise GateFailure("sample run did not reach a selected COMPLETED result")
        report.update(
            {
                "verdict": "ONBOARDING GATE PASSED",
                "configured_model": setup_record["selected_model"],
                "capability_status": "unrated",
                "configuration_path": str(home / "config.yaml"),
                "service_running": True,
                "console_url": console_url,
                "doctor": doctor_document,
                "sample_repository": str(sample_path),
                "sample_validation_exit_code": sample_validation.returncode,
                "sample_diff_check_exit_code": sample_diff.returncode,
                "sample_run_id": manifest["run_id"],
                "sample_final_state": manifest["final_state"],
                "sample_selected_attempt": manifest["selected_attempt_id"],
                "screenshots": screenshots,
            }
        )
    except BaseException as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            stopped = _run(
                records,
                artifacts,
                "09-service-stop",
                [*prefix, "service", "stop", "--json"],
                env=env,
                timeout=30,
                require_success=False,
            )
            stopped_document = json.loads(stopped.stdout) if stopped.stdout.strip() else {}
            report["service_stopped"] = stopped.returncode == 0 and not stopped_document.get(
                "running", True
            )
        except Exception as stop_error:
            report["service_stopped"] = False
            report["service_stop_error"] = f"{type(stop_error).__name__}: {stop_error}"
        if fixture.poll() is None:
            fixture.terminate()
            try:
                fixture.wait(timeout=5)
            except subprocess.TimeoutExpired:
                fixture.kill()
                fixture.wait(timeout=5)
        model_process_log.close()
        report["fixture_exit_code"] = fixture.returncode
        report["commands"] = [asdict(item) for item in records]
        report["completed_at"] = utc_now()
        if not report.get("service_stopped"):
            report["verdict"] = "ONBOARDING GATE FAILED"
        (artifacts / "onboarding-report.json").write_text(
            json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        (artifacts / "ONBOARDING_REPORT.txt").write_text(
            report["verdict"] + "\n", encoding="utf-8"
        )
    if not report.get("service_stopped"):
        raise GateFailure("Villani Service remained running after the recorded gate")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--skip-screenshots", action="store_true")
    args = parser.parse_args()
    try:
        report = run_gate(
            artifacts=args.artifacts,
            python=args.python,
            skip_screenshots=args.skip_screenshots,
        )
    except BaseException as error:
        print(f"ONBOARDING GATE FAILED: {error}", file=sys.stderr)
        return 1
    print(report["verdict"])
    print(f"Report: {args.artifacts.resolve() / 'onboarding-report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
