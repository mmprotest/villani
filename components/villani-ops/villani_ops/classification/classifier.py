from __future__ import annotations
from pathlib import Path
from typing import Any, Literal, cast
import subprocess
import json
import re
from villani_ops.core.backend import Backend, select_backend
from villani_ops.core.task import Task, TaskClassification
from villani_ops.llm.client import LLMClient, LLMCallResult
from villani_ops.policy_engine.engine import _write_controller_error
from .prompts import SYSTEM, USER
from .context import (
    collect_relevant_file_snippets,
    RelevantFileSnippet,
    is_skipped_repo_file,
)

_NORMALIZED_CLASSIFICATION_WARNING = "Classification failed validation after normalization, so Villani Ops used deterministic fallback classification."


def _repo_tree(repo: Path) -> list[str]:
    files: list[str] = []
    for p in repo.rglob("*"):
        if len(files) >= 200:
            break
        if p.is_file():
            rel = p.relative_to(repo).as_posix()
            if not is_skipped_repo_file(rel):
                files.append(rel)
    return files


def _repo_context(repo: Path) -> str:
    def run(args):
        try:
            return subprocess.run(
                args, cwd=repo, text=True, capture_output=True, timeout=5
            ).stdout.strip()
        except Exception as e:
            return f"ERROR: {e}"

    files = _repo_tree(repo)
    package = [
        f
        for f in files
        if Path(f).name
        in {
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "requirements.txt",
        }
    ]
    return json.dumps(
        {
            "repo_path": str(repo),
            "tree": files[:80],
            "package_files": package,
            "git_status": run(["git", "status", "--porcelain"]),
        },
        indent=2,
    )


def _canonical_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[/\\-]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def _normalize_enum(
    value: Any, mapping: dict[str, str], allowed: set[str], default: str
) -> str:
    key = _canonical_key(value)
    if key in allowed:
        return key
    return mapping.get(key, default)


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def _coerce_attempts(value: Any) -> int:
    if isinstance(value, bool):
        return 2
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        return 2
    return max(1, min(attempts, 5))


def _coerce_needs_tests(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        key = _canonical_key(value)
        if key in {"true", "yes", "needed"}:
            return True
        if key in {"false", "no", "not_needed"}:
            return False
    return True


def _coerce_confidence(value: Any) -> float:
    try:
        if isinstance(value, str):
            text = value.strip()
            if text.endswith("%"):
                conf = float(text[:-1].strip()) / 100
            else:
                conf = float(text)
        else:
            conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(conf, 1.0))


def fallback_task_classification_payload() -> dict[str, Any]:
    return {
        "difficulty": "medium",
        "category": "unknown",
        "risk": "medium",
        "estimated_attempts_needed": 2,
        "needs_tests": True,
        "likely_files": [],
        "required_capabilities": [],
        "reasoning_summary": _NORMALIZED_CLASSIFICATION_WARNING,
        "confidence": 0.0,
    }


def normalize_task_classification_payload(raw: dict) -> dict:
    difficulty_map = {
        "trivial": "easy",
        "simple": "easy",
        "straightforward": "easy",
        "low": "easy",
        "low_complexity": "easy",
        "minor": "easy",
        "moderate": "medium",
        "intermediate": "medium",
        "normal": "medium",
        "standard": "medium",
        "medium_complexity": "medium",
        "complex": "hard",
        "difficult": "hard",
        "high": "hard",
        "high_complexity": "hard",
        "very_hard": "hard",
        "very_difficult": "hard",
        "challenging": "hard",
    }
    risk_map = {
        "minimal": "low",
        "minor": "low",
        "low_risk": "low",
        "moderate": "medium",
        "medium_risk": "medium",
        "low_medium": "medium",
        "medium_low": "medium",
        "low_to_medium": "medium",
        "medium_to_low": "medium",
        "significant": "high",
        "severe": "high",
        "critical": "high",
        "high_risk": "high",
    }
    payload = dict(raw or {})
    payload["difficulty"] = _normalize_enum(
        payload.get("difficulty"), difficulty_map, {"easy", "medium", "hard"}, "medium"
    )
    payload["risk"] = _normalize_enum(
        payload.get("risk"), risk_map, {"low", "medium", "high"}, "medium"
    )
    payload["category"] = (
        payload.get("category")
        if isinstance(payload.get("category"), str) and payload.get("category")
        else "unknown"
    )
    payload["estimated_attempts_needed"] = _coerce_attempts(
        payload.get("estimated_attempts_needed")
    )
    payload["needs_tests"] = _coerce_needs_tests(payload.get("needs_tests"))
    payload["likely_files"] = _coerce_list(payload.get("likely_files"))
    payload["required_capabilities"] = _coerce_list(
        payload.get("required_capabilities")
    )
    payload["reasoning_summary"] = (
        payload.get("reasoning_summary")
        if isinstance(payload.get("reasoning_summary"), str)
        else "Classification normalized from local model output."
    )
    payload["confidence"] = _coerce_confidence(payload.get("confidence"))
    return payload


