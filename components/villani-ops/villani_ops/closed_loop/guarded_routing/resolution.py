from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


PRECEDENCE = ("organization", "workspace", "project", "repository")


def _merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def resolve_routing_configuration(
    configuration: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    raw = configuration.get("routing")
    routing = dict(raw) if isinstance(raw, Mapping) else {}
    resolved = {
        key: deepcopy(value)
        for key, value in routing.items()
        if key not in {"scopes", "identity"}
    }
    applied = ["installation_default"]
    scopes = routing.get("scopes")
    scope_values = scopes if isinstance(scopes, Mapping) else {}
    identity = routing.get("identity")
    identities = identity if isinstance(identity, Mapping) else {}
    for level in PRECEDENCE:
        value = scope_values.get(level)
        if not isinstance(value, Mapping):
            continue
        expected_id = value.get("scope_id")
        actual_id = identities.get(f"{level}_id")
        if expected_id is not None and str(expected_id) != str(actual_id):
            continue
        overlay = {key: item for key, item in value.items() if key != "scope_id"}
        _merge(resolved, overlay)
        applied.append(level)
    resolved.setdefault("mode", "observe")
    return resolved, tuple(applied)
