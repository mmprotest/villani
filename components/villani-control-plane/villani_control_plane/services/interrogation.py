from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import models
from ..config import Settings, get_settings
from ..errors import AuthorizationError, NotFoundError, ServiceError
from ..security import Principal, mask_sensitive_fields
from .auth import AuthorizationService, AuthorizedQueryScope


class PlanFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=1, max_length=64)
    operator: Literal["eq", "ne", "in", "gte", "lte"]
    value: str | int | float | bool | list[str | int | float | bool]


class PlanTimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["villani.query_plan.v1"] = "villani.query_plan.v1"
    metrics: list[str] = Field(default_factory=lambda: ["run_count"], min_length=1, max_length=8)
    dimensions: list[str] = Field(default_factory=list, max_length=4)
    filters: list[PlanFilter] = Field(default_factory=list, max_length=12)
    time_range: PlanTimeRange | None = None
    explicit_fields: list[str] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=50, ge=1, le=200)
    include_supporting_runs: bool = True


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    column: str | None
    sensitivity: str
    definition: str


DIMENSIONS: dict[str, CatalogEntry] = {
    "provider": CatalogEntry("provider_name", "metadata", "Recorded execution provider"),
    "model": CatalogEntry("model_name", "metadata", "Recorded model"),
    "agent": CatalogEntry("agent_name", "metadata", "Recorded coding agent"),
    "policy_version": CatalogEntry("policy_version", "metadata", "Routing policy version"),
    "task_category": CatalogEntry("task_category", "metadata", "Structured task category"),
    "state": CatalogEntry("status", "metadata", "Terminal or current run state"),
    "verification": CatalogEntry("verification_status", "metadata", "Latest verification status"),
    "failure_category": CatalogEntry("failure_category", "metadata", "Classified failure category"),
    "repository": CatalogEntry("repository_id", "metadata", "Authorized repository identifier"),
    "project": CatalogEntry("project_id", "metadata", "Authorized project identifier"),
}

FILTERS: dict[str, CatalogEntry] = {
    **DIMENSIONS,
    "cost_usd": CatalogEntry("cost_usd", "metadata", "Known run cost in USD"),
    "tokens": CatalogEntry("total_tokens", "metadata", "Known total tokens"),
    "duration_ms": CatalogEntry("duration_ms", "metadata", "Known run duration"),
    "queue_time_ms": CatalogEntry("queue_time_ms", "metadata", "Known queue duration"),
    "tag": CatalogEntry("tags_text", "metadata", "Exact normalized run tag"),
}

FIELDS: dict[str, CatalogEntry] = {
    "run_id": CatalogEntry("id", "metadata", "Canonical run identifier"),
    "repository": DIMENSIONS["repository"],
    "state": DIMENSIONS["state"],
    "task_text": CatalogEntry(None, "confidential", "Original task text"),
    "prompt": CatalogEntry(None, "restricted", "Model prompt content"),
    "response": CatalogEntry(None, "restricted", "Model response content"),
    "source": CatalogEntry(None, "confidential", "Repository source content"),
    "patch": CatalogEntry(None, "confidential", "Patch content"),
    "log": CatalogEntry(None, "restricted", "Command or agent logs"),
    "artifact": CatalogEntry(None, "restricted", "Artifact content"),
}

METRICS: dict[str, dict[str, str]] = {
    "run_count": {
        "expression": "COUNT(*)",
        "definition": "Count of authorized filtered runs; no outcomes are dropped.",
    },
    "verified_success_rate": {
        "expression": "SUM(CASE WHEN verification_status = 'accepted' THEN 1 ELSE 0 END) * 1.0 / NULLIF(SUM(CASE WHEN verification_status IN ('accepted','rejected') THEN 1 ELSE 0 END), 0)",
        "definition": "Accepted / (accepted + rejected); unclear, error, and missing outcomes are excluded from the denominator and reported as missingness.",
    },
    "known_cost_total_usd": {
        "expression": "SUM(cost_usd)",
        "definition": "Sum of known cost only; unknown cost remains null and is counted separately.",
    },
    "average_cost_usd": {
        "expression": "AVG(cost_usd)",
        "definition": "Average across runs with known cost; unknown cost is not numeric zero.",
    },
    "unknown_cost_count": {
        "expression": "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END)",
        "definition": "Count of runs with unknown cost.",
    },
    "average_duration_ms": {
        "expression": "AVG(duration_ms)",
        "definition": "Average across runs with known duration.",
    },
    "average_queue_time_ms": {
        "expression": "AVG(queue_time_ms)",
        "definition": "Average across runs with known queue time.",
    },
    "attempt_count": {
        "expression": "SUM(attempt_count)",
        "definition": "Sum of recorded attempt counts.",
    },
    "escalation_count": {
        "expression": "SUM(escalation_count)",
        "definition": "Sum of recorded escalation decisions.",
    },
    "verifier_spend_usd": {
        "expression": "SUM(verifier_cost_usd)",
        "definition": "Sum of known verifier cost; unknown verifier cost is reported separately.",
    },
    "rejected_spend_usd": {
        "expression": "SUM(rejected_cost_usd)",
        "definition": "Sum of known rejected-work spend; unknown values are reported separately.",
    },
    "verifier_disagreement_rate": {
        "expression": "SUM(CASE WHEN verifier_disagreement = true THEN 1 ELSE 0 END) * 1.0 / NULLIF(SUM(CASE WHEN verifier_disagreement IS NOT NULL THEN 1 ELSE 0 END), 0)",
        "definition": "Recorded disagreements / runs with a recorded disagreement boolean.",
    },
}


