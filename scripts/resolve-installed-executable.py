#!/usr/bin/env python3
"""Emit one installed-entry-point resolution from this Python environment."""

from __future__ import annotations

import argparse
import json

from villani_ops.executables import (
    resolve_installed_executable,
    resolved_executable_prefix,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command")
    args = parser.parse_args()
    resolution = resolve_installed_executable(args.command)
    document = {
        "command": resolution.command,
        "path": str(resolution.path) if resolution.path is not None else None,
        "source": resolution.source,
        "candidates": [str(item) for item in resolution.candidates],
        "diagnostic": resolution.diagnostic,
        "interpreter": str(resolution.interpreter),
        "scripts_directory": (
            str(resolution.scripts_directory)
            if resolution.scripts_directory is not None
            else None
        ),
        "path_searched": resolution.path_searched,
        "prefix": (
            list(resolved_executable_prefix(resolution))
            if resolution.path is not None
            else []
        ),
    }
    print(json.dumps(document, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
