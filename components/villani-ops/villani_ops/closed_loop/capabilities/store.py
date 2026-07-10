"""Atomic versioned snapshot storage with append-only rebuild provenance."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..durable_io import append_jsonl_durable, write_json_atomic
from .ingest import SCORER_VERSION, calculate_profile_digest, rebuild_snapshot
from .models import CapabilitySnapshot, RebuildResult


SNAPSHOT_FILENAME = "profiles-v1.json"
PROVENANCE_FILENAME = "provenance.jsonl"


def capability_directory() -> Path:
    configured = os.environ.get("VILLANI_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".villani"
    return home.resolve() / "capabilities"


class CapabilityStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = (
            Path(root).expanduser().resolve() if root is not None else capability_directory()
        )
        self.snapshot_path = self.root / SNAPSHOT_FILENAME
        self.provenance_path = self.root / PROVENANCE_FILENAME

    def load(self) -> CapabilitySnapshot | None:
        if not self.snapshot_path.is_file():
            return None
        value = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        snapshot = CapabilitySnapshot.model_validate(value)
        if calculate_profile_digest(snapshot) != snapshot.profile_digest:
            raise ValueError("capability snapshot profile digest does not match its content")
        return snapshot

    def rebuild(
        self,
        runs_root: str | Path,
        *,
        scorer_version: str = SCORER_VERSION,
    ) -> RebuildResult:
        snapshot = rebuild_snapshot(runs_root, scorer_version=scorer_version)
        existing = self.load()
        if existing is not None and existing.profile_digest == snapshot.profile_digest:
            return RebuildResult(snapshot=existing, changed=False)
        self.root.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.snapshot_path, snapshot)
        append_jsonl_durable(
            self.provenance_path,
            {
                "schema_version": "villani.capability_provenance.v1",
                "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "snapshot_file": SNAPSHOT_FILENAME,
                "scorer_version": snapshot.scorer_version,
                "source_data_digest": snapshot.source_data_digest,
                "profile_digest": snapshot.profile_digest,
                "source_run_count": snapshot.source_run_count,
                "source_attempt_count": snapshot.source_attempt_count,
                "excluded_outcome_counts": snapshot.excluded_outcome_counts,
            },
        )
        return RebuildResult(snapshot=snapshot, changed=True)