_BEHAVIOR_STOP_WORDS = {
    "change",
    "all",
    "and",
    "criteria",
    "ensure",
    "for",
    "implement",
    "make",
    "must",
    "repository",
    "that",
    "the",
    "update",
    "with",
}


def _normalized_clauses(text: str) -> list[str]:
    clauses: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[.;\n]+|\band\b", (text or "").casefold()):
        raw_text = " ".join(raw.split())
        if re.fullmatch(r"[a-z][a-z ]{1,40}:\s*[a-z0-9_.-]+", raw_text):
            # A labelled identifier supplies context, not another requested behavior.
            continue
        normalized = re.sub(r"[^a-z0-9_./\\-]+", " ", raw)
        normalized = " ".join(normalized.split())
        if len(normalized.split()) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        clauses.append(normalized)
    return clauses


def _clause_key(clause: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[a-z][a-z0-9_-]{2,}", clause)
        if token not in _BEHAVIOR_STOP_WORDS
    )


def _behavior_signals(text: str) -> dict[str, Any]:
    """Count distinct behaviors after removing restatements and validation."""

    behavior_keys: list[frozenset[str]] = []
    duplicate_count = 0
    validation_count = 0
    constraint_count = 0
    clauses = _normalized_clauses(text)
    for clause in clauses:
        if re.search(
            r"\b(?:do not|must not|never|only|without|avoid|preserve)\b", clause
        ):
            constraint_count += 1
            continue
        validation_subject = re.search(
            r"\b(?:tests?|checks?|suite|validation|lint|typecheck|build)\b", clause
        )
        validation_action = re.search(
            r"\b(?:add|pass|run|execute|green|succeed|fail|cover)\w*\b", clause
        )
        command_shaped_validation = bool(
            re.search(r"\b(?:pass|succeed|fail)\w*\b", clause)
            and re.search(r"(?:^|\s)-{1,2}[a-z0-9][a-z0-9-]*(?:\s|$)", clause)
        )
        if (validation_subject and validation_action) or command_shaped_validation:
            validation_count += 1
            continue
        key = _clause_key(clause)
        if not key:
            continue
        duplicate = False
        for existing in behavior_keys:
            overlap = len(key & existing)
            union = len(key | existing)
            if key <= existing or existing <= key or (union and overlap / union >= 0.7):
                duplicate = True
                break
        if duplicate:
            duplicate_count += 1
        else:
            behavior_keys.append(key)
    return {
        "behavior_count": len(behavior_keys),
        "normalized_behavior_keys": [sorted(item) for item in behavior_keys],
        "duplicate_or_restatement_count": duplicate_count,
        "validation_clause_count": validation_count,
        "constraint_clause_count": constraint_count,
        "acceptance_clause_count": len(clauses),
    }


