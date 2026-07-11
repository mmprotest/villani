from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from alembic.config import Config
from conftest import load_v2_fixture, seed_tenant
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from alembic import command
from villani_control_plane.models import Event
from villani_control_plane.services import IngestionService

pytestmark = [pytest.mark.postgres, pytest.mark.load]
EVENT_COUNT = 100_000
COMPONENT_ROOT = Path(__file__).resolve().parents[2]


def test_load_100000_events_records_throughput_and_database_size(
    postgres_url, request, record_property
) -> None:
    if not request.config.getoption("--run-load-smoke"):
        pytest.skip("pass --run-load-smoke to execute the 100,000-event smoke")
    os.environ["VILLANI_CONTROL_PLANE_DATABASE_URL"] = postgres_url
    config = Config(str(COMPONENT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(COMPONENT_ROOT / "alembic"))
    command.upgrade(config, "head")
    engine = create_engine(postgres_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE outbox,outcomes,artifacts,events,spans,attempts,ingest_batches,"
                "api_tokens,agent_installations,runs,repositories,projects,workspaces,organizations CASCADE"
            )
        )
    fixture = load_v2_fixture("telemetry-envelope.json")
    started = time.perf_counter()
    with Session(engine) as session:
        principal = seed_tenant(session)
        for batch_start in range(0, EVENT_COUNT, 1000):
            events = []
            for offset in range(1000):
                sequence = batch_start + offset + 1
                event = dict(fixture)
                event.update(
                    event_id=f"load_evt_{sequence}",
                    idempotency_key=f"load:{sequence}",
                    sequence=sequence,
                    span_id=f"{sequence:016x}",
                )
                events.append(event)
            IngestionService(session).ingest_batch(f"load_batch_{batch_start}", events, principal)
        assert session.scalar(select(func.count()).select_from(Event)) == EVENT_COUNT
        database_size = session.scalar(text("SELECT pg_database_size(current_database())"))
    duration = time.perf_counter() - started
    throughput = EVENT_COUNT / duration
    record_property("events", EVENT_COUNT)
    record_property("duration_seconds", round(duration, 3))
    record_property("events_per_second", round(throughput, 1))
    record_property("database_size_bytes", int(database_size))
    print(
        f"load_smoke events={EVENT_COUNT} duration_seconds={duration:.3f} "
        f"events_per_second={throughput:.1f} database_size_bytes={database_size}"
    )
    engine.dispose()
