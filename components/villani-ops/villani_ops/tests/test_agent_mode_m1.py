from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.agent_systems.configuration import (
    migrate_agent_system_configuration,
)
from villani_ops.closed_loop.agent_systems.factories import (
    RoleFactoryDependencies,
    build_attempt_runner,
    build_classifier,
    build_selector,
    build_verifier,
)
from villani_ops.closed_loop.agent_systems.registry import AgentSystemRegistry
from villani_ops.closed_loop.agent_systems.role_models import (
    AgentRole,
    AgentSystemCatalog,
    ApiAgentSystemConfig,
    CliAgentSystemConfig,
    InternalRunnerSystemConfig,
    RoleBindings,
    parse_agent_system,
)
from villani_ops.closed_loop.agent_systems.role_registry import (
    RoleBindingConfigurationError,
    RoleSystemRegistry,
)
from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.schema_validation import (
    parse_protocol_document,
    validate_protocol_document,
)
from villani_ops.core.backend import Backend


def _api_backend(
    name: str = "backend", *, enabled: bool = True, secret: str | None = None
) -> Backend:
    return Backend(
        name=name,
        provider="openai-compatible",
        base_url="http://127.0.0.1:11434/v1",
        model="fixture-model",
        api_key=secret,
        roles=["classification", "coding", "review", "selection"],
        capability_score=50,
        enabled=enabled,
    )


def _api_system(
    system_id: str = "api-system",
    *,
    roles: list[str] | None = None,
    enabled: bool = True,
    backend_reference: str = "backend",
) -> dict[str, object]:
    return {
        "kind": "api",
        "id": system_id,
        "enabled": enabled,
        "provider": "openai-compatible",
        "model": "fixture-model",
        "roles": roles or ["classification", "coding", "verification", "selection"],
        "existing_backend_reference": backend_reference,
        "timeout_seconds": 180,
        "max_parallel": 1,
        "metadata": {},
    }


def _cli_system(
    system_id: str = "codex-system", *, roles: list[str] | None = None
) -> dict[str, object]:
    return {
        "kind": "cli_agent",
        "id": system_id,
        "enabled": True,
        "driver": "codex",
        "executable": "codex",
        "model": "default",
        "roles": roles or ["classification", "coding", "verification", "selection"],
        "timeout_seconds": 180,
        "max_parallel": 1,
        "instruction_policy": "native_project",
        "permission_profile": "read_only",
        "environment_policy": "inherit",
        "provider_options": {},
    }


def _profile(system_id: str) -> dict[str, str]:
    return {role.value: system_id for role in AgentRole}


def _role_registry(
    systems: dict[str, dict[str, object]],
    profile: dict[str, str],
    *,
    backends: dict[str, Backend] | None = None,
    profile_id: str = "profile",
) -> RoleSystemRegistry:
    return RoleSystemRegistry(
        {
            "agent_systems": {"systems": systems},
            "execution_profiles": {profile_id: profile},
        },
        backends or {},
    )


def test_cli_agent_system_can_be_represented_without_driver_construction() -> None:
    configuration = {
        "agent_systems": {
            "schema_version": "villani.agent_system_configuration.v1",
            "systems": {
                "codex-classifier": {
                    "kind": "cli_agent",
                    "id": "codex-classifier",
                    "enabled": True,
                    "driver": "codex",
                    "executable": "codex",
                    "model": "default",
                    "roles": [
                        "classification",
                        "coding",
                        "verification",
                        "selection",
                    ],
                    "timeout_seconds": 180,
                    "max_parallel": 1,
                    "instruction_policy": "native_project",
                    "permission_profile": "read_only",
                    "environment_policy": "inherit",
                    "provider_options": {},
                }
            },
        },
        "execution_profiles": {
            "cli": {
                "classification": "codex-classifier",
                "coding": "codex-classifier",
                "verification": "codex-classifier",
                "selection": "codex-classifier",
            }
        },
    }

    registry = AgentSystemRegistry(configuration, {})

    configured = registry.inspect_configured("codex-classifier")
    assert configured.kind == "cli_agent"
    assert registry.profile_status("cli").status == "unavailable"


@pytest.mark.parametrize(
    ("document", "expected_type"),
    [
        (_api_system(), ApiAgentSystemConfig),
        (
            {
                "kind": "internal_runner",
                "id": "runner-system",
                "enabled": True,
                "runner": "villani_code",
                "roles": ["coding"],
                "timeout_seconds": 120,
                "max_parallel": 2,
                "metadata": {},
            },
            InternalRunnerSystemConfig,
        ),
        (_cli_system(), CliAgentSystemConfig),
    ],
)
def test_agent_system_discriminated_parsing(
    document: dict[str, object], expected_type: type[object]
) -> None:
    assert isinstance(parse_agent_system(document), expected_type)


