from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import AuthenticationError, ConflictError, NotFoundError
from ..models import (
    AdministrativeAuditEvent,
    ApiToken,
    BrowserSession,
    Group,
    GroupMembership,
    Identity,
    Invitation,
    Membership,
    Role,
    RoleAssignment,
    ServiceAccount,
    User,
    Workspace,
    utc_now,
)
from ..security import (
    Principal,
    hash_token,
    mask_sensitive_fields,
    token_lookup_digest,
    verify_token,
)
from ..tamper import canonical_timestamp
from .auth import AuthenticationService
from .authorization import BUILT_IN_ROLE_GRANTS, validate_custom_permissions

ADMINISTRATIVE_ACTION_CATEGORIES = frozenset(
    {
        "login",
        "membership",
        "role",
        "key",
        "policy",
        "export",
        "retention",
        "deletion",
        "secret",
        "deployment",
    }
)


def classify_ip(value: str | None) -> str:
    try:
        address = ipaddress.ip_address(value or "")
    except ValueError:
        return "unknown"
    if address.is_loopback:
        return "loopback"
    if address.is_private:
        return "private"
    return "public"


def safe_digest(value: object | None) -> str | None:
    if value is None:
        return None
    safe = mask_sensitive_fields(value)
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class AuditService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        actor_id: str,
        actor_type: str,
        organization_id: str,
        action: str,
        target_type: str,
        target_id: str,
        result: str,
        request_id: str,
        source_ip: str | None,
        before: object | None = None,
        after: object | None = None,
        corrects_event_id: str | None = None,
    ) -> AdministrativeAuditEvent:
        if action.partition(".")[0] not in ADMINISTRATIVE_ACTION_CATEGORIES and action.partition(
            "."
        )[0] not in {"group", "service_account", "session"}:
            raise ValueError("unknown administrative audit action category")
        previous = self.session.scalar(
            select(AdministrativeAuditEvent)
            .where(AdministrativeAuditEvent.organization_id == organization_id)
            .order_by(
                AdministrativeAuditEvent.occurred_at.desc(), AdministrativeAuditEvent.id.desc()
            )
            .limit(1)
            .with_for_update()
        )
        previous_hash = previous.event_hash if previous else "0" * 64
        occurred_at = utc_now()
        chain_body = {
            "actor_id": actor_id,
            "actor_type": actor_type,
            "organization_id": organization_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "result": result,
            "request_id": request_id,
            "source_ip_classification": classify_ip(source_ip),
            "before_digest": safe_digest(before),
            "after_digest": safe_digest(after),
            "corrects_event_id": corrects_event_id,
            "occurred_at": canonical_timestamp(occurred_at),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(
            json.dumps(chain_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        event = AdministrativeAuditEvent(
            actor_id=actor_id,
            actor_type=actor_type,
            organization_id=organization_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            request_id=request_id,
            source_ip_classification=chain_body["source_ip_classification"],
            before_digest=chain_body["before_digest"],
            after_digest=chain_body["after_digest"],
            previous_hash=previous_hash,
            event_hash=event_hash,
            corrects_event_id=corrects_event_id,
            occurred_at=occurred_at,
        )
        self.session.add(event)
        return event


@dataclass(frozen=True, slots=True)
class FederatedIdentity:
    issuer: str
    subject: str
    email: str
    display_name: str


class OIDCProvider(Protocol):
    def verify(self, assertion: str) -> FederatedIdentity: ...


class FakeOIDCProvider:
    """Deterministic test/development provider; not a production token verifier."""

    def verify(self, assertion: str) -> FederatedIdentity:
        try:
            issuer, subject, email, display_name = assertion.split("|", 3)
        except ValueError as error:
            raise AuthenticationError("invalid fake OIDC assertion") from error
        return FederatedIdentity(issuer, subject, email.lower(), display_name)


class SAMLProvider(Protocol):
    def metadata(self) -> dict[str, str]: ...


class FakeSAMLProvider:
    production_compatible = False

    def metadata(self) -> dict[str, str]:
        return {"provider": "fake", "status": "interface_only", "production_compatible": "false"}


class SCIMProvider(Protocol):
    def synchronize(self, organization_id: str, records: list[dict]) -> dict[str, int]: ...


class FakeSCIMProvider:
    production_compatible = False

    def synchronize(self, organization_id: str, records: list[dict]) -> dict[str, int]:
        del organization_id
        return {"received": len(records), "created": 0, "updated": 0}


class IdentityService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def local_login(
        self,
        email: str,
        password: str,
        organization_id: str,
        workspace_id: str,
        source_ip: str | None,
        request_id: str,
    ) -> tuple[str, str, BrowserSession]:
        user = self.session.scalar(
            select(User).where(User.email == email.lower(), User.deleted_at.is_(None))
        )
        if user is None or not user.password_hash or not verify_token(password, user.password_hash):
            raise AuthenticationError("invalid local credentials")
        return self._create_session(
            user, organization_id, workspace_id, source_ip, request_id, "local"
        )

    def oidc_login(
        self,
        assertion: str,
        organization_id: str,
        workspace_id: str,
        source_ip: str | None,
        request_id: str,
        provider: OIDCProvider | None = None,
    ) -> tuple[str, str, BrowserSession]:
        identity = (provider or FakeOIDCProvider()).verify(assertion)
        record = self.session.scalar(
            select(Identity).where(
                Identity.provider == "oidc",
                Identity.issuer == identity.issuer,
                Identity.subject == identity.subject,
            )
        )
        if record is None:
            raise AuthenticationError("OIDC identity is not provisioned")
        user = self.session.get(User, record.user_id)
        if user is None:
            raise AuthenticationError("OIDC user is unavailable")
        return self._create_session(
            user, organization_id, workspace_id, source_ip, request_id, "oidc"
        )

    def _create_session(
        self,
        user: User,
        organization_id: str,
        workspace_id: str,
        source_ip: str | None,
        request_id: str,
        method: str,
    ) -> tuple[str, str, BrowserSession]:
        AuthenticationService(self.session).permissions_for_user(
            user.id, organization_id, workspace_id
        )
        token = "vls_" + secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        record = BrowserSession(
            organization_id=organization_id,
            workspace_id=workspace_id,
            user_id=user.id,
            lookup_digest=token_lookup_digest(token),
            secret_hash=hash_token(token),
            csrf_hash=hash_token(csrf),
            expires_at=utc_now() + timedelta(seconds=self.settings.session_ttl_seconds),
            source_ip_classification=classify_ip(source_ip),
        )
        self.session.add(record)
        self.session.flush()
        AuditService(self.session).record(
            actor_id=user.id,
            actor_type="user",
            organization_id=organization_id,
            action=f"login.{method}",
            target_type="session",
            target_id=record.id,
            result="success",
            request_id=request_id,
            source_ip=source_ip,
            after={"session_id": record.id},
        )
        self.session.commit()
        return token, csrf, record


class IdentityAdministrationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_built_in_roles(self) -> None:
        for name, permissions in BUILT_IN_ROLE_GRANTS.items():
            role = self.session.scalar(
                select(Role).where(Role.built_in.is_(True), Role.name == name)
            )
            if role is None:
                self.session.add(Role(name=name, built_in=True, permissions=sorted(permissions)))
            else:
                role.permissions = sorted(permissions)

    def create_custom_role(self, principal: Principal, name: str, permissions: list[str]) -> Role:
        role = Role(
            organization_id=principal.organization_id,
            name=name,
            built_in=False,
            permissions=validate_custom_permissions(permissions),
        )
        self.session.add(role)
        self.session.flush()
        self._audit(
            principal,
            "role.create",
            "role",
            role.id,
            None,
            {"name": name, "permissions": role.permissions},
        )
        self.session.commit()
        return role

    def create_user(
        self,
        principal: Principal,
        email: str,
        display_name: str,
        provider: str,
        issuer: str,
        subject: str,
        password: str | None = None,
    ) -> User:
        if provider not in {"local", "oidc", "saml"}:
            raise ConflictError("unsupported identity provider")
        if provider == "local" and (not password or len(password) < 12):
            raise ConflictError("local passwords must contain at least 12 characters")
        user = User(
            email=email.lower(),
            display_name=display_name,
            password_hash=hash_token(password) if password else None,
        )
        self.session.add(user)
        self.session.flush()
        self.session.add(
            Identity(
                user_id=user.id,
                provider=provider,
                issuer=issuer,
                subject=subject,
                email=email.lower(),
            )
        )
        self._audit(
            principal,
            "membership.user.create",
            "user",
            user.id,
            None,
            {"email": email.lower(), "provider": provider},
        )
        self.session.commit()
        return user

    def list_roles(self, principal: Principal) -> list[Role]:
        return list(
            self.session.scalars(
                select(Role)
                .where(
                    (Role.organization_id.is_(None))
                    | (Role.organization_id == principal.organization_id),
                    Role.deleted_at.is_(None),
                )
                .order_by(Role.name)
            )
        )

    def create_membership(
        self, principal: Principal, user_id: str, role_id: str, workspace_id: str | None
    ) -> Membership:
        if self.session.get(User, user_id) is None:
            raise NotFoundError("user not found")
        self._role(principal, role_id)
        membership = Membership(organization_id=principal.organization_id, user_id=user_id)
        self.session.add(membership)
        self.session.flush()
        self.session.add(
            RoleAssignment(
                organization_id=principal.organization_id,
                role_id=role_id,
                subject_type="membership",
                subject_id=membership.id,
                workspace_id=workspace_id,
            )
        )
        self._audit(
            principal,
            "membership.create",
            "membership",
            membership.id,
            None,
            {"user_id": user_id, "role_id": role_id},
        )
        self.session.commit()
        return membership

    def create_group(self, principal: Principal, name: str) -> Group:
        group = Group(organization_id=principal.organization_id, name=name)
        self.session.add(group)
        self.session.flush()
        self._audit(principal, "group.create", "group", group.id, None, {"name": name})
        self.session.commit()
        return group

    def add_group_member(self, principal: Principal, group_id: str, membership_id: str) -> None:
        group = self.session.get(Group, (principal.organization_id, group_id))
        membership = self.session.get(Membership, membership_id)
        if group is None or group.deleted_at is not None:
            raise NotFoundError("group not found")
        if (
            membership is None
            or membership.organization_id != principal.organization_id
            or membership.deleted_at is not None
        ):
            raise NotFoundError("membership not found")
        self.session.add(
            GroupMembership(
                organization_id=principal.organization_id,
                group_id=group_id,
                membership_id=membership_id,
            )
        )
        self._audit(
            principal,
            "membership.group.add",
            "group",
            group_id,
            None,
            {"membership_id": membership_id},
        )
        self.session.commit()

    def assign_role(
        self,
        principal: Principal,
        role_id: str,
        subject_type: str,
        subject_id: str,
        workspace_id: str | None,
    ) -> RoleAssignment:
        self._role(principal, role_id)
        if subject_type == "membership":
            subject = self.session.get(Membership, subject_id)
        elif subject_type == "group":
            subject = self.session.get(Group, (principal.organization_id, subject_id))
        elif subject_type == "service_account":
            subject = self.session.get(ServiceAccount, subject_id)
        else:
            raise ConflictError("role subject type is not allowlisted")
        if subject is None or subject.organization_id != principal.organization_id:
            raise NotFoundError("role subject not found")
        if (
            workspace_id
            and self.session.get(Workspace, (principal.organization_id, workspace_id)) is None
        ):
            raise NotFoundError("workspace not found")
        assignment = RoleAssignment(
            organization_id=principal.organization_id,
            role_id=role_id,
            subject_type=subject_type,
            subject_id=subject_id,
            workspace_id=workspace_id,
        )
        self.session.add(assignment)
        self.session.flush()
        self._audit(
            principal,
            "role.assign",
            "role_assignment",
            assignment.id,
            None,
            {"role_id": role_id, "subject_type": subject_type, "subject_id": subject_id},
        )
        self.session.commit()
        return assignment

    def create_service_account(
        self, principal: Principal, name: str, role_id: str
    ) -> ServiceAccount:
        self._role(principal, role_id)
        account = ServiceAccount(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            name=name,
        )
        self.session.add(account)
        self.session.flush()
        self.session.add(
            RoleAssignment(
                organization_id=principal.organization_id,
                role_id=role_id,
                subject_type="service_account",
                subject_id=account.id,
                workspace_id=principal.workspace_id,
            )
        )
        self._audit(
            principal, "service_account.create", "service_account", account.id, None, {"name": name}
        )
        self.session.commit()
        return account

    def create_api_key(
        self,
        principal: Principal,
        name: str,
        scopes: list[str],
        expires_in_seconds: int,
        user_id: str | None = None,
        service_account_id: str | None = None,
    ) -> tuple[ApiToken, str]:
        validate_custom_permissions(scopes)
        if expires_in_seconds <= 0:
            raise ConflictError("API key expiry must be in the future")
        if bool(user_id) == bool(service_account_id):
            raise ConflictError("API key must belong to exactly one user or service account")
        if user_id:
            membership = self.session.scalar(
                select(Membership).where(
                    Membership.organization_id == principal.organization_id,
                    Membership.user_id == user_id,
                    Membership.status == "active",
                    Membership.deleted_at.is_(None),
                )
            )
            if membership is None:
                raise NotFoundError("user membership not found")
        if service_account_id:
            account = self.session.get(ServiceAccount, service_account_id)
            if (
                account is None
                or account.organization_id != principal.organization_id
                or account.workspace_id != principal.workspace_id
                or account.deleted_at is not None
            ):
                raise NotFoundError("service account not found")
        plaintext = "vlk_" + secrets.token_urlsafe(32)
        record = ApiToken(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            name=name,
            lookup_digest=token_lookup_digest(plaintext),
            secret_hash=hash_token(plaintext),
            scopes=sorted(set(scopes)),
            expires_at=utc_now() + timedelta(seconds=expires_in_seconds),
            user_id=user_id,
            service_account_id=service_account_id,
        )
        self.session.add(record)
        self.session.flush()
        self._audit(
            principal, "key.create", "api_key", record.id, None, {"name": name, "scopes": scopes}
        )
        self.session.commit()
        return record, plaintext

    def rotate_api_key(self, principal: Principal, key_id: str) -> tuple[ApiToken, str]:
        old = self._key(principal, key_id)
        old.revoked_at = utc_now()
        plaintext = "vlk_" + secrets.token_urlsafe(32)
        new = ApiToken(
            organization_id=old.organization_id,
            workspace_id=old.workspace_id,
            name=f"{old.name}-rotated-{secrets.token_hex(3)}",
            lookup_digest=token_lookup_digest(plaintext),
            secret_hash=hash_token(plaintext),
            scopes=old.scopes,
            expires_at=old.expires_at,
            rotated_from_id=old.id,
            user_id=old.user_id,
            service_account_id=old.service_account_id,
        )
        self.session.add(new)
        self.session.flush()
        self._audit(
            principal,
            "key.rotate",
            "api_key",
            old.id,
            {"revoked": False},
            {"revoked": True, "replacement_id": new.id},
        )
        self.session.commit()
        return new, plaintext

    def revoke_api_key(self, principal: Principal, key_id: str) -> None:
        record = self._key(principal, key_id)
        record.revoked_at = utc_now()
        self._audit(
            principal, "key.revoke", "api_key", key_id, {"revoked": False}, {"revoked": True}
        )
        self.session.commit()

    def revoke_session(self, principal: Principal, session_id: str) -> None:
        record = self.session.get(BrowserSession, session_id)
        if record is None or record.organization_id != principal.organization_id:
            raise NotFoundError("session not found")
        record.revoked_at = utc_now()
        self._audit(
            principal,
            "session.revoke",
            "session",
            session_id,
            {"revoked": False},
            {"revoked": True},
        )
        self.session.commit()

    def invite(
        self, principal: Principal, email: str, role_ids: list[str], expires_in_seconds: int
    ) -> tuple[Invitation, str]:
        plaintext = "vli_" + secrets.token_urlsafe(32)
        invitation = Invitation(
            organization_id=principal.organization_id,
            email=email.lower(),
            role_ids=role_ids,
            invited_by=principal.actor_id,
            token_lookup_digest=token_lookup_digest(plaintext),
            token_hash=hash_token(plaintext),
            expires_at=utc_now() + timedelta(seconds=expires_in_seconds),
        )
        self.session.add(invitation)
        self.session.flush()
        self._audit(
            principal,
            "membership.invite",
            "invitation",
            invitation.id,
            None,
            {"email": email, "role_ids": role_ids},
        )
        self.session.commit()
        return invitation, plaintext

    def audit_events(
        self, principal: Principal, limit: int = 100
    ) -> list[AdministrativeAuditEvent]:
        return list(
            self.session.scalars(
                select(AdministrativeAuditEvent)
                .where(AdministrativeAuditEvent.organization_id == principal.organization_id)
                .order_by(AdministrativeAuditEvent.occurred_at.desc())
                .limit(limit)
            )
        )

    def _key(self, principal: Principal, key_id: str) -> ApiToken:
        record = self.session.get(ApiToken, key_id)
        if record is None or record.organization_id != principal.organization_id:
            raise NotFoundError("API key not found")
        return record

    def _role(self, principal: Principal, role_id: str) -> Role:
        role = self.session.get(Role, role_id)
        if (
            role is None
            or role.deleted_at is not None
            or (not role.built_in and role.organization_id != principal.organization_id)
        ):
            raise NotFoundError("role not found")
        return role

    def _audit(
        self,
        principal: Principal,
        action: str,
        target_type: str,
        target_id: str,
        before: object | None,
        after: object | None,
    ) -> None:
        AuditService(self.session).record(
            actor_id=principal.actor_id,
            actor_type=principal.principal_type,
            organization_id=principal.organization_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result="success",
            request_id="service",
            source_ip=None,
            before=before,
            after=after,
        )
