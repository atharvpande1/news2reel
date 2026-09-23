"""Resolving a selection into the stories a carousel is built from.

Replaces the old prompt builder. That module existed only to hand the editor a
block of text to paste into their own ChatGPT session; the browser renders the
slides now and each one carries its story's own headline, so all that survives
here is the part that was always ours — turning a list of ids into ordered
stories.

`resolve_stories` touches the DB; `date_label` is pure, so the intro slide's
copy can be exercised without a session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import Settings
from app.core.text import clip
from app.db.models.article import Article
from app.db.models.carousel import Carousel, CarouselSlide
from app.db.models.city import City
from app.db.models.source import Source
from app.schemas.carousel import Slide, SlideWrite
from app.schemas.story import StoryRead
from app.services.stories import project_story


class NoKnownStoriesError(Exception):
    """Every requested id was unknown. A zero-slide carousel is nonsense, so
    this surfaces as a 404 rather than an empty 200 — an empty deck is exactly
    the kind of failure that reads as success."""


class TooManyStoriesError(Exception):
    def __init__(self, cap: int) -> None:
        super().__init__(f"select at most {cap} stories for one carousel")
        self.cap = cap


async def resolve_stories(
    db: AsyncSession, story_ids: list[str], settings: Settings
) -> tuple[list[StoryRead], list[str], City | None]:
    """Selected ids -> stories in slide order, the ids that had no row, and the
    city the carousel belongs to.

    Ordered by engagement, not by the order the ids arrived in: `IN (...)`
    returns rows in whatever order the planner likes, and a mis-ordered carousel
    reads perfectly fluently while being wrong. Engagement rather than the feed's
    chronological default because a carousel is a ranked artefact — the strongest
    story earns the first slide after the intro.

    The city is the selection's, not a setting's — resolved from the sources the
    stories came from. A selection spanning cities gets None rather than one of
    them picked arbitrarily, because naming the wrong city on a carousel is
    worse than naming none. The whole row comes back, not the name: a carousel
    belongs to a city by id, and the name only titles its intro slide.

    Deliberately *not* filtered to the story window. A selection lives in the
    browser and routinely outlives it; re-applying the window here would make it
    silently shrink between the feed and the canvas. The articles table outlives
    the window by design — see CLAUDE.md's invariants.
    """
    # Dedupe while preserving first-seen order: a double-click, or a selection
    # hydrated and then re-added, would otherwise produce a duplicate slide.
    wanted = list(dict.fromkeys(story_ids))
    if len(wanted) > settings.carousel_max_stories:
        raise TooManyStoriesError(settings.carousel_max_stories)

    rows = (
        await db.execute(
            select(Article, Source)
            .join(Source, Article.source_id == Source.id)
            .options(joinedload(Source.city))
            .where(Article.id.in_(wanted))
        )
    ).all()

    ranked = sorted(
        (project_story(article, source, settings, by_engagement=True) for article, source in rows),
        key=lambda pair: pair[0],
        reverse=True,
    )
    stories = [story for _, story in ranked]
    found = {story.id for story in stories}

    cities = {source.city.id: source.city for _, source in rows}
    city = next(iter(cities.values())) if len(cities) == 1 else None

    return stories, [sid for sid in wanted if sid not in found], city


def date_label(stories: list[StoryRead], settings: Settings) -> str:
    """The editorial date the intro slide carries: the newest story's own
    publication date, in the newsroom's timezone. Not datetime.now(UTC) — at
    02:00 IST that prints yesterday."""
    zone = ZoneInfo(settings.carousel_timezone)
    newest = max((s.published_at for s in stories), default=datetime.now(zone))
    return newest.astimezone(zone).strftime("%d %B %Y")


def story_slide(story: StoryRead, settings: Settings) -> Slide:
    """One story's slide. The headline is the text — no copy is written for it,
    see CLAUDE.md — clipped on a word boundary at the canvas's box size."""
    text, clipped = clip(story.title, settings.slide_text_max_chars)
    return Slide(
        kind="story",
        story_id=story.id,
        text=text,
        clipped=clipped,
        source_name=story.source_name,
        url=story.url,
        category=story.category,
        # Gated on `engagement`, not carried straight through: an unrated story
        # scores the floor, and the inspector's `typeof score === "number"` check
        # would print that 1.0 as though it were a verdict. Null means "not
        # rated" all the way to the canvas. Same gate as the feed card's.
        score=story.score if story.engagement else None,
    )


