from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from conftest import load_v2_fixture, seed_tenant
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from villani_control_plane import models
from villani_control_plane.errors import AuthorizationError, ConflictError, NotFoundError
from villani_control_plane.models import Event, IngestBatch, Outbox
from villani_control_plane.schemas import RemoteTaskRequest, TaskCompletionRequest
from villani_control_plane.security import Principal
from villani_control_plane.services import (
    GovernanceService,
    IngestionService,
    RemoteDispatchService,
    RunQueryService,
)

pytestmark = pytest.mark.postgres
COMPONENT_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    config = Config(str(COMPONENT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(COMPONENT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="module")
def postgres_engine(postgres_url):
    os.environ["VILLANI_CONTROL_PLANE_DATABASE_URL"] = postgres_url
    config = alembic_config(postgres_url)
    command.upgrade(config, "head")
    engine = create_engine(postgres_url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(postgres_engine):
    table_names = [
        "outbox",
        "outcomes",
        "artifacts",
        "events",
        "spans",
        "attempts",
        "ingest_batches",
        "api_tokens",
        "agent_installations",
        "runs",
        "repositories",
        "projects",
        "workspaces",
        "organizations",
    ]
    with postgres_engine.begin() as connection:
        connection.execute(text("TRUNCATE " + ",".join(table_names) + " CASCADE"))
    with Session(postgres_engine) as session:
        yield session


def make_event(sequence: int, *, run_id: str = "run_pg") -> dict:
    event = load_v2_fixture("telemetry-envelope.json")
    event.update(
        event_id=f"evt_pg_{sequence}",
        idempotency_key=f"pg:{sequence}",
        sequence=sequence,
        sequence_scope=f"run:{run_id}",
        run_id=run_id,
        span_id=f"{sequence:016x}",
        repository_id="repo_001",
    )
    return event


def test_migrates_from_zero_and_previous_revision_fixture_upgrades(postgres_url) -> None:
    os.environ["VILLANI_CONTROL_PLANE_DATABASE_URL"] = postgres_url
    config = alembic_config(postgres_url)
    command.downgrade(config, "base")
    command.upgrade(config, "4bf1fe1c3274")
    engine = create_engine(postgres_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO organizations(id,name,created_at,updated_at) "
                "VALUES ('previous_fixture','Previous',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM organizations WHERE id='previous_fixture'")
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='ingest_batches' AND column_name='protocol_version'"
                )
            )
            == 1
        )
    engine.dispose()


def test_postgres_uniqueness_rollback_and_tenant_isolation(pg_session) -> None:
    principal = seed_tenant(pg_session)
    service = IngestionService(pg_session)
    assert service.ingest_batch("one", [make_event(1)], principal).inserted == 1
    assert service.ingest_batch("two", [make_event(1)], principal).duplicates == 1

    conflicting = make_event(2)
    conflicting["sequence"] = 1
    with pytest.raises(ConflictError):
        service.ingest_batch("rollback", [make_event(2), conflicting], principal)
    assert pg_session.scalar(select(func.count()).select_from(Event)) == 1
    assert pg_session.scalar(select(func.count()).select_from(Outbox)) == 1
    assert pg_session.scalar(select(func.count()).select_from(IngestBatch)) == 2

    other = seed_tenant(
        pg_session,
        organization_id="org_other",
        workspace_id="workspace_other",
        project_id="project_other",
        repository_id="repo_other",
        token="postgres-other-development-token-long",
    )
    with pytest.raises(NotFoundError):
        RunQueryService(pg_session).get_run("run_pg", other)
    with pytest.raises(AuthorizationError):
        IngestionService(pg_session).ingest_batch(
            "cross", [make_event(3) | {"organization_id": "org_other"}], principal
        )


def test_concurrent_duplicate_batch_creates_one_event(postgres_engine) -> None:
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE outbox,outcomes,artifacts,events,spans,attempts,ingest_batches,"
                "api_tokens,agent_installations,runs,repositories,projects,workspaces,organizations CASCADE"
            )
        )
    with Session(postgres_engine) as session:
        principal = seed_tenant(session)

    def ingest() -> tuple[int, int]:
        with Session(postgres_engine) as session:
            result = IngestionService(session).ingest_batch(
                "concurrent", [make_event(1)], principal
            )
            return result.inserted, result.duplicates

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: ingest(), range(2)))
    assert sorted(results) == [(0, 1), (1, 0)]
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 1
        assert session.scalar(select(func.count()).select_from(Outbox)) == 1


