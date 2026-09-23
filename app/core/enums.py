from enum import StrEnum


class SensitivityFlag(StrEnum):
    """Rule-based only since the LLM scoring pass was removed — see CLAUDE.md.
    Deliberately narrow: each value must be something an editor acts on."""

    MINOR_NAMED = "minor_named"
    SEXUAL_OFFENCE_DETAIL = "sexual_offence_detail"
    SUB_JUDICE = "sub_judice"
    COMMUNAL_FRAMING = "communal_framing"
    UNVERIFIED_CASUALTY_COUNT = "unverified_casualty_count"
    NAMED_UNCONVICTED_ACCUSED = "named_unconvicted_accused"


class EngagementLevel(StrEnum):
    """One level on one engagement dimension. The same four values serve all
    seven — see ENGAGEMENT_DIMENSIONS — and each is read *relative to its own
    dimension*, so VERY_HIGH visual potential and VERY_HIGH impact are not
    claims about the same magnitude.

    **There is no middle value, deliberately.** The shareability axis this
    lineage replaced badged 52% of the feed at its midpoint because the model
    never had to pick a side. Four values with no midpoint make hedging
    impossible. It also inverts the collapse signature: a no-middle enum fails
    by emptying its extremes, so an axis where VERY_LOW and VERY_HIGH together
    take under ~10% of the feed has stopped measuring. See CLAUDE.md.

    The 0 / 0.33 / 0.67 / 1.0 mapping lives in services/rank.py, not here:
    this enum is vocabulary, rank.py is arithmetic.
    """

    VERY_LOW = "very_low"
    LOW = "low"
    HIGH = "high"
    VERY_HIGH = "very_high"


# The seven dimensions of the engagement score, in weight order. Hand-copied
# into static/js/feed.js; tests/test_ui.py pins the two together, for the same
# reason it pins Category — a mismatch is browser-only and silent.
#
# This is a tuple, not an enum: the names are column names, pydantic field names
# and config keys, and every one of those is already a string. An enum here would
# be a fourth spelling of the same seven words.
ENGAGEMENT_DIMENSIONS = (
    "emotional_salience",
    "audience_breadth",
    "impact",
    "novelty",
    "human_interest",
    "timeliness",
    "visual_potential",
)


class ContentType(StrEnum):
    """What KIND of article this is — the feed's second admission test, beside
    is_city_relevant. Discoverable values live in
    settings.discoverable_content_types; is_discoverable is derived from that,
    never stored. See CLAUDE.md.

    Form, never importance. A dull story is still NEWS: "Minister reviews
    drainage work" and "onion worth Rs 40,000 stolen" are both NEWS, and sinking
    them is the engagement score's job. The moment a value here means "this is
    minor", this enum and the score answer the same question and will
    contradict each other. The test: could a Pulitzer-winning version of this
    story carry the same value? If yes it is a form; if no it is an importance
    judgement wearing a form's clothes.

    Why the axis exists at all: relevance cannot answer it. Three near-identical
    lifestyle listicles from one source in one batch came back no, YES, no,
    because "is this about Pune" has no stable answer for a piece with no city
    in it. "Is this a tips listicle" has one.
    """

    # A reported event, decision or development. The default for anything that
    # actually happened, however small.
    NEWS = "news"
    # A viral curiosity or human-interest oddity. Discoverable on purpose: it
    # makes good carousel content, and it is the home HUMAN_INTEREST vacated.
    ODDITY = "oddity"
    # Column, editorial, commentary, analysis, speculation about what may happen.
    OPINION = "opinion"
    # How-to, guide, listicle, recommendation. "How to Grow Lemons at Home".
    ADVICE_TIPS = "advice_tips"
    # Press release, sponsored piece, product or brand announcement.
    PROMOTIONAL = "promotional"
    # Film, TV, celebrity lifestyle, gossip.
    CELEBRITY_ENTERTAINMENT = "celebrity_entertainment"
    # Background or service journalism with no new event — "what the new UPI
    # rules mean". Deliberately not discoverable: the feed answers "what
    # happened here today".
    EXPLAINER = "explainer"
    # Fits none of the above. The model's escape hatch, so the prompt tells it
    # to prefer NEWS for anything that reports an event — every OTHER is a story
    # dropped from the feed, and a swelling OTHER count means the prompt is not
    # landing rather than that the feed is full of oddities.
    OTHER = "other"


class Category(StrEnum):
    """What the story is about — a filter on the editor's terms, never a ranking
    signal. Hand-copied into static/js/feed.js; tests/test_ui.py pins the two
    together, because a mismatch shows up only as a chip that filters nothing.

    Values that carry volume get a `.tag--<name>` colour in css/app.css; the
    rest fall through to the neutral base `.tag`, as `other` always has. So
    adding a value is no longer necessarily a renderer change — and the palette
    stays legible by staying small. See CLAUDE.md's domain rules for the three
    overlapping pairs the prompt has to disambiguate."""

    CRIME = "crime"
    ACCIDENT = "accident"
    CIVIC = "civic"
    # Metro, buses, traffic, railways, flyovers. Split out of CIVIC, which was
    # the fattest bucket in the feed by some way and half of it transport.
    TRANSPORT = "transport"
    POLITICS = "politics"
    BUSINESS = "business"
    EDUCATION = "education"
    HEALTH = "health"
    SPORT = "sport"
    # The forecast and its damage. Pollution and tree-felling are ENVIRONMENT.
    WEATHER = "weather"
    ENVIRONMENT = "environment"
    AGRICULTURE = "agriculture"
    CULTURE = "culture"
    # Festivals and temples, which used to land in CULTURE.
    RELIGION = "religion"
    TECHNOLOGY = "technology"
    OTHER = "other"
