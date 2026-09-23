from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceCreate(BaseModel):
    name: str = Field(min_length=1)
    feed_url: str = Field(min_length=1)
    language: str = Field(min_length=1)
    publisher_group: str = Field(min_length=1)
    # No `city` field: a source is created under a city (POST /cities, or
    # POST /cities/{id}/sources) and never on its own, so the city comes from
    # the route rather than the payload.
    #
    # This masthead only covers its city, so skip the /city/<slug>/ URL check
    # and treat every article it publishes as local. A local outlet has no
    # reason to put its city in its paths.
    is_local_outlet: bool = False
    enabled: bool = True
    # 1 minute to 7 days. The scheduler treats this as a lower bound anyway —
    # a feed's own cache-control: max-age can push the real interval out.
    fetch_interval_minutes: int = Field(default=30, ge=1, le=10080)


class SourceUpdate(BaseModel):
    """All fields optional — PATCH semantics. Does not include archived_at:
    archiving goes through POST /sources/{id}/archive, not a field flip, so it
    can be safely idempotent."""

    name: str | None = Field(default=None, min_length=1)
    feed_url: str | None = Field(default=None, min_length=1)
    language: str | None = Field(default=None, min_length=1)
    publisher_group: str | None = Field(default=None, min_length=1)
    # No `city_id`: a source does not move between cities. Allowing it here
    # would let a PATCH push a city past its source cap, behind the check the
    # service does on the way in.
    is_local_outlet: bool | None = None
    enabled: bool | None = None
    fetch_interval_minutes: int | None = Field(default=None, ge=1, le=10080)


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    feed_url: str
    language: str
    enabled: bool
    publisher_group: str
    city_id: int
    # Denormalised for display: every consumer that shows a source shows its
    # city, and making each one join for a string is not worth the round trip.
    city_name: str | None = None
    is_local_outlet: bool
    fetch_interval_minutes: int
    last_fetched_at: datetime | None
    consecutive_failures: int
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SourceCheckResult(BaseModel):
    ok: bool
    status_code: int | None = None
    entry_count: int | None = None
    error: str | None = None
