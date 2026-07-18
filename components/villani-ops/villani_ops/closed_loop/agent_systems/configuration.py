"""Backward-compatible agent-system configuration migration and identity building."""

from __future__ import annotations

import copy
import importlib.metadata
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from villani_ops.core.backend import Backend
from villani_ops.execution_environment import ExecutionEnvironmentConfig
from villani_ops.subprocess_utils import resolve_command_prefix

from .models import (
    AgentSystemIdentity,
    BillingIdentity,
    CAPABILITY_NAMES,
    CapabilityAssessment,
    CapabilityEvidence,
    CapabilitySource,
    CapabilityState,
    ExecutionIdentity,
    HARNESS_LIFECYCLE_OPERATIONS,
    HARNESS_PROTOCOL_VERSION,
    HARNESS_RUNTIME_CONTRACT,
    HarnessIdentity,
    ModelProviderIdentity,
    QualificationReference,
    RouteProfile,
    configuration_digest,
    file_digest,
    utc_now,
)


AGENT_SYSTEM_CONFIGURATION_VERSION = "villani.agent_system_configuration.v1"
MIGRATION_VERSION = "villani.agent_system_migration.v1"
SUPPORTED_PRODUCTION_HARNESSES = frozenset({"villani-code"})


def _legacy_entry(name: str, backend: Backend) -> dict[str, Any]:
    enabled = bool(backend.enabled and "coding" in backend.roles)
    return {
        "harness": {
            "id": "villani-code",
            "display_name": "Villani Code",
            "adapter_id": "villani.villani_code_attempt",
            "adapter_version": "1.0.0",
            "protocol": "villani-harness",
            "protocol_version": HARNESS_PROTOCOL_VERSION,
            "transport": "structured_headless_cli",
            "command": backend.command_name or "villani-code",
        },
        "backend": name,
        "production_enabled": enabled,
        "qualification_status": "bootstrap" if enabled else "disabled",
        "repository_profile": "generic_repository",
        "task_profile": "generic_coding_task",
        "verification_policy": "controller_acceptance_evidence_v1",
        "tool_protocol": "villani_code_tools_v1",
        "prompt_protocol": "villani_code_headless_v1",
        "permission_profile": "configured_execution_environment",
        "network_policy": "unknown",
        "qualification_references": [
            {
                "kind": "conformance",
                "reference": "villani-code closed-loop regression suite",
            }
        ],
        "migration": {
            "version": MIGRATION_VERSION,
            "source": f"backends.{name}",
            "preserves_legacy_backend": True,
        },
    }


