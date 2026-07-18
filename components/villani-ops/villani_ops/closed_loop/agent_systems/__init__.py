"""Complete agent-system contracts.

Only data models are imported eagerly so run-bundle validation cannot create a
controller/adapter import cycle. Runtime factories are loaded on first access.
"""

from __future__ import annotations

from typing import Any

from .models import (
    AGENT_SYSTEM_SCHEMA_VERSION,
    CAPABILITY_NAMES,
    HARNESS_CONFORMANCE_SCHEMA_VERSION,
    HARNESS_PROTOCOL_VERSION,
    NORMALIZED_EVENT_NAMES,
    AgentSystemDoctorReport,
    AgentSystemIdentity,
    CapabilityAssessment,
    CapabilityEvidence,
    CapabilitySource,
    CapabilityState,
    HarnessConformanceCheck,
    HarnessConformanceReport,
    HarnessResult,
    HarnessSession,
    NormalizedHarnessEvent,
)


_LAZY_EXPORTS = {
    "AGENT_SYSTEM_CONFIGURATION_VERSION": ("configuration", "AGENT_SYSTEM_CONFIGURATION_VERSION"),
    "AgentSystemAttemptRunner": ("adapters", "AgentSystemAttemptRunner"),
    "AgentSystemRegistry": ("registry", "AgentSystemRegistry"),
    "HarnessAdapter": ("adapters", "HarnessAdapter"),
    "MIGRATION_VERSION": ("configuration", "MIGRATION_VERSION"),
    "REQUIRED_CONFORMANCE_CHECKS": ("conformance", "REQUIRED_CONFORMANCE_CHECKS"),
    "SUPPORTED_PRODUCTION_HARNESSES": ("configuration", "SUPPORTED_PRODUCTION_HARNESSES"),
    "VillaniCodeHarnessAdapter": ("adapters", "VillaniCodeHarnessAdapter"),
    "build_agent_system_identities": ("configuration", "build_agent_system_identities"),
    "build_agent_system_registry": ("registry", "build_agent_system_registry"),
    "build_agent_system_runner": ("registry", "build_agent_system_runner"),
    "build_harness_conformance_report": ("conformance", "build_harness_conformance_report"),
    "migrate_agent_system_configuration": ("configuration", "migrate_agent_system_configuration"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value


__all__ = [
    "AGENT_SYSTEM_CONFIGURATION_VERSION",
    "AGENT_SYSTEM_SCHEMA_VERSION",
    "AgentSystemAttemptRunner",
    "AgentSystemDoctorReport",
    "AgentSystemIdentity",
    "AgentSystemRegistry",
    "CAPABILITY_NAMES",
    "CapabilityAssessment",
    "CapabilityEvidence",
    "CapabilitySource",
    "CapabilityState",
    "HARNESS_CONFORMANCE_SCHEMA_VERSION",
    "HARNESS_PROTOCOL_VERSION",
    "HarnessAdapter",
    "HarnessConformanceCheck",
    "HarnessConformanceReport",
    "HarnessResult",
    "HarnessSession",
    "MIGRATION_VERSION",
    "NORMALIZED_EVENT_NAMES",
    "NormalizedHarnessEvent",
    "REQUIRED_CONFORMANCE_CHECKS",
    "SUPPORTED_PRODUCTION_HARNESSES",
    "VillaniCodeHarnessAdapter",
    "build_agent_system_identities",
    "build_agent_system_registry",
    "build_agent_system_runner",
    "build_harness_conformance_report",
    "migrate_agent_system_configuration",
]
