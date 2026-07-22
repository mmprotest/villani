#!/usr/bin/env python3
"""Fail-closed CLI Agent Mode release phase with durable evidence.

The deterministic phase uses the actual Codex/Claude argument builders and stream
parsers through CLI-shaped fakes.  Real providers are optional and require the
separate smoke command's explicit consent contract.  A final report is written on
success, failure, timeout, or interruption.
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
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
SCENARIOS_PATH = HERE / "cli-agent-mode-scenarios.json"
MATRIX_TEMPLATE_PATH = HERE / "cli-agent-mode-conformance-matrix.json"
REPORT_SCHEMA_VERSION = "villani.cli_agent_mode_release_gate.v1"


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected an object in {path}")
    return value


def _junit(path: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    counts = {
        key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    cases: list[dict[str, Any]] = []
    for suite in suites:
        for case in suite.findall("testcase"):
            status = "PASS"
            if case.find("failure") is not None:
                status = "FAIL"
            elif case.find("error") is not None:
                status = "ERROR"
            elif case.find("skipped") is not None:
                status = "SKIPPED"
            cases.append(
                {
                    "classname": case.attrib.get("classname", ""),
                    "name": case.attrib.get("name", ""),
                    "time_seconds": float(case.attrib.get("time", "0") or 0),
                    "status": status,
                }
            )
    return counts, cases


def _redact_text(value: str, source_root: Path) -> str:
    output = value
    replacements = [(str(source_root.resolve()), "<source-root>")]
    try:
        replacements.append((str(Path.home().resolve()), "<home>"))
    except OSError:
        pass
    for path, token in replacements:
        for variant in {path, path.replace("\\", "\\\\"), path.replace("\\", "/")}:
            output = re.sub(re.escape(variant), token, output, flags=re.IGNORECASE)
    return output


def _safe_command(command: Iterable[str], source_root: Path) -> list[str]:
    return [_redact_text(str(item), source_root) for item in command]


def _tree_stats(root: Path) -> tuple[int, int]:
    files = [path for path in root.rglob("*") if path.is_file()]
    return len(files), sum(path.stat().st_size for path in files)


def _remove_ephemeral_tree(root: Path) -> None:
    """Remove one validated test tree, including read-only Git object files."""

    def make_writable_and_retry(function: Any, path: str, _exc_info: Any) -> None:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        function(path)

    shutil.rmtree(root, onerror=make_writable_and_retry)


def _run(
    *,
    phase: str,
    command: list[str],
    cwd: Path,
    artifacts: Path,
    source_root: Path,
    timeout: int,
    environment: dict[str, str] | None = None,
    repetition: int | None = None,
) -> dict[str, Any]:
    suffix = f"-{repetition}" if repetition is not None else ""
    log = artifacts / "logs" / f"{phase}{suffix}.log"
    junit = artifacts / "junit" / f"{phase}{suffix}.xml"
    log.parent.mkdir(parents=True, exist_ok=True)
    junit.parent.mkdir(parents=True, exist_ok=True)
    artifact_token = hashlib.sha256(str(artifacts).encode("utf-8")).hexdigest()[:8]
    phase_token = hashlib.sha256(f"{phase}{suffix}".encode("utf-8")).hexdigest()[:10]
    governed_test_root = source_root / ".m8g" / artifact_token
    test_temp = governed_test_root / phase_token
    test_temp.mkdir(parents=True, exist_ok=True)
    is_pytest = "pytest" in command
    actual = [
        *command,
        *(
            ["--junitxml", str(junit), "--basetemp", str(test_temp)]
            if is_pytest
            else []
        ),
    ]
    actual_environment = os.environ.copy()
    if environment:
        actual_environment.update(environment)
    for name in ("TEMP", "TMP", "TMPDIR"):
        actual_environment[name] = str(test_temp)
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            actual,
            cwd=cwd,
            env=actual_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            shell=False,
        )
        output = completed.stdout
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as error:
        output = f"{type(error).__name__}: {error}"
        exit_code = 124
        timed_out = True
    log.write_text(_redact_text(output[-2_000_000:], source_root), encoding="utf-8")
    counts: dict[str, int] = {}
    cases: list[dict[str, Any]] = []
    if junit.is_file():
        counts, cases = _junit(junit)
    ephemeral_files, ephemeral_bytes = _tree_stats(test_temp)
    cleanup_started = time.monotonic()
    cleanup_error: str | None = None
    try:
        test_temp.resolve().relative_to(governed_test_root.resolve())
        _remove_ephemeral_tree(test_temp)
        for empty_parent in (governed_test_root, governed_test_root.parent):
            try:
                empty_parent.rmdir()
            except OSError:
                pass
    except Exception as error:  # cleanup is release evidence and fails closed
        cleanup_error = _redact_text(f"{type(error).__name__}: {error}", source_root)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"\nEPHEMERAL TEST CLEANUP FAILED: {cleanup_error}\n")
    cleanup_seconds = round(time.monotonic() - cleanup_started, 3)
    passed = (
        exit_code == 0
        and (
            not counts
            or (
                counts.get("tests", 0) > 0
                and counts.get("failures", 0) == 0
                and counts.get("errors", 0) == 0
                and counts.get("skipped", 0) == 0
            )
        )
        and cleanup_error is None
    )
    return {
        "phase": phase,
        "repetition": repetition,
        "status": "PASS" if passed else "FAIL",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "command": _safe_command(actual, source_root),
        "cwd": _redact_text(str(cwd.resolve()), source_root),
        "timeout_seconds": timeout,
        "ephemeral_test_files": ephemeral_files,
        "ephemeral_test_bytes": ephemeral_bytes,
        "ephemeral_cleanup_status": "PASS" if cleanup_error is None else "FAIL",
        "ephemeral_cleanup_seconds": cleanup_seconds,
        "ephemeral_cleanup_error": cleanup_error,
        "environment_names": sorted(
            key for key in (environment or {}) if key.startswith("VILLANI_")
        ),
        "counts": counts,
        "cases": cases,
        "log": log.relative_to(artifacts).as_posix(),
        "junit": junit.relative_to(artifacts).as_posix() if junit.is_file() else None,
    }


def _scenario_nodes(document: dict[str, Any], source_root: Path) -> list[str]:
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 30:
        raise RuntimeError(
            "the fake CLI conformance manifest must contain 30 scenarios"
        )
    if [item.get("id") for item in scenarios] != list(range(1, 31)):
        raise RuntimeError(
            "fake CLI scenario IDs must be the consecutive integers 1..30"
        )
    nodes: list[str] = []
    for scenario in scenarios:
        name = scenario.get("name")
        supplied = scenario.get("test_nodes")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(supplied, list)
            or not supplied
        ):
            raise RuntimeError(f"malformed fake CLI scenario record: {scenario!r}")
        for raw in supplied:
            node = str(raw)
            relative = node.split("::", 1)[0]
            path = source_root / "components" / "villani-ops" / relative
            if not path.is_file():
                raise RuntimeError(
                    f"scenario {scenario['id']} references missing test file {relative}"
                )
            if node not in nodes:
                nodes.append(node)
    return nodes


def _scenario_results(
    manifest: dict[str, Any], cases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for scenario in manifest["scenarios"]:
        matches: list[dict[str, Any]] = []
        missing: list[str] = []
        for node in scenario["test_nodes"]:
            function = node.rsplit("::", 1)[-1]
            found = [
                item
                for item in cases
                if str(item.get("name", "")) == function
                or str(item.get("name", "")).startswith(function + "[")
            ]
            if not found:
                missing.append(node)
            matches.extend(found)
        status = (
            "PASS"
            if not missing
            and matches
            and all(item["status"] == "PASS" for item in matches)
            else "FAIL"
        )
        output.append(
            {
                "id": scenario["id"],
                "name": scenario["name"],
                "status": status,
                "test_case_count": len(matches),
                "missing_test_nodes": missing,
                "maximum_test_seconds": max(
                    (float(item["time_seconds"]) for item in matches), default=0.0
                ),
            }
        )
    return output


def _secret_scan(root: Path) -> dict[str, Any]:
    patterns = {
        "openai_key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
        "github_token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
        "bearer_token": re.compile(rb"Bearer[ \t]+[A-Za-z0-9._~+/-]{20,}"),
        "private_key": re.compile(
            rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
        "api_key_assignment": re.compile(
            rb"(?i)(?:OPENAI|ANTHROPIC|GITHUB|AZURE|GOOGLE)[A-Z0-9_]*_KEY[ \t]*[:=][ \t]*[^\s,}\]]{8,}"
        ),
        "credential_file_path": re.compile(
            rb"(?i)[/\\](?:\.codex|\.claude|\.aws|\.config)[/\\][^\r\n]{0,80}(?:auth|credentials?)(?:\.json)?"
        ),
    }
    try:
        home = str(Path.home().resolve())
    except OSError:
        home = ""
    if home:
        variants = {
            home.encode("utf-8"),
            home.replace("\\", "\\\\").encode("utf-8"),
            home.replace("\\", "/").encode("utf-8"),
        }
        patterns["user_home_path"] = re.compile(
            b"(?:" + b"|".join(re.escape(item) for item in variants) + b")",
            re.IGNORECASE,
        )
    findings: list[dict[str, Any]] = []
    scanned = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "secret-scan.json":
            continue
        data = path.read_bytes()
        scanned += 1
        for code, pattern in patterns.items():
            if pattern.search(data):
                findings.append(
                    {"code": code, "path": path.relative_to(root).as_posix()}
                )
    return {
        "schema_version": "villani.cli_agent_secret_scan.v1",
        "status": "PASS" if not findings else "FAIL",
        "files_scanned": scanned,
        "finding_count": len(findings),
        "findings": findings,
        "checks": sorted(patterns),
        "scope": "CLI Agent Mode release-phase artifacts; this is not an external security audit",
    }


def _resource_bounds(
    cancellation_commands: list[dict[str, Any]],
    command_records: list[dict[str, Any]],
) -> dict[str, Any]:
    cancellation_seconds = [
        float(item["duration_seconds"]) for item in cancellation_commands
    ]
    return {
        "schema_version": "villani.cli_agent_resource_bounds.v1",
        "status": (
            "PASS"
            if cancellation_seconds
            and max(cancellation_seconds) <= 30
            and all(
                item.get("ephemeral_cleanup_status") == "PASS"
                for item in command_records
            )
            else "FAIL"
        ),
        "process_probe_timeout_seconds": 8,
        "doctor_probe_timeout_seconds": 8,
        "role_process_timeout_seconds_default": 180,
        "role_process_timeout_configurable": True,
        "controller_wall_time_budget_default": None,
        "maximum_event_line_bytes": 1048576,
        "maximum_stdout_bytes": 16777216,
        "maximum_stderr_bytes": 16777216,
        "maximum_in_memory_tail_bytes_per_stream": 16384,
        "maximum_read_chunk_bytes": 1048576,
        "default_read_chunk_bytes": 65536,
        "maximum_concurrent_candidates_per_system": 32,
        "default_concurrent_candidates_per_system": 1,
        "graceful_process_shutdown_seconds_default": 3,
        "baseline_copy_maximum_total_bytes_default": 524288000,
        "baseline_copy_maximum_file_bytes_default": 52428800,
        "cancellation_cleanup_repetitions": len(cancellation_seconds),
        "maximum_observed_cancellation_test_seconds": max(
            cancellation_seconds, default=None
        ),
        "cancellation_test_bound_seconds": 30,
        "all_ephemeral_test_directories_cleaned": all(
            item.get("ephemeral_cleanup_status") == "PASS" for item in command_records
        ),
        "maximum_observed_ephemeral_test_files": max(
            (int(item.get("ephemeral_test_files", 0)) for item in command_records),
            default=0,
        ),
        "maximum_observed_ephemeral_test_bytes": max(
            (int(item.get("ephemeral_test_bytes", 0)) for item in command_records),
            default=0,
        ),
        "maximum_observed_ephemeral_cleanup_seconds": max(
            (
                float(item.get("ephemeral_cleanup_seconds", 0))
                for item in command_records
            ),
            default=0,
        ),
        "run_bundle_growth_policy": (
            "Each process stream and event line is bounded; repository baseline and candidate "
            "capture use configured per-file/total limits. No unbounded in-memory transcript is retained."
        ),
    }


def _matrix(
    *, deterministic_passed: bool, real_smoke: dict[str, Any] | None
) -> dict[str, Any]:
    template = _load_json(MATRIX_TEMPLATE_PATH)
    columns = list(template["columns"])
    provider_status: dict[str, str] = {"codex": "NOT_RUN", "claude": "NOT_RUN"}
    provider_observations: dict[str, dict[str, Any]] = {}
    if real_smoke:
        providers = real_smoke.get("providers")
        if isinstance(providers, list):
            provider_observations = {
                str(item.get("provider")): item
                for item in providers
                if isinstance(item, dict)
                and str(item.get("provider")) in {"codex", "claude"}
            }
        cases = real_smoke.get("cases")
        if isinstance(cases, list):
            for provider in ("codex", "claude"):
                relevant = [
                    str(item.get("status"))
                    for item in cases
                    if str(item.get("name", "")).startswith(provider)
                ]
                provider_status[provider] = (
                    "PASS"
                    if relevant and all(item == "PASS" for item in relevant)
                    else "FAIL"
                    if "FAIL" in relevant
                    else "NOT_RUN"
                )
    rows: list[dict[str, Any]] = []
    for raw in template["rows"]:
        row = dict(raw)
        system = str(row["system"])
        if system == "deterministic":
            values = {
                "configured": "PASS" if deterministic_passed else "FAIL",
                "executable_present": "NOT_APPLICABLE",
                "auth_ready": "NOT_APPLICABLE",
                "version_supported": "NOT_APPLICABLE",
                "structured_output": "NOT_APPLICABLE",
                "permissions": "NOT_APPLICABLE",
                "cancellation": "NOT_APPLICABLE",
                "artifact_completeness": "PASS" if deterministic_passed else "FAIL",
                "normalized_events": "NOT_APPLICABLE",
                "isolation": "NOT_APPLICABLE",
                "role_contract": "PASS" if deterministic_passed else "FAIL",
                "fake_conformance": "PASS" if deterministic_passed else "FAIL",
                "real_smoke_status": "NOT_APPLICABLE",
                "production_enabled": "PASS" if deterministic_passed else "FAIL",
            }
        elif system == "api":
            values = {
                column: (
                    "NOT_APPLICABLE"
                    if column
                    in {
                        "executable_present",
                        "version_supported",
                        "permissions",
                        "normalized_events",
                        "real_smoke_status",
                    }
                    else "PASS"
                    if deterministic_passed
                    else "FAIL"
                )
                for column in columns
            }
            values["auth_ready"] = "NOT_RUN"
            values["cancellation"] = "PASS" if deterministic_passed else "FAIL"
        else:
            values = {
                column: "PASS" if deterministic_passed else "FAIL" for column in columns
            }
            observation = provider_observations.get(system)
            for column, key in (
                ("executable_present", "executable_present"),
                ("auth_ready", "auth_ready"),
                ("version_supported", "version_supported"),
            ):
                values[column] = (
                    "NOT_RUN"
                    if observation is None
                    else "PASS"
                    if observation.get(key) is True
                    else "FAIL"
                )
            values["real_smoke_status"] = provider_status[system]
            values["production_enabled"] = (
                "PASS"
                if deterministic_passed and provider_status[system] == "PASS"
                else "FAIL"
            )
        row.update(values)
        rows.append(row)
    return {
        "schema_version": template["schema_version"],
        "generated_at": _utc_now(),
        "columns": columns,
        "rows": rows,
        "invariant": (
            "production_enabled is PASS only after role contract, permissions, isolation, "
            "structured output, cancellation, artifacts, fake conformance, and real smoke pass."
        ),
        "status_semantics": {
            "PASS": "the relevant deterministic or observed evidence passed",
            "FAIL": "the relevant evidence failed or was observed unavailable",
            "NOT_RUN": "environment-dependent evidence was not exercised",
            "NOT_APPLICABLE": "the column does not apply to this role/system",
        },
    }


def _evidence_index(artifacts: Path) -> dict[str, Any]:
    index_path = artifacts / "release-evidence-index.json"
    records = []
    for path in sorted(item for item in artifacts.rglob("*") if item.is_file()):
        if path == index_path:
            continue
        records.append(
            {
                "path": path.relative_to(artifacts).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return {
        "schema_version": "villani.cli_agent_release_evidence_index.v1",
        "generated_at": _utc_now(),
        "artifact_count": len(records),
        "artifacts": records,
    }


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0915
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--test-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--installed-python", type=Path)
    parser.add_argument("--real-smoke", action="store_true")
    args = parser.parse_args(argv)
    source_root = args.source_root.resolve()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    report_path = artifacts / "cli-agent-mode-release-report.json"
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "started_at": _utc_now(),
        "finished_at": None,
        "status": "failed",
        "certification_status": "FAIL",
        "required_deterministic_evidence_complete": False,
        "real_provider_smoke_optional": True,
        "real_provider_smoke_requested": bool(args.real_smoke),
        "phases": [],
        "scenario_results": [],
        "failures": [],
        "reports": {
            "matrix": "cli-agent-mode-conformance-matrix.json",
            "resource_bounds": "resource-bounds.json",
            "secret_scan": "secret-scan.json",
            "command_report": "exact-test-command-report.json",
            "evidence_index": "release-evidence-index.json",
        },
    }
    command_records: list[dict[str, Any]] = []
    exit_code = 1
    try:
        scenarios = _load_json(SCENARIOS_PATH)
        nodes = _scenario_nodes(scenarios, source_root)
        ops = source_root / "components" / "villani-ops"
        test_python = args.test_python.resolve()
        installed_python = (args.installed_python or args.test_python).resolve()

        installed_probe = _run(
            phase="packaged_artifact_probe",
            command=[
                str(installed_python),
                "-c",
                (
                    "from villani_ops.closed_loop.agent_systems.role_models import AgentRole; "
                    "from villani_ops.closed_loop.agent_systems.configuration import migrate_agent_system_configuration; "
                    "c={'backends':{'api-main':{'provider':'openai-compatible','model':'m','roles':['classification','coding','review','selection']}}}; "
                    "assert len(AgentRole) == 4; "
                    "assert migrate_agent_system_configuration(c)[0]['active_execution_profile'] == 'api'"
                ),
            ],
            cwd=source_root,
            artifacts=artifacts,
            source_root=source_root,
            timeout=60,
        )
        command_records.append(installed_probe)

        api_regression = _run(
            phase="schema_migration_and_api_regression",
            command=[
                str(test_python),
                "-m",
                "pytest",
                "-q",
                "villani_ops/tests/test_cli_agent_mode_m7.py::test_existing_api_migration_is_default_and_idempotent",
                "villani_ops/tests/test_cli_classification_selection.py::test_api_factories_remain_compatible_and_cli_factories_use_same_ports",
                "villani_ops/tests/test_cli_verification.py::test_api_verifier_factory_remains_compatible",
                "villani_ops/tests/test_codex_cli_coding.py::test_hybrid_profile_still_constructs_existing_api_coder_when_selected",
            ],
            cwd=ops,
            artifacts=artifacts,
            source_root=source_root,
            timeout=180,
        )
        command_records.append(api_regression)

        fake_suite = _run(
            phase="fake_cli_end_to_end_scenarios",
            command=[str(test_python), "-m", "pytest", "-q", *nodes],
            cwd=ops,
            artifacts=artifacts,
            source_root=source_root,
            timeout=600,
        )
        command_records.append(fake_suite)
        report["scenario_results"] = _scenario_results(
            scenarios, list(fake_suite.get("cases") or [])
        )

        mixed_profiles = _run(
            phase="mixed_profile_end_to_end",
            command=[
                str(test_python),
                "-m",
                "pytest",
                "-q",
                "villani_ops/tests/test_cli_verification.py::test_all_coder_verifier_combinations_use_independent_sessions",
                "villani_ops/tests/test_cli_classification_selection.py::test_declared_profile_combinations_resolve_through_existing_role_ports",
                "villani_ops/tests/test_cli_classification_selection.py::test_same_vendor_role_bindings_have_distinct_invocation_identities",
                "villani_ops/tests/test_cli_agent_mode_m8.py::test_five_sequential_role_processes_have_unique_processes_sessions_and_workspaces",
            ],
            cwd=ops,
            artifacts=artifacts,
            source_root=source_root,
            timeout=240,
        )
        command_records.append(mixed_profiles)

        cancellation_records: list[dict[str, Any]] = []
        for repetition in range(1, 4):
            cancellation = _run(
                phase="cancellation_and_cleanup",
                repetition=repetition,
                command=[
                    str(test_python),
                    "-m",
                    "pytest",
                    "-q",
                    "villani_ops/tests/closed_loop/test_cli_runtime.py::test_child_process_tree_is_cleaned_repeatedly",
                    "villani_ops/tests/test_codex_cli_coding.py::test_controller_cancellation_is_distinct_and_cleans_child_process",
                    "villani_ops/tests/test_claude_code_cli_coding.py::test_controller_cancellation_cleans_descendant_and_preserves_partial_patch",
                    "villani_ops/tests/test_cli_verification.py::test_cancellation_is_distinct_and_fails_closed",
                ],
                cwd=ops,
                artifacts=artifacts,
                source_root=source_root,
                timeout=120,
            )
            cancellation_records.append(cancellation)
            command_records.append(cancellation)

        privacy = _run(
            phase="blindness_secret_and_projection",
            command=[
                str(test_python),
                "-m",
                "pytest",
                "-q",
                "villani_ops/tests/test_cli_agent_mode_m8.py::test_verifier_blindness_canary_is_absent_from_workspace_and_prompt",
                "villani_ops/tests/test_cli_agent_mode_m8.py::test_infrastructure_failed_candidate_never_enters_selector_packet",
                "villani_ops/tests/test_cli_agent_mode_m8.py::test_failure_projection_contains_every_required_public_fact",
                "villani_ops/tests/test_cli_agent_mode_m8.py::test_release_inputs_have_no_quota_or_new_provider_surface",
                "villani_ops/tests/test_cli_agent_mode_m8.py::test_release_commands_are_bounded_consent_gated_and_never_use_a_shell",
                "villani_ops/tests/test_cli_verification.py::test_binary_user_projection_is_exact_for_both_decisions",
                "villani_ops/tests/test_cli_agent_mode_m7.py::test_role_invocation_evidence_is_safe_strict_and_preserves_unknown_cost",
            ],
            cwd=ops,
            artifacts=artifacts,
            source_root=source_root,
            timeout=240,
        )
        command_records.append(privacy)

        real_smoke_report: dict[str, Any] | None = None
        if args.real_smoke:
            smoke_artifacts = artifacts / "real-smoke"
            smoke = _run(
                phase="optional_real_cli_smoke",
                command=[
                    str(test_python),
                    str(
                        source_root / "release-verification" / "run_cli_agent_smoke.py"
                    ),
                    "--consent",
                    "--python",
                    str(test_python),
                    "--source-root",
                    str(source_root),
                    "--artifacts",
                    str(smoke_artifacts),
                ],
                cwd=source_root,
                artifacts=artifacts,
                source_root=source_root,
                timeout=1200,
                environment=os.environ.copy(),
            )
            command_records.append(smoke)
            smoke_path = smoke_artifacts / "real-cli-smoke-report.json"
            if smoke_path.is_file():
                real_smoke_report = _load_json(smoke_path)
        else:
            detection_artifacts = artifacts / "real-provider-detection"
            detection = _run(
                phase="real_provider_detection",
                command=[
                    str(test_python),
                    str(
                        source_root / "release-verification" / "run_cli_agent_smoke.py"
                    ),
                    "--detect-only",
                    "--python",
                    str(test_python),
                    "--source-root",
                    str(source_root),
                    "--artifacts",
                    str(detection_artifacts),
                ],
                cwd=source_root,
                artifacts=artifacts,
                source_root=source_root,
                timeout=60,
            )
            command_records.append(detection)
            detection_path = detection_artifacts / "real-cli-smoke-report.json"
            if detection_path.is_file():
                real_smoke_report = _load_json(detection_path)
            report["phases"].append(
                {
                    "phase": "optional_real_cli_smoke",
                    "status": "NOT_RUN",
                    "reason": "optional real-provider smoke was not explicitly enabled",
                }
            )

        resource_bounds = _resource_bounds(cancellation_records, command_records)
        _write_json(artifacts / "resource-bounds.json", resource_bounds)
        deterministic_commands = [
            item
            for item in command_records
            if item["phase"]
            not in {"optional_real_cli_smoke", "real_provider_detection"}
        ]
        scenarios_passed = all(
            item["status"] == "PASS" for item in report["scenario_results"]
        )
        deterministic_passed = (
            all(item["status"] == "PASS" for item in deterministic_commands)
            and scenarios_passed
            and resource_bounds["status"] == "PASS"
        )

        command_report = {
            "schema_version": "villani.cli_agent_test_commands.v1",
            "generated_at": _utc_now(),
            "command_count": len(command_records),
            "commands": command_records,
            "totals": {
                key: sum(
                    int(item.get("counts", {}).get(key, 0)) for item in command_records
                )
                for key in ("tests", "failures", "errors", "skipped")
            },
        }
        _write_json(artifacts / "exact-test-command-report.json", command_report)
        scan = _secret_scan(artifacts)
        _write_json(artifacts / "secret-scan.json", scan)
        deterministic_passed = deterministic_passed and scan["status"] == "PASS"
        matrix = _matrix(
            deterministic_passed=deterministic_passed,
            real_smoke=real_smoke_report,
        )
        _write_json(artifacts / "cli-agent-mode-conformance-matrix.json", matrix)

        report["phases"].extend(command_records)
        report["required_deterministic_evidence_complete"] = deterministic_passed
        if not deterministic_passed:
            report["status"] = "failed"
            report["certification_status"] = "FAIL"
            report["failures"] = [
                {
                    "stage": item["phase"],
                    "role": "release_certification",
                    "agent_system": "deterministic_test_harness",
                    "safe_error_summary": "required deterministic release evidence failed",
                    "target_repository_modified": False,
                    "partial_patch_preserved": False,
                    "automatic_fallback_performed": False,
                    "exact_repair_action": f"Inspect {item['log']} and rerun this release phase.",
                    "evidence_path": item["log"],
                }
                for item in deterministic_commands
                if item["status"] != "PASS"
            ]
            exit_code = 1
        else:
            smoke_status = (
                str(real_smoke_report.get("status")) if real_smoke_report else "NOT_RUN"
            )
            report["status"] = "passed"
            report["certification_status"] = (
                "PASS" if smoke_status == "PASS" else "PARTIAL"
            )
            report["real_provider_smoke_status"] = smoke_status
            report["certification_reason"] = (
                "All deterministic certification and every explicitly requested real-provider smoke passed."
                if smoke_status == "PASS"
                else "All deterministic certification passed; optional real-provider evidence is incomplete."
            )
            exit_code = 0
    except KeyboardInterrupt:
        report["status"] = "failed"
        report["certification_status"] = "FAIL"
        report["failures"].append(
            {
                "stage": "cli_agent_mode_release_gate",
                "role": "release_certification",
                "agent_system": "deterministic_test_harness",
                "safe_error_summary": "release phase interrupted",
                "target_repository_modified": False,
                "partial_patch_preserved": False,
                "automatic_fallback_performed": False,
                "exact_repair_action": "Rerun python release-verification/run_cli_agent_gate.py with the same arguments.",
                "evidence_path": report_path.name,
            }
        )
        exit_code = 130
    except Exception as error:
        report["status"] = "failed"
        report["certification_status"] = "FAIL"
        report["failures"].append(
            {
                "stage": "cli_agent_mode_release_gate",
                "role": "release_certification",
                "agent_system": "deterministic_test_harness",
                "safe_error_summary": _redact_text(
                    f"{type(error).__name__}: {error}", source_root
                ),
                "target_repository_modified": False,
                "partial_patch_preserved": False,
                "automatic_fallback_performed": False,
                "exact_repair_action": "Correct the reported release evidence defect and rerun the CLI Agent Mode release phase.",
                "evidence_path": report_path.name,
            }
        )
        exit_code = 1
    finally:
        report["finished_at"] = _utc_now()
        _write_json(report_path, report)
        _write_json(
            artifacts / "release-evidence-index.json", _evidence_index(artifacts)
        )
        print(report_path)
        print(report["certification_status"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
