from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

from .executables import resolve_installed_executable, resolved_executable_prefix


_VILLANI_ENTRY_POINTS = {"villani", "villani-code", "villani-agentd", "vfr"}


def _exact_path_match(command: str) -> str | None:
    if os.name != "nt":
        return None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / command
        try:
            is_file = candidate.is_file()
        except OSError:
            continue
        if is_file:
            return str(candidate)
    return None


def resolve_command_prefix(command: str) -> list[str] | None:
    explicit = Path(command)
    if explicit.parent != Path("."):
        resolved = str(explicit) if explicit.is_file() else None
    else:
        installed = (
            resolve_installed_executable(command)
            if command in _VILLANI_ENTRY_POINTS
            else None
        )
        if installed is not None and installed.path is not None:
            return list(resolved_executable_prefix(installed))
        resolved = _exact_path_match(command) or shutil.which(command)

    if resolved is None:
        if os.name == "nt" and command.lower() == "echo":
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command]
        return None

    suffix = Path(resolved).suffix.lower()
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        # ``call`` keeps a quoted batch path with spaces from being parsed as
        # the command itself by cmd.exe when more arguments follow it.
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call", resolved]
    if os.name == "nt" and suffix not in {".exe", ".com"}:
        try:
            first_line = (
                Path(resolved)
                .read_text(encoding="utf-8", errors="ignore")
                .splitlines()[0]
            )
        except (OSError, IndexError):
            first_line = ""
        if first_line.startswith("#!") and "python" in first_line.lower():
            return [sys.executable, resolved]
    return [resolved]
