# Enterprise identity and authorization foundation

This document describes `villani.enterprise_rbac.v1`. It is the only authorization policy for
the control-plane API. Custom roles are data containing a subset of the code-owned permission
allowlist; they cannot contain expressions, scripts, conditions, or arbitrary policy code.

## Identity and tenancy

Users are global people. Local or OIDC identities map provider issuer/subject pairs to users.
Memberships place users in organizations, and groups collect memberships. A role assignment can
target a membership, group, or service account and can be organization-wide or workspace-scoped.
Organizations contain workspaces, workspaces contain projects, and projects contain repositories.
Every data lookup retains the organization predicate; a guessed identifier from another
organization is returned as inaccessible regardless of role.

The pre-enterprise development IDs remain `org_dev` and `workspace_dev`. Legacy Compose
development tokens remain supported for local compatibility. New credentials use the API-key
contract below. Artifact access requires both `artifact.read`/`artifact.write` and the existing
sensitivity/retention policy. Run subscriptions require `run.read` before a tenant-scoped broker
subscription is created. Natural-language queries require `query.execute`; non-metadata fields
additionally require `query.sensitive`. These checks do not replace tenant injection.

## Built-in role grants

The definitions in `services/authorization.py` are normative. This table is the exact documented
copy; the generated matrix test evaluates every registered endpoint against every role.

| Role | Exact permissions |
| --- | --- |
| Organization owner | Every allowlisted permission |
| Administrator | Every allowlisted permission except `billing.manage` |
| Billing administrator | `organization.read`, `billing.manage`, `audit.read` |
| Security administrator | `organization.read`, `security.manage`, `audit.read`, `identity.manage`, `membership.manage`, `group.manage`, `role.manage`, `workspace.read`, `api_key.manage`, `service_account.manage`, `session.revoke`, `invitation.manage`, `secret.manage`, `retention.manage`, `deletion.manage` |
| Workspace administrator | `organization.read`, `workspace.read`, `workspace.manage`, `project.read`, `project.manage`, `repository.read`, `repository.manage`, `run.read`, `run.write`, `artifact.read`, `artifact.write`, `artifact.export`, `query.execute`, `worker.manage`, `worker.operate`, `task.submit`, `task.operate`, `approval.review`, `policy.read`, `policy.manage`, `export.create`, `api_key.manage`, `service_account.manage`, `session.revoke` |
| Developer | `organization.read`, `workspace.read`, `project.read`, `repository.read`, `run.read`, `run.write`, `artifact.read`, `artifact.write`, `query.execute`, `worker.operate`, `task.submit`, `policy.read`, `session.revoke` |
| Operator | `organization.read`, `workspace.read`, `project.read`, `repository.read`, `run.read`, `run.write`, `artifact.read`, `artifact.write`, `query.execute`, `worker.operate`, `task.submit`, `task.operate`, `approval.review`, `policy.read`, `policy.manage`, `session.revoke` |
| Reviewer | `organization.read`, `workspace.read`, `project.read`, `repository.read`, `run.read`, `artifact.read`, `query.execute`, `approval.review`, `policy.read`, `session.revoke` |
| Viewer | `organization.read`, `workspace.read`, `project.read`, `repository.read`, `run.read`, `artifact.read`, `query.execute`, `policy.read`, `session.revoke` |

The complete allowlist also includes `organization.manage`, `billing.manage`, `security.manage`,
`audit.read`, `identity.manage`, `membership.manage`, `group.manage`, `role.manage`,
`repository.manage`, `artifact.export`, `query.sensitive`, `worker.manage`, `retention.manage`,
`deletion.manage`, `secret.manage`, `deployment.manage`, `invitation.manage`, and the permissions
shown above. A custom-role request containing any other string fails closed.

## Policy precedence

Evaluation order is fixed:

1. Public or dedicated one-time credential endpoints are identified explicitly; an unregistered
   endpoint fails closed.
2. Credential expiry, revocation, principal status, and service-account status are checked.
3. The authenticated organization is injected. A conflicting or foreign organization is denied
   before object existence is disclosed.
4. Interactive sessions are restricted to users. Unsafe browser methods require a matching CSRF
   token.
5. API-key scopes restrict effective permission; a key never expands role grants.
6. Organization/group/workspace role grants are unioned. There are no data-authored deny rules or
   executable policies.
7. Resource sensitivity, retention, ownership, approval, and state-machine constraints apply
   after RBAC and can only narrow access.

Role, key, membership, service-account, and session state is read from the database on every
request. The authorization cache bound is therefore **0 seconds**. Revocation is effective on the
next request.

## Authentication and credentials

Local authentication stores a scrypt password verifier. OIDC is represented by a verifier
interface and issuer/subject identity mapping; the included deterministic provider is for tests
and local development only. SAML and SCIM have typed interfaces and fake providers. Villani does
not claim production SAML or SCIM compatibility until a real integration-test provider is added.

Browser sessions expire, can be revoked, use `HttpOnly; Secure; SameSite=Strict` cookies, and
require a separate CSRF token for unsafe methods. Service accounts have no password or interactive
session relationship. Authentication and general API paths have independent per-minute rate
limits.

API keys have a random plaintext value returned only by create/rotate responses. The database
stores a SHA-256 lookup digest and salted scrypt verifier, scopes, expiry, revocation, rotation
lineage, and last-used time. Rotation revokes the predecessor in the same transaction. Plaintext
keys must never be placed in request logs, audit digests, fixtures, or persistence.

## Administrative audit

Administrative audit events are append-only ORM entities with no public mutation API; database
updates and deletes through the ORM are rejected. Events cover login, membership, role, key,
policy, export, retention, deletion, secret, and deployment changes. Every event records actor
and type, organization, action, target type/ID, result, request ID, source-IP classification,
timestamp, and optional canonical before/after SHA-256 digests. Sensitive values are masked
before digesting and are never stored in the event.