def _shape_signals(
    task_text: str,
    snippets: list[RelevantFileSnippet],
    likely_files: list[str] | None = None,
) -> dict[str, Any]:
    text = (task_text or "").lower()
    paths = sorted(
        dict.fromkeys(
            [
                *(snippet.path for snippet in snippets),
                *(str(path) for path in (likely_files or [])),
            ]
        )
    )
    subsystem_names = sorted(
        {
            normalized.split("/", 1)[0]
            for path in paths
            for normalized in [path.replace("\\", "/").removeprefix("./")]
            if "/" in normalized
        }
    )
    behavior = _behavior_signals(text)
    risk_markers = sorted(
        marker
        for marker in (
            "authentication",
            "authorization",
            "concurrency",
            "encryption",
            "migration",
            "permission",
            "public api",
            "schema",
            "security",
            "transaction",
        )
        if marker in text
    )
    dependency_uncertainty = bool(
        re.search(
            r"\b(?:dependency|dependencies|third[- ]party|package upgrade|"
            r"new package|external service|unknown integration)\b",
            text,
        )
    )
    broad_change = bool(
        re.search(
            r"\b(?:entire app|across (?:the )?repository|architecture|redesign|"
            r"migrate|rewrite|replatform|multiple subsystems)\b",
            text,
        )
    )
    breadth = len(paths)
    scope = (
        "broad"
        if broad_change or breadth >= 8 or len(subsystem_names) >= 4
        else "moderate"
        if breadth >= 4 or len(subsystem_names) >= 2
        else "narrow"
    )
    return {
        "relevant_file_count": len(snippets),
        "likely_file_count": len(likely_files or []),
        "repository_breadth": breadth,
        "explicit_tests_mentioned": bool(
            re.search(r"\b(pytest|tests?/|test_\w+|tests?)\b", text)
        ),
        "failing_tests_mentioned": bool(
            re.search(r"\b(failing|failed|failure|regression)\b", text)
        ),
        "do_not_change_tests": bool(
            re.search(
                r"do not (change|modify|edit) tests|don['’]t (change|modify|edit) tests",
                text,
            )
        ),
        "target_files_found": bool(snippets),
        "broad_change": broad_change,
        "subsystem_count": len(subsystem_names),
        "subsystem_names": subsystem_names,
        "risk_signal_count": len(risk_markers),
        "risk_signals": risk_markers,
        "dependency_uncertainty": dependency_uncertainty,
        "validation_burden": behavior["validation_clause_count"],
        "scope": scope,
        **behavior,
    }


def _lower_level(value: str, levels: list[str]) -> str:
    try:
        i = levels.index(value)
    except ValueError:
        return value
    return levels[max(0, i - 1)]


def adjust_classification_from_task_shape(
    classification: TaskClassification,
    task: str,
    relevant_files: list[RelevantFileSnippet],
) -> TaskClassification:
    cls = classification.model_copy(deep=True)
    signals = _shape_signals(task, relevant_files, cls.likely_files)
    notes = list(cls.adjustment_notes)
    original_difficulty, original_risk = cls.difficulty, cls.risk
    narrow = (
        signals["scope"] == "narrow"
        and signals["behavior_count"] <= 1
        and signals["risk_signal_count"] == 0
        and not signals["dependency_uncertainty"]
        and signals["target_files_found"]
    )
    tests_clear = (
        signals["explicit_tests_mentioned"] or signals["failing_tests_mentioned"]
        or signals["validation_clause_count"] > 0
    )
    if cls.confidence >= 0.5 and narrow and tests_clear and not signals["broad_change"]:
        new = _lower_level(cls.risk, ["low", "medium", "high"])
        if new != cls.risk:
            notes.append(
                f"Classification adjusted: risk {cls.risk} -> {new} because relevant context is narrow and explicit tests or success criteria are present."
            )
            cls.risk = cast(Literal["low", "medium", "high"], new)
    if (
        cls.confidence >= 0.80
        and narrow
        and tests_clear
        and not signals["broad_change"]
        and cls.estimated_attempts_needed <= 1
    ):
        new = _lower_level(cls.difficulty, ["easy", "medium", "hard"])
        if new != cls.difficulty:
            notes.append(
                f"Classification adjusted: difficulty {cls.difficulty} -> {new} because relevant context is narrow, validation is explicit, and confidence is high."
            )
            cls.difficulty = cast(Literal["easy", "medium", "hard"], new)
    medium_reasons = []
    hard_reasons = []
    if signals["likely_file_count"] >= 4:
        medium_reasons.append(f"task spans {signals['likely_file_count']} likely files")
    if signals["relevant_file_count"] >= 4:
        medium_reasons.append(
            f"task spans {signals['relevant_file_count']} relevant files"
        )
    if cls.estimated_attempts_needed >= 3:
        medium_reasons.append(
            f"estimated_attempts_needed={cls.estimated_attempts_needed}"
        )
    if signals["behavior_count"] >= 3:
        medium_reasons.append("success criteria mention multiple behaviours")
    if signals["subsystem_count"] >= 2:
        medium_reasons.append(
            f"task spans {signals['subsystem_count']} repository subsystems"
        )
    if signals["risk_signal_count"] >= 2:
        medium_reasons.append("task carries multiple material risk signals")
    if signals["dependency_uncertainty"]:
        medium_reasons.append("dependency behavior is uncertain")
    if signals["validation_burden"] >= 3:
        medium_reasons.append("validation burden spans three or more clauses")
    if str(cls.category).lower() in {"integration", "workflow"}:
        medium_reasons.append(f"category={cls.category}")
    if signals["likely_file_count"] >= 8:
        hard_reasons.append(f"task spans {signals['likely_file_count']} likely files")
    if signals["relevant_file_count"] >= 8:
        hard_reasons.append(
            f"task spans {signals['relevant_file_count']} relevant files"
        )
    if cls.estimated_attempts_needed >= 5:
        hard_reasons.append(
            f"estimated_attempts_needed={cls.estimated_attempts_needed}"
        )
    if signals["behavior_count"] >= 6:
        hard_reasons.append("success criteria mention six or more behaviours")
    if signals["subsystem_count"] >= 4:
        hard_reasons.append(
            f"task spans {signals['subsystem_count']} repository subsystems"
        )
    if signals["scope"] == "broad" and signals["risk_signal_count"] >= 2:
        hard_reasons.append("broad scope carries multiple material risk signals")
    if hard_reasons and cls.difficulty != "hard":
        notes.append(
            "Raised difficulty to hard because " + " and ".join(hard_reasons) + "."
        )
        cls.difficulty = "hard"
    elif medium_reasons and cls.difficulty == "easy":
        notes.append(
            "Raised difficulty to medium because " + " and ".join(medium_reasons) + "."
        )
        cls.difficulty = "medium"
    cls.adjustment_notes = notes
    cls.relevant_file_paths = [s.path for s in relevant_files]
    cls.task_shape_signals = signals
    cls.original_difficulty = original_difficulty
    cls.original_risk = original_risk
    return cls


