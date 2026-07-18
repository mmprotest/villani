from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.candidate_strategies import immutable_baseline_digest
from villani_ops.closed_loop.interfaces import ClosedLoopRunResult
from villani_ops.closed_loop.schema_validation import validate_protocol_document
from villani_ops.tests.closed_loop.fakes import (
    FakeAttemptRunner,
    FakeClassifier,
    FakeMaterializer,
    FakeMonotonic,
    FakePolicyEngine,
    FakeSelector,
    FakeVerifier,
    FixedNow,
    StableIds,
    accepted_verification,
    attempt,
    backend,
    policy,
)


runner = CliRunner()
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
VALID_RUN = (
    REPOSITORY_ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"
)


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
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["state"] = self.state
        state["terminal"] = True
        state["metadata"] = {
            "terminal_reason": (
                None if self.state == "COMPLETED" else f"fake {self.state.lower()}"
            )
        }
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["final_state"] = self.state
        if self.state != "COMPLETED":
            manifest["selected_attempt_id"] = None
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return ClosedLoopRunResult(
            run_id="run_protocol_fixture",
            terminal_state=self.state,  # type: ignore[arg-type]
            selected_attempt_id=("attempt_002" if self.state == "COMPLETED" else None),
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
    extra_args: tuple[str, ...] = (),
) -> tuple[Any, FakeController]:
    _init()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(unified, "_is_git_repository", lambda path: True)
    monkeypatch.setattr(unified, "_git_repository_root", lambda path: repository)
    monkeypatch.setattr(unified, "_repository_dirty", lambda path: False)
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
            "--validation-command",
            "python -m pytest -q",
            *extra_args,
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
    initialized = yaml.safe_load(original)
    assert initialized["delivery"]["default_mode"] == "apply"
    assert initialized["delivery"]["authority_policy"] == {
        "policy_version": "villani.default_delivery_authority.v1",
        "allow_automatic": True,
        "require_acceptance_eligible": True,
        "allowed_risks": ["low"],
    }
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
    assert "--provider local requires --base-url" in missing_capability.output

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
            "--base-url",
            "http://127.0.0.1:8000/v1",
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
            "--base-url",
            "http://127.0.0.1:8000/v1",
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


