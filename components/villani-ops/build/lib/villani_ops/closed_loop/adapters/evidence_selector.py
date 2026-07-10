"""Deterministic evidence-ranked selector for normalized eligible candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from villani_ops.orchestrator.selection import (
    POLICY,
    build_candidate_evidence_matrix,
    finalize_evidence_reasons,
    rank_candidates_by_evidence,
    write_candidate_evidence_matrix,
    write_selection_report,
)

from ..durable_io import write_json_atomic
from ..event_writer import redact_data
from ..interfaces import (
    EligibleCandidate,
    Selection,
    SelectionContext,
    SelectionRanking,
)
from ..protocol import CandidateRanking, SelectionSnapshot


@dataclass(slots=True)
class _EvidenceCandidate:
    candidate_id: str
    verifier_result: dict[str, Any]
    patch_path: Path
    changed_files: list[str]
    debug_dir: Path | None
    stdout_path: Path | None
    stderr_path: Path | None


def _legacy_verifier(candidate: EligibleCandidate) -> dict[str, Any]:
    verification = candidate.verification
    return {
        "result": 1,
        "verdict": "success",
        "confidence": verification.confidence,
        "recommendedAction": "accept",
        "requirementResults": [
            {
                "id": item.requirement_id,
                "requirement": item.description,
                "status": (
                    "satisfied"
                    if item.outcome in {"passed", "not_applicable"}
                    else "unsatisfied"
                ),
                "evidence": list(item.evidence_ids),
                "risks": [],
            }
            for item in verification.requirement_results
        ],
        "successEvidence": [item.summary for item in verification.success_evidence],
        "failureEvidence": [item.summary for item in verification.failure_evidence],
        "missingEvidence": [item.summary for item in verification.missing_evidence],
        "riskFlags": list(verification.risk_flags),
        "criticalRequirementCovered": True,
        "criticalRequirementCoverageProven": verification.metadata.get(
            "critical_requirement_coverage_proven", True
        ),
    }


class EvidenceSelectorAdapter:
    def __init__(
        self,
        *,
        advisory_comparator: Callable[[tuple[EligibleCandidate, ...]], Any]
        | None = None,
    ) -> None:
        self._advisory_comparator = advisory_comparator

    def select(
        self,
        eligible_candidates: tuple[EligibleCandidate, ...],
        context: SelectionContext,
    ) -> Selection:
        eligible = tuple(
            candidate
            for candidate in eligible_candidates
            if candidate.verification.acceptance_eligible
        )
        if not eligible:
            raise ValueError("selector received no normalized eligible candidates")
        run_dir = Path(context.run_directory).resolve()
        views: list[_EvidenceCandidate] = []
        candidate_by_id: dict[str, EligibleCandidate] = {}
        for candidate in eligible:
            attempt_id = candidate.attempt.attempt_id
            candidate_by_id[attempt_id] = candidate
            base = run_dir / "attempts" / attempt_id
            patch = (run_dir / str(candidate.attempt.patch_path)).resolve()
            changed = candidate.attempt.metadata.get("changed_files") or []
            trace_value = candidate.attempt.metadata.get("debug_trace_path")
            views.append(
                _EvidenceCandidate(
                    candidate_id=attempt_id,
                    verifier_result=_legacy_verifier(candidate),
                    patch_path=patch,
                    changed_files=[str(item) for item in changed],
                    debug_dir=(run_dir / str(trace_value)).resolve()
                    if trace_value
                    else None,
                    stdout_path=base / "stdout.log",
                    stderr_path=base / "stderr.log",
                )
            )

        ranked = rank_candidates_by_evidence(views)
        if not ranked:
            raise ValueError("evidence ranking produced no candidate")
        winner_id = str(ranked[0]["candidate_id"])
        matrix = finalize_evidence_reasons(
            build_candidate_evidence_matrix(views, winner_id), winner_id
        )
        advisory: dict[str, Any] | None = None
        if self._advisory_comparator is not None:
            try:
                raw = self._advisory_comparator(eligible)
                advisory = redact_data(
                    dict(raw) if isinstance(raw, dict) else {"result": raw}
                )
                advisory["used_for_final_decision"] = False
                advisory["usedForFinalDecision"] = False
                advisory["evidence_ranked_winner_id"] = winner_id
            except Exception as error:
                advisory = {
                    "error": str(error),
                    "used_for_final_decision": False,
                    "evidence_ranked_winner_id": winner_id,
                }
        if advisory:
            recommended = advisory.get("selected_candidate_id") or advisory.get(
                "selectedCandidateId"
            )
            for row in matrix:
                row["llm_comparison_recommended"] = row["candidate_id"] == recommended
                row["llm_comparison_reason"] = (
                    advisory.get("reason")
                    if row["candidate_id"] == recommended
                    else None
                )
                row["llm_comparison_used_for_final_decision"] = False

        matrix_path = run_dir / "candidate_evidence_matrix.json"
        report_path = run_dir / "selection_report.md"
        write_candidate_evidence_matrix(matrix_path, matrix)
        write_selection_report(report_path, matrix, winner_id)
        report = report_path.read_text(encoding="utf-8")
        rankings: list[SelectionRanking] = []
        protocol_rankings: list[CandidateRanking] = []
        for rank, row in enumerate(ranked, 1):
            candidate = candidate_by_id[str(row["candidate_id"])]
            reason = next(
                item["final_selection_reason"]
                for item in matrix
                if item["candidate_id"] == row["candidate_id"]
            )
            ranking = SelectionRanking(
                attempt_id=str(row["candidate_id"]),
                rank=rank,
                reason=reason,
                actual_cost_usd=candidate.attempt.cost_usd,
                cost_accounting_status=candidate.attempt.cost_accounting_status,
                evidence={"evidence_score": row["evidence_score"]},
            )
            rankings.append(ranking)
            protocol_rankings.append(
                CandidateRanking(
                    attempt_id=ranking.attempt_id,
                    rank=ranking.rank,
                    reason=ranking.reason,
                    actual_cost_usd=ranking.actual_cost_usd,
                    cost_accounting_status=ranking.cost_accounting_status,
                    evidence=dict(ranking.evidence),
                )
            )
        winner_row = next(row for row in matrix if row["candidate_id"] == winner_id)
        snapshot = SelectionSnapshot(
            schema_version="villani.selection.v1",
            selection_id="selection_001",
            run_id=context.run_id,
            selected_at=datetime.now(timezone.utc),
            strategy=POLICY,
            eligible_candidate_ids=sorted(candidate_by_id),
            selected_candidate_ids=[winner_id],
            rankings=protocol_rankings,
            reason=winner_row["final_selection_reason"],
            advisory_comparison=advisory,
            metadata={"deterministic_evidence_controls_selection": True},
        )
        write_json_atomic(
            run_dir / "selection.json", snapshot.model_dump(mode="json")
        )
        return Selection(
            selected_attempt_id=winner_id,
            strategy=POLICY,
            reason=winner_row["final_selection_reason"],
            rankings=tuple(rankings),
            advisory_comparison=advisory,
            report=report,
            metadata={"deterministic_evidence_controls_selection": True},
        )
