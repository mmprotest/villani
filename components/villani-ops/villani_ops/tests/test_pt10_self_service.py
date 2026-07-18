from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from villani_ops.self_service import (
    CORE_SAFETY_FEATURES,
    PRO_FEATURES,
    EntitlementError,
    apply_entitlement_execution_policy,
    load_entitlement,
)
from villani_ops.self_service.entitlements import install_license


ROOT = Path(__file__).resolve().parents[4]
LICENSES = ROOT / "integration" / "fixtures" / "licenses"


def test_pt10_normative_and_packaged_schemas_are_identical() -> None:
    names = {
        "doctor",
        "entitlement-state",
        "license",
        "package-manifest",
        "support-bundle-manifest",
        "update-feed",
        "update-policy",
        "update-state",
    }
    normative = ROOT / "schemas" / "v1"
    packaged = ROOT / "components" / "villani-ops" / "villani_ops" / "schemas" / "v1"
    for name in names:
        assert json.loads((normative / f"{name}.schema.json").read_text()) == json.loads(
            (packaged / f"{name}.schema.json").read_text()
        )


def test_free_entitlement_keeps_every_core_safety_capability(tmp_path: Path) -> None:
    state = load_entitlement(tmp_path)
    assert state.tier == "free"
    assert set(state.effective_features) == CORE_SAFETY_FEATURES
    assert set(state.locked_features) == PRO_FEATURES
    assert state.evidence_readable is True
    assert state.accepted_runs_verifiable is True
    assert state.licensing_network_used is False
    assert state.source_data_shared is False


def test_signed_development_license_is_explicitly_gated_and_tampering_fails(
    tmp_path: Path,
) -> None:
    (tmp_path / "license.json").write_bytes(
        (LICENSES / "development-pro.json").read_bytes()
    )
    disabled = load_entitlement(tmp_path, environ={})
    enabled = load_entitlement(
        tmp_path,
        environ={"VILLANI_ALLOW_DEVELOPMENT_LICENSE": "1"},
    )
    assert disabled.status == "invalid"
    assert enabled.tier == "pro" and enabled.status == "active"
    assert set(enabled.effective_features) == CORE_SAFETY_FEATURES | PRO_FEATURES

    (tmp_path / "license.json").write_bytes(
        (LICENSES / "development-tampered.json").read_bytes()
    )
    tampered = load_entitlement(
        tmp_path,
        environ={"VILLANI_ALLOW_DEVELOPMENT_LICENSE": "1"},
    )
    assert tampered.status == "invalid"
    assert tampered.tier == "free"


def test_expiry_and_offline_grace_never_remove_evidence_safety(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads((LICENSES / "development-pro.json").read_text())
    document["expires_at"] = "2026-07-18T00:00:00Z"
    (tmp_path / "license.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(
        "villani_ops.self_service.entitlements._verify_signature", lambda _value: True
    )
    environment = {"VILLANI_ALLOW_DEVELOPMENT_LICENSE": "1"}
    grace = load_entitlement(
        tmp_path,
        now=datetime(2026, 7, 25, tzinfo=timezone.utc),
        environ=environment,
    )
    expired = load_entitlement(
        tmp_path,
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
        environ=environment,
    )
    assert grace.status == "offline_grace" and grace.tier == "pro"
    assert expired.status == "expired" and expired.tier == "free"
    assert expired.evidence_readable and expired.accepted_runs_verifiable
    assert set(expired.effective_features) == CORE_SAFETY_FEATURES


def test_invalid_install_is_rejected_before_replacing_current_license(tmp_path: Path) -> None:
    destination = tmp_path / "license.json"
    destination.write_bytes((LICENSES / "development-pro.json").read_bytes())
    before = destination.read_bytes()
    with pytest.raises(EntitlementError):
        install_license(
            LICENSES / "development-tampered.json",
            tmp_path,
            environ={"VILLANI_ALLOW_DEVELOPMENT_LICENSE": "1"},
        )
    assert destination.read_bytes() == before


def test_free_execution_projection_disables_automatic_multi_route_without_mutation(
    tmp_path: Path,
) -> None:
    configuration = {
        "routing": {"mode": "enforce", "automatic_selection": True},
        "budgets": {"max_attempts": 4},
        "backends": {
            "a": {"enabled": True, "roles": ["coding", "classification"]},
            "b": {"enabled": True, "roles": ["coding"]},
        },
    }
    projected, state = apply_entitlement_execution_policy(configuration, tmp_path)
    assert state.tier == "free"
    assert projected["routing"] == {"mode": "observe", "automatic_selection": False}
    assert projected["budgets"]["max_attempts"] == 1
    assert "coding" not in projected["backends"]["b"]["roles"]
    assert configuration["routing"]["mode"] == "enforce"
