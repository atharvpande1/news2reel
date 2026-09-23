from datetime import datetime

from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime

# The formula currently in force. Lives beside the column it is written to, so
# the next axis change has one obvious place to bump and the meanings above stay
# next to the number.
SCORE_VERSION = 3


class Selection(Base):
    """One row per story the editor put in a carousel — the only evidence the
    engagement score has.

    Building a deck is the editor committing to a selection, so this is where
    their judgement becomes readable: compare the scores they picked against the
    window they picked from, which `articles` still holds because it outlives
    the window by design. Persistent agreement means the score works; no
    relationship means it does not. Without this a new rank axis is
    unfalsifiable, which is what got the previous set deleted — see CLAUDE.md.

    A table rather than a log line: the app configures no logging handlers, so
    `stage`-tagged records go nowhere under uvicorn, and the question this
    answers is weeks of picking away. LlmCall is the precedent — accounting that
    has to survive a restart.
    """

    __tablename__ = "selections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), nullable=False, index=True)

    # Copied, not joined back through: these are what the editor was actually
    # shown. Re-deriving them later would read through whatever the weights have
    # since become and quietly rewrite the history being measured.
    #
    # `score` always has a number, including the floor an unrated story gets;
    # `engagement` is that same number gated on the story having been rated, so
    # it is the database's copy of the card's badge gate. Redundant now that
    # score_version exists, and kept anyway — it is frozen evidence, and evidence
    # is not tidied up.
    engagement: Mapped[float | None] = mapped_column(Float, default=None)
    score: Mapped[float] = mapped_column(Float, nullable=False)

    # Which formula produced `score`. The table spans three of them and nothing
    # else distinguishes them, so a comparison that crosses a seam without
    # splitting on this column is meaningless:
    #
    #   1  scope + impact, two axes      (SQLite era — backups only)
    #   2  community_impact alone        (SQLite era — backups only)
    #   3  the seven engagement dims     (current; the Postgres baseline)
    #
    # NULL when the picked story had not been rated, which is a real and common
    # case — the feed is fail-open, so unrated stories are pickable. Written from
    # the row rather than a deploy constant for exactly that reason.
    score_version: Mapped[int | None] = mapped_column(Integer, default=None)

    # Which order the feed was in when they picked. The whole argument for
    # keeping chronological the default is that it leaves position uncorrelated
    # with score; a pick made under "engagement" came from a list the score
    # had already reordered, so it is biased evidence and has to be separable.
    # NULL for a client that did not say.
    sort: Mapped[str | None] = mapped_column(String, default=None)

    # Which deck the pick went into. SET NULL, never CASCADE: tidying up the
    # Gallery must not shrink the baseline the score is measured against, and
    # feeds carry only a recent window so the picks could not be rebuilt.
    # NULL for rows written before carousels were persisted, and for any whose
    # carousel has since been deleted.
    #
    # It also sharpens the evidence: a story picked into a carousel that was
    # actually downloaded is stronger proof the score works than one picked
    # into a draft nobody finished.
    carousel_id: Mapped[int | None] = mapped_column(
        ForeignKey("carousels.id", ondelete="SET NULL"), default=None, index=True
    )

    # Shared across the rows of one build call — group by it to recover the
    # selection as the editor made it.
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
