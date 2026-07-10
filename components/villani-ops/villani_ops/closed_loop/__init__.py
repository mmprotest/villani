"""Canonical closed-loop protocol and deterministic controller."""

from .adapters import (
    EvidenceSelectorAdapter,
    PatchMaterializerAdapter,
    VillaniCodeAttemptAdapter,
    VillaniVerifierAdapter,
)
from .controller import ClosedLoopController
from .durable_io import append_jsonl_durable, read_jsonl_tolerant, write_json_atomic
from .interfaces import ClosedLoopRunRequest, ClosedLoopRunResult
from .protocol import (
    AttemptSnapshot,
    ClassificationSnapshot,
    EventEnvelope,
    MaterializationSnapshot,
    PolicyDecisionSnapshot,
    RunManifestSnapshot,
    RunStateSnapshot,
    SelectionSnapshot,
    TaskSnapshot,
    VerificationSnapshot,
)
from .schema_validation import (
    SCHEMA_VERSION_TO_PATH,
    ProtocolValidationError,
    ProtocolValidationIssue,
    collect_protocol_validation_issues,
    parse_protocol_document,
    validate_event_stream,
    validate_jsonl_event_stream,
    validate_protocol_document,
)

__all__ = [
    "AttemptSnapshot",
    "ClassificationSnapshot",
    "ClosedLoopController",
    "ClosedLoopRunRequest",
    "ClosedLoopRunResult",
    "EventEnvelope",
    "EvidenceSelectorAdapter",
    "MaterializationSnapshot",
    "PatchMaterializerAdapter",
    "PolicyDecisionSnapshot",
    "ProtocolValidationError",
    "ProtocolValidationIssue",
    "RunManifestSnapshot",
    "RunStateSnapshot",
    "SCHEMA_VERSION_TO_PATH",
    "SelectionSnapshot",
    "TaskSnapshot",
    "VerificationSnapshot",
    "VillaniCodeAttemptAdapter",
    "VillaniVerifierAdapter",
    "append_jsonl_durable",
    "collect_protocol_validation_issues",
    "parse_protocol_document",
    "read_jsonl_tolerant",
    "validate_event_stream",
    "validate_jsonl_event_stream",
    "validate_protocol_document",
    "write_json_atomic",
]
