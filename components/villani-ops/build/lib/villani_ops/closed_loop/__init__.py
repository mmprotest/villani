"""Canonical closed-loop protocol and deterministic controller."""

from .adapters import (
    EvidenceSelectorAdapter,
    PatchMaterializerAdapter,
    VillaniCodeAttemptAdapter,
    VillaniVerifierAdapter,
)
from .controller import ClosedLoopController
from .costs import CostBreakdown, actual_attempt_cost, estimate_attempt_cost
from .durable_io import append_jsonl_durable, read_jsonl_tolerant, write_json_atomic
from .interfaces import ClosedLoopRunRequest, ClosedLoopRunResult
from .failure_classification import FailureCategory, classify_failure
from .policy import BootstrapPolicyConfiguration, BootstrapPolicyEngine
from .protocol import (
    AttemptSnapshot,
    ClassificationSnapshot,
    EventEnvelope,
    MaterializationSnapshot,
    PolicyDecisionSnapshot,
    RunManifestSnapshot,
    RunStateSnapshot,
    SelectionSnapshot,
    StageUsage,
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
    "BootstrapPolicyConfiguration",
    "BootstrapPolicyEngine",
    "CostBreakdown",
    "EventEnvelope",
    "EvidenceSelectorAdapter",
    "FailureCategory",
    "MaterializationSnapshot",
    "PatchMaterializerAdapter",
    "PolicyDecisionSnapshot",
    "ProtocolValidationError",
    "ProtocolValidationIssue",
    "RunManifestSnapshot",
    "RunStateSnapshot",
    "SCHEMA_VERSION_TO_PATH",
    "SelectionSnapshot",
    "StageUsage",
    "TaskSnapshot",
    "VerificationSnapshot",
    "VillaniCodeAttemptAdapter",
    "VillaniVerifierAdapter",
    "append_jsonl_durable",
    "actual_attempt_cost",
    "classify_failure",
    "collect_protocol_validation_issues",
    "estimate_attempt_cost",
    "parse_protocol_document",
    "read_jsonl_tolerant",
    "validate_event_stream",
    "validate_jsonl_event_stream",
    "validate_protocol_document",
    "write_json_atomic",
]
