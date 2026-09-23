"""The rank. Three things here are silent when wrong: the rated/unrated gate —
a partial row scored with its gaps at zero prints a confident low number and
nothing says it is a gap — the weight-to-dimension mapping, where a swap
reweights the whole feed without raising, and the two sort keys, where a swapped
term reorders it."""

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest

from app.core.config import Settings
from app.core.enums import ENGAGEMENT_DIMENSIONS, EngagementLevel
from app.services.rank import (
    LEVEL_VALUES,
    RankInputs,
    article_score,
    engagement_levels,
    engagement_sort_key,
    engagement_weights,
    score_contributions,
    sort_key,
)

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


class _Article:
    """Just the seven attributes engagement_levels reads."""

    def __init__(self, **levels):
        for name in ENGAGEMENT_DIMENSIONS:
            setattr(self, name, levels.get(name))


def _all(level: str) -> _Article:
    return _Article(**{name: level for name in ENGAGEMENT_DIMENSIONS})


def _inputs(*, at=NOW, levels=None, article_id="a") -> RankInputs:
    return RankInputs(published_at=at, levels=levels, article_id=article_id)


def _rated(level: str = "high", **overrides) -> RankInputs:
    article = _Article(**{name: overrides.get(name, level) for name in ENGAGEMENT_DIMENSIONS})
    return _inputs(levels=engagement_levels(article))


# --- the gate ---------------------------------------------------------------


def test_all_seven_present_is_rated() -> None:
    assert engagement_levels(_all("high")) == dict.fromkeys(
        ENGAGEMENT_DIMENSIONS, EngagementLevel.HIGH
    )


@pytest.mark.parametrize("missing", ENGAGEMENT_DIMENSIONS)
def test_one_missing_dimension_makes_the_whole_row_unrated(missing: str) -> None:
    """Not "score the rest and treat the gap as zero". That silently sinks the
    story by whatever the missing dimension was worth, and prints the result as
    though someone had judged it."""
    article = _all("very_high")
    setattr(article, missing, None)
    assert engagement_levels(article) is None


def test_a_value_outside_the_enum_makes_the_row_unrated() -> None:
    """A level written by a newer deploy that was then rolled back. Unrated, not
    partially scored — same degradation the content-type filter gives an
    unrecognised type."""
    article = _all("high")
    article.novelty = "medium"
    assert engagement_levels(article) is None


# --- the score --------------------------------------------------------------


def test_the_score_runs_from_one_to_ten() -> None:
    settings = _settings()
    assert article_score(_rated("very_low"), settings) == 1.0
    assert article_score(_rated("very_high"), settings) == 10.0


def test_an_unrated_article_scores_the_floor() -> None:
    """And is indistinguishable by score from an all-very_low one, which is
    exactly why the card gates on `engagement` and not on the number."""
    settings = _settings()
    assert article_score(_inputs(levels=None), settings) == 1.0
    assert article_score(_rated("very_low"), settings) == 1.0


def test_levels_are_ordered() -> None:
    values = [LEVEL_VALUES[level] for level in EngagementLevel]
    assert values == sorted(values)
    assert len(set(values)) == 4


def test_there_is_no_middle_level() -> None:
    """Four values, no midpoint, so the model has to pick a side. The axis this
    lineage replaced badged 52% of the feed at its middle value."""
    assert len(EngagementLevel) == 4
    assert 0.5 not in set(LEVEL_VALUES.values())


def test_weights_sum_to_one() -> None:
    assert sum(engagement_weights(_settings()).values()) == pytest.approx(1.0)


def test_every_dimension_has_a_weight() -> None:
    assert list(engagement_weights(_settings())) == list(ENGAGEMENT_DIMENSIONS)


def test_a_heavier_dimension_moves_the_score_more() -> None:
    """The direction check: raising a .20 dimension must beat raising a .05 one.
    A weights dict wired to the wrong names scores every story plausibly and
    orders the feed wrongly, without ever raising."""
    settings = _settings()
    breadth = article_score(_rated("very_low", audience_breadth="very_high"), settings)
    visual = article_score(_rated("very_low", visual_potential="very_high"), settings)
    assert breadth > visual


