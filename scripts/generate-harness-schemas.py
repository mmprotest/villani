#!/usr/bin/env python3
"""Regenerate normative and packaged PT5 harness evidence schemas."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "components" / "villani-ops"
sys.path.insert(0, str(OPS))

from villani_ops.closed_loop.agent_systems.models import (  # noqa: E402
    AgentSystemIdentity,
    HarnessConformanceReport,
    HarnessResult,
)


MODELS = {
    "agent-system.schema.json": AgentSystemIdentity,
    "harness-result.schema.json": HarnessResult,
    "harness-conformance-report.schema.json": HarnessConformanceReport,
}


def main() -> None:
    destinations = (
        ROOT / "schemas" / "v1",
        OPS / "villani_ops" / "schemas" / "v1",
    )
    for filename, model in MODELS.items():
        schema = model.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://villani.dev/schemas/v1/{filename}"
        payload = json.dumps(
            schema, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        for destination in destinations:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / filename).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
