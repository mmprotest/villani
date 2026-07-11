from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from villani_ops.closed_loop import (
    ClosedLoopController,
    EvidenceSelectorAdapter,
    PatchMaterializerAdapter,
    VillaniCodeAttemptAdapter,
    VillaniVerifierAdapter,
)
from villani_ops.closed_loop.plugins import (
    PluginDiscoveryError,
    PluginExecutionError,
    PluginManifest,
    SubprocessPluginClient,
    artifact_digest,
    builtin_plugin_manifests,
    discover_plugins,
    run_echo_conformance,
    validate_manifest_conformance,
)


ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "integration" / "fixtures" / "plugins"
FAKE = FIXTURES / "fake_plugin.py"


def _manifest(
    *, behavior: str = "echo", required_secrets: list[str] | None = None
) -> PluginManifest:
    raw = json.loads(
        (FIXTURES / "agent_runner.plugin.json").read_text(encoding="utf-8")
    )
    raw["entrypoint"] = [sys.executable, str(FAKE), "length-prefixed-json", behavior]
    raw["required_secrets"] = required_secrets or []
    return PluginManifest.model_validate(raw)


def _client(manifest: PluginManifest, **kwargs: object) -> SubprocessPluginClient:
    return SubprocessPluginClient(
        manifest,
        base_directory=FIXTURES,
        allowed_digests=[manifest.digest],
        **kwargs,
    )


def test_all_builtin_contracts_are_versioned_and_trusted() -> None:
    manifests = builtin_plugin_manifests()
    assert {item.kind.value for item in manifests} == {
        "agent_runner",
        "verifier",
        "selector",
        "materializer",
        "execution_provider",
    }
    for manifest in manifests:
        validate_manifest_conformance(manifest)
        assert manifest.version and manifest.protocol_versions and manifest.capabilities
        assert manifest.configuration_schema["type"] == "object"
        assert manifest.supported_platforms == ["any"]
        assert manifest.transport == "in-process"
        assert manifest.digest.startswith("sha256:")


def test_canonical_controller_records_all_used_plugin_identities() -> None:
    controller = ClosedLoopController(
        classifier=object(),  # type: ignore[arg-type]
        attempt_runner=VillaniCodeAttemptAdapter(),
        verifier=VillaniVerifierAdapter(),
        selector=EvidenceSelectorAdapter(),
        materializer=PatchMaterializerAdapter(),
    )
    identities = controller._plugin_identities  # noqa: SLF001 - durable contract assertion
    assert {item["kind"] for item in identities} == {
        "agent_runner",
        "verifier",
        "selector",
        "materializer",
        "execution_provider",
    }
    assert all(
        item["version"] and item["digest"].startswith("sha256:") for item in identities
    )


def test_discovery_is_inert_explicit_and_digest_allowlisted() -> None:
    digest = artifact_digest(FAKE)
    names = [
        json.loads(path.read_text())["name"] for path in FIXTURES.glob("*.plugin.json")
    ]
    found = discover_plugins(
        FIXTURES, digest_allowlist={name: [digest] for name in names}
    )
    assert len(found) == 5
    with pytest.raises(PluginDiscoveryError, match="not allowlisted"):
        discover_plugins(FIXTURES, digest_allowlist={})
    assert (
        discover_plugins(ROOT / "directory-that-does-not-exist", digest_allowlist={})
        == ()
    )
    with pytest.raises(ValueError, match="not allowlisted"):
        SubprocessPluginClient(_manifest(), base_directory=FIXTURES, allowed_digests=[])


def test_subprocess_conformance_and_unknown_secrets_are_not_forwarded() -> None:
    client = _client(_manifest(required_secrets=["KNOWN"]))
    result = client.call(
        "conformance.echo",
        {"safe": True},
        available_secrets={"KNOWN": "value", "UNKNOWN": "must-not-pass"},
    )
    assert result == {"echo": {"safe": True}, "secret_names": ["KNOWN"]}
    assert (
        run_echo_conformance(_client(_manifest()))["echo"]["conformance"]
        == "villani.plugin.v1"
    )


@pytest.mark.parametrize(
    ("behavior", "classification"),
    [
        ("crash", "crash"),
        ("timeout", "timeout"),
        ("oversized", "oversized_message"),
        ("malformed", "malformed_response"),
        ("mismatch", "protocol_mismatch"),
    ],
)
def test_subprocess_failures_are_classified(behavior: str, classification: str) -> None:
    client = _client(
        _manifest(behavior=behavior),
        timeout_seconds=0.05 if behavior == "timeout" else 5,
        maximum_message_bytes=1024,
    )
    with pytest.raises(PluginExecutionError) as captured:
        client.call("conformance.echo", {})
    assert captured.value.failure.classification == classification


def test_cancellation_fails_closed() -> None:
    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(PluginExecutionError) as captured:
        _client(_manifest(behavior="timeout"), timeout_seconds=5).call(
            "conformance.echo", {}, cancellation=cancellation
        )
    assert captured.value.failure.classification == "cancelled"


def test_untrusted_in_process_manifest_is_rejected() -> None:
    raw = json.loads((FIXTURES / "agent_runner.plugin.json").read_text())
    raw.update(
        {
            "transport": "in-process",
            "builtin": False,
            "entrypoint": None,
            "artifact_path": None,
        }
    )
    with pytest.raises(ValueError, match="built-in trusted"):
        PluginManifest.model_validate(raw)
