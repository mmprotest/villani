from __future__ import annotations

import json
import inspect
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.agent_systems.adapters import (
    AgentSystemAttemptRunner,
    VillaniCodeHarnessAdapter,
)
from villani_ops.closed_loop.agent_systems.configuration import (
    build_agent_system_identities,
    migrate_agent_system_configuration,
)
from villani_ops.closed_loop.agent_systems.conformance import (
    REQUIRED_CONFORMANCE_CHECKS,
    build_harness_conformance_report,
)
from villani_ops.closed_loop.agent_systems.models import (
    AgentSystemIdentity,
    CapabilityState,
    HarnessArtifact,
    HarnessCost,
    HarnessResult,
    MAXIMUM_HARNESS_MESSAGE_BYTES,
    NormalizedHarnessEvent,
    non_secret_configuration,
)
from villani_ops.closed_loop.agent_systems.registry import AgentSystemRegistry
from villani_ops.closed_loop.agent_systems.role_registry import RoleSystemRegistry
from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.adapters.evidence_selector import EvidenceSelectorAdapter
from villani_ops.closed_loop.adapters.villani_verifier import VillaniVerifierAdapter
from villani_ops.closed_loop.interfaces import (
    AttemptContext,
    AttemptResult,
    ClosedLoopRunRequest,
    DependencyFailure,
    RuntimeEvent,
)
from villani_ops.closed_loop.plugins.builtins import AGENT_RUNNER_MANIFEST
from villani_ops.closed_loop.schema_validation import validate_protocol_document
from villani_ops.core.backend import Backend
from villani_ops.tests.closed_loop.fakes import (
    FakeClassifier,
    FakeMaterializer,
    FakeMonotonic,
    FakePolicyEngine,
    FakeSelector,
    FakeVerifier,
    FixedNow,
    StableIds,
    accepted_verification,
    backend as backend_option,
    policy,
)


BASELINE = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _backend(
    name: str = "code", *, api_key: str | None = None, command: str | None = None
) -> Backend:
    return Backend(
        name=name,
        provider="local",
        base_url="http://127.0.0.1:8000/v1?credential=must-not-persist",
        model="fixture-model",
        api_key=api_key,
        roles=["coding", "classification"],
        capability_score=50,
        command_name=command or sys.executable,
        metadata={"tool_use_support": True},
    )


def _configuration(backend: Backend) -> dict[str, object]:
    return {
        "backends": {
            backend.name: backend.model_dump(mode="json", exclude={"name", "api_key"})
        },
        "execution_environment": {"provider": "inherit"},
    }


def _identity(backend: Backend | None = None):
    backend = backend or _backend()
    identities, by_backend, migration = build_agent_system_identities(
        _configuration(backend), {backend.name: backend}
    )
    return identities[0], by_backend, migration


def _context(tmp_path: Path, backend: Backend | None = None) -> AttemptContext:
    backend = backend or _backend()
    run = tmp_path / "runs" / "run_1"
    attempt = run / "attempts" / "attempt_1"
    attempt.mkdir(parents=True)
    return AttemptContext(
        run_id="run_1",
        trace_id="trace_1",
        task_id="task_1",
        attempt_id="attempt_1",
        ordinal=1,
        task="Change the fixture.",
        repository_path=str(tmp_path / "repository"),
        success_criteria="The fixture is changed.",
        requires_file_changes=True,
        backend_name=backend.name,
        model=backend.model,
        policy_configuration=_configuration(backend),
        run_directory=run,
        attempt_directory=attempt,
        baseline_sha256=BASELINE,
        cancellation_event=threading.Event(),
    )


class _FakeVillaniCodeAttempt:
    def run(self, context: AttemptContext) -> AttemptResult:
        now = datetime.now(timezone.utc)
        return AttemptResult(
            runner_name="fake-villani-code",
            status="completed",
            worktree_path=str(Path(context.attempt_directory) / "worktree"),
            patch="diff --git a/a.py b/a.py\n",
            exit_code=0,
            model=context.model,
            stdout="runner stdout",
            stderr="runner stderr",
            trace={
                "raw_trace_path": "attempts/attempt_1/trace/raw",
                "api_key": "trace-secret-must-not-persist",
            },
            trace_path="attempts/attempt_1/trace/raw",
            runtime_events=(
                RuntimeEvent(
                    event_type="command_completed",
                    timestamp=now,
                    payload={"exit_code": 0},
                ),
                RuntimeEvent(
                    event_type="future.vendor.event",
                    timestamp=now,
                    payload={"safe": True},
                ),
            ),
            duration_ms=10,
            duration_accounting_status="complete",
            input_tokens=None,
            output_tokens=None,
            token_accounting_status="unknown",
            cost_usd=None,
            cost_accounting_status="unknown",
            metadata={"changed_files": ["src\\a.py"]},
        )

    def execute_focused_probes(self, context, result, requests):
        return replace(result, metadata={**dict(result.metadata), "probes": requests})