def test_concurrent_outcome_finalization_creates_one_version(postgres_engine) -> None:
    with postgres_engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
    with Session(postgres_engine) as session:
        principal = seed_tenant(session)
        IngestionService(session).ingest_batch("outcome-run", [make_event(1)], principal)
    outcome = load_v2_fixture("outcome.json")
    outcome.update(run_id="run_pg", attempt_id=None, cost=None, currency=None)

    def finalize() -> int:
        with Session(postgres_engine) as session:
            IngestionService(session).record_outcome(outcome, principal)
            return int(
                session.scalar(select(func.count()).select_from(models.Outcome)) or 0
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        versions = list(executor.map(lambda _index: finalize(), range(2)))
    assert versions == [1, 1]
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(models.Outcome)) == 1


def test_outbox_skip_locked_claim_and_expired_lease_recovery(postgres_engine) -> None:
    with postgres_engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
    with Session(postgres_engine) as session:
        principal = seed_tenant(session)
        IngestionService(session).ingest_batch("outbox-run", [make_event(1)], principal)

    def claim(owner: str) -> list[str]:
        with Session(postgres_engine) as session:
            now = models.utc_now()
            rows = list(
                session.scalars(
                    select(models.Outbox)
                    .where(
                        models.Outbox.published_at.is_(None),
                        (models.Outbox.leased_until.is_(None))
                        | (models.Outbox.leased_until < now),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            )
            for row in rows:
                row.lease_owner = owner
                row.leased_until = now + timedelta(seconds=30)
            session.commit()
            return [row.id for row in rows]

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, ("one", "two")))
    assert sum(bool(value) for value in claims) == 1
    with Session(postgres_engine) as session:
        row = session.scalar(select(models.Outbox))
        assert row is not None
        row.leased_until = models.utc_now() - timedelta(seconds=1)
        session.commit()
    assert claim("recovery")


def test_postgres_deletion_workflow_respects_legal_hold(pg_session) -> None:
    principal = seed_tenant(pg_session)
    IngestionService(pg_session).ingest_batch("delete-run", [make_event(1)], principal)
    service = GovernanceService(pg_session)
    hold = service.place_hold(principal, "run", "run_pg", "litigation")
    with pytest.raises(ConflictError, match="legal hold"):
        service.request_deletion(principal, "run", "run_pg")
    hold.active = False
    pg_session.commit()
    workflow = service.request_deletion(principal, "run", "run_pg")
    completed = service.complete_deletion(principal, workflow.id)
    assert completed.state == "completed"
    assert completed.completion_evidence["tombstone_sha256"]


def test_cursor_pagination_and_representative_query_plans_use_indexes(pg_session) -> None:
    principal = seed_tenant(pg_session)
    IngestionService(pg_session).ingest_batch(
        "pagination", [make_event(index) for index in range(1, 8)], principal
    )
    query = RunQueryService(pg_session)
    first = query.events("run_pg", principal, cursor=None, limit=3)
    second = query.events("run_pg", principal, cursor=first.next_cursor, limit=3)
    third = query.events("run_pg", principal, cursor=second.next_cursor, limit=3)
    assert [len(first.events), len(second.events), len(third.events)] == [3, 3, 1]
    pg_session.execute(
        text(
            "INSERT INTO projects(organization_id,id,workspace_id,name,created_at,updated_at) "
            "SELECT :org,'noise_project_'||n,'workspace_1','Noise '||n,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP "
            "FROM generate_series(1,500) n"
        ),
        {"org": principal.organization_id},
    )
    pg_session.execute(
        text(
            "INSERT INTO repositories(organization_id,id,workspace_id,project_id,name,created_at,updated_at) "
            "SELECT :org,'noise_repo_'||n,'workspace_1','noise_project_'||n,'Noise '||n,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM generate_series(1,500) n"
        ),
        {"org": principal.organization_id},
    )
    pg_session.execute(
        text(
            "INSERT INTO runs(organization_id,id,workspace_id,project_id,repository_id,trace_id,status,"
            "first_occurred_at,first_observed_at,last_observed_at,created_at,updated_at) "
            "SELECT :org,'noise_run_'||n,'workspace_1','noise_project_'||n,'noise_repo_'||n,"
            "substr(md5(n::text)||md5(n::text),1,32),'noise',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM generate_series(1,500) n"
        ),
        {"org": principal.organization_id},
    )
    pg_session.execute(text("ANALYZE runs"))
    pg_session.execute(text("SET LOCAL enable_seqscan = off"))
    event_plan = "\n".join(
        row[0]
        for row in pg_session.execute(
            text(
                "EXPLAIN SELECT * FROM events WHERE organization_id=:org AND run_id=:run "
                "ORDER BY observed_at, internal_id LIMIT 100"
            ),
            {"org": principal.organization_id, "run": "run_pg"},
        )
    )
    run_plan = "\n".join(
        row[0]
        for row in pg_session.execute(
            text(
                "EXPLAIN SELECT * FROM runs WHERE organization_id=:org AND project_id='project_1' "
                "AND repository_id='repo_001' AND status='unknown'"
            ),
            {"org": principal.organization_id},
        )
    )
    assert "ix_events_run_cursor" in event_plan
    assert "ix_runs_tenant_filters" in run_plan


