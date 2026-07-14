from __future__ import annotations

import http.cookiejar
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from villani_agentd.config import AgentdPaths, Limits, ServerConfig, SyncConfig
from villani_agentd.console import (
    ConsoleAuthorizationError,
    ConsoleDataError,
    ConsoleInputError,
    ConsoleService,
    VfrConsoleBridge,
)
from villani_agentd.server import AgentdHTTPServer
from villani_agentd.spool import SQLiteSpool
from villani_agentd.structured_log import StructuredLogger
from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.interfaces import ClosedLoopRunRequest, ClosedLoopRunResult
from villani_ops.closed_loop.model_management import ModelDetection
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


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
VALID_RUN = REPOSITORY_ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"


class FakeBridge:
    def history(self, *, refresh: bool = False) -> dict[str, Any]:
        return {
            "schema_version": "villani.console.history.v1",
            "entries": [
                {
                    "id": "run_same",
                    "logical_id": "run_same",
                    "kind": "run",
                    "source": "villani",
                    "source_label": "Villani",
                    "provider": "villani",
                    "repository": "repo",
                    "task": "Canonical task",
                    "status": "completed",
                    "model": "model-a",
                    "started_at": "2026-07-14T00:00:00Z",
                    "updated_at": "2026-07-14T00:01:00Z",
                    "duration_ms": 60_000,
                    "cost": None,
                    "currency": None,
                    "cost_available": False,
                    "synchronization_state": "LOCAL",
                    "deep_link": "/console/runs/run_same",
                },
                {
                    "id": "claude_1",
                    "logical_id": "claude_1",
                    "kind": "session",
                    "source": "claude",
                    "source_label": "Claude Code",
                    "provider": "claude",
                    "repository": "repo",
                    "task": "Imported task",
                    "status": "success",
                    "model": "claude-model",
                    "started_at": "2026-07-13T00:00:00Z",
                    "updated_at": "2026-07-13T00:01:00Z",
                    "duration_ms": 60_000,
                    "cost": None,
                    "currency": None,
                    "cost_available": False,
                    "synchronization_state": "LOCAL",
                    "deep_link": "/console/sessions/claude_1",
                },
            ],
            "warnings": [],
            "refreshed": refresh,
        }

    def replay(self, record_id: str, kind: str) -> dict[str, Any]:
        return {
            "schema_version": "villani.console.replay.v1",
            "id": record_id,
            "logical_id": record_id,
            "kind": kind,
            "source": "villani" if kind == "run" else "claude",
            "source_label": "Villani" if kind == "run" else "Claude Code",
            "provider": "villani" if kind == "run" else "claude",
            "synchronization_state": "LOCAL",
            "summary": {},
            "events": [],
            "attempts": [],
            "evidence": {},
            "verification": {},
            "candidate_comparison": [],
            "files": [],
            "artifacts": [],
            "cost": {},
            "logs": [],
            "canonical": None,
            "warnings": [],
            "deep_links": {"self": f"/console/{kind}s/{record_id}"},
        }


class FakeRunController:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def run(self, request: Any) -> ClosedLoopRunResult:
        self.requests.append(request)
        destination = Path(request.runs_root) / request.run_id
        shutil.copytree(VALID_RUN, destination)
        for path in destination.rglob("*.json"):
            path.write_text(
                path.read_text(encoding="utf-8").replace("run_protocol_fixture", request.run_id),
                encoding="utf-8",
            )
        events = destination / "events.jsonl"
        events.write_text(
            events.read_text(encoding="utf-8").replace("run_protocol_fixture", request.run_id),
            encoding="utf-8",
        )
        return ClosedLoopRunResult(
            run_id=request.run_id,
            terminal_state="COMPLETED",
            selected_attempt_id="attempt_002",
            run_directory=destination,
            actual_known_cost_usd=0.05,
            accounting_status="complete",
            failure_or_exhaustion_reason=None,
        )


