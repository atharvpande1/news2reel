from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, get_db
from app.core.config import Settings, get_settings
from app.schemas.source import SourceCheckResult, SourceRead, SourceUpdate
from app.services import sources as sources_service
from app.services.sources import SourceNotFoundError

router = APIRouter(prefix="/sources", tags=["sources"], dependencies=[Depends(current_user)])


@router.get("", response_model=list[SourceRead])
async def list_sources(
    include_archived: bool = False, db: AsyncSession = Depends(get_db)
) -> list[SourceRead]:
    return await sources_service.list_sources(db, include_archived=include_archived)


# No POST here on purpose. A source is created under a city — POST /cities with
# its feeds, or POST /cities/{id}/sources — so that the per-city cap cannot be
# bypassed and no feed can exist without a city to rank it against. This route
# 405s, the same way DELETE does.


@router.get("/{source_id}", response_model=SourceRead)
async def get_source(source_id: int, db: AsyncSession = Depends(get_db)) -> SourceRead:
    try:
        return await sources_service.get_source(db, source_id)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        ) from None


@router.patch("/{source_id}", response_model=SourceRead)
async def update_source(
    source_id: int, data: SourceUpdate, db: AsyncSession = Depends(get_db)
) -> SourceRead:
    try:
        return await sources_service.update_source(db, source_id, data)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        ) from None


@router.post("/{source_id}/archive", response_model=SourceRead)
async def archive_source(source_id: int, db: AsyncSession = Depends(get_db)) -> SourceRead:
    try:
        return await sources_service.archive_source(db, source_id)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        ) from None


@router.post("/{source_id}/check", response_model=SourceCheckResult)
async def check_source(
    source_id: int, db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)
) -> SourceCheckResult:
    try:
        return await sources_service.check_source(db, source_id, settings)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        ) from None
