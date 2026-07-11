"""Reusable conformance assertions for plugin authors and host tests."""

from __future__ import annotations

from typing import Any

from .models import PROTOCOL_VERSIONS, PluginManifest
from .transport import SubprocessPluginClient


def validate_manifest_conformance(manifest: PluginManifest) -> None:
    """Raise when a parsed manifest does not satisfy the host contract."""

    expected = PROTOCOL_VERSIONS[manifest.kind]
    if expected not in manifest.protocol_versions:
        raise AssertionError(f"required protocol {expected} is not declared")
    if not manifest.name or not manifest.version or not manifest.digest:
        raise AssertionError("plugin identity is incomplete")
    if not manifest.supported_platforms:
        raise AssertionError("supported_platforms must not be empty")
    if manifest.transport == "in-process" and (
        not manifest.builtin or manifest.trust_level != "built_in_trusted"
    ):
        raise AssertionError("only trusted built-ins may use in-process transport")


def run_echo_conformance(client: SubprocessPluginClient) -> dict[str, Any]:
    """Exercise one protocol round trip without granting a secret."""

    marker = {"conformance": "villani.plugin.v1"}
    response = client.call("conformance.echo", marker)
    if response.get("echo") != marker:
        raise AssertionError("plugin did not preserve the conformance payload")
    return dict(response)
