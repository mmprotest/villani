from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Mapping

from villani_ops.core.backend import Backend

from ..costs import estimate_attempt_cost
from .models import CapabilityCatalogSnapshot, CapabilityOption, FeatureProvenance


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def capability_catalog_snapshot(
    backends: Mapping[str, Backend], *, generated_at: datetime
) -> CapabilityCatalogSnapshot:
    options: list[CapabilityOption] = []
    for backend in sorted(backends.values(), key=lambda item: item.name):
        estimated = estimate_attempt_cost(backend)
        metadata_capabilities = backend.metadata.get("capabilities", [])
        capabilities = (
            tuple(
                sorted(
                    {
                        str(item).lower()
                        for item in metadata_capabilities
                        if str(item).strip()
                    }
                )
            )
            if isinstance(metadata_capabilities, list)
            else ()
        )
        cost = estimated.total if estimated.accounting_status == "complete" else None
        options.append(
            CapabilityOption(
                option_id=f"{backend.name}:{backend.model}",
                backend_name=backend.name,
                agent_adapter=str(
                    backend.metadata.get("agent_adapter") or backend.provider
                ),
                provider=backend.provider,
                model=backend.model,
                roles=tuple(sorted(backend.roles)),
                capabilities=capabilities,
                capability_score=float(backend.capability_score),
                enabled=backend.enabled,
                estimated_cost_usd=cost,
                cost_accounting_status=estimated.accounting_status,
                context_limit=backend.max_tokens,
            )
        )
    payload = [item.model_dump(mode="json") for item in options]
    digest = _digest(
        {"catalog_version": "backend_agent_catalog_v1", "options": payload}
    )
    return CapabilityCatalogSnapshot(
        snapshot_id=f"sha256:{digest}",
        generated_at=generated_at,
        options=tuple(options),
        input_provenance=(
            FeatureProvenance(
                source_kind="aggregate",
                source_id="redacted_backend_configuration",
                digest_sha256=digest,
            ),
        ),
    )
