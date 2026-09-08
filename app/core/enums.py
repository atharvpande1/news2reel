from enum import StrEnum


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
