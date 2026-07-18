"""Append-only, optionally blinded human review ledger."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable

from villani_ops.closed_loop.durable_io import append_jsonl_durable

from .models import EvaluationTrial, HumanReview
from .workspace import contains_secret, load_suite, utc_now


def load_reviews(suite_directory: str | Path) -> tuple[HumanReview, ...]:
    path = Path(suite_directory).expanduser().resolve() / "human-reviews.jsonl"
    if not path.is_file():
        return ()
    reviews: list[HumanReview] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            reviews.append(HumanReview.model_validate_json(line))
        except Exception as error:
            raise ValueError(f"invalid append-only review at line {line_number}: {error}") from error
    identities = [item.review_id for item in reviews]
    if len(set(identities)) != len(identities):
        raise ValueError("append-only review ledger contains duplicate review identities")
    return tuple(reviews)


def latest_reviews(reviews: Iterable[HumanReview]) -> dict[str, HumanReview]:
    """Project the newest append for each trial without mutating history."""

    latest: dict[str, HumanReview] = {}
    seen: dict[str, HumanReview] = {}
    for review in reviews:
        if review.amends_review_id:
            prior = seen.get(review.amends_review_id)
            if prior is None or prior.trial_id != review.trial_id:
                raise ValueError("review amendment must name an earlier review for the same trial")
        seen[review.review_id] = review
        latest[review.trial_id] = review
    return latest


def append_review(
    suite_directory: str | Path,
    *,
    trial_id: str,
    reviewer_id: str,
    outcome: str,
    review_minutes: float,
    blinded: bool = True,
    arm_revealed_during_review: bool = False,
    correction_summary: str | None = None,
    severity: str = "none",
    later_rollback: bool | None = None,
    reopened_defect: bool | None = None,
    amends_review_id: str | None = None,
    artifact_references: tuple[str, ...] = (),
) -> HumanReview:
    root = Path(suite_directory).expanduser().resolve()
    load_suite(root)
    trial_path = root / "trials" / trial_id / "trial.json"
    if not trial_path.is_file():
        raise ValueError(f"trial not found: {trial_id}")
    trial = EvaluationTrial.model_validate_json(trial_path.read_text(encoding="utf-8"))
    if trial.status not in {"completed", "excluded"}:
        raise ValueError("review requires a terminal trial")
    prior_reviews = load_reviews(root)
    by_id = {item.review_id: item for item in prior_reviews}
    if amends_review_id:
        prior = by_id.get(amends_review_id)
        if prior is None or prior.trial_id != trial_id:
            raise ValueError("amendment must reference an existing review for this trial")
    review_text = "\n".join(
        value
        for value in (
            reviewer_id,
            correction_summary or "",
            *artifact_references,
        )
        if value
    )
    if contains_secret(review_text.encode("utf-8")):
        raise ValueError("possible secret in human review; use a redacted reference")
    correction_required = outcome == "accepted_after_correction"
    false_acceptance = bool(
        trial.proved_acceptable is True and outcome != "accepted_as_is"
    )
    false_rejection = bool(
        trial.proved_acceptable is False and outcome == "accepted_as_is"
    )
    now = utc_now()
    review_id = "review_" + hashlib.sha256(
        (
            f"{trial_id}\0{reviewer_id}\0{now.isoformat()}\0"
            + os.urandom(16).hex()
        ).encode()
    ).hexdigest()[:24]
    review = HumanReview(
        review_id=review_id,
        trial_id=trial_id,
        created_at=now,
        reviewer_id=reviewer_id,
        blinded=blinded,
        arm_revealed_during_review=arm_revealed_during_review,
        outcome=outcome,
        correction_required=correction_required,
        review_minutes=review_minutes,
        correction_summary=correction_summary,
        severity=severity,
        false_acceptance=false_acceptance,
        false_rejection=false_rejection,
        later_rollback=later_rollback,
        reopened_defect=reopened_defect,
        amends_review_id=amends_review_id,
        artifact_references=list(artifact_references),
    )
    append_jsonl_durable(
        root / "human-reviews.jsonl", review.model_dump(mode="json")
    )
    # Parse the complete ledger after append.  A failed lineage or duplicate
    # check is surfaced immediately; no prior record is ever rewritten.
    latest_reviews(load_reviews(root))
    return review