def semantic_catalog() -> dict[str, Any]:
    return {
        "version": "villani.semantic_catalog.v1",
        "dimensions": {key: value.definition for key, value in DIMENSIONS.items()},
        "metrics": {key: value["definition"] for key, value in METRICS.items()},
        "filters": {key: value.definition for key, value in FILTERS.items()},
        "fields": {
            key: {"definition": value.definition, "sensitivity": value.sensitivity}
            for key, value in FIELDS.items()
        },
        "maximums": {
            "dimensions": 4,
            "metrics": 8,
            "filters": 12,
            "limit": 200,
            "time_range_days": 366,
        },
    }


@dataclass(frozen=True, slots=True)
class ModelPlanResult:
    plan: QueryPlan | dict[str, Any]
    model_name: str
    usage: dict[str, Any]


class QueryPlanModel(Protocol):
    def create_plan(
        self, question: str, catalog: dict[str, Any], context: dict[str, Any] | None
    ) -> ModelPlanResult: ...


class CatalogQueryPlanModel:
    """Local deterministic planner; external models must implement the same typed boundary."""

    def create_plan(
        self, question: str, catalog: dict[str, Any], context: dict[str, Any] | None
    ) -> ModelPlanResult:
        del catalog
        lowered = question.lower()
        prior = (
            QueryPlan.model_validate(context["last_plan"])
            if context and context.get("last_plan")
            else None
        )
        metrics: list[str] = list(prior.metrics) if prior else []
        choices = [
            ("success", "verified_success_rate"),
            ("cost", "average_cost_usd"),
            ("spend", "known_cost_total_usd"),
            ("duration", "average_duration_ms"),
            ("latency", "average_duration_ms"),
            ("queue", "average_queue_time_ms"),
            ("attempt", "attempt_count"),
            ("escalat", "escalation_count"),
            ("verifier spend", "verifier_spend_usd"),
            ("disagreement", "verifier_disagreement_rate"),
            ("rejected spend", "rejected_spend_usd"),
        ]
        for needle, metric in choices:
            if needle in lowered and metric not in metrics:
                metrics.append(metric)
        if not metrics:
            metrics = ["run_count"]
        dimensions = list(prior.dimensions) if prior else []
        for label in (
            "provider",
            "model",
            "agent",
            "policy_version",
            "task_category",
            "state",
            "verification",
            "failure_category",
            "repository",
            "project",
        ):
            spoken = label.replace("_", " ")
            if f"by {spoken}" in lowered or f"per {spoken}" in lowered:
                dimensions = [label]
                break
        filters = list(prior.filters) if prior else []
        for state in ("completed", "failed", "exhausted", "accepted"):
            if re.search(rf"\b{state}\b", lowered):
                filters = [item for item in filters if item.field != "state"] + [
                    PlanFilter(field="state", operator="eq", value=state)
                ]
                break
        repository = re.search(r'repository\s+["\']([^"\']{1,128})["\']', question, re.I)
        if repository:
            filters = [item for item in filters if item.field != "repository"] + [
                PlanFilter(field="repository", operator="eq", value=repository.group(1))
            ]
        days = re.search(r"last\s+(\d{1,3})\s+days?", lowered)
        time_range = prior.time_range if prior else None
        if days:
            end = datetime.now(timezone.utc)
            time_range = PlanTimeRange(start=end - timedelta(days=int(days.group(1))), end=end)
        sensitive = [
            field
            for field in ("task_text", "prompt", "response", "source", "patch", "log", "artifact")
            if re.search(rf"\b{field}s?\b", lowered)
        ]
        plan = QueryPlan(
            metrics=metrics[:8],
            dimensions=dimensions,
            filters=filters[:12],
            time_range=time_range,
            explicit_fields=sensitive,
            limit=50,
            include_supporting_runs=True,
        )
        return ModelPlanResult(
            plan,
            "catalog_query_planner_v1",
            {
                "input_characters": len(question),
                "output_plan_fields": len(plan.model_dump()),
                "accounting_status": "not_applicable",
            },
        )


