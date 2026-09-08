from fastapi import APIRouter

from app.api.v1.endpoints import reports, runs, sources, stories

api_router = APIRouter()
api_router.include_router(sources.router)
api_router.include_router(runs.router)
api_router.include_router(stories.router)
api_router.include_router(reports.router)
