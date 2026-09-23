from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.core.enums import EngagementLevel


class EngagementRead(BaseModel):
    """The seven engagement dimensions, in weight order.

    Present as a whole or not at all — `StoryRead.engagement` is null for an
    unrated article rather than this object carrying nulls. A story is rated iff
    all seven levels are present and inside the enum; see
    `services/rank.py::engagement_levels`, which is the one place that decides.
    """

    emotional_salience: EngagementLevel
    audience_breadth: EngagementLevel
    impact: EngagementLevel
    novelty: EngagementLevel
    human_interest: EngagementLevel
    timeliness: EngagementLevel
    visual_potential: EngagementLevel


class StoryRead(BaseModel):
    """The render contract phase 2's renderer and phase 3's editor UI consume.

    A projection over Article, not a table: `score` is derived per request, so
    nothing here can drift from the article it describes. `images` is carried for
    phase 2's renderer even though the feed no longer shows thumbnails.

    Nothing here reports relevance. Every story the feed returns has passed the
    admission test, so a field saying so would print the same value on every card;
    a story that failed is simply absent. See CLAUDE.md's invariants.
    """

    id: str
    title: str
    # Sanitised and capped for display — the card shows it under the headline.
    # Nullable because 14% of real articles arrive without one; no article body
    # is fetched, so this is the only article text there is.
    summary: str | None
    url: str
    published_at: datetime

    source_name: str
    publisher_group: str

    # The slug of the city named in the article's URL, and its id — an ingested
    # fact, not a claim about what the story is about. `city` predates the cities
    # table and keeps its meaning; `city_id` is NULL when the URL named no city,
    # or one not onboarded.
    city: str | None
    city_id: int | None
    # Null until the classifier reaches it. A filter, never a ranking signal.
    category: str | None

    # The engagement score on a 1-10 scale: the seven dimensions below, weighted
    # and stretched from 0-1 so the number a card prints reads as a rating. 87
    # reachable values, and the floor of 1.0 before the classifier answers —
    # which is why the card gates on `engagement`, never on this.
    score: float
    # The seven levels the score was computed from, or null when the classifier
    # has not rated this article yet.
    #
    # One nullable object rather than seven nullable fields, and that is the
    # point of the shape: this is the *single* gate on whether a story is rated.
    # The old gate was one column, checked in four places with three different
    # idioms, and it worked only because that column happened to be null exactly
    # when the story was unrated. Seven columns have no such column, and
    # nominating one would make the gate a claim the schema cannot keep.
    #
    # It also carries what the card's hover needs. An unrated story and an
    # all-very_low story both score 1.0, so `score` alone cannot tell "we have
    # not looked" from "we looked and it is the floor" — this can.
    engagement: EngagementRead | None
    # Kept as the ingested fact, no longer scored: feeds are almost all ordered
    # by publish time, so position repeats the date rather than adding to it.
    feed_position: int | None

    images: list[dict]
    sensitivity_flags: list[str]

    # "in_draft" | "downloaded" | None — whether a carousel currently holds this
    # story. Added beside the render contract, never over it: an older consumer
    # ignores it, and nothing else on this schema changes meaning.
    #
    # Filled per request from carousel_slides, so it means "a deck holds this
    # now" rather than "was once picked" — delete the slide and it clears.
    # Downloaded wins over in_draft: having already shipped is the stronger
    # claim on a story. Informational only; it never blocks a re-pick, because a
    # follow-up on yesterday's story is a legitimate choice the editor makes.
    carousel_state: Literal["in_draft", "downloaded"] | None = None