def test_legacy_configuration_migrates_without_secret_or_identity_instability() -> None:
    first = _backend(api_key="secret-one")
    second = _backend(api_key="secret-two")
    first_config = {
        **_configuration(first),
        "backends": {first.name: first.model_dump(mode="json", exclude={"name"})},
    }
    second_config = {
        **_configuration(second),
        "backends": {second.name: second.model_dump(mode="json", exclude={"name"})},
    }
    migrated, report = migrate_agent_system_configuration(first_config)
    assert migrated["backends"] == first_config["backends"]
    assert report["legacy_backends_preserved"] is True
    identity_one = build_agent_system_identities(first_config, {"code": first})[0][0]
    identity_two = build_agent_system_identities(second_config, {"code": second})[0][0]
    assert identity_one.system_id == identity_two.system_id
    serialized = identity_one.model_dump_json()
    assert "secret-one" not in serialized
    assert "credential=must-not-persist" not in serialized
    assert identity_one.model_provider.endpoint_identity == "http://127.0.0.1:8000/v1"
    assert identity_one.capabilities["custom_model"].state == CapabilityState.UNKNOWN
    assert identity_one.harness.version != "unknown"
    assert (
        identity_one.configuration["harness"]["resolved_version"]
        == identity_one.harness.version
    )
    assert (
        identity_one.configuration["harness_contract"]["backpressure_policy"]
        == "bounded_buffer_fail_closed"
    )
    validate_protocol_document(identity_one.model_dump(mode="json"))
    tampered = identity_one.model_dump(mode="json")
    tampered["configuration"]["route_name"] = "tampered"
    with pytest.raises(ValueError, match="configuration_digest"):
        AgentSystemIdentity.model_validate(tampered)

    projection, removed = non_secret_configuration(
        {
            "apiKey": "secret-three",
            "private-key": "secret-four",
            "authorization": "Bearer secret-five",
            "api_key_env": "VILLANI_API_KEY",
            "private_key_ref": "operator-managed-key",
        }
    )
    assert removed is True
    assert projection == {
        "api_key_env": "VILLANI_API_KEY",
        "private_key_ref": "operator-managed-key",
    }


def test_external_harnesses_remain_visible_but_cannot_be_enabled_or_selected() -> None:
    backend = _backend()
    config = _configuration(backend)
    config["agent_systems"] = {
        "schema_version": "villani.agent_system_configuration.v1",
        "systems": {
            "code": {
                "backend": "code",
                "production_enabled": True,
                "qualification_status": "qualified",
                "harness": {"id": "codex", "display_name": "Codex"},
            }
        },
    }
    with pytest.raises(ValueError, match="production-disabled"):
        AgentSystemRegistry(config, {"code": backend})
    config["agent_systems"]["systems"]["code"]["production_enabled"] = False  # type: ignore[index]
    registry = AgentSystemRegistry(config, {"code": backend})
    assert registry.list()[0].qualification_status == "unsupported"
    assert registry.doctor("code")[0].checks[-1].status == "fail"
    with pytest.raises(ValueError, match="disabled"):
        registry.attempt_runner()._resolve("code")  # noqa: SLF001

    incompatible = _configuration(backend)
    incompatible["agent_systems"] = {
        "schema_version": "villani.agent_system_configuration.v1",
        "systems": {
            "code": {
                "backend": "code",
                "production_enabled": True,
                "qualification_status": "qualified",
                "harness": {
                    "id": "villani-code",
                    "protocol_version": "villani.harness_adapter.v999",
                },
                "qualification_references": [
                    {"kind": "conformance", "reference": "fixture"}
                ],
            }
        },
    }
    incompatible_registry = AgentSystemRegistry(incompatible, {"code": backend})
    assert incompatible_registry.list()[0].qualification_status == "unqualified"
    assert incompatible_registry.list()[0].production_enabled is False


