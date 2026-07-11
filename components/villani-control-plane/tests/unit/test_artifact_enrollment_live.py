from __future__ import annotations

import hashlib
import io
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import load_v2_fixture, seed_tenant
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from villani_control_plane import live as live_module
from villani_control_plane import models
from villani_control_plane.api.dependencies import object_store
from villani_control_plane.config import Settings
from villani_control_plane.database import Base, get_session
from villani_control_plane.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    RateLimitError,
)
from villani_control_plane.live import LiveBroker, LiveMessage
from villani_control_plane.main import create_app
from villani_control_plane.object_store import FilesystemObjectStore, S3ObjectStore
from villani_control_plane.security import hash_token, token_lookup_digest
from villani_control_plane.services import (
    AuthenticationService,
    EnrollmentService,
    IngestionService,
)
from villani_control_plane.services import ingestion as ingestion_module
from villani_control_plane.services.synchronization import ArtifactTransferService


def create_run(session, principal) -> None:
    event = load_v2_fixture("telemetry-envelope.json")
    IngestionService(session).ingest_batch("artifact_run", [event], principal)


def descriptor(content: bytes, *, artifact_id: str = "artifact_upload", sensitivity="internal"):
    value = load_v2_fixture("artifact-descriptor.json")
    value.update(
        artifact_id=artifact_id,
        digest={"algorithm": "sha256", "value": hashlib.sha256(content).hexdigest()},
        size_bytes=len(content),
        storage_reference=None,
        sensitivity=sensitivity,
    )
    return value


def test_artifact_upload_is_replayable_verified_and_content_addressed(
    session, principal, tmp_path
) -> None:
    create_run(session, principal)
    settings = Settings(database_url="sqlite://", object_store_path=tmp_path / "objects")
    store = FilesystemObjectStore(settings.object_store_path)
    service = ArtifactTransferService(session, store, settings)
    content = b"verified artifact bytes"
    first = service.register("run_001", descriptor(content), principal, "http://test")
    replay = service.register("run_001", descriptor(content), principal, "http://test")
    assert first.status == replay.status == "upload_required"
    assert replay.upload_instruction is not None
    token = replay.upload_instruction.headers["X-Villani-Upload-Token"]
    service.accept_filesystem_upload(replay.upload_id, token, io.BytesIO(content))
    assert service.complete(replay.upload_id, principal)["status"] == "available"
    assert service.complete(replay.upload_id, principal)["status"] == "available"
    artifact = session.get(models.Artifact, (principal.organization_id, "artifact_upload"))
    assert artifact.object_key.endswith(hashlib.sha256(content).hexdigest())
    assert store.open(artifact.object_key).read() == content


def test_digest_mismatch_never_becomes_available_and_prohibited_policy_precedes_upload(
    session, principal, tmp_path
) -> None:
    create_run(session, principal)
    settings = Settings(database_url="sqlite://", object_store_path=tmp_path / "objects")
    store = FilesystemObjectStore(settings.object_store_path)
    service = ArtifactTransferService(session, store, settings)
    content = b"right"
    registration = service.register("run_001", descriptor(content), principal, "http://test")
    token = registration.upload_instruction.headers["X-Villani-Upload-Token"]
    service.accept_filesystem_upload(registration.upload_id, token, io.BytesIO(b"wrong"))
    with pytest.raises(ConflictError):
        service.complete(registration.upload_id, principal)
    artifact = session.get(models.Artifact, (principal.organization_id, "artifact_upload"))
    assert artifact.status == "rejected"
    assert not store.exists(artifact.object_key)
    with pytest.raises(AuthorizationError):
        service.register(
            "run_001",
            descriptor(b"secret", artifact_id="secret", sensitivity="secret"),
            principal,
            "http://test",
        )
    assert session.get(models.Artifact, (principal.organization_id, "secret")) is None


def test_one_time_enrollment_and_rotation(session, principal) -> None:
    token = "one-time-enrollment-token-long-enough"
    session.add(
        models.EnrollmentToken(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            lookup_digest=token_lookup_digest(token),
            secret_hash=hash_token(token),
            expires_at=models.utc_now() + timedelta(hours=1),
        )
    )
    session.commit()
    enrolled = EnrollmentService(session).enroll(token, "install_1", "agentd", "1")
    assert enrolled["credential"] != token
    assert (
        AuthenticationService(session).authenticate(enrolled["credential"]).installation_id
        == "install_1"
    )
    with pytest.raises(AuthenticationError):
        EnrollmentService(session).enroll(token, "install_2", "agentd", "1")
    installation_principal = principal.__class__(
        "install_1", principal.organization_id, principal.workspace_id, "install_1"
    )
    rotated = EnrollmentService(session).rotate("install_1", installation_principal)
    assert rotated["credential"] != enrolled["credential"]
    assert rotated["credential_version"] == 2
    with pytest.raises(AuthenticationError):
        AuthenticationService(session).authenticate(enrolled["credential"])
    assert (
        AuthenticationService(session).authenticate(rotated["credential"]).installation_id
        == "install_1"
    )


