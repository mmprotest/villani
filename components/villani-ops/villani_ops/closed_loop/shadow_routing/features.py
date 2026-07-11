from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from ..protocol import ClassificationSnapshot
from .models import FeatureProvenance, FeatureValue, TaskFeatures

FEATURE_EXTRACTOR_VERSIONS = {
    "repository_size": "repository_size_v1",
    "languages": "extension_language_v1",
    "build_systems": "build_marker_v1",
    "dependencies": "dependency_marker_v1",
    "test_topology": "test_path_topology_v1",
    "change_category": "classification_category_v1",
    "change_radius": "deterministic_radius_v1",
    "security_paths": "security_path_v1",
    "required_tools": "required_tools_v1",
    "context_size": "byte_context_estimate_v1",
    "historical": "aggregate_projection_v1",
}

_SKIP_PARTS = {
    ".git",
    ".villani",
    ".villani-ops",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
}
_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".swift": "swift",
    ".sh": "shell",
    ".ps1": "powershell",
}
_BUILD_MARKERS = {
    "pyproject.toml": "python-pyproject",
    "setup.py": "python-setuptools",
    "package.json": "node",
    "cargo.toml": "cargo",
    "go.mod": "go-modules",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "makefile": "make",
    "cmakelists.txt": "cmake",
}
_LOCKFILES = {
    "poetry.lock",
    "pdm.lock",
    "uv.lock",
    "requirements.txt",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.lock",
    "go.sum",
    "gemfile.lock",
    "composer.lock",
}
_SECURITY_PARTS = {
    "auth",
    "authentication",
    "authorization",
    "security",
    "secrets",
    "credentials",
    "crypto",
    "payments",
    "billing",
    "permissions",
    "iam",
    "oauth",
}


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _files(repository: Path) -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for path in repository.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(repository).as_posix()
        if any(part.lower() in _SKIP_PARTS for part in Path(rel).parts):
            continue
        try:
            content_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append((rel, path.stat().st_size, content_digest))
        except OSError:
            continue
    return sorted(rows)


def _value(name: str, value: Any, *provenance: FeatureProvenance) -> FeatureValue:
    return FeatureValue(
        extractor_name=name,
        extractor_version=FEATURE_EXTRACTOR_VERSIONS[name],
        value=value,
        missing=value is None,
        provenance=tuple(provenance),
    )


def _safe_aggregates(raw: Mapping[str, Any] | None) -> dict[str, float | int] | None:
    if raw is None:
        return None
    result: dict[str, float | int] = {}
    for key, value in sorted(raw.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(key)):
            result[str(key)] = value
    return result


