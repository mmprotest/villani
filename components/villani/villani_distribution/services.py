from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from villani_agentd.client import ClientError, LocalClient
from villani_agentd.config import AgentdPaths, ServerConfig
from villani_agentd.lifecycle import _pid_exists, start_background, stop_background
from villani_ops.closed_loop.durable_io import write_json_atomic

from .migrations import check_upgrade

SERVICE_LABEL = "com.villani.agentd"


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    platform: str
    installed: bool
    definition: str
    active: bool | None
    user_level: bool = True
    running: bool = False
    automatic_start: bool = False
    pid: int | None = None
    stale_pid: bool = False
    log_path: str = ""
    last_error: str | None = None
    console_url: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "installed": self.installed,
            "definition": self.definition,
            "active": self.active,
            "user_level": self.user_level,
            "running": self.running,
            "automatic_start": self.automatic_start,
            "pid": self.pid,
            "stale_pid": self.stale_pid,
            "log_path": self.log_path,
            "last_error": self.last_error,
            "console_url": self.console_url,
        }


def villani_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("VILLANI_HOME") or Path.home() / ".villani").expanduser().resolve()


def _platform(environ: Mapping[str, str]) -> str:
    override = environ.get("VILLANI_SERVICE_PLATFORM")
    if override:
        if override not in {"linux", "darwin", "win32"}:
            raise ServiceError(f"unsupported service platform override: {override}")
        return override
    return sys.platform


def _test_root(environ: Mapping[str, str]) -> Path | None:
    value = environ.get("VILLANI_SERVICE_TEST_ROOT")
    return Path(value).resolve() if value else None


def _user_domain() -> str:
    getuid = getattr(os, "getuid", None)
    return f"gui/{getuid() if getuid is not None else 501}"


def _definition(platform: str, environ: Mapping[str, str]) -> Path:
    root = _test_root(environ)
    if root:
        suffix = {
            "linux": Path("systemd/user/villani-agentd.service"),
            "darwin": Path("LaunchAgents/com.villani.agentd.plist"),
            "win32": Path("TaskScheduler/VillaniAgentd.json"),
        }[platform]
        return root / suffix
    if platform == "linux":
        return Path.home() / ".config" / "systemd" / "user" / "villani-agentd.service"
    if platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    if platform == "win32":
        return villani_home(environ) / "service" / "windows-task.json"
    raise ServiceError(f"unsupported service platform: {platform}")


def _agentd_executable() -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    sibling = Path(sys.executable).resolve().parent / f"villani-agentd{suffix}"
    found = shutil.which("villani-agentd")
    if sibling.is_file():
        return sibling
    if found:
        return Path(found).resolve()
    raise ServiceError("villani-agentd executable is not installed beside villani")


