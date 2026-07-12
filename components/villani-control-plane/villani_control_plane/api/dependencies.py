from __future__ import annotations

from collections import defaultdict, deque
from functools import lru_cache
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_session
from ..errors import AuthenticationError, RateLimitError
from ..models import BrowserSession
from ..object_store import ObjectStore, create_object_store
from ..security import Principal, verify_token
from ..services import AuthenticationService
from ..services.authorization import ENDPOINT_PERMISSIONS, PUBLIC_ENDPOINTS, PolicyService

SessionDependency = Annotated[Session, Depends(get_session)]


def authenticated_principal(
    request: Request,
    session: SessionDependency,
    authorization: Annotated[str | None, Header()] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    authorization = authorization or request.headers.get("authorization")
    csrf_token = csrf_token or request.headers.get("x-csrf-token")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and token:
        principal = AuthenticationService(session).authenticate(token)
    else:
        cookie = request.cookies.get(get_settings().session_cookie_name)
        if not cookie:
            raise AuthenticationError("Bearer token or browser session required")
        principal = AuthenticationService(session).authenticate_session(cookie)
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            record = session.get(BrowserSession, principal.session_id)
            if record is None or not csrf_token or not verify_token(csrf_token, record.csrf_hash):
                raise AuthenticationError("valid CSRF token required")
    request.state.principal = principal
    return principal


PrincipalDependency = Annotated[Principal, Depends(authenticated_principal)]


@lru_cache
def object_store() -> ObjectStore:
    return create_object_store(get_settings())


ObjectStoreDependency = Annotated[ObjectStore, Depends(object_store)]


class _RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str, limit: int) -> None:
        now = monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= limit:
                raise RateLimitError("request rate limit exceeded", retry_after=60)
            hits.append(now)


_rate_limiter = _RateLimiter()


def authorize_request(request: Request, session: SessionDependency) -> None:
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    endpoint = (request.method.upper(), route_path)
    settings = get_settings()
    client = request.client.host if request.client else "unknown"
    if endpoint in PUBLIC_ENDPOINTS:
        limit = (
            settings.authentication_rate_limit_per_minute
            if route_path.startswith("/v1/auth/")
            else settings.api_rate_limit_per_minute
        )
        _rate_limiter.check(f"public:{client}:{route_path}", limit)
        return
    if endpoint not in ENDPOINT_PERMISSIONS:
        from ..errors import AuthorizationError

        raise AuthorizationError("endpoint has no registered authorization policy")
    principal = authenticated_principal(request, session)
    _rate_limiter.check(
        f"principal:{principal.principal_type}:{principal.token_id}",
        settings.api_rate_limit_per_minute,
    )
    PolicyService().authorize_endpoint(principal, request.method, route_path)
