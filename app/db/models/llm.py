from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime


class LlmCall(Base):
    """One row per LLM call, successful or not. Cost per run is a first-class
    number and margin depends on it — see CLAUDE.md's observability section."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)

    purpose: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "score_cluster"
    model: Mapped[str] = mapped_column(String, nullable=False)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)  # "ok" | "retried" | "failed"
    error: Mapped[str | None] = mapped_column(String, default=None)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )


class LlmScoreCache(Base):
    """Cluster scores cached by content_hash, independent of run — so a
    retried run, or an unchanged cluster reappearing tomorrow, is near-free
    rather than a full-price call. See CLAUDE.md's LLM funnel invariant."""

    __tablename__ = "llm_score_cache"

    content_hash: Mapped[str] = mapped_column(String, primary_key=True)
    model: Mapped[str] = mapped_column(String, nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
