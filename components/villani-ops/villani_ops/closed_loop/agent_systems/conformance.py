"""Machine-readable fail-closed harness conformance reporting."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .models import (
    AgentSystemIdentity,
    HarnessConformanceCheck,
    HarnessConformanceReport,
    REQUIRED_HARNESS_CONFORMANCE_CHECKS,
    utc_now,
)


REQUIRED_CONFORMANCE_CHECKS = REQUIRED_HARNESS_CONFORMANCE_CHECKS


def build_harness_conformance_report(
    identity: AgentSystemIdentity,
    observations: Mapping[str, Mapping[str, Any]],
) -> HarnessConformanceReport:
    """Normalize independently produced test observations; omissions never pass."""

    checks: list[HarnessConformanceCheck] = []
    for check_id in REQUIRED_CONFORMANCE_CHECKS:
        raw = observations.get(check_id)
        if not isinstance(raw, Mapping):
            checks.append(
                HarnessConformanceCheck(
                    check_id=check_id,
                    status="not_run",
                    reason="Required conformance evidence was not supplied.",
                )
            )
            continue
        status = str(raw.get("status") or "not_run")
        if status not in {"pass", "fail", "not_run"}:
            status = "fail"
        evidence = raw.get("evidence")
        checks.append(
            HarnessConformanceCheck(
                check_id=check_id,
                status=status,  # type: ignore[arg-type]
                reason=str(raw.get("reason") or "Conformance observation recorded."),
                evidence=dict(evidence) if isinstance(evidence, Mapping) else {},
            )
        )
    statuses = {item.status for item in checks}
    status = (
        "failed"
        if "fail" in statuses
        else "insufficient_evidence"
        if "not_run" in statuses
        else "passed"
    )
    report_seed = json.dumps(
        {
            "system_id": identity.system_id,
            "checks": [item.model_dump(mode="json") for item in checks],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return HarnessConformanceReport(
        report_id=f"hconf_{hashlib.sha256(report_seed).hexdigest()}",
        system_id=identity.system_id,
        harness_id=identity.harness.harness_id,
        harness_version=identity.harness.version,
        protocol_version=identity.harness.protocol_version,
        generated_at=utc_now(),
        status=status,  # type: ignore[arg-type]
        checks=checks,
        production_qualification_authorized=status == "passed",
    )


__all__ = ["REQUIRED_CONFORMANCE_CHECKS", "build_harness_conformance_report"]
