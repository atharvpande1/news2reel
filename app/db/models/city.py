from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime


class City(Base):
    """A city a newsroom covers, and the unit sources are onboarded under.

    Archived, never hard-deleted — Source.city_id points here forever. See
    CLAUDE.md's invariants.
    """

    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sources: Mapped[list["Source"]] = relationship(back_populates="city", lazy="raise_on_sql")  # noqa: F821

    # Display form, shown to people and used to title the carousel prompt.
    name: Mapped[str] = mapped_column(String, nullable=False)

    # Casefolded match key. `/city/<slug>/` URL segments are compared against
    # this, so it is what ingest resolves an article's city against — kept
    # separate from `name` because one is for matching and one is for reading,
    # and conflating them is what forced a .title() call on the prompt and
    # printed "nagpur" in the UI.
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)

    state: Mapped[str | None] = mapped_column(String, default=None)

    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
