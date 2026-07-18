"""Append-only qualification evidence and deterministic derived snapshots."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, TypeVar

from pydantic import BaseModel

from ..durable_io import append_jsonl_durable, write_json_atomic
from .models import (
    QualificationInvalidation,
    QUALIFICATION_CONFIGURATION_SCHEMA_VERSION,
    QualificationMigration,
    QualificationObservation,
    QualificationPolicy,
    QualificationProfile,
    QualificationProfileKey,
    QualificationSnapshot,
)
from .repository import canonical_digest
from .scoring import active_observations, qualification_statistics


OBSERVATIONS_FILENAME = "observations.jsonl"
INVALIDATIONS_FILENAME = "invalidations.jsonl"
SNAPSHOT_FILENAME = "snapshot-v1.json"
LOCK_FILENAME = ".qualification.lock"
TModel = TypeVar("TModel", bound=BaseModel)


def qualification_directory() -> Path:
    configured = os.environ.get("VILLANI_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".villani"
    return home.resolve() / "qualification"


def qualification_policy_from_configuration(
    configuration: Mapping[str, Any] | None,
) -> QualificationPolicy:
    root = configuration or {}
    raw = root.get("qualification")
    values = dict(raw) if isinstance(raw, Mapping) else {}
    schema_version = values.get("schema_version")
    if schema_version not in {None, QUALIFICATION_CONFIGURATION_SCHEMA_VERSION}:
        raise ValueError(
            f"unsupported repository qualification configuration {schema_version!r}"
        )
    policy_raw = values.get("policy")
    policy = dict(policy_raw) if isinstance(policy_raw, Mapping) else {}
    return QualificationPolicy.model_validate(policy)


def _read_jsonl(path: Path, model: type[TModel]) -> list[TModel]:
    if not path.is_file():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith((b"\n", b"\r")):
        raise ValueError(f"qualification ledger has a truncated final record: {path}")
    records: list[TModel] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except Exception as error:
            raise ValueError(
                f"invalid qualification ledger record at {path}:{line_number}: {error}"
            ) from error
    identities = [
        str(
            getattr(record, "observation_id", None)
            or getattr(record, "invalidation_id", None)
        )
        for record in records
    ]
    if len(identities) != len(set(identities)):
        raise ValueError(f"qualification ledger contains duplicate identities: {path}")
    return records


@contextmanager
def _file_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK_FILENAME
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ValueError(
                    "qualification ledger is already being updated"
                ) from error
        else:  # pragma: no cover - exercised by Linux CI
            import fcntl

            try:
                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
            except OSError as error:
                raise ValueError(
                    "qualification ledger is already being updated"
                ) from error
        locked = True
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - exercised by Linux CI
                    import fcntl

                    fcntl.flock(  # type: ignore[attr-defined]
                        handle.fileno(),
                        fcntl.LOCK_UN,  # type: ignore[attr-defined]
                    )
            except OSError:
                pass
        handle.close()


def _legacy_migration(root: Path) -> QualificationMigration:
    capability = root.parent / "capabilities" / "profiles-v1.json"
    if not capability.is_file():
        return QualificationMigration(
            migration_id="legacy_capability_snapshot_v1",
            source="capabilities/profiles-v1.json",
            source_digest=None,
            status="not_present",
            exclusion_reason=None,
        )
    try:
        value = json.loads(capability.read_text(encoding="utf-8"))
        digest = str(value.get("profile_digest") or "")
        source_digest = digest if digest else canonical_digest(value)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        source_digest = canonical_digest({"status": "unreadable"})
    return QualificationMigration(
        migration_id="legacy_capability_snapshot_v1",
        source="capabilities/profiles-v1.json",
        source_digest=source_digest,
        status="excluded",
        exclusion_reason=(
            "legacy capability profiles lack repository lineage, complete agent-system "
            "identity, execution fingerprint, and required human review"
        ),
    )


def calculate_snapshot_digest(snapshot: QualificationSnapshot) -> str:
    value = snapshot.model_dump(mode="json")
    value.pop("snapshot_digest", None)
    return canonical_digest(value)


def _reject_sensitive_values(record: BaseModel) -> None:
    # Import lazily because event_writer depends on run_store/schema_validation,
    # which validates the qualification models during controller startup.
    from ..event_writer import redact_data

    value = record.model_dump(mode="json")
    if redact_data(value) != value:
        raise ValueError(
            "qualification evidence contains a secret-shaped or registered sensitive value"
        )


class QualificationStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else qualification_directory()
        )
        self.observations_path = self.root / OBSERVATIONS_FILENAME
        self.invalidations_path = self.root / INVALIDATIONS_FILENAME
        self.snapshot_path = self.root / SNAPSHOT_FILENAME

    def load_observations(self) -> tuple[QualificationObservation, ...]:
        return tuple(_read_jsonl(self.observations_path, QualificationObservation))

    def load_invalidations(self) -> tuple[QualificationInvalidation, ...]:
        return tuple(_read_jsonl(self.invalidations_path, QualificationInvalidation))

    def append_observation(self, observation: QualificationObservation) -> bool:
        _reject_sensitive_values(observation)
        with _file_lock(self.root):
            existing = self.load_observations()
            by_id = {item.observation_id: item for item in existing}
            prior = by_id.get(observation.observation_id)
            if prior is not None:
                if prior != observation:
                    raise ValueError(
                        "qualification observation identity was reused with different content"
                    )
                return False
            append_jsonl_durable(self.observations_path, observation)
        return True

    def append_invalidation(self, invalidation: QualificationInvalidation) -> bool:
        _reject_sensitive_values(invalidation)
        with _file_lock(self.root):
            existing = self.load_invalidations()
            by_id = {item.invalidation_id: item for item in existing}
            prior = by_id.get(invalidation.invalidation_id)
            if prior is not None:
                if prior != invalidation:
                    raise ValueError(
                        "qualification invalidation identity was reused with different content"
                    )
                return False
            append_jsonl_durable(self.invalidations_path, invalidation)
        return True

    def load_snapshot(self) -> QualificationSnapshot | None:
        if not self.snapshot_path.is_file():
            return None
        snapshot = QualificationSnapshot.model_validate_json(
            self.snapshot_path.read_text(encoding="utf-8")
        )
        if calculate_snapshot_digest(snapshot) != snapshot.snapshot_digest:
            raise ValueError("qualification snapshot digest does not match its content")
        return snapshot

    def rebuild(
        self,
        *,
        policy: QualificationPolicy | None = None,
        generated_at: datetime | None = None,
    ) -> QualificationSnapshot:
        selected_policy = policy or QualificationPolicy()
        observations = list(self.load_observations())
        invalidations = list(self.load_invalidations())
        active, superseded = active_observations(observations)
        groups: dict[tuple[str, ...], list[QualificationObservation]] = defaultdict(
            list
        )
        keys: dict[tuple[str, ...], QualificationProfileKey] = {}
        exclusions: Counter[str] = Counter()
        for observation in active:
            if not observation.eligible:
                exclusions[observation.exclusion_reason or "unspecified_exclusion"] += 1
            key = QualificationProfileKey(
                repository_id=observation.repository_id,
                task_profile=observation.task_profile,
                system_identity_digest=observation.system.identity_digest,
                execution_environment_fingerprint=(
                    observation.system.execution_environment_fingerprint
                ),
                verification_policy_version=(
                    observation.system.verification_policy_version
                ),
            )
            sort_key = (
                key.repository_id,
                key.task_profile.category,
                key.task_profile.difficulty,
                key.task_profile.risk,
                "\0".join(key.task_profile.required_capabilities),
                key.system_identity_digest,
                key.execution_environment_fingerprint,
                key.verification_policy_version,
            )
            keys[sort_key] = key
            groups[sort_key].append(observation)
        profiles: list[QualificationProfile] = []
        for profile_sort_key in sorted(groups):
            rows = sorted(
                groups[profile_sort_key], key=lambda item: item.observation_id
            )
            source = canonical_digest([item.model_dump(mode="json") for item in rows])
            profiles.append(
                QualificationProfile(
                    key=keys[profile_sort_key],
                    observation_ids=[item.observation_id for item in rows],
                    statistics=qualification_statistics(
                        rows, wilson_z=selected_policy.wilson_z
                    ),
                    source_digest=source,
                )
            )
        migration = _legacy_migration(self.root)
        source_digest = canonical_digest(
            {
                "policy": selected_policy.model_dump(mode="json"),
                "observations": [
                    item.model_dump(mode="json")
                    for item in sorted(observations, key=lambda row: row.observation_id)
                ],
                "invalidations": [
                    item.model_dump(mode="json")
                    for item in sorted(
                        invalidations, key=lambda row: row.invalidation_id
                    )
                ],
                "migration": migration.model_dump(mode="json"),
            }
        )
        existing = self.load_snapshot()
        if (
            existing is not None
            and existing.source_digest == source_digest
            and existing.policy == selected_policy
        ):
            return existing
        provisional = {
            "schema_version": "villani.qualification_snapshot.v1",
            "generated_at": generated_at or datetime.now(timezone.utc),
            "policy": selected_policy,
            "source_digest": source_digest,
            "observation_count": len(active),
            "invalidation_count": len(invalidations),
            "superseded_observation_count": superseded,
            "profiles": profiles,
            "exclusions": dict(sorted(exclusions.items())),
            "migrations": [migration],
        }
        placeholder = QualificationSnapshot(
            **provisional,
            snapshot_digest=f"sha256:{'0' * 64}",
        )
        snapshot = QualificationSnapshot(
            **provisional,
            snapshot_digest=calculate_snapshot_digest(placeholder),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.snapshot_path, snapshot)
        return snapshot


__all__ = [
    "INVALIDATIONS_FILENAME",
    "OBSERVATIONS_FILENAME",
    "SNAPSHOT_FILENAME",
    "QualificationStore",
    "calculate_snapshot_digest",
    "qualification_directory",
    "qualification_policy_from_configuration",
]
