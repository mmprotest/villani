from __future__ import annotations

import base64
import csv
import io
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .. import models
from ..errors import AuthorizationError, ConflictError, NotFoundError, ServiceError
from ..schemas import (
    AlertRuleRequest,
    FeedbackRequest,
    FleetExportRequest,
    FleetFilters,
    FleetRunPage,
    FleetSearchRequest,
    MetricRequest,
    ReviewQueueRequest,
    SavedViewRequest,
)
from ..security import Principal, mask_sensitive_fields

METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "verified_success_rate": {
        "numerator": "runs whose latest verification outcome is accepted",
        "denominator": "runs whose latest verification outcome is accepted or rejected",
        "unknown_rule": "unclear, error, not-run, and missing verification are reported separately",
    },
    "cost_per_accepted_change": {
        "numerator": "sum of known run cost for accepted changes",
        "denominator": "accepted changes with known run cost",
        "unknown_rule": "accepted changes with unknown cost are counted separately, never as zero",
    },
    "cost_per_materialized_change": {
        "numerator": "sum of known run cost for materialized changes",
        "denominator": "materialized changes with known run cost",
        "unknown_rule": "materialized changes with unknown cost are counted separately",
    },
    "cost_per_merged_change": {
        "numerator": "sum of known run cost for merged changes",
        "denominator": "merged changes with known run cost",
        "unknown_rule": "merged changes with unknown cost are counted separately",
    },
    "false_acceptance_rate": {
        "numerator": "verified accepted runs later labeled rejected, reverted, or defect-associated",
        "denominator": "verified accepted runs having a human or downstream label",
        "unknown_rule": "accepted runs without a label are reported as unlabeled",
    },
    "false_rejection_rate": {
        "numerator": "verified rejected runs later labeled approved or merged",
        "denominator": "verified rejected runs having a human or downstream label",
        "unknown_rule": "rejected runs without a label are reported as unlabeled",
    },
    "duration_ms": {
        "numerator": "sum of known end-to-end run durations",
        "denominator": "runs with known duration",
        "unknown_rule": "runs without duration are reported separately",
    },
    "queue_time_ms": {
        "numerator": "sum of known pre-execution queue durations",
        "denominator": "runs with known queue time",
        "unknown_rule": "runs without queue telemetry are reported separately",
    },
    "attempts_and_escalations": {
        "numerator": "recorded attempts or escalation decisions",
        "denominator": "all filtered runs",
        "unknown_rule": "missing counters remain explicit zero only when ingestion recorded no matching event",
    },
    "verifier_spend_usd": {
        "numerator": "sum of known verifier cost",
        "denominator": "runs with known verifier cost",
        "unknown_rule": "unknown verifier cost is counted separately and never contributes zero",
    },
    "verifier_disagreement": {
        "numerator": "runs with recorded verifier disagreement",
        "denominator": "runs with a recorded disagreement boolean",
        "unknown_rule": "runs without a disagreement observation are reported separately",
    },
    "rejected_wasted_spend_usd": {
        "numerator": "sum of known spend assigned to rejected or otherwise wasted work",
        "denominator": "runs with known rejected-work spend",
        "unknown_rule": "unknown rejected-work spend is counted separately",
    },
    "comparisons": {
        "numerator": "the selected metric numerator within each agent, model, provider, or policy group",
        "denominator": "the selected metric denominator within the same group and filters",
        "unknown_rule": "missing dimension values form an explicit unknown group",
    },
}


