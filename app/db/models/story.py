from datetime import datetime

from sqlalchemy import JSON, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.core.enums import ScoringStatus, StoryStatus
from app.db.base import Base
from app.db.types import UTCDateTime


class Story(Base):
    """A cluster — the render candidate. Strict: anything the renderer needs
    is required, or the story is not render-eligible. See CLAUDE.md."""

    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)

    member_article_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)

    # Distinct publisher_groups AFTER syndication collapse — the primary
    # ranking signal. raw_masthead_count (before collapse) is kept for
    # debugging only; never rank on it.
    effective_masthead_count: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_masthead_count: Mapped[int] = mapped_column(Integer, nullable=False)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # Mean pairwise combined similarity within the cluster — the density
    # check that rejects single-linkage chaining. See cluster.py.
    cluster_density: Mapped[float] = mapped_column(Float, nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    latest_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    headline: Mapped[str] = mapped_column(String, nullable=False)
    # {name, admin_level} — required; a story that can't be localized scores
    # down rather than being render-eligible with an empty locality.
    locality: Mapped[dict] = mapped_column(JSON, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)

    # {local_relevance, consequence, human_interest, novelty, visual_potential,
    # composite} — not "virality"/"shock"; every axis is one an editor can
    # argue with. See CLAUDE.md's domain rules.
    scores: Mapped[dict] = mapped_column(JSON, nullable=False)
    score_reasons: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Union of an LLM pass and a deterministic rule pass. Never folded into a
    # score — scores rank, flags inform.
    sensitivity_flags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    sensitivity_source: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)

    # {has_usable_image, has_number, has_locality, is_discrete_event, score} —
    # computed, not LLM-scored.
    videoability: Mapped[dict] = mapped_column(JSON, nullable=False)

    # [{source_name, publisher_group, url, feed_position}]
    sources: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    # [{url, w, h, credit, eligible, reject_reason}]
    images: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    scoring_status: Mapped[str] = mapped_column(
        String, default=ScoringStatus.SCORED, nullable=False
    )
    status: Mapped[str] = mapped_column(String, default=StoryStatus.NEW, nullable=False)

    # Editor overrides — null until the phase 3 editor UI writes them. Never
    # confuse these with the immutable RunSelectionSnapshot defaults.
    selected: Mapped[bool | None] = mapped_column(default=None)
    order: Mapped[int | None] = mapped_column(Integer, default=None)

    # Reserved for phase 2 generation outputs — defined now so the render
    # contract doesn't churn when phase 2 lands.
    hook: Mapped[str | None] = mapped_column(String, default=None)
    facts: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
