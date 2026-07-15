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


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


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
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--find-links",
            str(wheels),
            "villani==0.3.0rc1",
            "pyinstaller",
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
    extracted = work / "archive"
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(extracted)
    for path in extracted.iterdir():
        if os.name != "nt" and path.suffix != ".json":
            path.chmod(path.stat().st_mode | 0o755)
    for name in ("villani", "villani-code", "villani-agentd", "vfr"):
        path = extracted / f"{name}{'.exe' if os.name == 'nt' else ''}"
        run([str(path), "--help"], env=env)
    print(f"RELEASE_ARTIFACT={archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