def test_two_postgres_workers_racing_cannot_own_the_same_live_lease(postgres_engine) -> None:
    with postgres_engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
    with Session(postgres_engine) as session:
        principal = seed_tenant(session)
        IngestionService(session).ingest_batch("remote-run", [make_event(1)], principal)
        session.add_all(
            [
                models.AgentInstallation(
                    organization_id=principal.organization_id,
                    id=f"installation-{index}",
                    workspace_id=principal.workspace_id,
                    agent_name=f"worker-{index}",
                )
                for index in (1, 2)
            ]
        )
        session.commit()
        service = RemoteDispatchService(session)
        request = RemoteTaskRequest.model_validate(
            {
                "task_id": "race-task",
                "submission_idempotency_key": "race-task-submit",
                "run_id": "run_pg",
                "task_input": {"goal": "race safely"},
                "policy_version": "policy-v1",
                "repository": {"repository_id": "repo_001", "revision": "abc"},
                "required_capabilities": {
                    "platforms": ["linux"],
                    "data_residency_labels": ["au-sydney"],
                },
                "max_attempts": 2,
            }
        )
        service.submit(request, principal)
        worker_capabilities = {
            "platform": "linux",
            "architecture": "x86_64",
            "execution_providers": ["container"],
            "agent_adapters": ["codex"],
            "reachable_models": [],
            "reachable_runtimes": [],
            "cpu_count": 8,
            "memory_bytes": 16 * 1024**3,
            "gpus": [],
            "concurrency": 1,
            "network_class": "restricted",
            "data_residency_labels": ["au-sydney"],
            "version": "test",
        }
        for index in (1, 2):
            worker_principal = Principal(
                f"worker-{index}",
                principal.organization_id,
                principal.workspace_id,
                f"installation-{index}",
            )
            service.heartbeat(f"worker-{index}", worker_capabilities, "online", worker_principal)

    def claim(index: int):
        with Session(postgres_engine) as session:
            worker_principal = Principal(
                f"worker-{index}", "org_1", "workspace_1", f"installation-{index}"
            )
            return RemoteDispatchService(session).claim(f"worker-{index}", worker_principal).task

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, (1, 2)))
    assert sum(value is not None for value in claims) == 1
    with Session(postgres_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(models.TaskLease)
                .where(models.TaskLease.state == "active")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(models.Event)
                .where(models.Event.name == "remote.task.leased")
            )
            == 1
        )
        with pytest.raises(DBAPIError, match="immutable"):
            session.execute(
                text(
                    'UPDATE remote_tasks SET task_input=\'{"goal":"mutated"}\'::jsonb '
                    "WHERE organization_id='org_1' AND id='race-task'"
                )
            )

    winner = 1 if claims[0] is not None else 2
    loser = 2 if winner == 1 else 1
    first = claims[winner - 1]
    assert first is not None
    winner_principal = Principal(
        f"worker-{winner}", "org_1", "workspace_1", f"installation-{winner}"
    )
    loser_principal = Principal(
        f"worker-{loser}", "org_1", "workspace_1", f"installation-{loser}"
    )
    with Session(postgres_engine) as session:
        renewed = RemoteDispatchService(session).renew(
            "race-task", first["lease_id"], winner_principal
        )
        assert renewed["expires_at"]
        lease = session.get(models.TaskLease, ("org_1", first["lease_id"]))
        assert lease is not None
        lease.expires_at = models.utc_now() - timedelta(seconds=1)
        session.commit()
    with Session(postgres_engine) as session:
        first_retry = RemoteDispatchService(session).claim(
            f"worker-{loser}", loser_principal
        ).task
        assert first_retry is None
        task = session.get(models.RemoteTask, ("org_1", "race-task"))
        assert task is not None
        task.next_eligible_at = models.utc_now() - timedelta(seconds=1)
        session.commit()
    with Session(postgres_engine) as session:
        reassigned = RemoteDispatchService(session).claim(
            f"worker-{loser}", loser_principal
        ).task
        assert reassigned is not None
        assert reassigned["lease_id"] != first["lease_id"]
        with pytest.raises(ConflictError):
            RemoteDispatchService(session).complete(
                "race-task",
                first["lease_id"],
                TaskCompletionRequest(
                    idempotency_key="stale-completion",
                    finalization_idempotency_key=first["finalization_idempotency_key"],
                    status="succeeded",
                    materialized=True,
                    finalized=True,
                ),
                winner_principal,
            )
