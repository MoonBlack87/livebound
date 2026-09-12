"""The FastAPI application.

Backend and frontend are served from one process, and the frontend dev server
proxies ``/api`` here - which is why there is no CORS middleware anywhere: there
is never a cross-origin request to allow.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, diagnostics, scheduler, security, usage_ping, watchers
from . import jobs as job_registry
from .api import (
    images,
    llm,
    model_roots,
    oauth,
    posts,
    publish,
    resources,
    schedule,
    settings,
    setup,
    sources,
)
from .api import jobs as jobs_api
from .civitai import client as civitai_client
from .llm import profiles
from .llm import service as llm_service
from .store import runs as run_store

_logger = logging.getLogger(__name__)
_FILESYSTEM_JOB_SHUTDOWN_TIMEOUT_SECONDS = 30.0


def _initialise_database() -> None:
    diagnostics.configure()
    db.init_db()
    profiles.ensure_builtins()

    # Nothing can still be running after a restart. Posts caught mid-push are
    # returned to a resumable state - and any that may have been created on
    # CivitAI without us learning their id go to needs_reconcile, so the next
    # run looks for them instead of creating duplicates.
    recovered = run_store.recover_interrupted()
    if any(recovered.values()):
        print(f"[startup] interrupted runs cleaned up: {recovered}")


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Registration is database-free. The scheduler itself stays stopped until
    # first-start setup has finished, then the checks gate themselves on their
    # own switches as before.
    watchers.register(scheduler.scheduler)
    app.state.lifespan_started = True
    if not app.state.setup_required:
        scheduler.scheduler.start()
        usage_ping.send_at_start()

    yield

    scheduler.scheduler.stop()
    app.state.lifespan_started = False

    # Starlette has drained in-flight request handlers by this point, including
    # synchronous database backup and restore calls. Background worker threads
    # need their own bounded wait before the interpreter can stop daemon threads.
    unfinished = job_registry.wait_for_filesystem_jobs(
        _FILESYSTEM_JOB_SHUTDOWN_TIMEOUT_SECONDS
    )
    if unfinished:
        labels = ", ".join(f"{job.id} ({job.kind})" for job in unfinished)
        _logger.warning(
            "Shutdown timed out after %.1f seconds waiting for filesystem jobs: %s",
            _FILESYSTEM_JOB_SHUTDOWN_TIMEOUT_SECONDS,
            labels,
        )

    try:
        llm_service.stop_worker()
    except Exception as exc:
        # An optional worker failing to stop must not prevent application shutdown.
        _logger.warning("Could not stop the local model worker during shutdown: %s", exc)

    # After the jobs, because a push thread may still be mid-request until here.
    civitai_client.close_http_client()


def create_app() -> FastAPI:
    app = FastAPI(title="Livebound", version=config.APP_VERSION, lifespan=_lifespan)

    app.state.setup_required = config.setup_required()
    app.state.setup_lock = threading.Lock()
    app.state.lifespan_started = False
    app.state.initialise_database = _initialise_database

    def finish_setup() -> None:
        app.state.setup_required = False
        if app.state.lifespan_started:
            scheduler.scheduler.start()

    app.state.finish_setup = finish_setup

    # No login guards this API, so this middleware is the whole boundary: the
    # Host check stops DNS rebinding, the Origin check stops a page anywhere
    # from POSTing to 127.0.0.1 and deleting a post that cannot be undeleted.
    app.middleware("http")(security.guard_request)

    @app.middleware("http")
    async def _require_setup(request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"
        allowed = {"/api/health", "/api/setup"}
        if app.state.setup_required and path.startswith("/api") and path not in allowed:
            return JSONResponse(
                status_code=428,
                content={
                    "detail": {
                        "code": "setup_required",
                        "message": "Complete first-start setup before using this endpoint.",
                        "params": {},
                    }
                },
            )
        return await call_next(request)

    @app.exception_handler(db.DatabaseBusy)
    def _database_busy(request: Request, exc: db.DatabaseBusy) -> JSONResponse:
        """503, not 500: the data directory is moving and the answer is "retry"."""
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "database_busy", "message": str(exc), "params": {}}},
        )

    app.add_exception_handler(RequestValidationError, diagnostics.validation_response)
    app.add_exception_handler(Exception, diagnostics.unexpected_response)

    if not app.state.setup_required:
        try:
            _initialise_database()
        except db.MigrationBackupError as exc:
            # Uvicorn would otherwise bury the actionable startup message in an
            # import traceback before the browser can exist.
            raise SystemExit(str(exc)) from None

    for module in (
        setup,
        settings,
        oauth,
        sources,
        model_roots,
        images,
        posts,
        schedule,
        publish,
        resources,
        llm,
        jobs_api,
    ):
        app.include_router(module.router)

    # No frontend caller on purpose: this is the route an operator reaches
    # without the UI, and what test_security.py uses to exercise the Host guard.
    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "version": config.APP_VERSION, "data_dir": str(config.data_dir())}

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    dist = config.frontend_dist()
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/LICENSE", include_in_schema=False)
    def licence() -> FileResponse:
        return FileResponse(config.shipped_licence(), media_type="text/plain")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        """Serve the single-page app, or say exactly how to build it.

        An unmatched ``/api/`` path is answered as a 404 rather than with the app
        shell. A browser tab left open across an update calls endpoints that no
        longer exist, and 200 with a page of HTML turns that into a JSON parse
        error somewhere far from the cause.
        """
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content={
                    "detail": {
                        "code": "unknown_endpoint",
                        "message": f"No such endpoint: /{full_path}",
                        "params": {"path": f"/{full_path}"},
                    }
                },
            )
        index = dist / "index.html"
        if not index.exists():
            return JSONResponse(
                status_code=503,
                content={
                    "error": "The frontend has not been built yet.",
                    "fix": "cd frontend && npm install && npm run build",
                },
            )
        return FileResponse(index)


app = create_app()
