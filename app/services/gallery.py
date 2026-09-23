"""The Gallery's list projection: every carousel, most recently worked on first.

Separate from services/carousel.py because it answers a different question.
That module builds and loads one deck; this one summarises many without
loading any of them — a Gallery of forty carousels must not be forty deck
reads, so the slide count comes back aggregated and no slide is read at all.
The card shows a carousel icon rather than a rendered slide, which is what lets
this stay one query.

Every city, deliberately: the Gallery is an archive rather than a feed, and the
work you are looking for is as likely to be in the city you covered last week.
It carries its own city filter; the masthead picker belongs to Discover. See
CLAUDE.md.

Unfiltered, too. The browser holds the whole list and narrows it there, which is
what makes the chips' counts — "All (12) / Draft (4) / Downloaded (8)" — free:
those totals have to hold whichever filter is active, so the client needs every
row regardless.

Formatting is not done here either. The server sends instants; the client
renders them in the newsroom's timezone, the same split date_label makes.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.carousel import Carousel, CarouselSlide
from app.db.models.city import City
from app.schemas.carousel import CarouselSummary


async def list_carousels(db: AsyncSession, settings: Settings) -> list[CarouselSummary]:
    """Every carousel, most recently worked on first.

    Ordered on last_edited_at coalesced with created_at — the card prints that
    same value, so the dates read straight down the grid. A shown date that is
    not the sort key looks like a sorting bug. NULL until the first autosave,
    hence the coalesce rather than a plain column sort.

    ponytail: returns everything, unpaged. That is what makes the chip counts
    free, and a newsroom builds a few carousels a day. Page it when the response
    gets heavy — the client-side filter would then need server support too.
    """
    worked_on = func.coalesce(Carousel.last_edited_at, Carousel.created_at)
    counts = (
        select(CarouselSlide.carousel_id, func.count().label("n"))
        .group_by(CarouselSlide.carousel_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Carousel, City, func.coalesce(counts.c.n, 0))
            .join(City, Carousel.city_id == City.id)
            .outerjoin(counts, counts.c.carousel_id == Carousel.id)
            # id descending only to break ties — two carousels can share a
            # timestamp, and an unstable order would shuffle the grid on reload.
            .order_by(worked_on.desc(), Carousel.id.desc())
        )
    ).all()
    if not rows:
        return []

    return [
        CarouselSummary(
            id=carousel.id,
            city_id=city.id,
            city_name=city.name,
            state=carousel.state,
            slide_count=count,
            created_at=carousel.created_at,
            last_edited_at=carousel.last_edited_at,
            last_downloaded_at=carousel.last_downloaded_at,
        )
        for carousel, city, count in rows
    ]
