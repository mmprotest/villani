#!/usr/bin/env python3
"""Install the local Villani product without telemetry or implicit model downloads."""

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
        raise SystemExit(f"{label} is required but was not executable: {error}") from error
    text = (completed.stdout or completed.stderr).strip().lstrip("v")
    try:
        return tuple(int(part) for part in text.split(".")[:3])
    except ValueError as error:
        raise SystemExit(f"Could not parse {label} version from {text!r}.") from error


def _write_launchers(venv: Path) -> Path:
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    node = shutil.which("node") or "node"
    cli = ROOT / "components" / "villani-flight-recorder" / "dist" / "cli.js"
    if os.name == "nt":
        launcher = scripts / "vfr.cmd"
        launcher.write_text(f'@echo off\r\n"{node}" "{cli}" %*\r\n', encoding="utf-8")
    else:
        launcher = scripts / "vfr"
        launcher.write_text(f'#!/bin/sh\nexec "{node}" "{cli}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return scripts


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
        raise SystemExit("Node.js 18 or newer is required; `node` was not found on PATH.")
    if npm_command is None:
        raise SystemExit("npm is required; `npm` was not found on PATH.")
    node_version = _version([node_command, "--version"], "Node.js")
    if node_version < (18, 0):
        raise SystemExit(
            "Node.js 18 or newer is required; found "
            f"{'.'.join(map(str, node_version))}."
        )
    _version([npm_command, "--version"], "npm")

    venv = args.venv.resolve()
    if not (venv / "pyvenv.cfg").is_file():
        _run([sys.executable, "-m", "venv", str(venv)])
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        [
            str(python), "-m", "pip", "install",
            "-e", str(ROOT / "components" / "villani-code"),
            "-e", str(ROOT / "components" / "villani-ops"),
        ]
    )
    flight = ROOT / "components" / "villani-flight-recorder"
    _run([npm_command, "ci"], cwd=flight)
    _run([npm_command, "run", "build"], cwd=flight)
    scripts = _write_launchers(venv)
    activation = (
        f"& '{scripts / 'Activate.ps1'}'"
        if os.name == "nt"
        else f"source '{scripts / 'activate'}'"
    )
    print("Villani local installation complete. No telemetry or model download was performed.")
    print(f"Activate this installation with:\n  {activation}")
    print(f"Executables after activation:\n  {scripts / 'villani'}\n  {scripts / ('vfr.cmd' if os.name == 'nt' else 'vfr')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