def test_agent_system_invalid_kind_is_field_level() -> None:
    with pytest.raises(ValidationError) as captured:
        parse_agent_system({**_api_system(), "kind": "shell_agent"})
    assert captured.value.errors()[0]["loc"] == ()
    assert "union_tag_invalid" in captured.value.errors()[0]["type"]


def test_invalid_and_duplicate_system_ids_fail() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        parse_agent_system({**_api_system(), "id": "not allowed/id"})
    system = parse_agent_system(_api_system())
    with pytest.raises(ValidationError, match="duplicate agent-system id"):
        AgentSystemCatalog(systems=[system, system])


@pytest.mark.parametrize(
    ("systems", "binding", "expected"),
    [
        (
            {"api-system": _api_system()},
            "missing-system",
            "unknown agent-system id 'missing-system'",
        ),
        (
            {"api-system": _api_system(enabled=False)},
            "api-system",
            "agent system 'api-system' is disabled",
        ),
        (
            {"api-system": _api_system(roles=["classification"])},
            "api-system",
            "does not declare role 'coding'",
        ),
    ],
)
def test_invalid_role_bindings_are_actionable(
    systems: dict[str, dict[str, object]], binding: str, expected: str
) -> None:
    registry = _role_registry(systems, _profile(binding))
    with pytest.raises(RoleBindingConfigurationError, match=expected):
        registry.resolve_profile("profile")


def test_missing_required_role_binding_fails_validation() -> None:
    with pytest.raises(
        ValidationError, match="missing required role binding.*selection"
    ):
        RoleBindings(
            profile_id="api",
            bindings={
                "classification": "api-system",
                "coding": "api-system",
                "verification": "api-system",
            },
        )


def test_complete_api_profile_is_ready() -> None:
    backend = _api_backend()
    registry = _role_registry(
        {"api-system": _api_system()},
        _profile("api-system"),
        backends={backend.name: backend},
        profile_id="api",
    )
    status = registry.profile_status("api")
    assert status.status == "ready"
    assert status.runnable is True


def test_complete_cli_profile_is_represented_but_unavailable_without_fallback() -> None:
    registry = _role_registry(
        {"codex-system": _cli_system()},
        _profile("codex-system"),
        profile_id="cli",
    )
    bindings = registry.resolve_profile("cli")
    status = registry.profile_status("cli")
    assert status.status == "unavailable"
    assert all("integration is unavailable" in reason for reason in status.reasons)
    with pytest.raises(
        RoleBindingConfigurationError, match="will not fall back to another profile"
    ):
        registry.require_runnable(bindings)


def test_hybrid_profile_representation() -> None:
    backend = _api_backend()
    systems = {
        "api-classifier": _api_system("api-classifier", roles=["classification"]),
        "code": {
            "kind": "internal_runner",
            "id": "code",
            "enabled": True,
            "runner": "villani_code",
            "roles": ["coding"],
            "timeout_seconds": 180,
            "max_parallel": 1,
            "metadata": {},
        },
        "verify": {
            "kind": "internal_runner",
            "id": "verify",
            "enabled": True,
            "runner": "villani_verifier",
            "roles": ["verification"],
            "timeout_seconds": 180,
            "max_parallel": 1,
            "metadata": {},
        },
        "select": {
            "kind": "internal_runner",
            "id": "select",
            "enabled": True,
            "runner": "evidence_selector",
            "roles": ["selection"],
            "timeout_seconds": 180,
            "max_parallel": 1,
            "metadata": {},
        },
    }
    registry = _role_registry(
        systems,
        {
            "classification": "api-classifier",
            "coding": "code",
            "verification": "verify",
            "selection": "select",
        },
        backends={backend.name: backend},
        profile_id="hybrid",
    )
    status = registry.profile_status("hybrid")
    assert status.status == "ready"
    assert len(set(status.bindings.bindings.values())) == 4  # type: ignore[union-attr]


