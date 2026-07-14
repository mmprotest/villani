from __future__ import annotations

import json
import sys
import threading
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest
import yaml
from typer.testing import CliRunner

from villani_distribution import cli, onboarding, services
from villani_distribution.cli import app
from villani_distribution.onboarding import (
    BackendProbe,
    ProviderDetection,
    SetupError,
    build_configuration,
    create_sample_repository,
    detect_providers,
    load_configuration,
    run_sample_task,
    validate_configuration,
    write_configuration_atomic,
    write_setup_record,
)
from villani_distribution.services import ServiceStatus


@contextmanager
def model_server(
    models: tuple[str, ...] = ("fixture-coder",), *, expected_key: str | None = None
) -> Iterator[tuple[str, list[dict[str, object]]]]:
    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send(self, value: object, status: int = 200) -> None:
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            return (
                expected_key is None
                or self.headers.get("Authorization") == f"Bearer {expected_key}"
            )

        def do_GET(self) -> None:  # noqa: N802
            requests.append(
                {
                    "method": "GET",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            if not self._authorized():
                self._send({"error": "unauthorized"}, 401)
                return
            if self.path == "/v1/models":
                self._send(
                    {
                        "data": [
                            {"id": name, "context_window": 32_768, "owned_by": "fixture"}
                            for name in models
                        ]
                    }
                )
                return
            self._send({"error": "not_found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length))
            requests.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": value,
                }
            )
            if not self._authorized():
                self._send({"error": "unauthorized"}, 401)
                return
            self._send({"choices": [{"message": {"role": "assistant", "content": "READY"}}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def detection(endpoint: str = "http://127.0.0.1:1234/v1") -> ProviderDetection:
    return ProviderDetection(
        "lm-studio",
        "LM Studio",
        endpoint,
        "connected",
        ("fixture-coder", "fixture-coder-2"),
        False,
        None,
        {"fixture-coder": {"context_window": 32_768}},
        None,
        "LM Studio is reachable with 2 model(s).",
    )


def stopped_status(home: Path) -> ServiceStatus:
    return ServiceStatus(
        "win32",
        False,
        str(home / "service" / "windows-task.json"),
        False,
        running=False,
        log_path=str(home / "agentd" / "agentd.log"),
    )


def _patch_setup(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("VILLANI_HOME", str(home))
    monkeypatch.setattr(cli, "detect_repository", lambda: None)
    monkeypatch.setattr(cli, "detect_providers", lambda **_kwargs: (detection(),))
    monkeypatch.setattr(cli, "detect_session_sources", lambda: ())
    monkeypatch.setattr(cli, "service_status", lambda: stopped_status(home))
    monkeypatch.setattr(
        cli,
        "test_backend",
        lambda *_args, **_kwargs: BackendProbe(
            True, "connection", "Model is available.", "fixture-coder"
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_capability_probe",
        lambda *_args, **_kwargs: BackendProbe(
            True,
            "capability",
            "Small non-destructive capability probe passed; the model remains unrated.",
            "fixture-coder",
            0.01,
        ),
    )


def test_clean_first_time_setup_creates_runnable_unrated_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _patch_setup(monkeypatch, home)
    result = CliRunner().invoke(app, ["setup", "--yes", "--no-start", "--no-open", "--no-sample"])
    assert result.exit_code == 0, result.output
    configuration = load_configuration(home / "config.yaml")
    parsed = validate_configuration(configuration)
    assert configuration["config_version"] == 1
    assert configuration["policy"]["hard_min_capability"] == 0
    assert parsed["default"].capability_score_source == "unrated"
    assert parsed["default"].metadata["capability_status"] == "unrated"
    assert parsed["default"].billing_mode == "unknown"
    assert "capability score" not in result.output.lower()


def test_existing_configuration_is_preserved_when_user_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _patch_setup(monkeypatch, home)
    config = home / "config.yaml"
    write_configuration_atomic(
        config, build_configuration(detection(), "fixture-coder", repository=None)
    )
    before = config.read_bytes()
    result = CliRunner().invoke(app, ["setup"], input="n\n")
    assert result.exit_code == 0
    assert config.read_bytes() == before
    assert "not changed" in result.output


def test_user_can_decline_every_optional_setup_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _patch_setup(monkeypatch, home)
    started: list[bool] = []
    monkeypatch.setattr(cli, "start_service", lambda **_kwargs: started.append(True))
    result = CliRunner().invoke(app, ["setup"], input="\nn\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert started == []
    assert (home / "config.yaml").is_file()
    record = json.loads((home / "setup-record.json").read_text(encoding="utf-8"))
    assert record["console_url"] is None
    assert record["sample"] is None


def test_atomic_write_backs_up_existing_configuration(tmp_path: Path) -> None:
    path = tmp_path / "home" / "config.yaml"
    first = build_configuration(detection(), "fixture-coder", repository=None)
    second = build_configuration(detection(), "fixture-coder-2", repository=None)
    write_configuration_atomic(path, first)
    result = write_configuration_atomic(path, second)
    assert result.backup_path is not None
    backup = Path(result.backup_path)
    assert load_configuration(backup)["backends"]["default"]["model"] == "fixture-coder"
    assert load_configuration(path)["backends"]["default"]["model"] == "fixture-coder-2"


def test_interrupted_configuration_activation_preserves_previous_file(tmp_path: Path) -> None:
    path = tmp_path / "home" / "config.yaml"
    write_configuration_atomic(
        path, build_configuration(detection(), "fixture-coder", repository=None)
    )
    previous = path.read_bytes()

    def interrupted(_source: object, _target: object) -> None:
        raise OSError("simulated interruption")

    with pytest.raises(OSError, match="simulated interruption"):
        write_configuration_atomic(
            path,
            build_configuration(detection(), "fixture-coder-2", repository=None),
            replace=interrupted,
        )
    assert path.read_bytes() == previous
    assert list(path.parent.glob(".config.yaml.*.tmp")) == []


def test_invalid_endpoint_is_rejected_before_network_access() -> None:
    with pytest.raises(SetupError, match="http or https"):
        detect_providers(explicit_endpoint="not-an-endpoint", timeout=0.01)
    with pytest.raises(SetupError, match="must not contain credentials"):
        detect_providers(explicit_endpoint="http://secret@example.test/v1", timeout=0.01)


def test_multiple_detected_providers_have_structured_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with model_server(("alpha",)) as (first, _), model_server(("beta",)) as (second, _):
        monkeypatch.setattr(
            onboarding,
            "_LOCAL_PROVIDERS",
            (("lm-studio", "LM Studio", second),),
        )
        results = detect_providers(explicit_endpoint=first, environ={}, timeout=1)
    connected = [item for item in results if item.connection_status == "connected"]
    assert len(connected) == 2
    assert {item.available_models[0] for item in connected} == {"alpha", "beta"}
    for item in connected:
        assert set(item.as_dict()) >= {
            "provider_identifier",
            "detected_endpoint",
            "connection_status",
            "available_models",
            "authentication_required",
            "tool_use_support",
            "context_metadata",
            "pricing_metadata_source",
            "diagnostic_message",
        }


def test_no_providers_detected_is_nonfatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "_LOCAL_PROVIDERS", ())
    results = detect_providers(environ={}, timeout=0.01)
    assert len(results) == 1
    assert results[0].provider_identifier == "openai"
    assert results[0].connection_status == "credential_missing"
    assert onboarding.recommend_backend(results) is None


def test_cloud_provider_missing_credential_never_attempts_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(onboarding, "_LOCAL_PROVIDERS", ())
    result = detect_providers(environ={}, timeout=0.01)[0]
    assert result.authentication_required is True
    assert result.credential_environment_variable == "OPENAI_API_KEY"
    assert result.credential_status == "missing"
    assert "OPENAI_API_KEY" in result.diagnostic_message


def test_authenticated_compatible_endpoint_reports_missing_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with model_server(expected_key="fixture-secret") as (endpoint, _requests):
        monkeypatch.setattr(onboarding, "_LOCAL_PROVIDERS", ())
        result = detect_providers(
            explicit_endpoint=endpoint,
            environ={"VILLANI_MODEL_API_KEY_ENV": "MODEL_KEY"},
            timeout=1,
        )[0]
    assert result.connection_status == "credential_missing"
    assert result.authentication_required is True
    assert result.credential_environment_variable == "MODEL_KEY"
    assert result.credential_status == "missing"
    assert result.available_models == ()


def test_ollama_backend_test_uses_native_model_listing_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "http://127.0.0.1:11434/v1"

    def request(method: str, url: str, **_kwargs: object) -> tuple[int, object]:
        assert method == "GET"
        if url.endswith("/models"):
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        assert url.endswith("/api/tags")
        return 200, {"models": [{"name": "ollama-fixture"}]}

    monkeypatch.setattr(onboarding, "_request_json", request)
    selected = ProviderDetection(
        "ollama",
        "Ollama",
        endpoint,
        "connected",
        ("ollama-fixture",),
    )
    probe = onboarding.test_backend(selected, "ollama-fixture")
    assert probe.succeeded is True


def test_secret_values_are_used_but_never_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "fixture-provider-secret-never-print-91c4"
    with model_server(expected_key=secret) as (endpoint, requests):
        monkeypatch.setattr(onboarding, "_LOCAL_PROVIDERS", ())
        results = detect_providers(
            explicit_endpoint=endpoint,
            environ={"VILLANI_MODEL_API_KEY_ENV": "MODEL_KEY", "MODEL_KEY": secret},
            timeout=1,
        )
    selected = next(item for item in results if item.provider_identifier == "openai-compatible")
    assert requests[0]["authorization"] == f"Bearer {secret}"
    serialized = json.dumps(selected.as_dict(), sort_keys=True)
    assert secret not in serialized
    path = tmp_path / "setup-record.json"
    write_setup_record(path, {"provider": selected.as_dict()})
    assert secret not in path.read_text(encoding="utf-8")


def test_unsupported_future_configuration_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("config_version: 99\nbackends: {}\n", encoding="utf-8")
    with pytest.raises(SetupError, match="newer than supported"):
        validate_configuration(load_configuration(path))


@pytest.mark.parametrize(
    ("platform", "expected_parts"),
    [
        ("win32", ("TaskScheduler", "VillaniAgentd.json")),
        ("linux", ("systemd", "user", "villani-agentd.service")),
        ("darwin", ("LaunchAgents", "com.villani.agentd.plist")),
    ],
)
def test_windows_and_posix_service_paths_are_user_scoped(
    platform: str, expected_parts: tuple[str, ...], tmp_path: Path
) -> None:
    test_root = (tmp_path / "root").resolve()
    env = {
        "VILLANI_HOME": str(tmp_path / "home"),
        "VILLANI_SERVICE_PLATFORM": platform,
        "VILLANI_SERVICE_TEST_ROOT": str(test_root),
    }
    path = services._definition(platform, env).resolve()
    assert path.is_relative_to(test_root)
    assert path.relative_to(test_root).parts == expected_parts
    assert ".." not in path.relative_to(test_root).parts


def test_native_service_definition_is_user_scoped() -> None:
    platform = sys.platform
    assert platform in {"linux", "darwin", "win32"}
    path = services._definition(platform, {}).expanduser().resolve()
    assert path.is_relative_to(Path.home().resolve())


def test_sample_repository_is_temporary_git_repo_and_runner_is_bounded(tmp_path: Path) -> None:
    sample = create_sample_repository(root=tmp_path)
    path = Path(sample.path)
    assert path.parent == tmp_path
    assert (path / ".git").is_dir()
    assert (path / "calculator.py").is_file()
    calls: list[list[str]] = []
    exit_code = run_sample_task(sample, runner=lambda command: calls.append(list(command)) or 0)
    assert exit_code == 0
    assert calls[0][-2:] == ["--max-attempts", "1"]
    assert sample.task in calls[0]


def test_reset_requires_explicit_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _patch_setup(monkeypatch, home)
    path = home / "config.yaml"
    write_configuration_atomic(
        path, build_configuration(detection(), "fixture-coder", repository=None)
    )
    original = path.read_bytes()
    result = CliRunner().invoke(app, ["setup", "--reset"], input="n\n")
    assert result.exit_code == 0
    assert path.read_bytes() == original


def test_generated_configuration_contains_no_direct_secret_fields(tmp_path: Path) -> None:
    cloud = ProviderDetection(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "connected",
        ("gpt-fixture",),
        True,
        True,
        {},
        None,
        "OpenAI is reachable.",
        "OPENAI_API_KEY",
        "present",
    )
    configuration = build_configuration(cloud, "gpt-fixture", repository=None)
    backend = configuration["backends"]["default"]
    assert backend["api_key_env"] == "OPENAI_API_KEY"
    assert "api_key" not in backend
    assert "input_cost_per_million" not in backend
    assert "output_cost_per_million" not in backend
    assert yaml.safe_dump(configuration).find("sk-") == -1

    # Configuration validity is structural. The referenced secret is resolved
    # only by a probe or execution preflight.
    parsed = validate_configuration(configuration)
    assert parsed["default"].credential_reference_configured() is True
    assert parsed["default"].runtime_credential_available({}) is False

    path = tmp_path / "config.yaml"
    write_configuration_atomic(path, configuration)
    serialized = path.read_text(encoding="utf-8")
    assert "api_key_env: OPENAI_API_KEY" in serialized
    assert "fixture-provider-secret" not in serialized


def test_setup_configuration_rejects_direct_and_redacted_credentials() -> None:
    configuration = build_configuration(detection(), "fixture-coder", repository=None)
    configuration["backends"]["default"]["provider"] = "openai"
    configuration["backends"]["default"]["base_url"] = "https://api.openai.com/v1"
    configuration["backends"]["default"]["api_key"] = "fixture-provider-secret"
    with pytest.raises(SetupError, match="reference credentials by environment variable"):
        validate_configuration(configuration)

    configuration["backends"]["default"]["api_key"] = "***REDACTED***"
    with pytest.raises(SetupError, match="credential reference"):
        validate_configuration(configuration)


def test_authenticated_probes_resolve_secret_only_at_runtime() -> None:
    secret = "fixture-provider-secret-never-print-28e7"
    with model_server(expected_key=secret) as (endpoint, requests):
        selected = ProviderDetection(
            "openai-compatible",
            "Fixture",
            endpoint,
            "connected",
            ("fixture-coder",),
            True,
            True,
            {},
            None,
            "Fixture endpoint is reachable.",
            "MODEL_KEY",
            "present",
        )
        missing = onboarding.test_backend(selected, "fixture-coder", environ={}, timeout=1)
        empty = onboarding.test_backend(
            selected, "fixture-coder", environ={"MODEL_KEY": "   "}, timeout=1
        )
        present = onboarding.test_backend(
            selected,
            "fixture-coder",
            environ={"MODEL_KEY": secret},
            timeout=1,
        )
        capability = onboarding.run_capability_probe(
            selected,
            "fixture-coder",
            environ={"MODEL_KEY": secret},
            timeout=1,
        )
    assert missing.stage == "credential"
    assert empty.stage == "credential"
    assert present.succeeded is True
    assert capability.succeeded is True
    assert any(item["authorization"] == f"Bearer {secret}" for item in requests)
    assert secret not in json.dumps(
        [missing.as_dict(), empty.as_dict(), present.as_dict(), capability.as_dict()]
    )


def test_rejected_runtime_credential_is_reported_without_secret() -> None:
    secret = "fixture-provider-secret-rejected-c77d"
    with model_server(expected_key="different-fixture-secret") as (endpoint, _requests):
        selected = ProviderDetection(
            "openai-compatible",
            "Fixture",
            endpoint,
            "connected",
            ("fixture-coder",),
            True,
            True,
            {},
            None,
            "Fixture endpoint is reachable.",
            "MODEL_KEY",
            "present",
        )
        result = onboarding.test_backend(
            selected,
            "fixture-coder",
            environ={"MODEL_KEY": secret},
            timeout=1,
        )
    assert result.succeeded is False
    assert "HTTP 401" in result.diagnostic_message
    assert secret not in json.dumps(result.as_dict())
