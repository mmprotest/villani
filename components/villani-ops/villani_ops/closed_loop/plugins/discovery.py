"""Inert discovery of explicitly configured local plugin manifests."""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path
from typing import Iterable, Mapping

from .models import PluginManifest


class PluginDiscoveryError(ValueError):
    pass


def artifact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def discover_plugins(
    directories: Iterable[str | Path],
    *,
    digest_allowlist: Mapping[str, Iterable[str]],
    current_platform: str | None = None,
) -> tuple[PluginManifest, ...]:
    """Read JSON manifests only; no import, subprocess, or entrypoint invocation occurs."""

    if isinstance(directories, (str, Path)):
        directories = (directories,)
    discovered: list[PluginManifest] = []
    system = (current_platform or platform.system()).lower()
    for configured in directories:
        directory = Path(configured).expanduser().resolve()
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.plugin.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                manifest = PluginManifest.model_validate(raw)
            except Exception as error:
                raise PluginDiscoveryError(
                    f"invalid plugin manifest {path}: {error}"
                ) from error
            if not manifest.enabled:
                continue
            allowed = set(digest_allowlist.get(manifest.name, ()))
            if manifest.digest not in allowed:
                raise PluginDiscoveryError(
                    f"plugin {manifest.name!r} digest is not allowlisted"
                )
            if manifest.transport == "in-process":
                raise PluginDiscoveryError(
                    "configured directories cannot provide in-process plugins"
                )
            artifact = (path.parent / str(manifest.artifact_path)).resolve()
            if (
                not artifact.is_relative_to(path.parent.resolve())
                or not artifact.is_file()
            ):
                raise PluginDiscoveryError(
                    f"plugin {manifest.name!r} artifact is unavailable or outside its directory"
                )
            if artifact_digest(artifact) != manifest.digest:
                raise PluginDiscoveryError(
                    f"plugin {manifest.name!r} artifact digest mismatch"
                )
            supported = {item.lower() for item in manifest.supported_platforms}
            if "any" not in supported and system not in supported:
                continue
            discovered.append(manifest)
    identities: set[tuple[str, str]] = set()
    for item in discovered:
        identity = (item.kind.value, item.name)
        if identity in identities:
            raise PluginDiscoveryError(f"duplicate enabled plugin {identity}")
        identities.add(identity)
    return tuple(discovered)


def discover_plugins_from_configuration(
    configuration: Mapping[str, object],
) -> tuple[PluginManifest, ...]:
    """Discover only directories and digests explicitly named in local configuration."""

    raw = configuration.get("plugins")
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise PluginDiscoveryError("plugins configuration must be an object")
    directories = raw.get("directories", ())
    allowlist = raw.get("digest_allowlist", {})
    if isinstance(directories, (str, Path)):
        directories = (directories,)
    if not isinstance(directories, (list, tuple)) or not all(
        isinstance(item, (str, Path)) for item in directories
    ):
        raise PluginDiscoveryError("plugins.directories must contain explicit paths")
    if not isinstance(allowlist, Mapping):
        raise PluginDiscoveryError("plugins.digest_allowlist must be an object")
    normalized: dict[str, tuple[str, ...]] = {}
    for name, digests in allowlist.items():
        if (
            not isinstance(name, str)
            or not isinstance(digests, (list, tuple))
            or not all(isinstance(item, str) for item in digests)
        ):
            raise PluginDiscoveryError(
                "plugin digest allowlist entries must be string lists"
            )
        normalized[name] = tuple(digests)
    return discover_plugins(directories, digest_allowlist=normalized)
