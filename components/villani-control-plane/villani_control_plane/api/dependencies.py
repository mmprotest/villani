from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_session
from ..errors import AuthenticationError
from ..object_store import ObjectStore, create_object_store
from ..security import Principal
from ..services import AuthenticationService

SessionDependency = Annotated[Session, Depends(get_session)]


def authenticated_principal(
    session: SessionDependency, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("Bearer token required")
    return AuthenticationService(session).authenticate(token)


PrincipalDependency = Annotated[Principal, Depends(authenticated_principal)]


@lru_cache
def object_store() -> ObjectStore:
    return create_object_store(get_settings())


ObjectStoreDependency = Annotated[ObjectStore, Depends(object_store)]
