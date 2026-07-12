from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    ApiToken,
    EnrollmentToken,
    Identity,
    Membership,
    Organization,
    Role,
    RoleAssignment,
    User,
    Workspace,
    utc_now,
)
from ..security import hash_token, token_lookup_digest
from ..tamper import backfill_legacy_audit_hashes
from .identity import IdentityAdministrationService


class DevelopmentBootstrapService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def bootstrap(self) -> None:
        backfill_legacy_audit_hashes(self.session)
        IdentityAdministrationService(self.session).ensure_built_in_roles()
        self.session.flush()
        if not any(
            (
                self.settings.dev_api_token,
                self.settings.dev_enrollment_token,
                self.settings.dev_user_email,
            )
        ):
            self.session.commit()
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
        if self.settings.dev_user_email and self.settings.dev_user_password:
            email = self.settings.dev_user_email.lower()
            user = self.session.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(
                    email=email,
                    display_name="Development Owner",
                    password_hash=hash_token(self.settings.dev_user_password),
                )
                self.session.add(user)
                self.session.flush()
                self.session.add(
                    Identity(
                        user_id=user.id,
                        provider="local",
                        issuer="local",
                        subject=email,
                        email=email,
                    )
                )
            membership = self.session.scalar(
                select(Membership).where(
                    Membership.organization_id == self.settings.dev_organization_id,
                    Membership.user_id == user.id,
                )
            )
            if membership is None:
                membership = Membership(
                    organization_id=self.settings.dev_organization_id, user_id=user.id
                )
                self.session.add(membership)
                self.session.flush()
                owner = self.session.scalar(
                    select(Role).where(Role.built_in.is_(True), Role.name == "organization owner")
                )
                self.session.add(
                    RoleAssignment(
                        organization_id=self.settings.dev_organization_id,
                        role_id=owner.id,
                        subject_type="membership",
                        subject_id=membership.id,
                    )
                )
        self.session.commit()
