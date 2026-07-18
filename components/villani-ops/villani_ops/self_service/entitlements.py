"""Offline-only entitlement verification with safety features that never expire."""

from __future__ import annotations

import base64
import hashlib
import importlib.resources
import json
import os
import copy
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from villani_ops.closed_loop.durable_io import write_json_atomic

from .contracts import EntitlementState


CORE_SAFETY_FEATURES = frozenset(
    {
        "activity",
        "doctor",
        "evidence_read",
        "isolation",
        "manual_delivery",
        "one_agent_system",
        "support_bundle",
        "updates",
        "verification",
    }
)
PRO_FEATURES = frozenset(
    {
        "adaptive_verification",
        "advanced_export",
        "analytics",
        "automatic_routing_escalation",
        "multi_harness_qualification",
        "pull_request_delivery",
        "repository_learning",
    }
)

# Release engineering retains the private key outside the repository.  The
# repository contains only this 2048-bit RSA public modulus and a deliberately
# gated development fixture signed by it.
_LICENSE_RSA_N = int(
    "dcde730d1f6680845a99dab460a9b978a78d839b001498a9822a5c79f9e0ebed"
    "a91a7a038faa66d26ec821ff7d087446ae697de298c97c8223b710324b5017c447"
    "e7a0f62496783ee1fab93e67444aa3fb24a87f45b395a2cf5953d88e15e3bdb5"
    "61126d6ebd2cc0b31a721be0c493a0737ac227bf4e7281b600db0fcd91afe9cac"
    "25553d3a724cd84738689f269ca762a388d7c602c76a414a4682774d66656ab6d"
    "17b484ac9af6f8b521f6f9afd7054e60d82c47e95e23f50dd8d41f393578f9bc"
    "53782dbeba423607996878a30fc50c8e190c8924561045181c84cf94ccbfa8eeaf"
    "698e9e99107d65409aab9fd926d78e3d1f380c821c355dfa1f804b71c1",
    16,
)
_LICENSE_RSA_E = 65537
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class EntitlementError(RuntimeError):
    pass


class _LicenseDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    license_id: str = Field(min_length=1)
    tier: str
    issued_at: datetime
    expires_at: datetime
    offline_grace_days: int = Field(ge=0, le=90)
    features: list[str]
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    signature_algorithm: str
    signature: str


def _canonical_payload(document: Mapping[str, Any]) -> bytes:
    value = {str(key): item for key, item in document.items() if key != "signature"}
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_signature(document: Mapping[str, Any]) -> bool:
    if document.get("signature_algorithm") != "RS256":
        return False
    signature_value = document.get("signature")
    if not isinstance(signature_value, str):
        return False
    try:
        padding = "=" * (-len(signature_value) % 4)
        signature = base64.urlsafe_b64decode(signature_value + padding)
    except (ValueError, TypeError):
        return False
    width = (_LICENSE_RSA_N.bit_length() + 7) // 8
    if len(signature) != width:
        return False
    decoded = pow(int.from_bytes(signature, "big"), _LICENSE_RSA_E, _LICENSE_RSA_N)
    encoded = decoded.to_bytes(width, "big")
    digest = hashlib.sha256(_canonical_payload(document)).digest()
    padding_width = width - len(_SHA256_DIGEST_INFO) - len(digest) - 3
    expected = (
        b"\x00\x01"
        + (b"\xff" * padding_width)
        + b"\x00"
        + _SHA256_DIGEST_INFO
        + digest
    )
    return hashlib.sha256(encoded).digest() == hashlib.sha256(expected).digest()


def _now(value: datetime | None) -> datetime:
    selected = value or datetime.now(timezone.utc)
    if selected.tzinfo is None:
        selected = selected.replace(tzinfo=timezone.utc)
    return selected.astimezone(timezone.utc)


def _free_state(path: Path, *, status: str = "free", repair: str | None = None) -> EntitlementState:
    return EntitlementState(
        tier="free",
        status=status,  # type: ignore[arg-type]
        effective_features=sorted(CORE_SAFETY_FEATURES),
        locked_features=sorted(PRO_FEATURES),
        core_safety_features=sorted(CORE_SAFETY_FEATURES),
        repair_action=repair,
        evidence_path=str(path),
    )