def test_current_configuration_migrates_with_roles_and_preserved_behavior() -> None:
    backend_document = _api_backend().model_dump(mode="json", exclude={"name"})
    legacy = {
        "policy": {"accepted_candidates_required": 3},
        "backends": {"backend": backend_document},
    }
    migrated, report = migrate_agent_system_configuration(legacy)
    assert migrated["backends"] == legacy["backends"]
    assert migrated["policy"]["accepted_candidates_required"] == 3
    assert migrated["agent_systems"]["systems"]["api-backend"]["roles"] == [
        "classification",
        "coding",
        "selection",
        "verification",
    ]
    assert migrated["execution_profiles"]["api"] == {
        "schema_version": "villani.role_bindings.v1",
        "profile_id": "api",
        "bindings": {
            "classification": "api-backend",
            "coding": "villani-code-runner",
            "verification": "villani-verifier",
            "selection": "evidence-selector",
        },
        "migration": {
            "version": "villani.agent_system_migration.v1",
            "source": "current_dependency_construction",
        },
    }
    assert report["legacy_backends_preserved"] is True


def test_configuration_migration_is_idempotent() -> None:
    legacy = {
        "backends": {
            "backend": _api_backend().model_dump(mode="json", exclude={"name"})
        }
    }
    first, _ = migrate_agent_system_configuration(legacy)
    second, _ = migrate_agent_system_configuration(first)
    assert second == first


def test_configuration_migration_removes_stale_generated_role_systems() -> None:
    primary = _api_backend("primary").model_copy(update={"capability_score": 10})
    secondary = _api_backend("secondary").model_copy(update={"capability_score": 90})
    legacy = {
        "backends": {
            "primary": primary.model_dump(mode="json", exclude={"name"}),
            "secondary": secondary.model_dump(mode="json", exclude={"name"}),
        }
    }
    migrated, _ = migrate_agent_system_configuration(legacy)
    assert migrated["execution_profiles"]["api"]["bindings"]["classification"] == (
        "api-secondary"
    )

    del migrated["backends"]["secondary"]
    cleaned, report = migrate_agent_system_configuration(migrated)

    assert "api-secondary" not in cleaned["agent_systems"]["systems"]
    assert cleaned["execution_profiles"]["api"]["bindings"]["classification"] == (
        "api-primary"
    )
    assert report["removed_generated_role_systems"] == ["api-secondary"]


def test_configuration_migration_refreshes_owned_system_and_default_binding() -> None:
    primary = _api_backend("primary").model_copy(update={"capability_score": 10})
    secondary = _api_backend("secondary").model_copy(update={"capability_score": 90})
    migrated, _ = migrate_agent_system_configuration(
        {
            "backends": {
                backend.name: backend.model_dump(mode="json", exclude={"name"})
                for backend in (primary, secondary)
            }
        }
    )
    migrated["backends"]["primary"]["model"] = "replacement-model"
    migrated["backends"]["primary"]["capability_score"] = 100

    refreshed, _ = migrate_agent_system_configuration(migrated)

    systems = refreshed["agent_systems"]["systems"]
    assert systems["api-primary"]["model"] == "replacement-model"
    assert not any(system_id.startswith("api-primary-") for system_id in systems)
    assert refreshed["execution_profiles"]["api"]["bindings"]["classification"] == (
        "api-primary"
    )


def test_malformed_legacy_configuration_has_exact_field_path() -> None:
    malformed = {
        "backends": {
            "bad": {
                "provider": "openai-compatible",
                "model": "fixture-model",
                "max_parallel": 0,
            }
        }
    }
    with pytest.raises(
        ValueError,
        match=r"^backends\.bad\.max_parallel: Input should be greater than or equal to 1$",
    ):
        migrate_agent_system_configuration(malformed)


def test_serialized_invocation_identity_contains_no_secret_value() -> None:
    secret = "do-not-persist-this-key"
    backend = _api_backend(secret=secret)
    legacy = {
        "backends": {backend.name: backend.model_dump(mode="json", exclude={"name"})}
    }
    migrated, _ = migrate_agent_system_configuration(legacy)
    registry = RoleSystemRegistry(migrated, {backend.name: backend})
    bindings = registry.resolve_profile("api")
    serialized = json.dumps(
        [
            item.model_dump(mode="json")
            for item in registry.invocation_identities(bindings)
        ],
        sort_keys=True,
    )
    assert secret not in serialized
    assert "api_key" not in serialized


def test_old_run_bundle_remains_readable_without_role_fields() -> None:
    root = Path(__file__).resolve().parents[4]
    manifest_path = (
        root
        / "integration"
        / "fixtures"
        / "protocol"
        / "v1"
        / "valid_run"
        / "manifest.json"
    )
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document.pop("execution_profile_id", None)
    document.pop("role_bindings", None)
    document.pop("agent_invocation_ids", None)
    document["artifact_paths"].pop("role_bindings", None)
    document["artifact_paths"].pop("agent_invocations", None)
    parsed = parse_protocol_document(document)
    assert parsed.role_bindings == {}
    assert parsed.agent_invocation_ids == {}