@pytest.mark.asyncio
async def test_live_broker_enforces_tenant_run_scope_and_backpressure() -> None:
    live = LiveBroker(queue_size=1)
    allowed = live.subscribe("org", "workspace", "run")
    other = live.subscribe("other", "workspace", "run")
    await live.publish(LiveMessage("1", "org", "workspace", "event", {"run_id": "run"}))
    await live.publish(LiveMessage("1", "org", "workspace", "event", {"run_id": "run"}))
    assert (await allowed.queue.get()).id == "1"
    assert other.queue.empty()
    await live.publish(LiveMessage("2", "org", "workspace", "event", {"run_id": "run"}))
    await live.publish(LiveMessage("3", "org", "workspace", "event", {"run_id": "run"}))
    assert await allowed.queue.get() is None


def test_artifact_download_is_tenant_scoped(session, principal, tmp_path) -> None:
    create_run(session, principal)
    settings = Settings(database_url="sqlite://", object_store_path=tmp_path / "objects")
    store = FilesystemObjectStore(settings.object_store_path)
    transfer = ArtifactTransferService(session, store, settings)
    content = b"tenant artifact"
    registration = transfer.register("run_001", descriptor(content), principal, "http://test")
    transfer.accept_filesystem_upload(
        registration.upload_id,
        registration.upload_instruction.headers["X-Villani-Upload-Token"],
        io.BytesIO(content),
    )
    transfer.complete(registration.upload_id, principal)
    seed_tenant(
        session,
        organization_id="org_other",
        workspace_id="workspace_other",
        project_id="project_other",
        repository_id="repo_other",
        token="other-tenant-development-token-long-enough",
    )
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[object_store] = lambda: store
    client = TestClient(app)
    own = client.get(
        "/v1/artifacts/artifact_upload/content",
        headers={"Authorization": "Bearer unit-development-token-that-is-long-enough"},
    )
    assert own.status_code == 200 and own.content == content
    denied = client.get(
        "/v1/artifacts/artifact_upload/content",
        headers={"Authorization": "Bearer other-tenant-development-token-long-enough"},
    )
    assert denied.status_code == 404


def test_outbox_claim_sees_only_committed_rows(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'outbox.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(live_module, "SessionFactory", factory)
    with factory() as session:
        principal = seed_tenant(session)
        session.add(
            models.Outbox(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                topic="event",
                aggregate_type="event",
                aggregate_id="one",
                payload={"run_id": "run"},
            )
        )
        assert live_module.claim_outbox("worker") == []
        session.commit()
    assert [message.payload["run_id"] for message in live_module.claim_outbox("worker")] == ["run"]


def test_per_installation_batch_and_rate_limits_apply_before_persistence(
    session, principal, monkeypatch
) -> None:
    session.add(
        models.AgentInstallation(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            id="limited",
            agent_name="agentd",
            attributes={},
        )
    )
    session.commit()
    limited = principal.__class__(
        "limited", principal.organization_id, principal.workspace_id, "limited"
    )
    settings = Settings(
        database_url="sqlite://",
        max_installation_batch_events=1,
        max_installation_events_per_minute=1,
    )
    monkeypatch.setattr(ingestion_module, "get_settings", lambda: settings)
    first = load_v2_fixture("telemetry-envelope.json")
    second = dict(first) | {
        "event_id": "second",
        "idempotency_key": "second",
        "sequence": 2,
        "span_id": "2222222222222222",
    }
    with pytest.raises(RateLimitError):
        IngestionService(session).ingest_batch("too-large", [first, second], limited)
    assert IngestionService(session).ingest_batch("one", [first], limited).inserted == 1
    with pytest.raises(RateLimitError):
        IngestionService(session).ingest_batch("rate", [second], limited)


def test_s3_instruction_is_checksum_bound_and_create_only(monkeypatch) -> None:
    class FakeS3:
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            self.operation = operation
            self.params = Params
            return "https://objects.example/upload"

    fake = FakeS3()
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *a, **k: fake))
    store = S3ObjectStore("bucket", endpoint_url="https://objects.example", region="test")
    instruction = store.presign_upload("key", 3, hashlib.sha256(b"abc").hexdigest(), 60)
    assert fake.params["IfNoneMatch"] == "*"
    assert fake.params["ChecksumSHA256"] == instruction.headers["x-amz-checksum-sha256"]
