"""Structured, local-only data boundary for the single Villani Console.

The browser never receives a filesystem path to inspect.  Flight Recorder owns
discovery and parsing; Agentd invokes its presentation-neutral JSON adapter and
merges synchronization state from the local spool.
"""

from __future__ import annotations

import importlib.metadata
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import yaml
from villani_ops.closed_loop.agent_systems.configuration import (
    migrate_agent_system_configuration,
)
from villani_ops.closed_loop.capabilities.store import CapabilityStore
from villani_ops.closed_loop.interfaces import ClosedLoopRunRequest
from villani_ops.closed_loop.product_run import build_product_run
from villani_ops.closed_loop.model_management import (
    add_model_to_configuration,
    configured_backends,
    detect_models,
    inventory_document,
    load_model_state,
    remove_model_from_configuration,
    set_bootstrap_default,
    test_models,
    update_detection_state,
    write_configuration_atomic,
    write_model_state,
)
from villani_ops.closed_loop.policy_presets import (
    apply_policy_preset,
    configure_policy_preset,
    configured_policy_preset,
    normalize_policy_preset,
    policy_preset_rows,
)
from villani_ops.closed_loop.policy_preview import simulate_historical_runs
from villani_ops.closed_loop.presentation import failure_experience, infer_failure_code
from villani_ops.execution_environment import (
    CONFIRMATION_THRESHOLD,
    confirmed_command,
    discover_repository_validation,
    parse_manual_command,
)
from villani_ops.executables import (
    resolve_installed_executable,
    resolved_executable_prefix,
)

from .config import AgentdPaths, SyncConfig
from .platform_process import windows_creation_flags
from .process import terminate_process_tree
from .redaction import redact_sensitive_text
from .spool import SQLiteSpool


CONSOLE_HISTORY_SCHEMA = "villani.console.history.v1"
CONSOLE_BOOTSTRAP_SCHEMA = "villani.console.bootstrap.v1"
CONSOLE_HOME_SCHEMA = "villani.console.home.v1"
CONSOLE_RUN_OPTIONS_SCHEMA = "villani.console.run_options.v1"
CONSOLE_RUN_SUBMISSION_SCHEMA = "villani.console.run_submission.v1"
CONSOLE_MODELS_SCHEMA = "villani.console.models.v1"
CONSOLE_POLICIES_SCHEMA = "villani.console.policies.v1"
SUPPORTED_CONFIG_VERSION = 1
_MAX_VFR_OUTPUT = 16 * 1024 * 1024
_CONFIG_HEADER = """# Villani local-first configuration.
# Secret values must remain in environment variables referenced by api_key_env.
"""


class ConsoleDataError(RuntimeError):
    """Safe diagnostic returned when the local replay engine is unavailable."""


class ConsoleInputError(ValueError):
    """A safe, user-correctable Console request error."""


class ConsoleAuthorizationError(PermissionError):
    """A connected approval was not authenticated."""