def test_run_task_file_preserves_complete_multiline_task_in_canonical_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
) -> None:
    _init()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(unified, "_git_repository_root", lambda _path: repository)
    monkeypatch.setattr(unified, "_repository_dirty", lambda _path: False)
    monkeypatch.setenv("VILLANI_TASK_CANARY", "expanded-value")
    task = (
        "Fix the assertion diff bug.\n\n"
        "The implementation must preserve identical trailing characters.\n"
        "Keep literal $VILLANI_TASK_CANARY and $(Write-Output 'not executed').\n\n"
        "Add a focused regression test and run the relevant repository tests."
    )
    task_file = tmp_path / "multiline-task.md"
    task_file.write_bytes(task.encode("utf-8"))

    selected_backend = backend("fixture")
    classifier = FakeClassifier()
    policy_engine = FakePolicyEngine(
        [
            policy("attempt", backend_option=selected_backend),
            policy("select"),
        ]
    )
    attempt_runner = FakeAttemptRunner([attempt()])
    verifier = FakeVerifier([accepted_verification()])
    selector = FakeSelector()
    materializer = FakeMaterializer()
    builder_calls: list[dict[str, Any]] = []

    def builder(configuration: Any, on_event: Any) -> ClosedLoopController:
        builder_calls.append({"configuration": configuration, "on_event": on_event})
        return ClosedLoopController(
            classifier=classifier,
            policy_engine=policy_engine,
            attempt_runner=attempt_runner,
            verifier=verifier,
            selector=selector,
            materializer=materializer,
            now=FixedNow(),
            monotonic=FakeMonotonic(),
            id_factory=StableIds(),
            on_event=on_event,
        )

    monkeypatch.setattr(unified, "_controller_builder", builder)
    result = runner.invoke(
        unified.app,
        [
            "run",
            "--task-file",
            str(task_file),
            "--repo",
            str(repository),
            "--validation-command",
            "python -m pytest -q",
            "--delivery",
            "suggest",
            "--max-attempts",
            "1",
            "--max-cost",
            "10",
            "--max-wall-time",
            "60",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "unexpected extra argument" not in result.output.lower()
    assert len(builder_calls) == 1
    assert classifier.calls[0][0] == task
    assert attempt_runner.calls[0].task == task
    assert verifier.calls[0][0].task == task
    assert selector.calls[0][1].task == task
    assert "$VILLANI_TASK_CANARY" in attempt_runner.calls[0].task
    assert "expanded-value" not in attempt_runner.calls[0].task
    run_directories = [
        path
        for path in (isolated_home / "runs").iterdir()
        if path.is_dir() and (path / "manifest.json").is_file()
    ]
    assert len(run_directories) == 1
    canonical_task = json.loads(
        (run_directories[0] / "task.json").read_text(encoding="utf-8")
    )
    assert canonical_task["instruction"] == task
    assert canonical_task["success_criteria"] == task
    assert "\n\n" in canonical_task["instruction"]
    candidate_strategy = json.loads(
        (run_directories[0] / "candidate_strategy.json").read_text(encoding="utf-8")
    )
    assert candidate_strategy["baseline_sha256"] == immutable_baseline_digest(
        repository, task, task
    )
    run_created = json.loads(
        (run_directories[0] / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert run_created["payload"]["task_instruction"] == task
    assert (
        unified._inspect_bundle(run_directories[0].name)["task"]["instruction"] == task
    )


@pytest.mark.parametrize(
    ("case", "expected_message"),
    [
        (
            "neither",
            "Provide exactly one task source: positional TASK or --task-file PATH.",
        ),
        (
            "both",
            "Provide exactly one task source: positional TASK or --task-file PATH.",
        ),
        ("missing", "Task file does not exist:"),
        ("directory", "Task file is not a regular file:"),
        ("invalid-utf8", "Task file must contain valid UTF-8:"),
        ("empty", "Task instruction is empty."),
        ("whitespace", "Task instruction is empty."),
        ("empty-positional", "Task instruction is empty."),
        ("whitespace-positional", "Task instruction is empty."),
    ],
)
def test_run_rejects_invalid_task_input_before_run_creation(
    case: str,
    expected_message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
) -> None:
    task_file = tmp_path / "task.md"
    if case == "invalid-utf8":
        task_file.write_bytes(b"task\xff")
    elif case == "empty":
        task_file.write_bytes(b"")
    elif case == "whitespace":
        task_file.write_bytes(b" \t\r\n")
    else:
        task_file.write_text("File task", encoding="utf-8")

    if case == "neither":
        arguments = ["run"]
    elif case == "both":
        arguments = ["run", "Positional task", "--task-file", str(task_file)]
    elif case == "empty-positional":
        arguments = ["run", ""]
    elif case == "whitespace-positional":
        arguments = ["run", " \t\r\n"]
    elif case == "missing":
        missing = tmp_path / "missing-task.md"
        arguments = ["run", "--task-file", str(missing)]
    elif case == "directory":
        arguments = ["run", "--task-file", str(tmp_path)]
    else:
        arguments = ["run", "--task-file", str(task_file)]

    backend_calls: list[object] = []

    def forbidden_builder(*args: object, **kwargs: object) -> object:
        backend_calls.append((args, kwargs))
        raise AssertionError("task validation must precede controller construction")

    monkeypatch.setattr(unified, "_controller_builder", forbidden_builder)
    result = runner.invoke(unified.app, arguments)

    assert result.exit_code == 2
    assert expected_message in result.output
    assert "Traceback" not in result.output
    assert backend_calls == []
    runs_root = isolated_home / "runs"
    assert not runs_root.exists() or list(runs_root.iterdir()) == []


def test_run_both_or_neither_task_source_prints_exact_message(
    tmp_path: Path,
) -> None:
    task_file = tmp_path / "task.md"
    task_file.write_text("File task", encoding="utf-8")
    expected = (
        "Provide exactly one task source: positional TASK or --task-file PATH.\n"
    )
    neither = runner.invoke(unified.app, ["run"])
    both = runner.invoke(
        unified.app,
        ["run", "Positional task", "--task-file", str(task_file)],
    )
    assert neither.exit_code == 2
    assert both.exit_code == 2
    assert neither.output == expected
    assert both.output == expected


def test_run_defaults_to_current_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.chdir(repository)
    monkeypatch.setattr(unified, "_git_repository_root", lambda path: repository)
    monkeypatch.setattr(unified, "_repository_dirty", lambda path: False)
    fake = FakeController("COMPLETED")
    monkeypatch.setattr(
        unified, "_controller_builder", lambda _configuration, _events: fake
    )

    result = runner.invoke(
        unified.app,
        [
            "run",
            "Use the current repository.",
            "--validation-command",
            "python -m pytest -q",
        ],
    )

    assert result.exit_code == 0, result.output
    assert fake.requests[0].repository_path == repository.resolve()


@pytest.mark.parametrize(
    ("delivery_mode", "materialization_type", "approval_mode"),
    [
        ("suggest", "patch_export", "automatic"),
        ("approve", "local_patch_apply", "explicit"),
        ("apply", "local_patch_apply", "automatic"),
        ("branch", "local_branch", "automatic"),
        ("pull-request", "pull_request", "automatic"),
    ],
)
def test_run_exposes_each_public_delivery_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
    delivery_mode: str,
    materialization_type: str,
    approval_mode: str,
) -> None:
    _init()
    if delivery_mode == "pull-request":
        monkeypatch.setenv("VILLANI_ALLOW_DEVELOPMENT_LICENSE", "1")
        shutil.copyfile(
            REPOSITORY_ROOT
            / "integration"
            / "fixtures"
            / "licenses"
            / "development-pro.json",
            isolated_home / "license.json",
        )
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(unified, "_git_repository_root", lambda path: repository)
    monkeypatch.setattr(unified, "_repository_dirty", lambda path: False)
    fake = FakeController("COMPLETED")
    monkeypatch.setattr(
        unified, "_controller_builder", lambda _configuration, _events: fake
    )

    result = runner.invoke(
        unified.app,
        [
            "run",
            "Exercise delivery selection.",
            "--repo",
            str(repository),
            "--validation-command",
            "python -m pytest -q",
            "--delivery",
            delivery_mode,
        ],
    )

    assert result.exit_code == 0, result.output
    delivery = fake.requests[0].policy_configuration["delivery"]
    assert delivery["workflow_version"] == "villani.delivery_workflow.v1"
    assert delivery["mode"] == delivery_mode
    assert delivery["materialization_type"] == materialization_type
    assert delivery["approval_mode"] == approval_mode


def test_bare_run_uses_initialized_delivery_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(unified, "_git_repository_root", lambda path: repository)
    monkeypatch.setattr(unified, "_repository_dirty", lambda path: False)
    monkeypatch.setattr(
        unified, "prepare_repository_validation", lambda *args, **kwargs: None
    )
    fake = FakeController("COMPLETED")
    monkeypatch.setattr(
        unified, "_controller_builder", lambda _configuration, _events: fake
    )

    result = runner.invoke(
        unified.app,
        ["run", "Exercise the configured default.", "--repo", str(repository)],
    )

    assert result.exit_code == 0, result.output
    delivery = fake.requests[0].policy_configuration["delivery"]
    assert delivery["mode"] == "approve"
    assert delivery["approval_mode"] == "explicit"
    run_experience = fake.requests[0].policy_configuration["run_experience"]
    assert run_experience["mode"] == "performance"
    assert run_experience["verification_required"] is True
    assert run_experience["default_wall_time_budget"] is None


def test_progress_symbols_fall_back_on_legacy_windows_encoding() -> None:
    assert unified._display_progress_symbol("●", "cp1252") == "*"
    assert unified._display_progress_symbol("✓", "cp1252") == "+"
    assert unified._display_progress_symbol("●", "utf-8") == "●"


def test_default_cli_progress_projects_exactly_four_product_stages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listener = unified._run_progress_listener(tmp_path)

    def event(sequence: int, event_type: str, to_state: str) -> SimpleNamespace:
        value = {
            "schema_version": "villani.event.v1",
            "sequence": sequence,
            "timestamp": "2026-07-17T00:00:00Z",
            "run_id": "run_stage_fixture",
            "attempt_id": None,
            "event_type": event_type,
            "payload": {"to_state": to_state},
        }
        return SimpleNamespace(
            **value,
            model_dump=lambda mode="json", value=value: value,
        )

    listener(event(1, "run_created", "CREATED"))
    listener(event(2, "attempt_started", "ATTEMPT_RUNNING"))
    listener(event(3, "verification_started", "VERIFYING"))
    listener(event(4, "run_completed", "COMPLETED"))

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert [line.split(":", 1)[0] for line in lines] == [
        "Understanding",
        "Working",
        "Checking",
        "Ready",
    ]
    assert all("run_" not in line and "event" not in line for line in lines)


def test_help_contains_no_architecture_selector() -> None:
    root_help = runner.invoke(unified.app, ["--help"])
    run_help = runner.invoke(unified.app, ["run", "--help"])
    assert root_help.exit_code == 0
    assert run_help.exit_code == 0
    normalized_run_help = " ".join(run_help.output.split())
    assert "Task instruction. Omit when using --task-file." in normalized_run_help
    assert "--task-file" in run_help.output
    assert "Read the complete task" in normalized_run_help
    assert "instruction from a UTF-8" in normalized_run_help
    assert "file." in normalized_run_help
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
    for command in (
        "init",
        "backend",
        "capability",
        "run",
        "resume",
        "rerun",
        "approve",
        "reject",
        "request-rerun",
        "choose-candidate",
        "runs",
        "inspect",
        "evidence",
        "open",
    ):
        assert command in root_help.output
    for delivery_mode in (
        "suggest",
        "approve",
        "apply",
        "branch",
        "pull-request",
    ):
        assert delivery_mode in run_help.output


def test_capability_commands_rebuild_list_and_explain_without_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _init()
    configuration = yaml.safe_load(config.read_text(encoding="utf-8"))
    configuration["backends"] = {
        "fixture": {
            "provider": "local",
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "fixture-model",
            "roles": ["classification", "coding"],
            "capability_score": 55,
            "billing_mode": "fixed",
            "fixed_cost_per_attempt": 0.1,
        }
    }
    unified._write_config(config, configuration)
    _copy_valid_run(unified._runs_root())

    rebuilt = runner.invoke(unified.app, ["capability", "rebuild"])
    assert rebuilt.exit_code == 0, rebuilt.output
    assert "Profile digest:" in rebuilt.output
    assert (
        Path(os.environ["VILLANI_HOME"]) / "capabilities" / "profiles-v1.json"
    ).is_file()

    listed = runner.invoke(unified.app, ["capability", "list"])
    assert listed.exit_code == 0, listed.output
    assert "static=55" in listed.output
    assert "samples=" in listed.output

    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(unified, "_is_git_repository", lambda path: True)
    monkeypatch.setattr(
        unified,
        "_classify_for_capability_explain",
        lambda *args, **kwargs: unified.ClassificationSnapshot(
            schema_version="villani.classification.v1",
            classification_id="capability_explain",
            run_id="capability_explain",
            task_id="capability_explain",
            classified_at="2026-07-10T00:00:00Z",
            difficulty="easy",
            risk="low",
            category="bug_fix",
            required_capabilities=[],
            estimated_attempts_needed=1,
            needs_tests=True,
            confidence=0.9,
            reasoning_summary="fixture",
            signals={},
            metadata={"classifier_version": "task_classifier_v1"},
        ),
    )
    explained = runner.invoke(
        unified.app,
        ["capability", "explain", "--task", "fix it", "--repo", str(repository)],
    )
    assert explained.exit_code == 0, explained.output
    payload = json.loads(explained.output)
    assert payload["coding_attempt_executed"] is False
    assert payload["path_used"] == "bootstrap_fallback"
    option = payload["bootstrap"]["considered_backends"][0]
    assert option["configured_capability_score"] == 55
    assert option["effective_capability_score"] == 35
    assert option["capability_score"] == 35


def test_policy_explain_reports_capability_provenance_and_reserves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _init()
    configuration = yaml.safe_load(config.read_text(encoding="utf-8"))
    configuration["model_management"]["bootstrap_default"] = "weak"
    configuration["backends"] = {
        "weak": {
            "provider": "local",
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "weak-model",
            "roles": ["classification", "coding"],
            "capability_score": 80,
            "billing_mode": "fixed",
            "fixed_cost_per_attempt": 0.1,
        },
        "strong": {
            "provider": "local",
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "strong-model",
            "roles": ["coding"],
            "capability_score": 100,
            "capability_score_source": "explicit_override",
            "billing_mode": "fixed",
            "fixed_cost_per_attempt": 1.0,
        },
    }
    unified._write_config(config, configuration)
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(unified, "_is_git_repository", lambda path: True)
    monkeypatch.setattr(unified, "_git_repository_root", lambda path: repository)
    monkeypatch.setattr(unified, "_repository_dirty", lambda path: False)
    monkeypatch.setattr(
        unified,
        "_classify_for_capability_explain",
        lambda *args, **kwargs: unified.ClassificationSnapshot(
            schema_version="villani.classification.v1",
            classification_id="policy_explain",
            run_id="policy_explain",
            task_id="policy_explain",
            classified_at="2026-07-17T00:00:00Z",
            difficulty="hard",
            risk="high",
            category="bug_fix",
            required_capabilities=[],
            estimated_attempts_needed=1,
            needs_tests=True,
            confidence=0.95,
            reasoning_summary="fixture",
            signals={},
            metadata={"classifier_version": "task_classifier_v1"},
        ),
    )

    explained = runner.invoke(
        unified.app,
        ["policy", "explain", "hard change", "--repo", str(repository)],
    )

    assert explained.exit_code == 0, explained.output
    assert "Coding route: none / none" in explained.output
    assert "selection=no_safe_route" in explained.output
    assert (
        "no qualified system exists and this system is not an eligible provisional fallback"
        in explained.output
    )
    assert "Backend weak: configured=80.0; effective=55.0" in explained.output
    assert "provenance=bootstrap" in explained.output
    assert "confidence=low" in explained.output
    assert "empirical_status=estimated" in explained.output
    assert "retry_reason=" in explained.output
    assert "Stage budget:" in explained.output
    assert "Empirical sequence: status=bootstrap_fallback" in explained.output


def test_completed_run_exits_zero_and_prints_evidence_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _invoke_fake_run(tmp_path, monkeypatch, "COMPLETED")
    assert result.exit_code == 0
    assert "Run ID: run_protocol_fixture" in result.output
    assert "Ready to apply" in result.output
    assert "ACCEPTED" not in result.output
    assert "What changed:" in result.output
    assert "Files changed:" in result.output
    assert "calculator.py" in result.output
    assert "Checks and tests:" in result.output
    assert "Requirement coverage:" in result.output
    assert "Known cost:" in result.output
    assert "Elapsed time:" in result.output
    assert "Next action:" in result.output
    assert "Evidence:" in result.output
    assert "Terminal state:" not in result.output


def test_cli_json_equals_the_product_projection_consumed_by_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _invoke_fake_run(
        tmp_path, monkeypatch, "COMPLETED", extra_args=("--json",)
    )

    assert result.exit_code == 0, result.output
    cli_projection = json.loads(result.output)
    web_projection = unified.build_product_run(
        unified._runs_root() / "run_protocol_fixture"
    ).model_dump(mode="json")
    assert cli_projection == web_projection
    assert cli_projection["schema_version"] == "villani.product_run.v1"


def test_exhausted_run_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _invoke_fake_run(tmp_path, monkeypatch, "EXHAUSTED")
    assert result.exit_code == 3
    assert "Could not prove" in result.output
    assert "EXHAUSTED" not in result.output
    assert "sufficient recorded evidence before the safe stop" in " ".join(
        result.output.split()
    )


def test_failed_run_exits_four(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = _invoke_fake_run(tmp_path, monkeypatch, "FAILED")
    assert result.exit_code == 4
    assert "Could not prove" in result.output
    assert "FAILED" not in result.output
    assert "fake failed" in result.output


def test_evidence_command_projects_the_shared_evidence_index() -> None:
    _init()
    _copy_valid_run(unified._runs_root())

    result = runner.invoke(
        unified.app, ["evidence", "run_protocol_fixture", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "villani.evidence_index.v1"
    assert payload["run_id"] == "run_protocol_fixture"
    assert payload["evidence_links"]
    assert "events.jsonl" in payload["technical_detail_references"]


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

    result = runner.invoke(unified.app, ["inspect", "run_protocol_fixture", "--json"])

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

    def record_launch(command: list[str], check: bool = False) -> SimpleNamespace:
        launched.append(list(command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(unified.subprocess, "run", record_launch)

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


def test_open_passes_optional_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init()
    _copy_valid_run(unified._runs_root())
    launched: list[str] = []
    monkeypatch.setattr(unified, "_resolve_vfr_command", lambda: ["vfr"])

    def record_launch(command: list[str], check: bool = False) -> SimpleNamespace:
        launched.extend(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(unified.subprocess, "run", record_launch)
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
