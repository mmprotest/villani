"""Advisory, pluggable repository-validation discovery.

Discovery inspects repository metadata only.  A suggestion is never evidence and
never becomes authoritative merely because its confidence is high.  Authority is
created later by the verifier when a confirmed argv is executed in the isolated
candidate worktree with the structured ``repository_validation`` role.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

import yaml


DISCOVERY_SCHEMA = "villani.repository_validation_discovery.v1"
CONFIRMATION_THRESHOLD = 0.80


@dataclass(frozen=True, slots=True)
class ValidationSuggestion:
    suggestion_id: str
    argv: tuple[str, ...]
    confidence: float
    reason: str
    source: str
    metadata_files: tuple[str, ...] = ()

    @property
    def requires_confirmation(self) -> bool:
        return self.confidence < CONFIRMATION_THRESHOLD

    def as_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "argv": list(self.argv),
            "display_command": display_argv(self.argv),
            "confidence": self.confidence,
            "confidence_label": confidence_label(self.confidence),
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
            "source": self.source,
            "metadata_files": list(self.metadata_files),
            "advisory_only": True,
            "authoritative": False,
        }


class ValidationDiscoveryPlugin(Protocol):
    """One metadata-only discovery extension."""

    name: str

    def discover(
        self, repository: Path, metadata: Mapping[str, Any]
    ) -> Iterable[ValidationSuggestion]: ...


def display_argv(argv: Sequence[str]) -> str:
    """Render an argv for inspection without changing how it will execute."""

    return subprocess.list2cmdline(list(argv)) if os.name == "nt" else shlex.join(argv)


def confidence_label(confidence: float) -> str:
    if confidence >= 0.90:
        return "high"
    if confidence >= CONFIRMATION_THRESHOLD:
        return "medium"
    return "low"


def parse_manual_command(command: str) -> tuple[str, ...]:
    """Parse an explicit command into the exact shell-free argv to execute."""

    try:
        values = shlex.split(command, posix=os.name != "nt")
        if os.name == "nt":
            values = [
                value[1:-1]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}
                else value
                for value in values
            ]
        argv = tuple(values)
    except ValueError as error:
        raise ValueError(f"validation command cannot be parsed: {error}") from error
    if not argv or any(not value or "\x00" in value for value in argv):
        raise ValueError("validation command must contain a non-empty executable argv")
    return argv


def _safe_mapping(path: Path) -> Mapping[str, Any]:
    if not path.is_file() or path.stat().st_size > 1_048_576:
        return {}
    try:
        if path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".toml":
            value = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
        yaml.YAMLError,
    ):
        return {}
    return value if isinstance(value, Mapping) else {}


def _explicit_commands(value: Mapping[str, Any]) -> list[tuple[str, ...]]:
    raw: Any = value.get("repository_validation_commands")
    if raw is None:
        validation = value.get("validation")
        if isinstance(validation, Mapping):
            raw = validation.get("commands") or validation.get("command")
    if isinstance(raw, (str, Mapping)):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    result: list[tuple[str, ...]] = []
    for item in raw:
        argv: Any = item.get("argv") if isinstance(item, Mapping) else item
        if isinstance(argv, str):
            try:
                parsed = parse_manual_command(argv)
            except ValueError:
                continue
        elif (
            isinstance(argv, list)
            and argv
            and all(isinstance(part, str) and part for part in argv)
        ):
            parsed = tuple(argv)
        else:
            continue
        result.append(parsed)
    return result


class ExplicitConfigurationPlugin:
    name = "explicit_configuration"

    def discover(
        self, repository: Path, metadata: Mapping[str, Any]
    ) -> Iterable[ValidationSuggestion]:
        del metadata
        for relative in (
            ".villani.yaml",
            ".villani.yml",
            ".villani/config.yaml",
            ".villani/config.yml",
            "pyproject.toml",
        ):
            path = repository / relative
            for index, argv in enumerate(_explicit_commands(_safe_mapping(path)), 1):
                yield ValidationSuggestion(
                    suggestion_id=f"explicit_{index:03d}",
                    argv=argv,
                    confidence=0.99,
                    reason="Repository metadata explicitly declares this validation command.",
                    source=self.name,
                    metadata_files=(relative,),
                )


class PackageScriptPlugin:
    name = "package_script"

    def discover(
        self, repository: Path, metadata: Mapping[str, Any]
    ) -> Iterable[ValidationSuggestion]:
        del metadata
        package = _safe_mapping(repository / "package.json")
        scripts = package.get("scripts")
        if not isinstance(scripts, Mapping):
            return
        manager = (
            "pnpm"
            if (repository / "pnpm-lock.yaml").is_file()
            else "yarn"
            if (repository / "yarn.lock").is_file()
            else "npm"
        )
        for script_name, confidence in (
            ("validate", 0.96),
            ("check", 0.93),
            ("test", 0.90),
        ):
            script = scripts.get(script_name)
            if not isinstance(script, str) or not script.strip():
                continue
            lowered = script.lower()
            if "no test specified" in lowered or re.search(r"\bexit\s+1\b", lowered):
                continue
            argv = (
                (manager, "run", script_name)
                if manager == "npm" and script_name != "test"
                else (manager, script_name)
            )
            yield ValidationSuggestion(
                suggestion_id=f"{manager}_{script_name}",
                argv=argv,
                confidence=confidence,
                reason=f"package.json defines a non-placeholder {script_name!r} script.",
                source=self.name,
                metadata_files=("package.json",),
            )
            return


class ToolConfigurationPlugin:
    name = "tool_configuration"

    def discover(
        self, repository: Path, metadata: Mapping[str, Any]
    ) -> Iterable[ValidationSuggestion]:
        del metadata
        pyproject = _safe_mapping(repository / "pyproject.toml")
        tool = (
            pyproject.get("tool") if isinstance(pyproject.get("tool"), Mapping) else {}
        )
        pytest_configured = bool(
            (repository / "pytest.ini").is_file()
            or (repository / "conftest.py").is_file()
            or isinstance(tool.get("pytest"), Mapping)
        )
        if pytest_configured:
            files = tuple(
                name
                for name in ("pyproject.toml", "pytest.ini", "conftest.py")
                if (repository / name).is_file()
            )
            yield ValidationSuggestion(
                suggestion_id="pytest",
                argv=("python", "-m", "pytest", "-q"),
                confidence=0.90,
                reason="Repository metadata configures pytest.",
                source=self.name,
                metadata_files=files,
            )
            return
        if (repository / "tox.ini").is_file():
            yield ValidationSuggestion(
                suggestion_id="tox",
                argv=("tox", "-q"),
                confidence=0.88,
                reason="tox.ini defines the repository test environment.",
                source=self.name,
                metadata_files=("tox.ini",),
            )


class RootTestDeclarationPlugin:
    name = "root_test_declaration"

    def discover(
        self, repository: Path, metadata: Mapping[str, Any]
    ) -> Iterable[ValidationSuggestion]:
        del metadata
        declared: list[str] = []
        try:
            candidates = sorted(repository.glob("test*.py"))[:100]
        except OSError:
            return
        for path in candidates:
            try:
                if not path.is_file() or path.stat().st_size > 262_144:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(
                r"(?m)^\s*(?:from\s+unittest\s+import|import\s+unittest\b)",
                text,
            ):
                declared.append(path.name)
        if declared:
            yield ValidationSuggestion(
                suggestion_id="unittest_root_discovery",
                argv=("python", "-m", "unittest", "-q"),
                confidence=0.84,
                reason=(
                    "Conventional root test files explicitly import unittest and are "
                    "covered by unittest discovery."
                ),
                source=self.name,
                metadata_files=tuple(declared),
            )


class ConventionalMetadataPlugin:
    name = "conventional_metadata"

    def discover(
        self, repository: Path, metadata: Mapping[str, Any]
    ) -> Iterable[ValidationSuggestion]:
        del metadata
        candidates = (
            ("cargo_test", ("cargo", "test"), "Cargo.toml"),
            ("go_test", ("go", "test", "./..."), "go.mod"),
            ("maven_test", ("mvn", "test"), "pom.xml"),
        )
        for suggestion_id, argv, marker in candidates:
            if (repository / marker).is_file():
                yield ValidationSuggestion(
                    suggestion_id=suggestion_id,
                    argv=argv,
                    confidence=0.86,
                    reason=f"{marker} declares the repository build/test tool.",
                    source=self.name,
                    metadata_files=(marker,),
                )
                return
        gradle = "gradlew.bat" if (repository / "gradlew.bat").is_file() else "gradlew"
        if (repository / gradle).is_file():
            executable = gradle if gradle.endswith(".bat") else f"./{gradle}"
            yield ValidationSuggestion(
                suggestion_id="gradle_test",
                argv=(executable, "test"),
                confidence=0.86,
                reason="The repository includes its Gradle wrapper.",
                source=self.name,
                metadata_files=(gradle,),
            )
            return
        makefile = repository / "Makefile"
        if makefile.is_file() and makefile.stat().st_size <= 1_048_576:
            try:
                text = makefile.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if re.search(r"(?m)^test\s*:", text):
                yield ValidationSuggestion(
                    suggestion_id="make_test",
                    argv=("make", "test"),
                    confidence=0.70,
                    reason="A conventional Makefile test target was found; its scope is not declared.",
                    source=self.name,
                    metadata_files=("Makefile",),
                )
                return
        if (repository / "tests").is_dir():
            yield ValidationSuggestion(
                suggestion_id="conventional_pytest",
                argv=("python", "-m", "pytest", "-q"),
                confidence=0.60,
                reason=(
                    "A conventional tests directory was found, but repository "
                    "metadata does not declare its validation scope."
                ),
                source=self.name,
                metadata_files=("tests/",),
            )


DEFAULT_PLUGINS: tuple[ValidationDiscoveryPlugin, ...] = (
    ExplicitConfigurationPlugin(),
    PackageScriptPlugin(),
    ToolConfigurationPlugin(),
    RootTestDeclarationPlugin(),
    ConventionalMetadataPlugin(),
)


class ValidationDiscoveryRegistry:
    """Runs a stable ordered set of metadata-only discovery plugins."""

    def __init__(
        self, plugins: Sequence[ValidationDiscoveryPlugin] = DEFAULT_PLUGINS
    ) -> None:
        self.plugins = tuple(plugins)

    def discover(self, repository: str | Path) -> dict[str, Any]:
        root = Path(repository).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"repository does not exist or is not a directory: {root}")
        metadata = repository_metadata(root)
        suggestions: list[ValidationSuggestion] = []
        failures: list[dict[str, str]] = []
        for plugin in self.plugins:
            try:
                suggestions.extend(plugin.discover(root, metadata))
            except (OSError, UnicodeError, ValueError) as error:
                failures.append({"plugin": plugin.name, "error": type(error).__name__})
        deduplicated: dict[tuple[str, ...], ValidationSuggestion] = {}
        for suggestion in suggestions:
            if not suggestion.argv or not 0 <= suggestion.confidence <= 1:
                continue
            previous = deduplicated.get(suggestion.argv)
            if previous is None or suggestion.confidence > previous.confidence:
                deduplicated[suggestion.argv] = suggestion
        ordered = sorted(
            deduplicated.values(),
            key=lambda item: (-item.confidence, item.source, item.suggestion_id),
        )
        return {
            "schema_version": DISCOVERY_SCHEMA,
            "repository_path": str(root),
            "metadata": metadata,
            "suggestions": [item.as_dict() for item in ordered],
            "selected_suggestion_id": ordered[0].suggestion_id if ordered else None,
            "confirmation_threshold": CONFIRMATION_THRESHOLD,
            "plugin_failures": failures,
            "recommendations_are_advisory": True,
            "authority": "none_until_confirmed_command_execution",
        }


def repository_metadata(repository: Path) -> dict[str, Any]:
    markers = (
        ".villani.yaml",
        ".villani.yml",
        ".villani/config.yaml",
        ".villani/config.yml",
        "pyproject.toml",
        "pytest.ini",
        "conftest.py",
        "tox.ini",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "gradlew",
        "gradlew.bat",
        "Makefile",
    )
    present = [name for name in markers if (repository / name).is_file()]
    return {
        "metadata_files": present,
        "has_tests_directory": (repository / "tests").is_dir(),
        "has_git_metadata": (repository / ".git").exists(),
        "language_routing_applied": False,
    }


def discover_repository_validation(
    repository: str | Path,
    *,
    plugins: Sequence[ValidationDiscoveryPlugin] = DEFAULT_PLUGINS,
) -> dict[str, Any]:
    return ValidationDiscoveryRegistry(plugins).discover(repository)


def confirmed_command(
    argv: Sequence[str],
    *,
    source: str,
    confidence: float,
    confirmed_by: str,
    validation_id: str = "repository_validation_001",
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    if not argv or any(not isinstance(value, str) or not value for value in argv):
        raise ValueError("confirmed validation argv must contain non-empty strings")
    return {
        "validation_id": validation_id,
        "argv": list(argv),
        "display_command": display_argv(argv),
        "timeout_seconds": timeout_seconds,
        "source": source,
        "confidence": confidence,
        "confirmed": True,
        "confirmed_by": confirmed_by,
        "advisory_discovery": True,
        "authoritative": False,
        "authority_begins": "on_structured_repository_validation_execution",
    }
