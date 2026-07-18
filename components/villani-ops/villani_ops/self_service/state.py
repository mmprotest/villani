"""Read-only self-service state projection for services and the Console."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .contracts import UpdatePolicy, UpdateState


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("document must be one JSON object")
    return value


def load_update_policy(home: Path) -> UpdatePolicy:
    """Read the user-controlled update policy without performing an update check."""

    path = home.expanduser().resolve() / "update-policy.json"
    if not path.is_file():
        return UpdatePolicy()
    return UpdatePolicy.model_validate(_object(path))


def load_update_state(home: Path, *, installed_version: str) -> UpdateState:
    """Read update state without network, migration, or repository access."""

    root = home.expanduser().resolve()
    state_path = root / "update-state.json"
    try:
        policy = load_update_policy(root)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
        policy = UpdatePolicy()
        return UpdateState(
            installed_version=installed_version,
            policy=policy,
            status="failed",
            evidence_path=str(root / "update-policy.json"),
            error=f"Update policy is invalid. Run: villani update channel stable ({error})",
        )
    if not state_path.is_file():
        return UpdateState(
            installed_version=installed_version,
            policy=policy,
            active_installation=(str(root / "current") if (root / "current").is_dir() else None),
            evidence_path=str(state_path),
        )
    try:
        state = UpdateState.model_validate(_object(state_path))
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
        return UpdateState(
            installed_version=installed_version,
            policy=policy,
            status="failed",
            evidence_path=str(state_path),
            error=f"Update state is invalid. Run: villani doctor ({error})",
        )
    return state.model_copy(update={"installed_version": installed_version, "policy": policy})


__all__ = ["load_update_policy", "load_update_state"]
