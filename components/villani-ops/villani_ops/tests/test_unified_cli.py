from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.interfaces import ClosedLoopRunResult
from villani_ops.closed_loop.schema_validation import validate_protocol_document


runner = CliRunner()
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
VALID_RUN = REPOSITORY_ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    monkeypatch.delenv("VILLANI_VFR_COMMAND", raising=False)
    monkeypatch.setattr(unified, "_controller_builder", None)
    return home


def _init() -> Path:
    result = runner.invoke(unified.app, ["init"])
    assert result.exit_code == 0, result.output
    return unified._config_path()


def _copy_valid_run(root: Path, name: str = "run_protocol_fixture") -> Path:
    destination = root / name
    shutil.copytree(VALID_RUN, destination)
    return destination


class FakeController:
    def __init__(self, state: str) -> None:
        self.state = state
        self.requests: list[Any] = []

    def run(self, request: Any) -> ClosedLoopRunResult:
        self.requests.append(request)
        run_dir = _copy_valid_run(Path(request.runs_root))
        return ClosedLoopRunResult(
            run_id="run_protocol_fixture",
            terminal_state=self.state,  # type: ignore[arg-type]
            selected_attempt_id=(
                "attempt_002" if self.state == "COMPLETED" else None
            ),
            run_directory=run_dir,
            actual_known_cost_usd=0.05,
            accounting_status="complete",
            failure_or_exhaustion_reason=(
                None if self.state == "COMPLETED" else f"fake {self.state.lower()}"
            ),
        )


def _invoke_fake_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> tuple[Any, FakeController]:
    _init()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(unified, "_is_git_repository", lambda path: True)
    fake = FakeController(state)
    calls: list[dict[str, Any]] = []

    def builder(configuration: Any, on_event: Any) -> FakeController:
        calls.append({"configuration": configuration, "on_event": on_event})
        return fake

    monkeypatch.setattr(unified, "_controller_builder", builder)
    result = runner.invoke(
        unified.app,
        [
            "run",
            "  Preserve this task verbatim.  ",
            "--repo",
            str(repository),
            "--success-criteria",
            "Exact success criteria.  ",
        ],
    )
    assert len(calls) == 1
    assert len(fake.requests) == 1
    assert fake.requests[0].task == "  Preserve this task verbatim.  "
    assert fake.requests[0].success_criteria == "Exact success criteria.  "
    return result, fake


def test_init_creates_config_and_does_not_overwrite(isolated_home: Path) -> None:
    config = _init()
    assert config == isolated_home / "config.yaml"
    assert (isolated_home / "runs").is_dir()
    original = config.read_text(encoding="utf-8")
    assert original.startswith("# Villani local-first configuration")
    config.write_text(original + "# user value\n", encoding="utf-8")

    second = runner.invoke(unified.app, ["init"])

    assert second.exit_code == 0
    assert "not overwritten" in second.output
    assert config.read_text(encoding="utf-8").endswith("# user value\n")
    forced = runner.invoke(unified.app, ["init", "--force"])
    assert forced.exit_code == 0
    assert "# user value" not in config.read_text(encoding="utf-8")


def test_backend_add_validates_capability_and_billing_fields() -> None:
    _init()
    missing_capability = runner.invoke(
        unified.app,
        ["backend", "add", "code", "--provider", "local", "--model", "m"],
    )
    assert missing_capability.exit_code == 2
    assert "--capability-score is required" in missing_capability.output

    incomplete_token = runner.invoke(
        unified.app,
        [
            "backend",
            "add",
            "code",
            "--provider",
            "local",
            "--model",
            "m",
            "--capability-score",
            "25",
            "--billing-mode",
            "token",
            "--input-cost-per-million",
            "1",
        ],
    )
    assert incomplete_token.exit_code == 2
    assert "requires both" in incomplete_token.output

    added = runner.invoke(
        unified.app,
        [
            "backend",
            "add",
            "code",
            "--provider",
            "local",
            "--model",
            "m",
            "--role",
            "coding",
            "--role",
            "classification",
            "--capability-score",
            "25",
            "--billing-mode",
            "hybrid",
            "--input-cost-per-million",
            "1",
            "--output-cost-per-million",
            "2",
            "--compute-cost-per-hour",
            "3",
            "--estimated-input-tokens",
            "1000",
            "--estimated-output-tokens",
            "200",
            "--estimated-duration-seconds",
            "60",
            "--api-key-env",
            "VILLANI_TEST_KEY",
        ],
    )
    assert added.exit_code == 0, added.output
    configuration = yaml.safe_load(unified._config_path().read_text(encoding="utf-8"))
    backend = configuration["backends"]["code"]
    assert backend["roles"] == ["coding", "classification"]
    assert backend["billing_mode"] == "hybrid"
    assert backend["capability_score"] == 25
    assert backend["api_key_env"] == "VILLANI_TEST_KEY"


