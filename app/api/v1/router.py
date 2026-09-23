from fastapi import APIRouter

from app.api.v1.endpoints import auth, carousels, cities, sources, stories

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(cities.router)
api_router.include_router(sources.router)
api_router.include_router(stories.router)
api_router.include_router(carousels.router)
