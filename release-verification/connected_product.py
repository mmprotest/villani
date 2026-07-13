#!/usr/bin/env python3
"""Run the packaged Villani product against PostgreSQL and deterministic models."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from canonical_reconciliation import (
    api_snapshot as canonical_api_snapshot,
    database_secret_occurrences,
    database_snapshots,
    local_snapshot as canonical_local_snapshot,
    project_spool_events,
    reconcile_sources,
)


ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_HEAD = "0a1b2c3d4e5f"
_SENSITIVE_LOG_TEXT = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{16,}\b"
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log: Path,
    expected: tuple[int, ...] = (0,),
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log_text = (
        "$ "
        + subprocess.list2cmdline(command)
        + "\n"
        + completed.stdout
        + completed.stderr
    )
    protected_values = {
        str(value)
        for key, value in env.items()
        if key.startswith("VILLANI_RELEASE_")
        and any(part in key for part in ("TOKEN", "SECRET", "PASSWORD"))
    }
    for secret in protected_values:
        if secret:
            log_text = log_text.replace(secret, "[REDACTED]")
    log.write_text(_SENSITIVE_LOG_TEXT.sub("[REDACTED]", log_text), encoding="utf-8")
    if completed.returncode not in expected:
        raise RuntimeError(
            f"command returned {completed.returncode}, expected {expected}; see {log}"
        )
    return completed


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_http(url: str, *, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except Exception as error:  # readiness retains the last concrete failure
            last = error
        time.sleep(0.1)
    raise RuntimeError(f"service did not become ready at {url}: {last}")


def _request_json(url: str, *, token: str | None = None) -> dict[str, Any]:
    headers = {"accept": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"GET {url} failed: {error.code} {error.read()!r}"
        ) from error
    if not isinstance(value, dict):
        raise RuntimeError(f"GET {url} did not return a JSON object")
    return value


def _post_json(
    url: str,
    document: dict[str, Any],
    *,
    token: str,
    expected: tuple[int, ...] = (200, 201),
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(document, separators=(",", ":")).encode(),
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    if status not in expected:
        raise RuntimeError(f"POST {url} failed: {status} {raw!r}")
    value = json.loads(raw or b"{}")
    if not isinstance(value, dict):
        raise RuntimeError(f"POST {url} did not return a JSON object")
    return status, value


def _exercise_agentd_redaction_and_withholding(
    home: Path, run_id: str, release_secret: str
) -> dict[str, Any]:
    endpoint = _read(home / "agentd" / "endpoint.json")["endpoint"]
    token = (home / "agentd" / "token").read_text(encoding="utf-8").strip()
    digest = hashlib.sha256(f"{run_id}:redaction-probe".encode()).hexdigest()
    legacy_trace = str(_read(home / "runs" / run_id / "manifest.json")["trace_id"])
    trace_id = hashlib.sha256(f"villani:v2:trace:{legacy_trace}".encode()).hexdigest()[
        :32
    ]
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    probe = {
        "schema_version": "villani.telemetry_envelope.v2",
        "event_id": f"evt2_{digest[:32]}",
        "idempotency_key": f"release:redaction:{digest}",
        "occurred_at": now,
        "observed_at": now,
        "sequence": 1,
        "sequence_scope": f"release:redaction:{run_id}",
        "organization_id": None,
        "workspace_id": None,
        "project_id": None,
        "repository_id": None,
        "run_id": run_id,
        "trace_id": trace_id,
        "span_id": digest[32:48],
        "parent_span_id": None,
        "attempt_id": None,
        "source": "villani-release-verification",
        "kind": "controller_stage",
        "name": "redaction_evidence_recorded",
        "status": "ok",
        "resource": {
            "schema_version": "villani.resource.v2",
            "service_name": "villani-release-verification",
            "service_version": None,
            "deployment_environment": "local",
            "host_id": None,
            "process_id": None,
            "attributes": {},
        },
        "attributes": {},
        "body": {
            "task_text": "authentication uses a harmless token and test-token fixture",
            "numeric_token_metric": 42,
            "input_tokens": 11,
            "registered_value": release_secret,
            "authorization": "Bearer release-bearer-secret-0001",
            "api_key": "sk-release-secret-0001",
        },
    }
    event_status, event_response = _post_json(
        f"{endpoint}/v1/events:batch", {"events": [probe]}, token=token
    )
    unsafe = f"secret={release_secret}".encode()
    unsafe_digest = hashlib.sha256(unsafe).hexdigest()
    descriptor = {
        "schema_version": "villani.artifact_descriptor.v2",
        "artifact_id": f"artifact_{digest[:24]}",
        "digest": {"algorithm": "sha256", "value": unsafe_digest},
        "size_bytes": len(unsafe),
        "media_type": "text/plain",
        "logical_role": "release.redaction_probe",
        "sensitivity": "internal",
        "retention_class": "run",
        "encryption_status": "unknown",
        "storage_reference": None,
        "provenance_status": "recorded",
        "attributes": {},
    }
    artifact_status, artifact_response = _post_json(
        f"{endpoint}/v1/artifacts/register",
        {
            "run_id": run_id,
            "descriptor": descriptor,
            "content_base64": base64.b64encode(unsafe).decode("ascii"),
        },
        token=token,
        expected=(422,),
    )
    return {
        "event_status": event_status,
        "event_inserted": event_response.get("inserted"),
        "artifact_status": artifact_status,
        "artifact_error": artifact_response.get("error"),
    }


def _terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


class PostgreSQLService:
    def __init__(self, *, env: dict[str, str], log: Path) -> None:
        self.env = env
        self.log = log
        self.container: str | None = None
        self.url = env.get("VILLANI_TEST_POSTGRES_URL", "")

    def start(self) -> str:
        if self.url:
            return self.url
        docker = shutil.which("docker")
        if not docker:
            raise RuntimeError(
                "PostgreSQL is required: set VILLANI_TEST_POSTGRES_URL or install Docker"
            )
        self.container = f"villani-release-postgres-{os.getpid()}"
        _run(
            [
                docker,
                "run",
                "--detach",
                "--rm",
                "--name",
                self.container,
                "--env",
                "POSTGRES_USER=villani",
                "--env",
                "POSTGRES_PASSWORD=villani",
                "--env",
                "POSTGRES_DB=villani",
                "--publish",
                "127.0.0.1::5432",
                "postgres:16-alpine",
            ],
            cwd=ROOT,
            env=self.env,
            log=self.log,
            timeout=180,
        )
        port_result = _run(
            [docker, "port", self.container, "5432/tcp"],
            cwd=ROOT,
            env=self.env,
            log=self.log.with_name("postgres-port.log"),
            timeout=30,
        )
        port = int(port_result.stdout.strip().rsplit(":", 1)[1])
        readiness_log: list[str] = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                [
                    docker,
                    "exec",
                    self.container,
                    "pg_isready",
                    "--username",
                    "villani",
                    "--dbname",
                    "villani",
                ],
                cwd=ROOT,
                env=self.env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=10,
            )
            readiness_log.append(ready.stdout + ready.stderr)
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(
                "PostgreSQL container did not become ready in 60 seconds"
            )
        self.log.with_name("postgres-readiness.log").write_text(
            "".join(readiness_log), encoding="utf-8"
        )
        self.url = f"postgresql+psycopg://villani:villani@127.0.0.1:{port}/villani"
        return self.url

    def stop(self) -> None:
        if self.container:
            docker = shutil.which("docker")
            if docker:
                subprocess.run(
                    [docker, "rm", "--force", self.container],
                    cwd=ROOT,
                    env=self.env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=30,
                )


def _entry(python: Path, name: str) -> Path:
    root = python.parent
    suffix = ".exe" if os.name == "nt" else ""
    value = root / f"{name}{suffix}"
    if not value.is_file():
        raise RuntimeError(f"packaged entry point is absent: {value}")
    return value


def _repository(root: Path, scenario: str, *, authentication: bool = False) -> Path:
    repo = root / scenario
    repo.mkdir(parents=True)
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    # failing baseline\n    return a - b\n",
        encoding="utf-8",
    )
    (repo / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import add\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    if authentication:
        (repo / "auth_fixture.py").write_text(
            "token = 'test-token'\n"
            "numeric_token_metric = 42\n"
            "bearer_example = 'Bearer release-example-placeholder'\n"
            "api_key_example = 'sk-release-example-placeholder'\n",
            encoding="utf-8",
        )
    base_env = os.environ.copy()
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "release@example.invalid"],
        ["git", "config", "user.name", "Villani Release Gate"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "failing baseline"],
    ):
        completed = subprocess.run(
            command,
            cwd=repo,
            env=base_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode:
            raise RuntimeError(completed.stdout + completed.stderr)
    return repo


def _backend(
    *,
    base_url: str,
    model: str,
    roles: list[str],
    capability: int,
    command: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "provider": "local",
        "base_url": base_url,
        "model": model,
        "roles": roles,
        "capability_score": capability,
        "capability_score_source": "release_fixture_contract",
        "billing_mode": "token",
        "input_cost_per_million": 1.0,
        "output_cost_per_million": 2.0,
        "api_key_env": "VILLANI_RELEASE_TEST_SECRET",
        "metadata": {"allow_dummy_api_key": True},
    }
    if command:
        value["command_name"] = command
    return value


def _configuration(
    original: dict[str, Any],
    *,
    base_url: str,
    villani_code: str,
    python: str,
    scenario: str,
) -> dict[str, Any]:
    config = json.loads(json.dumps(original))
    config.setdefault("budgets", {})["max_attempts"] = 3
    config["budgets"]["max_cost_usd"] = 10.0
    config["policy"] = {
        "version": "bootstrap_v1",
        "easy_min_capability": 20,
        "medium_min_capability": 50,
        "hard_min_capability": 80,
        "verifier_retry_limit": 0,
        "max_same_backend_retries": 0,
    }
    config["repository_validation_commands"] = [
        {
            "validation_id": "repository_unittest",
            "argv": [python, "-m", "unittest", "-q"],
            "timeout_seconds": 30,
        }
    ]
    config["backends"] = {
        "classifier": _backend(
            base_url=base_url,
            model="fixture-classifier",
            roles=["classification"],
            capability=100,
        ),
        "economy": _backend(
            base_url=base_url,
            model="fixture-economy",
            roles=["coding"],
            capability=20,
            command=villani_code,
        ),
        "standard": _backend(
            base_url=base_url,
            model="fixture-standard",
            roles=["coding"],
            capability=50,
            command=villani_code,
        ),
        "expert": _backend(
            base_url=base_url,
            model="fixture-expert",
            roles=["coding"],
            capability=90,
            command=villani_code,
        ),
        "verifier-low": _backend(
            base_url=base_url,
            model="fixture-verifier-low",
            roles=["review"],
            capability=60,
        ),
        "verifier-high": _backend(
            base_url=base_url,
            model="fixture-verifier-high",
            roles=["review"],
            capability=90,
        ),
    }
    config["verifier"] = {"no_llm": True, "timeout_seconds": 30}
    if scenario == "scenario_e":
        config["repository_validation_commands"] = []
        config["budgets"]["max_attempts"] = 1
    if scenario == "scenario_f":
        config["repository_validation_commands"] = []
        config["budgets"]["max_attempts"] = 1
        config["verifier"] = {
            "timeout_seconds": 30,
            "routes": [
                {
                    "backend": "verifier-low",
                    "capability_score": 60,
                    "price_per_call_usd": 0.00004,
                    "authority": "acceptance",
                },
                {
                    "backend": "verifier-high",
                    "capability_score": 90,
                    "price_per_call_usd": 0.00008,
                    "authority": "acceptance",
                },
            ],
            "policy": {
                "version": "release-verifier-routing-v1",
                "low_risk_minimum_capability": 20,
                "medium_risk_minimum_capability": 50,
                "high_risk_minimum_capability": 80,
            },
        }
    if scenario == "scenario_g":
        config["classification_policy"] = {
            "version": "release-classification-policy-v1",
            "risk_floor": "medium",
        }
    if scenario == "scenario_h":
        config["policy"]["accepted_candidates_required"] = 2
        config["budgets"]["max_attempts"] = 2
        config["candidate_reliability"] = {
            "strategy": "parallel_diverse_candidates",
            "stop_policy": "compare",
            "accepted_candidate_requirement": 2,
            "maximum_candidates": 2,
            "maximum_parallelism": 1,
            "candidates": [
                {"prompt_strategy_id": "direct", "seed": 7},
                {"prompt_strategy_id": "test_first", "seed": 99},
            ],
        }
    return config


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected object at {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    output: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError(f"expected JSON object records in {path}")
        output.append(value)
    return output


def _run_id(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("Run ID:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError(f"public CLI did not report a run ID:\n{output}")


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    run_id: str
    terminal_state: str
    run_directory: Path
    repository: Path
    passed: bool
    assertions: dict[str, Any]


def _scenario_assertions(
    scenario: str,
    run_dir: Path,
    repo: Path,
    *,
    initial_validation_exit_code: int,
    final_validation_exit_code: int,
    fixture_requests: list[dict[str, Any]],
    recovery_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = _read(run_dir / "manifest.json")
    state = _read(run_dir / "state.json")
    task = _read(run_dir / "task.json")
    classification = _read(run_dir / "classification.json")
    classification_metadata = classification.get("metadata") or {}
    selection = (
        _read(run_dir / "selection.json")
        if (run_dir / "selection.json").is_file()
        else {}
    )
    materialization = (
        _read(run_dir / "materialization.json")
        if (run_dir / "materialization.json").is_file()
        else {}
    )
    attempts = [
        _read(path) for path in sorted((run_dir / "attempts").glob("*/attempt.json"))
    ]
    verifications = {
        path.stem: _read(path)
        for path in sorted((run_dir / "verification").glob("attempt_*.json"))
    }
    events = _read_jsonl(run_dir / "events.jsonl")
    model_requests = [
        item for item in fixture_requests if item.get("scenario_id") == scenario
    ]
    models = [str(item.get("request_model") or "") for item in model_requests]

    def validation_events(attempt_id: str) -> list[dict[str, Any]]:
        return [
            event
            for event in events
            if event.get("attempt_id") == attempt_id
            and event.get("event_type") in {"command_completed", "command_failed"}
            and (event.get("payload") or {}).get("command_role")
            == "repository_validation"
        ]

    def write_events(attempt_id: str) -> list[dict[str, Any]]:
        return [
            event
            for event in events
            if event.get("attempt_id") == attempt_id
            and event.get("event_type") in {"file_write", "file_patch_applied"}
        ]

    assertions: dict[str, Any] = {
        "initial_validation_failed": initial_validation_exit_code != 0,
        "initial_attempt_id": bool(
            attempts and attempts[0]["attempt_id"] == "attempt_001"
        ),
        "manifest_attempt_count": len(manifest.get("attempt_ids") or [])
        == len(attempts),
        "canonical_attempt_ids": all(
            re.fullmatch(r"attempt_[0-9]{3}", str(item.get("attempt_id") or ""))
            for item in attempts
        ),
        "isolated_attempts": all(
            _read(run_dir / "attempts" / item["attempt_id"] / "worktree.json").get(
                "isolated"
            )
            is True
            for item in attempts
        ),
        "task_captured": bool(task.get("instruction")),
        "policy_captured": bool(
            (policy_records := _read_jsonl(run_dir / "policy_decisions.jsonl"))
            and all(
                str(item.get("policy_version") or "").strip() for item in policy_records
            )
        ),
    }
    if scenario == "scenario_a":
        verification = verifications.get("attempt_001") or {}
        structured = validation_events("attempt_001")
        assertions.update(
            completed=state["state"] == "COMPLETED",
            one_attempt=len(attempts) == 1,
            classified_easy_low=(
                (classification_metadata.get("raw_classification") or {}).get(
                    "difficulty"
                )
                == "easy"
                and (classification_metadata.get("raw_classification") or {}).get(
                    "risk"
                )
                == "low"
            ),
            economy_selected=bool(
                attempts and attempts[0].get("backend_name") == "economy"
            ),
            mutation_captured=bool(write_events("attempt_001")),
            validation_role_captured=bool(
                structured
                and structured[-1].get("event_type") == "command_completed"
                and (structured[-1].get("payload") or {}).get("exit_code") == 0
            ),
            repository_validation_authority=(
                (verification.get("metadata") or {}).get("authority_source")
                == "authoritative_repository_validation"
                and verification.get("acceptance_eligible") is True
            ),
            no_llm_verifier_request=not any(
                model.startswith("fixture-verifier-") for model in models
            ),
            no_llm_verifier_cost=all(
                int(item.get("model_calls") or 0) == 0 and item.get("cost") is None
                for item in verification.get("llm_usage") or []
            ),
            materialized=(repo / "calculator.py")
            .read_text(encoding="utf-8")
            .endswith("return a + b\n"),
            selected=selection.get("selected_candidate_ids") == ["attempt_001"],
            final_validation_passed=final_validation_exit_code == 0,
            structured_files=(
                attempts[0].get("metadata", {}).get("total_file_writes", 0) >= 1
                and attempts[0].get("metadata", {}).get("changed_files")
                == ["calculator.py"]
            ),
        )
    elif scenario == "scenario_b":
        first_verification = verifications.get("attempt_001") or {}
        second_verification = verifications.get("attempt_002") or {}
        first_validations = validation_events("attempt_001")
        second_validations = validation_events("attempt_002")
        assertions.update(
            completed=state["state"] == "COMPLETED",
            two_attempts=len(attempts) == 2,
            backends=[item["backend_name"] for item in attempts]
            == ["standard", "expert"],
            first_candidate_mutated=bool(write_events("attempt_001")),
            first_validation_failed=bool(
                first_validations
                and first_validations[-1].get("event_type") == "command_failed"
                and (first_validations[-1].get("payload") or {}).get("exit_code")
                not in {None, 0}
            ),
            first_rejected=(
                first_verification.get("acceptance_eligible") is False
                and first_verification.get("outcome") == "rejected"
            ),
            second_validation_passed=bool(
                second_validations
                and second_validations[-1].get("event_type") == "command_completed"
                and (second_validations[-1].get("payload") or {}).get("exit_code") == 0
            ),
            second_accepted=(
                second_verification.get("acceptance_eligible") is True
                and second_verification.get("outcome") == "accepted"
            ),
            selected=selection.get("selected_candidate_ids") == ["attempt_002"],
            escalation_count=(
                int(manifest.get("escalation_count") or 0) == 1
                or sum(
                    event.get("event_type") == "escalation_selected" for event in events
                )
                == 1
            ),
            only_expert_materialized=(
                materialization.get("selected_attempt_id") == "attempt_002"
                and materialization.get("patch_sha256")
                == attempts[1].get("patch_sha256")
                and materialization.get("patch_sha256")
                != attempts[0].get("patch_sha256")
            ),
            attempt_history_preserved=(
                (run_dir / "attempts" / "attempt_001" / "attempt.json").is_file()
                and (run_dir / "verification" / "attempt_001.json").is_file()
            ),
            materialized=(repo / "calculator.py")
            .read_text(encoding="utf-8")
            .endswith("return a + b\n"),
            final_validation_passed=final_validation_exit_code == 0,
        )
    elif scenario == "scenario_c":
        assertions.update(
            completed=state["state"] == "COMPLETED",
            one_attempt=len(attempts) == 1,
            public_attempt_id=bool(
                attempts and attempts[0].get("attempt_id") == "attempt_001"
            ),
            selected=selection.get("selected_candidate_ids") == ["attempt_001"],
            materialized=(repo / "calculator.py")
            .read_text(encoding="utf-8")
            .endswith("return a + b\n"),
            final_validation_passed=final_validation_exit_code == 0,
        )
    elif scenario == "scenario_d":
        assertions.update(
            completed=state["state"] == "COMPLETED",
            one_attempt=len(attempts) == 1,
            harmless_token_source_preserved=(
                "token = 'test-token'"
                in (repo / "auth_fixture.py").read_text(encoding="utf-8")
                and "numeric_token_metric = 42"
                in (repo / "auth_fixture.py").read_text(encoding="utf-8")
            ),
            selected=selection.get("selected_candidate_ids") == ["attempt_001"],
            materialized=(repo / "calculator.py")
            .read_text(encoding="utf-8")
            .endswith("return a + b\n"),
            final_validation_passed=final_validation_exit_code == 0,
        )
    elif scenario == "scenario_e":
        verification = verifications.get("attempt_001") or {}
        assertions.update(
            exhausted=state["state"] == "EXHAUSTED",
            not_materialized=not (run_dir / "materialization.json").is_file(),
            heuristic_advisory_positive=(
                (verification.get("metadata") or {}).get("raw_verdict") == "success"
                and (verification.get("metadata") or {}).get("authority_source")
                == "heuristic_only"
            ),
            candidate_ineligible=verification.get("acceptance_eligible") is False,
            authority_blocker=any(
                "non_authoritative_heuristic" in str(item)
                for item in verification.get("risk_flags") or []
            ),
            terminal_reason_explicit=bool(
                (state.get("metadata") or {}).get("terminal_reason")
                or (manifest.get("metadata") or {}).get("terminal_reason")
            ),
            repository_unchanged=(repo / "calculator.py")
            .read_text(encoding="utf-8")
            .endswith("return a - b\n"),
            final_validation_still_fails=final_validation_exit_code != 0,
        )
    elif scenario == "scenario_f":
        verification = _read(run_dir / "verification" / "attempt_001.json")
        calls = verification.get("metadata", {}).get("verifier_calls") or []
        stage_metrics = manifest.get("stage_metrics") or {}
        coding_cost = sum(
            float((stage_metrics.get(stage) or {}).get("cost") or 0)
            for stage in ("classification", "coding")
        )
        verifier_cost = sum(float(item.get("cost_usd") or 0) for item in calls)
        assertions.update(
            completed=state["state"] == "COMPLETED",
            two_verifiers=len(calls) == 2,
            cheapest_low_first=bool(
                calls
                and calls[0].get("backend") == "verifier-low"
                and calls[0].get("selection_reason") == "cheapest_eligible"
            ),
            malformed_low=bool(
                calls
                and calls[0].get("malformed_output")
                and calls[0].get("invocation_status") == "malformed_output"
            ),
            high_selected=bool(
                len(calls) == 2 and calls[1].get("backend") == "verifier-high"
            ),
            escalation_reason=bool(
                calls and calls[0].get("escalation_reason") == "malformed_output"
            ),
            stronger_valid_result=bool(
                len(calls) == 2
                and calls[1].get("outcome") == "accepted"
                and calls[1].get("authority") == "acceptance"
            ),
            billing_exact=(
                len(verification.get("llm_usage") or []) == 3
                and abs(
                    sum(
                        float(item.get("cost") or 0)
                        for item in verification.get("llm_usage") or []
                    )
                    - sum(float(item.get("cost_usd") or 0) for item in calls)
                )
                < 1e-12
            ),
            total_cost_exact_once=abs(
                float(manifest.get("total_cost_usd") or 0) - coding_cost - verifier_cost
            )
            < 1e-12,
            recovery_did_not_rebill=bool(
                recovery_evidence
                and recovery_evidence.get("manifest_unchanged")
                and recovery_evidence.get("verifier_usage_unchanged")
            ),
            final_validation_passed=final_validation_exit_code == 0,
        )
    elif scenario == "scenario_g":
        raw = classification_metadata.get("raw_classification") or {}
        effective = classification_metadata.get("effective_classification") or {}
        adjustments = classification_metadata.get("classification_adjustments") or []
        adjustment = adjustments[0] if len(adjustments) == 1 else {}
        assertions.update(
            completed=state["state"] == "COMPLETED",
            raw_immutable=(raw.get("difficulty"), raw.get("risk")) == ("easy", "low"),
            effective_derived=(effective.get("difficulty"), effective.get("risk"))
            == ("easy", "medium"),
            adjustment_complete=(
                adjustment.get("field") == "risk"
                and adjustment.get("before") == "low"
                and adjustment.get("after") == "medium"
                and adjustment.get("rule_id") == "risk_floor.v1"
                and bool(adjustment.get("reason"))
                and adjustment.get("policy_version")
                == "release-classification-policy-v1"
                and adjustment.get("authority") == "configured_policy"
                and bool(adjustment.get("timestamp"))
            ),
            routing_used_effective=bool(
                attempts
                and attempts[0].get("backend_name") == "standard"
                and attempts[0].get("model") == "fixture-standard"
            ),
            final_validation_passed=final_validation_exit_code == 0,
        )
    elif scenario == "scenario_h":
        configs = [
            item.get("metadata", {}).get("effective_candidate_configuration") or {}
            for item in attempts
        ]
        digests = {
            item.get("effective_configuration_digest")
            for item in configs
            if item.get("runner_acknowledged")
        }
        from villani_ops.closed_loop.candidate_strategies import (
            acknowledged_diversity_summary,
        )

        synthetic_unacknowledged = {
            "metadata": {
                "runner_acknowledged_candidate_configuration": False,
                "effective_configuration_sha256": "f" * 64,
            }
        }
        counted, counted_distinct = acknowledged_diversity_summary(
            [*attempts, synthetic_unacknowledged]
        )
        accounting = _read(run_dir / "reliability_accounting.json")
        assertions.update(
            completed=state["state"] == "COMPLETED",
            two_attempts=len(attempts) == 2,
            acknowledged=all(item.get("runner_acknowledged") for item in configs),
            requested_dimensions=all(
                item.get("requested_dimensions") for item in configs
            ),
            applied_dimensions=all(item.get("applied_dimensions") for item in configs),
            unsupported_seeds=all(
                "seed" in (item.get("unsupported_dimensions") or {}) for item in configs
            ),
            unsupported_seed_not_applied=all(
                "seed" not in (item.get("applied_dimensions") or {}) for item in configs
            ),
            provider_status_explicit=all(
                (item.get("provider_acknowledgement") or {}).get("status")
                == "not_reported"
                for item in configs
            ),
            prompt_digests_differ=len(
                {item.get("rendered_prompt_digest") for item in configs}
            )
            == 2,
            distinct_configurations=len(digests) == 2,
            accounting_counted_two=(
                accounting.get("distinct_effective_configurations") == 2
                and accounting.get("diversity_claimed") is True
            ),
            unacknowledged_not_counted=counted and counted_distinct == 2,
            recovery_preserved_configuration=bool(
                recovery_evidence
                and recovery_evidence.get("manifest_unchanged")
                and recovery_evidence.get("candidate_configurations_unchanged")
            ),
            selected_from_eligible=bool(
                selection.get("selected_candidate_ids")
                and selection.get("selected_candidate_ids")[0]
                in (selection.get("eligible_candidate_ids") or [])
            ),
            final_validation_passed=final_validation_exit_code == 0,
        )
    return assertions


def _local_snapshot(run_dir: Path) -> dict[str, Any]:
    return canonical_local_snapshot(run_dir)


def _reconcile(local: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    return reconcile_sources(
        {"local_bundle": local, "control_plane_api": canonical_api_snapshot(remote)}
    )


def _api_assertions(
    scenario: ScenarioResult,
    remote: dict[str, Any],
    events_page: dict[str, Any],
    artifacts_page: dict[str, Any],
    *,
    release_secret: str,
) -> dict[str, Any]:
    """Prove the synchronized public representation without private scoped IDs."""

    candidates = remote.get("candidate_outcomes") or {}
    attempts = remote.get("attempts") or []
    events = events_page.get("events") or []
    artifacts = artifacts_page.get("artifacts") or []
    attempt_ids = [str(item.get("id") or "") for item in attempts]
    candidate_ids = [str(item) for item in candidates]
    serialized = json.dumps(
        {"run": remote, "events": events_page, "artifacts": artifacts_page},
        sort_keys=True,
    )
    assertions: dict[str, Any] = {
        "api_run_visible": remote.get("id") == scenario.run_id,
        "api_attempt_count": int(remote.get("attempt_count") or 0) == len(attempts),
        "public_attempt_ids_canonical": bool(attempt_ids)
        and all(re.fullmatch(r"attempt_[0-9]{3}", value) for value in attempt_ids),
        "candidate_cards_deduplicated": (
            len(attempt_ids) == len(set(attempt_ids))
            and len(candidate_ids) == len(set(candidate_ids))
            and set(attempt_ids) == set(candidate_ids)
        ),
        "no_internal_attempt_identity": not any(
            separator in value
            for value in [*attempt_ids, *candidate_ids]
            for separator in ("::", "/", "|")
        ),
        "task_and_policy_synchronized": bool(
            remote.get("task_instruction") and remote.get("policy_version")
        ),
        "event_stream_synchronized": bool(events),
        "safe_artifact_synchronized": bool(
            artifacts and all(item.get("status") == "available" for item in artifacts)
        ),
        "registered_secret_absent_from_api": release_secret not in serialized,
    }
    if scenario.scenario_id == "scenario_a":
        candidate = candidates.get("attempt_001") or {}
        verification = candidate.get("verification") or {}
        assertions.update(
            api_selected_attempt=remote.get("selected_attempt_id") == "attempt_001",
            api_economy_route=(
                remote.get("selected_backend") == "economy"
                and remote.get("selected_model") == "fixture-economy"
            ),
            api_accounting_complete=(
                int(remote.get("total_tokens") or 0) > 0
                and float(remote.get("total_cost_usd") or 0) > 0
                and remote.get("cost_accounting_status") == "complete"
            ),
            api_authority=(
                remote.get("verification_authority")
                == "authoritative_repository_validation"
                and verification.get("acceptance_eligible") is True
            ),
            api_file_activity=(
                int(remote.get("file_write_count") or 0) >= 1
                and remote.get("changed_files") == ["calculator.py"]
                and int(candidate.get("file_write_count") or 0) >= 1
                and candidate.get("changed_files") == ["calculator.py"]
            ),
            api_no_verifier_model=not any(
                (item.get("model") or "").startswith("fixture-verifier-")
                for item in (verification.get("llm_usage") or [])
            ),
        )
    elif scenario.scenario_id == "scenario_b":
        assertions.update(
            api_two_attempts=len(attempts) == 2,
            api_first_rejected=(
                (candidates.get("attempt_001") or {}).get("candidate_eligibility")
                is False
            ),
            api_expert_selected=(
                remote.get("selected_attempt_id") == "attempt_002"
                and remote.get("selected_backend") == "expert"
                and (candidates.get("attempt_002") or {}).get("candidate_eligibility")
                is True
            ),
            api_escalation_count=int(remote.get("escalation_count") or 0) == 1,
        )
    elif scenario.scenario_id == "scenario_d":
        assertions.update(
            api_harmless_token_visible=(
                "harmless token" in str(remote.get("task_instruction") or "")
                and "test-token" in str(remote.get("task_instruction") or "")
            ),
            api_redaction_notice=(
                remote.get("redaction_applied") is True
                and int(remote.get("redacted_field_count") or 0) >= 3
                and {"registered_secret", "sensitive_field"}
                <= set(remote.get("redaction_categories") or [])
            ),
            api_withholding_notice=(
                int(remote.get("withheld_artifact_count") or 0) >= 1
                and bool(remote.get("withheld_artifact_categories"))
            ),
            unsafe_artifact_not_available=all(
                item.get("logical_role") != "release.redaction_probe"
                for item in artifacts
            ),
        )
    elif scenario.scenario_id == "scenario_e":
        verification = (candidates.get("attempt_001") or {}).get("verification") or {}
        assertions.update(
            api_exhausted=str(remote.get("status") or "").upper() == "EXHAUSTED",
            api_heuristic_ineligible=(
                verification.get("acceptance_eligible") is False
                and (verification.get("metadata") or {}).get("authority_source")
                == "heuristic_only"
            ),
            api_not_materialized=(
                remote.get("materialization_status") == "not_materialized"
            ),
            api_terminal_reason=bool(remote.get("terminal_reason")),
        )
    elif scenario.scenario_id == "scenario_f":
        verification = (candidates.get("attempt_001") or {}).get("verification") or {}
        calls = (verification.get("metadata") or {}).get("verifier_calls") or []
        assertions.update(
            api_verifier_cascade=(
                len(calls) == 2
                and calls[0].get("backend") == "verifier-low"
                and calls[0].get("malformed_output") is True
                and calls[1].get("backend") == "verifier-high"
                and calls[1].get("outcome") == "accepted"
            ),
            api_verifier_billing_exact=abs(
                float(remote.get("total_cost_usd") or 0)
                - float(remote.get("coding_cost_usd") or 0)
                - float(remote.get("verifier_cost_usd") or 0)
            )
            < 1e-12,
        )
    elif scenario.scenario_id == "scenario_g":
        adjustments = remote.get("classification_adjustments") or []
        assertions.update(
            api_raw_classification=(
                (remote.get("raw_classification") or {}).get("risk") == "low"
            ),
            api_effective_classification=(
                (remote.get("effective_classification") or {}).get("risk") == "medium"
            ),
            api_adjustment_reason=bool(
                len(adjustments) == 1 and adjustments[0].get("reason")
            ),
            api_routing_used_effective=remote.get("selected_backend") == "standard",
        )
    elif scenario.scenario_id == "scenario_h":
        candidate_values = list(candidates.values())
        configurations = [
            value.get("candidate_configuration") or {} for value in candidate_values
        ]
        assertions.update(
            api_two_acknowledged_configurations=(
                len(configurations) == 2
                and all(item.get("runner_acknowledged") for item in configurations)
                and len(
                    {
                        item.get("effective_configuration_sha256")
                        for item in candidate_values
                    }
                )
                == 2
            ),
            api_unsupported_seed_visible=all(
                "seed" in (item.get("unsupported_dimensions") or {})
                for item in configurations
            ),
        )
    return assertions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--mode", choices=("local", "ci", "release"), required=True)
    args = parser.parse_args(argv)
    python = args.python.resolve()
    work = args.work.resolve()
    artifacts = args.artifacts.resolve()
    logs = artifacts / "logs"
    home = work / "home"
    repositories = work / "repositories"
    repositories.mkdir(parents=True, exist_ok=True)
    release_secret = "release-canary-" + secrets.token_urlsafe(32)
    api_token = "release-api-" + secrets.token_urlsafe(32)
    enrollment_token = "release-enroll-" + secrets.token_urlsafe(32)
    env = os.environ.copy()
    env.update(
        {
            "VILLANI_HOME": str(home),
            "VILLANI_CODE_INLINE_PROMPT_LIMIT": "1",
            "VILLANI_RELEASE_TEST_SECRET": release_secret,
            "VILLANI_REGISTERED_SECRET_ENV_VARS": "VILLANI_RELEASE_TEST_SECRET",
            "VILLANI_RELEASE_API_TOKEN": api_token,
            "VILLANI_RELEASE_ENROLLMENT_TOKEN": enrollment_token,
            "PYTHONUTF8": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(name, None)
    scripts = python.parent
    env["PATH"] = str(scripts) + os.pathsep + env.get("PATH", "")
    villani = _entry(python, "villani")
    villani_code = _entry(python, "villani-code")
    agentd = _entry(python, "villani-agentd")
    fixture: subprocess.Popen[Any] | None = None
    control_plane: subprocess.Popen[Any] | None = None
    browser_server: subprocess.Popen[Any] | None = None
    postgres = PostgreSQLService(env=env, log=logs / "postgres-start.log")
    scenarios: list[ScenarioResult] = []
    try:
        database_url = postgres.start()
        migration_env = {**env, "VILLANI_CONTROL_PLANE_DATABASE_URL": database_url}
        _run(
            [str(python), "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=ROOT / "components" / "villani-control-plane",
            env=migration_env,
            log=logs / "alembic-upgrade.log",
        )
        heads = _run(
            [str(python), "-m", "alembic", "-c", "alembic.ini", "heads"],
            cwd=ROOT / "components" / "villani-control-plane",
            env=migration_env,
            log=logs / "alembic-heads.log",
        )
        if ALEMBIC_HEAD not in heads.stdout:
            raise RuntimeError(f"unexpected Alembic head: {heads.stdout.strip()}")
        migration_summary_path = artifacts / "postgres-migration-summary.json"
        _run(
            [
                str(python),
                str(ROOT / "release-verification" / "postgres_migration_proof.py"),
                "--database-url",
                database_url,
                "--output",
                str(migration_summary_path),
            ],
            cwd=ROOT,
            env=migration_env,
            log=logs / "postgres-populated-migration-proof.log",
            timeout=300,
        )
        migration_summary = _read(migration_summary_path)
        migration_summary["fresh_database_upgrade"] = "passed"
        if migration_summary.get("status") != "passed":
            raise RuntimeError(
                "populated pre-composite PostgreSQL migration proof failed"
            )
        _write_json(migration_summary_path, migration_summary)

        cp_port = _free_port()
        cp_env = {
            **migration_env,
            "VILLANI_CONTROL_PLANE_DEV_API_TOKEN": api_token,
            "VILLANI_CONTROL_PLANE_DEV_ENROLLMENT_TOKEN": enrollment_token,
            "VILLANI_CONTROL_PLANE_EXPECTED_MIGRATION": ALEMBIC_HEAD,
            "VILLANI_CONTROL_PLANE_OBJECT_STORE_PATH": str(work / "object-store"),
            "VILLANI_CONTROL_PLANE_SECURE_COOKIES": "false",
        }
        cp_log = (logs / "control-plane.log").open("w", encoding="utf-8")
        control_plane = subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                "villani_control_plane.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cp_port),
            ],
            cwd=work,
            env=cp_env,
            stdout=cp_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        cp_log.close()
        control_plane_url = f"http://127.0.0.1:{cp_port}"
        _wait_http(control_plane_url + "/health")

        endpoint_file = work / "fixture-endpoint.json"
        fixture_log = (logs / "fixture-service.log").open("w", encoding="utf-8")
        fixture = subprocess.Popen(
            [
                str(python),
                str(ROOT / "release-verification" / "fixtures" / "model_service.py"),
                "--log",
                str(artifacts / "fixture-model-requests.jsonl"),
                "--endpoint-file",
                str(endpoint_file),
            ],
            cwd=work,
            env=env,
            stdout=fixture_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        fixture_log.close()
        deadline = time.monotonic() + 20
        while not endpoint_file.is_file() and time.monotonic() < deadline:
            if fixture.poll() is not None:
                raise RuntimeError("fixture service exited during startup")
            time.sleep(0.05)
        fixture_base = _read(endpoint_file)["base_url"]
        _wait_http(str(fixture_base).rsplit("/v1", 1)[0] + "/health")

        _run([str(villani), "init"], cwd=work, env=env, log=logs / "villani-init.log")
        _run([str(agentd), "start"], cwd=work, env=env, log=logs / "agentd-start.log")
        _run(
            [
                str(agentd),
                "enroll",
                "--control-plane",
                control_plane_url,
                "--token",
                enrollment_token,
                "--installation-id",
                "release-installation-01",
            ],
            cwd=work,
            env=env,
            log=logs / "agentd-enroll.log",
        )
        original = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        if not isinstance(original, dict):
            raise RuntimeError("villani init produced an invalid configuration")

        security_probe: dict[str, Any] = {}
        fixture_request_log = artifacts / "fixture-model-requests.jsonl"
        for scenario in (
            "scenario_a",
            "scenario_b",
            "scenario_c",
            "scenario_d",
            "scenario_e",
            "scenario_f",
            "scenario_g",
            "scenario_h",
        ):
            repo = _repository(
                repositories, scenario, authentication=scenario == "scenario_d"
            )
            config = _configuration(
                original,
                base_url=str(fixture_base),
                villani_code=str(villani_code),
                python=str(python),
                scenario=scenario,
            )
            (home / "config.yaml").write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )
            task = (
                f"Release scenario: {scenario}. Fix calculator addition."
                if scenario != "scenario_d"
                else (
                    "Release scenario: scenario_d. Fix calculator addition while preserving "
                    "the harmless token and test-token authentication fixtures. Registered "
                    "secrets and unsafe authentication artifacts must never be disclosed."
                )
            )
            initial_validation = _run(
                [str(python), "-m", "unittest", "-q"],
                cwd=repo,
                env=env,
                log=logs / f"{scenario}-initial-validation.log",
                expected=(1,),
                timeout=60,
            )
            expected = (3,) if scenario == "scenario_e" else (0,)
            result = _run(
                [
                    str(villani),
                    "run",
                    task,
                    "--repo",
                    str(repo),
                    "--success-criteria",
                    "python -m unittest -q passes",
                    "--max-attempts",
                    str(config["budgets"]["max_attempts"]),
                ],
                cwd=work,
                env=env,
                log=logs / f"{scenario}-cli.log",
                expected=expected,
                timeout=300,
            )
            output = result.stdout + result.stderr
            run_id = _run_id(output)
            run_dir = home / "runs" / run_id
            if scenario == "scenario_d":
                security_probe = _exercise_agentd_redaction_and_withholding(
                    home, run_id, release_secret
                )
            recovery_evidence: dict[str, Any] | None = None
            if scenario in {"scenario_f", "scenario_h"}:
                manifest_before_resume = _read(run_dir / "manifest.json")
                verification_before_resume = (
                    _read(run_dir / "verification" / "attempt_001.json")
                    if scenario == "scenario_f"
                    else {}
                )
                candidate_configurations_before_resume = [
                    (_read(path).get("metadata") or {}).get(
                        "effective_candidate_configuration"
                    )
                    for path in sorted((run_dir / "attempts").glob("*/attempt.json"))
                ]
                _run(
                    [str(villani), "resume", run_id],
                    cwd=work,
                    env=env,
                    log=logs / f"{scenario}-terminal-resume.log",
                    timeout=60,
                )
                manifest_after_resume = _read(run_dir / "manifest.json")
                verification_after_resume = (
                    _read(run_dir / "verification" / "attempt_001.json")
                    if scenario == "scenario_f"
                    else {}
                )
                candidate_configurations_after_resume = [
                    (_read(path).get("metadata") or {}).get(
                        "effective_candidate_configuration"
                    )
                    for path in sorted((run_dir / "attempts").glob("*/attempt.json"))
                ]
                recovery_evidence = {
                    "manifest_unchanged": manifest_before_resume
                    == manifest_after_resume,
                    "verifier_usage_unchanged": (
                        verification_before_resume.get("llm_usage")
                        == verification_after_resume.get("llm_usage")
                        and (verification_before_resume.get("metadata") or {}).get(
                            "verifier_calls"
                        )
                        == (verification_after_resume.get("metadata") or {}).get(
                            "verifier_calls"
                        )
                    ),
                    "candidate_configurations_unchanged": (
                        candidate_configurations_before_resume
                        == candidate_configurations_after_resume
                    ),
                }
            final_validation = _run(
                [str(python), "-m", "unittest", "-q"],
                cwd=repo,
                env=env,
                log=logs / f"{scenario}-final-validation.log",
                expected=(1,) if scenario == "scenario_e" else (0,),
                timeout=60,
            )
            assertions = _scenario_assertions(
                scenario,
                run_dir,
                repo,
                initial_validation_exit_code=initial_validation.returncode,
                final_validation_exit_code=final_validation.returncode,
                fixture_requests=_read_jsonl(fixture_request_log),
                recovery_evidence=recovery_evidence,
            )
            passed = all(bool(value) for value in assertions.values())
            scenarios.append(
                ScenarioResult(
                    scenario,
                    run_id,
                    _read(run_dir / "state.json")["state"],
                    run_dir,
                    repo,
                    passed,
                    assertions,
                )
            )
            _write_json(
                artifacts / "canonical-run-snapshots" / f"{scenario}-{run_id}.json",
                _local_snapshot(run_dir),
            )

        spool = home / "agentd" / "spool.sqlite3"
        with sqlite3.connect(spool) as connection:
            replay_rows = connection.execute(
                """SELECT event_id,payload_json FROM events
                   WHERE upload_state IN ('offline','retry')
                   ORDER BY sequence_scope,sequence,event_id LIMIT 250"""
            ).fetchall()
            spool_events_by_run: dict[str, list[dict[str, Any]]] = {}
            for run_id_value, payload_json in connection.execute(
                """SELECT run_id,payload_json FROM events
                   ORDER BY sequence_scope,sequence,event_id"""
            ).fetchall():
                spool_events_by_run.setdefault(str(run_id_value), []).append(
                    json.loads(str(payload_json))
                )
        replay_batch_document: dict[str, Any] | None = None
        if replay_rows:
            replay_ids = [str(row[0]) for row in replay_rows]
            replay_batch_document = {
                "batch_id": "agentd:"
                + hashlib.sha256("\n".join(replay_ids).encode()).hexdigest(),
                "events": [json.loads(str(row[1])) for row in replay_rows],
            }
        sync_history: list[dict[str, Any]] = []
        # The packaged fixture produces up to six safe canonical artifacts per
        # run.  Agentd intentionally uploads only ``concurrency`` artifacts per
        # pass, so the bound must cover the maximum deterministic backlog as
        # well as event batches.  Sixty-four passes remains finite and leaves
        # ample headroom for the eight-scenario gate at the default concurrency.
        for sync_ordinal in range(1, 65):
            sync = _run(
                [str(agentd), "sync-once"],
                cwd=work,
                env=env,
                log=logs / f"agentd-sync-{sync_ordinal:02d}.log",
                timeout=300,
            )
            sync_result = json.loads(sync.stdout)
            with sqlite3.connect(spool) as connection:
                pending_events = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM events WHERE upload_state IN ('offline','retry')"
                    ).fetchone()[0]
                )
                pending_outcomes = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM runs
                           WHERE final_payload_json IS NOT NULL
                             AND upload_state IN ('offline','retry')"""
                    ).fetchone()[0]
                )
                pending_artifacts = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM artifacts
                           WHERE upload_state IN ('offline','retry')"""
                    ).fetchone()[0]
                )
            sync_history.append(
                {
                    "ordinal": sync_ordinal,
                    "result": sync_result,
                    "pending_events": pending_events,
                    "pending_outcomes": pending_outcomes,
                    "pending_artifacts": pending_artifacts,
                }
            )
            if pending_events == 0 and pending_outcomes == 0 and pending_artifacts == 0:
                break
            if not any(
                int(sync_result.get(key) or 0)
                for key in ("events", "outcomes", "artifacts")
            ):
                time.sleep(0.5)
        else:
            raise RuntimeError(
                "Agentd synchronization did not drain in 64 bounded iterations"
            )

        idempotency_sync = _run(
            [str(agentd), "sync-once"],
            cwd=work,
            env=env,
            log=logs / "agentd-sync-idempotency.log",
            timeout=300,
        )
        idempotency_sync_result = json.loads(idempotency_sync.stdout)
        replay_responses: list[dict[str, Any]] = []
        if replay_batch_document is not None:
            for _ in range(2):
                _, replay_response = _post_json(
                    f"{control_plane_url}/v1/ingest/batches",
                    replay_batch_document,
                    token=api_token,
                )
                replay_responses.append(replay_response)
        replay_idempotency_passed = (
            bool(
                len(replay_responses) == 2
                and all(item.get("replayed") is True for item in replay_responses)
                and all(
                    int(item.get("inserted") or 0) == 0 for item in replay_responses
                )
                and all(
                    int(item.get("duplicates") or 0)
                    == len(replay_batch_document["events"])
                    for item in replay_responses
                )
            )
            if replay_batch_document is not None
            else False
        )

        with sqlite3.connect(spool) as connection:
            synchronized = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE upload_state='acknowledged'"
                ).fetchone()[0]
            )
            dead_letters = sum(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE upload_state='dead_letter'"
                    ).fetchone()[0]
                )
                for table in ("events", "runs", "artifacts")
            )
            repeated = connection.execute(
                "SELECT run_id, final_payload_json FROM runs ORDER BY run_id"
            ).fetchall()
            spool_outcomes_by_run = {
                str(run_id_value): (
                    json.loads(str(payload_json)).get("outcome")
                    if payload_json
                    else None
                )
                for run_id_value, payload_json in repeated
            }
            spool_diagnostics = {
                "runs": [
                    dict(
                        zip(
                            ("run_id", "upload_state", "retry_count", "last_error"), row
                        )
                    )
                    for row in connection.execute(
                        "SELECT run_id,upload_state,retry_count,last_error FROM runs ORDER BY run_id"
                    ).fetchall()
                ],
                "events": [
                    dict(zip(("run_id", "upload_state", "count", "last_error"), row))
                    for row in connection.execute(
                        """SELECT run_id,upload_state,COUNT(*),MAX(last_error)
                           FROM events GROUP BY run_id,upload_state ORDER BY run_id,upload_state"""
                    ).fetchall()
                ],
                "artifacts": [
                    dict(zip(("run_id", "upload_state", "count", "last_error"), row))
                    for row in connection.execute(
                        """SELECT run_id,upload_state,COUNT(*),MAX(last_error)
                           FROM artifacts GROUP BY run_id,upload_state ORDER BY run_id,upload_state"""
                    ).fetchall()
                ],
            }
        spool_snapshots = {
            item.run_id: project_spool_events(
                spool_events_by_run.get(item.run_id, []),
                spool_outcomes_by_run.get(item.run_id),
            )
            for item in scenarios
        }
        _write_json(
            artifacts / "agentd-spool-summary.json",
            {
                "sync_history": sync_history,
                "idempotency_sync": idempotency_sync_result,
                "replayed_batch": replay_batch_document,
                "replay_responses": replay_responses,
                "replay_idempotency_passed": replay_idempotency_passed,
                "canonical_snapshots": spool_snapshots,
                **spool_diagnostics,
            },
        )
        api_snapshots: dict[str, dict[str, Any]] = {}
        api_event_snapshots: dict[str, dict[str, Any]] = {}
        api_artifact_snapshots: dict[str, dict[str, Any]] = {}
        synchronized_scenarios: list[ScenarioResult] = []
        for scenario in scenarios:
            remote = _request_json(
                f"{control_plane_url}/v1/runs/{scenario.run_id}", token=api_token
            )
            remote_events = _request_json(
                f"{control_plane_url}/v1/runs/{scenario.run_id}/events?limit=1000",
                token=api_token,
            )
            remote_artifacts = _request_json(
                f"{control_plane_url}/v1/runs/{scenario.run_id}/artifacts?limit=250",
                token=api_token,
            )
            api_snapshots[scenario.scenario_id] = remote
            api_event_snapshots[scenario.scenario_id] = remote_events
            api_artifact_snapshots[scenario.scenario_id] = remote_artifacts
            _write_json(
                artifacts
                / "control-plane-api-snapshots"
                / f"{scenario.scenario_id}-{scenario.run_id}.json",
                remote,
            )
            _write_json(
                artifacts
                / "control-plane-api-snapshots"
                / f"{scenario.scenario_id}-{scenario.run_id}-events.json",
                remote_events,
            )
            _write_json(
                artifacts
                / "control-plane-api-snapshots"
                / f"{scenario.scenario_id}-{scenario.run_id}-artifacts.json",
                remote_artifacts,
            )
            merged_assertions = {
                **scenario.assertions,
                **_api_assertions(
                    scenario,
                    remote,
                    remote_events,
                    remote_artifacts,
                    release_secret=release_secret,
                ),
            }
            synchronized_scenarios.append(
                ScenarioResult(
                    scenario.scenario_id,
                    scenario.run_id,
                    scenario.terminal_state,
                    scenario.run_directory,
                    scenario.repository,
                    all(bool(value) for value in merged_assertions.values()),
                    merged_assertions,
                )
            )
        scenarios = synchronized_scenarios
        database_by_run = database_snapshots(
            database_url, (item.run_id for item in scenarios)
        )
        database_secret_counts = database_secret_occurrences(
            database_url, release_secret
        )
        ui_model_input = work / "ui-model-input.json"
        ui_model_output = work / "ui-model-output.json"
        _write_json(ui_model_input, api_snapshots)
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node is required for connected UI model reconciliation")
        _run(
            [
                node,
                str(ROOT / "release-verification" / "derive_ui_models.mjs"),
                str(ui_model_input),
                str(ui_model_output),
            ],
            cwd=ROOT,
            env=env,
            log=logs / "ui-model-reconciliation.log",
            timeout=120,
        )
        ui_models = _read(ui_model_output)
        reconciliations: dict[str, Any] = {}
        for scenario in scenarios:
            models = ui_models.get(scenario.scenario_id) or {}
            reconciliations[scenario.scenario_id] = reconcile_sources(
                {
                    "local_bundle": _local_snapshot(scenario.run_directory),
                    "agentd_spool": spool_snapshots[scenario.run_id],
                    "control_plane_database": database_by_run[scenario.run_id],
                    "control_plane_api": canonical_api_snapshot(
                        api_snapshots[scenario.scenario_id]
                    ),
                    "villani_web": models.get("web") or {},
                    "flight_recorder": models.get("flight_recorder") or {},
                }
            )
        repeated_attempt_ids = [
            json.loads(payload)["outcome"].get("attempt_id")
            for _run_id_value, payload in repeated
            if payload
        ]
        repeated_runs = [
            api_snapshots[scenario_id] for scenario_id in ("scenario_a", "scenario_c")
        ]
        repeated_attempt_details = {
            "distinct_runs": len({item.get("id") for item in repeated_runs}) == 2,
            "canonical_ids": all(
                [attempt.get("id") for attempt in item.get("attempts") or []]
                == ["attempt_001"]
                for item in repeated_runs
            ),
            "no_duplicate_candidates": all(
                list((item.get("candidate_outcomes") or {}).keys()) == ["attempt_001"]
                for item in repeated_runs
            ),
            "spool_outcomes_preserved": repeated_attempt_ids.count("attempt_001") >= 2,
            "batch_replay_idempotent": replay_idempotency_passed,
            "no_dead_letters": dead_letters == 0,
        }
        repeated_attempt_proof = all(repeated_attempt_details.values())
        reconciliation_passed = all(item["passed"] for item in reconciliations.values())
        scenario_documents = [
            {
                "scenario_id": item.scenario_id,
                "run_id": item.run_id,
                "terminal_state": item.terminal_state,
                "passed": item.passed,
                "assertions": item.assertions,
            }
            for item in scenarios
        ]
        connected = {
            "status": "passed"
            if all(item.passed for item in scenarios)
            and synchronized == len(scenarios)
            and not dead_letters
            and reconciliation_passed
            and repeated_attempt_proof
            and all(
                int(idempotency_sync_result.get(key) or 0) == 0
                for key in ("events", "outcomes", "artifacts")
            )
            else "failed",
            "scenario_count": len(scenarios),
            "passed_scenarios": sum(item.passed for item in scenarios),
            "synchronized_run_count": synchronized,
            "completed_run_count": sum(
                item.terminal_state == "COMPLETED" for item in scenarios
            ),
            "exhausted_run_count": sum(
                item.terminal_state == "EXHAUSTED" for item in scenarios
            ),
            "dead_letter_count": dead_letters,
            "redacted_field_count": sum(
                int(item.get("redacted_field_count") or 0)
                for item in api_snapshots.values()
            ),
            "withheld_artifact_count": sum(
                int(item.get("withheld_artifact_count") or 0)
                for item in api_snapshots.values()
            ),
            "repeated_attempt_001": repeated_attempt_proof,
            "repeated_attempt_001_details": repeated_attempt_details,
            "batch_replay_idempotent": replay_idempotency_passed,
            "sync_history": sync_history,
            "idempotent_sync_result": idempotency_sync_result,
            "scenarios": scenario_documents,
            "run_ids": {item.scenario_id: item.run_id for item in scenarios},
            "control_plane_url": control_plane_url,
            "home": str(home),
        }
        _write_json(artifacts / "connected-product-summary.json", connected)
        _write_json(
            artifacts / "canonical-reconciliation.json",
            {
                "status": "passed" if reconciliation_passed else "failed",
                "runs": reconciliations,
            },
        )
        _write_json(
            artifacts / "dead-letter-summary.json",
            {
                "status": "passed" if dead_letters == 0 else "failed",
                "count": dead_letters,
            },
        )
        verifier_scenario = next(
            item for item in scenario_documents if item["scenario_id"] == "scenario_f"
        )
        verifier_api = api_snapshots["scenario_f"]
        verifier_candidate = (verifier_api.get("candidate_outcomes") or {}).get(
            "attempt_001"
        ) or {}
        verifier_result = verifier_candidate.get("verification") or {}
        verifier_calls = (verifier_result.get("metadata") or {}).get(
            "verifier_calls"
        ) or []
        verifier_checks = {
            "cheapest_eligible_first": bool(
                verifier_calls
                and verifier_calls[0].get("selection_reason") == "cheapest_eligible"
            ),
            "malformed_output_recorded": bool(
                verifier_calls and verifier_calls[0].get("malformed_output") is True
            ),
            "stronger_verifier_selected": bool(
                len(verifier_calls) == 2
                and str(verifier_calls[1].get("selection_reason") or "").startswith(
                    "stronger_after_"
                )
            ),
            "all_invocations_accounted": all(
                all(
                    key in call
                    for key in (
                        "backend",
                        "model",
                        "capability",
                        "selection_reason",
                        "authority",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "cost_usd",
                        "duration_ms",
                        "outcome",
                        "confidence",
                        "retry_number",
                        "escalation_reason",
                        "malformed_output",
                        "timeout",
                    )
                )
                for call in verifier_calls
            ),
            "verifier_billing_exact": abs(
                sum(float(call.get("cost_usd") or 0) for call in verifier_calls)
                - float(verifier_api.get("verifier_cost_usd") or 0)
            )
            < 1e-12,
            "total_billing_exact": abs(
                float(verifier_api.get("coding_cost_usd") or 0)
                + float(verifier_api.get("verifier_cost_usd") or 0)
                - float(verifier_api.get("total_cost_usd") or 0)
            )
            < 1e-12,
            "recovery_did_not_rebill": bool(
                verifier_scenario["assertions"].get("recovery_did_not_rebill")
            ),
        }
        _write_json(
            artifacts / "verifier-routing-summary.json",
            {
                "status": ("passed" if all(verifier_checks.values()) else "failed"),
                "scenario_id": "scenario_f",
                "run_id": verifier_scenario["run_id"],
                "policy_version": (verifier_result.get("metadata") or {}).get(
                    "verifier_policy_version"
                ),
                "minimum_capability": (verifier_result.get("metadata") or {}).get(
                    "minimum_capability"
                ),
                "selection_reasons": (verifier_result.get("metadata") or {}).get(
                    "selection_reasons"
                ),
                "calls": verifier_calls,
                "coding_cost_usd": verifier_api.get("coding_cost_usd"),
                "verifier_cost_usd": verifier_api.get("verifier_cost_usd"),
                "total_cost_usd": verifier_api.get("total_cost_usd"),
                "checks": verifier_checks,
                "fail_closed_rules": {
                    "failed_validation": True,
                    "unknown_command_role": True,
                    "heuristic_only": True,
                    "malformed_output": True,
                    "timeout": True,
                    "unavailable_authority": True,
                    "unresolved_disagreement": True,
                },
            },
        )
        diversity_scenario = next(
            item for item in scenario_documents if item["scenario_id"] == "scenario_h"
        )
        diversity_candidates = (
            api_snapshots["scenario_h"].get("candidate_outcomes") or {}
        )
        diversity_configurations = {
            attempt_id: candidate.get("candidate_configuration")
            for attempt_id, candidate in diversity_candidates.items()
        }
        acknowledged_fingerprints = sorted(
            {
                str(configuration.get("effective_configuration_digest"))
                for configuration in diversity_configurations.values()
                if isinstance(configuration, dict)
                and configuration.get("runner_acknowledged") is True
                and configuration.get("effective_configuration_digest")
            }
        )
        diversity_checks = {
            "two_acknowledged_configurations": len(acknowledged_fingerprints) == 2,
            "prompt_digests_differ": len(
                {
                    configuration.get("rendered_prompt_digest")
                    for configuration in diversity_configurations.values()
                    if isinstance(configuration, dict)
                }
            )
            == 2,
            "unsupported_seed_not_applied": bool(
                diversity_scenario["assertions"].get("unsupported_seed_not_applied")
            ),
            "unacknowledged_not_counted": bool(
                diversity_scenario["assertions"].get("unacknowledged_not_counted")
            ),
            "recovery_preserved_configuration": bool(
                diversity_scenario["assertions"].get("recovery_preserved_configuration")
            ),
        }
        _write_json(
            artifacts / "candidate-diversity-summary.json",
            {
                "status": "passed" if all(diversity_checks.values()) else "failed",
                "scenario_id": "scenario_h",
                "run_id": diversity_scenario["run_id"],
                "configurations": diversity_configurations,
                "effective_fingerprints": acknowledged_fingerprints,
                "counted_diversity": len(acknowledged_fingerprints),
                "checks": diversity_checks,
            },
        )
        classification_api = api_snapshots["scenario_g"]
        classification_scenario = next(
            item for item in scenario_documents if item["scenario_id"] == "scenario_g"
        )
        _write_json(
            artifacts / "classification-adjustment-summary.json",
            {
                **classification_scenario,
                "status": "passed" if classification_scenario["passed"] else "failed",
                "raw": classification_api.get("raw_classification"),
                "effective": classification_api.get("effective_classification"),
                "adjustments": classification_api.get("classification_adjustments"),
                "selected_backend": classification_api.get("selected_backend"),
                "selected_model": classification_api.get("selected_model"),
                "web_model": (ui_models.get("scenario_g") or {}).get("web"),
                "flight_recorder_model": (ui_models.get("scenario_g") or {}).get(
                    "flight_recorder"
                ),
                "checks": {
                    key: classification_scenario["assertions"].get(key)
                    for key in (
                        "raw_immutable",
                        "effective_derived",
                        "adjustment_complete",
                        "routing_used_effective",
                        "api_raw_classification",
                        "api_effective_classification",
                        "api_adjustment_reason",
                    )
                },
            },
        )
        serialized_evidence = json.dumps(
            {
                "api": api_snapshots,
                "connected": connected,
                "reconciliation": reconciliations,
            },
            sort_keys=True,
        )
        redaction_api = api_snapshots["scenario_d"]
        redaction_status = redaction_api.get("redaction_status") or {}
        spool_secret_absent = release_secret not in json.dumps(
            {
                "events": spool_events_by_run,
                "outcomes": spool_outcomes_by_run,
            },
            sort_keys=True,
        )
        evidence_secret_occurrences: list[str] = []
        searchable_suffixes = {
            ".css",
            ".html",
            ".js",
            ".json",
            ".jsonl",
            ".log",
            ".md",
            ".txt",
        }
        for path in artifacts.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in searchable_suffixes:
                continue
            try:
                if release_secret in path.read_text(encoding="utf-8", errors="strict"):
                    evidence_secret_occurrences.append(
                        path.relative_to(artifacts).as_posix()
                    )
            except (OSError, UnicodeError):
                continue
        redaction_passed = (
            release_secret not in serialized_evidence
            and spool_secret_absent
            and not any(database_secret_counts.values())
            and not evidence_secret_occurrences
            and bool(redaction_api.get("redaction_applied"))
            and int(redaction_api.get("redacted_field_count") or 0) >= 3
            and int(redaction_api.get("withheld_artifact_count") or 0) >= 1
            and "test-token" in str(redaction_api.get("task_instruction") or "")
            and security_probe.get("event_inserted") == 1
            and security_probe.get("artifact_status") == 422
        )
        _write_json(
            artifacts / "redaction-proof.json",
            {
                "status": "passed" if redaction_passed else "failed",
                "registered_secret_absent": redaction_passed,
                "agentd_remote_payload_secret_absent": spool_secret_absent,
                "database_secret_occurrences": database_secret_counts,
                "release_evidence_secret_occurrences": evidence_secret_occurrences,
                "run_visible": bool(redaction_api.get("id")),
                "harmless_token_source_visible": "test-token"
                in str(redaction_api.get("task_instruction") or ""),
                "redaction_status": redaction_status,
                "redaction_categories": redaction_api.get("redaction_categories"),
                "redacted_field_count": redaction_api.get("redacted_field_count"),
                "withheld_artifact_count": redaction_api.get("withheld_artifact_count"),
                "withheld_artifact_categories": redaction_api.get(
                    "withheld_artifact_categories"
                ),
                "agentd_probe": security_probe,
                "unsafe_artifact_rejected": security_probe.get("artifact_status")
                == 422,
                "safe_artifact_synchronized": bool(
                    api_artifact_snapshots["scenario_d"].get("artifacts")
                ),
                "dead_lettered_for_redaction": False,
            },
        )
        browser_runs = work / "browser-runs.json"
        _write_json(browser_runs, connected["run_ids"])
        browser_endpoint = work / "browser-endpoint.json"
        browser_log = (logs / "browser-server.log").open("w", encoding="utf-8")
        browser_server = subprocess.Popen(
            [
                node,
                str(ROOT / "release-verification" / "browser_server.mjs"),
                "--web-root",
                str(ROOT / "components" / "villani-web" / "dist"),
                "--control-plane",
                control_plane_url,
                "--token",
                api_token,
                "--port",
                "0",
                "--endpoint-file",
                str(browser_endpoint),
            ],
            cwd=ROOT,
            env=env,
            stdout=browser_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        browser_log.close()
        deadline = time.monotonic() + 30
        while not browser_endpoint.is_file() and time.monotonic() < deadline:
            if browser_server.poll() is not None:
                raise RuntimeError("connected browser server exited during startup")
            time.sleep(0.05)
        if not browser_endpoint.is_file():
            raise RuntimeError("connected browser server did not publish its endpoint")
        browser_base = str(_read(browser_endpoint)["base_url"])
        _wait_http(browser_base + "/__release/health")
        try:
            _run(
                [
                    node,
                    str(
                        ROOT
                        / "components"
                        / "villani-web"
                        / "e2e"
                        / "release-connected.mjs"
                    ),
                    "--base",
                    browser_base,
                    "--artifacts",
                    str(artifacts),
                    "--runs",
                    str(browser_runs),
                ],
                cwd=ROOT / "components" / "villani-web",
                env=env,
                log=logs / "connected-browser.log",
                timeout=300,
            )
        finally:
            _terminate(browser_server)
            browser_server = None
        browser_summary = _read(artifacts / "browser-summary.json")
        if browser_summary.get("status") != "passed":
            return 1
        if connected["status"] != "passed" or not redaction_passed:
            return 1
        print(json.dumps(connected, sort_keys=True))
        return 0
    finally:
        try:
            if home.exists() and (home / "agentd" / "endpoint.json").exists():
                subprocess.run(
                    [str(agentd), "stop"],
                    cwd=work,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=20,
                )
        finally:
            _terminate(browser_server)
            _terminate(fixture)
            _terminate(control_plane)
            postgres.stop()


if __name__ == "__main__":
    raise SystemExit(main())
