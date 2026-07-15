"""Resolve console scripts from the Python installation that owns Villani.

The interpreter path is deliberately kept lexical.  Resolving a virtual
environment's ``python`` symlink can move discovery to the base interpreter
and make sibling console scripts disappear.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


_SCRIPT_QUERY = (
    "import json, sysconfig; "
    "print(json.dumps({'scripts': sysconfig.get_path('scripts')}))"
)
_DEFAULT_QUERY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ScriptsDirectoryDiscovery:
    """The scripts directory reported by a selected interpreter."""

    interpreter: Path
    path: Path | None
    source: str
    diagnostic: str


@dataclass(frozen=True, slots=True)
class ExecutableResolution:
    """Auditable result of installed executable discovery."""

    command: str
    path: Path | None
    source: str
    candidates: tuple[Path, ...]
    diagnostic: str
    interpreter: Path
    scripts_directory: Path | None
    path_searched: bool


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _same_literal_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def _validated_directory(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        return _absolute_without_resolving(candidate) if candidate.is_dir() else None
    except OSError:
        return None


def discover_interpreter_scripts_directory(
    interpreter: Path | None = None,
    *,
    timeout_seconds: float = _DEFAULT_QUERY_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
) -> ScriptsDirectoryDiscovery:
    """Return ``sysconfig``'s scripts directory for ``interpreter``.

    Another interpreter is queried in a bounded subprocess.  The query imports
    only Python's standard library and never imports Villani or repository code.
    """

    selected = _absolute_without_resolving(interpreter or Path(sys.executable))
    current = _same_literal_path(selected, Path(sys.executable))
    if current:
        try:
            scripts = _validated_directory(sysconfig.get_path("scripts"))
        except (KeyError, OSError, TypeError, ValueError) as error:
            return ScriptsDirectoryDiscovery(
                selected,
                None,
                "current_interpreter_sysconfig",
                f"Current interpreter scripts discovery failed: {type(error).__name__}.",
            )
        if scripts is None:
            return ScriptsDirectoryDiscovery(
                selected,
                None,
                "current_interpreter_sysconfig",
                "Current interpreter returned no existing absolute scripts directory.",
            )
        return ScriptsDirectoryDiscovery(
            selected,
            scripts,
            "current_interpreter_sysconfig",
            f"Current interpreter reported scripts directory {scripts}.",
        )

    if timeout_seconds <= 0:
        raise ValueError("scripts-directory query timeout must be positive")
    try:
        completed = subprocess.run(
            [str(selected), "-I", "-c", _SCRIPT_QUERY],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            shell=False,
            check=False,
            timeout=timeout_seconds,
            env=dict(environ) if environ is not None else None,
        )
    except subprocess.TimeoutExpired:
        return ScriptsDirectoryDiscovery(
            selected,
            None,
            "external_interpreter_query",
            f"Selected interpreter scripts query timed out after {timeout_seconds:g} seconds.",
        )
    except OSError as error:
        return ScriptsDirectoryDiscovery(
            selected,
            None,
            "external_interpreter_query",
            f"Selected interpreter scripts query could not start: {type(error).__name__}.",
        )
    if completed.returncode != 0:
        return ScriptsDirectoryDiscovery(
            selected,
            None,
            "external_interpreter_query",
            f"Selected interpreter scripts query exited with code {completed.returncode}.",
        )
    try:
        document = json.loads(completed.stdout.strip())
    except (TypeError, json.JSONDecodeError):
        document = None
    scripts = _validated_directory(
        document.get("scripts") if isinstance(document, dict) else None
    )
    if scripts is None:
        return ScriptsDirectoryDiscovery(
            selected,
            None,
            "external_interpreter_query",
            "Selected interpreter returned no existing absolute scripts directory.",
        )
    return ScriptsDirectoryDiscovery(
        selected,
        scripts,
        "external_interpreter_query",
        f"Selected interpreter reported scripts directory {scripts}.",
    )


def _entry_point_names(
    command: str,
    *,
    windows: bool,
    pathext: str = "",
) -> tuple[str, ...]:
    name = Path(command).name
    if not windows:
        return (name,)
    lower = name.lower()
    known_suffixes = {".exe", ".com", ".cmd", ".bat", ".py"}
    if Path(lower).suffix in known_suffixes:
        return (name,)
    separator = ";" if windows else os.pathsep
    extensions = [
        extension.lower() for extension in pathext.split(separator) if extension.strip()
    ]
    ordered_extensions = [".exe", ".com", ".cmd", ".bat"]
    for extension in extensions:
        normalized = extension if extension.startswith(".") else f".{extension}"
        if normalized not in ordered_extensions:
            ordered_extensions.append(normalized)
    values = [f"{name}{extension}" for extension in ordered_extensions]
    values.extend((f"{name}-script.py", name))
    return tuple(dict.fromkeys(values))


def _usable_file(path: Path, *, windows: bool) -> bool:
    try:
        return path.is_file() and (windows or os.access(path, os.X_OK))
    except OSError:
        return False


def resolve_installed_executable(
    command: str,
    *,
    interpreter: Path | None = None,
    additional_search_dirs: Sequence[Path] = (),
    compatibility_fallbacks: Sequence[Path] = (),
    timeout_seconds: float = _DEFAULT_QUERY_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
) -> ExecutableResolution:
    """Resolve ``command`` from the selected Python installation.

    Search order is the interpreter-reported scripts directory, the literal
    interpreter parent, explicit directories, ``PATH``, then compatibility
    fallbacks.  No interpreter path is resolved through a symlink.
    """

    if not command.strip() or Path(command).name != command:
        raise ValueError("installed executable command must be a non-empty base name")
    env = dict(os.environ if environ is None else environ)
    discovery = discover_interpreter_scripts_directory(
        interpreter,
        timeout_seconds=timeout_seconds,
        environ=env,
    )
    selected = discovery.interpreter
    windows = os.name == "nt"
    names = _entry_point_names(
        command,
        windows=windows,
        pathext=env.get("PATHEXT", ""),
    )
    ordered_directories: list[tuple[str, Path]] = []
    if discovery.path is not None:
        ordered_directories.append(("interpreter_scripts", discovery.path))
    ordered_directories.append(("interpreter_parent", selected.parent))
    ordered_directories.extend(
        ("additional_search_dir", _absolute_without_resolving(item))
        for item in additional_search_dirs
    )
    candidates: list[Path] = []
    seen: set[str] = set()
    for source, directory in ordered_directories:
        for name in names:
            candidate = directory / name
            key = os.path.normcase(os.path.abspath(str(candidate)))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
            if _usable_file(candidate, windows=windows):
                return ExecutableResolution(
                    command,
                    candidate,
                    source,
                    tuple(candidates),
                    (
                        f"Resolved {command!r} for interpreter {selected} from {source}; "
                        f"scripts directory attempted: {discovery.path or 'unavailable'}; "
                        "PATH searched: no."
                    ),
                    selected,
                    discovery.path,
                    False,
                )

    found: str | None = None
    for name in names:
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            break
    if found:
        candidate = _absolute_without_resolving(Path(found))
        key = os.path.normcase(str(candidate))
        if key not in seen:
            candidates.append(candidate)
            seen.add(key)
        return ExecutableResolution(
            command,
            candidate,
            "PATH",
            tuple(candidates),
            (
                f"Resolved {command!r} for interpreter {selected} through PATH; "
                f"scripts directory attempted: {discovery.path or 'unavailable'}; "
                "PATH searched: yes."
            ),
            selected,
            discovery.path,
            True,
        )

    for fallback in compatibility_fallbacks:
        candidate = _absolute_without_resolving(fallback)
        key = os.path.normcase(str(candidate))
        if key not in seen:
            candidates.append(candidate)
            seen.add(key)
        if _usable_file(candidate, windows=windows):
            return ExecutableResolution(
                command,
                candidate,
                "compatibility_fallback",
                tuple(candidates),
                (
                    f"Resolved {command!r} for interpreter {selected} from a compatibility "
                    f"fallback; scripts directory attempted: {discovery.path or 'unavailable'}; "
                    "PATH searched: yes."
                ),
                selected,
                discovery.path,
                True,
            )

    rendered_candidates = ", ".join(str(item) for item in candidates) or "none"
    diagnostic = (
        f"Installed executable {command!r} was not found for interpreter {selected}. "
        f"Scripts directory attempted: {discovery.path or 'unavailable'} "
        f"({discovery.diagnostic}) Candidate paths: {rendered_candidates}. "
        "PATH searched: yes. Reinstall Villani into the selected interpreter, then retry."
    )
    return ExecutableResolution(
        command,
        None,
        "not_found",
        tuple(candidates),
        diagnostic,
        selected,
        discovery.path,
        True,
    )


def resolved_executable_prefix(
    resolution: ExecutableResolution,
    *,
    interpreter: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return a shell-free argv prefix for a successful resolution."""

    if resolution.path is None:
        raise FileNotFoundError(resolution.diagnostic)
    path = resolution.path
    if os.name != "nt":
        return (str(path),)
    suffix = path.suffix.lower()
    if suffix in {".cmd", ".bat"}:
        env = os.environ if environ is None else environ
        return (
            env.get("COMSPEC", "cmd.exe"),
            "/d",
            "/c",
            "call",
            str(path),
        )
    if suffix == ".py":
        selected = _absolute_without_resolving(interpreter or resolution.interpreter)
        return (str(selected), str(path))
    return (str(path),)
