from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from villani_control_plane.database import Base
from villani_control_plane.models import ApiToken, Organization, Project, Repository, Workspace
from villani_control_plane.security import Principal, hash_token, token_lookup_digest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
V2_FIXTURES = REPOSITORY_ROOT / "integration" / "fixtures" / "protocol" / "v2" / "valid"
TEST_TOKEN = "unit-development-token-that-is-long-enough"


def load_v2_fixture(name: str) -> dict:
    return json.loads((V2_FIXTURES / name).read_text(encoding="utf-8"))


def seed_tenant(
    session: Session,
    *,
    organization_id: str = "org_1",
    workspace_id: str = "workspace_1",
    project_id: str = "project_1",
    repository_id: str = "repo_001",
    token: str = TEST_TOKEN,
) -> Principal:
    session.add(Organization(id=organization_id, name=f"Organization {organization_id}"))
    session.flush()
    session.add(Workspace(organization_id=organization_id, id=workspace_id, name="Workspace"))
    session.flush()
    session.add(
        Project(
            organization_id=organization_id,
            workspace_id=workspace_id,
            id=project_id,
            name="Project",
        )
    )
    session.flush()
    session.add(
        Repository(
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
            id=repository_id,
            name="Repository",
        )
    )
    session.flush()
    record = ApiToken(
        organization_id=organization_id,
        workspace_id=workspace_id,
        name="test",
        lookup_digest=token_lookup_digest(token),
        secret_hash=hash_token(token),
    )
    session.add(record)
    session.commit()
    return Principal(record.id, organization_id, workspace_id)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


@pytest.fixture
def principal(session: Session) -> Principal:
    return seed_tenant(session)


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--run-load-smoke",
        action="store_true",
        default=False,
        help="run the 100,000-event PostgreSQL load smoke test",
    )


@pytest.fixture(scope="session")
def postgres_url() -> str:
    value = os.environ.get("VILLANI_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("VILLANI_TEST_POSTGRES_URL is not configured")
    if "postgresql" not in value:
        pytest.fail("VILLANI_TEST_POSTGRES_URL must use PostgreSQL")
    return value
