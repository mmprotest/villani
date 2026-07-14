#!/usr/bin/env python3
"""Run Villani Python suites from each package's intentional working directory."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUITES: dict[str, tuple[Path, tuple[str, ...]]] = {
    "distribution": (ROOT / "components" / "villani", ("-m", "pytest", "tests", "-q")),
    "root": (ROOT, ("-m", "pytest", "tests", "-q")),
    "villani-code": (
        ROOT / "components" / "villani-code",
        ("-m", "pytest", "-q"),
    ),
    "villani-ops": (
        ROOT / "components" / "villani-ops",
        ("-m", "pytest", "-q"),
    ),
    "villani-agentd": (
        ROOT / "components" / "villani-agentd",
        ("-m", "pytest", "-q"),
    ),
    "control-plane-unit": (
        ROOT / "components" / "villani-control-plane",
        ("-m", "pytest", "tests/unit", "-q"),
    ),
    "final-foundation": (
        ROOT,
        ("-m", "pytest", "tests/final_foundation", "-q"),
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        action="append",
        choices=tuple(SUITES),
        help="Suite to run; repeat as needed. The default is every listed suite.",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    selected = args.suite or list(SUITES)
    manifest = [
        {
            "suite": name,
            "cwd": str(SUITES[name][0]),
            "command": [args.python, *SUITES[name][1]],
        }
        for name in selected
    ]
    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return 0
    for item in manifest:
        print(
            f"[{item['suite']}] cwd={item['cwd']} command="
            + subprocess.list2cmdline(item["command"]),
            flush=True,
        )
        completed = subprocess.run(
            item["command"], cwd=item["cwd"], shell=False, check=False
        )
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
