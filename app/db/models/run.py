from datetime import date, datetime

from sqlalchemy import JSON, Date, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.core.enums import RunStage
from app.db.base import Base
from app.db.types import UTCDateTime

DEFAULT_TENANT_ID = "default"


class Run(Base):
    """One run = one attempt at producing a lineup for a logical news date.

    Uniqueness is a DB constraint, not a SELECT-then-INSERT check — two
    concurrent POST /runs for the same date must not both create a run and
    double the LLM spend. tenant_id is defaulted to a single value now
    (multi-tenancy is out of scope for phase 1) purely because this constraint
    is expensive to add after the fact.
    """

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "logical_date", "attempt", name="uq_run_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, default=DEFAULT_TENANT_ID, nullable=False)
    logical_date: Mapped[date] = mapped_column(Date, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    stage: Mapped[str] = mapped_column(String, default=RunStage.QUEUED, nullable=False)
    stage_started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    error: Mapped[str | None] = mapped_column(String, default=None)
    error_stage: Mapped[str | None] = mapped_column(String, default=None)

    # One current run per (tenant, logical_date) — force=true creates a new
    # attempt, retains the previous run, and moves this flag.
    is_current: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Per-source outcomes for this run: {source_id: {"ok": bool, "error": str|None}}.
    # Populated by the ingest stage; lets the run response report per-source
    # failures without aborting the run.
    source_outcomes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class RunSelectionSnapshot(Base):
    """The default lineup, written once at the end of a run and never
    mutated. Its diff against the editor's later selected/order is the only
    labelled data we get for improving the ranker — a mutable column
    protected by convention would lose that silently. See CLAUDE.md."""

    __tablename__ = "run_selection_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)

    # Ordered list of selected story ids, then the alternates, in order.
    selected_story_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    alternate_story_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
