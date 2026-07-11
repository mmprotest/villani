from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def create_database_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    engine = create_engine(url, pool_pre_ping=True)
    if make_url(url).get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = create_database_engine()
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    with SessionFactory() as session:
        yield session