def _encode_cursor(run: models.Run) -> str:
    raw = json.dumps([run.last_observed_at.isoformat(), run.id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        timestamp = datetime.fromisoformat(value[0].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp, str(value[1])
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ServiceError("invalid fleet cursor") from error


def _apply_filters(query, filters: FleetFilters, principal: Principal):
    if filters.organization_id and filters.organization_id != principal.organization_id:
        raise AuthorizationError("organization filter is outside the token scope")
    if filters.workspace_id and filters.workspace_id != principal.workspace_id:
        raise AuthorizationError("workspace filter is outside the token scope")
    query = query.where(
        models.Run.organization_id == principal.organization_id,
        models.Run.workspace_id == principal.workspace_id,
        models.Run.deleted_at.is_(None),
    )
    exact = {
        models.Run.project_id: filters.project_id,
        models.Run.repository_id: filters.repository_id,
        models.Run.agent_name: filters.agent,
        models.Run.model_name: filters.model,
        models.Run.provider_name: filters.provider,
        models.Run.policy_version: filters.policy_version,
        models.Run.task_category: filters.task_category,
        models.Run.status: filters.state,
        models.Run.verification_status: filters.verification,
        models.Run.failure_category: filters.failure_category,
    }
    for column, value in exact.items():
        if value is not None:
            query = query.where(column == value)
    ranges = (
        (models.Run.first_observed_at, filters.started_after, filters.started_before),
        (models.Run.cost_usd, filters.min_cost_usd, filters.max_cost_usd),
        (models.Run.total_tokens, filters.min_tokens, filters.max_tokens),
        (models.Run.duration_ms, filters.min_duration_ms, filters.max_duration_ms),
    )
    for column, minimum, maximum in ranges:
        if minimum is not None:
            query = query.where(column.is_not(None), column >= minimum)
        if maximum is not None:
            query = query.where(column.is_not(None), column <= maximum)
    for tag in filters.tags:
        query = query.where(models.Run.tags_text.like(f"%|{tag}|%"))
    return query


def _run_document(run: models.Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "organization_id": run.organization_id,
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "repository_id": run.repository_id,
        "state": run.status,
        "started_at": run.first_occurred_at,
        "last_observed_at": run.last_observed_at,
        "agent": run.agent_name,
        "model": run.model_name,
        "provider": run.provider_name,
        "policy_version": run.policy_version,
        "task_category": run.task_category,
        "verification": run.verification_status,
        "failure_category": run.failure_category,
        "cost_usd": run.cost_usd,
        "cost_accounting_status": run.cost_accounting_status,
        "total_tokens": run.total_tokens,
        "token_accounting_status": run.token_accounting_status,
        "duration_ms": run.duration_ms,
        "queue_time_ms": run.queue_time_ms,
        "attempts": run.attempt_count,
        "escalations": run.escalation_count,
        "verifier_cost_usd": run.verifier_cost_usd,
        "verifier_disagreement": run.verifier_disagreement,
        "rejected_cost_usd": run.rejected_cost_usd,
        "tags": run.tags,
    }


class FleetObservabilityService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def search(self, request: FleetSearchRequest, principal: Principal) -> FleetRunPage:
        query = _apply_filters(select(models.Run), request.filters, principal)
        if request.cursor:
            timestamp, run_id = _decode_cursor(request.cursor)
            query = query.where(
                or_(
                    models.Run.last_observed_at < timestamp,
                    and_(models.Run.last_observed_at == timestamp, models.Run.id > run_id),
                )
            )
        rows = list(
            self.session.scalars(
                query.order_by(models.Run.last_observed_at.desc(), models.Run.id).limit(
                    request.limit + 1
                )
            )
        )
        page = rows[: request.limit]
        return FleetRunPage(
            runs=[_run_document(run) for run in page],
            next_cursor=_encode_cursor(page[-1]) if len(rows) > request.limit and page else None,
        )

    def metric_definitions(self) -> dict[str, Any]:
        return {"version": "villani.fleet_metrics.v1", "metrics": METRIC_DEFINITIONS}

    def metrics(self, request: MetricRequest, principal: Principal) -> dict[str, Any]:
        runs = list(
            self.session.scalars(_apply_filters(select(models.Run), request.filters, principal))
        )
        outcome_rows = list(
            self.session.scalars(
                select(models.Outcome)
                .where(
                    models.Outcome.organization_id == principal.organization_id,
                    models.Outcome.workspace_id == principal.workspace_id,
                    models.Outcome.run_id.in_([run.id for run in runs] or ["__none__"]),
                )
                .order_by(models.Outcome.run_id, models.Outcome.version)
            )
        )
        outcomes = {row.run_id: row.document for row in outcome_rows}
        feedback_rows = list(
            self.session.scalars(
                select(models.RunFeedback)
                .where(
                    models.RunFeedback.organization_id == principal.organization_id,
                    models.RunFeedback.workspace_id == principal.workspace_id,
                    models.RunFeedback.kind == "developer_disposition",
                    models.RunFeedback.run_id.in_([run.id for run in runs] or ["__none__"]),
                )
                .order_by(models.RunFeedback.run_id, models.RunFeedback.version)
            )
        )
        dispositions = {row.run_id: row.document.get("disposition") for row in feedback_rows}

        def compute(items: list[models.Run]) -> dict[str, Any]:
            verified = [
                run
                for run in items
                if (outcomes.get(run.id, {}).get("verification_status") or run.verification_status)
                in {"accepted", "rejected"}
            ]
            accepted = [
                run
                for run in verified
                if (outcomes.get(run.id, {}).get("verification_status") or run.verification_status)
                == "accepted"
            ]
            rejected = [run for run in verified if run not in accepted]
            materialized = [
                run for run in items if outcomes.get(run.id, {}).get("materialized") is True
            ]
            merged = [run for run in items if outcomes.get(run.id, {}).get("merged") is True]

            def cost_metric(selected: list[models.Run]) -> dict[str, Any]:
                known = [run.cost_usd for run in selected if run.cost_usd is not None]
                return {
                    "value": sum(known) / len(known) if known else None,
                    "known_denominator": len(known),
                    "unknown_cost_count": len(selected) - len(known),
                    "total_changes": len(selected),
                }

            false_acceptance_labeled = [run for run in accepted if dispositions.get(run.id)]
            false_rejection_labeled = [run for run in rejected if dispositions.get(run.id)]
            false_acceptance = [
                run
                for run in false_acceptance_labeled
                if dispositions.get(run.id) in {"rejected", "reverted", "defect"}
            ]
            false_rejection = [
                run
                for run in false_rejection_labeled
                if dispositions.get(run.id) in {"approved", "merged"}
            ]
            known_duration = [run.duration_ms for run in items if run.duration_ms is not None]
            known_queue = [run.queue_time_ms for run in items if run.queue_time_ms is not None]
            known_verifier = [
                run.verifier_cost_usd for run in items if run.verifier_cost_usd is not None
            ]
            known_waste = [
                run.rejected_cost_usd for run in items if run.rejected_cost_usd is not None
            ]
            return {
                "run_count": len(items),
                "verified_success_rate": {
                    "value": len(accepted) / len(verified) if verified else None,
                    "numerator": len(accepted),
                    "denominator": len(verified),
                    "unknown_outcome_count": len(items) - len(verified),
                },
                "cost_per_accepted_change": cost_metric(accepted),
                "cost_per_materialized_change": cost_metric(materialized),
                "cost_per_merged_change": cost_metric(merged),
                "duration_ms": {
                    "average": sum(known_duration) / len(known_duration)
                    if known_duration
                    else None,
                    "known_count": len(known_duration),
                    "unknown_count": len(items) - len(known_duration),
                },
                "queue_time_ms": {
                    "average": sum(known_queue) / len(known_queue) if known_queue else None,
                    "known_count": len(known_queue),
                    "unknown_count": len(items) - len(known_queue),
                },
                "attempts": sum(run.attempt_count for run in items),
                "escalations": sum(run.escalation_count for run in items),
                "false_acceptance_rate": {
                    "value": len(false_acceptance) / len(false_acceptance_labeled)
                    if false_acceptance_labeled
                    else None,
                    "numerator": len(false_acceptance),
                    "denominator": len(false_acceptance_labeled),
                    "unlabeled_count": len(accepted) - len(false_acceptance_labeled),
                },
                "false_rejection_rate": {
                    "value": len(false_rejection) / len(false_rejection_labeled)
                    if false_rejection_labeled
                    else None,
                    "numerator": len(false_rejection),
                    "denominator": len(false_rejection_labeled),
                    "unlabeled_count": len(rejected) - len(false_rejection_labeled),
                },
                "verifier_spend_usd": {
                    "known_total": sum(known_verifier),
                    "known_count": len(known_verifier),
                    "unknown_count": len(items) - len(known_verifier),
                },
                "verifier_disagreement": {
                    "count": sum(run.verifier_disagreement is True for run in items),
                    "known_count": sum(run.verifier_disagreement is not None for run in items),
                    "unknown_count": sum(run.verifier_disagreement is None for run in items),
                },
                "rejected_wasted_spend_usd": {
                    "known_total": sum(known_waste),
                    "known_count": len(known_waste),
                    "unknown_count": len(items) - len(known_waste),
                },
            }

        result = compute(runs)
        comparisons: dict[str, Any] = {}
        if request.group_by:
            attribute = {
                "agent": "agent_name",
                "model": "model_name",
                "provider": "provider_name",
                "policy_version": "policy_version",
            }[request.group_by]
            groups: dict[str, list[models.Run]] = defaultdict(list)
            for run in runs:
                groups[str(getattr(run, attribute) or "unknown")].append(run)
            comparisons = {name: compute(items) for name, items in sorted(groups.items())}
        return {
            "definition_version": "villani.fleet_metrics.v1",
            "metrics": result,
            "comparisons": comparisons,
        }

    def create_view(self, request: SavedViewRequest, principal: Principal) -> dict[str, Any]:
        document = request.model_dump()
        document["filter_ast"] = mask_sensitive_fields(document["filter_ast"])
        view = models.SavedView(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            owner_id=principal.token_id,
            **document,
        )
        self.session.add(view)
        self.session.commit()
        return self._view_document(view)

    def list_views(self, principal: Principal) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.SavedView)
            .where(
                models.SavedView.organization_id == principal.organization_id,
                models.SavedView.workspace_id == principal.workspace_id,
                or_(
                    models.SavedView.owner_id == principal.token_id,
                    models.SavedView.visibility == "workspace",
                ),
            )
            .order_by(models.SavedView.name)
        )
        return [self._view_document(row) for row in rows]

    def update_view(
        self, view_id: str, request: SavedViewRequest, principal: Principal
    ) -> dict[str, Any]:
        view = self.session.get(models.SavedView, (principal.organization_id, view_id))
        if (
            view is None
            or view.workspace_id != principal.workspace_id
            or view.owner_id != principal.token_id
        ):
            raise NotFoundError("saved view not found")
        if request.version != view.version:
            raise ConflictError("saved view version conflict")
        for field in ("name", "visibility", "filter_ast", "columns", "sort"):
            setattr(view, field, getattr(request, field))
        view.version += 1
        self.session.commit()
        return self._view_document(view)

    @staticmethod
    def _view_document(view: models.SavedView) -> dict[str, Any]:
        return {
            "id": view.id,
            "owner": view.owner_id,
            "name": view.name,
            "visibility": view.visibility,
            "filter_ast": view.filter_ast,
            "columns": view.columns,
            "sort": view.sort,
            "version": view.version,
        }

    def create_feedback(
        self, run_id: str, request: FeedbackRequest, principal: Principal
    ) -> dict[str, Any]:
        run = self._run(run_id, principal)
        version = 1
        if request.corrects_feedback_id:
            corrected = self.session.get(
                models.RunFeedback, (principal.organization_id, request.corrects_feedback_id)
            )
            if (
                corrected is None
                or corrected.workspace_id != principal.workspace_id
                or corrected.run_id != run_id
            ):
                raise NotFoundError("feedback not found")
            version = corrected.version + 1
        feedback = models.RunFeedback(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            run_id=run_id,
            actor_id=principal.token_id,
            kind=request.kind,
            document=mask_sensitive_fields(request.document),
            corrects_feedback_id=request.corrects_feedback_id,
            version=version,
        )
        self.session.add(feedback)
        if request.kind == "label":
            tags = request.document.get("tags", [])
            if isinstance(tags, list):
                run.tags = sorted({str(tag) for tag in tags})
                run.tags_text = "|" + "|".join(run.tags) + "|" if run.tags else ""
        self.session.commit()
        return {
            "id": feedback.id,
            "run_id": run_id,
            "kind": feedback.kind,
            "document": feedback.document,
            "version": version,
            "corrects_feedback_id": feedback.corrects_feedback_id,
        }

    def feedback(self, run_id: str, principal: Principal) -> list[dict[str, Any]]:
        self._run(run_id, principal)
        rows = self.session.scalars(
            select(models.RunFeedback)
            .where(
                models.RunFeedback.organization_id == principal.organization_id,
                models.RunFeedback.workspace_id == principal.workspace_id,
                models.RunFeedback.run_id == run_id,
            )
            .order_by(models.RunFeedback.created_at, models.RunFeedback.id)
        )
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "document": row.document,
                "version": row.version,
                "corrects_feedback_id": row.corrects_feedback_id,
            }
            for row in rows
        ]

    def enqueue_review(self, request: ReviewQueueRequest, principal: Principal) -> dict[str, Any]:
        self._run(request.run_id, principal)
        item = models.ReviewQueueItem(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            **request.model_dump(),
        )
        self.session.add(item)
        self.session.commit()
        return {
            "id": item.id,
            "run_id": item.run_id,
            "queue": item.queue,
            "priority": item.priority,
            "state": item.state,
            "reason": item.reason,
        }

    def review_queue(self, principal: Principal, queue: str | None = None) -> list[dict[str, Any]]:
        query = select(models.ReviewQueueItem).where(
            models.ReviewQueueItem.organization_id == principal.organization_id,
            models.ReviewQueueItem.workspace_id == principal.workspace_id,
        )
        if queue:
            query = query.where(models.ReviewQueueItem.queue == queue)
        rows = self.session.scalars(
            query.order_by(
                models.ReviewQueueItem.priority.desc(), models.ReviewQueueItem.created_at
            )
        )
        return [
            {
                "id": row.id,
                "run_id": row.run_id,
                "queue": row.queue,
                "priority": row.priority,
                "state": row.state,
                "assigned_to": row.assigned_to,
                "reason": row.reason,
            }
            for row in rows
        ]

    def clusters(self, principal: Principal) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.FailureCluster)
            .where(
                models.FailureCluster.organization_id == principal.organization_id,
                models.FailureCluster.workspace_id == principal.workspace_id,
            )
            .order_by(
                models.FailureCluster.occurrence_count.desc(), models.FailureCluster.signature
            )
        )
        return [
            {
                "signature": row.signature,
                "failure_category": row.failure_category,
                "label": row.deterministic_label,
                "occurrence_count": row.occurrence_count,
                "first_seen_at": row.first_seen_at,
                "last_seen_at": row.last_seen_at,
                "advisory_label": row.advisory_label,
                "advisory_label_version": row.advisory_label_version,
            }
            for row in rows
        ]

    def export(self, request: FleetExportRequest, principal: Principal) -> tuple[str, str]:
        rows = list(
            self.session.scalars(
                _apply_filters(select(models.Run), request.filters, principal).order_by(
                    models.Run.last_observed_at.desc(), models.Run.id
                )
            )
        )
        documents = [mask_sensitive_fields(_run_document(row)) for row in rows]
        if request.format == "json":
            return "application/json", json.dumps(
                {"runs": documents}, default=str, separators=(",", ":")
            )
        output = io.StringIO(newline="")
        fields = list(documents[0]) if documents else ["id"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for document in documents:
            writer.writerow(
                {
                    key: json.dumps(value) if isinstance(value, (list, dict)) else value
                    for key, value in document.items()
                }
            )
        return "text/csv", output.getvalue()

    def _run(self, run_id: str, principal: Principal) -> models.Run:
        run = self.session.get(models.Run, (principal.organization_id, run_id))
        if run is None or run.workspace_id != principal.workspace_id or run.deleted_at is not None:
            raise NotFoundError("run not found")
        return run


class AlertService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, request: AlertRuleRequest, principal: Principal) -> dict[str, Any]:
        if request.destination.get("type") != "test_webhook":
            raise ServiceError("only test_webhook destinations are supported")
        rule = models.AlertRule(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            owner_id=principal.token_id,
            **request.model_dump(),
        )
        self.session.add(rule)
        self.session.commit()
        return self._document(rule)

    def list(self, principal: Principal) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.AlertRule)
            .where(
                models.AlertRule.organization_id == principal.organization_id,
                models.AlertRule.workspace_id == principal.workspace_id,
            )
            .order_by(models.AlertRule.name)
        )
        return [self._document(row) for row in rows]

    @staticmethod
    def _document(rule: models.AlertRule) -> dict[str, Any]:
        return {
            "id": rule.id,
            "name": rule.name,
            "rule_type": rule.rule_type,
            "filter_ast": rule.filter_ast,
            "threshold": rule.threshold,
            "cooldown_seconds": rule.cooldown_seconds,
            "destination": mask_sensitive_fields(rule.destination),
            "enabled": rule.enabled,
        }

    def events(self, principal: Principal) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.AlertEvent)
            .where(
                models.AlertEvent.organization_id == principal.organization_id,
                models.AlertEvent.workspace_id == principal.workspace_id,
            )
            .order_by(models.AlertEvent.created_at.desc())
            .limit(500)
        )
        return [
            {
                "id": row.id,
                "rule_id": row.rule_id,
                "event_type": row.event_type,
                "source_message_id": row.source_message_id,
                "document": row.document,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    def evaluate(self, message) -> int:
        rules = list(
            self.session.scalars(
                select(models.AlertRule).where(
                    models.AlertRule.organization_id == message.organization_id,
                    models.AlertRule.workspace_id == message.workspace_id,
                    models.AlertRule.enabled.is_(True),
                )
            )
        )
        created = 0
        for rule in rules:
            if self.session.scalar(
                select(models.AlertEvent.id).where(
                    models.AlertEvent.organization_id == message.organization_id,
                    models.AlertEvent.rule_id == rule.id,
                    models.AlertEvent.source_message_id == message.id,
                )
            ):
                continue
            value, dedupe_key = self._value(rule, message)
            if value is None:
                continue
            operator = str(rule.threshold.get("operator", "gte"))
            threshold = float(rule.threshold.get("value", 1))
            firing = (
                value >= threshold
                if operator == "gte"
                else value > threshold
                if operator == "gt"
                else value <= threshold
                if operator == "lte"
                else value < threshold
            )
            instance = self.session.scalar(
                select(models.AlertInstance).where(
                    models.AlertInstance.organization_id == message.organization_id,
                    models.AlertInstance.rule_id == rule.id,
                    models.AlertInstance.dedupe_key == dedupe_key,
                )
            )
            now = models.utc_now()
            if instance is None:
                instance = models.AlertInstance(
                    organization_id=message.organization_id,
                    workspace_id=message.workspace_id,
                    rule_id=rule.id,
                    dedupe_key=dedupe_key,
                    state="resolved",
                    last_source_id=message.id,
                )
                self.session.add(instance)
                self.session.flush()
            event_type = None
            if firing:
                last_fired = instance.last_fired_at
                if last_fired is not None and last_fired.tzinfo is None:
                    last_fired = last_fired.replace(tzinfo=timezone.utc)
                cooldown_elapsed = last_fired is None or now - last_fired >= timedelta(
                    seconds=rule.cooldown_seconds
                )
                if instance.state != "firing" or cooldown_elapsed:
                    event_type = "fired"
                    instance.last_fired_at = now
                instance.state = "firing"
                instance.resolved_at = None
            elif instance.state == "firing":
                event_type = "resolved"
                instance.state = "resolved"
                instance.resolved_at = now
            instance.last_value = value
            instance.last_source_id = message.id
            if event_type:
                self.session.add(
                    models.AlertEvent(
                        organization_id=message.organization_id,
                        workspace_id=message.workspace_id,
                        rule_id=rule.id,
                        instance_id=instance.id,
                        source_message_id=message.id,
                        event_type=event_type,
                        document={
                            "value": value,
                            "threshold": rule.threshold,
                            "dedupe_key": dedupe_key,
                            "delivery": {"type": "test_webhook", "status": "recorded_not_sent"},
                        },
                    )
                )
                created += 1
        self.session.commit()
        return created

    def _value(self, rule: models.AlertRule, message) -> tuple[float | None, str]:
        payload = message.payload
        event = payload.get("event", {})
        values = {**event.get("attributes", {}), **event.get("body", {})}
        run_id = str(payload.get("run_id") or "fleet")
        kind = rule.rule_type
        for key, expected in rule.filter_ast.items():
            if values.get(key, event.get(key)) != expected:
                return None, run_id
        if kind == "spend":
            return _numeric(values, "cost_usd", "total_cost_usd"), run_id
        if kind == "failure_rate":
            return self._failure_rate(rule, message), "workspace"
        if kind == "latency":
            return _numeric(values, "duration_ms", "latency_ms"), run_id
        if kind == "loop_signature":
            return (1.0 if values.get("loop_signature") else 0.0), str(
                values.get("loop_signature") or run_id
            )
        if kind == "provider_health":
            provider = str(values.get("provider") or values.get("provider_name") or "unknown")
            return self._failure_rate(rule, message, provider), provider
        if kind == "verifier_disagreement":
            return (1.0 if values.get("verifier_disagreement") is True else 0.0), run_id
        if kind == "policy_drift":
            return (1.0 if values.get("policy_drift") is True else 0.0), str(
                values.get("policy_version") or run_id
            )
        if kind == "suspicious_tools":
            return (
                1.0
                if values.get("suspicious_tool") or event.get("name") == "suspicious_tool"
                else 0.0
            ), str(values.get("tool") or run_id)
        if kind == "spool_backlog":
            return float(
                self.session.query(models.Outbox)
                .filter(
                    models.Outbox.organization_id == message.organization_id,
                    models.Outbox.published_at.is_(None),
                )
                .count()
            ), "workspace"
        if kind == "worker_capacity":
            return _numeric(values, "available_capacity", "capacity"), str(
                values.get("worker_id") or "workspace"
            )
        return None, run_id

    def _failure_rate(
        self, rule: models.AlertRule, message, provider: str | None = None
    ) -> float | None:
        window_seconds = int(rule.threshold.get("window_seconds", 3600))
        query = select(models.Run).where(
            models.Run.organization_id == message.organization_id,
            models.Run.workspace_id == message.workspace_id,
            models.Run.last_observed_at >= models.utc_now() - timedelta(seconds=window_seconds),
            models.Run.status.in_(["completed", "failed", "exhausted", "accepted"]),
        )
        if provider and provider != "unknown":
            query = query.where(models.Run.provider_name == provider)
        rows = list(self.session.scalars(query))
        return sum(run.status == "failed" for run in rows) / len(rows) if rows else None


def _numeric(values: dict[str, Any], *keys: str) -> float | None:
    value = next((values.get(key) for key in keys if values.get(key) is not None), None)
    return float(value) if isinstance(value, (int, float)) else None
