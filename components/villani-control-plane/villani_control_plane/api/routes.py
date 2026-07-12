from __future__ import annotations

import io
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select

from ..config import get_settings
from ..live import broker, encode_sse
from ..metrics import metrics
from ..models import AdministrativeAuditEvent, RemoteTask, Run, RunCommitment
from ..schemas import (
    AlertRuleRequest,
    ArtifactDescriptorRequest,
    ArtifactPage,
    EnrollmentRequest,
    EventPage,
    FeedbackRequest,
    FleetExportRequest,
    FleetRunPage,
    FleetSearchRequest,
    GitOutcomeWebhook,
    IngestBatchRequest,
    InterrogationRequestModel,
    MetricRequest,
    OutcomeLedgerRequest,
    PolicyCanaryEvaluationRequest,
    PolicyEmergencyDisableRequest,
    PolicyPublicationApprovalRequest,
    PolicyPublicationCreateRequest,
    PolicyPublicationTransitionRequest,
    RemoteTaskRequest,
    ReviewQueueRequest,
    RunDetail,
    RunList,
    SavedViewRequest,
    ShadowRoutingObservationRequest,
    SpanPage,
    TaskCancellationRequest,
    TaskCompletionRequest,
    WorkerHeartbeatRequest,
)
from ..services import (
    AlertService,
    ArtifactTransferService,
    EnrollmentService,
    FleetObservabilityService,
    GovernanceService,
    IngestionService,
    NaturalLanguageInterrogationService,
    OperationsService,
    OutcomeLedgerService,
    PolicyPublicationService,
    QuotaService,
    RemoteDispatchService,
    RunQueryService,
)
from ..services.identity import (
    AuditService,
    FakeSAMLProvider,
    FakeSCIMProvider,
    IdentityAdministrationService,
    IdentityService,
)
from ..services.interrogation import InterrogationRequest, semantic_catalog
from ..tamper import digest_body, verify_audit_events
from .dependencies import (
    ObjectStoreDependency,
    PrincipalDependency,
    SessionDependency,
    authorize_request,
)

router = APIRouter(dependencies=[Depends(authorize_request)])


def _request_context(request: Request) -> tuple[str | None, str]:
    return (
        request.client.host if request.client else None,
        request.headers.get("X-Request-ID", "unassigned"),
    )


def _audit_route(
    session,
    principal,
    request: Request,
    action: str,
    target_type: str,
    target_id: str,
) -> None:
    source_ip, request_id = _request_context(request)
    AuditService(session).record(
        actor_id=principal.actor_id,
        actor_type=principal.principal_type,
        organization_id=principal.organization_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result="success",
        request_id=request_id,
        source_ip=source_ip,
    )
    session.commit()


@router.get("/liveness")
def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/metrics")
def structured_metrics() -> dict[str, Any]:
    return {"metrics": metrics.snapshot()}


@router.get("/v1/admin/governance/policies")
def get_governance_policy(
    principal: PrincipalDependency,
    session: SessionDependency,
    project_id: str | None = None,
) -> dict[str, Any]:
    policy = GovernanceService(session).resolve(
        principal.organization_id, principal.workspace_id, project_id
    )
    return {
        "policy": None
        if policy is None
        else {
            "id": policy.id,
            "version": policy.version,
            "retention_days": policy.retention_days,
            "metadata_only": policy.metadata_only,
            "exclusions": policy.exclusions,
            "redaction_rules": policy.redaction_rules,
            "dlp_hook": policy.dlp_hook,
            "allowed_regions": policy.allowed_regions,
            "required_residency_labels": policy.required_residency_labels,
        }
    }


