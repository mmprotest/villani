"""Fail-closed deterministic route-policy publication and instant rollback."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..durable_io import append_jsonl_durable, write_json_atomic
from .models import (
    RoutePolicy,
    RoutePolicyEvaluation,
    RoutePolicyPublication,
    canonical_digest,
)


PUBLICATIONS_DIR = "publications"
ACTIVE_FILE = "active.json"
HISTORY_FILE = "history.jsonl"
LOCK_FILE = ".route-policy.lock"


def policy_directory() -> Path:
    configured = os.environ.get("VILLANI_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".villani"
    return home.resolve() / "route-policies"


@contextmanager
def _publication_lock(root: Path) -> Iterator[None]:
    """Serialize pointer and immutable-publication updates across processes."""

    root.mkdir(parents=True, exist_ok=True)
    handle = (root / LOCK_FILE).open("a+b")
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
        raise ValueError("route-policy publication is already being updated") from error
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


class RoutePolicyStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else policy_directory()
        )
        self.publications_root = self.root / PUBLICATIONS_DIR
        self.active_path = self.root / ACTIVE_FILE
        self.history_path = self.root / HISTORY_FILE

    def _publication_path(self, version: str) -> Path:
        if not version or Path(version).name != version:
            raise ValueError("policy version must be a safe single path component")
        return self.publications_root / f"{version}.json"

    def load_publication(self, version: str) -> RoutePolicyPublication | None:
        path = self._publication_path(version)
        if not path.is_file():
            return None
        return RoutePolicyPublication.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def active_publication(self) -> RoutePolicyPublication | None:
        if not self.active_path.is_file():
            return None
        value = json.loads(self.active_path.read_text(encoding="utf-8"))
        version = value.get("policy_version") if isinstance(value, dict) else None
        if not isinstance(version, str):
            raise ValueError("active route policy pointer is malformed")
        publication = self.load_publication(version)
        if publication is None:
            raise ValueError("active route policy publication is missing")
        return publication

    def active_policy(self, fallback: RoutePolicy) -> RoutePolicy:
        publication = self.active_publication()
        return publication.policy if publication is not None else fallback

    def publish(
        self,
        policy: RoutePolicy,
        evaluation: RoutePolicyEvaluation,
        *,
        published_at: datetime | None = None,
    ) -> RoutePolicyPublication:
        with _publication_lock(self.root):
            digest = canonical_digest(policy.model_dump(mode="json"))
            if (
                evaluation.proposed_policy_version != policy.policy_version
                or evaluation.proposed_policy_digest != digest
            ):
                raise ValueError(
                    "evaluation does not address the exact proposed policy"
                )
            if not evaluation.point_in_time_replay or not evaluation.safe_to_publish:
                raise ValueError(
                    "route policy publication refused by point-in-time safety evaluation"
                )
            if not evaluation.conservative_reliability_non_decreasing:
                raise ValueError(
                    "route policy publication would reduce conservative reliability"
                )
            if not evaluation.false_acceptance_exposure_non_increasing:
                raise ValueError(
                    "route policy publication would increase false-acceptance exposure"
                )
            existing = self.load_publication(policy.policy_version)
            if existing is not None:
                if existing.policy_digest != digest:
                    raise ValueError("published policy version is immutable")
                return existing
            prior = self.active_publication()
            now = published_at or datetime.now(timezone.utc)
            content: dict[str, Any] = {
                "published_at": now.isoformat(),
                "policy_digest": digest,
                "evaluation_id": evaluation.evaluation_id,
                "evaluation_digest": canonical_digest(
                    evaluation.model_dump(mode="json")
                ),
                "prior_policy_version": prior.policy.policy_version if prior else None,
            }
            publication = RoutePolicyPublication(
                publication_id="rpub_"
                + canonical_digest(content).removeprefix("sha256:"),
                published_at=now,
                policy=policy,
                policy_digest=digest,
                evaluation_id=evaluation.evaluation_id,
                evaluation_digest=canonical_digest(evaluation.model_dump(mode="json")),
                prior_policy_version=prior.policy.policy_version if prior else None,
                state="active",
            )
            self.publications_root.mkdir(parents=True, exist_ok=True)
            write_json_atomic(
                self._publication_path(policy.policy_version), publication
            )
            write_json_atomic(
                self.active_path,
                {
                    "schema_version": "villani.route_policy_pointer.v1",
                    "policy_version": policy.policy_version,
                    "publication_id": publication.publication_id,
                    "policy_digest": digest,
                },
            )
            append_jsonl_durable(
                self.history_path,
                {
                    "action": "publish",
                    "policy_version": policy.policy_version,
                    "publication_id": publication.publication_id,
                    "at": now.isoformat().replace("+00:00", "Z"),
                },
            )
            return publication

    def rollback(
        self,
        *,
        target_version: str | None = None,
        rolled_back_at: datetime | None = None,
    ) -> RoutePolicyPublication:
        with _publication_lock(self.root):
            active = self.active_publication()
            if active is None:
                raise ValueError("no published route policy is active")
            target = target_version or active.prior_policy_version
            if not target:
                raise ValueError("active route policy has no prior publication")
            publication = self.load_publication(target)
            if publication is None:
                raise ValueError(f"rollback target {target!r} is not published")
            now = rolled_back_at or datetime.now(timezone.utc)
            write_json_atomic(
                self.active_path,
                {
                    "schema_version": "villani.route_policy_pointer.v1",
                    "policy_version": publication.policy.policy_version,
                    "publication_id": publication.publication_id,
                    "policy_digest": publication.policy_digest,
                },
            )
            append_jsonl_durable(
                self.history_path,
                {
                    "action": "rollback",
                    "from_policy_version": active.policy.policy_version,
                    "policy_version": publication.policy.policy_version,
                    "publication_id": publication.publication_id,
                    "at": now.isoformat().replace("+00:00", "Z"),
                },
            )
            return publication


__all__ = ["RoutePolicyStore", "policy_directory"]
