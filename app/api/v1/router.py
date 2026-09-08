from fastapi import APIRouter

from app.api.v1.endpoints import sources

api_router = APIRouter()
api_router.include_router(sources.router)
