"""Deterministic evidence-ranked selector for normalized eligible candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from villani_ops.orchestrator.selection import (
    POLICY,
    build_candidate_evidence_matrix,
    evidence_rank_signature,
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
from ..plugins.builtins import SELECTOR_MANIFEST


@dataclass(slots=True)
class _EvidenceCandidate:
    candidate_id: str
    verifier_result: dict[str, Any]
    patch_path: Path
    changed_files: list[str]
    debug_dir: Path | None
    stdout_path: Path | None
    stderr_path: Path | None


@dataclass(slots=True)
class EvidenceSelectionPlan:
    eligible: tuple[EligibleCandidate, ...]
    run_directory: Path
    views: list[_EvidenceCandidate]
    candidate_by_id: dict[str, EligibleCandidate]
    ranked: list[dict[str, Any]]

    @property
    def deterministic_winner_is_unambiguous(self) -> bool:
        return len(self.ranked) == 1 or evidence_rank_signature(
            self.ranked[0]
        ) != evidence_rank_signature(self.ranked[1])

    @property
    def tied_winner_ids(self) -> tuple[str, ...]:
        if not self.ranked:
            return ()
        signature = evidence_rank_signature(self.ranked[0])
        return tuple(
            str(row["candidate_id"])
            for row in self.ranked
            if evidence_rank_signature(row) == signature
        )


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


def prepare_evidence_selection(
    eligible_candidates: tuple[EligibleCandidate, ...], context: SelectionContext
) -> EvidenceSelectionPlan:
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
    return EvidenceSelectionPlan(
        eligible=eligible,
        run_directory=run_dir,
        views=views,
        candidate_by_id=candidate_by_id,
        ranked=ranked,
    )


def finalize_evidence_selection(
    plan: EvidenceSelectionPlan,
    context: SelectionContext,
    *,
    selected_attempt_id: str | None = None,
    strategy: str = POLICY,
    selection_reason: str | None = None,
    advisory: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Selection:
    winner_id = selected_attempt_id or str(plan.ranked[0]["candidate_id"])
    if winner_id not in plan.candidate_by_id:
        raise ValueError("selection override was not acceptance eligible")
    ranked = sorted(
        plan.ranked,
        key=lambda row: 0 if str(row["candidate_id"]) == winner_id else 1,
    )
    matrix = finalize_evidence_reasons(
        build_candidate_evidence_matrix(plan.views, winner_id), winner_id
    )
    winner_row = next(row for row in matrix if row["candidate_id"] == winner_id)
    if selection_reason:
        winner_row["final_selection_reason"] = selection_reason
    if advisory:
        recommended = advisory.get("selected_candidate_id") or advisory.get(
            "selectedCandidateId"
        )
        for row in matrix:
            row["llm_comparison_recommended"] = row["candidate_id"] == recommended
            row["llm_comparison_reason"] = (
                advisory.get("reason") if row["candidate_id"] == recommended else None
            )
            row["llm_comparison_used_for_final_decision"] = False

    matrix_path = plan.run_directory / "candidate_evidence_matrix.json"
    report_path = plan.run_directory / "selection_report.md"
    write_candidate_evidence_matrix(matrix_path, matrix)
    write_selection_report(report_path, matrix, winner_id)
    report = report_path.read_text(encoding="utf-8")
    rankings: list[SelectionRanking] = []
    protocol_rankings: list[CandidateRanking] = []
    for rank, row in enumerate(ranked, 1):
        candidate = plan.candidate_by_id[str(row["candidate_id"])]
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
    selection_metadata = dict(
        metadata or {"deterministic_evidence_controls_selection": True}
    )
    snapshot = SelectionSnapshot(
        schema_version="villani.selection.v1",
        selection_id="selection_001",
        run_id=context.run_id,
        selected_at=datetime.now(timezone.utc),
        strategy=strategy,
        eligible_candidate_ids=sorted(plan.candidate_by_id),
        selected_candidate_ids=[winner_id],
        rankings=protocol_rankings,
        reason=winner_row["final_selection_reason"],
        advisory_comparison=advisory,
        metadata=selection_metadata,
    )
    write_json_atomic(
        plan.run_directory / "selection.json", snapshot.model_dump(mode="json")
    )
    return Selection(
        selected_attempt_id=winner_id,
        strategy=strategy,
        reason=winner_row["final_selection_reason"],
        rankings=tuple(rankings),
        advisory_comparison=advisory,
        report=report,
        metadata=selection_metadata,
    )


class EvidenceSelectorAdapter:
    plugin_manifest = SELECTOR_MANIFEST

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
        plan = prepare_evidence_selection(eligible_candidates, context)
        winner_id = str(plan.ranked[0]["candidate_id"])
        advisory: dict[str, Any] | None = None
        if self._advisory_comparator is not None:
            try:
                raw = self._advisory_comparator(plan.eligible)
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
        return finalize_evidence_selection(plan, context, advisory=advisory)


__all__ = [
    "EvidenceSelectionPlan",
    "EvidenceSelectorAdapter",
    "finalize_evidence_selection",
    "prepare_evidence_selection",
]
