from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import AuthenticationError, AuthorizationError
from ..models import AgentInstallation, ApiToken
from ..security import Principal, token_lookup_digest, verify_token


class AuthenticationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def authenticate(self, token: str) -> Principal:
        record = self.session.scalar(
            select(ApiToken).where(
                ApiToken.lookup_digest == token_lookup_digest(token),
                ApiToken.deleted_at.is_(None),
            )
        )
        if record is None or not verify_token(token, record.secret_hash):
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
                raise AuthenticationError("invalid API or installation credential")
            return Principal(
                installation.id,
                installation.organization_id,
                installation.workspace_id,
                installation.id,
            )
        return Principal(record.id, record.organization_id, record.workspace_id)


@dataclass(frozen=True, slots=True)
class AuthorizedQueryScope:
    organization_id: str
    workspace_id: str
    permission_version: str = "tenant_scope.v1"


class AuthorizationService:
    """Stable query-authorization boundary for future enterprise role policies."""

    def query_scope(self, principal: Principal) -> AuthorizedQueryScope:
        return AuthorizedQueryScope(principal.organization_id, principal.workspace_id)

    def authorize_query_fields(
        self, principal: Principal, fields: list[str], sensitivities: dict[str, str]
    ) -> list[str]:
        del principal
        denied = [field for field in fields if sensitivities.get(field, "restricted") != "metadata"]
        if denied:
            raise AuthorizationError(
                f"query fields require an unavailable sensitive-data permission: {', '.join(sorted(denied))}"
            )
        return list(fields)
