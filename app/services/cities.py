"""City CRUD, and the onboarding that creates a city together with its feeds.

A source is only ever created here. There is no standalone create: a city with
no feeds produces nothing and reports nothing, which is a failure that looks
exactly like a quiet news day. See CLAUDE.md's invariants.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.db.models.city import City
from app.db.models.source import Source
from app.schemas.city import CityCreate, CityUpdate, normalize_slug
from app.schemas.source import SourceCreate
from app.services.sources import get_source


class CityNotFoundError(Exception):
    pass


class DuplicateCitySlugError(Exception):
    """Two cities whose names normalise to the same slug are the same city —
    and the slug is what article URLs resolve against, so a duplicate would make
    that resolution ambiguous."""


class CityFullError(Exception):
    def __init__(self, cap: int) -> None:
        super().__init__(f"a city can hold at most {cap} sources")
        self.cap = cap


def _live_sources(city: City) -> list[Source]:
    return [source for source in city.sources if source.archived_at is None]


def source_count(city: City) -> int:
    """Non-archived only. Archiving a dead feed frees its slot — the cap is
    about how much an editor reads, not how much history we keep."""
    return len(_live_sources(city))


async def list_cities(db: AsyncSession, *, include_archived: bool = False) -> list[City]:
    stmt = select(City).options(selectinload(City.sources))
    if not include_archived:
        stmt = stmt.where(City.archived_at.is_(None))
    return list(await db.scalars(stmt.order_by(City.name)))


async def get_city(db: AsyncSession, city_id: int) -> City:
    """Sources eager-loaded (CityRead lists them, the cascades walk them), and
    populate_existing so an identity-map copy is reloaded with them."""
    city = await db.get(City, city_id, options=[selectinload(City.sources)], populate_existing=True)
    if city is None:
        raise CityNotFoundError(city_id)
    return city


async def _assert_slug_free(db: AsyncSession, slug: str) -> None:
    existing = await db.scalar(select(func.count()).select_from(City).where(City.slug == slug))
    if existing:
        raise DuplicateCitySlugError(slug)


async def create_city(db: AsyncSession, data: CityCreate, settings: Settings) -> City:
    """The city and every one of its sources, or neither.

    One commit at the end on purpose: a city that half-committed would sit there
    with fewer feeds than the editor entered and no error to explain it.
    """
    if len(data.sources) > settings.max_sources_per_city:
        raise CityFullError(settings.max_sources_per_city)

    slug = normalize_slug(data.name)
    await _assert_slug_free(db, slug)

    city = City(name=data.name.strip(), slug=slug, state=data.state)
    db.add(city)
    # Flush, not commit: city.id has to exist for the sources to reference, but
    # a failure below must still take the city with it.
    await db.flush()

    for source_data in data.sources:
        db.add(Source(city_id=city.id, **source_data.model_dump()))

    await db.commit()
    return await get_city(db, city.id)


async def update_city(db: AsyncSession, city_id: int, data: CityUpdate) -> City:
    city = await get_city(db, city_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(city, field, value)
    await db.commit()
    return await get_city(db, city.id)


async def archive_city(db: AsyncSession, city_id: int) -> City:
    """Soft delete, cascading to the city's sources.

    The cascade is the load-bearing half. The scheduler selects sources on
    `enabled AND archived_at IS NULL` and knows nothing about cities, so leaving
    them active would keep polling every feed — and keep paying the classifier —
    for a city nobody can select. Idempotent: re-archiving refreshes the stamps.
    """
    city = await get_city(db, city_id)
    now = datetime.now(UTC)
    city.archived_at = now
    for source in city.sources:
        if source.archived_at is None:
            source.archived_at = now
    await db.commit()
    return await get_city(db, city.id)


async def unarchive_city(db: AsyncSession, city_id: int) -> City:
    """The inverse of archive_city, cascade included.

    Reviving the sources is the load-bearing half, for the mirror of the reason
    archiving them is: a city back in the picker whose every feed is still
    archived produces nothing and reports nothing, which is the failure the
    "a city is onboarded with its feeds" rule exists to prevent.

    **It cannot tell why a source was archived.** A feed archived by hand in
    March and a feed the cascade took down in April look identical on the row,
    so this revives both, and a feed someone deliberately retired comes back
    polling. Recording the difference means a column on `sources` that exists
    only to serve undo — the cheaper fix is that archiving a single feed is one
    click away again. Idempotent: re-unarchiving is a no-op.
    """
    city = await get_city(db, city_id)
    city.archived_at = None
    for source in city.sources:
        source.archived_at = None
    await db.commit()
    return await get_city(db, city.id)


async def add_source(
    db: AsyncSession, city_id: int, data: SourceCreate, settings: Settings
) -> Source:
    city = await get_city(db, city_id)
    if source_count(city) >= settings.max_sources_per_city:
        raise CityFullError(settings.max_sources_per_city)

    source = Source(city_id=city.id, **data.model_dump())
    db.add(source)
    await db.commit()
    # Re-read with the city loaded: SourceRead reads city_name off it.
    return await get_source(db, source.id)
