from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_shared_secret
from app.core.config import Settings, get_settings
from app.schemas.source import SourceCheckResult, SourceCreate, SourceRead, SourceUpdate
from app.services import sources as sources_service
from app.services.sources import SourceNotFoundError

router = APIRouter(
    prefix="/sources", tags=["sources"], dependencies=[Depends(require_shared_secret)]
)


@router.get("", response_model=list[SourceRead])
def list_sources(include_archived: bool = False, db: Session = Depends(get_db)) -> list[SourceRead]:
    return sources_service.list_sources(db, include_archived=include_archived)


@router.post("", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def create_source(data: SourceCreate, db: Session = Depends(get_db)) -> SourceRead:
    return sources_service.create_source(db, data)


@router.get("/{source_id}", response_model=SourceRead)
def get_source(source_id: int, db: Session = Depends(get_db)) -> SourceRead:
    try:
        return sources_service.get_source(db, source_id)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        ) from None


@router.patch("/{source_id}", response_model=SourceRead)
def update_source(source_id: int, data: SourceUpdate, db: Session = Depends(get_db)) -> SourceRead:
    try:
        return sources_service.update_source(db, source_id, data)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        ) from None


@router.post("/{source_id}/archive", response_model=SourceRead)
def archive_source(source_id: int, db: Session = Depends(get_db)) -> SourceRead:
    try:
        return sources_service.archive_source(db, source_id)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        ) from None


@router.post("/{source_id}/check", response_model=SourceCheckResult)
def check_source(
    source_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> SourceCheckResult:
    try:
        return sources_service.check_source(db, source_id, settings)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        ) from None
