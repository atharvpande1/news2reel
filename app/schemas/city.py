from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.schemas.source import SourceCreate, SourceRead


def normalize_slug(value: str) -> str:
    """The match key for a city.

    `ingest.extract_city` casefolds the `/city/<slug>/` URL segment, and the
    article-to-city resolution is an equality test against this — so a city
    stored as "Nagpur" would match nothing. Normalise at the write boundary so
    the two sides cannot drift.
    """
    return value.strip().casefold()


CitySlug = Annotated[str, Field(min_length=1), AfterValidator(normalize_slug)]


class CityCreate(BaseModel):
    """A city and its feeds, onboarded together.

    `sources` is required and bounded: a city with no feeds silently produces
    nothing, and a feed is only ever created under a city — here, or through
    POST /cities/{id}/sources afterwards. There is no standalone POST /sources.
    The upper bound is re-checked in the service against
    settings.max_sources_per_city — this one is the schema's own guard rail so
    an oversized payload is rejected before any of it is written.
    """

    name: str = Field(min_length=1)
    state: str | None = None
    sources: list[SourceCreate] = Field(min_length=1, max_length=20)


class CityUpdate(BaseModel):
    """Name and state only. The slug is not editable: it is the key articles
    were already resolved against, so changing it would orphan them."""

    name: str | None = Field(default=None, min_length=1)
    state: str | None = None


class CityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    state: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime

    # Non-archived only — this is what the cap counts and what the UI shows as
    # remaining capacity.
    source_count: int
    # Every feed the city has ever had, archived ones included, each carrying its
    # own `archived_at`. Deliberately wider than source_count: archiving a city
    # cascades to all of its feeds, so a live-only list said nothing at all about
    # an archived city. Filter on archived_at for the live ones.
    sources: list[SourceRead]


class RefreshedSourceRead(BaseModel):
    source_id: int
    name: str
    ok: bool
    new_articles: int
    # A 304 is a real outcome, not a failure: the feed confirmed nothing has
    # changed since the last poll.
    not_modified: bool
    error: str | None


class CityRefreshResult(BaseModel):
    """What a forced refresh did, per source and in total.

    `new_articles` counts rows actually inserted, so it is the honest answer to
    "did that do anything". Categories and location scopes are assigned by the
    classifier on its own tick, so they are deliberately absent here — they do
    not exist yet for anything this refresh brought in.
    """

    city_id: int
    sources_polled: int
    new_articles: int
    failed: int
    sources: list[RefreshedSourceRead]
