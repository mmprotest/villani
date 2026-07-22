#!/usr/bin/env python3
"""Cross-platform, fail-closed Villani packaged release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import venv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import supply_chain

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "release-verification" / "artifacts" / "latest"
PYTHON_COMPONENTS = (
    "villani-ops",
    "villani-code",
    "villani-agentd",
    "villani-control-plane",
    "villani",
)
NODE_COMPONENTS = (
    "villani-ui",
    "villani-run-model",
    "villani-web",
    "villani-flight-recorder",
)
ASSET_RE = re.compile(r"(?:src|href)=[\"']([^\"'#?]+)")
IMPORT_RE = re.compile(
    r"(?:\bfrom\s*|\bimport\s*\(|\brequire\s*\()\s*[\"']([^\"']+)[\"']"
    r"|\bimport\s*[\"']([^\"']+)[\"']"
)
SOURCE_SUFFIXES = frozenset({".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"})
EXCLUDED_SOURCE_DIRECTORIES = frozenset(
    {
        ".cache",
        ".eggs",
        ".git",
        ".hypothesis",
        ".mypy_cache",
        ".next",
        ".nox",
        ".npm",
        ".onboarding-temp",
        ".pip-cache",
        ".pyright",
        ".nyc_output",
        ".parcel-cache",
        ".pytest_cache",
        ".ruff_cache",
        ".test-temp",
        ".tox",
        ".turbo",
        ".venv",
        ".vite",
        "__pycache__",
        "artifacts",
        "build",
        "coverage",
        "dist",
        "dist-model",
        "env",
        "htmlcov",
        "node_modules",
        "out",
        "pip-wheel-metadata",
        "playwright-report",
        "site",
        "temp",
        "test-results",
        "tmp",
        "venv",
        "wheelhouse",
    }
)
EXCLUDED_DATABASE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})
COMMAND_RECORDS: list[dict[str, Any]] = []
PHASE_TIMEOUTS: dict[str, int] = {
    "source_isolation": 180,
    "compatibility": 120,
    "node_package_build": 1_800,
    "python_package_build": 1_200,
    "packed_node_install": 600,
    "wheel_install": 600,
    "cli_agent_mode": 1_800,
    "connected_runtime_preparation": 900,
    "installed_user_onboarding": 1_800,
    "connected_scenarios": 2_400,
    "canonical_reconciliation": 120,
    "browser": 180,
    "screenshots": 120,
    "redaction": 120,
    "postgresql": 180,
    "verifier_routing": 120,
    "candidate_diversity": 120,
    "classification_adjustment": 120,
    "supply_chain": 900,
    "final_evidence_validation": 180,
}
PHASE_STATUSES = frozenset(
    {"pending", "running", "passed", "failed", "timed_out", "not_applicable"}
)
_ACTIVE_REPORTER: Any | None = None


def _environment_paths(env: dict[str, str] | None) -> dict[str, str]:
    values = env or os.environ
    keys = (
        "HOME",
        "PATH",
        "PIP_CACHE_DIR",
        "PLAYWRIGHT_BROWSERS_PATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "VIRTUAL_ENV",
        "npm_config_cache",
    )
    return {key: values[key] for key in keys if values.get(key)}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _remove_tree(path: Path) -> None:
    """Remove generated gate state, retrying read-only Windows Git objects."""

    def retry_writable(function: Any, value: str, error: Any) -> None:
        try:
            if os.path.islink(value):
                function(value)
                return
            current_mode = os.stat(value).st_mode
            os.chmod(value, current_mode | stat.S_IWUSR)
            function(value)
        except OSError:
            raise error[1]

    if path.exists():
        shutil.rmtree(path, onerror=retry_writable)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_source_manifest(root: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(paths):
        path = root / value
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8"))
        else:
            digest.update(b"file\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _digest_configuration(root: Path) -> tuple[str, list[str]]:
    inputs = [
        ".github/workflows/ci.yml",
        "release/component-compatibility.json",
        "release-verification/run_release_gate.py",
        "release-verification/connected_product.py",
        "onboarding-verification/run_onboarding_gate.py",
        "onboarding-verification/capture_screenshots.mjs",
    ]
    return _digest_source_manifest(root, inputs), inputs


def _git_value(arguments: list[str], *, allow_empty: bool = False) -> str | None:
    try:
        completed = subprocess.run(
            [shutil.which("git") or "git", *arguments],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    if completed.returncode != 0:
        return None
    return value if value or allow_empty else None


def _certification_identity(mode: str) -> dict[str, Any]:
    commit = _git_value(["rev-parse", "HEAD"])
    branch = (
        os.environ.get("GITHUB_HEAD_REF")
        or os.environ.get("GITHUB_REF_NAME")
        or _git_value(["branch", "--show-current"])
        or "detached"
    )
    status = _git_value(
        ["status", "--porcelain", "--untracked-files=all"], allow_empty=True
    )
    if commit is None or status is None:
        raise RuntimeError("could not record the checked-out Git revision and status")
    node = subprocess.run(
        [shutil.which("node") or "node", "--version"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    configuration_digest, configuration_inputs = _digest_configuration(ROOT)
    hosted = os.environ.get("GITHUB_ACTIONS") == "true"
    identity: dict[str, Any] = {
        "git_commit_sha": commit,
        "branch": branch,
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
        "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "workflow_name": os.environ.get("GITHUB_WORKFLOW"),
        "workflow_job": os.environ.get("GITHUB_JOB"),
        "operating_system": platform.platform(),
        "python_version": platform.python_version(),
        "node_version": node.stdout.strip() if node.returncode == 0 else None,
        "mode": mode,
        "hosted_ci": hosted,
        "authoritative_hosted_ci": hosted and mode == "ci",
        "working_tree_clean": status == "",
        "source_manifest_sha256": None,
        "configuration_sha256": configuration_digest,
        "configuration_inputs": configuration_inputs,
        "package_versions": {},
    }
    expected = os.environ.get("GITHUB_SHA")
    if hosted and expected and commit != expected:
        raise RuntimeError(
            f"checked-out commit {commit!r} does not match GITHUB_SHA {expected!r}"
        )
    if hosted and status:
        raise RuntimeError(
            "hosted release certification requires a clean checkout; "
            f"unexpected changes: {status.splitlines()[:20]}"
        )
    return identity


def _write_artifact_manifest(identity: dict[str, Any]) -> None:
    artifacts: list[dict[str, Any]] = []
    manifest = LATEST / "release-artifact-manifest.json"
    for path in sorted(item for item in LATEST.rglob("*") if item.is_file()):
        if path == manifest:
            continue
        artifacts.append(
            {
                "path": path.relative_to(LATEST).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    write_json(
        manifest,
        {
            "status": "passed"
            if all(item.get("sha256") for item in artifacts)
            else "failed",
            "certification_identity": identity,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        },
    )


def run(
    command: list[str],
    *,
    cwd: Path,
    log: Path,
    env: dict[str, str] | None = None,
    timeout: int = 1_800,
) -> subprocess.CompletedProcess[str]:
    if _ACTIVE_REPORTER is not None:
        remaining = _ACTIVE_REPORTER.remaining_timeout()
        if remaining is not None:
            timeout = max(1, min(timeout, remaining))
    started = time.monotonic()
    record: dict[str, Any] = {
        "command": command,
        "cwd": str(cwd.resolve()),
        "executable": shutil.which(command[0], path=(env or os.environ).get("PATH"))
        or command[0],
        "environment_paths": _environment_paths(env),
        "log": str(log.resolve()),
        "timeout_seconds": timeout,
    }
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        elapsed = round(time.monotonic() - started, 3)
        record.update(
            {
                "status": "timed_out",
                "error": "TimeoutExpired",
                "failure_type": "subprocess_timeout",
                "failure_message": f"command exceeded {timeout} seconds",
                "elapsed_seconds": elapsed,
                "process_status": "terminated_after_timeout",
                "partial_stdout_bytes": len(stdout.encode("utf-8")),
                "partial_stderr_bytes": len(stderr.encode("utf-8")),
            }
        )
        COMMAND_RECORDS.append(record)
        log.write_text(
            "$ "
            + subprocess.list2cmdline(command)
            + f"\ncwd: {cwd.resolve()}\n"
            + f"timeout seconds: {timeout}\n"
            + f"elapsed seconds: {elapsed}\n"
            + "process status: terminated_after_timeout\n"
            + "environment paths: "
            + json.dumps(record["environment_paths"], sort_keys=True)
            + "\n\n[partial stdout]\n"
            + stdout
            + "\n[partial stderr]\n"
            + stderr,
            encoding="utf-8",
        )
        if _ACTIVE_REPORTER is not None:
            _ACTIVE_REPORTER.persist()
        raise
    except Exception as error:
        elapsed = round(time.monotonic() - started, 3)
        record.update(
            {
                "status": "failed",
                "error": type(error).__name__,
                "failure_type": "subprocess_start_failure",
                "failure_message": str(error),
                "elapsed_seconds": elapsed,
                "process_status": "not_completed",
            }
        )
        COMMAND_RECORDS.append(record)
        log.write_text(
            "$ "
            + subprocess.list2cmdline(command)
            + f"\ncwd: {cwd.resolve()}\n"
            + f"elapsed seconds: {elapsed}\n"
            + "process status: not_completed\n"
            + "environment paths: "
            + json.dumps(record["environment_paths"], sort_keys=True)
            + f"\n\n{type(error).__name__}: {error}\n",
            encoding="utf-8",
        )
        if _ACTIVE_REPORTER is not None:
            _ACTIVE_REPORTER.persist()
        raise
    record.update(
        {
            "status": "passed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    )
    COMMAND_RECORDS.append(record)
    log.write_text(
        "$ "
        + subprocess.list2cmdline(command)
        + "\n"
        + f"cwd: {cwd.resolve()}\n"
        + "environment paths: "
        + json.dumps(record["environment_paths"], sort_keys=True)
        + "\n\n"
        + completed.stdout
        + completed.stderr,
        encoding="utf-8",
    )
    if completed.returncode:
        if _ACTIVE_REPORTER is not None:
            _ACTIVE_REPORTER.persist()
        raise RuntimeError(f"command failed ({completed.returncode}); see {log}")
    return completed


def _installed_entry_point(
    python: Path,
    command: str,
    *,
    root: Path,
    release_env: dict[str, str],
) -> dict[str, Any]:
    """Query the resolver installed in the exact wheel environment under test."""

    completed = run(
        [
            str(python),
            "-I",
            str(root / "scripts" / "resolve-installed-executable.py"),
            command,
        ],
        cwd=root,
        log=LATEST / "logs" / f"wheel-entrypoint-{command}-resolution.log",
        env=release_env,
        timeout=60,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"installed entry point resolver returned malformed output for {command!r}"
        ) from error
    if not isinstance(result, dict) or not result.get("path"):
        diagnostic = result.get("diagnostic") if isinstance(result, dict) else None
        raise RuntimeError(
            str(diagnostic or f"installed entry point is missing: {command}")
        )
    if result.get("source") not in {"interpreter_scripts", "interpreter_parent"}:
        raise RuntimeError(
            f"installed entry point {command!r} came from outside the selected "
            f"wheel environment: {result.get('diagnostic')}"
        )
    prefix = result.get("prefix")
    if (
        not isinstance(prefix, list)
        or not prefix
        or not all(isinstance(item, str) and item for item in prefix)
    ):
        raise RuntimeError(f"installed entry point prefix is invalid: {command}")
    return result


def _source_path_is_excluded(relative: Path) -> bool:
    lowered = tuple(part.lower() for part in relative.parts)
    if any(part in EXCLUDED_SOURCE_DIRECTORIES for part in lowered):
        return True
    if any(part.endswith(".egg-info") for part in lowered):
        return True
    if any(
        part.startswith(
            (
                ".m55-",
                ".npm-cache",
                ".onboarding-",
                ".release-",
                ".test-temp",
            )
        )
        or (part.startswith("root-") and part.endswith("-temp"))
        for part in lowered
    ):
        return True
    if relative.suffix.lower() in EXCLUDED_DATABASE_SUFFIXES:
        return True
    name = relative.name.lower()
    if name in {
        ".coverage",
        ".ds_store",
        ".env",
        "coverage.xml",
        "desktop.ini",
        "thumbs.db",
    }:
        return True
    if name.startswith((".coverage.", ".env.")) and not name.endswith(".example"):
        return True
    if name.endswith(
        (
            ".bak",
            ".cover",
            ".log",
            ".pid",
            ".pid.lock",
            ".pyc",
            ".pyo",
            ".swo",
            ".swp",
            ".temp",
            ".tmp",
            ".tsbuildinfo",
        )
    ):
        if name.endswith(".log") and lowered[:2] == ("integration", "fixtures"):
            return False
        return True
    if name.endswith("~"):
        return True
    return False


def _git_source_manifest(source: Path) -> list[str] | None:
    git = shutil.which("git") or "git"
    try:
        probe = subprocess.run(
            [git, "-c", "core.quotePath=false", "rev-parse", "--show-toplevel"],
            cwd=source,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    try:
        top_level = Path(probe.stdout.strip()).resolve()
    except OSError:
        return None
    if top_level != source.resolve():
        return None
    try:
        listed = subprocess.run(
            [
                git,
                "-c",
                "core.quotePath=false",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            cwd=source,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    log = LATEST / "logs" / "isolated-source-manifest.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "$ "
        + subprocess.list2cmdline(listed.args)
        + f"\ncwd: {source.resolve()}\n\n"
        + listed.stdout.replace("\0", "\n")
        + listed.stderr,
        encoding="utf-8",
    )
    if listed.returncode != 0:
        return None
    return sorted(value for value in listed.stdout.split("\0") if value)


def _filesystem_inventory(
    source: Path,
) -> tuple[list[str], dict[str, str], set[str]]:
    """Return deterministic candidate paths, exclusions, and virtualenv roots."""

    candidates: list[str] = []
    excluded: dict[str, str] = {}
    virtual_environments: set[str] = set()
    for current, directory_names, file_names in os.walk(
        source, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        relative_current = current_path.relative_to(source)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            relative = relative_current / name
            normalized = relative.as_posix()
            absolute = current_path / name
            if _source_path_is_excluded(relative):
                excluded[normalized] = "excluded_directory"
                continue
            if (absolute / "pyvenv.cfg").is_file():
                excluded[normalized] = "python_virtual_environment"
                virtual_environments.add(normalized)
                continue
            if absolute.is_symlink():
                candidates.append(normalized)
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in sorted(file_names):
            relative = relative_current / name
            normalized = relative.as_posix()
            if _source_path_is_excluded(relative):
                excluded[normalized] = "excluded_file"
            else:
                candidates.append(normalized)
    return sorted(set(candidates)), excluded, virtual_environments


def create_isolated_source(source: Path, destination: Path) -> dict[str, Any]:
    """Copy only release source inputs into a dependency-free checkout image."""
    source = source.resolve()
    destination = destination.resolve()
    if destination == source or destination.is_relative_to(source):
        raise RuntimeError(
            "isolated source destination must be outside the source root"
        )
    filesystem_paths, exclusion_reasons, virtual_environments = _filesystem_inventory(
        source
    )
    git_paths = _git_source_manifest(source)
    selection_method = (
        "git_manifest" if git_paths is not None else "filesystem_manifest"
    )
    selected = git_paths if git_paths is not None else filesystem_paths
    selected_set = {Path(value).as_posix() for value in selected}
    if git_paths is not None:
        for normalized in filesystem_paths:
            if normalized not in selected_set:
                exclusion_reasons.setdefault(normalized, "not_in_git_manifest")
    destination.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
    for value in sorted(selected_set):
        relative = Path(value)
        source_path = source / relative
        if not source_path.is_file() and not source_path.is_symlink():
            continue
        normalized = relative.as_posix()
        if _source_path_is_excluded(relative):
            exclusion_reasons.setdefault(normalized, "excluded_file")
            continue
        if source_path.is_symlink():
            target = source_path.resolve(strict=False)
            if not target.is_relative_to(source):
                raise RuntimeError(
                    f"source symlink escapes the source root: {normalized} -> {os.readlink(source_path)}"
                )
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_symlink():
            source_target = source_path.resolve(strict=False)
            destination_target = destination / source_target.relative_to(source)
            safe_target = os.path.relpath(destination_target, destination_path.parent)
            destination_path.symlink_to(
                safe_target, target_is_directory=source_target.is_dir()
            )
        else:
            shutil.copy2(source_path, destination_path)
        copied.append(normalized)
    forbidden = sorted(
        str(path.relative_to(destination)).replace("\\", "/")
        for path in destination.rglob("*")
        if _source_path_is_excluded(path.relative_to(destination))
    )
    if forbidden:
        raise RuntimeError(
            f"isolated source contains forbidden generated paths: {forbidden[:20]}"
        )
    report = {
        "status": "passed",
        "source_root": str(source.resolve()),
        "isolated_root": str(destination.resolve()),
        "source_selection": selection_method,
        "copied_file_count": len(copied),
        "excluded_file_count": len(exclusion_reasons),
        "copied_paths": sorted(copied),
        "excluded_paths": sorted(exclusion_reasons),
        "excluded_entries": [
            {"path": path, "reason": exclusion_reasons[path]}
            for path in sorted(exclusion_reasons)
        ],
        "virtual_environment_roots": sorted(virtual_environments),
        "forbidden_paths_present": [],
        "git_metadata_copied": False,
        "source_manifest_sha256": _digest_source_manifest(destination, copied),
    }
    write_json(LATEST / "isolated-source.json", report)
    return report


def component_versions(root: Path = ROOT) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in PYTHON_COMPONENTS:
        document = tomllib.loads(
            (root / "components" / name / "pyproject.toml").read_text(encoding="utf-8")
        )
        result[str(document["project"]["name"])] = str(document["project"]["version"])
    for name in NODE_COMPONENTS:
        document = json.loads(
            (root / "components" / name / "package.json").read_text(encoding="utf-8")
        )
        result[str(document["name"])] = str(document["version"])
    result["shared-protocol"] = "2"
    return result


def validate_compatibility(
    versions: dict[str, str], root: Path = ROOT
) -> dict[str, Any]:
    template = json.loads(
        (root / "release/component-compatibility.json").read_text(encoding="utf-8")
    )
    expected = template["components"]
    mismatches: dict[str, Any] = {
        name: {"manifest": expected.get(name), "package": version}
        for name, version in versions.items()
        if expected.get(name) != version
    }
    if set(expected) != set(versions):
        mismatches["component_set"] = {
            "manifest_only": sorted(set(expected) - set(versions)),
            "packages_only": sorted(set(versions) - set(expected)),
        }
    if (
        template["spool_schema_version"] != 4
        or template["alembic_head"] != "0a1b2c3d4e5f"
    ):
        mismatches["wire_contract"] = "unexpected spool version or Alembic head"
    minimum_python = tuple(
        int(value) for value in str(template["minimum_python"]).split(".")
    )
    if sys.version_info[:2] < minimum_python:
        mismatches["python"] = {
            "minimum": template["minimum_python"],
            "actual": platform.python_version(),
        }
    node = shutil.which("node")
    if not node:
        mismatches["node"] = "node executable is unavailable"
    else:
        actual_node = (
            subprocess.run(
                [node, "--version"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=True,
            )
            .stdout.strip()
            .lstrip("v")
        )
        if int(actual_node.split(".", 1)[0]) < int(template["node"]):
            mismatches["node"] = {"minimum": template["node"], "actual": actual_node}
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(
        platform.system()
    )
    if system not in template["supported_operating_systems"]:
        mismatches["platform"] = system or platform.system()
    if mismatches:
        raise RuntimeError(f"component compatibility mismatch: {mismatches}")
    return template


def validate_frontend_assets(root: Path = ROOT) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for application in ("villani-web",):
        application_root = root / "components" / application / "dist"
        html = application_root / "index.html"
        if not html.is_file():
            raise RuntimeError(f"{application} dist/index.html is missing")
        references = []
        for reference in ASSET_RE.findall(html.read_text(encoding="utf-8")):
            if reference.startswith(("http:", "https:", "data:", "mailto:")):
                continue
            target = application_root / reference.lstrip("/")
            references.append({"reference": reference, "exists": target.is_file()})
            if not target.is_file():
                raise RuntimeError(f"{html} references missing asset {reference}")
        reports.append(
            {"application": application, "html": str(html), "references": references}
        )
    return {"passed": True, "applications": reports}


def _package_name(specifier: str) -> str:
    if specifier.startswith("@"):
        return "/".join(specifier.split("/")[:2])
    return specifier.split("/", 1)[0]


def validate_node_boundaries(root: Path) -> dict[str, Any]:
    """Reject source imports outside a component or outside its manifest."""
    packages: list[dict[str, Any]] = []
    violations: list[dict[str, str]] = []
    for name in NODE_COMPONENTS:
        component = (root / "components" / name).resolve()
        manifest = json.loads((component / "package.json").read_text(encoding="utf-8"))
        declared = set()
        local_dependencies: dict[str, str] = {}
        for section in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            for dependency, specification in manifest.get(section, {}).items():
                declared.add(dependency)
                if isinstance(specification, str) and specification.startswith("file:"):
                    target = (component / specification.removeprefix("file:")).resolve()
                    target_manifest = json.loads(
                        (target / "package.json").read_text(encoding="utf-8")
                    )
                    if target_manifest.get("name") != dependency:
                        violations.append(
                            {
                                "component": name,
                                "file": "package.json",
                                "specifier": specification,
                                "reason": f"local dependency does not provide {dependency}",
                            }
                        )
                    local_dependencies[dependency] = str(target)
        scanned = 0
        for source in sorted(component.rglob("*")):
            if (
                not source.is_file()
                or source.suffix.lower() not in SOURCE_SUFFIXES
                or any(
                    part.lower() in EXCLUDED_SOURCE_DIRECTORIES
                    for part in source.relative_to(component).parts
                )
            ):
                continue
            scanned += 1
            text = source.read_text(encoding="utf-8", errors="replace")
            specifiers = {first or second for first, second in IMPORT_RE.findall(text)}
            for specifier in sorted(specifiers):
                if specifier.startswith("."):
                    target = (source.parent / specifier).resolve()
                    try:
                        target.relative_to(component)
                    except ValueError:
                        violations.append(
                            {
                                "component": name,
                                "file": str(source.relative_to(component)).replace(
                                    "\\", "/"
                                ),
                                "specifier": specifier,
                                "reason": "relative import leaves package boundary",
                            }
                        )
                    continue
                if specifier.startswith(("node:", "#")):
                    continue
                dependency = _package_name(specifier)
                if dependency not in declared:
                    violations.append(
                        {
                            "component": name,
                            "file": str(source.relative_to(component)).replace(
                                "\\", "/"
                            ),
                            "specifier": specifier,
                            "reason": "bare import is absent from package manifest",
                        }
                    )
        packages.append(
            {
                "component": name,
                "scanned_source_files": scanned,
                "declared_dependencies": sorted(declared),
                "declared_local_dependencies": local_dependencies,
            }
        )
    report = {
        "status": "passed" if not violations else "failed",
        "packages": packages,
        "violations": violations,
    }
    write_json(LATEST / "node-package-boundaries.json", report)
    if violations:
        raise RuntimeError(f"Node package boundary violations: {violations}")
    return report


def _node_modules_paths(root: Path) -> list[Path]:
    return [
        root / "components" / name / "node_modules"
        for name in NODE_COMPONENTS
        if (root / "components" / name / "node_modules").exists()
    ]


def _assert_no_sibling_node_modules(root: Path, component_name: str) -> None:
    component = root / "components" / component_name
    siblings = [
        path
        for path in _node_modules_paths(root)
        if path.parent.resolve() != component.resolve()
    ]
    if siblings:
        raise RuntimeError(
            f"{component_name} build can see sibling node_modules: "
            + ", ".join(str(path) for path in siblings)
        )


def _stage_node_package(component: Path, stage: Path) -> None:
    """Create publishable package input without mutating the source manifest."""
    document = json.loads((component / "package.json").read_text(encoding="utf-8"))
    for section in ("dependencies", "optionalDependencies", "peerDependencies"):
        dependencies = document.get(section, {})
        for dependency, specification in list(dependencies.items()):
            if not isinstance(specification, str) or not specification.startswith(
                "file:"
            ):
                continue
            target = (component / specification.removeprefix("file:")).resolve()
            target_document = json.loads(
                (target / "package.json").read_text(encoding="utf-8")
            )
            if target_document.get("name") != dependency:
                raise RuntimeError(f"local Node dependency name mismatch: {dependency}")
            dependencies[dependency] = str(target_document["version"])
    files = list(document.get("files", []))
    if not files:
        files = [name for name in ("dist", "dist-model") if (component / name).exists()]
        document["files"] = files
    stage.mkdir(parents=True, exist_ok=True)
    write_json(stage / "package.json", document)
    for name in files:
        source = component / name
        destination = stage / name
        if not source.exists():
            raise RuntimeError(f"declared Node package content is missing: {source}")
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    for name in ("README.md", "LICENSE", "LICENSE.md"):
        if (component / name).is_file():
            shutil.copy2(component / name, stage / name)


def install_packed_node_packages(
    work: Path, package_dir: Path, env: dict[str, str]
) -> dict[str, Any]:
    consumer = work / "node-consumer"
    consumer.mkdir(parents=True)
    write_json(
        consumer / "package.json", {"name": "villani-release-consumer", "private": True}
    )
    archives = sorted(package_dir.glob("*.tgz"))
    npm = "npm.cmd" if os.name == "nt" else "npm"
    run(
        [
            npm,
            "install",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            *map(str, archives),
        ],
        cwd=consumer,
        log=LATEST / "logs/node-package-install.log",
        env=env,
    )
    run(
        [
            shutil.which("node") or "node",
            "--input-type=module",
            "--eval",
            "await import('@villani/run-model'); await import('@villani/ui');",
        ],
        cwd=consumer,
        log=LATEST / "logs/node-package-import.log",
        env=env,
    )
    run(
        [
            shutil.which("node") or "node",
            str(
                consumer
                / "node_modules"
                / "villani-flight-recorder"
                / "dist"
                / "cli.js"
            ),
            "--help",
        ],
        cwd=consumer,
        log=LATEST / "logs/node-package-cli.log",
        env=env,
    )
    web_root = consumer / "node_modules" / "villani-web" / "dist"
    web_html = web_root / "index.html"
    if not web_html.is_file():
        raise RuntimeError("installed packed villani-web is missing dist/index.html")
    references = []
    for reference in ASSET_RE.findall(web_html.read_text(encoding="utf-8")):
        if reference.startswith(("http:", "https:", "data:", "mailto:")):
            continue
        exists = (web_root / reference.lstrip("/")).is_file()
        references.append({"reference": reference, "exists": exists})
        if not exists:
            raise RuntimeError(
                f"installed packed villani-web references missing asset {reference}"
            )
    return {
        "status": "passed",
        "packages": [path.name for path in archives],
        "web_assets": references,
    }


def build_node_packages(
    work: Path, root: Path, release_env: dict[str, str]
) -> tuple[list[Path], dict[str, Any]]:
    package_dir = LATEST / "packages"
    package_dir.mkdir(parents=True, exist_ok=True)
    logs = LATEST / "logs"
    npm = "npm.cmd" if os.name == "nt" else "npm"
    boundary_report = validate_node_boundaries(root)
    isolation_proofs: list[dict[str, Any]] = []
    for name in NODE_COMPONENTS:
        cwd = root / "components" / name
        _assert_no_sibling_node_modules(root, name)
        if (cwd / "node_modules").exists():
            raise RuntimeError(
                f"{name} did not start from a clean dependency directory"
            )
        if (cwd / "package-lock.json").is_file():
            run(
                [npm, "ci", "--no-audit", "--no-fund"],
                cwd=cwd,
                log=logs / f"{name}-install.log",
                env=release_env,
            )
        else:
            run(
                [
                    npm,
                    "install",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                ],
                cwd=cwd,
                log=logs / f"{name}-install.log",
                env=release_env,
            )
        _assert_no_sibling_node_modules(root, name)
        run(
            [npm, "run", "build"],
            cwd=cwd,
            log=logs / f"{name}-build.log",
            env=release_env,
        )
        stage = work / "node-stage" / name
        _stage_node_package(cwd, stage)
        run(
            [npm, "pack", "--ignore-scripts", "--pack-destination", str(package_dir)],
            cwd=stage,
            log=logs / f"{name}-pack.log",
            env=release_env,
        )
        isolation_proofs.append(
            {
                "component": name,
                "declared_dependency_install": "passed",
                "independent_build": "passed",
                "sibling_node_modules_during_build": [],
                "package": "packed",
            }
        )
        shutil.rmtree(cwd / "node_modules")
        remaining = _node_modules_paths(root)
        if remaining:
            raise RuntimeError(f"dependency cleanup after {name} left: {remaining}")
    asset_report = validate_frontend_assets(root)
    run(
        [
            sys.executable,
            str(root / "scripts" / "sync-console-assets.py"),
        ],
        cwd=root,
        log=logs / "console-assets-sync.log",
        env=release_env,
    )
    run(
        [
            sys.executable,
            str(root / "scripts" / "sync-console-assets.py"),
            "--check",
        ],
        cwd=root,
        log=logs / "console-assets-check.log",
        env=release_env,
    )
    asset_report["packaged_console_assets"] = "passed"
    asset_report["package_boundaries"] = boundary_report
    asset_report["isolated_builds"] = isolation_proofs
    write_json(
        LATEST / "isolated-node-build.json",
        {
            "status": "passed",
            "source_root": str(root.resolve()),
            "all_sibling_node_modules_removed": True,
            "builds": isolation_proofs,
            "villani_web_without_flight_recorder_dependencies": "passed",
            "flight_recorder_without_villani_web_dependencies": "passed",
        },
    )
    return sorted(package_dir.glob("*.tgz")), asset_report


def build_python_packages(
    work: Path, root: Path, release_env: dict[str, str]
) -> list[Path]:
    package_dir = LATEST / "packages"
    package_dir.mkdir(parents=True, exist_ok=True)
    logs = LATEST / "logs"
    built: list[Path] = []
    bun = shutil.which("bun", path=release_env.get("PATH"))
    if bun:
        compiler = [bun]
    else:
        npx = shutil.which(
            "npx.cmd" if os.name == "nt" else "npx", path=release_env.get("PATH")
        )
        if not npx:
            raise RuntimeError(
                "Bun or npx is required to build the Flight Recorder compatibility executable"
            )
        compiler = [npx, "--yes", "bun@1.2.20"]
    vfr_output = (
        root
        / "components"
        / "villani"
        / "villani_distribution"
        / "bin"
        / ("vfr.exe" if os.name == "nt" else "vfr")
    )
    flight_root = root / "components" / "villani-flight-recorder"
    _assert_no_sibling_node_modules(root, "villani-flight-recorder")
    if (flight_root / "node_modules").exists():
        raise RuntimeError(
            "Flight Recorder standalone build did not start from a clean dependency directory"
        )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    run(
        [npm, "ci", "--no-audit", "--no-fund"],
        cwd=flight_root,
        log=logs / "flight-recorder-standalone-install.log",
        env=release_env,
        timeout=600,
    )
    try:
        run(
            [
                *compiler,
                "build",
                str(flight_root / "dist" / "cli.js"),
                "--compile",
                "--outfile",
                str(vfr_output),
            ],
            cwd=flight_root,
            log=logs / "flight-recorder-standalone-build.log",
            env=release_env,
            timeout=600,
        )
    finally:
        shutil.rmtree(flight_root / "node_modules", ignore_errors=True)
    if _node_modules_paths(root):
        raise RuntimeError("Flight Recorder standalone dependency cleanup failed")
    if not vfr_output.is_file() or vfr_output.stat().st_size == 0:
        raise RuntimeError("Flight Recorder compatibility executable was not built")
    for name in PYTHON_COMPONENTS:
        output = work / "python" / name
        output.mkdir(parents=True, exist_ok=True)
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--sdist",
                "--outdir",
                str(output),
            ],
            cwd=root / "components" / name,
            log=logs / f"{name}-build.log",
            env=release_env,
        )
        for artifact in output.iterdir():
            destination = package_dir / artifact.name
            shutil.copy2(artifact, destination)
            built.append(destination)
    run(
        [
            sys.executable,
            str(root / "scripts" / "validate-console-wheel.py"),
            str(package_dir),
        ],
        cwd=root,
        log=logs / "console-wheel-content.log",
        env=release_env,
    )
    return built


def build_packages(
    work: Path, root: Path, release_env: dict[str, str]
) -> tuple[list[Path], dict[str, Any]]:
    """Compatibility wrapper used by focused packaging callers."""

    node_packages, assets = build_node_packages(work, root, release_env)
    assets["packed_node_install"] = install_packed_node_packages(
        work, LATEST / "packages", release_env
    )
    python_packages = build_python_packages(work, root, release_env)
    assets["packaged_console_wheel"] = "passed"
    return python_packages + node_packages, assets


def prepare_connected_node_runtime(
    root: Path, release_env: dict[str, str]
) -> dict[str, Any]:
    """Install the two browser runtimes after isolated builds have completed."""
    npm = "npm.cmd" if os.name == "nt" else "npm"
    logs = LATEST / "logs"
    if _node_modules_paths(root):
        raise RuntimeError("connected runtime preparation did not start clean")
    for name in ("villani-flight-recorder", "villani-web"):
        cwd = root / "components" / name
        run(
            [npm, "ci", "--no-audit", "--no-fund"],
            cwd=cwd,
            log=logs / f"{name}-runtime-install.log",
            env=release_env,
        )
    web = root / "components" / "villani-web"
    playwright_cli = web / "node_modules" / "playwright" / "cli.js"
    node = shutil.which("node") or "node"
    version = run(
        [node, str(playwright_cli), "--version"],
        cwd=web,
        log=logs / "playwright-version.log",
        env=release_env,
    )
    run(
        [node, str(playwright_cli), "install", "chromium"],
        cwd=web,
        log=logs / "playwright-browser-install.log",
        env=release_env,
    )
    installed = run(
        [node, str(playwright_cli), "install", "--list"],
        cwd=web,
        log=logs / "playwright-browser-list.log",
        env=release_env,
    )
    result = {
        "status": "passed",
        "runtime_node_modules": [
            str(path.resolve()) for path in _node_modules_paths(root)
        ],
        "playwright_browsers_path": release_env["PLAYWRIGHT_BROWSERS_PATH"],
        "playwright_version": version.stdout.strip(),
        "installed_browsers": installed.stdout.strip().splitlines(),
        "preinstalled_before_release_gate": release_env.get(
            "VILLANI_PLAYWRIGHT_PREINSTALLED"
        )
        == "1",
    }
    write_json(LATEST / "playwright-runtime.json", result)
    return result


def install_wheels(
    work: Path,
    packages: list[Path],
    root: Path,
    release_env: dict[str, str],
) -> Path:
    environment = work / "installed"
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run(
        [str(python), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=root,
        log=LATEST / "logs/wheel-environment-pip-upgrade.log",
        env=release_env,
    )
    wheels = [str(path) for path in packages if path.suffix == ".whl"]
    run(
        [str(python), "-m", "pip", "install", *wheels],
        cwd=root,
        log=LATEST / "logs/wheel-install.log",
        env=release_env,
    )
    entry_points: dict[str, Any] = {}
    for command in ("villani", "villani-code", "villani-agentd", "vfr"):
        resolution = _installed_entry_point(
            python,
            command,
            root=root,
            release_env=release_env,
        )
        entry_points[command] = resolution
        run(
            [*resolution["prefix"], "--help"],
            cwd=root,
            log=LATEST / "logs" / f"wheel-entrypoint-{command}.log",
            env=release_env,
            timeout=60,
        )
    write_json(
        LATEST / "installed-entrypoints.json",
        {
            "status": "passed",
            "selected_interpreter": str(python),
            "entry_points": entry_points,
        },
    )
    run(
        [str(python), "-m", "pip", "check"],
        cwd=root,
        log=LATEST / "logs/wheel-pip-check.log",
        env=release_env,
    )
    run(
        [
            str(python),
            "-c",
            "import villani_distribution, villani_ops, villani_code, villani_agentd, villani_control_plane",
        ],
        cwd=root,
        log=LATEST / "logs/wheel-imports.log",
        env=release_env,
    )
    return python


def evidence_skeleton(mode: str) -> dict[str, Any]:
    incomplete = {
        "status": "not_executed",
        "reason": "connected scenario harness not completed",
    }
    for name in (
        "redaction-proof.json",
        "canonical-reconciliation.json",
        "dead-letter-summary.json",
        "browser-summary.json",
        "security-summary.json",
        "test-summary.json",
        "postgres-migration-summary.json",
        "verifier-routing-summary.json",
        "candidate-diversity-summary.json",
        "classification-adjustment-summary.json",
        "installed-user-onboarding-summary.json",
        "cli-agent-mode-summary.json",
    ):
        write_json(LATEST / name, incomplete)
    for directory in (
        "screenshots",
        "control-plane-api-snapshots",
        "canonical-run-snapshots",
        "logs",
        "packages",
    ):
        (LATEST / directory).mkdir(parents=True, exist_ok=True)
    return {
        "mode": mode,
        "connected": incomplete,
        "browser": incomplete,
        "reconciliation": incomplete,
    }


def _summary(name: str) -> dict[str, Any]:
    path = LATEST / name
    if not path.is_file():
        raise RuntimeError(f"required release evidence is missing: {name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"required release evidence is not an object: {name}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _validate_connected_summary(
    connected: dict[str, Any], dead_letters: dict[str, Any]
) -> None:
    _require(connected.get("status") == "passed", "connected packaged scenarios failed")
    _require(
        connected.get("scenario_count") == 8 and connected.get("passed_scenarios") == 8,
        "all eight connected scenarios did not pass",
    )
    _require(
        connected.get("synchronized_run_count", 0) > 0,
        "zero synchronized runs cannot pass the release gate",
    )
    _require(
        connected.get("dead_letter_count") == 0 and dead_letters.get("count") == 0,
        "unexpected dead letters exist",
    )


def _validate_installed_user_onboarding(
    evidence: Path,
    installed_python: Path,
) -> dict[str, Any]:
    report_path = evidence / "onboarding-report.json"
    _require(report_path.is_file(), "installed-user onboarding report is missing")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    _require(
        isinstance(report, dict) and report.get("verdict") == "ONBOARDING GATE PASSED",
        "installed-user onboarding gate failed",
    )
    _require(
        Path(str(report.get("selected_interpreter") or "")).resolve()
        == installed_python.resolve(),
        "onboarding did not use the clean installed interpreter",
    )
    sample = report.get("sample_evidence") or {}
    _require(
        report.get("sample_final_state") == "COMPLETED"
        and bool(report.get("sample_selected_attempt"))
        and sample.get("attempt_count") == 1
        and sample.get("acceptance_eligible_attempt_count") == 1
        and (sample.get("acceptance") or {}).get("decision") is True,
        "onboarding did not produce exactly one selected acceptance-eligible attempt",
    )
    _require(
        sample.get("repository_checks")
        == {
            "passed": 1,
            "failed": 0,
            "not_run": 0,
            "unavailable": 0,
            "accounting_status": "complete",
        },
        "installed-user onboarding validation counts are not exactly one pass",
    )
    _require(
        (sample.get("focused_probes") or {}).get("passed") == 0
        and (sample.get("focused_probes") or {}).get("failed") == 0
        and (sample.get("requirements") or {}).get("not_proved") == 0,
        "installed-user onboarding canonical evidence is unresolved",
    )
    deliveries = report.get("delivery_modes") or {}
    _require(
        (deliveries.get("suggest") or {}).get("repository_unchanged") is True
        and (deliveries.get("branch") or {}).get("original_repository_unchanged")
        is True,
        "configured non-destructive delivery was not proven",
    )
    doctor = report.get("doctor") or {}
    _require(
        doctor.get("healthy") is True
        and doctor.get("ok") is True
        and (doctor.get("summary") or {}).get("failed") == 0,
        "installed-user doctor did not pass",
    )
    _require(
        report.get("service_running") is True
        and report.get("service_stopped") is True
        and report.get("dead_letters") == 0,
        "installed-user service lifecycle or dead-letter evidence failed",
    )
    screenshots = [Path(str(item)) for item in report.get("screenshots") or []]
    _require(
        len(screenshots) == 5
        and all(path.is_file() and path.stat().st_size > 1_000 for path in screenshots),
        "installed-user onboarding screenshots are incomplete",
    )
    _require(
        (report.get("secret_scan") or {}).get("status") == "passed"
        and not (report.get("secret_scan") or {}).get("matches"),
        "installed-user onboarding evidence contains secret material",
    )
    setup_commands = [
        item.get("command") or []
        for item in report.get("commands") or []
        if "setup" in (item.get("command") or [])
    ]
    _require(
        len(setup_commands) == 1
        and "--yes" in setup_commands[0]
        and "--sample" in setup_commands[0]
        and "fixture-onboarding" in setup_commands[0],
        "installed-user setup was not deterministic and non-interactive",
    )
    summary = {
        "status": "passed",
        "schema_version": "villani.installed_user_onboarding_gate.v1",
        "evidence_directory": str(evidence.resolve()),
        "report": str(report_path.resolve()),
        "selected_interpreter": str(installed_python.resolve()),
        "sample_run_id": report.get("sample_run_id"),
        "selected_attempt_id": report.get("sample_selected_attempt"),
        "acceptance_eligible_attempt_count": 1,
        "repository_checks": sample["repository_checks"],
        "focused_probes": sample.get("focused_probes"),
        "requirements": sample.get("requirements"),
        "classification": sample.get("classification"),
        "non_destructive_delivery_modes": ["suggest", "branch"],
        "doctor": "passed",
        "dead_letters": 0,
        "screenshots": [str(path.resolve()) for path in screenshots],
        "secret_scan": "passed",
        "service_stopped": True,
    }
    write_json(LATEST / "installed-user-onboarding-summary.json", summary)
    return summary


def _validate_screenshots(browser: dict[str, Any]) -> None:
    required = {
        "01-villani-web-overview.png",
        "02-runs-list.png",
        "03-easy-successful-run.png",
        "04-escalated-run-overview.png",
        "05-candidate-comparison.png",
        "06-verification-evidence.png",
        "07-classification-adjustment.png",
        "08-redaction-withheld-artifact.png",
        "09-heuristic-only-failed-run.png",
        "10-flight-recorder-overview.png",
        "11-replay-timeline.png",
        "12-event-stream.png",
        "13-evidence-panel.png",
        "14-file-activity.png",
        "15-flight-candidate-comparison.png",
        "16-overview-1280x800.png",
        "17-overview-1920x1080.png",
    }
    documented = {str(item.get("name")) for item in browser.get("screenshots", [])}
    _require(
        required == documented,
        f"browser screenshot set mismatch: missing={sorted(required - documented)} extra={sorted(documented - required)}",
    )
    expected_dimensions = {
        "16-overview-1280x800.png": (1280, 800),
        "17-overview-1920x1080.png": (1920, 1080),
    }
    for item in browser["screenshots"]:
        path = LATEST / "screenshots" / item["name"]
        _require(
            path.is_file() and path.stat().st_size > 0,
            f"browser screenshot is missing or empty: {item['name']}",
        )
        _require(
            sha256(path) == item.get("sha256"),
            f"browser screenshot hash mismatch: {item['name']}",
        )
        contents = path.read_bytes()[:24]
        _require(
            len(contents) == 24 and contents[1:4] == b"PNG",
            f"browser screenshot is not PNG: {item['name']}",
        )
        dimensions = (
            int.from_bytes(contents[16:20], "big"),
            int.from_bytes(contents[20:24], "big"),
        )
        _require(
            item.get("width") == dimensions[0] and item.get("height") == dimensions[1],
            f"browser screenshot metadata mismatch: {item['name']}",
        )
        if item["name"] in expected_dimensions:
            _require(
                dimensions == expected_dimensions[item["name"]],
                f"browser screenshot dimensions are {dimensions[0]}x{dimensions[1]} for {item['name']}",
            )
    _require(
        browser.get("screenshot_count") == len(required),
        "browser screenshot count is not 17",
    )
    _require(
        set(browser.get("viewport_coverage", []))
        == {"1280x800", "1440x900", "1920x1080"},
        "browser viewport coverage is incomplete",
    )


def _test_summary(connected: dict[str, Any], browser: dict[str, Any]) -> dict[str, Any]:
    scenarios = connected.get("scenarios", [])
    assertion_count = sum(len(item.get("assertions", {})) for item in scenarios)
    passed_assertions = sum(
        sum(value is True for value in item.get("assertions", {}).values())
        for item in scenarios
    )
    return {
        "status": "passed",
        "scope": "packaged connected release gate",
        "scenario_count": connected.get("scenario_count", 0),
        "passed_scenarios": connected.get("passed_scenarios", 0),
        "failed_scenarios": connected.get("scenario_count", 0)
        - connected.get("passed_scenarios", 0),
        "scenario_assertions": {
            "total": assertion_count,
            "passed": passed_assertions,
            "failed": assertion_count - passed_assertions,
        },
        "browser_assertions": browser.get("assertions", {}),
        "browser_screenshot_count": browser.get("screenshot_count", 0),
        "installed_user_onboarding": "passed",
        "note": "Component and full-suite results are enforced by their dedicated CI jobs; this file records the packaged connected gate only.",
    }


def _markdown(report: dict[str, Any]) -> str:
    verdict = report["release_verdict"]
    return (
        "# Villani release gate\n\n"
        f"Verdict: **{verdict}**\n\n"
        f"Mode: `{report['mode']}`\n\n"
        f"Scenarios: {report.get('passed_scenario_count', 0)}/{report.get('scenario_count', 0)} passed  \n"
        f"Synchronized runs: {report.get('synchronized_run_count', 0)}  \n"
        f"Dead letters: {report.get('dead_letter_count', 0)}  \n"
        f"API reconciliation: {report.get('api_reconciliation_status')}  \n"
        f"Villani Web reconciliation: {report.get('villani_web_reconciliation_status')}  \n"
        f"Flight Recorder reconciliation: {report.get('flight_recorder_reconciliation_status')}  \n"
        f"Browser: {report.get('browser_result')}  \n"
        f"Installed-user onboarding: {report.get('installed_user_onboarding_status')}  \n"
        f"CLI Agent Mode: {report.get('cli_agent_mode_status', 'not_executed')}  \n"
        f"Security: {report.get('security_scan_status')}\n\n"
        + (f"Failure: {report['failure']}\n\n" if report.get("failure") else "")
        + str(report.get("certification_note", ""))
        + "\n"
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connected_commands() -> list[dict[str, Any]]:
    path = LATEST / "connected-command-manifest.json"
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        {"scope": "connected_product", **item}
        for item in document.get("commands", [])
        if isinstance(item, dict)
    ]


def _all_commands() -> list[dict[str, Any]]:
    return [
        {"scope": "release_gate", **item} for item in COMMAND_RECORDS
    ] + _connected_commands()


class GateReporter:
    """Persist an authoritative report at every observable gate transition."""

    def __init__(self, mode: str) -> None:
        started = _utc_timestamp()
        self.report: dict[str, Any] = {
            "schema_version": "villani.release_gate.v1",
            "mode": mode,
            "started_at": started,
            "finished_at": None,
            "release_verdict": "RELEASE GATE FAILED",
            "phases": {
                name: {
                    "status": "pending",
                    "started_at": None,
                    "finished_at": None,
                    "elapsed_seconds": None,
                    "timeout_seconds": timeout,
                    "log_paths": [],
                    "failure_type": None,
                    "failure_message": None,
                }
                for name, timeout in PHASE_TIMEOUTS.items()
            },
            "active_phase": None,
            "last_completed_phase": None,
            "synchronized_run_count": 0,
            "completed_run_count": 0,
            "exhausted_run_count": 0,
            "dead_letter_count": 0,
            "redacted_field_count": 0,
            "withheld_artifact_count": 0,
            "api_reconciliation_status": "not_executed",
            "villani_web_reconciliation_status": "not_executed",
            "flight_recorder_reconciliation_status": "not_executed",
            "browser_result": "not_executed",
            "security_scan_status": "not_executed",
            "commands": [],
            "environment_paths": {},
        }
        self._started_monotonic: dict[str, float] = {}
        self.persist()

    def remaining_timeout(self) -> int | None:
        name = self.report.get("active_phase")
        if not name:
            return None
        started = self._started_monotonic.get(name)
        if started is None:
            return int(self.report["phases"][name]["timeout_seconds"])
        remaining = float(self.report["phases"][name]["timeout_seconds"]) - (
            time.monotonic() - started
        )
        return max(0, int(remaining))

    def start(self, name: str, *, logs: list[Path] | None = None) -> None:
        if name not in self.report["phases"]:
            raise RuntimeError(f"unknown release phase: {name}")
        if self.report["active_phase"] is not None:
            raise RuntimeError(
                f"cannot start {name}; {self.report['active_phase']} is still running"
            )
        phase = self.report["phases"][name]
        if phase["status"] != "pending":
            raise RuntimeError(f"release phase {name} was already started")
        phase.update(
            {
                "status": "running",
                "started_at": _utc_timestamp(),
                "log_paths": [str(path.resolve()) for path in (logs or [])],
            }
        )
        self._started_monotonic[name] = time.monotonic()
        self.report["active_phase"] = name
        self.persist()

    def finish(
        self,
        name: str,
        status: str = "passed",
        *,
        failure_type: str | None = None,
        failure_message: str | None = None,
        logs: list[Path] | None = None,
    ) -> None:
        if status not in PHASE_STATUSES - {"pending", "running"}:
            raise RuntimeError(f"invalid terminal phase status: {status}")
        phase = self.report["phases"][name]
        started = self._started_monotonic.get(name, time.monotonic())
        if logs:
            phase["log_paths"] = sorted(
                set(phase["log_paths"]) | {str(path.resolve()) for path in logs}
            )
        phase.update(
            {
                "status": status,
                "finished_at": _utc_timestamp(),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "failure_type": failure_type,
                "failure_message": failure_message,
            }
        )
        self.report["active_phase"] = None
        if status in {"passed", "not_applicable"}:
            self.report["last_completed_phase"] = name
        self.persist()

    def fail_active(self, error: BaseException, *, timed_out: bool = False) -> None:
        name = self.report.get("active_phase")
        if name:
            self.finish(
                name,
                "timed_out" if timed_out else "failed",
                failure_type=type(error).__name__,
                failure_message=str(error),
            )
        self.report["failure"] = str(error)
        self.report["failure_type"] = type(error).__name__
        self.persist()

    def persist(self, *, final: bool = False) -> None:
        commands = _all_commands()
        self.report["commands"] = commands
        self.report["command_count"] = len(commands)
        command_status = (
            "passed"
            if all(
                item.get("status") in {"passed", "exited", "terminated"}
                for item in commands
            )
            else "failed"
        )
        write_json(
            LATEST / "command-manifest.json",
            {
                "status": command_status,
                "certification_identity": self.report.get("certification_identity", {}),
                "environment_paths": self.report.get("environment_paths", {}),
                "commands": commands,
            },
        )
        if final:
            self.report["finished_at"] = _utc_timestamp()
        write_json(LATEST / "release-gate-report.json", self.report)
        (LATEST / "release-gate-report.md").write_text(
            _markdown(self.report), encoding="utf-8"
        )


def _contains_not_executed(value: Any) -> bool:
    if value == "not_executed":
        return True
    if isinstance(value, dict):
        return any(_contains_not_executed(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_not_executed(item) for item in value)
    return False


def validate_final_evidence(mode: str, reporter: GateReporter) -> None:
    required = (
        "connected-product-summary.json",
        "canonical-reconciliation.json",
        "dead-letter-summary.json",
        "browser-summary.json",
        "redaction-proof.json",
        "security-summary.json",
        "test-summary.json",
        "postgres-migration-summary.json",
        "verifier-routing-summary.json",
        "candidate-diversity-summary.json",
        "classification-adjustment-summary.json",
        "installed-user-onboarding-summary.json",
        "cli-agent-mode-summary.json",
    )
    for name in required:
        document = _summary(name)
        _require(
            document.get("status") == "passed", f"required evidence failed: {name}"
        )
        if mode in {"ci", "release"}:
            _require(
                not _contains_not_executed(document),
                f"required evidence remains not_executed: {name}",
            )
    failed_commands = [
        item
        for item in _all_commands()
        if item.get("status") not in {"passed", "exited", "terminated"}
    ]
    _require(
        not failed_commands,
        f"command manifest contains failed commands: {failed_commands}",
    )
    for name, phase in reporter.report["phases"].items():
        if name == "final_evidence_validation":
            continue
        _require(
            phase["status"] in {"passed", "not_applicable"},
            f"required phase did not pass: {name} ({phase['status']})",
        )
    identity = reporter.report.get("certification_identity")
    _require(isinstance(identity, dict), "certification identity is missing")
    assert isinstance(identity, dict)
    _require(
        bool(identity.get("git_commit_sha"))
        and bool(identity.get("source_manifest_sha256"))
        and bool(identity.get("configuration_sha256")),
        "certification identity digests are incomplete",
    )
    if mode == "ci" and identity.get("hosted_ci"):
        _require(
            identity.get("authoritative_hosted_ci") is True
            and bool(identity.get("workflow_run_id"))
            and identity.get("working_tree_clean") is True,
            "hosted certification identity is incomplete",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "ci", "release"), default="ci")
    args = parser.parse_args(argv)
    global _ACTIVE_REPORTER
    COMMAND_RECORDS.clear()
    _remove_tree(LATEST)
    LATEST.mkdir(parents=True)
    evidence_skeleton(args.mode)
    reporter = GateReporter(args.mode)
    _ACTIVE_REPORTER = reporter
    report = reporter.report
    exit_code = 1
    try:
        identity = _certification_identity(args.mode)
        report["certification_identity"] = identity
        write_json(
            LATEST / "certification-identity.json",
            {"status": "passed", **identity},
        )
        reporter.persist()
        reporter.start(
            "source_isolation",
            logs=[LATEST / "logs/isolated-source-manifest.log"],
        )
        temporary_parent_value = os.environ.get("VILLANI_RELEASE_TEMP_ROOT")
        temporary_parent = (
            Path(temporary_parent_value).resolve()
            if temporary_parent_value
            else (Path(tempfile.gettempdir()) / "villani-release-gate").resolve()
        )
        try:
            temporary_parent.relative_to(ROOT)
        except ValueError:
            pass
        else:
            raise RuntimeError(
                "release temporary root must be outside the source root; "
                "set VILLANI_RELEASE_TEMP_ROOT to an external writable directory"
            )
        temporary_parent.mkdir(parents=True, exist_ok=True)
        write_probe = temporary_parent / (
            f".villani-write-probe-{os.getpid()}-{time.time_ns()}"
        )
        try:
            write_probe.mkdir()
            write_probe.rmdir()
        except OSError as error:
            raise RuntimeError(
                f"release temporary root is not writable: {temporary_parent}"
            ) from error
        with tempfile.TemporaryDirectory(
            prefix="villani-release-gate-", dir=temporary_parent
        ) as temporary:
            work = Path(temporary)
            source_root = work / "clean-source"
            isolation = create_isolated_source(ROOT, source_root)
            release_env = os.environ.copy()
            configured_playwright = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            use_preinstalled_playwright = os.environ.get(
                "VILLANI_PLAYWRIGHT_PREINSTALLED"
            ) == "1" and bool(configured_playwright)
            playwright_browsers = (
                Path(str(configured_playwright)).expanduser().absolute()
                if use_preinstalled_playwright
                else work / "playwright-browsers"
            )
            if use_preinstalled_playwright and not any(playwright_browsers.iterdir()):
                raise RuntimeError(
                    "hosted Playwright preinstallation was declared but its browser "
                    "directory is empty"
                )
            environment_directories = {
                "temporary_root": work / "temp",
                "npm_cache": work / "npm-cache",
                "pip_cache": work / "pip-cache",
                "playwright_browsers": playwright_browsers,
                "isolated_source": source_root,
                "installed_python": work / "installed",
                "release_artifacts": LATEST,
            }
            for directory in environment_directories.values():
                directory.mkdir(parents=True, exist_ok=True)
            release_env.update(
                {
                    "TEMP": str(environment_directories["temporary_root"]),
                    "TMP": str(environment_directories["temporary_root"]),
                    "TMPDIR": str(environment_directories["temporary_root"]),
                    "npm_config_cache": str(environment_directories["npm_cache"]),
                    "PIP_CACHE_DIR": str(environment_directories["pip_cache"]),
                    "PLAYWRIGHT_BROWSERS_PATH": str(
                        environment_directories["playwright_browsers"]
                    ),
                }
            )
            report["environment_paths"] = {
                name: str(path.resolve())
                for name, path in environment_directories.items()
            }
            report["isolated_source"] = isolation
            identity["source_manifest_sha256"] = isolation["source_manifest_sha256"]
            report["certification_identity"] = identity
            write_json(
                LATEST / "certification-identity.json",
                {"status": "passed", **identity},
            )
            reporter.finish("source_isolation")

            reporter.start("compatibility")
            versions = component_versions(source_root)
            identity["package_versions"] = versions
            report["certification_identity"] = identity
            write_json(
                LATEST / "certification-identity.json",
                {"status": "passed", **identity},
            )
            template = validate_compatibility(versions, source_root)
            write_json(LATEST / "component-versions.json", versions)
            reporter.finish("compatibility")

            reporter.start(
                "node_package_build",
                logs=[
                    LATEST / "logs" / f"{name}-build.log" for name in NODE_COMPONENTS
                ],
            )
            node_packages, assets = build_node_packages(work, source_root, release_env)
            reporter.finish("node_package_build")

            reporter.start(
                "python_package_build",
                logs=[
                    LATEST / "logs" / f"{name}-build.log" for name in PYTHON_COMPONENTS
                ]
                + [
                    LATEST / "logs/flight-recorder-standalone-install.log",
                    LATEST / "logs/flight-recorder-standalone-build.log",
                ],
            )
            python_packages = build_python_packages(work, source_root, release_env)
            assets["packaged_console_wheel"] = "passed"
            reporter.finish("python_package_build")

            reporter.start(
                "packed_node_install",
                logs=[LATEST / "logs/node-package-install.log"],
            )
            assets["packed_node_install"] = install_packed_node_packages(
                work, LATEST / "packages", release_env
            )
            reporter.finish("packed_node_install")

            packages = python_packages + node_packages
            reporter.start("wheel_install", logs=[LATEST / "logs/wheel-install.log"])
            installed_python = install_wheels(work, packages, source_root, release_env)
            reporter.finish("wheel_install")

            cli_agent_artifacts = LATEST / "cli-agent-mode"
            cli_agent_command = [
                str(Path(sys.executable).resolve()),
                str(source_root / "release-verification" / "run_cli_agent_gate.py"),
                "--source-root",
                str(source_root),
                "--artifacts",
                str(cli_agent_artifacts),
                "--test-python",
                str(Path(sys.executable).resolve()),
                "--installed-python",
                str(installed_python),
            ]
            if (
                os.environ.get("VILLANI_CLI_AGENT_SMOKE_CONSENT")
                == "I_ACCEPT_EXTERNAL_USAGE"
            ):
                cli_agent_command.append("--real-smoke")
            reporter.start(
                "cli_agent_mode",
                logs=[LATEST / "logs/cli-agent-mode.log"],
            )
            run(
                cli_agent_command,
                cwd=source_root,
                log=LATEST / "logs/cli-agent-mode.log",
                env=release_env,
                timeout=PHASE_TIMEOUTS["cli_agent_mode"],
            )
            cli_agent_report = json.loads(
                (cli_agent_artifacts / "cli-agent-mode-release-report.json").read_text(
                    encoding="utf-8"
                )
            )
            _require(
                cli_agent_report.get("status") == "passed"
                and cli_agent_report.get("certification_status")
                in {"PASS", "PARTIAL"}
                and cli_agent_report.get("required_deterministic_evidence_complete")
                is True,
                "CLI Agent Mode deterministic release certification failed",
            )
            write_json(
                LATEST / "cli-agent-mode-summary.json",
                {
                    "status": "passed",
                    "certification_status": cli_agent_report[
                        "certification_status"
                    ],
                    "required_deterministic_evidence_complete": True,
                    "real_provider_smoke_status": cli_agent_report.get(
                        "real_provider_smoke_status", "NOT_RUN"
                    ),
                    "report": "cli-agent-mode/cli-agent-mode-release-report.json",
                    "matrix": "cli-agent-mode/cli-agent-mode-conformance-matrix.json",
                    "evidence_index": "cli-agent-mode/release-evidence-index.json",
                },
            )
            report["cli_agent_mode_status"] = cli_agent_report[
                "certification_status"
            ]
            reporter.finish("cli_agent_mode")

            reporter.start(
                "connected_runtime_preparation",
                logs=[
                    LATEST / "logs/playwright-version.log",
                    LATEST / "logs/playwright-browser-install.log",
                    LATEST / "logs/playwright-browser-list.log",
                ],
            )
            connected_node_runtime = prepare_connected_node_runtime(
                source_root, release_env
            )
            reporter.finish("connected_runtime_preparation")

            onboarding_stamp = datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            onboarding_evidence = (
                LATEST / "installed-user-onboarding" / onboarding_stamp
            )
            onboarding_env = dict(release_env)
            onboarding_env["VILLANI_ONBOARDING_ALLOW_EXTERNAL_ARTIFACTS"] = "1"
            reporter.start(
                "installed_user_onboarding",
                logs=[LATEST / "logs/installed-user-onboarding.log"],
            )
            run(
                [
                    str(installed_python),
                    str(
                        source_root
                        / "onboarding-verification"
                        / "run_onboarding_gate.py"
                    ),
                    "--artifacts",
                    str(onboarding_evidence),
                    "--python",
                    str(installed_python),
                ],
                cwd=source_root,
                log=LATEST / "logs/installed-user-onboarding.log",
                env=onboarding_env,
                timeout=PHASE_TIMEOUTS["installed_user_onboarding"],
            )
            onboarding_summary = _validate_installed_user_onboarding(
                onboarding_evidence,
                installed_python,
            )
            report["installed_user_onboarding_status"] = "passed"
            report["installed_user_onboarding"] = onboarding_summary
            reporter.finish("installed_user_onboarding")

            hashes = {path.name: sha256(path) for path in sorted(packages)}
            generated = json.loads(json.dumps(template))
            generated["generated"] = {
                "build_timestamp": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "package_hashes": hashes,
                "python": platform.python_version(),
                "node": subprocess.run(
                    [shutil.which("node") or "node", "--version"],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                "platform": platform.platform(),
            }
            write_json(LATEST / "component-compatibility.json", generated)
            write_json(LATEST / "package-hashes.json", hashes)
            write_json(LATEST / "frontend-asset-validation.json", assets)
            write_json(
                LATEST / "build-manifest.json",
                {
                    "status": "passed",
                    "packages": [
                        {
                            "name": name,
                            "sha256": digest,
                            "size": (LATEST / "packages" / name).stat().st_size,
                        }
                        for name, digest in sorted(hashes.items())
                    ],
                    "clean_wheel_install": "passed",
                    "clean_packed_node_install": assets["packed_node_install"][
                        "status"
                    ],
                    "editable_installs": False,
                    "node_packages_packed": len(
                        [name for name in hashes if name.endswith(".tgz")]
                    ),
                    "python_wheels": len(
                        [name for name in hashes if name.endswith(".whl")]
                    ),
                    "python_source_distributions": len(
                        [name for name in hashes if name.endswith(".tar.gz")]
                    ),
                    "isolated_source": isolation,
                    "isolated_node_build": "passed",
                    "connected_node_runtime": connected_node_runtime,
                },
            )
            report["package_versions"] = versions
            report["package_hashes"] = hashes
            report["build_result"] = "passed"
            reporter.persist()

            connected_work = work / "connected"
            connected_work.mkdir()
            reporter.start(
                "connected_scenarios",
                logs=[LATEST / "logs/connected-product.log"],
            )
            run(
                [
                    str(installed_python),
                    str(source_root / "release-verification" / "connected_product.py"),
                    "--python",
                    str(installed_python),
                    "--work",
                    str(connected_work),
                    "--artifacts",
                    str(LATEST),
                    "--mode",
                    args.mode,
                ],
                cwd=source_root,
                log=LATEST / "logs/connected-product.log",
                env=release_env,
                timeout=PHASE_TIMEOUTS["connected_scenarios"],
            )
            connected = _summary("connected-product-summary.json")
            dead_letters = _summary("dead-letter-summary.json")
            _validate_connected_summary(connected, dead_letters)
            reporter.finish("connected_scenarios")

            reporter.start("canonical_reconciliation")
            reconciliation = _summary("canonical-reconciliation.json")
            _require(
                reconciliation.get("status") == "passed",
                "canonical six-source reconciliation failed",
            )
            reporter.finish("canonical_reconciliation")

            reporter.start("browser", logs=[LATEST / "logs/connected-browser.log"])
            browser = _summary("browser-summary.json")
            _require(browser.get("status") == "passed", "connected browser gate failed")
            _require(
                browser.get("villani_web_reconciliation") == "passed",
                "Villani Web reconciliation failed",
            )
            _require(
                browser.get("flight_recorder_reconciliation") == "passed",
                "Flight Recorder reconciliation failed",
            )
            reporter.finish("browser")

            reporter.start("screenshots")
            _validate_screenshots(browser)
            reporter.finish("screenshots")

            reporter.start("redaction")
            redaction = _summary("redaction-proof.json")
            _require(
                redaction.get("status") == "passed"
                and redaction.get("registered_secret_absent") is True
                and redaction.get("unsafe_artifact_rejected") is True
                and int(redaction.get("withheld_artifact_count") or 0) >= 1,
                "redaction or artifact-withholding proof failed",
            )
            reporter.finish("redaction")

            reporter.start("postgresql")
            postgres = _summary("postgres-migration-summary.json")
            _require(
                postgres.get("status") == "passed"
                and postgres.get("alembic_head") == template["alembic_head"],
                "PostgreSQL migration proof failed",
            )
            _require(
                postgres.get("fresh_database_upgrade") == "passed"
                and all(postgres.get("checks", {}).values()),
                "populated pre-composite PostgreSQL proof is incomplete",
            )
            reporter.finish("postgresql")

            reporter.start("verifier_routing")
            verifier = _summary("verifier-routing-summary.json")
            _require(
                verifier.get("status") == "passed", "verifier-routing proof failed"
            )
            reporter.finish("verifier_routing")

            reporter.start("candidate_diversity")
            diversity = _summary("candidate-diversity-summary.json")
            _require(
                diversity.get("status") == "passed"
                and diversity.get("counted_diversity") == 2,
                "candidate-diversity proof failed",
            )
            reporter.finish("candidate_diversity")

            reporter.start("classification_adjustment")
            classification = _summary("classification-adjustment-summary.json")
            _require(
                classification.get("status") == "passed",
                "classification-adjustment proof failed",
            )
            reporter.finish("classification_adjustment")

            report["synchronized_run_count"] = connected["synchronized_run_count"]
            report["completed_run_count"] = connected["completed_run_count"]
            report["exhausted_run_count"] = connected["exhausted_run_count"]
            report["dead_letter_count"] = connected["dead_letter_count"]
            report["redacted_field_count"] = connected["redacted_field_count"]
            report["withheld_artifact_count"] = connected["withheld_artifact_count"]
            report["scenario_count"] = connected["scenario_count"]
            report["passed_scenario_count"] = connected["passed_scenarios"]
            report["failed_scenario_count"] = (
                connected["scenario_count"] - connected["passed_scenarios"]
            )
            report["api_reconciliation_status"] = reconciliation["status"]
            report["villani_web_reconciliation_status"] = browser.get(
                "villani_web_reconciliation", "failed"
            )
            report["flight_recorder_reconciliation_status"] = browser.get(
                "flight_recorder_reconciliation", "failed"
            )
            report["browser_result"] = browser.get("status", "failed")
            report["alembic_head"] = postgres.get("alembic_head")
            report["spool_schema_version"] = template["spool_schema_version"]
            report["verifier_routing_result"] = verifier.get("status")
            report["candidate_diversity_result"] = diversity.get("status")
            report["classification_adjustment_result"] = classification.get("status")
            reporter.persist()

            reporter.start("supply_chain")
            security = supply_chain.generate(
                mode=args.mode,
                installed_python=installed_python,
                packages=packages,
                output=LATEST,
                package_hashes=hashes,
                source_root=source_root,
            )
            report["security_scan_status"] = security["status"]
            report["official_release_certification"] = security[
                "official_release_certification"
            ]
            report["certification_note"] = security["certification_note"]
            _require(
                security["status"] == "passed",
                "required supply-chain scanner or deterministic security check failed",
            )
            reporter.finish("supply_chain")

            tests = _test_summary(connected, browser)
            write_json(LATEST / "test-summary.json", tests)
            report["test_summary"] = tests

            reporter.start("final_evidence_validation")
            validate_final_evidence(args.mode, reporter)
            reporter.finish("final_evidence_validation")
            report["release_verdict"] = (
                "LOCAL GATE PASSED"
                if args.mode == "local"
                else "RELEASE GATE PASSED"
                if identity["authoritative_hosted_ci"] or args.mode == "release"
                else "CI-MODE GATE PASSED (NON-AUTHORITATIVE)"
            )
            exit_code = 0
    except KeyboardInterrupt as error:
        reporter.fail_active(error)
        report["failure"] = "release gate interrupted by user"
        exit_code = 130
    except subprocess.TimeoutExpired as error:
        reporter.fail_active(error, timed_out=True)
    except Exception as error:
        reporter.fail_active(error)
    finally:
        reporter.persist(final=True)
        _write_artifact_manifest(
            report.get("certification_identity", {})
            if isinstance(report.get("certification_identity"), dict)
            else {}
        )
        _ACTIVE_REPORTER = None
    print(LATEST / "release-gate-report.json")
    print(report["release_verdict"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