def test_backend_list_redacts_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _init()
    configuration = yaml.safe_load(config.read_text(encoding="utf-8"))
    monkeypatch.setenv("VILLANI_RUNTIME_KEY", "runtime-resolved-secret")
    configuration["backends"] = {
        "manual": {
            "provider": "openai",
            "model": "m",
            "roles": ["classification"],
            "api_key": "resolved-secret-value",
        },
        "environment": {
            "provider": "openai",
            "model": "m2",
            "roles": ["classification"],
            "api_key_env": "VILLANI_RUNTIME_KEY",
        },
    }
    unified._write_config(config, configuration)

    result = runner.invoke(unified.app, ["backend", "list"])

    assert result.exit_code == 0
    assert "resolved-secret-value" not in result.output
    assert "runtime-resolved-secret" not in result.output
    assert "configured (redacted)" in result.output
    assert "env:VILLANI_RUNTIME_KEY" in result.output


def test_run_calls_closed_loop_controller_not_legacy_orchestrators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake = _invoke_fake_run(tmp_path, monkeypatch, "COMPLETED")
    assert result.exit_code == 0, result.output
    assert fake.requests[0].runs_root == unified._runs_root()
    source = Path(unified.__file__).read_text(encoding="utf-8")
    assert "villani_ops.cli.main" not in source
    assert "OpsRunner" not in source
    assert "VillaniOps(" not in source


def test_help_contains_no_architecture_selector() -> None:
    root_help = runner.invoke(unified.app, ["--help"])
    run_help = runner.invoke(unified.app, ["run", "--help"])
    assert root_help.exit_code == 0
    assert run_help.exit_code == 0
    combined = (root_help.output + run_help.output).lower()
    for forbidden in (
        "--orchestrator",
        "adaptive",
        "agentic",
        "graph",
        "verifier-parallel",
        "verifier-sequential",
        "tournament",
        "decomposition",
        "scheduling",
    ):
        assert forbidden not in combined
    for command in ("init", "backend", "run", "runs", "inspect", "open"):
        assert command in root_help.output


def test_completed_run_exits_zero_and_prints_evidence_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _invoke_fake_run(tmp_path, monkeypatch, "COMPLETED")
    assert result.exit_code == 0
    assert "Run ID: run_protocol_fixture" in result.output
    assert "Run directory:" in result.output
    assert "Terminal state: COMPLETED" in result.output
    assert "Classification:" in result.output
    assert "Attempts:" in result.output
    assert "Verifier outcomes:" in result.output
    assert "Selected attempt: attempt_002" in result.output
    assert "Cost: USD 0.050000" in result.output
    assert "Tokens:" in result.output
    assert "Duration:" in result.output
    assert "Final patch: succeeded" in result.output


def test_exhausted_run_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _invoke_fake_run(tmp_path, monkeypatch, "EXHAUSTED")
    assert result.exit_code == 3
    assert "Terminal state: EXHAUSTED" in result.output


def test_failed_run_exits_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _invoke_fake_run(tmp_path, monkeypatch, "FAILED")
    assert result.exit_code == 4
    assert "Terminal state: FAILED" in result.output


def test_runs_tolerates_one_corrupt_bundle() -> None:
    _init()
    _copy_valid_run(unified._runs_root())
    corrupt = unified._runs_root() / "run_corrupt"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("{", encoding="utf-8")

    result = runner.invoke(unified.app, ["runs"])

    assert result.exit_code == 0
    assert "run_protocol_fixture" in result.output
    assert "COMPLETED" in result.output
    assert "run_corrupt" in result.output
    assert "corrupt" in result.output