@dataclass(frozen=True, slots=True)
class AuthorizedPlan:
    plan: QueryPlan
    scope: AuthorizedQueryScope
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    sql: str
    parameters: dict[str, Any]
    count_sql: str
    metadata_sql: str
    supporting_sql: str


class QueryPlanValidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate(self, value: QueryPlan | dict[str, Any]) -> QueryPlan:
        try:
            plan = value if isinstance(value, QueryPlan) else QueryPlan.model_validate(value)
        except ValidationError as error:
            raise ServiceError("model returned a malformed QueryPlan") from error
        unknown_metrics = sorted(set(plan.metrics) - METRICS.keys())
        unknown_dimensions = sorted(set(plan.dimensions) - DIMENSIONS.keys())
        unknown_filters = sorted({item.field for item in plan.filters} - FILTERS.keys())
        unknown_fields = sorted(set(plan.explicit_fields) - FIELDS.keys())
        if unknown_metrics or unknown_dimensions or unknown_filters or unknown_fields:
            raise ServiceError("QueryPlan references a non-allowlisted catalog item")
        for item in plan.filters:
            if item.operator == "in" and (
                not isinstance(item.value, list) or not item.value or len(item.value) > 50
            ):
                raise ServiceError("QueryPlan IN filters require 1 to 50 values")
            if item.operator != "in" and isinstance(item.value, list):
                raise ServiceError("QueryPlan scalar filter received a list")
        now = datetime.now(timezone.utc)
        if plan.time_range is None:
            plan = plan.model_copy(
                update={
                    "time_range": PlanTimeRange(
                        start=now
                        - timedelta(days=self.settings.natural_language_query_default_days),
                        end=now,
                    )
                }
            )
        start = plan.time_range.start
        end = plan.time_range.end
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start >= end
            or end - start > timedelta(days=366)
        ):
            raise ServiceError(
                "QueryPlan time range must be bounded, timezone-aware, ordered, and at most 366 days"
            )
        if plan.limit > self.settings.natural_language_query_max_result_rows:
            raise ServiceError("QueryPlan result limit exceeds policy")
        return plan


class QueryPlanCompiler:
    TABLE = "runs"

    def compile(self, authorized: AuthorizedPlan) -> CompiledQuery:
        plan = authorized.plan
        parameters: dict[str, Any] = {
            "scope_organization_id": authorized.scope.organization_id,
            "scope_workspace_id": authorized.scope.workspace_id,
            "time_start": plan.time_range.start,
            "time_end": plan.time_range.end,
        }
        predicates = [
            "organization_id = :scope_organization_id",
            "workspace_id = :scope_workspace_id",
            "deleted_at IS NULL",
            "first_observed_at >= :time_start",
            "first_observed_at < :time_end",
        ]
        for index, item in enumerate(plan.filters):
            column = FILTERS[item.field].column
            if item.field == "tag":
                parameters[f"filter_{index}"] = f"%|{item.value}|%"
                predicates.append(f"{column} LIKE :filter_{index}")
                continue
            operator = {"eq": "=", "ne": "!=", "gte": ">=", "lte": "<="}.get(item.operator)
            if item.operator == "in":
                names = []
                for value_index, value in enumerate(item.value):
                    name = f"filter_{index}_{value_index}"
                    parameters[name] = value
                    names.append(f":{name}")
                predicates.append(f"{column} IN ({','.join(names)})")
            else:
                parameters[f"filter_{index}"] = item.value
                predicates.append(f"{column} {operator} :filter_{index}")
        dimensions = [(name, DIMENSIONS[name].column) for name in plan.dimensions]
        select_parts = [f"{column} AS {name}" for name, column in dimensions]
        select_parts.extend(f"{METRICS[name]['expression']} AS {name}" for name in plan.metrics)
        where = " AND ".join(predicates)
        group = f" GROUP BY {','.join(column for _, column in dimensions)}" if dimensions else ""
        order = f" ORDER BY {','.join(name for name, _ in dimensions)}" if dimensions else ""
        parameters["result_limit"] = plan.limit
        sql = f"SELECT {','.join(select_parts)} FROM {self.TABLE} WHERE {where}{group}{order} LIMIT :result_limit"
        count_sql = f"SELECT COUNT(*) AS estimated_rows FROM {self.TABLE} WHERE {where}"
        metadata_sql = f"SELECT COUNT(*) AS row_count, MAX(last_observed_at) AS data_freshness, SUM(CASE WHEN verification_status IS NULL OR verification_status NOT IN ('accepted','rejected') THEN 1 ELSE 0 END) AS unknown_outcomes, SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unknown_cost, SUM(CASE WHEN total_tokens IS NULL THEN 1 ELSE 0 END) AS unknown_tokens, SUM(CASE WHEN duration_ms IS NULL THEN 1 ELSE 0 END) AS unknown_duration FROM {self.TABLE} WHERE {where}"
        supporting_parameters = dict(parameters)
        supporting_parameters["support_limit"] = min(20, plan.limit)
        supporting_sql = f"SELECT id,last_observed_at FROM {self.TABLE} WHERE {where} ORDER BY last_observed_at DESC,id LIMIT :support_limit"
        return CompiledQuery(sql, parameters, count_sql, metadata_sql, supporting_sql)


