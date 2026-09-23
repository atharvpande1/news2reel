from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime


class Article(Base):
    """Ingestion normalisation target — lenient, most columns nullable, since
    it mirrors messy feed reality. See CLAUDE.md: Article is lenient, Story is
    strict."""

    __tablename__ = "articles"

    # sha256(canonical_url + "\n" + normalized_title) — deliberately NOT
    # source-scoped, so the same story arriving from a second masthead collides
    # with the first and is skipped. This column IS the dedupe ledger: a row's
    # absence is what makes a story eligible. See CLAUDE.md's invariants.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    source: Mapped["Source"] = relationship(lazy="raise_on_sql")  # noqa: F821

    url: Mapped[str] = mapped_column(String, nullable=False)
    canonical_url: Mapped[str] = mapped_column(String, nullable=False)

    title: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str | None] = mapped_column(String, default=None)

    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    fetched_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    language: Mapped[str] = mapped_column(String, nullable=False)

    # Position in the feed at fetch time. Not a rank input (feeds are ordered
    # by publish time, so it repeats the date); carried on StoryRead.
    feed_position: Mapped[int | None] = mapped_column(Integer, default=None)

    # [{url, width, height, credit, caption}]. Dimensions are feed-declared and
    # NOT verified — see videoability in docs/plan-phase-1.md step 5.
    images: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    content_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Parsed from a /city/<slug>/ URL segment, casefolded; NULL when the URL
    # carries no city. Kept even though city_id exists: it is the ingested fact,
    # and it is the only thing that lets a city onboarded later be backfilled
    # against articles already stored.
    city: Mapped[str | None] = mapped_column(String, default=None)

    # The city this article is about — `city` resolved against cities.slug at
    # ingest. NULL when the URL named no city, or one not onboarded. An ingested
    # fact, not a judgement: whether the story is *about* the feed's city is
    # `is_city_relevant` below, which the model answers off the text rather than
    # off a URL convention 78% of articles don't follow.
    city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"), default=None)

    # Assigned by the batched classifier from the title alone. NULL until
    # classified; a story serves fine without it. Filter only, never a ranking
    # signal — see CLAUDE.md's invariants.
    category: Mapped[str | None] = mapped_column(String, default=None)

    # Written together with category by the classifier: does the article's
    # primary subject concern the Source's city? This is the feed's admission
    # test, not a ranking signal. NULL until classified, and a NULL is *shown* —
    # the test is fail-open, so a story is dropped only on a confident no, and
    # the confidence below gates that removal. See CLAUDE.md's invariants.
    is_city_relevant: Mapped[bool | None] = mapped_column(Boolean, default=None)
    relevance_confidence: Mapped[float | None] = mapped_column(Float, default=None)

    # The article's FORM, not its importance — news, opinion, advice_tips,
    # promotional, celebrity_entertainment, explainer, oddity, other. The feed's
    # other admission test: a dull story is still news and stays, a listicle
    # goes. NULL until classified, and a NULL is shown, since there is nothing
    # deterministic to guess a form from. is_discoverable is derived from this
    # against settings.discoverable_content_types, never stored, so retuning the
    # set needs no backfill. See CLAUDE.md's invariants.
    content_type: Mapped[str | None] = mapped_column(String, default=None)

    # The seven engagement dimensions, in weight order — how appealing this story
    # would be as an Instagram post, judged from title and summary alone. Each is
    # an EngagementLevel (very_low / low / high / very_high), written by the same
    # classifier call, and together they are the whole engagement score.
    #
    # Stored as the enums the model returned, never as the weighted score: an
    # article is classified once and never revisited inside the window, so
    # collapsing on write would make the weighting permanent. Seven columns rather
    # than one JSON blob for one reason — every scoring axis has to be tunable
    # against a *measured distribution*, and `select novelty, count(*) ... group
    # by 1` must stay a one-liner. See CLAUDE.md's invariants.
    #
    # All seven or none: the classifier writes them in one transaction off a
    # strict schema that makes all seven required, and rank.py treats a partial
    # row as unrated rather than scoring its gaps at zero.
    emotional_salience: Mapped[str | None] = mapped_column(String, default=None)
    audience_breadth: Mapped[str | None] = mapped_column(String, default=None)
    impact: Mapped[str | None] = mapped_column(String, default=None)
    novelty: Mapped[str | None] = mapped_column(String, default=None)
    human_interest: Mapped[str | None] = mapped_column(String, default=None)
    timeliness: Mapped[str | None] = mapped_column(String, default=None)
    visual_potential: Mapped[str | None] = mapped_column(String, default=None)

    # Bounded retry for the classifier, so a title the model keeps refusing
    # isn't re-sent every pass for the article's whole life in the window.
    category_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    # Rule-based only since the LLM scoring pass was removed. Never folded into
    # the rank — scores order, flags inform.
    sensitivity_flags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
