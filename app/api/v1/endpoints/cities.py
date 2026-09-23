from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, get_db
from app.core.config import Settings, get_settings
from app.db.models.city import City
from app.schemas.city import (
    CityCreate,
    CityRead,
    CityRefreshResult,
    CityUpdate,
    RefreshedSourceRead,
)
from app.schemas.source import SourceCreate, SourceRead
from app.services import cities as cities_service
from app.services import scheduler as scheduler_service
from app.services.cities import CityFullError, CityNotFoundError, DuplicateCitySlugError

router = APIRouter(prefix="/cities", tags=["cities"], dependencies=[Depends(current_user)])


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="city not found")


def _read(city: City) -> CityRead:
    """Every source, archived ones included; `source_count` counts only live
    ones.

    The two differ on purpose. `source_count` is what the per-city cap is
    measured against and what the UI reads as remaining capacity, so a dead feed
    must not occupy a slot. The list is what the admin page draws, and it needs
    the archived rows to say anything at all about an archived city — cascade
    archiving means every one of its feeds is stamped, so filtering them here
    returned an empty list and a city that looked like it had never had a feed.
    Each row carries its own `archived_at`, so a caller that only wants the live
    ones filters on that.
    """
    return CityRead(
        id=city.id,
        name=city.name,
        slug=city.slug,
        state=city.state,
        archived_at=city.archived_at,
        created_at=city.created_at,
        updated_at=city.updated_at,
        source_count=cities_service.source_count(city),
        sources=[SourceRead.model_validate(s) for s in city.sources],
    )


@router.get("", response_model=list[CityRead])
async def list_cities(
    include_archived: bool = False, db: AsyncSession = Depends(get_db)
) -> list[CityRead]:
    found = await cities_service.list_cities(db, include_archived=include_archived)
    return [_read(city) for city in found]


@router.post("", response_model=CityRead, status_code=status.HTTP_201_CREATED)
async def create_city(
    data: CityCreate,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CityRead:
    """A city and its feeds in one transaction — the only way a source gets
    created, apart from adding one to a city that already exists."""
    try:
        return _read(await cities_service.create_city(db, data, settings))
    except DuplicateCitySlugError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a city named {exc.args[0]!r} already exists",
        ) from None
    except CityFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None


@router.get("/{city_id}", response_model=CityRead)
async def get_city(city_id: int, db: AsyncSession = Depends(get_db)) -> CityRead:
    try:
        return _read(await cities_service.get_city(db, city_id))
    except CityNotFoundError:
        raise _not_found() from None


@router.patch("/{city_id}", response_model=CityRead)
async def update_city(
    city_id: int, data: CityUpdate, db: AsyncSession = Depends(get_db)
) -> CityRead:
    try:
        return _read(await cities_service.update_city(db, city_id, data))
    except CityNotFoundError:
        raise _not_found() from None


@router.post("/{city_id}/archive", response_model=CityRead)
async def archive_city(city_id: int, db: AsyncSession = Depends(get_db)) -> CityRead:
    try:
        return _read(await cities_service.archive_city(db, city_id))
    except CityNotFoundError:
        raise _not_found() from None


@router.post("/{city_id}/unarchive", response_model=CityRead)
async def unarchive_city(city_id: int, db: AsyncSession = Depends(get_db)) -> CityRead:
    """Bring a city and its feeds back. See the service for what the cascade
    cannot distinguish."""
    try:
        return _read(await cities_service.unarchive_city(db, city_id))
    except CityNotFoundError:
        raise _not_found() from None


@router.post("/{city_id}/refresh", response_model=CityRefreshResult)
async def refresh_city(
    city_id: int,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> CityRefreshResult:
    """Poll this city's feeds right now, ignoring the schedule.

    Takes the session factory off app.state rather than a session dependency:
    it runs the scheduler's own load/fetch/persist shape, which opens a short
    session per step instead of holding one across the fetches.
    """
    try:
        refreshed = await scheduler_service.refresh_city(
            request.app.state.session_factory, settings, city_id
        )
    except scheduler_service.CityNotFoundError:
        raise _not_found() from None
    except scheduler_service.RefreshInProgressError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a refresh for this city is already running",
        ) from None

    return CityRefreshResult(
        city_id=city_id,
        sources_polled=len(refreshed),
        new_articles=sum(source.new_articles for source in refreshed),
        failed=sum(1 for source in refreshed if not source.ok),
        sources=[RefreshedSourceRead(**vars(source)) for source in refreshed],
    )


@router.post("/{city_id}/sources", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
async def add_source(
    city_id: int,
    data: SourceCreate,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SourceRead:
    try:
        return SourceRead.model_validate(
            await cities_service.add_source(db, city_id, data, settings)
        )
    except CityNotFoundError:
        raise _not_found() from None
    except CityFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
