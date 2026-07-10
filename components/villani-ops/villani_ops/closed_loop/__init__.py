"""Canonical closed-loop protocol contracts and durable I/O primitives.

This package intentionally contains no controller or execution behavior.
"""

from .durable_io import append_jsonl_durable, read_jsonl_tolerant, write_json_atomic
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
    "EventEnvelope",
    "MaterializationSnapshot",
    "PolicyDecisionSnapshot",
    "ProtocolValidationError",
    "ProtocolValidationIssue",
    "RunManifestSnapshot",
    "RunStateSnapshot",
    "SCHEMA_VERSION_TO_PATH",
    "SelectionSnapshot",
    "TaskSnapshot",
    "VerificationSnapshot",
    "append_jsonl_durable",
    "collect_protocol_validation_issues",
    "parse_protocol_document",
    "read_jsonl_tolerant",
    "validate_event_stream",
    "validate_jsonl_event_stream",
    "validate_protocol_document",
    "write_json_atomic",
]
