"""FastAPI application factory. Wiring of real services happens in app/main.py (A-14)."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import admin, auth, gallery, runs
from app.api.deps import AppState, get_state
from app.api.security_headers import SecurityHeadersMiddleware

__all__ = ["AppState", "create_app"]
log = logging.getLogger(__name__)


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(
        title="Paper2Proof",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.p2p = state
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "invalid request"}, status_code=400)

    @app.exception_handler(Exception)
    async def internal(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse({"error": "internal error"}, status_code=500)  # no stack traces

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        state = get_state(request)
        try:
            async with state.store.engine.connect() as conn:
                db_ok = (await conn.execute(text("SELECT 1"))).scalar() == 1
        except Exception:
            db_ok = False
        checks = {"db": db_ok, "models": state.models_ready, "replication": state.replication_ready}
        return JSONResponse({"checks": checks}, status_code=200 if all(checks.values()) else 503)

    app.include_router(auth.router)
    app.include_router(runs.router)
    app.include_router(gallery.router)
    app.include_router(admin.router)
    return app
