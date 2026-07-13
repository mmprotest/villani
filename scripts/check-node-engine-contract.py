#!/usr/bin/env python3
"""Verify the connected Node packages and CI use one compatible minimum."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "ui": ROOT / "components/villani-ui/package.json",
    "run-model": ROOT / "components/villani-run-model/package.json",
    "flight-recorder": ROOT / "components/villani-flight-recorder/package.json",
    "web": ROOT / "components/villani-web/package.json",
}


def _minimum_node(path: Path) -> int:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    declared = manifest.get("engines", {}).get("node", "")
    match = re.fullmatch(r">=(\d+)", declared)
    if match is None:
        raise SystemExit(f"{path}: expected an exact >=MAJOR Node engine declaration")
    return int(match.group(1))


def main() -> None:
    minimums = {name: _minimum_node(path) for name, path in PACKAGES.items()}
    run_model_minimum = minimums["run-model"]
    for consumer in ("ui", "flight-recorder", "web"):
        if minimums[consumer] < run_model_minimum:
            raise SystemExit(
                f"{consumer} Node minimum {minimums[consumer]} is below "
                f"run-model minimum {run_model_minimum}"
            )

    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    declared_ci_versions = [
        int(match)
        for match in re.findall(r"node-version:\s*[\"']?(\d+)", workflow)
    ]
    for matrix in re.findall(r"\bnode:\s*\[([^\]]+)\]", workflow):
        declared_ci_versions.extend(
            int(item)
            for item in re.findall(r"(?:^|,)\s*[\"']?(\d+)", matrix)
        )
    below_minimum = [
        version for version in declared_ci_versions if version < run_model_minimum
    ]
    if below_minimum:
        raise SystemExit(
            f"CI declares Node {below_minimum}, below required {run_model_minimum}"
        )
    print(
        "Node engine contract passed: "
        + ", ".join(f"{name}>={version}" for name, version in minimums.items())
    )


if __name__ == "__main__":
    main()
