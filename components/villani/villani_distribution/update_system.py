"""User-controlled verified side-by-side installs, updates, and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from villani_ops.closed_loop.durable_io import write_json_atomic
from villani_ops.self_service import (
    MigrationPreview,
    PackageManifest,
    UpdateArtifact,
    UpdateFeed,
    UpdatePolicy,
    UpdateRelease,
    UpdateState,
)

from . import __version__
from .migrations import (
    SUPPORTED_CONFIG_VERSION,
    SUPPORTED_SPOOL_VERSION,
    MigrationError,
    check_upgrade,
)


MAX_UPDATE_BYTES = 2 * 1024 * 1024 * 1024
MAX_FEED_BYTES = 2 * 1024 * 1024
COMMANDS = ("villani", "villani-code", "villani-agentd", "vfr")


class UpdateError(RuntimeError):
    pass


def _process_running(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def installed_version() -> str:
    # Every shipped component carries the same generated constant.  Reading the
    # module also stays correct in an editable development checkout whose stale
    # dist-info may predate a version-contract change.
    return __version__


def _system() -> str:
    value = platform.system().lower()
    return {"darwin": "macos", "win32": "windows"}.get(value, value)


def _architecture(value: str | None = None) -> str:
    selected = (value or platform.machine()).lower().replace("-", "_")
    return {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }.get(selected, selected)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"{path.name} is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise UpdateError(f"{path.name} must contain one JSON object")
    return value


class UpdateManager:
    """Own software lifecycle mutations under one explicitly selected home."""

    def __init__(self, home: Path, *, version: str | None = None) -> None:
        self.home = home.expanduser().resolve()
        self.version = version or installed_version()
        self.policy_path = self.home / "update-policy.json"
        self.state_path = self.home / "update-state.json"
        self.current = self.home / "current"
        self.installations = self.home / "installations"
        self.transactions = self.home / "update-transactions"
        self.failures = self.home / "update-failures"
        self.downloads = self.home / "downloads"
        self.launchers = self.home / "bin"
        self.runners = self.home / "runners"
        self.update_lock = self.home / "update.lock"
        self._transaction_active = False

    def _acquire_update_lock(self) -> str:
        self.home.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(16)
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self.update_lock,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                try:
                    owner = _json(self.update_lock)
                except UpdateError as error:
                    raise UpdateError(
                        "the update lock is unreadable; if no update process is running, "
                        "remove it and retry: " + str(self.update_lock)
                    ) from error
                if _process_running(owner.get("owner_pid")):
                    raise UpdateError(
                        "another update process is active; wait for it, then run: "
                        "villani update status"
                    )
                try:
                    self.update_lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            try:
                with os.fdopen(
                    descriptor, "w", encoding="utf-8", newline="\n"
                ) as handle:
                    json.dump(
                        {
                            "schema_version": "villani.update_lock.v1",
                            "owner_pid": os.getpid(),
                            "token": token,
                            "repositories_modified": False,
                        },
                        handle,
                        sort_keys=True,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                self.update_lock.unlink(missing_ok=True)
                raise
            return token
        raise UpdateError("the stale update lock could not be recovered; run: villani doctor")

    def _release_update_lock(self, token: str) -> None:
        try:
            owner = _json(self.update_lock)
        except UpdateError:
            return
        if owner.get("token") == token and owner.get("owner_pid") == os.getpid():
            self.update_lock.unlink(missing_ok=True)

    def policy(self) -> UpdatePolicy:
        if not self.policy_path.is_file():
            return UpdatePolicy()
        try:
            return UpdatePolicy.model_validate(_json(self.policy_path))
        except ValidationError as error:
            raise UpdateError(f"update policy is invalid: {error}") from error

    def set_policy(
        self,
        channel: str,
        *,
        pinned_version: str | None = None,
        feed_url: str | None = None,
        checks_enabled: bool | None = None,
    ) -> UpdatePolicy:
        current = self.policy()
        try:
            policy = UpdatePolicy(
                channel=channel,  # type: ignore[arg-type]
                pinned_version=pinned_version,
                feed_url=current.feed_url if feed_url is None else feed_url,
                checks_enabled=(
                    current.checks_enabled if checks_enabled is None else checks_enabled
                ),
            )
        except ValidationError as error:
            raise UpdateError(str(error)) from error
        self.home.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.policy_path, policy.model_dump(mode="json"))
        previous = self.status()
        self._write_state(previous.model_copy(update={"policy": policy}))
        return policy

    def status(self) -> UpdateState:
        if not self._transaction_active:
            self.recover_interrupted_update()
        policy = self.policy()
        if not self.state_path.is_file():
            return UpdateState(
                installed_version=self.version,
                policy=policy,
                active_installation=str(self.current) if self.current.is_dir() else None,
                evidence_path=str(self.state_path),
            )
        try:
            value = UpdateState.model_validate(_json(self.state_path))
        except ValidationError as error:
            raise UpdateError(f"update state is invalid: {error}") from error
        return value.model_copy(update={"policy": policy})

    def _managed_path(self, value: object) -> Path | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            path = Path(value).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        return path if path.is_relative_to(self.home) else None

    @staticmethod
    def _write_journal(path: Path, value: Mapping[str, Any]) -> None:
        write_json_atomic(path, dict(value))

    def recover_interrupted_update(self) -> bool:
        """Fail closed after a crash during the atomic switch and restore backup state."""

        if self._transaction_active or not self.transactions.is_dir():
            return False
        recovered = False
        for transaction in sorted(self.transactions.iterdir()):
            journal_path = transaction / "transaction.json"
            if not journal_path.is_file():
                continue
            try:
                journal = _json(journal_path)
            except UpdateError:
                continue
            phase = journal.get("phase")
            if phase in {"complete", "rolled_back", "recovered", "aborted"}:
                continue
            if _process_running(journal.get("owner_pid")):
                continue
            previous = self._managed_path(journal.get("previous_installation"))
            backup = self._managed_path(journal.get("configuration_backup"))
            failed: Path | None = None
            if self.current.is_dir() and phase in {
                "previous_saved",
                "activated",
                "verifying",
            }:
                self.failures.mkdir(parents=True, exist_ok=True)
                failed = self.failures / f"interrupted-{transaction.name}"
                if failed.exists():
                    raise UpdateError(
                        f"interrupted update recovery target already exists: {failed}"
                    )
                os.replace(self.current, failed)
            if previous is not None and previous.is_dir() and not self.current.exists():
                os.replace(previous, self.current)
            if backup is not None and backup.is_file():
                configuration = self.home / "config.yaml"
                restore = configuration.with_suffix(".yaml.recovery")
                shutil.copy2(backup, restore)
                os.replace(restore, configuration)
            journal.update(
                {
                    "phase": "recovered",
                    "recovered_at": utc_now().isoformat(),
                    "repositories_modified": False,
                }
            )
            self._write_journal(journal_path, journal)
            installed = self.version
            manifest_path = self.current / "package-manifest.json"
            if manifest_path.is_file():
                try:
                    installed = str(_json(manifest_path).get("version") or installed)
                except UpdateError:
                    pass
            self._write_state(
                UpdateState(
                    installed_version=installed,
                    policy=self.policy(),
                    status="failed",
                    active_installation=(str(self.current) if self.current.is_dir() else None),
                    configuration_backup=str(backup) if backup else None,
                    evidence_path=str(journal_path),
                    error=(
                        "An interrupted update was rolled back. "
                        "Run: villani doctor --installation-only"
                    ),
                )
            )
            recovered = True
        return recovered

    def _write_state(self, state: UpdateState) -> UpdateState:
        self.home.mkdir(parents=True, exist_ok=True)
        selected = state.model_copy(update={"evidence_path": str(self.state_path)})
        write_json_atomic(self.state_path, selected.model_dump(mode="json"))
        return selected

    @staticmethod
    def _read_location(location: str, *, limit: int) -> bytes:
        direct = Path(location).expanduser()
        if direct.is_absolute():
            try:
                if direct.stat().st_size > limit:
                    raise UpdateError("update resource exceeds the configured size limit")
                return direct.read_bytes()
            except OSError as error:
                raise UpdateError(f"update resource is unreadable: {error}") from error
        parsed = urllib.parse.urlsplit(location)
        if parsed.scheme in {"", "file"}:
            path = (
                Path(urllib.request.url2pathname(parsed.path))
                if parsed.scheme == "file"
                else Path(location)
            )
            try:
                if path.stat().st_size > limit:
                    raise UpdateError("update resource exceeds the configured size limit")
                return path.read_bytes()
            except OSError as error:
                raise UpdateError(f"update resource is unreadable: {error}") from error
        if parsed.scheme not in {"https", "http"}:
            raise UpdateError("update locations must use HTTPS, loopback HTTP, or a local file")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise UpdateError("unencrypted update checks are allowed only on loopback")
        request = urllib.request.Request(
            location,
            headers={
                "Accept": "application/json, application/zip",
                "User-Agent": f"Villani/{installed_version()} update-client",
            },
            method="GET",
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=30
            ) as response:
                data = response.read(limit + 1)
        except (OSError, urllib.error.URLError) as error:
            raise UpdateError(f"update request failed: {error}") from error
        if len(data) > limit:
            raise UpdateError("update resource exceeds the configured size limit")
        return data

    @staticmethod
    def _resolve_artifact_url(feed_url: str, artifact_url: str) -> str:
        parsed = urllib.parse.urlsplit(feed_url)
        if urllib.parse.urlsplit(artifact_url).scheme:
            return artifact_url
        if parsed.scheme in {"http", "https", "file"}:
            return urllib.parse.urljoin(feed_url, artifact_url)
        return str((Path(feed_url).expanduser().resolve().parent / artifact_url).resolve())

    def _migration_preview(self) -> MigrationPreview:
        try:
            report = check_upgrade(self.home, apply=False)
        except MigrationError as error:
            raise UpdateError(f"migration preflight failed: {error}") from error
        actions: list[str] = []
        after = report.spool_version_after
        if report.spool_version_before is not None and report.spool_version_before < SUPPORTED_SPOOL_VERSION:
            after = SUPPORTED_SPOOL_VERSION
            actions.append(
                f"Back up and migrate the local spool from v{report.spool_version_before} to v{SUPPORTED_SPOOL_VERSION}."
            )
        return MigrationPreview(
            configuration_version=report.config_version,
            spool_version_before=report.spool_version_before,
            spool_version_after=after,
            checked_run_bundles=report.checked_run_bundles,
            protocol_majors=list(report.protocol_majors),
            actions=actions,
            configuration_backup_required=(self.home / "config.yaml").is_file(),
        )

    def migration_preview(self) -> MigrationPreview:
        return self._migration_preview()

    def check(self, *, feed_url: str | None = None) -> UpdateState:
        policy = self.policy()
        if not policy.checks_enabled:
            raise UpdateError("update checks are disabled; enable them with: villani update channel")
        selected_url = feed_url or policy.feed_url
        if not selected_url:
            raise UpdateError("no update feed is configured; run: villani update channel stable --feed URL")
        try:
            feed = UpdateFeed.model_validate_json(
                self._read_location(selected_url, limit=MAX_FEED_BYTES)
            )
        except ValidationError as error:
            raise UpdateError(f"update feed is invalid: {error}") from error
        system = _system()
        architecture = _architecture()
        candidates: list[tuple[Version, UpdateRelease, UpdateArtifact]] = []
        for release in feed.releases:
            if policy.channel == "stable" and release.channel != "stable":
                continue
            if policy.channel == "pinned" and release.version != policy.pinned_version:
                continue
            if not (
                release.minimum_config_version
                <= SUPPORTED_CONFIG_VERSION
                <= release.maximum_config_version
            ):
                continue
            artifact = next(
                (
                    item
                    for item in release.artifacts
                    if item.operating_system == system
                    and _architecture(item.architecture) == architecture
                ),
                None,
            )
            if artifact is None:
                continue
            try:
                candidates.append((Version(release.version), release, artifact))
            except InvalidVersion as error:
                raise UpdateError(f"update feed has invalid version {release.version!r}") from error
        if not candidates:
            raise UpdateError(
                f"no {policy.channel} release is certified for {system}/{architecture} and config v{SUPPORTED_CONFIG_VERSION}"
            )
        _parsed, release, artifact = max(candidates, key=lambda item: item[0])
        try:
            current_version = Version(self.version)
            candidate_version = Version(release.version)
        except InvalidVersion as error:
            raise UpdateError(f"installed version is invalid: {self.version}") from error
        status = "current" if candidate_version == current_version else "available"
        return self._write_state(
            UpdateState(
                installed_version=self.version,
                policy=policy,
                status=status,
                available_version=release.version,
                last_checked_at=utc_now(),
                release_notes=release.release_notes,
                artifact_url=self._resolve_artifact_url(selected_url, artifact.url),
                artifact_sha256=artifact.sha256,
                migration_preview=self._migration_preview(),
                active_installation=str(self.current) if self.current.is_dir() else None,
                previous_installation=self.status().previous_installation,
                configuration_backup=self.status().configuration_backup,
                evidence_path=str(self.state_path),
            )
        )

    @staticmethod
    def _safe_member(info: zipfile.ZipInfo) -> PurePosixPath:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise UpdateError(f"unsafe archive member: {info.filename}")
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise UpdateError(f"symbolic links are not allowed in update archives: {info.filename}")
        return path

    def _extract_verified(self, archive_path: Path, destination: Path) -> PackageManifest:
        total = 0
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
                for info in archive.infolist():
                    path = self._safe_member(info)
                    total += info.file_size
                    if total > MAX_UPDATE_BYTES:
                        raise UpdateError("expanded update exceeds the configured size limit")
                    members.append((info, path))
                destination.mkdir(parents=True, exist_ok=False)
                for info, relative in members:
                    target = destination.joinpath(*relative.parts)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    mode = (info.external_attr >> 16) & 0o777
                    if mode:
                        target.chmod(mode)
        except (OSError, zipfile.BadZipFile) as error:
            raise UpdateError(f"update archive cannot be extracted: {error}") from error
        manifest_path = destination / "package-manifest.json"
        try:
            manifest = PackageManifest.model_validate(_json(manifest_path))
        except ValidationError as error:
            raise UpdateError(f"package manifest is invalid: {error}") from error
        if manifest.operating_system != _system() or _architecture(manifest.architecture) != _architecture():
            raise UpdateError(
                f"artifact targets {manifest.operating_system}/{manifest.architecture}, not {_system()}/{_architecture()}"
            )
        self._verify_manifest_files(destination, manifest)
        return manifest

    @staticmethod
    def _manifest_relative(value: str, *, label: str) -> PurePosixPath:
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise UpdateError(f"unsafe {label} path: {value}")
        return relative

    def _verify_manifest_files(
        self,
        destination: Path,
        manifest: PackageManifest,
    ) -> None:
        declared: set[str] = set()
        for item in manifest.files:
            relative = self._manifest_relative(item.path, label="package-manifest")
            canonical = relative.as_posix()
            if canonical in declared:
                raise UpdateError(f"duplicate package-manifest path: {canonical}")
            declared.add(canonical)
            path = destination.joinpath(*relative.parts)
            if not path.is_file():
                raise UpdateError(f"package member is missing: {item.path}")
            if path.stat().st_size != item.size_bytes or _sha256(path) != item.sha256:
                raise UpdateError(f"package member verification failed: {item.path}")
            if item.executable and os.name != "nt":
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        actual = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file() and path.name != "package-manifest.json"
        }
        if actual != declared:
            extra = sorted(actual - declared)
            missing = sorted(declared - actual)
            raise UpdateError(
                f"package contents differ from the manifest; extra={extra}, missing={missing}"
            )
        sbom_relative = self._manifest_relative(manifest.sbom_path, label="SBOM")
        notes_relative = self._manifest_relative(
            manifest.release_notes_path, label="release-notes"
        )
        if sbom_relative.as_posix() not in declared or not destination.joinpath(*sbom_relative.parts).is_file():
            raise UpdateError("package SBOM is missing")
        if notes_relative.as_posix() not in declared or not destination.joinpath(*notes_relative.parts).is_file():
            raise UpdateError("release notes are missing")
        extension = ".exe" if os.name == "nt" else ""
        for command in COMMANDS:
            if not (destination / f"{command}{extension}").is_file():
                raise UpdateError(f"package command is missing: {command}{extension}")

    def _download(self, location: str, expected_sha256: str) -> Path:
        data = self._read_location(location, limit=MAX_UPDATE_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256:
            raise UpdateError(
                f"artifact SHA-256 mismatch: expected {expected_sha256}, observed {digest}"
            )
        self.downloads.mkdir(parents=True, exist_ok=True)
        destination = self.downloads / f"{digest}.zip"
        if not destination.is_file():
            temporary = destination.with_suffix(".zip.pending")
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        return destination

    def _backup_configuration(self, transaction_id: str) -> Path | None:
        source = self.home / "config.yaml"
        if not source.is_file():
            return None
        destination = self.home / "config-backups" / f"config-before-{transaction_id}.yaml"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as input_handle, destination.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        return destination

    @staticmethod
    def _atomic_text(path: Path, payload: str, *, newline: str) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.pending")
        with temporary.open("x", encoding="utf-8", newline=newline) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _windows_runner(self) -> Path:
        manifest = self.current / "package-manifest.json"
        if not manifest.is_file():
            raise UpdateError("active package manifest is missing; run: villani doctor --installation-only")
        runner_id = _sha256(manifest)[:20]
        runner = self.runners / runner_id
        if runner.is_dir():
            for command in COMMANDS:
                source = self.current / f"{command}.exe"
                existing = runner / source.name
                if not existing.is_file() or _sha256(existing) != _sha256(source):
                    raise UpdateError(
                        "Windows command runner verification failed; run: "
                        "villani doctor --installation-only"
                    )
            return runner
        self.runners.mkdir(parents=True, exist_ok=True)
        staged = self.runners / f".{runner_id}.{secrets.token_hex(4)}.pending"
        staged.mkdir(parents=False, exist_ok=False)
        try:
            for command in COMMANDS:
                source = self.current / f"{command}.exe"
                destination = staged / source.name
                shutil.copy2(source, destination)
                if _sha256(destination) != _sha256(source):
                    raise UpdateError(f"Windows command runner copy failed verification: {command}")
            os.replace(staged, runner)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        return runner

    def _write_launchers(self) -> None:
        self.launchers.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            runner = self._windows_runner()
            for command in COMMANDS:
                path = self.launchers / f"{command}.cmd"
                payload = (
                    "@echo off\r\n"
                    f'"%~dp0..\\runners\\{runner.name}\\{command}.exe" %*\r\n'
                )
                self._atomic_text(path, payload, newline="")
            return
        for command in COMMANDS:
            path = self.launchers / command
            payload = (
                "#!/bin/sh\n"
                'VILLANI_INSTALL_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                f'exec "$VILLANI_INSTALL_ROOT/current/{command}" "$@"\n'
            )
            self._atomic_text(path, payload, newline="\n")
            path.chmod(0o755)

    def _require_safe_windows_invocation(self) -> None:
        if os.name != "nt" or not self.current.is_dir() or not getattr(sys, "frozen", False):
            return
        try:
            running = Path(sys.executable).resolve()
        except OSError:
            return
        if running.is_relative_to(self.current):
            raise UpdateError(
                "Windows cannot atomically switch a directory containing this running binary. "
                f"Run: {self.launchers / 'villani.cmd'} update install"
            )

    def _verify_started_installation(self, root: Path, version: str) -> tuple[bool, str]:
        executable = root / f"villani{'.exe' if os.name == 'nt' else ''}"
        environment = dict(os.environ)
        environment["VILLANI_HOME"] = str(self.home)
        try:
            identified = subprocess.run(
                [str(executable), "--version"],
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=30,
                check=False,
            )
            diagnosed = subprocess.run(
                [str(executable), "doctor", "--json", "--installation-only"],
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"startup verification could not execute: {error}"
        if identified.returncode != 0 or version not in identified.stdout:
            return False, "the activated binary did not report the expected version"
        if diagnosed.returncode != 0:
            return False, "installation-only doctor verification failed"
        try:
            report = json.loads(diagnosed.stdout)
        except json.JSONDecodeError:
            return False, "installation-only doctor returned malformed JSON"
        if not isinstance(report, dict) or report.get("healthy") is not True:
            return False, "installation-only doctor did not report healthy"
        return True, "startup and installation-only doctor verification passed"

    def install_artifact(
        self,
        location: str,
        expected_sha256: str,
        *,
        expected_version: str | None = None,
        verifier: Callable[[Path, str], tuple[bool, str]] | None = None,
    ) -> UpdateState:
        if self._transaction_active:
            raise UpdateError("an update transaction is already active")
        token = self._acquire_update_lock()
        try:
            self.recover_interrupted_update()
            self._transaction_active = True
            try:
                return self._install_artifact(
                    location,
                    expected_sha256,
                    expected_version=expected_version,
                    verifier=verifier,
                )
            finally:
                self._transaction_active = False
        finally:
            self._release_update_lock(token)

    def _install_artifact(
        self,
        location: str,
        expected_sha256: str,
        *,
        expected_version: str | None = None,
        verifier: Callable[[Path, str], tuple[bool, str]] | None = None,
    ) -> UpdateState:
        self._require_safe_windows_invocation()
        if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
            raise UpdateError("--sha256 must be one lowercase 64-character SHA-256 digest")
        prior_state = self.status()
        preview = self._migration_preview()
        transaction_id = utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
        transaction = self.transactions / transaction_id
        staged = transaction / "staged"
        archive = self._download(location, expected_sha256)
        manifest = self._extract_verified(archive, staged)
        if expected_version is not None and manifest.version != expected_version:
            raise UpdateError(
                f"artifact version {manifest.version} does not match expected {expected_version}"
            )
        backup = self._backup_configuration(transaction_id)
        previous: Path | None = None
        self.installations.mkdir(parents=True, exist_ok=True)
        self.failures.mkdir(parents=True, exist_ok=True)
        previous_target = self.installations / f"previous-{transaction_id}"
        journal_path = transaction / "transaction.json"
        journal: dict[str, Any] = {
            "schema_version": "villani.update_transaction.v1",
            "transaction_id": transaction_id,
            "owner_pid": os.getpid(),
            "phase": "prepared",
            "version": manifest.version,
            "previous_installation": str(previous_target) if self.current.exists() else None,
            "configuration_backup": str(backup) if backup else None,
            "repositories_modified": False,
        }
        self._write_journal(journal_path, journal)
        if self.current.exists():
            previous = previous_target
            os.replace(self.current, previous)
            journal["phase"] = "previous_saved"
            self._write_journal(journal_path, journal)
        try:
            os.replace(staged, self.current)
        except OSError as error:
            if previous is not None and previous.exists() and not self.current.exists():
                os.replace(previous, self.current)
            raise UpdateError(f"atomic activation failed: {error}") from error
        journal["phase"] = "activated"
        self._write_journal(journal_path, journal)
        pending_state = UpdateState(
            installed_version=manifest.version,
            policy=self.policy(),
            status="installing",
            available_version=manifest.version,
            artifact_url=location,
            artifact_sha256=expected_sha256,
            migration_preview=preview,
            active_installation=str(self.current),
            previous_installation=str(previous) if previous else None,
            configuration_backup=str(backup) if backup else None,
            evidence_path=str(self.state_path),
        )
        self._write_state(pending_state)
        journal["phase"] = "verifying"
        self._write_journal(journal_path, journal)
        verification = verifier or self._verify_started_installation
        verified, evidence = verification(self.current, manifest.version)
        if not verified:
            failed = self.failures / f"failed-{transaction_id}"
            os.replace(self.current, failed)
            if previous is not None and previous.exists():
                os.replace(previous, self.current)
            if backup is not None and backup.is_file():
                config = self.home / "config.yaml"
                restore = config.with_suffix(".yaml.rollback")
                shutil.copy2(backup, restore)
                os.replace(restore, config)
            state = pending_state.model_copy(
                update={
                    "status": "failed",
                    "installed_version": self.version,
                    "active_installation": str(self.current) if self.current.exists() else None,
                    "previous_installation": None,
                    "error": evidence,
                }
            )
            self._write_state(state)
            journal.update({"phase": "rolled_back", "error": evidence})
            self._write_journal(journal_path, journal)
            raise UpdateError(f"update verification failed and was rolled back: {evidence}")
        self._write_launchers()
        result = self._write_state(
            pending_state.model_copy(
                update={
                    "status": "verified",
                    "last_checked_at": utc_now(),
                    "release_notes": prior_state.release_notes,
                    "error": None,
                }
            )
        )
        journal.update({"phase": "complete", "completed_at": utc_now().isoformat()})
        self._write_journal(journal_path, journal)
        return result

    def install_checked_update(
        self,
        *,
        verifier: Callable[[Path, str], tuple[bool, str]] | None = None,
    ) -> UpdateState:
        state = self.status()
        if state.status not in {"available", "downloaded"}:
            raise UpdateError("no checked update is available; run: villani update check")
        if not state.artifact_url or not state.artifact_sha256 or not state.available_version:
            raise UpdateError("checked update state is incomplete; run: villani update check")
        return self.install_artifact(
            state.artifact_url,
            state.artifact_sha256,
            expected_version=state.available_version,
            verifier=verifier,
        )

    def rollback(
        self,
        *,
        verifier: Callable[[Path, str], tuple[bool, str]] | None = None,
    ) -> UpdateState:
        if self._transaction_active:
            raise UpdateError("an update transaction is already active")
        token = self._acquire_update_lock()
        try:
            self._transaction_active = True
            try:
                return self._rollback(verifier=verifier)
            finally:
                self._transaction_active = False
        finally:
            self._release_update_lock(token)

    def _rollback(
        self,
        *,
        verifier: Callable[[Path, str], tuple[bool, str]] | None = None,
    ) -> UpdateState:
        self._require_safe_windows_invocation()
        state = self.status()
        previous_value = state.previous_installation
        if not previous_value:
            raise UpdateError("no previous installation is available for rollback")
        previous = Path(previous_value).expanduser().resolve()
        if not previous.is_relative_to(self.home) or not previous.is_dir():
            raise UpdateError("previous installation is outside the managed Villani home")
        try:
            manifest = PackageManifest.model_validate(_json(previous / "package-manifest.json"))
        except ValidationError as error:
            raise UpdateError(f"previous package manifest is invalid: {error}") from error
        self._verify_manifest_files(previous, manifest)
        transaction_id = utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
        displaced = self.installations / f"rolled-forward-{transaction_id}"
        os.replace(self.current, displaced)
        try:
            os.replace(previous, self.current)
        except OSError:
            os.replace(displaced, self.current)
            raise
        verification = verifier or self._verify_started_installation
        verified, evidence = verification(self.current, manifest.version)
        if not verified:
            os.replace(self.current, previous)
            os.replace(displaced, self.current)
            raise UpdateError(f"rollback verification failed; current version was restored: {evidence}")
        if state.configuration_backup:
            backup = Path(state.configuration_backup).expanduser().resolve()
            if backup.is_relative_to(self.home) and backup.is_file():
                restore = (self.home / "config.yaml").with_suffix(".yaml.rollback")
                shutil.copy2(backup, restore)
                os.replace(restore, self.home / "config.yaml")
        self._write_launchers()
        return self._write_state(
            state.model_copy(
                update={
                    "status": "rolled_back",
                    "installed_version": manifest.version,
                    "available_version": None,
                    "active_installation": str(self.current),
                    "previous_installation": str(displaced),
                    "configuration_backup": None,
                    "error": None,
                }
            )
        )


__all__ = ["UpdateError", "UpdateManager", "installed_version"]
