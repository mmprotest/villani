from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from villani_control_plane.api.routes import router
from villani_control_plane.config import Settings
from villani_control_plane.database import get_session
from villani_control_plane.errors import AuthenticationError, AuthorizationError, NotFoundError
from villani_control_plane.main import create_app
from villani_control_plane.models import (
    AdministrativeAuditEvent,
    ApiToken,
    BrowserSession,
    Membership,
    Role,
    RoleAssignment,
    User,
    utc_now,
)
from villani_control_plane.security import Principal, hash_token
from villani_control_plane.services import AuthenticationService
from villani_control_plane.services.authorization import (
    BUILT_IN_ROLE_GRANTS,
    ENDPOINT_PERMISSIONS,
    PERMISSIONS,
    PUBLIC_ENDPOINTS,
    PolicyService,
)
from villani_control_plane.services.identity import (
    AuditService,
    IdentityAdministrationService,
    IdentityService,
)


def _client(session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app, base_url="https://testserver")


def _seed_user_role(session, principal, role_name="organization owner"):
    IdentityAdministrationService(session).ensure_built_in_roles()
    user = User(email=f"{role_name.replace(' ', '.')}@example.test", display_name=role_name)
    session.add(user)
    session.flush()
    membership = Membership(organization_id=principal.organization_id, user_id=user.id)
    session.add(membership)
    session.flush()
    role = session.scalar(select(Role).where(Role.built_in.is_(True), Role.name == role_name))
    session.add(
        RoleAssignment(
            organization_id=principal.organization_id,
            role_id=role.id,
            subject_type="membership",
            subject_id=membership.id,
        )
    )
    session.commit()
    return user, role


def test_generated_authorization_matrix_covers_every_endpoint_and_builtin_role():
    registered = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }
    assert registered == set(ENDPOINT_PERMISSIONS) | set(PUBLIC_ENDPOINTS)
    assert set(BUILT_IN_ROLE_GRANTS) == {
        "organization owner",
        "administrator",
        "billing administrator",
        "security administrator",
        "workspace administrator",
        "developer",
        "operator",
        "reviewer",
        "viewer",
    }
    for role, grants in BUILT_IN_ROLE_GRANTS.items():
        assert grants <= PERMISSIONS, role
        principal = Principal(
            role,
            "org",
            "workspace",
            principal_type="user",
            subject_id=role,
            permissions=grants,
            scopes=frozenset({"*"}),
        )
        for endpoint, permission in ENDPOINT_PERMISSIONS.items():
            if permission in grants:
                PolicyService().authorize_endpoint(principal, *endpoint)
            else:
                with pytest.raises(AuthorizationError):
                    PolicyService().authorize_endpoint(principal, *endpoint)


def test_custom_roles_are_permission_allowlists(session, principal):
    service = IdentityAdministrationService(session)
    role = service.create_custom_role(principal, "release reader", ["run.read", "artifact.read"])
    assert role.permissions == ["artifact.read", "run.read"]
    with pytest.raises(AuthorizationError, match="unknown permissions"):
        service.create_custom_role(principal, "arbitrary", ["python: allow_all()"])


def test_scoped_expiring_rotatable_keys_are_hashed_and_revoke_immediately(session, principal):
    user, _ = _seed_user_role(session, principal, "viewer")
    service = IdentityAdministrationService(session)
    key, plaintext = service.create_api_key(
        principal, "viewer-key", ["run.read"], 3600, user_id=user.id
    )
    assert plaintext not in key.secret_hash
    assert plaintext not in key.lookup_digest
    authenticated = AuthenticationService(session).authenticate(plaintext)
    assert authenticated.scopes == frozenset({"run.read"})
    assert session.get(ApiToken, key.id).last_used_at is not None
    replacement, replacement_plaintext = service.rotate_api_key(principal, key.id)
    with pytest.raises(AuthenticationError):
        AuthenticationService(session).authenticate(plaintext)
    assert (
        AuthenticationService(session).authenticate(replacement_plaintext).token_id
        == replacement.id
    )
    service.revoke_api_key(principal, replacement.id)
    with pytest.raises(AuthenticationError):
        AuthenticationService(session).authenticate(replacement_plaintext)


