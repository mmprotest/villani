from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from villani_ops.cli.unified import app
from villani_ops.closed_loop.agent_systems.models import (
    AgentSystemIdentity,
    HarnessReadiness,
    configuration_digest,
)
from villani_ops.closed_loop.interfaces import BudgetContext, PolicyContext
from villani_ops.closed_loop.policy import BootstrapPolicyEngine
from villani_ops.closed_loop.protocol import ClassificationSnapshot
from villani_ops.closed_loop.qualification import (
    QualificationInvalidation,
    QualificationObservation,
    QualificationPolicy,
    QualificationStore,
    assess_qualification,
    build_gate_c_report,
    repository_qualification_context,
    task_profile,
)
from villani_ops.closed_loop.qualification.repository import (
    qualification_system_identity,
)
from villani_ops.closed_loop.qualification.scoring import wilson_lower_bound
from villani_ops.closed_loop.qualification.store import (
    qualification_policy_from_configuration,
)
from villani_ops.core.backend import Backend


NOW = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[4]
IDENTITY_FIXTURE = (
    ROOT
    / "integration"
    / "fixtures"
    / "protocol"
    / "v1"
    / "valid_run"
    / "agent-systems"
    / "asys_d605dea1f6503cf9996864423c705228b426ccee3c2e02869084ac9bbbbda575.json"
)


def _run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True)
    _run_git(path, "init")
    _run_git(path, "config", "user.email", "pt7@example.invalid")
    _run_git(path, "config", "user.name", "PT7 Fixture")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _run_git(path, "add", "README.md")
    _run_git(path, "commit", "-m", "baseline")
    return path, _run_git(path, "rev-parse", "HEAD")


def _identity(
    route: str = "villani_code",
    *,
    harness_id: str = "villani-code",
    harness_version: str = "1.0.0",
    provider: str = "local",
    model: str = "villani-model",
    environment: str = "pt7-environment-v1",
    conformance: str = "passed",
) -> AgentSystemIdentity:
    base = AgentSystemIdentity.model_validate_json(
        IDENTITY_FIXTURE.read_text(encoding="utf-8")
    )
    protocol = {
        "villani-code": ("villani-harness", "villani.harness_adapter.v1"),
        "codex": ("codex-app-server-jsonrpc", "codex.app_server.v1"),
        "claude-code": ("claude-code-stream-json", "claude.stream_json.v1"),
    }[harness_id]
    display_name = {
        "villani-code": "Villani Code",
        "codex": "Codex",
        "claude-code": "Claude Code",
    }[harness_id]
    configuration = {
        "route_name": route,
        "backend": route,
        "harness": {
            "id": harness_id,
            "version": harness_version,
            "protocol": protocol[0],
            "protocol_version": protocol[1],
        },
        "provider": provider,
        "model": model,
        "environment_fingerprint": environment,
        "verification_policy": "controller_acceptance_evidence_v1",
    }
    digest, projection, removed = configuration_digest(configuration)
    assert not removed
    document = base.model_dump(mode="json")
    document.update(
        {
            "system_id": f"asys_{digest.removeprefix('sha256:')}",
            "route_name": route,
            "production_enabled": True,
            "qualification_status": "provisional",
            "configuration": projection,
            "configuration_digest": digest,
            "unknown_fields": [],
            "readiness": HarnessReadiness(
                installed=True,
                command_identity=harness_id,
                exact_version=harness_version,
                supported_version_range=f"=={harness_version}",
                version_supported=True,
                authentication_status="ready",
                protocol=protocol[0],
                conformance_status=conformance,  # type: ignore[arg-type]
                qualification_state="provisional",
                custom_model_capability="unknown",
                custom_provider_capability="unknown",
                local_model_capability="unknown",
                repair_action=f"Run villani agents doctor {route}",
                details={},
            ).model_dump(mode="json"),
        }
    )
    document["harness"].update(
        {
            "harness_id": harness_id,
            "display_name": display_name,
            "version": harness_version,
            "adapter_id": f"villani.{harness_id}.adapter",
            "protocol": protocol[0],
            "protocol_version": protocol[1],
            "transport": (
                "direct_protocol"
                if harness_id == "codex"
                else "structured_headless_cli"
            ),
        }
    )
    document["model_provider"].update(
        {
            "provider": provider,
            "model_id": model,
            "model_revision": "fixture-revision-1",
            "endpoint_identity": None,
        }
    )
    document["execution"]["environment_fingerprint"] = environment
    return AgentSystemIdentity.model_validate(document)