def test_new_contract_documents_validate_against_normative_schemas() -> None:
    backend = _api_backend()
    registry = _role_registry(
        {"api-system": _api_system()},
        _profile("api-system"),
        backends={backend.name: backend},
        profile_id="api",
    )
    bindings = registry.resolve_profile("api")
    catalog = AgentSystemCatalog(systems=list(registry.systems))
    validate_protocol_document(catalog.model_dump(mode="json"))
    validate_protocol_document(bindings.model_dump(mode="json"))
    validate_protocol_document(
        registry.invocation_identity(bindings, AgentRole.CLASSIFICATION).model_dump(
            mode="json"
        )
    )


def test_role_specific_factories_construct_all_controller_ports() -> None:
    backend = _api_backend()
    registry = _role_registry(
        {"api-system": _api_system()},
        _profile("api-system"),
        backends={backend.name: backend},
        profile_id="api",
    )
    bindings = registry.resolve_profile("api")
    classifier = object()
    attempt_runner = object()
    verifier = object()
    selector = object()
    dependencies = RoleFactoryDependencies(
        api_classifiers={backend.name: classifier},  # type: ignore[arg-type]
        api_attempt_runners={backend.name: attempt_runner},  # type: ignore[arg-type]
        api_verifiers={backend.name: verifier},  # type: ignore[arg-type]
        api_selectors={backend.name: selector},  # type: ignore[arg-type]
    )
    assert build_classifier(bindings, registry, dependencies) is classifier
    assert build_attempt_runner(bindings, registry, dependencies) is attempt_runner
    assert build_verifier(bindings, registry, dependencies) is verifier
    assert build_selector(bindings, registry, dependencies) is selector


def test_controller_composition_uses_role_specific_factories() -> None:
    source = inspect.getsource(unified.build_controller)
    for factory in (
        "build_classifier(",
        "build_attempt_runner(",
        "build_verifier(",
        "build_selector(",
    ):
        assert factory in source
    assert source.count("ClosedLoopController(") == 1


def test_controller_honors_bound_api_classifier_backend() -> None:
    preferred = _api_backend("preferred").model_copy(update={"capability_score": 5})
    other = _api_backend("other").model_copy(update={"capability_score": 95})
    controller = ClosedLoopController(
        classifier=object(),  # type: ignore[arg-type]
        attempt_runner=object(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        selector=object(),  # type: ignore[arg-type]
        materializer=object(),  # type: ignore[arg-type]
        classification_backend_name="preferred",
    )
    configuration = {
        "backends": {
            backend.name: backend.model_dump(mode="json", exclude={"name"})
            for backend in (preferred, other)
        }
    }

    resolved = controller._resolve_classification_backend(configuration)  # noqa: SLF001

    assert resolved is not None
    assert resolved.name == "preferred"


def test_controller_source_does_not_import_provider_drivers() -> None:
    controller_source = (
        Path(unified.__file__)
        .parents[1]
        .joinpath("closed_loop", "controller.py")
        .read_text(encoding="utf-8")
    )
    assert "CodexAppServerRunner" not in controller_source
    assert "ClaudeCodeRunner" not in controller_source
    assert "runners.codex" not in controller_source
    assert "runners.claude" not in controller_source


def test_agents_and_profiles_cli_inspect_static_cli_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = {
        "agent_systems": {"systems": {"codex-system": _cli_system()}},
        "execution_profiles": {"cli": _profile("codex-system")},
    }
    monkeypatch.setattr(unified, "_load_config", lambda: configuration)
    runner = CliRunner()
    listed = runner.invoke(unified.app, ["agents", "list", "--json"])
    inspected = runner.invoke(
        unified.app, ["agents", "inspect", "codex-system", "--json"]
    )
    profiles = runner.invoke(unified.app, ["profiles", "list", "--json"])
    profile = runner.invoke(unified.app, ["profiles", "inspect", "cli", "--json"])
    assert (
        listed.exit_code,
        inspected.exit_code,
        profiles.exit_code,
        profile.exit_code,
    ) == (
        0,
        0,
        0,
        0,
    )
    assert json.loads(inspected.stdout)["status"] == "configured"
    assert json.loads(profile.stdout)["status"] == "unavailable"
    assert "quota" not in listed.stdout.lower()
