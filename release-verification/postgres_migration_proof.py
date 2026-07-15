#!/usr/bin/env python3
"""Prove the composite-attempt migration against populated PostgreSQL data."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.orm import Session

from villani_control_plane.errors import NotFoundError
from villani_control_plane.security import Principal
from villani_control_plane.services import RunQueryService


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components" / "villani-control-plane"
PRE_COMPOSITE = "f9a0b1c2d3e4"
HEAD = "0a1b2c3d4e5f"


def _schema_url(url: str, schema: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _config(url: str) -> Config:
    config = Config(str(COMPONENT / "alembic.ini"))
    config.set_main_option("script_location", str(COMPONENT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _fill(table: Table, values: dict[str, Any], marker: str) -> dict[str, Any]:
    """Fill only genuinely required reflected columns with deterministic values."""
    result = dict(values)
    for column in table.columns:
        if column.name in result or column.nullable or column.autoincrement is True:
            continue
        if column.server_default is not None or column.default is not None:
            continue
        kind = column.type
        if isinstance(kind, sa.DateTime):
            result[column.name] = datetime(2026, 1, 1, tzinfo=timezone.utc)
        elif isinstance(kind, (sa.JSON,)):
            result[column.name] = {}
        elif isinstance(kind, sa.Boolean):
            result[column.name] = False
        elif isinstance(kind, (sa.Integer, sa.BigInteger)):
            result[column.name] = 1
        elif isinstance(kind, (sa.Float, sa.Numeric)):
            result[column.name] = 0
        elif isinstance(kind, (sa.String, sa.Text)):
            limit = getattr(kind, "length", None)
            result[column.name] = f"{marker}_{column.name}"[:limit]
        else:  # pragma: no cover - migration additions must be deliberately supported
            raise TypeError(
                f"unsupported required column {table.name}.{column.name}: {kind}"
            )
    return result


def _insert(
    connection: sa.Connection,
    metadata: MetaData,
    table: str,
    values: dict[str, Any],
    marker: str,
) -> None:
    connection.execute(
        metadata.tables[table]
        .insert()
        .values(**_fill(metadata.tables[table], values, marker))
    )


def _tenant_rows(
    connection: sa.Connection, metadata: MetaData, *, suffix: str
) -> dict[str, str]:
    org = f"org_{suffix}"
    workspace = f"workspace_{suffix}"
    project = f"project_{suffix}"
    repository = f"repository_{suffix}"
    run = f"run_{suffix}"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _insert(
        connection,
        metadata,
        "organizations",
        {"id": org, "name": f"Organization {suffix}"},
        suffix,
    )
    _insert(
        connection,
        metadata,
        "workspaces",
        {"organization_id": org, "id": workspace, "name": f"Workspace {suffix}"},
        suffix,
    )
    _insert(
        connection,
        metadata,
        "projects",
        {
            "organization_id": org,
            "id": project,
            "workspace_id": workspace,
            "name": f"Project {suffix}",
        },
        suffix,
    )
    _insert(
        connection,
        metadata,
        "repositories",
        {
            "organization_id": org,
            "id": repository,
            "workspace_id": workspace,
            "project_id": project,
            "name": f"Repository {suffix}",
        },
        suffix,
    )
    _insert(
        connection,
        metadata,
        "runs",
        {
            "organization_id": org,
            "id": run,
            "workspace_id": workspace,
            "project_id": project,
            "repository_id": repository,
            "trace_id": (suffix * 32)[:32],
            "status": "completed",
            "first_occurred_at": now,
            "first_observed_at": now,
            "last_observed_at": now,
        },
        suffix,
    )
    _insert(
        connection,
        metadata,
        "attempts",
        {
            "organization_id": org,
            "id": "attempt_001",
            "run_id": run,
            "status": "completed",
        },
        suffix,
    )
    _insert(
        connection,
        metadata,
        "events",
        {
            "organization_id": org,
            "event_id": f"event_{suffix}",
            "idempotency_key": f"migration:{suffix}",
            "workspace_id": workspace,
            "project_id": project,
            "repository_id": repository,
            "run_id": run,
            "attempt_id": "attempt_001",
            "trace_id": (suffix * 32)[:32],
            "span_id": (suffix * 16)[:16],
            "sequence_scope": f"run:{run}",
            "sequence": 1,
            "occurred_at": now,
            "observed_at": now,
            "source": "migration-proof",
            "kind": "control",
            "name": "attempt.completed",
            "status": "ok",
            "payload_sha256": (suffix * 64)[:64],
            "document": {"attempt_id": "attempt_001"},
        },
        suffix,
    )
    _insert(
        connection,
        metadata,
        "outcomes",
        {
            "organization_id": org,
            "id": f"outcome_{suffix}",
            "workspace_id": workspace,
            "run_id": run,
            "attempt_id": "attempt_001",
            "attempt_key": "attempt_001",
            "document": {"status": "succeeded", "attempt_id": "attempt_001"},
        },
        suffix,
    )
    _insert(
        connection,
        metadata,
        "artifacts",
        {
            "organization_id": org,
            "id": f"artifact_{suffix}",
            "workspace_id": workspace,
            "run_id": run,
            "digest_sha256": hashlib_sha256(f"artifact:{suffix}"),
            "size_bytes": 4,
            "status": "available",
            "object_key": f"migration/{suffix}",
            "document": {"kind": "safe"},
        },
        suffix,
    )
    if "policy_publications" in metadata.tables:
        _insert(
            connection,
            metadata,
            "policy_publications",
            {
                "organization_id": org,
                "id": f"policy_{suffix}",
                "workspace_id": workspace,
                "policy_id": "routing",
                "policy_version": f"v-{suffix}",
                "policy_snapshot": {"version": suffix},
                "snapshot_sha256": hashlib_sha256(f"policy:{suffix}"),
                "canary_percentage": 100.0,
                "rollback_thresholds": {},
                "evaluation_provenance": {"source": "migration-proof"},
                "manual_approval_required": False,
                "created_by": "migration-proof",
            },
            suffix,
        )
    return {
        "organization": org,
        "workspace": workspace,
        "project": project,
        "repository": repository,
        "run": run,
    }


def hashlib_sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _count(connection: sa.Connection, table: str) -> int:
    return int(connection.scalar(text(f'SELECT count(*) FROM "{table}"')) or 0)


def prove(database_url: str) -> dict[str, Any]:
    schema = f"villani_migration_proof_{os.getpid()}"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    proof_url = _schema_url(database_url, schema)
    engine = create_engine(proof_url, pool_pre_ping=True)
    previous_database_url = os.environ.get("VILLANI_CONTROL_PLANE_DATABASE_URL")
    # Alembic's ConfigParser requires literal percent signs to be doubled.  The
    # environment override in alembic/env.py is therefore supplied in escaped
    # form while SQLAlchemy receives the normal URL directly above.
    os.environ["VILLANI_CONTROL_PLANE_DATABASE_URL"] = proof_url.replace("%", "%%")
    seeded_tables = (
        "organizations",
        "workspaces",
        "projects",
        "repositories",
        "runs",
        "attempts",
        "events",
        "outcomes",
        "artifacts",
        "policy_publications",
    )
    try:
        with engine.connect() as connection:
            server_version = str(connection.scalar(text("SHOW server_version")))
            server_version_number = int(
                str(connection.scalar(text("SHOW server_version_num")))
            )
        config = _config(proof_url)
        command.upgrade(config, PRE_COMPOSITE)
        before_metadata = MetaData()
        before_metadata.reflect(engine)
        with engine.begin() as connection:
            first = _tenant_rows(connection, before_metadata, suffix="a")
            second = _tenant_rows(connection, before_metadata, suffix="b")
            before = {
                table: _count(connection, table)
                for table in seeded_tables
                if table in before_metadata.tables
            }
        command.upgrade(config, "head")
        after_metadata = MetaData()
        after_metadata.reflect(engine)
        with engine.begin() as connection:
            after = {table: _count(connection, table) for table in before}
            if before != after:
                raise AssertionError(
                    f"migration data loss: before={before}, after={after}"
                )
            attempts = after_metadata.tables["attempts"]
            primary_key = list(
                inspect(engine).get_pk_constraint("attempts")["constrained_columns"]
            )
            if primary_key != ["organization_id", "run_id", "id"]:
                raise AssertionError(f"unexpected attempt primary key: {primary_key}")
            # The same organization can now safely reuse the public canonical ID in a second run.
            run_table = after_metadata.tables["runs"]
            original = (
                connection.execute(
                    select(run_table).where(run_table.c.id == first["run"])
                )
                .mappings()
                .one()
            )
            second_run = "run_a_second"
            run_values = {
                key: value
                for key, value in original.items()
                if key not in {"id", "canonical_projection", "created_at", "updated_at"}
            }
            run_values["id"] = second_run
            run_values["trace_id"] = "c" * 32
            connection.execute(
                run_table.insert().values(**_fill(run_table, run_values, "a_second"))
            )
            connection.execute(
                attempts.insert().values(
                    **_fill(
                        attempts,
                        {
                            "organization_id": first["organization"],
                            "run_id": second_run,
                            "id": "attempt_001",
                            "status": "completed",
                        },
                        "a_second",
                    )
                )
            )
            repeated = [
                dict(row)
                for row in connection.execute(
                    select(
                        attempts.c.organization_id, attempts.c.run_id, attempts.c.id
                    ).where(attempts.c.id == "attempt_001")
                ).mappings()
            ]
            joined_outcomes = int(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM outcomes o JOIN runs r "
                        "ON r.organization_id=o.organization_id AND r.id=o.run_id "
                        "JOIN attempts a ON a.organization_id=o.organization_id "
                        "AND a.run_id=o.run_id AND a.id=o.attempt_id"
                    )
                )
                or 0
            )
            canonical_projection_defaults = int(
                connection.scalar(
                    text("SELECT count(*) FROM runs WHERE canonical_projection IS NULL")
                )
                or 0
            )
        with Session(engine) as session:
            principal = Principal(
                "migration-proof", first["organization"], first["workspace"], None
            )
            try:
                RunQueryService(session).get_run(second["run"], principal)
            except NotFoundError:
                tenant_isolation = True
            else:
                tenant_isolation = False
        with engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        checks = {
            "postgresql_16": 160000 <= server_version_number < 170000,
            "fresh_schema_created": True,
            "pre_composite_revision_populated": bool(before),
            "no_data_loss": before == after,
            "multiple_organizations": before.get("organizations") == 2,
            "multiple_runs": before.get("runs") == 2,
            "events_preserved": before.get("events") == 2,
            "outcomes_preserved": before.get("outcomes") == 2,
            "artifacts_preserved": before.get("artifacts") == 2,
            "policy_records_preserved": before.get("policy_publications") == 2,
            "composite_attempt_primary_key": primary_key
            == ["organization_id", "run_id", "id"],
            "repeated_public_attempt_ids": len(repeated) == 3
            and all(row["id"] == "attempt_001" for row in repeated),
            "outcome_relationships_valid": joined_outcomes == 2,
            "tenant_isolation": tenant_isolation,
            "canonical_projection_non_null": canonical_projection_defaults == 0,
            "alembic_head_matches": revision == HEAD,
        }
        return {
            "status": "passed" if all(checks.values()) else "failed",
            "database": "postgresql",
            "server_version": server_version,
            "server_version_number": server_version_number,
            "pre_composite_revision": PRE_COMPOSITE,
            "alembic_head": revision,
            "before_counts": before,
            "after_counts": after,
            "attempt_primary_key": primary_key,
            "repeated_attempt_rows": repeated,
            "checks": checks,
        }
    finally:
        if previous_database_url is None:
            os.environ.pop("VILLANI_CONTROL_PLANE_DATABASE_URL", None)
        else:
            os.environ["VILLANI_CONTROL_PLANE_DATABASE_URL"] = previous_database_url
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = prove(args.database_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
