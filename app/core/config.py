from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import ENGAGEMENT_DIMENSIONS, ContentType


class Settings(BaseSettings):
    # No env_file: compose injects .env.local / .env.prod as real environment.
    model_config = SettingsConfigDict(env_prefix="FEEDCAST_", extra="ignore")

    # postgresql+asyncpg://… — required, so a container missing it fails at start.
    database_url: str

    # Auth — see CLAUDE.md's "Auth". The secret signs every token; rotating it
    # logs everyone out, which is the only global revocation there is.
    jwt_secret: str
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    # False only in .env.local: a Secure cookie is never sent over plain http,
    # so local dev on http://127.0.0.1 could not stay logged in.
    cookie_secure: bool = True

    # /docs, /redoc and /openapi.json. Off in prod: they publish the whole API.
    api_docs: bool = False

    # core/net.py fetch limits — every outbound fetch (feed polling, source
    # checks) goes through these caps. See "External input is hostile".
    fetch_connect_timeout_seconds: float = 5.0
    fetch_read_timeout_seconds: float = 10.0
    fetch_max_response_bytes: int = 5_000_000
    fetch_max_redirects: int = 3

    # The ingestion scheduler (services/scheduler.py), started from the app
    # lifespan. Disable it to run the API without polling — the test suite
    # does this through create_app(start_background=False) instead.
    scheduler_enabled: bool = True
    # How often the loop wakes to look for due sources. This is the resolution
    # of the schedule, not the poll rate: a source with a 30m interval is still
    # polled every 30m, just checked every tick.
    scheduler_tick_seconds: int = 60
    scheduler_max_concurrent_fetches: int = 5
    # Ceiling on the exponential backoff a failing source accumulates, so a
    # dead feed settles at one attempt every few hours rather than never
    # retrying at all.
    scheduler_max_backoff_seconds: int = 21_600

    # The category classifier (services/classify.py), the second lifespan loop.
    classify_enabled: bool = True
    # How long an unclassified article sits in the feed unfiltered. The feed is
    # fail-open, so until this tick lands a city's feed is everything its
    # mastheads published, national filler included — which is the one window
    # where the product visibly isn't doing its job. Shortening it costs nothing
    # per article: the batch is one call either way, so a faster tick buys
    # smaller batches at the same price, not more of them.
    classify_tick_seconds: int = 30
    # Titles per LLM call. Held at 20 on measured evidence, not preference: the
    # worry that a long list makes the model answer by pattern did not survive a
    # sweep. Over the same 20 real headlines run three times, content_type came
    # back identical 20/20 at this batch size, and the other axes were no more
    # stable at 5 or 10 than at 20.
    #
    # What batching does buy is input tokens, and that is now the dominant cost:
    # the ~2.2k-token prompt and schema are re-sent per call, so the same 20
    # articles cost 2,864 input tokens in one call and 8,999 in four.
    classify_batch_size: int = 20
    # Bounded retry, so a title the model keeps refusing stops being re-sent
    # every pass for the article's whole window lifetime.
    classify_max_attempts: int = 3
    # Titles are untrusted third-party text entering a prompt — cap the length
    # that reaches the model. See CLAUDE.md: "External input is hostile".
    classify_title_max_chars: int = 200
    # Summaries ride along so utility is judgeable — "plan your route" reads as
    # filler until the body names the closed roads. Real summaries average ~198
    # chars, so this clips the long tail rather than the typical case.
    classify_summary_max_chars: int = 400
    # How sure the model has to be before a "not about this city" actually hides
    # the story. Self-reported confidence is poorly calibrated, so this is a
    # blunt gate rather than a multiplier — and it gates *removal* only: below
    # this, the story stays in the feed. Raising it shows more off-city stories;
    # lowering it risks withholding a local one, which is the costlier mistake.
    classify_confidence_floor: float = 0.5

    # Which content types reach the feed. The other half of the admission test:
    # a city's feed is only news, and only about that city. is_discoverable is
    # derived from this per request and never stored, so retuning it takes
    # effect on the next request with no backfill.
    #
    # Typed as the enum rather than set[str] so an unknown value is rejected at
    # startup — a typo here would otherwise filter the whole feed out silently.
    # An env override must be JSON: FEEDCAST_DISCOVERABLE_CONTENT_TYPES=
    # '["news","oddity"]'. A bare comma-separated list raises at startup.
    #
    # ODDITY is in because a viral local curiosity makes good carousel content,
    # and it is where human-interest stories went when that category was
    # dropped. EXPLAINER is out: "what the new UPI rules mean" is a service
    # piece, and this feed answers "what happened here today".
    discoverable_content_types: set[ContentType] = {ContentType.NEWS, ContentType.ODDITY}

    # GET /stories: the rolling wall-clock window and the rank weights. Nothing
    # is persisted, so re-weighting takes effect immediately with no backfill.
    # Shared with the classifier: services/classify.py bounds its backlog by the
    # same value, so narrowing this narrows what ever gets rated. That stays
    # consistent — an article too old for the feed is one nobody will see — but
    # it does mean a widening later leaves a gap of unrated older rows behind.
    story_window_hours: int = 24
    # The engagement score: a weighted sum over the seven dimensions, predicting
    # how well a story would work as an Instagram post. Every component is 0-1 and
    # these sum to 1.0, so the sum is 0-1 before the 1-10 stretch in rank.py.
    #
    # Retuning is free — nothing is persisted, so a change takes effect on the next
    # request with no backfill. That is the point of storing the levels rather than
    # the collapsed number.
    #
    # No feed-position weight: feeds are almost all ordered by publish time, so
    # position repeats the date rather than adding to it. No recency weight: a decay
    # rate is a free parameter there is no data to set.
    #
    # These are a guess until measured. The number that judges them is not the weight
    # here but each axis's share of score *variance* on real rows — impact and
    # audience_breadth correlate, so .20 each does not mean .40 of the answer. See
    # CLAUDE.md before retuning, and never against a single run: the classifier
    # disagrees with itself 25-35% of the time.
    rank_weight_emotional_salience: float = 0.20
    rank_weight_audience_breadth: float = 0.20
    rank_weight_impact: float = 0.20
    rank_weight_novelty: float = 0.15
    rank_weight_human_interest: float = 0.10
    rank_weight_timeliness: float = 0.10
    rank_weight_visual_potential: float = 0.05
    # How much of the feed summary a card shows. Third-party markup soup on the
    # way to the DOM, so it goes through core/text.sanitize like the prompt's
    # copy does — same policy, one implementation.
    story_summary_max_chars: int = 220
    story_default_limit: int = 50
    story_max_limit: int = 200

    # A city is onboarded with its feeds in one go, and this caps how many. The
    # limit is editorial, not technical: past a handful of mastheads the feed
    # stops being a scan and starts being a firehose.
    max_sources_per_city: int = 5

    # POST /carousel — the Creative Canvas. "Top news ... <date>" on the intro
    # slide is an editorial date, not a UTC one: at 02:00 IST a UTC date names
    # yesterday, which is the wrong date to print on a carousel. Validated at
    # load — an unknown zone should fail at startup, not as a 500 per request.
    carousel_timezone: str = "Asia/Kolkata"
    # Story slides per carousel. Two more slides bracket them — an intro and a
    # CTA — so this is Instagram's 10-slide sweet spot, well under its cap of 20.
    carousel_max_stories: int = 8

    # The slide's text box, in characters. Must equal TEXT_MAX in
    # app/static/js/slide-canvas.js: the browser hard-slices the deck it stores
    # at that number, so a server clipping any higher just hands it a mid-word
    # cut instead of the word-boundary one core/text.py::clip made. Widening it
    # is a canvas change too — the box is sized for roughly this much text.
    slide_text_max_chars: int = 120

    # POST /carousel/zip — the browser hands back rendered JPEGs, so this is an
    # upload boundary and gets the same treatment as core/net.py's fetch caps.
    # Two beyond carousel_max_stories, for the intro and CTA slides.
    zip_max_bytes_per_image: int = 2_000_000
    zip_max_total_bytes: int = 20_000_000

    # services/llm.py — retry with backoff, never a bare API call. No key means
    # the classifier loop never starts; the fetch loop runs regardless.
    llm_api_key: str | None = None
    # Lightest tier — filing a headline against fixed enums is a well-specified
    # classification task, not open-ended reasoning, and
    # batching makes per-call cost the thing to keep down.
    llm_model: str = "gpt-5-nano"
    # How hard the model thinks before filing a headline. Tuned against a
    # measured sweep, not a guess, and the answer *changed* when the engagement
    # score became seven graded judgements.
    #
    # `minimal` was right for four enum values — filing a headline against fixed
    # categories is not a reasoning task, every boundary is spelled out in the
    # prompt, and unset the call spent ~5,000 output tokens and 44.6s over 276
    # real calls to re-derive rules it had already been given.
    #
    # It is wrong for the seven engagement dimensions, and the failure is silent.
    # Measured on 40 real headlines, all seven axes at minimal and at low come
    # back with **zero** very_low and zero very_high — novelty was one single
    # value for 100% of them. The model hedges into the two inner levels, which
    # is the same central-tendency collapse the no-middle enum was meant to
    # prevent; removing the midpoint just moved the hedge to low/high. At medium
    # every axis reaches both extremes on 10-25% of rows. Grading "how striking
    # is this" genuinely is a judgement, unlike picking a category.
    #
    # The cost is real and was measured per 20-headline batch: minimal 7.7s /
    # 1,369 output tokens, low 4.3s / 705, medium 49.2s / 10,575. At ~21 batches
    # a day that is cents, and it buys a score that varies. Do not lower this
    # without re-running the per-axis distribution — a collapsed axis does not
    # error, it just quietly ranks everything the same.
    #
    # Empty string sends no `reasoning` at all, for a model that does not accept
    # one.
    llm_reasoning_effort: str = "medium"
    llm_max_attempts: int = 3
    llm_retry_backoff_seconds: float = 1.0

    @model_validator(mode="after")
    def _engagement_weights_sum_to_one(self) -> "Settings":
        """The 1-10 stretch assumes the weighted sum lands in 0-1. Seven
        independent floats make a typo silently rescale every score in the
        product, with nothing erroring and no card looking wrong — just a feed
        ordered by a slightly different question. Cheaper to fail at startup."""
        total = sum(getattr(self, f"rank_weight_{name}") for name in ENGAGEMENT_DIMENSIONS)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"engagement rank weights must sum to 1.0, got {total!r}")
        return self

    @field_validator("jwt_secret")
    @classmethod
    def _strong_secret(cls, value: str) -> str:
        """HS256 is only as strong as the key; a short one is brute-forceable
        offline from any captured token."""
        if len(value) < 32:
            raise ValueError("jwt_secret must be at least 32 characters")
        return value

    @field_validator("carousel_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        """Resolve once here so a typo is a startup failure rather than a 500
        on every prompt request."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
