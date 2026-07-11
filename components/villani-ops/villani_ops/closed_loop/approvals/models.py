"""Versioned approval contracts."""

from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalRule(FrozenModel):
    rule_id: str
    risks: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    tool_actions: tuple[str, ...] = ()
    evidence_gaps: tuple[str, ...] = ()
    minimum_cost_usd: float | None = Field(default=None, ge=0)
    materialization_types: tuple[str, ...] = ()


class ApprovalPolicy(FrozenModel):
    schema_version: Literal["villani.approval_policy.v1"] = "villani.approval_policy.v1"
    policy_version: str
    rules: tuple[ApprovalRule, ...] = ()


class ApprovalScope(FrozenModel):
    repository: str
    paths: tuple[str, ...] = ()
    tool_actions: tuple[str, ...] = ()
    materialization_type: str
    maximum_cost_usd: float | None = Field(default=None, ge=0)


class ApprovalRecord(FrozenModel):
    schema_version: Literal["villani.approval_record.v1"] = "villani.approval_record.v1"
    approval_id: str
    run_id: str
    attempt_id: str
    approver_identity: str
    scope: ApprovalScope
    decision: Literal["approved", "denied"]
    reason: str
    issued_at: datetime
    expires_at: datetime
    policy_version: str


class ApprovalContext(FrozenModel):
    run_id: str
    attempt_id: str
    risk: str
    repository: str
    paths: tuple[str, ...] = ()
    tool_actions: tuple[str, ...] = ()
    evidence_gaps: tuple[str, ...] = ()
    cost_usd: float | None = None
    materialization_type: str


class ApprovalRequirement(FrozenModel):
    rule_id: str
    policy_version: str
    reasons: tuple[str, ...]


class ApprovalValidation(FrozenModel):
    valid: bool
    reason: str