def test_villani_code_uses_complete_lifecycle_and_writes_equivalent_evidence(
    tmp_path: Path,
) -> None:
    backend = _backend()
    identity, by_backend, migration = _identity(backend)
    adapter = VillaniCodeHarnessAdapter(
        identity,
        {backend.name: backend},
        implementation=_FakeVillaniCodeAttempt(),  # type: ignore[arg-type]
    )
    runner = AgentSystemAttemptRunner(
        (identity,),
        by_backend,
        {identity.system_id: adapter},
        migration_report=migration,
    )
    context = _context(tmp_path, backend)
    probe = adapter.probe()
    assert probe["protocol_version"] == "villani.harness_adapter.v1"
    assert set(probe["lifecycle_operations"]) == {
        "probe",
        "describe_capabilities",
        "prepare_session",
        "execute_task",
        "stream_events",
        "request_cancellation",
        "collect_result",
        "collect_artifacts",
        "cleanup",
        "doctor",
    }
    assert (
        probe["runtime_contract"]["max_stdout_bytes"] == MAXIMUM_HARNESS_MESSAGE_BYTES
    )
    assert "villani.harness_adapter.v1" in AGENT_RUNNER_MANIFEST.protocol_versions
    result = runner.run(context)
    assert result.metadata["agent_system_id"] == identity.system_id
    assert result.cost_usd is None and result.cost_accounting_status == "unknown"
    assert [event.event_type for event in result.runtime_events] == [
        "session_started",
        "command_complete",
        "raw_villani_code_future_vendor_event",
        "session_complete",
    ]
    evidence_path = Path(context.run_directory) / str(
        result.metadata["harness_result_path"]
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    parsed = HarnessResult.model_validate(evidence)
    validate_protocol_document(evidence)
    assert "trace-secret-must-not-persist" not in json.dumps(evidence)
    assert parsed.baseline_digest == BASELINE
    assert parsed.patch == result.patch
    assert parsed.changed_files == ["src/a.py"]
    assert parsed.cleanup.status == "succeeded"
    assert parsed.normalized_events[2].raw_namespace == "villani-code"


def test_controller_bundle_records_complete_system_identity_and_attempt_link(
    tmp_path: Path,
) -> None:
    backend = _backend().model_copy(update={"model": "code-model"})
    identity, by_backend, migration = _identity(backend)
    adapter = VillaniCodeHarnessAdapter(
        identity,
        {backend.name: backend},
        implementation=_FakeVillaniCodeAttempt(),  # type: ignore[arg-type]
    )
    runner = AgentSystemAttemptRunner(
        (identity,),
        by_backend,
        {identity.system_id: adapter},
        migration_report=migration,
    )
    migrated, _ = migrate_agent_system_configuration(_configuration(backend))
    role_registry = RoleSystemRegistry(migrated, {backend.name: backend})
    role_bindings = role_registry.resolve_profile("api")
    invocation_identities = role_registry.invocation_identities(role_bindings)
    controller = ClosedLoopController(
        classifier=FakeClassifier(),
        policy_engine=FakePolicyEngine(
            [
                policy("attempt", backend_option=backend_option("code")),
                policy("select"),
            ]
        ),
        attempt_runner=runner,
        verifier=FakeVerifier([accepted_verification()]),
        selector=FakeSelector(),
        materializer=FakeMaterializer(),
        now=FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
        role_bindings=role_bindings,
        agent_invocation_identities=invocation_identities,
    )
    result = controller.run(
        ClosedLoopRunRequest(
            task="Change the fixture.",
            repository_path=tmp_path / "repository",
            success_criteria="The fixture is changed.",
            runs_root=tmp_path / "runs",
            max_attempts=1,
            policy_configuration=_configuration(backend),
        )
    )

    assert result.terminal_state == "COMPLETED"
    manifest = json.loads(
        (result.run_directory / "manifest.json").read_text(encoding="utf-8")
    )
    attempt_snapshot = json.loads(
        (result.run_directory / "attempts" / "attempt_001" / "attempt.json").read_text(
            encoding="utf-8"
        )
    )
    identity_path = result.run_directory / manifest["artifact_paths"]["agent_systems"]
    identity_document = json.loads(
        (
            result.run_directory / attempt_snapshot["agent_system_identity_path"]
        ).read_text(encoding="utf-8")
    )
    assert manifest["agent_system_ids"] == [identity.system_id]
    assert manifest["execution_profile_id"] == "api"
    assert manifest["role_bindings"]["coding"] == "villani-code-runner"
    assert set(manifest["agent_invocation_ids"]) == {
        "classification",
        "coding",
        "verification",
        "selection",
    }
    role_bindings_path = (
        result.run_directory / manifest["artifact_paths"]["role_bindings"]
    )
    invocation_index_path = (
        result.run_directory / manifest["artifact_paths"]["agent_invocations"]
    )
    assert (
        json.loads(role_bindings_path.read_text(encoding="utf-8"))["bindings"]
        == manifest["role_bindings"]
    )
    assert set(
        json.loads(invocation_index_path.read_text(encoding="utf-8"))["roles"]
    ) == set(manifest["agent_invocation_ids"])
    assert attempt_snapshot["agent_system_id"] == identity.system_id
    assert identity_document["system_id"] == identity.system_id
    assert (
        json.loads(identity_path.read_text(encoding="utf-8"))["systems"][0]["system_id"]
        == identity.system_id
    )
    assert (result.run_directory / attempt_snapshot["harness_result_path"]).is_file()


def test_cancellation_path_safety_event_ordering_and_bounds_fail_closed(
    tmp_path: Path,
) -> None:
    backend = _backend()
    identity, _, _ = _identity(backend)
    adapter = VillaniCodeHarnessAdapter(
        identity,
        {backend.name: backend},
        implementation=_FakeVillaniCodeAttempt(),  # type: ignore[arg-type]
    )
    context = _context(tmp_path, backend)
    session = adapter.prepare_session(context)
    assert adapter.request_cancellation(session) is True
    assert context.cancellation_event.is_set()
    result = adapter.execute_task(session, context)
    events = adapter.stream_events(session, result)
    cleanup = adapter.cleanup(session)
    harness = adapter.collect_result(session, context, result, events, cleanup)
    with pytest.raises(ValueError, match="worktree-relative"):
        HarnessResult.model_validate(
            {**harness.model_dump(mode="json"), "changed_files": ["../escape.py"]}
        )
    unordered = harness.model_dump(mode="json")
    unordered["normalized_events"][1]["sequence"] = 7
    with pytest.raises(ValueError, match="contiguous"):
        HarnessResult.model_validate(unordered)
    with pytest.raises(ValueError, match="stdout exceeds"):
        HarnessResult.model_validate(
            {
                **harness.model_dump(mode="json"),
                "stdout": "x" * (MAXIMUM_HARNESS_MESSAGE_BYTES + 1),
            }
        )
    with pytest.raises(ValueError, match="unknown cost"):
        HarnessCost(amount=0, currency="USD", accounting_status="unknown")
    with pytest.raises(ValueError, match="run-relative"):
        HarnessArtifact(kind="trace", path="../outside.json")
    with pytest.raises(ValueError, match="permission requests"):
        NormalizedHarnessEvent(
            sequence=1,
            timestamp=datetime.now(timezone.utc),
            name="permission_request",
            payload={},
        )
    cancelled = replace(result, status="cancelled")
    cancelled_events = adapter.stream_events(session, cancelled)
    assert cancelled_events[-1].name == "cancellation"
    unsafe_reasoning = replace(
        result,
        runtime_events=(
            RuntimeEvent(
                event_type="reasoning_summary",
                timestamp=datetime.now(timezone.utc),
                payload={"text": "private chain", "safe_to_persist": False},
            ),
        ),
    )
    reasoning_events = adapter.stream_events(session, unsafe_reasoning)
    assert reasoning_events[1].name == "warning"
    assert "private chain" not in json.dumps(
        [item.model_dump(mode="json") for item in reasoning_events]
    )
    out_of_order = replace(
        result,
        runtime_events=(
            RuntimeEvent(
                event_type="warning",
                timestamp=session.prepared_at,
                payload={"message": "first"},
            ),
            RuntimeEvent(
                event_type="warning",
                timestamp=session.prepared_at.replace(year=2025),
                payload={"message": "second"},
            ),
        ),
    )
    normalized = adapter.stream_events(session, out_of_order)
    assert [item.timestamp for item in normalized] == sorted(
        item.timestamp for item in normalized
    )
    assert "event_timestamp_adjusted_from" in normalized[2].payload
    with pytest.raises(ValueError, match="target repository directly"):
        adapter.collect_result(
            session,
            context,
            replace(result, worktree_path=context.repository_path),
            events,
            cleanup,
        )


@pytest.mark.parametrize(
    ("failure_code", "expected_category"),
    [
        ("runner_timeout", "timeout"),
        ("process_crash", "process"),
        ("permission_denied", "permission"),
    ],
)
def test_failure_classification_is_structured_and_not_inferred_as_success(
    tmp_path: Path, failure_code: str, expected_category: str
) -> None:
    backend = _backend()
    identity, _, _ = _identity(backend)
    adapter = VillaniCodeHarnessAdapter(
        identity,
        {backend.name: backend},
        implementation=_FakeVillaniCodeAttempt(),  # type: ignore[arg-type]
    )
    context = _context(tmp_path, backend)
    session = adapter.prepare_session(context)
    result = adapter.execute_task(session, context)
    failed = replace(
        result,
        status="failed",
        error=DependencyFailure(
            code=failure_code,
            message="The harness failed without acceptance evidence.",
            details={"retryable": False},
        ),
    )
    events = adapter.stream_events(session, failed)
    evidence = adapter.collect_result(
        session, context, failed, events, adapter.cleanup(session)
    )
    assert evidence.harness_status == "failed"
    assert evidence.infrastructure_failure is not None
    assert evidence.infrastructure_failure.category == expected_category
    assert evidence.infrastructure_failure.retryable is False


def test_missing_executable_and_malformed_results_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(command="definitely-missing-villani-code")
    identity, _, _ = _identity(backend)
    adapter = VillaniCodeHarnessAdapter(identity, {backend.name: backend})
    monkeypatch.setattr(
        "villani_ops.closed_loop.agent_systems.adapters.resolve_command_prefix",
        lambda _command: None,
    )
    report = adapter.doctor()
    assert report.selectable is False
    assert (
        next(check for check in report.checks if check.name == "executable").status
        == "fail"
    )
    with pytest.raises(ValueError):
        HarnessResult.model_validate(
            {
                "schema_version": "villani.harness_result.v1",
                "system_id": identity.system_id,
                "malformed": True,
            }
        )


def test_conformance_report_requires_every_scenario_and_authorizes_only_all_pass() -> (
    None
):
    identity, _, _ = _identity()
    observations = {
        name: {
            "status": "pass",
            "reason": f"{name} exercised by the PT5 test kit.",
            "evidence": {"test": f"test_pt5_agent_systems::{name}"},
        }
        for name in REQUIRED_CONFORMANCE_CHECKS
    }
    report = build_harness_conformance_report(identity, observations)
    assert report.status == "passed"
    assert report.production_qualification_authorized is True
    assert {item.check_id for item in report.checks} == set(REQUIRED_CONFORMANCE_CHECKS)
    incomplete = build_harness_conformance_report(identity, {})
    assert incomplete.status == "insufficient_evidence"
    assert incomplete.production_qualification_authorized is False
    validate_protocol_document(report.model_dump(mode="json"))


def test_agents_cli_migrates_lists_inspects_and_doctors_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    cli = CliRunner()
    assert cli.invoke(unified.app, ["init"]).exit_code == 0
    config_path = home / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    backend = _backend(api_key="cli-secret")
    config["backends"] = {"code": backend.model_dump(mode="json", exclude={"name"})}
    # Exercise loading a legacy config: the explicit agent_systems key is absent.
    config.pop("agent_systems", None)
    unified._write_config(config_path, config)
    listing = cli.invoke(unified.app, ["agents", "list", "--json"])
    assert listing.exit_code == 0, listing.output
    assert "cli-secret" not in listing.output
    document = json.loads(listing.output)
    system_id = document["systems"][0]["system_id"]
    inspected = cli.invoke(unified.app, ["agents", "inspect", system_id, "--json"])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output)["schema_version"] == "villani.agent_system.v1"
    doctor = cli.invoke(unified.app, ["agents", "doctor", "code", "--json"])
    assert doctor.exit_code == 0, doctor.output
    assert json.loads(doctor.output)["reports"][0]["selectable"] is True


def test_public_controller_source_does_not_construct_villani_code_adapter() -> None:
    source = Path(unified.__file__).read_text(encoding="utf-8")
    assert "VillaniCodeAttemptAdapter" not in source
    assert "agent_registry.attempt_runner()" in source


def test_semantic_verification_and_selection_remain_harness_neutral() -> None:
    verification_context = inspect.getsource(
        VillaniVerifierAdapter._verification_context  # noqa: SLF001
    )
    for forbidden in (
        "agent_system",
        "harness",
        "backend_name",
        "cost_usd",
        "competing_candidate",
    ):
        assert forbidden not in verification_context
    selector = inspect.getsource(EvidenceSelectorAdapter.select)
    assert "agent_system" not in selector
    assert "harness" not in selector
