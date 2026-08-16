from __future__ import annotations

import collections
import logging
import os
import secrets
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from spatial_probe_atlas import __version__
from spatial_probe_atlas.adapters.camera import CameraService
from spatial_probe_atlas.adapters.filesystem import ArtifactStore
from spatial_probe_atlas.adapters.persistence import Database
from spatial_probe_atlas.api import api_router, root_router
from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.jobs import JobCoordinator
from spatial_probe_atlas.observability import configure_logging, log_event
from spatial_probe_atlas.pipelines.tracking.runtime import set_runtime_container
from spatial_probe_atlas.services import Catalog
from spatial_probe_atlas.settings import Settings


logger = logging.getLogger("spatial_probe_atlas.app")


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(f"Data root is already locked: {self.path}") from exc

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class Container:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()
        self.database = Database(settings.database_url)
        self.artifacts = ArtifactStore(settings.data_root)
        self.catalog = Catalog(self.database, self.artifacts)
        self.camera = CameraService()
        self.jobs = JobCoordinator(self.database, self.catalog, self.artifacts)
        self.session_secret = secrets.token_urlsafe(32)
        self.bootstrap_consumed = False
        self.tracking_snapshots: dict[str, dict[str, Any]] = {}
        self.tracking_sequences: dict[str, int] = {}
        self.active_paths: dict[str, dict[str, Any]] = {}
        # Rolling buffer of recently tracked probe tip positions per session.
        # Only frames where probe_state == "tracked" are stored.
        # Each entry: {"t": monotonic_ns, "session_id": str, "tip_w_m": [x,y,z]}
        self.probe_tip_buffer: collections.deque = collections.deque(maxlen=2000)
        self.lock = InstanceLock(settings.data_root / "instance.lock")
        set_runtime_container(self)


def _error_payload(error: AppError, trace_id: str) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
            "trace_id": trace_id,
            "retryable": error.retryable,
            "suggested_action": error.suggested_action,
        }
    }


def _authority_parts(value: str) -> tuple[str | None, int | None]:
    try:
        parsed = urlsplit(f"//{value}")
        return parsed.hostname.lower() if parsed.hostname else None, parsed.port
    except ValueError:
        return None, None


def _host_allowed(value: str, settings: Settings) -> bool:
    if settings.allow_test_host and value.lower() == "testserver":
        return True
    host, port = _authority_parts(value)
    return host in {"127.0.0.1", "localhost", "::1"} and port == settings.port


def _origin_allowed(value: str | None, settings: Settings) -> bool:
    if not value:
        return True
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == settings.port
    except ValueError:
        return False


