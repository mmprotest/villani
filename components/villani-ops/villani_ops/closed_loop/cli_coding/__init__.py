"""Provider-neutral helpers shared by CLI coding attempt adapters."""

from .evidence import (
    CollectedCandidateEvidence,
    PreparedCandidate,
    collect_candidate_evidence,
    prepare_candidate,
    relative_to_run,
    sanitize_and_parse_final,
    write_normalized_events,
)

__all__ = [
    "CollectedCandidateEvidence",
    "PreparedCandidate",
    "collect_candidate_evidence",
    "prepare_candidate",
    "relative_to_run",
    "sanitize_and_parse_final",
    "write_normalized_events",
]
