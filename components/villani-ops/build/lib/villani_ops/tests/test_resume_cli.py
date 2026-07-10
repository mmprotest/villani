from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.interfaces import ClosedLoopRunResult


runner = CliRunner()
FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "integration"
    / "fixtures"
    / "protocol"
    / "v1"
    / "valid_run"
)


def _copy_bundle(home: Path, run_id: str) -> Path:
    destination = home / "runs" / run_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, destination)
    return destination


def _make_interrupted(bundle: Path, *, state: str = "ATTEMPT_RUNNING", repository: str | None = None) -> None:
    state_document = json.loads((bundle / "state.json").read_text(encoding="utf-8"))
    state_document.update(
        {
            "state": state,
            "previous_state": "POLICY_SELECTED",
            "terminal": False,
        }
    )
    (bundle / "state.json").write_text(
        json.dumps(state_document), encoding="utf-8"
    )
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(
        {
            "final_state": state,
            "completed_at": None,
            "metadata": {
                "policy_configuration": {"backends": {}},
            },
        }
    )
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if repository is not None:
        task = json.loads((bundle / "task.json").read_text(encoding="utf-8"))
        task["repository_path"] = repository
        (bundle / "task.json").write_text(json.dumps(task), encoding="utf-8")


class _ResumeController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def resume(self, run_id: str, runs_root: Path) -> ClosedLoopRunResult:
        self.calls.append((run_id, runs_root))
        return ClosedLoopRunResult(
            run_id=run_id,
            terminal_state="COMPLETED",
            selected_attempt_id=None,
            run_directory=runs_root / run_id,
            actual_known_cost_usd=None,
            accounting_status="unknown",
            failure_or_exhaustion_reason=None,
        )


def test_resume_missing_run_is_clear(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VILLANI_HOME", str(tmp_path / "home"))
    result = runner.invoke(unified.app, ["resume", "missing"])
    assert result.exit_code == 2
    assert "run not found" in result.output


def test_resume_terminal_run_is_read_only(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    bundle = _copy_bundle(home, "terminal")
    before = (bundle / "events.jsonl").read_bytes()
    result = runner.invoke(unified.app, ["resume", "terminal"])
    assert result.exit_code == 0
    assert "COMPLETED" in result.output
    assert (bundle / "events.jsonl").read_bytes() == before


def test_resume_interrupted_run_calls_controller_and_latest_discovers_it(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    _make_interrupted(_copy_bundle(home, "interrupted"))
    controller = _ResumeController()
    monkeypatch.setattr(unified, "_controller_builder", lambda _config, _events: controller)

    result = runner.invoke(unified.app, ["resume", "--latest"])
    assert result.exit_code == 0, result.output
    assert controller.calls and controller.calls[0][0] == "interrupted"


def test_resume_refuses_dirty_materialization_target(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    repository = tmp_path / "repo"
    repository.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "tests@example.invalid"),
        ("config", "user.name", "Villani tests"),
    ):
        result = subprocess.run(["git", *args], cwd=repository, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    (repository / "tracked.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
    (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    _make_interrupted(
        _copy_bundle(home, "unsafe"),
        state="MATERIALIZING",
        repository=str(repository),
    )
    result = runner.invoke(unified.app, ["resume", "unsafe"])
    assert result.exit_code == 2
    assert "target repository is dirty" in result.output
