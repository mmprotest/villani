from __future__ import annotations

import io
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse

from ..config import get_settings
from ..live import broker, encode_sse
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
    IngestionService,
    NaturalLanguageInterrogationService,
    OperationsService,
    OutcomeLedgerService,
    PolicyPublicationService,
    RemoteDispatchService,
    RunQueryService,
)
from ..services.interrogation import InterrogationRequest, semantic_catalog
from .dependencies import ObjectStoreDependency, PrincipalDependency, SessionDependency

router = APIRouter()


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
) -> dict[str, Any]:
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
) -> Response:
    media_type, body = FleetObservabilityService(session).export(request, principal)
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
) -> dict[str, Any]:
    return RemoteDispatchService(session).submit(request, principal)


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
) -> dict[str, Any]:
    return PolicyPublicationService(session).create(request, principal)


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
) -> dict[str, Any]:
    return PolicyPublicationService(session).approve(publication_id, request.evidence, principal)


@router.post("/v1/policy-publications/{publication_id}/transition")
def transition_policy_publication(
    publication_id: str,
    request: PolicyPublicationTransitionRequest,
    principal: PrincipalDependency,
    session: SessionDependency,
) -> dict[str, Any]:
    return PolicyPublicationService(session).transition(
        publication_id, request.state, request.reason, principal
    )


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
) -> dict[str, Any]:
    return PolicyPublicationService(session).emergency_disable(
        request.disabled, request.reason, principal
    )


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
