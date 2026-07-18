#!/usr/bin/env python3
"""Generate the normative and packaged PT10 self-service JSON Schemas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DESTINATIONS = (
    ROOT / "schemas" / "v1",
    ROOT / "components" / "villani-ops" / "villani_ops" / "schemas" / "v1",
)
SHA256 = {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
DATE_TIME = {"type": ["string", "null"], "format": "date-time"}


def strict_schema(
    identifier: str,
    title: str,
    required: list[str],
    properties: dict[str, Any],
    *,
    definitions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://schemas.villani.local/v1/{identifier}.schema.json",
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }
    if definitions:
        value["$defs"] = definitions
    return value


UPDATE_POLICY = strict_schema(
    "update-policy",
    "Villani update policy v1",
    ["schema_version", "channel", "pinned_version", "feed_url", "checks_enabled"],
    {
        "schema_version": {"const": "villani.update_policy.v1"},
        "channel": {"enum": ["stable", "beta", "pinned"]},
        "pinned_version": {"type": ["string", "null"]},
        "feed_url": {"type": ["string", "null"]},
        "checks_enabled": {"type": "boolean"},
    },
)

SCHEMAS: dict[str, dict[str, Any]] = {
    "update-policy.schema.json": UPDATE_POLICY,
    "update-feed.schema.json": strict_schema(
        "update-feed",
        "Villani update feed v1",
        ["schema_version", "releases", "source_upload_required"],
        {
            "schema_version": {"const": "villani.update_feed.v1"},
            "releases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["version", "channel", "released_at", "release_notes", "minimum_config_version", "maximum_config_version", "artifacts"],
                    "properties": {
                        "version": {"type": "string"},
                        "channel": {"enum": ["stable", "beta"]},
                        "released_at": {"type": "string", "format": "date-time"},
                        "release_notes": {"type": "string"},
                        "minimum_config_version": {"type": "integer", "minimum": 1},
                        "maximum_config_version": {"type": "integer", "minimum": 1},
                        "artifacts": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["operating_system", "architecture", "url", "sha256"],
                                "properties": {
                                    "operating_system": {"enum": ["windows", "macos", "linux"]},
                                    "architecture": {"type": "string"},
                                    "url": {"type": "string"},
                                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                },
                            },
                        },
                    },
                },
            },
            "source_upload_required": {"const": False},
        },
    ),
    "license.schema.json": strict_schema(
        "license",
        "Villani offline license v1",
        [
            "schema_version",
            "license_id",
            "tier",
            "issued_at",
            "expires_at",
            "offline_grace_days",
            "features",
            "issuer",
            "subject",
            "signature_algorithm",
            "signature",
        ],
        {
            "schema_version": {"const": "villani.license.v1"},
            "license_id": {"type": "string", "minLength": 1},
            "tier": {"const": "pro"},
            "issued_at": {"type": "string", "format": "date-time"},
            "expires_at": {"type": "string", "format": "date-time"},
            "offline_grace_days": {"type": "integer", "minimum": 0, "maximum": 90},
            "features": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string"},
            },
            "issuer": {"type": "string", "minLength": 1},
            "subject": {"type": "string", "minLength": 1},
            "signature_algorithm": {"const": "RS256"},
            "signature": {"type": "string", "minLength": 1},
        },
    ),
    "entitlement-state.schema.json": strict_schema(
        "entitlement-state",
        "Villani entitlement state v1",
        [
            "schema_version",
            "tier",
            "status",
            "license_id",
            "issuer",
            "issued_at",
            "expires_at",
            "offline_grace_ends_at",
            "effective_features",
            "locked_features",
            "core_safety_features",
            "evidence_readable",
            "accepted_runs_verifiable",
            "licensing_network_used",
            "source_data_shared",
            "repair_action",
            "evidence_path",
        ],
        {
            "schema_version": {"const": "villani.entitlement_state.v1"},
            "tier": {"enum": ["free", "pro"]},
            "status": {"enum": ["free", "active", "offline_grace", "expired", "invalid"]},
            "license_id": {"type": ["string", "null"]},
            "issuer": {"type": ["string", "null"]},
            "issued_at": DATE_TIME,
            "expires_at": DATE_TIME,
            "offline_grace_ends_at": DATE_TIME,
            "effective_features": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "locked_features": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "core_safety_features": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "evidence_readable": {"const": True},
            "accepted_runs_verifiable": {"const": True},
            "licensing_network_used": {"const": False},
            "source_data_shared": {"const": False},
            "repair_action": {"type": ["string", "null"]},
            "evidence_path": {"type": "string"},
        },
    ),
    "update-state.schema.json": strict_schema(
        "update-state",
        "Villani update state v1",
        [
            "schema_version",
            "installed_version",
            "policy",
            "status",
            "available_version",
            "last_checked_at",
            "release_notes",
            "artifact_url",
            "artifact_sha256",
            "migration_preview",
            "active_installation",
            "previous_installation",
            "configuration_backup",
            "evidence_path",
            "error",
            "repositories_modified",
            "source_uploaded",
            "forced",
        ],
        {
            "schema_version": {"const": "villani.update_state.v1"},
            "installed_version": {"type": "string"},
            "policy": UPDATE_POLICY,
            "status": {"enum": ["idle", "current", "available", "downloaded", "installing", "verified", "rolled_back", "failed"]},
            "available_version": {"type": ["string", "null"]},
            "last_checked_at": DATE_TIME,
            "release_notes": {"type": ["string", "null"]},
            "artifact_url": {"type": ["string", "null"]},
            "artifact_sha256": SHA256,
            "migration_preview": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["configuration_version", "spool_version_before", "spool_version_after", "checked_run_bundles", "protocol_majors", "actions", "configuration_backup_required", "destructive", "repositories_modified"],
                "properties": {
                    "configuration_version": {"type": ["integer", "null"]},
                    "spool_version_before": {"type": ["integer", "null"]},
                    "spool_version_after": {"type": ["integer", "null"]},
                    "checked_run_bundles": {"type": "integer", "minimum": 0},
                    "protocol_majors": {"type": "array", "items": {"type": "integer"}},
                    "actions": {"type": "array", "items": {"type": "string"}},
                    "configuration_backup_required": {"type": "boolean"},
                    "destructive": {"const": False},
                    "repositories_modified": {"const": False},
                },
            },
            "active_installation": {"type": ["string", "null"]},
            "previous_installation": {"type": ["string", "null"]},
            "configuration_backup": {"type": ["string", "null"]},
            "evidence_path": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
            "repositories_modified": {"const": False},
            "source_uploaded": {"const": False},
            "forced": {"const": False},
        },
    ),
    "support-bundle-manifest.schema.json": strict_schema(
        "support-bundle-manifest",
        "Villani privacy-preserving support bundle manifest v1",
        [
            "schema_version",
            "generated_at",
            "preview",
            "explicit_run_ids",
            "items",
            "redactions",
            "archive_name",
            "archive_sha256",
            "uploaded",
            "repositories_modified",
            "prompts_included",
            "source_included",
            "diffs_included",
            "terminal_content_included",
        ],
        {
            "schema_version": {"const": "villani.support_bundle_manifest.v1"},
            "generated_at": {"type": "string", "format": "date-time"},
            "preview": {"type": "boolean"},
            "explicit_run_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["logical_name", "source_class", "included", "reason", "size_bytes", "sha256"],
                    "properties": {
                        "logical_name": {"type": "string"},
                        "source_class": {"enum": ["versions", "schemas", "logs", "doctor", "failure_codes", "run_evidence"]},
                        "included": {"type": "boolean"},
                        "reason": {"type": "string"},
                        "size_bytes": {"type": ["integer", "null"], "minimum": 0},
                        "sha256": SHA256,
                    },
                },
            },
            "redactions": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "archive_name": {"type": ["string", "null"]},
            "archive_sha256": SHA256,
            "uploaded": {"const": False},
            "repositories_modified": {"const": False},
            "prompts_included": {"const": False},
            "source_included": {"const": False},
            "diffs_included": {"const": False},
            "terminal_content_included": {"const": False},
        },
    ),
    "package-manifest.schema.json": strict_schema(
        "package-manifest",
        "Villani standalone package manifest v1",
        ["schema_version", "version", "operating_system", "architecture", "files", "sbom_path", "release_notes_path", "source_checkout_required", "sibling_node_modules_required"],
        {
            "schema_version": {"const": "villani.package_manifest.v1"},
            "version": {"type": "string"},
            "operating_system": {"enum": ["windows", "macos", "linux"]},
            "architecture": {"type": "string"},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "sha256", "size_bytes", "executable"],
                    "properties": {
                        "path": {"type": "string"},
                        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "size_bytes": {"type": "integer", "minimum": 0},
                        "executable": {"type": "boolean"},
                    },
                },
            },
            "sbom_path": {"type": "string"},
            "release_notes_path": {"type": "string"},
            "source_checkout_required": {"const": False},
            "sibling_node_modules_required": {"const": False},
        },
    ),
    "doctor.schema.json": strict_schema(
        "doctor",
        "Villani doctor report v1",
        ["schema_version", "generated_at", "healthy", "ok", "summary", "checks", "repositories_modified", "evidence_path"],
        {
            "schema_version": {"const": "villani.doctor.v1"},
            "generated_at": {"type": "string", "format": "date-time"},
            "healthy": {"type": "boolean"},
            "ok": {"type": "boolean"},
            "summary": {"type": "object", "additionalProperties": False, "required": ["passed", "warnings", "failed"], "properties": {"passed": {"type": "integer", "minimum": 0}, "warnings": {"type": "integer", "minimum": 0}, "failed": {"type": "integer", "minimum": 0}}},
            "checks": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["identifier", "status", "message", "recovery_action", "details", "repositories_modified", "evidence_path"], "properties": {"identifier": {"type": "string"}, "status": {"enum": ["pass", "warn", "fail"]}, "message": {"type": "string"}, "recovery_action": {"type": ["string", "null"]}, "details": {"type": "object"}, "repositories_modified": {"const": False}, "evidence_path": {"type": "string"}}}},
            "repositories_modified": {"const": False},
            "evidence_path": {"type": "string"},
        },
    ),
}

# Doctor carries additive component-specific diagnostic domains so old and new
# consumers can retain the stable envelope while ignoring unknown top-level data.
SCHEMAS["doctor.schema.json"]["additionalProperties"] = True


def payload(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def generate(*, check: bool) -> list[str]:
    drift: list[str] = []
    for directory in DESTINATIONS:
        for name, schema in SCHEMAS.items():
            path = directory / name
            expected = payload(schema)
            if check:
                if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                    drift.append(str(path.relative_to(ROOT)).replace("\\", "/"))
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected, encoding="utf-8", newline="\n")
    return drift


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    drift = generate(check=args.check)
    if drift:
        print("PT10 schema drift: " + ", ".join(drift))
        return 1
    print(f"PT10 schemas {'verified' if args.check else 'generated'}: {len(SCHEMAS)} contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
