"""Human presentation projected from canonical closed-loop state.

This module is intentionally read-only.  It never mutates controller state and
never upgrades advisory or heuristic evidence into acceptance authority.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .durable_io import read_jsonl_tolerant
from .event_writer import redact_data, redact_message


PRESENTATION_SCHEMA = "villani.run_presentation.v1"


FAILURE_CATALOG: dict[str, dict[str, str]] = {
    "no_backend": {
        "failed": "No enabled coding and classification backend is configured.",
        "tried": "Villani checked the local configuration before creating a run.",
        "missing": "No backend is available to classify or execute the task.",
        "next": "Run `villani setup`, then retry the task.",
    },
    "backend_unreachable": {
        "failed": "The configured model server could not be reached.",
        "tried": "Villani preserved the run and applied the configured infrastructure retry policy.",
        "missing": "No successful model response was captured.",
        "next": "Start the model server, verify its endpoint with `villani doctor`, then resume or rerun.",
    },
    "model_not_loaded": {
        "failed": "The model server is reachable but the configured model is not loaded.",
        "tried": "Villani checked the configured backend and stopped without treating availability as task failure.",
        "missing": "No loaded model can execute the selected backend route.",
        "next": "Load the configured model or select an available model, then resume or rerun.",
    },
    "invalid_repository": {
        "failed": "The selected path is not an accessible Git repository.",
        "tried": "Villani inspected the repository before creating a run.",
        "missing": "A valid Git work tree and baseline are required for isolation and safe apply.",
        "next": "Open a terminal in a Git repository or select a valid repository, then run again.",
    },
    "dirty_repository": {
        "failed": "The repository has uncommitted changes, so safe materialization cannot be proven.",
        "tried": "Villani checked the target repository before spending model cost.",
        "missing": "A clean, stable Git baseline is required.",
        "next": "Commit, stash, or discard the existing changes, then run again.",
    },
    "no_validation_command": {
        "failed": "Villani could not identify a repository validation command.",
        "tried": "Metadata discovery inspected the repository without executing any suggestion.",
        "missing": "No confirmed command can produce authoritative repository-validation evidence.",
        "next": "Provide `--validation-command` or choose and confirm a command in Villani Console.",
    },
    "validation_failure": {
        "failed": "Repository validation failed for the candidate.",
        "tried": "Villani ran the confirmed command in the isolated candidate worktree and rejected failing evidence.",
        "missing": "Passing authoritative repository-validation evidence is missing.",
        "next": "Inspect the failed command in Replay, fix the cause, then rerun.",
    },
    "verifier_unavailable": {
        "failed": "The configured verifier was unavailable or returned unusable output.",
        "tried": "Villani retried or escalated verification without rerunning or rebilling the coding attempt.",
        "missing": "A normalized acceptance-eligible verifier outcome is missing.",
        "next": "Restore the verifier, then resume the preserved run.",
    },
    "no_authoritative_evidence": {
        "failed": "The candidate has no acceptance-grade authoritative evidence.",
        "tried": "Villani retained heuristic evidence but correctly excluded the candidate from selection.",
        "missing": "A passing structured repository-validation or required verification-graph result is missing.",
        "next": "Confirm an appropriate validation command and rerun; do not apply the heuristic-only patch automatically.",
    },
    "repository_changed_before_materialization": {
        "failed": "The repository changed after verification and before materialization.",
        "tried": "Villani compared the target lineage with the recorded baseline and refused an unsafe apply.",
        "missing": "Proof that the selected recorded patch still applies to the verified baseline is missing.",
        "next": "Preserve or review the recorded patch, restore the baseline, then rerun.",
    },
    "delivery_authority_insufficient": {
        "failed": "The accepted patch did not have sufficient authority for automatic delivery.",
        "tried": "Villani evaluated the configured delivery authority after selection and failed closed.",
        "missing": "A configured authority policy permitting this risk and verifier source is missing.",
        "next": "Review the preserved patch, then rerun with `--delivery approve` or change the advanced authority policy.",
    },
    "repository_moved": {
        "failed": "The repository moved after the coding attempt was recorded.",
        "tried": "Villani compared the requested target and Git root with the persisted execution baseline.",
        "missing": "The original target repository identity is no longer available at the recorded path.",
        "next": "Restore the repository to its original path or use the preserved patch manually after review.",
    },
    "target_branch_changed": {
        "failed": "The target branch changed after the selected patch was verified.",
        "tried": "Villani compared HEAD with the persisted candidate baseline before modifying Git state.",
        "missing": "Proof that the selected patch still targets the verified commit is missing.",
        "next": "Review the preserved patch and rerun against the current target branch.",
    },
    "patch_conflict": {
        "failed": "The selected patch conflicts with the target baseline.",
        "tried": "Villani ran a non-mutating Git apply check and stopped before an unsafe partial application.",
        "missing": "A conflict-free application of the exact selected patch is missing.",
        "next": "Inspect the preserved patch and rerun against the current repository state.",
    },
    "branch_already_exists": {
        "failed": "The requested delivery branch already exists and is not Villani's recorded delivery branch.",
        "tried": "Villani checked the branch and durable delivery marker before creating a new worktree.",
        "missing": "An unambiguous branch identity for this selected patch is missing.",
        "next": "Choose a new delivery branch name or remove the unrelated branch, then retry delivery.",
    },
    "detached_head": {
        "failed": "The target repository is on a detached HEAD.",
        "tried": "Villani checked the symbolic target branch before attempting delivery.",
        "missing": "A stable original branch is required for safe delivery lineage.",
        "next": "Switch to the intended target branch and rerun.",
    },
    "push_rejected": {
        "failed": "The remote rejected the delivery branch push.",
        "tried": "Villani preserved the committed local branch and exact selected patch.",
        "missing": "The remote branch has not accepted the delivery commit.",
        "next": "Resolve remote branch protection or divergence, then push the preserved delivery branch.",
    },
    "remote_unavailable": {
        "failed": "The Git remote or provider adapter was unavailable.",
        "tried": "Villani created and preserved the local delivery branch before stopping.",
        "missing": "No confirmed remote push or pull-request URL was recorded.",
        "next": "Restore the remote or configure GitHub/GitLab, then retry from the preserved branch.",
    },
    "authentication_failure": {
        "failed": "The Git host rejected the configured credentials.",
        "tried": "Villani preserved the local branch and did not expose provider credentials in the run bundle.",
        "missing": "An authenticated push and pull-request response are missing.",
        "next": "Authenticate the configured Git-host tool, then retry delivery.",
    },
    "provider_tool_unavailable": {
        "failed": "The configured Git-host command-line adapter is unavailable.",
        "tried": "Villani preserved the selected patch and any local delivery branch before invoking the provider.",
        "missing": "No provider response or pull-request URL was recorded.",
        "next": "Install and authenticate the configured GitHub or GitLab tool, then retry delivery.",
    },
    "pull_request_creation_failed": {
        "failed": "The delivery branch was prepared, but pull-request creation failed.",
        "tried": "Villani preserved the committed local branch, push evidence, redacted PR body, and selected patch.",
        "missing": "No confirmed pull-request URL was recorded.",
        "next": "Inspect the provider error, then create or retry the pull request from the preserved branch.",
    },
    "delivery_synchronization_failure": {
        "failed": "The recorded delivery branch and its isolated worktree are out of sync.",
        "tried": "Villani compared the durable branch marker, branch ref, worktree, baseline, and selected patch.",
        "missing": "A consistent local delivery state cannot be proven safe to resume.",
        "next": "Keep the preserved patch, inspect the delivery branch state, then rerun with a new branch name.",
    },
    "approval_timeout": {
        "failed": "Delivery approval timed out under the configured fail-closed policy.",
        "tried": "Villani persisted the approval deadline, applied its timeout policy, and preserved the patch.",
        "missing": "No explicit approval was recorded before the deadline.",
        "next": "Review the patch and rerun with a new approval window if delivery is still wanted.",
    },
    "service_offline": {
        "failed": "Villani Service is offline, so Console could not submit or observe the run.",
        "tried": "Console attempted the authenticated local service boundary.",
        "missing": "No live local service connection is available.",
        "next": "Run `villani service start`, then retry. Existing local run bundles remain available.",
    },
    "synchronization_failure": {
        "failed": "The local run completed, but synchronization failed.",
        "tried": "Villani retained the canonical local bundle and queued safe retry evidence.",
        "missing": "The connected workspace has not acknowledged the local run.",
        "next": "Keep the local bundle, restore connectivity, and let Villani Service retry synchronization.",
    },
    "budget_exhausted": {
        "failed": "The configured run budget was exhausted before an eligible candidate was available.",
        "tried": "Villani stopped at the deterministic attempt, cost, or time boundary.",
        "missing": "No acceptance-eligible candidate was produced within the confirmed budget.",
        "next": "Review the attempts, then rerun with a changed budget or policy if the extra spend is acceptable.",
    },
    "timeout": {
        "failed": "The run reached its configured time limit.",
        "tried": "Villani stopped further work and preserved completed attempt evidence.",
        "missing": "The remaining controller stages did not complete before the deadline.",
        "next": "Resume if the persisted state permits it, or rerun with a longer time limit.",
    },
    "user_cancelled": {
        "failed": "The run was cancelled by the user.",
        "tried": "Villani stopped the active work and retained committed events and captured patches.",
        "missing": "No terminal acceptance decision was reached after cancellation.",
        "next": "Inspect the preserved evidence, then resume if permitted or rerun as a new run.",
    },
    "unknown_failure": {
        "failed": "The run ended without completing the requested delivery.",
        "tried": "Villani preserved canonical state and stopped without hiding the failure.",
        "missing": "Acceptance or safe materialization evidence is incomplete.",
        "next": "Open Replay for the exact event and evidence trail, then resume or rerun as advised.",
    },
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _attempt_number(
    event: Mapping[str, Any], ordinals: Mapping[str, int] | None
) -> str:
    payload = _mapping(event.get("payload"))
    ordinal = payload.get("ordinal")
    attempt_id = str(event.get("attempt_id") or payload.get("attempt_id") or "")
    if ordinal is None and ordinals is not None:
        ordinal = ordinals.get(attempt_id)
    if ordinal is None and attempt_id.startswith("attempt_"):
        try:
            ordinal = int(attempt_id.rsplit("_", 1)[1])
        except ValueError:
            pass
    return str(ordinal or attempt_id or "unknown")


def _command(payload: Mapping[str, Any]) -> str:
    argv = payload.get("argv")
    if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
        import shlex

        return shlex.join(argv)
    return "confirmed repository command"


def _is_structured_repository_validation(
    event: Mapping[str, Any], *, run_id: str, selected_attempt_id: str | None
) -> bool:
    """Accept only the canonical post-mutation validation event shape."""

    payload = _mapping(event.get("payload"))
    attempt_id = event.get("attempt_id")
    return bool(
        event.get("event_type") in {"command_completed", "command_failed"}
        and payload.get("command_role") == "repository_validation"
        and isinstance(attempt_id, str)
        and attempt_id
        and payload.get("attempt_id") == attempt_id
        and (selected_attempt_id is None or attempt_id == selected_attempt_id)
        and payload.get("run_id") == run_id
        and isinstance(payload.get("validation_id"), str)
        and payload.get("validation_id")
        and isinstance(payload.get("worktree_path"), str)
        and payload.get("worktree_path")
        and isinstance(payload.get("baseline_sha256"), str)
        and payload.get("baseline_sha256")
        and payload.get("candidate_state") == "post_mutation"
    )


def progress_lines_for_event(
    event: Mapping[str, Any] | Any,
    *,
    ordinals: Mapping[str, int] | None = None,
    include_raw: bool = False,
) -> list[dict[str, Any]]:
    """Translate one canonical event without replacing or mutating it."""

    if not isinstance(event, Mapping):
        dump = getattr(event, "model_dump", None)
        event = dump(mode="json") if callable(dump) else vars(event)
    value = _mapping(event)
    event_type = str(value.get("event_type") or "")
    payload = _mapping(value.get("payload"))
    attempt = _attempt_number(value, ordinals)
    lines: list[tuple[str, str, str]] = []

    if event_type == "run_created":
        lines.append(
            (
                "info",
                "·",
                f"Run {value.get('run_id')} created; canonical events are being recorded",
            )
        )
    elif event_type == "classification_started":
        lines.append(("active", "●", "Classifying task difficulty and risk"))
    elif event_type == "classification_completed":
        effective = _mapping(payload.get("effective_classification"))
        difficulty = effective.get("difficulty") or "unknown difficulty"
        risk = effective.get("risk") or "unknown risk"
        lines.append(
            ("success", "✓", f"Classified as {difficulty} difficulty, {risk} risk")
        )
    elif event_type == "classification_fallback":
        lines.append(
            (
                "failure",
                "×",
                "Classifier evidence was unavailable; using the conservative fail-safe classification",
            )
        )
    elif event_type == "policy_selected":
        backend = payload.get("chosen_backend")
        if backend and payload.get("action") not in {"select", "exhaust", "fail"}:
            reason = str(payload.get("reason") or "it meets the active policy")
            lines.append(("success", "✓", f"Selected {backend} because {reason}"))
    elif event_type == "attempt_started":
        backend = payload.get("backend_name") or "selected backend"
        lines.append(("active", "●", f"Attempt {attempt} running with {backend}"))
    elif event_type == "attempt_completed":
        lines.append(
            (
                "success",
                "✓",
                f"Attempt {attempt} finished; collecting authoritative evidence",
            )
        )
    elif event_type == "attempt_failed":
        detail = (
            payload.get("failure_category")
            or payload.get("message")
            or "coding attempt failed"
        )
        lines.append(("failure", "×", f"Attempt {attempt} failed: {detail}"))
    elif _is_structured_repository_validation(
        value,
        run_id=str(value.get("run_id") or ""),
        selected_attempt_id=None,
    ):
        passed = event_type == "command_completed" and payload.get("exit_code") == 0
        lines.append(
            (
                "success" if passed else "failure",
                "✓" if passed else "×",
                f"Repository validation {'passed' if passed else 'failed'}: {_command(payload)}",
            )
        )
    elif event_type == "verification_retry_started":
        lines.append(
            (
                "escalation",
                "↗",
                f"Retrying verification for attempt {attempt} without rerunning code",
            )
        )
    elif event_type == "verification_completed":
        verifier_calls = _list(_mapping(payload.get("metadata")).get("verifier_calls"))
        if len(verifier_calls) > 1:
            lines.append(
                (
                    "escalation",
                    "↗",
                    f"Escalated verification across {len(verifier_calls)} verifier routes",
                )
            )
        if bool(payload.get("acceptance_eligible")):
            lines.append(
                (
                    "success",
                    "✓",
                    f"Attempt {attempt} has all required acceptance evidence",
                )
            )
        else:
            reason = payload.get("reason") or "required evidence is incomplete"
            lines.append(
                (
                    "failure",
                    "×",
                    f"Attempt {attempt} is not acceptance eligible: {reason}",
                )
            )
    elif event_type == "candidate_rejected":
        lines.append(
            (
                "failure",
                "×",
                f"Attempt {attempt} rejected: {payload.get('reason') or 'evidence requirements were not met'}",
            )
        )
    elif event_type == "escalation_selected":
        backend = payload.get("chosen_backend") or "the next eligible backend"
        lines.append(("escalation", "↗", f"Escalating to {backend}"))
    elif event_type == "retry_selected":
        lines.append(
            (
                "escalation",
                "↗",
                f"Retrying with {payload.get('chosen_backend') or 'the active backend'}",
            )
        )
    elif event_type == "candidate_selected":
        selected = str(payload.get("selected_attempt_id") or "unknown")
        selected_number = ordinals.get(selected, selected) if ordinals else selected
        lines.append(("selection", "◆", f"Selected attempt {selected_number}"))
    elif event_type == "approval_requested":
        files = len(_list(payload.get("files_changed")))
        lines.append(
            (
                "active",
                "●",
                f"Accepted patch is waiting for approval ({files} changed file{'s' if files != 1 else ''})",
            )
        )
    elif event_type == "approval_granted":
        lines.append(("success", "✓", "Explicit delivery approval was recorded"))
    elif event_type == "approval_rejected":
        lines.append(("failure", "×", "Delivery was rejected; the patch was preserved"))
    elif event_type == "approval_rerun_requested":
        lines.append(
            ("escalation", "↗", "A new run was requested; this patch was preserved")
        )
    elif event_type == "approval_candidate_changed":
        lines.append(
            (
                "selection",
                "◆",
                f"Approval selection changed to {payload.get('selected_attempt_id')}",
            )
        )
    elif event_type == "approval_timed_out":
        lines.append(
            (
                "failure",
                "×",
                f"Approval timed out; applied {payload.get('timeout_policy') or 'fail-closed'} policy",
            )
        )
    elif event_type == "approval_unauthorized":
        lines.append(("failure", "×", "Unauthenticated connected approval was refused"))
    elif event_type == "materialization_started":
        mode = payload.get("delivery_mode")
        lines.append(
            (
                "active",
                "●",
                f"Delivering only the selected recorded patch ({mode or 'configured mode'})",
            )
        )
    elif event_type == "delivery_completed":
        delivery_state = str(payload.get("delivery_state") or "completed").replace(
            "_", " "
        )
        lines.append(("success", "✓", f"Delivery state: {delivery_state}"))
    elif event_type == "materialization_completed":
        changed = _list(payload.get("changed_files"))
        lines.append(
            (
                "success",
                "✓",
                f"Delivered changes to {len(changed)} file{'s' if len(changed) != 1 else ''}",
            )
        )
    elif event_type == "run_completed":
        delivery_state = payload.get("delivery_state")
        detail = (
            f"; delivery is {str(delivery_state).replace('_', ' ')}"
            if delivery_state
            else ""
        )
        lines.append(("success", "✓", f"Run accepted{detail}"))
    elif event_type == "run_exhausted":
        lines.append(
            (
                "failure",
                "×",
                f"Run exhausted: {payload.get('reason') or payload.get('terminal_reason') or 'budget or evidence limit reached'}",
            )
        )
    elif event_type == "run_failed":
        lines.append(
            (
                "failure",
                "×",
                f"Run failed: {payload.get('message') or payload.get('terminal_reason') or 'see Replay'}",
            )
        )

    output: list[dict[str, Any]] = []
    for tone, symbol, message in lines:
        item: dict[str, Any] = {
            "tone": tone,
            "symbol": symbol,
            "message": redact_message(message, limit=1000),
            "event_id": value.get("event_id"),
            "sequence": value.get("sequence"),
        }
        if include_raw:
            item["raw_event_type"] = event_type
        output.append(item)
    return output


def infer_failure_code(code: str | None, reason: str | None) -> str:
    combined = f"{code or ''} {reason or ''}".lower()
    ordered = (
        (
            "delivery_authority_insufficient",
            ("delivery_authority_insufficient", "automatic delivery authority"),
        ),
        ("repository_moved", ("repository_moved", "repository moved")),
        ("target_branch_changed", ("target_branch_changed", "target branch changed")),
        ("patch_conflict", ("patch_conflict", "patch conflicts")),
        ("branch_already_exists", ("branch_already_exists", "branch already exists")),
        ("detached_head", ("detached_head", "detached head")),
        ("push_rejected", ("push_rejected", "push rejected", "non-fast-forward")),
        ("authentication_failure", ("authentication_failure", "authentication failed")),
        (
            "provider_tool_unavailable",
            ("provider_tool_unavailable", "provider tool is unavailable"),
        ),
        (
            "pull_request_creation_failed",
            ("pull_request_creation_failed", "pull-request creation failed"),
        ),
        (
            "delivery_synchronization_failure",
            (
                "delivery_synchronization_failure",
                "delivery branch state is malformed",
                "delivery worktree",
            ),
        ),
        (
            "remote_unavailable",
            ("remote_unavailable", "remote unavailable", "provider adapter"),
        ),
        ("approval_timeout", ("approval_timeout", "approval timed out")),
        ("no_backend", ("backend with role", "no backend", "backend is configured")),
        (
            "model_not_loaded",
            ("model not loaded", "no model loaded", "model is not available"),
        ),
        (
            "backend_unreachable",
            ("connection", "unreachable", "server is not running", "connecterror"),
        ),
        ("dirty_repository", ("repository is dirty", "uncommitted changes")),
        (
            "invalid_repository",
            ("not a git", "invalid repository", "repository is missing"),
        ),
        (
            "no_validation_command",
            ("no validation command", "validation command is required"),
        ),
        (
            "repository_changed_before_materialization",
            (
                "lineage",
                "repository changed",
                "baseline changed",
                "before materialization",
            ),
        ),
        ("validation_failure", ("repository_validation_failed", "validation failed")),
        (
            "verifier_unavailable",
            (
                "verifier unavailable",
                "verifier endpoint",
                "malformed verifier",
                "verification_failure",
            ),
        ),
        (
            "no_authoritative_evidence",
            ("no acceptance-eligible", "authoritative evidence", "heuristic"),
        ),
        ("synchronization_failure", ("synchronization failed", "sync failed")),
        ("service_offline", ("service offline", "service is offline", "not running")),
        ("user_cancelled", ("cancelled", "canceled")),
        ("timeout", ("wall-time", "time limit", "timed out", "timeout")),
        ("budget_exhausted", ("budget exhausted", "cost budget", "attempt budget")),
    )
    for failure_code, needles in ordered:
        if any(needle in combined for needle in needles):
            return failure_code
    return "unknown_failure"


def failure_experience(
    code: str,
    *,
    reason: str | None = None,
    attempts: int = 0,
    patch_preserved: bool = False,
) -> dict[str, Any]:
    canonical = code if code in FAILURE_CATALOG else infer_failure_code(code, reason)
    template = FAILURE_CATALOG.get(canonical, FAILURE_CATALOG["unknown_failure"])
    patch = (
        "A captured patch is preserved in the run bundle and was not applied automatically."
        if patch_preserved
        else "No preserved patch is available."
    )
    return redact_data(
        {
            "code": canonical,
            "what_failed": reason or template["failed"],
            "what_villani_tried": template["tried"],
            "attempts_recorded": attempts,
            "missing_evidence": template["missing"],
            "patch_preserved": patch_preserved,
            "patch_status": patch,
            "next_action": template["next"],
        }
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return _mapping(value)


def _events(path: Path) -> list[dict[str, Any]]:
    try:
        return [_mapping(item) for item in read_jsonl_tolerant(path)]
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def _patch_preserved(run_directory: Path, attempt_ids: Sequence[str]) -> bool:
    paths = [
        run_directory / "final.patch",
        run_directory / "delivery" / "selected.patch",
    ] + [
        run_directory / "attempts" / attempt_id / "patch.diff"
        for attempt_id in attempt_ids
    ]
    for path in paths:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _stage_cost(manifest: Mapping[str, Any], stage: str) -> tuple[float | None, str]:
    value = _mapping(_mapping(manifest.get("stage_metrics")).get(stage))
    cost = value.get("cost")
    return (
        float(cost)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool)
        else None,
        str(value.get("cost_accounting_status") or "unknown"),
    )


def build_run_presentation(
    run_directory: str | Path,
    *,
    synchronization_state: str | None = None,
    include_raw_events: bool = False,
) -> dict[str, Any]:
    """Build a complete user-facing projection from one canonical run bundle."""

    run_dir = Path(run_directory)
    manifest = _read_json(run_dir / "manifest.json")
    task = _read_json(run_dir / "task.json")
    state = _read_json(run_dir / "state.json")
    classification = _read_json(run_dir / "classification.json")
    materialization = _read_json(run_dir / "materialization.json")
    delivery_record = _read_json(run_dir / "delivery.json")
    delivery_review = _mapping(delivery_record.get("review"))
    selection = _read_json(run_dir / "selection.json")
    events = _events(run_dir / "events.jsonl")
    attempt_ids = [str(item) for item in _list(manifest.get("attempt_ids"))]
    attempts = [
        _read_json(run_dir / "attempts" / attempt_id / "attempt.json")
        for attempt_id in attempt_ids
    ]
    ordinals = {
        str(attempt.get("attempt_id") or attempt_id): int(
            attempt.get("ordinal") or index
        )
        for index, (attempt_id, attempt) in enumerate(zip(attempt_ids, attempts), 1)
    }
    verifications = {
        attempt_id: _read_json(run_dir / "verification" / f"{attempt_id}.json")
        for attempt_id in attempt_ids
    }
    selected_id = str(manifest.get("selected_attempt_id") or "") or None
    selected_verification = verifications.get(selected_id or "", {})
    canonical_run_id = str(manifest.get("run_id") or run_dir.name)
    terminal_state = str(state.get("state") or manifest.get("final_state") or "CREATED")
    outcome = (
        "ACCEPTED"
        if terminal_state == "COMPLETED"
        else terminal_state
        if terminal_state in {"EXHAUSTED", "FAILED"}
        else "AWAITING APPROVAL"
        if terminal_state == "AWAITING_APPROVAL"
        else "RUNNING"
    )
    changed_files = [str(item) for item in _list(materialization.get("changed_files"))]
    if not changed_files:
        changed_files = [
            str(item) for item in _list(delivery_review.get("files_changed"))
        ]
    validation_events = [
        event
        for event in events
        if _is_structured_repository_validation(
            event,
            run_id=canonical_run_id,
            selected_attempt_id=selected_id,
        )
    ]
    validation_rows = []
    for event in validation_events:
        payload = _mapping(event.get("payload"))
        passed = (
            event.get("event_type") == "command_completed"
            and payload.get("exit_code") == 0
        )
        validation_rows.append(
            {
                "command": _command(payload),
                "argv": _list(payload.get("argv")),
                "passed": passed,
                "exit_code": payload.get("exit_code"),
                "authority": "repository_validation",
            }
        )
    requirement_results = _list(selected_verification.get("requirement_results"))
    verified_requirements = sum(
        _mapping(item).get("outcome") in {"passed", "not_applicable"}
        for item in requirement_results
    )
    missing_evidence = _list(selected_verification.get("missing_evidence"))
    authority = str(
        _mapping(selected_verification.get("metadata")).get("authority_source")
        or _mapping(selected_verification.get("metadata")).get("verification_mode")
        or (
            "structured repository-validation evidence"
            if validation_rows and all(bool(item["passed"]) for item in validation_rows)
            else "normalized verifier decision"
            if bool(selected_verification.get("acceptance_eligible"))
            else "not established"
        )
    )
    acceptance_eligible = bool(selected_verification.get("acceptance_eligible"))
    confidence_value = selected_verification.get("confidence")
    confidence = (
        float(confidence_value)
        if isinstance(confidence_value, (int, float))
        and not isinstance(confidence_value, bool)
        else None
    )
    patch_preserved = _patch_preserved(run_dir, attempt_ids)
    synchronization_failure = (
        failure_experience(
            "synchronization_failure",
            attempts=len(attempts),
            patch_preserved=patch_preserved,
        )
        if synchronization_state == "SYNC FAILED"
        else None
    )
    terminal_reason = (
        str(
            _mapping(state.get("metadata")).get("terminal_reason")
            or _mapping(manifest.get("metadata")).get("terminal_reason")
            or _mapping(state.get("failure")).get("message")
            or ""
        )
        or None
    )
    failure_code = str(_mapping(state.get("failure")).get("code") or "")

    progress: list[dict[str, Any]] = []
    for event in events:
        progress.extend(
            progress_lines_for_event(
                event, ordinals=ordinals, include_raw=include_raw_events
            )
        )

    recovery: list[str] = []
    for attempt in attempts:
        if attempt.get("status") != "completed" or attempt.get("exit_code") not in {
            0,
            None,
        }:
            category = (
                _mapping(attempt.get("metadata")).get("failure_category") or "failed"
            )
            recovery.append(
                f"Attempt {attempt.get('ordinal') or ordinals.get(str(attempt.get('attempt_id')), '?')} {category}"
            )
    for event in events:
        if event.get("event_type") == "escalation_selected":
            payload = _mapping(event.get("payload"))
            recovery.append(
                f"Escalated to {payload.get('chosen_backend') or 'the next eligible backend'}"
            )
        if event.get("event_type") == "verification_retry_started":
            recovery.append(
                "Retried verification without rerunning or rebilling coding"
            )
        if (
            event.get("event_type") == "command_failed"
            and _mapping(event.get("payload")).get("command_role")
            == "repository_validation"
        ):
            recovery.append(
                f"Attempt {ordinals.get(str(event.get('attempt_id')), event.get('attempt_id') or '?')} failed repository validation"
            )
    verifier_calls = _list(
        _mapping(selected_verification.get("metadata")).get("verifier_calls")
    )
    if len(verifier_calls) > 1:
        recovery.append(f"Escalated across {len(verifier_calls)} verifier routes")
    if selected_id:
        recovery.append(f"Selected attempt {ordinals.get(selected_id, selected_id)}")
    if not recovery:
        recovery.append("No retry or escalation was needed")

    coding_only, coding_status = _stage_cost(manifest, "coding")
    classification_cost, classification_status = _stage_cost(manifest, "classification")
    if coding_only is not None and classification_cost is not None:
        coding_cost = coding_only + classification_cost
        coding_status = (
            "complete"
            if coding_status == classification_status == "complete"
            else "partial"
        )
    elif coding_only is not None:
        coding_cost = coding_only
    elif classification_cost is not None:
        coding_cost = classification_cost
        coding_status = classification_status
    else:
        coding_cost = None
    verification_cost, verification_status = _stage_cost(manifest, "verification")
    total_cost = manifest.get("total_cost_usd")
    total_cost = (
        float(total_cost)
        if isinstance(total_cost, (int, float)) and not isinstance(total_cost, bool)
        else None
    )

    risks = [str(item) for item in _list(selected_verification.get("risk_flags"))]
    risks.extend(
        str(_mapping(item).get("summary") or _mapping(item).get("evidence_id") or item)
        for item in missing_evidence
    )
    risks.extend(str(item) for item in _list(delivery_review.get("remaining_risks")))
    risks.extend(
        str(item) for item in _list(delivery_review.get("unrelated_change_warnings"))
    )
    risks.extend(
        str(item) for item in _list(delivery_review.get("sensitive_file_warnings"))
    )
    if synchronization_state in {"SYNC PENDING", "SYNC FAILED"}:
        risks.append(
            "Workspace synchronization is pending; the canonical local run remains authoritative."
            if synchronization_state == "SYNC PENDING"
            else FAILURE_CATALOG["synchronization_failure"]["failed"]
        )
    if not risks:
        risks.append("No remaining risk was recorded by the verifier.")
    if not changed_files and outcome in {"ACCEPTED", "AWAITING APPROVAL"}:
        risks.append("The accepted result contains no file changes.")
    risks = list(dict.fromkeys(risks))

    delivery_mode = str(delivery_record.get("mode") or "legacy")
    delivery_state = str(
        delivery_record.get("state")
        or ("applied" if materialization.get("status") == "succeeded" else "pending")
    )
    approval_record = _mapping(delivery_record.get("approval"))
    delivery_result = _mapping(delivery_record.get("result"))
    receipt = _mapping(delivery_result.get("delivery_receipt"))
    receipt_metadata = _mapping(receipt.get("metadata"))
    pull_request = _mapping(receipt_metadata.get("pull_request"))
    run_id_for_command = str(manifest.get("run_id") or run_dir.name)

    next_actions: list[dict[str, str]] = [
        {
            "label": "Open replay",
            "action": f"villani open {run_id_for_command}",
        }
    ]
    if outcome == "AWAITING APPROVAL":
        next_actions[0:0] = [
            {
                "label": "Approve and apply",
                "action": f"villani approve {run_id_for_command}",
            },
            {
                "label": "Reject delivery",
                "action": f"villani reject {run_id_for_command}",
            },
            {
                "label": "Request rerun",
                "action": f"villani request-rerun {run_id_for_command}",
            },
        ]
        if bool(approval_record.get("allow_candidate_change")):
            next_actions.insert(
                2,
                {
                    "label": "Choose candidate",
                    "action": f"villani choose-candidate {run_id_for_command} <candidate-id>",
                },
            )
    elif outcome == "ACCEPTED":
        if delivery_state == "suggested":
            next_actions.insert(
                0,
                {
                    "label": "Apply preserved patch later",
                    "action": f"git apply {run_dir / 'delivery' / 'selected.patch'}",
                },
            )
        elif delivery_state == "applied":
            next_actions.insert(
                0, {"label": "Review changes", "action": "git diff --stat"}
            )
        elif delivery_state == "branched":
            branch = receipt_metadata.get("branch") or "<delivery-branch>"
            worktree = receipt_metadata.get("delivery_worktree")
            next_actions.insert(
                0,
                {
                    "label": "Review delivery branch",
                    "action": (
                        f"git -C {worktree} status"
                        if worktree
                        else f"git show {branch}"
                    ),
                },
            )
        elif delivery_state == "pull_request_created":
            next_actions.insert(
                0,
                {
                    "label": "Open pull request",
                    "action": str(pull_request.get("url") or "See delivery receipt"),
                },
            )
        elif delivery_state == "rerun_requested":
            next_actions.insert(
                0,
                {
                    "label": "Create requested rerun",
                    "action": f"villani rerun {run_id_for_command}",
                },
            )
        elif delivery_state in {"rejected", "timed_out"}:
            next_actions.insert(
                0,
                {
                    "label": "Review preserved patch",
                    "action": str(run_dir / "delivery" / "selected.patch"),
                },
            )
    elif outcome in {"FAILED", "EXHAUSTED"}:
        explanation = failure_experience(
            infer_failure_code(failure_code, terminal_reason),
            reason=terminal_reason,
            attempts=len(attempts),
            patch_preserved=patch_preserved,
        )
        next_actions.extend(
            [
                {"label": "Recover", "action": str(explanation["next_action"])},
            ]
        )
    else:
        next_actions.insert(
            0,
            {"label": "Resume", "action": f"villani resume {run_id_for_command}"},
        )
    if synchronization_failure is not None:
        next_actions.append(
            {
                "label": "Retry synchronization",
                "action": str(synchronization_failure["next_action"]),
            }
        )

    lineage = _mapping(_mapping(manifest.get("metadata")).get("lineage"))
    if not lineage:
        lineage = _mapping(_mapping(task.get("metadata")).get("lineage"))
    if outcome == "AWAITING APPROVAL":
        summary = (
            "An acceptance-eligible patch is waiting for explicit delivery approval."
        )
    elif outcome == "ACCEPTED" and delivery_record and delivery_state == "suggested":
        summary = "The accepted patch was preserved as a suggestion; the target repository was not modified."
    elif outcome == "ACCEPTED" and delivery_record and delivery_state == "applied":
        summary = "The accepted patch was applied to the target working tree."
    elif outcome == "ACCEPTED" and delivery_record and delivery_state == "branched":
        summary = "The accepted patch was delivered on a separate local branch without switching the original branch."
    elif (
        outcome == "ACCEPTED"
        and delivery_record
        and delivery_state == "pull_request_created"
    ):
        summary = "The accepted patch was committed, pushed, and submitted for review."
    elif outcome == "ACCEPTED" and delivery_record and delivery_state == "rejected":
        summary = "The patch passed acceptance checks, but delivery was explicitly rejected and nothing was applied."
    elif (
        outcome == "ACCEPTED"
        and delivery_record
        and delivery_state == "rerun_requested"
    ):
        summary = "The patch passed acceptance checks, but a new run was requested and nothing was applied."
    elif outcome == "ACCEPTED" and delivery_record and delivery_state == "timed_out":
        summary = "Approval timed out; the fail-closed policy preserved the patch without applying it."
    else:
        summary = str(
            (
                terminal_reason
                if outcome in {"FAILED", "EXHAUSTED"}
                else selected_verification.get("reason")
                or materialization.get("final_report")
            )
            or task.get("instruction")
            or "Villani run"
        )
    eligible_candidate_ids = [
        attempt_id
        for attempt_id, verification in verifications.items()
        if bool(verification.get("acceptance_eligible"))
    ]
    presentation: dict[str, Any] = {
        "schema_version": PRESENTATION_SCHEMA,
        "run_id": canonical_run_id,
        "outcome": outcome,
        "controller_state": terminal_state if include_raw_events else None,
        "summary": summary,
        "changed": {
            "files": changed_files,
            "file_count": len(changed_files),
            "zero_file_change": len(changed_files) == 0,
            "delivery_status": delivery_state,
        },
        "confidence": {
            "value": confidence,
            "label": "acceptance-grade"
            if acceptance_eligible
            else "not acceptance eligible",
            "acceptance_eligible": acceptance_eligible,
            "authority": authority,
        },
        "validation": {
            "commands": validation_rows,
            "checks_passed": sum(bool(item["passed"]) for item in validation_rows),
            "checks_failed": sum(not bool(item["passed"]) for item in validation_rows),
            "requirements_verified": verified_requirements,
            "missing_evidence_count": len(missing_evidence),
            "authority": "executed_repository_validation"
            if validation_rows
            else "none",
        },
        "remaining_risks": risks,
        "cost": {
            "currency": str(manifest.get("currency") or "USD"),
            "coding": coding_cost,
            "coding_status": coding_status,
            "verification": verification_cost,
            "verification_status": verification_status,
            "total": total_cost,
            "accounting_status": str(
                manifest.get("cost_accounting_status") or "unknown"
            ),
        },
        "recovery": recovery,
        "next_actions": next_actions,
        "delivery": {
            "mode": delivery_mode,
            "state": delivery_state,
            "label": delivery_state.replace("_", " ").title(),
            "repository_modified": bool(delivery_record.get("repository_modified")),
            "target_worktree_modified": bool(
                delivery_record.get("target_worktree_modified")
            ),
            "patch_artifact": delivery_record.get("patch_artifact"),
            "patch_sha256": delivery_record.get("patch_sha256"),
            "authority": _mapping(delivery_record.get("authority")),
            "approval": approval_record,
            "review": delivery_review,
            "result": delivery_result,
            "failure": _mapping(delivery_record.get("failure")) or None,
            "eligible_candidate_ids": eligible_candidate_ids,
        },
        "patch": {
            "preserved": patch_preserved,
            "applied": outcome == "ACCEPTED" and delivery_state == "applied",
            "branch_created": delivery_state in {"branched", "pull_request_created"},
            "pull_request_created": delivery_state == "pull_request_created",
        },
        "failure": (
            failure_experience(
                infer_failure_code(failure_code, terminal_reason),
                reason=terminal_reason,
                attempts=len(attempts),
                patch_preserved=patch_preserved,
            )
            if outcome in {"FAILED", "EXHAUSTED"}
            else None
        ),
        "synchronization_state": synchronization_state or "LOCAL",
        "synchronization_failure": synchronization_failure,
        "lineage": lineage,
        "progress": progress,
        "attempts": [
            {
                "attempt_id": attempt.get("attempt_id"),
                "ordinal": attempt.get("ordinal"),
                "backend": attempt.get("backend_name"),
                "model": attempt.get("model"),
                "status": attempt.get("status"),
                "verification": verifications.get(
                    str(attempt.get("attempt_id")), {}
                ).get("outcome"),
                "acceptance_eligible": verifications.get(
                    str(attempt.get("attempt_id")), {}
                ).get("acceptance_eligible"),
            }
            for attempt in attempts
        ],
        "selected_attempt_id": selected_id,
        "classification": {
            "difficulty": classification.get("difficulty"),
            "risk": classification.get("risk"),
            "category": classification.get("category"),
            "confidence": classification.get("confidence"),
        },
        "selection": {
            "strategy": selection.get("strategy"),
            "reason": selection.get("reason"),
        },
    }
    if not include_raw_events:
        presentation.pop("controller_state", None)
    return redact_data(presentation)
