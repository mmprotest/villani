"""Canonical final-delivery provenance statements and signatures."""

from __future__ import annotations
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProvenanceStatement(FrozenModel):
    schema_version: Literal["villani.final_provenance.v1"] = (
        "villani.final_provenance.v1"
    )
    run_id: str
    selected_attempt_id: str
    patch_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verification_graph_id: str
    verification_graph_version: str
    evidence_digests: tuple[str, ...]
    approval_digests: tuple[str, ...]
    materializer_name: str
    materializer_version: str
    materialization_type: str
    issued_at: datetime
    key_id: str


class SignedProvenance(FrozenModel):
    statement: ProvenanceStatement
    algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")


def canonical_bytes(statement: ProvenanceStatement) -> bytes:
    return json.dumps(
        statement.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def record_digest(value: object) -> str:
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class ProvenanceSigner:
    def __init__(self, key: bytes, *, key_id: str) -> None:
        if not key:
            raise ValueError("provenance signing key must not be empty")
        self.key, self.key_id = key, key_id

    def sign(self, statement: ProvenanceStatement) -> SignedProvenance:
        if statement.key_id != self.key_id:
            raise ValueError("provenance key id mismatch")
        return SignedProvenance(
            statement=statement,
            signature=hmac.new(
                self.key, canonical_bytes(statement), hashlib.sha256
            ).hexdigest(),
        )

    def verify(self, signed: SignedProvenance) -> bool:
        expected = hmac.new(
            self.key, canonical_bytes(signed.statement), hashlib.sha256
        ).hexdigest()
        return signed.statement.key_id == self.key_id and hmac.compare_digest(
            expected, signed.signature
        )


def build_statement(
    *,
    run_id: str,
    attempt_id: str,
    patch_sha256: str,
    graph_id: str,
    graph_version: str,
    evidence_digests: tuple[str, ...],
    approval_digests: tuple[str, ...],
    materializer_name: str,
    materializer_version: str,
    materialization_type: str,
    key_id: str,
    issued_at: datetime | None = None,
) -> ProvenanceStatement:
    return ProvenanceStatement(
        run_id=run_id,
        selected_attempt_id=attempt_id,
        patch_sha256=patch_sha256,
        verification_graph_id=graph_id,
        verification_graph_version=graph_version,
        evidence_digests=tuple(sorted(evidence_digests)),
        approval_digests=tuple(sorted(approval_digests)),
        materializer_name=materializer_name,
        materializer_version=materializer_version,
        materialization_type=materialization_type,
        issued_at=issued_at or datetime.now(timezone.utc),
        key_id=key_id,
    )