def _approval_controller(_configuration: Any = None, _events: Any = None) -> ClosedLoopController:
    option = backend("fixture")
    return ClosedLoopController(
        classifier=FakeClassifier(),
        policy_engine=FakePolicyEngine(
            [policy("attempt", backend_option=option), policy("select")]
        ),
        attempt_runner=FakeAttemptRunner([attempt()]),
        verifier=FakeVerifier([accepted_verification()]),
        selector=FakeSelector(),
        materializer=FakeMaterializer(),
        now=FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
    )


def _awaiting_approval_run(home: Path, *, authenticated_required: bool) -> ClosedLoopRunResult:
    configuration = {
        "version": "fake_v1",
        "collect_candidates": 1,
        "delivery": {
            "workflow_version": "villani.delivery_workflow.v1",
            "mode": "approve",
            "materialization_type": "local_patch_apply",
            "approval": {
                "timeout_seconds": 86_400,
                "timeout_policy": "reject",
                "authenticated_required": authenticated_required,
            },
        },
    }
    return _approval_controller().run(
        ClosedLoopRunRequest(
            task="Review the accepted fixture patch.",
            repository_path=home / "repository",
            success_criteria="The fake evidence is accepted.",
            runs_root=home / "runs",
            max_attempts=1,
            policy_configuration=configuration,
            run_id="run_approval_fixture",
        )
    )


