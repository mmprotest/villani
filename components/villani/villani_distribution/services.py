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

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "installed": self.installed,
            "definition": self.definition,
            "active": self.active,
            "user_level": self.user_level,
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
    return subprocess.run(list(command), text=True, capture_output=True, shell=False, check=False)


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
    return service_status(env)


def service_status(environ: Mapping[str, str] | None = None) -> ServiceStatus:
    env = dict(os.environ if environ is None else environ)
    platform = _platform(env)
    path = _definition(platform, env)
    if not path.is_file():
        return ServiceStatus(platform, False, str(path), False)
    if env.get("VILLANI_SERVICE_DRY_RUN") == "1":
        return ServiceStatus(platform, True, str(path), None)
    command = {
        "linux": ["systemctl", "--user", "is-active", "villani-agentd.service"],
        "darwin": ["launchctl", "print", f"{_user_domain()}/{SERVICE_LABEL}"],
        "win32": ["schtasks", "/Query", "/TN", "VillaniAgentd"],
    }[platform]
    completed = _run(command, env)
    return ServiceStatus(platform, True, str(path), completed.returncode == 0)


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
    if delete_data:
        home = villani_home(env)
        if home in {Path.home().resolve(), Path(home.anchor).resolve()}:
            raise ServiceError(f"refusing unsafe data deletion target: {home}")
        if home.exists():
            shutil.rmtree(home, ignore_errors=False)
    return ServiceStatus(platform, False, str(path), False)
