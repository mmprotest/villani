from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import Settings


class OperationsService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def migration_state(self) -> dict[str, str | bool | None]:
        component_root = Path(__file__).resolve().parents[2]
        config = Config(str(component_root / "alembic.ini"))
        config.set_main_option("script_location", str(component_root / "alembic"))
        head = ScriptDirectory.from_config(config).get_current_head()
        try:
            current = MigrationContext.configure(self.session.connection()).get_current_revision()
        except SQLAlchemyError:
            self.session.rollback()
            return {"current": None, "head": head, "up_to_date": False, "status": "unavailable"}
        return {"current": current, "head": head, "up_to_date": current == head}

    def readiness(self) -> dict[str, str | bool | None]:
        try:
            self.session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            self.session.rollback()
            state = self.migration_state()
            return {**state, "status": "not_ready"}
        state = self.migration_state()
        return {**state, "status": "ready" if state["up_to_date"] else "not_ready"}
