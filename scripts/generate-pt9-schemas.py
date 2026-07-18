"""Generate normative and packaged JSON Schemas for PT9 public contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "components" / "villani-ops"
sys.path.insert(0, str(OPS))

from villani_ops.closed_loop.adaptive_verification.models import (  # noqa: E402
    AdaptiveVerificationPlan,
    BinaryVerificationDecision,
    CompactReviewPackage,
    GateDReport,
    HumanOutcome,
    SupervisionMetrics,
)


CONTRACTS = {
    "adaptive-verification-plan.schema.json": AdaptiveVerificationPlan,
    "binary-verification-decision.schema.json": BinaryVerificationDecision,
    "review-package.schema.json": CompactReviewPackage,
    "human-outcome.schema.json": HumanOutcome,
    "supervision-metrics.schema.json": SupervisionMetrics,
    "gate-d.schema.json": GateDReport,
}


def main() -> None:
    destinations = [
        ROOT / "schemas" / "v1",
        OPS / "villani_ops" / "schemas" / "v1",
    ]
    for filename, model in CONTRACTS.items():
        schema = model.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://villani.dev/schemas/v1/{filename}"
        payload = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        for destination in destinations:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / filename).write_text(payload, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
