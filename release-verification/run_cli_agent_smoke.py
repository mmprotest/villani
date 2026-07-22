#!/usr/bin/env python3
"""Run explicitly consented, bounded real Codex/Claude CLI smoke checks.

This command never runs from the normal unit suite.  It probes installed CLIs without
reading credential files, then delegates tiny disposable-repository calls to the
production adapter smoke tests.  A report is written even when consent is absent,
providers are unavailable, a call fails, or the command is interrupted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONSENT_VALUE = "I_ACCEPT_EXTERNAL_USAGE"
CONSENT_ENVIRONMENT = "VILLANI_CLI_AGENT_SMOKE_CONSENT"
_VERSION = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _safe_path(value: str | None) -> str | None:
    if not value:
        return None
    path = str(Path(value).resolve())
    try:
        home = str(Path.home().resolve())
    except OSError:
        return path
    if path.casefold().startswith(home.casefold()):
        suffix = path[len(home) :].lstrip("/\\")
        return str(Path("<home>") / suffix) if suffix else "<home>"
    return path


def _redact_text(value: str) -> str:
    output = value
    replacements = [(str(ROOT.resolve()), "<source-root>")]
    try:
        replacements.append((str(Path.home().resolve()), "<home>"))
    except OSError:
        pass
    for path, token in replacements:
        for variant in {path, path.replace("\\", "\\\\"), path.replace("\\", "/")}:
            output = re.sub(re.escape(variant), token, output, flags=re.IGNORECASE)
    return output


def _safe_command(command: list[str]) -> list[str]:
    return [_redact_text(item) for item in command]


def _tree_stats(root: Path) -> tuple[int, int]:
    files = [path for path in root.rglob("*") if path.is_file()]
    return len(files), sum(path.stat().st_size for path in files)


def _remove_ephemeral_tree(root: Path) -> None:
    def make_writable_and_retry(function: Any, path: str, _exc_info: Any) -> None:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        function(path)

    shutil.rmtree(root, onerror=make_writable_and_retry)


def _version_supported(provider: str, output: str | None) -> bool:
    match = _VERSION.search(output or "")
    if match is None:
        return False
    version = tuple(int(part) for part in match.groups())
    if provider == "codex":
        return version >= (0, 138, 0)
    return (2, 1, 138) <= version < (2, 2, 0)


def _bounded_probe(command: list[str], timeout: float = 8.0) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "status": "ERROR",
            "exit_code": None,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "safe_summary": _redact_text(f"{type(error).__name__}: {error}"),
            "output_sha256": None,
        }
    combined = (result.stdout + "\n" + result.stderr).encode("utf-8", errors="replace")
    return {
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "exit_code": result.returncode,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "safe_summary": "probe completed" if result.returncode == 0 else "probe failed",
        "output_sha256": f"sha256:{hashlib.sha256(combined).hexdigest()}",
        "stdout": result.stdout[:4096],
        "stderr": result.stderr[:4096],
    }


def _detect_provider(name: str) -> dict[str, Any]:
    executable = shutil.which(name)
    if executable is None:
        return {
            "provider": name,
            "executable_present": False,
            "safe_executable_path": None,
            "exact_version": None,
            "auth_ready": False,
            "version_supported": False,
            "capability_ready": False,
            "status": "SKIPPED",
            "reason": f"`{name}` executable was not found on PATH",
            "exact_repair_action": (
                f"Install the provider-owned `{name}` CLI, then rerun this command."
            ),
        }
    version = _bounded_probe([executable, "--version"])
    if name == "codex":
        auth = _bounded_probe([executable, "login", "status"])
        help_probe = _bounded_probe([executable, "exec", "--help"])
        required = (
            "--ephemeral",
            "--json",
            "--output-schema",
            "--output-last-message",
            "--strict-config",
            "--config",
        )
        help_text = str(help_probe.get("stdout") or "")
        doctor = None
    else:
        auth = _bounded_probe([executable, "auth", "status"])
        help_probe = _bounded_probe([executable, "--help"])
        doctor = _bounded_probe([executable, "doctor"])
        required = (
            "--print",
            "--output-format",
            "--json-schema",
            "--no-session-persistence",
            "--tools",
        )
        help_text = str(help_probe.get("stdout") or "")
    capability_ready = help_probe["status"] == "PASS" and all(
        item in help_text for item in required
    )
    version_text = str(version.get("stdout") or "").strip() or None
    version_supported = _version_supported(name, version_text)
    ready = (
        version["status"] == "PASS"
        and version_supported
        and auth["status"] == "PASS"
        and capability_ready
        and (doctor is None or doctor["status"] == "PASS")
    )
    reasons: list[str] = []
    if version["status"] != "PASS":
        reasons.append("exact version probe failed")
    elif not version_supported:
        reasons.append("installed version is outside Villani's supported range")
    if auth["status"] != "PASS":
        reasons.append("provider-owned authentication is not ready")
    if not capability_ready:
        reasons.append("required safe structured-output capabilities are unavailable")
    if doctor is not None and doctor["status"] != "PASS":
        reasons.append("Claude Doctor is not ready")
    return {
        "provider": name,
        "executable_present": True,
        "safe_executable_path": _safe_path(executable),
        "exact_version": version_text,
        "version_supported": version_supported,
        "auth_ready": auth["status"] == "PASS",
        "capability_ready": capability_ready,
        "doctor_ready": None if doctor is None else doctor["status"] == "PASS",
        "status": "READY" if ready else "SKIPPED",
        "reason": "ready for explicitly consented smoke calls"
        if ready
        else "; ".join(reasons),
        "exact_repair_action": (
            "Configure a supported model string and rerun this smoke command."
            if ready
            else "Run `villani agents detect`, configure the detected system, then run `villani agents doctor <id>`."
        ),
        "probe_evidence": {
            "version": {
                key: value
                for key, value in version.items()
                if key not in {"stdout", "stderr"}
            },
            "authentication": {
                key: value
                for key, value in auth.items()
                if key not in {"stdout", "stderr"}
            },
            "capabilities": {
                key: value
                for key, value in help_probe.items()
                if key not in {"stdout", "stderr"}
            },
            "doctor": (
                {
                    key: value
                    for key, value in doctor.items()
                    if key not in {"stdout", "stderr"}
                }
                if doctor is not None
                else None
            ),
        },
    }


def _junit_counts(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return {
        key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def _run_pytest_case(
    *,
    python: Path,
    source_root: Path,
    artifacts: Path,
    name: str,
    nodes: list[str],
    environment: dict[str, str],
) -> dict[str, Any]:
    junit = artifacts / "junit" / f"{name}.xml"
    log = artifacts / "logs" / f"{name}.log"
    junit.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    test_temp = artifacts / "test-temp" / name
    test_temp.mkdir(parents=True, exist_ok=True)
    command = [
        str(python),
        "-m",
        "pytest",
        "-q",
        *nodes,
        "--junitxml",
        str(junit),
        "--basetemp",
        str(test_temp),
    ]
    run_environment = dict(environment)
    for variable in ("TEMP", "TMP", "TMPDIR"):
        run_environment[variable] = str(test_temp)
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=source_root / "components" / "villani-ops",
            env=run_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
            shell=False,
        )
        output = result.stdout
        exit_code = result.returncode
    except subprocess.TimeoutExpired as error:
        output = str(error)
        exit_code = 124
    counts = _junit_counts(junit) if junit.is_file() else {}
    ephemeral_files, ephemeral_bytes = _tree_stats(test_temp)
    cleanup_error: str | None = None
    try:
        governed_root = (artifacts / "test-temp").resolve()
        test_temp.resolve().relative_to(governed_root)
        _remove_ephemeral_tree(test_temp)
    except Exception as error:
        cleanup_error = _redact_text(f"{type(error).__name__}: {error}")
    safe_output = _redact_text(output[-2_000_000:])
    if cleanup_error:
        safe_output += f"\nEPHEMERAL TEST CLEANUP FAILED: {cleanup_error}\n"
    log.write_text(safe_output, encoding="utf-8")
    passed = (
        exit_code == 0
        and counts.get("tests", 0) > 0
        and counts.get("failures", 0) == 0
        and counts.get("errors", 0) == 0
        and counts.get("skipped", 0) == 0
        and cleanup_error is None
    )
    return {
        "name": name,
        "status": "PASS" if passed else "FAIL",
        "exit_code": exit_code,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "counts": counts,
        "command": _safe_command(command),
        "environment_names": sorted(
            key for key in environment if key.startswith("VILLANI_")
        ),
        "log": log.relative_to(artifacts).as_posix(),
        "junit": junit.relative_to(artifacts).as_posix() if junit.is_file() else None,
        "ephemeral_test_files": ephemeral_files,
        "ephemeral_test_bytes": ephemeral_bytes,
        "ephemeral_cleanup_status": "PASS" if cleanup_error is None else "FAIL",
        "ephemeral_cleanup_error": cleanup_error,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consent", action="store_true")
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="Run bounded readiness probes only; no model call is made.",
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args(argv)
    source_root = args.source_root.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifacts = (
        args.artifacts.resolve()
        if args.artifacts
        else (ROOT / "release-verification" / "artifacts" / "cli-agent-smoke" / stamp)
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    consented = args.consent or os.environ.get(CONSENT_ENVIRONMENT) == CONSENT_VALUE
    report: dict[str, Any] = {
        "schema_version": "villani.cli_agent_real_smoke.v1",
        "started_at": _utc_now(),
        "finished_at": None,
        "consent_required": True,
        "consent_recorded": consented,
        "external_usage_notice": (
            "Enabled coding, classification, verification, and selection calls may consume "
            "provider usage or incur provider charges. Villani does not inspect or manage quota."
        ),
        "disposable_repositories_only": True,
        "providers": [],
        "cases": [],
        "status": "NOT_RUN",
        "reason": None,
    }
    exit_code = 2
    try:
        report["providers"] = [_detect_provider("codex"), _detect_provider("claude")]
        if args.detect_only:
            report["reason"] = (
                "Bounded executable, version, authentication, and capability detection completed; no model call was made."
            )
            return 0
        if not consented:
            report["reason"] = (
                f"Pass --consent or set {CONSENT_ENVIRONMENT}={CONSENT_VALUE}; no model call was made."
            )
            return exit_code
        ready = {
            str(item["provider"]): item
            for item in report["providers"]
            if item.get("status") == "READY"
        }
        environment = os.environ.copy()
        environment.update(
            {
                "VILLANI_ENABLE_REAL_CODEX_TESTS": "1",
                "VILLANI_ENABLE_REAL_CLAUDE_TESTS": "1",
                "VILLANI_RUN_REAL_CLI_VERIFIER_SMOKE": "1",
                "VILLANI_RUN_REAL_CLI_ROLE_SMOKE": "1",
            }
        )
        cases: list[tuple[str, str, list[str], tuple[str, ...]]] = [
            (
                "codex_coder_existing_verifier",
                "codex",
                [
                    "villani_ops/tests/test_codex_cli_coding.py::test_real_codex_coding_smoke_is_explicitly_opt_in"
                ],
                ("VILLANI_REAL_CODEX_MODEL",),
            ),
            (
                "claude_coder_existing_verifier",
                "claude",
                [
                    "villani_ops/tests/test_claude_code_cli_coding.py::test_real_claude_code_smoke_is_explicitly_opt_in"
                ],
                ("VILLANI_REAL_CLAUDE_MODEL",),
            ),
            (
                "codex_independent_verifier",
                "codex",
                [
                    "villani_ops/tests/test_cli_verification.py::test_real_cli_verifier_smoke_is_opt_in[codex]"
                ],
                ("VILLANI_REAL_CODEX_VERIFIER_MODEL",),
            ),
            (
                "claude_independent_verifier",
                "claude",
                [
                    "villani_ops/tests/test_cli_verification.py::test_real_cli_verifier_smoke_is_opt_in[claude]"
                ],
                ("VILLANI_REAL_CLAUDE_VERIFIER_MODEL",),
            ),
            (
                "codex_classifier_and_selector",
                "codex",
                [
                    "villani_ops/tests/test_cli_classification_selection.py::test_real_cli_classifier_selector_smoke_is_opt_in[codex-codex]"
                ],
                ("VILLANI_REAL_CODEX_ROLE_MODEL",),
            ),
            (
                "claude_classifier_and_selector",
                "claude",
                [
                    "villani_ops/tests/test_cli_classification_selection.py::test_real_cli_classifier_selector_smoke_is_opt_in[claude-claude]"
                ],
                ("VILLANI_REAL_CLAUDE_ROLE_MODEL",),
            ),
        ]
        for name, provider, nodes, model_variables in cases:
            if provider not in ready:
                report["cases"].append(
                    {
                        "name": name,
                        "status": "SKIPPED",
                        "reason": next(
                            str(item.get("reason"))
                            for item in report["providers"]
                            if item.get("provider") == provider
                        ),
                    }
                )
                continue
            missing = [key for key in model_variables if not environment.get(key)]
            if missing:
                report["cases"].append(
                    {
                        "name": name,
                        "status": "SKIPPED",
                        "reason": f"set {', '.join(missing)} to an installed CLI model string",
                    }
                )
                continue
            report["cases"].append(
                _run_pytest_case(
                    python=args.python.resolve(),
                    source_root=source_root,
                    artifacts=artifacts,
                    name=name,
                    nodes=nodes,
                    environment=environment,
                )
            )
        statuses = [str(item.get("status")) for item in report["cases"]]
        if any(item == "FAIL" for item in statuses):
            report["status"] = "FAIL"
            report["reason"] = "At least one consented real-provider smoke case failed."
            exit_code = 1
        elif statuses and all(item == "PASS" for item in statuses):
            report["status"] = "PASS"
            report["reason"] = "Every declared bounded real-provider smoke case passed."
            exit_code = 0
        else:
            report["status"] = "PARTIAL"
            report["reason"] = (
                "Available consented cases passed, but at least one provider or model was unavailable."
            )
            exit_code = 0
    except KeyboardInterrupt:
        report["status"] = "FAIL"
        report["reason"] = (
            "Real-provider smoke was interrupted; partial evidence was preserved."
        )
        exit_code = 130
    except Exception as error:  # fail closed while preserving a report
        report["status"] = "FAIL"
        report["reason"] = (
            f"Smoke infrastructure failed: {type(error).__name__}: {error}"
        )
        exit_code = 1
    finally:
        report["finished_at"] = _utc_now()
        _write_json(artifacts / "real-cli-smoke-report.json", report)
        print(artifacts / "real-cli-smoke-report.json")
        print(report["status"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