def test_expired_key_and_cross_organization_guessed_id_fail(session, principal):
    user, _ = _seed_user_role(session, principal, "viewer")
    service = IdentityAdministrationService(session)
    key, plaintext = service.create_api_key(principal, "expired", ["run.read"], 1, user_id=user.id)
    key.expires_at = utc_now() - timedelta(seconds=1)
    session.commit()
    with pytest.raises(AuthenticationError):
        AuthenticationService(session).authenticate(plaintext)
    other = Principal("other", "org_other", "workspace_other", permissions=PERMISSIONS)
    with pytest.raises(NotFoundError):
        service.revoke_api_key(other, key.id)


def test_browser_sessions_require_csrf_are_secure_and_revoke_immediately(session, principal):
    user, _ = _seed_user_role(session, principal, "organization owner")
    user.password_hash = hash_token("correct horse battery staple")
    session.commit()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        secure_cookies=True,
        session_ttl_seconds=3600,
    )
    token, csrf, record = IdentityService(session, settings).local_login(
        user.email,
        "correct horse battery staple",
        principal.organization_id,
        principal.workspace_id,
        "127.0.0.1",
        "req-login",
    )
    assert AuthenticationService(session).authenticate_session(token).subject_id == user.id
    admin = Principal(
        "admin",
        principal.organization_id,
        principal.workspace_id,
        principal_type="user",
        subject_id=user.id,
        permissions=PERMISSIONS,
    )
    IdentityAdministrationService(session).revoke_session(admin, record.id)
    with pytest.raises(AuthenticationError):
        AuthenticationService(session).authenticate_session(token)

    client = _client(session)
    response = client.post(
        "/v1/auth/local/login",
        json={
            "email": user.email,
            "password": "correct horse battery staple",
            "organization_id": principal.organization_id,
            "workspace_id": principal.workspace_id,
        },
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert client.post("/v1/auth/logout").status_code == 401
    assert (
        client.post(
            "/v1/auth/logout", headers={"X-CSRF-Token": response.json()["csrf_token"]}
        ).status_code
        == 200
    )


def test_service_accounts_have_no_interactive_session_identity(session, principal):
    _, role = _seed_user_role(session, principal, "viewer")
    account = IdentityAdministrationService(session).create_service_account(
        principal, "automation", role.id
    )
    assert not hasattr(account, "password_hash")
    assert not hasattr(BrowserSession, "service_account_id")


def test_audit_events_are_secret_free_immutable_and_have_required_envelope(session, principal):
    event = AuditService(session).record(
        actor_id=principal.actor_id,
        actor_type="development_token",
        organization_id=principal.organization_id,
        action="secret.change",
        target_type="secret",
        target_id="secret-1",
        result="success",
        request_id="req-1",
        source_ip="10.0.0.1",
        before={"api_key": "plaintext-before"},
        after={"api_key": "plaintext-after"},
    )
    session.commit()
    assert event.before_digest and event.after_digest
    assert "plaintext" not in repr(event.__dict__)
    event.result = "altered"
    with pytest.raises(ValueError, match="immutable"):
        session.commit()
    session.rollback()
    assert session.scalar(select(AdministrativeAuditEvent)).result == "success"


def test_plaintext_api_keys_never_appear_in_database_string_fields(session, principal):
    user, _ = _seed_user_role(session, principal, "viewer")
    key, plaintext = IdentityAdministrationService(session).create_api_key(
        principal, "no-plaintext", ["run.read"], 3600, user_id=user.id
    )
    stored = session.get(ApiToken, key.id)
    assert all(
        plaintext not in value for value in stored.__dict__.values() if isinstance(value, str)
    )
