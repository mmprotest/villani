"""Structured, local-only data boundary for the single Villani Console.

The browser never receives a filesystem path to inspect.  Flight Recorder owns
discovery and parsing; Agentd invokes its presentation-neutral JSON adapter and
merges synchronization state from the local spool.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import yaml

from .config import AgentdPaths, SyncConfig
from .platform_process import windows_creation_flags
from .process import terminate_process_tree
from .redaction import redact_sensitive_text
from .spool import SQLiteSpool


CONSOLE_HISTORY_SCHEMA = "villani.console.history.v1"
CONSOLE_BOOTSTRAP_SCHEMA = "villani.console.bootstrap.v1"
CONSOLE_HOME_SCHEMA = "villani.console.home.v1"
SUPPORTED_CONFIG_VERSION = 1
_MAX_VFR_OUTPUT = 16 * 1024 * 1024


class ConsoleDataError(RuntimeError):
    """Safe diagnostic returned when the local replay engine is unavailable."""


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
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return BridgeCommand((str(candidate.resolve()),), "configured Flight Recorder adapter")

    suffix = ".exe" if os.name == "nt" else ""
    sibling = Path(sys.executable).resolve().parent / f"vfr{suffix}"
    if sibling.is_file():
        return BridgeCommand((str(sibling),), "packaged Flight Recorder adapter")

    installed = shutil.which("vfr")
    if installed:
        return BridgeCommand((str(Path(installed).resolve()),), "installed Flight Recorder adapter")

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
        located = _locate_vfr() if command is None else BridgeCommand(tuple(command), "test adapter")
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
            raise ConsoleDataError("Local replay engine returned invalid structured data.") from error
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


def _models(configuration: Mapping[str, Any], home: Path) -> list[dict[str, Any]]:
    detection: dict[str, Any] = {}
    try:
        record = json.loads((home / "setup-record.json").read_text(encoding="utf-8"))
        detection = _mapping(_mapping(record).get("provider"))
    except (OSError, json.JSONDecodeError):
        pass
    detected_models = {
        str(value)
        for value in detection.get("available_models", [])
        if isinstance(value, str) and value
    }
    connection_status = _text(detection.get("connection_status"))
    result: list[dict[str, Any]] = []
    configured_ids: set[str] = set()
    for name, raw in _mapping(configuration.get("backends")).items():
        backend = _mapping(raw)
        metadata = _mapping(backend.get("metadata"))
        context = _mapping(metadata.get("context"))
        model = _text(backend.get("model")) or str(name)
        configured_ids.add(model)
        capability = _text(metadata.get("capability_status")) or "unrated"
        pricing_source = _text(metadata.get("pricing_metadata_source"))
        result.append(
            {
                "id": model,
                "provider": _text(backend.get("provider")) or "unknown",
                "endpoint": _text(backend.get("base_url")),
                "configured": True,
                "detected": model in detected_models,
                "available": (
                    connection_status == "connected"
                    if model in detected_models
                    else None
                ),
                "capability": capability,
                "context_window": _number(context.get("context_window")),
                "pricing_status": "known" if pricing_source else "unknown",
            }
        )
    for model in sorted(detected_models - configured_ids):
        context = _mapping(_mapping(detection.get("context_metadata")).get(model))
        result.append(
            {
                "id": model,
                "provider": _text(detection.get("provider_identifier")) or "unknown",
                "endpoint": _text(detection.get("detected_endpoint")),
                "configured": False,
                "detected": True,
                "available": connection_status == "connected",
                "capability": "unrated",
                "context_window": _number(context.get("context_window")),
                "pricing_status": (
                    "known" if _text(detection.get("pricing_metadata_source")) else "unknown"
                ),
            }
        )
    return result


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
    ) -> None:
        self.paths = paths
        self.spool = spool
        self._bridge = bridge

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
            "models": _models(configuration, self.home_path),
            "active_policy": _text(policy.get("version")),
        }

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
        completed = [entry for entry in runs if str(entry.get("status", "")).lower() in {"accepted", "completed", "success"}]
        finalized = [entry for entry in runs if str(entry.get("status", "")).lower() not in {"running", "queued", "unknown"}]
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
