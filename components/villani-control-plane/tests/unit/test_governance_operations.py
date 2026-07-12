from __future__ import annotations

import json
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import select

from villani_control_plane.backup import backup_sqlite, restore_sqlite
from villani_control_plane.config import Settings
from villani_control_plane.encryption import (
    DevelopmentKeyProvider,
    EnvelopeEncryptionService,
    FakeKMSProvider,
    record_key_rotation,
)
from villani_control_plane.errors import AuthorizationError, ConflictError, RateLimitError
from villani_control_plane.metrics import FakeOTLPExporter, StructuredMetrics
from villani_control_plane.models import (
    AdministrativeAuditEvent,
    Artifact,
    GovernancePolicy,
    QuotaPolicy,
    Run,
    utc_now,
)
from villani_control_plane.object_store import FilesystemObjectStore, create_object_store
from villani_control_plane.services.governance import (
    QUOTA_METRICS,
    GovernanceService,
    QuotaService,
)
from villani_control_plane.services.identity import AuditService
from villani_control_plane.tamper import verify_audit_events


def test_governance_precedence_metadata_exclusions_redaction_dlp_and_residency(session, principal):
    session.add(
        GovernancePolicy(
            organization_id=principal.organization_id,
            retention_days={"prompt": 30},
            exclusions=[],
            redaction_rules={},
            allowed_regions=["au-sydney"],
            required_residency_labels=["au"],
        )
    )
    session.add(
        GovernancePolicy(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            retention_days={"prompt": 7},
            metadata_only=True,
            exclusions=["response"],
            redaction_rules={"name": "masked"},
            dlp_hook="fake",
            allowed_regions=["au-sydney"],
            required_residency_labels=["au"],
        )
    )
    session.commit()
    service = GovernanceService(session)
    prompt = service.govern(
        "prompt",
        {"schema_version": "v", "run_id": "run", "name": "prompt", "body": "secret"},
        principal.organization_id,
        principal.workspace_id,
        "project_1",
    )
    assert prompt.policy_id
    assert prompt.document == {
        "schema_version": "v",
        "run_id": "run",
        "name": "masked",
        "dlp_checked": "prompt",
    }
    assert prompt.expires_at
    assert not service.govern(
        "response", {"body": "x"}, principal.organization_id, principal.workspace_id, "project_1"
    ).retained
    service.enforce_residency(
        principal.organization_id, principal.workspace_id, "project_1", "au-sydney", ["au"]
    )
    with pytest.raises(AuthorizationError):
        service.enforce_residency(
            principal.organization_id, principal.workspace_id, "project_1", "us-east", ["us"]
        )


def test_legal_hold_blocks_deletion_and_export_masks_secrets(session, principal):
    service = GovernanceService(session)
    service.place_hold(principal, "run", "run_1", "litigation")
    with pytest.raises(ConflictError, match="legal hold"):
        service.request_deletion(principal, "run", "run_1")
    exported = service.create_export(
        principal, "project_1", [{"run_id": "run_1", "api_key": "never-store-me"}]
    )
    assert exported.manifest["rows"][0]["api_key"] == "********"
    assert "never-store-me" not in json.dumps(exported.manifest)


def test_deletion_tombstone_completion_evidence_and_artifact_removal(
    session, principal, tmp_path: Path
):
    now = utc_now()
    run = Run(
        organization_id=principal.organization_id,
        workspace_id=principal.workspace_id,
        project_id="project_1",
        repository_id="repo_001",
        id="run-delete",
        trace_id="a" * 32,
        status="completed",
        first_occurred_at=now,
        first_observed_at=now,
        last_observed_at=now,
    )
    store = FilesystemObjectStore(tmp_path / "objects")
    key = "organizations/org_1/sha256/aa/aabb"
    store.put(key, BytesIO(b"data"), 4)
    session.add(run)
    session.flush()
    session.add(
        Artifact(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            id="artifact-delete",
            run_id=run.id,
            digest_sha256="a" * 64,
            size_bytes=4,
            status="available",
            object_key=key,
            document={"artifact_id": "artifact-delete"},
        )
    )
    session.commit()
    service = GovernanceService(session)
    workflow = service.request_deletion(principal, "run", run.id)
    completed = service.complete_deletion(principal, workflow.id, store)
    assert completed.state == "completed"
    assert completed.completion_evidence["deleted_artifacts"] == 1
    assert completed.completion_evidence["tombstone_sha256"]
    assert not store.exists(key)
    assert run.deleted_at is not None


