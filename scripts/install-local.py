#!/usr/bin/env python3
"""Install a verified local Villani environment without starting services."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", subprocess.list2cmdline(command))
    subprocess.run(command, cwd=cwd or ROOT, check=True)


def _version(command: list[str], label: str) -> tuple[int, ...]:
    try:
        completed = subprocess.run(command, text=True, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(
            f"{label} is required but was not executable: {error}"
        ) from error
    text = (completed.stdout or completed.stderr).strip().lstrip("v")
    try:
        return tuple(int(part) for part in text.split(".")[:3])
    except ValueError as error:
        raise SystemExit(f"Could not parse {label} version from {text!r}.") from error


def _scripts_directory(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _environment_python(venv: Path) -> Path:
    return _scripts_directory(venv) / ("python.exe" if os.name == "nt" else "python")


def _entry_point(python: Path, name: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(python),
            "-I",
            str(ROOT / "scripts" / "resolve-installed-executable.py"),
            name,
        ],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"Installed executable resolver failed for {name!r}: "
            f"{completed.stderr.strip()}"
        )
    try:
        resolution = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"Installed executable resolver returned malformed output for {name!r}."
        ) from error
    if not isinstance(resolution, dict) or not resolution.get("path"):
        diagnostic = (
            resolution.get("diagnostic") if isinstance(resolution, dict) else None
        )
        raise SystemExit(str(diagnostic or f"Missing installed executable {name!r}."))
    if resolution.get("source") not in {"interpreter_scripts", "interpreter_parent"}:
        raise SystemExit(
            f"Editable installation resolved {name!r} outside the selected "
            f"environment: {resolution.get('diagnostic')}"
        )
    return resolution


def _pip_available(python: Path) -> bool:
    completed = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _remove_environment(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _transaction_path(target: Path, role: str) -> Path:
    return target.parent / f".{target.name}.villani-{role}-{uuid.uuid4().hex}"


def _publish_staged_environment(
    staged: Path,
    target: Path,
    *,
    verify: Callable[[Path], None],
) -> None:
    """Publish one staged environment and restore the previous target on failure."""

    backup = _transaction_path(target, "backup")
    previous_exists = target.exists()
    try:
        if previous_exists:
            target.replace(backup)
        staged.replace(target)
        verify(target)
    except BaseException:
        _remove_environment(target)
        if previous_exists and backup.exists():
            backup.replace(target)
        _remove_environment(staged)
        raise
    else:
        _remove_environment(backup)


def _install_python_packages(python: Path, *, development: bool) -> None:
    packages = [
        (ROOT / "components" / "villani-code", "dev"),
        (ROOT / "components" / "villani-ops", "test"),
        (ROOT / "components" / "villani-agentd", "test"),
        (ROOT / "components" / "villani", "test"),
    ]
    command = [str(python), "-m", "pip", "install"]
    for package, development_extra in packages:
        specification = (
            f"{package}[{development_extra}]" if development else str(package)
        )
        command.extend(["-e", specification])
    _run(command)


def _verify_environment(venv: Path) -> dict[str, dict[str, object]]:
    python = _environment_python(venv)
    if not python.is_file():
        raise RuntimeError(f"Virtual environment Python is missing at {python}.")
    _run(
        [
            str(python),
            "-c",
            "import villani_distribution, villani_ops, villani_code, villani_agentd",
        ]
    )
    resolutions = {
        name: _entry_point(python, name)
        for name in ("villani", "villani-code", "villani-agentd", "vfr")
    }
    for resolution in resolutions.values():
        prefix = resolution.get("prefix")
        if not isinstance(prefix, list) or not all(
            isinstance(item, str) for item in prefix
        ):
            raise RuntimeError("Installed executable resolver returned an invalid prefix.")
        _run([*prefix, "--help"])
    _run([str(python), "-m", "pip", "check"])
    return resolutions


def _repair_command(venv: Path, *, development: bool) -> str:
    command = [sys.executable, str(ROOT / "scripts" / "install-local.py"), "--venv", str(venv)]
    if development:
        command.append("--development")
    return subprocess.list2cmdline(command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv")
    parser.add_argument(
        "--development",
        action="store_true",
        help="also install optional test, lint, and type-check dependencies",
    )
    args = parser.parse_args()

    venv = args.venv.resolve()
    try:
        return _install(venv, development=args.development)
    except KeyboardInterrupt:
        print("Installation interrupted; the prior environment was restored.", file=sys.stderr)
        print(f"Repair command: {_repair_command(venv, development=args.development)}", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, subprocess.CalledProcessError, SystemExit) as error:
        message = error.code if isinstance(error, SystemExit) else error
        print(f"Installation failed; the prior environment was restored: {message}", file=sys.stderr)
        print(f"Repair command: {_repair_command(venv, development=args.development)}", file=sys.stderr)
        return 1


def _install(venv: Path, *, development: bool) -> int:

    if sys.version_info < (3, 11):
        raise SystemExit(
            f"Python 3.11 or newer is required; found {sys.version.split()[0]}."
        )
    node_command = shutil.which("node")
    npm_command = shutil.which("npm")
    if node_command is None:
        raise SystemExit(
            "Node.js 20 or newer is required; `node` was not found on PATH."
        )
    if npm_command is None:
        raise SystemExit("npm is required; `npm` was not found on PATH.")
    node_version = _version([node_command, "--version"], "Node.js")
    if node_version < (20, 0):
        raise SystemExit(
            "Node.js 20 or newer is required; found "
            f"{'.'.join(map(str, node_version))}."
        )
    _version([npm_command, "--version"], "npm")

    web = ROOT / "components" / "villani-web"
    _run([npm_command, "ci"], cwd=web)
    _run([npm_command, "run", "build"], cwd=web)
    flight = ROOT / "components" / "villani-flight-recorder"
    _run([npm_command, "ci"], cwd=flight)
    _run([npm_command, "run", "build"], cwd=flight)

    staged = _transaction_path(venv, "staging")
    _remove_environment(staged)
    resolutions: dict[str, dict[str, object]] = {}
    try:
        _run([sys.executable, "-m", "venv", str(staged)])
        staged_python = _environment_python(staged)
        if not staged_python.is_file():
            raise RuntimeError(
                f"Virtual environment Python was not created at {staged_python}."
            )
        if not _pip_available(staged_python):
            _run([str(staged_python), "-m", "ensurepip", "--upgrade"])
        _run(
            [
                str(staged_python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "pip",
                "setuptools",
                "wheel",
                "build",
                "packaging",
            ]
        )
        _install_python_packages(staged_python, development=development)
        _run([str(staged_python), str(ROOT / "scripts" / "sync-console-assets.py")])
        _verify_environment(staged)

        def verify_published(target: Path) -> None:
            nonlocal resolutions
            published_python = _environment_python(target)
            # Console launchers embed environment paths, so regenerate them after
            # the atomic directory switch and before declaring the install usable.
            _install_python_packages(published_python, development=development)
            resolutions = _verify_environment(target)

        _publish_staged_environment(staged, venv, verify=verify_published)
    finally:
        _remove_environment(staged)

    scripts = next(
        (
            Path(str(resolution["scripts_directory"]))
            for resolution in resolutions.values()
            if resolution.get("scripts_directory")
        ),
        _scripts_directory(venv),
    )
    entry_points = {
        name: Path(str(resolution["path"])) for name, resolution in resolutions.items()
    }
    activation = (
        f"& '{scripts / 'Activate.ps1'}'"
        if os.name == "nt"
        else f"source '{scripts / 'activate'}'"
    )
    print(
        "Villani local installation complete. No background service was installed or "
        "started. No API key, telemetry, or model download was required."
    )
    print(
        "Dependency profile: "
        + ("runtime plus optional development tools." if development else "runtime only.")
    )
    print(f"Activate this installation with:\n  {activation}")
    print(
        "Executables after activation:\n"
        + "\n".join(f"  {path}" for path in entry_points.values())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