class ConsoleBridge(Protocol):
    def history(self, *, refresh: bool = False) -> dict[str, Any]: ...

    def replay(self, record_id: str, kind: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class BridgeCommand:
    prefix: tuple[str, ...]
    description: str


def _package_version() -> str:
    for package in ("villani", "villani-agentd"):
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            continue
    return "development"


def _locate_vfr() -> BridgeCommand | None:
    configured = os.environ.get("VILLANI_VFR_EXECUTABLE")
    fallbacks = (Path(configured).expanduser(),) if configured else ()
    resolution = resolve_installed_executable("vfr", compatibility_fallbacks=fallbacks)
    if resolution.path is not None:
        description = {
            "interpreter_scripts": "packaged Flight Recorder adapter",
            "interpreter_parent": "packaged Flight Recorder adapter",
            "additional_search_dir": "installed Flight Recorder adapter",
            "PATH": "installed Flight Recorder adapter",
            "compatibility_fallback": "configured Flight Recorder adapter",
        }.get(resolution.source, "installed Flight Recorder adapter")
        return BridgeCommand(resolved_executable_prefix(resolution), description)

    # Development checkout only.  This path is never used by an installed
    # wheel; packaged products resolve the sibling executable above.
    repository = Path(__file__).resolve().parents[3]
    cli = repository / "components" / "villani-flight-recorder" / "dist" / "cli.js"
    node = shutil.which("node")
    if cli.is_file() and node:
        return BridgeCommand((str(Path(node).resolve()), str(cli)), "development replay adapter")
    return None


class VfrConsoleBridge:
    """Bounded subprocess bridge to Flight Recorder's structured adapter."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        located = (
            _locate_vfr() if command is None else BridgeCommand(tuple(command), "test adapter")
        )
        if located is None:
            raise ConsoleDataError(
                "Local replay engine is unavailable. Reinstall Villani, then run: villani doctor"
            )
        self.command = located
        self.timeout_seconds = timeout_seconds

    def _run(self, arguments: Sequence[str]) -> dict[str, Any]:
        command = [*self.command.prefix, "console-data", *arguments]
        windows = os.name == "nt"
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=windows_creation_flags() if windows else 0,
            start_new_session=not windows,
        )
        stdout = b""
        stderr = b""
        timed_out = False
        try:
            try:
                stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_tree(process)
                stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                terminate_process_tree(process)
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        safe_stderr = redact_sensitive_text(
            stderr[:4096].decode("utf-8", errors="replace").strip()
        ).value
        if timed_out:
            raise ConsoleDataError(
                f"Local replay indexing timed out after {elapsed_ms} ms. Run: villani doctor"
            )
        if process.returncode != 0:
            detail = safe_stderr or "no diagnostic output"
            raise ConsoleDataError(
                f"Local replay indexing failed (exit {process.returncode}, {elapsed_ms} ms): {detail}"
            )
        if len(stdout) > _MAX_VFR_OUTPUT:
            raise ConsoleDataError("Local replay response exceeded its safe size limit.")
        try:
            value = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConsoleDataError(
                "Local replay engine returned invalid structured data."
            ) from error
        if not isinstance(value, dict):
            raise ConsoleDataError("Local replay engine returned an invalid response shape.")
        return value

    def history(self, *, refresh: bool = False) -> dict[str, Any]:
        arguments = ["--kind", "history"]
        if refresh:
            arguments.append("--refresh")
        return self._run(arguments)

    def replay(self, record_id: str, kind: str) -> dict[str, Any]:
        if kind not in {"run", "session"}:
            raise ConsoleDataError("Replay kind must be run or session.")
        return self._run(["--kind", kind, "--id", record_id])


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _product_failure(
    code: str,
    *,
    reason: str | None = None,
    run_started: bool = False,
) -> dict[str, Any]:
    """Return an actionable public failure without making target-state guesses."""

    failure = failure_experience(code, reason=reason)
    failure["patch_status"] = (
        "The target repository was not modified."
        if run_started
        else "No run was started. The target repository was not modified."
    )
    return failure


def _product_failure_code(reason: str) -> str:
    lowered = reason.lower()
    if any(value in lowered for value in ("command is unavailable", "runner", "not installed")):
        return "runner_missing"
    if any(value in lowered for value in ("credential", "unauthorized", "401", "forbidden")):
        return "expired_credentials"
    inferred = infer_failure_code(None, reason)
    return {
        "no_backend": "no_usable_agent",
        "model_not_loaded": "unavailable_model",
        "verifier_unavailable": "verification_infrastructure_failure",
        "no_authoritative_evidence": "no_acceptable_candidate",
        "repository_changed_before_materialization": "target_drift",
        "patch_conflict": "delivery_conflict",
        "service_offline": "service_interruption",
        "user_cancelled": "cancellation",
    }.get(inferred, inferred)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _configuration(home: Path) -> tuple[dict[str, Any], list[str], int | None]:
    path = home / "config.yaml"
    if not path.is_file():
        return {}, ["No coding backend is configured. Run: villani setup"], None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}, ["Configuration cannot be read. Run: villani setup"], None
    if not isinstance(loaded, dict):
        return {}, ["Configuration must be a YAML object. Run: villani setup --reset"], None
    raw_version = loaded.get("config_version", 1)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        return loaded, ["Configuration schema version is invalid. Run: villani doctor"], None
    if raw_version > SUPPORTED_CONFIG_VERSION:
        return (
            loaded,
            [
                f"Configuration schema {raw_version} is newer than this Villani version. "
                "Upgrade Villani before starting the service."
            ],
            raw_version,
        )
    backends = loaded.get("backends")
    if not isinstance(backends, Mapping) or not backends:
        return loaded, ["No coding backend is configured. Run: villani setup"], raw_version
    return loaded, [], raw_version


def _model_state_with_setup_detection(home: Path) -> dict[str, Any]:
    """Read current state and project the older setup probe until first detect."""

    state = load_model_state(home / "models-state.json")
    if state.get("detections"):
        return state
    try:
        record = json.loads((home / "setup-record.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return state
    provider = _mapping(_mapping(record).get("provider"))
    models = [
        str(value)
        for value in provider.get("available_models", [])
        if isinstance(value, str) and value
    ]
    endpoint = _text(provider.get("detected_endpoint"))
    if not endpoint:
        return state
    connection = _text(provider.get("connection_status"))
    state["detections"] = [
        {
            "detector": "setup-record-compatibility-v1",
            "provider": _text(provider.get("provider_identifier")) or "unknown",
            "provider_display_name": _text(provider.get("provider_display_name"))
            or "Detected provider",
            "endpoint": endpoint,
            "availability": (
                "available"
                if connection == "connected" and models
                else "no_model_loaded"
                if connection == "connected"
                else "unreachable"
            ),
            "models": models,
            "tool_support": provider.get("tool_support"),
            "context_metadata": _mapping(provider.get("context_metadata")),
            "detected_at": _text(record.get("recorded_at")) or "",
            "diagnostic": "Imported from the local setup probe.",
        }
    ]
    return state


def _model_inventory(
    configuration: Mapping[str, Any], home: Path, *, refresh: bool = False
) -> dict[str, Any]:
    store = CapabilityStore(home / "capabilities")
    snapshot = store.rebuild(home / "runs").snapshot if refresh else store.load()
    state = _model_state_with_setup_detection(home)
    return inventory_document(configuration, snapshot, state)


def _models(configuration: Mapping[str, Any], home: Path) -> list[dict[str, Any]]:
    return _model_inventory(configuration, home).get("models", [])


def _last_error(path: Path) -> str | None:
    try:
        raw = path.read_bytes()[-131_072:]
    except OSError:
        return None
    for line in reversed(raw.decode("utf-8", errors="replace").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("level") == "error":
            return _text(value.get("event")) or "See the Villani Service log."
    return None


def _history_key(entry: Mapping[str, Any]) -> tuple[str, str]:
    logical = str(entry.get("logical_id") or entry.get("id") or "")
    kind = str(entry.get("kind") or "run")
    # A synchronized/local representation of the same Villani run is one row.
    return ("run" if kind == "run" else kind, logical)


def _merge_entry(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if value is not None or key in {"synchronization_state", "deep_link"}:
            result[key] = value
    return result


class ConsoleService:
    """Combines local replay data with authoritative service/sync state."""

    def __init__(
        self,
        paths: AgentdPaths,
        spool: SQLiteSpool,
        *,
        bridge: ConsoleBridge | None = None,
        controller_builder: Callable[[Mapping[str, Any], Callable[[Any], None] | None], Any]
        | None = None,
        policy_preview_builder: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.paths = paths
        self.spool = spool
        self._bridge = bridge
        self._controller_builder = controller_builder
        self._policy_preview_builder = policy_preview_builder
        self._configuration_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._pending_runs: dict[str, dict[str, Any]] = {}
        self._run_threads: dict[str, threading.Thread] = {}
        self._run_cancellations: dict[str, threading.Event] = {}
        self._submission_ids: dict[str, str] = {}
        self._run_condition = threading.Condition(self._run_lock)
        self._validation_cache: dict[str, dict[str, Any]] = {}

    def _get_bridge(self) -> ConsoleBridge:
        if self._bridge is None:
            self._bridge = VfrConsoleBridge()
        return self._bridge

    @property
    def home_path(self) -> Path:
        return self.paths.root.parent

    def bootstrap(self) -> dict[str, Any]:
        configuration, issues, schema_version = _configuration(self.home_path)
        status = self.spool.status()
        sync = SyncConfig.load(self.paths.sync_config)
        endpoint: dict[str, Any] = {}
        try:
            loaded = json.loads(self.paths.endpoint.read_text(encoding="utf-8"))
            endpoint = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass
        try:
            writable = os.access(self.home_path, os.W_OK)
        except OSError:
            writable = False
        policy = _mapping(configuration.get("policy"))
        try:
            models = _models(configuration, self.home_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            models = []
            issues.append(f"Model inventory cannot be read: {error}")
        return {
            "schema_version": CONSOLE_BOOTSTRAP_SCHEMA,
            "mode": "connected" if sync else "local",
            "data_source": "local-service",
            "version": _package_version(),
            "workspace": {
                "connected": sync is not None,
                "id": sync.installation_id if sync else None,
                "endpoint": sync.endpoint if sync else None,
            },
            "service": {
                "status": "running",
                "started_at": _text(endpoint.get("started_at")),
                "log_path": str(self.paths.log),
                "last_error": _last_error(self.paths.log),
            },
            "setup": {
                "configured": (self.home_path / "config.yaml").is_file(),
                "valid": not issues,
                "schema_version": schema_version,
                "issues": issues,
            },
            "synchronization": {
                "pending": int(status.get("pending_events", 0))
                + int(status.get("pending_outcomes", 0)),
                "dead_letters": int(status.get("dead_letters", 0)),
            },
            "storage": {
                "home": str(self.home_path),
                "runs": str(self.home_path / "runs"),
                "spool": str(self.paths.database),
                "writable": writable,
            },
            "models": models,
            "active_policy": _text(policy.get("version")),
            "active_policy_preset": configured_policy_preset(configuration),
        }

    @staticmethod
    def _repository_status(value: str | Path) -> dict[str, Any]:
        try:
            selected = Path(value).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            selected = Path(str(value))
        result: dict[str, Any] = {
            "path": str(selected),
            "name": selected.name or str(selected),
            "valid": False,
            "dirty": None,
            "root": None,
        }
        if not selected.is_dir():
            result["failure"] = failure_experience("invalid_repository")
            return result
        try:
            root_result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=selected,
                shell=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            result["failure"] = failure_experience("invalid_repository")
            return result
        if root_result.returncode != 0 or not root_result.stdout.strip():
            result["failure"] = failure_experience("invalid_repository")
            return result
        try:
            root = Path(root_result.stdout.strip()).resolve()
            dirty_result = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=root,
                shell=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            result["failure"] = failure_experience("invalid_repository")
            return result
        if dirty_result.returncode != 0:
            result["failure"] = failure_experience("invalid_repository")
            return result
        result.update(
            {
                "path": str(root),
                "name": root.name,
                "valid": True,
                "dirty": bool(dirty_result.stdout.strip()),
                "root": str(root),
            }
        )
        if result["dirty"]:
            result["failure"] = failure_experience("dirty_repository")
        return result

    def _repository_candidates(self, configuration: Mapping[str, Any]) -> list[dict[str, Any]]:
        candidates: dict[str, str] = {}

        def add(value: Any, source: str) -> None:
            if not isinstance(value, str) or not value:
                return
            try:
                key = str(Path(value).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                return
            candidates.setdefault(key, source)

        add(_mapping(configuration.get("setup")).get("repository"), "setup")
        try:
            record = json.loads((self.home_path / "setup-record.json").read_text(encoding="utf-8"))
            add(_mapping(record).get("repository"), "setup-record")
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        runs_root = self.home_path / "runs"
        try:
            run_directories = sorted(
                (item for item in runs_root.iterdir() if item.is_dir()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:100]
        except OSError:
            run_directories = []
        for run_directory in run_directories:
            try:
                task = json.loads((run_directory / "task.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            add(_mapping(task).get("repository_path"), "recent-run")
        repositories: list[dict[str, Any]] = []
        for path, source in candidates.items():
            status = self._repository_status(path)
            status["source"] = source
            repositories.append(status)
        return repositories

    def run_options(self) -> dict[str, Any]:
        configuration, issues, _schema_version = _configuration(self.home_path)
        routing = _mapping(configuration.get("routing"))
        advanced_policies = [
            {
                "id": "configured",
                "label": "Configured policy",
                "description": "Use the active configured policy and deterministic fallback.",
            },
            {
                "id": "bootstrap",
                "label": "Deterministic bootstrap",
                "description": "Ignore advanced active policies for this run.",
            },
        ]
        active = _mapping(routing.get("active_policy"))
        if active.get("state") == "active":
            advanced_policies.append(
                {
                    "id": "active",
                    "label": "Active advanced policy",
                    "description": "Require the configured active routing policy.",
                }
            )
        last_known_good = _mapping(routing.get("last_known_good_policy"))
        if last_known_good.get("state") == "active":
            advanced_policies.append(
                {
                    "id": "last-known-good",
                    "label": "Last-known-good policy",
                    "description": "Use the persisted last-known-good routing policy.",
                }
            )
        budgets = _mapping(configuration.get("budgets"))
        repositories = self._repository_candidates(configuration)
        presets = policy_preset_rows(configuration)
        return {
            "schema_version": CONSOLE_RUN_OPTIONS_SCHEMA,
            "repositories": repositories,
            "default_repository": next(
                (
                    item["path"]
                    for item in repositories
                    if item.get("valid") and not item.get("dirty")
                ),
                repositories[0]["path"] if repositories else None,
            ),
            "delivery_modes": [
                {
                    "id": "suggest",
                    "label": "Suggest",
                    "description": "Preserve the accepted patch and evidence without modifying the repository.",
                },
                {
                    "id": "approve",
                    "label": "Apply with approval",
                    "description": "Pause after selection and wait for your explicit review decision.",
                },
                {
                    "id": "apply",
                    "label": "Apply automatically",
                    "description": "Apply only when the configured delivery authority permits it.",
                },
                {
                    "id": "branch",
                    "label": "Create local branch",
                    "description": "Apply in a separate delivery worktree without switching the original branch.",
                },
                {
                    "id": "pull-request",
                    "label": "Create pull request",
                    "description": "Branch, commit, push, and submit through the configured Git-host adapter.",
                },
            ],
            "approval_modes": [
                {
                    "id": "automatic",
                    "label": "Automatic after acceptance",
                    "description": "Deliver only after acceptance-grade evidence is present.",
                },
                {
                    "id": "review",
                    "label": "Review before apply",
                    "description": "Compatibility option; use Apply with approval as the delivery mode.",
                },
            ],
            "policy_presets": presets,
            "policies": presets,
            "advanced_policies": advanced_policies,
            "routing_modes": ["observe", "recommend", "enforce"],
            "defaults": {
                "delivery_mode": "approve",
                "approval_mode": "automatic",
                "policy_preset": "performance",
                "policy_selection": "configured",
                "routing_mode": str(routing.get("mode") or "observe"),
                "max_attempts": budgets.get("max_attempts", 3),
                "max_cost": budgets.get("max_cost"),
                "max_wall_time": None,
                "verification_required": True,
                "mode": "performance",
            },
            "setup_issues": issues,
        }

    def _configuration_for_mutation(self) -> dict[str, Any]:
        path = self.home_path / "config.yaml"
        if not path.is_file():
            raise ConsoleInputError("Villani is not configured. Run: villani setup")
        configuration, _issues, _schema_version = _configuration(self.home_path)
        if not configuration:
            raise ConsoleInputError("Villani configuration cannot be read. Run: villani doctor")
        return configuration

    def _write_configuration(self, configuration: Mapping[str, Any]) -> None:
        migrated, _migration = migrate_agent_system_configuration(configuration)
        write_configuration_atomic(
            self.home_path / "config.yaml",
            migrated,
            header=_CONFIG_HEADER,
        )

    def models(self, *, refresh_capabilities: bool = False) -> dict[str, Any]:
        configuration, issues, _schema_version = _configuration(self.home_path)
        if not configuration:
            return {
                "schema_version": CONSOLE_MODELS_SCHEMA,
                "models": [],
                "bootstrap_default": None,
                "capability_states": [
                    "UNRATED",
                    "BOOTSTRAP",
                    "OBSERVED",
                    "QUALIFIED",
                    "DISABLED",
                ],
                "setup_issues": issues,
            }
        try:
            document = _model_inventory(
                configuration,
                self.home_path,
                refresh=refresh_capabilities,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ConsoleDataError(f"Model inventory cannot be read: {error}") from error
        return {**document, "schema_version": CONSOLE_MODELS_SCHEMA, "setup_issues": issues}

    def models_detect(self, body: Mapping[str, Any]) -> dict[str, Any]:
        configuration = self._configuration_for_mutation()
        timeout = self._optional_number(body.get("timeout", 1.5), "timeout", minimum=0.1)
        try:
            detections = detect_models(configuration, timeout=float(timeout or 1.5))
            state = update_detection_state(
                load_model_state(self.home_path / "models-state.json"), detections
            )
            write_model_state(self.home_path / "models-state.json", state)
            inventory = inventory_document(
                configuration,
                CapabilityStore(self.home_path / "capabilities").load(),
                state,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ConsoleDataError(f"Model detection failed: {error}") from error
        return {
            **inventory,
            "schema_version": CONSOLE_MODELS_SCHEMA,
            "detections": [item.as_dict() for item in detections],
            "discovery_authority": "advisory",
        }

    def models_test(self, body: Mapping[str, Any]) -> dict[str, Any]:
        configuration = self._configuration_for_mutation()
        backend_name = body.get("backend_name")
        if backend_name is not None and not isinstance(backend_name, str):
            raise ConsoleInputError("backend_name must be a string")
        timeout = self._optional_number(body.get("timeout", 3.0), "timeout", minimum=0.1)
        try:
            state, results = test_models(
                configuration,
                load_model_state(self.home_path / "models-state.json"),
                backend_names=([backend_name] if backend_name else ()),
                timeout=float(timeout or 3.0),
            )
            write_model_state(self.home_path / "models-state.json", state)
        except (OSError, ValueError) as error:
            raise ConsoleInputError(f"Model test failed: {error}") from error
        return {
            "schema_version": "villani.console.model_test.v1",
            "results": results,
            "model_tokens_used": 0,
        }

    @staticmethod
    def _model_boolean(body: Mapping[str, Any], key: str, default: bool = False) -> bool:
        value = body.get(key, default)
        if not isinstance(value, bool):
            raise ConsoleInputError(f"{key} must be a boolean")
        return value

    def models_add(self, body: Mapping[str, Any]) -> dict[str, Any]:
        required: dict[str, str] = {}
        for key in ("backend_name", "model", "provider"):
            value = body.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConsoleInputError(f"{key} is required")
            required[key] = value.strip()
        roles_value = body.get("roles", ["coding", "classification"])
        if not isinstance(roles_value, list) or not all(
            isinstance(item, str) and item for item in roles_value
        ):
            raise ConsoleInputError("roles must be a non-empty string list")
        tool_support = body.get("tool_support")
        if tool_support is not None and not isinstance(tool_support, bool):
            raise ConsoleInputError("tool_support must be true, false, or unknown")
        context_window_value = body.get("context_window")
        if context_window_value is None or context_window_value == "":
            context_window = None
        elif isinstance(context_window_value, bool):
            raise ConsoleInputError("context_window must be a positive integer")
        else:
            try:
                context_window = int(context_window_value)
            except (TypeError, ValueError) as error:
                raise ConsoleInputError("context_window must be a positive integer") from error
        manual_score = self._optional_number(
            body.get("manual_capability_score"),
            "Advanced manual capability score",
        )
        if manual_score is not None and manual_score > 100:
            raise ConsoleInputError("Advanced manual capability score must be at most 100")
        input_price = self._optional_number(body.get("input_cost_per_million"), "input token price")
        output_price = self._optional_number(
            body.get("output_cost_per_million"), "output token price"
        )
        fixed_price = self._optional_number(
            body.get("fixed_cost_per_attempt"), "fixed attempt price"
        )
        billing_mode = str(body.get("billing_mode") or "unknown")
        if billing_mode not in {"unknown", "token", "fixed"}:
            raise ConsoleInputError("billing_mode must be unknown, token, or fixed")
        if billing_mode == "unknown" and any(
            value is not None for value in (input_price, output_price, fixed_price)
        ):
            raise ConsoleInputError("unknown pricing cannot include numeric prices")
        if billing_mode == "token" and (input_price is None or output_price is None):
            raise ConsoleInputError("token pricing requires both input and output prices")
        if billing_mode == "token" and fixed_price is not None:
            raise ConsoleInputError("token pricing cannot include a fixed attempt price")
        if billing_mode == "fixed" and fixed_price is None:
            raise ConsoleInputError("fixed pricing requires a fixed attempt price")
        if billing_mode == "fixed" and any(
            value is not None for value in (input_price, output_price)
        ):
            raise ConsoleInputError("fixed pricing cannot include token prices")
        try:
            with self._configuration_lock:
                configuration = self._configuration_for_mutation()
                add_model_to_configuration(
                    configuration,
                    backend_name=required["backend_name"],
                    model=required["model"],
                    provider=required["provider"],
                    endpoint=(str(body["endpoint"]) if body.get("endpoint") else None),
                    display_name=(str(body["display_name"]) if body.get("display_name") else None),
                    roles=roles_value,
                    api_key_env=(str(body["api_key_env"]) if body.get("api_key_env") else None),
                    tool_support=tool_support,
                    context_window=context_window,
                    make_default=self._model_boolean(body, "make_default"),
                    manual_capability_score=manual_score,
                    billing_mode=billing_mode,
                    input_cost_per_million=input_price,
                    output_cost_per_million=output_price,
                    fixed_cost_per_attempt=fixed_price,
                )
                self._write_configuration(configuration)
        except (OSError, ValueError) as error:
            raise ConsoleInputError(f"Model configuration is invalid: {error}") from error
        return self.models()

    def models_remove(self, body: Mapping[str, Any]) -> dict[str, Any]:
        backend_name = body.get("backend_name")
        if not isinstance(backend_name, str) or not backend_name:
            raise ConsoleInputError("backend_name is required")
        try:
            with self._configuration_lock:
                configuration = self._configuration_for_mutation()
                remove_model_from_configuration(configuration, backend_name)
                self._write_configuration(configuration)
        except (OSError, ValueError) as error:
            raise ConsoleInputError(f"Model removal failed: {error}") from error
        return self.models()

    def models_default(self, body: Mapping[str, Any]) -> dict[str, Any]:
        backend_name = body.get("backend_name")
        if not isinstance(backend_name, str) or not backend_name:
            raise ConsoleInputError("backend_name is required")
        try:
            with self._configuration_lock:
                configuration = self._configuration_for_mutation()
                set_bootstrap_default(configuration, backend_name)
                self._write_configuration(configuration)
        except (OSError, ValueError) as error:
            raise ConsoleInputError(f"Default model selection failed: {error}") from error
        return self.models()

    def policies(self) -> dict[str, Any]:
        configuration, issues, _schema_version = _configuration(self.home_path)
        return {
            "schema_version": CONSOLE_POLICIES_SCHEMA,
            "active_preset": configured_policy_preset(configuration),
            "presets": policy_preset_rows(configuration),
            "setup_issues": issues,
        }

    def policy_select(self, body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            preset = normalize_policy_preset(body.get("preset"))
            with self._configuration_lock:
                configuration = configure_policy_preset(self._configuration_for_mutation(), preset)
                self._write_configuration(configuration)
        except (OSError, ValueError) as error:
            raise ConsoleInputError(f"Policy selection failed: {error}") from error
        return self.policies()

    def policy_preview(self, body: Mapping[str, Any]) -> dict[str, Any]:
        task = body.get("task")
        repository_value = body.get("repository")
        if not isinstance(task, str) or not task.strip():
            raise ConsoleInputError("task instruction is required")
        if not isinstance(repository_value, str) or not repository_value:
            raise ConsoleInputError("repository is required")
        repository = self._repository_status(repository_value)
        if not repository.get("valid"):
            raise ConsoleInputError("repository must be a Git working tree")
        configuration = self._configuration_for_mutation()
        builder = self._policy_preview_builder
        if builder is None:
            from villani_ops.cli.unified import build_policy_preview

            builder = build_policy_preview
        success = body.get("success_criteria")
        try:
            return builder(
                task=task,
                repository=Path(str(repository["root"])),
                success_criteria=(
                    success if isinstance(success, str) and success.strip() else task
                ),
                configuration=configuration,
                preset=(str(body["preset"]) if body.get("preset") else None),
            )
        except (OSError, TypeError, ValueError) as error:
            raise ConsoleInputError(f"Policy preview failed: {error}") from error

    def policy_simulation(self, body: Mapping[str, Any]) -> dict[str, Any]:
        configuration = self._configuration_for_mutation()
        try:
            preset = normalize_policy_preset(
                body.get("preset"), default=configured_policy_preset(configuration)
            )
            snapshot = CapabilityStore(self.home_path / "capabilities").load()
            return simulate_historical_runs(
                runs_root=self.home_path / "runs",
                configuration=configuration,
                backends=configured_backends(configuration),
                snapshot=snapshot,
                preset=preset,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ConsoleInputError(f"Policy simulation failed: {error}") from error

    def validation_discovery(self, repository: str) -> dict[str, Any]:
        status = self._repository_status(repository)
        if not status["valid"] or status["dirty"]:
            return {
                "schema_version": "villani.console.validation_discovery.v1",
                "repository": status,
                "suggestions": [],
                "selected_suggestion_id": None,
                "authority": "none",
                "failure": status.get("failure"),
            }
        root = Path(str(status["root"]))
        fingerprint_parts = [str(root)]
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                shell=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            fingerprint_parts.append(head.stdout.strip() if head.returncode == 0 else "no-head")
            for name in (
                "package.json",
                "pyproject.toml",
                "tox.ini",
                "Cargo.toml",
                "Makefile",
                "justfile",
            ):
                path = root / name
                if path.is_file():
                    metadata = path.stat()
                    fingerprint_parts.append(
                        f"{name}:{metadata.st_size}:{metadata.st_mtime_ns}"
                    )
            fingerprint = hashlib.sha256(
                "\0".join(fingerprint_parts).encode("utf-8")
            ).hexdigest()
            cached = self._validation_cache.get(fingerprint)
            if cached is not None:
                return json.loads(json.dumps(cached))
            discovery = discover_repository_validation(str(root))
        except (OSError, ValueError):
            return {
                "schema_version": "villani.console.validation_discovery.v1",
                "repository": status,
                "suggestions": [],
                "selected_suggestion_id": None,
                "authority": "none",
                "failure": _product_failure("validation_unavailable"),
            }
        result = {
            "schema_version": "villani.console.validation_discovery.v1",
            "repository": status,
            "repository_fingerprint": fingerprint,
            **discovery,
            "failure": (
                None
                if discovery.get("suggestions")
                else _product_failure("validation_unavailable")
            ),
        }
        self._validation_cache[fingerprint] = json.loads(json.dumps(result))
        return result

    @staticmethod
    def _optional_number(value: Any, name: str, *, minimum: float = 0) -> float | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ConsoleInputError(f"{name} must be numeric")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ConsoleInputError(f"{name} must be numeric") from error
        if number < minimum:
            raise ConsoleInputError(f"{name} must be at least {minimum:g}")
        return number

    @staticmethod
    def _configure_experience(
        configuration: dict[str, Any],
        *,
        delivery_mode: str,
        approval_mode: str,
        policy_preset: str,
        policy_selection: str,
        routing_mode: str,
    ) -> None:
        delivery_kinds = {
            "suggest": "patch_export",
            "approve": "local_patch_apply",
            "apply": "local_patch_apply",
            "branch": "local_branch",
            "pull-request": "pull_request",
        }
        if delivery_mode == "patch":
            delivery_mode = "suggest"
        if delivery_mode not in delivery_kinds:
            raise ConsoleInputError(
                "delivery_mode must be suggest, approve, apply, branch, or pull-request"
            )
        if approval_mode not in {"automatic", "review"}:
            raise ConsoleInputError("approval_mode must be automatic or review")
        if policy_selection not in {
            "configured",
            "bootstrap",
            "active",
            "last-known-good",
        }:
            raise ConsoleInputError("policy_selection is invalid")
        if routing_mode not in {"observe", "recommend", "enforce"}:
            raise ConsoleInputError("routing_mode is invalid")
        try:
            selected_preset = normalize_policy_preset(policy_preset)
            configured = apply_policy_preset(configuration, selected_preset)
        except ValueError as error:
            raise ConsoleInputError(str(error)) from error
        configuration.clear()
        configuration.update(configured)
        if approval_mode == "review" and delivery_mode == "apply":
            delivery_mode = "approve"
        requested = delivery_kinds[delivery_mode]
        effective = requested
        existing_delivery = _mapping(configuration.get("delivery"))
        approval_configuration = _mapping(existing_delivery.get("approval"))
        approval_configuration.setdefault("timeout_seconds", 24 * 60 * 60)
        approval_configuration.setdefault("timeout_policy", "reject")
        configuration["delivery"] = {
            **existing_delivery,
            "workflow_version": "villani.delivery_workflow.v1",
            "mode": delivery_mode,
            "materialization_type": effective,
            "requested_materialization_type": requested,
            "approval_mode": "explicit" if delivery_mode == "approve" else "automatic",
            "approval": approval_configuration,
        }
        routing = _mapping(configuration.get("routing"))
        routing["mode"] = routing_mode
        if policy_selection == "bootstrap":
            for key in ("active_policy", "last_known_good_policy"):
                value = _mapping(routing.get(key))
                if value:
                    routing[key] = {**value, "state": "paused"}
        elif policy_selection == "active":
            if _mapping(routing.get("active_policy")).get("state") != "active":
                raise ConsoleInputError("no active advanced policy is configured")
        elif policy_selection == "last-known-good":
            if _mapping(routing.get("last_known_good_policy")).get("state") != "active":
                raise ConsoleInputError("no active last-known-good policy is configured")
            active = _mapping(routing.get("active_policy"))
            if active:
                routing["active_policy"] = {**active, "state": "paused"}
        configuration["routing"] = routing
        configuration["run_experience"] = {
            "delivery_mode": delivery_mode,
            "approval_mode": "explicit" if delivery_mode == "approve" else "automatic",
            "policy_preset": selected_preset,
            "policy_selection": policy_selection,
            "routing_mode": routing_mode,
            "effective_materialization_type": effective,
        }

    def _validation_selection(
        self, body: Mapping[str, Any], repository: Path
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        manual = body.get("validation_command")
        requested_argv = body.get("validation_argv")
        if isinstance(manual, str) and manual.strip():
            argv = parse_manual_command(manual)
            try:
                discovery = discover_repository_validation(repository)
            except (OSError, ValueError) as error:
                discovery = {
                    "suggestions": [],
                    "selected_suggestion_id": None,
                    "authority": "none",
                    "diagnostic": str(error),
                }
            command = confirmed_command(
                argv,
                source="manual_override",
                confidence=1.0,
                confirmed_by="console",
            )
            discovery["selection"] = {
                "source": "manual_override",
                "commands": [command["display_command"]],
                "confirmed": True,
            }
            return [command], discovery

        try:
            discovery = discover_repository_validation(repository)
        except (OSError, ValueError) as error:
            discovery = {
                "suggestions": [],
                "selected_suggestion_id": None,
                "authority": "none",
                "diagnostic": str(error),
            }
        suggestions = [
            item for item in discovery.get("suggestions", []) if isinstance(item, Mapping)
        ]
        selected: Mapping[str, Any] | None = None
        if (
            isinstance(requested_argv, list)
            and requested_argv
            and all(isinstance(item, str) and item for item in requested_argv)
        ):
            selected = next(
                (item for item in suggestions if item.get("argv") == requested_argv),
                None,
            )
            if selected is None:
                raise ConsoleInputError(
                    "the selected validation command no longer matches repository metadata"
                )
        elif suggestions:
            selected = suggestions[0]
        if selected is None:
            discovery["selection"] = {
                "source": "none",
                "commands": [],
                "confirmed": False,
                "alternative_evidence_required": True,
            }
            return [], discovery
        confidence = float(selected.get("confidence") or 0)
        if confidence < CONFIRMATION_THRESHOLD and not bool(body.get("validation_confirmed")):
            raise ConsoleInputError(
                "low-confidence validation discovery must be explicitly confirmed"
            )
        selected_argv = selected.get("argv")
        if not isinstance(selected_argv, list):
            raise ConsoleInputError("the selected validation command is malformed")
        command = confirmed_command(
            selected_argv,
            source=str(selected.get("source") or "metadata_discovery"),
            confidence=confidence,
            confirmed_by="console",
        )
        discovery["selection"] = {
            "suggestion_id": selected.get("suggestion_id"),
            "source": selected.get("source"),
            "commands": [command["display_command"]],
            "confirmed": True,
            "confirmed_by": "console",
        }
        return [command], discovery

    def start_run(self, body: Mapping[str, Any]) -> dict[str, Any]:
        submission_id = body.get("submission_id")
        if submission_id is not None and (
            not isinstance(submission_id, str)
            or not submission_id
            or len(submission_id) > 200
        ):
            raise ConsoleInputError("submission_id is invalid")
        if isinstance(submission_id, str):
            with self._run_lock:
                existing_run_id = self._submission_ids.get(submission_id)
                existing = dict(self._pending_runs.get(existing_run_id or "", {}))
            if existing_run_id and existing:
                return {
                    "schema_version": CONSOLE_RUN_SUBMISSION_SCHEMA,
                    "status": existing.get("status", "RUNNING"),
                    "run_id": existing_run_id,
                    "run_url": f"/console?run={urllib.parse.quote(existing_run_id, safe='')}",
                    "replay_url": f"/console/runs/{urllib.parse.quote(existing_run_id, safe='')}",
                    "validation_commands": existing.get("validation", []),
                    "deduplicated": True,
                    "failure": None,
                }
        task = body.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ConsoleInputError("task instruction is required")
        if len(task) > 200_000:
            raise ConsoleInputError("task instruction exceeds the safe size limit")
        repository_value = body.get("repository")
        if not isinstance(repository_value, str) or not repository_value:
            return {
                "schema_version": CONSOLE_RUN_SUBMISSION_SCHEMA,
                "status": "FAILED",
                "run_id": None,
                "failure": _product_failure("no_repository"),
            }
        repository_status = self._repository_status(repository_value)
        if not repository_status["valid"]:
            return {
                "schema_version": CONSOLE_RUN_SUBMISSION_SCHEMA,
                "status": "FAILED",
                "run_id": None,
                "failure": _product_failure("no_repository"),
            }
        if repository_status["dirty"]:
            return {
                "schema_version": CONSOLE_RUN_SUBMISSION_SCHEMA,
                "status": "FAILED",
                "run_id": None,
                "failure": _product_failure("dirty_repository"),
            }
        repository = Path(str(repository_status["root"]))
        configuration, issues, _schema_version = _configuration(self.home_path)
        if issues:
            return {
                "schema_version": CONSOLE_RUN_SUBMISSION_SCHEMA,
                "status": "FAILED",
                "run_id": None,
                "failure": _product_failure("incomplete_setup"),
            }
        try:
            commands, discovery = self._validation_selection(body, repository)
        except (ConsoleInputError, OSError, ValueError):
            return {
                "schema_version": CONSOLE_RUN_SUBMISSION_SCHEMA,
                "status": "FAILED",
                "run_id": None,
                "failure": _product_failure("validation_unavailable"),
            }
        configuration["repository_validation_commands"] = commands
        configuration["repository_validation_discovery"] = discovery

        defaults = _mapping(configuration.get("budgets"))
        max_attempts_value = body.get("max_attempts", defaults.get("max_attempts", 3))
        if isinstance(max_attempts_value, bool):
            raise ConsoleInputError("max_attempts must be a positive integer")
        try:
            max_attempts = int(max_attempts_value)
        except (TypeError, ValueError) as error:
            raise ConsoleInputError("max_attempts must be a positive integer") from error
        if max_attempts < 1:
            raise ConsoleInputError("max_attempts must be at least 1")
        max_cost = self._optional_number(body.get("max_cost", defaults.get("max_cost")), "budget")
        max_wall_time = self._optional_number(body.get("max_wall_time"), "time limit")
        self._configure_experience(
            configuration,
            delivery_mode=str(body.get("delivery_mode") or "approve"),
            approval_mode=str(body.get("approval_mode") or "automatic"),
            policy_preset=str(body.get("policy_preset") or "performance"),
            policy_selection=str(body.get("policy_selection") or "configured"),
            routing_mode=str(
                body.get("routing_mode")
                or _mapping(configuration.get("routing")).get("mode")
                or "observe"
            ),
        )
        delivery_configuration = _mapping(configuration.get("delivery"))
        approval_configuration = _mapping(delivery_configuration.get("approval"))
        approval_configuration["authenticated_required"] = (
            SyncConfig.load(self.paths.sync_config) is not None
        )
        timeout_value = body.get("approval_timeout_seconds")
        if timeout_value is not None and timeout_value != "":
            try:
                timeout_seconds = int(timeout_value)
            except (TypeError, ValueError) as error:
                raise ConsoleInputError(
                    "approval_timeout_seconds must be a non-negative integer"
                ) from error
            if timeout_seconds < 0:
                raise ConsoleInputError("approval_timeout_seconds must be a non-negative integer")
            approval_configuration["timeout_seconds"] = timeout_seconds
        timeout_policy = body.get("approval_timeout_policy")
        if timeout_policy is not None and timeout_policy not in {
            "",
            "reject",
            "suggest",
            "fail",
        }:
            raise ConsoleInputError("approval_timeout_policy is invalid")
        if timeout_policy:
            approval_configuration["timeout_policy"] = timeout_policy
        if body.get("allow_candidate_change") is not None:
            if not isinstance(body.get("allow_candidate_change"), bool):
                raise ConsoleInputError("allow_candidate_change must be a boolean")
            approval_configuration["allow_candidate_change"] = body["allow_candidate_change"]
        delivery_configuration["approval"] = approval_configuration
        if body.get("commit_delivery_branch") is not None:
            if not isinstance(body.get("commit_delivery_branch"), bool):
                raise ConsoleInputError("commit_delivery_branch must be a boolean")
            delivery_configuration["commit"] = body["commit_delivery_branch"]
        for request_key, configuration_key in (
            ("delivery_branch", "branch"),
            ("git_host_provider", "provider"),
            ("git_remote", "remote"),
            ("pull_request_base", "base_branch"),
        ):
            value = body.get(request_key)
            if value is not None and value != "":
                if not isinstance(value, str):
                    raise ConsoleInputError(f"{request_key} must be text")
                delivery_configuration[configuration_key] = value
        configuration["delivery"] = delivery_configuration
        policy = _mapping(configuration.get("policy"))
        accepted_required = body.get("accepted_candidates_required")
        if accepted_required is not None and accepted_required != "":
            if isinstance(accepted_required, bool):
                raise ConsoleInputError("accepted_candidates_required must be a positive integer")
            try:
                accepted_value = int(accepted_required)
            except (TypeError, ValueError) as error:
                raise ConsoleInputError(
                    "accepted_candidates_required must be a positive integer"
                ) from error
            if accepted_value < 1:
                raise ConsoleInputError("accepted_candidates_required must be at least 1")
            policy["accepted_candidates_required"] = accepted_value
        configuration["policy"] = policy
        run_experience = _mapping(configuration.get("run_experience"))
        run_experience.update(
            {
                "mode": "performance",
                "verification_required": True,
                "default_wall_time_budget": None,
                "repository_validation_optional": not bool(commands),
            }
        )
        configuration["run_experience"] = run_experience

        success = body.get("success_criteria")
        success_criteria = success if isinstance(success, str) and success.strip() else task
        reference_text = body.get("reference_text")
        if reference_text is not None and not isinstance(reference_text, str):
            raise ConsoleInputError("reference_text must be text")
        if isinstance(reference_text, str) and len(reference_text) > 50_000:
            raise ConsoleInputError("reference text exceeds the safe size limit")
        attachment_values = body.get("attachments")
        if attachment_values is not None and not isinstance(attachment_values, list):
            raise ConsoleInputError("attachments must be a list")
        attachment_context: list[str] = []
        attachment_total = 0
        for index, value in enumerate(attachment_values or [], 1):
            attachment = _mapping(value)
            name = attachment.get("name")
            content = attachment.get("content")
            if not isinstance(name, str) or not name or not isinstance(content, str):
                raise ConsoleInputError("each attachment requires a name and text content")
            if len(name) > 255 or len(content) > 100_000:
                raise ConsoleInputError("an attachment exceeds the safe size limit")
            attachment_total += len(content)
            if attachment_total > 250_000:
                raise ConsoleInputError("attachments exceed the combined safe size limit")
            attachment_context.append(f"Attachment {index} ({name}):\n{content}")
        details = []
        if isinstance(reference_text, str) and reference_text:
            details.append(f"Issue or reference text:\n{reference_text}")
        details.extend(attachment_context)
        if details:
            success_criteria = (
                f"{success_criteria}\n\nAdditional task context supplied by the user:\n\n"
                + "\n\n".join(details)
            )
        if len(success_criteria) > 200_000:
            raise ConsoleInputError("success criteria exceed the safe size limit")
        requires_file_changes = body.get("requires_file_changes", True)
        if not isinstance(requires_file_changes, bool):
            raise ConsoleInputError("requires_file_changes must be a boolean")
        run_id = f"run_{uuid.uuid4().hex}"
        cancellation = threading.Event()
        request = ClosedLoopRunRequest(
            task=task,
            repository_path=repository,
            success_criteria=success_criteria,
            runs_root=self.home_path / "runs",
            max_attempts=max_attempts,
            max_cost=max_cost,
            max_wall_time=max_wall_time,
            requires_file_changes=requires_file_changes,
            policy_configuration=configuration,
            run_id=run_id,
            cancellation_event=cancellation,
        )
        record = {
            "run_id": run_id,
            "status": "QUEUED",
            "task": task,
            "success_criteria": success_criteria,
            "repository": str(repository),
            "validation": [item["display_command"] for item in commands],
            "error": None,
            "failure": None,
            "terminal_state": None,
            "queued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        with self._run_lock:
            self._pending_runs[run_id] = record
            self._run_cancellations[run_id] = cancellation
            if isinstance(submission_id, str):
                self._submission_ids[submission_id] = run_id
        thread = threading.Thread(
            target=self._execute_console_run,
            args=(run_id, configuration, request),
            daemon=True,
            name=f"villani-console-{run_id[-8:]}",
        )
        with self._run_lock:
            self._run_threads[run_id] = thread
        thread.start()
        return {
            "schema_version": CONSOLE_RUN_SUBMISSION_SCHEMA,
            "status": "QUEUED",
            "run_id": run_id,
            "run_url": f"/console?run={urllib.parse.quote(run_id, safe='')}",
            "replay_url": f"/console/runs/{urllib.parse.quote(run_id, safe='')}",
            "validation_commands": record["validation"],
            "deduplicated": False,
            "failure": None,
        }

    def _execute_console_run(
        self,
        run_id: str,
        configuration: Mapping[str, Any],
        request: ClosedLoopRunRequest,
    ) -> None:
        with self._run_condition:
            self._pending_runs[run_id]["status"] = "RUNNING"
            self._run_condition.notify_all()
        try:
            builder: Callable[
                [Mapping[str, Any], Callable[[Any], None] | None], Any
            ]
            if self._controller_builder is None:
                from villani_ops.cli.unified import build_controller

                builder = build_controller
            else:
                builder = self._controller_builder

            def on_event(event: Any) -> None:
                with self._run_condition:
                    record = self._pending_runs.get(run_id)
                    if record is not None:
                        record["last_event_sequence"] = int(
                            getattr(event, "sequence", 0) or 0
                        )
                    self._run_condition.notify_all()

            controller = builder(configuration, on_event)
            result = controller.run(request)
            capability_sync: dict[str, Any]
            try:
                capabilities = _mapping(configuration.get("capabilities"))
                scorer = str(capabilities.get("scorer_version") or "empirical_wilson_v1")
                rebuilt = CapabilityStore(self.home_path / "capabilities").rebuild(
                    self.home_path / "runs",
                    scorer_version=scorer,
                )
                capability_sync = {
                    "status": "current",
                    "profile_digest": rebuilt.snapshot.profile_digest,
                }
            except (OSError, ValueError, json.JSONDecodeError) as sync_error:
                capability_sync = {
                    "status": "pending",
                    "error": redact_sensitive_text(str(sync_error)).value,
                }
            with self._run_condition:
                record = self._pending_runs[run_id]
                record["status"] = result.terminal_state
                record["terminal_state"] = result.terminal_state
                record["capability_synchronization"] = capability_sync
                self._run_condition.notify_all()
        except BaseException as error:  # noqa: BLE001 - background boundary
            safe = redact_sensitive_text(str(error)).value
            failure = _product_failure(
                _product_failure_code(safe),
                run_started=True,
            )
            with self._run_condition:
                record = self._pending_runs[run_id]
                record["status"] = "FAILED"
                record["terminal_state"] = "FAILED"
                record["error"] = safe
                record["failure"] = failure
                self._run_condition.notify_all()
        finally:
            with self._run_condition:
                self._run_threads.pop(run_id, None)
                self._run_cancellations.pop(run_id, None)
                self._run_condition.notify_all()

    def run_status(self, run_id: str) -> dict[str, Any]:
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ConsoleInputError("run identifier is invalid")
        with self._run_lock:
            pending = dict(self._pending_runs.get(run_id, {}))
        run_directory = self.home_path / "runs" / run_id
        if run_directory.is_dir():
            return build_product_run(run_directory).model_dump(mode="json")
        if not pending:
            raise ConsoleInputError(f"run not found: {run_id}")
        if pending.get("error"):
            failure = _mapping(pending.get("failure")) or _product_failure(
                _product_failure_code(str(pending["error"])),
                run_started=True,
            )
            reason = str(failure.get("what_failed") or pending["error"])
            failed = self._pending_product_run(run_id, pending)
            failed.update(
                {
                    "current_stage": "Ready",
                    "stage_sentence": "Villani could not begin the run safely.",
                    "stage_transitions": [
                        {
                            "sequence": 1,
                            "timestamp": str(pending.get("queued_at") or "unknown"),
                            "stage": "Ready",
                            "sentence": "Villani could not begin the run safely.",
                        }
                    ],
                    "final_verdict": "Could not prove",
                    "verdict_reason": reason,
                    "available_actions": [
                        {
                            "id": "retry",
                            "label": "Start again",
                            "method": "GET",
                            "href": "/console",
                        }
                    ],
                    "recovery_action": {
                        "label": "Start again",
                        "instruction": str(
                            failure.get("next_action")
                            or "Resolve the stated issue, then start the task again."
                        ),
                        "href": "/console",
                    },
                    "target_repository": {
                        "modified": False,
                        "accounting_status": "known",
                        "statement": "The target repository was not modified.",
                    },
                }
            )
            return failed
        return self._pending_product_run(run_id, pending)

    def _pending_product_run(
        self, run_id: str, pending: Mapping[str, Any]
    ) -> dict[str, Any]:
        timestamp = str(pending.get("queued_at") or "unknown")
        return {
            "schema_version": "villani.product_run.v1",
            "run_identity": {"run_id": run_id, "trace_id": None},
            "task_summary": {
                "task": str(pending.get("task") or "Task instruction was not recorded."),
                "success_criteria": pending.get("success_criteria"),
                "repository": pending.get("repository"),
            },
            "current_stage": "Understanding",
            "stage_sentence": "Understanding the task and choosing a safe route.",
            "stage_transitions": [
                {
                    "sequence": 1,
                    "timestamp": timestamp,
                    "stage": "Understanding",
                    "sentence": "Understanding the task and choosing a safe route.",
                }
            ],
            "final_verdict": None,
            "verdict_reason": None,
            "change_summary": "No file changes were recorded.",
            "changed_files": [],
            "checks_summary": {
                "passed": None,
                "failed": None,
                "not_run": None,
                "unavailable": None,
                "accounting_status": "unknown",
            },
            "requirement_summary": {
                "proved": None,
                "not_proved": None,
                "accounting_status": "unknown",
            },
            "cost": {"value": None, "currency": None, "accounting_status": "unknown"},
            "duration": {"value_ms": None, "accounting_status": "unknown"},
            "agent_system": {
                "name": "Villani agent system",
                "backend": None,
                "model": None,
            },
            "escalation_summary": {
                "attempts": 0,
                "retries": 0,
                "escalations": 0,
                "summary": "No retry or escalation was needed.",
            },
            "available_actions": [
                {
                    "id": "cancel",
                    "label": "Cancel",
                    "method": "POST",
                    "href": f"/v1/console/runs/{run_id}/cancel",
                }
            ],
            "evidence_links": [
                {
                    "label": "Recorded evidence",
                    "href": f"/console/runs/{run_id}/replay",
                    "artifact": "events.jsonl",
                }
            ],
            "recovery_action": None,
            "technical_detail_references": [],
            "target_repository": {
                "modified": False,
                "accounting_status": "known",
                "statement": "The target repository was not modified.",
            },
            "last_event_sequence": max(int(pending.get("last_event_sequence") or 1), 1),
            "updated_at": timestamp,
        }

    def run_events(
        self, run_id: str, *, after_sequence: int = 0, wait_seconds: float = 20.0
    ) -> dict[str, Any]:
        """Wait for a canonical projection change without high-frequency polling."""

        if after_sequence < 0:
            raise ConsoleInputError("after_sequence must be non-negative")
        wait_seconds = min(max(float(wait_seconds), 0.0), 25.0)
        deadline = time.monotonic() + wait_seconds
        while True:
            product = self.run_status(run_id)
            sequence = int(product.get("last_event_sequence") or 1)
            if sequence > after_sequence or product.get("final_verdict") is not None:
                return product
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return product
            with self._run_condition:
                self._run_condition.wait(timeout=remaining)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Request cancellation and return the latest truthful product projection."""

        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ConsoleInputError("run identifier is invalid")
        with self._run_condition:
            cancellation = self._run_cancellations.get(run_id)
            thread = self._run_threads.get(run_id)
            if cancellation is not None:
                cancellation.set()
                self._run_condition.notify_all()
        if cancellation is not None:
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=15.0)
            return self.run_status(run_id)

        run_directory = self.home_path / "runs" / run_id
        if not run_directory.is_dir():
            raise ConsoleInputError(f"run not found: {run_id}")
        current = self.run_status(run_id)
        if current.get("final_verdict") is not None:
            return current
        configuration, _issues, _schema_version = _configuration(self.home_path)
        builder: Callable[[Mapping[str, Any], Callable[[Any], None] | None], Any]
        if self._controller_builder is None:
            from villani_ops.cli.unified import build_controller

            builder = build_controller
        else:
            builder = self._controller_builder
        controller = builder(configuration, None)
        controller.cancel(run_id, self.home_path / "runs")
        return self.run_status(run_id)

    def approval_action(
        self,
        run_id: str,
        body: Mapping[str, Any],
        *,
        authenticated: bool,
        actor: str,
        authentication_type: str,
    ) -> dict[str, Any]:
        """Apply an audited action to a durable controller approval pause."""

        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ConsoleInputError("run identifier is invalid")
        action = body.get("action")
        if action not in {"approve", "reject", "request_rerun", "choose_candidate"}:
            raise ConsoleInputError("approval action is invalid")
        candidate_id = body.get("candidate_id")
        if action == "choose_candidate" and not isinstance(candidate_id, str):
            raise ConsoleInputError("candidate_id is required")
        if candidate_id is not None and not isinstance(candidate_id, str):
            raise ConsoleInputError("candidate_id must be text")
        reason = body.get("reason")
        if reason is None:
            reason = str(action).replace("_", " ")
        if not isinstance(reason, str) or len(reason) > 10_000:
            raise ConsoleInputError("approval reason is invalid")
        connected = SyncConfig.load(self.paths.sync_config) is not None
        if connected and not authenticated:
            raise ConsoleAuthorizationError(
                "connected approval requires an authenticated Console session"
            )

        run_directory = self.home_path / "runs" / run_id
        try:
            manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
            state = json.loads((run_directory / "state.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ConsoleInputError(f"run cannot be loaded: {run_id}") from error
        if not isinstance(manifest, dict) or not isinstance(state, dict):
            raise ConsoleInputError("run bundle is malformed")
        if state.get("state") != "AWAITING_APPROVAL":
            raise ConsoleInputError("run is not awaiting approval")
        persisted = _mapping(_mapping(manifest.get("metadata")).get("policy_configuration"))
        if not persisted:
            raise ConsoleInputError("run has no persisted delivery configuration")
        configuration = dict(persisted)
        current, _issues, _schema_version = _configuration(self.home_path)
        persisted_backends = _mapping(configuration.get("backends"))
        current_backends = _mapping(current.get("backends"))
        for name, backend_value in persisted_backends.items():
            backend = _mapping(backend_value)
            current_backend = _mapping(current_backends.get(name))
            if backend.get("api_key") == "***REDACTED***" and current_backend.get("api_key"):
                backend["api_key"] = current_backend["api_key"]
            persisted_backends[name] = backend
        if persisted_backends:
            configuration["backends"] = persisted_backends
        try:
            builder: Callable[
                [Mapping[str, Any], Callable[[Any], None] | None], Any
            ]
            if self._controller_builder is None:
                from villani_ops.cli.unified import build_controller

                builder = build_controller
            else:
                builder = self._controller_builder
            controller = builder(configuration, None)
            result = controller.approval_action(
                run_id,
                self.home_path / "runs",
                action=action,
                actor=redact_sensitive_text(actor).value,
                authenticated=authenticated,
                authentication_type=authentication_type,
                reason=reason,
                candidate_id=candidate_id,
            )
        except PermissionError as error:
            raise ConsoleAuthorizationError(str(error)) from error
        except (OSError, TypeError, ValueError) as error:
            raise ConsoleInputError(
                f"approval action failed: {redact_sensitive_text(str(error)).value}"
            ) from error
        with self._run_lock:
            record = self._pending_runs.setdefault(run_id, {})
            record["status"] = result.terminal_state
            record["terminal_state"] = result.terminal_state
        return self.run_status(run_id)

    def history(self, *, refresh: bool = False) -> dict[str, Any]:
        warnings: list[str] = []
        entries: list[dict[str, Any]] = []
        try:
            document = self._get_bridge().history(refresh=refresh)
            raw_entries = document.get("entries")
            if isinstance(raw_entries, list):
                entries = [dict(item) for item in raw_entries if isinstance(item, Mapping)]
            raw_warnings = document.get("warnings")
            if isinstance(raw_warnings, list):
                warnings.extend(str(item) for item in raw_warnings)
        except ConsoleDataError as error:
            warnings.append(str(error))

        local_runs = self.spool.console_runs(SyncConfig.load(self.paths.sync_config) is not None)
        merged = {_history_key(entry): entry for entry in entries}
        for local in local_runs:
            key = _history_key(local)
            merged[key] = _merge_entry(merged.get(key, {}), local)
        values = sorted(
            merged.values(),
            key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""),
            reverse=True,
        )
        return {
            "schema_version": CONSOLE_HISTORY_SCHEMA,
            "entries": values,
            "warnings": warnings,
        }

    def replay(self, record_id: str, kind: str) -> dict[str, Any]:
        if not record_id or len(record_id) > 512:
            raise ConsoleDataError("Replay identifier is invalid.")
        value = self._get_bridge().replay(record_id, kind)
        if kind == "run":
            state = self.spool.console_run_states(
                SyncConfig.load(self.paths.sync_config) is not None
            ).get(record_id)
            if state:
                value["synchronization_state"] = state
        return value

    def home(self) -> dict[str, Any]:
        bootstrap = self.bootstrap()
        history = self.history(refresh=False)
        entries = history["entries"]
        runs = [entry for entry in entries if entry.get("kind") == "run"]
        sessions = [entry for entry in entries if entry.get("kind") == "session"]
        completed = [
            entry
            for entry in runs
            if str(entry.get("status", "")).lower() in {"accepted", "completed", "success"}
        ]
        finalized = [
            entry
            for entry in runs
            if str(entry.get("status", "")).lower() not in {"running", "queued", "unknown"}
        ]
        accepted_rate = (len(completed) / len(finalized)) if finalized else None
        return {
            "schema_version": CONSOLE_HOME_SCHEMA,
            "service": bootstrap["service"],
            "models": bootstrap["models"],
            "recent_runs": runs[:5],
            "recent_sessions": sessions[:5],
            "accepted_task_rate": accepted_rate,
            "recent_recovery_events": self.spool.console_recovery_events(limit=5),
            "pending_synchronization": bootstrap["synchronization"]["pending"],
            "setup_issues": bootstrap["setup"]["issues"],
            "warnings": history["warnings"],
        }

    def workspace(self, surface: str | None = None) -> dict[str, Any]:
        sync = SyncConfig.load(self.paths.sync_config)
        allowed = {"fleet", "tasks", "costs", "alerts", "audit", "settings"}
        selected = surface if surface in allowed else None
        items: list[dict[str, Any]] = []
        if sync and selected:
            runs = [
                entry
                for entry in self.history(refresh=False)["entries"]
                if entry.get("kind") == "run"
            ]
            if selected == "fleet":
                items = [
                    {
                        "id": entry.get("logical_id"),
                        "status": entry.get("synchronization_state"),
                        "summary": entry.get("task") or entry.get("logical_id"),
                        "detail": entry.get("repository"),
                        "deep_link": entry.get("deep_link"),
                    }
                    for entry in runs
                ]
            elif selected == "tasks":
                items = [
                    {
                        "id": entry.get("logical_id"),
                        "status": entry.get("status"),
                        "summary": entry.get("task") or "Task text unavailable",
                        "detail": entry.get("synchronization_state"),
                        "deep_link": entry.get("deep_link"),
                    }
                    for entry in runs
                ]
            elif selected == "costs":
                items = [
                    {
                        "id": entry.get("logical_id"),
                        "status": "KNOWN" if entry.get("cost_available") else "UNKNOWN",
                        "summary": entry.get("model") or "Model unavailable",
                        "cost": entry.get("cost"),
                        "currency": entry.get("currency"),
                        "deep_link": entry.get("deep_link"),
                    }
                    for entry in runs
                ]
            elif selected == "alerts":
                items = [
                    {
                        "id": entry.get("logical_id"),
                        "status": "SYNC FAILED",
                        "summary": "Run synchronization needs attention",
                        "detail": entry.get("task"),
                        "deep_link": entry.get("deep_link"),
                    }
                    for entry in runs
                    if entry.get("synchronization_state") == "SYNC FAILED"
                ]
            elif selected == "audit":
                items = [
                    {
                        "id": event.get("id"),
                        "status": event.get("status"),
                        "summary": event.get("name"),
                        "detail": event.get("timestamp"),
                        "deep_link": (
                            f"/console/runs/{urllib.parse.quote(str(event['run_id']), safe='')}"
                            if event.get("run_id")
                            else None
                        ),
                    }
                    for event in self.spool.console_recovery_events(limit=50)
                ]
            elif selected == "settings":
                items = [
                    {
                        "id": sync.installation_id,
                        "status": "CONNECTED",
                        "summary": "Connected workspace",
                        "detail": sync.endpoint,
                        "deep_link": None,
                    }
                ]
        return {
            "schema_version": "villani.console.workspace.v1",
            "connected": sync is not None,
            "workspace_id": sync.installation_id if sync else None,
            "endpoint": sync.endpoint if sync else None,
            "surface": selected,
            "items": items,
            "message": (
                "Workspace data is available from the connected Villani Console."
                if sync
                else "No workspace is connected."
            ),
        }
