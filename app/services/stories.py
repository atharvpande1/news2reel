"""The Article -> StoryRead projection, and its sort key.

Extracted out of the endpoint because there are now two callers: GET /stories
renders it, and the carousel builds slides from it. The score and the display
city are derived per request rather than stored, so a second copy of these rules
would let the canvas quietly disagree with the card the editor actually clicked.

The feed's admission test is deliberately NOT applied here. This projection also
serves the carousel lookup, which must still return a story the feed has since
hidden — same reason the carousel looks stories up by PK with no window filter.
`derive_is_local` lives here because it is one of the projection's rules about an
article and its source, and one implementation is the whole point. See CLAUDE.md's
invariants.
"""

from app.core.config import Settings
from app.core.text import sanitize
from app.db.models.article import Article
from app.db.models.source import Source
from app.schemas.story import EngagementRead, StoryRead
from app.services.rank import (
    RankInputs,
    article_score,
    engagement_levels,
    engagement_sort_key,
    sort_key,
)


def derive_is_local(article: Article, source: Source) -> bool:
    """The provisional admission answer, used only while `is_city_relevant` is
    still NULL — see `_hidden` in the stories endpoint.

    A local outlet's articles are local by definition: it has no reason to put
    its city in its URLs, so the path check alone would score every one of them
    non-local. Deliberately not on StoryRead — this is an input to the filter,
    not something an editor reads or acts on.

    Compares ids, not strings. The old string form needed a defensive casefold
    on both sides, because a source row written as "Nagpur" silently matched
    nothing and every article came back non-local; an integer has no case.
    """
    return source.is_local_outlet or (
        article.city_id is not None and article.city_id == source.city_id
    )


def project_story(
    article: Article, source: Source, settings: Settings, *, by_engagement: bool = False
) -> tuple[tuple, StoryRead]:
    """Returns (sort_key, story). Sort descending on the key.

    `by_engagement` picks which key comes back — chronological by default, score
    alone when asked. The story itself is identical either way; only the ordering
    changes, so nothing the editor reads depends on the sort.
    """
    levels = engagement_levels(article)
    inputs = RankInputs(
        published_at=article.published_at or article.fetched_at,
        levels=levels,
        article_id=article.id,
    )
    story = StoryRead(
        id=article.id,
        title=article.title,
        summary=sanitize(article.summary, settings.story_summary_max_chars) or None,
        url=article.url,
        published_at=inputs.published_at,
        source_name=source.name,
        publisher_group=source.publisher_group,
        # A local outlet's articles carry no city in their URLs, so fall back to
        # the outlet's own city rather than showing nothing. Stays the slug, as
        # it has always been — StoryRead is the versioned render contract, so
        # city_id is added beside it, not over it. The ingested fact only: what
        # the story is *about* is the admission test, not this.
        city=article.city or (source.city.slug if source.is_local_outlet else None),
        city_id=article.city_id,
        category=article.category,
        score=article_score(inputs, settings),
        engagement=EngagementRead(**levels) if levels else None,
        feed_position=article.feed_position,
        images=article.images,
        sensitivity_flags=article.sensitivity_flags,
    )
    key = engagement_sort_key if by_engagement else sort_key
    return key(inputs, settings), story