@router.post("/v1/admin/governance/policies", status_code=status.HTTP_201_CREATED)
def create_governance_policy(
    payload: dict[str, Any],
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    policy = GovernanceService(session).create_policy(
        principal,
        workspace_id=payload.get("workspace_id"),
        project_id=payload.get("project_id"),
        retention_days=dict(payload.get("retention_days", {})),
        metadata_only=bool(payload.get("metadata_only", False)),
        exclusions=list(payload.get("exclusions", [])),
        redaction_rules=dict(payload.get("redaction_rules", {})),
        dlp_hook=str(payload.get("dlp_hook", "builtin")),
        allowed_regions=list(payload.get("allowed_regions", [])),
        required_residency_labels=list(payload.get("required_residency_labels", [])),
    )
    _audit_route(
        session, principal, http_request, "retention.policy.create", "governance_policy", policy.id
    )
    return {"id": policy.id, "version": policy.version}


@router.post("/v1/admin/governance/legal-holds", status_code=status.HTTP_201_CREATED)
def create_legal_hold(
    payload: dict[str, Any],
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    hold = GovernanceService(session).place_hold(
        principal,
        str(payload.get("target_type", "")),
        str(payload.get("target_id", "")),
        str(payload.get("reason", "")),
    )
    _audit_route(
        session, principal, http_request, "retention.legal_hold.create", "legal_hold", hold.id
    )
    return {"id": hold.id, "active": hold.active}


@router.post("/v1/admin/governance/retention/sweep")
def sweep_governance_retention(
    principal: PrincipalDependency,
    session: SessionDependency,
    store: ObjectStoreDependency,
    http_request: Request,
) -> dict[str, int]:
    result = GovernanceService(session).sweep_retention(principal, store)
    _audit_route(
        session, principal, http_request, "retention.sweep", "workspace", principal.workspace_id
    )
    return result


@router.post("/v1/admin/governance/deletions", status_code=status.HTTP_202_ACCEPTED)
def request_governance_deletion(
    payload: dict[str, Any],
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    workflow = GovernanceService(session).request_deletion(
        principal, str(payload.get("target_type", "")), str(payload.get("target_id", ""))
    )
    _audit_route(
        session, principal, http_request, "deletion.request", "deletion_workflow", workflow.id
    )
    return {"id": workflow.id, "state": workflow.state, "tombstone": workflow.tombstone}


@router.post("/v1/admin/governance/deletions/{workflow_id}/complete")
def complete_governance_deletion(
    workflow_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
    store: ObjectStoreDependency,
    http_request: Request,
) -> dict[str, Any]:
    workflow = GovernanceService(session).complete_deletion(principal, workflow_id, store)
    _audit_route(
        session, principal, http_request, "deletion.complete", "deletion_workflow", workflow.id
    )
    return {
        "id": workflow.id,
        "state": workflow.state,
        "completion_evidence": workflow.completion_evidence,
    }


@router.post("/v1/admin/governance/exports", status_code=status.HTTP_201_CREATED)
def create_governance_export(
    payload: dict[str, Any],
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    QuotaService(session).consume(
        principal,
        "exports",
        1,
        str(payload.get("request_id", "governance-export")),
        payload.get("project_id"),
    )
    record = GovernanceService(session).create_export(
        principal, payload.get("project_id"), list(payload.get("rows", []))
    )
    _audit_route(
        session, principal, http_request, "export.governance", "governance_export", record.id
    )
    return {"id": record.id, "digest_sha256": record.digest_sha256, "manifest": record.manifest}


@router.post("/v1/admin/quotas/policies", status_code=status.HTTP_201_CREATED)
def create_quota_policy(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    policy = QuotaService(session).create_policy(
        principal,
        {str(key): float(value) for key, value in dict(payload.get("limits", {})).items()},
        int(payload.get("soft_percent", 80)),
        payload.get("workspace_id"),
        payload.get("project_id"),
    )
    return {"id": policy.id, "limits": policy.limits, "soft_percent": policy.soft_percent}


@router.get("/v1/admin/usage/export")
def export_usage(principal: PrincipalDependency, session: SessionDependency) -> dict[str, Any]:
    return QuotaService(session).export_usage(principal)


@router.get("/v1/admin/tamper/audit/verify")
def verify_audit_chain(
    principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    events = list(
        session.scalars(
            select(AdministrativeAuditEvent)
            .where(AdministrativeAuditEvent.organization_id == principal.organization_id)
            .order_by(AdministrativeAuditEvent.occurred_at, AdministrativeAuditEvent.id)
        )
    )
    valid, head = verify_audit_events(events)
    result = {"valid": valid, "event_count": len(events), "head_sha256": head}
    return {**result, "commitment_sha256": digest_body(result)}


@router.post("/v1/auth/local/login")
def local_login(
    payload: dict[str, Any], request: Request, response: Response, session: SessionDependency
):
    source_ip, request_id = _request_context(request)
    token, csrf, record = IdentityService(session, get_settings()).local_login(
        str(payload.get("email", "")).lower(),
        str(payload.get("password", "")),
        str(payload.get("organization_id", "")),
        str(payload.get("workspace_id", "")),
        source_ip,
        request_id,
    )
    response.set_cookie(
        get_settings().session_cookie_name,
        token,
        httponly=True,
        secure=get_settings().secure_cookies,
        samesite="strict",
        max_age=get_settings().session_ttl_seconds,
        path="/",
    )
    return {"csrf_token": csrf, "expires_at": record.expires_at}


@router.post("/v1/auth/oidc/login")
def oidc_login(
    payload: dict[str, Any], request: Request, response: Response, session: SessionDependency
):
    source_ip, request_id = _request_context(request)
    token, csrf, record = IdentityService(session, get_settings()).oidc_login(
        str(payload.get("assertion", "")),
        str(payload.get("organization_id", "")),
        str(payload.get("workspace_id", "")),
        source_ip,
        request_id,
    )
    response.set_cookie(
        get_settings().session_cookie_name,
        token,
        httponly=True,
        secure=get_settings().secure_cookies,
        samesite="strict",
        max_age=get_settings().session_ttl_seconds,
        path="/",
    )
    return {"csrf_token": csrf, "expires_at": record.expires_at}


@router.post("/v1/auth/logout")
def logout(
    principal: PrincipalDependency, response: Response, session: SessionDependency
) -> dict[str, bool]:
    if principal.session_id:
        IdentityAdministrationService(session).revoke_session(principal, principal.session_id)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return {"revoked": True}


@router.get("/v1/admin/roles")
def list_roles(principal: PrincipalDependency, session: SessionDependency) -> dict[str, Any]:
    roles = IdentityAdministrationService(session).list_roles(principal)
    return {
        "roles": [
            {"id": r.id, "name": r.name, "built_in": r.built_in, "permissions": r.permissions}
            for r in roles
        ]
    }


@router.post("/v1/admin/roles", status_code=status.HTTP_201_CREATED)
def create_role(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    role = IdentityAdministrationService(session).create_custom_role(
        principal, str(payload.get("name", "")), list(payload.get("permissions", []))
    )
    return {"id": role.id, "name": role.name, "permissions": role.permissions}


@router.post("/v1/admin/users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    user = IdentityAdministrationService(session).create_user(
        principal,
        str(payload.get("email", "")),
        str(payload.get("display_name", "")),
        str(payload.get("provider", "local")),
        str(payload.get("issuer", "local")),
        str(payload.get("subject", payload.get("email", ""))),
        payload.get("password"),
    )
    return {"id": user.id, "email": user.email, "display_name": user.display_name}


@router.post("/v1/admin/memberships", status_code=status.HTTP_201_CREATED)
def create_membership(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    membership = IdentityAdministrationService(session).create_membership(
        principal,
        str(payload.get("user_id", "")),
        str(payload.get("role_id", "")),
        payload.get("workspace_id"),
    )
    return {"id": membership.id, "status": membership.status}


@router.post("/v1/admin/groups", status_code=status.HTTP_201_CREATED)
def create_group(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    group = IdentityAdministrationService(session).create_group(
        principal, str(payload.get("name", ""))
    )
    return {"id": group.id, "name": group.name}


@router.post("/v1/admin/groups/{group_id}/members", status_code=status.HTTP_201_CREATED)
def add_group_member(
    group_id: str,
    payload: dict[str, Any],
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, bool]:
    IdentityAdministrationService(session).add_group_member(
        principal, group_id, str(payload.get("membership_id", ""))
    )
    return {"created": True}


@router.post("/v1/admin/role-assignments", status_code=status.HTTP_201_CREATED)
def create_role_assignment(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    assignment = IdentityAdministrationService(session).assign_role(
        principal,
        str(payload.get("role_id", "")),
        str(payload.get("subject_type", "")),
        str(payload.get("subject_id", "")),
        payload.get("workspace_id"),
    )
    return {"id": assignment.id}


@router.post("/v1/admin/service-accounts", status_code=status.HTTP_201_CREATED)
def create_service_account(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    account = IdentityAdministrationService(session).create_service_account(
        principal, str(payload.get("name", "")), str(payload.get("role_id", ""))
    )
    return {"id": account.id, "name": account.name}


@router.post("/v1/admin/api-keys", status_code=status.HTTP_201_CREATED)
def create_api_key(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    key, plaintext = IdentityAdministrationService(session).create_api_key(
        principal,
        str(payload.get("name", "")),
        list(payload.get("scopes", [])),
        int(payload.get("expires_in_seconds", 3600)),
        payload.get("user_id"),
        payload.get("service_account_id"),
    )
    return {
        "id": key.id,
        "api_key": plaintext,
        "expires_at": key.expires_at,
        "displayed_once": True,
    }


@router.post("/v1/admin/api-keys/{key_id}/rotate")
def rotate_api_key(
    key_id: str, principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    key, plaintext = IdentityAdministrationService(session).rotate_api_key(principal, key_id)
    return {
        "id": key.id,
        "api_key": plaintext,
        "expires_at": key.expires_at,
        "displayed_once": True,
    }


@router.post("/v1/admin/api-keys/{key_id}/revoke")
def revoke_api_key(
    key_id: str, principal: PrincipalDependency, session: SessionDependency
) -> dict[str, bool]:
    IdentityAdministrationService(session).revoke_api_key(principal, key_id)
    return {"revoked": True}


@router.post("/v1/admin/sessions/{session_id}/revoke")
def revoke_session(
    session_id: str, principal: PrincipalDependency, session: SessionDependency
) -> dict[str, bool]:
    IdentityAdministrationService(session).revoke_session(principal, session_id)
    return {"revoked": True}


@router.post("/v1/admin/invitations", status_code=status.HTTP_201_CREATED)
def create_invitation(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    invitation, plaintext = IdentityAdministrationService(session).invite(
        principal,
        str(payload.get("email", "")),
        list(payload.get("role_ids", [])),
        int(payload.get("expires_in_seconds", 604800)),
    )
    return {
        "id": invitation.id,
        "invitation_token": plaintext,
        "expires_at": invitation.expires_at,
        "displayed_once": True,
    }


@router.get("/v1/admin/audit-events")
def audit_events(
    principal: PrincipalDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    events = IdentityAdministrationService(session).audit_events(principal, limit)
    return {
        "events": [
            {
                "id": e.id,
                "actor": e.actor_id,
                "organization_id": e.organization_id,
                "action": e.action,
                "target": {"type": e.target_type, "id": e.target_id},
                "result": e.result,
                "request_id": e.request_id,
                "source_ip_classification": e.source_ip_classification,
                "timestamp": e.occurred_at,
                "before_digest": e.before_digest,
                "after_digest": e.after_digest,
            }
            for e in events
        ]
    }


@router.get("/v1/admin/federation/saml")
def saml_interface(principal: PrincipalDependency, session: SessionDependency) -> dict[str, str]:
    del principal, session
    return FakeSAMLProvider().metadata()


@router.post("/v1/admin/federation/scim/sync")
def scim_sync(
    payload: dict[str, Any], principal: PrincipalDependency, session: SessionDependency
) -> dict[str, int]:
    del session
    return FakeSCIMProvider().synchronize(
        principal.organization_id, list(payload.get("records", []))
    )


@router.get("/v1/interrogation/catalog")
def interrogation_catalog(
    principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    del principal, session
    if not get_settings().natural_language_query_enabled:
        from ..errors import NotFoundError

        raise NotFoundError("natural-language interrogation is disabled")
    return semantic_catalog()


@router.post("/v1/interrogation/query")
def interrogate_runs(
    request: InterrogationRequestModel,
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    _, request_id = _request_context(http_request)
    QuotaService(session).consume(
        principal, "queries", 1, f"query:{request_id if request_id != 'unassigned' else uuid4()}"
    )
    return NaturalLanguageInterrogationService(session).ask(
        InterrogationRequest(request.question, request.conversation_id), principal
    )


@router.post("/v1/fleet/runs/search", response_model=FleetRunPage)
def fleet_run_search(
    request: FleetSearchRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> FleetRunPage:
    return FleetObservabilityService(session).search(request, principal)


@router.get("/v1/fleet/metrics/definitions")
def fleet_metric_definitions(
    principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    del principal
    return FleetObservabilityService(session).metric_definitions()


@router.post("/v1/fleet/metrics")
def fleet_metrics(
    request: MetricRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return FleetObservabilityService(session).metrics(request, principal)


@router.post("/v1/fleet/saved-views", status_code=status.HTTP_201_CREATED)
def create_saved_view(
    request: SavedViewRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return FleetObservabilityService(session).create_view(request, principal)


@router.get("/v1/fleet/saved-views")
def list_saved_views(principal: PrincipalDependency, session: SessionDependency) -> dict[str, Any]:
    return {"views": FleetObservabilityService(session).list_views(principal)}


@router.put("/v1/fleet/saved-views/{view_id}")
def update_saved_view(
    view_id: str,
    request: SavedViewRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return FleetObservabilityService(session).update_view(view_id, request, principal)


@router.post("/v1/fleet/alerts", status_code=status.HTTP_201_CREATED)
def create_alert_rule(
    request: AlertRuleRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return AlertService(session).create(request, principal)


@router.get("/v1/fleet/alerts")
def list_alert_rules(principal: PrincipalDependency, session: SessionDependency) -> dict[str, Any]:
    service = AlertService(session)
    return {"rules": service.list(principal), "events": service.events(principal)}


@router.post("/v1/fleet/runs/{run_id}/feedback", status_code=status.HTTP_201_CREATED)
def create_run_feedback(
    run_id: str,
    request: FeedbackRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return FleetObservabilityService(session).create_feedback(run_id, request, principal)


@router.get("/v1/fleet/runs/{run_id}/feedback")
def list_run_feedback(
    run_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return {"feedback": FleetObservabilityService(session).feedback(run_id, principal)}


@router.post("/v1/fleet/review-queue", status_code=status.HTTP_201_CREATED)
def enqueue_human_review(
    request: ReviewQueueRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return FleetObservabilityService(session).enqueue_review(request, principal)


@router.get("/v1/fleet/review-queue")
def list_human_review_queue(
    principal: PrincipalDependency,
    session: SessionDependency,
    queue: str | None = None,
) -> dict[str, Any]:
    return {"items": FleetObservabilityService(session).review_queue(principal, queue)}


@router.get("/v1/fleet/failure-clusters")
def list_failure_clusters(
    principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    return {"clusters": FleetObservabilityService(session).clusters(principal)}


@router.post("/v1/fleet/export")
def export_fleet_runs(
    request: FleetExportRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> Response:
    _, request_id = _request_context(http_request)
    QuotaService(session).consume(
        principal,
        "exports",
        1,
        f"fleet-export:{request_id if request_id != 'unassigned' else uuid4()}",
    )
    media_type, body = FleetObservabilityService(session).export(request, principal)
    _audit_route(session, principal, http_request, "export.create", "fleet", principal.workspace_id)
    extension = "csv" if request.format == "csv" else "json"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="villani-fleet.{extension}"'},
    )


@router.put("/v1/workers/{worker_id}/heartbeat")
def worker_heartbeat(
    worker_id: str,
    request: WorkerHeartbeatRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    QuotaService(session).consume(principal, "workers", 1, f"worker:{worker_id}")
    return RemoteDispatchService(session).heartbeat(
        worker_id,
        request.capabilities.model_dump(mode="json"),
        request.status,
        principal,
    )


@router.post("/v1/workers/{worker_id}/tasks/claim")
def claim_remote_task(
    worker_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return {"task": RemoteDispatchService(session).claim(worker_id, principal).task}


@router.post("/v1/tasks", status_code=status.HTTP_201_CREATED)
def submit_remote_task(
    request: RemoteTaskRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    run = session.get(Run, (principal.organization_id, request.run_id))
    project_id = run.project_id if run else None
    active = (
        session.scalar(
            select(func.count())
            .select_from(RemoteTask)
            .where(
                RemoteTask.organization_id == principal.organization_id,
                RemoteTask.workspace_id == principal.workspace_id,
                RemoteTask.state.in_(["queued", "leased", "cancellation_requested"]),
            )
        )
        or 0
    )
    QuotaService(session).enforce_current(principal, "concurrency", float(active), project_id)
    result = RemoteDispatchService(session).submit(request, principal)
    _audit_route(
        session,
        principal,
        http_request,
        "deployment.task.submit",
        "task",
        str(result.get("id", "submitted")),
    )
    return result


@router.post("/v1/tasks/{task_id}/cancel")
def cancel_remote_task(
    task_id: str,
    request: TaskCancellationRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return RemoteDispatchService(session).cancel(task_id, request.reason, principal)


@router.post("/v1/tasks/{task_id}/leases/{lease_id}/renew")
def renew_remote_task_lease(
    task_id: str,
    lease_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return RemoteDispatchService(session).renew(task_id, lease_id, principal)


@router.post("/v1/tasks/{task_id}/leases/{lease_id}/complete")
def complete_remote_task(
    task_id: str,
    lease_id: str,
    request: TaskCompletionRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return RemoteDispatchService(session).complete(task_id, lease_id, request, principal)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/build-version")
def build_version() -> dict[str, str]:
    return {"version": get_settings().build_version}


@router.get("/migration-state")
def migration_state(session: SessionDependency) -> dict[str, str | bool | None]:
    return OperationsService(session, get_settings()).migration_state()


@router.get("/readiness")
def readiness(response: Response, session: SessionDependency) -> dict[str, str | bool | None]:
    result = OperationsService(session, get_settings()).readiness()
    if not result["up_to_date"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result


@router.post("/v1/ingest/batches")
def ingest_batch(
    request: IngestBatchRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    result = IngestionService(session).ingest_batch(request.batch_id, request.events, principal)
    return {
        "batch_id": result.batch_id,
        "inserted": result.inserted,
        "duplicates": result.duplicates,
        "replayed": result.replayed,
    }


@router.post("/v1/artifacts/descriptors", status_code=status.HTTP_201_CREATED)
def artifact_descriptor(
    request: ArtifactDescriptorRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
    store: ObjectStoreDependency,
    http_request: Request,
) -> dict[str, Any]:
    result = ArtifactTransferService(session, store, get_settings()).register(
        request.run_id, request.descriptor, principal, str(http_request.base_url).rstrip("/")
    )
    return {
        "descriptor": result.descriptor,
        "status": result.status,
        "upload_id": result.upload_id,
        "upload_instruction": (
            {
                "method": result.upload_instruction.method,
                "url": result.upload_instruction.url,
                "headers": result.upload_instruction.headers,
                "expires_at": result.upload_instruction.expires_at,
            }
            if result.upload_instruction
            else None
        ),
    }


@router.put("/v1/artifact-uploads/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_artifact(
    upload_id: str,
    request: Request,
    session: SessionDependency,
    store: ObjectStoreDependency,
    upload_token: Annotated[str | None, Header(alias="X-Villani-Upload-Token")] = None,
) -> Response:
    if not upload_token:
        from ..errors import AuthenticationError

        raise AuthenticationError("upload token required")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > get_settings().max_artifact_size_bytes:
            from ..errors import ServiceError

            raise ServiceError("artifact upload exceeds configured maximum size")
        chunks.append(chunk)
    body = b"".join(chunks)
    ArtifactTransferService(session, store, get_settings()).accept_filesystem_upload(
        upload_id, upload_token, io.BytesIO(body)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/v1/artifact-uploads/{upload_id}/complete")
def complete_artifact(
    upload_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
    store: ObjectStoreDependency,
) -> dict[str, str]:
    return ArtifactTransferService(session, store, get_settings()).complete(upload_id, principal)


@router.get("/v1/artifacts/{artifact_id}/content")
def download_artifact(
    artifact_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
    store: ObjectStoreDependency,
):
    from ..errors import NotFoundError
    from ..models import Artifact

    artifact = session.get(Artifact, (principal.organization_id, artifact_id))
    if (
        artifact is None
        or artifact.workspace_id != principal.workspace_id
        or artifact.status != "available"
        or artifact.document.get("sensitivity") == "secret"
    ):
        raise NotFoundError("artifact not found")
    redirect = store.presign_download(artifact.object_key, 300)
    if redirect:
        return RedirectResponse(redirect, status_code=307)
    return StreamingResponse(
        store.open(artifact.object_key), media_type=artifact.document["media_type"]
    )


@router.post("/v1/installations/enroll")
def enroll_installation(request: EnrollmentRequest, session: SessionDependency) -> dict[str, str]:
    return EnrollmentService(session).enroll(
        request.enrollment_token,
        request.installation_id,
        request.agent_name,
        request.agent_version,
    )


@router.post("/v1/installations/{installation_id}/credentials/rotate")
def rotate_installation(
    installation_id: str, principal: PrincipalDependency, session: SessionDependency
) -> dict[str, str | int]:
    return EnrollmentService(session).rotate(installation_id, principal)


@router.get("/v1/runs/{run_id}/stream")
async def stream_run_updates(
    run_id: str, principal: PrincipalDependency, session: SessionDependency
):
    run = RunQueryService(session).repository.get_run(principal.organization_id, run_id)
    if run is None or run.workspace_id != principal.workspace_id:
        from ..errors import NotFoundError

        raise NotFoundError("run not found")
    subscription = broker.subscribe(principal.organization_id, principal.workspace_id, run_id)

    async def generate():
        try:
            while True:
                message = await subscription.queue.get()
                if message is None:
                    yield "event: backpressure\ndata: {}\n\n"
                    return
                yield encode_sse(message)
        finally:
            broker.unsubscribe(subscription)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/v1/outcomes", status_code=status.HTTP_201_CREATED)
def outcome(
    document: dict[str, Any],
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    if document.get("cost") is not None:
        run = session.get(Run, (principal.organization_id, str(document.get("run_id", ""))))
        QuotaService(session).consume(
            principal,
            "model_cost",
            float(document["cost"]),
            f"outcome:{document.get('run_id')}:{document.get('attempt_id') or 'run'}",
            run.project_id if run else None,
        )
    return {"outcome": IngestionService(session).record_outcome(document, principal)}


@router.post("/v1/outcome-ledger/outcomes", status_code=status.HTTP_201_CREATED)
def outcome_ledger_record(
    request: OutcomeLedgerRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return OutcomeLedgerService(session).record_v2(
        request.outcome,
        principal,
        provenance=request.provenance,
        confidence=request.confidence,
        corrects_version=request.corrects_version,
    )


@router.post("/v1/outcome-ledger/git-webhooks", status_code=status.HTTP_201_CREATED)
def outcome_ledger_webhook(
    request: GitOutcomeWebhook,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return {"signals": OutcomeLedgerService(session).ingest_webhook(request, principal)}


@router.get("/v1/outcome-ledger/runs/{run_id}")
def outcome_ledger_run(
    run_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return OutcomeLedgerService(session).ledger(run_id, principal)


@router.post("/v1/shadow-routing/observations", status_code=status.HTTP_201_CREATED)
def shadow_routing_observation(
    request: ShadowRoutingObservationRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return OutcomeLedgerService(session).record_shadow(request, principal)


@router.get("/v1/shadow-routing/metrics")
def shadow_routing_metrics(
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return OutcomeLedgerService(session).metrics(principal)


@router.post("/v1/policy-publications", status_code=status.HTTP_201_CREATED)
def create_policy_publication(
    request: PolicyPublicationCreateRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    result = PolicyPublicationService(session).create(request, principal)
    _audit_route(
        session,
        principal,
        http_request,
        "policy.create",
        "policy_publication",
        str(result.get("id", "created")),
    )
    return result


@router.get("/v1/policy-publications/{publication_id}")
def get_policy_publication(
    publication_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return PolicyPublicationService(session).get(publication_id, principal)


@router.post("/v1/policy-publications/{publication_id}/approve")
def approve_policy_publication(
    publication_id: str,
    request: PolicyPublicationApprovalRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    result = PolicyPublicationService(session).approve(publication_id, request.evidence, principal)
    _audit_route(
        session, principal, http_request, "policy.approve", "policy_publication", publication_id
    )
    return result


@router.post("/v1/policy-publications/{publication_id}/transition")
def transition_policy_publication(
    publication_id: str,
    request: PolicyPublicationTransitionRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    result = PolicyPublicationService(session).transition(
        publication_id, request.state, request.reason, principal
    )
    _audit_route(
        session,
        principal,
        http_request,
        "deployment.policy.transition",
        "policy_publication",
        publication_id,
    )
    return result


@router.post("/v1/policy-publications/{publication_id}/evaluate-canary")
def evaluate_policy_canary(
    publication_id: str,
    request: PolicyCanaryEvaluationRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return PolicyPublicationService(session).evaluate_canary(
        publication_id, request.model_dump(mode="python"), principal
    )


@router.post("/v1/policy-publications/emergency-disable")
def emergency_policy_disable(
    request: PolicyEmergencyDisableRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
    http_request: Request,
) -> dict[str, Any]:
    result = PolicyPublicationService(session).emergency_disable(
        request.disabled, request.reason, principal
    )
    _audit_route(
        session,
        principal,
        http_request,
        "policy.emergency_disable",
        "workspace",
        principal.workspace_id,
    )
    return result


@router.get("/v1/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, principal: PrincipalDependency, session: SessionDependency) -> RunDetail:
    return RunQueryService(session).get_run(run_id, principal)


@router.get("/v1/runs/{run_id}/events", response_model=EventPage)
def get_run_events(
    run_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> EventPage:
    return RunQueryService(session).events(run_id, principal, cursor=cursor, limit=limit)


@router.get("/v1/runs/{run_id}/spans", response_model=SpanPage)
def get_run_spans(
    run_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 250,
) -> SpanPage:
    return RunQueryService(session).spans(run_id, principal, cursor=cursor, limit=limit)


@router.get("/v1/runs/{run_id}/artifacts", response_model=ArtifactPage)
def get_run_artifacts(
    run_id: str,
    principal: PrincipalDependency,
    session: SessionDependency,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
) -> ArtifactPage:
    return RunQueryService(session).artifacts(run_id, principal, cursor=cursor, limit=limit)


@router.get("/v1/runs/{run_id}/commitment")
def get_run_commitment(
    run_id: str, principal: PrincipalDependency, session: SessionDependency
) -> dict[str, Any]:
    run = session.get(Run, (principal.organization_id, run_id))
    commitment = session.get(RunCommitment, (principal.organization_id, run_id))
    if run is None or run.workspace_id != principal.workspace_id or commitment is None:
        from ..errors import NotFoundError

        raise NotFoundError("run commitment not found")
    result = {
        "run_id": run_id,
        "root_sha256": commitment.root_sha256,
        "item_count": commitment.item_count,
        "finalized_at": commitment.finalized_at,
        "correction_of_root": commitment.correction_of_root,
    }
    return {**result, "commitment_sha256": digest_body(result)}


@router.get("/v1/runs", response_model=RunList)
def list_runs(
    principal: PrincipalDependency,
    session: SessionDependency,
    project_id: str | None = None,
    repository_id: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> RunList:
    return RunQueryService(session).list_runs(
        principal,
        project_id=project_id,
        repository_id=repository_id,
        status=status_filter,
        started_after=started_after,
        started_before=started_before,
        limit=limit,
    )