def _observation(
    identity: AgentSystemIdentity,
    repository_id: str,
    commit: str,
    ordinal: int,
    *,
    profile=None,
    eligible: bool = True,
    successful: bool = True,
    false_acceptance: bool = False,
    cost: float | None = None,
    trial_id: str | None = None,
    recorded_at: datetime | None = None,
) -> QualificationObservation:
    selected_profile = profile or task_profile("maintenance", "easy", "low")
    stamp = recorded_at or NOW + timedelta(seconds=ordinal)
    trial = trial_id or f"{identity.route_name}-trial-{ordinal:03d}"
    infrastructure = "resolved" if eligible else "excluded"
    exclusion = None if eligible else "provider_outage"
    expected_success = successful and not false_acceptance if eligible else None
    token = (
        f"{identity.system_id}:{repository_id}:{trial}:{stamp.isoformat()}:"
        f"{eligible}:{successful}:{false_acceptance}"
    )
    return QualificationObservation(
        observation_id="qobs_" + hashlib.sha256(token.encode()).hexdigest(),
        recorded_at=stamp,
        observed_at=stamp,
        source_kind="imported_qualification_evidence",
        source_suite_id="frozen-founder-suite-v1",
        source_suite_digest="1" * 64,
        source_task_id=f"task-{ordinal:03d}",
        source_task_digest="2" * 64,
        source_trial_id=trial,
        source_review_id=f"review-{ordinal:03d}",
        repository_id=repository_id,
        repository_commit=commit,
        repository_baseline_digest="3" * 64,
        task_profile=selected_profile,
        profile_source="explicit_evaluation_profile",
        system=qualification_system_identity(
            identity,
            environment_fingerprint=(
                identity.execution.environment_fingerprint or "unavailable"
            ),
        ),
        baseline_valid=True,
        candidate_evidence_complete=True,
        authoritative_verification_complete=True,
        infrastructure_status=infrastructure,
        human_review_required=True,
        human_review_status="complete",
        corruption_detected=False,
        secret_issue_detected=False,
        target_repository_modified=False,
        proved_acceptable=successful,
        accepted_as_is=successful,
        successful=expected_success,
        false_acceptance=false_acceptance,
        false_rejection=False,
        later_rollback=False,
        reopened_defect=False,
        cost_amount=cost,
        cost_currency="USD" if cost is not None else None,
        cost_accounting_status="complete" if cost is not None else "unknown",
        duration_ms=1_000 + ordinal,
        duration_accounting_status="complete",
        review_minutes=2.5,
        eligible=eligible,
        exclusion_reason=exclusion,
        artifacts=[
            {
                "kind": "evaluation_trial",
                "path": f"trials/{trial}/trial.json",
                "digest": f"sha256:{'4' * 64}",
            }
        ],
    )


def _append(
    store: QualificationStore,
    identity: AgentSystemIdentity,
    repository_id: str,
    commit: str,
    count: int,
    *,
    profile=None,
    start: int = 1,
) -> None:
    for ordinal in range(start, start + count):
        store.append_observation(
            _observation(
                identity,
                repository_id,
                commit,
                ordinal,
                profile=profile,
                cost=0.01 * ordinal if ordinal % 2 else None,
            )
        )


def _assessment(
    store: QualificationStore,
    identity: AgentSystemIdentity,
    repository: Path,
    *,
    profile=None,
    policy: QualificationPolicy | None = None,
):
    return assess_qualification(
        identity=identity,
        repository=repository_qualification_context(repository),
        requested_task=profile or task_profile("maintenance", "easy", "low"),
        configuration={},
        store=store,
        policy=policy,
        evaluated_at=NOW + timedelta(days=1),
    )


def test_state_transitions_inclusion_exclusion_wilson_and_unknown_cost(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path / "repo")
    context = repository_qualification_context(repository)
    identity = _identity()
    store = QualificationStore(tmp_path / "qualification")

    assert _assessment(store, identity, repository).state == "experimental"
    store.append_observation(
        _observation(identity, context.repository_id, commit, 1, cost=0.25)
    )
    provisional = _assessment(store, identity, repository)
    assert provisional.state == "provisional"
    assert provisional.statistics.sample_count == 1
    assert provisional.statistics.cost_distribution_by_currency["USD"].median == 0.25

    store.append_observation(
        _observation(
            identity,
            context.repository_id,
            commit,
            99,
            eligible=False,
        )
    )
    excluded = _assessment(store, identity, repository)
    assert excluded.statistics.exclusions == {"provider_outage": 1}

    _append(store, identity, context.repository_id, commit, 19, start=2)
    qualified = _assessment(store, identity, repository)
    assert qualified.state == "qualified"
    assert qualified.automatic_eligible is True
    assert qualified.statistics.sample_count == 20
    assert qualified.statistics.successes == 20
    assert qualified.statistics.cost_unknown_count == 10
    assert qualified.statistics.wilson_lower_bound == pytest.approx(
        wilson_lower_bound(20, 20)
    )
    assert qualified.statistics.wilson_lower_bound > 0.80


