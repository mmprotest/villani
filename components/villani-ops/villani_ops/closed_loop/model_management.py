"""Model inventory, lifecycle, discovery, and secret-safe local state."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import yaml
from villani_ops.core.backend import Backend
from villani_ops.providers import CANONICAL_PROVIDERS, canonical_provider

from .capabilities.models import CapabilityProfile, CapabilitySnapshot
from .durable_io import write_json_atomic


MODEL_STATE_SCHEMA = "villani.model_management_state.v1"
MODEL_INVENTORY_SCHEMA = "villani.model_inventory.v1"
MODEL_POLICY_VERSION = "villani-model-lifecycle-v1"


class CapabilityStatus(str, Enum):
    UNRATED = "UNRATED"
    BOOTSTRAP = "BOOTSTRAP"
    OBSERVED = "OBSERVED"
    QUALIFIED = "QUALIFIED"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class ModelDetection:
    detector: str
    provider: str
    provider_display_name: str
    endpoint: str
    availability: str
    models: tuple[str, ...] = ()
    tool_support: bool | None = None
    context_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    detected_at: str = ""
    diagnostic: str = ""

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["models"] = list(self.models)
        return value


class ModelDetector(Protocol):
    @property
    def detector_name(self) -> str: ...

    def detect(self, *, timeout: float = 1.5) -> ModelDetection: ...


LOCAL_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("lm-studio", "LM Studio", "http://127.0.0.1:1234/v1"),
    ("ollama", "Ollama", "http://127.0.0.1:11434/v1"),
    ("llama-cpp", "llama.cpp", "http://127.0.0.1:8080/v1"),
    ("vllm", "vLLM", "http://127.0.0.1:8000/v1"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_endpoint(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("model endpoint must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("model endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("model endpoint must not contain a query or fragment")
    return candidate


def _is_loopback(endpoint: str) -> bool:
    host = (urllib.parse.urlsplit(endpoint).hostname or "").rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _opener(endpoint: str) -> urllib.request.OpenerDirector:
    if _is_loopback(endpoint):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _request_json(
    url: str,
    *,
    api_key: str | None,
    timeout: float,
) -> Any:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    with _opener(url).open(request, timeout=timeout) as response:
        raw = response.read(1_048_576)
    return json.loads(raw.decode("utf-8")) if raw else {}


def _models_from_document(
    value: Any,
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    items: Any = value.get("data") if isinstance(value, Mapping) else None
    if not isinstance(items, list) and isinstance(value, Mapping):
        items = value.get("models")
    if not isinstance(items, list):
        return (), {}
    names: set[str] = set()
    contexts: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, str) and item.strip():
            names.add(item.strip())
            continue
        if not isinstance(item, Mapping):
            continue
        raw_name = item.get("id") or item.get("name") or item.get("model")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        name = raw_name.strip()
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
            contexts[name] = metadata
    return tuple(sorted(names)), contexts


@dataclass(frozen=True, slots=True)
class OpenAICompatibleDetector:
    provider: str
    provider_display_name: str
    endpoint: str
    api_key_env: str | None = None
    tool_support: bool | None = None
    detector_name: str = "openai-compatible-model-list-v1"

    def detect(self, *, timeout: float = 1.5) -> ModelDetection:
        endpoint = normalize_endpoint(self.endpoint)
        api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
        detected_at = utc_now()
        try:
            value = _request_json(
                endpoint + "/models", api_key=api_key, timeout=timeout
            )
            models, contexts = _models_from_document(value)
        except urllib.error.HTTPError as error:
            if self.provider == "ollama" and error.code in {404, 405}:
                parsed = urllib.parse.urlsplit(endpoint)
                tags = urllib.parse.urlunsplit(
                    (parsed.scheme, parsed.netloc, "/api/tags", "", "")
                )
                try:
                    value = _request_json(tags, api_key=None, timeout=timeout)
                    models, contexts = _models_from_document(value)
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    urllib.error.URLError,
                ):
                    return ModelDetection(
                        self.detector_name,
                        self.provider,
                        self.provider_display_name,
                        endpoint,
                        "unreachable",
                        detected_at=detected_at,
                        diagnostic=f"{self.provider_display_name} is unreachable.",
                    )
            elif error.code in {401, 403}:
                return ModelDetection(
                    self.detector_name,
                    self.provider,
                    self.provider_display_name,
                    endpoint,
                    "authentication_failed" if api_key else "credential_missing",
                    detected_at=detected_at,
                    diagnostic=(
                        f"{self.provider_display_name} rejected its configured credential."
                        if api_key
                        else f"{self.provider_display_name} requires a credential."
                    ),
                )
            else:
                return ModelDetection(
                    self.detector_name,
                    self.provider,
                    self.provider_display_name,
                    endpoint,
                    "error",
                    detected_at=detected_at,
                    diagnostic=(
                        f"{self.provider_display_name} returned HTTP {error.code}."
                    ),
                )
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
            return ModelDetection(
                self.detector_name,
                self.provider,
                self.provider_display_name,
                endpoint,
                "unreachable",
                detected_at=detected_at,
                diagnostic=f"{self.provider_display_name} is unreachable.",
            )
        availability = "available" if models else "no_model_loaded"
        diagnostic = (
            f"{self.provider_display_name} exposes {len(models)} model(s)."
            if models
            else f"{self.provider_display_name} is reachable but no model is loaded."
        )
        return ModelDetection(
            self.detector_name,
            self.provider,
            self.provider_display_name,
            endpoint,
            availability,
            models,
            self.tool_support,
            contexts,
            detected_at,
            diagnostic,
        )


def configured_backends(configuration: Mapping[str, Any]) -> dict[str, Backend]:
    raw = configuration.get("backends")
    if not isinstance(raw, Mapping):
        return {}
    parsed: dict[str, Backend] = {}
    for name, value in raw.items():
        if isinstance(value, Backend):
            parsed[str(name)] = value
        elif isinstance(value, Mapping):
            parsed[str(name)] = Backend.model_validate(
                {"name": str(name), **dict(value)}
            )
    return parsed


def default_bootstrap_backend(configuration: Mapping[str, Any]) -> str | None:
    management = configuration.get("model_management")
    values = management if isinstance(management, Mapping) else {}
    explicit = values.get("bootstrap_default")
    if isinstance(explicit, str) and explicit:
        return explicit
    setup = configuration.get("setup")
    setup_values = setup if isinstance(setup, Mapping) else {}
    legacy = setup_values.get("primary_backend")
    if isinstance(legacy, str) and legacy:
        return legacy
    for name, backend in configured_backends(configuration).items():
        if backend.metadata.get("bootstrap_default") is True:
            return name
    return None


def manual_override(backend: Backend) -> bool:
    return bool(
        backend.metadata.get("manual_capability_override")
        or backend.capability_score_source in {"manual_override", "user_configured"}
    )


def _capability_values(configuration: Mapping[str, Any]) -> tuple[int, float]:
    raw = configuration.get("capabilities")
    values = raw if isinstance(raw, Mapping) else {}
    minimum = int(values.get("minimum_empirical_samples", 20))
    configured = values.get("minimum_empirical_wilson_lower_bound")
    bound = float(
        configured
        if configured is not None
        else values.get("target_success_probability", 0.80)
    )
    return minimum, bound


def _global_profile(
    backend: Backend, snapshot: CapabilitySnapshot | None
) -> CapabilityProfile | None:
    if snapshot is None:
        return None
    matches = [
        profile
        for profile in snapshot.profiles
        if profile.key.backend_name == backend.name
        and profile.key.provider == backend.provider
        and profile.key.model == backend.model
        and profile.key.task_category == "*"
        and profile.key.difficulty == "*"
        and profile.key.risk == "*"
    ]
    return max(matches, key=lambda item: item.sample_count, default=None)


def capability_status(
    backend: Backend,
    configuration: Mapping[str, Any],
    snapshot: CapabilitySnapshot | None,
) -> CapabilityStatus:
    if not backend.enabled:
        return CapabilityStatus.DISABLED
    profile = _global_profile(backend, snapshot)
    minimum, bound = _capability_values(configuration)
    if (
        profile is not None
        and profile.sample_count >= minimum
        and profile.wilson_lower_bound >= bound
    ):
        return CapabilityStatus.QUALIFIED
    if profile is not None and profile.sample_count > 0:
        return CapabilityStatus.OBSERVED
    if default_bootstrap_backend(configuration) == backend.name:
        return CapabilityStatus.BOOTSTRAP
    return CapabilityStatus.UNRATED


def route_basis(
    backend: Backend,
    configuration: Mapping[str, Any],
    snapshot: CapabilitySnapshot | None,
    *,
    qualified_empirical_route: bool,
) -> str:
    if manual_override(backend):
        return "manual_override"
    if qualified_empirical_route:
        return "qualified_empirical_policy"
    state = capability_status(backend, configuration, snapshot)
    if state == CapabilityStatus.OBSERVED and (
        default_bootstrap_backend(configuration) != backend.name
    ):
        return "observed_policy"
    return "bootstrap_default"


def is_local_backend(backend: Backend) -> bool:
    return backend.provider == "local" or bool(
        backend.base_url and _is_loopback(backend.base_url)
    )


def load_model_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": MODEL_STATE_SCHEMA,
            "updated_at": None,
            "detections": [],
            "tests": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "schema_version": MODEL_STATE_SCHEMA,
            "updated_at": None,
            "detections": [],
            "tests": {},
        }
    if not isinstance(value, dict) or value.get("schema_version") != MODEL_STATE_SCHEMA:
        return {
            "schema_version": MODEL_STATE_SCHEMA,
            "updated_at": None,
            "detections": [],
            "tests": {},
        }
    return value


def write_model_state(path: Path, value: Mapping[str, Any]) -> None:
    payload = {
        "schema_version": MODEL_STATE_SCHEMA,
        "updated_at": utc_now(),
        "detections": list(value.get("detections", [])),
        "tests": dict(value.get("tests", {})),
    }
    write_json_atomic(path, payload)


def write_configuration_atomic(
    path: Path,
    configuration: Mapping[str, Any],
    *,
    header: str = "",
) -> None:
    """Atomically replace a secret-reference-only YAML configuration."""

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = header + yaml.safe_dump(
        dict(configuration), sort_keys=False, allow_unicode=True
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def default_detectors(
    configuration: Mapping[str, Any],
) -> tuple[ModelDetector, ...]:
    candidates: list[OpenAICompatibleDetector] = [
        OpenAICompatibleDetector(identifier, label, endpoint)
        for identifier, label, endpoint in LOCAL_ENDPOINTS
    ]
    seen = {normalize_endpoint(item.endpoint) for item in candidates}
    for backend in configured_backends(configuration).values():
        if not backend.base_url:
            continue
        endpoint = normalize_endpoint(backend.base_url)
        if endpoint in seen:
            continue
        seen.add(endpoint)
        candidates.append(
            OpenAICompatibleDetector(
                backend.provider,
                str(backend.metadata.get("provider_display_name") or backend.provider),
                endpoint,
                api_key_env=backend.api_key_env,
                tool_support=(
                    backend.metadata.get("tool_use_support")
                    if isinstance(backend.metadata.get("tool_use_support"), bool)
                    else None
                ),
            )
        )
    return tuple(candidates)


def detect_models(
    configuration: Mapping[str, Any],
    *,
    detectors: Sequence[ModelDetector] | None = None,
    timeout: float = 1.5,
) -> tuple[ModelDetection, ...]:
    selected = (
        tuple(detectors) if detectors is not None else default_detectors(configuration)
    )
    return tuple(detector.detect(timeout=timeout) for detector in selected)


def update_detection_state(
    state: Mapping[str, Any], detections: Sequence[ModelDetection]
) -> dict[str, Any]:
    return {
        "schema_version": MODEL_STATE_SCHEMA,
        "updated_at": utc_now(),
        "detections": [item.as_dict() for item in detections],
        "tests": dict(state.get("tests", {})),
    }


def test_models(
    configuration: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    backend_names: Sequence[str] = (),
    timeout: float = 3.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    backends = configured_backends(configuration)
    selected = list(backend_names) if backend_names else sorted(backends)
    unknown = [name for name in selected if name not in backends]
    if unknown:
        raise ValueError("unknown model backend(s): " + ", ".join(unknown))
    tests = dict(state.get("tests", {}))
    results: list[dict[str, Any]] = []
    for name in selected:
        backend = backends[name]
        if not backend.enabled:
            availability = "disabled"
            diagnostic = "Model is disabled."
        elif not backend.base_url:
            availability = "unsupported"
            diagnostic = (
                "This provider does not expose a configured model-list endpoint."
            )
        else:
            detector = OpenAICompatibleDetector(
                backend.provider,
                str(backend.metadata.get("provider_display_name") or backend.provider),
                backend.base_url,
                api_key_env=backend.api_key_env,
                tool_support=(
                    backend.metadata.get("tool_use_support")
                    if isinstance(backend.metadata.get("tool_use_support"), bool)
                    else None
                ),
            )
            detection = detector.detect(timeout=timeout)
            if detection.availability == "available":
                availability = (
                    "available"
                    if backend.model in detection.models
                    else "model_not_loaded"
                )
                diagnostic = (
                    f"Model {backend.model} is available."
                    if availability == "available"
                    else f"Endpoint is reachable but model {backend.model} is not loaded."
                )
            else:
                availability = detection.availability
                diagnostic = detection.diagnostic
        record = {
            "backend_name": name,
            "model": backend.model,
            "availability": availability,
            "tested_at": utc_now(),
            "diagnostic": diagnostic,
            "model_tokens_used": 0,
        }
        tests[name] = record
        results.append(record)
    updated = {
        "schema_version": MODEL_STATE_SCHEMA,
        "updated_at": utc_now(),
        "detections": list(state.get("detections", [])),
        "tests": tests,
    }
    return updated, results


def _pricing_status(backend: Backend) -> str:
    if backend.billing_mode == "unknown":
        return "unknown"
    if backend.billing_mode == "token":
        return (
            "known"
            if backend.input_cost_per_million > 0 or backend.output_cost_per_million > 0
            else "unknown"
        )
    if backend.billing_mode == "compute_time":
        return "known" if backend.compute_cost_per_hour is not None else "unknown"
    if backend.billing_mode == "fixed":
        return "known" if backend.fixed_cost_per_attempt is not None else "unknown"
    components = (
        backend.input_cost_per_million > 0 or backend.output_cost_per_million > 0,
        backend.compute_cost_per_hour is not None,
        backend.fixed_cost_per_attempt is not None,
    )
    return "known" if sum(components) >= 2 else "unknown"


def _tool_support(value: object) -> str:
    if value is True:
        return "supported"
    if value is False:
        return "unsupported"
    return "unknown"


def _detection_rows(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = state.get("detections")
    return (
        [item for item in raw if isinstance(item, Mapping)]
        if isinstance(raw, list)
        else []
    )


def _availability_for(
    backend: Backend, state: Mapping[str, Any]
) -> tuple[str, bool, Mapping[str, Any] | None]:
    tests = state.get("tests")
    test = tests.get(backend.name) if isinstance(tests, Mapping) else None
    if isinstance(test, Mapping) and isinstance(test.get("availability"), str):
        return str(test["availability"]), True, test
    endpoint = normalize_endpoint(backend.base_url) if backend.base_url else None
    for detection in _detection_rows(state):
        try:
            detected_endpoint = normalize_endpoint(str(detection.get("endpoint") or ""))
        except ValueError:
            continue
        models = detection.get("models")
        if endpoint == detected_endpoint and isinstance(models, list):
            status = str(detection.get("availability") or "unknown")
            if status == "available" and backend.model not in models:
                status = "model_not_loaded"
            return status, True, None
    return "unknown", False, None


def _detected_metadata_for(
    backend: Backend, state: Mapping[str, Any]
) -> tuple[dict[str, Any], bool | None]:
    if not backend.base_url:
        return {}, None
    endpoint = normalize_endpoint(backend.base_url)
    for detection in _detection_rows(state):
        try:
            detected_endpoint = normalize_endpoint(str(detection.get("endpoint") or ""))
        except ValueError:
            continue
        if endpoint != detected_endpoint:
            continue
        contexts = detection.get("context_metadata")
        context = contexts.get(backend.model) if isinstance(contexts, Mapping) else None
        tool_support = detection.get("tool_support")
        return (
            dict(context) if isinstance(context, Mapping) else {},
            tool_support if isinstance(tool_support, bool) else None,
        )
    return {}, None


def model_records(
    configuration: Mapping[str, Any],
    snapshot: CapabilitySnapshot | None,
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    configured_models: set[tuple[str, str]] = set()
    for backend in sorted(
        configured_backends(configuration).values(), key=lambda item: item.name
    ):
        configured_models.add((backend.base_url or "", backend.model))
        profile = _global_profile(backend, snapshot)
        availability, detected, test = _availability_for(backend, state)
        detected_context, detected_tool_support = _detected_metadata_for(backend, state)
        context = backend.metadata.get("context")
        context_values = {
            **detected_context,
            **(dict(context) if isinstance(context, Mapping) else {}),
        }
        configured_tool_support = backend.metadata.get("tool_use_support")
        status = capability_status(backend, configuration, snapshot)
        cost_per_accepted: float | None = None
        # Older capability snapshots do not retain a complete-cost observation
        # count. A mean over a partial subset cannot safely be promoted to cost
        # per accepted task, so this remains unknown until the store gains that
        # explicit accounting fact in a future schema version.
        records.append(
            {
                "id": backend.model,
                "backend_name": backend.name,
                "display_name": str(
                    backend.metadata.get("display_name") or backend.model
                ),
                "model": backend.model,
                "provider": backend.provider,
                "endpoint": backend.base_url,
                "configured": True,
                "detected": detected,
                "availability": availability,
                "available": (
                    True
                    if availability == "available"
                    else False
                    if availability
                    not in {"unknown", "unsupported", "credential_missing"}
                    else None
                ),
                "tool_support": _tool_support(
                    configured_tool_support
                    if isinstance(configured_tool_support, bool)
                    else detected_tool_support
                ),
                "context_metadata": context_values,
                "context_window": context_values.get("context_window"),
                "configured_roles": list(backend.roles),
                "pricing_status": _pricing_status(backend),
                "currency": backend.currency,
                "observed_task_count": profile.sample_count if profile else 0,
                "observed_success_rate": profile.raw_success_rate if profile else None,
                "observed_cost_per_accepted_task": cost_per_accepted,
                "capability_status": status.value,
                "capability": status.value,
                "bootstrap_default": default_bootstrap_backend(configuration)
                == backend.name,
                "manual_override": manual_override(backend),
                "manual_override_label": (
                    "Advanced manual capability override"
                    if manual_override(backend)
                    else None
                ),
                "last_tested_at": (
                    str(test.get("tested_at"))
                    if isinstance(test, Mapping) and test.get("tested_at")
                    else None
                ),
                "last_test_diagnostic": (
                    str(test.get("diagnostic"))
                    if isinstance(test, Mapping) and test.get("diagnostic")
                    else None
                ),
                "capability_policy_version": MODEL_POLICY_VERSION,
            }
        )

    for detection in _detection_rows(state):
        endpoint = str(detection.get("endpoint") or "")
        provider = str(detection.get("provider") or "unknown")
        contexts = detection.get("context_metadata")
        context_map = contexts if isinstance(contexts, Mapping) else {}
        raw_models = detection.get("models")
        models = raw_models if isinstance(raw_models, list) else []
        for model_value in models:
            if (
                not isinstance(model_value, str)
                or (endpoint, model_value) in configured_models
            ):
                continue
            context = context_map.get(model_value)
            context_values = dict(context) if isinstance(context, Mapping) else {}
            records.append(
                {
                    "id": model_value,
                    "backend_name": None,
                    "display_name": model_value,
                    "model": model_value,
                    "provider": provider,
                    "endpoint": endpoint,
                    "configured": False,
                    "detected": True,
                    "availability": str(detection.get("availability") or "unknown"),
                    "available": detection.get("availability") == "available",
                    "tool_support": _tool_support(detection.get("tool_support")),
                    "context_metadata": context_values,
                    "context_window": context_values.get("context_window"),
                    "configured_roles": [],
                    "pricing_status": "unknown",
                    "currency": None,
                    "observed_task_count": 0,
                    "observed_success_rate": None,
                    "observed_cost_per_accepted_task": None,
                    "capability_status": CapabilityStatus.UNRATED.value,
                    "capability": CapabilityStatus.UNRATED.value,
                    "bootstrap_default": False,
                    "manual_override": False,
                    "manual_override_label": None,
                    "last_tested_at": None,
                    "last_test_diagnostic": None,
                    "capability_policy_version": MODEL_POLICY_VERSION,
                }
            )
    return records


def inventory_document(
    configuration: Mapping[str, Any],
    snapshot: CapabilitySnapshot | None,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    records = model_records(configuration, snapshot, state)
    return {
        "schema_version": MODEL_INVENTORY_SCHEMA,
        "models": records,
        "bootstrap_default": default_bootstrap_backend(configuration),
        "capability_states": [item.value for item in CapabilityStatus],
        "qualification": {
            "minimum_sample_count": _capability_values(configuration)[0],
            "minimum_conservative_confidence_bound": _capability_values(configuration)[
                1
            ],
            "policy_version": MODEL_POLICY_VERSION,
        },
    }


def set_bootstrap_default(configuration: dict[str, Any], backend_name: str) -> None:
    backends = configured_backends(configuration)
    if backend_name not in backends:
        raise ValueError(f"unknown model backend: {backend_name}")
    if not backends[backend_name].enabled:
        raise ValueError("a disabled model cannot be the bootstrap default")
    management = configuration.get("model_management")
    values = dict(management) if isinstance(management, Mapping) else {}
    values.update(
        {
            "version": MODEL_POLICY_VERSION,
            "bootstrap_default": backend_name,
        }
    )
    configuration["model_management"] = values
    raw_backends = configuration.get("backends")
    if isinstance(raw_backends, dict):
        for name, raw in raw_backends.items():
            if not isinstance(raw, dict):
                continue
            metadata = raw.get("metadata")
            metadata_values = dict(metadata) if isinstance(metadata, Mapping) else {}
            metadata_values["bootstrap_default"] = str(name) == backend_name
            if str(name) == backend_name and not manual_override(backends[str(name)]):
                metadata_values["capability_status"] = CapabilityStatus.BOOTSTRAP.value
            elif (
                str(metadata_values.get("capability_status") or "").upper()
                == CapabilityStatus.BOOTSTRAP.value
            ):
                metadata_values["capability_status"] = CapabilityStatus.UNRATED.value
            raw["metadata"] = metadata_values
    setup = configuration.get("setup")
    if isinstance(setup, dict):
        setup["primary_backend"] = backend_name
        setup["capability_status"] = CapabilityStatus.BOOTSTRAP.value


def add_model_to_configuration(
    configuration: dict[str, Any],
    *,
    backend_name: str,
    model: str,
    provider: str,
    endpoint: str | None,
    display_name: str | None = None,
    roles: Sequence[str] = ("coding", "classification"),
    api_key_env: str | None = None,
    tool_support: bool | None = None,
    context_window: int | None = None,
    make_default: bool = False,
    manual_capability_score: float | None = None,
    billing_mode: str = "unknown",
    input_cost_per_million: float | None = None,
    output_cost_per_million: float | None = None,
    fixed_cost_per_attempt: float | None = None,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", backend_name):
        raise ValueError(
            "model name must use letters, numbers, dots, underscores, or dashes"
        )
    if not model.strip():
        raise ValueError("model identifier must not be empty")
    normalized_provider = canonical_provider(provider)
    if normalized_provider not in CANONICAL_PROVIDERS:
        raise ValueError(
            "provider must be one of: " + ", ".join(sorted(CANONICAL_PROVIDERS))
        )
    normalized_endpoint = normalize_endpoint(endpoint) if endpoint else None
    if (
        normalized_provider in {"local", "openai-compatible"}
        and not normalized_endpoint
    ):
        raise ValueError(f"provider {normalized_provider} requires an endpoint")
    if api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise ValueError("credential environment variable name is invalid")
    if context_window is not None and context_window < 1:
        raise ValueError("context window must be positive")
    if manual_capability_score is not None and not 0 <= manual_capability_score <= 100:
        raise ValueError("manual capability score must be between 0 and 100")
    unique_roles = list(dict.fromkeys(str(item) for item in roles))
    if not unique_roles:
        raise ValueError("at least one configured role is required")
    metadata: dict[str, Any] = {
        "display_name": display_name or model,
        "capability_status": CapabilityStatus.UNRATED.value,
        "tool_use_support": tool_support,
        "context": (
            {"context_window": context_window} if context_window is not None else {}
        ),
    }
    capability_score = 0
    capability_source = "unrated"
    if manual_capability_score is not None:
        capability_score = int(manual_capability_score)
        capability_source = "manual_override"
        metadata["manual_capability_override"] = {
            "label": "Advanced manual capability override",
            "configured_at": utc_now(),
        }
    payload = {
        "provider": normalized_provider,
        "base_url": normalized_endpoint,
        "model": model.strip(),
        "api_key_env": api_key_env,
        "roles": unique_roles,
        "capability_score": capability_score,
        "capability_score_source": capability_source,
        "billing_mode": billing_mode,
        "input_cost_per_million": input_cost_per_million or 0.0,
        "output_cost_per_million": output_cost_per_million or 0.0,
        "fixed_cost_per_attempt": fixed_cost_per_attempt,
        "currency": "USD",
        "enabled": True,
        "metadata": metadata,
    }
    backend = Backend.model_validate({"name": backend_name, **payload})
    raw = configuration.setdefault("backends", {})
    if not isinstance(raw, dict):
        raise ValueError("config backends must be a mapping")
    raw[backend_name] = backend.model_dump(mode="json", exclude={"name", "api_key"})
    if make_default:
        set_bootstrap_default(configuration, backend_name)


def remove_model_from_configuration(
    configuration: dict[str, Any], backend_name: str
) -> None:
    raw = configuration.get("backends")
    if not isinstance(raw, dict) or backend_name not in raw:
        raise ValueError(f"unknown model backend: {backend_name}")
    del raw[backend_name]
    management = configuration.get("model_management")
    if (
        isinstance(management, dict)
        and management.get("bootstrap_default") == backend_name
    ):
        management["bootstrap_default"] = None
    setup = configuration.get("setup")
    if isinstance(setup, dict) and setup.get("primary_backend") == backend_name:
        setup["primary_backend"] = None
        setup["capability_status"] = CapabilityStatus.UNRATED.value


__all__ = [
    "CapabilityStatus",
    "MODEL_INVENTORY_SCHEMA",
    "MODEL_POLICY_VERSION",
    "MODEL_STATE_SCHEMA",
    "ModelDetection",
    "ModelDetector",
    "OpenAICompatibleDetector",
    "add_model_to_configuration",
    "capability_status",
    "configured_backends",
    "default_bootstrap_backend",
    "detect_models",
    "inventory_document",
    "is_local_backend",
    "load_model_state",
    "manual_override",
    "model_records",
    "remove_model_from_configuration",
    "route_basis",
    "set_bootstrap_default",
    "test_models",
    "update_detection_state",
    "write_model_state",
    "write_configuration_atomic",
]
