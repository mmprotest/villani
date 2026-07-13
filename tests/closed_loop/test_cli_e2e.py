from __future__ import annotations

import json
import importlib
import os
import shutil
import subprocess
import sys
import threading
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque
from pathlib import Path
from typing import Any

import yaml
import pytest
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop import (
    BootstrapPolicyEngine,
    ClosedLoopController,
    EvidenceSelectorAdapter,
    PatchMaterializerAdapter,
    VillaniCodeAttemptAdapter,
)
from villani_ops.closed_loop.interfaces import Classification
from villani_ops.closed_loop.interfaces import EvidenceItem, Requirement, Verification
from villani_ops.closed_loop.policy import configured_backends
from villani_ops.cli.agentd_sink import build_agentd_event_sink
from villani_agentd.config import AgentdPaths, Limits, ServerConfig
from villani_agentd.server import AgentdHTTPServer
from villani_agentd.spool import SQLiteSpool
from villani_agentd.structured_log import StructuredLogger


ROOT = Path(__file__).resolve().parents[2]
FLIGHT_RECORDER = ROOT / "components" / "villani-flight-recorder" / "dist" / "cli.js"


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed


def _installed_entry_point(name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    adjacent = Path(sys.executable).resolve().parent / f"{name}{suffix}"
    if adjacent.is_file():
        return str(adjacent)
    resolved = shutil.which(name)
    assert resolved is not None, f"the installed {name} entry point is required"
    return resolved


def _tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    # deliberately wrong baseline\n    return a - b\n",
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
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "m9@example.invalid"], repo)
    _run(["git", "config", "user.name", "M9 E2E"], repo)
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "failing baseline"], repo)
    failing = subprocess.run(
        [sys.executable, "-m", "unittest", "-q"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    assert failing.returncode != 0, "the target fixture must start with a failing test"
    shutil.rmtree(repo / "__pycache__", ignore_errors=True)
    importlib.invalidate_caches()
    return repo


FAKE_CODE = r'''from __future__ import annotations
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
repo = Path(args[args.index("--repo") + 1])
debug_root = Path(args[args.index("--debug-dir") + 1])
attempt = os.environ["VILLANI_ATTEMPT_ID"]
accepted = attempt == "attempt_002"
(repo / "calculator.py").write_text(
    "def add(a, b):\n    return a + b\n" if accepted
    else "def add(a, b):\n    return a - b - 1\n",
    encoding="utf-8",
)
trace = debug_root / "trace"
trace.mkdir(parents=True, exist_ok=True)
stamp = "2026-07-10T00:00:01Z" if not accepted else "2026-07-10T00:00:02Z"
(trace / "session_meta.json").write_text(json.dumps({
    "run_id": os.environ["VILLANI_RUN_ID"],
    "objective": "deterministic e2e",
    "repo": str(repo),
    "model": args[args.index("--model") + 1],
    "provider": "local",
    "created_at": stamp,
}), encoding="utf-8")
(trace / "commands.jsonl").write_text(json.dumps({
    "event_id": f"command-{attempt}", "ts": stamp,
    "command": "python -m unittest -q", "cwd": str(repo),
    "exit_code": 0 if accepted else 1,
    "stdout": "1 passed" if accepted else "1 failed", "stderr": "",
}) + "\n", encoding="utf-8")
(trace / "tool_calls.jsonl").write_text(json.dumps({
    "tool_call_id": f"write-{attempt}", "tool_name": "Write",
    "tool_category": "file_mutation", "started_at": stamp,
    "status": "completed", "args": {"file_path": "calculator.py"},
    "result_summary": "wrote calculator.py",
}) + "\n", encoding="utf-8")
(trace / "model_responses.jsonl").write_text(json.dumps({
    "event_id": f"model-{attempt}", "ts": stamp, "text": "candidate complete",
    "usage": {"input_tokens": 11, "output_tokens": 5},
}) + "\n", encoding="utf-8")
(trace / "patches.jsonl").write_text(json.dumps({
    "event_id": f"patch-{attempt}", "ts": stamp,
    "file_path": "calculator.py", "ok": True,
}) + "\n", encoding="utf-8")
(trace / "validations.jsonl").write_text("", encoding="utf-8")
summary = {
    "status": "completed", "duration_ms": 25,
    "changed_files": ["calculator.py"], "tokens_input": 11, "tokens_output": 5,
}
(trace / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
(trace / "final_summary.json").write_text(json.dumps(summary), encoding="utf-8")
print(f"completed {attempt}")
'''


def _fake_executable(tmp_path: Path) -> Path:
    script = tmp_path / "fake_villani_code.py"
    script.write_text(FAKE_CODE, encoding="utf-8")
    if os.name == "nt":
        wrapper = tmp_path / "fake-villani-code.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper = tmp_path / "fake-villani-code"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    return wrapper


class SequenceVerifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def verify(self, context: Any, result: Any) -> Verification:
        self.calls.append({"context": context, "result": result})
        accepted = len(self.calls) == 2
        if not accepted:
            return Verification(
                verifier="m9_deterministic_verifier",
                outcome="rejected",
                acceptance_eligible=False,
                confidence=0.99,
                reason="The economy backend lacks required capability.",
                recommended_action="escalate",
                requirement_results=(Requirement(
                    requirement_id="addition", description="addition works",
                    outcome="failed", evidence_ids=("test-fail",),
                ),),
                failure_evidence=(EvidenceItem(
                    evidence_id="test-fail", kind="test_result",
                    summary="test_add failed",
                ),),
                metadata={"failure_category": "capability_failure", "verifier_version": "m9_e2e_v1"},
            )
        return Verification(
            verifier="m9_deterministic_verifier",
            outcome="accepted",
            acceptance_eligible=True,
            confidence=0.99,
            reason="The repository test proves addition works.",
            recommended_action="accept",
            requirement_results=(Requirement(
                requirement_id="addition", description="addition works",
                outcome="passed", evidence_ids=("test-pass",),
            ),),
            success_evidence=(EvidenceItem(
                evidence_id="test-pass", kind="test_result",
                summary="test_add passed",
            ),),
            metadata={"verifier_version": "m9_e2e_v1"},
        )


def test_public_cli_two_backend_end_to_end_and_flight_recorder(
    tmp_path: Path, monkeypatch: Any
) -> None:
    home = tmp_path / "home"
    repo = _tiny_repo(tmp_path)
    executable = _fake_executable(tmp_path)
    monkeypatch.setenv("VILLANI_HOME", str(home))
    raw_secret = "complete-path-secret-canary-91bc"
    monkeypatch.setenv("VILLANI_E2E_API_SECRET", raw_secret)
    # Force the prompt through --task-file so the fake executable receives one
    # deterministic argument vector on both Windows batch and POSIX shells.
    monkeypatch.setenv("VILLANI_CODE_INLINE_PROMPT_LIMIT", "1")
    agentd_paths = AgentdPaths(home / "agentd")
    agentd_token = "root-e2e-local-agentd-token"
    spool = SQLiteSpool(agentd_paths, Limits())
    agentd_server = AgentdHTTPServer(
        ("127.0.0.1", 0),
        agentd_token,
        spool,
        ServerConfig(),
        StructuredLogger(agentd_paths.log),
    )
    agentd_thread = threading.Thread(target=agentd_server.serve_forever, daemon=True)
    agentd_thread.start()
    agentd_paths.endpoint.write_text(
        json.dumps(
            {
                "schema_version": "villani.agentd_endpoint.v1",
                "endpoint": f"http://127.0.0.1:{agentd_server.server_port}",
            }
        ),
        encoding="utf-8",
    )
    agentd_paths.token.write_text(agentd_token, encoding="utf-8")
    runner = CliRunner()
    initialized = runner.invoke(unified.app, ["init"])
    assert initialized.exit_code == 0, initialized.output
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    config["budgets"]["max_attempts"] = 2
    config["backends"] = {
        "economy": {
            "provider": "local", "base_url": "http://127.0.0.1:1/v1", "model": "fake-small",
            "roles": ["classification", "coding"], "capability_score": 25,
            "billing_mode": "unknown",
            "command_name": str(executable),
            "api_key_env": "VILLANI_E2E_API_SECRET",
            "metadata": {"allow_dummy_api_key": True},
        },
        "capable": {
            "provider": "local", "base_url": "http://127.0.0.1:1/v1", "model": "fake-large",
            "roles": ["coding"], "capability_score": 90,
            "billing_mode": "unknown",
            "command_name": str(executable),
            "api_key_env": "VILLANI_E2E_API_SECRET",
            "metadata": {"allow_dummy_api_key": True},
        },
    }
    unified._write_config(home / "config.yaml", config)
    verifier = SequenceVerifier()

    def builder(configuration: Any, on_event: Any) -> ClosedLoopController:
        backends = configured_backends(configuration)
        return ClosedLoopController(
            classifier=type("Classifier", (), {"classify": lambda self, task, context: Classification(
                difficulty="easy", risk="low", category="bug_fix",
                confidence=0.99, needs_tests=True,
                reasoning_summary="deterministic command e2e",
                metadata={"classifier_version": "m9_e2e_v1"},
            )})(),
            policy_engine=BootstrapPolicyEngine(backends, configuration),
            attempt_runner=VillaniCodeAttemptAdapter(backends=backends),
            verifier=verifier,
            selector=EvidenceSelectorAdapter(),
            materializer=PatchMaterializerAdapter(),
            on_event=on_event,
            event_sink=build_agentd_event_sink(),
        )

    monkeypatch.setattr(unified, "_controller_builder", builder)
    result = runner.invoke(
        unified.app,
        [
            "run", "Fix calculator addition", "--repo", str(repo),
            "--success-criteria", "test_add passes", "--max-attempts", "2",
        ],
    )
    assert result.exit_code == 0, result.output
    run_id = next(
        line.split(":", 1)[1].strip()
        for line in result.output.splitlines()
        if line.startswith("Run ID:")
    )
    run_dir = home / "runs" / run_id
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    classification_sequence = next(e["sequence"] for e in events if e["event_type"] == "classification_completed")
    policy_sequence = next(e["sequence"] for e in events if e["event_type"] == "policy_decision_started")
    assert classification_sequence < policy_sequence
    assert len(verifier.calls) == 2
    assert json.loads((run_dir / "verification" / "attempt_001.json").read_text())["acceptance_eligible"] is False
    assert json.loads((run_dir / "verification" / "attempt_002.json").read_text())["acceptance_eligible"] is True
    selection = json.loads((run_dir / "selection.json").read_text())
    assert selection["eligible_candidate_ids"] == ["attempt_002"]
    assert selection["selected_candidate_ids"] == ["attempt_002"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["attempt_ids"] == ["attempt_001", "attempt_002"]
    assert manifest["total_input_tokens"] == 22
    assert manifest["total_output_tokens"] == 10
    assert manifest["total_duration_ms"] == 50
    assert manifest["total_cost_usd"] is None
    assert manifest["cost_accounting_status"] == "unknown"
    assert (repo / "calculator.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    _run([sys.executable, "-m", "unittest", "-q"], repo)

    node = shutil.which("node")
    assert node and FLIGHT_RECORDER.is_file()
    rendered = tmp_path / "flight-recorder"
    _run(
        [
            node, str(FLIGHT_RECORDER), "launch", "--provider", "villani",
            "--root", str(home / "runs"), "--run-id", run_id,
            "--no-open", "--out", str(rendered),
        ],
        ROOT,
    )
    html = "\n".join(
        path.read_text(encoding="utf-8")
        for path in rendered.rglob("*.html")
    )
    assert "attempt_001" in html and "attempt_002" in html
    assert "attempt_002" in html and "fake-large" in html
    assert "32" in html and "total tokens" in html and "50ms" in html
    assert "Unknown" in html
    assert raw_secret not in html

    with sqlite3.connect(agentd_paths.database) as connection:
        spooled_runs = connection.execute("SELECT run_id FROM runs").fetchall()
        spooled_events = connection.execute(
            "SELECT payload_json FROM events ORDER BY sequence"
        ).fetchall()
        final_payload = connection.execute(
            "SELECT final_payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    assert spooled_runs == [(run_id,)]
    assert len(spooled_events) == len(events)
    remote_events = [json.loads(row[0]) for row in spooled_events]
    assert {event["run_id"] for event in remote_events} == {run_id}
    outcome = json.loads(final_payload)["outcome"]
    assert outcome["run_id"] == run_id
    assert outcome["attempt_id"] == "attempt_002"
    assert outcome["cost"] is None
    assert raw_secret.encode() not in agentd_paths.database.read_bytes()

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from villani_control_plane.database import Base, get_session
    from villani_control_plane.main import create_app
    from villani_control_plane.models import (
        ApiToken,
        Organization,
        Project,
        Repository,
        Workspace,
    )
    from villani_control_plane.security import (
        Principal,
        hash_token,
        token_lookup_digest,
    )
    from villani_control_plane.services import IngestionService

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    api_token = "root-e2e-control-plane-token-long-enough"
    with Session(engine) as session:
        session.add(Organization(id="org_e2e", name="E2E"))
        session.flush()
        session.add(Workspace(organization_id="org_e2e", id="workspace_e2e", name="E2E"))
        session.flush()
        session.add(
            Project(
                organization_id="org_e2e",
                workspace_id="workspace_e2e",
                id="project_e2e",
                name="E2E",
            )
        )
        session.flush()
        session.add(
            Repository(
                organization_id="org_e2e",
                workspace_id="workspace_e2e",
                project_id="project_e2e",
                id="repo_e2e",
                name="E2E",
            )
        )
        session.flush()
        token_record = ApiToken(
            organization_id="org_e2e",
            workspace_id="workspace_e2e",
            name="e2e",
            lookup_digest=token_lookup_digest(api_token),
            secret_hash=hash_token(api_token),
        )
        session.add(token_record)
        session.commit()
        principal = Principal(token_record.id, "org_e2e", "workspace_e2e")
        service = IngestionService(session)
        first_ingest = service.ingest_batch("root-e2e-batch", remote_events, principal)
        retry_ingest = service.ingest_batch("root-e2e-batch", remote_events, principal)
        assert first_ingest.inserted == len(remote_events)
        assert retry_ingest.replayed is True
        service.record_outcome(outcome, principal)
        service.record_outcome(outcome, principal)
        app = create_app()
        app.dependency_overrides[get_session] = lambda: session
        api = TestClient(app)
        detail = api.get(
            f"/v1/runs/{run_id}",
            headers={"Authorization": f"Bearer {api_token}"},
        )
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert detail_body["id"] == run_id
        assert len(detail_body["outcomes"]) == 1
        assert detail_body["outcomes"][0]["attempt_id"] == "attempt_002"
        assert detail_body["outcomes"][0]["accepted"] is True
        assert detail_body["outcomes"][0]["cost"] is None
        assert detail_body["status"] == "COMPLETED"
        assert raw_secret not in detail.text
    engine.dispose()

    agentd_server.shutdown()
    agentd_server.server_close()
    agentd_thread.join(timeout=5)
    secret_scan = _run(
        [sys.executable, str(ROOT / "scripts" / "check-secrets.py"), str(run_dir)],
        ROOT,
    )
    assert "0 findings" in secret_scan.stdout


class _DeterministicOpenAIHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        tools = payload.get("tools") or []
        if not tools:
            content = json.dumps(
                {
                    "difficulty": "easy",
                    "risk": "low",
                    "category": "bug_fix",
                    "estimated_attempts_needed": 1,
                    "needs_tests": True,
                    "required_capabilities": [],
                    "reasoning_summary": "deterministic local classifier",
                    "confidence": 0.99,
                }
            )
            response = {"choices": [{"message": {"role": "assistant", "content": content}}]}
        else:
            type(self).calls += 1
            if self.calls == 1:
                tool = {
                    "id": "write-1",
                    "type": "function",
                    "function": {
                        "name": "Write",
                        "arguments": json.dumps(
                            {
                                "file_path": "calculator.py",
                                "content": "def add(a, b):\n    return a + b\n",
                            }
                        ),
                    },
                }
                response = {"choices": [{"message": {"role": "assistant", "tool_calls": [tool]}}]}
            elif self.calls == 2:
                tool = {
                    "id": "bash-1",
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "arguments": json.dumps({"command": "python -m unittest -q"}),
                    },
                }
                response = {"choices": [{"message": {"role": "assistant", "tool_calls": [tool]}}]}
            else:
                response = {"choices": [{"message": {"role": "assistant", "content": "Completed the requested fix."}}]}
        body = json.dumps({**response, "usage": {"prompt_tokens": 12, "completion_tokens": 6}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@pytest.mark.e2e
@pytest.mark.parametrize("proxy_mode", ["loopback_no_proxy", "no_proxy_variables"])
def test_public_local_stub_quickstart_uses_real_villani_code_cli(
    tmp_path: Path, proxy_mode: str
) -> None:
    """Exercise installed public entry points and every canonical release stage."""

    villani = _installed_entry_point("villani")
    villani_code = _installed_entry_point("villani-code")
    repo = _tiny_repo(tmp_path)
    home = tmp_path / "home"
    _DeterministicOpenAIHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeterministicOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        environment = os.environ.copy()
        environment["VILLANI_HOME"] = str(home)
        environment["VILLANI_CODE_INLINE_PROMPT_LIMIT"] = "1"
        proxy_names = (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
            "NO_PROXY", "no_proxy",
            "GIT_ASKPASS", "SSH_ASKPASS",
            "VSCODE_GIT_ASKPASS_NODE", "VSCODE_GIT_ASKPASS_EXTRA_ARGS",
            "VSCODE_GIT_ASKPASS_MAIN", "VSCODE_GIT_IPC_HANDLE",
        )
        for name in proxy_names:
            environment.pop(name, None)
        if proxy_mode == "loopback_no_proxy":
            environment.update(
                {
                    "HTTP_PROXY": "http://127.0.0.1:9",
                    "HTTPS_PROXY": "http://127.0.0.1:9",
                    "ALL_PROXY": "http://127.0.0.1:9",
                    "NO_PROXY": "127.0.0.1,localhost",
                }
            )
        initialized = subprocess.run(
            [villani, "init"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        assert initialized.returncode == 0, initialized.stdout + initialized.stderr
        config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        config["budgets"]["max_attempts"] = 1
        config["repository_validation_commands"] = [
            {
                "validation_id": "repository_unittest",
                "argv": [sys.executable, "-m", "unittest", "-q"],
                "timeout_seconds": 30,
            }
        ]
        config["backends"] = {
            "local-stub": {
                "provider": "local",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "model": "deterministic",
                "roles": ["classification", "coding"],
                "capability_score": 100,
                "billing_mode": "unknown",
                "command_name": villani_code,
                "metadata": {"allow_dummy_api_key": True},
            }
        }
        unified._write_config(home / "config.yaml", config)
        doctor = subprocess.run(
            [villani, "doctor", "--repo", str(repo), "--json"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        assert doctor.returncode == 0, doctor.stdout + doctor.stderr
        doctor_report = json.loads(doctor.stdout)
        assert doctor_report["schema_version"] == "villani.doctor.v1"
        assert doctor_report["ok"] is True
        assert doctor_report["backend_connectivity"][0]["model_tokens_spent"] == 0
        assert doctor_report["inferred_commands_executed"] is False
        result = subprocess.run(
            [
                villani,
                "run",
                "Fix calculator addition",
                "--repo",
                str(repo),
                "--success-criteria",
                "The test suite passes",
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert (repo / "calculator.py").read_text(encoding="utf-8").endswith("return a + b\n")
        _run([sys.executable, "-m", "unittest", "-q"], repo)
        run_id = next(
            line.split(":", 1)[1].strip()
            for line in output.splitlines()
            if line.startswith("Run ID:")
        )
        run_dir = home / "runs" / run_id
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        assert state["state"] == "COMPLETED"
        event_types = {
            json.loads(line)["event_type"]
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        }
        assert {
            "classification_completed",
            "attempt_completed",
            "verification_completed",
            "candidate_selected",
            "materialization_completed",
            "run_completed",
        } <= event_types
        selection = json.loads((run_dir / "selection.json").read_text(encoding="utf-8"))
        assert selection["selected_candidate_ids"] == ["attempt_001"]
        attempt_dir = run_dir / "attempts" / "attempt_001"
        assert (attempt_dir / "patch.diff").is_file()
        assert (attempt_dir / "worktree.json").is_file()
        execution_environment = json.loads(
            (run_dir / "execution_environment.json").read_text(encoding="utf-8")
        )
        assert execution_environment["provider"] == "inherit"
        assert execution_environment["fingerprint"]
        preflight = json.loads((run_dir / "preflight.json").read_text(encoding="utf-8"))
        assert preflight["execution_environment_fingerprint"] == execution_environment["fingerprint"]
        assert preflight["inferred_setup_executed"] is False
        resource = json.loads((run_dir / "resource.json").read_text(encoding="utf-8"))
        assert resource["attributes"]["villani.execution_environment.fingerprint"] == execution_environment["fingerprint"]
        assert not (attempt_dir / "worktree").exists()

        node = shutil.which("node")
        assert node is not None and FLIGHT_RECORDER.is_file()
        rendered = tmp_path / "flight-recorder"
        _run(
            [
                node,
                str(FLIGHT_RECORDER),
                "launch",
                "--provider",
                "villani",
                "--root",
                str(home / "runs"),
                "--run-id",
                run_id,
                "--no-open",
                "--out",
                str(rendered),
            ],
            ROOT,
        )
        html = "\n".join(
            path.read_text(encoding="utf-8") for path in rendered.rglob("*.html")
        )
        assert run_id in html and "attempt_001" in html
        secret_scan = _run(
            [sys.executable, str(ROOT / "scripts" / "check-secrets.py"), str(run_dir)],
            ROOT,
        )
        assert "0 findings" in secret_scan.stdout
    finally:
        server.shutdown()
        thread.join(timeout=5)
