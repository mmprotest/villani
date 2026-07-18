from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from villani_code.state import Runner


def _is_runtime_artifact_path(path: str) -> bool:
    normalized = str(path).strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    return ".villani_code" in parts


def _filter_model_facing_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if not _is_runtime_artifact_path(path)]


def _git_output(runner: "Runner", *args: str, limit: int) -> str:
    repo = getattr(runner, "repo", None)
    if repo is None:
        return ""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "")[:limit]


def build_model_context_packet(runner: "Runner") -> dict[str, Any]:
    mission = getattr(runner, "_mission_state", None)
    constraints = []
    contract = getattr(runner, "_task_contract", {}) or {}
    if contract:
        constraints.append(f"Success predicate: {contract.get('success_predicate', '')}")
        constraints.extend([f"No-go: {p}" for p in contract.get("no_go_paths", [])[:4]])
    skill_guidance = [getattr(skill, "guidance", "") for skill in getattr(runner, "skills", []) if getattr(skill, "guidance", "")]
    ledger = getattr(runner, "_context_ledger", None)
    active_items = list(ledger.active_items()) if ledger is not None else []
    relevant_excerpts = [
        {
            "item_id": item.item_id,
            "reference": item.source_reference,
            "digest": item.content_digest,
            "content": item.content[:2_400],
        }
        for item in active_items
        if item.source_type == "file_excerpt"
    ][-8:]
    latest_tool_outputs = [
        {
            "item_id": item.item_id,
            "source_type": item.source_type,
            "reference": item.source_reference,
            "summary": item.summary[:300],
        }
        for item in active_items
        if item.source_type in {"search_result", "tool_output", "command_output"}
    ][-8:]
    latest_validation = next(
        (
            item.content[:4_000]
            for item in reversed(active_items)
            if item.source_type == "validation"
        ),
        "",
    )
    return {
        "objective": getattr(mission, "objective", ""),
        "success_criteria": str(contract.get("success_predicate", "")),
        "runtime_mode": getattr(mission, "mode", getattr(runner, "_runtime_mode", "execution")),
        "current_step": getattr(mission, "current_step_id", ""),
        "plan_summary": getattr(mission, "plan_summary", ""),
        "verified_facts": [f.value for f in getattr(mission, "verified_facts", [])],
        "open_hypotheses": [h.statement for h in getattr(mission, "open_hypotheses", [])],
        "intended_targets": _filter_model_facing_paths(list(getattr(mission, "intended_targets", []))),
        "changed_files": _filter_model_facing_paths(list(getattr(mission, "changed_files", []))),
        "last_failed_command": getattr(mission, "last_failed_command", ""),
        "unresolved_failures": [
            value
            for value in [
                getattr(mission, "last_failed_summary", ""),
                *list(getattr(mission, "validation_failures", [])),
            ]
            if value
        ],
        "validation_failures": list(getattr(mission, "validation_failures", [])),
        "latest_validation": latest_validation,
        "candidate_diff": _git_output(
            runner,
            "diff",
            "--binary",
            "HEAD",
            "--",
            limit=16_000,
        ),
        "repository_state_summary": _git_output(
            runner,
            "status",
            "--short",
            "--untracked-files=all",
            limit=4_000,
        ),
        "relevant_file_excerpts": relevant_excerpts,
        "latest_relevant_tool_outputs": latest_tool_outputs,
        "compact_recent_actions": getattr(mission, "compact_summary", ""),
        "constraints": constraints,
        "repo_root": str(getattr(runner, "repo", "")),
        "skill_guidance": [s for s in skill_guidance if s][:8],
    }


def render_model_context_packet(packet: dict[str, Any]) -> str:
    lines = [
        "Mission context packet:",
        "Original task (preserve verbatim):",
        str(packet.get("objective", "")),
        "Success criteria:",
        str(packet.get("success_criteria", "")),
        f"Mode: {packet.get('runtime_mode', '')}",
        f"Current step: {packet.get('current_step', '')}",
        f"Plan summary: {packet.get('plan_summary', '')}",
        f"Intended targets: {', '.join(packet.get('intended_targets', []))}",
        f"Changed files: {', '.join(packet.get('changed_files', []))}",
        f"Last failed command: {packet.get('last_failed_command', '')}",
        f"Validation failures: {' | '.join(packet.get('validation_failures', []))}",
        f"Compact actions: {packet.get('compact_recent_actions', '')}",
    ]
    unresolved = packet.get("unresolved_failures", [])
    if unresolved:
        lines.append("Unresolved failures:")
        lines.extend(f"- {value}" for value in unresolved[:8])
    latest_validation = str(packet.get("latest_validation", "")).strip()
    if latest_validation:
        lines.extend(["Latest validation result:", latest_validation])
    repository_state = str(packet.get("repository_state_summary", "")).strip()
    if repository_state:
        lines.extend(["Current repository state:", repository_state])
    candidate_diff = str(packet.get("candidate_diff", "")).strip()
    if candidate_diff:
        lines.extend(["Current candidate diff:", candidate_diff])
    excerpts = packet.get("relevant_file_excerpts", [])
    if excerpts:
        lines.append("Current relevant files and excerpts:")
        for excerpt in excerpts:
            lines.append(
                f"- [{excerpt.get('item_id')}] {excerpt.get('reference')} "
                f"digest={str(excerpt.get('digest', ''))[:12]}"
            )
            lines.append(str(excerpt.get("content", "")))
    latest_outputs = packet.get("latest_relevant_tool_outputs", [])
    if latest_outputs:
        lines.append("Latest relevant tool outputs:")
        lines.extend(
            f"- [{item.get('item_id')}] {item.get('source_type')} "
            f"{item.get('reference')}: {item.get('summary')}"
            for item in latest_outputs
        )
    constraints = packet.get("constraints", [])
    if constraints:
        lines.append("Constraints:")
        lines.extend(f"- {c}" for c in constraints[:8])
    guidance = packet.get("skill_guidance", [])
    if guidance:
        lines.append("Skill guidance:")
        lines.extend(f"- {g}" for g in guidance[:6])
    return "\n".join(lines)
