"""Bounded retention cleanup that is a dry run unless explicitly applied."""

from __future__ import annotations

import os
import shutil
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CleanupItem:
    category: str
    path: str
    age_days: float
    size_bytes: int


@dataclass(frozen=True, slots=True)
class CleanupReport:
    schema_version: str
    applied: bool
    items: tuple[CleanupItem, ...]
    reclaimed_bytes: int
    runs_deleted: int = 0
    repositories_modified: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "items": [asdict(item) for item in self.items],
        }


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _candidates(home: Path, *, now: float) -> list[CleanupItem]:
    policies = (
        (home / "update-transactions", "abandoned_update_transaction", 7),
        (home / "downloads", "verified_update_download", 30),
        (home / "update-failures", "failed_update_artifact", 30),
    )
    items: list[CleanupItem] = []
    for root, category, retention_days in policies:
        if not root.is_dir():
            continue
        for path in root.iterdir():
            try:
                age_days = (now - path.stat().st_mtime) / (24 * 60 * 60)
            except OSError:
                continue
            if age_days >= retention_days:
                items.append(
                    CleanupItem(category, str(path), age_days, _size(path))
                )
    log = home / "agentd" / "agentd.log"
    for index in range(4, 100):
        path = log.with_name(f"{log.name}.{index}")
        if path.is_file():
            age_days = (now - path.stat().st_mtime) / (24 * 60 * 60)
            items.append(CleanupItem("excess_log_backup", str(path), age_days, _size(path)))
    runners = home / "runners"
    manifest = home / "current" / "package-manifest.json"
    active_runner = None
    if manifest.is_file():
        try:
            active_runner = hashlib.sha256(manifest.read_bytes()).hexdigest()[:20]
        except OSError:
            active_runner = None
    if runners.is_dir():
        for path in runners.iterdir():
            if path.name == active_runner:
                continue
            try:
                age_days = (now - path.stat().st_mtime) / (24 * 60 * 60)
            except OSError:
                continue
            if age_days >= 30:
                items.append(
                    CleanupItem("inactive_command_runner", str(path), age_days, _size(path))
                )
    return sorted(items, key=lambda item: (item.category, item.path))


def cleanup(home: Path, *, apply: bool = False, now: datetime | None = None) -> CleanupReport:
    selected_home = home.expanduser().resolve()
    selected_now = (now or datetime.now(timezone.utc)).timestamp()
    items = _candidates(selected_home, now=selected_now)
    reclaimed = 0
    if apply:
        for item in items:
            path = Path(item.path).resolve()
            if not path.is_relative_to(selected_home) or path == selected_home:
                raise CleanupError("cleanup target escaped the Villani home")
            # Run bundles, configuration, licenses, and the active/previous
            # installations are intentionally absent from the candidate policy.
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                os.unlink(path)
            reclaimed += item.size_bytes
    return CleanupReport(
        "villani.cleanup_report.v1",
        apply,
        tuple(items),
        reclaimed if apply else sum(item.size_bytes for item in items),
    )


__all__ = ["CleanupError", "CleanupItem", "CleanupReport", "cleanup"]