def test_inspect_json_is_schema_valid_and_redacted() -> None:
    _init()
    run_dir = _copy_valid_run(unified._runs_root())
    task_path = run_dir / "task.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["metadata"]["api_key"] = "resolved-secret-value"
    task_path.write_text(json.dumps(task, indent=2), encoding="utf-8")
    attempt_path = run_dir / "attempts" / "attempt_002" / "attempt.json"
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    attempt["metadata"]["cost_breakdown"] = {
        "input_token_cost": 0.01,
        "output_token_cost": 0.02,
        "compute_time_cost": None,
        "fixed_cost": None,
        "total": 0.03,
        "currency": "USD",
        "accounting_status": "complete",
        "source": "captured_telemetry_and_backend_config",
    }
    attempt_path.write_text(json.dumps(attempt, indent=2), encoding="utf-8")

    result = runner.invoke(
        unified.app, ["inspect", "run_protocol_fixture", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert "resolved-secret-value" not in result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "villani.inspect.v1"
    validate_protocol_document(payload["manifest"])
    validate_protocol_document(payload["task"])
    validate_protocol_document(payload["classification"])
    for collection in (
        "policy_decisions",
        "attempts",
        "verifications",
    ):
        for document in payload[collection]:
            validate_protocol_document(document)
    validate_protocol_document(payload["selection"])
    validate_protocol_document(payload["materialization"])
    assert payload["cost_components"][1]["cost"]["total"] == 0.03


@pytest.mark.parametrize("mode", ["configured", "path", "monorepo"])
def test_open_resolves_command_fallbacks_in_order(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init()
    calls: list[str] = []
    launched: list[list[str]] = []
    bundled = tmp_path / "dist" / "cli.js"
    bundled.parent.mkdir()
    bundled.write_text("// built", encoding="utf-8")
    monkeypatch.setattr(unified, "_monorepo_vfr_path", lambda: bundled)

    def resolve(command: str) -> list[str] | None:
        calls.append(command)
        if mode == "configured" and command == "custom-vfr":
            return ["custom-executable"]
        if mode == "path" and command == "vfr":
            return ["vfr-executable"]
        if mode == "monorepo" and command == "node":
            return ["node-executable"]
        return None

    if mode == "configured":
        monkeypatch.setenv("VILLANI_VFR_COMMAND", "custom-vfr --flag")
    monkeypatch.setattr(unified, "resolve_command_prefix", resolve)
    monkeypatch.setattr(
        unified.subprocess,
        "run",
        lambda command, check=False: (
            launched.append(list(command)) or SimpleNamespace(returncode=0)
        ),
    )

    result = runner.invoke(unified.app, ["open"])

    assert result.exit_code == 0, result.output
    assert len(launched) == 1
    assert launched[0][-5:] == [
        "launch",
        "--provider",
        "villani",
        "--root",
        str(unified._runs_root()),
    ]
    if mode == "configured":
        assert calls == ["custom-vfr"]
        assert launched[0][:2] == ["custom-executable", "--flag"]
    elif mode == "path":
        assert calls == ["vfr"]
        assert launched[0][0] == "vfr-executable"
    else:
        assert calls == ["vfr", "node"]
        assert launched[0][:2] == ["node-executable", str(bundled)]


def test_open_passes_optional_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init()
    _copy_valid_run(unified._runs_root())
    launched: list[str] = []
    monkeypatch.setattr(unified, "_resolve_vfr_command", lambda: ["vfr"])
    monkeypatch.setattr(
        unified.subprocess,
        "run",
        lambda command, check=False: (
            launched.extend(command) or SimpleNamespace(returncode=0)
        ),
    )
    result = runner.invoke(unified.app, ["open", "run_protocol_fixture"])
    assert result.exit_code == 0
    assert launched == [
        "vfr",
        "replay",
        "--provider",
        "villani",
        "--root",
        str(unified._runs_root()),
        "--id",
        "run_protocol_fixture",
        "--open",
    ]


def test_open_fails_clearly_when_flight_recorder_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    monkeypatch.setattr(unified, "_resolve_vfr_command", lambda: None)
    result = runner.invoke(unified.app, ["open"])
    assert result.exit_code == 2
    assert "npm install -g villani-flight-recorder" in result.output
    assert "npm install && npm run build" in result.output
    assert "old Villani Ops viewer" not in result.output
