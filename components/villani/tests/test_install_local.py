from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _installer_module():
    path = ROOT / "scripts" / "install-local.py"
    spec = importlib.util.spec_from_file_location("villani_install_local", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_interrupted_publish_restores_prior_environment(tmp_path: Path) -> None:
    installer = _installer_module()
    target = tmp_path / "installed"
    staged = tmp_path / "staged"
    target.mkdir()
    staged.mkdir()
    (target / "working.txt").write_text("prior", encoding="utf-8")
    (staged / "broken-entry-point").write_text("incomplete", encoding="utf-8")

    def interrupt(_: Path) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        installer._publish_staged_environment(staged, target, verify=interrupt)

    assert (target / "working.txt").read_text(encoding="utf-8") == "prior"
    assert not (target / "broken-entry-point").exists()
    assert not staged.exists()
    assert not list(tmp_path.glob(".installed.villani-backup-*"))


def test_missing_mandatory_dependency_rolls_back_entry_points(tmp_path: Path) -> None:
    installer = _installer_module()
    target = tmp_path / "installed"
    staged = tmp_path / "staged"
    target.mkdir()
    staged.mkdir()
    (target / "working.txt").write_text("prior", encoding="utf-8")
    (staged / "villani").write_text("unusable launcher", encoding="utf-8")

    def missing_import(_: Path) -> None:
        raise RuntimeError("mandatory import unavailable")

    with pytest.raises(RuntimeError, match="mandatory import unavailable"):
        installer._publish_staged_environment(staged, target, verify=missing_import)

    assert (target / "working.txt").is_file()
    assert not (target / "villani").exists()
    assert not list(tmp_path.glob(".installed.villani-backup-*"))


def test_runtime_and_development_dependency_profiles_are_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(installer, "_run", lambda command, **_: commands.append(command))

    installer._install_python_packages(Path(sys.executable), development=False)
    runtime = commands.pop()
    assert not any("[test]" in item or "[dev]" in item for item in runtime)

    installer._install_python_packages(Path(sys.executable), development=True)
    development = commands.pop()
    editable_specs = [
        development[index + 1]
        for index, item in enumerate(development[:-1])
        if item == "-e"
    ]
    assert any("villani-code[dev]" in item.replace("\\", "/") for item in editable_specs)
    assert sum("[test]" in item for item in editable_specs) == 3


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [(RuntimeError("missing dependency"), 1), (KeyboardInterrupt(), 130)],
)
def test_failed_install_prints_one_exact_repair_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    expected_code: int,
) -> None:
    installer = _installer_module()
    target = tmp_path / "installed"

    def fail(*_: object, **__: object) -> int:
        raise failure

    monkeypatch.setattr(installer, "_install", fail)
    monkeypatch.setattr(sys, "argv", ["install-local.py", "--venv", str(target)])

    assert installer.main() == expected_code
    stderr = capsys.readouterr().err
    expected = installer._repair_command(target.resolve(), development=False)
    assert stderr.count("Repair command:") == 1
    assert f"Repair command: {expected}" in stderr