def create_app(settings: Settings | None = None, *, acquire_lock: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    container = Container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        locked = False
        coordinator_started = False
        try:
            if acquire_lock:
                container.lock.acquire()
                locked = True
            # Migrations mutate the shared catalog and therefore may only run while this
            # process owns the selected data-root lock.
            container.database.migrate()
            configure_logging(settings.data_root, settings.log_level)
            log_event(logger, "application.starting", "Starting Spatial Probe Atlas", compute_mode=settings.compute_profile)
            coordinator_started = True
            await container.jobs.start()
            instance = {
                "url": f"http://{settings.host}:{settings.port}",
                "port": settings.port,
                "pid": os.getpid(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "version": __version__,
            }
            container.artifacts.atomic_write_json(settings.data_root / "instance.json", instance)
            log_event(logger, "application.ready", "Spatial Probe Atlas is ready", compute_mode=settings.compute_profile)
            yield
        finally:
            log_event(logger, "application.stopping", "Stopping Spatial Probe Atlas", compute_mode=settings.compute_profile)
            try:
                await container.camera.disconnect()
            finally:
                try:
                    if coordinator_started:
                        await container.jobs.shutdown()
                finally:
                    try:
                        (settings.data_root / "instance.json").unlink(missing_ok=True)
                    except OSError:
                        pass
                    log_event(logger, "application.stopped", "Spatial Probe Atlas stopped", compute_mode=settings.compute_profile)
                    container.database.engine.dispose()
                    if locked:
                        container.lock.release()

    app = FastAPI(
        title="Spatial Probe Atlas",
        version=__version__,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.container = container

    @app.middleware("http")
    async def local_security(request: Request, call_next: Any):
        started = time.perf_counter()
        trace_id = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex[:16]
        request.state.trace_id = trace_id
        response = None
        status_code = 500
        error_code: str | None = None
        try:
            if not _host_allowed(request.headers.get("host", ""), settings):
                error_code = "HOST_NOT_ALLOWED"
                response = JSONResponse(
                    status_code=400,
                    content=_error_payload(
                        AppError("HOST_NOT_ALLOWED", "Spatial Probe Atlas accepts the selected loopback authority only.", status_code=400),
                        trace_id,
                    ),
                )
            elif not _origin_allowed(request.headers.get("origin"), settings):
                error_code = "ORIGIN_NOT_ALLOWED"
                response = JSONResponse(
                    status_code=403,
                    content=_error_payload(AppError("ORIGIN_NOT_ALLOWED", "Cross-origin requests are disabled.", status_code=403), trace_id),
                )
            else:
                protected = request.url.path.startswith("/api/") and not request.url.path.startswith("/api/v1/health/")
                host_str = (request.headers.get("host") or "").lower()
                is_loopback = any(host_str.startswith(prefix) for prefix in ("127.0.0.1", "localhost", "[::1]", "::1"))
                if settings.bootstrap_token and protected and not is_loopback and request.cookies.get("spa_session") != container.session_secret:
                    error_code = "BOOTSTRAP_SESSION_REQUIRED"
                    response = JSONResponse(
                        status_code=401,
                        content=_error_payload(
                            AppError("BOOTSTRAP_SESSION_REQUIRED", "Open the one-time bootstrap URL first.", status_code=401),
                            trace_id,
                        ),
                    )
                else:
                    response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Trace-ID"] = trace_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            return response
        finally:
            parts = [part for part in request.url.path.split("/") if part]
            project_id = parts[parts.index("projects") + 1] if "projects" in parts and parts.index("projects") + 1 < len(parts) else None
            session_id = parts[parts.index("sessions") + 1] if "sessions" in parts and parts.index("sessions") + 1 < len(parts) else None
            log_event(
                logger,
                "http.request.completed",
                f"{request.method} {request.url.path} -> {status_code}",
                level=logging.ERROR if status_code >= 500 else logging.WARNING if status_code >= 400 else logging.INFO,
                trace_id=trace_id,
                correlation_id=trace_id,
                project_id=project_id,
                session_id=session_id,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
                compute_mode=settings.compute_profile,
                error_code=error_code,
                method=request.method,
                path=request.url.path,
                status_code=status_code,
            )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, error: AppError) -> JSONResponse:
        log_event(logger, "http.application_error", error.message, level=logging.ERROR if error.status_code >= 500 else logging.WARNING, trace_id=getattr(request.state, "trace_id", None), correlation_id=getattr(request.state, "trace_id", None), error_code=error.code, status_code=error.status_code)
        return JSONResponse(
            status_code=error.status_code,
            content=_error_payload(error, getattr(request.state, "trace_id", uuid.uuid4().hex[:16])),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, error: RequestValidationError) -> JSONResponse:
        log_event(logger, "http.validation_error", "Request validation failed", level=logging.WARNING, trace_id=getattr(request.state, "trace_id", None), correlation_id=getattr(request.state, "trace_id", None), error_code="REQUEST_VALIDATION_FAILED", field_error_count=len(error.errors()))
        app_error = AppError(
            "REQUEST_VALIDATION_FAILED",
            "The request did not pass validation.",
            status_code=422,
            details={"field_errors": [{"path": ".".join(map(str, item["loc"])), "message": item["msg"]} for item in error.errors()]},
        )
        return JSONResponse(
            status_code=422,
            content=_error_payload(app_error, getattr(request.state, "trace_id", uuid.uuid4().hex[:16])),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex[:16])
        log_event(logger, "http.unexpected_error", "Unexpected request failure", level=logging.ERROR, exc_info=(type(error), error, error.__traceback__), trace_id=trace_id, correlation_id=trace_id, error_code="INTERNAL_ERROR")
        app_error = AppError(
            "INTERNAL_ERROR",
            "An unexpected local error occurred.",
            status_code=500,
            retryable=True,
            suggested_action="Retry once, then open Diagnostics with the trace ID.",
        )
        return JSONResponse(status_code=500, content=_error_payload(app_error, trace_id))

    app.include_router(root_router)
    app.include_router(api_router)

    @app.api_route(
        "/api",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/{unmatched_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    def api_fallback(unmatched_path: str = "") -> None:
        raise AppError(
            "API_ROUTE_NOT_FOUND",
            "The requested API route does not exist.",
            status_code=404,
            details={"path": f"/api/{unmatched_path}" if unmatched_path else "/api"},
        )

    frontend = settings.frontend_dist
    if frontend and frontend.is_dir():
        assets = frontend / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa_fallback(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/"):
                raise AppError(
                    "API_ROUTE_NOT_FOUND",
                    "The requested API route does not exist.",
                    status_code=404,
                    details={"path": f"/{path}"},
                )
            index = frontend / "index.html"
            if not index.is_file():
                raise AppError("FRONTEND_NOT_BUILT", "The frontend build is missing.", status_code=503, suggested_action="Run setup.bat.")
            return FileResponse(index)

    return app


def cli() -> None:
    settings = Settings.from_env()
    # Passing the already-created app avoids importing this module a second time. Access
    # logging is disabled because the bootstrap token is carried in the initial URL.
    # ws_ping_interval is set to None to avoid concurrency collisions during high-rate image streaming.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=False,
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )


if __name__ == "__main__":
    cli()