@dataclass(frozen=True, slots=True)
class InterrogationRequest:
    question: str
    conversation_id: str | None = None


class NaturalLanguageInterrogationService:
    def __init__(
        self,
        session: Session,
        *,
        planner: QueryPlanModel | None = None,
        authorization: AuthorizationService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.planner = planner or CatalogQueryPlanModel()
        self.authorization = authorization or AuthorizationService()
        self.settings = settings or get_settings()
        self.validator = QueryPlanValidator(self.settings)
        self.compiler = QueryPlanCompiler()

    def ask(self, request: InterrogationRequest, principal: Principal) -> dict[str, Any]:
        if not self.settings.natural_language_query_enabled:
            raise NotFoundError("natural-language interrogation is disabled")
        if not request.question.strip() or len(request.question) > 2_000:
            raise ServiceError("question must contain 1 to 2000 characters")
        conversation = self._conversation(request.conversation_id, principal)
        context = conversation.structured_context if conversation else None
        proposal: ModelPlanResult | None = None
        plan: QueryPlan | None = None
        compiled: CompiledQuery | None = None
        try:
            proposal = self.planner.create_plan(request.question, semantic_catalog(), context)
            plan = self.validator.validate(proposal.plan)
            scope = self.authorization.query_scope(principal)
            sensitivities = {name: FIELDS[name].sensitivity for name in plan.explicit_fields}
            fields = self.authorization.authorize_query_fields(
                principal, plan.explicit_fields, sensitivities
            )
            if any(FIELDS[field].column is None for field in fields):
                raise AuthorizationError(
                    "requested sensitive fields are not available through aggregate interrogation"
                )
            authorized = AuthorizedPlan(plan, scope, tuple(fields))
            compiled = self.compiler.compile(authorized)
            estimate = int(
                self.session.execute(text(compiled.count_sql), compiled.parameters).scalar_one()
            )
            estimated_cells = estimate * max(1, len(plan.dimensions) + len(plan.metrics))
            if estimate > self.settings.natural_language_query_max_scan_rows:
                raise ServiceError(
                    "QueryPlan rejected because estimated scan cardinality exceeds policy"
                )
            rows = [
                dict(row)
                for row in self.session.execute(text(compiled.sql), compiled.parameters).mappings()
            ]
            metadata = dict(
                self.session.execute(text(compiled.metadata_sql), compiled.parameters)
                .mappings()
                .one()
            )
            supporting = []
            if plan.include_supporting_runs:
                support_parameters = dict(compiled.parameters)
                support_parameters["support_limit"] = min(20, plan.limit)
                supporting = [
                    {
                        "run_id": row.id,
                        "last_observed_at": row.last_observed_at,
                        "url": f"/runs/{row.id}",
                    }
                    for row in self.session.execute(
                        text(compiled.supporting_sql), support_parameters
                    )
                ]
            interpretation = self._interpret(plan)
            conversation = self._save_context(conversation, plan, interpretation, principal)
            self._audit(
                request.question,
                proposal,
                plan,
                compiled,
                "completed",
                None,
                conversation.id,
                principal,
            )
            return {
                "answer": f"The authorized structured query returned {len(rows)} aggregate row(s) from {metadata.get('row_count', 0)} supporting run(s).",
                "interpreted_query": interpretation,
                "query_plan": plan.model_dump(mode="json"),
                "semantic_catalog_version": "villani.semantic_catalog.v1",
                "metric_definitions": {name: METRICS[name]["definition"] for name in plan.metrics},
                "filters": [item.model_dump(mode="json") for item in plan.filters],
                "authorization": {
                    "permission_version": scope.permission_version,
                    "tenant_predicates_injected": True,
                },
                "estimate": {
                    "scan_rows": estimate,
                    "result_limit": plan.limit,
                    "estimated_cells": estimated_cells,
                    "cost_units": estimated_cells,
                },
                "data_freshness": metadata.get("data_freshness"),
                "row_count": metadata.get("row_count", 0),
                "uncertainty": {
                    "unknown_outcomes": metadata.get("unknown_outcomes", 0),
                    "unknown_cost": metadata.get("unknown_cost", 0),
                    "unknown_tokens": metadata.get("unknown_tokens", 0),
                    "unknown_duration": metadata.get("unknown_duration", 0),
                },
                "rows": mask_sensitive_fields(rows),
                "supporting_runs": supporting,
                "conversation": {
                    "id": conversation.id,
                    "version": conversation.version,
                    "stored_context": "structured_query_only",
                },
            }
        except Exception as error:
            self._audit(
                request.question,
                proposal,
                plan,
                compiled,
                "rejected",
                type(error).__name__,
                conversation.id if conversation else None,
                principal,
            )
            raise

    def _conversation(
        self, conversation_id: str | None, principal: Principal
    ) -> models.QueryConversation | None:
        if not conversation_id:
            return None
        value = self.session.get(
            models.QueryConversation, (principal.organization_id, conversation_id)
        )
        if (
            value is None
            or value.workspace_id != principal.workspace_id
            or value.owner_id != principal.token_id
        ):
            raise NotFoundError("query conversation not found")
        return value

    def _save_context(
        self,
        conversation: models.QueryConversation | None,
        plan: QueryPlan,
        interpretation: str,
        principal: Principal,
    ) -> models.QueryConversation:
        structured = {
            "schema_version": "villani.query_context.v1",
            "last_plan": plan.model_dump(mode="json"),
            "last_interpretation": interpretation,
        }
        if conversation is None:
            conversation = models.QueryConversation(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                owner_id=principal.token_id,
                structured_context=structured,
                version=1,
            )
            self.session.add(conversation)
        else:
            conversation.structured_context = structured
            conversation.version += 1
        self.session.flush()
        return conversation

    def _audit(
        self,
        question: str,
        proposal: ModelPlanResult | None,
        plan: QueryPlan | None,
        compiled: CompiledQuery | None,
        status: str,
        error_category: str | None,
        conversation_id: str | None,
        principal: Principal,
    ) -> None:
        safe_plan = self._redacted_plan(plan) if plan else {"status": "invalid_or_unavailable"}
        row = models.QueryAuditLog(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            actor_id=principal.token_id,
            conversation_id=conversation_id,
            question_sha256=hashlib.sha256(question.encode()).hexdigest(),
            model_name=proposal.model_name if proposal else "unavailable",
            model_usage=mask_sensitive_fields(proposal.usage)
            if proposal
            else {"accounting_status": "unknown"},
            query_plan=safe_plan,
            sql_sha256=hashlib.sha256(compiled.sql.encode()).hexdigest() if compiled else None,
            status=status,
            error_category=error_category,
        )
        self.session.add(row)
        self.session.commit()

    @staticmethod
    def _redacted_plan(plan: QueryPlan) -> dict[str, Any]:
        value = plan.model_dump(mode="json")
        for item in value["filters"]:
            item["value"] = "[redacted]"
        return mask_sensitive_fields(value)

    @staticmethod
    def _interpret(plan: QueryPlan) -> str:
        metrics = ", ".join(plan.metrics)
        grouping = f" grouped by {', '.join(plan.dimensions)}" if plan.dimensions else ""
        return f"Compute {metrics}{grouping} between {plan.time_range.start.isoformat()} and {plan.time_range.end.isoformat()} with {len(plan.filters)} structured filter(s)."
