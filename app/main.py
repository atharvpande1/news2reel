import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.endpoints import health
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.classify import classifier_loop
from app.services.scheduler import scheduler_loop

logger = logging.getLogger(__name__)

# Resolved from __file__, not a relative path: uvicorn can be started from
# anywhere and the mount must not depend on the working directory.
_STATIC_DIR = Path(__file__).resolve().parent / "static"


class _RevalidatedStatics(StaticFiles):
    """StaticFiles sends an ETag and Last-Modified but no Cache-Control, which
    leaves the browser free to cache heuristically — and it does, hardest of all
    for ES modules. The symptom is brutal to diagnose: index.html and the
    stylesheet come back fresh while the JS keeps running a previous version,
    so the page renders as new markup driven by old code.

    `no-cache` does not mean "don't cache", it means "revalidate first". The
    ETag is still there, so an unchanged file costs one 304 and no body.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        response_headers.setdefault("cache-control", "no-cache")
        return super().is_not_modified(response_headers, request_headers)

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("cache-control", "no-cache")
        return response


# Every response, API and UI alike. Holds because the UI has no inline script,
# no style attributes and no third-party images; Google Fonts is the one outside
# origin. See CLAUDE.md's "External input is hostile". HSTS is nginx's, since it
# only means anything over TLS.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


def _build_lifespan(start_background: bool):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # app.state.session_factory, not a hardcoded SessionLocal import: both
        # loops run outside FastAPI's dependency injection (lifespan has no
        # Depends()), so they wouldn't otherwise honour a test's get_db
        # override — they would silently touch the real on-disk DB from every
        # test that spins up the app.
        settings = get_settings()
        tasks = []
        if start_background:
            if settings.scheduler_enabled:
                tasks.append(
                    asyncio.create_task(scheduler_loop(app.state.session_factory, settings))
                )
            if settings.classify_enabled:
                # classifier_loop returns immediately (with a warning) when no
                # API key is configured — the fetch loop must still run.
                tasks.append(
                    asyncio.create_task(classifier_loop(app.state.session_factory, settings))
                )

        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

    return lifespan


def create_app(session_factory=SessionLocal, *, start_background: bool = True) -> FastAPI:
    """`start_background=False` is how the test suite keeps the app from
    polling real feeds and spending money on classification; production relies
    on settings.scheduler_enabled / classify_enabled."""
    # The interactive docs are a map of every endpoint; off unless asked for,
    # which only .env.local does.
    docs = get_settings().api_docs
    app = FastAPI(
        title="Feedcast",
        lifespan=_build_lifespan(start_background),
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.include_router(health.router)
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        """The UI is the product; / is what someone types. Without this they
        get a bare 404 on a server that is working fine."""
        return RedirectResponse("/ui/")

    # Mounted last and under an explicit prefix, so it can never shadow
    # /api/v1 or /health. html=True serves index.html for /ui/.
    app.mount("/ui", _RevalidatedStatics(directory=_STATIC_DIR, html=True), name="ui")
    return app


app = create_app()
