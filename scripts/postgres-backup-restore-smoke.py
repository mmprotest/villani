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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("VILLANI_TEST_POSTGRES_URL"))
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
    with tempfile.TemporaryDirectory(prefix="villani-pg-backup-") as temporary:
        backup = Path(temporary) / "representative.dump"
        _run(["pg_dump", "--format=custom", "--file", str(backup), source_url], environment)
        _run(["createdb", "--maintenance-db", source_url, restored_name], environment)
        try:
            _run(["pg_restore", "--dbname", restored_url, str(backup)], environment)
            source = create_engine(args.url)
            restored = create_engine(args.url.rsplit("/", 1)[0] + f"/{restored_name}")
            with source.connect() as source_connection, restored.connect() as restored_connection:
                source_tables = source_connection.scalar(
                    text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
                )
                restored_tables = restored_connection.scalar(
                    text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
                )
                source_runs = source_connection.scalar(text("SELECT count(*) FROM runs"))
                restored_runs = restored_connection.scalar(text("SELECT count(*) FROM runs"))
            source.dispose()
            restored.dispose()
            if source_tables != restored_tables or source_runs != restored_runs:
                raise SystemExit("restored database does not match representative source counts")
        finally:
            _run(["dropdb", "--maintenance-db", source_url, restored_name], environment)
    print("PostgreSQL representative backup/restore: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
