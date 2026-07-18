"""Append-only economics evidence, derived profiles, and policy configuration."""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..durable_io import append_jsonl_durable, write_json_atomic
from ..qualification.models import QualificationDistribution, QualificationTaskProfile
from .models import (
    ECONOMICS_CONFIGURATION_SCHEMA_VERSION,
    EconomicsObservation,
    EconomicsProfile,
    EconomicsProfileKey,
    EconomicsSnapshot,
    MoneyEstimate,
    RouteConstraints,
    RoutePolicy,
    canonical_digest,
)


OBSERVATIONS_FILENAME = "observations.jsonl"
SNAPSHOT_FILENAME = "snapshot-v1.json"
LOCK_FILENAME = ".economics.lock"
_COST_COMPONENTS = (
    "execution_cost",
    "verification_cost",
    "human_review_cost",
    "retry_escalation_cost",
)


def economics_directory() -> Path:
    configured = os.environ.get("VILLANI_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".villani"
    return home.resolve() / "economics"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def route_policy_from_configuration(
    configuration: Mapping[str, Any] | None,
) -> RoutePolicy:
    root = configuration or {}
    raw = _mapping(root.get("economics"))
    schema_version = raw.get("schema_version")
    if schema_version not in {None, ECONOMICS_CONFIGURATION_SCHEMA_VERSION}:
        raise ValueError(
            f"unsupported accepted-change economics configuration {schema_version!r}"
        )
    policy = dict(_mapping(raw.get("policy")))
    constraints = dict(_mapping(policy.get("constraints")))
    runtime_constraints = _mapping(raw.get("constraints"))
    constraints.update(runtime_constraints)
    policy["constraints"] = RouteConstraints.model_validate(constraints)
    return RoutePolicy.model_validate(policy)


def _read_observations(path: Path) -> tuple[EconomicsObservation, ...]:
    if not path.is_file():
        return ()
    raw = path.read_bytes()
    if raw and not raw.endswith((b"\n", b"\r")):
        raise ValueError(f"economics ledger has a truncated final record: {path}")
    records: list[EconomicsObservation] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(EconomicsObservation.model_validate_json(line))
        except Exception as error:
            raise ValueError(
                f"invalid economics ledger record at {path}:{line_number}: {error}"
            ) from error
    ids = [item.observation_id for item in records]
    if len(ids) != len(set(ids)):
        raise ValueError("economics ledger contains duplicate observation identities")
    return tuple(records)


@contextmanager
def _file_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / LOCK_FILENAME).open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - Linux CI
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        locked = True
        yield
    except OSError as error:
        raise ValueError("economics ledger is already being updated") from error
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - Linux CI
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            except OSError:
                pass
        handle.close()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction) + 0.999999) - 1))
    return ordered[index]


def _distribution(
    values: list[float],
    *,
    unknown_count: int,
    unit: str,
) -> QualificationDistribution:
    if not values:
        return QualificationDistribution(
            known_count=0,
            unknown_count=unknown_count,
            minimum=None,
            median=None,
            p90=None,
            maximum=None,
            unit=unit,
        )
    ordered = sorted(values)
    return QualificationDistribution(
        known_count=len(ordered),
        unknown_count=unknown_count,
        minimum=ordered[0],
        median=_percentile(ordered, 0.50),
        p90=_percentile(ordered, 0.90),
        maximum=ordered[-1],
        unit=unit,
    )


def _profile(rows: list[EconomicsObservation]) -> EconomicsProfile:
    first = rows[0]
    eligible = [item for item in rows if item.eligible_for_profile]
    exclusions = Counter(
        item.exclusion_reason or "unspecified"
        for item in rows
        if not item.eligible_for_profile
    )
    distributions: dict[str, dict[str, QualificationDistribution]] = {}
    unknown_counts: dict[str, int] = {}
    for component_name in _COST_COMPONENTS:
        by_currency: dict[str, list[float]] = defaultdict(list)
        unknown = 0
        for item in eligible:
            component: MoneyEstimate = getattr(item, component_name)
            if (
                component.accounting_status == "complete"
                and component.amount is not None
                and component.currency is not None
            ):
                by_currency[component.currency.upper()].append(component.amount)
            elif component.accounting_status != "not_applicable":
                unknown += 1
        distributions[component_name] = {
            currency: _distribution(values, unknown_count=unknown, unit=currency)
            for currency, values in sorted(by_currency.items())
        }
        unknown_counts[component_name] = unknown
    duration_values = [
        float(item.duration.duration_ms)
        for item in eligible
        if item.duration.accounting_status == "complete"
        and item.duration.duration_ms is not None
    ]
    review_values = [
        float(item.review_minutes)
        for item in eligible
        if item.review_minutes is not None
    ]
    attempt_values = [float(item.attempt_count) for item in eligible]
    escalation_values = [float(item.escalation_count) for item in eligible]
    successes = sum(
        bool(item.proved_acceptable and item.accepted_as_is is not False)
        for item in eligible
    )
    source = [
        item.model_dump(mode="json")
        for item in sorted(rows, key=lambda row: row.observation_id)
    ]
    return EconomicsProfile(
        key=EconomicsProfileKey(
            repository_id=first.repository_id,
            task_profile=first.task_profile,
            system_id=first.system_id,
            system_identity_digest=first.system_identity_digest,
            route_name=first.route_name,
        ),
        observation_ids=sorted(item.observation_id for item in rows),
        sample_count=len(eligible),
        successes=successes,
        failures=len(eligible) - successes,
        exclusions=dict(sorted(exclusions.items())),
        cost_distributions=distributions,
        cost_unknown_counts=unknown_counts,
        duration_distribution=_distribution(
            duration_values,
            unknown_count=len(eligible) - len(duration_values),
            unit="ms",
        ),
        review_minutes_distribution=_distribution(
            review_values,
            unknown_count=len(eligible) - len(review_values),
            unit="minutes",
        ),
        attempt_count_distribution=_distribution(
            attempt_values, unknown_count=0, unit="attempts"
        ),
        escalation_count_distribution=_distribution(
            escalation_values, unknown_count=0, unit="escalations"
        ),
        false_acceptance_count=sum(item.false_acceptance for item in rows),
        last_evidence_at=max((item.observed_at for item in eligible), default=None),
        source_digest=canonical_digest(source),
    )


