#!/usr/bin/env python3
"""Compile Flight Recorder into a native executable; Node is build-time only."""

from __future__ import annotations

import argparse
import os
import shutil
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-npm-build", action="store_true")
    parser.add_argument(
        "--bun-command",
        help="compiler command override, for example 'npx --yes bun@1.2.20'",
    )
    args = parser.parse_args()
    flight = ROOT / "components" / "villani-flight-recorder"
    bun_command = (
        shlex.split(args.bun_command, posix=os.name != "nt")
        if args.bun_command
        else ([shutil.which("bun")] if shutil.which("bun") else [])
    )
    if not bun_command:
        raise SystemExit("Bun is required to compile the standalone Flight Recorder")
    if not args.skip_npm_build:
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
        if not npm:
            raise SystemExit("npm is required to build Flight Recorder assets")
        subprocess.run([npm, "ci"], cwd=flight, check=True)
        subprocess.run([npm, "run", "build"], cwd=flight, check=True)
    output = args.output or (
        ROOT
        / "components"
        / "villani"
        / "villani_distribution"
        / "bin"
        / ("vfr.exe" if os.name == "nt" else "vfr")
    )
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [*bun_command, "build", str(flight / "dist" / "cli.js"), "--compile", "--outfile", str(output)],
        cwd=flight,
        check=True,
    )
    if os.name != "nt":
        output.chmod(output.stat().st_mode | 0o755)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