def test_quota_precedence_soft_warning_hard_limit_and_chargeback_export(session, principal):
    assert QUOTA_METRICS == {
        "runs",
        "events",
        "artifact_bytes",
        "model_cost",
        "concurrency",
        "workers",
        "exports",
        "queries",
    }
    session.add(
        QuotaPolicy(
            organization_id=principal.organization_id,
            limits={"events": 100},
            soft_percent=80,
        )
    )
    session.add(
        QuotaPolicy(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            limits={"events": 10},
            soft_percent=80,
        )
    )
    session.commit()
    quota = QuotaService(session)
    assert quota.consume(
        principal, "events", 8, "batch-1", chargeback_tags={"team": "platform"}
    ).warning
    session.commit()
    with pytest.raises(RateLimitError, match="hard quota"):
        quota.consume(principal, "events", 3, "batch-2")
    session.rollback()
    exported = quota.export_usage(principal)
    assert exported["records"][0]["chargeback_tags"] == {"team": "platform"}


@pytest.mark.parametrize(
    "provider",
    [
        DevelopmentKeyProvider(b"development-key-material", "dev-v1"),
        FakeKMSProvider(b"fake-kms-key-material", "byok-v2", 2),
    ],
)
def test_envelope_encryption_key_interfaces_and_tamper_detection(provider):
    service = EnvelopeEncryptionService(provider)
    envelope = service.encrypt(b"governed payload", b"org-1")
    assert service.decrypt(envelope, b"org-1") == b"governed payload"
    altered = envelope.__class__(
        envelope.algorithm,
        envelope.nonce,
        envelope.ciphertext + b"x",
        envelope.authentication_tag,
        envelope.data_key,
    )
    with pytest.raises(ValueError, match="authentication"):
        service.decrypt(altered, b"org-1")


def test_key_rotation_metadata_retires_predecessor(session, principal):
    first = record_key_rotation(session, principal.organization_id, "development", "key-v1", 1)
    second = record_key_rotation(
        session, principal.organization_id, "fake-kms", "key-v2", 2, "key-v1"
    )
    assert first.retired_at is not None
    assert second.previous_key_id == "key-v1"


def test_audit_hash_chain_and_append_only_correction(session, principal):
    audit = AuditService(session)
    first = audit.record(
        actor_id=principal.actor_id,
        actor_type="user",
        organization_id=principal.organization_id,
        action="retention.change",
        target_type="policy",
        target_id="p1",
        result="success",
        request_id="r1",
        source_ip="127.0.0.1",
        after={"days": 30},
    )
    session.flush()
    second = audit.record(
        actor_id=principal.actor_id,
        actor_type="user",
        organization_id=principal.organization_id,
        action="retention.change",
        target_type="policy",
        target_id="p1",
        result="corrected",
        request_id="r2",
        source_ip="127.0.0.1",
        before={"days": 30},
        after={"days": 7},
        corrects_event_id=first.id,
    )
    session.commit()
    events = list(session.scalars(select(AdministrativeAuditEvent)))
    assert second.corrects_event_id == first.id
    assert verify_audit_events(reversed(events))[0]
    second.result = "rewritten"
    with pytest.raises(ValueError, match="immutable"):
        session.commit()


def test_backup_restore_integrity(tmp_path: Path):
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
        database.execute("INSERT INTO evidence(value) VALUES ('durable')")
        database.commit()
    backup = tmp_path / "backup.sqlite"
    restored = tmp_path / "restored.sqlite"
    manifest = backup_sqlite(source, backup)
    result = restore_sqlite(backup, restored)
    assert result["sha256"] == manifest["sha256"]
    with sqlite3.connect(restored) as database:
        assert database.execute("SELECT value FROM evidence").fetchone()[0] == "durable"


def test_structured_metrics_and_fake_otlp_export():
    exporter = FakeOTLPExporter()
    metrics = StructuredMetrics(exporter)
    metrics.add("ingest_events", 2, region="local")
    assert metrics.snapshot() == [
        {"name": "ingest_events", "value": 2.0, "labels": {"region": "local"}}
    ]
    assert exporter.exports


def test_air_gapped_mode_rejects_network_object_store():
    settings = Settings(
        air_gapped=True,
        object_store_backend="s3",
        s3_bucket="bucket",
    )
    with pytest.raises(ValueError, match="air-gapped"):
        create_object_store(settings)
