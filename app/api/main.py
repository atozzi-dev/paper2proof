"""FastAPI application factory. Wiring of real services happens in app/main.py (A-14)."""

import logging
from pathlib import PurePath

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from app.api import admin, auth, gallery, runs
from app.api.deps import AppState, get_state
from app.api.security_headers import (
    BodySizeLimitMiddleware,
    OriginCheckMiddleware,
    SecurityHeadersMiddleware,
)

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
    # add_middleware puts each new middleware outside the previous ones: headers outermost,
    # so 403/413/500 responses produced by the inner guards carry them too.
    app.add_middleware(OriginCheckMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "invalid request"}, status_code=400)

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
        body = {"checks": checks, "commit": state.settings.p2p_build_commit}  # deploy checks it
        return JSONResponse(body, status_code=200 if all(checks.values()) else 503)

    app.include_router(auth.router)
    app.include_router(runs.router)
    app.include_router(gallery.router)
    app.include_router(admin.router)
    dist = getattr(state, "web_dist", None)  # openapi_export builds the app without a state
    if dist is not None and (dist / "index.html").is_file():
        app.mount("/", WebFiles(directory=dist, html=True), name="web")  # after every route
    return app


class WebFiles(StaticFiles):
    """The built UI. Hashed assets never change (cached for good); index.html always
    revalidates, so a deploy is visible at once. The CSP comes from SecurityHeadersMiddleware."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        first = PurePath(path).parts[:1]  # an OS path (backslashes on Windows)
        immutable = first == ("assets",) and response.status_code == 200
        response.headers["cache-control"] = (
            "public, max-age=31536000, immutable" if immutable else "no-cache"
        )
        return response