def _quote_systemd(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_definition(platform: str, path: Path, agentd: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [str(agentd), "service-run"]
    if platform == "linux":
        content = (
            "[Unit]\nDescription=Villani local agent daemon\nAfter=default.target\n\n"
            "[Service]\nType=simple\n"
            f"ExecStart={' '.join(_quote_systemd(part) for part in command)}\n"
            "Restart=on-failure\nRestartSec=2\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        path.write_text(content, encoding="utf-8", newline="\n")
    elif platform == "darwin":
        with path.open("wb") as handle:
            plistlib.dump(
                {
                    "Label": SERVICE_LABEL,
                    "ProgramArguments": command,
                    "RunAtLoad": True,
                    "KeepAlive": {"SuccessfulExit": False},
                    "ProcessType": "Background",
                },
                handle,
                sort_keys=True,
            )
    else:
        path.write_text(
            json.dumps({"task_name": "VillaniAgentd", "command": command}, sort_keys=True),
            encoding="utf-8",
        )


def _run(command: Sequence[str], environ: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    if environ.get("VILLANI_SERVICE_DRY_RUN") == "1":
        return subprocess.CompletedProcess(list(command), 0, "dry-run", "")
    try:
        return subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            shell=False,
            check=False,
            timeout=20,
        )
    except subprocess.TimeoutExpired as error:
        raise ServiceError("Villani Service manager command timed out") from error


def _paths(environ: Mapping[str, str]) -> AgentdPaths:
    return AgentdPaths(villani_home(environ) / "agentd")


def _state_path(environ: Mapping[str, str]) -> Path:
    return villani_home(environ) / "service" / "state.json"


def _write_state(environ: Mapping[str, str], *, automatic_start: bool) -> None:
    write_json_atomic(
        _state_path(environ),
        {
            "schema_version": "villani.service_state.v1",
            "automatic_start": automatic_start,
        },
    )


def _automatic_start(environ: Mapping[str, str], definition_exists: bool) -> bool:
    path = _state_path(environ)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return definition_exists
    return bool(value.get("automatic_start")) if isinstance(value, dict) else definition_exists


def _last_error(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 262_144))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return "Villani Service log could not be read."
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and str(value.get("level", "")).lower() == "error":
            event = value.get("event") or value.get("message") or "service_error"
            return f"{event} (see {path})"
        if "error" in line.lower() or "traceback" in line.lower():
            return f"Villani Service reported an error (see {path})."
    return None


def _runtime_details(paths: AgentdPaths) -> tuple[bool, int | None, bool, str | None, str | None]:
    pid: int | None = None
    endpoint: str | None = None
    if not paths.endpoint.is_file():
        return False, None, False, None, _last_error(paths.log)
    try:
        value = json.loads(paths.endpoint.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("endpoint is not an object")
        raw_pid = value.get("pid")
        pid = int(raw_pid) if raw_pid is not None else None
        endpoint = str(value.get("endpoint") or "").rstrip("/") or None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, None, True, None, "Villani Service endpoint state is invalid."
    try:
        healthy = LocalClient.from_files(paths).health().get("status") == "ok"
    except ClientError:
        alive = bool(pid and _pid_exists(pid))
        return (
            False,
            pid,
            not alive,
            None,
            (
                "Villani Service has stale process state."
                if not alive
                else "Villani Service process is present but not responding."
            ),
        )
    return bool(healthy), pid, False, f"{endpoint}/console" if healthy and endpoint else None, _last_error(paths.log)


def _manager_command(platform: str, operation: str, path: Path) -> list[str]:
    if platform == "linux":
        return ["systemctl", "--user", operation, "villani-agentd.service"]
    if platform == "darwin":
        domain = _user_domain()
        if operation == "stop":
            return ["launchctl", "kill", "SIGTERM", f"{domain}/{SERVICE_LABEL}"]
        return ["launchctl", "kickstart", f"{domain}/{SERVICE_LABEL}"]
    return ["schtasks", "/End" if operation == "stop" else "/Run", "/TN", "VillaniAgentd"]


def install_service(environ: Mapping[str, str] | None = None) -> ServiceStatus:
    env = dict(os.environ if environ is None else environ)
    check_upgrade(villani_home(env), apply=True)
    platform = _platform(env)
    path = _definition(platform, env)
    agentd = _agentd_executable()
    _write_definition(platform, path, agentd)
    commands: tuple[list[str], ...]
    if platform == "linux":
        commands = (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "villani-agentd.service"],
        )
    elif platform == "darwin":
        domain = _user_domain()
        commands = (
            ["launchctl", "bootout", domain, str(path)],
            ["launchctl", "bootstrap", domain, str(path)],
            ["launchctl", "kickstart", f"{domain}/{SERVICE_LABEL}"],
        )
    else:
        action = subprocess.list2cmdline([str(agentd), "service-run"])
        commands = (
            ["schtasks", "/Create", "/TN", "VillaniAgentd", "/SC", "ONLOGON", "/TR", action, "/F"],
            ["schtasks", "/Run", "/TN", "VillaniAgentd"],
        )
    for command in commands:
        completed = _run(command, env)
        if completed.returncode != 0 and not (platform == "darwin" and "bootout" in command):
            raise ServiceError(
                (completed.stderr or completed.stdout or "service command failed").strip()
            )
    _write_state(env, automatic_start=True)
    return service_status(env)


def service_status(environ: Mapping[str, str] | None = None) -> ServiceStatus:
    env = dict(os.environ if environ is None else environ)
    platform = _platform(env)
    path = _definition(platform, env)
    installed = path.is_file()
    active: bool | None = False
    if installed and env.get("VILLANI_SERVICE_DRY_RUN") == "1":
        active = None
    elif installed:
        command = {
            "linux": ["systemctl", "--user", "is-active", "villani-agentd.service"],
            "darwin": ["launchctl", "print", f"{_user_domain()}/{SERVICE_LABEL}"],
            "win32": ["schtasks", "/Query", "/TN", "VillaniAgentd"],
        }[platform]
        completed = _run(command, env)
        active = completed.returncode == 0
    paths = _paths(env)
    running, pid, stale, console_url, last_error = _runtime_details(paths)
    return ServiceStatus(
        platform,
        installed,
        str(path),
        active,
        True,
        running,
        _automatic_start(env, installed),
        pid,
        stale,
        str(paths.log),
        last_error,
        console_url,
    )


def _public_error(error: BaseException) -> ServiceError:
    message = str(error).replace("villani-agentd", "Villani Service").replace(
        "agentd", "Villani Service"
    )
    return ServiceError(message or error.__class__.__name__)


def start_service(
    *,
    automatic_start: bool = False,
    environ: Mapping[str, str] | None = None,
) -> ServiceStatus:
    """Start once, recovering stale endpoint state without creating duplicates."""

    env = dict(os.environ if environ is None else environ)
    check_upgrade(villani_home(env), apply=True)
    current = service_status(env)
    if current.running:
        if automatic_start and not current.automatic_start:
            return install_service(env)
        return current
    paths = _paths(env)
    if current.stale_pid:
        paths.endpoint.unlink(missing_ok=True)
        paths.token.unlink(missing_ok=True)
    elif current.pid is not None:
        raise ServiceError(
            "Villani Service process is present but unresponsive. Run `villani service stop` "
            "and inspect the service log before retrying."
        )
    if automatic_start:
        return install_service(env)
    if env.get("VILLANI_SERVICE_DRY_RUN") == "1":
        _write_state(env, automatic_start=False)
        return service_status(env)
    try:
        start_background(ServerConfig(host="127.0.0.1", port=0), paths, timeout=10)
        _write_state(env, automatic_start=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise _public_error(error) from error
    result = service_status(env)
    if not result.running:
        raise ServiceError(
            f"Villani Service did not become ready. Inspect the log at {result.log_path}."
        )
    return result


def stop_service(
    *, environ: Mapping[str, str] | None = None, timeout: float = 10
) -> ServiceStatus:
    """Stop safely and idempotently, retaining automatic-start configuration."""

    env = dict(os.environ if environ is None else environ)
    current = service_status(env)
    paths = _paths(env)
    if env.get("VILLANI_SERVICE_DRY_RUN") == "1":
        return current
    if current.running:
        try:
            stop_background(paths, timeout=timeout)
        except (OSError, RuntimeError, ValueError) as error:
            raise _public_error(error) from error
    elif current.stale_pid:
        paths.endpoint.unlink(missing_ok=True)
        paths.token.unlink(missing_ok=True)
    elif current.pid is not None:
        raise ServiceError(
            "Villani Service is unresponsive and its process could not be verified. "
            f"Inspect {current.log_path}."
        )
    if current.installed and current.active:
        completed = _run(
            _manager_command(current.platform, "stop", Path(current.definition)), env
        )
        if completed.returncode != 0:
            raise ServiceError(
                (completed.stderr or completed.stdout or "Villani Service did not stop").strip()
            )
    return service_status(env)


def restart_service(
    *,
    automatic_start: bool | None = None,
    environ: Mapping[str, str] | None = None,
) -> ServiceStatus:
    env = dict(os.environ if environ is None else environ)
    current = service_status(env)
    selected_automatic = (
        current.automatic_start if automatic_start is None else automatic_start
    )
    stop_service(environ=env)
    return start_service(automatic_start=selected_automatic, environ=env)


def uninstall_service(
    *,
    delete_data: bool = False,
    confirm_delete_data: bool = False,
    environ: Mapping[str, str] | None = None,
) -> ServiceStatus:
    env = dict(os.environ if environ is None else environ)
    if delete_data and not confirm_delete_data:
        raise ServiceError("--delete-data requires --confirm-delete-data")
    platform = _platform(env)
    path = _definition(platform, env)
    commands: tuple[list[str], ...]
    if platform == "linux":
        commands = (
            ["systemctl", "--user", "disable", "--now", "villani-agentd.service"],
            ["systemctl", "--user", "daemon-reload"],
        )
    elif platform == "darwin":
        commands = (["launchctl", "bootout", _user_domain(), str(path)],)
    else:
        commands = (
            ["schtasks", "/End", "/TN", "VillaniAgentd"],
            ["schtasks", "/Delete", "/TN", "VillaniAgentd", "/F"],
        )
    for command in commands:
        _run(command, env)
    path.unlink(missing_ok=True)
    _state_path(env).unlink(missing_ok=True)
    if delete_data:
        home = villani_home(env)
        if home in {Path.home().resolve(), Path(home.anchor).resolve()}:
            raise ServiceError(f"refusing unsafe data deletion target: {home}")
        if home.exists():
            shutil.rmtree(home, ignore_errors=False)
    return service_status(env)
