from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session
from villani_ops.closed_loop.protocol_v2 import ArtifactDescriptorV2
from villani_ops.closed_loop.schema_validation import (
    ProtocolValidationError,
    parse_protocol_document,
)

from .. import models
from ..config import Settings
from ..errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
)
from ..object_store import ObjectStore, UploadInstruction
from ..security import Principal, hash_token, token_lookup_digest, verify_token


@dataclass(frozen=True, slots=True)
class ArtifactRegistration:
    descriptor: dict[str, Any]
    status: str
    upload_id: str | None
    upload_instruction: UploadInstruction | None


def instant(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


class EnrollmentService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enroll(
        self, token: str, installation_id: str, name: str, version: str | None
    ) -> dict[str, str]:
        record = self.session.scalar(
            select(models.EnrollmentToken)
            .where(models.EnrollmentToken.lookup_digest == token_lookup_digest(token))
            .with_for_update()
        )
        now = models.utc_now()
        if (
            record is None
            or record.used_at is not None
            or instant(record.expires_at) <= instant(now)
            or not verify_token(token, record.secret_hash)
        ):
            raise AuthenticationError("invalid, expired, or used enrollment token")
        if self.session.get(models.AgentInstallation, (record.organization_id, installation_id)):
            raise ConflictError("installation_id already exists")
        credential = secrets.token_urlsafe(48)
        self.session.add(
            models.AgentInstallation(
                organization_id=record.organization_id,
                workspace_id=record.workspace_id,
                id=installation_id,
                agent_name=name,
                agent_version=version,
                credential_lookup_digest=token_lookup_digest(credential),
                credential_hash=hash_token(credential),
                credential_rotated_at=now,
                last_seen_at=now,
                attributes={},
            )
        )
        record.used_at = now
        self.session.commit()
        return {
            "installation_id": installation_id,
            "organization_id": record.organization_id,
            "workspace_id": record.workspace_id,
            "credential": credential,
        }

    def rotate(self, installation_id: str, principal: Principal) -> dict[str, str | int]:
        if principal.installation_id != installation_id:
            raise AuthorizationError("installation credential may rotate only itself")
        installation = self.session.get(
            models.AgentInstallation, (principal.organization_id, installation_id)
        )
        if installation is None:
            raise NotFoundError("installation not found")
        credential = secrets.token_urlsafe(48)
        installation.credential_lookup_digest = token_lookup_digest(credential)
        installation.credential_hash = hash_token(credential)
        installation.credential_version += 1
        installation.credential_rotated_at = models.utc_now()
        self.session.commit()
        return {"credential": credential, "credential_version": installation.credential_version}


class ArtifactTransferService:
    def __init__(self, session: Session, store: ObjectStore, settings: Settings) -> None:
        self.session = session
        self.store = store
        self.settings = settings

    def register(
        self, run_id: str, document: dict[str, Any], principal: Principal, base_url: str
    ) -> ArtifactRegistration:
        try:
            parsed = parse_protocol_document(document)
        except ProtocolValidationError as error:
            raise ServiceError(f"v2 schema validation failed: {error}") from error
        if not isinstance(parsed, ArtifactDescriptorV2):
            raise ServiceError("descriptor must use villani.artifact_descriptor.v2")
        if parsed.sensitivity not in self.settings.sensitivity_policy:
            raise AuthorizationError("artifact sensitivity is prohibited")
        if parsed.retention_class not in self.settings.retention_policy:
            raise AuthorizationError("artifact retention class is prohibited")
        if parsed.size_bytes > self.settings.max_artifact_size_bytes:
            raise ServiceError("artifact exceeds configured maximum size")
        run = self.session.get(models.Run, (principal.organization_id, run_id))
        if run is None or run.workspace_id != principal.workspace_id:
            raise NotFoundError("run not found")
        object_key = f"organizations/{principal.organization_id}/sha256/{parsed.digest.value[:2]}/{parsed.digest.value}"
        normalized = parsed.model_copy(
            update={"storage_reference": f"object://{object_key}"}
        ).model_dump(mode="json")
        existing = self.session.get(
            models.Artifact, (principal.organization_id, parsed.artifact_id)
        )
        if existing:
            if existing.document != normalized or existing.run_id != run_id:
                raise ConflictError("artifact_id already has different content")
            return self._instruction(existing, base_url)
        available = self.session.scalar(
            select(models.Artifact).where(
                models.Artifact.organization_id == principal.organization_id,
                models.Artifact.digest_sha256 == parsed.digest.value,
                models.Artifact.status == "available",
            )
        )
        if available is not None and not self.store.exists(available.object_key):
            available = None
        upload_id = None if available else models.new_id()
        upload_token = None if available else secrets.token_urlsafe(32)
        artifact = models.Artifact(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            id=parsed.artifact_id,
            run_id=run_id,
            digest_sha256=parsed.digest.value,
            size_bytes=parsed.size_bytes,
            document=normalized,
            status="available" if available else "pending",
            object_key=object_key,
            upload_id=upload_id,
            upload_token_hash=hash_token(upload_token) if upload_token else None,
            upload_expires_at=(
                models.utc_now() + timedelta(seconds=self.settings.artifact_upload_ttl_seconds)
                if upload_id
                else None
            ),
            available_at=models.utc_now() if available else None,
        )
        self.session.add(artifact)
        self.session.commit()
        registration = self._instruction(artifact, base_url, upload_token=upload_token)
        return registration

    def _instruction(
        self, artifact: models.Artifact, base_url: str, upload_token: str | None = None
    ) -> ArtifactRegistration:
        if artifact.status == "available":
            return ArtifactRegistration(artifact.document, "already_present", None, None)
        if artifact.upload_expires_at is None or instant(artifact.upload_expires_at) <= instant(
            models.utc_now()
        ):
            raise ConflictError("artifact upload instruction expired; register descriptor again")
        instruction = self.store.presign_upload(
            artifact.object_key,
            artifact.size_bytes,
            artifact.digest_sha256,
            self.settings.artifact_upload_ttl_seconds,
        )
        if instruction is None:
            if upload_token is None:
                upload_token = secrets.token_urlsafe(32)
                artifact.upload_token_hash = hash_token(upload_token)
                artifact.upload_expires_at = models.utc_now() + timedelta(
                    seconds=self.settings.artifact_upload_ttl_seconds
                )
                self.session.commit()
            instruction = UploadInstruction(
                "PUT",
                f"{base_url.rstrip('/')}/v1/artifact-uploads/{artifact.upload_id}",
                {
                    "X-Villani-Upload-Token": upload_token,
                    "Content-Length": str(artifact.size_bytes),
                },
                artifact.upload_expires_at.isoformat(),
            )
        return ArtifactRegistration(
            artifact.document, "upload_required", artifact.upload_id, instruction
        )

    def accept_filesystem_upload(self, upload_id: str, token: str, stream: BinaryIO) -> None:
        artifact = self.session.scalar(
            select(models.Artifact).where(models.Artifact.upload_id == upload_id)
        )
        if artifact is None or artifact.status != "pending":
            raise NotFoundError("upload not found")
        if artifact.upload_expires_at is None or instant(artifact.upload_expires_at) <= instant(
            models.utc_now()
        ):
            raise AuthenticationError("upload instruction expired")
        if not artifact.upload_token_hash or not verify_token(token, artifact.upload_token_hash):
            raise AuthenticationError("invalid upload token")
        self.store.put(artifact.object_key, stream, artifact.size_bytes)

    def complete(self, upload_id: str, principal: Principal) -> dict[str, str]:
        artifact = self.session.scalar(
            select(models.Artifact).where(
                models.Artifact.upload_id == upload_id,
                models.Artifact.organization_id == principal.organization_id,
                models.Artifact.workspace_id == principal.workspace_id,
            )
        )
        if artifact is None:
            raise NotFoundError("upload not found")
        if artifact.status == "available":
            return {"status": "available", "artifact_id": artifact.id}
        if not self.store.verify(artifact.object_key, artifact.size_bytes, artifact.digest_sha256):
            self.store.delete(artifact.object_key)
            artifact.status = "rejected"
            artifact.rejection_reason = "digest_or_size_mismatch"
            self.session.commit()
            raise ConflictError("uploaded artifact digest or size mismatch")
        artifact.status = "available"
        artifact.available_at = models.utc_now()
        artifact.upload_token_hash = None
        self.session.add(
            models.Outbox(
                organization_id=artifact.organization_id,
                workspace_id=artifact.workspace_id,
                topic="artifact.available",
                aggregate_type="artifact",
                aggregate_id=artifact.id,
                payload={"artifact_id": artifact.id, "run_id": artifact.run_id},
            )
        )
        self.session.commit()
        return {"status": "available", "artifact_id": artifact.id}
