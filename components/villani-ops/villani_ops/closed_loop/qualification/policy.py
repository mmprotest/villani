"""Repository/task-aware qualification resolution and conservative backoff."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ..agent_systems.models import AgentSystemIdentity, CapabilityState
from .models import (
    BackoffLevel,
    QualificationAssessment,
    QualificationBackoffEvidence,
    QualificationDriftFlag,
    QualificationInvalidation,
    QualificationObservation,
    QualificationPolicy,
    QualificationSystemIdentity,
    QualificationTaskProfile,
)
from .repository import (
    RepositoryQualificationContext,
    commit_is_ancestor,
    exact_conformance_status,
    execution_environment_fingerprint,
    qualification_system_identity,
)
from .scoring import active_observations, qualification_statistics
from .store import QualificationStore, qualification_policy_from_configuration


_DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def task_profile(
    category: str,
    difficulty: str,
    risk: str,
    required_capabilities: Iterable[str] = (),
) -> QualificationTaskProfile:
    return QualificationTaskProfile(
        category=category or "unknown",
        difficulty=difficulty or "hard",
        risk=risk or "high",
        required_capabilities=sorted(
            {str(value) for value in required_capabilities if str(value)}
        ),
    )


def _at_least(value: str, requested: str, order: Mapping[str, int]) -> bool:
    return order.get(value, max(order.values())) >= order.get(
        requested, max(order.values())
    )


def _task_compatible(
    observed: QualificationTaskProfile,
    requested: QualificationTaskProfile,
    *,
    category_required: bool,
) -> bool:
    if category_required and observed.category != requested.category:
        return False
    if not _at_least(observed.difficulty, requested.difficulty, _DIFFICULTY_ORDER):
        return False
    if not _at_least(observed.risk, requested.risk, _RISK_ORDER):
        return False
    return set(requested.required_capabilities).issubset(observed.required_capabilities)


def _threshold(policy: QualificationPolicy, risk: str) -> float:
    if risk in policy.task_wilson_thresholds:
        return policy.task_wilson_thresholds[risk]
    if "default" in policy.task_wilson_thresholds:
        return policy.task_wilson_thresholds["default"]
    return max(policy.task_wilson_thresholds.values())


def _cohorts(
    policy: QualificationPolicy, repository_id: str
) -> list[tuple[str, list[str]]]:
    return sorted(
        (
            cohort,
            sorted(set(repositories)),
        )
        for cohort, repositories in policy.compatible_repository_cohorts.items()
        if repository_id in repositories
    )


def _matches_scope(
    invalidation: QualificationInvalidation,
    identity: AgentSystemIdentity,
    repository_id: str,
) -> bool:
    return bool(
        invalidation.route_name == identity.route_name
        and invalidation.system_id == identity.system_id
        and (
            invalidation.repository_id is None
            or invalidation.repository_id == repository_id
        )
    )


def _identity_drift(
    related: list[QualificationObservation],
    current: QualificationSystemIdentity,
    *,
    requalified_current_identity: bool,
) -> list[QualificationDriftFlag]:
    flags: list[QualificationDriftFlag] = []
    mismatched = [
        item
        for item in related
        if item.system.identity_digest != current.identity_digest
    ]
    if not mismatched:
        return flags

    def add(code: str, detail: str, rows: list[QualificationObservation]) -> None:
        if rows:
            flags.append(
                QualificationDriftFlag(
                    code=code,
                    severity=("warning" if requalified_current_identity else "severe"),
                    detail=detail,
                    evidence_ids=sorted(item.observation_id for item in rows),
                )
            )

    add(
        "harness_incompatibility",
        "The harness, adapter, or structured-protocol identity differs from repository evidence.",
        [
            item
            for item in mismatched
            if item.system.harness_id != current.harness_id
            or item.system.harness_version != current.harness_version
            or item.system.adapter_id != current.adapter_id
            or item.system.adapter_version != current.adapter_version
            or item.system.protocol != current.protocol
            or item.system.protocol_version != current.protocol_version
        ],
    )
    add(
        "model_identity_change",
        "The complete model identity differs from repository evidence.",
        [
            item
            for item in mismatched
            if item.system.model_id != current.model_id
            or item.system.model_revision != current.model_revision
        ],
    )
    add(
        "provider_identity_change",
        "The provider or serving-engine identity differs from repository evidence.",
        [
            item
            for item in mismatched
            if item.system.provider != current.provider
            or item.system.serving_engine != current.serving_engine
            or item.system.serving_engine_version != current.serving_engine_version
        ],
    )
    add(
        "execution_environment_change",
        "The measured execution-environment fingerprint differs from repository evidence.",
        [
            item
            for item in mismatched
            if item.system.execution_provider != current.execution_provider
            or item.system.execution_environment_fingerprint
            != current.execution_environment_fingerprint
        ],
    )
    add(
        "verification_policy_change",
        "The verification-policy version differs from repository evidence.",
        [
            item
            for item in mismatched
            if item.system.verification_policy_version
            != current.verification_policy_version
        ],
    )
    return flags


def assess_qualification(
    *,
    identity: AgentSystemIdentity,
    repository: RepositoryQualificationContext,
    requested_task: QualificationTaskProfile,
    configuration: Mapping[str, Any],
    store: QualificationStore,
    backend_execution_selection: str | None = None,
    policy: QualificationPolicy | None = None,
    evaluated_at: datetime | None = None,
) -> QualificationAssessment:
    """Resolve one system conservatively without mutating evidence or routing."""

    now = evaluated_at or datetime.now(timezone.utc)
    selected_policy = policy or qualification_policy_from_configuration(configuration)
    threshold = _threshold(selected_policy, requested_task.risk)
    unsupported: list[str] = []
    readiness = identity.readiness
    if not identity.production_enabled:
        unsupported.append("system is explicitly disabled or not production-ready")
    if readiness is not None:
        if not readiness.installed:
            unsupported.append("harness executable is not installed")
        if readiness.version_supported is False:
            unsupported.append("harness version is incompatible")
        if readiness.authentication_status == "not_ready":
            unsupported.append("harness authentication is not ready")
    conformance = exact_conformance_status(identity)
    if conformance == "failed":
        unsupported.append("exact harness conformance failed")
    for capability in sorted(
        set(requested_task.required_capabilities)
        | {"isolated_worktree", "non_interactive_execution"}
    ):
        assessment = identity.capabilities.get(capability)
        if assessment is None or assessment.state != CapabilityState.SUPPORTED:
            unsupported.append(
                f"required capability {capability!r} is absent or unproved"
            )

    environment_error: str | None = None
    try:
        environment = execution_environment_fingerprint(
            identity,
            repository,
            configuration,
            backend_execution_selection=backend_execution_selection,
        )
    except Exception as error:  # provider errors are evidence, never qualification
        environment = "unavailable"
        environment_error = error.__class__.__name__
        unsupported.append(
            f"execution-environment fingerprint is unavailable ({environment_error})"
        )
    current = qualification_system_identity(
        identity, environment_fingerprint=environment
    )

    observations, _superseded = active_observations(store.load_observations())
    invalidations = store.load_invalidations()
    repository_rows = [
        item
        for item in observations
        if item.repository_id == repository.repository_id
        and item.system.route_name == identity.route_name
    ]
    lineage_rows: list[QualificationObservation] = []
    divergent_rows: list[QualificationObservation] = []
    for observation in repository_rows:
        if commit_is_ancestor(repository, observation.repository_commit):
            lineage_rows.append(observation)
        else:
            divergent_rows.append(observation)
    exact_identity_rows = [
        item
        for item in lineage_rows
        if item.system.identity_digest == current.identity_digest
    ]
    drift_flags = _identity_drift(
        [item for item in lineage_rows if item.eligible],
        current,
        requalified_current_identity=(
            sum(item.eligible for item in exact_identity_rows)
            >= selected_policy.minimum_qualified_observations
        ),
    )
    eligible_divergent_rows = [item for item in divergent_rows if item.eligible]
    if eligible_divergent_rows:
        drift_flags.append(
            QualificationDriftFlag(
                code="repository_lineage_divergence",
                severity=(
                    "warning"
                    if sum(item.eligible for item in exact_identity_rows)
                    >= selected_policy.minimum_qualified_observations
                    else "severe"
                ),
                detail=(
                    "Repository evidence is not an ancestor of the current HEAD; "
                    "history is retained but cannot qualify this lineage."
                ),
                evidence_ids=sorted(
                    item.observation_id for item in eligible_divergent_rows
                ),
            )
        )

    levels: list[
        tuple[BackoffLevel, list[str], str | None, list[QualificationObservation], bool]
    ] = []
    levels.append(
        (
            "exact_repository_task",
            [repository.repository_id],
            None,
            [
                item
                for item in exact_identity_rows
                if item.task_profile == requested_task
            ],
            "exact_repository_task" in selected_policy.approved_backoff_levels,
        )
    )
    levels.append(
        (
            "repository_category",
            [repository.repository_id],
            None,
            [
                item
                for item in exact_identity_rows
                if _task_compatible(
                    item.task_profile, requested_task, category_required=True
                )
            ],
            "repository_category" in selected_policy.approved_backoff_levels,
        )
    )
    levels.append(
        (
            "repository_wide",
            [repository.repository_id],
            None,
            [
                item
                for item in exact_identity_rows
                if _task_compatible(
                    item.task_profile, requested_task, category_required=False
                )
            ],
            "repository_wide" in selected_policy.approved_backoff_levels,
        )
    )
    for cohort, repository_ids in _cohorts(selected_policy, repository.repository_id):
        cohort_rows = [
            item
            for item in observations
            if item.repository_id in repository_ids
            and item.repository_id != repository.repository_id
            and item.system.identity_digest == current.identity_digest
            and _task_compatible(
                item.task_profile, requested_task, category_required=True
            )
        ]
        levels.append(
            (
                "compatible_repository_cohort",
                repository_ids,
                cohort,
                cohort_rows,
                bool(
                    "compatible_repository_cohort"
                    in selected_policy.approved_backoff_levels
                    and cohort in selected_policy.approved_repository_cohorts
                ),
            )
        )

    selected_index: int | None = None
    for index, (_level, _repositories, _cohort, rows, _approved) in enumerate(levels):
        if (
            sum(item.eligible for item in rows)
            >= selected_policy.minimum_qualified_observations
        ):
            selected_index = index
            break
    if selected_index is None:
        selected_index = next(
            (
                index
                for index, (
                    _level,
                    _repositories,
                    _cohort,
                    rows,
                    _approved,
                ) in enumerate(levels)
                if rows
            ),
            None,
        )
    selected_rows = levels[selected_index][3] if selected_index is not None else []
    selected_level = levels[selected_index][0] if selected_index is not None else None
    selected_cohort = levels[selected_index][2] if selected_index is not None else None
    selected_approved = (
        levels[selected_index][4] if selected_index is not None else False
    )

    matching_invalidations = [
        item
        for item in invalidations
        if _matches_scope(item, identity, repository.repository_id)
    ]
    for invalidation in matching_invalidations:
        drift_flags.append(
            QualificationDriftFlag(
                code=invalidation.reason,
                severity=invalidation.severity,
                detail=invalidation.detail,
                evidence_ids=[invalidation.invalidation_id],
            )
        )
        if invalidation.severity == "unsupported":
            unsupported.append(invalidation.detail)

    statistics = qualification_statistics(
        selected_rows,
        drift_flags=drift_flags,
        wilson_z=selected_policy.wilson_z,
    )
    if statistics.sample_count >= selected_policy.minimum_qualified_observations:
        recent = sorted(
            (item for item in selected_rows if item.eligible),
            key=lambda item: (item.observed_at, item.observation_id),
        )[-selected_policy.recent_reliability_window :]
        if (
            recent
            and sum(item.successful is True for item in recent) / len(recent)
            < threshold
        ):
            breach = QualificationDriftFlag(
                code="recent_reliability_breach",
                severity="severe",
                detail=(
                    "The recent eligible evidence window is below the current task threshold."
                ),
                evidence_ids=[item.observation_id for item in recent],
            )
            drift_flags.append(breach)
            statistics = qualification_statistics(
                selected_rows,
                drift_flags=drift_flags,
                wilson_z=selected_policy.wilson_z,
            )

    stale = False
    if statistics.last_evidence_at is not None:
        stale = (
            now - statistics.last_evidence_at
        ).days > selected_policy.maximum_evidence_age_days
        if stale:
            drift_flags.append(
                QualificationDriftFlag(
                    code="stale_evidence",
                    severity="warning",
                    detail="The newest eligible observation is outside the approved time window.",
                    evidence_ids=[],
                )
            )
            statistics = qualification_statistics(
                selected_rows,
                drift_flags=drift_flags,
                wilson_z=selected_policy.wilson_z,
            )

    severe_drift = any(item.severity == "severe" for item in statistics.drift_flags)
    false_acceptance = statistics.false_acceptance_count > 0
    conformance_unproved = conformance != "passed"
    if unsupported:
        state = "unsupported"
    elif statistics.sample_count == 0 or conformance_unproved:
        state = "experimental"
    elif false_acceptance or severe_drift:
        state = "experimental"
    elif (
        statistics.sample_count >= selected_policy.minimum_qualified_observations
        and selected_approved
        and statistics.wilson_lower_bound is not None
        and statistics.wilson_lower_bound > threshold
        and not stale
    ):
        state = "qualified"
    else:
        state = "provisional"

    backoff: list[QualificationBackoffEvidence] = []
    for index, (
        level,
        repository_ids,
        level_cohort,
        rows,
        approved,
    ) in enumerate(levels):
        reasons: list[str] = []
        count = sum(item.eligible for item in rows)
        if count == 0:
            reasons.append("no eligible observations")
        elif count < selected_policy.minimum_qualified_observations:
            reasons.append("sample size is below the qualification minimum")
        if not approved:
            reasons.append("backoff level is not approved for qualification")
        backoff.append(
            QualificationBackoffEvidence(
                level=level,
                repository_ids=repository_ids,
                cohort=level_cohort,
                eligible_observation_count=count,
                selected=index == selected_index,
                approved_for_qualification=approved,
                rejection_reasons=reasons,
            )
        )

    if state == "qualified":
        caveat = (
            f"Qualified from {statistics.sample_count} eligible observations at "
            f"{selected_level}; Wilson lower bound {statistics.wilson_lower_bound:.3f}."
        )
    elif state == "provisional":
        caveat = (
            f"Valid repository evidence exists ({statistics.sample_count} observations), "
            "but sample size, confidence, backoff approval, or recency is insufficient."
        )
    elif state == "unsupported":
        caveat = "; ".join(sorted(set(unsupported)))
    elif statistics.sample_count == 0:
        caveat = "No eligible evidence matches this repository, task profile, and exact system identity."
    elif false_acceptance:
        caveat = (
            "Known false acceptance or later defect evidence invalidates automatic use."
        )
    else:
        caveat = "Material drift or missing exact conformance requires new repository evidence."

    return QualificationAssessment(
        policy_version=selected_policy.policy_version,
        system_id=identity.system_id,
        route_name=identity.route_name,
        repository_id=repository.repository_id,
        repository_head=repository.head,
        task_profile=requested_task,
        state=state,  # type: ignore[arg-type]
        selected_level=selected_level,
        selected_cohort=selected_cohort,
        task_wilson_threshold=threshold,
        statistics=statistics,
        backoff_evidence=backoff,
        automatic_eligible=state == "qualified",
        provisional_fallback_eligible=state == "provisional",
        manual_override_required=state == "experimental",
        unsupported_reasons=sorted(set(unsupported)),
        caveat=caveat,
        doctor_action=f"villani agents doctor {identity.route_name}",
        evidence_action=(
            f"villani agents evidence {identity.route_name} --repo {repository.path}"
        ),
        evaluated_at=now,
    )


def assess_configured_systems(
    *,
    identities: Iterable[AgentSystemIdentity],
    repository: RepositoryQualificationContext,
    requested_task: QualificationTaskProfile,
    configuration: Mapping[str, Any],
    store: QualificationStore,
    backend_execution_selections: Mapping[str, str | None] | None = None,
    evaluated_at: datetime | None = None,
) -> tuple[QualificationAssessment, ...]:
    selections = backend_execution_selections or {}
    policy = qualification_policy_from_configuration(configuration)
    return tuple(
        assess_qualification(
            identity=identity,
            repository=repository,
            requested_task=requested_task,
            configuration=configuration,
            store=store,
            backend_execution_selection=selections.get(identity.route_name),
            policy=policy,
            evaluated_at=evaluated_at,
        )
        for identity in sorted(identities, key=lambda item: item.route_name)
    )


__all__ = [
    "assess_configured_systems",
    "assess_qualification",
    "task_profile",
]
