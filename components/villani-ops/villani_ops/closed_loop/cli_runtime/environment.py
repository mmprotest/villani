"""Explicit environment construction with value-free serialized provenance."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping

from .models import CliEnvironmentVariable


def _validated_mapping(values: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        value = str(raw_value)
        if not key or "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError(f"invalid environment entry {key!r}")
        result[key] = value
    return result


def minimal_cli_environment_values(
    current_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return only OS/bootstrap paths needed by persisted-login CLI processes.

    Provider credentials, model hints, session identifiers, hooks, and arbitrary
    parent-process variables are intentionally excluded.
    """

    ambient = _validated_mapping(
        os.environ if current_environment is None else current_environment
    )
    names = (
        (
            "PATH",
            "Path",
            "PATHEXT",
            "SystemRoot",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "HOMEDRIVE",
            "HOMEPATH",
            "PROGRAMDATA",
        )
        if os.name == "nt"
        else (
            "PATH",
            "HOME",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        )
    )
    allowed = {name.casefold() if os.name == "nt" else name for name in names}
    return {
        name: value
        for name, value in ambient.items()
        if (name.casefold() if os.name == "nt" else name) in allowed
    }


@dataclass(frozen=True, slots=True)
class ResolvedCliEnvironment:
    values: Mapping[str, str]
    metadata: tuple[CliEnvironmentVariable, ...]
    redaction_keys: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class CliEnvironmentPolicy:
    mode: Literal["inherit", "minimal"] = "inherit"
    additions: Mapping[str, str] = field(default_factory=dict)
    overrides: Mapping[str, str] = field(default_factory=dict)
    removals: frozenset[str] = frozenset()
    redaction_keys: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.mode not in {"inherit", "minimal"}:
            raise ValueError("environment mode must be 'inherit' or 'minimal'")
        object.__setattr__(
            self, "additions", MappingProxyType(_validated_mapping(self.additions))
        )
        object.__setattr__(
            self, "overrides", MappingProxyType(_validated_mapping(self.overrides))
        )
        object.__setattr__(self, "removals", frozenset(map(str, self.removals)))
        object.__setattr__(
            self, "redaction_keys", frozenset(map(str, self.redaction_keys))
        )

    @staticmethod
    def _identity(name: str) -> str:
        return name.casefold() if os.name == "nt" else name

    def resolve(
        self, current_environment: Mapping[str, str] | None = None
    ) -> ResolvedCliEnvironment:
        """Resolve values once; durable metadata records only names/provenance."""

        ambient = _validated_mapping(
            os.environ if current_environment is None else current_environment
        )
        values: dict[str, str] = dict(ambient) if self.mode == "inherit" else {}
        provenance: dict[str, str] = {
            self._identity(name): "inherited" for name in values
        }
        canonical_names: dict[str, str] = {
            self._identity(name): name for name in values
        }

        def remove(name: str) -> None:
            identity = self._identity(name)
            existing = canonical_names.pop(identity, None)
            provenance.pop(identity, None)
            if existing is not None:
                values.pop(existing, None)

        def set_value(name: str, value: str, source: str, *, replace: bool) -> None:
            identity = self._identity(name)
            existing = canonical_names.get(identity)
            if existing is not None and not replace:
                return
            if existing is not None and existing != name:
                values.pop(existing, None)
            values[name] = value
            canonical_names[identity] = name
            provenance[identity] = source

        for name in self.removals:
            remove(name)
        for name, value in self.additions.items():
            set_value(name, value, "addition", replace=False)
        for name, value in self.overrides.items():
            set_value(name, value, "override", replace=True)

        redacted = {self._identity(name) for name in self.redaction_keys}
        metadata = tuple(
            CliEnvironmentVariable(
                name=name,
                provenance=provenance[self._identity(name)],  # type: ignore[arg-type]
                redacted=self._identity(name) in redacted,
            )
            for name in sorted(values, key=lambda item: item.casefold())
        )
        return ResolvedCliEnvironment(
            values=values,
            metadata=metadata,
            redaction_keys=frozenset(
                name for name in values if self._identity(name) in redacted
            ),
        )


__all__ = [
    "CliEnvironmentPolicy",
    "ResolvedCliEnvironment",
    "minimal_cli_environment_values",
]
