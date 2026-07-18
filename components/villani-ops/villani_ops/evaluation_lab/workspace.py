"""Content-addressed task capture and portable evaluation-suite storage."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from villani_ops.closed_loop.durable_io import write_json_atomic

from .models import (
    EvaluationSuite,
    EvaluationTask,
    EvaluationTaskReference,
    EvaluatorOnlyMaterial,
    FileChangeRequirement,
    LocalComputeConfiguration,
    SetupCommand,
    SourceSnapshot,
    TaskProvenance,
    ValidationCommand,
)


BUILTIN_EXCLUDED_PATTERNS = (
    ".git",
    ".git/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/credentials*",
    "**/secrets*",
    ".villani",
    ".villani/**",
    "node_modules",
    "node_modules/**",
    "**/__pycache__/**",
)

_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        rb"(?i)\b(api[_-]?key|access[_-]?token|password|client[_-]?secret)\b\s*[:=]\s*['\"][^'\"\r\n]{8,}['\"]"
    ),
    re.compile(
        rb"(?i)\b(api[_-]?key|access[_-]?token|authorization|password|client[_-]?secret)\b\s*[:=]\s*(?:bearer\s+)?['\"]?[A-Za-z0-9_./+=:-]{12,}"
    ),
)

_RUNNER_FORBIDDEN_KEYS = {
    "task_id",
    "task_name",
    "expected_patch",
    "expected_files",
    "future_context",
    "hidden_checks",
    "evaluator_only",
    "arm",
    "route",
    "cost",
    "harness",
    "competing_candidates",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _model_digest(value: EvaluationSuite | EvaluationTask) -> str:
    document = value.model_dump(mode="json")
    document["content_digest"] = None
    return canonical_digest(document)


def _safe_relative(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe repository path: {value!r}")
    return path.as_posix()


def _matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    for raw in patterns:
        pattern = raw.replace("\\", "/").strip()
        if not pattern:
            continue
        prefix = pattern.rstrip("/")
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
        if fnmatch.fnmatchcase(normalized, pattern):
            return True
    return False


def contains_secret(data: bytes) -> bool:
    return any(pattern.search(data) is not None for pattern in _SECRET_PATTERNS)


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(arguments)} failed: {message}")
    return result


def _repository_identity(repository: Path) -> str:
    remote = _git(repository, "remote", "get-url", "origin", check=False)
    value = remote.stdout.decode("utf-8", errors="replace").strip()
    if value:
        # Preserve enough identity for the two-repository gate without putting a
        # credential-bearing URL in evidence.
        sanitized = re.sub(r"(?i)://[^/@]+@", "://", value)
        return "repo_" + hashlib.sha256(sanitized.encode()).hexdigest()[:20]
    return "repo_" + hashlib.sha256(str(repository.resolve()).encode()).hexdigest()[:20]


def init_suite(
    suite_directory: str | Path,
    *,
    title: str,
    suite_id: str | None = None,
    randomization_seed: str | None = None,
    evidence_kind: str = "real_founder_work",
    confidentiality: str = "internal",
    measured_power_watts: float | None = None,
    electricity_price_per_kwh: float | None = None,
    currency: str | None = None,
) -> EvaluationSuite:
    root = Path(suite_directory).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"evaluation suite directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "baselines").mkdir()
    (root / "tasks").mkdir()
    (root / "trials").mkdir()
    (root / "evaluator-only").mkdir()
    now = utc_now()
    identity = suite_id or f"suite_{hashlib.sha256((title + now.isoformat()).encode()).hexdigest()[:16]}"
    seed = randomization_seed or hashlib.sha256(os.urandom(32)).hexdigest()
    suite = EvaluationSuite(
        suite_id=identity,
        title=title,
        suite_version=1,
        status="draft",
        created_at=now,
        randomization_seed=seed,
        evidence_kind=evidence_kind,
        confidentiality=confidentiality,
        local_compute=LocalComputeConfiguration(
            measured_power_watts=measured_power_watts,
            electricity_price_per_kwh=electricity_price_per_kwh,
            currency=currency.upper() if currency else None,
        ),
    )
    write_json_atomic(root / "suite.json", suite.model_dump(mode="json"))
    return suite


def load_suite(suite_directory: str | Path) -> EvaluationSuite:
    path = Path(suite_directory).expanduser().resolve() / "suite.json"
    return EvaluationSuite.model_validate_json(path.read_text(encoding="utf-8"))


def _baseline_entries(
    repository: Path,
    commit: str,
    *,
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
) -> tuple[list[tuple[str, str, str, bytes]], list[str]]:
    tree = _git(repository, "ls-tree", "-r", "-z", "--full-tree", commit).stdout
    included: list[tuple[str, str, str, bytes]] = []
    excluded: list[str] = []
    all_exclusions = (*BUILTIN_EXCLUDED_PATTERNS, *exclude_patterns)
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        path = _safe_relative(raw_path.decode("utf-8", errors="surrogateescape"))
        if object_type != "blob" or mode in {"120000", "160000"}:
            excluded.append(path)
            continue
        if include_patterns and not _matches(path, include_patterns):
            excluded.append(path)
            continue
        if _matches(path, all_exclusions):
            excluded.append(path)
            continue
        data = _git(repository, "cat-file", "blob", object_id).stdout
        if contains_secret(data):
            raise ValueError(
                f"possible secret in {path}; exclude the file explicitly before capture"
            )
        included.append((path, mode, object_id, data))
    if not included:
        raise ValueError("baseline selection contains no allowed regular files")
    included.sort(key=lambda item: item[0])
    return included, sorted(set(excluded))


def import_baseline(
    suite_directory: str | Path,
    *,
    repository: str | Path,
    commit: str = "HEAD",
    include_patterns: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
) -> SourceSnapshot:
    root = Path(suite_directory).expanduser().resolve()
    suite = load_suite(root)
    if suite.status != "draft":
        raise ValueError("cannot import a baseline into a frozen suite")
    source = Path(repository).expanduser().resolve()
    resolved = _git(source, "rev-parse", f"{commit}^{{commit}}").stdout.decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40,64}", resolved):
        raise ValueError("baseline must resolve to a full immutable commit")
    entries, excluded = _baseline_entries(
        source,
        resolved,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
    )
    manifest_entries = [
        {"path": path, "mode": mode, "sha256": hashlib.sha256(data).hexdigest()}
        for path, mode, _object_id, data in entries
    ]
    baseline_digest = canonical_digest(manifest_entries)
    baseline_root = root / "baselines" / baseline_digest
    baseline_root.mkdir(parents=True, exist_ok=False)
    archive = baseline_root / "code.zip"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".code.", suffix=".zip", dir=baseline_root
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path, mode, _object_id, data in entries:
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if mode == "100755" else 0o644) << 16
                bundle.writestr(info, data)
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    archive_digest = file_sha256(archive)
    snapshot = SourceSnapshot(
        repository_identity=_repository_identity(source),
        resolved_commit=resolved,
        baseline_digest=baseline_digest,
        archive_digest=archive_digest,
        archive_path=f"baselines/{baseline_digest}/code.zip",
        included_paths=[item["path"] for item in manifest_entries],
        excluded_paths=excluded,
        file_count=len(entries),
        restore_verified=True,
    )
    write_json_atomic(
        baseline_root / "baseline.json",
        {
            "schema_version": "villani.evaluation_baseline.v1",
            "source_snapshot": snapshot.model_dump(mode="json"),
            "files": manifest_entries,
        },
    )
    with tempfile.TemporaryDirectory(prefix="villani-eval-restore-") as temporary_root:
        restored = Path(temporary_root) / "repo"
        restored_digest = restore_snapshot(root, snapshot, restored, initialize_git=False)
        if restored_digest != baseline_digest:
            shutil.rmtree(baseline_root, ignore_errors=True)
            raise RuntimeError("captured snapshot could not be restored byte-for-byte")
    return snapshot


def load_snapshot(suite_directory: str | Path, baseline_digest: str) -> SourceSnapshot:
    root = Path(suite_directory).expanduser().resolve()
    path = root / "baselines" / baseline_digest / "baseline.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    return SourceSnapshot.model_validate(document["source_snapshot"])


def _tree_digest(directory: Path) -> str:
    entries: list[dict[str, str]] = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        if ".git" in path.relative_to(directory).parts:
            continue
        relative = path.relative_to(directory).as_posix()
        entries.append({"path": relative, "mode": "100644", "sha256": file_sha256(path)})
    # Executable mode is material to the archive manifest. Recover it from ZIP
    # rather than the host filesystem on Windows.
    return canonical_digest(entries)


def restore_snapshot(
    suite_directory: str | Path,
    snapshot: SourceSnapshot,
    destination: str | Path,
    *,
    initialize_git: bool = True,
) -> str:
    root = Path(suite_directory).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise ValueError(f"snapshot destination already exists: {target}")
    archive = (root / snapshot.archive_path).resolve()
    if not archive.is_relative_to(root) or file_sha256(archive) != snapshot.archive_digest:
        raise ValueError("baseline archive is missing, outside the suite, or changed")
    target.mkdir(parents=True)
    restored_entries: list[dict[str, str]] = []
    with zipfile.ZipFile(archive) as bundle:
        for info in sorted(bundle.infolist(), key=lambda item: item.filename):
            relative = _safe_relative(info.filename)
            destination_path = (target / relative).resolve()
            if not destination_path.is_relative_to(target):
                raise ValueError("baseline archive contains a traversal path")
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            data = bundle.read(info)
            if contains_secret(data):
                raise ValueError(f"baseline archive contains possible secret material: {relative}")
            destination_path.write_bytes(data)
            executable = bool((info.external_attr >> 16) & 0o111)
            if executable and os.name != "nt":
                destination_path.chmod(0o755)
            restored_entries.append(
                {
                    "path": relative,
                    "mode": "100755" if executable else "100644",
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    digest = canonical_digest(restored_entries)
    if digest != snapshot.baseline_digest:
        raise ValueError("restored baseline digest does not match the frozen snapshot")
    if initialize_git:
        _git(target, "init", "-q")
        _git(target, "config", "user.email", "evaluation@villani.invalid")
        _git(target, "config", "user.name", "Villani Evaluation")
        _git(target, "add", "-A")
        environment = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
        result = subprocess.run(
            ["git", "commit", "-qm", "immutable evaluation baseline"],
            cwd=target,
            env=environment,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError("could not initialize restored Git baseline")
    return digest


def _copy_evaluator_material(
    root: Path,
    task_id: str,
    kind: str,
    paths: Sequence[str | Path],
) -> list[str]:
    references: list[str] = []
    destination_root = root / "evaluator-only" / task_id / kind
    for index, raw in enumerate(paths, start=1):
        source = Path(raw).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"evaluator-only material is not a file: {source}")
        data = source.read_bytes()
        if contains_secret(data):
            raise ValueError(f"possible secret in evaluator-only material: {source.name}")
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / f"{index:03d}-{source.name}"
        destination.write_bytes(data)
        references.append(destination.relative_to(root).as_posix())
    return references


def add_task(
    suite_directory: str | Path,
    *,
    baseline_digest: str,
    verbatim_task: str,
    success_criteria: Sequence[str],
    validation: Sequence[ValidationCommand],
    task_id: str | None = None,
    allowed_setup: Sequence[SetupCommand] = (),
    file_change_requirement: FileChangeRequirement | None = None,
    captured_by: str = "founder",
    source_reference: str = "founder_work",
    risk_labels: Sequence[str] = (),
    category_labels: Sequence[str] = (),
    secret_exclusions: Sequence[str] = (),
    hidden_check_files: Sequence[str | Path] = (),
    future_context_files: Sequence[str | Path] = (),
    confidentiality: str = "internal",
    evidence_kind: str | None = None,
) -> EvaluationTask:
    root = Path(suite_directory).expanduser().resolve()
    suite = load_suite(root)
    if suite.status != "draft":
        raise ValueError("cannot add a task to a frozen suite")
    snapshot = load_snapshot(root, baseline_digest)
    identity = task_id or (
        "task_"
        + canonical_digest(
            {"baseline": baseline_digest, "task": verbatim_task, "criteria": list(success_criteria)}
        )[:16]
    )
    if (root / "tasks" / identity).exists():
        raise ValueError(f"task already exists: {identity}")
    capture_text = json.dumps(
        {
            "task": verbatim_task,
            "success_criteria": list(success_criteria),
            "validation": [item.model_dump(mode="json") for item in validation],
            "allowed_setup": [item.model_dump(mode="json") for item in allowed_setup],
            "source_reference": source_reference,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    if contains_secret(capture_text):
        raise ValueError(
            "possible secret in task metadata; replace it with a reference or exclusion"
        )
    hidden = _copy_evaluator_material(root, identity, "hidden-checks", hidden_check_files)
    future = _copy_evaluator_material(root, identity, "future-context", future_context_files)
    kind = evidence_kind or suite.evidence_kind
    task = EvaluationTask(
        task_id=identity,
        suite_id=suite.suite_id,
        task_version=1,
        immutable_baseline_digest=baseline_digest,
        source_snapshot=snapshot,
        verbatim_task=verbatim_task,
        success_criteria=list(success_criteria),
        authoritative_validation=list(validation),
        allowed_setup=list(allowed_setup),
        file_change_requirement=file_change_requirement or FileChangeRequirement(),
        provenance=TaskProvenance(
            captured_at=utc_now(),
            captured_by=captured_by,
            source_reference=source_reference,
            later_context_present=bool(future),
        ),
        risk_labels=sorted(set(risk_labels)),
        category_labels=sorted(set(category_labels)),
        secret_exclusions=sorted(
            set((*secret_exclusions, *snapshot.excluded_paths))
        ),
        evaluator_only=EvaluatorOnlyMaterial(
            hidden_check_references=hidden,
            future_context_references=future,
        ),
        confidentiality=confidentiality,
        evidence_kind=kind,
        evidence_eligible=kind == "real_founder_work",
        frozen=False,
    )
    task = task.model_copy(update={"content_digest": _model_digest(task)})
    destination = root / "tasks" / identity
    destination.mkdir(parents=True)
    write_json_atomic(destination / "task.json", task.model_dump(mode="json"))
    references = [item for item in suite.task_versions if item.task_id != identity]
    references.append(EvaluationTaskReference(task_id=identity, task_digest=task.content_digest or ""))
    suite = suite.model_copy(update={"task_versions": sorted(references, key=lambda item: item.task_id)})
    write_json_atomic(root / "suite.json", suite.model_dump(mode="json"))
    return task


def load_task(suite_directory: str | Path, task_id: str) -> EvaluationTask:
    path = Path(suite_directory).expanduser().resolve() / "tasks" / task_id / "task.json"
    return EvaluationTask.model_validate_json(path.read_text(encoding="utf-8"))


def _forbidden_runner_keys(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _RUNNER_FORBIDDEN_KEYS:
                findings.append("/" + "/".join((*path, key)))
            findings.extend(_forbidden_runner_keys(child, (*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(_forbidden_runner_keys(child, (*path, str(index))))
    return findings


def validate_suite(suite_directory: str | Path) -> dict[str, Any]:
    root = Path(suite_directory).expanduser().resolve()
    suite = load_suite(root)
    issues: list[dict[str, str]] = []
    tasks: list[EvaluationTask] = []
    references = {item.task_id: item.task_digest for item in suite.task_versions}
    if not references:
        issues.append({"code": "no_tasks", "message": "suite contains no tasks"})
    for task_id, expected_digest in sorted(references.items()):
        try:
            task = load_task(root, task_id)
            tasks.append(task)
            if task.content_digest != expected_digest or _model_digest(task) != expected_digest:
                issues.append({"code": "task_digest_mismatch", "message": task_id})
            forbidden = _forbidden_runner_keys(task.runner_payload())
            if forbidden:
                issues.append(
                    {"code": "runner_payload_leak", "message": ",".join(forbidden)}
                )
            if contains_secret(
                json.dumps(
                    task.runner_payload(), ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            ):
                issues.append({"code": "runner_payload_secret", "message": task_id})
            with tempfile.TemporaryDirectory(prefix="villani-eval-validate-") as temporary:
                digest = restore_snapshot(
                    root,
                    task.source_snapshot,
                    Path(temporary) / "repo",
                    initialize_git=False,
                )
            if digest != task.immutable_baseline_digest:
                issues.append({"code": "baseline_restore_mismatch", "message": task_id})
            runner_text = json.dumps(task.runner_payload(), sort_keys=True)
            for reference in (
                *task.evaluator_only.hidden_check_references,
                *task.evaluator_only.future_context_references,
            ):
                evaluator_path = (root / reference).resolve()
                if not evaluator_path.is_relative_to(root):
                    issues.append(
                        {"code": "evaluator_reference_escape", "message": task_id}
                    )
                    continue
                evaluator_data = evaluator_path.read_bytes()
                if contains_secret(evaluator_data):
                    issues.append(
                        {"code": "evaluator_material_secret", "message": task_id}
                    )
                decoded = evaluator_data.decode("utf-8", errors="ignore")
                if reference in runner_text or (decoded and decoded in runner_text):
                    issues.append({"code": "evaluator_material_leak", "message": task_id})
        except Exception as error:
            issues.append({"code": "invalid_task", "message": f"{task_id}: {error}"})
    return {
        "schema_version": "villani.evaluation_validation.v1",
        "suite_id": suite.suite_id,
        "valid": not issues,
        "task_count": len(tasks),
        "issues": issues,
        "passive_monitoring": False,
        "external_harness": False,
    }


def freeze_suite(
    suite_directory: str | Path, *, disclosure_complete: bool = False
) -> EvaluationSuite:
    root = Path(suite_directory).expanduser().resolve()
    suite = load_suite(root)
    if suite.status == "frozen":
        return suite
    validation = validate_suite(root)
    if not validation["valid"]:
        raise ValueError(f"suite validation failed: {validation['issues']}")
    references: list[EvaluationTaskReference] = []
    for item in suite.task_versions:
        task = load_task(root, item.task_id)
        frozen = task.model_copy(update={"frozen": True, "content_digest": None})
        frozen = frozen.model_copy(update={"content_digest": _model_digest(frozen)})
        write_json_atomic(
            root / "tasks" / item.task_id / "task.json",
            frozen.model_dump(mode="json"),
        )
        references.append(
            EvaluationTaskReference(task_id=item.task_id, task_digest=frozen.content_digest or "")
        )
    frozen_suite = suite.model_copy(
        update={
            "status": "frozen",
            "frozen_at": utc_now(),
            "task_versions": references,
            "disclosure_complete": disclosure_complete,
            "content_digest": None,
        }
    )
    frozen_suite = frozen_suite.model_copy(
        update={"content_digest": _model_digest(frozen_suite)}
    )
    write_json_atomic(root / "suite.json", frozen_suite.model_dump(mode="json"))
    return frozen_suite


def export_portable_suite(
    suite_directory: str | Path, output_path: str | Path
) -> Path:
    root = Path(suite_directory).expanduser().resolve()
    suite = load_suite(root)
    if suite.status != "frozen":
        raise ValueError("freeze the suite before exporting it")
    validation = validate_suite(root)
    if not validation["valid"]:
        raise ValueError(f"suite validation failed: {validation['issues']}")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            manifest = {
                "schema_version": "villani.evaluation_portable_bundle.v1",
                "suite_digest": suite.content_digest,
                "task_count": len(suite.task_versions),
                "contains_evaluator_only_material": False,
                "contains_expected_patch": False,
                "contains_actual_allowed_code": True,
            }
            bundle.writestr("manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
            baseline_written: set[str] = set()
            for index, reference in enumerate(suite.task_versions, start=1):
                task = load_task(root, reference.task_id)
                slot = f"task-{index:04d}"
                bundle.writestr(
                    f"tasks/{slot}/runner-task.json",
                    json.dumps(task.runner_payload(), ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n",
                )
                digest = task.immutable_baseline_digest
                if digest not in baseline_written:
                    archive = root / task.source_snapshot.archive_path
                    bundle.writestr(f"baselines/{digest}/code.zip", archive.read_bytes())
                    baseline_written.add(digest)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def compact_artifact_path(path: str | Path, anchor: str | Path) -> str:
    """Compact in-tree paths while accepting an external run root safely."""

    resolved = Path(path).expanduser().resolve()
    root = Path(anchor).expanduser().resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()