def bracket_slides(city_name: str | None, date: str) -> tuple[Slide, Slide]:
    """The intro and the CTA. Built from templates rather than feed text, which
    is why neither can be clipped."""
    where = city_name or "your city"
    return (
        Slide(
            kind="intro",
            story_id=None,
            text=f"Top news\n{where}\n{date}",
            clipped=False,
            source_name=None,
        ),
        Slide(
            kind="cta",
            story_id=None,
            text=f"Follow for daily\n{where} news",
            clipped=False,
            source_name=None,
        ),
    )


def save_deck(db: AsyncSession, carousel: Carousel, slides: list[SlideWrite]) -> None:
    """Replace the deck wholesale and stamp the edit.

    Whole-deck rather than a diff: the canvas holds the deck as one array and
    reorders it in place, so positions are rewritten dense every time and there
    is no insert-between-neighbours to make cheap. delete-orphan on the
    relationship is what drops the rows this no longer names.

    Writes no Selection rows. Editing is not picking — only the two endpoints
    where the editor chooses stories touch that table, or an autosave could
    inflate the evidence the engagement score is measured against.
    """
    carousel.slides = [
        CarouselSlide(
            position=index,
            kind=slide.kind,
            article_id=slide.story_id,
            text=slide.text,
            clipped=slide.clipped,
        )
        for index, slide in enumerate(slides)
    ]
    carousel.last_edited_at = datetime.now(UTC)


async def load_deck(db: AsyncSession, carousel: Carousel, settings: Settings) -> list[Slide]:
    """The stored deck, rehydrated with each slide's story.

    Outer joins: the brackets carry no article, and neither does a slide the
    editor wrote by hand. Everything the inspector shows comes back through
    project_story rather than out of the slide row — one implementation, and
    `score` in particular *has* to be re-derived, or re-weighting would stop
    taking effect on decks already saved.
    """
    rows = (
        await db.execute(
            select(CarouselSlide, Article, Source)
            .outerjoin(Article, CarouselSlide.article_id == Article.id)
            .outerjoin(Source, Article.source_id == Source.id)
            .options(joinedload(Source.city))
            .where(CarouselSlide.carousel_id == carousel.id)
            .order_by(CarouselSlide.position)
        )
    ).all()

    deck = []
    for row, article, source in rows:
        story = None
        if article is not None and source is not None:
            _, story = project_story(article, source, settings)
        deck.append(
            Slide(
                kind=row.kind,
                story_id=row.article_id,
                text=row.text,
                clipped=row.clipped,
                source_name=story.source_name if story else None,
                url=story.url if story else None,
                category=story.category if story else None,
                score=story.score if story and story.engagement else None,
            )
        )
    return deck


async def article_states(db: AsyncSession, article_ids: list[str]) -> dict[str, str]:
    """article id -> "downloaded" | "in_draft", for the ids that are in a deck.

    One grouped query for the whole window, not one per story. Joined through
    carousel_slides rather than selections, which is what makes the badge mean
    "a deck currently holds this" — delete the slide and the row is gone with
    it, so the badge clears.

    MAX over the download flag *is* "downloaded wins": an article sitting in a
    finished carousel and an abandoned draft reads as downloaded, because
    having already shipped is the stronger claim on it.
    """
    if not article_ids:
        return {}

    downloaded = case((Carousel.last_downloaded_at.is_not(None), 1), else_=0)
    rows = (
        await db.execute(
            select(CarouselSlide.article_id, func.max(downloaded))
            .join(Carousel, CarouselSlide.carousel_id == Carousel.id)
            .where(CarouselSlide.article_id.in_(article_ids))
            .group_by(CarouselSlide.article_id)
        )
    ).all()
    return {article_id: "downloaded" if flag else "in_draft" for article_id, flag in rows}