def _load_entitlement_path(
    path: Path,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> EntitlementState:
    """Load a local license without network access or source/repository input."""

    path = path.expanduser().resolve()
    if not path.is_file():
        return _free_state(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("license must be an object")
        license_document = _LicenseDocument.model_validate(raw)
        if license_document.schema_version != "villani.license.v1":
            raise ValueError("unsupported license schema")
        if license_document.tier != "pro":
            raise ValueError("unsupported license tier")
        if set(license_document.features) != PRO_FEATURES:
            raise ValueError("license feature set is not canonical")
        if not _verify_signature(raw):
            raise ValueError("license signature is invalid")
        env = os.environ if environ is None else environ
        if (
            license_document.issuer == "development"
            and env.get("VILLANI_ALLOW_DEVELOPMENT_LICENSE") != "1"
        ):
            raise ValueError("development license use is disabled")
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
        return _free_state(
            path,
            status="invalid",
            repair=f"Run: villani license install PATH ({error})",
        )

    selected_now = _now(now)
    expires_at = _now(license_document.expires_at)
    issued_at = _now(license_document.issued_at)
    grace_ends = expires_at + timedelta(days=license_document.offline_grace_days)
    if selected_now <= expires_at:
        status = "active"
        tier = "pro"
        effective = CORE_SAFETY_FEATURES | PRO_FEATURES
        locked: set[str] = set()
        repair = None
    elif selected_now <= grace_ends:
        status = "offline_grace"
        tier = "pro"
        effective = CORE_SAFETY_FEATURES | PRO_FEATURES
        locked = set()
        repair = "Install a renewed local license before offline grace ends."
    else:
        status = "expired"
        tier = "free"
        effective = CORE_SAFETY_FEATURES
        locked = set(PRO_FEATURES)
        repair = "Run: villani license install PATH"
    return EntitlementState(
        tier=tier,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        license_id=license_document.license_id,
        issuer=license_document.issuer,
        issued_at=issued_at,
        expires_at=expires_at,
        offline_grace_ends_at=grace_ends,
        effective_features=sorted(effective),
        locked_features=sorted(locked),
        core_safety_features=sorted(CORE_SAFETY_FEATURES),
        repair_action=repair,
        evidence_path=str(path),
    )


def load_entitlement(
    home: Path,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> EntitlementState:
    return _load_entitlement_path(
        home.expanduser().resolve() / "license.json",
        now=now,
        environ=environ,
    )


def entitlement_allows(feature: str, home: Path, **kwargs: Any) -> bool:
    return feature in load_entitlement(home, **kwargs).effective_features


def require_entitlement(feature: str, home: Path, **kwargs: Any) -> EntitlementState:
    state = load_entitlement(home, **kwargs)
    if feature not in state.effective_features:
        raise EntitlementError(
            f"{feature.replace('_', ' ')} requires Villani Pro. "
            "Run: villani license status"
        )
    return state


def apply_entitlement_execution_policy(
    configuration: Mapping[str, Any],
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], EntitlementState]:
    """Return a run-local policy copy; safety and stored evidence are never removed."""

    selected = copy.deepcopy(dict(configuration))
    state = load_entitlement(home, environ=environ)
    entitlement_record = {
        "schema_version": state.schema_version,
        "tier": state.tier,
        "status": state.status,
        "evidence_readable": True,
        "accepted_runs_verifiable": True,
        "source_data_shared": False,
    }
    selected["entitlement"] = entitlement_record
    if "automatic_routing_escalation" in state.effective_features:
        return selected, state
    routing = selected.setdefault("routing", {})
    if isinstance(routing, dict):
        routing["mode"] = "observe"
        routing["automatic_selection"] = False
    economics = selected.setdefault("economics", {})
    if isinstance(economics, dict):
        online = economics.setdefault("online_update", {})
        if isinstance(online, dict):
            online["enabled"] = False
    budgets = selected.setdefault("budgets", {})
    if isinstance(budgets, dict):
        current = budgets.get("max_attempts")
        if isinstance(current, int) and not isinstance(current, bool):
            budgets["max_attempts"] = min(current, 1)
    raw_backends = selected.get("backends")
    if isinstance(raw_backends, dict):
        coding = [
            name
            for name, value in sorted(raw_backends.items())
            if isinstance(value, dict)
            and bool(value.get("enabled", True))
            and "coding" in value.get("roles", ["coding"])
        ]
        if len(coding) > 1:
            retained = coding[0]
            for name in coding[1:]:
                value = raw_backends[name]
                roles = value.get("roles", ["coding"])
                value["roles"] = [role for role in roles if role != "coding"]
                value.setdefault("metadata", {})["entitlement_limited"] = True
            entitlement_record["retained_coding_backend"] = retained
            entitlement_record["limited_coding_backends"] = coding[1:]
    return selected, state


def install_license(
    source: Path,
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> EntitlementState:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise EntitlementError(f"license file does not exist: {source}")
    destination = home.expanduser().resolve() / "license.json"
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EntitlementError(f"license file is unreadable: {error}") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.pending")
    write_json_atomic(temporary, value)
    pending_state = _load_entitlement_path(temporary, environ=environ)
    if pending_state.status == "invalid":
        temporary.unlink(missing_ok=True)
        raise EntitlementError(pending_state.repair_action or "license validation failed")
    original = destination
    backup = destination.with_suffix(".json.backup")
    if original.is_file():
        backup.write_bytes(original.read_bytes())
    os.replace(temporary, destination)
    state = load_entitlement(home, environ=environ)
    if state.status == "invalid":
        if backup.is_file():
            os.replace(backup, destination)
        else:
            destination.unlink(missing_ok=True)
        raise EntitlementError(state.repair_action or "license validation failed")
    backup.unlink(missing_ok=True)
    return state


def development_license_bytes() -> bytes:
    fixture = importlib.resources.files("villani_ops.self_service").joinpath(
        "fixtures/development-pro.json"
    )
    return fixture.read_bytes()


__all__ = [
    "CORE_SAFETY_FEATURES",
    "PRO_FEATURES",
    "EntitlementError",
    "development_license_bytes",
    "apply_entitlement_execution_policy",
    "entitlement_allows",
    "install_license",
    "load_entitlement",
    "require_entitlement",
]
