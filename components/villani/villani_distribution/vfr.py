from __future__ import annotations

import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Sequence


def bundled_vfr() -> Path:
    name = "vfr.exe" if os.name == "nt" else "vfr"
    return Path(str(files("villani_distribution").joinpath("bin", name)))


def _development_command() -> list[str] | None:
    if os.environ.get("VILLANI_DEVELOPMENT_VFR") != "1":
        return None
    root = Path(__file__).resolve().parents[3]
    script = root / "components" / "villani-flight-recorder" / "dist" / "cli.js"
    if script.is_file():
        return ["node", str(script)]
    return None


def command_prefix() -> list[str]:
    executable = bundled_vfr()
    if executable.is_file():
        if os.name != "nt":
            executable.chmod(executable.stat().st_mode | 0o111)
        return [str(executable)]
    development = _development_command()
    if development:
        return development
    raise RuntimeError(
        "this Villani installation has no platform Flight Recorder executable; "
        "install an official platform wheel or release archive"
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        completed = subprocess.run([*command_prefix(), *arguments], shell=False, check=False)
    except OSError as error:
        print(f"vfr: {error}", file=sys.stderr)
        return 2
    return completed.returncode