def test_hierarchical_backoff_and_no_language_or_framework_pooling(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path / "repo-a")
    other_repository, _other_commit = _repository(tmp_path / "repo-b")
    repository_context = repository_qualification_context(repository)
    other_context = repository_qualification_context(other_repository)
    identity = _identity()
    store = QualificationStore(tmp_path / "qualification")

    harder = task_profile("maintenance", "hard", "high")
    _append(
        store,
        identity,
        repository_context.repository_id,
        commit,
        20,
        profile=harder,
    )
    category = _assessment(
        store,
        identity,
        repository,
        profile=task_profile("maintenance", "medium", "medium"),
    )
    assert category.state == "qualified"
    assert category.selected_level == "repository_category"

    repository_wide = _assessment(
        store,
        identity,
        repository,
        profile=task_profile("documentation", "medium", "medium"),
    )
    assert repository_wide.state == "qualified"
    assert repository_wide.selected_level == "repository_wide"

    unrelated = _assessment(
        store,
        identity,
        other_repository,
        profile=task_profile("maintenance", "medium", "medium"),
    )
    assert unrelated.state == "experimental"
    assert unrelated.statistics.sample_count == 0
    assert "language" not in type(unrelated.task_profile).model_fields

    cohort_policy = QualificationPolicy(
        approved_backoff_levels=[
            "exact_repository_task",
            "repository_category",
            "repository_wide",
            "compatible_repository_cohort",
        ],
        compatible_repository_cohorts={
            "reviewed-lineage-cohort": [
                repository_context.repository_id,
                other_context.repository_id,
            ]
        },
        approved_repository_cohorts=["reviewed-lineage-cohort"],
    )
    cohort = _assessment(
        store,
        identity,
        other_repository,
        profile=task_profile("maintenance", "medium", "medium"),
        policy=cohort_policy,
    )
    assert cohort.state == "qualified"
    assert cohort.selected_level == "compatible_repository_cohort"


def test_false_acceptance_amendment_drift_lineage_and_requalification(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path / "repo")
    repository_context = repository_qualification_context(repository)
    old_identity = _identity(model="old-model")
    store = QualificationStore(tmp_path / "qualification")
    _append(store, old_identity, repository_context.repository_id, commit, 20)
    assert _assessment(store, old_identity, repository).state == "qualified"

    amended = _observation(
        old_identity,
        repository_context.repository_id,
        commit,
        1,
        false_acceptance=True,
        trial_id="villani_code-trial-001",
        recorded_at=NOW + timedelta(days=2),
    )
    store.append_observation(amended)
    downgraded = _assessment(store, old_identity, repository)
    assert downgraded.state == "experimental"
    assert downgraded.statistics.false_acceptance_count == 1
    assert store.rebuild(generated_at=NOW).superseded_observation_count == 1

    new_identity = _identity(model="new-model")
    drifted = _assessment(store, new_identity, repository)
    assert drifted.state == "experimental"
    assert "model_identity_change" in {
        flag.code for flag in drifted.statistics.drift_flags
    }

    harness_drift = _assessment(
        store,
        _identity(harness_version="1.1.0", model="old-model"),
        repository,
    )
    assert "harness_incompatibility" in {
        flag.code for flag in harness_drift.statistics.drift_flags
    }
    environment_drift = _assessment(
        store,
        _identity(environment="pt7-environment-v2", model="old-model"),
        repository,
    )
    assert "execution_environment_change" in {
        flag.code for flag in environment_drift.statistics.drift_flags
    }

    _append(
        store,
        new_identity,
        repository_context.repository_id,
        commit,
        20,
        start=101,
    )
    requalified = _assessment(store, new_identity, repository)
    assert requalified.state == "qualified"
    assert any(
        flag.code == "model_identity_change" and flag.severity == "warning"
        for flag in requalified.statistics.drift_flags
    )

    divergent_store = QualificationStore(tmp_path / "divergent")
    _append(
        divergent_store,
        new_identity,
        repository_context.repository_id,
        "f" * 40,
        20,
    )
    divergent = _assessment(divergent_store, new_identity, repository)
    assert divergent.state == "experimental"
    assert "repository_lineage_divergence" in {
        flag.code for flag in divergent.statistics.drift_flags
    }


