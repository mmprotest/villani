from __future__ import annotations

from dataclasses import dataclass

from ..errors import AuthorizationError
from ..security import Principal

# This is the complete custom-role allowlist. Adding a permission is a reviewed code change.
PERMISSIONS = frozenset(
    {
        "organization.read",
        "organization.manage",
        "billing.manage",
        "security.manage",
        "audit.read",
        "identity.manage",
        "membership.manage",
        "group.manage",
        "role.manage",
        "workspace.read",
        "workspace.manage",
        "project.read",
        "project.manage",
        "repository.read",
        "repository.manage",
        "run.read",
        "run.write",
        "artifact.read",
        "artifact.write",
        "artifact.export",
        "query.execute",
        "query.sensitive",
        "worker.operate",
        "worker.manage",
        "task.submit",
        "task.operate",
        "approval.review",
        "policy.read",
        "policy.manage",
        "export.create",
        "retention.manage",
        "deletion.manage",
        "secret.manage",
        "deployment.manage",
        "api_key.manage",
        "service_account.manage",
        "session.revoke",
        "invitation.manage",
    }
)

VIEWER = {
    "organization.read",
    "workspace.read",
    "project.read",
    "repository.read",
    "run.read",
    "artifact.read",
    "query.execute",
    "policy.read",
    "session.revoke",
}
BUILT_IN_ROLE_GRANTS: dict[str, frozenset[str]] = {
    "organization owner": PERMISSIONS,
    "administrator": PERMISSIONS - {"billing.manage"},
    "billing administrator": frozenset({"organization.read", "billing.manage", "audit.read"}),
    "security administrator": frozenset(
        {
            "organization.read",
            "security.manage",
            "audit.read",
            "identity.manage",
            "membership.manage",
            "group.manage",
            "role.manage",
            "workspace.read",
            "api_key.manage",
            "service_account.manage",
            "session.revoke",
            "invitation.manage",
            "secret.manage",
            "retention.manage",
            "deletion.manage",
        }
    ),
    "workspace administrator": frozenset(
        VIEWER
        | {
            "workspace.manage",
            "project.manage",
            "repository.manage",
            "run.write",
            "artifact.write",
            "artifact.export",
            "worker.manage",
            "worker.operate",
            "task.submit",
            "task.operate",
            "approval.review",
            "policy.manage",
            "export.create",
            "api_key.manage",
            "service_account.manage",
        }
    ),
    "developer": frozenset(
        VIEWER | {"run.write", "artifact.write", "task.submit", "worker.operate"}
    ),
    "operator": frozenset(
        VIEWER
        | {
            "run.write",
            "artifact.write",
            "worker.operate",
            "task.submit",
            "task.operate",
            "approval.review",
            "policy.manage",
        }
    ),
    "reviewer": frozenset(VIEWER | {"approval.review"}),
    "viewer": frozenset(VIEWER),
}

PUBLIC_ENDPOINTS = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/build-version"),
        ("GET", "/migration-state"),
        ("GET", "/readiness"),
        ("POST", "/v1/auth/local/login"),
        ("POST", "/v1/auth/oidc/login"),
        ("POST", "/v1/installations/enroll"),
        ("PUT", "/v1/artifact-uploads/{upload_id}"),
        ("GET", "/liveness"),
        ("GET", "/metrics"),
    }
)