def migrate_agent_system_configuration(
    configuration: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Migrate legacy backend routes in memory without deleting old keys."""

    migrated = copy.deepcopy(dict(configuration))
    raw_backends = migrated.get("backends")
    backends: dict[str, Backend] = {}
    if isinstance(raw_backends, Mapping):
        for raw_name, raw_value in raw_backends.items():
            name = str(raw_name)
            if isinstance(raw_value, Backend):
                backends[name] = raw_value
            elif isinstance(raw_value, Mapping):
                backends[name] = Backend.model_validate(
                    {"name": name, **dict(raw_value)}
                )

    existing = migrated.get("agent_systems")
    if existing is None:
        systems: dict[str, Any] = {}
        container: dict[str, Any] = {
            "schema_version": AGENT_SYSTEM_CONFIGURATION_VERSION,
            "systems": systems,
        }
        migrated["agent_systems"] = container
        source_version = "legacy_backends"
    elif isinstance(existing, Mapping):
        container = copy.deepcopy(dict(existing))
        version = container.get("schema_version")
        if version not in {None, AGENT_SYSTEM_CONFIGURATION_VERSION}:
            raise ValueError(f"unsupported agent-system configuration {version!r}")
        container["schema_version"] = AGENT_SYSTEM_CONFIGURATION_VERSION
        raw_systems = container.get("systems")
        if raw_systems is None:
            raw_systems = {}
        if not isinstance(raw_systems, Mapping):
            raise ValueError("agent_systems.systems must be a mapping")
        systems = copy.deepcopy(dict(raw_systems))
        container["systems"] = systems
        migrated["agent_systems"] = container
        source_version = str(version or "unversioned_agent_systems")
    else:
        raise ValueError("agent_systems must be a mapping")

    added: list[str] = []
    preserved: list[str] = []
    for name, backend in backends.items():
        if "coding" not in backend.roles:
            continue
        if name in systems:
            preserved.append(name)
        else:
            systems[name] = _legacy_entry(name, backend)
            added.append(name)

    removed: list[str] = []
    for route_name, raw_entry in list(systems.items()):
        if not isinstance(raw_entry, Mapping):
            continue
        migration_value = raw_entry.get("migration")
        backend_name = str(raw_entry.get("backend") or route_name)
        if (
            isinstance(migration_value, Mapping)
            and migration_value.get("version") == MIGRATION_VERSION
            and backend_name not in backends
        ):
            del systems[route_name]
            removed.append(str(route_name))

    report = {
        "schema_version": MIGRATION_VERSION,
        "source_version": source_version,
        "target_version": AGENT_SYSTEM_CONFIGURATION_VERSION,
        "added_systems": sorted(added),
        "preserved_systems": sorted(preserved),
        "removed_generated_systems": sorted(removed),
        "legacy_backends_preserved": True,
        "destructive_changes": False,
    }
    return migrated, report


def _version(distribution: str, fallback: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return fallback


def _endpoint(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def _executable(command: str) -> tuple[Path | None, str | None]:
    prefix = resolve_command_prefix(command)
    if not prefix:
        return None, None
    candidates = [Path(item) for item in prefix if Path(item).is_file()]
    path = candidates[-1].resolve() if candidates else None
    return path, file_digest(path)


def _capabilities(harness_id: str) -> dict[str, CapabilityAssessment]:
    unsupported = {"resume", "fork", "mcp", "acp"}
    conformance = {
        "file_editing",
        "command_execution",
        "cancellation",
        "session_identity",
        "structured_result",
        "complete_trace",
        "isolated_worktree",
        "non_interactive_execution",
    }
    detected = {"usage_reporting", "cost_reporting", "model_identity"}
    output: dict[str, CapabilityAssessment] = {}
    for name in CAPABILITY_NAMES:
        if harness_id != "villani-code":
            output[name] = CapabilityAssessment(
                state=CapabilityState.UNKNOWN,
                notes="External harness is contract-only and production-disabled in PT5.",
            )
        elif name in unsupported:
            output[name] = CapabilityAssessment(
                state=CapabilityState.UNSUPPORTED,
                evidence=[
                    CapabilityEvidence(
                        source=CapabilitySource.UNSUPPORTED,
                        reference="Villani Code PT5 adapter declaration",
                    )
                ],
            )
        elif name in conformance:
            output[name] = CapabilityAssessment(
                state=CapabilityState.SUPPORTED,
                evidence=[
                    CapabilityEvidence(
                        source=CapabilitySource.CONFORMANCE,
                        reference="villani.harness_conformance_report.v1",
                    )
                ],
            )
        elif name in detected:
            output[name] = CapabilityAssessment(
                state=CapabilityState.SUPPORTED,
                evidence=[
                    CapabilityEvidence(
                        source=CapabilitySource.DETECTED,
                        reference="Villani Code structured attempt result",
                    )
                ],
            )
        else:
            output[name] = CapabilityAssessment(
                state=CapabilityState.UNKNOWN,
                notes="No declaration or conformance evidence is available.",
            )
    return output


def build_agent_system_identities(
    configuration: Mapping[str, Any], backends: Mapping[str, Backend]
) -> tuple[tuple[AgentSystemIdentity, ...], dict[str, AgentSystemIdentity], dict[str, Any]]:
    migrated, migration = migrate_agent_system_configuration(configuration)
    container = migrated["agent_systems"]
    systems = container["systems"]
    identities: list[AgentSystemIdentity] = []
    by_backend: dict[str, AgentSystemIdentity] = {}
    for route_name in sorted(systems):
        raw = systems[route_name]
        if not isinstance(raw, Mapping):
            raise ValueError(f"agent system {route_name!r} must be a mapping")
        entry = dict(raw)
        backend_name = str(entry.get("backend") or route_name)
        backend = backends.get(backend_name)
        if backend is None:
            raise ValueError(
                f"agent system {route_name!r} references unknown backend {backend_name!r}"
            )
        harness_raw = entry.get("harness")
        if not isinstance(harness_raw, Mapping):
            raise ValueError(f"agent system {route_name!r} requires harness identity")
        harness_config = dict(harness_raw)
        harness_id = str(harness_config.get("id") or "")
        if not harness_id:
            raise ValueError(f"agent system {route_name!r} harness id is required")
        production_enabled = bool(entry.get("production_enabled", False))
        qualification_status = str(
            entry.get("qualification_status")
            or ("bootstrap" if production_enabled else "disabled")
        )
        if harness_id not in SUPPORTED_PRODUCTION_HARNESSES:
            if production_enabled:
                raise ValueError(
                    f"external harness {harness_id!r} is production-disabled in PT5"
                )
            qualification_status = "unsupported"
        elif (
            str(harness_config.get("protocol_version") or HARNESS_PROTOCOL_VERSION)
            != HARNESS_PROTOCOL_VERSION
            or str(harness_config.get("adapter_version") or "1.0.0") != "1.0.0"
        ):
            production_enabled = False
            qualification_status = "unqualified"
        if not backend.enabled:
            production_enabled = False
            qualification_status = "disabled"

        execution = ExecutionEnvironmentConfig.from_configuration(
            migrated, backend.execution_environment
        )
        command = str(harness_config.get("command") or backend.command_name or "villani-code")
        executable_path, executable_digest = _executable(command)
        version = (
            _version("villani-code", "0.1.0rc1")
            if harness_id == "villani-code"
            else str(harness_config.get("version") or "unknown")
        )
        backend_configuration = backend.redacted_dict()
        backend_configuration["base_url"] = _endpoint(backend.base_url)
        safe_harness_configuration = {
            key: value
            for key, value in harness_config.items()
            if key != "command"
        }
        safe_harness_configuration.update(
            {
                "command_identity": (
                    executable_path.name
                    if executable_path is not None
                    else Path(command.split()[0]).name
                ),
                "resolved_version": version,
                "executable_digest": executable_digest,
            }
        )
        identity_configuration = {
            "route_name": route_name,
            "harness": safe_harness_configuration,
            "harness_contract": {
                "protocol": HARNESS_PROTOCOL_VERSION,
                "lifecycle_operations": list(HARNESS_LIFECYCLE_OPERATIONS),
                **HARNESS_RUNTIME_CONTRACT,
            },
            "backend": backend_configuration,
            "execution_environment": execution.model_dump(mode="json"),
            "repository_profile": entry.get("repository_profile", "generic_repository"),
            "task_profile": entry.get("task_profile", "generic_coding_task"),
            "verification_policy": entry.get(
                "verification_policy", "controller_acceptance_evidence_v1"
            ),
            "tool_protocol": entry.get("tool_protocol", "unknown"),
            "prompt_protocol": entry.get("prompt_protocol", "unknown"),
            "permission_profile": entry.get("permission_profile", "unknown"),
            "network_policy": entry.get("network_policy", "unknown"),
        }
        digest, projection, redacted = configuration_digest(identity_configuration)
        refs_raw = entry.get("qualification_references")
        refs = (
            [QualificationReference.model_validate(item) for item in refs_raw]
            if isinstance(refs_raw, list)
            else []
        )
        unknown_fields: list[str] = []
        if executable_digest is None:
            unknown_fields.append("harness.executable_digest")
        if backend.metadata.get("model_revision") is None:
            unknown_fields.append("model_provider.model_revision")
        if backend.metadata.get("serving_engine") is None:
            unknown_fields.extend(
                ["model_provider.serving_engine", "model_provider.serving_engine_version"]
            )
        unknown_fields.extend(
            ["execution.environment_fingerprint", "execution.sandbox_identity"]
        )
        cost_source = str(backend.metadata.get("cost_source") or "") or None
        billing_unknown = []
        if backend.billing_mode == "unknown":
            billing_unknown.append("billing.mode")
        if cost_source is None:
            billing_unknown.append("billing.cost_source")
        identity = AgentSystemIdentity(
            system_id=f"asys_{digest.removeprefix('sha256:')}",
            route_name=route_name,
            production_enabled=production_enabled,
            qualification_status=qualification_status,  # type: ignore[arg-type]
            harness=HarnessIdentity(
                harness_id=harness_id,
                display_name=str(
                    harness_config.get("display_name") or harness_id
                ),
                version=version,
                executable_digest=executable_digest,
                adapter_id=str(
                    harness_config.get("adapter_id") or f"villani.{harness_id}"
                ),
                adapter_version=str(harness_config.get("adapter_version") or "1.0.0"),
                protocol=str(harness_config.get("protocol") or "villani-harness"),
                protocol_version=str(
                    harness_config.get("protocol_version") or HARNESS_PROTOCOL_VERSION
                ),
                transport=str(
                    harness_config.get("transport") or "structured_headless_cli"
                ),  # type: ignore[arg-type]
            ),
            model_provider=ModelProviderIdentity(
                provider=backend.provider,
                model_id=backend.model,
                model_revision=(
                    str(backend.metadata["model_revision"])
                    if backend.metadata.get("model_revision") is not None
                    else None
                ),
                endpoint_identity=_endpoint(backend.base_url),
                serving_engine=(
                    str(backend.metadata["serving_engine"])
                    if backend.metadata.get("serving_engine") is not None
                    else None
                ),
                serving_engine_version=(
                    str(backend.metadata["serving_engine_version"])
                    if backend.metadata.get("serving_engine_version") is not None
                    else None
                ),
                context_metadata=dict(backend.metadata.get("context") or {}),
                tool_metadata={
                    "support": backend.metadata.get("tool_use_support"),
                    "authoritative": backend.metadata.get("tool_metadata_authoritative", False),
                },
            ),
            execution=ExecutionIdentity(
                execution_provider=execution.provider,
                environment_fingerprint=None,
                permission_profile=str(entry.get("permission_profile") or "unknown"),
                network_policy=str(entry.get("network_policy") or "unknown"),  # type: ignore[arg-type]
                sandbox_identity=None,
            ),
            route_profile=RouteProfile(
                repository_profile=str(
                    entry.get("repository_profile") or "generic_repository"
                ),
                task_profile=str(entry.get("task_profile") or "generic_coding_task"),
                verification_policy=str(
                    entry.get("verification_policy")
                    or "controller_acceptance_evidence_v1"
                ),
                tool_protocol=str(entry.get("tool_protocol") or "unknown"),
                prompt_protocol=str(entry.get("prompt_protocol") or "unknown"),
            ),
            capabilities=_capabilities(harness_id),
            qualification_references=refs,
            billing=BillingIdentity(
                mode=backend.billing_mode,
                cost_source=cost_source,
                currency=backend.currency if backend.billing_mode != "unknown" else None,
                unknown_fields=billing_unknown,
            ),
            detection_time=utc_now(),
            detection_source="configuration_migration_and_local_probe",
            configuration_digest=digest,
            configuration=projection,
            redaction_status=("redacted" if redacted else "no_sensitive_values_detected"),
            unknown_fields=sorted(set(unknown_fields + billing_unknown)),
        )
        identities.append(identity)
        by_backend[backend_name] = identity
    return tuple(identities), by_backend, migration


__all__ = [
    "AGENT_SYSTEM_CONFIGURATION_VERSION",
    "MIGRATION_VERSION",
    "SUPPORTED_PRODUCTION_HARNESSES",
    "build_agent_system_identities",
    "migrate_agent_system_configuration",
]
