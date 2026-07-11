from __future__ import annotations

import base64
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..errors import NotFoundError, ServiceError
from ..repositories import QueryRepository
from ..schemas import EventPage, RunDetail, RunList, RunSummary
from ..security import Principal


def _run_summary(run) -> RunSummary:
    return RunSummary(
        id=run.id,
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        repository_id=run.repository_id,
        trace_id=run.trace_id,
        status=run.status,
        first_occurred_at=run.first_occurred_at,
        first_observed_at=run.first_observed_at,
        last_observed_at=run.last_observed_at,
    )


def encode_cursor(observed_at: datetime, internal_id: int) -> str:
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    raw = f"{observed_at.isoformat()}|{internal_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        timestamp, internal_id = raw.rsplit("|", 1)
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value, int(internal_id)
    except (ValueError, UnicodeDecodeError) as error:
        raise ServiceError("invalid event cursor") from error


class RunQueryService:
    def __init__(self, session: Session) -> None:
        self.repository = QueryRepository(session)

    def get_run(self, run_id: str, principal: Principal) -> RunDetail:
        run = self.repository.get_run(principal.organization_id, run_id)
        if run is None or run.workspace_id != principal.workspace_id:
            raise NotFoundError("run not found")
        summary = _run_summary(run)
        attempts = [
            {"id": attempt.id, "status": attempt.status}
            for attempt in self.repository.attempts(principal.organization_id, run_id)
        ]
        outcomes = [
            outcome.document
            for outcome in self.repository.outcomes(principal.organization_id, run_id)
        ]
        return RunDetail(
            **summary.model_dump(),
            attempts=attempts,
            outcomes=outcomes,
            artifact_count=self.repository.artifact_count(principal.organization_id, run_id),
        )

    def events(
        self, run_id: str, principal: Principal, *, cursor: str | None, limit: int
    ) -> EventPage:
        run = self.repository.get_run(principal.organization_id, run_id)
        if run is None or run.workspace_id != principal.workspace_id:
            raise NotFoundError("run not found")
        rows = self.repository.events(
            principal.organization_id,
            run_id,
            limit=limit + 1,
            after=decode_cursor(cursor) if cursor else None,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1].observed_at, page[-1].internal_id) if has_more and page else None
        )
        return EventPage(events=[row.document for row in page], next_cursor=next_cursor)

    def list_runs(
        self,
        principal: Principal,
        *,
        project_id: str | None,
        repository_id: str | None,
        status: str | None,
        started_after: datetime | None,
        started_before: datetime | None,
        limit: int,
    ) -> RunList:
        rows = self.repository.list_runs(
            principal.organization_id,
            project_id=project_id,
            repository_id=repository_id,
            status=status,
            started_after=started_after,
            started_before=started_before,
            limit=limit,
        )
        rows = [row for row in rows if row.workspace_id == principal.workspace_id]
        return RunList(runs=[_run_summary(row) for row in rows])
