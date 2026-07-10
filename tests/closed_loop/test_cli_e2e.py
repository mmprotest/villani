from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any

import yaml
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


ROOT = Path(__file__).resolve().parents[2]
FLIGHT_RECORDER = ROOT / "components" / "villani-flight-recorder" / "dist" / "cli.js"


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed


def _tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "test_calculator.py").write_text(
        "from calculator import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "m9@example.invalid"], repo)
    _run(["git", "config", "user.name", "M9 E2E"], repo)
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "failing baseline"], repo)
    failing = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    assert failing.returncode != 0
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
    "command": "python -m pytest -q", "cwd": str(repo),
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
    # Force the prompt through --task-file so the fake executable receives one
    # deterministic argument vector on both Windows batch and POSIX shells.
    monkeypatch.setenv("VILLANI_CODE_INLINE_PROMPT_LIMIT", "1")
    runner = CliRunner()
    initialized = runner.invoke(unified.app, ["init"])
    assert initialized.exit_code == 0, initialized.output
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    config["budgets"]["max_attempts"] = 2
    config["backends"] = {
        "economy": {
            "provider": "local", "model": "fake-small",
            "roles": ["classification", "coding"], "capability_score": 25,
            "billing_mode": "fixed", "fixed_cost_per_attempt": 0.10,
            "command_name": str(executable),
            "metadata": {"allow_dummy_api_key": True},
        },
        "capable": {
            "provider": "local", "model": "fake-large",
            "roles": ["coding"], "capability_score": 90,
            "billing_mode": "fixed", "fixed_cost_per_attempt": 0.50,
            "command_name": str(executable),
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
    assert manifest["total_cost_usd"] == 0.60
    assert (repo / "calculator.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    _run([sys.executable, "-m", "pytest", "-q"], repo)

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
    assert "0.600000" in html or "0.60" in html
    secret_scan = _run(
        [sys.executable, str(ROOT / "scripts" / "check-secrets.py"), str(run_dir)],
        ROOT,
    )
    assert "0 findings" in secret_scan.stdout
