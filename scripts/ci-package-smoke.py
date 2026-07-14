#!/usr/bin/env python3
"""Cross-platform local release-candidate build and isolated command smoke."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def executable(venv_root: Path, name: str) -> Path:
    scripts = venv_root / ("Scripts" if os.name == "nt" else "bin")
    return scripts / f"{name}{'.exe' if os.name == 'nt' else ''}"


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
    if args.bun_command:
        vfr_build.extend(["--bun-command", args.bun_command])
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
    python = executable(isolated, "python")
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
    for name in ("villani", "villani-code", "villani-agentd", "vfr"):
        command = executable(isolated, name)
        command_env = dict(env)
        if name == "vfr":
            command_env["PATH"] = str(command.parent)
        run([str(command), "--help"], env=command_env)
    villani = executable(isolated, "villani")
    run([str(villani), "install-service"], env=env)
    run([str(villani), "service", "status"], env=env)
    run([str(villani), "uninstall-service"], env=env)
    preserved = home / "runs" / "preserved.txt"
    preserved.parent.mkdir(parents=True, exist_ok=True)
    preserved.write_text("preserved", encoding="utf-8")
    run([str(villani), "uninstall-service"], env=env)
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
        archive_env = dict(env)
        if name == "vfr":
            archive_env["PATH"] = str(extracted)
        run([str(path), "--help"], env=archive_env)
    print(f"RELEASE_ARTIFACT={archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
