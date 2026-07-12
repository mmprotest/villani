"""Ingest trustworthy outcomes from canonical local run bundles.

M8 denominator rules (these are intentionally explicit and mirrored by tests):

* A success is included only when its verification is acceptance eligible and
  either (a) that exact selected patch was materialized successfully, or (b)
  the attempt has the explicit ``accepted_not_selected`` capability label and
  the run policy collected multiple accepted candidates.
* A failure enters the model-capability denominator only after completed
  normalized verification classifies it as ``implementation_failure``,
  ``capability_failure``, or ``no_change_failure``.
* Infrastructure failures, verifier failures/errors, corrupt bundles,
  interrupted attempts, unknown outcomes, and materialization failures are
  excluded from the denominator and counted by their exclusion reason.
* Human/manual modifications are never mixed with clean model attempts.  They
  are excluded even when explicitly labelled, with that label retained only
  as provenance for a separately addressable future population.
* Observations are deduplicated by ``(run_id, attempt_id, scorer_version)``.

Aggregation is implemented only after these rules have executable coverage.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean, median
from typing import Any, Iterable, Mapping, TypeVar

from pydantic import BaseModel

from ..protocol import (
    AttemptSnapshot,
    ClassificationSnapshot,
    MaterializationSnapshot,
    RunManifestSnapshot,
    SelectionSnapshot,
    VerificationSnapshot,
)
from .models import (
    CapabilityProfile,
    CapabilitySnapshot,
    IncludedAttempt,
    ProfileKey,
)
from .scoring import wilson_lower_bound


SCORER_VERSION = "empirical_wilson_v1"
MODEL_FAILURE_CATEGORIES = frozenset(
    {"implementation_failure", "capability_failure", "no_change_failure"}
)
EPOCH = "1970-01-01T00:00:00Z"
TModel = TypeVar("TModel", bound=BaseModel)


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def calculate_profile_digest(snapshot: CapabilitySnapshot) -> str:
    """Recalculate the self-authenticating digest of a stored snapshot."""

    value = snapshot.model_dump(mode="json")
    value.pop("profile_digest", None)
    return _canonical_digest(value)


def _load_model(path: Path, model: type[TModel]) -> TModel:
    value = json.loads(path.read_text(encoding="utf-8"))
    return model.model_validate(value)


def _optional_model(path: Path, model: type[TModel]) -> TModel | None:
    return _load_model(path, model) if path.is_file() else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _provider(attempt: AttemptSnapshot, manifest: RunManifestSnapshot) -> str | None:
    explicit = attempt.metadata.get("provider")
    if isinstance(explicit, str) and explicit:
        return explicit
    configuration = _mapping(manifest.metadata.get("policy_configuration"))
    backends = configuration.get("backends")
    if isinstance(backends, Mapping):
        backend = _mapping(backends.get(attempt.backend_name))
        value = backend.get("provider")
        return str(value) if isinstance(value, str) and value else None
    if isinstance(backends, list):
        for value in backends:
            backend = _mapping(value)
            if backend.get("name") == attempt.backend_name:
                provider = backend.get("provider")
                return str(provider) if isinstance(provider, str) and provider else None
    return None


def _accepted_candidates_required(manifest: RunManifestSnapshot) -> int:
    configuration = _mapping(manifest.metadata.get("policy_configuration"))
    policy = _mapping(configuration.get("policy"))
    value = policy.get("accepted_candidates_required", 1)
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return 1


def _human_modified(
    attempt: AttemptSnapshot, verification: VerificationSnapshot | None
) -> bool:
    metadata: dict[str, Any] = dict(attempt.metadata)
    if verification is not None:
        metadata.update(verification.metadata)
    true_flags = (
        "human_modified",
        "manually_edited",
        "manual_edit",
        "human_intervention",
    )
    if any(metadata.get(key) is True for key in true_flags):
        return True
    provenance = str(metadata.get("provenance") or "").lower()
    return provenance in {"human", "human_modified", "manual", "manually_edited"}


def _outcome_label(
    attempt: AttemptSnapshot, verification: VerificationSnapshot | None
) -> str | None:
    for metadata in (
        verification.metadata if verification is not None else {},
        attempt.metadata,
    ):
        value = metadata.get("capability_outcome_label")
        if isinstance(value, str) and value:
            return value
    return None


def _failure_category(
    attempt: AttemptSnapshot, verification: VerificationSnapshot | None
) -> str | None:
    for metadata in (
        verification.metadata if verification is not None else {},
        attempt.metadata,
    ):
        value = metadata.get("failure_category")
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True, slots=True)
class _Outcome:
    key: ProfileKey
    run_id: str
    attempt_id: str
    observed_at: str
    included_outcome: str | None
    exclusion_reason: str | None
    actual_cost: float | None
    duration_ms: float | None
    input_tokens: float | None
    output_tokens: float | None
    source_digest: str

    @property
    def deduplication_key(self) -> tuple[str, str, str]:
        return (self.run_id, self.attempt_id, self.key.scorer_version)


def _classification_version(classification: ClassificationSnapshot) -> str:
    explicit = classification.metadata.get("classifier_version")
    return (
        explicit
        if isinstance(explicit, str) and explicit
        else classification.schema_version
    )


def _verifier_version(verification: VerificationSnapshot | None) -> str:
    if verification is None:
        return "unknown"
    explicit = verification.metadata.get("verifier_version")
    return explicit if isinstance(explicit, str) and explicit else verification.verifier


def _selected_and_materialized(
    attempt: AttemptSnapshot,
    selection: SelectionSnapshot | None,
    materialization: MaterializationSnapshot | None,
) -> bool:
    return bool(
        selection is not None
        and attempt.attempt_id in selection.selected_candidate_ids
        and materialization is not None
        and materialization.status == "succeeded"
        and materialization.selected_attempt_id == attempt.attempt_id
        and materialization.source_patch_path == attempt.patch_path
        and materialization.patch_sha256 == attempt.patch_sha256
    )


def _make_outcome(
    *,
    attempt: AttemptSnapshot,
    classification: ClassificationSnapshot,
    manifest: RunManifestSnapshot,
    verification: VerificationSnapshot | None,
    selection: SelectionSnapshot | None,
    materialization: MaterializationSnapshot | None,
    scorer_version: str,
) -> _Outcome:
    provider = _provider(attempt, manifest)
    key = ProfileKey(
        backend_name=attempt.backend_name,
        provider=provider or "unknown",
        model=attempt.model or "unknown",
        task_category=classification.category,
        difficulty=classification.difficulty,
        risk=classification.risk,
        classifier_version=_classification_version(classification),
        verifier_version=_verifier_version(verification),
        scorer_version=scorer_version,
    )
    category = _failure_category(attempt, verification)
    included: str | None = None
    excluded: str | None = None

    if provider is None or attempt.model is None:
        excluded = "missing_backend_identity"
    elif _human_modified(attempt, verification):
        excluded = "human_modified"
    elif attempt.status in {"pending", "running", "cancelled"}:
        excluded = "interrupted_attempt"
    elif category == "infrastructure_failure":
        excluded = "infrastructure_failure"
    elif category == "verification_failure" or (
        verification is not None and verification.outcome == "error"
    ):
        excluded = "verification_failure"
    elif category == "materialization_failure":
        excluded = "materialization_failure"
    elif verification is None:
        excluded = "unknown_outcome"
    elif verification.outcome == "accepted" and verification.acceptance_eligible:
        if _selected_and_materialized(attempt, selection, materialization):
            included = "success"
        elif (
            _outcome_label(attempt, verification) == "accepted_not_selected"
            and _accepted_candidates_required(manifest) > 1
            and (
                selection is None
                or attempt.attempt_id not in selection.selected_candidate_ids
            )
        ):
            included = "success"
        elif (
            selection is not None
            and attempt.attempt_id in selection.selected_candidate_ids
        ):
            excluded = "materialization_failure"
        else:
            excluded = "accepted_not_selected_untrusted"
    elif verification.outcome == "rejected" and category in MODEL_FAILURE_CATEGORIES:
        included = "verified_model_failure"
    elif verification.outcome == "unclear":
        excluded = "unknown_outcome"
    else:
        excluded = "unknown_outcome"

    observed_at = (
        verification.model_dump(mode="json")["verified_at"]
        if verification is not None
        else attempt.model_dump(mode="json").get("completed_at")
        or attempt.model_dump(mode="json").get("started_at")
        or classification.model_dump(mode="json")["classified_at"]
    )
    actual_cost = (
        float(attempt.cost_usd)
        if attempt.cost_accounting_status == "complete" and attempt.cost_usd is not None
        else None
    )
    duration = (
        float(attempt.duration_ms)
        if attempt.duration_accounting_status == "complete"
        and attempt.duration_ms is not None
        else None
    )
    tokens_known = attempt.token_accounting_status == "complete"
    payload = {
        "key": key.model_dump(mode="json"),
        "run_id": attempt.run_id,
        "attempt_id": attempt.attempt_id,
        "observed_at": observed_at,
        "included_outcome": included,
        "exclusion_reason": excluded,
        "actual_cost": actual_cost,
        "duration_ms": duration,
        "input_tokens": attempt.input_tokens if tokens_known else None,
        "output_tokens": attempt.output_tokens if tokens_known else None,
    }
    return _Outcome(
        key=key,
        run_id=attempt.run_id,
        attempt_id=attempt.attempt_id,
        observed_at=str(observed_at),
        included_outcome=included,
        exclusion_reason=excluded,
        actual_cost=actual_cost,
        duration_ms=duration,
        input_tokens=(
            float(attempt.input_tokens)
            if tokens_known and attempt.input_tokens is not None
            else None
        ),
        output_tokens=(
            float(attempt.output_tokens)
            if tokens_known and attempt.output_tokens is not None
            else None
        ),
        source_digest=_canonical_digest(payload),
    )


def _directory_digest(directory: Path) -> str:
    entries: list[dict[str, str]] = []
    try:
        files = sorted(path for path in directory.rglob("*") if path.is_file())
    except OSError:
        files = []
    for path in files:
        if path.suffix not in {".json", ".jsonl"}:
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "unreadable"
        entries.append(
            {"relative_path": path.relative_to(directory).as_posix(), "digest": digest}
        )
    return _canonical_digest(entries)


def _read_directory(
    directory: Path, scorer_version: str
) -> tuple[str | None, list[_Outcome], list[str]]:
    corrupt: list[str] = []
    try:
        manifest = _load_model(directory / "manifest.json", RunManifestSnapshot)
        classification = _load_model(
            directory / "classification.json", ClassificationSnapshot
        )
        if manifest.run_id != classification.run_id:
            raise ValueError("manifest and classification run IDs differ")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None, [], [_directory_digest(directory)]

    try:
        selection = _optional_model(directory / "selection.json", SelectionSnapshot)
        materialization = _optional_model(
            directory / "materialization.json", MaterializationSnapshot
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        selection = None
        materialization = None
        corrupt.append(_directory_digest(directory))

    outcomes: list[_Outcome] = []
    for attempt_id in sorted(set(manifest.attempt_ids)):
        try:
            attempt = _load_model(
                directory / "attempts" / attempt_id / "attempt.json",
                AttemptSnapshot,
            )
            verification = _optional_model(
                directory / "verification" / f"{attempt_id}.json",
                VerificationSnapshot,
            )
            if attempt.run_id != manifest.run_id or attempt.attempt_id != attempt_id:
                raise ValueError("attempt identity does not match manifest")
            if verification is not None and (
                verification.run_id != manifest.run_id
                or verification.attempt_id != attempt_id
            ):
                raise ValueError("verification identity does not match attempt")
            outcomes.append(
                _make_outcome(
                    attempt=attempt,
                    classification=classification,
                    manifest=manifest,
                    verification=verification,
                    selection=selection,
                    materialization=materialization,
                    scorer_version=scorer_version,
                )
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            corrupt.append(_directory_digest(directory))
    return manifest.run_id, outcomes, corrupt


@dataclass(slots=True)
class _Accumulator:
    key: ProfileKey
    included: list[IncludedAttempt] = field(default_factory=list)
    successes: int = 0
    failures: int = 0
    exclusions: Counter[str] = field(default_factory=Counter)
    costs: list[float] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    input_tokens: list[float] = field(default_factory=list)
    output_tokens: list[float] = field(default_factory=list)
    timestamps: list[str] = field(default_factory=list)
    source_digests: list[str] = field(default_factory=list)

    def add(self, outcome: _Outcome, *, forced_exclusion: str | None = None) -> None:
        self.timestamps.append(outcome.observed_at)
        self.source_digests.append(outcome.source_digest)
        exclusion = forced_exclusion or outcome.exclusion_reason
        if exclusion:
            self.exclusions[exclusion] += 1
            return
        if outcome.included_outcome is None:
            self.exclusions["unknown_outcome"] += 1
            return
        self.included.append(
            IncludedAttempt(
                run_id=outcome.run_id,
                attempt_id=outcome.attempt_id,
                outcome=outcome.included_outcome,  # type: ignore[arg-type]
            )
        )
        if outcome.included_outcome == "success":
            self.successes += 1
        else:
            self.failures += 1
        if outcome.actual_cost is not None:
            self.costs.append(outcome.actual_cost)
        if outcome.duration_ms is not None:
            self.durations.append(outcome.duration_ms)
        if outcome.input_tokens is not None:
            self.input_tokens.append(outcome.input_tokens)
        if outcome.output_tokens is not None:
            self.output_tokens.append(outcome.output_tokens)


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def _profile(accumulator: _Accumulator) -> CapabilityProfile:
    sample_count = accumulator.successes + accumulator.failures
    source_digest = _canonical_digest(
        {
            "key": accumulator.key.model_dump(mode="json"),
            "outcomes": sorted(accumulator.source_digests),
            "exclusions": dict(sorted(accumulator.exclusions.items())),
        }
    )
    return CapabilityProfile(
        key=accumulator.key,
        included_attempts=sorted(
            accumulator.included,
            key=lambda item: (item.run_id, item.attempt_id, item.outcome),
        ),
        successes=accumulator.successes,
        verified_model_failures=accumulator.failures,
        sample_count=sample_count,
        raw_success_rate=(
            accumulator.successes / sample_count if sample_count else 0.0
        ),
        wilson_lower_bound=wilson_lower_bound(accumulator.successes, sample_count),
        mean_actual_attempt_cost=_mean(accumulator.costs),
        median_actual_attempt_cost=_median(accumulator.costs),
        mean_duration_ms=_mean(accumulator.durations),
        median_duration_ms=_median(accumulator.durations),
        mean_input_tokens=_mean(accumulator.input_tokens),
        mean_output_tokens=_mean(accumulator.output_tokens),
        excluded_outcome_counts=dict(sorted(accumulator.exclusions.items())),
        first_observed_at=min(accumulator.timestamps)
        if accumulator.timestamps
        else None,
        last_observed_at=max(accumulator.timestamps)
        if accumulator.timestamps
        else None,
        source_data_digest=source_digest,
    )


def _aggregate_keys(key: ProfileKey) -> Iterable[ProfileKey]:
    yielded: set[tuple[str, ...]] = set()
    for _, candidate in key.backoff_keys():
        identity = candidate.sort_key()
        if identity not in yielded:
            yielded.add(identity)
            yield candidate


def rebuild_snapshot(
    runs_root: str | Path,
    *,
    scorer_version: str = SCORER_VERSION,
) -> CapabilitySnapshot:
    """Build a deterministic snapshot from immediate canonical run directories."""

    root = Path(runs_root).expanduser().resolve()
    directories = (
        sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
        if root.is_dir()
        else []
    )
    outcomes: list[_Outcome] = []
    corrupt_digests: list[str] = []
    run_ids: set[str] = set()
    for directory in directories:
        run_id, directory_outcomes, corrupt = _read_directory(directory, scorer_version)
        if run_id:
            run_ids.add(run_id)
        outcomes.extend(directory_outcomes)
        corrupt_digests.extend(corrupt)

    by_identity: dict[tuple[str, str, str], list[_Outcome]] = defaultdict(list)
    for outcome in outcomes:
        by_identity[outcome.deduplication_key].append(outcome)

    unique: list[_Outcome] = []
    duplicates: list[_Outcome] = []
    for identity in sorted(by_identity):
        ordered = sorted(by_identity[identity], key=lambda item: item.source_digest)
        unique.append(ordered[0])
        duplicates.extend(ordered[1:])

    accumulators: dict[tuple[str, ...], _Accumulator] = {}

    def add(outcome: _Outcome, forced_exclusion: str | None = None) -> None:
        for key in _aggregate_keys(outcome.key):
            accumulator = accumulators.setdefault(key.sort_key(), _Accumulator(key=key))
            accumulator.add(outcome, forced_exclusion=forced_exclusion)

    global_exclusions: Counter[str] = Counter()
    for outcome in unique:
        add(outcome)
        if outcome.exclusion_reason:
            global_exclusions[outcome.exclusion_reason] += 1
    for outcome in duplicates:
        add(outcome, forced_exclusion="duplicate_attempt")
        global_exclusions["duplicate_attempt"] += 1
    if corrupt_digests:
        global_exclusions["corrupt_bundle"] += len(corrupt_digests)

    profiles = [_profile(accumulators[key]) for key in sorted(accumulators)]
    source_rows = [
        {
            "deduplication_key": list(outcome.deduplication_key),
            "source_digest": outcome.source_digest,
            "exclusion_reason": outcome.exclusion_reason,
        }
        for outcome in sorted(
            unique, key=lambda item: (item.deduplication_key, item.source_digest)
        )
    ]
    source_rows.extend(
        {
            "deduplication_key": list(outcome.deduplication_key),
            "source_digest": outcome.source_digest,
            "exclusion_reason": "duplicate_attempt",
        }
        for outcome in sorted(
            duplicates, key=lambda item: (item.deduplication_key, item.source_digest)
        )
    )
    source_digest = _canonical_digest(
        {
            "scorer_version": scorer_version,
            "outcomes": source_rows,
            "corrupt_bundle_digests": sorted(corrupt_digests),
        }
    )
    generated_at = max((outcome.observed_at for outcome in unique), default=EPOCH)
    provisional = {
        "schema_version": "villani.capability_snapshot.v1",
        "scorer_version": scorer_version,
        "source_data_digest": source_digest,
        "generated_at": generated_at,
        "profiles": [profile.model_dump(mode="json") for profile in profiles],
        "excluded_outcome_counts": dict(sorted(global_exclusions.items())),
        "source_run_count": len(run_ids),
        "source_attempt_count": len(unique),
    }
    profile_digest = _canonical_digest(provisional)
    return CapabilitySnapshot(
        **provisional,
        profile_digest=profile_digest,
    )
