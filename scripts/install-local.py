#!/usr/bin/env python3
"""Install an editable monorepo development environment without starting services."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


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


def _entry_point(scripts: Path, name: str) -> Path:
    candidates = (
        [scripts / f"{name}.exe", scripts / f"{name}.cmd", scripts / name]
        if os.name == "nt"
        else [scripts / name]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(f"Editable installation did not create the {name!r} entry point.")


def _pip_available(python: Path) -> bool:
    completed = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv")
    args = parser.parse_args()

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

    venv = args.venv.resolve()
    if not (venv / "pyvenv.cfg").is_file():
        _run([sys.executable, "-m", "venv", str(venv)])
    python = _environment_python(venv)
    if not python.is_file():
        raise SystemExit(f"Virtual environment Python was not created at {python}.")
    if not _pip_available(python):
        _run([str(python), "-m", "ensurepip", "--upgrade"])
    _run(
        [
            str(python),
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
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-e",
            str(ROOT / "components" / "villani-code"),
            "-e",
            str(ROOT / "components" / "villani-ops"),
            "-e",
            str(ROOT / "components" / "villani-agentd"),
            "-e",
            str(ROOT / "components" / "villani"),
        ]
    )
    web = ROOT / "components" / "villani-web"
    _run([npm_command, "ci"], cwd=web)
    _run([npm_command, "run", "build"], cwd=web)
    _run([str(python), str(ROOT / "scripts" / "sync-console-assets.py")])
    flight = ROOT / "components" / "villani-flight-recorder"
    _run([npm_command, "ci"], cwd=flight)
    _run([npm_command, "run", "build"], cwd=flight)
    _run(
        [
            str(python),
            "-c",
            ("import villani_distribution, villani_ops, villani_code, villani_agentd"),
        ]
    )
    scripts = _scripts_directory(venv)
    entry_points = {
        name: _entry_point(scripts, name)
        for name in ("villani", "villani-code", "villani-agentd", "vfr")
    }
    for entry_point in entry_points.values():
        _run([str(entry_point), "--help"])
    _run([str(python), "-m", "pip", "check"])
    activation = (
        f"& '{scripts / 'Activate.ps1'}'"
        if os.name == "nt"
        else f"source '{scripts / 'activate'}'"
    )
    print(
        "Villani local installation complete. No background service was installed or "
        "started. No API key, telemetry, or model download was required."
    )
    print(f"Activate this installation with:\n  {activation}")
    print(
        "Executables after activation:\n"
        + "\n".join(f"  {path}" for path in entry_points.values())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
