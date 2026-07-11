from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import ApiToken, EnrollmentToken, Organization, Workspace, utc_now
from ..security import hash_token, token_lookup_digest


class DevelopmentBootstrapService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def bootstrap(self) -> None:
        if not self.settings.dev_api_token:
            if not self.settings.dev_enrollment_token:
                return
        organization = self.session.get(Organization, self.settings.dev_organization_id)
        if organization is None:
            self.session.add(
                Organization(
                    id=self.settings.dev_organization_id,
                    name=self.settings.dev_organization_name,
                )
            )
            self.session.flush()
        workspace = self.session.get(
            Workspace,
            (self.settings.dev_organization_id, self.settings.dev_workspace_id),
        )
        if workspace is None:
            self.session.add(
                Workspace(
                    organization_id=self.settings.dev_organization_id,
                    id=self.settings.dev_workspace_id,
                    name=self.settings.dev_workspace_name,
                )
            )
            self.session.flush()
        if self.settings.dev_api_token:
            digest = token_lookup_digest(self.settings.dev_api_token)
            token = self.session.scalar(select(ApiToken).where(ApiToken.lookup_digest == digest))
        else:
            token = None
        if self.settings.dev_api_token and token is None:
            self.session.add(
                ApiToken(
                    organization_id=self.settings.dev_organization_id,
                    workspace_id=self.settings.dev_workspace_id,
                    name="development",
                    lookup_digest=digest,
                    secret_hash=hash_token(self.settings.dev_api_token),
                )
            )
        if self.settings.dev_enrollment_token:
            digest = token_lookup_digest(self.settings.dev_enrollment_token)
            enrollment = self.session.scalar(
                select(EnrollmentToken).where(EnrollmentToken.lookup_digest == digest)
            )
            if enrollment is None:
                self.session.add(
                    EnrollmentToken(
                        organization_id=self.settings.dev_organization_id,
                        workspace_id=self.settings.dev_workspace_id,
                        lookup_digest=digest,
                        secret_hash=hash_token(self.settings.dev_enrollment_token),
                        expires_at=utc_now() + timedelta(days=7),
                    )
                )
        self.session.commit()