def test_invalidation_is_append_only_and_unsupported_is_actionable(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path / "repo")
    context = repository_qualification_context(repository)
    identity = _identity()
    store = QualificationStore(tmp_path / "qualification")
    _append(store, identity, context.repository_id, commit, 20)
    invalidation = QualificationInvalidation(
        invalidation_id="qinv_" + "5" * 64,
        recorded_at=NOW,
        system_id=identity.system_id,
        route_name=identity.route_name,
        repository_id=context.repository_id,
        reason="capability_loss",
        severity="unsupported",
        evidence_reference="doctor/conformance.json",
        evidence_digest=f"sha256:{'6' * 64}",
        detail="Required isolated-worktree capability was lost.",
    )
    assert store.append_invalidation(invalidation) is True
    assert store.append_invalidation(invalidation) is False
    assessment = _assessment(store, identity, repository)
    assert assessment.state == "unsupported"
    assert assessment.automatic_eligible is False
    assert assessment.unsupported_reasons == [
        "Required isolated-worktree capability was lost."
    ]
    assert len(store.load_invalidations()) == 1

    sensitive = _observation(
        identity,
        context.repository_id,
        commit,
        999,
    ).model_copy(update={"source_task_id": "api_key=abcdefghijklmnop"})
    with pytest.raises(ValueError, match="sensitive value"):
        store.append_observation(sensitive)


def _backend(identity: AgentSystemIdentity, capability: int = 100) -> Backend:
    return Backend(
        name=identity.route_name,
        provider=identity.model_provider.provider,
        model=identity.model_provider.model_id,
        roles=["coding"],
        capability_score=capability,
        billing_mode="unknown",
    )


def _policy_context(configuration: dict) -> PolicyContext:
    classification = ClassificationSnapshot(
        schema_version="villani.classification.v1",
        classification_id="classification-pt7",
        run_id="run-pt7",
        task_id="task-pt7",
        classified_at=NOW,
        difficulty="easy",
        risk="low",
        category="maintenance",
        required_capabilities=[],
        estimated_attempts_needed=1,
        needs_tests=True,
        confidence=1.0,
        reasoning_summary="PT7 routing fixture",
        signals={},
        metadata={},
    )
    return PolicyContext(
        run_id="run-pt7",
        trace_id="trace-pt7",
        state="CLASSIFIED",
        classification=classification,
        attempts=(),
        verifications=(),
        eligible_candidate_ids=(),
        budget=BudgetContext(
            remaining_attempts=3,
            remaining_cost_usd=None,
            cost_accounting_status="not_applicable",
            remaining_wall_time_ms=None,
            duration_accounting_status="not_applicable",
        ),
        policy_configuration=configuration,
    )


def test_automatic_routing_and_manual_experimental_override(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path / "repo")
    context = repository_qualification_context(repository)
    strong = _identity("strong", model="strong-model")
    other = _identity("other", model="other-model")
    store = QualificationStore(tmp_path / "qualification")
    _append(store, strong, context.repository_id, commit, 20)
    _append(store, other, context.repository_id, commit, 1)
    backends = {"strong": _backend(strong), "other": _backend(other, 95)}
    configuration = {
        "version": "bootstrap_v1",
        "qualification": {"repository_path": str(repository)},
    }
    engine = BootstrapPolicyEngine(
        backends,
        configuration,
        qualification_store=store,
        agent_system_by_backend={"strong": strong, "other": other},
    )
    alternatives = engine._alternatives(  # noqa: SLF001
        _policy_context(configuration), 20
    )
    assert [item.backend_name for item in alternatives if item.eligible] == ["strong"]
    assert (
        next(
            item for item in alternatives if item.backend_name == "other"
        ).cost_components["repository_qualification_state"]
        == "provisional"
    )

    fallback_store = QualificationStore(tmp_path / "fallback")
    _append(fallback_store, strong, context.repository_id, commit, 1)
    _append(fallback_store, other, context.repository_id, commit, 1)
    fallback_engine = BootstrapPolicyEngine(
        backends,
        configuration,
        qualification_store=fallback_store,
        agent_system_by_backend={"strong": strong, "other": other},
    )
    fallback = fallback_engine._alternatives(  # noqa: SLF001
        _policy_context(configuration), 20
    )
    assert [item.backend_name for item in fallback if item.eligible] == ["strong"]

    empty = QualificationStore(tmp_path / "empty")
    no_override = BootstrapPolicyEngine(
        backends,
        configuration,
        qualification_store=empty,
        agent_system_by_backend={"strong": strong, "other": other},
    )._alternatives(_policy_context(configuration), 20)  # noqa: SLF001
    assert not any(item.eligible for item in no_override)

    manual_configuration = {
        **configuration,
        "qualification": {
            "repository_path": str(repository),
            "manual_override": {
                "route_name": "other",
                "allow_experimental": True,
                "qualification_created": False,
            },
        },
    }
    manual = BootstrapPolicyEngine(
        backends,
        manual_configuration,
        qualification_store=empty,
        agent_system_by_backend={"strong": strong, "other": other},
    )._alternatives(_policy_context(manual_configuration), 20)  # noqa: SLF001
    assert [item.backend_name for item in manual if item.eligible] == ["other"]
    selected = next(item for item in manual if item.backend_name == "other")
    assert selected.cost_components["repository_qualification_state"] == "experimental"
    assert selected.cost_components["qualification_manual_override"] is True


