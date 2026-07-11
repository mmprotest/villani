from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from .. import models


class TenantRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def workspace_exists(self, organization_id: str, workspace_id: str) -> bool:
        return self.session.get(models.Workspace, (organization_id, workspace_id)) is not None

    def project(self, organization_id: str, project_id: str) -> models.Project | None:
        return self.session.get(models.Project, (organization_id, project_id))

    def repository(self, organization_id: str, repository_id: str) -> models.Repository | None:
        return self.session.get(models.Repository, (organization_id, repository_id))

    def add_default_project_repository(
        self, organization_id: str, workspace_id: str
    ) -> tuple[models.Project, models.Repository]:
        project_id = f"{workspace_id}:local"
        repository_id = f"{workspace_id}:local"
        project = self.project(organization_id, project_id)
        if project is None:
            project = models.Project(
                organization_id=organization_id,
                workspace_id=workspace_id,
                id=project_id,
                name="Local runs",
            )
            self.session.add(project)
            self.session.flush()
        repository = self.repository(organization_id, repository_id)
        if repository is None:
            repository = models.Repository(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
                id=repository_id,
                name="Local repository",
            )
            self.session.add(repository)
            self.session.flush()
        return project, repository

    def add_local_repository(
        self, organization_id: str, workspace_id: str, project: models.Project
    ) -> models.Repository:
        repository_id = f"{project.id}:local"
        repository = self.repository(organization_id, repository_id)
        if repository is None:
            repository = models.Repository(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project.id,
                id=repository_id,
                name="Local repository",
            )
            self.session.add(repository)
            self.session.flush()
        return repository


class IngestionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def batch(self, organization_id: str, batch_id: str) -> models.IngestBatch | None:
        return self.session.get(models.IngestBatch, (organization_id, batch_id))

    def event_by_event_id(self, organization_id: str, event_id: str) -> models.Event | None:
        return self.session.scalar(
            select(models.Event).where(
                models.Event.organization_id == organization_id,
                models.Event.event_id == event_id,
            )
        )

    def event_by_idempotency(
        self, organization_id: str, idempotency_key: str
    ) -> models.Event | None:
        return self.session.scalar(
            select(models.Event).where(
                models.Event.organization_id == organization_id,
                models.Event.idempotency_key == idempotency_key,
            )
        )

    def run(self, organization_id: str, run_id: str) -> models.Run | None:
        return self.session.get(models.Run, (organization_id, run_id))

    def add(self, value: object) -> None:
        self.session.add(value)

    def flush(self) -> None:
        self.session.flush()


class QueryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_run(self, organization_id: str, run_id: str) -> models.Run | None:
        return self.session.scalar(
            select(models.Run).where(
                models.Run.organization_id == organization_id,
                models.Run.id == run_id,
                models.Run.deleted_at.is_(None),
            )
        )

    def attempts(self, organization_id: str, run_id: str) -> list[models.Attempt]:
        return list(
            self.session.scalars(
                select(models.Attempt)
                .where(
                    models.Attempt.organization_id == organization_id,
                    models.Attempt.run_id == run_id,
                )
                .order_by(models.Attempt.id)
            )
        )

    def outcomes(self, organization_id: str, run_id: str) -> list[models.Outcome]:
        return list(
            self.session.scalars(
                select(models.Outcome)
                .where(
                    models.Outcome.organization_id == organization_id,
                    models.Outcome.run_id == run_id,
                )
                .order_by(models.Outcome.created_at, models.Outcome.id)
            )
        )

    def artifact_count(self, organization_id: str, run_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(models.Artifact)
                .where(
                    models.Artifact.organization_id == organization_id,
                    models.Artifact.run_id == run_id,
                )
            )
            or 0
        )

    def events(
        self,
        organization_id: str,
        run_id: str,
        *,
        limit: int,
        after: tuple[datetime, int] | None,
    ) -> list[models.Event]:
        query = select(models.Event).where(
            models.Event.organization_id == organization_id,
            models.Event.run_id == run_id,
        )
        if after is not None:
            observed_at, internal_id = after
            query = query.where(
                or_(
                    models.Event.observed_at > observed_at,
                    and_(
                        models.Event.observed_at == observed_at,
                        models.Event.internal_id > internal_id,
                    ),
                )
            )
        return list(
            self.session.scalars(
                query.order_by(models.Event.observed_at, models.Event.internal_id).limit(limit)
            )
        )

    def list_runs(
        self,
        organization_id: str,
        *,
        project_id: str | None,
        repository_id: str | None,
        status: str | None,
        started_after: datetime | None,
        started_before: datetime | None,
        limit: int,
    ) -> list[models.Run]:
        query: Select[tuple[models.Run]] = select(models.Run).where(
            models.Run.organization_id == organization_id,
            models.Run.deleted_at.is_(None),
        )
        if project_id:
            query = query.where(models.Run.project_id == project_id)
        if repository_id:
            query = query.where(models.Run.repository_id == repository_id)
        if status:
            query = query.where(models.Run.status == status)
        if started_after:
            query = query.where(models.Run.first_observed_at >= started_after)
        if started_before:
            query = query.where(models.Run.first_observed_at < started_before)
        return list(
            self.session.scalars(
                query.order_by(models.Run.last_observed_at.desc(), models.Run.id).limit(limit)
            )
        )
