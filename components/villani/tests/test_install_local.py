from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _scripts(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _python(venv: Path) -> Path:
    return _scripts(venv) / ("python.exe" if os.name == "nt" else "python")


def _entry_point(venv: Path, name: str) -> Path:
    scripts = _scripts(venv)
    candidates = (
        [scripts / f"{name}.exe", scripts / f"{name}.cmd", scripts / name]
        if os.name == "nt"
        else [scripts / name]
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def test_install_local_bootstraps_a_real_clean_environment(tmp_path: Path) -> None:
    venv = tmp_path / "clean-install"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "install-local.py"),
            "--venv",
            str(venv),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    python = _python(venv)
    assert python.is_file()
    bootstrap = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import setuptools; import villani_distribution; import villani_ops; "
                "import villani_code; import villani_agentd"
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    pip_check = subprocess.run(
        [str(python), "-m", "pip", "check"],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert pip_check.returncode == 0, pip_check.stdout + pip_check.stderr

    for name in ("villani", "villani-code", "villani-agentd", "vfr"):
        executable = _entry_point(venv, name)
        assert executable.is_file(), name
        help_result = subprocess.run(
            [str(executable), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        assert help_result.returncode == 0, help_result.stdout + help_result.stderr
