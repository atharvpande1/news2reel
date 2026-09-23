"""The rank. Pure — no DB, no LLM, no network.

Nothing here is persisted: the score is recomputed on every request from
columns already loaded, so changing a weight in config takes effect immediately
with no backfill. See CLAUDE.md's domain rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.config import Settings
from app.core.enums import ENGAGEMENT_DIMENSIONS, EngagementLevel

# What each level is worth, 0-1. A module constant and not config: these four
# numbers *are* the enum's meaning, and as tunables they would be an invitation
# to add a middle value by editing a float — which is the one thing the enum's
# shape exists to prevent. The count of reachable scores is a property of these
# exact values, so "tidying" 0.33/0.67 to 1/3 and 2/3 changes the answer.
LEVEL_VALUES: dict[EngagementLevel, float] = {
    EngagementLevel.VERY_LOW: 0.00,
    EngagementLevel.LOW: 0.33,
    EngagementLevel.HIGH: 0.67,
    EngagementLevel.VERY_HIGH: 1.00,
}


class HasEngagement(Protocol):
    """What `engagement_levels` needs off an Article — spelled out so rank.py
    keeps importing no models and stays pure."""

    emotional_salience: str | None
    audience_breadth: str | None
    impact: str | None
    novelty: str | None
    human_interest: str | None
    timeliness: str | None
    visual_potential: str | None


def engagement_weights(settings: Settings) -> dict[str, float]:
    """Dimension -> weight, in weight order. The weights sum to 1.0, which
    Settings enforces at startup."""
    return {name: getattr(settings, f"rank_weight_{name}") for name in ENGAGEMENT_DIMENSIONS}


def engagement_levels(article: HasEngagement) -> dict[str, EngagementLevel] | None:
    """The seven levels, or None if this article is not rated.

    **Rated means all seven, present and inside the enum.** Not "any" and not
    "some with the rest defaulted": a partial row scored with its gaps at zero
    prints a confident low number on a card, and nothing on that card says the
    number is a gap rather than a verdict. Whole-row-or-nothing makes the unrated
    state one thing instead of seven.

    In normal operation the question does not arise — the strict schema makes all
    seven required, pydantic revalidates them, and classify.py writes them in one
    transaction. It arises at the v2->v3 seam, where every pre-existing row has
    seven NULLs, and after a rolled-back deploy. Both resolve on their own: every
    classifier-schema migration resets `category_attempts`, so a stale row is
    re-rated within one drain rather than limping along mis-scored.

    This is the single gate. `project_story` builds StoryRead.engagement from it
    and `RankInputs` scores from it, so the thing the card shows and the thing
    the sort uses cannot disagree — the same "one implementation" argument
    `project_story` itself rests on.
    """
    levels = {}
    for name in ENGAGEMENT_DIMENSIONS:
        stored = getattr(article, name)
        if stored is None:
            return None
        try:
            levels[name] = EngagementLevel(stored)
        except ValueError:
            # A value written by a newer deploy that was then rolled back. Treat
            # the row as unrated rather than scoring the rest and quietly
            # dropping this dimension's contribution to zero.
            return None
    return levels


@dataclass
class RankInputs:
    published_at: datetime
    # None when the article is not rated — see engagement_levels.
    levels: dict[str, EngagementLevel] | None
    # Only ever a tiebreak. Both sort keys end on it so that ties order the same
    # way on every request — the window is loaded with no ORDER BY, and an
    # unstable tail silently duplicates or skips rows across limit/offset pages.
    article_id: str


# The score is printed on every card, so it is expressed on a scale a reader
# already knows how to hold: 1 is the floor, 10 the ceiling, nothing below.
_SCORE_MIN = 1.0
_SCORE_MAX = 10.0


def article_score(inputs: RankInputs, settings: Settings) -> float:
    """The engagement score, 1-10: how well this story would work as an
    Instagram post, judged from its title and summary alone.

    Seven weighted dimensions — emotional salience, audience breadth, impact,
    novelty, human interest, timeliness and visual potential. Deliberately not
    "how much does this ask of a resident", which is the question the single
    impact tier used to answer and which sank exactly the wrong things: a
    water-cut notice is maximally actionable and makes a dull carousel.
    Actionability survives as one dimension of seven.

    An unrated article scores the floor rather than a provisional guess. There is
    nothing to guess *from*, and a middling default would rank a story nobody has
    looked at above one the model actually judged dull. The card hides the score
    entirely while `engagement` is null, so the floor never reaches a reader as a
    verdict.

    The weights sum to 1.0 and every component is 0-1, so the sum is 0-1; that is
    then stretched onto 1-10 for display. The stretch is linear and monotonic, so
    it changes no ordering — it exists so the number on a card reads as a rating
    rather than a probability. Rounded to one decimal because seven four-value
    inputs reach 87 distinct scores, where three tiers reached three and decimals
    would have been noise dressed as precision.

    No recency term: a decay rate is a free parameter there is no data to set,
    and it would trade against the score invisibly. No feed-position term —
    feeds are almost all ordered by publish time, so position repeats the date
    rather than adding to it. No image term: `visual_potential` asks whether the
    *event* is photogenic, which is a fact about the story; whether this
    particular feed attached a photo is a fact about the publisher.
    """
    if inputs.levels is None:
        return _SCORE_MIN
    weights = engagement_weights(settings)
    unit = sum(weights[name] * LEVEL_VALUES[level] for name, level in inputs.levels.items())
    return round(_SCORE_MIN + unit * (_SCORE_MAX - _SCORE_MIN), 1)


def score_contributions(inputs: RankInputs, settings: Settings) -> list[tuple[str, float]]:
    """Each dimension's share of this article's score, largest first — what the
    card's hover names. Empty for an unrated article.

    Kept here beside `article_score` rather than in the serializer: it is the
    same arithmetic read a different way, and two copies would let the
    explanation disagree with the number it explains.
    """
    if inputs.levels is None:
        return []
    weights = engagement_weights(settings)
    scored = [(name, weights[name] * LEVEL_VALUES[level]) for name, level in inputs.levels.items()]
    return sorted(scored, key=lambda pair: pair[1], reverse=True)


# No badge helper: the card shows the score itself. A threshold would be a
# second opinion about the same number, and one more thing to keep in step with
# it. The card gates on `engagement` instead — an unrated story has no score at
# all, only the floor, and must not display it. See CLAUDE.md's badge rule.


def sort_key(inputs: RankInputs, settings: Settings) -> tuple:
    """`sort=recent`, the default. Sort descending: publish date, then score,
    then id purely to keep ties stable across paged requests.

    Feed timestamps are near-unique (measured on real data, 149 distinct values
    across 151 articles), so the score decides almost nothing here — that is the
    accepted cost of reading chronologically, and the point of it: under a date
    sort, position is uncorrelated with score, so which stories the editor picks
    is an unbiased read on whether the score works at all.
    """
    return (inputs.published_at, article_score(inputs, settings), inputs.article_id)


def engagement_sort_key(inputs: RankInputs, settings: Settings) -> tuple:
    """`sort=engagement`. Descending on (score, published_at, id) — the score
    alone, with the date only breaking ties between equal scores.

    With 87 reachable values this is genuinely score-driven; under the single
    impact tier it had three, so the date tiebreak carried most of the ordering
    and the sort mostly re-read as chronological within a tier.

    Deliberately not bucketed by day. A 47-hour-old story can top this list;
    every card carries its publish time, and bucketing made the order read as a
    jumble whenever a day boundary fell mid-page.
    """
    return (article_score(inputs, settings), inputs.published_at, inputs.article_id)
