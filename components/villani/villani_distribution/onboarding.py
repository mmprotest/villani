"""Guided, secret-safe onboarding primitives for the public Villani CLI."""

from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml
from pydantic import ValidationError

from villani_ops.cli.unified import DEFAULT_CONFIG
from villani_ops.core.backend import Backend
from villani_ops.providers import ProviderConfigurationError, validate_closed_loop_backend

from .migrations import SUPPORTED_CONFIG_VERSION


class SetupError(RuntimeError):
    """A recoverable setup/configuration problem safe to show to a user."""


@dataclass(frozen=True, slots=True)
class ProviderDetection:
    provider_identifier: str
    display_name: str
    detected_endpoint: str
    connection_status: str
    available_models: tuple[str, ...] = ()
    authentication_required: bool = False
    tool_use_support: bool | None = None
    context_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    pricing_metadata_source: str | None = None
    diagnostic_message: str = ""
    credential_environment_variable: str | None = None
    credential_status: str = "not_required"

    @property
    def usable(self) -> bool:
        return self.connection_status == "connected" and bool(self.available_models)

    def as_dict(self) -> dict[str, Any]:
        # This object deliberately contains only credential metadata, never a value.
        return asdict(self) | {
            "available_models": list(self.available_models),
            "usable": self.usable,
        }