def calculate_snapshot_digest(snapshot: EconomicsSnapshot) -> str:
    value = snapshot.model_dump(mode="json")
    value.pop("snapshot_digest", None)
    return canonical_digest(value)


class EconomicsStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else economics_directory()
        )
        self.observations_path = self.root / OBSERVATIONS_FILENAME
        self.snapshot_path = self.root / SNAPSHOT_FILENAME

    def load_observations(self) -> tuple[EconomicsObservation, ...]:
        return _read_observations(self.observations_path)

    def append_observation(self, observation: EconomicsObservation) -> bool:
        # Import lazily so the durable event writer can import schema validation
        # without creating an economics -> event writer -> schema cycle.
        from ..event_writer import redact_data

        value = observation.model_dump(mode="json")
        if redact_data(value) != value:
            raise ValueError("economics evidence contains a secret-shaped value")
        with _file_lock(self.root):
            existing = {item.observation_id: item for item in self.load_observations()}
            prior = existing.get(observation.observation_id)
            if prior is not None:
                if prior != observation:
                    raise ValueError("economics observation identity was reused")
                return False
            append_jsonl_durable(self.observations_path, observation)
        return True

    def rebuild(self, *, generated_at: datetime | None = None) -> EconomicsSnapshot:
        observations = self.load_observations()
        grouped: dict[
            tuple[str, str, str, str, str, str, str, tuple[str, ...]],
            list[EconomicsObservation],
        ] = defaultdict(list)
        for item in observations:
            profile = item.task_profile
            grouped[
                (
                    item.repository_id,
                    profile.category,
                    profile.difficulty,
                    profile.risk,
                    item.system_id,
                    item.system_identity_digest,
                    item.route_name,
                    tuple(profile.required_capabilities),
                )
            ].append(item)
        profiles = sorted(
            (_profile(rows) for rows in grouped.values()),
            key=lambda item: (
                item.key.repository_id,
                item.key.task_profile.category,
                item.key.task_profile.difficulty,
                item.key.task_profile.risk,
                item.key.system_identity_digest,
            ),
        )
        exclusions = Counter(
            item.exclusion_reason or "unspecified"
            for item in observations
            if not item.eligible_for_profile
        )
        now = generated_at or datetime.now(timezone.utc)
        source_digest = canonical_digest(
            [item.model_dump(mode="json") for item in observations]
        )
        provisional = EconomicsSnapshot(
            generated_at=now,
            source_digest=source_digest,
            snapshot_digest="sha256:" + "0" * 64,
            observation_count=len(observations),
            profiles=profiles,
            exclusions=dict(sorted(exclusions.items())),
        )
        snapshot = provisional.model_copy(
            update={"snapshot_digest": calculate_snapshot_digest(provisional)}
        )
        write_json_atomic(self.snapshot_path, snapshot)
        return snapshot

    def load_snapshot(self) -> EconomicsSnapshot | None:
        if not self.snapshot_path.is_file():
            return None
        return EconomicsSnapshot.model_validate_json(
            self.snapshot_path.read_text(encoding="utf-8")
        )

    def profile_for(
        self,
        *,
        repository_id: str,
        task_profile: QualificationTaskProfile,
        system_id: str,
    ) -> EconomicsProfile | None:
        snapshot = self.load_snapshot()
        if snapshot is None:
            snapshot = self.rebuild()
        return next(
            (
                item
                for item in snapshot.profiles
                if item.key.repository_id == repository_id
                and item.key.task_profile == task_profile
                and item.key.system_id == system_id
            ),
            None,
        )


__all__ = [
    "EconomicsStore",
    "calculate_snapshot_digest",
    "economics_directory",
    "route_policy_from_configuration",
]
