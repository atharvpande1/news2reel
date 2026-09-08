from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StoryRead(BaseModel):
    """The render contract phase 2's renderer and phase 3's editor UI both
    consume via GET /api/v1/runs/{id}/stories. See CLAUDE.md: version it,
    keep it stable."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    member_article_ids: list[str]
    effective_masthead_count: int
    raw_masthead_count: int
    article_count: int
    cluster_density: float
    first_seen_at: datetime
    latest_at: datetime
    headline: str
    locality: dict
    category: str
    scores: dict
    score_reasons: dict
    sensitivity_flags: list[str]
    sensitivity_source: dict[str, str]
    videoability: dict
    sources: list[dict]
    images: list[dict]
    scoring_status: str
    status: str
    selected: bool | None
    order: int | None
    hook: str | None
    facts: list[str] | None
    created_at: datetime