def test_weights_are_configurable_without_code_change() -> None:
    settings = _settings(
        rank_weight_emotional_salience=1.0,
        rank_weight_audience_breadth=0.0,
        rank_weight_impact=0.0,
        rank_weight_novelty=0.0,
        rank_weight_human_interest=0.0,
        rank_weight_timeliness=0.0,
        rank_weight_visual_potential=0.0,
    )
    assert article_score(_rated("very_low", emotional_salience="very_high"), settings) == 10.0
    assert article_score(_rated("very_low", impact="very_high"), settings) == 1.0


def test_the_score_has_as_many_values_as_its_inputs() -> None:
    """87, where the single impact tier reached 3. That is the honest resolution
    of seven four-value inputs, and the reason the score is rounded to one
    decimal at all — the number is pinned because a weight typo or a changed
    level map moves it and nothing else would notice."""
    settings = _settings()
    combos = product(EngagementLevel, repeat=len(ENGAGEMENT_DIMENSIONS))
    scores = {
        article_score(_inputs(levels=dict(zip(ENGAGEMENT_DIMENSIONS, c, strict=True))), settings)
        for c in combos
    }
    assert len(scores) == 87
    assert min(scores) == 1.0
    assert max(scores) == 10.0


def test_contributions_rank_by_weight_times_level() -> None:
    """What the card's hover names. Same arithmetic as the score, read a
    different way, so the explanation cannot disagree with the number."""
    settings = _settings()
    contributions = score_contributions(
        _rated("very_low", visual_potential="very_high", impact="very_high"), settings
    )
    assert [name for name, value in contributions if value > 0] == ["impact", "visual_potential"]


def test_an_unrated_article_has_no_contributions() -> None:
    assert score_contributions(_inputs(levels=None), _settings()) == []


# --- the sorts --------------------------------------------------------------


def test_the_feed_is_chronological_first() -> None:
    settings = _settings()
    fresh_dull = _rated("very_low")
    old_strong = _inputs(at=NOW - timedelta(hours=5), levels=engagement_levels(_all("very_high")))
    assert sort_key(fresh_dull, settings) > sort_key(old_strong, settings)


def test_score_only_separates_stories_published_at_the_same_instant() -> None:
    settings = _settings()
    strong = _rated("very_high")
    dull = _rated("very_low")
    assert sort_key(strong, settings) > sort_key(dull, settings)


def test_engagement_sort_is_the_score_alone() -> None:
    settings = _settings()
    old_strong = _inputs(at=NOW - timedelta(hours=47), levels=engagement_levels(_all("very_high")))
    fresh_dull = _rated("very_low")
    assert engagement_sort_key(old_strong, settings) > engagement_sort_key(fresh_dull, settings)


def test_engagement_sort_breaks_equal_scores_on_recency() -> None:
    settings = _settings()
    levels = engagement_levels(_all("high"))
    fresh = _inputs(at=NOW, levels=levels)
    old = _inputs(at=NOW - timedelta(hours=3), levels=levels)
    assert engagement_sort_key(fresh, settings) > engagement_sort_key(old, settings)


def test_both_keys_break_ties_on_id_for_stable_paging() -> None:
    settings = _settings()
    levels = engagement_levels(_all("high"))
    first = _inputs(levels=levels, article_id="bbb")
    second = _inputs(levels=levels, article_id="aaa")
    assert sort_key(first, settings) > sort_key(second, settings)
    assert engagement_sort_key(first, settings) > engagement_sort_key(second, settings)


def test_weights_that_do_not_sum_to_one_are_rejected_at_startup() -> None:
    """The 1-10 stretch assumes the sum lands in 0-1. Seven independent floats
    make a typo rescale every score in the product with nothing erroring and no
    card looking wrong — just a feed ordered by a slightly different question."""
    with pytest.raises(ValueError, match=r"must sum to 1\.0"):
        _settings(rank_weight_novelty=0.9)
