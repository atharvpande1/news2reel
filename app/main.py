from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from app.api.v1.endpoints import health
from app.api.v1.router import api_router
from app.db.session import SessionLocal
from app.services.pipeline import reconcile_stuck_runs

# How long a run may sit in a non-terminal stage before the startup
# reconciler gives up on it. See CLAUDE.md: BackgroundTasks share the web
# process's fate, so a crash or deploy mid-run needs this safety net.
_STUCK_RUN_TIMEOUT = timedelta(hours=2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # app.state.session_factory, not a hardcoded SessionLocal import: the
    # reconciler runs outside FastAPI's dependency injection (lifespan has no
    # Depends()), so it wouldn't otherwise honour a test's get_db override —
    # it would silently touch the real on-disk DB from every test that spins
    # up the app via TestClient.
    db = app.state.session_factory()
    try:
        reconciled = reconcile_stuck_runs(db, _STUCK_RUN_TIMEOUT)
        if reconciled:
            print(f"startup reconciler: marked runs {reconciled} as failed (stuck past timeout)")
    finally:
        db.close()
    yield


def create_app(session_factory=SessionLocal) -> FastAPI:
    app = FastAPI(title="news2reel", lifespan=lifespan)
    app.state.session_factory = session_factory
    app.include_router(health.router)
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
