from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router
from .config import get_settings
from .database import SessionFactory
from .errors import ServiceError
from .live import outbox_worker
from .services import DevelopmentBootstrapService


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with SessionFactory() as session:
        DevelopmentBootstrapService(session, get_settings()).bootstrap()
    stop = asyncio.Event()
    worker = asyncio.create_task(outbox_worker(stop))
    try:
        yield
    finally:
        stop.set()
        await worker


def create_app() -> FastAPI:
    app = FastAPI(
        title="Villani Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, error: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": error.code, "message": str(error)},
            headers=getattr(error, "headers", None),
        )

    app.include_router(router)
    return app


app = create_app()
