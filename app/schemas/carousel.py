from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.core.config import get_settings

# Article.id is a sha256 hex digest. Capped rather than pattern-matched because
# these values round-trip back out in `missing_ids`: a selection restored from
# the browser can name anything at all, and the cap is what bounds it.
StoryId = Annotated[str, Field(min_length=1, max_length=128)]

SlideKind = Literal["intro", "story", "cta"]

# Belt to settings.slide_text_max_chars' braces. A save is an upload boundary
# like any other, and the canvas caps its own field at the same number — this
# is what stops a hand-rolled request storing a megabyte of slide text.
SLIDE_TEXT_CAP = 2_000

# Upload limits derived from config.py rather than restated: this schema once
# hardcoded its own per-image and slide-count numbers, and changing the setting
# silently did nothing. Settings are already loaded at import (db/session.py).
_settings = get_settings()
# Intro and CTA on top of the story slides.
MAX_SLIDES = _settings.carousel_max_stories + 2
# Base64 costs 4 bytes per 3, plus the "data:image/jpeg;base64," prefix.
MAX_ENCODED_IMAGE = _settings.zip_max_bytes_per_image * 4 // 3 + 64
# The service caps distinct stories at carousel_max_stories after dedupe, and
# its error names the cap; this only bounds what gets parsed on the way there.
MAX_STORY_IDS = _settings.carousel_max_stories * 4


class CarouselRequest(BaseModel):
    """The editor's selection, as article ids — the body of both the create call
    and the add-to-an-open-deck call. The upper bound on how many is
    settings.carousel_max_stories, enforced in the service so the error message
    can name the configured cap."""

    story_ids: list[StoryId] = Field(min_length=1, max_length=MAX_STORY_IDS)
    # Which order the feed was in when these were picked, recorded against the
    # selection. A pick made under "Engagement score" came from a list the score
    # had already reordered, so it is biased evidence about that same score and
    # has to stay separable from a pick made chronologically. Optional: an older
    # client, or a direct API caller, simply does not say.
    sort: Literal["recent", "engagement"] | None = None


class Slide(BaseModel):
    """One slide as the canvas first receives it. Everything here is editable in
    the browser afterwards — this is a starting point, not a contract about what
    gets exported."""

    kind: SlideKind
    # Null on the intro and CTA slides, which belong to no single story.
    story_id: str | None
    text: str
    # True when the headline was longer than the slide's text box and lost its
    # last words on the way in. The canvas badges it — a headline that quietly
    # dropped half a sentence is the silent failure here.
    clipped: bool

    # The story behind the slide, for the canvas inspector. All null on the
    # intro and CTA, and on a slide written by hand — which is what makes the
    # inspector's source block conditional without a second flag to keep in
    # step. `score` is the engagement score, 1-10.
    source_name: str | None
    url: str | None = None
    category: str | None = None
    score: float | None = None


class SlideWrite(BaseModel):
    """One slide on its way back from the canvas, for the autosave.

    Deliberately smaller than Slide: source_name, url, category and score are
    all recovered from the article on read, and score *must* be, because it is
    derived from the current rank weights. Accepting them here would let a stale
    tab write a stale score into the deck.
    """

    kind: SlideKind
    story_id: StoryId | None = None
    text: str = Field(max_length=SLIDE_TEXT_CAP)
    clipped: bool = False


class DeckWrite(BaseModel):
    """The whole deck, every save. Not a patch: the canvas holds the deck as one
    array and reorders it in place, so a diff would be the client computing
    something it does not otherwise need."""

    # The same bound the zip endpoint enforces on images.
    slides: list[SlideWrite] = Field(max_length=MAX_SLIDES)


class CarouselResponse(BaseModel):
    """One carousel, deck and all — what the canvas opens on."""

    id: int
    slides: list[Slide]
    # Ids with no article row — reported rather than fatal, so one story that
    # aged out of the browser's selection doesn't strand the rest. Always empty
    # on a plain read; only a build can miss something.
    missing_ids: list[str] = Field(default_factory=list)
    # "draft" | "downloaded", derived from last_downloaded_at rather than
    # stored. Served so no client re-implements the rule.
    state: str
    created_at: datetime
    last_edited_at: datetime | None
    last_downloaded_at: datetime | None


class CarouselSummary(BaseModel):
    """One Gallery row.

    No slide comes back with it. The card shows a carousel icon rather than a
    rendered first slide — which is what keeps the listing a single query, and
    what stops a grid of forty cards each running the canvas renderer."""

    id: int
    # The id as well as the name: the Gallery's city <select> filters on it, and
    # two cities may legitimately share a display name where the slug is unique.
    city_id: int | None
    city_name: str | None
    state: str
    slide_count: int
    created_at: datetime
    last_edited_at: datetime | None
    last_downloaded_at: datetime | None


class ZipRequest(BaseModel):
    """Rendered slides on their way back to be bundled.

    Both bounds are enforced here, before the endpoint decodes anything: a
    request that would blow the caps is rejected while it is still a string.
    The per-image cap is expressed in encoded bytes — base64 costs 4 bytes per
    3, plus the data-URL prefix.
    """

    images: list[Annotated[str, Field(min_length=1, max_length=MAX_ENCODED_IMAGE)]] = Field(
        min_length=1, max_length=MAX_SLIDES
    )