# One registry is consumed by runtime authorization, documentation, and the generated matrix test.
ENDPOINT_PERMISSIONS: dict[tuple[str, str], str] = {
    ("GET", "/v1/interrogation/catalog"): "query.execute",
    ("POST", "/v1/interrogation/query"): "query.execute",
    ("POST", "/v1/fleet/runs/search"): "run.read",
    ("GET", "/v1/fleet/metrics/definitions"): "run.read",
    ("POST", "/v1/fleet/metrics"): "run.read",
    ("POST", "/v1/fleet/saved-views"): "query.execute",
    ("GET", "/v1/fleet/saved-views"): "query.execute",
    ("PUT", "/v1/fleet/saved-views/{view_id}"): "query.execute",
    ("POST", "/v1/fleet/alerts"): "workspace.manage",
    ("GET", "/v1/fleet/alerts"): "run.read",
    ("POST", "/v1/fleet/runs/{run_id}/feedback"): "approval.review",
    ("GET", "/v1/fleet/runs/{run_id}/feedback"): "run.read",
    ("POST", "/v1/fleet/review-queue"): "approval.review",
    ("GET", "/v1/fleet/review-queue"): "approval.review",
    ("GET", "/v1/fleet/failure-clusters"): "run.read",
    ("POST", "/v1/fleet/export"): "export.create",
    ("PUT", "/v1/workers/{worker_id}/heartbeat"): "worker.operate",
    ("POST", "/v1/workers/{worker_id}/tasks/claim"): "worker.operate",
    ("POST", "/v1/tasks"): "task.submit",
    ("POST", "/v1/tasks/{task_id}/cancel"): "task.operate",
    ("POST", "/v1/tasks/{task_id}/leases/{lease_id}/renew"): "worker.operate",
    ("POST", "/v1/tasks/{task_id}/leases/{lease_id}/complete"): "worker.operate",
    ("POST", "/v1/ingest/batches"): "run.write",
    ("POST", "/v1/artifacts/descriptors"): "artifact.write",
    ("POST", "/v1/artifact-uploads/{upload_id}/complete"): "artifact.write",
    ("GET", "/v1/artifacts/{artifact_id}/content"): "artifact.read",
    ("POST", "/v1/installations/{installation_id}/credentials/rotate"): "worker.operate",
    ("GET", "/v1/runs/{run_id}/stream"): "run.read",
    ("POST", "/v1/outcomes"): "run.write",
    ("POST", "/v1/outcome-ledger/outcomes"): "run.write",
    ("POST", "/v1/outcome-ledger/git-webhooks"): "run.write",
    ("GET", "/v1/outcome-ledger/runs/{run_id}"): "run.read",
    ("POST", "/v1/shadow-routing/observations"): "policy.manage",
    ("GET", "/v1/shadow-routing/metrics"): "policy.read",
    ("POST", "/v1/policy-publications"): "policy.manage",
    ("GET", "/v1/policy-publications/{publication_id}"): "policy.read",
    ("POST", "/v1/policy-publications/{publication_id}/approve"): "approval.review",
    ("POST", "/v1/policy-publications/{publication_id}/transition"): "policy.manage",
    ("POST", "/v1/policy-publications/{publication_id}/evaluate-canary"): "policy.manage",
    ("POST", "/v1/policy-publications/emergency-disable"): "security.manage",
    ("GET", "/v1/runs/{run_id}"): "run.read",
    ("GET", "/v1/runs/{run_id}/events"): "run.read",
    ("GET", "/v1/runs/{run_id}/spans"): "run.read",
    ("GET", "/v1/runs/{run_id}/artifacts"): "artifact.read",
    ("GET", "/v1/runs/{run_id}/commitment"): "run.read",
    ("GET", "/v1/runs"): "run.read",
    ("POST", "/v1/auth/logout"): "session.revoke",
    ("GET", "/v1/admin/roles"): "role.manage",
    ("POST", "/v1/admin/roles"): "role.manage",
    ("POST", "/v1/admin/users"): "identity.manage",
    ("POST", "/v1/admin/memberships"): "membership.manage",
    ("POST", "/v1/admin/groups"): "group.manage",
    ("POST", "/v1/admin/groups/{group_id}/members"): "group.manage",
    ("POST", "/v1/admin/role-assignments"): "role.manage",
    ("POST", "/v1/admin/service-accounts"): "service_account.manage",
    ("POST", "/v1/admin/api-keys"): "api_key.manage",
    ("POST", "/v1/admin/api-keys/{key_id}/rotate"): "api_key.manage",
    ("POST", "/v1/admin/api-keys/{key_id}/revoke"): "api_key.manage",
    ("POST", "/v1/admin/sessions/{session_id}/revoke"): "session.revoke",
    ("POST", "/v1/admin/invitations"): "invitation.manage",
    ("GET", "/v1/admin/audit-events"): "audit.read",
    ("GET", "/v1/admin/federation/saml"): "identity.manage",
    ("POST", "/v1/admin/federation/scim/sync"): "identity.manage",
    ("GET", "/v1/admin/governance/policies"): "retention.manage",
    ("POST", "/v1/admin/governance/policies"): "retention.manage",
    ("POST", "/v1/admin/governance/legal-holds"): "retention.manage",
    ("POST", "/v1/admin/governance/retention/sweep"): "retention.manage",
    ("POST", "/v1/admin/governance/deletions"): "deletion.manage",
    ("POST", "/v1/admin/governance/deletions/{workflow_id}/complete"): "deletion.manage",
    ("POST", "/v1/admin/governance/exports"): "export.create",
    ("POST", "/v1/admin/quotas/policies"): "billing.manage",
    ("GET", "/v1/admin/usage/export"): "billing.manage",
    ("GET", "/v1/admin/tamper/audit/verify"): "audit.read",
}

INSTALLATION_PERMISSIONS = frozenset({"worker.operate", "run.write", "artifact.write"})


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    permission: str
    allowed: bool
    policy_version: str = "villani.enterprise_rbac.v1"


class PolicyService:
    """Fail-closed authorization boundary shared by APIs and internal service seams."""

    def authorize(self, principal: Principal, permission: str) -> AuthorizationDecision:
        if permission not in PERMISSIONS:
            raise AuthorizationError("unknown permission")
        if permission not in principal.permissions:
            raise AuthorizationError(f"permission required: {permission}")
        if "*" not in principal.scopes and permission not in principal.scopes:
            raise AuthorizationError(f"credential scope required: {permission}")
        return AuthorizationDecision(permission, True)

    def authorize_endpoint(self, principal: Principal, method: str, route_path: str) -> None:
        permission = ENDPOINT_PERMISSIONS.get((method.upper(), route_path))
        if permission is None:
            raise AuthorizationError("endpoint has no registered authorization policy")
        self.authorize(principal, permission)

    def authorize_tenant(self, principal: Principal, organization_id: str) -> None:
        if principal.organization_id != organization_id:
            raise AuthorizationError("cross-organization access denied")


def validate_custom_permissions(permissions: list[str]) -> list[str]:
    unknown = sorted(set(permissions) - PERMISSIONS)
    if unknown:
        raise AuthorizationError(f"custom role contains unknown permissions: {', '.join(unknown)}")
    return sorted(set(permissions))
