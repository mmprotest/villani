from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..errors import AuthenticationError, AuthorizationError
from ..models import (
    AgentInstallation,
    ApiToken,
    BrowserSession,
    GroupMembership,
    Membership,
    Role,
    RoleAssignment,
    ServiceAccount,
    User,
    utc_now,
)
from ..security import Principal, token_lookup_digest, verify_token
from .authorization import INSTALLATION_PERMISSIONS, PERMISSIONS


def _active(timestamp: datetime | None) -> bool:
    if timestamp is None:
        return True
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp > utc_now()


class AuthenticationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def authenticate(self, token: str) -> Principal:
        record = self.session.scalar(
            select(ApiToken).where(
                ApiToken.lookup_digest == token_lookup_digest(token),
                ApiToken.deleted_at.is_(None),
                ApiToken.revoked_at.is_(None),
            )
        )
        if (
            record is not None
            and verify_token(token, record.secret_hash)
            and _active(record.expires_at)
        ):
            record.last_used_at = utc_now()
            permissions, principal_type, subject_id = self._api_key_permissions(record)
            self.session.commit()
            return Principal(
                record.id,
                record.organization_id,
                record.workspace_id,
                principal_type=principal_type,
                subject_id=subject_id,
                permissions=permissions,
                scopes=frozenset(record.scopes),
            )
        installation = self.session.scalar(
            select(AgentInstallation).where(
                AgentInstallation.credential_lookup_digest == token_lookup_digest(token),
                AgentInstallation.deleted_at.is_(None),
            )
        )
        if (
            installation is None
            or not installation.credential_hash
            or not verify_token(token, installation.credential_hash)
        ):
            raise AuthenticationError("invalid, expired, or revoked credential")
        return Principal(
            installation.id,
            installation.organization_id,
            installation.workspace_id,
            installation.id,
            "installation",
            installation.id,
            INSTALLATION_PERMISSIONS,
            INSTALLATION_PERMISSIONS,
        )

    def authenticate_session(self, token: str) -> Principal:
        record = self.session.scalar(
            select(BrowserSession).where(
                BrowserSession.lookup_digest == token_lookup_digest(token),
                BrowserSession.revoked_at.is_(None),
            )
        )
        if (
            record is None
            or not _active(record.expires_at)
            or not verify_token(token, record.secret_hash)
        ):
            raise AuthenticationError("invalid, expired, or revoked session")
        user = self.session.get(User, record.user_id)
        if user is None or user.deleted_at is not None or user.status != "active":
            raise AuthenticationError("session user is inactive")
        permissions = self.permissions_for_user(
            record.user_id, record.organization_id, record.workspace_id
        )
        record.last_used_at = utc_now()
        self.session.commit()
        return Principal(
            record.id,
            record.organization_id,
            record.workspace_id,
            principal_type="user",
            subject_id=record.user_id,
            permissions=permissions,
            scopes=frozenset({"*"}),
            session_id=record.id,
        )

    def _api_key_permissions(self, record: ApiToken) -> tuple[frozenset[str], str, str]:
        if record.service_account_id:
            account = self.session.get(ServiceAccount, record.service_account_id)
            if account is None or account.deleted_at is not None or account.disabled_at is not None:
                raise AuthenticationError("service account is inactive")
            return (
                self._assigned_permissions(
                    record.organization_id, record.workspace_id, "service_account", account.id
                ),
                "service_account",
                account.id,
            )
        if record.user_id:
            return (
                self.permissions_for_user(
                    record.user_id, record.organization_id, record.workspace_id
                ),
                "user",
                record.user_id,
            )
        # Pre-enterprise development tokens retain compatibility and are never issued by the new API.
        return PERMISSIONS, "development_token", record.id

    def permissions_for_user(
        self, user_id: str, organization_id: str, workspace_id: str
    ) -> frozenset[str]:
        membership = self.session.scalar(
            select(Membership).where(
                Membership.organization_id == organization_id,
                Membership.user_id == user_id,
                Membership.status == "active",
                Membership.deleted_at.is_(None),
            )
        )
        if membership is None:
            raise AuthenticationError("active organization membership required")
        subjects = [membership.id]
        subjects.extend(
            self.session.scalars(
                select(GroupMembership.group_id).where(
                    GroupMembership.organization_id == organization_id,
                    GroupMembership.membership_id == membership.id,
                )
            ).all()
        )
        direct = self._assigned_permissions(
            organization_id, workspace_id, "membership", membership.id
        )
        group_permissions: set[str] = set()
        for group_id in subjects[1:]:
            group_permissions.update(
                self._assigned_permissions(organization_id, workspace_id, "group", group_id)
            )
        return frozenset(set(direct) | group_permissions)

    def _assigned_permissions(
        self, organization_id: str, workspace_id: str, subject_type: str, subject_id: str
    ) -> frozenset[str]:
        rows = self.session.execute(
            select(Role.permissions)
            .join(RoleAssignment, RoleAssignment.role_id == Role.id)
            .where(
                RoleAssignment.organization_id == organization_id,
                RoleAssignment.subject_type == subject_type,
                RoleAssignment.subject_id == subject_id,
                RoleAssignment.deleted_at.is_(None),
                Role.deleted_at.is_(None),
                or_(
                    RoleAssignment.workspace_id.is_(None),
                    RoleAssignment.workspace_id == workspace_id,
                ),
            )
        ).scalars()
        return frozenset(permission for permissions in rows for permission in permissions)


@dataclass(frozen=True, slots=True)
class AuthorizedQueryScope:
    organization_id: str
    workspace_id: str
    permission_version: str = "tenant_scope.v1"


class AuthorizationService:
    """Compatibility facade backed by the centralized enterprise policy contract."""

    def query_scope(self, principal: Principal) -> AuthorizedQueryScope:
        # Empty permissions identify pre-enterprise in-process principals used by compatibility
        # callers. Network-authenticated principals are always resolved with an explicit set.
        if principal.permissions and "query.execute" not in principal.permissions:
            raise AuthorizationError("permission required: query.execute")
        return AuthorizedQueryScope(principal.organization_id, principal.workspace_id)

    def authorize_query_fields(
        self, principal: Principal, fields: list[str], sensitivities: dict[str, str]
    ) -> list[str]:
        denied = [
            field
            for field in fields
            if sensitivities.get(field, "restricted") != "metadata"
            and principal.permissions
            and "query.sensitive" not in principal.permissions
        ]
        if denied:
            raise AuthorizationError(
                "query fields require query.sensitive: " + ", ".join(sorted(denied))
            )
        return list(fields)
