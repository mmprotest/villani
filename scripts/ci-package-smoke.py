#!/usr/bin/env python3
"""Cross-platform local release-candidate build and isolated command smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "release" / "VERSION").read_text(encoding="utf-8").strip()


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print("+", subprocess.list2cmdline(command), flush=True)
    return subprocess.run(
        command,
        cwd=cwd or ROOT,
        env=env,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        shell=False,
    )


def command_prefix(home: Path, name: str) -> list[str]:
    if os.name == "nt":
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            "call",
            str(home / "bin" / f"{name}.cmd"),
        ]
    return [str(home / "bin" / name)]


def environment_python(venv_root: Path) -> Path:
    scripts = venv_root / ("Scripts" if os.name == "nt" else "bin")
    return scripts / ("python.exe" if os.name == "nt" else "python")


def installed_executable(
    python: Path, name: str, environment: dict[str, str]
) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(python),
            "-I",
            str(ROOT / "scripts" / "resolve-installed-executable.py"),
            name,
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"Installed executable resolver failed for {name!r}: "
            f"{completed.stderr.strip()}"
        )
    try:
        resolution = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"Installed executable resolver returned malformed output for {name!r}."
        ) from error
    if not isinstance(resolution, dict) or not resolution.get("path"):
        diagnostic = (
            resolution.get("diagnostic") if isinstance(resolution, dict) else None
        )
        raise SystemExit(str(diagnostic or f"Missing installed executable {name!r}."))
    if resolution.get("source") not in {"interpreter_scripts", "interpreter_parent"}:
        raise SystemExit(
            f"Isolated installation resolved {name!r} outside its environment: "
            f"{resolution.get('diagnostic')}"
        )
    return resolution


def inspect_support_archive(
    path: Path, *, privacy_needles: tuple[str, ...]
) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "manifest.json" not in names:
            raise SystemExit("support archive omitted its manifest")
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except json.JSONDecodeError as error:
            raise SystemExit("support archive manifest is malformed") from error
        items = manifest.get("items")
        if not isinstance(items, list):
            raise SystemExit("support archive manifest items are invalid")
        declared: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("included") is not True:
                continue
            name = item.get("logical_name")
            if not isinstance(name, str) or name not in names:
                raise SystemExit("support archive manifest references a missing item")
            payload = archive.read(name)
            if len(payload) != item.get("size_bytes") or hashlib.sha256(
                payload
            ).hexdigest() != item.get("sha256"):
                raise SystemExit("support archive item digest or size is invalid")
            declared.add(name)
        if names != declared | {"manifest.json"}:
            raise SystemExit("support archive contains an undeclared item")
        combined = b"\n".join(archive.read(name) for name in sorted(names)).decode(
            "utf-8", errors="replace"
        )
    if (
        manifest.get("uploaded") is not False
        or manifest.get("repositories_modified") is not False
        or manifest.get("explicit_run_ids") != []
        or manifest.get("source_included") is not False
        or manifest.get("prompts_included") is not False
        or manifest.get("diffs_included") is not False
        or manifest.get("terminal_content_included") is not False
        or any(name.startswith("runs/") for name in names)
    ):
        raise SystemExit("support archive violated the default privacy contract")
    folded = combined.casefold()
    for needle in privacy_needles:
        variants = {needle.casefold(), needle.replace("\\", "\\\\").casefold()}
        if any(value and value in folded for value in variants):
            raise SystemExit("support archive retained a private absolute path")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument(
        "--bun-command",
        help="standalone compiler command forwarded to build-vfr-standalone.py",
    )
    args = parser.parse_args()
    work = args.work.resolve()
    wheels = work / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    vfr_output = (
        ROOT
        / "components"
        / "villani"
        / "villani_distribution"
        / "bin"
        / ("vfr.exe" if os.name == "nt" else "vfr")
    )
    vfr_build = [
        sys.executable,
        "scripts/build-vfr-standalone.py",
        "--output",
        str(vfr_output),
        "--skip-npm-build",
    ]
    bun_command = args.bun_command
    if not bun_command and not shutil.which("bun"):
        npx = shutil.which("npx.cmd" if os.name == "nt" else "npx") or shutil.which(
            "npx"
        )
        if not npx:
            raise SystemExit(
                "Bun or npx is required to compile the Flight Recorder executable"
            )
        bun_command = (
            "npx.cmd --yes bun@1.2.20" if os.name == "nt" else "npx --yes bun@1.2.20"
        )
    if bun_command:
        vfr_build.extend(["--bun-command", bun_command])
    run(vfr_build)
    run([sys.executable, "scripts/sync-console-assets.py", "--check"])
    for component in ("villani-code", "villani-ops", "villani-agentd", "villani"):
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(wheels),
                str(ROOT / "components" / component),
            ]
        )
    run([sys.executable, "scripts/validate-console-wheel.py", str(wheels)])
    isolated = work / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(isolated)
    python = environment_python(isolated)
    run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--find-links",
            str(wheels),
            f"villani=={VERSION}",
            "pyinstaller",
            "pip-audit",
        ]
    )
    home = work / "home"
    service_root = work / "service"
    env = dict(os.environ)
    env.update(
        {
            "VILLANI_HOME": str(home),
            "VILLANI_SERVICE_TEST_ROOT": str(service_root),
            "VILLANI_SERVICE_DRY_RUN": "1",
        }
    )
    env.pop("PYTHONPATH", None)
    env.pop("NODE_PATH", None)
    env.pop("VIRTUAL_ENV", None)
    source_scripts = os.path.normcase(os.path.abspath(Path(sys.executable).parent))
    env["PATH"] = os.pathsep.join(
        item
        for item in env.get("PATH", "").split(os.pathsep)
        if item and os.path.normcase(os.path.abspath(item)) != source_scripts
    )
    entry_points = {
        name: installed_executable(python, name, env)
        for name in ("villani", "villani-code", "villani-agentd", "vfr")
    }
    for resolution in entry_points.values():
        prefix = resolution.get("prefix")
        if not isinstance(prefix, list) or not all(
            isinstance(item, str) for item in prefix
        ):
            raise SystemExit(
                "Installed executable resolver returned an invalid prefix."
            )
        run([*prefix, "--help"], env=env)
    villani_value = entry_points["villani"].get("prefix")
    if not isinstance(villani_value, list) or not all(
        isinstance(item, str) for item in villani_value
    ):
        raise SystemExit("Installed Villani executable prefix is invalid.")
    villani = tuple(villani_value)
    run([*villani, "install-service"], env=env)
    run([*villani, "service", "status"], env=env)
    run([*villani, "uninstall-service"], env=env)
    preserved = home / "runs" / "preserved.txt"
    preserved.parent.mkdir(parents=True, exist_ok=True)
    preserved.write_text("preserved", encoding="utf-8")
    run([*villani, "uninstall-service"], env=env)
    if preserved.read_text(encoding="utf-8") != "preserved":
        raise SystemExit("service uninstall removed user run data")
    release = work / "release"
    run(
        [
            str(python),
            "scripts/build-release.py",
            "--output-dir",
            str(release),
            "--vfr",
            str(vfr_output),
        ]
    )
    checksum = (release / "SHA256SUMS").read_text(encoding="utf-8").split()[0]
    archive = next(release.glob("villani-*.zip"))
    if hashlib.sha256(archive.read_bytes()).hexdigest() != checksum:
        raise SystemExit("release checksum verification failed")
    scan_report = release / "release-artifact-scan.json"
    run(
        [
            str(python),
            "scripts/scan-release-artifact.py",
            "--archive",
            str(archive),
            "--output",
            str(scan_report),
            "--mode",
            "local",
        ]
    )
    scan = json.loads(scan_report.read_text(encoding="utf-8"))
    if not scan.get("passed"):
        raise SystemExit("release artifact package or scan verification failed")
    extracted = work / "archive"
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(extracted)
    manifest = json.loads(
        (extracted / "package-manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("version") != VERSION:
        raise SystemExit(
            "release manifest version differs from the canonical release version"
        )
    if manifest.get("source_checkout_required") or manifest.get(
        "sibling_node_modules_required"
    ):
        raise SystemExit(
            "release manifest incorrectly requires build-time source dependencies"
        )
    for item in manifest.get("files", []):
        path = extracted / str(item.get("path") or "")
        if os.name != "nt" and item.get("executable") is True:
            path.chmod(path.stat().st_mode | 0o755)
    consumer = work / "artifact-consumer"
    consumer.mkdir(parents=True, exist_ok=True)
    for name in ("villani", "villani-code", "villani-agentd", "vfr"):
        path = extracted / f"{name}{'.exe' if os.name == 'nt' else ''}"
        run([str(path), "--help"], env=env, cwd=consumer)

    # Install and operate only the standalone archive, from a directory outside
    # the checkout and with Python/Node source-resolution environment removed.
    artifact_home = work / "artifact-home"
    artifact_env = dict(env)
    artifact_env["VILLANI_HOME"] = str(artifact_home)
    raw_villani = extracted / f"villani{'.exe' if os.name == 'nt' else ''}"
    installed = run(
        [
            str(raw_villani),
            "install",
            "--artifact",
            str(archive),
            "--sha256",
            checksum,
            "--json",
        ],
        env=artifact_env,
        cwd=consumer,
        capture=True,
    )
    installed_state = json.loads(installed.stdout)
    if installed_state.get("status") != "verified":
        raise SystemExit("standalone archive did not reach verified installation state")
    villani = command_prefix(artifact_home, "villani")
    version = run([*villani, "--version"], env=artifact_env, cwd=consumer, capture=True)
    if VERSION not in version.stdout:
        raise SystemExit("managed launcher did not report the canonical version")
    doctor = run(
        [*villani, "doctor", "--installation-only", "--json"],
        env=artifact_env,
        cwd=consumer,
        capture=True,
    )
    doctor_document = json.loads(doctor.stdout)
    if (
        not doctor_document.get("healthy")
        or doctor_document.get("repositories_modified") is not False
    ):
        raise SystemExit(
            "installed artifact doctor did not pass without repository mutation"
        )
    performance_command = (
        next((artifact_home / "runners").glob("*/villani.exe"))
        if os.name == "nt"
        else artifact_home / "bin" / "villani"
    )
    performance_report = release / "performance-report.json"
    run(
        [
            str(python),
            "scripts/pt10-performance-gate.py",
            "--command",
            str(performance_command),
            "--home",
            str(artifact_home),
            "--output",
            str(performance_report),
        ],
        env=artifact_env,
    )
    feed = release / "update-feed.json"
    run(
        [*villani, "update", "channel", "stable", "--feed", str(feed), "--json"],
        env=artifact_env,
        cwd=consumer,
    )
    checked = run(
        [*villani, "update", "check", "--json"],
        env=artifact_env,
        cwd=consumer,
        capture=True,
    )
    if json.loads(checked.stdout).get("available_version") != VERSION:
        raise SystemExit("local update feed did not resolve the certified artifact")
    run([*villani, "update", "preview", "--json"], env=artifact_env, cwd=consumer)
    run(
        [
            *villani,
            "update",
            "install",
            "--artifact",
            str(archive),
            "--sha256",
            checksum,
            "--json",
        ],
        env=artifact_env,
        cwd=consumer,
    )
    run([*villani, "update", "rollback", "--json"], env=artifact_env, cwd=consumer)
    preview = run(
        [*villani, "support", "preview", "--json"],
        env=artifact_env,
        cwd=consumer,
        capture=True,
    )
    preview_document = json.loads(preview.stdout)
    if (
        preview_document.get("preview") is not True
        or preview_document.get("uploaded") is not False
    ):
        raise SystemExit(
            "support manifest preview did not remain local and non-mutating"
        )
    support = run(
        [*villani, "support", "create", "--confirm-manifest", "--json"],
        env=artifact_env,
        cwd=consumer,
        capture=True,
    )
    support_document = json.loads(support.stdout)
    support_archive = Path(str(support_document.get("archive") or ""))
    if (
        not support_archive.is_file()
        or support_document.get("manifest", {}).get("uploaded") is not False
    ):
        raise SystemExit("support archive was not created locally with upload disabled")
    inspect_support_archive(
        support_archive,
        privacy_needles=(
            str(ROOT.resolve()),
            str(artifact_home.resolve()),
            str(Path.home().resolve()),
        ),
    )
    run([*villani, "cleanup", "--json"], env=artifact_env, cwd=consumer)
    dependency_report = release / "dependency-audit.json"
    audit_environment = dict(env)
    audit_environment["PIPAPI_PYTHON_LOCATION"] = str(python)
    run(
        [
            str(python),
            "-m",
            "pip_audit",
            "--local",
            "--format",
            "json",
            "--output",
            str(dependency_report),
        ],
        env=audit_environment,
    )
    certification = {
        "schema_version": "villani.pt10_platform_certification.v1",
        "version": VERSION,
        "operating_system": manifest.get("operating_system"),
        "architecture": manifest.get("architecture"),
        "source_checkout_cwd_used_for_artifact_commands": False,
        "source_checkout_virtual_environment_visible": False,
        "sibling_node_modules_required": False,
        "installation": "verified",
        "doctor": "passed",
        "update": "verified",
        "rollback": "verified",
        "support_bundle": "created_inspected_and_not_uploaded",
        "dependency_audit": str(dependency_report),
        "artifact_scan": str(scan_report),
        "performance": str(performance_report),
        "passed": True,
    }
    (release / "pt10-platform-certification.json").write_text(
        json.dumps(certification, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"RELEASE_ARTIFACT={archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
