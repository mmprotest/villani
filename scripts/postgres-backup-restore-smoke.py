#!/usr/bin/env python3
"""Back up and restore a representative Villani PostgreSQL database."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import urllib.parse
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text


def _postgres_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _run(command: list[str], environment: dict[str, str]) -> None:
    subprocess.run(command, env=environment, check=True, capture_output=True, text=True)


def _tool_command(
    tool: str,
    arguments: list[str],
    *,
    environment: dict[str, str],
    container: str | None,
) -> None:
    if not container:
        _run([tool, *arguments], environment)
        return
    command = ["docker", "exec"]
    if environment.get("PGPASSWORD"):
        command.extend(["--env", f"PGPASSWORD={environment['PGPASSWORD']}"])
    command.extend([container, tool, *arguments])
    _run(command, environment)


def _container_url(source_url: str) -> str:
    parsed = urllib.parse.urlsplit(source_url)
    credentials = parsed.username or ""
    if parsed.password:
        credentials += ":" + urllib.parse.quote(urllib.parse.unquote(parsed.password))
    authority = f"{credentials}@127.0.0.1:5432" if credentials else "127.0.0.1:5432"
    return urllib.parse.urlunsplit(
        (parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("VILLANI_TEST_POSTGRES_URL"))
    parser.add_argument(
        "--docker-container",
        default=os.environ.get("VILLANI_POSTGRES_DOCKER_CONTAINER"),
        help="Use PostgreSQL client tools already present in this container.",
    )
    args = parser.parse_args()
    if not args.url:
        raise SystemExit("--url or VILLANI_TEST_POSTGRES_URL is required")
    source_url = _postgres_url(args.url)
    parsed = urllib.parse.urlsplit(source_url)
    restored_name = f"villani_restore_{uuid.uuid4().hex[:12]}"
    restored_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{restored_name}", "", "")
    )
    environment = os.environ.copy()
    if parsed.password:
        environment["PGPASSWORD"] = urllib.parse.unquote(parsed.password)
    tool_source_url = (
        _container_url(source_url) if args.docker_container else source_url
    )
    tool_restored_url = (
        _container_url(restored_url) if args.docker_container else restored_url
    )
    with tempfile.TemporaryDirectory(prefix="villani-pg-backup-") as temporary:
        backup = (
            f"/tmp/villani-{uuid.uuid4().hex}.dump"
            if args.docker_container
            else str(Path(temporary) / "representative.dump")
        )
        _tool_command(
            "pg_dump",
            ["--format=custom", "--file", backup, tool_source_url],
            environment=environment,
            container=args.docker_container,
        )
        _tool_command(
            "createdb",
            ["--maintenance-db", tool_source_url, restored_name],
            environment=environment,
            container=args.docker_container,
        )
        try:
            _tool_command(
                "pg_restore",
                ["--dbname", tool_restored_url, backup],
                environment=environment,
                container=args.docker_container,
            )
            source = create_engine(args.url)
            restored = create_engine(args.url.rsplit("/", 1)[0] + f"/{restored_name}")
            with (
                source.connect() as source_connection,
                restored.connect() as restored_connection,
            ):
                source_tables = source_connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
                    )
                )
                restored_tables = restored_connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
                    )
                )
                source_runs = source_connection.scalar(
                    text("SELECT count(*) FROM runs")
                )
                restored_runs = restored_connection.scalar(
                    text("SELECT count(*) FROM runs")
                )
            source.dispose()
            restored.dispose()
            if source_tables != restored_tables or source_runs != restored_runs:
                raise SystemExit(
                    "restored database does not match representative source counts"
                )
        finally:
            _tool_command(
                "dropdb",
                ["--maintenance-db", tool_source_url, restored_name],
                environment=environment,
                container=args.docker_container,
            )
            if args.docker_container:
                _tool_command(
                    "rm",
                    ["-f", backup],
                    environment=environment,
                    container=args.docker_container,
                )
    print("PostgreSQL representative backup/restore: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
