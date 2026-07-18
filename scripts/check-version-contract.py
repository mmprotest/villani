#!/usr/bin/env python3
"""Fail when any current Villani package drifts from release/VERSION."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    expected = (ROOT / "release" / "VERSION").read_text(encoding="utf-8").strip()
    observed: dict[str, str] = {}
    for component in (
        "villani",
        "villani-ops",
        "villani-code",
        "villani-agentd",
        "villani-control-plane",
    ):
        path = ROOT / "components" / component / "pyproject.toml"
        observed[f"components/{component}/pyproject.toml"] = str(
            tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"]
        )
    module_versions = {
        "villani": "villani_distribution/__init__.py",
        "villani-ops": "villani_ops/__init__.py",
        "villani-code": "villani_code/__init__.py",
        "villani-agentd": "villani_agentd/__init__.py",
        "villani-control-plane": "villani_control_plane/__init__.py",
    }
    for component, relative in module_versions.items():
        path = ROOT / "components" / component / relative
        match = re.search(
            r'^__version__\s*=\s*["\']([^"\']+)',
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        observed[f"components/{component}/{relative}#__version__"] = (
            match.group(1) if match else "missing"
        )
    for component in (
        "villani-run-model",
        "villani-ui",
        "villani-web",
        "villani-flight-recorder",
    ):
        root = ROOT / "components" / component
        for name in ("package.json", "package-lock.json"):
            path = root / name
            observed[f"components/{component}/{name}"] = str(
                json.loads(path.read_text(encoding="utf-8"))["version"]
            )
    compatibility = json.loads(
        (ROOT / "release" / "component-compatibility.json").read_text(encoding="utf-8")
    )
    observed["release/component-compatibility.json#release_candidate"] = str(
        compatibility["release_candidate"]
    )
    for name, version in compatibility["components"].items():
        if name != "shared-protocol":
            observed[f"release/component-compatibility.json#components/{name}"] = str(
                version
            )
    chart = (ROOT / "deploy" / "helm" / "villani-control-plane" / "Chart.yaml").read_text(
        encoding="utf-8"
    )
    for key in ("version", "appVersion"):
        match = re.search(rf"^{key}:\s*[\"']?([^\"'\s]+)", chart, re.MULTILINE)
        observed[f"deploy/helm/villani-control-plane/Chart.yaml#{key}"] = (
            match.group(1) if match else "missing"
        )
    failures = {name: value for name, value in observed.items() if value != expected}
    if failures:
        print(json.dumps({"expected": expected, "drift": failures}, indent=2, sort_keys=True))
        return 1
    print(f"canonical version {expected}: {len(observed)} declarations verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