class TaskClassifier:
    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient()

    def select_backend(self, backends: dict[str, Backend]) -> Backend:
        return select_backend(backends, "classification")

    def classify(
        self,
        task: Task,
        backends: dict[str, Backend],
        out_path: str | Path | None = None,
        backend_override: Backend | None = None,
        estimate_cost: bool = True,
    ) -> tuple[TaskClassification, LLMCallResult]:
        backend = backend_override or self.select_backend(backends)
        repo = Path(task.repo_path).resolve()
        tree = _repo_tree(repo)
        task_parts = [
            str(value or "").strip()
            for value in (
                task.objective,
                task.instruction,
                task.success_criteria,
                "\n".join(task.constraints),
            )
            if str(value or "").strip()
        ]
        task_text = "\n".join(dict.fromkeys(task_parts))
        snippets = collect_relevant_file_snippets(repo, task_text, tree)
        relevant = [
            {"path": s.path, "reason": s.reason, "content_excerpt": s.content_excerpt}
            for s in snippets
        ]
        context = {
            "objective": task.objective,
            "success_criteria": task.success_criteria,
            "constraints": task.constraints,
            "repo": _repo_context(repo),
            "relevant_files": relevant,
        }

        try:
            result = self.client.complete_json(
                backend,
                SYSTEM,
                USER.format(context=json.dumps(context, indent=2)),
                "TaskClassification",
                estimate_cost=estimate_cost,
            )
        except TypeError:
            result = self.client.complete_json(
                backend,
                SYSTEM,
                USER.format(context=json.dumps(context, indent=2)),
                "TaskClassification",
            )
        normalized = normalize_task_classification_payload(result.parsed_json)
        try:
            cls = TaskClassification.model_validate(normalized)
            cls = adjust_classification_from_task_shape(cls, task_text, snippets)
        except Exception as e:
            fallback = fallback_task_classification_payload()
            _write_controller_error(
                Path(out_path).parent if out_path else None,
                "classification",
                backend,
                "TaskClassification",
                result,
                validation_error=str(e),
                normalized_payload=normalized,
                raw_payload=result.parsed_json,
                fallback_used=True,
                fallback_payload=fallback,
            )
            result.error = _NORMALIZED_CLASSIFICATION_WARNING
            cls = TaskClassification.model_validate(fallback)
            cls = adjust_classification_from_task_shape(cls, task_text, snippets)
        if out_path:
            Path(out_path).write_text(cls.model_dump_json(indent=2))
        return cls, result