def test_gate_c_passes_only_with_matched_qualified_scorecards(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path / "repo")
    context = repository_qualification_context(repository)
    identities = [
        _identity("villani", harness_id="villani-code", model="villani-model"),
        _identity(
            "codex",
            harness_id="codex",
            harness_version="0.144.5",
            provider="openai",
            model="codex-model",
        ),
        _identity(
            "claude",
            harness_id="claude-code",
            harness_version="2.1.138",
            provider="anthropic",
            model="claude-model",
        ),
    ]
    backends = {item.route_name: _backend(item) for item in identities}
    empty = QualificationStore(tmp_path / "empty")
    insufficient = build_gate_c_report(
        identities=identities,
        backends=backends,
        repository=context,
        requested_task=task_profile("maintenance", "easy", "low"),
        configuration={},
        store=empty,
        generated_at=NOW,
    )
    assert insufficient.status == "INSUFFICIENT_EVIDENCE"
    assert {card.system_name for card in insufficient.scorecards} == {
        "Villani Code",
        "Codex",
        "Claude Code",
    }

    incomplete = build_gate_c_report(
        identities=identities[:2],
        backends={
            item.route_name: backends[item.route_name] for item in identities[:2]
        },
        repository=context,
        requested_task=task_profile("maintenance", "easy", "low"),
        configuration={},
        store=empty,
        generated_at=NOW,
    )
    assert incomplete.status == "INSUFFICIENT_EVIDENCE"
    required_scorecards = next(
        check for check in incomplete.checks if check.check_id == "required_scorecards"
    )
    assert required_scorecards.status == "insufficient_evidence"
    assert required_scorecards.actual["missing"] == ["claude-code"]

    store = QualificationStore(tmp_path / "qualified")
    for identity in identities:
        _append(store, identity, context.repository_id, commit, 20)
    report = build_gate_c_report(
        identities=identities,
        backends=backends,
        repository=context,
        requested_task=task_profile("maintenance", "easy", "low"),
        configuration={},
        store=store,
        generated_at=NOW,
    )
    assert report.status == "PASS"
    assert report.unmatched_sample_warning is None
    assert all(card.assessment.state == "qualified" for card in report.scorecards)
    assert all(card.accepted_as_is == 20 for card in report.scorecards)
    assert all(card.proved_acceptable == 20 for card in report.scorecards)
    assert all(card.false_cases == 0 for card in report.scorecards)
    assert all(
        card.known_duration and card.known_review_time for card in report.scorecards
    )


def test_legacy_migration_excludes_non_repository_capability_profiles(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    capability = home / "capabilities" / "profiles-v1.json"
    capability.parent.mkdir(parents=True)
    capability.write_text(
        json.dumps({"schema_version": "villani.capability_snapshot.v1"}),
        encoding="utf-8",
    )
    snapshot = QualificationStore(home / "qualification").rebuild(generated_at=NOW)
    assert snapshot.observation_count == 0
    assert snapshot.migrations[0].status == "excluded"
    assert snapshot.migrations[0].qualification_created is False
    assert "repository lineage" in (snapshot.migrations[0].exclusion_reason or "")


def test_agents_cli_exposes_evidence_backed_commands() -> None:
    result = CliRunner().invoke(app, ["agents", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("qualify", "status", "evidence", "invalidate", "gate-c"):
        assert command in result.output


def test_unknown_qualification_policy_fields_fail_closed() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        qualification_policy_from_configuration(
            {"qualification": {"policy": {"invented_score": 99}}}
        )
