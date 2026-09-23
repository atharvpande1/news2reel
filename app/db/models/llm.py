from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime


class LlmCall(Base):
    """One row per LLM call, successful or not. Cost per day is a first-class
    number and margin depends on it — aggregate over created_at. See
    CLAUDE.md's observability section."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    purpose: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "classify"
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