@dataclass(frozen=True, slots=True)
class SessionSourceDetection:
    source_identifier: str
    installed: bool
    path: str
    session_count: int
    diagnostic_message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BackendProbe:
    succeeded: bool
    stage: str
    diagnostic_message: str
    model: str
    elapsed_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ConfigurationWrite:
    path: str
    backup_path: str | None
    schema_version: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SampleRepository:
    path: str
    task: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_LOCAL_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("lm-studio", "LM Studio", "http://127.0.0.1:1234/v1"),
    ("ollama", "Ollama", "http://127.0.0.1:11434/v1"),
    ("llama-cpp", "llama.cpp", "http://127.0.0.1:8080/v1"),
    ("vllm", "vLLM", "http://127.0.0.1:8000/v1"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_endpoint(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SetupError("model endpoint must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise SetupError("model endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise SetupError("model endpoint must not contain a query or fragment")
    return candidate


def _opener(endpoint: str) -> urllib.request.OpenerDirector:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.hostname in {"127.0.0.1", "::1", "localhost"}:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _request_json(
    method: str,
    url: str,
    *,
    api_key: str | None = None,
    body: Mapping[str, Any] | None = None,
    timeout: float = 1.5,
) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if body is not None:
        data = json.dumps(dict(body), separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _opener(url).open(request, timeout=timeout) as response:
        raw = response.read(1_048_576)
        value = json.loads(raw.decode("utf-8")) if raw else {}
        return int(response.status), value


def _models_from_document(value: Any) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    items: Any = value.get("data") if isinstance(value, Mapping) else None
    if not isinstance(items, list) and isinstance(value, Mapping):
        items = value.get("models")
    if not isinstance(items, list):
        return (), {}
    names: set[str] = set()
    context: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, str):
            names.add(item)
            continue
        if not isinstance(item, Mapping):
            continue
        name = item.get("id") or item.get("name") or item.get("model")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        names.add(name)
        metadata: dict[str, Any] = {}
        for source, target in (
            ("context_window", "context_window"),
            ("context_length", "context_window"),
            ("max_model_len", "context_window"),
            ("owned_by", "owned_by"),
        ):
            raw = item.get(source)
            if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
                metadata[target] = raw
        if metadata:
            context[name] = metadata
    return tuple(sorted(names)), context


def _ollama_models(endpoint: str, timeout: float) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    parsed = urllib.parse.urlsplit(endpoint)
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/api/tags", "", ""))
    _status, value = _request_json("GET", url, timeout=timeout)
    return _models_from_document(value)


def _detect_endpoint(
    provider_identifier: str,
    display_name: str,
    endpoint: str,
    *,
    authentication_required: bool,
    credential_environment_variable: str | None,
    environ: Mapping[str, str],
    timeout: float,
    tool_use_support: bool | None = None,
) -> ProviderDetection:
    credential = (
        environ.get(credential_environment_variable, "")
        if credential_environment_variable
        else ""
    )
    credential_status = (
        "present"
        if credential
        else "missing"
        if authentication_required
        else "not_required"
    )
    if authentication_required and not credential:
        return ProviderDetection(
            provider_identifier,
            display_name,
            endpoint,
            "credential_missing",
            authentication_required=True,
            tool_use_support=tool_use_support,
            diagnostic_message=(
                f"{display_name} credential is not configured; set "
                f"{credential_environment_variable}."
            ),
            credential_environment_variable=credential_environment_variable,
            credential_status=credential_status,
        )
    try:
        _status, value = _request_json(
            "GET", endpoint.rstrip("/") + "/models", api_key=credential or None, timeout=timeout
        )
        models, context = _models_from_document(value)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            authentication_required = True
            models, context = (), {}
            credential_status = "present" if credential else "missing"
            if credential:
                status = "authentication_failed"
                message = f"{display_name} rejected the configured credential."
            else:
                status = "credential_missing"
                message = (
                    f"{display_name} requires authentication; set "
                    "VILLANI_MODEL_API_KEY_ENV to the name of its credential variable."
                )
        elif provider_identifier == "ollama" and error.code in {404, 405}:
            try:
                models, context = _ollama_models(endpoint, timeout)
                status = "connected"
                message = (
                    f"{display_name} is reachable with {len(models)} model(s)."
                    if models
                    else f"{display_name} is reachable but no model is loaded."
                )
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
                status, models, context = "unreachable", (), {}
                message = f"{display_name} is not reachable at its loopback endpoint."
        else:
            status, models, context = "error", (), {}
            message = f"{display_name} returned HTTP {error.code} while listing models."
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        status, models, context = "unreachable", (), {}
        message = f"{display_name} is not reachable at {endpoint}."
    else:
        status = "connected"
        message = (
            f"{display_name} is reachable with {len(models)} model(s)."
            if models
            else f"{display_name} is reachable but no model is loaded."
        )
    return ProviderDetection(
        provider_identifier,
        display_name,
        endpoint,
        status,
        models,
        authentication_required,
        tool_use_support,
        context,
        None,
        message,
        credential_environment_variable,
        credential_status,
    )


def detect_providers(
    *,
    explicit_endpoint: str | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: float = 1.5,
) -> tuple[ProviderDetection, ...]:
    """Detect all supported endpoints independently; one failure never aborts the scan."""

    env = dict(os.environ if environ is None else environ)
    candidates: list[tuple[str, str, str, bool, str | None, bool | None]] = []
    if explicit_endpoint:
        candidates.append(
            (
                "openai-compatible",
                "OpenAI-compatible endpoint",
                _normalize_endpoint(explicit_endpoint),
                False,
                env.get("VILLANI_MODEL_API_KEY_ENV") or None,
                None,
            )
        )
    candidates.extend((*item, False, None, None) for item in _LOCAL_PROVIDERS)
    candidates.append(
        (
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            True,
            "OPENAI_API_KEY",
            True,
        )
    )
    results: list[ProviderDetection] = []
    seen: set[str] = set()
    for identifier, name, raw_endpoint, required, credential_env, tools in candidates:
        endpoint = _normalize_endpoint(raw_endpoint)
        if endpoint.lower() in seen:
            continue
        seen.add(endpoint.lower())
        try:
            result = _detect_endpoint(
                identifier,
                name,
                endpoint,
                authentication_required=required,
                credential_environment_variable=credential_env,
                environ=env,
                timeout=timeout,
                tool_use_support=tools,
            )
        except Exception:
            # Provider adapters are a fault-isolation boundary. Do not include the
            # exception text because third-party errors can echo request headers.
            result = ProviderDetection(
                identifier,
                name,
                endpoint,
                "error",
                authentication_required=required,
                tool_use_support=tools,
                diagnostic_message=f"{name} detection failed; check its endpoint and logs.",
                credential_environment_variable=credential_env,
                credential_status=("present" if credential_env and env.get(credential_env) else "missing"),
            )
        results.append(result)
    return tuple(results)


def recommend_backend(
    detections: Sequence[ProviderDetection],
) -> tuple[ProviderDetection, str] | None:
    usable = [item for item in detections if item.usable]
    if not usable:
        return None
    local_order = {name: index for index, (name, _display, _url) in enumerate(_LOCAL_PROVIDERS)}
    chosen = min(
        usable,
        key=lambda item: (
            1 if item.provider_identifier == "openai" else 0,
            local_order.get(item.provider_identifier, len(local_order)),
            item.detected_endpoint,
        ),
    )
    return chosen, chosen.available_models[0]


def detect_repository(path: Path | None = None) -> Path | None:
    directory = (path or Path.cwd()).expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            shell=False,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    result = Path(completed.stdout.strip()).expanduser().resolve()
    return result if result.is_dir() else None


def _session_count(path: Path) -> int:
    if not path.is_dir():
        return 0
    count = 0
    try:
        for candidate in path.rglob("*"):
            if count >= 10_000:
                break
            if candidate.is_file() and candidate.suffix.lower() in {".json", ".jsonl", ".ndjson"}:
                count += 1
    except OSError:
        return count
    return count


def detect_session_sources(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> tuple[SessionSourceDetection, ...]:
    env = dict(os.environ if environ is None else environ)
    user_home = (home or Path.home()).expanduser().resolve()
    candidates = (
        ("claude", Path(env.get("CLAUDE_CONFIG_DIR") or user_home / ".claude")),
        ("codex", Path(env.get("CODEX_HOME") or user_home / ".codex")),
        ("pi", Path(env.get("PI_CODING_AGENT_DIR") or user_home / ".pi")),
    )
    results: list[SessionSourceDetection] = []
    for name, path in candidates:
        resolved = path.expanduser().resolve()
        installed = resolved.is_dir()
        count = _session_count(resolved) if installed else 0
        results.append(
            SessionSourceDetection(
                name,
                installed,
                str(resolved),
                count,
                (
                    f"{name.title()} sessions are available for local run history."
                    if installed
                    else f"No {name.title()} session directory was detected."
                ),
            )
        )
    return tuple(results)


def _provider_for_configuration(detection: ProviderDetection) -> str:
    if detection.provider_identifier == "openai":
        return "openai"
    if detection.provider_identifier in {"lm-studio", "ollama", "llama-cpp", "vllm"}:
        return "local"
    return "openai-compatible"


def _coding_command() -> str | None:
    suffix = ".exe" if os.name == "nt" else ""
    sibling = Path(sys.executable).resolve().parent / f"villani-code{suffix}"
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("villani-code")
    return str(Path(found).resolve()) if found else None


def build_configuration(
    detection: ProviderDetection,
    model: str,
    *,
    repository: Path | None,
    session_sources: Sequence[SessionSourceDetection] = (),
) -> dict[str, Any]:
    if not model.strip():
        raise SetupError("a model must be selected")
    if detection.available_models and model not in detection.available_models:
        raise SetupError(f"model {model!r} is not available from {detection.display_name}")
    configuration = copy.deepcopy(DEFAULT_CONFIG)
    configuration["config_version"] = SUPPORTED_CONFIG_VERSION
    # This is the explicit bootstrap policy for unrated models. No fabricated
    # capability is assigned; evidence can replace the unrated state later.
    configuration["policy"].update(
        {
            "easy_min_capability": 0,
            "medium_min_capability": 0,
            "hard_min_capability": 0,
        }
    )
    # Acceptance still requires the existing verifier contract. The selected
    # primary model supplies that review; the user is not asked to understand
    # or configure an internal verifier component.
    configuration["verifier"].update(
        {
            "no_llm": False,
            "backend": "default",
            "base_url": None,
            "model": None,
        }
    )
    metadata: dict[str, Any] = {
        "capability_status": "unrated",
        "setup_provider_identifier": detection.provider_identifier,
        "tool_use_support": detection.tool_use_support,
        "context": detection.context_metadata.get(model, {}),
        "pricing_metadata_source": detection.pricing_metadata_source,
    }
    backend: dict[str, Any] = {
        "provider": _provider_for_configuration(detection),
        "base_url": detection.detected_endpoint,
        "model": model,
        "billing_mode": "unknown",
        "currency": "USD",
        "capability_score_source": "unrated",
        "capability_score": 0,
        "roles": ["coding", "classification"],
        "max_parallel": 1,
        "metadata": metadata,
    }
    command = _coding_command()
    if command:
        backend["command_name"] = command
    if detection.credential_environment_variable:
        backend["api_key_env"] = detection.credential_environment_variable
    configuration["backends"] = {"default": backend}
    configuration["setup"] = {
        "schema_version": "villani.setup.v1",
        "configured_at": utc_now(),
        "primary_backend": "default",
        "capability_status": "unrated",
        "bootstrap_policy": True,
        "repository": str(repository) if repository else None,
        "session_sources": [
            item.source_identifier for item in session_sources if item.installed
        ],
    }
    validate_configuration(configuration)
    return configuration


def load_configuration(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SetupError(f"configuration cannot be read: {error}") from error
    if not isinstance(value, dict):
        raise SetupError("configuration must be a YAML object")
    return value


def validate_configuration(configuration: Mapping[str, Any]) -> dict[str, Backend]:
    version = configuration.get("config_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise SetupError("configuration schema version must be an integer")
    if version > SUPPORTED_CONFIG_VERSION:
        raise SetupError(
            f"configuration schema version {version} is newer than supported version "
            f"{SUPPORTED_CONFIG_VERSION}"
        )
    raw_backends = configuration.get("backends")
    if not isinstance(raw_backends, Mapping) or not raw_backends:
        raise SetupError("no coding backend is configured")
    parsed: dict[str, Backend] = {}
    for name, value in raw_backends.items():
        if not isinstance(value, Mapping):
            raise SetupError(f"backend {name!r} must be an object")
        try:
            backend = Backend.model_validate({"name": str(name), **dict(value)})
            validate_closed_loop_backend(backend)
        except (ValidationError, ProviderConfigurationError) as error:
            raise SetupError(f"backend {name!r} is invalid: {error}") from error
        if backend.api_key not in {None, "", "***REDACTED***"}:
            raise SetupError("configuration must reference credentials by environment variable")
        parsed[str(name)] = backend
    if not any(item.enabled and "coding" in item.roles for item in parsed.values()):
        raise SetupError("no enabled coding backend is configured")
    if not any(item.enabled and "classification" in item.roles for item in parsed.values()):
        raise SetupError("no enabled classification backend is configured")
    return parsed


def _configuration_payload(configuration: Mapping[str, Any]) -> str:
    return (
        "# Villani configuration generated by `villani setup`.\n"
        "# Credentials are referenced by environment-variable name; do not store secrets here.\n"
        + yaml.safe_dump(dict(configuration), sort_keys=False, allow_unicode=True)
    )


def write_configuration_atomic(
    path: Path,
    configuration: Mapping[str, Any],
    *,
    replace: Callable[[str | bytes | os.PathLike[str] | os.PathLike[bytes], str | bytes | os.PathLike[str] | os.PathLike[bytes]], None] = os.replace,
) -> ConfigurationWrite:
    """Validate, back up, fsync, and atomically activate configuration."""

    validate_configuration(configuration)
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    backup: Path | None = None
    payload = _configuration_payload(configuration)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        validate_configuration(load_configuration(temporary))
        if path.is_file():
            backup_root = path.parent / "config-backups"
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = backup_root / f"config-{stamp}.yaml"
            shutil.copy2(path, backup)
        replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return ConfigurationWrite(str(path), str(backup) if backup else None, SUPPORTED_CONFIG_VERSION)


def _api_key(detection: ProviderDetection, environ: Mapping[str, str]) -> str | None:
    name = detection.credential_environment_variable
    return environ.get(name) if name else None


def test_backend(
    detection: ProviderDetection,
    model: str,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 5,
) -> BackendProbe:
    env = dict(os.environ if environ is None else environ)
    try:
        _status, value = _request_json(
            "GET",
            detection.detected_endpoint.rstrip("/") + "/models",
            api_key=_api_key(detection, env),
            timeout=timeout,
        )
        models, _context = _models_from_document(value)
    except urllib.error.HTTPError as error:
        if detection.provider_identifier == "ollama" and error.code in {404, 405}:
            try:
                models, _context = _ollama_models(detection.detected_endpoint, timeout)
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
                return BackendProbe(
                    False, "connection", "Backend test could not reach the endpoint.", model
                )
        else:
            return BackendProbe(
                False, "connection", f"Backend test returned HTTP {error.code}.", model
            )
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return BackendProbe(False, "connection", "Backend test could not reach the endpoint.", model)
    if not models:
        return BackendProbe(False, "model", "The endpoint returned no available models.", model)
    if model not in models:
        return BackendProbe(False, "model", f"Model {model!r} is no longer available.", model)
    return BackendProbe(True, "connection", f"Model {model} is available.", model)


def run_capability_probe(
    detection: ProviderDetection,
    model: str,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 20,
) -> BackendProbe:
    """Run one tiny, non-destructive inference; it does not assign a capability score."""

    import time

    env = dict(os.environ if environ is None else environ)
    started = time.monotonic()
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Connectivity probe only. Reply with the single word READY.",
            }
        ],
        "temperature": 0,
        "max_tokens": 8,
    }
    try:
        status, value = _request_json(
            "POST",
            detection.detected_endpoint.rstrip("/") + "/chat/completions",
            api_key=_api_key(detection, env),
            body=body,
            timeout=timeout,
        )
    except urllib.error.HTTPError as error:
        return BackendProbe(
            False,
            "capability",
            f"Capability probe returned HTTP {error.code}.",
            model,
            round(time.monotonic() - started, 3),
        )
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return BackendProbe(
            False,
            "capability",
            "Capability probe could not complete.",
            model,
            round(time.monotonic() - started, 3),
        )
    choices = value.get("choices") if isinstance(value, Mapping) else None
    succeeded = status in range(200, 300) and isinstance(choices, list) and bool(choices)
    return BackendProbe(
        succeeded,
        "capability",
        (
            "Small non-destructive capability probe passed; the model remains unrated."
            if succeeded
            else "Capability probe returned no completion."
        ),
        model,
        round(time.monotonic() - started, 3),
    )


def create_sample_repository(*, root: Path | None = None) -> SampleRepository:
    parent = root.expanduser().resolve() if root else None
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="villani-sample-", dir=str(parent) if parent else None))
    (directory / "calculator.py").write_text(
        '"""Tiny disposable Villani setup sample."""\n\n\ndef add(left: int, right: int) -> int:\n    return left + right\n',
        encoding="utf-8",
    )
    (directory / "test_calculator.py").write_text(
        "import unittest\n\nfrom calculator import add\n\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    (directory / "README.md").write_text(
        "# Disposable Villani sample\n\nThis repository may be deleted after setup.\n",
        encoding="utf-8",
    )
    commands = (
        ["git", "init", "--quiet", str(directory)],
        ["git", "-C", str(directory), "config", "user.name", "Villani Setup"],
        ["git", "-C", str(directory), "config", "user.email", "setup@localhost"],
        ["git", "-C", str(directory), "add", "."],
        ["git", "-C", str(directory), "commit", "--quiet", "-m", "Initial sample"],
    )
    try:
        for command in commands:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                shell=False,
                check=False,
                timeout=15,
            )
            if completed.returncode != 0:
                raise SetupError("Git could not initialize the temporary sample repository")
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return SampleRepository(
        str(directory),
        "Add a typed subtract(left, right) function to calculator.py and add a passing test for it.",
    )


def run_sample_task(
    sample: SampleRepository,
    *,
    runner: Callable[[Sequence[str]], int] | None = None,
) -> int:
    command = [
        sys_executable(),
        "-m",
        "villani_distribution.frozen_entry",
        "run",
        sample.task,
        "--repo",
        sample.path,
        "--success-criteria",
        "All tests pass and only the disposable sample repository is changed.",
        "--max-attempts",
        "1",
    ]
    if runner is not None:
        return int(runner(command))
    return subprocess.run(command, shell=False, check=False).returncode


def sys_executable() -> str:
    return sys.executable


def write_setup_record(path: Path, document: Mapping[str, Any]) -> None:
    """Persist a sanitized setup transcript without credential values."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    payload = json.dumps(dict(document), sort_keys=True, indent=2) + "\n"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
