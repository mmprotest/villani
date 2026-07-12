from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router
from .config import get_settings
from .database import SessionFactory
from .errors import ServiceError
from .live import outbox_worker
from .metrics import OTLPHTTPMetricsExporter, metrics
from .services import DevelopmentBootstrapService


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    if settings.air_gapped and settings.otlp_endpoint:
        raise RuntimeError("OTLP export is disabled in air-gapped mode")
    otlp = OTLPHTTPMetricsExporter(settings.otlp_endpoint) if settings.otlp_endpoint else None
    metrics.exporter = otlp
    with SessionFactory() as session:
        DevelopmentBootstrapService(session, settings).bootstrap()
    stop = asyncio.Event()
    worker = asyncio.create_task(outbox_worker(stop))
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(worker, timeout=get_settings().graceful_shutdown_seconds)
        except TimeoutError:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if otlp:
            otlp.shutdown()
        metrics.exporter = None


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

    @app.middleware("http")
    async def observe_request(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        metrics.add(
            "villani_http_requests_total",
            method=request.method,
            route=request.url.path,
            status=str(response.status_code),
        )
        metrics.add(
            "villani_http_request_duration_ms_total",
            (time.perf_counter() - started) * 1000,
            method=request.method,
        )
        return response

    app.include_router(router)
    return app


app = create_app()
