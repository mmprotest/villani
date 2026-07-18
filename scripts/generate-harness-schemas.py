#!/usr/bin/env python3
"""Regenerate normative and packaged structured-harness evidence schemas."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "components" / "villani-ops"
sys.path.insert(0, str(OPS))

from villani_ops.closed_loop.agent_systems.models import (  # noqa: E402
    AgentSystemIdentity,
    HarnessDiscovery,
    HarnessConformanceReport,
    HarnessResult,
)
from villani_ops.closed_loop.qualification.models import (  # noqa: E402
    GateCReport,
    QualificationInvalidation,
    QualificationObservation,
    QualificationSnapshot,
)
from villani_ops.closed_loop.economics.models import (  # noqa: E402
    EconomicsObservation,
    EconomicsSnapshot,
    OnlineEvidenceUpdateReport,
    RoutePlan,
    RoutePolicy,
    RoutePolicyEvaluation,
    RoutePolicyPublication,
)


MODELS = {
    "agent-system.schema.json": AgentSystemIdentity,
    "harness-result.schema.json": HarnessResult,
    "harness-conformance-report.schema.json": HarnessConformanceReport,
    "harness-discovery.schema.json": HarnessDiscovery,
    "qualification-observation.schema.json": QualificationObservation,
    "qualification-invalidation.schema.json": QualificationInvalidation,
    "qualification-snapshot.schema.json": QualificationSnapshot,
    "gate-c.schema.json": GateCReport,
    "economics-observation.schema.json": EconomicsObservation,
    "economics-snapshot.schema.json": EconomicsSnapshot,
    "online-evidence-update.schema.json": OnlineEvidenceUpdateReport,
    "route-plan.schema.json": RoutePlan,
    "route-policy.schema.json": RoutePolicy,
    "route-policy-evaluation.schema.json": RoutePolicyEvaluation,
    "route-policy-publication.schema.json": RoutePolicyPublication,
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
        payload = (
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        for destination in destinations:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / filename).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
