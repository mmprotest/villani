"""Read-only repository toolchain inspection. It never executes recommendations."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


LOCKFILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
    "pom.xml",
    "gradle.lockfile",
    "flake.lock",
)


def _present(repo: Path, *names: str) -> list[str]:
    return [name for name in names if (repo / name).is_file()]


def lockfile_digests(repo: str | Path) -> dict[str, str]:
    root = Path(repo)
    result: dict[str, str] = {}
    for name in LOCKFILES:
        path = root / name
        if path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def inspect_repository(repo: str | Path) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    ecosystems: list[dict[str, Any]] = []
    test_tools: list[str] = []
    likely: list[list[str]] = []

    python = _present(
        root,
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "Pipfile",
        "poetry.lock",
        "uv.lock",
    )
    if python:
        ecosystems.append({"name": "python", "files": python})
        pyproject = (
            (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            if (root / "pyproject.toml").is_file()
            else ""
        )
        requirements = "\n".join(
            (root / name).read_text(encoding="utf-8", errors="replace")
            for name in ("requirements.txt", "requirements-dev.txt")
            if (root / name).is_file()
        )
        if (
            "pytest" in (pyproject + requirements).lower()
            or (root / "pytest.ini").is_file()
        ):
            test_tools.append("pytest")
            likely.append(["python", "-m", "pytest", "-q"])
        elif (root / "tests").is_dir():
            test_tools.append("unittest")
            likely.append(["python", "-m", "unittest"])

    node = _present(
        root, "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"
    )
    if node:
        ecosystems.append({"name": "node", "files": node})
        manager = (
            "pnpm"
            if "pnpm-lock.yaml" in node
            else "yarn"
            if "yarn.lock" in node
            else "npm"
        )
        test_tools.append(manager)
        likely.append([manager, "test"])
    if (root / "Cargo.toml").is_file():
        ecosystems.append(
            {"name": "cargo", "files": _present(root, "Cargo.toml", "Cargo.lock")}
        )
        test_tools.append("cargo")
        likely.append(["cargo", "test"])
    if (root / "go.mod").is_file():
        ecosystems.append({"name": "go", "files": _present(root, "go.mod", "go.sum")})
        test_tools.append("go")
        likely.append(["go", "test", "./..."])
    if (root / "pom.xml").is_file():
        ecosystems.append({"name": "maven", "files": ["pom.xml"]})
        test_tools.append("maven")
        likely.append(["mvn", "test"])
    gradle = _present(
        root,
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradlew",
        "gradlew.bat",
    )
    if gradle:
        ecosystems.append({"name": "gradle", "files": gradle})
        test_tools.append("gradle")
        likely.append(
            ["gradlew.bat" if (root / "gradlew.bat").is_file() else "./gradlew", "test"]
        )

    environment_files = _present(
        root,
        ".devcontainer/devcontainer.json",
        "devcontainer.json",
        "flake.nix",
        "shell.nix",
        "default.nix",
    )
    explicit = _present(
        root,
        ".villani.yaml",
        ".villani.yml",
        ".villani/config.yaml",
        ".villani/config.yml",
    )
    return {
        "schema_version": "villani.repository_inspection.v1",
        "repository_path": str(root),
        "ecosystems": ecosystems,
        "environment_files": environment_files,
        "explicit_villani_config": explicit,
        "lockfile_digests": lockfile_digests(root),
        "detected_test_tools": list(dict.fromkeys(test_tools)),
        "likely_test_commands": likely,
        "recommendations_are_advisory": True,
        "inferred_commands_executed": False,
    }
