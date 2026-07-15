from __future__ import annotations

import os
import stat
import subprocess
import venv
from pathlib import Path

import pytest

from villani_ops import executables
from villani_ops.executables import (
    discover_interpreter_scripts_directory,
    resolve_installed_executable,
)


def _entry_point(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"{name}.exe" if os.name == "nt" else name)
    path.write_text("fixture\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _venv_python(root: Path) -> Path:
    scripts = root / ("Scripts" if os.name == "nt" else "bin")
    return scripts / ("python.exe" if os.name == "nt" else "python")


def test_current_interpreter_prefers_sysconfig_scripts_without_resolving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = tmp_path / "literal-environment" / "python"
    scripts = tmp_path / "reported-scripts"
    expected = _entry_point(scripts, "villani-code")
    interpreter.parent.mkdir()
    interpreter.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(executables.sys, "executable", str(interpreter))
    monkeypatch.setattr(executables.sysconfig, "get_path", lambda name: str(scripts))

    resolution = resolve_installed_executable(
        "villani-code", environ={"PATH": "", "PATHEXT": ".EXE;.CMD"}
    )

    assert resolution.path == expected
    assert resolution.source == "interpreter_scripts"
    assert resolution.interpreter == interpreter.absolute()
    assert resolution.path_searched is False


def test_external_virtual_environment_query_beats_resolved_interpreter_parent(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "external-environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = _venv_python(environment)
    scripts = python.parent
    expected = _entry_point(scripts, "villani-fixture-command")

    discovery = discover_interpreter_scripts_directory(python)
    resolution = resolve_installed_executable(
        "villani-fixture-command",
        interpreter=python,
        environ={"PATH": "", "PATHEXT": ".EXE;.CMD"},
    )

    assert discovery.path == scripts.absolute()
    assert discovery.source == "external_interpreter_query"
    assert resolution.path == expected
    assert resolution.source == "interpreter_scripts"
    if python.is_symlink():
        assert python.resolve().parent != resolution.path.parent


def test_failed_external_query_falls_back_to_literal_interpreter_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = tmp_path / "selected" / "python"
    interpreter.parent.mkdir()
    interpreter.write_text("not executable", encoding="utf-8")
    expected = _entry_point(interpreter.parent, "villani-agentd")
    monkeypatch.setattr(
        executables.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 9, "", "failure"),
    )

    resolution = resolve_installed_executable(
        "villani-agentd", interpreter=interpreter, environ={"PATH": ""}
    )

    assert resolution.path == expected
    assert resolution.source == "interpreter_parent"
    assert resolution.scripts_directory is None


def test_additional_directories_precede_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = tmp_path / "selected" / "python"
    interpreter.parent.mkdir()
    interpreter.write_text("fixture", encoding="utf-8")
    additional = tmp_path / "additional"
    expected = _entry_point(additional, "vfr")
    path_directory = tmp_path / "path"
    _entry_point(path_directory, "vfr")
    monkeypatch.setattr(
        executables.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", ""),
    )

    resolution = resolve_installed_executable(
        "vfr",
        interpreter=interpreter,
        additional_search_dirs=(additional,),
        environ={"PATH": str(path_directory)},
    )

    assert resolution.path == expected
    assert resolution.source == "additional_search_dir"
    assert resolution.path_searched is False


def test_windows_entry_point_forms_include_generated_script_and_pathext() -> None:
    names = executables._entry_point_names(
        "villani", windows=True, pathext=".EXE;.CMD;.PS1"
    )
    assert names[:4] == (
        "villani.exe",
        "villani.com",
        "villani.cmd",
        "villani.bat",
    )
    assert "villani-script.py" in names
    assert "villani.ps1" in names


def test_missing_executable_diagnostic_is_actionable_and_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = tmp_path / "selected" / "python"
    interpreter.parent.mkdir()
    interpreter.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        executables.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, "", "secret-output"
        ),
    )
    resolution = resolve_installed_executable(
        "villani-code",
        interpreter=interpreter,
        environ={"PATH": "", "TOP_SECRET": "never-report"},
    )

    assert resolution.path is None
    assert resolution.source == "not_found"
    assert "villani-code" in resolution.diagnostic
    assert str(interpreter) in resolution.diagnostic
    assert "Scripts directory attempted" in resolution.diagnostic
    assert "Candidate paths:" in resolution.diagnostic
    assert "PATH searched: yes" in resolution.diagnostic
    assert "Reinstall Villani" in resolution.diagnostic
    assert "secret-output" not in resolution.diagnostic
    assert "never-report" not in resolution.diagnostic
