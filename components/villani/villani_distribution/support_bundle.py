"""Opt-in, local-only, allowlisted and redacted support archives."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import sys
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from villani_ops.closed_loop.presentation import FAILURE_CATALOG
from villani_ops.closed_loop.schema_validation import SCHEMA_ROOT, SCHEMA_ROOT_V2
from villani_ops.self_service import SupportBundleItem, SupportBundleManifest


MAX_LOG_LINES = 2_000
MAX_LOG_BYTES = 2 * 1024 * 1024
RUN_JSON_ALLOWLIST = (
    "manifest.json",
    "state.json",
    "run-summary.json",
    "product-run.json",
    "selection.json",
    "materialization.json",
    "delivery.json",
)
REDACTIONS = (
    "absolute_paths",
    "diffs",
    "prompts_and_task_text",
    "repository_names",
    "secrets_and_credentials",
    "source_code",
    "terminal_content",
    "usernames",
)
_BLOCKED_KEY_PARTS = (
    "api_key",
    "authorization",
    "command",
    "content",
    "credential",
    "diff",
    "patch",
    "path",
    "prompt",
    "repository",
    "secret",
    "source_code",
    "stderr",
    "stdout",
    "task",
    "terminal",
    "token",
    "username",
)
_SECRET = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/-]+|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+)"
)
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\r\n]*")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9.:/])/[^\r\n]*")


class SupportBundleError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _redact_string(value: str, *, usernames: Sequence[str]) -> str:
    redacted = _SECRET.sub("[REDACTED_SECRET]", value)
    redacted = _WINDOWS_PATH.sub("[REDACTED_ABSOLUTE_PATH]", redacted)
    redacted = _POSIX_PATH.sub("[REDACTED_ABSOLUTE_PATH]", redacted)
    for username in usernames:
        if username:
            redacted = re.sub(re.escape(username), "[REDACTED_USERNAME]", redacted, flags=re.I)
    return redacted[:4_000]


def redact_support_value(value: Any, *, key: str = "", usernames: Sequence[str] = ()) -> Any:
    normalized = key.casefold()
    if any(part in normalized for part in _BLOCKED_KEY_PARTS):
        return "[REDACTED_BY_SUPPORT_POLICY]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_support_value(
                item,
                key=str(item_key),
                usernames=usernames,
            )
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_support_value(item, key=key, usernames=usernames) for item in value]
    if isinstance(value, tuple):
        return [redact_support_value(item, key=key, usernames=usernames) for item in value]
    if isinstance(value, str):
        return _redact_string(value, usernames=usernames)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value), usernames=usernames)


class SupportBundleBuilder:
    def __init__(self, home: Path) -> None:
        self.home = home.expanduser().resolve()
        self.output_root = self.home / "support"
        self.usernames = tuple(
            value
            for value in {
                os.environ.get("USERNAME"),
                os.environ.get("USER"),
                self.home.parent.name,
            }
            if value
        )

    @staticmethod
    def _versions() -> dict[str, Any]:
        versions: dict[str, str] = {}
        for package in (
            "villani",
            "villani-ops",
            "villani-code",
            "villani-agentd",
            "villani-control-plane",
        ):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = "not-installed"
        return {
            "schema_version": "villani.support_versions.v1",
            "packages": versions,
            "python": {
                "implementation": sys.implementation.name,
                "version": ".".join(map(str, sys.version_info[:3])),
            },
            "operating_system": os.name,
        }

    def _log_projection(self, path: Path) -> bytes | None:
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        projected: list[dict[str, Any]] = []
        for line in lines[-MAX_LOG_LINES:]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            safe = {
                key: value.get(key)
                for key in ("timestamp", "level", "event", "status", "code")
                if key in value
            }
            projected.append(redact_support_value(safe, usernames=self.usernames))
        payload = b"\n".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
            for item in projected
        )
        if payload:
            payload += b"\n"
        return payload[-MAX_LOG_BYTES:]

    def _run_files(self, run_ids: Sequence[str]) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for run_id in sorted(set(run_ids)):
            if Path(run_id).name != run_id or run_id in {".", ".."}:
                raise SupportBundleError("run IDs must be one path segment")
            root = self.home / "runs" / run_id
            if not root.is_dir():
                raise SupportBundleError(f"selected run does not exist: {run_id}")
            for name in RUN_JSON_ALLOWLIST:
                path = root / name
                if not path.is_file():
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                files[f"runs/{run_id}/{name}"] = _json_bytes(
                    redact_support_value(value, usernames=self.usernames)
                )
            verification = root / "verification"
            if verification.is_dir():
                for path in sorted(verification.glob("*-decision.json")):
                    try:
                        value = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    files[f"runs/{run_id}/verification/{path.name}"] = _json_bytes(
                        redact_support_value(value, usernames=self.usernames)
                    )
        return files

    def _collect(
        self,
        *,
        run_ids: Sequence[str],
        doctor: Mapping[str, Any] | None,
    ) -> tuple[dict[str, bytes], list[SupportBundleItem]]:
        files: dict[str, bytes] = {
            "versions.json": _json_bytes(self._versions()),
            "failure-codes.json": _json_bytes(
                {
                    "schema_version": "villani.failure_code_catalog.v1",
                    "codes": sorted(FAILURE_CATALOG),
                }
            ),
        }
        if doctor is not None:
            files["doctor.json"] = _json_bytes(
                redact_support_value(dict(doctor), usernames=self.usernames)
            )
        for schema_root, prefix in ((SCHEMA_ROOT, "v1"), (SCHEMA_ROOT_V2, "v2")):
            if schema_root.is_dir():
                for path in sorted(schema_root.glob("*.json")):
                    files[f"schemas/{prefix}/{path.name}"] = path.read_bytes()
        log = self._log_projection(self.home / "agentd" / "agentd.log")
        if log is not None:
            files["logs/agentd-structured-redacted.jsonl"] = log
        run_files = self._run_files(run_ids)
        files.update(run_files)
        source_class: dict[str, str] = {
            "versions.json": "versions",
            "failure-codes.json": "failure_codes",
            "doctor.json": "doctor",
            "logs/agentd-structured-redacted.jsonl": "logs",
        }
        items = [
            SupportBundleItem(
                logical_name=name,
                source_class=(
                    "schemas"
                    if name.startswith("schemas/")
                    else "run_evidence"
                    if name.startswith("runs/")
                    else source_class[name]
                ),  # type: ignore[arg-type]
                included=True,
                reason=(
                    "Explicit run selection; privacy redaction and evidence allowlist applied."
                    if name.startswith("runs/")
                    else "Default privacy-safe diagnostic allowlist."
                ),
                size_bytes=len(payload),
                sha256=_sha256_bytes(payload),
            )
            for name, payload in sorted(files.items())
        ]
        items.extend(
            [
                SupportBundleItem(
                    logical_name="prompts-source-diffs-terminal",
                    source_class="run_evidence",
                    included=False,
                    reason="Excluded by the default support privacy policy.",
                ),
                SupportBundleItem(
                    logical_name="unselected-run-evidence",
                    source_class="run_evidence",
                    included=False,
                    reason="Run evidence is opt-in by exact run ID.",
                ),
            ]
        )
        return files, items

    def preview(
        self,
        *,
        run_ids: Sequence[str] = (),
        doctor: Mapping[str, Any] | None = None,
    ) -> SupportBundleManifest:
        _files, items = self._collect(run_ids=run_ids, doctor=doctor)
        return SupportBundleManifest(
            generated_at=datetime.now(timezone.utc),
            preview=True,
            explicit_run_ids=sorted(set(run_ids)),
            items=items,
            redactions=list(REDACTIONS),
        )

    def create(
        self,
        *,
        run_ids: Sequence[str] = (),
        doctor: Mapping[str, Any] | None = None,
    ) -> tuple[Path, SupportBundleManifest]:
        files, items = self._collect(run_ids=run_ids, doctor=doctor)
        generated = datetime.now(timezone.utc)
        stamp = generated.strftime("%Y%m%dT%H%M%SZ")
        archive = self.output_root / f"villani-support-{stamp}.zip"
        self.output_root.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            archive = self.output_root / f"villani-support-{stamp}-{os.getpid()}.zip"
        inside_manifest = SupportBundleManifest(
            generated_at=generated,
            preview=False,
            explicit_run_ids=sorted(set(run_ids)),
            items=items,
            redactions=list(REDACTIONS),
            archive_name=archive.name,
        )
        files["manifest.json"] = _json_bytes(inside_manifest.model_dump(mode="json"))
        with zipfile.ZipFile(
            archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as output:
            for name, payload in sorted(files.items()):
                output.writestr(name, payload)
        digest = _sha256_bytes(archive.read_bytes())
        final = inside_manifest.model_copy(update={"archive_sha256": digest})
        sidecar = archive.with_suffix(".manifest.json")
        sidecar.write_bytes(_json_bytes(final.model_dump(mode="json")))
        return archive, final


__all__ = [
    "REDACTIONS",
    "SupportBundleBuilder",
    "SupportBundleError",
    "redact_support_value",
]
