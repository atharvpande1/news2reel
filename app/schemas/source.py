from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import Category


class SourceCreate(BaseModel):
    name: str = Field(min_length=1)
    feed_url: str = Field(min_length=1)
    language: str = Field(min_length=1)
    publisher_group: str = Field(min_length=1)
    category_map: dict[str, Category] = Field(default_factory=dict)
    enabled: bool = True


class SourceUpdate(BaseModel):
    """All fields optional — PATCH semantics. Does not include archived_at:
    archiving goes through POST /sources/{id}/archive, not a field flip, so it
    can be safely idempotent."""

    name: str | None = Field(default=None, min_length=1)
    feed_url: str | None = Field(default=None, min_length=1)
    language: str | None = Field(default=None, min_length=1)
    publisher_group: str | None = Field(default=None, min_length=1)
    category_map: dict[str, Category] | None = None
    enabled: bool | None = None


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    feed_url: str
    language: str
    enabled: bool
    publisher_group: str
    category_map: dict[str, str]
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
