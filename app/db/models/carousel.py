"""A carousel the editor is working on, and the slides in it.

The deck used to live only in the browser's sessionStorage, on the argument
that a table here would be machinery for state with no second reader. The
Gallery is that reader, so it moved: a carousel now survives the tab, and the
canvas writes through to it.

There is no `state` column. DOWNLOADED is `last_downloaded_at IS NOT NULL` and
DRAFT is the absence of it — the timestamp is the state machine, and a column
restating it is a second copy free to drift. See CLAUDE.md's invariants.
"""

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.models.city import City
from app.db.types import UTCDateTime


class Carousel(Base):
    """One deck. Belongs to exactly one city, like everything else the UI shows.

    `created_at` is the Gallery's date folder — rendered in
    settings.carousel_timezone, not UTC, or a carousel built at 01:00 IST files
    under the previous day. The same trap services/carousel.py::date_label
    already exists to avoid.
    """

    __tablename__ = "carousels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Not null: a mixed-city carousel has no title to print on its intro slide,
    # and the Gallery is scoped to one city like every other view.
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False, index=True
    )
    # Stamped by the autosave. NULL on a deck nobody has touched since it was
    # built, which is a real and visible thing in the Gallery.
    last_edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    # Stamped only when a zip actually streamed — the state has to mean a file
    # reached someone, not that a button was pressed.
    last_downloaded_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), default=None, index=True
    )

    city: Mapped["City"] = relationship(lazy="raise_on_sql")
    slides: Mapped[list["CarouselSlide"]] = relationship(
        back_populates="carousel",
        cascade="all, delete-orphan",
        order_by="CarouselSlide.position",
        lazy="raise_on_sql",
    )

    @property
    def state(self) -> str:
        return "downloaded" if self.last_downloaded_at is not None else "draft"


class CarouselSlide(Base):
    """One slide, as the deck currently holds it.

    Mutable working state, and deliberately not the same thing as a Selection:
    slides are edited, reordered and deleted, while `selections` freezes what
    the editor was shown at pick time. The feed's "already used" badge reads
    from here precisely so that deleting a slide clears it.

    Carries only what cannot be recovered from the article: the text (which the
    editor rewrites) and `clipped`. source_name, url, category, scope and score
    all come back through project_story on the join — and score *must*, because
    it is derived from the current weights by design.
    """

    __tablename__ = "carousel_slides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    carousel_id: Mapped[int] = mapped_column(
        ForeignKey("carousels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Dense and 0-based, rewritten wholesale on every save. Not a float gap
    # scheme: the client always sends the whole deck, so there is no insert
    # between two neighbours to make cheap.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    kind: Mapped[str] = mapped_column(String, nullable=False)
    # NULL on the intro and CTA, and on a slide written by hand. Indexed
    # because the feed asks "which articles are in a deck" on every request.
    article_id: Mapped[str | None] = mapped_column(
        ForeignKey("articles.id"), default=None, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    clipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    carousel: Mapped["Carousel"] = relationship(back_populates="slides", lazy="raise_on_sql")