def extract_task_features(
    repository_path: str | Path,
    *,
    run_id: str,
    task: str,
    success_criteria: str,
    classification: ClassificationSnapshot,
    historical_aggregates: Mapping[str, Any] | None = None,
    historical_snapshot_id: str | None = None,
) -> TaskFeatures:
    repository = Path(repository_path).resolve()
    rows = _files(repository)
    snapshot_digest = _digest(rows)
    repo_prov = FeatureProvenance(
        source_kind="repository_snapshot",
        source_id=f"sha256:{snapshot_digest}",
        digest_sha256=snapshot_digest,
    )
    task_digest = _digest({"task": task, "success_criteria": success_criteria})
    task_prov = FeatureProvenance(
        source_kind="task_input",
        source_id=f"sha256:{task_digest}",
        digest_sha256=task_digest,
    )
    classification_digest = _digest(classification.model_dump(mode="json"))
    cls_prov = FeatureProvenance(
        source_kind="classification",
        source_id=classification.classification_id,
        digest_sha256=classification_digest,
    )
    paths = [row[0] for row in rows]
    basenames = [Path(path).name.lower() for path in paths]
    languages = sorted(
        {
            _LANGUAGES[Path(path).suffix.lower()]
            for path in paths
            if Path(path).suffix.lower() in _LANGUAGES
        }
    )
    builds = sorted(
        {_BUILD_MARKERS[name] for name in basenames if name in _BUILD_MARKERS}
    )
    locks = sorted(path for path in paths if Path(path).name.lower() in _LOCKFILES)
    test_paths = [
        path
        for path in paths
        if "test" in Path(path).name.lower()
        or any(
            part.lower() in {"test", "tests", "spec", "specs"}
            for part in Path(path).parts
        )
    ]
    source_count = max(len(paths) - len(test_paths), 0)
    topology = {
        "test_file_count": len(test_paths),
        "test_directory_count": len({str(Path(path).parent) for path in test_paths}),
        "source_to_test_ratio": round(source_count / len(test_paths), 4)
        if test_paths
        else None,
        "has_integration_tests": any(
            "integration" in Path(path).parts for path in test_paths
        ),
        "has_e2e_tests": any(
            part.lower() in {"e2e", "end_to_end"}
            for path in test_paths
            for part in Path(path).parts
        ),
    }
    text = f"{task}\n{success_criteria}".lower()
    category = (
        classification.category.strip().lower()
        if classification.category.strip()
        else None
    )
    broad = bool(
        re.search(
            r"\b(rewrite|migrate|architecture|across|entire|all components)\b", text
        )
    )
    likely_count = int(
        classification.signals.get("likely_file_count")
        or classification.signals.get("relevant_file_count")
        or 0
    )
    radius = (
        "large"
        if broad or likely_count >= 8
        else "medium"
        if likely_count >= 3 or classification.difficulty != "easy"
        else "small"
    )
    security_paths = sorted(
        path
        for path in paths
        if any(
            (
                part.lower().replace("-", "_") in _SECURITY_PARTS
                or Path(part).stem.lower().replace("-", "_") in _SECURITY_PARTS
            )
            for part in Path(path).parts
        )
    )
    tools = set(str(value).lower() for value in classification.required_capabilities)
    for pattern, tool in (
        (r"\bdocker\b", "docker"),
        (r"\bkubernetes|\bk8s\b", "kubernetes"),
        (r"\bpostgres", "postgresql"),
        (r"\bnode|\bnpm\b", "node"),
        (r"\bpytest\b", "pytest"),
        (r"\bgit\b", "git"),
    ):
        if re.search(pattern, text):
            tools.add(tool)
    context = {
        "repository_bytes": sum(row[1] for row in rows),
        "estimated_repository_tokens": (sum(row[1] for row in rows) + 3) // 4,
        "task_tokens": (
            len(task.encode("utf-8")) + len(success_criteria.encode("utf-8")) + 3
        )
        // 4,
        "likely_context_bytes": sum(
            row[1] for row in rows[: min(max(likely_count, 1), 20)]
        ),
    }
    aggregate = _safe_aggregates(historical_aggregates)
    aggregate_prov = FeatureProvenance(
        source_kind="aggregate", source_id=historical_snapshot_id or "missing"
    )
    input_provenance = (repo_prov, task_prov, cls_prov, aggregate_prov)
    return TaskFeatures(
        run_id=run_id,
        repository_snapshot_id=f"sha256:{snapshot_digest}",
        repository_size_bytes=_value(
            "repository_size", sum(row[1] for row in rows), repo_prov
        ),
        repository_file_count=_value("repository_size", len(rows), repo_prov),
        detected_languages=_value("languages", languages, repo_prov),
        detected_build_systems=_value("build_systems", builds, repo_prov),
        dependency_lockfiles=_value("dependencies", locks, repo_prov),
        test_topology=_value("test_topology", topology, repo_prov),
        requested_change_category=_value(
            "change_category", category, task_prov, cls_prov
        ),
        estimated_change_radius=_value("change_radius", radius, task_prov, cls_prov),
        security_sensitive_paths=_value("security_paths", security_paths, repo_prov),
        required_tools_capabilities=_value(
            "required_tools", sorted(tools), task_prov, cls_prov
        ),
        context_size_estimates=_value("context_size", context, repo_prov, task_prov),
        historical_aggregates=_value("historical", aggregate, aggregate_prov),
        input_provenance=input_provenance,
    )