def _git_repository(path: Path) -> Path:
    path.mkdir()
    for arguments in (
        ("init", "-q"),
        ("config", "user.email", "tests@example.invalid"),
        ("config", "user.name", "Villani tests"),
    ):
        result = subprocess.run(["git", *arguments], cwd=path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    (path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    subprocess.run(["git", "add", "package.json"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return path


@pytest.fixture
def console(tmp_path: Path) -> tuple[ConsoleService, SQLiteSpool, AgentdPaths]:
    paths = AgentdPaths(tmp_path / "home" / "agentd")
    spool = SQLiteSpool(paths, Limits())
    return ConsoleService(paths, spool, bridge=FakeBridge()), spool, paths


def test_merged_history_has_no_duplicate_logical_run(console) -> None:
    service, spool, _paths = console
    spool.register_run("run_same", "trace", "2026-07-14T00:00:00Z")
    document = service.history()
    assert document["schema_version"] == "villani.console.history.v1"
    assert [item["logical_id"] for item in document["entries"]].count("run_same") == 1
    assert {item["source"] for item in document["entries"]} == {"villani", "claude"}
    run = next(item for item in document["entries"] if item["logical_id"] == "run_same")
    assert run["task"] == "Canonical task"
    assert run["synchronization_state"] == "LOCAL"
    assert "sourcePath" not in json.dumps(document)


def test_sync_states_are_authoritative_and_replay_uses_same_state(console) -> None:
    service, spool, paths = console
    spool.register_run("run_same", "trace", "2026-07-14T00:00:00Z")
    SyncConfig("https://workspace.invalid", "workspace_1").save(paths.sync_config)
    assert spool.console_run_states(True)["run_same"] == "SYNC PENDING"
    with sqlite3.connect(paths.database) as connection:
        connection.execute("UPDATE runs SET upload_state='acknowledged' WHERE run_id='run_same'")
    assert spool.console_run_states(True)["run_same"] == "SYNCHRONIZED"
    assert service.replay("run_same", "run")["synchronization_state"] == "SYNCHRONIZED"
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            "UPDATE runs SET final_payload_json=? WHERE run_id='run_same'",
            ('{"villani_redaction":{"status":"redacted"}}',),
        )
    assert spool.console_run_states(True)["run_same"] == "REDACTED"
    with sqlite3.connect(paths.database) as connection:
        connection.execute("UPDATE runs SET upload_state='dead_letter' WHERE run_id='run_same'")
    assert spool.console_run_states(True)["run_same"] == "SYNC FAILED"
    fleet = service.workspace("fleet")
    assert fleet["connected"] is True
    assert fleet["items"] == [
        {
            "id": "run_same",
            "status": "SYNC FAILED",
            "summary": "Canonical task",
            "detail": "repo",
            "deep_link": "/console/runs/run_same",
        }
    ]
    costs = service.workspace("costs")
    assert costs["items"][0]["status"] == "UNKNOWN"
    assert costs["items"][0]["cost"] is None


def test_bootstrap_is_secret_safe_and_team_navigation_contract_is_enrolment_gated(
    console,
) -> None:
    service, _spool, paths = console
    home = paths.root.parent
    (home / "config.yaml").write_text(
        """config_version: 1
backends:
  default:
    provider: openai-compatible
    base_url: http://127.0.0.1:1234/v1
    model: local-model
    api_key_env: TOP_SECRET_ENV
    metadata:
      capability_status: unrated
policy:
  version: bootstrap_v1
""",
        encoding="utf-8",
    )
    local = service.bootstrap()
    assert local["mode"] == "local" and local["workspace"]["connected"] is False
    assert local["models"][0]["capability"] == "UNRATED"
    assert "TOP_SECRET_ENV" not in json.dumps(local)
    SyncConfig("https://workspace.invalid", "workspace_1").save(paths.sync_config)
    connected = service.bootstrap()
    assert connected["mode"] == "connected"
    assert connected["workspace"]["connected"] is True


def test_run_options_and_validation_discovery_are_repository_backed(console, tmp_path) -> None:
    service, _spool, paths = console
    repository = _git_repository(tmp_path / "repo")
    (paths.root.parent / "config.yaml").write_text(
        f"""config_version: 1
setup:
  repository: {json.dumps(str(repository))}
backends:
  default:
    provider: local
    model: fixture
policy:
  version: bootstrap_v1
""",
        encoding="utf-8",
    )

    options = service.run_options()
    discovery = service.validation_discovery(str(repository))

    assert options["default_repository"] == str(repository.resolve())
    assert options["repositories"][0]["valid"] is True
    assert {item["id"] for item in options["delivery_modes"]} == {
        "suggest",
        "approve",
        "apply",
        "branch",
        "pull-request",
    }
    assert [item["label"] for item in options["policy_presets"]] == [
        "Reliable",
        "Balanced",
        "Local first",
        "Cheapest acceptable",
        "Custom",
    ]
    assert options["defaults"]["policy_preset"] == "balanced"
    assert discovery["suggestions"][0]["argv"] == ["npm", "test"]
    assert discovery["suggestions"][0]["authoritative"] is False
    assert discovery["authority"] == "none_until_confirmed_command_execution"


def test_console_model_operations_share_unrated_lifecycle(
    console, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _spool, paths = console
    home = paths.root.parent
    (home / "config.yaml").write_text(
        """config_version: 1
public_policy:
  preset: balanced
model_management:
  bootstrap_default:
policy:
  version: bootstrap_v1
capabilities:
  minimum_empirical_samples: 5
  minimum_empirical_wilson_lower_bound: 0.5
backends:
  existing:
    provider: local
    base_url: http://127.0.0.1:1234/v1
    model: existing-model
    roles: [classification, coding]
    capability_score_source: unrated
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "villani_agentd.console.detect_models",
        lambda _configuration, timeout: (
            ModelDetection(
                detector="fixture",
                provider="local",
                provider_display_name="Fixture local",
                endpoint="http://127.0.0.1:1234/v1",
                availability="available",
                models=("existing-model", "detected-model"),
                tool_support=True,
                context_metadata={"detected-model": {"context_window": 32768}},
                detected_at="2026-07-14T00:00:00Z",
                diagnostic="Two models found.",
            ),
        ),
    )
    monkeypatch.setattr(
        "villani_agentd.console.test_models",
        lambda _configuration, state, backend_names, timeout: (
            state,
            [
                {
                    "backend_name": backend_names[0],
                    "availability": "available",
                    "diagnostic": "Fixture is available.",
                    "tested_at": "2026-07-14T00:01:00Z",
                    "model_tokens_used": 0,
                }
            ],
        ),
    )

    detected = service.models_detect({})
    added = service.models_add(
        {
            "backend_name": "new-model",
            "model": "detected-model",
            "provider": "local",
            "endpoint": "http://127.0.0.1:1234/v1",
            "make_default": False,
        }
    )
    selected = service.models_default({"backend_name": "new-model"})
    tested = service.models_test({"backend_name": "new-model"})

    assert detected["discovery_authority"] == "advisory"
    assert any(item["configured"] is False for item in detected["models"])
    configured = next(item for item in added["models"] if item["backend_name"] == "new-model")
    assert configured["capability_status"] == "UNRATED"
    assert configured["pricing_status"] == "unknown"
    default = next(item for item in selected["models"] if item["backend_name"] == "new-model")
    assert default["capability_status"] == "BOOTSTRAP"
    assert default["bootstrap_default"] is True
    assert tested["model_tokens_used"] == 0
    assert tested["results"][0]["model_tokens_used"] == 0

    removed = service.models_remove({"backend_name": "new-model"})
    assert all(item["backend_name"] != "new-model" for item in removed["models"])


def test_console_policy_selection_preview_and_simulation_are_public_and_read_only(
    console, tmp_path
) -> None:
    _original, spool, paths = console
    repository = _git_repository(tmp_path / "repo-policy")
    home = paths.root.parent
    (home / "config.yaml").write_text(
        """config_version: 1
public_policy:
  preset: balanced
policy:
  version: bootstrap_v1
backends:
  default:
    provider: local
    base_url: http://127.0.0.1:1234/v1
    model: fixture
    roles: [classification, coding]
    capability_score_source: unrated
""",
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def preview(**values: Any) -> dict[str, Any]:
        calls.append(values)
        return {
            "schema_version": "villani.policy_preview.v1",
            "raw_classification": {"difficulty": "easy", "risk": "low"},
            "effective_classification": {"difficulty": "medium", "risk": "low"},
            "adjustments": [{"field": "difficulty"}],
            "eligible_models": [{"backend_name": "default"}],
            "excluded_models": [],
            "selected_coding_route": {"backend": "default"},
            "selected_verifier_route": {"selected": {"route": "deterministic"}},
            "estimated_cost": {"value": None, "status": "unknown"},
            "uncertainty": ["Selected-route cost is unknown."],
            "policy_version": {"public": "villani-public-policy-v1"},
            "coding_attempt_executed": False,
        }

    service = ConsoleService(
        paths,
        spool,
        bridge=FakeBridge(),
        policy_preview_builder=preview,
    )
    selected = service.policy_select({"preset": "Reliable"})
    explanation = service.policy_preview(
        {
            "repository": str(repository),
            "task": "Fix parser",
            "preset": "local-first",
        }
    )
    simulation = service.policy_simulation({"preset": "cheapest-acceptable"})

    assert selected["active_preset"] == "reliable"
    assert [item["label"] for item in selected["presets"]] == [
        "Reliable",
        "Balanced",
        "Local first",
        "Cheapest acceptable",
        "Custom",
    ]
    assert explanation["coding_attempt_executed"] is False
    assert calls[0]["preset"] == "local-first"
    assert simulation["live_policy_changed"] is False
    assert simulation["causal_savings_supported"] is False
    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert saved["public_policy"]["preset"] == "reliable"
    assert "accepted_candidates_required" not in saved["policy"]


def test_console_submission_queues_canonical_run_with_confirmed_validation(
    console, tmp_path
) -> None:
    _original, spool, paths = console
    repository = _git_repository(tmp_path / "repo")
    (paths.root.parent / "config.yaml").write_text(
        """config_version: 1
backends:
  default:
    provider: local
    model: fixture
    roles: [classification, coding]
    capability_score: 50
policy:
  version: bootstrap_v1
budgets:
  max_attempts: 3
""",
        encoding="utf-8",
    )
    controller = FakeRunController()
    service = ConsoleService(
        paths,
        spool,
        bridge=FakeBridge(),
        controller_builder=lambda _configuration, _events: controller,
    )

    submission = service.start_run(
        {
            "repository": str(repository),
            "task": "Fix the parser.",
            "success_criteria": "The repository test passes.",
            "validation_argv": ["npm", "test"],
            "delivery_mode": "patch",
            "approval_mode": "review",
            "max_cost": "1.25",
            "max_wall_time": "30",
            "policy_selection": "configured",
            "policy_preset": "local-first",
            "routing_mode": "observe",
        }
    )
    deadline = time.monotonic() + 5
    while not controller.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    while submission["run_id"] in service._run_threads and time.monotonic() < deadline:
        time.sleep(0.01)

    assert submission["status"] == "QUEUED"
    assert submission["validation_commands"] == ["npm test"]
    request = controller.requests[0]
    assert request.run_id == submission["run_id"]
    assert request.max_cost == 1.25
    assert request.max_wall_time == 30
    command = request.policy_configuration["repository_validation_commands"][0]
    assert command["argv"] == ["npm", "test"]
    assert command["authoritative"] is False
    assert request.policy_configuration["delivery"]["materialization_type"] == "patch_export"
    assert request.policy_configuration["delivery"]["mode"] == "suggest"
    assert request.policy_configuration["delivery"]["workflow_version"] == (
        "villani.delivery_workflow.v1"
    )
    assert request.policy_configuration["public_policy"]["preset"] == "local-first"
    assert request.policy_configuration["run_experience"]["policy_preset"] == "local-first"
    status = service.run_status(submission["run_id"])
    while status["outcome"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.01)
        status = service.run_status(submission["run_id"])
    assert status["outcome"] == "ACCEPTED"
    assert status["run_id"] == submission["run_id"]
    with pytest.raises(ConsoleInputError, match="requires_file_changes"):
        service.start_run(
            {
                "repository": str(repository),
                "task": "Fix the parser.",
                "validation_argv": ["npm", "test"],
                "requires_file_changes": "false",
            }
        )


def test_console_submission_explains_missing_validation(console, tmp_path) -> None:
    service, _spool, paths = console
    repository = _git_repository(tmp_path / "repo")
    (repository / "package.json").unlink()
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "remove validation metadata"],
        cwd=repository,
        check=True,
    )
    (paths.root.parent / "config.yaml").write_text(
        """config_version: 1
backends:
  default:
    provider: local
    model: fixture
    roles: [classification, coding]
""",
        encoding="utf-8",
    )

    submission = service.start_run({"repository": str(repository), "task": "Fix the parser."})

    assert submission["status"] == "FAILED"
    assert submission["failure"]["code"] == "no_validation_command"
    assert "authoritative" in submission["failure"]["missing_evidence"]


def test_console_approval_survives_service_restart(console) -> None:
    _service, spool, paths = console
    home = paths.root.parent
    initial = _awaiting_approval_run(home, authenticated_required=False)
    assert initial.terminal_state == "AWAITING_APPROVAL"

    restarted = ConsoleService(
        paths,
        spool,
        bridge=FakeBridge(),
        controller_builder=_approval_controller,
    )
    before = restarted.run_status(initial.run_id)
    after = restarted.approval_action(
        initial.run_id,
        {"action": "approve", "reason": "Reviewed in Console."},
        authenticated=False,
        actor="local-console-user",
        authentication_type="local_console",
    )

    assert before["outcome"] == "AWAITING APPROVAL"
    assert before["delivery"]["review"]["files_changed"] == ["example.txt"]
    assert after["outcome"] == "ACCEPTED"
    assert after["delivery"]["state"] == "applied"
    assert (initial.run_directory / "approval-audit.jsonl").is_file()


def test_console_connected_approval_requires_authenticated_session(console) -> None:
    _service, spool, paths = console
    home = paths.root.parent
    SyncConfig("https://workspace.invalid", "workspace_approval").save(paths.sync_config)
    initial = _awaiting_approval_run(home, authenticated_required=True)
    restarted = ConsoleService(
        paths,
        spool,
        bridge=FakeBridge(),
        controller_builder=_approval_controller,
    )

    with pytest.raises(ConsoleAuthorizationError, match="authenticated Console"):
        restarted.approval_action(
            initial.run_id,
            {"action": "approve"},
            authenticated=False,
            actor="anonymous",
            authentication_type="none",
        )

    assert restarted.run_status(initial.run_id)["outcome"] == "AWAITING APPROVAL"


def test_console_http_approval_uses_authenticated_session(console) -> None:
    _service, spool, paths = console
    home = paths.root.parent
    SyncConfig("https://workspace.invalid", "workspace_approval").save(paths.sync_config)
    initial = _awaiting_approval_run(home, authenticated_required=True)
    service = ConsoleService(
        paths,
        spool,
        bridge=FakeBridge(),
        controller_builder=_approval_controller,
    )
    server = AgentdHTTPServer(
        ("127.0.0.1", 0),
        "test-token",
        spool,
        ServerConfig(),
        StructuredLogger(paths.log),
        service,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        jar = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)
        )
        with browser.open(f"{endpoint}/console/history"):
            pass
        request = urllib.request.Request(
            f"{endpoint}/v1/console/runs/{initial.run_id}/approval",
            data=json.dumps(
                {"action": "approve", "reason": "Reviewed in connected Console."}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with browser.open(request) as response:
            payload = json.loads(response.read())

        assert payload["outcome"] == "ACCEPTED"
        delivery = json.loads((initial.run_directory / "delivery.json").read_text(encoding="utf-8"))
        assert delivery["approval"]["authentication_type"] == ("agentd_authenticated_session")
        assert delivery["approval"]["actor"] == ("connected-console:workspace_approval")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_console_cookie_authenticates_only_console_gets(console) -> None:
    service, spool, paths = console
    server = AgentdHTTPServer(
        ("127.0.0.1", 0),
        "test-token",
        spool,
        ServerConfig(),
        StructuredLogger(paths.log),
        service,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        jar = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)
        )
        with browser.open(f"{endpoint}/console/history") as response:
            assert "Villani Console" in response.read().decode("utf-8")
        with browser.open(f"{endpoint}/v1/console/bootstrap") as response:
            assert json.loads(response.read())["data_source"] == "local-service"
        with pytest.raises(urllib.error.HTTPError) as caught:
            browser.open(f"{endpoint}/v1/status")
        assert caught.value.code == 401
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as caught:
            direct.open(f"{endpoint}/v1/console/history")
        assert caught.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_packaged_console_references_existing_assets() -> None:
    root = Path(__file__).parents[1] / "villani_agentd" / "console_assets"
    html = (root / "index.html").read_text(encoding="utf-8")
    manifest = json.loads((root / "console-assets.json").read_text(encoding="utf-8"))
    assert "Villani Console" in html
    assert all((root / name).is_file() for name in manifest["files"])


def test_replay_bridge_redacts_child_process_diagnostics(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import sys\nsys.stderr.write('api_key=super-secret-value')\nraise SystemExit(7)\n",
        encoding="utf-8",
    )
    bridge = VfrConsoleBridge(command=[sys.executable, str(adapter)])
    with pytest.raises(ConsoleDataError) as caught:
        bridge.history()
    assert "super-secret-value" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
