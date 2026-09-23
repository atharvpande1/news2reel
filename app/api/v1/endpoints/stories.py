from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.deps import current_user, get_db
from app.core.config import Settings, get_settings
from app.core.enums import Category, ContentType
from app.db.models.article import Article
from app.db.models.source import Source
from app.db.types import UTCDateTime
from app.schemas.story import StoryRead
from app.services.carousel import article_states
from app.services.stories import derive_is_local, project_story

router = APIRouter(prefix="/stories", tags=["stories"], dependencies=[Depends(current_user)])


def _occurred_at():
    """published_at is nullable and plenty of feeds omit it. Filtering on it
    directly silently drops those articles, so coalesce to the fetch time."""
    return sa_func.coalesce(Article.published_at, Article.fetched_at, type_=UTCDateTime())


# Every value the enum knows. An article carrying something outside this — a row
# written by a newer deploy that was then rolled back — is treated as unjudged
# and shown, rather than silently deleted from the feed. Same degradation as
# rank.py::impact_component gives an unrecognised tier.
_KNOWN_CONTENT_TYPES = frozenset(ContentType)


def _hidden(article: Article, source: Source, settings: Settings) -> bool:
    """The admission tests: a city's feed is only news, and only about that city.

    Content type runs first, and must: both locality branches below `return`, so
    a check appended after them would never run for an unjudged row, and a
    promotional piece with a matching URL city would sail through. Junk is junk
    wherever it happened.

    An unjudged content_type is shown. Unlike locality there is nothing
    deterministic to guess a form from, and hiding NULLs would empty the feed for
    a tick after every ingest — and for the whole drain after a migration that
    resets the backlog, which reads exactly like the dead-scheduler alert.

    Then locality, in two branches, because the classifier runs on its own tick
    and NULL is the normal state of a freshly ingested row, not an error state.

    There, unclassified falls back to `derive_is_local` — the outlet flag, or the
    URL's city segment matching the source's city. A poor test (78% of articles
    carry no city segment) but a cheap one, and the alternatives were worse:
    showing every NULL made a refresh serve an unfiltered feed for a whole tick,
    which is the one state the product exists to prevent. Locality can afford
    that stand-in where form cannot, which is why the two NULLs are treated
    differently.

    Once the model has answered it takes over completely, fail-open: dropped only
    on a confident no, so a hesitant no keeps the story. `is False`, never
    `not article.is_city_relevant`, or the unclassified branch would never be
    reached. All of it in Python rather than SQL — `NOT (TRUE AND NULL)` is NULL,
    which drops a row silently, and the endpoint already loads the whole window
    to sort it.
    """
    if (
        article.content_type in _KNOWN_CONTENT_TYPES
        and article.content_type not in settings.discoverable_content_types
    ):
        return True
    if article.is_city_relevant is None:
        return not derive_is_local(article, source)
    return (
        article.is_city_relevant is False
        and (article.relevance_confidence or 0.0) >= settings.classify_confidence_floor
    )


@router.get("", response_model=list[StoryRead])
async def list_stories(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    category: list[Category] | None = Query(default=None),
    city_id: int | None = None,
    sort: Literal["recent", "engagement"] = "recent",
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> list[StoryRead]:
    """Ranked stories from the rolling window.

    The sort key is computed in Python rather than SQL because the score is
    deliberately not persisted — and at this scale that is free: a 24h window
    across a handful of feeds is hundreds of rows, so the whole window is
    loaded, ordered, then sliced.

    `sort` is a server concern, not the caller's to do afterwards: the feed
    pages through this endpoint, so re-ordering client-side would only shuffle
    the page in hand. `recent` is the default and stays that way until the
    selection log says the score has earned the top slot — see CLAUDE.md.

    Stories judged not to be about the feed's city are absent, not ranked low,
    and there is no parameter to bring them back. Before the classifier reaches
    an article that judgement is the deterministic `derive_is_local` — see
    `_hidden`.

    `carousel_state` is filled in afterwards rather than filtered on: it tells
    the editor a story has already run, and never hides it — a follow-up is a
    legitimate pick and only they can judge that.
    """
    limit = min(limit or settings.story_default_limit, settings.story_max_limit)
    window_start = datetime.now(UTC) - timedelta(hours=settings.story_window_hours)

    occurred_at = _occurred_at()
    query = (
        select(Article, Source)
        .join(Source, Article.source_id == Source.id)
        .options(joinedload(Source.city))
        .where(occurred_at >= window_start)
    )
    if category:
        query = query.where(Article.category.in_([c.value for c in category]))
    # Filtered on the source's city, not the article's own: that selects
    # everything the city's mastheads published, and the admission test below is
    # what narrows it. Filtering on Article.city would hide every story whose URL
    # carries no city segment — most of them.
    if city_id is not None:
        query = query.where(Source.city_id == city_id)

    stories = []
    for article, source in (await db.execute(query)).all():
        if _hidden(article, source, settings):
            continue
        key, story = project_story(article, source, settings, by_engagement=sort == "engagement")
        stories.append((key, story))

    stories.sort(key=lambda pair: pair[0], reverse=True)
    page = [story for _, story in stories[offset : offset + limit]]

    # One grouped query for the page, after the slice — asking about the whole
    # window would be up to 200 ids for the 24 on screen. Set rather than passed
    # into project_story: whether a deck holds a story is a fact about the
    # carousels, not about the projection, and project_story stays the one
    # implementation of the latter.
    states = await article_states(db, [story.id for story in page])
    for story in page:
        story.carousel_state = states.get(story.id)
    return page
