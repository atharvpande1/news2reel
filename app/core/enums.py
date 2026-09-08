from enum import StrEnum


class RunStage(StrEnum):
    """See docs/plan-phase-1.md's Run state machine. Checkpointed to the DB
    after each stage so a resume skips completed work."""

    QUEUED = "queued"
    INGESTING = "ingesting"
    CLUSTERING = "clustering"
    SCORING = "scoring"
    SELECTING = "selecting"
    READY = "ready"
    FAILED = "failed"


class StoryStatus(StrEnum):
    """Cross-day dedupe outcome — never a boolean "seen" flag, since local news
    follows the same event for days and follow-ups are legitimate stories."""

    NEW = "new"
    DEVELOPING = "developing"
    REPEAT = "repeat"


class ScoringStatus(StrEnum):
    SCORED = "scored"
    FAILED = "failed"


class SensitivityFlag(StrEnum):
    """Union of an LLM pass and a deterministic rule pass — see CLAUDE.md.
    Deliberately narrow: each value must be something an editor acts on."""

    MINOR_NAMED = "minor_named"
    SEXUAL_OFFENCE_DETAIL = "sexual_offence_detail"
    SUB_JUDICE = "sub_judice"
    COMMUNAL_FRAMING = "communal_framing"
    UNVERIFIED_CASUALTY_COUNT = "unverified_casualty_count"
    NAMED_UNCONVICTED_ACCUSED = "named_unconvicted_accused"


class SensitivitySource(StrEnum):
    LLM = "llm"
    RULE = "rule"
    BOTH = "both"


class Category(StrEnum):
    """Fixed, small enum — drives per-category colour tokens in the renderer,
    so adding a value is a renderer change too. See CLAUDE.md's domain rules."""

    CRIME = "crime"
    ACCIDENT = "accident"
    CIVIC = "civic"
    POLITICS = "politics"
    BUSINESS = "business"
    EDUCATION = "education"
    HEALTH = "health"
    SPORT = "sport"
    WEATHER = "weather"
    CULTURE = "culture"
    HUMAN_INTEREST = "human_interest"
    OTHER = "other"
