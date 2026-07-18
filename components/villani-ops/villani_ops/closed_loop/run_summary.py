"""One canonical, backwards-readable projection of run evidence counts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import Field, model_validator

from .durable_io import read_jsonl_tolerant, write_json_atomic
from .protocol import StrictProtocolModel


CountStatus = Literal["complete", "unknown"]


class EvidenceCounts(StrictProtocolModel):
    passed: int | None = Field(default=None, ge=0)
    failed: int | None = Field(default=None, ge=0)
    not_run: int | None = Field(default=None, ge=0)
    unavailable: int | None = Field(default=None, ge=0)
    accounting_status: CountStatus

    @model_validator(mode="after")
    def validate_known_counts(self) -> "EvidenceCounts":
        values = (self.passed, self.failed, self.not_run, self.unavailable)
        if self.accounting_status == "complete" and any(item is None for item in values):
            raise ValueError("complete evidence counts require every count")
        if self.accounting_status == "unknown" and any(item is not None for item in values):
            raise ValueError("unknown evidence counts must remain null")
        return self


class RequirementCounts(StrictProtocolModel):
    proved: int | None = Field(default=None, ge=0)
    not_proved: int | None = Field(default=None, ge=0)
    accounting_status: CountStatus

    @model_validator(mode="after")
    def validate_known_counts(self) -> "RequirementCounts":
        if self.accounting_status == "complete" and (
            self.proved is None or self.not_proved is None
        ):
            raise ValueError("complete requirement counts require both values")
        if self.accounting_status == "unknown" and (
            self.proved is not None or self.not_proved is not None
        ):
            raise ValueError("unknown requirement counts must remain null")
        return self


class AccountingProjection(StrictProtocolModel):
    known: bool
    accounting_status: str = Field(min_length=1)
    total_cost: float | None = Field(default=None, ge=0)
    currency: str | None = None

    @model_validator(mode="after")
    def validate_unknown_cost(self) -> "AccountingProjection":
        if self.known and self.total_cost is None:
            raise ValueError("known accounting requires a cost value")
        if not self.known and self.total_cost is not None:
            raise ValueError("unknown accounting cannot carry a numeric cost")
        return self


class AcceptanceProjection(StrictProtocolModel):
    decision: bool
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RunSummary(StrictProtocolModel):
    schema_version: Literal["villani.run_summary.v1"]
    run_id: str = Field(min_length=1)
    attempt_id: str | None = None
    checks: EvidenceCounts
    focused_probes: EvidenceCounts
    requirements: RequirementCounts
    accounting: AccountingProjection
    acceptance: AcceptanceProjection
    source_artifacts: list[str]
    generated_at: str
    migration: dict[str, Any] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _attempt_id(run_directory: Path, manifest: Mapping[str, Any]) -> str | None:
    selected = manifest.get("selected_attempt_id")
    if isinstance(selected, str) and selected:
        return selected
    attempt_ids = manifest.get("attempt_ids")
    if isinstance(attempt_ids, list):
        values = [str(item) for item in attempt_ids if str(item)]
        if values:
            return values[-1]
    attempts = run_directory / "attempts"
    if attempts.is_dir():
        values = sorted(path.name for path in attempts.iterdir() if path.is_dir())
        return values[-1] if values else None
    return None


def _structured_legacy_checks(
    run_directory: Path, attempt_id: str | None
) -> EvidenceCounts | None:
    events_path = run_directory / "events.jsonl"
    if not events_path.is_file():
        return None
    terminal: dict[str, dict[str, Any]] = {}
    accepted_types = {
        "repository_validation_completed",
        "repository_validation_failed",
        "repository_validation_infrastructure_error",
        "command_completed",
        "command_failed",
    }
    malformed_index = 0
    for event in read_jsonl_tolerant(events_path):
        if event.get("event_type") not in accepted_types:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("command_role") != "repository_validation":
            continue
        if attempt_id and event.get("attempt_id") != attempt_id:
            continue
        required = (
            payload.get("validation_id"),
            payload.get("worktree_path"),
            payload.get("baseline_sha256"),
            payload.get("candidate_state"),
        )
        if any(not item for item in required):
            malformed_index += 1
            terminal[f"unavailable:{malformed_index}"] = {
                "event_type": "repository_validation_infrastructure_error",
                "exit_code": None,
            }
            continue
        terminal[str(payload["validation_id"])] = {
            "event_type": event.get("event_type"),
            "exit_code": payload.get("exit_code"),
        }
    if not terminal:
        return None
    passed = sum(
        item["event_type"] in {"repository_validation_completed", "command_completed"}
        and item["exit_code"] == 0
        for item in terminal.values()
    )
    failed = sum(
        item["event_type"] in {"repository_validation_failed", "command_failed"}
        and item["exit_code"] not in {None, 0}
        for item in terminal.values()
    )
    unavailable = len(terminal) - passed - failed
    return EvidenceCounts(
        passed=passed,
        failed=failed,
        not_run=0,
        unavailable=unavailable,
        accounting_status="complete",
    )


def _check_counts(
    run_directory: Path, attempt_id: str | None
) -> tuple[EvidenceCounts, list[str], dict[str, Any] | None]:
    if attempt_id:
        path = run_directory / "attempts" / attempt_id / "repository-validation.json"
        report = _read(path)
        if report is not None:
            commands = report.get("commands")
            rows = commands if isinstance(commands, list) else []
            passed = sum(
                isinstance(item, Mapping) and item.get("status") == "passed"
                for item in rows
            )
            failed = sum(
                isinstance(item, Mapping) and item.get("status") == "failed"
                for item in rows
            )
            unavailable = sum(
                isinstance(item, Mapping)
                and item.get("status")
                in {"timed_out", "infrastructure_error", "policy_denied", "unavailable"}
                for item in rows
            )
            if not rows and report.get("status") == "unavailable":
                unavailable = 1
            not_run = sum(
                isinstance(item, Mapping) and item.get("status") == "not_run"
                for item in rows
            )
            return (
                EvidenceCounts(
                    passed=passed,
                    failed=failed,
                    not_run=not_run,
                    unavailable=unavailable,
                    accounting_status="complete",
                ),
                [path.relative_to(run_directory).as_posix()],
                report,
            )
    legacy = _structured_legacy_checks(run_directory, attempt_id)
    if legacy is not None:
        return legacy, ["events.jsonl"], None
    return (
        EvidenceCounts(accounting_status="unknown"),
        [],
        None,
    )


def _probe_counts(
    run_directory: Path, attempt_id: str | None, verification_exists: bool
) -> tuple[EvidenceCounts, list[str]]:
    if not attempt_id:
        return EvidenceCounts(accounting_status="unknown"), []
    report_path = run_directory / "verification" / f"{attempt_id}-focused-probes.json"
    request_path = (
        run_directory / "verification" / f"{attempt_id}-focused-probe-requests.json"
    )
    report = _read(report_path)
    if report is not None:
        results = report.get("results")
        rows = results if isinstance(results, list) else []
        return (
            EvidenceCounts(
                passed=sum(
                    isinstance(item, Mapping) and item.get("status") == "passed"
                    for item in rows
                ),
                failed=sum(
                    isinstance(item, Mapping) and item.get("status") == "failed"
                    for item in rows
                ),
                not_run=0,
                unavailable=sum(
                    isinstance(item, Mapping)
                    and item.get("status") == "infrastructure_error"
                    for item in rows
                ),
                accounting_status="complete",
            ),
            [report_path.relative_to(run_directory).as_posix()],
        )
    requests = _read(request_path)
    if requests is not None:
        request_rows = requests.get("requests")
        count = len(request_rows) if isinstance(request_rows, list) else 0
        return (
            EvidenceCounts(
                passed=0,
                failed=0,
                not_run=count,
                unavailable=0,
                accounting_status="complete",
            ),
            [request_path.relative_to(run_directory).as_posix()],
        )
    if verification_exists:
        return (
            EvidenceCounts(
                passed=0,
                failed=0,
                not_run=0,
                unavailable=0,
                accounting_status="complete",
            ),
            [],
        )
    return EvidenceCounts(accounting_status="unknown"), []


def _requirement_counts(
    run_directory: Path, attempt_id: str | None
) -> tuple[RequirementCounts, list[str], dict[str, Any] | None]:
    if not attempt_id:
        return RequirementCounts(accounting_status="unknown"), [], None
    path = run_directory / "verification" / f"{attempt_id}-evidence.json"
    evidence = _read(path)
    if evidence is None:
        return RequirementCounts(accounting_status="unknown"), [], None
    values = evidence.get("requirements")
    rows = values if isinstance(values, list) else []
    proved = sum(
        isinstance(item, Mapping)
        and item.get("final_status") in {"passed", "not_applicable"}
        for item in rows
    )
    return (
        RequirementCounts(
            proved=proved,
            not_proved=len(rows) - proved,
            accounting_status="complete",
        ),
        [path.relative_to(run_directory).as_posix()],
        evidence,
    )


def build_run_summary(run_directory: Path) -> RunSummary:
    run_directory = Path(run_directory).resolve()
    manifest = _read(run_directory / "manifest.json") or {}
    state = _read(run_directory / "state.json") or {}
    run_id = str(manifest.get("run_id") or state.get("run_id") or run_directory.name)
    attempt_id = _attempt_id(run_directory, manifest)
    verification_path = (
        run_directory / "verification" / f"{attempt_id}.json" if attempt_id else None
    )
    verification = _read(verification_path) if verification_path else None
    checks, check_sources, _ = _check_counts(run_directory, attempt_id)
    probes, probe_sources = _probe_counts(
        run_directory, attempt_id, verification is not None
    )
    requirements, requirement_sources, evidence = _requirement_counts(
        run_directory, attempt_id
    )
    cost_status = str(manifest.get("cost_accounting_status") or "unknown")
    cost_value = manifest.get("total_cost_usd")
    known_cost = cost_status == "complete" and isinstance(cost_value, (int, float))
    normalized_cost = (
        float(cost_value) if isinstance(cost_value, (int, float)) else None
    )
    accounting = AccountingProjection(
        known=known_cost,
        accounting_status=cost_status,
        total_cost=normalized_cost if known_cost else None,
        currency=str(manifest.get("currency") or "USD") if known_cost else None,
    )
    acceptance_eligible = bool(
        verification is not None and verification.get("acceptance_eligible") is True
    )
    evidence_result = evidence.get("final_result") if evidence else None
    decision = acceptance_eligible and evidence_result == 1
    reason_code = str(
        (evidence or {}).get("final_reason_code")
        or (verification or {}).get("metadata", {}).get("computed_final_reason_code")
        or ("accepted" if decision else "no_acceptance_eligible_candidate")
    )
    reason = str(
        (evidence or {}).get("final_reason")
        or (verification or {}).get("reason")
        or state.get("metadata", {}).get("terminal_reason")
        or ("Accepted with complete deterministic evidence." if decision else "No acceptance-eligible candidate was selected.")
    )
    sources = sorted(
        set(
            [
                "manifest.json",
                "state.json",
                *check_sources,
                *probe_sources,
                *requirement_sources,
                *(
                    [verification_path.relative_to(run_directory).as_posix()]
                    if verification_path and verification_path.is_file()
                    else []
                ),
            ]
        )
    )
    return RunSummary(
        schema_version="villani.run_summary.v1",
        run_id=run_id,
        attempt_id=attempt_id,
        checks=checks,
        focused_probes=probes,
        requirements=requirements,
        accounting=accounting,
        acceptance=AcceptanceProjection(
            decision=decision,
            reason_code=reason_code,
            reason=reason,
        ),
        source_artifacts=sources,
        generated_at=_utc_now(),
        migration=(
            {"mode": "legacy_event_projection", "source": "events.jsonl"}
            if "events.jsonl" in check_sources
            else None
        ),
    )


def load_run_summary(run_directory: Path) -> RunSummary | None:
    run_directory = Path(run_directory).resolve()
    path = run_directory / "run-summary.json"
    if not path.is_file():
        return None
    try:
        summary = RunSummary.model_validate_json(path.read_text(encoding="utf-8"))
        summary_modified = path.stat().st_mtime_ns
        for relative in summary.source_artifacts:
            source = (run_directory / relative).resolve()
            source.relative_to(run_directory)
            if source.is_file() and source.stat().st_mtime_ns > summary_modified:
                return None
        return summary
    except (OSError, ValueError):
        return None


def canonical_run_summary(run_directory: Path) -> RunSummary:
    return load_run_summary(run_directory) or build_run_summary(run_directory)


def _count_text(value: int | None) -> str:
    return "Unknown" if value is None else str(value)


def summary_markdown(summary: RunSummary) -> str:
    checks = summary.checks
    probes = summary.focused_probes
    requirements = summary.requirements
    accounting = (
        f"{summary.accounting.total_cost:.6f} {summary.accounting.currency}"
        if summary.accounting.known and summary.accounting.total_cost is not None
        else f"Unknown ({summary.accounting.accounting_status})"
    )
    return (
        "<!-- villani-run-summary:start -->\n"
        "## Canonical evidence summary\n\n"
        f"- Repository checks: passed {_count_text(checks.passed)}, failed {_count_text(checks.failed)}, "
        f"not run {_count_text(checks.not_run)}, unavailable {_count_text(checks.unavailable)}.\n"
        f"- Focused probes: passed {_count_text(probes.passed)}, failed {_count_text(probes.failed)}, "
        f"not run {_count_text(probes.not_run)}, unavailable {_count_text(probes.unavailable)}.\n"
        f"- Requirements: proved {_count_text(requirements.proved)}, not proved {_count_text(requirements.not_proved)}.\n"
        f"- Accounting: {accounting}.\n"
        f"- Final acceptance: {'accepted' if summary.acceptance.decision else 'not accepted'} "
        f"(`{summary.acceptance.reason_code}`) — {summary.acceptance.reason}\n"
        "<!-- villani-run-summary:end -->\n"
    )


def _replace_summary_block(contents: str, block: str) -> str:
    start = "<!-- villani-run-summary:start -->"
    end = "<!-- villani-run-summary:end -->"
    if start in contents and end in contents:
        prefix = contents.split(start, 1)[0].rstrip()
        suffix = contents.split(end, 1)[1].lstrip()
        return prefix + "\n\n" + block + ("\n" + suffix if suffix else "")
    return contents.rstrip() + "\n\n" + block


def persist_run_summary(run_directory: Path) -> RunSummary:
    run_directory = Path(run_directory).resolve()
    summary = load_run_summary(run_directory)
    if summary is None:
        summary = build_run_summary(run_directory)
        write_json_atomic(run_directory / "run-summary.json", summary)
    block = summary_markdown(summary)
    for name, heading in (
        ("selection_report.md", "# Selection report"),
        ("final_report.md", "# Final report"),
    ):
        path = run_directory / name
        contents = path.read_text(encoding="utf-8") if path.is_file() else heading + "\n"
        path.write_text(_replace_summary_block(contents, block), encoding="utf-8")
    return summary
