"""User-facing policy presets over the canonical closed-loop configuration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Mapping


PUBLIC_POLICY_VERSION = "villani-public-policy-v1"


@dataclass(frozen=True, slots=True)
class PolicyPreset:
    preset_id: str
    label: str
    description: str
    selection_preference: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


POLICY_PRESETS: tuple[PolicyPreset, ...] = (
    PolicyPreset(
        "performance",
        "Performance",
        "Minimize conservative total cost per proven accepted change.",
        "accepted_change_optimizer",
    ),
    PolicyPreset(
        "reliable",
        "Reliable",
        "Prefer stronger validation and escalation evidence.",
        "strongest_eligible",
    ),
    PolicyPreset(
        "balanced",
        "Balanced",
        "Use conservative accepted-change economics with explicit unknowns.",
        "accepted_change_optimizer",
    ),
    PolicyPreset(
        "local-first",
        "Local first",
        "Prefer an eligible local model and retain escalation when required.",
        "local_first",
    ),
    PolicyPreset(
        "cheapest-acceptable",
        "Cheapest acceptable",
        "Choose the lowest known-cost model that meets the active requirements.",
        "cheapest_acceptable",
    ),
    PolicyPreset(
        "custom",
        "Custom",
        "Use the Advanced routing, verifier, budget, and policy controls.",
        "custom",
    ),
)

_PRESETS = {item.preset_id: item for item in POLICY_PRESETS}
_ALIASES = {
    "local_first": "local-first",
    "cheapest": "cheapest-acceptable",
    "cheapest_acceptable": "cheapest-acceptable",
}


def normalize_policy_preset(value: object, *, default: str = "balanced") -> str:
    candidate = str(value or default).strip().lower().replace(" ", "-")
    candidate = _ALIASES.get(candidate, candidate)
    if candidate not in _PRESETS:
        choices = ", ".join(item.preset_id for item in POLICY_PRESETS)
        raise ValueError(f"policy preset must be one of: {choices}")
    return candidate


def configured_policy_preset(configuration: Mapping[str, Any]) -> str:
    public = configuration.get("public_policy")
    values = public if isinstance(public, Mapping) else {}
    return normalize_policy_preset(values.get("preset"), default="balanced")


def policy_preset_rows(
    configuration: Mapping[str, Any],
) -> list[dict[str, object]]:
    active = configured_policy_preset(configuration)
    return [
        {
            "id": item.preset_id,
            "label": item.label,
            "description": item.description,
            "active": item.preset_id == active,
            "advanced": item.preset_id == "custom",
            "policy_version": PUBLIC_POLICY_VERSION,
        }
        for item in POLICY_PRESETS
    ]


def configure_policy_preset(
    configuration: Mapping[str, Any], preset: str | None = None
) -> dict[str, Any]:
    """Return a saved configuration containing only the public preset choice."""

    result = deepcopy(dict(configuration))
    selected = normalize_policy_preset(
        preset,
        default=configured_policy_preset(configuration),
    )
    definition = _PRESETS[selected]
    public = result.get("public_policy")
    public_values = dict(public) if isinstance(public, Mapping) else {}
    public_values.update(
        {
            "version": PUBLIC_POLICY_VERSION,
            "preset": selected,
            "selection_preference": definition.selection_preference,
        }
    )
    result["public_policy"] = public_values
    return result


def apply_policy_preset(
    configuration: Mapping[str, Any], preset: str | None = None
) -> dict[str, Any]:
    """Return a run-scoped configuration; never mutate the saved input."""

    result = configure_policy_preset(configuration, preset)
    selected = configured_policy_preset(result)

    # Presets tune ordering and recovery only. They do not weaken verifier
    # evidence gates or change controller transition/acceptance rules.
    policy = result.get("policy")
    policy_values = dict(policy) if isinstance(policy, Mapping) else {}
    if selected == "performance":
        policy_values["accepted_candidates_required"] = 1
    elif selected == "reliable":
        policy_values["accepted_candidates_required"] = max(
            int(policy_values.get("accepted_candidates_required", 1)), 2
        )
        policy_values["max_same_backend_retries"] = max(
            int(policy_values.get("max_same_backend_retries", 1)), 1
        )
        policy_values["verifier_retry_limit"] = max(
            int(policy_values.get("verifier_retry_limit", 1)), 1
        )
    result["policy"] = policy_values
    return result


def selection_preference(configuration: Mapping[str, Any]) -> str:
    public = configuration.get("public_policy")
    values = public if isinstance(public, Mapping) else {}
    explicit = values.get("selection_preference")
    if isinstance(explicit, str) and explicit:
        return explicit
    return _PRESETS[configured_policy_preset(configuration)].selection_preference


__all__ = [
    "POLICY_PRESETS",
    "PUBLIC_POLICY_VERSION",
    "PolicyPreset",
    "apply_policy_preset",
    "configure_policy_preset",
    "configured_policy_preset",
    "normalize_policy_preset",
    "policy_preset_rows",
    "selection_preference",
]
